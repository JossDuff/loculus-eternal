"""Sorted runs of entries on disk, so that neither encoding nor decoding holds the dataset.

A run file is a sequence of entry records, each `uvarint(len(accession)) ‖ accession ‖
uvarint(version) ‖ uvarint(len(payload)) ‖ payload`, sorted by (accession, version). The key
travels with the payload so that merging runs never parses JSON. Runs are produced by an
external sorter that buffers a bounded number of bytes, sorts, spills a chunk, and merges
the chunks at the end, and are consumed by streaming readers and k-way merges.
"""

from __future__ import annotations

import heapq
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator

from loculus_eternal.format import varint

EntryKey = tuple[str, int]
Record = tuple[str, int, bytes]  # accession, version, payload

DEFAULT_BUFFER_BYTES = 256 * 1024 * 1024


class RecordReader:
    """Reads length-prefixed records from a binary stream without reading ahead past them.

    With a `limit`, no length prefix may ask for more bytes than remain of the declared
    total, so a hostile prefix cannot provoke a huge allocation, and a stream that keeps
    producing bytes past the declared total is cut off.
    """

    def __init__(self, stream: BinaryIO, limit: int | None = None):
        self.stream = stream
        self.consumed = 0
        self.limit = limit

    def _check_room(self, n: int) -> None:
        if self.limit is not None and self.consumed + n > self.limit:
            raise ValueError(f"record asks for {n} bytes but only {self.limit - self.consumed} remain of the declared length")

    def _read_exact(self, n: int) -> bytes:
        self._check_room(n)
        data = self.stream.read(n)
        if len(data) != n:
            raise EOFError(f"stream ended after {self.consumed + len(data)} bytes, needed {n} more")
        self.consumed += n
        return data

    def read_uvarint(self) -> int | None:
        """A uvarint, or None at a clean end of stream."""
        buf = bytearray()
        while True:
            if len(buf) >= varint.MAX_BYTES:
                raise ValueError("length prefix longer than ten bytes")
            if not buf and self.limit is not None and self.consumed >= self.limit:
                return None  # the declared length is used up: a clean end, whatever follows
            self._check_room(1)
            b = self.stream.read(1)
            if not b:
                if not buf:
                    return None
                raise EOFError("stream ended inside a length prefix")
            self.consumed += 1
            buf += b
            if b[0] & 0x80 == 0:
                value, _ = varint.decode(bytes(buf))
                return value

    def read_block(self) -> bytes | None:
        """One `uvarint(len) ‖ bytes` block, or None at a clean end."""
        n = self.read_uvarint()
        if n is None:
            return None
        return self._read_exact(n)


def write_record(out: BinaryIO, accession: str, version: int, payload: bytes) -> int:
    a = accession.encode("utf-8")
    data = varint.encode(len(a)) + a + varint.encode(version) + varint.encode(len(payload)) + payload
    out.write(data)
    return len(data)


def iter_run(path: Path) -> Iterator[Record]:
    with open(path, "rb", buffering=1 << 20) as f:
        reader = RecordReader(f)
        while True:
            a = reader.read_block()
            if a is None:
                return
            version = reader.read_uvarint()
            payload = reader.read_block()
            if version is None or payload is None:
                raise EOFError(f"{path} ends inside a record")
            yield a.decode("utf-8"), version, payload


def _key(record: Record) -> EntryKey:
    return (record[0], record[1])


class ExternalSorter:
    """Collects (accession, version, payload) records and writes them out sorted.

    Records are buffered up to `buffer_bytes`, then sorted and spilled to a chunk file; at the
    end the chunks are merged into the destination. Memory stays at about the buffer size no
    matter how many records arrive.
    """

    def __init__(self, work_dir: Path, buffer_bytes: int = DEFAULT_BUFFER_BYTES):
        self.work_dir = Path(work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.buffer_bytes = buffer_bytes
        self._buffer: list[Record] = []
        self._buffered = 0
        self._chunks: list[Path] = []
        self.count = 0

    def add(self, accession: str, version: int, payload: bytes) -> None:
        self._buffer.append((accession, version, payload))
        self._buffered += len(payload) + len(accession) + 16
        self.count += 1
        if self._buffered >= self.buffer_bytes:
            self._spill()

    def _spill(self) -> None:
        if not self._buffer:
            return
        self._buffer.sort(key=_key)
        fd, name = tempfile.mkstemp(prefix="chunk-", suffix=".run", dir=self.work_dir)
        with os.fdopen(fd, "wb", buffering=1 << 20) as f:
            for a, v, p in self._buffer:
                write_record(f, a, v, p)
        self._chunks.append(Path(name))
        self._buffer = []
        self._buffered = 0

    def finish(self, destination: Path) -> int:
        """Write the sorted run to destination (atomically) and return the record count."""
        self._spill()
        tmp = destination.with_name(destination.name + ".tmp")
        with open(tmp, "wb", buffering=1 << 20) as out:
            for a, v, p in heapq.merge(*(iter_run(c) for c in self._chunks), key=_key):
                write_record(out, a, v, p)
        os.replace(tmp, destination)
        self.abort()
        return self.count

    def abort(self) -> None:
        """Drop everything buffered or spilled and remove the work directory."""
        self._buffer = []
        self._buffered = 0
        for c in self._chunks:
            c.unlink(missing_ok=True)
        self._chunks = []
        try:
            self.work_dir.rmdir()
        except OSError:
            pass


def merge_runs(runs: Iterable[tuple[int, Path]], on_duplicate: Callable[[EntryKey, int, int], None] | None = None) -> Iterator[tuple[int, Record]]:
    """Merge sorted runs tagged with their batch number into one sorted stream.

    When the same (accession, version) appears in several runs, the one from the earliest
    batch wins and `on_duplicate(key, kept_batch, dropped_batch)` is called for each loser.
    """
    runs = list(runs)

    def tag(batch: int, path: Path):
        for rec in iter_run(path):
            yield (rec[0], rec[1], batch), rec

    merged = heapq.merge(*(tag(batch, path) for batch, path in runs), key=lambda item: item[0])
    last_key: EntryKey | None = None
    last_batch = -1
    for (accession, version, batch), rec in merged:
        key = (accession, version)
        if key == last_key:
            if on_duplicate is not None:
                on_duplicate(key, last_batch, batch)
            continue
        last_key, last_batch = key, batch
        yield batch, rec


class BodyRecordReader:
    """Reads container records (`uvarint(len) ‖ type ‖ payload`) from a decompressing stream,
    never asking for more than `limit` bytes in total."""

    def __init__(self, stream: BinaryIO, limit: int | None = None):
        self.reader = RecordReader(stream, limit)

    def __iter__(self) -> Iterator[tuple[int, bytes]]:
        while True:
            block = self.reader.read_block()
            if block is None:
                return
            if not block:
                raise ValueError("record with zero length")
            yield block[0], block[1:]

    @property
    def consumed(self) -> int:
        return self.reader.consumed
