"""Bounded memory: results do not depend on how often entries spill to disk."""

import hashlib
import io
from pathlib import Path

from loculus_eternal.format import canonical
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import Entry, StreamEncoder
from loculus_eternal.format.gen_vectors import CHAIN_ID, CONTRACT, sample_entry, tooling
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD, BatchBegin, Header, RecordType, frame, sha256
from loculus_eternal.format.runs import ExternalSorter, iter_run, merge_runs


def many_entries(n: int):
    # Deliberately out of order, across two organisms, with several versions per accession.
    for i in range(n, 0, -1):
        org = "zika" if i % 3 else "mpox"
        yield sample_entry(org, f"PP_{(i * 7919) % 1000:06d}", 1 + i % 4, seq_len=200 + (i % 50))


def dedupe(entries):
    seen = {}
    for e in entries:
        seen.setdefault(Entry.key(e), e)
    return list(seen.values())


def test_F18_tiny_sort_buffer_gives_identical_stream_and_files(tmp_path):
    entries = dedupe(many_entries(400))
    big = StreamEncoder(CHAIN_ID, CONTRACT).encode_batch(iter(entries), tooling=tooling(), codec=CODEC_RAW)
    small = StreamEncoder(CHAIN_ID, CONTRACT, sort_buffer_bytes=1).encode_batch(iter(entries), tooling=tooling(), codec=CODEC_RAW)
    assert small.stream == big.stream and small.manifest == big.manifest
    assert small.entry_count == len(entries)

    with StreamDecoder(big.blobs).decode() as roomy, StreamDecoder(small.blobs, spill_dir=tmp_path / "spill", sort_buffer_bytes=1).decode() as tight:
        assert not roomy.torn and not tight.torn
        assert roomy.artifact_digests() == tight.artifact_digests()
        assert all(tight.verify_artifacts().values())
        for org in tight.organisms():
            assert tight.materialise(org) == roomy.materialise(org)
            buf = io.BytesIO()
            digest = tight.materialise_to(org, buf)
            assert buf.getvalue() == roomy.materialise(org) and digest == hashlib.sha256(buf.getvalue()).hexdigest()
        # The spill files are the only place the entries live, and they are inside spill_dir.
        assert any((tmp_path / "spill").rglob("*.run"))


def test_F18_second_batch_digests_via_streamed_previous_entries(tmp_path):
    first = dedupe(many_entries(150))
    keys = {Entry.key(e) for e in first}
    second = [e for e in dedupe(many_entries(260)) if Entry.key(e) not in keys]
    enc = StreamEncoder(CHAIN_ID, CONTRACT, sort_buffer_bytes=1)
    b0 = enc.encode_batch(iter(first), codec=CODEC_ZSTD)
    with StreamDecoder(b0.blobs, sort_buffer_bytes=1).decode() as dec0:
        b1 = enc.encode_batch(iter(second), previous_entries=dec0.records, codec=CODEC_ZSTD)
    with StreamDecoder(b0.blobs + b1.blobs, sort_buffer_bytes=1).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert {o: dec.count(o) for o in dec.organisms()} == {o: b1.manifest["organisms"][o]["entriesTotal"] for o in b1.manifest["organisms"]}
        # The iterator yields keys alongside payloads, sorted, without parsing JSON.
        for org in dec.organisms():
            recs = list(dec.records(org))
            assert [(a, v) for a, v, _ in recs] == sorted((a, v) for a, v, _ in recs)
            assert all(canonical.loads(p)["metadata"]["accessionVersion"] == f"{a}.{v}" for a, v, p in recs)
        resumed = dec.encoder_state()
        assert resumed.entries_total == {o: dec.count(o) for o in dec.organisms()}


def test_F18_out_of_order_body_still_materialises_sorted():
    """A body that another encoder wrote unsorted decodes to the same sorted file."""
    entries = [sample_entry("zika", "PP_000003", 1), sample_entry("zika", "PP_000001", 2), sample_entry("zika", "PP_000001", 1)]
    inner = b"".join(frame(RecordType.ENTRY, Entry.payload(e)) for e in entries)  # as given: unsorted
    lines = b"".join(p + b"\n" for p in sorted((Entry.payload(e) for e in entries), key=lambda p: (canonical.loads(p)["metadata"]["accession"], canonical.loads(p)["metadata"]["version"])))
    begin = BatchBegin(0, 0, b"\x00" * 32, CODEC_RAW, len(inner), len(inner), sha256(inner))
    manifest = {"batch": 0, "firstBlobSeq": 0, "blobCountAfter": 1, "previousManifestDigest": "00" * 32, "bodyDigest": begin.body_digest.hex(), "hasIndex": False, "organisms": {"zika": {"entriesInBatch": 3, "entriesTotal": 3, "artifactSha256": hashlib.sha256(lines).hexdigest()}}}
    stream = frame(RecordType.HEADER, Header(CHAIN_ID, CONTRACT).encode()) + frame(RecordType.BATCH_BEGIN, begin.encode()) + frame(RecordType.BODY, inner) + frame(RecordType.BATCH_MANIFEST, canonical.dumps(manifest))
    from loculus_eternal.format.chunks import BLOB_DATA_BYTES, pack_blobs

    blobs = pack_blobs(stream + b"\x00" * (BLOB_DATA_BYTES - len(stream)))
    with StreamDecoder(blobs, sort_buffer_bytes=1).decode() as dec:
        assert not dec.torn and dec.materialise("zika") == lines and all(dec.verify_artifacts().values())


def test_F18_external_sorter_and_merge(tmp_path):
    sorter = ExternalSorter(tmp_path / "work", buffer_bytes=64)
    records = [(f"A{i % 25:02d}", 1 + i // 25, f"p{i}".encode()) for i in range(50)]  # fifty distinct keys, unsorted
    unique = {}
    for a, v, p in records:
        unique.setdefault((a, v), p)
    for a, v, p in records:
        sorter.add(a, v, p)
    out = tmp_path / "sorted.run"
    assert sorter.finish(out) == 50
    got = list(iter_run(out))
    assert [(a, v) for a, v, _ in got] == sorted((a, v) for a, v, _ in got)
    assert len(got) == 50 and not list((tmp_path / "work").glob("chunk-*"))
    # Merging two runs: the earlier batch wins a duplicate key and the loser is reported.
    other = tmp_path / "other.run"
    s2 = ExternalSorter(tmp_path / "work2", buffer_bytes=64)
    s2.add("A00", 1, b"later")
    s2.add("Z9", 9, b"new")
    s2.finish(other)
    dropped = []
    merged = list(merge_runs([(0, out), (1, other)], on_duplicate=lambda k, kept, lost: dropped.append((k, kept, lost))))
    keys = [(a, v) for _, (a, v, _) in merged]
    assert keys == sorted(set(keys)) and ("Z9", 9) in keys
    assert dropped == [(("A00", 1), 0, 1)]
    assert [p for b, (a, v, p) in merged if (a, v) == ("A00", 1)] == [b"p0"]


# --- hostile streams: the decoder must tear the batch, never crash or escape its directory ---


def _handmade(inner: bytes, manifest_organisms: dict, uncompressed_length: int | None = None) -> list[bytes]:
    from loculus_eternal.format.chunks import BLOB_DATA_BYTES, pack_blobs

    begin = BatchBegin(0, 0, b"\x00" * 32, CODEC_RAW, uncompressed_length if uncompressed_length is not None else len(inner), len(inner), sha256(inner))
    manifest = {"batch": 0, "firstBlobSeq": 0, "blobCountAfter": 1, "previousManifestDigest": "00" * 32, "bodyDigest": begin.body_digest.hex(), "hasIndex": False, "organisms": manifest_organisms}
    stream = frame(RecordType.HEADER, Header(CHAIN_ID, CONTRACT).encode()) + frame(RecordType.BATCH_BEGIN, begin.encode()) + frame(RecordType.BODY, inner) + frame(RecordType.BATCH_MANIFEST, canonical.dumps(manifest))
    return pack_blobs(stream + b"\x00" * (BLOB_DATA_BYTES - len(stream)))


def _organism_entry(organism: str) -> dict:
    e = sample_entry("zika", "PP_000001", 1)
    e["organism"] = organism
    return e


def test_F18_hostile_organism_name_cannot_escape_the_spill_directory(tmp_path):
    e = _organism_entry("../../escaped")
    payload = Entry.payload(e)
    line = payload + b"\n"
    blobs = _handmade(frame(RecordType.ENTRY, payload), {"../../escaped": {"entriesInBatch": 1, "entriesTotal": 1, "artifactSha256": hashlib.sha256(line).hexdigest()}})
    spill = tmp_path / "a" / "b" / "spill"
    with StreamDecoder(blobs, spill_dir=spill).decode() as dec:
        assert not dec.torn and dec.organisms() == ["../../escaped"]
        assert not (tmp_path / "escaped").exists() and not (tmp_path / "a" / "escaped").exists()
        assert all(spill in p.parents for p in spill.rglob("*"))
    assert not (spill / "entries").exists(), "closing removes the spilled entries"


def test_F18_hostile_length_prefix_tears_the_batch_without_allocating(tmp_path):
    from loculus_eternal.format import varint

    inner = varint.encode(2**40) + bytes([RecordType.ENTRY]) + b"x" * 100
    blobs = _handmade(inner, {})
    with StreamDecoder(blobs).decode() as dec:
        assert not dec.batches and "remain of the declared length" in dec.torn[0].reason or "body does not decode" in dec.torn[0].reason


def test_F18_non_object_payloads_tear_the_batch(tmp_path):
    for inner in (frame(RecordType.ENTRY, b"5"), frame(RecordType.SCHEMA, b"[]"), frame(RecordType.ENTRY, b'{"organism":1}')):
        blobs = _handmade(inner, {})
        with StreamDecoder(blobs).decode() as dec:
            assert not dec.batches and dec.torn, inner


def test_F18_a_torn_body_leaves_no_spilled_chunks(tmp_path):
    good = Entry.payload(sample_entry("zika", "PP_000001", 1))
    inner = frame(RecordType.ENTRY, good) + frame(RecordType.BATCH_BEGIN, b"x")
    blobs = _handmade(inner, {})
    spill = tmp_path / "spill"
    with StreamDecoder(blobs, spill_dir=spill, sort_buffer_bytes=1).decode() as dec:
        assert not dec.batches and "outer record type" in dec.torn[0].reason
        assert not list(spill.rglob("*.run")) and not list(spill.rglob("chunk-*"))


def test_F18_entry_count_excludes_duplicates_the_store_ignored():
    e1 = sample_entry("zika", "PP_000001", 1)
    e2 = dict(e1, metadata=dict(e1["metadata"], hostAge=99))
    p1, p2 = Entry.payload(e1), Entry.payload(e2)
    blobs = _handmade(frame(RecordType.ENTRY, p1) + frame(RecordType.ENTRY, p2), {"zika": {"entriesInBatch": 1, "entriesTotal": 1, "artifactSha256": hashlib.sha256(p1 + b"\n").hexdigest()}})
    with StreamDecoder(blobs).decode() as dec:
        assert not dec.torn and dec.count("zika") == 1 and dec.batches[0].entry_count == 1
        assert any("duplicate" in w for w in dec.warnings)


def test_F18_encoder_creates_its_work_directory(tmp_path):
    enc = StreamEncoder(CHAIN_ID, CONTRACT, work_dir=tmp_path / "fresh" / "deep")
    b = enc.encode_batch([sample_entry("zika", "PP_000001", 1)], codec=CODEC_RAW)
    assert b.entry_count == 1 and (tmp_path / "fresh" / "deep").exists()
