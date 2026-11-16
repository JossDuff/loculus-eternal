"""Withdrawal records: the bytes stay, the output and digests leave them out. Reinstatement
records: the entry returns to the output without being published again."""

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
    # The entry withdrawn in advance cannot be published while the withdrawal stands.
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


# --- reinstatement ----------------------------------------------------------------------------


def withdrawn_once(avs=("PP_000001.1",)):
    """Genesis, then a batch that withdraws the named zika entries."""
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        original = dec0.materialise("zika")
        b1 = enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": list(avs), "note": "gone"}])
    return enc, b0, b1, original


def test_F21_reinstated_entry_returns_to_the_output_without_being_published_again():
    enc, b0, b1, original = withdrawn_once()
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        assert b"PP_000001.1" not in dec1.materialise("zika")
        b2 = enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"], "note": "back"}], force_index=True)
    assert b2.entry_count == 0 and b2.reinstated_count == 1 and b2.withdrawn_count == 0
    # The output is byte for byte what it was before the withdrawal, and the batch that
    # brings the entry back carries a record naming it, not the entry.
    assert b2.manifest["organisms"]["zika"] == {"entriesInBatch": 0, "entriesTotal": 3, "withdrawnInBatch": 0, "withdrawnTotal": 0, "artifactSha256": hashlib.sha256(original).hexdigest()}
    assert b2.index["withdrawn"] == {} and len(b2.stream.rstrip(b"\x00")) < 2000
    with StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert dec.materialise("zika") == original
        assert dec.has("zika", "PP_000001", 1) and not dec.is_withdrawn("zika", "PP_000001", 1)
        assert dec.count("zika") == 3 and dec.withdrawn() == {} and dec.batches[2].reinstated_count == 1
        assert dec.payload("zika", "PP_000001", 1) is not None
        # An encoder that starts from the decoded stream stands where the running one does.
        state = dec.encoder_state()
        assert state.withdrawn == enc.state.withdrawn == {} and state.entries_total == enc.state.entries_total
        fresh = StreamEncoder(CHAIN_ID, CONTRACT, state=state).encode_batch([sample_entry("zika", "PP_000011", 1)], previous_entries=dec.records, codec=CODEC_RAW)
        running = enc.encode_batch([sample_entry("zika", "PP_000011", 1)], previous_entries=dec.records, codec=CODEC_RAW)
        assert fresh.stream == running.stream


def test_F21_the_most_recent_batch_naming_an_entry_decides():
    enc, b0, b1, original = withdrawn_once()
    blobs = b0.blobs + b1.blobs
    expected = []
    for step in ("reinstatements", "withdrawals", "reinstatements"):
        with StreamDecoder(blobs).decode() as dec:
            b = enc.encode_batch([], previous_entries=dec.records, codec=CODEC_RAW, **{step: [{"organism": "zika", "accessionVersions": ["PP_000001.1"]}]})
        blobs = blobs + b.blobs
        expected.append(step == "withdrawals")
        with StreamDecoder(blobs).decode() as dec:
            assert not dec.torn and all(dec.verify_artifacts().values())
            assert dec.is_withdrawn("zika", "PP_000001", 1) is expected[-1]
            assert (b"PP_000001.1" in dec.materialise("zika")) is not expected[-1]
    assert expected == [False, True, False]
    with StreamDecoder(blobs).decode() as dec:
        assert dec.materialise("zika") == original and [b.withdrawn_count for b in dec.batches] == [0, 1, 0, 1, 0] and [b.reinstated_count for b in dec.batches] == [0, 0, 1, 0, 1]


def test_F21_one_batch_may_withdraw_some_entries_and_reinstate_others():
    enc, b0, b1, _ = withdrawn_once(("PP_000001.1", "PP_000002.1"))
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        assert dec1.count("zika") == 1
        b2 = enc.encode_batch(
            [sample_entry("zika", "PP_000010", 1)],
            previous_entries=dec1.records,
            codec=CODEC_ZSTD,
            withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.2"]}],
            reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000002.1"]}],
            force_index=True,
        )
    assert b2.manifest["organisms"]["zika"]["entriesTotal"] == 2 and b2.manifest["organisms"]["zika"]["withdrawnInBatch"] == 1 and b2.manifest["organisms"]["zika"]["withdrawnTotal"] == 2
    assert b2.index["withdrawn"] == {"zika": {"PP_000001": [1, 2]}}
    with StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert [canonical.loads(p)["metadata"]["accessionVersion"] for p in dec.payloads("zika")] == ["PP_000002.1", "PP_000010.1"]


def test_F21_entry_withdrawn_before_it_was_published_can_be_published_once_reinstated():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        b1 = enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "mpox", "accessionVersions": ["PP_000006.1"]}])
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        with pytest.raises(FormatError, match="withdrawn and cannot be published"):
            enc.encode_batch([sample_entry("mpox", "PP_000006", 1)], previous_entries=dec1.records, codec=CODEC_RAW)
        # The reinstatement and the entry may travel in the same batch.
        b2 = enc.encode_batch([sample_entry("mpox", "PP_000006", 1)], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "mpox", "accessionVersions": ["PP_000006.1"]}])
    with StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert dec.has("mpox", "PP_000006", 1) and not dec.is_withdrawn("mpox", "PP_000006", 1) and dec.count("mpox") == 2


def test_F21_nothing_earlier_is_needed_to_publish_an_entry_withdrawn_in_advance():
    # A stream whose only batch so far withdraws an entry that was never published: the batch
    # that reinstates and publishes it has no earlier entries to read.
    enc = StreamEncoder(CHAIN_ID, CONTRACT)
    b0 = enc.encode_batch([], codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
    b1 = enc.encode_batch([sample_entry("zika", "PP_000001", 1)], codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values()) and dec.count("zika") == 1
        assert [b.withdrawn_count for b in dec.batches] == [1, 0] and [b.reinstated_count for b in dec.batches] == [0, 1]
    # But an entry that is in the stream cannot be brought back without reading it.
    enc2, g0, w1, _ = withdrawn_once()
    with pytest.raises(FormatError, match="previous entries of .* are needed"):
        enc2.encode_batch([], codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])


def test_F21_an_organism_emptied_by_withdrawal_comes_back_whole():
    enc, b0 = genesis()
    with StreamDecoder(b0.blobs).decode() as dec0:
        original = dec0.materialise("mpox")
        b1 = enc.encode_batch([], previous_entries=dec0.records, codec=CODEC_RAW, withdrawals=[{"organism": "mpox", "accessionVersions": ["PP_000003.1"]}])
    assert b1.manifest["organisms"]["mpox"]["entriesTotal"] == 0
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        b2 = enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "mpox", "accessionVersions": ["PP_000003.1"]}])
    with StreamDecoder(b0.blobs + b1.blobs + b2.blobs).decode() as dec:
        assert not dec.torn and all(dec.verify_artifacts().values()) and dec.materialise("mpox") == original


def test_F21_encoder_refuses_bad_reinstatements():
    enc, b0, b1, _ = withdrawn_once()
    with StreamDecoder(b0.blobs + b1.blobs).decode() as dec1:
        for bad in ({"organism": "zika", "accessionVersions": []}, {"organism": "", "accessionVersions": ["PP_000001.1"]}, {"organism": "zika", "accessionVersions": ["PP_000001"]}, {"organism": "zika", "accessionVersions": ["PP_000001.1"], "note": 5}):
            with pytest.raises(FormatError, match="reinstatement"):
                enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[bad])
        # Only what is withdrawn can be reinstated: not a published entry, not an unknown one.
        for av in ("PP_000002.1", "PP_000777.1"):
            with pytest.raises(FormatError, match="not withdrawn"):
                enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": [av]}])
        # The same entry cannot be withdrawn and reinstated by one batch, whichever it is now.
        with pytest.raises(FormatError, match="already withdrawn"):
            enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}], reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
        with pytest.raises(FormatError, match="not withdrawn"):
            enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, withdrawals=[{"organism": "zika", "accessionVersions": ["PP_000002.1"]}], reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000002.1"]}])
        with pytest.raises(FormatError, match="more than one reinstatement"):
            enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}, {"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
        # A refused batch leaves the encoder where it was.
        b2 = enc.encode_batch([], previous_entries=dec1.records, codec=CODEC_RAW, reinstatements=[{"organism": "zika", "accessionVersions": ["PP_000001.1"]}])
    assert b2.batch == 2 and b2.reinstated_count == 1


def test_F21_vector(vectors):
    v = vectors["reinstatement.json"]
    i = v["input"]
    enc = StreamEncoder(i["chainId"], bytes.fromhex(i["contract"]))
    blobs: list[bytes] = []
    for n, spec in enumerate(i["batches"]):
        if blobs:
            with StreamDecoder(blobs).decode() as dec:
                b = enc.encode_batch(spec["entries"], previous_entries=dec.records, codec=CODEC_RAW, withdrawals=spec["withdrawals"], reinstatements=spec["reinstatements"], force_index=spec["forceIndex"])
        else:
            b = enc.encode_batch(spec["entries"], codec=CODEC_RAW, force_index=spec["forceIndex"])
        assert b.stream.rstrip(b"\x00").hex() == v["expected"][n]["streamHex"]
        assert b.manifest == v["expected"][n]["manifest"]
        blobs = blobs + b.blobs
        with StreamDecoder(blobs).decode() as dec:
            assert not dec.torn and dec.withdrawn() == v["withdrawnAfterEachBatch"][n]
    with StreamDecoder(blobs).decode() as dec:
        assert dec.published() == v["published"]
        for org, a in v["artifacts"].items():
            assert dec.materialise(org).decode("utf-8") == a["ndjson"]
            assert hashlib.sha256(dec.materialise(org)).hexdigest() == a["sha256"]
