"""Encoding batches of entries into the stream, blob by blob.

The encoder holds the little state the stream has: the next batch number, the next blob
sequence number, the digest of the previous manifest, which organisms have had a schema
record, the published accessionVersions, and how many compressed bytes have gone by since
the last index. That state is rebuilt from the chain and the stream by the decoder, so an
encoder can be resumed on any machine.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, blobs_needed, last_blob_chunk_count, pack_blobs
from loculus_eternal.format.records import (
    CODEC_ZSTD,
    DIGEST_BYTES,
    BatchBegin,
    FormatError,
    Header,
    RecordType,
    compress,
    encode_tooling,
    frame,
    sha256,
)

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


def sort_key_from_payload(payload: bytes) -> tuple[str, int]:
    """(accession, version) of a canonical entry payload, for merging sorted runs."""
    m = canonical.loads(payload)["metadata"]
    return (m["accession"], m["version"])


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
    # organism -> entries published so far
    entries_total: dict[str, int] = field(default_factory=dict)
    bytes_since_index: int = 0

    def is_published(self, organism: str, accession: str, version: int) -> bool:
        return any(v == version for v, _ in self.published.get(organism, {}).get(accession, []))


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


PreviousEntries = Callable[[str], Iterable[bytes]]


class StreamEncoder:
    def __init__(self, chain_id: int, contract: bytes, state: EncoderState | None = None, schema_id: str = "pathoplexus"):
        self.header = Header(chain_id=chain_id, contract=contract, schema_id=schema_id)
        self.state = state or EncoderState()

    def encode_batch(
        self,
        entries: Iterable[dict],
        previous_entries: PreviousEntries | None = None,
        tooling: Iterable[tuple[str, bytes]] = (),
        codec: int = CODEC_ZSTD,
        force_index: bool = False,
    ) -> EncodedBatch:
        """Encode one batch and advance the state.

        `previous_entries(organism)` must yield the canonical payloads of every entry of that
        organism published in earlier batches, in materialisation order; it is needed for the
        cumulative artifact digests. It may be omitted only when nothing has been published.
        """
        st = self.state
        new = list(entries)
        for e in new:
            Entry.validate(e)
        new.sort(key=Entry.key)
        if not new:
            raise FormatError("a batch must contain at least one entry")

        # Validate, check for duplicates against the stream and within the batch, and build
        # the inner records. Schema records go first, tooling second, entries last.
        schemas_now: dict[str, dict] = {}
        inner_entries: list[bytes] = []
        seen: set[EntryKey] = set()
        per_org_new: dict[str, list[bytes]] = {}
        for e in new:
            k = Entry.key(e)
            if k in seen:
                raise FormatError(f"duplicate entry in batch: {k}")
            seen.add(k)
            if st.is_published(*k):
                raise FormatError(f"entry already published: {k}")
            schema = Entry.schema(e)
            if st.schemas.get(k[0]) != schema and schemas_now.get(k[0]) != schema:
                schemas_now[k[0]] = schema
            payload = Entry.payload(e)
            inner_entries.append(frame(RecordType.ENTRY, payload))
            per_org_new.setdefault(k[0], []).append(payload)

        body = b"".join(
            [frame(RecordType.SCHEMA, canonical.dumps(schemas_now[o])) for o in sorted(schemas_now)]
            + [frame(RecordType.TOOLING, encode_tooling(p, c)) for p, c in sorted(tooling, key=lambda t: t[0])]
            + inner_entries
        )
        compressed = compress(codec, body)
        body_digest = sha256(compressed)

        # Cumulative artifact digests: merge previously published payloads with the new ones.
        organisms = sorted(set(st.entries_total) | set(per_org_new))
        artifacts: dict[str, dict] = {}
        for org in organisms:
            if st.entries_total.get(org, 0) > 0:
                if previous_entries is None:
                    raise FormatError(f"previous entries of {org} are needed for the cumulative digest")
                prev: Iterable[bytes] = previous_entries(org)
            else:
                prev = ()
            digest = _digest_materialised(prev, per_org_new.get(org, []))
            artifacts[org] = {
                "entriesInBatch": len(per_org_new.get(org, [])),
                "entriesTotal": st.entries_total.get(org, 0) + len(per_org_new.get(org, [])),
                "artifactSha256": digest.hex(),
            }

        # Published set after this batch, for the index.
        published = {o: {a: [list(p) for p in vs] for a, vs in accs.items()} for o, accs in st.published.items()}
        for e in new:
            org, acc, ver = Entry.key(e)
            published.setdefault(org, {}).setdefault(acc, []).append([ver, st.next_batch])
        for accs in published.values():
            for vs in accs.values():
                vs.sort()

        has_index = force_index or st.bytes_since_index + len(compressed) >= INDEX_THRESHOLD_BYTES
        index = {"batch": st.next_batch, "entries": published} if has_index else None

        begin = BatchBegin(
            batch=st.next_batch,
            first_blob_seq=st.next_blob_seq,
            previous_manifest_digest=st.previous_manifest_digest,
            codec=codec,
            uncompressed_length=len(body),
            compressed_length=len(compressed),
            body_digest=body_digest,
        )
        prefix = b"".join(
            ([frame(RecordType.HEADER, self.header.encode())] if st.next_batch == 0 and st.next_blob_seq == 0 else [])
            + [frame(RecordType.BATCH_BEGIN, begin.encode()), frame(RecordType.BODY, compressed)]
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
        st.schemas.update(schemas_now)
        for org, a in artifacts.items():
            st.entries_total[org] = a["entriesTotal"]
        st.bytes_since_index = 0 if has_index else st.bytes_since_index + len(compressed)

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
        )


def materialise(payloads: Iterable[bytes]) -> Iterator[bytes]:
    """Yield the lines of an organism's output file given its payloads in any order."""
    for p in sorted(payloads, key=sort_key_from_payload):
        yield p + b"\n"


def _digest_materialised(previous: Iterable[bytes], new: list[bytes]) -> bytes:
    """SHA-256 of the output file formed by merging two sorted runs of payloads."""
    import hashlib

    h = hashlib.sha256()
    merged = heapq.merge(previous, sorted(new, key=sort_key_from_payload), key=sort_key_from_payload)
    for p in merged:
        h.update(p)
        h.update(b"\n")
    return h.digest()
