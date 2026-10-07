"""The health check: is the published record still available, and still correct?

One check reads the contract, verifies the blob list against the chain, fetches every blob
from every configured source and verifies each against its versioned hash, walks the
stream's structure, checks the IPFS snapshot against the on-chain pointer, and compares the
backend's released counts with what is published. It materialises nothing. The result is a
plain report with a verdict and the reasons for it, meant for a third party who wants to
confirm, without trusting anyone, that the data is still there and still right.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

import httpx

from loculus_eternal import kzg
from loculus_eternal.chain import BlobRef, ChainReader, ManifestMismatch, chain_head, verify_manifest
from loculus_eternal.config import Config
from loculus_eternal.format.structure import read_structure
from loculus_eternal.ipfs import IpfsError, KuboClient, app_pointer, parse_manifest
from loculus_eternal.rpc import connect
from loculus_eternal.sources import IpfsSource
from loculus_eternal.sources.base import BlobContext, verify_candidates

RETENTION_SECONDS = 4096 * 32 * 12   # 4096 epochs of 32 slots of 12 seconds, about 18.2 days


@dataclass
class Report:
    started_at: float
    finished_at: float | None = None
    verdict: str = "running"            # running, healthy, degraded, failing
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    chain: dict = field(default_factory=dict)
    blob_list: dict = field(default_factory=dict)
    sources: dict = field(default_factory=dict)
    stream: dict = field(default_factory=dict)
    ipfs: dict = field(default_factory=dict)
    backend: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return dict(self.__dict__)


class HealthCheck:
    def __init__(self, config: Config, *, log: Callable[[str], None] | None = None, w3=None, http: httpx.Client | None = None):
        self.cfg = config
        self.w3 = w3 or connect(config.chain.rpc_url, max_rps=config.chain.max_requests_per_second, timeout=60)
        self.http = http or httpx.Client(timeout=60)
        self._log = log or (lambda s: None)

    def run(self) -> Report:
        report = Report(started_at=time.time())

        def log(msg: str) -> None:
            report.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
            self._log(msg)

        try:
            state, refs = self._chain(report, log)
            if state is None:
                report.verdict = "failing"
                return self._finish(report)
            blobs = self._sources(report, refs, state, log)
            self._stream(report, blobs, state, log)
            self._ipfs(report, state, refs, log)
            self._backend(report, log)
        except Exception as e:  # a check must end with a verdict, never a traceback
            report.problems.append(f"the check itself failed: {type(e).__name__}: {e}")
            log(f"check failed: {e}")
        return self._finish(report)

    def _finish(self, report: Report) -> Report:
        report.finished_at = time.time()
        if report.verdict == "running":
            hard = [p for p in report.problems if not p.startswith("note:")]
            report.verdict = "healthy" if not hard else ("failing" if any(k in p for p in hard for k in ("cannot", "no verified", "not verifiable", "torn", "does not match", "check itself")) else "degraded")
        return report

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
        log(f"contract holds {state.blob_count} blobs, head 0x{state.head.hex()[:16]}…")

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
        """Ask every source for every blob; keep the first verified copy of each."""
        blobs: list[bytes | None] = [None] * len(refs)
        now = time.time()
        in_retention = {r.seq for r in refs if r.block_timestamp and now - r.block_timestamp < RETENTION_SECONDS}
        matrix: dict[str, dict] = {}
        for src in self.cfg.sources:
            if hasattr(src, "set_app_pointer"):
                src.set_app_pointer(state.app_pointer)
            counts = {"verified": 0, "missing": 0, "corrupt": 0, "error": 0, "missingOutsideRetention": 0}
            log(f"fetching every blob from {src.name}")
            groups: dict[tuple, list[BlobRef]] = {}
            for r in refs:
                groups.setdefault((r.block_number, r.block_timestamp), []).append(r)
            for (bn, bt), group in groups.items():
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
                        if blobs[r.seq] is None:
                            blobs[r.seq] = accepted[r.versioned_hash]
                    else:
                        counts["missing"] += 1
                        if r.seq not in in_retention:
                            counts["missingOutsideRetention"] += 1
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

    def _ipfs(self, report: Report, state, refs: list[BlobRef], log) -> None:
        ipfs_sources = [s for s in self.cfg.sources if isinstance(s, IpfsSource)]
        if not ipfs_sources or not ipfs_sources[0].snapshot_cid:
            report.ipfs = {"configured": False}
            report.notes.append("no IPFS snapshot CID is configured; the snapshot cannot be checked")
            return
        src = ipfs_sources[0]
        cid = src.snapshot_cid
        log(f"checking the IPFS snapshot {cid[:16]}…")
        result: dict = {"configured": True, "snapshotCid": cid, "pointerMatches": app_pointer(cid) == state.app_pointer}
        if not result["pointerMatches"]:
            report.problems.append("the configured snapshot CID does not match the contract's appPointer: the snapshot is stale or not the publisher's")
        client = KuboClient(src.endpoints[0])
        try:
            manifest = parse_manifest(client.cat(f"{cid}/manifest.json"))
            listed = [BlobRef(int(b["seq"]), bytes.fromhex(b["versionedHash"][2:])) for b in manifest["blobs"]]
            result["manifestBlobs"] = len(listed)
            try:
                verify_manifest(listed, state)
                result["manifestMatchesChain"] = True
            except ManifestMismatch as e:
                result["manifestMatchesChain"] = False
                report.problems.append(f"the snapshot's blob list does not match the chain: {e}")
            objects_ok = 0
            # A dead blob of an abandoned upload is listed without a CID; nobody needs it.
            with_cid = [b for b in manifest["blobs"] if b.get("cid")]
            for b in with_cid:
                try:
                    data = client.block_get(b["cid"])
                    if kzg.verify_blob(data, bytes.fromhex(b["versionedHash"][2:])):
                        objects_ok += 1
                except IpfsError:
                    pass
            result["blobObjectsRetrievable"] = objects_ok
            result["blobObjectsListed"] = len(with_cid)
            if objects_ok < len(with_cid):
                report.notes.append(f"{len(with_cid) - objects_ok} blob object(s) not retrievable from the IPFS endpoint")
            files = []
            for name in ("manifest.json", "container-spec.md"):
                try:
                    client.cat(f"{cid}/{name}")
                    files.append(name)
                except IpfsError:
                    report.problems.append(f"the snapshot lacks {name}")
            result["files"] = files
        except (IpfsError, ValueError, KeyError) as e:
            result["error"] = str(e)
            report.problems.append(f"cannot read the snapshot's manifest from IPFS: {e}")
        report.ipfs = result

    def _backend(self, report: Report, log) -> None:
        if self.cfg.backend is None:
            report.backend = {"configured": False}
            return
        log("asking the backend for its released counts")
        organisms = self.cfg.backend.organisms
        rows = {}
        try:
            if organisms is None:
                r = self.http.get(f"{self.cfg.backend.url}/api-docs", headers={"Accept": "application/json"})
                r.raise_for_status()
                organisms = sorted(r.json()["components"]["schemas"]["Organism"]["enum"])
            published = report.stream.get("organisms", {})
            for org in organisms:
                released = None
                try:
                    with self.http.stream("GET", f"{self.cfg.backend.url}/{org}/get-released-data", params={"compression": "zstd"}) as resp:
                        total = resp.headers.get("x-total-records")
                        released = int(total) if total else None
                except httpx.HTTPError as e:
                    rows[org] = {"error": str(e)[:120]}
                    continue
                p = published.get(org, {})
                rows[org] = {"released": released, "published": p.get("entriesTotal", 0), "withdrawn": p.get("withdrawnTotal", 0)}
            for org in published:
                if org not in rows:
                    rows[org] = {"released": None, "published": published[org].get("entriesTotal", 0), "withdrawn": published[org].get("withdrawnTotal", 0), "note": "no longer served by the backend"}
            report.backend = {"configured": True, "url": self.cfg.backend.url, "organisms": rows, "note": "released counts every version including restricted ones, which are never published; a gap is expected"}
        except Exception as e:
            report.backend = {"configured": True, "url": self.cfg.backend.url, "error": str(e)}
            report.notes.append(f"the backend could not be reached ({e}); this does not affect the record's availability")
