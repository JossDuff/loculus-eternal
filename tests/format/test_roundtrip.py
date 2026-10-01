"""F13: for any list of entries, encode to blobs and decode back yields the same entries,
the same materialised files, and a verifying manifest."""

import hashlib

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from loculus_eternal.format import canonical
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import Entry, StreamEncoder
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD

CONTRACT = bytes(20)

json_scalar = st.one_of(st.none(), st.booleans(), st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1), st.floats(allow_nan=False, allow_infinity=False, min_value=-1e15, max_value=1e15), st.text(max_size=20))
metadata_extra = st.dictionaries(st.text(min_size=1, max_size=10).filter(lambda k: k not in {"accession", "version", "accessionVersion", "versionStatus", "dataUseTerms", "dataUseTermsRestrictedUntil", "dataUseTermsUrl"}), json_scalar, max_size=5)
sequence = st.one_of(st.none(), st.text(alphabet="ACGTN-", max_size=200))


@st.composite
def entry(draw):
    organism = draw(st.sampled_from(["zika", "mpox", "andv"]))
    accession = "PP_" + draw(st.text(alphabet="0123456789ABCDEF", min_size=1, max_size=6))
    version = draw(st.integers(min_value=1, max_value=20))
    segments = ["main"] if organism != "andv" else ["L", "M", "S"]
    genes = draw(st.lists(st.sampled_from(["E", "NS1", "GPC", "N"]), unique=True, max_size=3))
    metadata = {"accession": accession, "version": version, "accessionVersion": f"{accession}.{version}", **draw(metadata_extra)}
    return {
        "organism": organism,
        "metadata": metadata,
        "unalignedNucleotideSequences": {s: draw(sequence) for s in segments},
        "alignedNucleotideSequences": {s: draw(sequence) for s in segments},
        "nucleotideInsertions": {s: draw(st.lists(st.text(max_size=8), max_size=3)) for s in segments},
        "alignedAminoAcidSequences": {g: draw(sequence) for g in genes},
        "aminoAcidInsertions": {g: [] for g in genes},
    }


def _dedupe(entries):
    seen = {}
    for e in entries:
        seen.setdefault(Entry.key(e), e)
    return list(seen.values())


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(first=st.lists(entry(), min_size=1, max_size=12), second=st.lists(entry(), min_size=1, max_size=8), codec=st.sampled_from([CODEC_RAW, CODEC_ZSTD]))
def test_F13_encode_decode_round_trip(first, second, codec):
    first = _dedupe(first)
    first_keys = {Entry.key(e) for e in first}
    second = [e for e in _dedupe(second) if Entry.key(e) not in first_keys]

    enc = StreamEncoder(chain_id=1, contract=CONTRACT)
    b0 = enc.encode_batch(first, codec=codec)
    dec0 = StreamDecoder(b0.blobs).decode()
    assert not dec0.torn and all(dec0.verify_artifacts().values())
    assert {o: set(items) for o, items in dec0.entries.items()} == _expected_keys(first)

    if second:
        b1 = enc.encode_batch(second, previous_entries=dec0.payloads, codec=codec)
        dec = StreamDecoder(b0.blobs + b1.blobs).decode()
        assert not dec.torn and all(dec.verify_artifacts().values())
        assert {o: set(items) for o, items in dec.entries.items()} == _expected_keys(first + second)
        # Every decoded payload is the canonical form of the input entry.
        for e in first + second:
            org, acc, ver = Entry.key(e)
            assert dec.entries[org][(acc, ver)][0] == canonical.dumps(e)
        # Resuming an encoder from the decoded stream reproduces the encoder's own state.
        resumed = dec.encoder_state()
        assert resumed.next_batch == 2 and resumed.next_blob_seq == b1.blob_count_after
        assert resumed.previous_manifest_digest == b1.manifest_digest
        assert resumed.published == enc.state.published
        for org in dec.entries:
            assert hashlib.sha256(dec.materialise(org)).hexdigest() == b1.manifest["organisms"][org]["artifactSha256"]


def _expected_keys(entries):
    out = {}
    for e in entries:
        org, acc, ver = Entry.key(e)
        out.setdefault(org, set()).add((acc, ver))
    return out
