"""Reading a stream's structure without touching its entries.

The health page needs to know what the stream contains and whether it is coherent, not to
rebuild the dataset. This reader walks only the outer records: the header, each batch's
header, body (length and digest checked, never decompressed), index and manifest. It
applies the same torn-batch rules as the full decoder and reports the same batch facts, but
nothing is written to disk and the whole pass over a 41 MB stream takes a second.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, unpack_blob
from loculus_eternal.format.records import DIGEST_BYTES, BatchBegin, FormatError, Header, RecordType, decompress, read_record, sha256


@dataclass
class BatchSummary:
    batch: int
    first_blob_seq: int
    blob_count_after: int
    codec: int
    uncompressed_length: int
    compressed_length: int
    has_index: bool
    manifest: dict
    manifest_digest: str


@dataclass
class TornSummary:
    expected_batch: int
    first_blob: int
    last_blob: int
    reason: str


@dataclass
class StreamStructure:
    header: Header | None
    batches: list[BatchSummary] = field(default_factory=list)
    torn: list[TornSummary] = field(default_factory=list)
    missing_blobs: list[int] = field(default_factory=list)

    @property
    def last_manifest(self) -> dict | None:
        return self.batches[-1].manifest if self.batches else None

    def organisms(self) -> dict[str, dict]:
        """Per organism, the latest manifest's counts and digest."""
        m = self.last_manifest
        return dict(sorted(m["organisms"].items())) if m else {}

    def dead_blobs(self) -> set[int]:
        """Blobs no reader needs: those of a torn batch that a complete batch follows. The
        later batch header proves the publisher started over. Blob 0 is never dead, since it
        holds the stream header; a torn range at the end of the stream is not dead either."""
        if not self.batches:
            return set()
        last_complete = self.batches[-1].blob_count_after
        dead: set[int] = set()
        for t in self.torn:
            if t.last_blob < last_complete:
                dead.update(range(t.first_blob, t.last_blob + 1))
        dead.discard(0)
        return dead

    def torn_tail(self) -> TornSummary | None:
        """A torn range at the very end of the stream means an upload is unfinished or lost."""
        if self.torn and (not self.batches or self.torn[-1].first_blob >= self.batches[-1].blob_count_after):
            return self.torn[-1]
        return None


def read_structure(blobs: Sequence[bytes | None]) -> StreamStructure:
    """Walk the outer records of the stream. blobs[i] is None when blob i is unavailable."""
    missing = {i for i, b in enumerate(blobs) if b is None}
    data = b"".join(b"\x00" * BLOB_DATA_BYTES if b is None else unpack_blob(b) for b in blobs)
    out = StreamStructure(header=None, missing_blobs=sorted(missing))
    if not blobs:
        return out

    def check_range(start: int, end: int) -> None:
        if end > len(data):
            raise FormatError("stream ends inside a record")
        for blob in range(start // BLOB_DATA_BYTES, (max(end, start + 1) - 1) // BLOB_DATA_BYTES + 1):
            if blob in missing:
                raise FormatError(f"blob {blob} is missing")

    def record(pos: int) -> tuple[int, bytes, int]:
        check_range(pos, min(pos + 11, len(data)))
        t, payload, end = read_record(data, pos)
        check_range(pos, end)
        return t, payload, end

    pos = 0
    try:
        t, payload, pos = record(0)
        if t != RecordType.HEADER:
            raise FormatError("stream does not start with a header record")
        out.header = Header.decode(payload)
    except FormatError as e:
        out.torn.append(TornSummary(0, 0, len(blobs) - 1, f"cannot read the stream header: {e}"))
        return out

    expected = 0
    previous = b"\x00" * DIGEST_BYTES
    blob = 0
    after_header = True
    while blob < len(blobs):
        if not after_header:
            pos = blob * BLOB_DATA_BYTES
        after_header = False
        try:
            summary, pos = _read_batch(data, pos, expected, previous, blob, record, check_range)
        except FormatError as e:
            next_blob = _resync(data, blob + 1, len(blobs), missing, expected, previous)
            last = (next_blob if next_blob is not None else len(blobs)) - 1
            out.torn.append(TornSummary(expected, blob, last, str(e)))
            if next_blob is None:
                return out
            blob = next_blob
            continue
        out.batches.append(summary)
        expected += 1
        previous = bytes.fromhex(summary.manifest_digest)
        blob = summary.blob_count_after
    return out


def _read_batch(data, pos, expected, previous, first_blob, record, check_range) -> tuple[BatchSummary, int]:
    t, payload, pos = record(pos)
    if t != RecordType.BATCH_BEGIN:
        raise FormatError(f"expected a batch header, found record type {t:#x}")
    begin = BatchBegin.decode(payload)
    if begin.batch != expected:
        raise FormatError(f"batch number {begin.batch}, expected {expected}")
    if begin.previous_manifest_digest != previous:
        raise FormatError("previous manifest digest does not match")
    if begin.first_blob_seq != first_blob:
        raise FormatError(f"batch claims to start at blob {begin.first_blob_seq} but is at blob {first_blob}")
    t, body, pos = record(pos)
    if t != RecordType.BODY:
        raise FormatError(f"expected a body record, found record type {t:#x}")
    if len(body) != begin.compressed_length or sha256(body) != begin.body_digest:
        raise FormatError("body length or digest does not match the batch header")
    t, payload, pos = record(pos)
    has_index = False
    if t == RecordType.INDEX:
        has_index = True
        decompress(payload[0], payload[1:])  # must at least decode
        t, payload, pos = record(pos)
    if t != RecordType.BATCH_MANIFEST:
        raise FormatError(f"expected a manifest, found record type {t:#x}")
    manifest = canonical.loads(payload)
    if not canonical.is_canonical(payload):
        raise FormatError("manifest is not canonical JSON")
    blob_count_after = -(-pos // BLOB_DATA_BYTES)
    checks = {"batch": expected, "firstBlobSeq": first_blob, "blobCountAfter": blob_count_after, "previousManifestDigest": previous.hex(), "bodyDigest": begin.body_digest.hex(), "hasIndex": has_index}
    for k, v in checks.items():
        if manifest.get(k) != v:
            raise FormatError(f"manifest field {k} is {manifest.get(k)!r}, expected {v!r}")
    pad_end = blob_count_after * BLOB_DATA_BYTES
    check_range(pos, pad_end)
    if any(data[pos:pad_end]):
        raise FormatError("bytes after the manifest are not zero")
    return BatchSummary(expected, first_blob, blob_count_after, begin.codec, begin.uncompressed_length, begin.compressed_length, has_index, manifest, sha256(payload).hex()), pad_end


def _resync(data, from_blob, blob_count, missing, expected, previous):
    for b in range(from_blob, blob_count):
        if b in missing:
            continue
        try:
            t, payload, _ = read_record(data, b * BLOB_DATA_BYTES)
            if t != RecordType.BATCH_BEGIN:
                continue
            begin = BatchBegin.decode(payload)
        except FormatError:
            continue
        if begin.batch == expected and begin.previous_manifest_digest == previous and begin.first_blob_seq == b:
            return b
    return None
