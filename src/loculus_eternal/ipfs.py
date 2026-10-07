"""IPFS: per-blob objects, the snapshot directory, pinning, and the on-chain pointer.

Every pinner must derive identical content identifiers from identical bytes, so the rules
are fixed by `docs/ipfs-profile.md` and encoded here:

- a **blob object** is the blob's 131,072 bytes as one raw block: CIDv1, codec raw (0x55),
  multihash sha2-256. Its CID is computed locally and Kubo's answer must agree;
- a **snapshot** is a UnixFS directory added with CIDv1, raw leaves, chunker size-262144,
  sha2-256, holding `manifest.json`, the container spec, and each organism's file compressed
  with zstd;
- the **application pointer** stored on-chain is sha256 of the snapshot CID's binary form.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from loculus_eternal.format import varint
from loculus_eternal.format.chunks import BLOB_BYTES

CODEC_RAW = 0x55
CODEC_DAG_PB = 0x70
MULTIHASH_SHA2_256 = 0x12
SNAPSHOT_ADD_PARAMS = {
    "cid-version": "1",
    "raw-leaves": "true",
    "chunker": "size-262144",
    "hash": "sha2-256",
    "pin": "true",
    "wrap-with-directory": "false",
}
MANIFEST_NAME = "manifest.json"
SPEC_NAME = "container-spec.md"


class IpfsError(RuntimeError):
    pass


# --- CIDs -------------------------------------------------------------------------------------


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").lower().rstrip("=")


def _unb32(text: str) -> bytes:
    padded = text.upper() + "=" * (-len(text) % 8)
    return base64.b32decode(padded)


def cid_v1(codec: int, digest: bytes) -> str:
    """A CIDv1 string in base32 (multibase prefix 'b') for a sha2-256 digest."""
    raw = varint.encode(1) + varint.encode(codec) + bytes([MULTIHASH_SHA2_256, len(digest)]) + digest
    return "b" + _b32(raw)


def cid_bytes(cid: str) -> bytes:
    """The binary form of a base32 CIDv1 string, which is what the on-chain pointer hashes."""
    if not cid.startswith("b"):
        raise IpfsError(f"only base32 CIDv1 strings are used here, got {cid!r}")
    return _unb32(cid[1:])


def blob_cid(blob: bytes) -> str:
    if len(blob) != BLOB_BYTES:
        raise IpfsError(f"a blob object is exactly {BLOB_BYTES} bytes, got {len(blob)}")
    return cid_v1(CODEC_RAW, hashlib.sha256(blob).digest())


def app_pointer(snapshot_cid: str) -> bytes:
    return hashlib.sha256(cid_bytes(snapshot_cid)).digest()


# --- Kubo RPC ---------------------------------------------------------------------------------


_shared: dict[float, httpx.Client] = {}


def shared_client(timeout: float) -> httpx.Client:
    """One connection pool per timeout for every Kubo endpoint in the process."""
    if timeout not in _shared:
        _shared[timeout] = httpx.Client(timeout=timeout)
    return _shared[timeout]


class KuboClient:
    """The subset of the Kubo RPC API (`POST /api/v0/…`) this project uses."""

    def __init__(self, api_url: str, *, timeout: float = 300.0, client: httpx.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self.client = client or shared_client(timeout)

    def _post(self, path: str, params: dict | None = None, files=None, data=None) -> httpx.Response:
        try:
            r = self.client.post(f"{self.api_url}/api/v0/{path}", params=params, files=files, data=data)
        except httpx.HTTPError as e:
            raise IpfsError(f"{self.api_url}: {e}") from e
        if r.status_code != 200:
            raise IpfsError(f"{self.api_url}: {path} returned HTTP {r.status_code}: {r.text[:200]}")
        return r

    def version(self) -> str:
        return self._post("version").json()["Version"]

    def block_put(self, data: bytes) -> str:
        """Store one raw block and pin it. Returns the CID, which must match blob_cid(data)."""
        r = self._post("block/put", params={"cid-codec": "raw", "mhtype": "sha2-256", "pin": "true"}, files={"file": ("blob", data)})
        return r.json()["Key"]

    def block_get(self, cid: str, *, offline: bool = False) -> bytes:
        return self._post("block/get", params=self._read_params(cid, offline)).content

    def cat(self, path: str, *, offline: bool = False) -> bytes:
        return self._post("cat", params=self._read_params(path, offline)).content

    @staticmethod
    def _read_params(arg: str, offline: bool) -> dict:
        """A read that must say whether THIS node holds the content asks offline, with a
        short deadline; otherwise Kubo searches the public network for minutes."""
        return {"arg": arg, "offline": "true", "timeout": "20s"} if offline else {"arg": arg}

    def pin_add(self, cid: str) -> None:
        self._post("pin/add", params={"arg": cid, "recursive": "true"})

    def pin_rm(self, cid: str, *, recursive: bool = True) -> None:
        """Drop a pin (recursive for a snapshot directory, direct for a blob object). The
        blocks stay until the node's next garbage collection."""
        self._post("pin/rm", params={"arg": cid, "recursive": "true" if recursive else "false"})

    def pinned_cids(self) -> set[str]:
        """Every CID the node pins, directly or recursively, in one request."""
        return set(self._post("pin/ls", params={"type": "all"}).json().get("Keys", {}))

    def is_pinned(self, cid: str) -> bool:
        try:
            self._post("pin/ls", params={"arg": cid, "type": "all"})
            return True
        except IpfsError:
            return False

    def add_directory(self, name: str, files: dict[str, bytes | Path]) -> str:
        """Add a directory with the snapshot profile's fixed parameters; returns its CID."""
        parts = []
        for filename in sorted(files):
            content = files[filename]
            if isinstance(content, Path):
                parts.append(("file", (f"{name}/{filename}", open(content, "rb"), "application/octet-stream")))
            else:
                parts.append(("file", (f"{name}/{filename}", content, "application/octet-stream")))
        try:
            r = self._post("add", params=SNAPSHOT_ADD_PARAMS, files=parts)
        finally:
            for _, (_, handle, _) in parts:
                if hasattr(handle, "close"):
                    handle.close()
        root = None
        for line in r.text.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            if item.get("Name") == name:
                root = item["Hash"]
        if root is None:
            raise IpfsError("Kubo did not report the directory's CID")
        return root


# --- publishing -------------------------------------------------------------------------------


@dataclass
class IpfsResult:
    endpoint: str
    blob_cids: list[str]
    snapshot_cid: str | None
    error: str | None = None


def publish_blobs(client: KuboClient, blobs: Iterable[bytes]) -> list[str]:
    """Put every blob as a raw block, checking Kubo's CID against the locally computed one."""
    cids = []
    for blob in blobs:
        expected = blob_cid(blob)
        got = client.block_put(blob)
        if got != expected:
            raise IpfsError(f"Kubo returned CID {got} for a blob whose profile CID is {expected}; the node is not following the profile")
        cids.append(got)
    return cids


def snapshot_manifest(*, chain_id: int, contract: str, blob_count: int, head: bytes, batches: list[dict], blobs: list[dict]) -> bytes:
    """manifest.json: enough to recover with nothing but IPFS, verified against the chain.

    Each blob entry has `seq`, `versionedHash`, `cid`, and, when known at build time,
    `blockNumber` and `blockTimestamp`, which let beacon-style sources locate the blob too.
    """
    doc = {
        "chainId": chain_id,
        "contract": contract,
        "blobCount": blob_count,
        "head": "0x" + head.hex(),
        "batches": batches,
        "blobs": blobs,
        "profile": "docs/ipfs-profile.md",
    }
    return (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()


def parse_manifest(data: bytes) -> dict:
    doc = json.loads(data.decode("utf-8"))
    for k in ("blobCount", "head", "blobs"):
        if k not in doc:
            raise IpfsError(f"snapshot manifest lacks {k!r}")
    return doc
