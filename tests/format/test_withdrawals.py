"""Withdrawal records: the bytes stay, the output and digests leave them out."""

import hashlib

import pytest

from loculus_eternal.format import canonical
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import EmptyBatch, StreamEncoder
from loculus_eternal.format.gen_vectors import CHAIN_ID, CONTRACT, genesis_entries, sample_entry
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD, FormatError


def genesis():
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b0 = enc.encode_batch(genesis_entries(), codec=CODEC_RAW)
    return enc, b0


def test_F19_withdrawn_entry_leaves_the_output_but_stays_in_the_stream():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        before = dec0.materialise("zika")
        b1 = enc.encode_batch([sample_entry("zika", "PP_000010", 1)], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.1"], "note": "test"}])
    assert b1.withdrawn_count == 1 and b1.manifest["organisms"]["zika"] == {"entriesInBatch": 1, "entriesTotal": 3, "withdrawnInBatch": 1, "withdrawnTotal": 1, "artifactSha256": b1.manifest["organisms"]["zika"]["artifactSha256"]}
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert dec.is_withdrawn("zika", "PP_000001", 1) and dec.has("zika", "PP_000001", 1)
        assert dec.count("zika") == 3 and dec.batches[1].withdrawn_count == 1
        after = dec.materialise("zika")
        assert b"PP_000001.1" not in after and b"PP_000001.1" in before
        # The decoded stream knows the entry exists and is withdrawn, and none of its
        # payload-producing surface yields it.
        assert dec.payload("zika", "PP_000001", 1) is None
        assert all(canonical.loads(p)["metadata"]["accessionVersion"] != "PP_000001.1" for p in dec.payloads("zika"))
        assert all(a != "PP_000001" or v != 1 for a, v, _ in dec.records("zika"))
        assert b"PP_000001.1" not in dec.materialise("zika")
        import io

        buf = io.BytesIO()
        dec.materialise_to("zika", buf)
        assert b"PP_000001.1" not in buf.getvalue()
        assert hashlib.sha256(after).hexdigest() == b1.manifest["organisms"]["zika"]["artifactSha256"]
        assert dec.withdrawn() == {"zika": {"PP_000001": [1]}}
        state = dec.encoder_state()
        assert state.withdrawn == {"zika": {"PP_000001": [1]}} and state.entries_total["zika"] == 3


def test_F19_withdrawal_before_the_entry_also_applies():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        b1 = enc.encode_batch([sample_entry("mpox", "PP_000005", 1)], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "mpox", "accessionVersions": ["PP_000006.1"], "note": None}])
    # The entry withdrawn in advance can never be published.
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        with pytest.raises(FormatError, match="withdrawn"):
            enc.encode_batch([sample_entry("mpox", "PP_000006", 1)], previous_entries=dec1.records, codec=CODEC_RAW)
        assert dec1.is_withdrawn("mpox", "PP_000006", 1) and not dec1.has("mpox", "PP_000006", 1)


def test_F19_withdrawals_alone_make_a_batch_and_the_index_lists_them():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        b1 = enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_ZSTD, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.2", "PP_000002.1"], "note": "two at once"}], force_index=True)
    assert b1.entry_count == 0 and b1.withdrawn_count == 2
    assert b1.index["withdrawn"] == {"zika": {"PP_000001": [2], "PP_000002": [1]}}
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec:
        assert not dec.torn and dec.count("zika") == 1 and all(dec.verify_artifacts().values())
    with pytest.raises(EmptyBatch):
        enc.encode_batch([], previous_entries=lambda o: [], codec=CODEC_RAW)


def test_F19_encoder_refuses_bad_or_repeated_withdrawals():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        for bad in ({"organism": "zika", "accessionVersions": []}, {"organism": "", "accessionVersions": ["PP_1.1"]}, {"organism": "zika", "accessionVersions": ["PP_1"]}, {"organism": "zika", "accessionVersions": ["PP_1.0"]}, {"organism": "zika", "accessionVersions": ["PP_000001.1"], "note": 5}):
            with pytest.raises(FormatError):
                enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[bad])
        b1 = enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        with pytest.raises(FormatError, match="already withdrawn"):
            enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
        # Republishing it is refused either way: it is already published, and withdrawn.
        with pytest.raises(FormatError, match="already published|withdrawn"):
            enc.encode_batch([sample_entry("zika", "PP_000001", 1)], previous_entries=dec1.records, codec=CODEC_RAW)
        with pytest.raises(FormatError, match="withdrawn"):
            enc.encode_batch([sample_entry("zika", "PP_000099", 1)], previous_entries=dec1.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000099.1"]}])


def test_F19_vector(vectors):
    v = vectors["withdrawal.json"]
    i = v["input"]
    enc = StreamEncoder(i["chainId"], bytes.fromhex(i["contract"]))
    b0 = enc.encode_batch(i["batches"][0]["entries"], codec=CODEC_RAW)
    with StreamDecoder(b0.blobs).decode() as dec0:
        b1 = enc.encode_batch(i["batches"][1]["entries"], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=i["batches"][1]["withdrawals"], force_index=True)
    assert b1.stream.rstrip(b"\x00").hex() == v["expected"][1]["streamHex"]
    assert b1.manifest == v["expected"][1]["manifest"]
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec:
        assert dec.withdrawn() == v["withdrawn"] and dec.published() == v["published"]
        for org, a in v["artifacts"].items():
            assert dec.materialise(org).decode("utf-8") == a["ndjson"]
