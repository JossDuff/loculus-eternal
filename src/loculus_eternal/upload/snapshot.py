"""Building and publishing the IPFS snapshot and blob objects for a batch.

The snapshot is built from the stream as it will stand after the batch, before anything is
sent, because the batch-end transaction carries the pointer to it. IPFS is a hedge, not a
requirement: if no endpoint accepts the snapshot the batch still publishes with the previous
pointer carried forward, and the report says so, unless the configuration marks IPFS as
required.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import zstandard

from loculus_eternal import kzg
from loculus_eternal.chain import chain_head
from loculus_eternal.config import IpfsConfig
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.entrystore import safe_dirname
from loculus_eternal.ipfs import MANIFEST_NAME, SPEC_NAME, IpfsError, KuboClient, app_pointer, blob_cid, publish_blobs, snapshot_manifest
from loculus_eternal.store import BlobStore

SNAPSHOT_ZSTD_LEVEL = 9


@dataclass
class SnapshotResult:
    snapshot_cid: str | None
    pointer: bytes | None
    blob_cids: list[str]
    endpoints: list[dict] = field(default_factory=list)   # {"url", "snapshot": ok/error, "blobs": n or error}
    files: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"snapshotCid": self.snapshot_cid, "appPointer": None if self.pointer is None else "0x" + self.pointer.hex(), "blobObjects": len(self.blob_cids), "endpoints": self.endpoints, "files": self.files}


def build_and_publish(cfg: IpfsConfig, *, chain_id: int, contract: str, store: BlobStore, refs, batch, work_dir: Path, log: Callable[[str], None]) -> SnapshotResult:
    """Decode the stream as it will stand after `batch`, write the snapshot files, add them
    and the batch's blob objects to every endpoint, and return the pointer to store on-chain.

    `refs` are the chain's BlobRefs for the blobs already published (with block numbers and
    timestamps), so the manifest can carry them; the new batch's blobs get theirs later.
    """
    existing_blob_count = len(refs)
    if not cfg.spec_path.is_file():
        raise IpfsError(f"the container spec is a fixed member of the snapshot but was not found at {cfg.spec_path}")
    all_blobs = store.blobs(existing_blob_count) + list(batch.blobs)
    work_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes | Path] = {}
    with tempfile.TemporaryDirectory(prefix="snapshot-", dir=work_dir) as tmp:
        tmp_path = Path(tmp)
        with StreamDecoder(all_blobs, spill_dir=tmp_path / "spill").decode() as decoded:
            if decoded.torn and decoded.torn[-1].last_blob >= existing_blob_count:
                raise IpfsError("the planned batch does not decode cleanly on top of the published stream; refusing to build a snapshot for it")
            for org in decoded.organisms():
                path = tmp_path / f"{safe_dirname(org)}.ndjson.zst"
                # The level is not normative (the bytes are kept by CID, not regenerated), so
                # a fast level with every core beats level 19 on gigabytes of NDJSON.
                with open(path, "wb") as raw, zstandard.ZstdCompressor(level=SNAPSHOT_ZSTD_LEVEL, threads=-1).stream_writer(raw) as out:
                    decoded.materialise_to(org, out)
                files[path.name] = path
            batches = [{"batch": b.batch, "firstBlobSeq": b.first_blob_seq, "lastBlobSeq": b.blob_count_after - 1, "manifestDigest": "0x" + b.manifest_digest.hex()} for b in decoded.batches]
        # Versioned hashes: the store already holds them for published blobs; only the new
        # batch's blobs need the KZG computation.
        hashes = [store.have[i] for i in range(existing_blob_count)] + [kzg.blob_to_versioned_hash(b) for b in batch.blobs]
        cids = [blob_cid(b) for b in all_blobs]
        by_seq = {r.seq: r for r in refs}
        manifest_blobs = []
        for seq, (vh, cid) in enumerate(zip(hashes, cids)):
            entry = {"seq": seq, "versionedHash": "0x" + vh.hex(), "cid": cid}
            r = by_seq.get(seq)
            if r is not None and r.block_number is not None:
                entry["blockNumber"] = r.block_number
                entry["blockTimestamp"] = r.block_timestamp
            manifest_blobs.append(entry)
        files[MANIFEST_NAME] = snapshot_manifest(chain_id=chain_id, contract=contract, blob_count=len(all_blobs), head=chain_head(hashes), batches=batches, blobs=manifest_blobs)
        files[SPEC_NAME] = cfg.spec_path.read_bytes()

        result = SnapshotResult(snapshot_cid=None, pointer=None, blob_cids=cids[existing_blob_count:], files=sorted(files))
        for url in cfg.endpoints:
            client = KuboClient(url)
            entry: dict = {"url": url}
            try:
                cid = client.add_directory("snapshot", files)
                if result.snapshot_cid is None:
                    result.snapshot_cid = cid
                elif cid != result.snapshot_cid:
                    raise IpfsError(f"endpoint returned snapshot CID {cid}, another returned {result.snapshot_cid}; the nodes disagree on the profile")
                entry["snapshot"] = "ok"
            except IpfsError as e:
                entry["snapshot"] = f"error: {e}"
                log(f"IPFS endpoint {url} did not take the snapshot: {e}")
            try:
                publish_blobs(client, batch.blobs)
                entry["blobs"] = len(batch.blobs)
            except IpfsError as e:
                entry["blobs"] = f"error: {e}"
                log(f"IPFS endpoint {url} did not take the blob objects: {e}")
            result.endpoints.append(entry)
    if result.snapshot_cid is not None:
        result.pointer = app_pointer(result.snapshot_cid)
        log(f"snapshot {result.snapshot_cid} added to {sum(1 for e in result.endpoints if e.get('snapshot') == 'ok')} endpoint(s); pointer 0x{result.pointer.hex()[:16]}…")
    elif cfg.endpoints:
        msg = "no IPFS endpoint accepted the snapshot"
        if cfg.required:
            raise IpfsError(msg + " and [ipfs].required is set")
        log(msg + "; publishing with the previous pointer carried forward")
    return result


def unpin_orphans(result_json: dict, blob_cids: list[str], log: Callable[[str], None]) -> list[str]:
    """A batch was refused or abandoned after its snapshot and blob objects were added: remove
    those pins from every endpoint that took them, so nothing unpublished lingers."""
    cleaned = []
    for e in result_json.get("endpoints", []):
        client = KuboClient(e["url"])
        failures = 0
        targets = []
        if e.get("snapshot") == "ok" and result_json.get("snapshotCid"):
            targets.append(result_json["snapshotCid"])
        if isinstance(e.get("blobs"), int):
            targets.extend(blob_cids)
        for cid in targets:
            try:
                # `block put --pin` pins recursively even for a leaf, so recursive removal
                # is right for blob objects as well as for the snapshot directory.
                client.pin_rm(cid)
            except IpfsError as err:
                failures += 1
                log(f"could not remove pin {cid} on {e['url']}: {err}")
        if targets and not failures:
            cleaned.append(e["url"])
    return cleaned
