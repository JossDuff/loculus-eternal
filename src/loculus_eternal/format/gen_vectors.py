"""Generate the golden vectors in vectors/.

Run with `uv run python -m loculus_eternal.format.gen_vectors`. The output is deterministic
for codec 0 streams; codec 1 streams depend on the zstd implementation and are published as
decode vectors only. Never hand-edit the output: change the generator, regenerate, and
explain the diff in the commit.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from loculus_eternal.format import canonical, varint
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, last_blob_chunk_count, pack_blobs
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import StreamEncoder
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD

CHAIN_ID = 11155111
CONTRACT = bytes.fromhex("1111111111111111111111111111111111111111")
VECTORS_DIR = Path(__file__).resolve().parents[3] / "vectors"


def sample_entry(organism: str, accession: str, version: int, *, seq_len: int = 40, revocation: bool = False, extra: dict | None = None) -> dict:
    """A small synthetic entry in the published shape. Sequences are a repeating pattern of
    the stated length so vectors stay readable."""
    pattern = "ACGTTGCA"
    seq = (pattern * (seq_len // len(pattern) + 1))[:seq_len]
    metadata = {
        "accession": accession,
        "version": version,
        "accessionVersion": f"{accession}.{version}",
        "isRevocation": revocation,
        "pipelineVersion": 7,
        "releasedDate": "2026-09-30",
        "submitter": "insdc_ingest_user",
        "geoLocCountry": "Switzerland",
        "sampleCollectionDate": None,
        "hostAge": 42,
        "geoLocLatitude": 46.52,
        "authors": "Müller, A.; 山田, 太郎",
    }
    if extra:
        metadata.update(extra)
    return {
        "organism": organism,
        "metadata": metadata,
        "unalignedNucleotideSequences": {"main": seq},
        "alignedNucleotideSequences": {"main": seq.replace("T", "N", 1)},
        "nucleotideInsertions": {"main": ["12:AC"] if version > 1 else []},
        "alignedAminoAcidSequences": {"E": "MKVL*", "NS1": None},
        "aminoAcidInsertions": {"E": [], "NS1": []},
    }


def genesis_entries() -> list[dict]:
    return [
        sample_entry("zika", "PP_000002", 1),
        sample_entry("zika", "PP_000001", 1),
        sample_entry("zika", "PP_000001", 2),
        sample_entry("mpox", "PP_000003", 1, extra={"clade": "IIb"}),
    ]


def second_batch_entries() -> list[dict]:
    return [
        sample_entry("zika", "PP_000001", 3, revocation=True),
        sample_entry("zika", "PP_000010", 1),
        sample_entry("hmpv", "PP_000004", 1, extra={"lineage": "A2.2"}),
    ]


def tooling() -> list[tuple[str, bytes]]:
    return [("docs/container-spec.md", b"# Container specification\n(sample)\n"), ("src/loculus_eternal/__init__.py", b'"""sample"""\n')]


def _hex(b: bytes) -> str:
    return b.hex()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _batch_expectation(encoded, with_stream_hex: bool) -> dict:
    out = {
        "batch": encoded.batch,
        "firstBlobSeq": encoded.first_blob_seq,
        "blobCountAfter": encoded.blob_count_after,
        "lastBlobChunkCount": encoded.last_blob_chunk_count,
        "streamLength": len(encoded.stream),
        "streamSha256": _sha(encoded.stream),
        "blobSha256": [_sha(b) for b in encoded.blobs],
        "bodyDigest": _hex(encoded.body_digest),
        "manifest": encoded.manifest,
        "manifestDigest": _hex(encoded.manifest_digest),
        "index": encoded.index,
        "schemasPublished": encoded.schemas_published,
    }
    if with_stream_hex:
        out["streamHex"] = _hex(encoded.stream.rstrip(b"\x00"))
    return out


def _artifacts(decoded) -> dict:
    return {o: {"ndjson": decoded.materialise(o).decode("utf-8"), "sha256": d} for o, d in decoded.artifact_digests().items()}


def vec_varint() -> dict:
    values = [0, 1, 127, 128, 255, 300, 16383, 16384, 2**32 - 1, 2**63, 2**64 - 1]
    return {
        "valid": [{"value": str(v), "hex": _hex(varint.encode(v))} for v in values],
        "invalid": {
            "redundant trailing zero group": ["8000", "ff00", "818000"],
            "longer than ten bytes": ["ffffffffffffffffffff7f", "8080808080808080808000"],
            "above 2^64-1": ["ffffffffffffffffff02", "80808080808080808002"],
            "truncated": ["80", "ffff"],
        },
    }


def vec_canonical_json() -> dict:
    samples = [
        {"b": 1, "a": [1.0, 1e21, 0.000001, -0.0, 10], "é": "ü", "z": None, "t": True},
        {"nested": {"y": {"k": "v"}, "x": " \u0007\"\\"}, "n": 9007199254740991},
        sample_entry("zika", "PP_000001", 1),
    ]
    return {"samples": [{"input": s, "canonical": canonical.dumps(s).decode("utf-8")} for s in samples]}


def vec_chunks() -> dict:
    stream = bytes(range(256)) * 600  # 153,600 bytes: two blobs, the second partially filled
    blobs = pack_blobs(stream)
    return {
        "streamLength": len(stream),
        "streamSha256": _sha(stream),
        "streamRule": "bytes 0x00..0xff repeated 600 times",
        "blobCount": len(blobs),
        "blobSha256": [_sha(b) for b in blobs],
        "lastBlobChunkCount": last_blob_chunk_count(len(stream)),
        "elementSamples": [
            {"blob": 0, "element": 0, "hex": _hex(blobs[0][0:32])},
            {"blob": 0, "element": 1, "hex": _hex(blobs[0][32:64])},
            {"blob": 0, "element": 4095, "hex": _hex(blobs[0][4095 * 32 : 4096 * 32])},
            {"blob": 1, "element": 0, "hex": _hex(blobs[1][0:32])},
            {"blob": 1, "element": last_blob_chunk_count(len(stream)) - 1, "hex": _hex(blobs[1][(last_blob_chunk_count(len(stream)) - 1) * 32 : last_blob_chunk_count(len(stream)) * 32])},
            {"blob": 1, "element": last_blob_chunk_count(len(stream)), "hex": "00" * 32},
        ],
        "blobDataBytes": BLOB_DATA_BYTES,
    }


def vec_batch(codec: int) -> dict:
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b = enc.encode_batch(genesis_entries(), tooling=tooling(), codec=codec)
    dec = StreamDecoder(b.blobs).decode()
    assert not dec.torn and all(dec.verify_artifacts().values())
    return {
        "note": "codec 1 streams are decode vectors: compressed bytes are not canonical" if codec == CODEC_ZSTD else "codec 0: any conforming encoder produces these exact bytes",
        "decodeOnly": codec == CODEC_ZSTD,
        "input": {"chainId": CHAIN_ID, "contract": _hex(CONTRACT), "codec": codec, "entries": genesis_entries(), "tooling": [{"path": p, "contentHex": _hex(c)} for p, c in tooling()]},
        "expected": _batch_expectation(b, with_stream_hex=True),
        "artifacts": _artifacts(dec),
    }


def vec_multi_blob() -> dict:
    entries = [sample_entry("mpox", f"PP_00010{i}", 1, seq_len=60000) for i in range(3)]
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b = enc.encode_batch(entries, codec=CODEC_RAW)
    assert len(b.blobs) == 3 and b.last_blob_chunk_count < 4096
    dec = StreamDecoder(b.blobs).decode()
    assert not dec.torn and all(dec.verify_artifacts().values())
    return {
        "input": {"chainId": CHAIN_ID, "contract": _hex(CONTRACT), "codec": CODEC_RAW, "entries": entries, "tooling": []},
        "expected": _batch_expectation(b, with_stream_hex=False),
        "artifactSha256": dec.artifact_digests(),
    }


def vec_two_batches_index() -> dict:
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b0 = enc.encode_batch(genesis_entries(), tooling=tooling(), codec=CODEC_RAW)
    dec0 = StreamDecoder(b0.blobs).decode()
    enc1 = StreamEncoder(CHAIN_ID, CONTRACT, state=dec0.encoder_state())
    b1 = enc1.encode_batch(second_batch_entries(), previous_entries=dec0.records, codec=CODEC_RAW, force_index=True)
    dec = StreamDecoder(b0.blobs + b1.blobs).decode()
    assert not dec.torn and all(dec.verify_artifacts().values()) and dec.batches[1].index is not None
    return {
        "input": {
            "chainId": CHAIN_ID,
            "contract": _hex(CONTRACT),
            "codec": CODEC_RAW,
            "batches": [
                {"entries": genesis_entries(), "tooling": [{"path": p, "contentHex": _hex(c)} for p, c in tooling()], "forceIndex": False},
                {"entries": second_batch_entries(), "tooling": [], "forceIndex": True},
            ],
        },
        "expected": [_batch_expectation(b0, True), _batch_expectation(b1, True)],
        "published": dec.published(),
        "artifacts": _artifacts(dec),
    }


def vec_torn_batch() -> dict:
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b0 = enc.encode_batch(genesis_entries(), codec=CODEC_RAW)
    dec0 = StreamDecoder(b0.blobs).decode()
    big = [sample_entry("zika", f"PP_00020{i}", 1, seq_len=60000) for i in range(3)]
    # The interrupted attempt: only its first blob reached the chain.
    enc_t = StreamEncoder(CHAIN_ID, CONTRACT, state=dec0.encoder_state())
    attempt = enc_t.encode_batch(big, previous_entries=dec0.records, codec=CODEC_RAW)
    torn_blob = attempt.blobs[0]
    # The retry starts at the next blob boundary with the same batch number.
    enc_r = StreamEncoder(CHAIN_ID, CONTRACT, state=dec0.encoder_state())
    enc_r.state.next_blob_seq += 1
    retry = enc_r.encode_batch(big, previous_entries=dec0.records, codec=CODEC_RAW)
    blobs = b0.blobs + [torn_blob] + retry.blobs
    dec = StreamDecoder(blobs).decode()
    assert [t.first_blob for t in dec.torn] == [1] and [b.batch for b in dec.batches] == [0, 1]
    assert all(dec.verify_artifacts().values())
    return {
        "input": {
            "chainId": CHAIN_ID,
            "contract": _hex(CONTRACT),
            "codec": CODEC_RAW,
            "genesisEntries": genesis_entries(),
            "batch1Entries": big,
            "layout": "blob 0: genesis batch; blob 1: first blob of an interrupted batch 1; blobs 2..: batch 1 republished",
        },
        "blobSha256": [_sha(b) for b in blobs],
        "expected": {
            "torn": [{"expectedBatch": t.expected_batch, "firstBlob": t.first_blob, "lastBlob": t.last_blob} for t in dec.torn],
            "batches": [{"batch": b.batch, "firstBlobSeq": b.first_blob_seq, "blobCountAfter": b.blob_count_after, "manifestDigest": _hex(b.manifest_digest)} for b in dec.batches],
            "published": dec.published(),
            "artifactSha256": dec.artifact_digests(),
        },
    }


def vec_withdrawal() -> dict:
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b0 = enc.encode_batch(genesis_entries(), codec=CODEC_RAW)
    dec0 = StreamDecoder(b0.blobs).decode()
    withdrawal = {"organism": "zika", "accessionVersions": ["PP_000001.1"], "note": "withdrawn in the vector for the sample's sake"}
    b1 = enc.encode_batch([sample_entry("zika", "PP_000010", 1)], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[withdrawal], force_index=True)
    dec = StreamDecoder(b0.blobs + b1.blobs).decode()
    assert not dec.torn and all(dec.verify_artifacts().values())
    assert dec.is_withdrawn("zika", "PP_000001", 1) and dec.count("zika") == 3
    return {
        "input": {
            "chainId": CHAIN_ID,
            "contract": _hex(CONTRACT),
            "codec": CODEC_RAW,
            "batches": [
                {"entries": genesis_entries(), "tooling": [], "withdrawals": [], "forceIndex": False},
                {"entries": [sample_entry("zika", "PP_000010", 1)], "tooling": [], "withdrawals": [withdrawal], "forceIndex": True},
            ],
        },
        "expected": [_batch_expectation(b0, True), _batch_expectation(b1, True)],
        "published": dec.published(),
        "withdrawn": dec.withdrawn(),
        "artifacts": _artifacts(dec),
    }


GENERATORS = {
    "varint.json": vec_varint,
    "canonical_json.json": vec_canonical_json,
    "chunks.json": vec_chunks,
    "batch_raw.json": lambda: vec_batch(CODEC_RAW),
    "batch_zstd.json": lambda: vec_batch(CODEC_ZSTD),
    "multi_blob.json": vec_multi_blob,
    "two_batches_index.json": vec_two_batches_index,
    "torn_batch.json": vec_torn_batch,
    "withdrawal.json": vec_withdrawal,
}


def render(name: str) -> bytes:
    return (json.dumps(GENERATORS[name](), indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    check = "--check" in argv
    VECTORS_DIR.mkdir(exist_ok=True)
    differing = []
    for name in GENERATORS:
        data = render(name)
        path = VECTORS_DIR / name
        if check:
            if not path.exists() or path.read_bytes() != data:
                differing.append(name)
        else:
            path.write_bytes(data)
            print(f"wrote {path} ({len(data):,} bytes)")
    if differing:
        print("vectors differ from the generator:", ", ".join(differing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
