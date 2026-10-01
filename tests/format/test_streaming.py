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
