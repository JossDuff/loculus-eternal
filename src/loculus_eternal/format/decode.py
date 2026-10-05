"""Decoding a stream of blobs back into batches, entries and materialised files.

The decoder trusts nothing about the stream beyond the bytes: every declared length and
digest is recomputed, a batch that fails any check is torn and contributes nothing, and the
decoder resynchronises at the next blob boundary that carries the batch it expects.

Entries never sit in memory. Each batch body is decompressed as a stream, its entries are
written to a sorted run file, and the cumulative digests are checked by merging that run
with the runs already accepted. Only the keys are kept in memory.
"""

from __future__ import annotations

import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

import zstandard

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, unpack_blob
from loculus_eternal.format.encode import EncoderState, Entry
from loculus_eternal.format.entrystore import EntryStore
from loculus_eternal.format.records import (
    CODEC_RAW,
    CODEC_ZSTD,
    DIGEST_BYTES,
    INNER_MAX,
    INNER_MIN,
    BatchBegin,
    FormatError,
    Header,
    RecordType,
    decode_tooling,
    decompress,
    read_record,
    sha256,
)
from loculus_eternal.format.runs import BodyRecordReader, ExternalSorter, Record


class Torn(FormatError):
    """Raised inside batch decoding when the batch cannot be completed."""


@dataclass
class TornBatch:
    expected_batch: int
    first_blob: int
    last_blob: int
    reason: str


@dataclass
class DecodedBatch:
    batch: int
    first_blob_seq: int
    blob_count_after: int
    begin: BatchBegin
    manifest: dict
    manifest_digest: bytes
    index: dict | None
    schemas: dict[str, dict]
    tooling: dict[str, bytes]
    entry_count: int


@dataclass
class DecodedStream:
    header: Header | None
    store: EntryStore
    batches: list[DecodedBatch] = field(default_factory=list)
    torn: list[TornBatch] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    schemas: dict[str, dict] = field(default_factory=dict)
    tooling: dict[str, bytes] = field(default_factory=dict)
    blobs_consumed: int = 0
    _cleanup: object = field(default=None, repr=False)

    # --- entries ----------------------------------------------------------------------------

    def organisms(self) -> list[str]:
        return self.store.organisms()

    def has(self, organism: str, accession: str, version: int) -> bool:
        return self.store.has(organism, accession, version)

    def count(self, organism: str) -> int:
        return self.store.count(organism)

    def records(self, organism: str) -> Iterator[Record]:
        """(accession, version, payload) in materialisation order, streamed from disk."""
        return self.store.iter_records(organism)

    def payloads(self, organism: str) -> list[bytes]:
        """Every payload in materialisation order, in memory. For tests and small streams."""
        return [p for _, _, p in self.records(organism)]

    def payload(self, organism: str, accession: str, version: int) -> bytes | None:
        return self.store.payload(organism, accession, version)

    def materialise_to(self, organism: str, out: BinaryIO) -> str:
        """Write the organism's file while hashing it; returns the SHA-256 hex digest."""
        return self.store.materialise_to(organism, out)[0]

    def materialise(self, organism: str) -> bytes:
        return self.store.materialise(organism)

    def artifact_digests(self) -> dict[str, str]:
        return {o: self.store.digest(o)[0] for o in self.organisms()}

    def verify_artifacts(self) -> dict[str, bool]:
        """Compare the materialised files against the last manifest's cumulative digests."""
        if not self.batches:
            return {}
        last = self.batches[-1].manifest["organisms"]
        actual = self.artifact_digests()
        return {o: last.get(o, {}).get("artifactSha256") == actual.get(o) for o in sorted(set(last) | set(actual))}

    def published(self) -> dict[str, dict[str, list[list[int]]]]:
        out: dict[str, dict[str, list[list[int]]]] = {}
        for org, idx in self.store.index.items():
            for (acc, ver), batch in idx.items():
                out.setdefault(org, {}).setdefault(acc, []).append([ver, batch])
        for accs in out.values():
            for vs in accs.values():
                vs.sort()
        return out

    def encoder_state(self) -> EncoderState:
        """The state an encoder needs to append the next batch after this stream."""
        if not self.batches:
            return EncoderState()
        last = self.batches[-1]
        bytes_since_index = 0
        for b in reversed(self.batches):
            if b.index is not None:
                break
            bytes_since_index += b.begin.compressed_length
        return EncoderState(
            next_batch=last.batch + 1,
            next_blob_seq=last.blob_count_after,
            previous_manifest_digest=last.manifest_digest,
            published=self.published(),
            schemas=dict(self.schemas),
            entries_total={o: self.store.count(o) for o in self.organisms()},
            bytes_since_index=bytes_since_index,
        )

    def close(self) -> None:
        """Remove the spilled entries. They are a working copy; the stream is the record."""
        self.store.destroy()
        if self._cleanup is not None:
            self._cleanup.cleanup()
            self._cleanup = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Cursor:
    """Reads records from the stream while refusing to cross a missing blob."""

    def __init__(self, data: bytes, missing: set[int]):
        self.data = data
        self.missing = missing
        self.pos = 0

    def _check(self, start: int, end: int) -> None:
        if end > len(self.data):
            raise Torn("stream ends inside a record")
        for blob in range(start // BLOB_DATA_BYTES, (max(end, start + 1) - 1) // BLOB_DATA_BYTES + 1):
            if blob in self.missing:
                raise Torn(f"blob {blob} is missing")

    def record(self) -> tuple[int, bytes]:
        try:
            self._check(self.pos, min(self.pos + 11, len(self.data)))
            record_type, payload, end = read_record(self.data, self.pos)
        except FormatError as e:
            raise Torn(str(e)) from e
        self._check(self.pos, end)
        self.pos = end
        return record_type, payload


def _body_stream(codec: int, body: bytes) -> BinaryIO:
    if codec == CODEC_RAW:
        return io.BytesIO(body)
    if codec == CODEC_ZSTD:
        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body), read_across_frames=False)
    raise Torn(f"unknown codec {codec}")


class StreamDecoder:
    def __init__(self, blobs: Sequence[bytes | None], spill_dir: Path | None = None, sort_buffer_bytes: int | None = None):
        """blobs[i] is blob i's 131,072 bytes, or None if it could not be obtained.

        Entries are spilled under `spill_dir` (a fresh temporary directory by default, removed
        when the returned stream is closed). `sort_buffer_bytes` bounds the memory used to sort
        a batch whose entries are not already in order.
        """
        parts = []
        self.missing: set[int] = set()
        for i, b in enumerate(blobs):
            if b is None:
                self.missing.add(i)
                parts.append(b"\x00" * BLOB_DATA_BYTES)
            else:
                parts.append(unpack_blob(b))
        self.data = b"".join(parts)
        self.blob_count = len(blobs)
        self._tmp = None
        if spill_dir is None:
            self._tmp = tempfile.TemporaryDirectory(prefix="loculus-eternal-decode-")
            spill_dir = Path(self._tmp.name)
        self.spill_dir = Path(spill_dir)
        self.sort_buffer_bytes = sort_buffer_bytes

    def decode(self) -> DecodedStream:
        store = EntryStore(self.spill_dir / "entries")
        out = DecodedStream(header=None, store=store, _cleanup=self._tmp)
        cur = _Cursor(self.data, self.missing)
        if self.blob_count == 0:
            return out

        try:
            t, payload = cur.record()
            if t != RecordType.HEADER:
                raise FormatError("stream does not start with a header record")
            out.header = Header.decode(payload)
        except Torn as e:
            raise FormatError(f"cannot read the stream header: {e}") from e

        expected_batch = 0
        previous_digest = b"\x00" * DIGEST_BYTES
        blob = 0
        after_header = True
        while blob < self.blob_count:
            # Batch 0 follows the header inside blob 0; every other batch starts at chunk 0.
            if not after_header:
                cur.pos = blob * BLOB_DATA_BYTES
            after_header = False
            try:
                decoded = self._decode_batch(cur, expected_batch, previous_digest, blob, out)
            except Torn as e:
                next_blob = self._find_resync(blob + 1, expected_batch, previous_digest)
                last = (next_blob if next_blob is not None else self.blob_count) - 1
                out.torn.append(TornBatch(expected_batch, blob, last, str(e)))
                if next_blob is None:
                    out.blobs_consumed = self.blob_count
                    return out
                blob = next_blob
                continue
            out.batches.append(decoded)
            expected_batch += 1
            previous_digest = decoded.manifest_digest
            blob = decoded.blob_count_after
            out.blobs_consumed = blob
        return out

    def _find_resync(self, from_blob: int, expected_batch: int, previous_digest: bytes) -> int | None:
        for b in range(from_blob, self.blob_count):
            if b in self.missing:
                continue
            try:
                t, payload, _ = read_record(self.data, b * BLOB_DATA_BYTES)
                if t != RecordType.BATCH_BEGIN:
                    continue
                begin = BatchBegin.decode(payload)
            except FormatError:
                continue
            if begin.batch == expected_batch and begin.previous_manifest_digest == previous_digest and begin.first_blob_seq == b:
                return b
        return None

    def _decode_batch(self, cur: _Cursor, expected_batch: int, previous_digest: bytes, first_blob: int, out: DecodedStream) -> DecodedBatch:
        t, payload = cur.record()
        if t != RecordType.BATCH_BEGIN:
            raise Torn(f"expected a batch header, found record type {t:#x}")
        try:
            begin = BatchBegin.decode(payload)
        except FormatError as e:
            raise Torn(str(e)) from e
        if begin.batch != expected_batch:
            raise Torn(f"batch number {begin.batch}, expected {expected_batch}")
        if begin.previous_manifest_digest != previous_digest:
            raise Torn("previous manifest digest does not match")
        if begin.first_blob_seq != first_blob:
            raise Torn(f"batch claims to start at blob {begin.first_blob_seq} but is at blob {first_blob}")

        t, body = cur.record()
        if t != RecordType.BODY:
            raise Torn(f"expected a body record, found record type {t:#x}")
        if len(body) != begin.compressed_length or sha256(body) != begin.body_digest:
            raise Torn("body length or digest does not match the batch header")

        t, payload = cur.record()
        index = None
        if t == RecordType.INDEX:
            try:
                index = canonical.loads(decompress(payload[0], payload[1:]))
            except (FormatError, ValueError, IndexError) as e:
                raise Torn(f"index does not decode: {e}") from e
            t, payload = cur.record()
        if t != RecordType.BATCH_MANIFEST:
            raise Torn(f"expected a manifest, found record type {t:#x}")
        try:
            manifest = canonical.loads(payload)
        except ValueError as e:
            raise Torn(f"manifest is not JSON: {e}") from e
        if not canonical.is_canonical(payload):
            raise Torn("manifest is not canonical JSON")

        # The batch ends in the blob that holds the manifest's last byte.
        blob_count_after = -(-cur.pos // BLOB_DATA_BYTES)
        checks = {
            "batch": expected_batch,
            "firstBlobSeq": first_blob,
            "blobCountAfter": blob_count_after,
            "previousManifestDigest": previous_digest.hex(),
            "bodyDigest": begin.body_digest.hex(),
            "hasIndex": index is not None,
        }
        for k, v in checks.items():
            if manifest.get(k) != v:
                raise Torn(f"manifest field {k} is {manifest.get(k)!r}, expected {v!r}")
        pad_end = blob_count_after * BLOB_DATA_BYTES
        cur._check(cur.pos, pad_end)
        if any(self.data[cur.pos:pad_end]):
            raise Torn("bytes after the manifest are not zero")

        # Everything structural verified. Stream the body: schemas and tooling are small and
        # kept in memory; entries go to one staged sorted run per organism.
        schemas: dict[str, dict] = {}
        tooling: dict[str, bytes] = {}
        sorters: dict[str, ExternalSorter] = {}
        keys: dict[str, list[tuple[str, int]]] = {}
        warnings: list[str] = []
        count = 0
        sorter_kwargs = {"buffer_bytes": self.sort_buffer_bytes} if self.sort_buffer_bytes else {}
        try:
            # The reader refuses any record that would run past the declared uncompressed
            # length, so a hostile length prefix or a decompression bomb cannot make it
            # allocate or spill more than the batch header admits to.
            reader = BodyRecordReader(_body_stream(begin.codec, body), limit=begin.uncompressed_length)
            for it, ipayload in reader:
                if it == RecordType.ENTRY:
                    try:
                        entry = canonical.loads(ipayload)
                        Entry.validate(entry)
                    except (ValueError, TypeError, KeyError, FormatError) as e:
                        raise Torn(f"entry is invalid: {e}") from e
                    if not canonical.is_canonical(ipayload):
                        raise Torn("entry is not canonical JSON")
                    org, acc, ver = Entry.key(entry)
                    if org not in sorters:
                        sorters[org] = ExternalSorter(out.store.organism_dir(org) / f"sort-{expected_batch}", **sorter_kwargs)
                        keys[org] = []
                    sorters[org].add(acc, ver, ipayload)
                    keys[org].append((acc, ver))
                elif it == RecordType.SCHEMA:
                    s = canonical.loads(ipayload)
                    if not isinstance(s, dict) or not isinstance(s.get("organism"), str):
                        raise Torn("schema record is not an object naming an organism")
                    schemas[s["organism"]] = s
                elif it == RecordType.TOOLING:
                    p, c = decode_tooling(ipayload)
                    tooling[p] = c
                elif INNER_MIN <= it <= INNER_MAX:
                    warnings.append(f"batch {expected_batch}: skipped unknown inner record type {it:#x}")
                else:
                    raise Torn(f"outer record type {it:#x} inside a body")
            if reader.consumed != begin.uncompressed_length:
                raise Torn(f"decoded length {reader.consumed} differs from declared {begin.uncompressed_length}")
        except Torn:
            for sorter in sorters.values():
                sorter.abort()
            raise
        except (zstandard.ZstdError, EOFError, ValueError, TypeError, KeyError, MemoryError) as e:
            for sorter in sorters.values():
                sorter.abort()
            raise Torn(f"body does not decode: {e}") from e

        # Stage each organism's run, check every cumulative digest and count against the store
        # as it would stand with this batch, and only then accept. A failing batch leaves nothing.
        staged: dict[str, Path] = {}
        try:
            for org, sorter in sorters.items():
                path = out.store.staging_path(org, expected_batch)
                sorter.finish(path)
                staged[org] = path
            organisms_in_manifest = manifest.get("organisms", {})
            if set(organisms_in_manifest) != set(out.store.index) | set(staged):
                raise Torn("manifest organisms do not match the entries published so far")
            for org, info in organisms_in_manifest.items():
                extra = (expected_batch, staged[org]) if org in staged else None
                digest, total = out.store.digest(org, extra)
                if info.get("artifactSha256") != digest or info.get("entriesTotal") != total:
                    raise Torn(f"cumulative digest for {org} does not match the materialised entries")
        except Torn:
            for path in staged.values():
                out.store.discard(path)
            raise
        for org, path in staged.items():
            duplicates = out.store.accept(org, expected_batch, path, keys[org])
            count += len(keys[org]) - len(duplicates)
            for acc, ver in duplicates:
                warnings.append(f"batch {expected_batch}: duplicate entry {org} {acc}.{ver} ignored")
        out.warnings.extend(warnings)
        out.schemas.update(schemas)
        out.tooling.update(tooling)
        return DecodedBatch(
            batch=expected_batch,
            first_blob_seq=first_blob,
            blob_count_after=blob_count_after,
            begin=begin,
            manifest=manifest,
            manifest_digest=sha256(payload),
            index=index,
            schemas=schemas,
            tooling=tooling,
            entry_count=count,
        )

