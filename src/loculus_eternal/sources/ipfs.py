"""IPFS source: blob objects located through a snapshot's manifest.

A blob object's CID is the sha2-256 of its bytes, which cannot be derived from the versioned
hash the chain records, so IPFS recovery starts from a snapshot CID learned off-chain. The
snapshot is trusted only after its CID hashes to the contract's `appPointer`; its manifest
then maps every versioned hash to a blob-object CID, and every blob fetched is verified like
any other. Endpoints are Kubo RPC API URLs.
"""

from __future__ import annotations

import httpx

from loculus_eternal.chain import BlobRef
from loculus_eternal.ipfs import IpfsError, KuboClient, app_pointer, parse_manifest
from loculus_eternal.sources.base import BlobContext, SourceError


class IpfsSource:
    def __init__(self, endpoints: list[str], snapshot_cid: str | None, *, timeout: float = 300.0, client: httpx.Client | None = None):
        if not endpoints:
            raise ValueError("at least one IPFS endpoint is required")
        self.endpoints = list(endpoints)
        self.clients = [KuboClient(e, timeout=timeout, client=client) for e in endpoints]
        self.snapshot_cid = snapshot_cid
        self.expected_pointer: bytes | None = None
        self.manifest: dict | None = None
        self._cid_by_hash: dict[bytes, str] = {}

    @property
    def name(self) -> str:
        cid = (self.snapshot_cid or "no snapshot")[:16]
        return f"ipfs({cid}…; " + ", ".join(self.endpoints) + ")"

    def set_snapshot(self, snapshot_cid: str | None) -> None:
        """Point the source at another snapshot (the upload command's latest, for instance)."""
        if snapshot_cid != self.snapshot_cid:
            self.snapshot_cid = snapshot_cid
            self.manifest = None
            self._cid_by_hash = {}

    def set_app_pointer(self, pointer: bytes) -> None:
        """Called by the recovery command with the contract's pointer at the finalized block."""
        self.expected_pointer = pointer

    def _load_manifest(self) -> dict:
        if self.manifest is not None:
            return self.manifest
        if self.snapshot_cid is None:
            raise SourceError("no snapshot CID: the IPFS source cannot locate blobs without one")
        if self.expected_pointer is None:
            raise SourceError("the snapshot has not been checked against the contract's appPointer; refusing to trust it")
        if app_pointer(self.snapshot_cid) != self.expected_pointer:
            raise SourceError(f"snapshot {self.snapshot_cid} does not match the contract's appPointer; refusing its manifest")
        errors = []
        for c in self.clients:
            try:
                self.manifest = parse_manifest(c.cat(f"{self.snapshot_cid}/manifest.json"))
                break
            except (IpfsError, ValueError) as e:
                errors.append(f"{c.api_url}: {e}")
        if self.manifest is None:
            raise SourceError("cannot read the snapshot manifest: " + "; ".join(errors))
        self._cid_by_hash = {bytes.fromhex(b["versionedHash"][2:]): b["cid"] for b in self.manifest["blobs"]}
        return self.manifest

    def blob_refs(self) -> list[BlobRef]:
        """The manifest's blob list, for use as a manifest source (verified by the caller)."""
        m = self._load_manifest()
        return [BlobRef(seq=int(b["seq"]), versioned_hash=bytes.fromhex(b["versionedHash"][2:]), block_number=b.get("blockNumber"), block_timestamp=b.get("blockTimestamp")) for b in m["blobs"]]

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        self._load_manifest()
        out: list[bytes] = []
        for vh in wanted:
            cid = self._cid_by_hash.get(vh)
            if cid is None:
                continue
            for c in self.clients:
                try:
                    out.append(c.block_get(cid))
                    break
                except IpfsError:
                    continue
        return out
