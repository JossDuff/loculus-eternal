"""IPFS source: blob objects located through a snapshot's manifest.

A blob object's CID is the sha2-256 of its bytes, which cannot be derived from the versioned
hash the chain records, so IPFS recovery starts from the snapshot. The snapshot's CID is the
contract's `appPointer`, read at the start of every run; no configuration names a snapshot,
so none can go stale. The snapshot's manifest maps every versioned hash to a blob-object
CID, and every blob fetched is verified like any other, so the snapshot is trusted for
nothing but locating bytes. Endpoints are Kubo RPC API URLs.
"""

from __future__ import annotations

import httpx

from loculus_eternal.chain import BlobRef
from loculus_eternal.ipfs import IpfsError, KuboClient, parse_manifest
from loculus_eternal.sources.base import BlobContext, SourceError


class IpfsSource:
    def __init__(self, endpoints: list[str], *, timeout: float = 300.0, client: httpx.Client | None = None):
        if not endpoints:
            raise ValueError("at least one IPFS endpoint is required")
        self.endpoints = list(endpoints)
        self.clients = [KuboClient(e, timeout=timeout, client=client) for e in endpoints]
        self.snapshot_cid: str | None = None    # set from the contract's pointer at the start of every run
        self._manifest_error: str | None = None
        self.manifest: dict | None = None
        self._cid_by_hash: dict[bytes, str] = {}

    @property
    def name(self) -> str:
        cid = (self.snapshot_cid or "no snapshot")[:16]
        return f"ipfs({cid}…; " + ", ".join(self.endpoints) + ")"

    def set_app_pointer(self, pointer: str) -> None:
        """Called at the start of every run with the contract's pointer at the finalized
        block: the snapshot CID, or an empty string before the first batch is committed. A
        manifest read for another snapshot is dropped, and a manifest failure remembered
        from an earlier run is forgotten here: the node may be back."""
        cid = pointer or None
        if cid != self.snapshot_cid:
            self.snapshot_cid = cid
            self.manifest = None
            self._cid_by_hash = {}
        self._manifest_error = None

    def _load_manifest(self) -> dict:
        if self.manifest is not None:
            return self.manifest
        if self._manifest_error is not None:
            # Asked once per block group: a manifest that could not be read is not retried.
            raise SourceError(self._manifest_error)
        if self.snapshot_cid is None:
            raise SourceError("the contract names no IPFS snapshot: the IPFS source cannot locate blobs without one")
        errors = []
        for c in self.clients:
            try:
                self.manifest = parse_manifest(c.cat(f"{self.snapshot_cid}/manifest.json"))
                break
            except (IpfsError, ValueError) as e:
                errors.append(f"{c.api_url}: {e}")
        if self.manifest is None:
            self._manifest_error = "cannot read the snapshot manifest: " + "; ".join(errors)
            raise SourceError(self._manifest_error)
        # A blob the publisher could not obtain (dead bytes of an abandoned upload) has no CID.
        self._cid_by_hash = {bytes.fromhex(b["versionedHash"][2:]): b["cid"] for b in self.manifest["blobs"] if b.get("cid")}
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
