"""F4 canonical JSON, F5 chunk packing."""

import hashlib

import pytest

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import (
    BLOB_BYTES,
    BLOB_DATA_BYTES,
    ChunkError,
    blobs_needed,
    last_blob_chunk_count,
    pack_blobs,
    unpack_blob,
    unpack_blobs,
)


def test_F4_canonical_json_matches_vectors(vectors):
    for case in vectors["canonical_json.json"]["samples"]:
        out = canonical.dumps(case["input"])
        assert out.decode("utf-8") == case["canonical"]
        assert canonical.is_canonical(out)


def test_F4_canonical_json_sorts_keys_and_strips_whitespace():
    assert canonical.dumps({"b": 1, "a": {"d": 2, "c": [1, 2]}}) == b'{"a":{"c":[1,2],"d":2},"b":1}'


def test_F4_canonical_json_is_a_fixed_point():
    obj = {"x": [1.5, 1e300, "ü\n"], "a": None}
    once = canonical.dumps(obj)
    assert canonical.dumps(canonical.loads(once)) == once
    assert not canonical.is_canonical(b'{"b": 1, "a": 2}')


def test_F5_pack_matches_vectors(vectors):
    v = vectors["chunks.json"]
    stream = bytes(range(256)) * 600
    assert hashlib.sha256(stream).hexdigest() == v["streamSha256"]
    blobs = pack_blobs(stream)
    assert [hashlib.sha256(b).hexdigest() for b in blobs] == v["blobSha256"]
    assert last_blob_chunk_count(len(stream)) == v["lastBlobChunkCount"]
    for s in v["elementSamples"]:
        assert blobs[s["blob"]][s["element"] * 32 : (s["element"] + 1) * 32].hex() == s["hex"]


def test_F5_every_element_has_zero_high_byte_and_padding_is_zero():
    stream = b"\xff" * (BLOB_DATA_BYTES + 5)
    blobs = pack_blobs(stream)
    assert len(blobs) == 2 and all(len(b) == BLOB_BYTES for b in blobs)
    for b in blobs:
        assert all(b[i] == 0 for i in range(0, BLOB_BYTES, 32))
    # Second blob: one element of 5 data bytes, everything after it zero.
    assert blobs[1][1:6] == b"\xff" * 5 and not any(blobs[1][6:])
    assert last_blob_chunk_count(len(stream)) == 1


def test_F5_unpack_inverts_pack_and_rejects_bad_high_byte():
    stream = bytes(range(256)) * 10
    assert unpack_blobs(pack_blobs(stream))[: len(stream)] == stream
    bad = bytearray(pack_blobs(stream)[0])
    bad[32] = 1
    with pytest.raises(ChunkError, match="high byte"):
        unpack_blob(bytes(bad))
    with pytest.raises(ChunkError):
        unpack_blob(b"\x00" * (BLOB_BYTES - 1))


@pytest.mark.parametrize("length,blobs,chunks", [(1, 1, 1), (31, 1, 1), (32, 1, 2), (BLOB_DATA_BYTES, 1, 4096), (BLOB_DATA_BYTES + 1, 2, 1)])
def test_F5_blob_and_chunk_counts(length, blobs, chunks):
    assert blobs_needed(length) == blobs
    assert last_blob_chunk_count(length) == chunks
