"""The upload command: one idempotent run that publishes whatever is newly eligible.

    learn what is published (chain + stream)  →  finish an interrupted batch if there is one
    →  sync the backend  →  select eligible, unpublished entries  →  nothing new? stop
    →  encode a batch  →  journal it  →  dry run  →  send with finality  →  store own blobs
    →  report

Every step is safe to repeat. The lock on the data directory keeps two runs on one machine
apart; the contract's sequence guard keeps two runs on different machines apart.
"""

from __future__ import annotations

import importlib.metadata
import json
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from eth_account import Account
from web3 import Web3

from loculus_eternal.config import Config
from loculus_eternal.format.encode import StreamEncoder
from loculus_eternal.format.records import CODEC_ZSTD
from loculus_eternal.sources.base import SourceChain
from loculus_eternal.store import BlobStore
from loculus_eternal.upload.published import PublishedView, load_published_view
from loculus_eternal.upload.submitter import BLOB_GAS_PER_BLOB, GWEI, MAX_BLOBS_PER_TX, NOMINAL_GAS_LIMIT, FeePolicy, Journal, Refused, RevertedOnChain, SubmitError, Submitter, current_fees
from loculus_eternal.upload.sync import BackendClient, SyncStats, select_new_entries


@dataclass
class UploadReport:
    mode: str
    outcome: str                         # "published", "nothing-to-publish", "refused", "checked", "dry-run-ok", "resumed"
    chain: dict = field(default_factory=dict)
    sync: list[dict] = field(default_factory=list)
    new_entries: int = 0
    tooling_published: bool = False
    batch: dict | None = None
    dry_run: dict | None = None
    transactions: list[dict] = field(default_factory=list)
    message: str = ""
    duration_seconds: float = 0.0

    def to_json(self) -> dict:
        return self.__dict__


class Uploader:
    def __init__(self, config: Config, *, w3: Web3 | None = None, account=None, log: Callable[[str], None] = print, sleep=time.sleep, poll_interval: float | None = None):
        if config.backend is None or config.upload is None:
            raise SystemExit("configuration error: the upload command needs [backend] and [upload] sections")
        self.cfg = config
        self.log = log
        self.w3 = w3 or Web3(Web3.HTTPProvider(config.chain.rpc_url, request_kwargs={"timeout": 120}))
        self.account = account
        self.data_dir = config.upload.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        policy = FeePolicy(
            max_blob_fee_gwei=config.upload.max_blob_fee_gwei,
            max_priority_fee_gwei=config.upload.max_priority_fee_gwei,
            escalation_attempts=config.upload.escalation_attempts,
            inclusion_timeout_blocks=config.upload.inclusion_timeout_blocks,
            finality_timeout_seconds=config.upload.finality_timeout_seconds,
        )
        if poll_interval is not None:
            policy.poll_interval = poll_interval
        self.policy = policy
        self.sleep = sleep
        self.journal_path = self.data_dir / "journal.json"
        self.submitter: Submitter | None = None
        if account is not None:
            self.submitter = Submitter(self.w3, account, config.chain.contract, self.journal_path, self.data_dir / "pending", policy, log=log, sleep=sleep)
        if config.chain.chain_id is not None and self.w3.eth.chain_id != config.chain.chain_id:
            raise SystemExit(f"configuration error: the config says chain_id {config.chain.chain_id} but the RPC endpoint serves chain {self.w3.eth.chain_id}")

    @classmethod
    def with_key(cls, config: Config, key: str, **kw) -> "Uploader":
        return cls(config, account=Account.from_key(key), **kw)

    # --- pieces -------------------------------------------------------------------------------

    def _view(self, store: BlobStore) -> PublishedView:
        return load_published_view(self.w3, self.cfg.chain.contract, SourceChain(self.cfg.sources), store, deployment_block=self.cfg.chain.deployment_block, log=self.log)

    def _sync(self, view: PublishedView) -> tuple[list[dict], list[SyncStats]]:
        client = BackendClient(self.cfg.backend.url, self.data_dir / "feeds")
        entries: list[dict] = []
        stats: list[SyncStats] = []
        for organism in self.cfg.backend.organisms:
            feed = client.fetch(organism)
            new, st = select_new_entries(organism, feed, lambda acc, ver, o=organism: view.is_published(o, acc, ver))
            entries.extend(new)
            stats.append(st)
            self.log(f"{organism}: {st.total} released, {st.open} open, {st.already_published} already published, {st.new} new" + (" (feed unchanged, from cache)" if feed.from_cache else ""))
        return entries, stats

    def _tooling(self, view: PublishedView) -> list[tuple[str, bytes]]:
        """The spec and source files, when none are in the stream yet or the version changed."""
        paths = self.cfg.upload.tooling_paths
        if not paths:
            return []
        base = self.cfg.path.parent
        files = []
        for pattern in paths:
            for p in sorted(base.glob(pattern)):
                if p.is_file():
                    files.append((p.relative_to(base).as_posix(), p.read_bytes()))
        if not files:
            self.log("tooling_paths matched no files; nothing to publish as tooling")
            return []
        current_version = _version_of(dict(files)) or _installed_version()
        published_version = _version_of(view.decoded.tooling)
        # Republish unless the stream already carries tooling with a readable, identical version.
        if view.decoded.tooling and published_version is not None and published_version == current_version:
            return []
        self.log(f"tooling will be published: {len(files)} file(s), version {current_version}" + (f" (stream has {published_version})" if view.decoded.tooling else " (none in the stream yet)"))
        return files

    def _finish(self, journal: Journal, store: BlobStore, report: UploadReport) -> None:
        """A batch reached finality: keep its blobs in the local store, record, clear the journal."""
        for tx in journal.txs:
            for seq, blob in zip(tx.seqs, self.submitter.blobs_for(journal, tx)):
                store.write(seq, bytes.fromhex(tx.versioned_hashes[tx.seqs.index(seq)][2:]), blob)
            report.transactions.append(
                {
                    "index": tx.index,
                    "txHash": tx.included_tx,
                    "block": tx.included_block,
                    "blockTimestamp": tx.block_timestamp,
                    "seqs": tx.seqs,
                    "versionedHashes": tx.versioned_hashes,
                    "gasUsed": tx.gas_used,
                    "effectiveGasPrice": tx.effective_gas_price,
                    "blobGasPrice": tx.blob_gas_price,
                    "attempts": len(tx.attempts),
                    "isBatchEnd": tx.is_batch_end,
                }
            )
        done_dir = self.data_dir / "done"
        done_dir.mkdir(exist_ok=True)
        self.submitter.journal_path.replace(done_dir / f"batch-{journal.batch}-from-{journal.first_blob_seq}.json")
        for p in Path(journal.blobs_dir).glob("*.blob"):
            p.unlink()
        Path(journal.blobs_dir).rmdir()

    # --- the run --------------------------------------------------------------------------------

    def run(self, mode: str = "publish") -> UploadReport:
        """mode: "publish" (default), "dry-run" (everything but sending), "check" (counts and cost only)."""
        started = time.time()
        report = UploadReport(mode=mode, outcome="")
        with BlobStore(self.data_dir / "stream") as store:
            # An interrupted batch comes first, before the chain is even read: its blobs are in
            # the journal, not yet in the store or necessarily in any archive, so finishing it
            # (which also stores its blobs) is what makes the published view computable.
            if self.journal_path.exists():
                journal = Journal.load(self.journal_path)
                if mode != "publish" or self.submitter is None:
                    report.outcome = "refused"
                    report.message = f"an interrupted batch (batch {journal.batch}, blobs from {journal.first_blob_seq}) is waiting in {self.journal_path}; run a plain publish with the key to finish it"
                    self.log(report.message)
                    return self._done(report, started)
                if journal.finished:
                    # Every transaction reached finality but the run died before bookkeeping.
                    self._finish(journal, store, report)
                elif not self._resume(journal, store, report):
                    return self._done(report, started)

            view = self._view(store)
            report.chain = {"contract": self.cfg.chain.contract, "blockNumber": view.state.block_number, "blobCount": view.state.blob_count, "head": "0x" + view.state.head.hex(), "publisher": view.state.publisher, "batches": len(view.decoded.batches), "tornBlobs": view.torn_blobs}

            entries, stats = self._sync(view)
            report.sync = [s.__dict__ for s in stats]
            report.new_entries = len(entries)
            tooling = self._tooling(view)
            report.tooling_published = bool(tooling)
            if not entries and not tooling:
                report.outcome = "nothing-to-publish" if not report.transactions else "resumed"
                report.message = "nothing to publish: every eligible entry is already in the stream"
                self.log(report.message)
                return self._done(report, started)
            if not entries:
                # Tooling alone does not make a batch; it rides along with the next data batch.
                report.outcome = "nothing-to-publish"
                report.message = "nothing to publish: new tooling will be included with the next batch that has new entries"
                self.log(report.message)
                return self._done(report, started)

            encoder = StreamEncoder(self.w3.eth.chain_id, bytes.fromhex(self.cfg.chain.contract[2:]), state=view.encoder_state)
            batch = encoder.encode_batch(entries, previous_entries=view.previous_entries, tooling=tooling, codec=CODEC_ZSTD)
            report.batch = {"batch": batch.batch, "firstBlobSeq": batch.first_blob_seq, "blobCountAfter": batch.blob_count_after, "blobs": len(batch.blobs), "transactions": -(-len(batch.blobs) // MAX_BLOBS_PER_TX), "lastBlobChunkCount": batch.last_blob_chunk_count, "bodyDigest": batch.body_digest.hex(), "manifestDigest": batch.manifest_digest.hex(), "hasIndex": batch.index is not None, "organisms": batch.manifest["organisms"], "schemasPublished": batch.schemas_published}
            self.log(f"planned batch {batch.batch}: {len(entries)} entries in {len(batch.blobs)} blob(s), {report.batch['transactions']} transaction(s)")

            if mode == "check":
                fees = current_fees(self.w3, self.policy)
                likely = len(batch.blobs) * BLOB_GAS_PER_BLOB * fees["blobBaseFee"] + report.batch["transactions"] * 60_000 * fees["baseFeePerGas"]
                at_most = len(batch.blobs) * BLOB_GAS_PER_BLOB * fees["maxFeePerBlobGas"] + report.batch["transactions"] * NOMINAL_GAS_LIMIT * fees["maxFeePerGas"]
                report.dry_run = {"fees": fees, "likelyCostWei": likely, "maxCostWei": at_most}
                line = f"estimated cost at current fees: {likely / 10**18:.6f} ETH, at most {at_most / 10**18:.6f} ETH (blob base fee {fees['blobBaseFee'] / GWEI:.4f} gwei)"
                if self.account is not None:
                    balance = self.w3.eth.get_balance(self.account.address)
                    report.dry_run["balanceWei"] = balance
                    line += f"; wallet {self.account.address} holds {balance / 10**18:.6f} ETH"
                self.log(line)
                report.outcome = "checked"
                return self._done(report, started)

            if self.submitter is None:
                raise SystemExit("configuration error: publishing needs the publisher key")
            # The pointer is carried forward unchanged until the IPFS snapshot sets a new one;
            # a batch end always writes it, so passing zero here would wipe a pointer set by hand.
            journal = self.submitter.plan(batch, batch.blobs, view.state.app_pointer)
            try:
                report.dry_run = self.submitter.dry_run(journal)
            except Refused as e:
                report.outcome = "refused"
                report.message = str(e)
                self.log(f"refused: {e}")
                self._abandon(journal)
                return self._done(report, started)
            self.log(f"dry run passed: up to {report.dry_run['maxCostWei'] / 10**18:.6f} ETH, likely {report.dry_run['likelyCostWei'] / 10**18:.6f} ETH, wallet holds {report.dry_run['balanceWei'] / 10**18:.6f} ETH")
            if mode == "dry-run":
                report.outcome = "dry-run-ok"
                self._abandon(journal)
                return self._done(report, started)

            try:
                self.submitter.run(journal)
            except RevertedOnChain as e:
                report.outcome = "failed"
                report.message = f"{e}; the batch has been set aside and the next run will start a fresh one after the torn blobs"
                self.log(f"publishing stopped: {report.message}")
                self._abandon(journal)
                return self._done(report, started)
            except SubmitError as e:
                report.outcome = "failed"
                report.message = str(e)
                self.log(f"publishing stopped: {e}")
                return self._done(report, started)
            self._finish(journal, store, report)
            report.outcome = "published"
            report.message = f"published batch {batch.batch}: {len(entries)} entries in {len(batch.blobs)} blob(s); contract now holds {batch.blob_count_after} blobs"
            self.log(report.message)
            return self._done(report, started)

    def _resume(self, journal: Journal, store: BlobStore, report: UploadReport) -> bool:
        """Finish an interrupted batch. Returns False if it had to be abandoned instead."""
        self.log(f"resuming interrupted batch {journal.batch} (blobs from {journal.first_blob_seq}): " + ", ".join(f"tx {t.index} {t.status}" for t in journal.txs))
        try:
            self.submitter.run(journal)
        except (Refused, RevertedOnChain) as e:
            # The contract has moved past what this batch assumed, either because another
            # upload ran in between or because one of our transactions reverted. Set the
            # batch aside; the run goes on with a fresh view and a fresh batch.
            self.log(f"the interrupted batch cannot be finished ({e}); setting it aside")
            self._abandon(journal)
            report.message = f"abandoned interrupted batch {journal.batch}: {e}"
            return True
        except SubmitError as e:
            report.outcome = "failed"
            report.message = f"could not finish the interrupted batch: {e}"
            self.log(report.message)
            return False
        self._finish(journal, store, report)
        return True

    def _abandon(self, journal: Journal) -> None:
        aside = self.data_dir / "abandoned"
        aside.mkdir(exist_ok=True)
        self.submitter.journal_path.replace(aside / f"batch-{journal.batch}-from-{journal.first_blob_seq}-{int(time.time())}.json")
        for p in Path(journal.blobs_dir).glob("*.blob"):
            p.unlink()
        Path(journal.blobs_dir).rmdir()

    def _done(self, report: UploadReport, started: float) -> UploadReport:
        report.duration_seconds = round(time.time() - started, 2)
        reports = self.data_dir / "reports"
        reports.mkdir(exist_ok=True)
        (reports / f"{time.strftime('%Y%m%dT%H%M%S')}-{report.mode}-{report.outcome}.json").write_text(json.dumps(report.to_json(), indent=1, default=str))
        return report


def _version_of(files: dict[str, bytes]) -> str | None:
    """The package version named in a tooling set's pyproject.toml, or None if absent/unreadable."""
    for path, content in files.items():
        if path.endswith("pyproject.toml"):
            try:
                return tomllib.loads(content.decode("utf-8"))["project"]["version"]
            except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                return None
    return None


def _installed_version() -> str | None:
    try:
        return importlib.metadata.version("loculus-eternal")
    except importlib.metadata.PackageNotFoundError:
        return None
