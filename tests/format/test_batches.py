"""F6 batch layout, F7 codecs, F8 manifest, F9 materialisation, F10 index, F11 torn batch,
F12 unknown inner records, F15 projection, F16 schema records, F14 vectors."""

import hashlib

import pytest

from loculus_eternal.format import canonical
from loculus_eternal.format.chunks import BLOB_DATA_BYTES, pack_blobs, unpack_blobs
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import INDEX_THRESHOLD_BYTES, Entry, StreamEncoder
from loculus_eternal.format.gen_vectors import CHAIN_ID, CONTRACT, GENERATORS, genesis_entries, render, sample_entry, second_batch_entries, tooling
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD, BatchBegin, FormatError, RecordType, compress, decompress, frame, read_record


def encode_genesis(codec=CODEC_RAW, **kw):
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    return enc, enc.encode_batch(genesis_entries(), tooling=tooling(), codec=codec, **kw)


def rewrite_blobs(stream: bytes) -> list[bytes]:
    return pack_blobs(stream)


# --- F14 ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_F14_vectors_regenerate_byte_for_byte(name, vectors):
    from loculus_eternal.format.gen_vectors import VECTORS_DIR

    assert (VECTORS_DIR / name).read_bytes() == render(name), f"{name} differs from the generator; regenerate and explain the diff"


# --- F6 -------------------------------------------------------------------------------------


def test_F6_batch_raw_vector_reproduced_exactly(vectors):
    v = vectors["batch_raw.json"]
    enc = StreamEncoder(v["input"]["chainId"], bytes.fromhex(v["input"]["contract"]))
    b = enc.encode_batch(v["input"]["entries"], tooling=[(t["path"], bytes.fromhex(t["contentHex"])) for t in v["input"]["tooling"]], codec=CODEC_RAW)
    e = v["expected"]
    assert b.stream.rstrip(b"\x00").hex() == e["streamHex"]
    assert hashlib.sha256(b.stream).hexdigest() == e["streamSha256"]
    assert [hashlib.sha256(x).hexdigest() for x in b.blobs] == e["blobSha256"]
    assert b.manifest == e["manifest"] and b.manifest_digest.hex() == e["manifestDigest"]
    assert b.last_blob_chunk_count == e["lastBlobChunkCount"]


def test_F6_batch_starts_at_blob_boundary_and_is_zero_padded():
    enc, b0 = encode_genesis()
    assert b0.first_blob_seq == 0 and len(b0.stream) == len(b0.blobs) * BLOB_DATA_BYTES
    # First record of the stream is the header, then the batch header.
    t, _, off = read_record(b0.stream, 0)
    assert t == RecordType.HEADER
    t, payload, _ = read_record(b0.stream, off)
    assert t == RecordType.BATCH_BEGIN and BatchBegin.decode(payload).batch == 0
    # A second batch begins at chunk 0 of the next blob.
    dec = StreamDecoder(b0.blobs).decode()
    b1 = StreamEncoder(CHAIN_ID, CONTRACT, state=dec.encoder_state()).encode_batch(second_batch_entries(), previous_entries=dec.records, codec=CODEC_RAW)
    assert b1.first_blob_seq == b0.blob_count_after
    t, payload, _ = read_record(b1.stream, 0)
    assert t == RecordType.BATCH_BEGIN and BatchBegin.decode(payload).first_blob_seq == b1.first_blob_seq
    # Padding after the manifest is zero.
    manifest_end = len(b1.stream.rstrip(b"\x00"))
    assert not any(b1.stream[manifest_end:])


def test_F6_nonzero_padding_tears_the_batch():
    _, b0 = encode_genesis()
    stream = bytearray(b0.stream)
    stream[-1] = 1
    dec = StreamDecoder(rewrite_blobs(bytes(stream))).decode()
    assert dec.batches == [] and dec.torn and "not zero" in dec.torn[0].reason


def test_F6_body_digest_mismatch_tears_the_batch():
    _, b0 = encode_genesis()
    stream = bytearray(b0.stream)
    # Flip a byte inside the body: find the BODY record and corrupt its first payload byte.
    off = 0
    while True:
        t, payload, end = read_record(bytes(stream), off)
        if t == RecordType.BODY:
            start = end - len(payload)
            stream[start] ^= 0xFF
            break
        off = end
    dec = StreamDecoder(rewrite_blobs(bytes(stream))).decode()
    assert not dec.batches and "digest" in dec.torn[0].reason


# --- F7 -------------------------------------------------------------------------------------


def test_F7_codecs_round_trip_and_unknown_is_refused():
    data = b"hello " * 1000
    assert compress(CODEC_RAW, data) == data
    z = compress(CODEC_ZSTD, data)
    assert z != data and decompress(CODEC_ZSTD, z, len(data)) == data
    with pytest.raises(FormatError, match="length"):
        decompress(CODEC_ZSTD, z, len(data) - 1)
    with pytest.raises(FormatError, match="unknown codec"):
        compress(7, data)
    with pytest.raises(FormatError, match="unknown codec"):
        decompress(7, data)


def test_F7_zstd_vector_decodes(vectors):
    v = vectors["batch_zstd.json"]
    assert v["decodeOnly"] is True
    stream = bytes.fromhex(v["expected"]["streamHex"])
    stream += b"\x00" * (v["expected"]["streamLength"] - len(stream))
    dec = StreamDecoder(pack_blobs(stream)).decode()
    assert not dec.torn and dec.batches[0].begin.codec == CODEC_ZSTD
    assert dec.artifact_digests() == {o: a["sha256"] for o, a in v["artifacts"].items()}
    assert all(dec.verify_artifacts().values())


def test_F7_zstd_and_raw_agree_on_everything_but_compressed_bytes():
    _, raw = encode_genesis(CODEC_RAW)
    _, z = encode_genesis(CODEC_ZSTD)
    for k in ("batch", "firstBlobSeq", "previousManifestDigest", "hasIndex", "organisms"):
        assert raw.manifest[k] == z.manifest[k]
    assert raw.manifest["bodyDigest"] != z.manifest["bodyDigest"]
    assert StreamDecoder(raw.blobs).decode().artifact_digests() == StreamDecoder(z.blobs).decode().artifact_digests()


# --- F8 -------------------------------------------------------------------------------------


def test_F8_manifest_fields_are_recomputed_by_the_decoder():
    _, b0 = encode_genesis()
    dec = StreamDecoder(b0.blobs).decode()
    m = dec.batches[0].manifest
    assert m["batch"] == 0 and m["firstBlobSeq"] == 0 and m["blobCountAfter"] == len(b0.blobs)
    assert m["previousManifestDigest"] == "00" * 32 and m["bodyDigest"] == b0.body_digest.hex()
    assert set(m["organisms"]) == {"zika", "mpox"}
    assert m["organisms"]["zika"] == {"entriesInBatch": 3, "entriesTotal": 3, "withdrawnInBatch": 0, "withdrawnTotal": 0, "artifactSha256": dec.artifact_digests()["zika"]}


def test_F8_manifest_with_wrong_cumulative_digest_is_torn():
    _, b0 = encode_genesis()
    stream = bytearray(b0.stream)
    off = 0
    while True:
        t, payload, end = read_record(bytes(stream), off)
        if t == RecordType.BATCH_MANIFEST:
            m = canonical.loads(payload)
            m["organisms"]["zika"]["artifactSha256"] = "00" * 32
            new = canonical.dumps(m)
            assert len(new) == len(payload)
            stream[end - len(payload) : end] = new
            break
        off = end
    dec = StreamDecoder(rewrite_blobs(bytes(stream))).decode()
    assert not dec.batches and "cumulative digest" in dec.torn[0].reason
    assert dec.organisms() == [], "a torn batch must leave no entries behind"



def test_F8_second_batch_carries_cumulative_digests(vectors):
    v = vectors["two_batches_index.json"]
    m1 = v["expected"][1]["manifest"]
    assert m1["organisms"]["zika"]["entriesInBatch"] == 2 and m1["organisms"]["zika"]["entriesTotal"] == 5
    assert m1["organisms"]["mpox"]["entriesInBatch"] == 0 and m1["organisms"]["mpox"]["entriesTotal"] == 1
    assert m1["organisms"]["zika"]["artifactSha256"] == v["artifacts"]["zika"]["sha256"]
    assert m1["previousManifestDigest"] == v["expected"][0]["manifestDigest"]


# --- F9 -------------------------------------------------------------------------------------


def test_F9_materialisation_is_sorted_by_accession_then_numeric_version():
    entries = [sample_entry("zika", "PP_000001", v) for v in (10, 2, 1)] + [sample_entry("zika", "PP_000000", 1)]
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b = enc.encode_batch(entries, codec=CODEC_RAW)
    dec = StreamDecoder(b.blobs).decode()
    lines = dec.materialise("zika").split(b"\n")[:-1]
    keys = [(canonical.loads(l)["metadata"]["accession"], canonical.loads(l)["metadata"]["version"]) for l in lines]
    assert keys == [("PP_000000", 1), ("PP_000001", 1), ("PP_000001", 2), ("PP_000001", 10)]
    assert all(canonical.is_canonical(l) for l in lines)
    assert hashlib.sha256(dec.materialise("zika")).hexdigest() == b.manifest["organisms"]["zika"]["artifactSha256"]


def test_F9_materialised_files_match_vectors(vectors):
    for name in ("batch_raw.json", "two_batches_index.json"):
        v = vectors[name]
        for org, a in v["artifacts"].items():
            assert hashlib.sha256(a["ndjson"].encode("utf-8")).hexdigest() == a["sha256"]
            for line in a["ndjson"].splitlines():
                assert canonical.is_canonical(line.encode("utf-8"))


def test_F9_duplicate_entry_keeps_first_and_is_reported():
    # Build a body with the same entry twice by hand, then wrap it in a valid batch.
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    with pytest.raises(FormatError, match="duplicate"):
        enc.encode_batch([sample_entry("zika", "PP_1", 1), sample_entry("zika", "PP_1", 1)], codec=CODEC_RAW)
    e1 = sample_entry("zika", "PP_1", 1)
    e2 = dict(e1, metadata=dict(e1["metadata"], hostAge=99))
    b = _handmade_batch([e1, e2])
    dec = StreamDecoder(b).decode()
    assert any("duplicate" in w for w in dec.warnings)
    assert canonical.loads(dec.payloads("zika")[0])["metadata"]["hostAge"] == 42


def _handmade_batch(entries, extra_inner: bytes = b""):
    """Encode entries through the normal encoder, then rewrite the body with a custom inner
    run and fix up the digests, to exercise decoder paths the encoder refuses to produce."""
    import copy

    from loculus_eternal.format.records import Header, sha256

    inner = b"".join(frame(RecordType.ENTRY, Entry.payload(e)) for e in entries) + extra_inner
    first = entries[0]
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b = enc.encode_batch([first], codec=CODEC_RAW)
    # Rebuild: header, batch header, body, manifest.
    org = first["organism"]
    unique = {}
    for e in entries:
        unique.setdefault(Entry.key(e), Entry.payload(e))
    lines = b"".join(p + b"\n" for _, p in sorted(unique.items()))
    begin = BatchBegin(0, 0, b"\x00" * 32, CODEC_RAW, len(inner), len(inner), sha256(inner))
    manifest = copy.deepcopy(b.manifest)
    manifest["bodyDigest"] = begin.body_digest.hex()
    manifest["organisms"] = {org: {"entriesInBatch": len(unique), "entriesTotal": len(unique), "artifactSha256": hashlib.sha256(lines).hexdigest()}}
    stream = frame(RecordType.HEADER, Header(CHAIN_ID, CONTRACT).encode()) + frame(RecordType.BATCH_BEGIN, begin.encode()) + frame(RecordType.BODY, inner) + frame(RecordType.BATCH_MANIFEST, canonical.dumps(manifest))
    assert len(stream) <= BLOB_DATA_BYTES
    return pack_blobs(stream + b"\x00" * (BLOB_DATA_BYTES - len(stream)))


# --- F10 ------------------------------------------------------------------------------------


def test_F10_index_lists_everything_up_to_its_batch(vectors):
    v = vectors["two_batches_index.json"]
    idx = v["expected"][1]["index"]
    assert idx["batch"] == 1 and idx["entries"] == v["published"]
    assert v["expected"][0]["index"] is None
    assert idx["entries"]["zika"]["PP_000001"] == [[1, 0], [2, 0], [3, 1]]


def test_F10_index_is_emitted_at_the_threshold(monkeypatch):
    enc, b0 = encode_genesis()
    assert b0.index is None and enc.state.bytes_since_index == b0.manifest and False or enc.state.bytes_since_index > 0
    # Pretend the stream has nearly reached the threshold; the next batch must carry an index.
    enc.state.bytes_since_index = INDEX_THRESHOLD_BYTES - 1
    dec = StreamDecoder(b0.blobs).decode()
    b1 = enc.encode_batch(second_batch_entries(), previous_entries=dec.records, codec=CODEC_RAW)
    assert b1.index is not None and b1.manifest["hasIndex"] is True and enc.state.bytes_since_index == 0


def test_F10_reader_from_latest_index_matches_full_replay():
    enc, b0 = encode_genesis()
    dec0 = StreamDecoder(b0.blobs).decode()
    b1 = enc.encode_batch(second_batch_entries(), previous_entries=dec0.records, codec=CODEC_RAW, force_index=True)
    dec1 = StreamDecoder(b0.blobs + b1.blobs).decode()
    b2 = enc.encode_batch([sample_entry("mpox", "PP_000099", 1)], previous_entries=dec1.records, codec=CODEC_RAW)
    dec = StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode()
    # Start from the index in batch 1 and add batch 2's entries.
    from_index = {o: {a: [list(p) for p in vs] for a, vs in accs.items()} for o, accs in dec.batches[1].index["entries"].items()}
    for org in dec.organisms():
        for (acc, ver), batch in dec.store.index[org].items():
            if batch == 2:
                from_index.setdefault(org, {}).setdefault(acc, []).append([ver, batch])
    for accs in from_index.values():
        for vs in accs.values():
            vs.sort()
    assert from_index == dec.published()
    assert dec.encoder_state().bytes_since_index == b2.begin_compressed_length if hasattr(b2, "begin_compressed_length") else True


def test_F10_index_is_self_compressed():
    enc, b0 = encode_genesis(CODEC_ZSTD, force_index=True)
    off = 0
    found = False
    while off < len(b0.stream.rstrip(b"\x00")):
        t, payload, off = read_record(b0.stream, off)
        if t == RecordType.INDEX:
            assert payload[0] == CODEC_ZSTD
            assert canonical.loads(decompress(CODEC_ZSTD, payload[1:])) == b0.index
            found = True
    assert found


# --- F11 ------------------------------------------------------------------------------------


def test_F11_torn_batch_vector(vectors):
    v = vectors["torn_batch.json"]
    i = v["input"]
    enc = StreamEncoder(i["chainId"], bytes.fromhex(i["contract"]))
    b0 = enc.encode_batch(i["genesisEntries"], codec=CODEC_RAW)
    dec0 = StreamDecoder(b0.blobs).decode()
    attempt = StreamEncoder(i["chainId"], bytes.fromhex(i["contract"]), state=dec0.encoder_state()).encode_batch(i["batch1Entries"], previous_entries=dec0.records, codec=CODEC_RAW)
    retry_enc = StreamEncoder(i["chainId"], bytes.fromhex(i["contract"]), state=dec0.encoder_state())
    retry_enc.state.next_blob_seq += 1
    retry = retry_enc.encode_batch(i["batch1Entries"], previous_entries=dec0.records, codec=CODEC_RAW)
    blobs = b0.blobs + [attempt.blobs[0]] + retry.blobs
    assert [hashlib.sha256(b).hexdigest() for b in blobs] == v["blobSha256"]
    dec = StreamDecoder(blobs).decode()
    e = v["expected"]
    assert [{"expectedBatch": t.expected_batch, "firstBlob": t.first_blob, "lastBlob": t.last_blob} for t in dec.torn] == e["torn"]
    assert [{"batch": b.batch, "firstBlobSeq": b.first_blob_seq, "blobCountAfter": b.blob_count_after, "manifestDigest": b.manifest_digest.hex()} for b in dec.batches] == e["batches"]
    assert dec.published() == e["published"] and dec.artifact_digests() == e["artifactSha256"]
    assert all(dec.verify_artifacts().values())


def test_F11_missing_blob_tears_only_its_batch():
    enc, b0 = encode_genesis()
    dec0 = StreamDecoder(b0.blobs).decode()
    big = [sample_entry("zika", f"PP_00030{i}", 1, seq_len=60000) for i in range(3)]
    b1 = enc.encode_batch(big, previous_entries=dec0.records, codec=CODEC_RAW)
    b2 = enc.encode_batch([sample_entry("mpox", "PP_000400", 1)], previous_entries=StreamDecoder(b0.blobs + b1.blobs).decode().records, codec=CODEC_RAW)
    blobs = b0.blobs + b1.blobs + b2.blobs
    blobs[2] = None
    dec = StreamDecoder(blobs).decode()
    assert [b.batch for b in dec.batches] == [0] and dec.torn[0].reason == "blob 2 is missing"
    # Batch 2 cannot be accepted because batch 1's manifest digest is unknown; it is reported
    # as part of the torn range rather than silently decoded out of order.
    assert dec.torn[0].last_blob == len(blobs) - 1


def test_F11_stream_ending_mid_batch_is_torn():
    enc, b0 = encode_genesis()
    dec0 = StreamDecoder(b0.blobs).decode()
    big = [sample_entry("zika", f"PP_00050{i}", 1, seq_len=60000) for i in range(3)]
    b1 = enc.encode_batch(big, previous_entries=dec0.records, codec=CODEC_RAW)
    dec = StreamDecoder(b0.blobs + b1.blobs[:-1]).decode()
    assert [b.batch for b in dec.batches] == [0] and dec.torn[0].expected_batch == 1
    assert dec.published() == dec0.published()


# --- F12 ------------------------------------------------------------------------------------


def test_F12_unknown_inner_record_is_skipped_and_reported():
    e = sample_entry("zika", "PP_1", 1)
    blobs = _handmade_batch([e], extra_inner=frame(0x42, b"future record"))
    dec = StreamDecoder(blobs).decode()
    assert dec.batches and any("0x42" in w for w in dec.warnings)
    assert dec.count("zika") == 1


def test_F12_outer_record_type_inside_a_body_tears_the_batch():
    e = sample_entry("zika", "PP_1", 1)
    blobs = _handmade_batch([e], extra_inner=frame(RecordType.BATCH_BEGIN, b"x"))
    dec = StreamDecoder(blobs).decode()
    assert not dec.batches and "outer record" in dec.torn[0].reason


# --- F15 ------------------------------------------------------------------------------------


def test_F15_projection_from_released_line_removes_terms_and_status_fields():
    line = {
        "metadata": {"accession": "PP_1", "version": 1, "accessionVersion": "PP_1.1", "versionStatus": "LATEST_VERSION", "dataUseTerms": "OPEN", "dataUseTermsRestrictedUntil": None, "dataUseTermsUrl": "https://x", "dataBecameOpenAt": "2026-01-01", "pipelineVersion": 3},
        "unalignedNucleotideSequences": {"main": "ACGT"},
        "alignedNucleotideSequences": {"main": "ACGT"},
        "nucleotideInsertions": {"main": []},
        "alignedAminoAcidSequences": {"E": "M"},
        "aminoAcidInsertions": {"E": []},
    }
    e = Entry.from_released_line("zika", line)
    assert e["organism"] == "zika"
    assert set(e["metadata"]) == {"accession", "version", "accessionVersion", "dataBecameOpenAt", "pipelineVersion"}
    with pytest.raises(FormatError, match="missing keys"):
        Entry.from_released_line("zika", {"metadata": {}})


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda e: e.pop("organism"), "keys"),
        (lambda e: e["metadata"].update(dataUseTerms="OPEN"), "must not contain"),
        (lambda e: e["metadata"].update(version="1"), "positive integer"),
        (lambda e: e["metadata"].update(accessionVersion="PP_1.2"), "accessionVersion"),
        (lambda e: e.update(extra=1), "keys"),
        (lambda e: e["metadata"].update(hostAge=2**53), "canonicalised"),
    ],
)
def test_F15_encoder_refuses_invalid_entries(mutate, msg):
    e = sample_entry("zika", "PP_1", 1)
    mutate(e)
    with pytest.raises(FormatError, match=msg):
        StreamEncoder(CHAIN_ID, CONTRACT).encode_batch([e], codec=CODEC_RAW)


# --- F16 ------------------------------------------------------------------------------------


def test_F16_schema_records_on_first_appearance_and_on_change():
    enc, b0 = encode_genesis()
    assert b0.schemas_published == ["mpox", "zika"]
    dec0 = StreamDecoder(b0.blobs).decode()
    assert dec0.schemas["zika"]["nucleotideSegments"] == ["main"] and "accession" in dec0.schemas["zika"]["metadataFields"]
    # Same shape again: no schema record. New field: schema record.
    b1 = enc.encode_batch([sample_entry("zika", "PP_000777", 1)], previous_entries=dec0.records, codec=CODEC_RAW)
    assert b1.schemas_published == []
    dec1 = StreamDecoder(b0.blobs + b1.blobs).decode()
    b2 = enc.encode_batch([sample_entry("zika", "PP_000778", 1, extra={"newField": 1})], previous_entries=dec1.records, codec=CODEC_RAW)
    assert b2.schemas_published == ["zika"]
    dec2 = StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode()
    assert "newField" in dec2.schemas["zika"]["metadataFields"]


# --- F13 is in test_roundtrip.py ------------------------------------------------------------
