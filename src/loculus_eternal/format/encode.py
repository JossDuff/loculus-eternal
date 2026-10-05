"""Encoding batches of entries into the stream, blob by blob.

The encoder holds the little state the stream has: the next batch number, the next blob
sequence number, the digest of the previous manifest, which organisms have had a schema
record, the published accessionVersions, and how many compressed bytes have gone by since
the last index. That state is rebuilt from the chain and the stream by the decoder, so an
encoder can be resumed on any machine.

Entries are consumed as a stream: each is validated, canonicalised, and written to a sorted
run on disk; the body is compressed as the run is replayed; the cumulative digests are
computed by merging the run with the previously published entries. Memory stays bounded
however large the batch.
"""

from __future__ import annotations

import hashlib
import heapq
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import zstandard

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, blobs_needed, last_blob_chunk_count, pack_blobs
from loculus_eternal.format.records import (
    CODEC_RAW,
    CODEC_ZSTD,
    DIGEST_BYTES,
    ZSTD_LEVEL,
    BatchBegin,
    FormatError,
    Header,
    RecordType,
    compress,
    encode_tooling,
    frame,
    sha256,
)
from loculus_eternal.format.runs import DEFAULT_BUFFER_BYTES, ExternalSorter, Record, iter_run

INDEX_THRESHOLD_BYTES = 16 * 1024 * 1024

DATA_KEYS = (
    "unalignedNucleotideSequences",
    "alignedNucleotideSequences",
    "nucleotideInsertions",
    "alignedAminoAcidSequences",
    "aminoAcidInsertions",
)
REMOVED_METADATA = ("versionStatus", "dataUseTerms", "dataUseTermsRestrictedUntil", "dataUseTermsUrl")

EntryKey = tuple[str, str, int]  # (organism, accession, version)


class EmptyBatch(FormatError):
    """The entry stream produced nothing to publish."""


class Entry:
    """Helpers for the published form of one accessionVersion."""

    @staticmethod
    def from_released_line(organism: str, line: dict) -> dict:
        """Apply the published projection to one line of the backend's release feed."""
        missing = [k for k in ("metadata", *DATA_KEYS) if k not in line]
        if missing:
            raise FormatError(f"released line is missing keys {missing}")
        metadata = {k: v for k, v in line["metadata"].items() if k not in REMOVED_METADATA}
        entry = {"organism": organism, "metadata": metadata}
        for k in DATA_KEYS:
            entry[k] = line[k]
        Entry.validate(entry)
        return entry

    @staticmethod
    def validate(entry: dict) -> None:
        if not isinstance(entry, dict):
            raise FormatError("entry must be a JSON object")
        expected = {"organism", "metadata", *DATA_KEYS}
        if set(entry) != expected:
            raise FormatError(f"entry keys must be exactly {sorted(expected)}, got {sorted(entry)}")
        if not isinstance(entry["organism"], str) or not entry["organism"]:
            raise FormatError("entry organism must be a non-empty string")
        m = entry["metadata"]
        if not isinstance(m, dict):
            raise FormatError("entry metadata must be an object")
        for k in REMOVED_METADATA:
            if k in m:
                raise FormatError(f"entry metadata must not contain {k}")
        if not isinstance(m.get("accession"), str) or not m["accession"]:
            raise FormatError("entry metadata.accession must be a non-empty string")
        if not isinstance(m.get("version"), int) or isinstance(m["version"], bool) or m["version"] < 1:
            raise FormatError("entry metadata.version must be a positive integer")
        if m.get("accessionVersion") != f"{m['accession']}.{m['version']}":
            raise FormatError("entry metadata.accessionVersion must be '<accession>.<version>'")

    @staticmethod
    def key(entry: dict) -> EntryKey:
        return (entry["organism"], entry["metadata"]["accession"], entry["metadata"]["version"])

    @staticmethod
    def payload(entry: dict) -> bytes:
        try:
            payload = canonical.dumps(entry)
        except (ValueError, TypeError) as e:
            # rfc8785 refuses integers outside the IEEE double range and non-JSON values.
            raise FormatError(f"entry cannot be canonicalised: {e}") from e
        # An integral float at or above 2^53 prints as integer digits and would re-parse as
        # an integer outside the safe range, so the payload would not survive a round trip.
        if not canonical.is_canonical(payload):
            raise FormatError("entry contains a number that does not survive canonical JSON round-tripping")
        return payload

    @staticmethod
    def schema(entry: dict) -> dict:
        return {
            "organism": entry["organism"],
            "metadataFields": sorted(entry["metadata"]),
            "nucleotideSegments": sorted(entry["unalignedNucleotideSequences"]),
            "genes": sorted(entry["alignedAminoAcidSequences"]),
        }


@dataclass
class EncoderState:
    """Everything the encoder needs to append the next batch."""

    next_batch: int = 0
    next_blob_seq: int = 0
    previous_manifest_digest: bytes = b"\x00" * DIGEST_BYTES
    # organism -> accession -> list of [version, batch], versions ascending
    published: dict[str, dict[str, list[list[int]]]] = field(default_factory=dict)
    # organism -> schema dict last published
    schemas: dict[str, dict] = field(default_factory=dict)
    # organism -> lines in the materialised file so far (published minus withdrawn)
    entries_total: dict[str, int] = field(default_factory=dict)
    # organism -> accession -> sorted withdrawn versions
    withdrawn: dict[str, dict[str, list[int]]] = field(default_factory=dict)
    bytes_since_index: int = 0

    def is_published(self, organism: str, accession: str, version: int) -> bool:
        return any(v == version for v, _ in self.published.get(organism, {}).get(accession, []))

    def is_withdrawn(self, organism: str, accession: str, version: int) -> bool:
        return version in self.withdrawn.get(organism, {}).get(accession, [])

    def withdrawn_total(self, organism: str) -> int:
        return sum(len(v) for v in self.withdrawn.get(organism, {}).values())


@dataclass
class EncodedBatch:
    batch: int
    first_blob_seq: int
    blob_count_after: int
    last_blob_chunk_count: int
    stream: bytes          # this batch's bytes including zero padding to the blob boundary
    blobs: list[bytes]
    body_digest: bytes
    manifest: dict
    manifest_digest: bytes
    index: dict | None
    schemas_published: list[str]
    entry_count: int
    withdrawn_count: int = 0


# previous_entries(organism) yields (accession, version, payload) in materialisation order
PreviousEntries = Callable[[str], Iterable[Record]]


class StreamEncoder:
    def __init__(self, chain_id: int, contract: bytes, state: EncoderState | None = None, schema_id: str = "pathoplexus", work_dir: Path | None = None, sort_buffer_bytes: int = DEFAULT_BUFFER_BYTES):
        self.header = Header(chain_id=chain_id, contract=contract, schema_id=schema_id)
        self.state = state or EncoderState()
        self.work_dir = Path(work_dir) if work_dir else None
        if self.work_dir is not None:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        self.sort_buffer_bytes = sort_buffer_bytes

    def encode_batch(
        self,
        entries: Iterable[dict],
        previous_entries: PreviousEntries | None = None,
        tooling: Iterable[tuple[str, bytes]] = (),
        codec: int = CODEC_ZSTD,
        force_index: bool = False,
        withdrawals: Iterable[dict] = (),
    ) -> EncodedBatch:
        """Encode one batch from a stream of entries and advance the state.

        `previous_entries(organism)` must yield `(accession, version, payload)` for every
        entry of that organism published in earlier batches and not withdrawn, in
        materialisation order; it is needed for the cumulative artifact digests and may be
        omitted only when nothing has been published yet.

        `withdrawals` are `{"organism", "accessionVersions", "note"}` dicts; withdrawn
        entries leave the materialised output from this batch on.
        """
        with tempfile.TemporaryDirectory(prefix="loculus-eternal-encode-", dir=self.work_dir) as tmp:
            return self._encode(Path(tmp), entries, previous_entries, list(tooling), codec, force_index, withdrawals)

    def _encode(self, work: Path, entries, previous_entries, tooling, codec, force_index, withdrawals) -> EncodedBatch:
        st = self.state

        # Pass 1: validate, canonicalise, and spill every entry into a sorted run per organism.
        sorters: dict[str, ExternalSorter] = {}
        schemas_now: dict[str, dict] = {}
        seen: set[EntryKey] = set()
        count = 0
        for e in entries:
            Entry.validate(e)
            k = Entry.key(e)
            if k in seen:
                raise FormatError(f"duplicate entry in batch: {k}")
            seen.add(k)
            if st.is_published(*k):
                raise FormatError(f"entry already published: {k}")
            if st.is_withdrawn(*k):
                raise FormatError(f"entry is withdrawn and cannot be published: {k}")
            schema = Entry.schema(e)
            if st.schemas.get(k[0]) != schema and schemas_now.get(k[0]) != schema:
                schemas_now[k[0]] = schema
            org = k[0]
            if org not in sorters:
                sorters[org] = ExternalSorter(work / "sort" / org, buffer_bytes=self.sort_buffer_bytes)
            sorters[org].add(k[1], k[2], Entry.payload(e))
            count += 1
        # Withdrawals are read only after every entry, so a caller may decide them from what
        # the entries turned out to be (the upload command withdraws what vanished from the
        # feeds). An entry that is withdrawn in this very batch is then refused.
        withdraw_now: dict[str, dict[str, list[int]]] = {}
        withdraw_records: list[tuple[str, bytes]] = []
        for w in withdrawals:
            org = w.get("organism")
            if not isinstance(org, str) or not org or not isinstance(w.get("accessionVersions"), list) or not w["accessionVersions"]:
                raise FormatError("a withdrawal names an organism and a non-empty list of accessionVersions")
            versions = sorted(set(w["accessionVersions"]))
            for av in versions:
                acc, _, ver = av.rpartition(".")
                if not acc or not ver.isdigit() or int(ver) < 1:
                    raise FormatError(f"withdrawal names a malformed accessionVersion {av!r}")
                if st.is_withdrawn(org, acc, int(ver)) or int(ver) in withdraw_now.get(org, {}).get(acc, []):
                    raise FormatError(f"{org} {av} is already withdrawn")
                withdraw_now.setdefault(org, {}).setdefault(acc, []).append(int(ver))
            note = w.get("note")
            if note is not None and not isinstance(note, str):
                raise FormatError("a withdrawal note is a string or null")
            withdraw_records.append((org, canonical.dumps({"organism": org, "accessionVersions": versions, "note": note})))
        for accs in withdraw_now.values():
            for vs in accs.values():
                vs.sort()

        for org, acc, ver in seen:
            if ver in withdraw_now.get(org, {}).get(acc, []):
                raise FormatError(f"entry is withdrawn and cannot be published: {(org, acc, ver)}")
        if count == 0 and not withdraw_records:
            raise EmptyBatch("a batch must contain at least one entry or one withdrawal")
        runs: dict[str, Path] = {}
        for org, sorter in sorters.items():
            path = work / f"{org}.run"
            sorter.finish(path)
            runs[org] = path

        # Pass 2: the body, compressed as it is produced. Schema records first, tooling second,
        # then entries by organism, accession, version.
        uncompressed_length = 0
        compressed = io.BytesIO()
        sink = _Compressor(codec, compressed)
        for org in sorted(schemas_now):
            uncompressed_length += sink.write(frame(RecordType.SCHEMA, canonical.dumps(schemas_now[org])))
        for p, c in sorted(tooling, key=lambda t: t[0]):
            uncompressed_length += sink.write(frame(RecordType.TOOLING, encode_tooling(p, c)))
        for _, payload in sorted(withdraw_records, key=lambda t: t[0]):
            uncompressed_length += sink.write(frame(RecordType.WITHDRAW, payload))
        for org in sorted(runs):
            for _, _, payload in iter_run(runs[org]):
                uncompressed_length += sink.write(frame(RecordType.ENTRY, payload))
        sink.close()
        compressed_bytes = compressed.getvalue()
        body_digest = sha256(compressed_bytes)

        # Pass 3: cumulative artifact digests by merging previous entries with the new run,
        # leaving out anything withdrawn so far or in this batch.
        withdrawn_after = {o: {a: list(v) for a, v in accs.items()} for o, accs in st.withdrawn.items()}
        for org, accs in withdraw_now.items():
            for acc, vs in accs.items():
                withdrawn_after.setdefault(org, {}).setdefault(acc, []).extend(vs)
        for accs in withdrawn_after.values():
            for vs in accs.values():
                vs.sort()
        organisms = sorted(set(st.entries_total) | set(runs) | set(withdraw_now) | set(st.withdrawn))
        artifacts: dict[str, dict] = {}
        for org in organisms:
            if st.entries_total.get(org, 0) > 0:
                if previous_entries is None:
                    raise FormatError(f"previous entries of {org} are needed for the cumulative digest")
                prev: Iterable[Record] = previous_entries(org)
            else:
                prev = ()
            new: Iterable[Record] = iter_run(runs[org]) if org in runs else ()
            excluded = withdrawn_after.get(org, {})
            digest, total, new_count = _digest_merged(prev, new, excluded)
            withdrawn_in_batch = sum(len(v) for v in withdraw_now.get(org, {}).values())
            artifacts[org] = {
                "entriesInBatch": new_count,
                "entriesTotal": total,
                "withdrawnInBatch": withdrawn_in_batch,
                "withdrawnTotal": sum(len(v) for v in excluded.values()),
                "artifactSha256": digest,
            }

        # Published set after this batch, for the index; the keys are already in hand.
        published = {o: {a: [list(p) for p in vs] for a, vs in accs.items()} for o, accs in st.published.items()}
        for org, acc, ver in seen:
            published.setdefault(org, {}).setdefault(acc, []).append([ver, st.next_batch])
        for accs in published.values():
            for vs in accs.values():
                vs.sort()

        has_index = force_index or st.bytes_since_index + len(compressed_bytes) >= INDEX_THRESHOLD_BYTES
        index = {"batch": st.next_batch, "entries": published, "withdrawn": withdrawn_after} if has_index else None

        begin = BatchBegin(
            batch=st.next_batch,
            first_blob_seq=st.next_blob_seq,
            previous_manifest_digest=st.previous_manifest_digest,
            codec=codec,
            uncompressed_length=uncompressed_length,
            compressed_length=len(compressed_bytes),
            body_digest=body_digest,
        )
        prefix = b"".join(
            ([frame(RecordType.HEADER, self.header.encode())] if st.next_batch == 0 and st.next_blob_seq == 0 else [])
            + [frame(RecordType.BATCH_BEGIN, begin.encode()), frame(RecordType.BODY, compressed_bytes)]
            + ([frame(RecordType.INDEX, bytes([codec]) + compress(codec, canonical.dumps(index)))] if index else [])
        )

        # The manifest states the blob count after the batch, which depends on the length of
        # the batch including the manifest itself. The number has a handful of digits, so
        # iterating from a guess settles in one or two rounds.
        blob_count_after = st.next_blob_seq + blobs_needed(len(prefix) + 200)
        for _ in range(8):
            manifest = {
                "batch": st.next_batch,
                "firstBlobSeq": st.next_blob_seq,
                "blobCountAfter": blob_count_after,
                "previousManifestDigest": st.previous_manifest_digest.hex(),
                "bodyDigest": body_digest.hex(),
                "hasIndex": has_index,
                "organisms": artifacts,
            }
            manifest_payload = canonical.dumps(manifest)
            unpadded = prefix + frame(RecordType.BATCH_MANIFEST, manifest_payload)
            n = st.next_blob_seq + blobs_needed(len(unpadded))
            if n == blob_count_after:
                break
            blob_count_after = n
        else:
            raise FormatError("blob count did not settle")

        blob_count = blob_count_after - st.next_blob_seq
        stream = unpadded + b"\x00" * (blob_count * BLOB_DATA_BYTES - len(unpadded))
        blobs = pack_blobs(stream)
        manifest_digest = sha256(manifest_payload)

        # Advance.
        st.previous_manifest_digest = manifest_digest
        st.next_batch += 1
        st.next_blob_seq = blob_count_after
        st.published = published
        st.withdrawn = withdrawn_after
        st.schemas.update(schemas_now)
        for org, a in artifacts.items():
            st.entries_total[org] = a["entriesTotal"]
        st.bytes_since_index = 0 if has_index else st.bytes_since_index + len(compressed_bytes)

        return EncodedBatch(
            batch=begin.batch,
            first_blob_seq=begin.first_blob_seq,
            blob_count_after=blob_count_after,
            last_blob_chunk_count=last_blob_chunk_count(len(unpadded) - (blob_count - 1) * BLOB_DATA_BYTES),
            stream=stream,
            blobs=blobs,
            body_digest=body_digest,
            manifest=manifest,
            manifest_digest=manifest_digest,
            index=index,
            schemas_published=sorted(schemas_now),
            entry_count=count,
            withdrawn_count=sum(len(v) for accs in withdraw_now.values() for v in accs.values()),
        )


class _Compressor:
    """Writes body bytes through the codec into a buffer, counting the uncompressed bytes."""

    def __init__(self, codec: int, out: io.BytesIO):
        self.out = out
        if codec == CODEC_RAW:
            self._c = None
        elif codec == CODEC_ZSTD:
            self._c = zstandard.ZstdCompressor(level=ZSTD_LEVEL).compressobj()
        else:
            raise FormatError(f"unknown codec {codec}")

    def write(self, data: bytes) -> int:
        self.out.write(data if self._c is None else self._c.compress(data))
        return len(data)

    def close(self) -> None:
        if self._c is not None:
            self.out.write(self._c.flush())


def _digest_merged(previous: Iterable[Record], new: Iterable[Record], excluded: dict[str, list[int]]) -> tuple[str, int, int]:
    """SHA-256 and line count of the output file formed by merging two sorted runs minus the
    excluded (withdrawn) keys, and the count of new records that made it in."""
    h = hashlib.sha256()
    total = 0
    new_count = 0
    prev_tagged = ((rec, 0) for rec in previous)
    new_tagged = ((rec, 1) for rec in new)
    for rec, is_new in heapq.merge(prev_tagged, new_tagged, key=lambda item: (item[0][0], item[0][1])):
        if rec[1] in excluded.get(rec[0], ()):
            continue
        h.update(rec[2])
        h.update(b"\n")
        total += 1
        new_count += is_new
    return h.hexdigest(), total, new_count
