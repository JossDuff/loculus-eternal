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

import time
from dataclasses import dataclass, field
from typing import Callable

from loculus_eternal.chain import BlobRef, ChainReader, ManifestMismatch, verify_manifest
from loculus_eternal.config import Config
from loculus_eternal.format.structure import read_structure
from loculus_eternal.ipfs import app_pointer
from loculus_eternal.rpc import connect
from loculus_eternal.sources import IpfsSource
from loculus_eternal.sources.base import BlobContext, verify_candidates

RETENTION_SECONDS = 4096 * 32 * 12   # 4096 epochs of 32 slots of 12 seconds, about 18.2 days
STEPS = ["contract check", "blob check"]   # how the page groups the work


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


class HealthCheck:
    def __init__(self, config: Config, *, log: Callable[[str], None] | None = None, progress: Callable[[dict], None] | None = None, w3=None):
        self.cfg = config
        self.w3 = w3 or connect(config.chain.rpc_url, max_rps=config.chain.max_requests_per_second, timeout=60)
        self._log = log or (lambda s: None)
        self._progress = progress or (lambda p: None)

    def _step(self, report: Report, step: str, detail: str | None = None) -> None:
        """Where the check is, for a page that shows progress: the step and a line of detail."""
        report.progress = {"steps": STEPS, "step": step, "detail": detail}
        self._progress(report.progress)

    def run(self) -> Report:
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
        if report.verdict == "running":
            report.verdict = "not recoverable" if self._unrecoverable(report) else "recoverable"
        return report

    @staticmethod
    def _unrecoverable(report: Report) -> bool:
        """The one question the page answers: can the dataset be rebuilt from what is out
        there? It cannot when the contract cannot be read, when the blob list does not
        reproduce the chain's head, when a blob a reader needs is unavailable from every
        source, when the blobs do not parse as a stream, or when the check itself broke.
        A dead archive, a stopped IPFS node or a stale snapshot CID are listed as problems
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
        block = self.w3.eth.get_block(state.block_number)
        report.chain = {
            "contract": self.cfg.chain.contract,
            "chainId": self.w3.eth.chain_id,
            "finalizedBlock": state.block_number,
            "finalizedAgeSeconds": int(time.time() - block["timestamp"]),
            "publisher": state.publisher,
            "blobCount": state.blob_count,
            "head": "0x" + state.head.hex(),
            "appPointer": "0x" + state.app_pointer.hex(),
            "successor": state.successor,
        }
        if int(state.successor, 16) != 0:
            report.notes.append(f"a successor contract is set: {state.successor}; this deployment may have been replaced")
        # The configured snapshot CID is the publisher's only if its hash is the contract's
        # pointer; that costs no request. The blob check then uses the snapshot as a source.
        cid = next((s.snapshot_cid for s in self.cfg.sources if isinstance(s, IpfsSource) and s.snapshot_cid), None)
        if cid:
            report.chain["snapshotCid"] = cid
            report.chain["snapshotMatchesPointer"] = app_pointer(cid) == state.app_pointer
            if not report.chain["snapshotMatchesPointer"]:
                report.problems.append("the configured snapshot CID does not match the contract's appPointer: the snapshot is stale or not the publisher's")
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
            report.chain["lastPublishAgeSeconds"] = int(time.time() - last.block_timestamp) if last.block_timestamp else None
        return state, refs

    def _sources(self, report: Report, refs: list[BlobRef], state, log) -> list[bytes | None]:
        """Ask every source for every blob a reader needs; keep the first verified copy of each.

        Which blobs are dead (an abandoned upload a later batch skips) is learned from the
        record's own structure, which lives in the blobs. So the first source is asked for
        everything; from then on the structure of what is in hand is read before each source,
        and blobs already known to be dead are not asked for again. When too little is in
        hand to read the structure, nothing is known to be dead and everything is asked for,
        which is the safe direction."""
        blobs: list[bytes | None] = [None] * len(refs)
        now = time.time()
        in_retention = {r.seq for r in refs if r.block_timestamp and now - r.block_timestamp < RETENTION_SECONDS}
        matrix: dict[str, dict] = {}
        for src in self.cfg.sources:
            if hasattr(src, "set_app_pointer"):
                src.set_app_pointer(state.app_pointer)
            dead = read_structure(blobs).dead_blobs() if any(b is not None for b in blobs) else set()
            to_ask = [r for r in refs if r.seq not in dead]
            counts = {"verified": 0, "missing": 0, "corrupt": 0, "error": 0, "missingOutsideRetention": 0, "verifiedSeqs": [], "asked": len(to_ask)}
            log(f"fetching {len(to_ask)} blob(s) from {src.name}" + (f" ({len(dead)} dead blobs of an abandoned upload not asked for)" if dead else ""))
            groups: dict[tuple, list[BlobRef]] = {}
            for r in to_ask:
                groups.setdefault((r.block_number, r.block_timestamp), []).append(r)
            asked = 0
            for (bn, bt), group in groups.items():
                self._step(report, "blob check", f"{src.name}: {asked:,} of {len(to_ask):,} blobs asked for")
                asked += len(group)
                wanted = [r.versioned_hash for r in group]
                try:
                    candidates = src.fetch(BlobContext(bn, bt), wanted)
                except Exception as e:
                    counts["error"] += len(group)
                    counts.setdefault("lastError", str(e)[:200])
                    continue
                accepted, rejected = verify_candidates(candidates, set(wanted))
                counts["corrupt"] += rejected
                for r in group:
                    if r.versioned_hash in accepted:
                        counts["verified"] += 1
                        counts["verifiedSeqs"].append(r.seq)
                        if blobs[r.seq] is None:
                            blobs[r.seq] = accepted[r.versioned_hash]
                    else:
                        counts["missing"] += 1
                        if r.seq not in in_retention:
                            counts["missingOutsideRetention"] += 1
            counts["reachable"] = counts["error"] < len(to_ask)   # some request got an answer
            matrix[src.name] = counts
            log(f"{src.name}: {counts['verified']} verified, {counts['missing']} missing, {counts['corrupt']} corrupt")
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
        needed_total = len(blobs) - len(dead)
        report.sources["needed"] = needed_total
        report.sources["neededUnavailable"] = needed
        for m in report.sources.get("matrix", {}).values():
            m["neededVerified"] = sum(1 for i in m.pop("verifiedSeqs", []) if i not in dead)
            m["complete"] = m["neededVerified"] == needed_total
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
            "entriesTotal": sum(o.get("entriesTotal", 0) for o in structure.organisms().values()),
            "withdrawnTotal": sum(o.get("withdrawnTotal", 0) for o in structure.organisms().values()),
        }
        if structure.header is not None:
            if structure.header.chain_id != report.chain.get("chainId"):
                report.problems.append(f"the stream header names chain {structure.header.chain_id} but the node serves chain {report.chain.get('chainId')}")
            if structure.header.contract.hex().lower() != self.cfg.chain.contract[2:].lower():
                report.problems.append("the stream header names a different contract than the one being checked")
        if structure.batches and structure.batches[-1].blob_count_after != state.blob_count and not structure.torn_tail():
            report.problems.append("the last complete batch does not reach the chain's blob count")
        tail = structure.torn_tail()
        if tail is not None:
            report.problems.append(f"torn batch at the end of the stream (blobs {tail.first_blob} to {tail.last_blob}): {tail.reason}; an upload was interrupted or its blobs are unavailable")
        for t in structure.torn:
            if t is not tail:
                report.notes.append(f"a torn batch inside the stream (blobs {t.first_blob} to {t.last_blob}) was skipped, as the format requires")
        log(f"{len(structure.batches)} complete batch(es), {report.stream['entriesTotal']:,} entries across {len(structure.organisms())} organisms")
