"""The verify-before-write flat store of recovered blobs.

Layout of the store directory:

    chunks.dat     stream bytes: chunk i of the stream at byte offset 31·i, so blob s occupies
                   bytes [126976·s, 126976·(s+1)); unfilled regions are holes
    have.json      which blob sequence numbers are present and their versioned hashes;
                   rewritten atomically after each verified write, so it is the commit point
    .lock          exclusive lock while a process has the store open

Nothing reaches chunks.dat before its versioned hash has been recomputed from the bytes and
matched. On open, a region in chunks.dat not listed in have.json is treated as absent, so a
crash between the data write and the have.json rename loses nothing but that one write.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from loculus_eternal import kzg
from loculus_eternal.format.chunks import BLOB_BYTES, BLOB_DATA_BYTES, pack_blobs, unpack_blob


class StoreError(RuntimeError):
    pass


class VerificationFailed(StoreError):
    """The bytes offered for a sequence number are not the blob behind its versioned hash."""


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class BlobStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_fd = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            os.close(self._lock_fd)
            raise StoreError(f"another process has {self.directory} open") from e
        self.have_path = self.directory / "have.json"
        self.data_path = self.directory / "chunks.dat"
        self.have: dict[int, bytes] = {}
        if self.have_path.exists():
            raw = json.loads(self.have_path.read_text())
            self.have = {int(k): bytes.fromhex(v[2:]) for k, v in raw.items()}
        self.data_path.touch()

    # --- queries ---------------------------------------------------------------------------

    def has(self, seq: int) -> bool:
        return seq in self.have

    def count(self) -> int:
        return len(self.have)

    def read_stream_bytes(self, seq: int) -> bytes | None:
        """The 126,976 stream bytes of blob seq, or None if absent."""
        if seq not in self.have:
            return None
        with open(self.data_path, "rb") as f:
            f.seek(seq * BLOB_DATA_BYTES)
            data = f.read(BLOB_DATA_BYTES)
        if len(data) != BLOB_DATA_BYTES:
            raise StoreError(f"chunks.dat is shorter than have.json claims for blob {seq}")
        return data

    def read_blob(self, seq: int) -> bytes | None:
        """Reconstruct the full 131,072-byte blob (zero high bytes restored)."""
        data = self.read_stream_bytes(seq)
        return None if data is None else pack_blobs(data)[0] if any(data) else bytes(BLOB_BYTES)

    def blobs(self, count: int) -> list[bytes | None]:
        """Blobs 0..count-1 for the decoder, None where missing."""
        return [self.read_blob(i) for i in range(count)]

    def verify_all(self) -> list[int]:
        """Re-check every stored blob against its recorded hash; returns the sequence numbers that fail."""
        bad = []
        for seq, vh in sorted(self.have.items()):
            blob = self.read_blob(seq)
            if blob is None or not kzg.verify_blob(blob, vh):
                bad.append(seq)
        return bad

    # --- writes ----------------------------------------------------------------------------

    def write(self, seq: int, versioned_hash: bytes, blob: bytes) -> None:
        """Store a blob for sequence number seq. Verifies first; nothing is written on mismatch."""
        if len(blob) != BLOB_BYTES:
            raise VerificationFailed(f"blob {seq}: wrong length {len(blob)}")
        if not kzg.verify_blob(blob, versioned_hash):
            raise VerificationFailed(f"blob {seq}: bytes do not match versioned hash 0x{versioned_hash.hex()}")
        if seq in self.have:
            if self.have[seq] != versioned_hash:
                raise StoreError(f"blob {seq} is already stored with a different hash")
            return
        data = unpack_blob(blob)
        with open(self.data_path, "r+b") as f:
            f.seek(seq * BLOB_DATA_BYTES)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        self.have[seq] = versioned_hash
        self._commit()

    def _commit(self) -> None:
        payload = json.dumps({str(k): "0x" + v.hex() for k, v in sorted(self.have.items())}, indent=0).encode()
        _write_atomic(self.have_path, payload)

    def export_blobs(self, directory: str | Path) -> int:
        """Write every stored blob as `<versioned hash>.blob`, the local-source layout."""
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        n = 0
        for seq, vh in sorted(self.have.items()):
            blob = self.read_blob(seq)
            if blob is not None:
                (out / (vh.hex() + ".blob")).write_bytes(blob)
                n += 1
        return n

    def close(self) -> None:
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
