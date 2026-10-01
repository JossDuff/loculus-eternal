"""Recovery: chain state, verified blob list, verified blobs, decoded dataset, report.

Trusts nothing but the chain. The blob list may come from event logs, from a manifest file
someone handed over, or (later) from an IPFS snapshot; whichever it is, it is checked against
the contract's head. Every blob from every source is verified before it is written. Missing
blobs are a result, not a failure: the report says which ones and what was tried.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from web3 import Web3

from loculus_eternal.chain import BlobRef, ChainReader, ChainState, ManifestMismatch, verify_manifest
from loculus_eternal.format.decode import DecodedStream, StreamDecoder
from loculus_eternal.sources.base import BlobSource, SourceChain
from loculus_eternal.format.records import FormatError
from loculus_eternal.store import BlobStore, fill_store


@dataclass
class ManifestSource:
    """Where the ordered blob list may come from. Exactly one of the fields is set."""

    logs: bool = False
    file: Path | None = None

    @property
    def name(self) -> str:
        return "event logs" if self.logs else f"file {self.file}"


@dataclass
class RecoveryConfig:
    rpc_url: str
    contract: str
    data_dir: Path
    out_dir: Path
    sources: list[BlobSource]
    manifest_sources: list[ManifestSource] = field(default_factory=lambda: [ManifestSource(logs=True)])
    deployment_block: int = 0
    decode: bool = True
    log: Callable[[str], None] = print


@dataclass
class RecoveryReport:
    chain: dict
    manifest_source: str | None
    manifest_errors: list[str]
    blobs_total: int
    blobs_present: int
    blobs_fetched: int
    candidates_rejected: int
    missing: list[dict]
    decode: dict | None
    duration_seconds: float

    def to_json(self) -> dict:
        return self.__dict__


class Recovery:
    def __init__(self, config: RecoveryConfig, w3: Web3 | None = None):
        self.cfg = config
        self.w3 = w3 or Web3(Web3.HTTPProvider(config.rpc_url, request_kwargs={"timeout": 60}))
        self.reader = ChainReader(self.w3, config.contract)
        self.chain = SourceChain(config.sources)

    # --- steps -----------------------------------------------------------------------------

    def read_chain_state(self) -> ChainState:
        state = self.reader.state_at_finalized()
        self.cfg.log(f"contract {self.cfg.contract} at finalized block {state.block_number}: {state.blob_count} blobs, head 0x{state.head.hex()[:16]}…")
        return state

    def acquire_manifest(self, state: ChainState) -> tuple[list[BlobRef], str | None, list[str]]:
        """Try each manifest source in order; return the first list that reproduces head."""
        errors: list[str] = []
        for src in self.cfg.manifest_sources:
            try:
                if src.logs:
                    refs = self.reader.blob_refs_from_logs(self.cfg.deployment_block, state.block_number)
                else:
                    refs = _read_manifest_file(src.file)
                verify_manifest(refs, state)
            except (ManifestMismatch, OSError, ValueError, KeyError) as e:
                errors.append(f"{src.name}: {e}")
                self.cfg.log(f"blob list from {src.name} rejected: {e}")
                continue
            except Exception as e:  # transport failures and the like
                errors.append(f"{src.name}: {e}")
                self.cfg.log(f"blob list from {src.name} unavailable: {e}")
                continue
            self.cfg.log(f"blob list from {src.name} verified against head ({len(refs)} blobs)")
            return refs, src.name, errors
        return [], None, errors

    def fetch_blobs(self, refs: list[BlobRef], store: BlobStore) -> tuple[int, int, list[dict]]:
        """Fill the store from the sources. Returns (fetched, rejected, missing report)."""
        return fill_store(store, self.chain, refs, log=self.cfg.log)

    def decode(self, store: BlobStore, count: int) -> tuple[DecodedStream, dict]:
        decoded = StreamDecoder(store.blobs(count), spill_dir=self.cfg.data_dir / "spill").decode()
        out = self.cfg.out_dir
        out.mkdir(parents=True, exist_ok=True)
        last = decoded.batches[-1].manifest["organisms"] if decoded.batches else {}
        files = {}
        digests = {}
        for org in decoded.organisms():
            path = out / f"{org}.ndjson"
            with open(path, "wb", buffering=1 << 20) as f:
                digests[org] = decoded.materialise_to(org, f)
            files[org] = {"path": str(path), "entries": decoded.count(org), "sha256": digests[org], "matchesManifest": last.get(org, {}).get("artifactSha256") == digests[org]}
        verified = {o: last.get(o, {}).get("artifactSha256") == digests.get(o) for o in sorted(set(last) | set(digests))}
        report = {
            "header": None if decoded.header is None else {"chainId": decoded.header.chain_id, "contract": "0x" + decoded.header.contract.hex(), "schemaId": decoded.header.schema_id, "version": f"{decoded.header.major}.{decoded.header.minor}"},
            "batches": [{"batch": b.batch, "firstBlobSeq": b.first_blob_seq, "blobCountAfter": b.blob_count_after, "entries": b.entry_count, "hasIndex": b.index is not None} for b in decoded.batches],
            "torn": [t.__dict__ for t in decoded.torn],
            "warnings": decoded.warnings,
            "files": files,
            "allArtifactsMatch": bool(verified) and all(verified.values()),
            "tooling": sorted(decoded.tooling),
        }
        (out / "recovery-report.json").write_text(json.dumps(report, indent=1))
        return decoded, report

    # --- the whole thing ----------------------------------------------------------------------

    def run(self) -> RecoveryReport:
        started = time.time()
        state = self.read_chain_state()
        refs, manifest_source, manifest_errors = self.acquire_manifest(state)
        if manifest_source is None:
            self.cfg.log("no source produced a blob list that matches the chain; nothing fetched")
        with BlobStore(self.cfg.data_dir) as store:
            present_before = store.count()
            fetched, rejected, missing = self.fetch_blobs(refs, store) if refs else (0, 0, [])
            if manifest_source is not None:
                # Only a verified list is worth keeping: the saved manifest is the natural
                # fallback for a later run against a node that no longer serves old logs, so
                # it must never be replaced by an empty or unverified one.
                (self.cfg.data_dir / "missing.json").write_text(json.dumps(missing, indent=1))
                (self.cfg.data_dir / "manifest.json").write_text(json.dumps({"blobCount": state.blob_count, "head": "0x" + state.head.hex(), "blockNumber": state.block_number, "blobs": [r.to_json() for r in refs]}, indent=0))
            decode_report = None
            if self.cfg.decode and refs:
                if any(m["seq"] == 0 for m in missing):
                    decode_report = {"skipped": "blob 0 is missing, so the stream header cannot be read; nothing can be decoded until it is recovered", "allArtifactsMatch": False}
                    self.cfg.log(decode_report["skipped"])
                else:
                    try:
                        _, decode_report = self.decode(store, len(refs))
                    except FormatError as e:
                        decode_report = {"skipped": f"decoding failed: {e}", "allArtifactsMatch": False}
                        self.cfg.log(decode_report["skipped"])
            report = RecoveryReport(
                chain={"contract": self.cfg.contract, "blockNumber": state.block_number, "blobCount": state.blob_count, "head": "0x" + state.head.hex(), "appPointer": "0x" + state.app_pointer.hex(), "publisher": state.publisher},
                manifest_source=manifest_source,
                manifest_errors=manifest_errors,
                blobs_total=len(refs),
                blobs_present=store.count(),
                blobs_fetched=fetched,
                candidates_rejected=rejected,
                missing=missing,
                decode=decode_report,
                duration_seconds=round(time.time() - started, 2),
            )
        self.cfg.log(f"done: {report.blobs_present}/{report.blobs_total} blobs in the store ({fetched} fetched now, {rejected} candidates rejected, {len(missing)} missing, {present_before} were already there)")
        return report


def _read_manifest_file(path: Path) -> list[BlobRef]:
    """A manifest file is the `manifest.json` a recovery or snapshot writes: `{"blobs": [...]}`."""
    data = json.loads(Path(path).read_text())
    blobs = data["blobs"] if isinstance(data, dict) else data
    return [BlobRef.from_json(b) for b in blobs]
