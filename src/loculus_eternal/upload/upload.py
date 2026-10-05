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
from loculus_eternal.format.encode import EmptyBatch, StreamEncoder
from loculus_eternal.format.records import CODEC_ZSTD
from loculus_eternal.sources import IpfsSource
from loculus_eternal.sources.base import SourceChain
from loculus_eternal.store import BlobStore
from loculus_eternal.ipfs import IpfsError, KuboClient
from loculus_eternal.upload.published import PublishedView, load_published_view
from loculus_eternal.upload.snapshot import build_and_publish, unpin_orphans
from loculus_eternal.upload.submitter import BLOB_GAS_PER_BLOB, GWEI, MAX_BLOBS_PER_TX, NOMINAL_GAS_LIMIT, FeePolicy, Journal, Refused, RevertedOnChain, SubmitError, Submitter, current_fees
from loculus_eternal.upload.sync import BackendClient, SyncStats, find_vanished, iter_new_entries


@dataclass
class UploadReport:
    mode: str
    outcome: str                         # "published", "nothing-to-publish", "refused", "checked", "dry-run-ok", "resumed"
    chain: dict = field(default_factory=dict)
    sync: list[dict] = field(default_factory=list)
    new_entries: int = 0
    vanished: dict = field(default_factory=dict)      # organism -> accessionVersions published but gone from the feed
    withdrawn: dict = field(default_factory=dict)     # organism -> accessionVersions withdrawn by this run
    tooling_published: bool = False
    batch: dict | None = None
    ipfs: dict | None = None            # the new batch's snapshot and blob objects
    ipfs_finalised: dict | None = None  # bookkeeping done when a batch reached finality (possibly a resumed one)
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
        # An IPFS source configured for uploads follows this machine's latest snapshot rather
        # than a frozen CID, since each publish unpins the previous snapshot.
        current = self._current_snapshot_cid()
        for src in self.cfg.sources:
            if isinstance(src, IpfsSource) and (src.snapshot_cid is None or current):
                src.set_snapshot(current or src.snapshot_cid)
        return load_published_view(self.w3, self.cfg.chain.contract, SourceChain(self.cfg.sources), store, deployment_block=self.cfg.chain.deployment_block, spill_dir=self.data_dir / "spill" / "decoded", log=self.log)

    def _sync(self, view: PublishedView, stats: list[SyncStats]):
        """A generator over every new entry of every organism, filling `stats` as it runs and
        logging each organism's numbers once its feed has been read. Entries are never held."""
        client = BackendClient(self.cfg.backend.url, self.data_dir / "feeds")
        for organism in self.cfg.backend.organisms:
            feed = client.fetch(organism)
            st = SyncStats(organism)
            stats.append(st)
            yield from iter_new_entries(organism, feed, lambda acc, ver, o=organism: view.is_published(o, acc, ver), st)
            gone = find_vanished(organism, st, view.decoded.published().get(organism, {}), view.decoded.withdrawn().get(organism, {}))
            self.log(f"{organism}: {st.total} released, {st.open} open, {st.already_published} already published, {st.new} new" + (f", {len(gone)} published but no longer in the feed" if gone else "") + (" (feed unchanged, from cache)" if feed.from_cache else ""))

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
        # The batch is final, so the chain now points at the new snapshot: record it and let
        # go of the previous one on every endpoint that holds the new one. The blob objects
        # have their own pins and stay. Old blocks leave at the node's next garbage collection.
        if journal.snapshot_cid:
            (self.data_dir / "snapshot-cid.txt").write_text(journal.snapshot_cid + "\n")
            # Each endpoint remembers which snapshot it holds, because an endpoint that was
            # down for a batch still has an older one to let go of.
            held = self._snapshots_held()
            unpinned = {}
            for url in journal.snapshot_endpoints:
                previous = held.get(url)
                if previous and previous != journal.snapshot_cid:
                    try:
                        KuboClient(url).pin_rm(previous)
                        unpinned[url] = previous
                    except IpfsError as e:
                        self.log(f"could not unpin the previous snapshot {previous} on {url}: {e}")
                held[url] = journal.snapshot_cid
            self._save_snapshots_held(held)
            report.ipfs_finalised = {"batch": journal.batch, "snapshotCid": journal.snapshot_cid, "previousUnpinned": unpinned}
        done_dir = self.data_dir / "done"
        done_dir.mkdir(exist_ok=True)
        self.submitter.journal_path.replace(done_dir / f"batch-{journal.batch}-from-{journal.first_blob_seq}.json")
        for p in Path(journal.blobs_dir).glob("*.blob"):
            p.unlink()
        Path(journal.blobs_dir).rmdir()

    # --- the run --------------------------------------------------------------------------------

    def run(self, mode: str = "publish", withdraw_vanished: bool = False) -> UploadReport:
        """mode: "publish" (default), "dry-run" (everything but sending), "check" (counts and cost only).

        `withdraw_vanished` is the maintainer's explicit confirmation that every published
        entry the backend no longer serves should be withdrawn in this batch."""
        started = time.time()
        report = UploadReport(mode=mode, outcome="")
        self._withdraw_vanished = withdraw_vanished
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
            try:
                return self._plan_and_publish(mode, view, store, report, started)
            finally:
                # The decoded stream's spilled entries served the published set and the
                # cumulative digests; they are a working copy and go away with the run.
                view.decoded.close()

    def _plan_and_publish(self, mode: str, view: PublishedView, store: BlobStore, report: UploadReport, started: float) -> UploadReport:
        tooling = self._tooling(view)
        report.tooling_published = bool(tooling)
        stats: list[SyncStats] = []
        encoder = StreamEncoder(self.w3.eth.chain_id, bytes.fromhex(self.cfg.chain.contract[2:]), state=view.encoder_state, work_dir=self.data_dir / "spill")

        try:
            # The entries flow from the backend feeds straight into the encoder's sorted
            # runs on disk; the whole batch is never in memory. The encoder reads the
            # withdrawals only after the entries, so by then every feed has been seen and
            # the vanished entries are known.
            batch = encoder.encode_batch(self._sync(view, stats), previous_entries=view.previous_entries, tooling=tooling, codec=CODEC_ZSTD, withdrawals=_WithdrawalsLater(lambda: self._withdrawals(stats, report)))
        except EmptyBatch:
            report.sync = [_stats_json(s) for s in stats]
            report.vanished = {s.organism: s.vanished for s in stats if s.vanished}
            report.outcome = "nothing-to-publish" if not report.transactions else "resumed"
            report.message = "nothing to publish: every eligible entry is already in the stream" + ("; new tooling will be included with the next batch that has new entries" if tooling else "")
            if report.vanished:
                n = sum(len(v) for v in report.vanished.values())
                report.message += f"; {n} published entries are no longer in the backend feed (run with --withdraw-vanished to withdraw them)"
            report.tooling_published = False
            self.log(report.message)
            return self._done(report, started)
        report.sync = [_stats_json(s) for s in stats]
        report.vanished = {s.organism: s.vanished for s in stats if s.vanished}
        report.new_entries = batch.entry_count
        if report.vanished and not self._withdraw_vanished:
            n = sum(len(v) for v in report.vanished.values())
            self.log(f"{n} published entries are no longer in the backend feed; they stay in the record until a run with --withdraw-vanished confirms their withdrawal")
        report.batch = {"batch": batch.batch, "firstBlobSeq": batch.first_blob_seq, "blobCountAfter": batch.blob_count_after, "blobs": len(batch.blobs), "transactions": -(-len(batch.blobs) // MAX_BLOBS_PER_TX), "lastBlobChunkCount": batch.last_blob_chunk_count, "bodyDigest": batch.body_digest.hex(), "manifestDigest": batch.manifest_digest.hex(), "hasIndex": batch.index is not None, "organisms": batch.manifest["organisms"], "schemasPublished": batch.schemas_published}
        report.batch["withdrawn"] = batch.withdrawn_count
        self.log(f"planned batch {batch.batch}: {batch.entry_count} entries" + (f", {batch.withdrawn_count} withdrawal(s)" if batch.withdrawn_count else "") + f" in {len(batch.blobs)} blob(s), {report.batch['transactions']} transaction(s)")

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
        # The snapshot of the stream as it will stand after this batch goes to IPFS first,
        # because the batch-end transaction carries the pointer to it. Without IPFS, or if
        # no endpoint takes it, the existing pointer is carried forward: a batch end always
        # writes the pointer, so zero would wipe one set by hand.
        pointer = view.state.app_pointer
        snap = None
        if self.cfg.ipfs is not None and self.cfg.ipfs.endpoints and mode == "publish":
            # The cheap refusals come before the expensive IPFS work.
            try:
                self.submitter.check_fees()
            except Refused as e:
                report.outcome = "refused"
                report.message = str(e)
                self.log(f"refused: {e}")
                return self._done(report, started)
            try:
                snap = build_and_publish(self.cfg.ipfs, chain_id=self.w3.eth.chain_id, contract=self.cfg.chain.contract, store=store, refs=view.refs, batch=batch, work_dir=self.data_dir / "spill", log=self.log)
            except IpfsError as e:
                report.outcome = "refused"
                report.message = f"IPFS: {e}"
                self.log(f"refused: {e}")
                return self._done(report, started)
            report.ipfs = snap.to_json()
            if snap.pointer is not None:
                pointer = snap.pointer
        journal = self.submitter.plan(
            batch,
            batch.blobs,
            pointer,
            snapshot_cid=snap.snapshot_cid if snap else None,
            snapshot_endpoints=[e["url"] for e in snap.endpoints if e.get("snapshot") == "ok"] if snap else [],
            blob_cids=snap.blob_cids if snap else [],
            ipfs_publish=snap.to_json() if snap else None,
        )
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
        report.message = f"published batch {batch.batch}: {batch.entry_count} entries in {len(batch.blobs)} blob(s); contract now holds {batch.blob_count_after} blobs" + (f"; snapshot {snap.snapshot_cid}" if snap and snap.snapshot_cid else "")
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

    def _withdrawals(self, stats: list[SyncStats], report: UploadReport) -> list[dict]:
        """Called by the encoder once every feed has been read. Withdraws nothing unless the
        maintainer confirmed it for this run."""
        if not self._withdraw_vanished:
            return []
        out = []
        for st in stats:
            if st.vanished:
                out.append({"organism": st.organism, "accessionVersions": st.vanished, "note": f"no longer served by the Pathoplexus backend as of {time.strftime('%Y-%m-%d')}; withdrawn by the maintainer"})
                report.withdrawn[st.organism] = list(st.vanished)
                self.log(f"{st.organism}: withdrawing {len(st.vanished)} entries the backend no longer serves")
        return out

    def _current_snapshot_cid(self) -> str | None:
        path = self.data_dir / "snapshot-cid.txt"
        return path.read_text().strip() or None if path.exists() else None

    def _snapshots_held(self) -> dict[str, str]:
        path = self.data_dir / "ipfs-snapshots.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _save_snapshots_held(self, held: dict[str, str]) -> None:
        (self.data_dir / "ipfs-snapshots.json").write_text(json.dumps(held, indent=1, sort_keys=True))

    def _abandon(self, journal: Journal) -> None:
        # Whatever this batch already put on IPFS was never published; take the pins back.
        if journal.ipfs_publish:
            cleaned = unpin_orphans(journal.ipfs_publish, journal.blob_cids, self.log)
            if cleaned:
                self.log(f"removed the abandoned batch's snapshot and blob objects from {len(cleaned)} IPFS endpoint(s)")
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


def _stats_json(st: SyncStats) -> dict:
    d = dict(st.__dict__)
    d.pop("seen", None)
    return d


class _WithdrawalsLater:
    """An iterable the encoder consumes only after it has read every entry, so the list can
    depend on what the feeds contained."""

    def __init__(self, make):
        self._make = make

    def __iter__(self):
        return iter(self._make())
