"""The health check: is the published record still available, and still correct?

One check reads the contract, verifies the blob list against the chain, fetches every blob
from every configured source and verifies each against its versioned hash, walks the
stream's structure. IPFS is one of the sources, through the snapshot the contract points
at. It materialises nothing, and it never consults the live database: the record is judged on
its own. The result is a
plain report with a verdict and the reasons for it, meant for a third party who wants to
confirm, without trusting anyone, that the data is still there and still right.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlsplit

from loculus_eternal.chain import BlobRef, ChainReader, ManifestMismatch, verify_manifest
from loculus_eternal.config import Config
from loculus_eternal.format.structure import read_structure
from loculus_eternal.rpc import connect
from loculus_eternal.sources import BeaconSource, BlobArchiverSource, BlobscanSource, BlockscoutSource, HashLookupSource, IpfsSource, LocalDirectorySource
from loculus_eternal.sources.base import BlobContext, verify_candidates

RETENTION_SLOTS = 4096 * 32   # 4096 epochs of 32 slots: about 18.2 days at 12 seconds a slot
STEPS = ["contract check", "blob check"]   # how the page groups the work


def public_url(url: str) -> str:
    """An endpoint as the page may show it: scheme and host only. Hosted RPC and archive
    URLs carry API keys in their path or query, and the page must never show one."""
    parts = urlsplit(str(url))
    if not parts.scheme or not parts.netloc:
        return str(url)
    host = parts.hostname or parts.netloc
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}"


def source_endpoints(src) -> list[str]:
    """The raw endpoint strings a source was built with."""
    if isinstance(src, (BeaconSource, IpfsSource)):
        return list(src.endpoints)
    if isinstance(src, (HashLookupSource, BlobArchiverSource)):
        return [src.base_url]
    if isinstance(src, LocalDirectorySource):
        return [str(src.directory)]
    return []


def source_kind(src) -> str:
    return {BeaconSource: "beacon", BlobscanSource: "blobscan", BlockscoutSource: "blockscout", BlobArchiverSource: "blob-archiver", IpfsSource: "ipfs", LocalDirectorySource: "local"}.get(type(src), type(src).__name__)


def public_source(src) -> str:
    """The name a source goes by on the page: its kind and its hosts, never a full URL."""
    return f"{source_kind(src)}({', '.join(public_url(e) for e in source_endpoints(src))})"


def redact(text: str, src) -> str:
    """Error text with every raw endpoint of the source replaced by its public form."""
    for raw in source_endpoints(src):
        text = text.replace(raw.rstrip("/"), public_url(raw))
    return text


def redact_all(text: str, config: Config) -> str:
    """Error text with the RPC URL and every source endpoint replaced by their hosts. An
    RPC error from a hosted node quotes the full URL, key included."""
    text = text.replace(config.chain.rpc_url.rstrip("/"), public_url(config.chain.rpc_url))
    for src in config.sources:
        text = redact(text, src)
    return text


@dataclass
class Report:
    started_at: float
    finished_at: float | None = None
    verdict: str = "running"            # running, recoverable, not recoverable
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    chain: dict = field(default_factory=dict)
    blob_list: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    stream: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    progress: dict = field(default_factory=dict)   # {"steps", "step", "detail"} while running

    def to_json(self) -> dict:
        return dict(self.__dict__)


SEQ_LISTS = ("verifiedSeqs", "missingSeqs", "staleSeqs")    # per row, for the stream walk's judgement


def _row_copy(row: dict) -> dict:
    """A source's row as the page sees it while the check runs: the counts, without the
    growing sequence lists the page never reads."""
    return {k: v for k, v in row.items() if k not in SEQ_LISTS}


def _empty_counts(asked: int = 0) -> dict:
    """One source's row before anything is fetched from it. The sequence lists say which
    blobs were verified, which were missing, and which of the missing are past retention,
    so that the dead blobs of an abandoned upload can be left out once they are known."""
    return {"verified": 0, "missing": 0, "corrupt": 0, "error": 0, "missingOutsideRetention": 0, "asked": asked, "answered": 0, **{k: [] for k in SEQ_LISTS}}


class HealthCheck:
    def __init__(self, config: Config, *, log: Callable[[str], None] | None = None, progress: Callable[[dict], None] | None = None, w3=None):
        self.cfg = config
        self.w3 = w3 or connect(config.chain.rpc_url, max_rps=config.chain.max_requests_per_second, timeout=60)
        self._log = log or (lambda s: None)
        self._progress = progress or (lambda p: None)

    def _step(self, report: Report, step: str, detail: str | None = None) -> None:
        """Where the check is, for a page that shows progress: the step and a line of detail.
        Called from the source threads as well; it replaces the progress object whole and
        hands the caller's callback a copy of its own."""
        report.progress = {"steps": STEPS, "step": step, "detail": detail}
        self._progress(report.progress)

    def run(self, report: Report | None = None) -> Report:
        """Run one check. With `report` given, that object is filled in step by step, so a
        caller that shows it while the check runs sees the contract's facts as soon as the
        contract check is done, then the sources, and finally the verdict."""
        if report is None:
            report = Report(started_at=time.time())

        def log(msg: str) -> None:
            report.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
            self._log(msg)

        try:
            # Contract check: the contract's state and the blob list that reproduces its head.
            self._step(report, "contract check", "reading the contract at the finalized block")
            state, refs = self._chain(report, log)
            if state is None:
                return self._finish(report)
            # Blob check: every blob from every source, then the record's structure.
            self._step(report, "blob check")
            blobs = self._sources(report, refs, state, log)
            self._step(report, "blob check", "walking the record's structure")
            self._stream(report, blobs, state, log)
        except Exception as e:  # a check must end with a verdict, never a traceback
            report.problems.append(f"the check itself failed: {type(e).__name__}: {e}")
            log(f"check failed: {e}")
        return self._finish(report)

    def _finish(self, report: Report) -> Report:
        report.finished_at = time.time()
        report.progress = {}
        if "matrix" in report.sources:
            # A check that failed midway still ends with no row in progress. The published
            # rows are never edited, so the section is replaced whole.
            matrix = {k: {kk: v for kk, v in m.items() if kk != "status"} for k, m in report.sources["matrix"].items()}
            report.sources = {**report.sources, "matrix": matrix}
        # Judged here in every case: a verdict announced while the sources were still being
        # asked was provisional, and is confirmed or overturned on the whole picture.
        report.verdict = "not recoverable" if self._unrecoverable(report) else "recoverable"
        # Nothing that quotes a URL leaves with a key in it, whichever step produced it.
        clean = lambda t: redact_all(t, self.cfg)
        report.problems = [clean(p) for p in report.problems]
        report.notes = [clean(p) for p in report.notes]
        report.log = [clean(l) for l in report.log]
        for section in (report.chain, report.blob_list):
            if "error" in section:
                section["error"] = clean(section["error"])
        return report

    @staticmethod
    def _unrecoverable(report: Report) -> bool:
        """The one question the page answers: can the dataset be rebuilt from what is out
        there? It cannot when the contract cannot be read, when the blob list does not
        reproduce the chain's head, when a blob a reader needs is unavailable from every
        source, when the blobs do not parse as a stream, or when the check itself broke.
        A dead archive, a stopped IPFS node or an unreadable snapshot are listed as problems
        but do not change the answer: the other sources still recover the dataset."""
        if "error" in report.chain:
            return True
        if report.blob_list and not report.blob_list.get("verified"):
            return True
        if report.sources.get("neededUnavailable"):
            return True
        if report.chain.get("blobCount") and report.stream and report.stream.get("header") is None:
            return True
        return any(p.startswith("the check itself failed") for p in report.problems)

    # --- steps -------------------------------------------------------------------------------

    def _chain(self, report: Report, log):
        log("reading the contract at the finalized block")
        reader = ChainReader(self.w3, self.cfg.chain.contract)
        try:
            state = reader.state_at_finalized()
        except Exception as e:
            report.problems.append(f"cannot read the contract: {e}")
            report.chain = {"contract": self.cfg.chain.contract, "error": str(e)}
            return None, []
        block = {"timestamp": reader.block_timestamp(state.block_number)}
        report.chain = {
            "contract": self.cfg.chain.contract,
            "chainId": self.w3.eth.chain_id,
            "finalizedBlock": state.block_number,
            "finalizedAgeSeconds": int(time.time() - block["timestamp"]),
            "publisher": state.publisher,
            "blobCount": state.blob_count,
            "head": "0x" + state.head.hex(),
            "snapshotCid": state.app_pointer or None,
            "successor": state.successor,
        }
        if int(state.successor, 16) != 0:
            report.notes.append(f"a successor contract is set: {state.successor}; this deployment may have been replaced")
        # The snapshot CID comes from the contract, so there is nothing to compare it with;
        # the blob check reads that snapshot through the IPFS source and shows what it finds.
        if not state.app_pointer and any(isinstance(s, IpfsSource) for s in self.cfg.sources):
            report.notes.append("the contract names no IPFS snapshot yet: the IPFS source has nothing to read until a batch is committed")
        log(f"contract holds {state.blob_count} blobs, head 0x{state.head.hex()[:16]}…")

        self._step(report, "contract check", "rebuilding the blob list from event logs")

        log("collecting the blob list from event logs")
        try:
            refs = reader.blob_refs_from_logs(self.cfg.chain.deployment_block, state.block_number)
            verify_manifest(refs, state)
            report.blob_list = {"source": "event logs", "verified": True, "count": len(refs)}
            log(f"blob list verified against head ({len(refs)} blobs)")
        except ManifestMismatch as e:
            report.blob_list = {"source": "event logs", "verified": False, "error": str(e)}
            report.problems.append(f"the blob list from event logs does not match the chain's head: {e}")
            refs = []
        except Exception as e:
            report.blob_list = {"source": "event logs", "verified": False, "error": str(e)}
            report.problems.append(f"cannot read event logs ({e}); without a blob list nothing can be fetched")
            refs = []
        if refs:
            last = refs[-1]
            report.chain["lastPublishBlock"] = last.block_number
            report.chain["lastPublishTimestamp"] = last.block_timestamp
            report.chain["lastPublishAgeSeconds"] = int(time.time() - last.block_timestamp) if last.block_timestamp else None
        return state, refs

    def _sources(self, report: Report, refs: list[BlobRef], state, log) -> list[bytes | None]:
        """Ask every source for every blob, all sources at once; keep the first verified copy
        of each blob, in configuration order.

        The sources are independent operators, so they are asked in parallel and the check
        takes as long as the slowest of them. Each source works on its own row of counts and
        its own verified blobs; nothing is shared between the threads but the published
        copies of the rows, written under one lock because the server serialises the report
        from yet another thread while a check runs. The dead blobs of an abandoned upload are
        asked for like any other; the stream walk at the end leaves them out when each source
        is judged, so they cost a few requests and change no answer."""
        now = time.time()
        retention_seconds = RETENTION_SLOTS * self.cfg.chain.seconds_per_slot
        in_retention = {r.seq for r in refs if r.block_timestamp and now - r.block_timestamp < retention_seconds}
        groups: dict[tuple, list[BlobRef]] = {}
        for r in refs:
            groups.setdefault((r.block_number, r.block_timestamp), []).append(r)
        named = self._named_sources()
        for src, _ in named:
            if hasattr(src, "set_app_pointer"):
                src.set_app_pointer(state.app_pointer)

        # What the page sees while the check runs: every source with where it stands, as
        # copies that are never changed after they are published.
        lock = threading.Lock()
        live: dict[str, dict] = {name: {"status": "queued"} for _, name in named}

        def publish(name: str, row: dict) -> None:
            with lock:
                live[name] = _row_copy(row)
                answered = sum(1 for m in live.values() if m.get("status") == "done" or "skipped" in m)
                report.sources = {"matrix": dict(live), "blobs": len(refs)}
                # One source holding every blob settles the question before the rest answer.
                # The verdict is judged again, on what a reader needs, once everything is in.
                if report.verdict == "running" and any(m.get("status") == "done" and m.get("complete") for m in live.values()):
                    report.verdict = "recoverable"
                self._step(report, "blob check", f"{answered} of {len(named)} sources answered")

        def ask(src, name: str) -> tuple[dict, dict[int, bytes]]:
            """One source's whole pass: its row of counts and the blobs it verified, by sequence."""
            found: dict[int, bytes] = {}
            skipped = self._not_asked(src, state, refs)
            if skipped:
                row = {**_empty_counts(), "skipped": skipped}
                log(f"{name}: not asked, {skipped}")
                publish(name, row)
                return row, found
            counts = _empty_counts(asked=len(refs))
            counts["status"] = "in progress"
            publish(name, counts)
            log(f"fetching {len(refs)} blob(s) from {name}")
            for (bn, bt), group in groups.items():
                wanted = [r.versioned_hash for r in group]
                try:
                    candidates = src.fetch(BlobContext(bn, bt), wanted)
                except Exception as e:
                    counts["error"] += len(group)
                    counts["answered"] += len(group)
                    counts.setdefault("lastError", redact(str(e), src)[:200])
                    publish(name, counts)
                    continue
                accepted, rejected = verify_candidates(candidates, set(wanted))
                counts["corrupt"] += rejected
                # A source that is asked blob by blob says which requests failed; such a blob
                # is an error for this source, not one the source is known to lack.
                failed = getattr(src, "last_errors", None) or {}
                for r in group:
                    if r.versioned_hash in accepted:
                        counts["verified"] += 1
                        counts["verifiedSeqs"].append(r.seq)
                        found.setdefault(r.seq, accepted[r.versioned_hash])
                    elif r.versioned_hash in failed:
                        counts["error"] += 1
                        counts.setdefault("lastError", redact(failed[r.versioned_hash], src)[:200])
                    else:
                        counts["missing"] += 1
                        counts["missingSeqs"].append(r.seq)
                        if r.seq not in in_retention:
                            counts["missingOutsideRetention"] += 1
                            counts["staleSeqs"].append(r.seq)
                counts["answered"] += len(group)
                publish(name, counts)
            counts["reachable"] = not refs or counts["error"] < len(refs)   # some request got an answer
            # Provisional, so a finished row reads like a result while the others are still
            # asked: complete means every blob was verified. The stream walk at the end
            # judges every row again on the blobs a reader needs.
            counts["complete"] = counts["verified"] == len(refs)
            counts["status"] = "done"
            log(f"{name}: {counts['verified']} verified, {counts['missing']} missing, {counts['corrupt']} corrupt")
            publish(name, counts)
            return counts, found

        with ThreadPoolExecutor(max_workers=max(1, len(named)), thread_name_prefix="source") as pool:
            results = list(pool.map(lambda pair: ask(*pair), named))

        blobs: list[bytes | None] = [None] * len(refs)
        matrix: dict[str, dict] = {}
        for (_, name), (row, found) in zip(named, results):
            row.pop("status", None)   # the finished report carries results, not progress
            matrix[name] = row
            for seq, blob in found.items():
                if blobs[seq] is None:
                    blobs[seq] = blob
        unavailable = [i for i, b in enumerate(blobs) if b is None]
        report.sources = {"matrix": matrix, "blobs": len(refs), "verifiedFromAnySource": len(refs) - len(unavailable), "unavailable": unavailable, "withinRetention": len(in_retention)}
        if refs and not self.cfg.sources:
            report.problems.append("no blob sources are configured, so availability cannot be checked")
        # Whether an unavailable blob matters is decided once the stream's structure is known:
        # the dead blobs of an abandoned upload are not needed by anyone.
        for name, c in matrix.items():
            if c["corrupt"]:
                report.notes.append(f"{name} served {c['corrupt']} corrupt or wrong blob(s); they were rejected")
        return blobs

    def _named_sources(self) -> list[tuple]:
        """The configured sources with the name each goes by on the page, in order. Two
        sources of one kind on one host keep separate rows."""
        named: list[tuple] = []
        for src in self.cfg.sources:
            name = public_source(src)
            taken = [n for _, n in named]
            if name in taken:
                name = f"{name} #{sum(1 for k in taken if k.startswith(name)) + 1}"
            named.append((src, name))
        return named

    @staticmethod
    def _not_asked(src, state, refs: list[BlobRef]) -> str | None:
        """Why a source has nothing to be asked for, or None. Such a source is neither
        reachable nor unreachable; its row says why it was not asked. That is every source
        before the first blob is published or while the blob list could not be verified,
        and the IPFS source until the first batch commits a snapshot."""
        if state.blob_count == 0:
            return "no blobs published yet"
        if not refs:
            return "the blob list could not be verified, so there is nothing to ask for"
        if isinstance(src, IpfsSource) and not state.app_pointer:
            return "the contract names no IPFS snapshot yet"
        return None

    def _stream(self, report: Report, blobs: list[bytes | None], state, log) -> None:
        if not blobs:
            report.stream = {"batches": 0}
            return
        log("walking the stream's structure")
        structure = read_structure(blobs)
        unavailable = [i for i, b in enumerate(blobs) if b is None]
        dead = structure.dead_blobs()
        needed = [i for i in unavailable if i not in dead]
        # Each source is judged on the blobs a reader needs: the dead blobs of an abandoned
        # upload do not count against it. A source that holds every needed blob is complete.
        # The report may be serialised by another thread while a check runs, so sections
        # are replaced whole rather than edited in place.
        needed_total = len(blobs) - len(dead)
        matrix = {}
        for name, row in report.sources.get("matrix", {}).items():
            m = {k: v for k, v in row.items() if k not in SEQ_LISTS}
            if "skipped" in row:
                matrix[name] = m
                continue
            # Judged on the blobs a reader needs: a dead blob a source lacks is not missing.
            alive = lambda key: [i for i in row.get(key, []) if i not in dead]
            m["neededVerified"] = len(alive("verifiedSeqs"))
            m["missing"] = len(alive("missingSeqs"))
            m["missingOutsideRetention"] = len(alive("staleSeqs"))
            m["complete"] = m["neededVerified"] == needed_total
            matrix[name] = m
        report.sources = {**report.sources, "matrix": matrix, "needed": needed_total, "neededUnavailable": needed}
        if needed:
            report.problems.append(f"{len(needed)} blob(s) not verifiable from any configured source: {needed[:10]}{'…' if len(needed) > 10 else ''}")
        if len(needed) < len(unavailable):
            report.notes.append(f"{len(unavailable) - len(needed)} blob(s) of an abandoned upload are unavailable; a later batch skips them and no reader needs them")
        report.stream = {
            "header": None if structure.header is None else {"version": f"{structure.header.major}.{structure.header.minor}", "chainId": structure.header.chain_id, "contract": "0x" + structure.header.contract.hex(), "schemaId": structure.header.schema_id},
            "batches": len(structure.batches),
            "batchList": [{"batch": b.batch, "firstBlobSeq": b.first_blob_seq, "blobCountAfter": b.blob_count_after, "codec": b.codec, "compressedBytes": b.compressed_length, "uncompressedBytes": b.uncompressed_length, "hasIndex": b.has_index} for b in structure.batches],
            "torn": [t.__dict__ for t in structure.torn],
            "deadBlobs": len(dead),
            "organisms": structure.organisms(),
            # The dataset's size, from the batch headers: no file is materialised for it.
            "rawBytes": sum(b.uncompressed_length for b in structure.batches),
            "compressedBytes": sum(b.compressed_length for b in structure.batches),
            "entriesTotal": sum(o.get("entriesTotal", 0) for o in structure.organisms().values()),
            "withdrawnTotal": sum(o.get("withdrawnTotal", 0) for o in structure.organisms().values()),
        }
        if structure.header is not None:
            if structure.header.chain_id != report.chain.get("chainId"):
                report.problems.append(f"the stream header names chain {structure.header.chain_id} but the node serves chain {report.chain.get('chainId')}")
            if structure.header.contract.hex().lower() != self.cfg.chain.contract[2:].lower():
                report.problems.append("the stream header names a different contract than the one being checked")
        tail = structure.torn_tail()
        if tail is not None:
            report.problems.append(f"torn batch at the end of the stream (blobs {tail.first_blob} to {tail.last_blob}): {tail.reason}; an upload was interrupted or its blobs are unavailable")
        for t in structure.torn:
            if t is not tail:
                report.notes.append(f"a torn batch inside the stream (blobs {t.first_blob} to {t.last_blob}) was skipped, as the format requires")
        log(f"{len(structure.batches)} complete batch(es), {report.stream['entriesTotal']:,} entries across {len(structure.organisms())} organisms")
