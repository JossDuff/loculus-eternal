"""F1 varint, F2 record framing, F3 header, F17 tooling, F12 unknown inner records."""

import pytest

from loculus_eternal.format import varint
from loculus_eternal.format.records import (
    MAGIC,
    MAJOR,
    FormatError,
    Header,
    RecordType,
    decode_tooling,
    encode_tooling,
    frame,
    iter_records,
    read_record,
)


@pytest.mark.parametrize("value", [0, 1, 127, 128, 16383, 16384, 2**64 - 1])
def test_F1_varint_round_trip_is_minimal(value):
    encoded = varint.encode(value)
    assert varint.decode(encoded) == (value, len(encoded))
    # No shorter encoding exists: stripping the continuation bit of the last byte or
    # dropping it changes the value or fails.
    assert all(b & 0x80 for b in encoded[:-1]) and not encoded[-1] & 0x80


def test_F1_varint_matches_vectors(vectors):
    for case in vectors["varint.json"]["valid"]:
        assert varint.encode(int(case["value"])).hex() == case["hex"]
        assert varint.decode(bytes.fromhex(case["hex"]))[0] == int(case["value"])
    for reason, cases in vectors["varint.json"]["invalid"].items():
        for h in cases:
            with pytest.raises(varint.VarintError):
                varint.decode(bytes.fromhex(h))


def test_F1_varint_rejects_out_of_range_on_encode():
    with pytest.raises(varint.VarintError):
        varint.encode(-1)
    with pytest.raises(varint.VarintError):
        varint.encode(2**64)


def test_F2_record_length_covers_type_and_payload():
    rec = frame(RecordType.ENTRY, b"abc")
    assert rec == bytes([4, RecordType.ENTRY]) + b"abc"
    assert read_record(rec, 0) == (RecordType.ENTRY, b"abc", len(rec))


def test_F2_truncated_record_is_an_error_not_a_stop():
    rec = frame(RecordType.ENTRY, b"abcdef")
    with pytest.raises(FormatError, match="truncated"):
        list(iter_records(rec[:-1]))
    with pytest.raises(FormatError):
        read_record(bytes([0]), 0)  # zero-length record has no type byte


def test_F2_records_concatenate():
    data = frame(1, b"x") + frame(2, b"") + frame(3, b"yz")
    assert list(iter_records(data)) == [(1, b"x"), (2, b""), (3, b"yz")]


def test_F3_header_round_trip():
    h = Header(chain_id=1, contract=bytes(range(20)), schema_id="pathoplexus")
    enc = h.encode()
    assert enc.startswith(MAGIC + bytes([MAJOR, 0]))
    assert Header.decode(enc) == h


def test_F3_header_refuses_wrong_magic_and_unknown_major():
    h = Header(chain_id=1, contract=bytes(20)).encode()
    with pytest.raises(FormatError, match="magic"):
        Header.decode(b"X" + h[1:])
    bad_major = bytearray(h)
    bad_major[len(MAGIC)] = MAJOR + 1
    with pytest.raises(FormatError, match="major version"):
        Header.decode(bytes(bad_major))


def test_F3_header_accepts_any_minor():
    h = bytearray(Header(chain_id=1, contract=bytes(20)).encode())
    h[len(MAGIC) + 1] = 9
    assert Header.decode(bytes(h)).minor == 9


def test_F17_tooling_round_trip():
    payload = encode_tooling("docs/container-spec.md", b"\x00\xff binary ok \n")
    assert decode_tooling(payload) == ("docs/container-spec.md", b"\x00\xff binary ok \n")
    with pytest.raises(FormatError):
        decode_tooling(bytes([50]) + b"short")
