"""IPFS source: blob objects located through a snapshot's manifest.

A blob object's CID is the sha2-256 of its bytes, which cannot be derived from the versioned
hash the chain records, so IPFS recovery starts from a snapshot CID learned off-chain. The
snapshot is trusted only after its CID hashes to the contract's `appPointer`; its manifest
then maps every versioned hash to a blob-object CID, and every blob fetched is verified like
any other. Endpoints are Kubo RPC URLs; a plain HTTP gateway works too for `block/get`
through `/ipfs/<cid>?format=raw`.
"""

from __future__ import annotations

import httpx

from loculus_eternal.chain import BlobRef
from loculus_eternal.ipfs import IpfsError, KuboClient, app_pointer, parse_manifest
from loculus_eternal.sources.base import BlobContext, SourceError


class IpfsSource:
    def __init__(self, endpoints: list[str], snapshot_cid: str, *, timeout: float = 300.0, client: httpx.Client | None = None):
        if not endpoints:
            raise ValueError("at least one IPFS endpoint is required")
        self.clients = [KuboClient(e, timeout=timeout, client=client) for e in endpoints]
        self.snapshot_cid = snapshot_cid
        self.expected_pointer: bytes | None = None
        self.manifest: dict | None = None
        self._cid_by_hash: dict[bytes, str] = {}
        self.name = f"ipfs({snapshot_cid[:16]}…; " + ", ".join(e for e in endpoints) + ")"

    def set_app_pointer(self, pointer: bytes) -> None:
        """Called by the recovery command with the contract's pointer at the finalized block."""
        self.expected_pointer = pointer

    def _load_manifest(self) -> dict:
        if self.manifest is not None:
            return self.manifest
        if self.expected_pointer is not None and app_pointer(self.snapshot_cid) != self.expected_pointer:
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
        return [BlobRef(seq=int(b["seq"]), versioned_hash=bytes.fromhex(b["versionedHash"][2:])) for b in m["blobs"]]

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
