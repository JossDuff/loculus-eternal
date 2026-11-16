"""The upload command: one idempotent run that publishes whatever is newly eligible.

    learn what is published (chain + stream)  →  finish an interrupted batch if there is one
    →  sync the backend  →  select what to publish, withdraw and reinstate  →  nothing? stop
    →  encode a batch  →  have the maintainer confirm it  →  journal it  →  dry run
    →  send with finality  →  store own blobs  →  report

The record follows the latest copy of the database: after a run, recovery produces exactly
the entries that were eligible in the feeds that run read. Every step is safe to repeat. The lock on the data directory keeps two runs on one machine
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
from loculus_eternal.format.chunks import BLOB_BYTES
from loculus_eternal.human import human_duration, human_size
from loculus_eternal.format.encode import EmptyBatch, StreamEncoder
from loculus_eternal.format.records import CODEC_ZSTD
from loculus_eternal.sources.base import SourceChain
from loculus_eternal.store import BlobStore
from loculus_eternal.ipfs import IpfsError, KuboClient
from loculus_eternal.rpc import connect
from loculus_eternal.upload.published import PublishedView, load_published_view
from loculus_eternal.upload.snapshot import build_and_publish, unpin_orphans
from loculus_eternal.upload.submitter import BLOB_GAS_PER_BLOB, GWEI, FeePolicy, Journal, Refused, RevertedOnChain, SubmitError, Submitter, planned_transaction, preflight, publish_contract
from loculus_eternal.upload.sync import BackendClient, SyncError, SyncStats, find_changes, iter_new_entries


@dataclass
class UploadReport:
    mode: str
    outcome: str                         # "published", "nothing-to-publish", "refused", "declined", "dry-run-ok", "resumed"
    chain: dict = field(default_factory=dict)
    sync: list[dict] = field(default_factory=list)
    new_entries: int = 0
    # organism -> accessionVersions this run's batch withdraws (published, no longer eligible
    # in the copy) and reinstates (withdrawn, eligible in it again); a dry run reports what
    # a publish would do
    withdrawn: dict = field(default_factory=dict)
    reinstated: dict = field(default_factory=dict)
    tooling_published: bool = False
    batch: dict | None = None
    ipfs: dict | None = None            # the new batch's snapshot and blob objects
    ipfs_finalised: dict | None = None  # bookkeeping done when a batch reached finality (possibly a resumed one)
    dry_run: dict | None = None
    transactions: list[dict] = field(default_factory=list)
    message: str = ""
    duration_seconds: float = 0.0        # the work the run did; time spent waiting at the question is left out
    waited_seconds: float = 0.0          # how long the question waited for its answer

    def to_json(self) -> dict:
        return self.__dict__


# The time a batch takes to send is estimated for the operator's planning and for nothing
# else. Transactions go out several at a time; a block has taken one or two of them in the
# runs so far, and one per block is assumed. A block is final two epochs after its own ends,
# so between two and three epochs after it is made; two and a half are assumed.
SLOTS_PER_EPOCH = 32
EPOCHS_TO_FINALITY_ASSUMED = 2.5


def estimated_seconds(transactions: int, seconds_per_slot: float) -> tuple[float, float]:
    """Roughly how long a batch takes: (to include its transactions, then to finality)."""
    return transactions * seconds_per_slot, EPOCHS_TO_FINALITY_ASSUMED * SLOTS_PER_EPOCH * seconds_per_slot


class NobodyToAsk(RuntimeError):
    """Raised by a confirm callback that has nobody to put the question to. The batch is not
    sent, and the run says why instead of reporting that somebody answered no."""


class Uploader:
    def __init__(self, config: Config, *, w3: Web3 | None = None, account=None, log: Callable[[str], None] = print, sleep=time.sleep, poll_interval: float | None = None, clock: Callable[[], float] = time.time):
        if config.backend is None or config.upload is None:
            raise SystemExit("configuration error: the upload command needs [backend] and [upload] sections")
        self.cfg = config
        self.log = log
        self.clock = clock
        self.w3 = w3 or connect(config.chain.rpc_url, max_rps=config.chain.max_requests_per_second, timeout=120)
        self.account = account
        self.data_dir = config.upload.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        policy = FeePolicy(
            max_blob_fee_gwei=config.upload.max_blob_fee_gwei,
            max_priority_fee_gwei=config.upload.max_priority_fee_gwei,
            escalation_attempts=config.upload.escalation_attempts,
            inclusion_timeout_blocks=config.upload.inclusion_timeout_blocks,
            finality_timeout_seconds=config.upload.finality_timeout_seconds,
            max_in_flight=config.upload.max_in_flight,
            max_blobs_per_transaction=config.upload.max_blobs_per_transaction,
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
        # An IPFS source follows the snapshot the contract names; the published view hands
        # every source the chain's pointer before asking it for anything.
        return load_published_view(self.w3, self.cfg.chain.contract, SourceChain(self.cfg.sources), store, deployment_block=self.cfg.chain.deployment_block, spill_dir=self.data_dir / "spill" / "decoded", log=self.log)

    def _organisms(self, client: BackendClient) -> list[str]:
        """The organisms to sync: every organism the backend serves, asked at the start of each
        run, so a newly added organism is picked up by the next run with nothing to configure."""
        organisms = client.organisms()
        self.log(f"backend at {self.cfg.backend.url} serves {len(organisms)} organisms: {', '.join(organisms)}")
        return organisms

    def _sync(self, view: PublishedView, stats: list[SyncStats], client: BackendClient, organisms: list[str]):
        """A generator over every new entry of every organism, filling `stats` as it runs and
        logging each organism's numbers once its feed has been read. Entries are never held."""
        n = len(organisms)
        # What the stream holds does not change while the feeds are read.
        published, withdrawn = view.decoded.published(), view.decoded.withdrawn()
        for k, organism in enumerate(organisms, 1):
            feed = client.fetch(organism)
            st = SyncStats(organism)
            stats.append(st)
            total_text = f"{feed.total_records:,}" if feed.total_records else "?"
            self.log(f"[{k}/{n}] {organism}: reading {total_text} released entries" + (" (feed unchanged, from cache)" if feed.from_cache else " (downloaded)"))

            def progress(read, total, o=organism, k=k):
                self.log(f"[{k}/{n}] {o}: {read:,}/{total:,} read, {st.new:,} new so far" if total else f"[{k}/{n}] {o}: {read:,} read, {st.new:,} new so far")

            yield from iter_new_entries(organism, feed, lambda acc, ver, o=organism: view.is_published(o, acc, ver), st, progress, submitted_cutoff=self._submitted_cutoff)
            # An entry that is not in the feed is withdrawn, so a feed that arrived short must
            # not be mistaken for a database that no longer holds the rest.
            client.check_complete(feed, st.total)
            gone, back = find_changes(st, published.get(organism, {}), withdrawn.get(organism, {}))
            days = self.cfg.backend.min_age_days
            self.log(
                f"[{k}/{n}] {organism}: done. {st.total:,} released, {st.open:,} open, "
                + (f"{st.held_back:,} held back (submitted less than {days} days ago), " if days else "")
                + f"{st.already_published:,} already published, {st.new:,} new"
                + (f", {len(gone):,} to withdraw (published, not in this copy)" if gone else "")
                + (f", {len(back):,} to reinstate (withdrawn, in this copy again)" if back else "")
            )
        # An organism that is in the stream but that the backend no longer serves at all: none
        # of its entries is in this copy, so every published one is withdrawn.
        for organism in view.decoded.organisms():
            if organism in organisms:
                continue
            st = SyncStats(organism)
            stats.append(st)
            gone, _ = find_changes(st, published.get(organism, {}), withdrawn.get(organism, {}))
            if gone:
                self.log(f"{organism}: the backend no longer serves this organism; its {len(gone):,} published entries are to be withdrawn")

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

    def run(self, mode: str = "publish", confirm: Callable[[str], bool] | None = None) -> UploadReport:
        """mode: "publish" (default) or "dry-run", which sends nothing and writes no journal.
        A dry run plans the batch, simulates its first transaction and checks the fees and
        the balance. With the publisher's account it does so as that account, which also
        shows whether the key is the contract's publisher; without one it does so as the
        contract's publisher, and says so.

        `confirm` is asked once a publish has planned its batch and before anything of it is
        sent: it is handed a few lines naming the database, the contract, and how many
        entries the batch adds, withdraws and reinstates, and the run stops unless it returns
        True. It raises NobodyToAsk when it cannot put the question to anyone, which also
        stops the run. None means nobody is asked."""
        if mode not in ("publish", "dry-run"):
            # Every later step asks "is this a publish?" or "is this a dry run?", so a name
            # that is neither would pass each test and end up sending. Refuse it here.
            raise ValueError(f"unknown upload mode {mode!r}: it is \"publish\" or \"dry-run\"")
        started = time.time()
        report = UploadReport(mode=mode, outcome="")
        self._confirm = confirm
        # One reading of the clock decides every entry's age for the whole run, so an entry
        # cannot be old enough for one organism's feed and too young for the next.
        now = int(self.clock())
        self._today = time.strftime("%Y-%m-%d", time.gmtime(now))
        days = self.cfg.backend.min_age_days
        self._submitted_cutoff = now - days * 86_400 if days else None
        self._client = BackendClient(self.cfg.backend.url, self.data_dir / "feeds")
        try:
            return self._run(mode, report, started)
        except SyncError as e:
            # The backend, not the chain, is the problem: nothing was sent because of it,
            # and the run still ends with its result line and report.
            report.outcome = "failed"
            report.message = f"backend problem: {e}"
            self.log(report.message)
            return self._done(report, started)

    def _run(self, mode: str, report: UploadReport, started: float) -> UploadReport:
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
            # The backend is asked only now: an interrupted batch has transactions racing
            # their inclusion window, and finishing it must not wait on a backend outage.
            # It still comes before the chain read, which is the expensive step.
            self._organism_list = self._organisms(self._client)
            self.log(f"reading the contract at {self.cfg.chain.contract} and the published stream")
            view = self._view(store)
            self.log(f"contract holds {view.state.blob_count} blob(s) in {len(view.decoded.batches)} complete batch(es)")
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
        encoder = StreamEncoder(self.w3.eth.chain_id, bytes.fromhex(self.cfg.chain.contract[2:]), state=view.encoder_state, work_dir=self.data_dir / "spill", progress=self.log)

        try:
            # The entries flow from the backend feeds straight into the encoder's sorted
            # runs on disk; the whole batch is never in memory. The encoder reads the
            # withdrawals and reinstatements only after the entries, so by then every feed
            # has been read and it is known what the copy holds.
            batch = encoder.encode_batch(
                self._sync(view, stats, self._client, self._organism_list),
                previous_entries=view.previous_entries,
                tooling=tooling,
                codec=CODEC_ZSTD,
                withdrawals=_Later(lambda: self._records(stats, "to_withdraw", report.withdrawn, f"not among the entries eligible for publication in the database on {self._today}")),
                reinstatements=_Later(lambda: self._records(stats, "to_reinstate", report.reinstated, f"among the entries eligible for publication in the database again on {self._today}")),
            )
        except EmptyBatch:
            report.sync = [_stats_json(s) for s in stats]
            report.outcome = "nothing-to-publish" if not report.transactions else "resumed"
            report.message = "nothing to publish: the stream already holds exactly the eligible entries of the database" + ("; new tooling will be included with the next batch that changes the record" if tooling else "")
            report.tooling_published = False
            self.log(report.message)
            return self._done(report, started)
        report.sync = [_stats_json(s) for s in stats]
        report.new_entries = batch.entry_count
        report.batch = {"batch": batch.batch, "firstBlobSeq": batch.first_blob_seq, "blobCountAfter": batch.blob_count_after, "blobs": len(batch.blobs), "transactions": -(-len(batch.blobs) // self.policy.max_blobs_per_transaction), "lastBlobChunkCount": batch.last_blob_chunk_count, "bodyDigest": batch.body_digest.hex(), "manifestDigest": batch.manifest_digest.hex(), "hasIndex": batch.index is not None, "organisms": batch.manifest["organisms"], "schemasPublished": batch.schemas_published}
        report.batch["withdrawn"] = batch.withdrawn_count
        report.batch["reinstated"] = batch.reinstated_count
        to_include, to_finality = estimated_seconds(report.batch["transactions"], self.cfg.chain.seconds_per_slot)
        report.batch.update({"uncompressedBytes": batch.uncompressed_bytes, "compressedBytes": batch.compressed_bytes, "blobBytes": len(batch.blobs) * BLOB_BYTES, "estimatedSeconds": round(to_include + to_finality)})
        self.log(f"planned batch {batch.batch}: {batch.entry_count} entries" + (f", {batch.withdrawn_count} withdrawal(s)" if batch.withdrawn_count else "") + (f", {batch.reinstated_count} reinstatement(s)" if batch.reinstated_count else "") + f" in {len(batch.blobs)} blob(s), {report.batch['transactions']} transaction(s)")

        if mode == "publish" and self.submitter is None:
            raise SystemExit("configuration error: publishing needs the publisher private key")
        # A first look at sending the batch, before the summary: the fees are under the
        # limit, the first transaction would be accepted, and the wallet covers the most the
        # batch can cost. It needs no key, because a simulation needs no signature: without
        # one it is run as the contract's publisher. Nothing is written for it, so a dry run
        # can be interrupted at any point and leave nothing a later publish could pick up.
        transactions = report.batch["transactions"]
        per_tx = self.policy.max_blobs_per_transaction
        sender = self.account.address if self.account is not None else view.state.publisher
        rows = [("transactions", f"{transactions}")]
        time_row = ("estimated time", f"about {human_duration(to_include + to_finality)}: {human_duration(to_include)} to include {transactions} transaction(s) at about one per block, then {human_duration(to_finality)} until the last is final")
        try:
            first = planned_transaction(batch, batch.blobs, view.state.app_pointer, per_tx, 0)
            look = preflight(self.w3, publish_contract(self.w3, self.cfg.chain.contract), sender, self.policy, first, [len(batch.blobs[i : i + per_tx]) for i in range(0, len(batch.blobs), per_tx)])
        except Refused as e:
            self._summarise(batch, stats, "planned", rows + [time_row])
            report.outcome = "refused"
            report.message = str(e)
            self.log(f"refused: {e}")
            return self._done(report, started)
        whose = "wallet" if self.account is not None else "the publisher's wallet"
        cost = f"{look['likelyCostWei'] / 10**18:.6f} ETH at current fees, at most {look['maxCostWei'] / 10**18:.6f} ETH (blob base fee {look['fees']['blobBaseFee'] / GWEI:.4f} gwei); {whose} {sender} holds {look['balanceWei'] / 10**18:.6f} ETH"
        self._summarise(batch, stats, "planned", rows + [("estimated cost", cost), time_row])

        if mode == "dry-run":
            report.dry_run = look
            self.log(f"dry run passed: up to {look['maxCostWei'] / 10**18:.6f} ETH, likely {look['likelyCostWei'] / 10**18:.6f} ETH, {whose} holds {look['balanceWei'] / 10**18:.6f} ETH")
            if self.account is None:
                self.log(f"the publisher key is not in the environment, so the first transaction was simulated as the contract's publisher {sender}; with the key, a dry run also checks that the key is that publisher's")
            report.outcome = "dry-run-ok"
            return self._done(report, started)
        # The maintainer confirms the batch before anything of it leaves this machine: which
        # database it was read from, where it goes, and how many entries it adds, withdraws
        # and reinstates. The record will hold exactly what that copy holds, so a copy with
        # entries missing withdraws them, and this is where that is seen in time. An
        # interrupted batch was finished above without asking: the run that planned it had
        # it confirmed, and its transactions could not wait on a person. A dry run sends
        # nothing and asks nothing.
        if mode == "publish" and self._confirm is not None:
            asked = time.time()
            try:
                stopped = None if self._confirm(self._confirmation_text(batch, stats)) else ("declined", "stopped at the confirmation")
            except NobodyToAsk as e:
                stopped = ("refused", str(e))
            finally:
                report.waited_seconds = round(time.time() - asked, 2)
            if stopped:
                report.outcome = "resumed" if report.transactions else stopped[0]
                report.message = ("the interrupted batch was finished; " if report.transactions else "") + f"{stopped[1]}: nothing of batch {batch.batch} was sent"
                if stopped[0] == "refused":
                    self.log(f"refused: {report.message}")
                return self._done(report, started)
        # The snapshot of the stream as it will stand after this batch goes to IPFS first,
        # because the batch-end transaction carries its CID as the pointer. Without IPFS, or
        # if no endpoint takes it, the existing pointer is carried forward: a batch end always
        # writes the pointer, so an empty one would wipe a snapshot set by hand.
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
            if snap.snapshot_cid is not None:
                pointer = snap.snapshot_cid
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

        sending_started = time.time()
        try:
            self.submitter.run(journal)
        except (Refused, RevertedOnChain) as e:
            # A refusal here (after the dry run) comes from a transaction simulated once its
            # predecessors were in: the chain moved under the batch. Same remedy as a revert.
            report.outcome = "failed"
            report.message = f"{e}; the batch has been set aside; wait for the chain to finalise, then run again to start a fresh batch after the torn blobs"
            self.log(f"publishing stopped: {report.message}")
            self._abandon(journal)
            return self._done(report, started)
        except SubmitError as e:
            report.outcome = "failed"
            report.message = str(e)
            self.log(f"publishing stopped: {e}")
            return self._done(report, started)
        sending_seconds = time.time() - sending_started
        self._finish(journal, store, report)
        # What it took, now that it is known: the summary shown before sending gave estimates.
        sent = report.transactions[-len(journal.txs):]
        paid = sum((t.get("gasUsed") or 0) * (t.get("effectiveGasPrice") or 0) + len(t["seqs"]) * BLOB_GAS_PER_BLOB * (t.get("blobGasPrice") or 0) for t in sent)
        blocks = [t["block"] for t in sent if t.get("block") is not None]
        report.batch.update({"costWei": paid, "sendingSeconds": round(sending_seconds)})
        self._summarise(batch, stats, "published", [
            ("transactions", f"{len(sent)}" + (f", in blocks {min(blocks):,} to {max(blocks):,}" if blocks else "")),
            ("cost", f"{paid / 10**18:.6f} ETH paid"),
            ("time", f"{human_duration(time.time() - started - report.waited_seconds)} in all; {human_duration(sending_seconds)} from the first transaction sent until the last was final"),
        ])
        report.outcome = "published"
        report.message = f"published batch {batch.batch}: {batch.entry_count} entries in {len(batch.blobs)} blob(s); contract now holds {batch.blob_count_after} blobs" + (f", {view.torn_blobs} of them from an abandoned upload that readers skip" if view.torn_blobs else "") + (f"; snapshot {snap.snapshot_cid}" if snap and snap.snapshot_cid else "")
        self.log(report.message)
        return self._done(report, started)

    def _resume(self, journal: Journal, store: BlobStore, report: UploadReport) -> bool:
        """Finish an interrupted batch. Returns False if it had to be abandoned instead."""
        self.log(f"resuming interrupted batch {journal.batch} (blobs from {journal.first_blob_seq}): " + ", ".join(f"tx {t.index} {t.status}" for t in journal.txs))
        try:
            self.submitter.run(journal)
        except (Refused, RevertedOnChain) as e:
            # The contract has moved past what this batch assumed. Set the batch aside and
            # stop: transactions of this batch may still be landing, and the finalized view a
            # fresh plan would be built on lags the chain by minutes. The next run, started
            # once the chain is quiet, starts a fresh batch after the torn blobs.
            self.log(f"the interrupted batch cannot be finished ({e}); setting it aside")
            self._abandon(journal)
            report.outcome = "failed"
            report.message = f"abandoned interrupted batch {journal.batch}: {e}. Wait for the chain to finalise, then run again to start a fresh batch after the torn blobs."
            return False
        except SubmitError as e:
            report.outcome = "failed"
            report.message = f"could not finish the interrupted batch: {e}"
            self.log(report.message)
            return False
        self._finish(journal, store, report)
        return True

    def _summarise(self, batch, stats: list[SyncStats], state: str, rows: list[tuple[str, str]]) -> None:
        """The few numbers that describe a batch, together in one place near the end of what a
        run logs: what it changes in the record and how large it is, followed by what the
        caller knows about its transactions, cost and time (estimates before it is sent,
        what it took afterwards)."""
        days = self.cfg.backend.min_age_days
        entries = f"{batch.entry_count:,} new, {batch.withdrawn_count:,} withdrawn, {batch.reinstated_count:,} reinstated"
        if days:
            entries += f"; {sum(s.held_back for s in stats):,} held back as submitted less than {days} days ago"
        blob_bytes = len(batch.blobs) * BLOB_BYTES
        size = f"{human_size(batch.uncompressed_bytes)} of records, {human_size(batch.compressed_bytes)} compressed, in {len(batch.blobs):,} blob(s) that take {human_size(blob_bytes)} on chain"
        lines = [("entries", entries), ("size", size), *rows]
        width = max(len(name) for name, _ in lines)
        self.log(f"summary of batch {batch.batch} ({state}):")
        for name, text in lines:
            self.log(f"  {name.ljust(width)}  {text}")

    def _confirmation_text(self, batch, stats: list[SyncStats]) -> str:
        """What the maintainer agrees to before a batch is sent: where the data comes from,
        where it goes, and what the batch does to the record."""
        lines = [
            f"You are publishing a copy of the Loculus database located at {self.cfg.backend.url}",
            f"to contract {self.cfg.chain.contract} on chain {self.w3.eth.chain_id}.",
            f"  new entries:  {batch.entry_count:,}",
            f"  withdrawn:    {batch.withdrawn_count:,}  (published, not in this copy)",
            f"  reinstated:   {batch.reinstated_count:,}  (withdrawn, in this copy again)",
        ]
        days = self.cfg.backend.min_age_days
        if days:
            lines.append(f"  held back:    {sum(s.held_back for s in stats):,}  (submitted less than {days} days ago)")
        return "\n".join(lines)

    @staticmethod
    def _records(stats: list[SyncStats], which: str, reported: dict, note: str) -> list[dict]:
        """The withdrawal or reinstatement records of a batch, one per organism that has any,
        called by the encoder once every feed has been read. `which` is the list in the
        organism's statistics (what to withdraw, or what to reinstate); the same lists go
        into the run report under `reported`."""
        out = []
        for st in stats:
            named = getattr(st, which)
            if named:
                out.append({"organism": st.organism, "accessionVersions": named, "note": note})
                reported[st.organism] = list(named)
        return out

    def _snapshots_held(self) -> dict[str, str]:
        path = self.data_dir / "ipfs-snapshots.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def _save_snapshots_held(self, held: dict[str, str]) -> None:
        (self.data_dir / "ipfs-snapshots.json").write_text(json.dumps(held, indent=1, sort_keys=True))

    def _abandon(self, journal: Journal) -> None:
        # Whatever this batch already put on IPFS was never published; take the pins back.
        if journal.ipfs_publish:
            # Blob 0 holds the stream header, which every later batch relies on even when this
            # first attempt is abandoned; its object stays pinned.
            cids = journal.blob_cids[1:] if journal.first_blob_seq == 0 else journal.blob_cids
            cleaned = unpin_orphans(journal.ipfs_publish, cids, self.log)
            if cleaned:
                self.log(f"removed the abandoned batch's snapshot and blob objects from {len(cleaned)} IPFS endpoint(s)")
        # The journal is kept under abandoned/ as the record of what was planned. The blob
        # files are kept only if something was sent: then they are the exact bytes behind a
        # transaction on chain and may be wanted to understand what happened. A dry run or a
        # refusal sent nothing, and its blobs would only pile up on disk.
        aside = self.data_dir / "abandoned"
        aside.mkdir(exist_ok=True)
        # Nanoseconds, so two batches set aside within the same second never collide.
        stamp = f"batch-{journal.batch}-from-{journal.first_blob_seq}-{time.time_ns()}"
        self.submitter.journal_path.replace(aside / f"{stamp}.json")
        blobs_dir = Path(journal.blobs_dir)
        if not blobs_dir.exists():
            return
        if any(t.attempts or t.reverted_attempts for t in journal.txs):
            blobs_dir.replace(aside / stamp)
        else:
            for p in blobs_dir.glob("*.blob"):
                p.unlink()
            blobs_dir.rmdir()

    def _done(self, report: UploadReport, started: float) -> UploadReport:
        # The time a run takes is the work it did, not how long it sat at the question.
        report.duration_seconds = round(time.time() - started - report.waited_seconds, 2)
        # The last line of every run says plainly how it ended, in words a maintainer who
        # only reads the end of a log can act on.
        verdicts = {
            "published": "SUCCESS",
            "resumed": "SUCCESS",
            "nothing-to-publish": "NOTHING TO DO",
            "dry-run-ok": "DRY RUN PASSED (nothing sent)",
            "refused": "REFUSED (nothing sent)",
            "declined": "STOPPED AT THE CONFIRMATION (nothing sent)",
            "failed": "FAILED",
        }
        self.log(f"RESULT: {verdicts.get(report.outcome, report.outcome.upper())}" + (f": {report.message}" if report.message else ""))
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
    # The eligible set is the whole feed's worth of names, and the names withdrawn and
    # reinstated are already in the report under their own keys.
    for key in ("eligible", "to_withdraw", "to_reinstate"):
        d.pop(key, None)
    return d


class _Later:
    """An iterable the encoder consumes only after it has read every entry, so the list can
    depend on what the feeds contained."""

    def __init__(self, make):
        self._make = make

    def __iter__(self):
        return iter(self._make())
