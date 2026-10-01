"""Unsigned LEB128 integers, as used for every record length prefix in the stream.

Seven bits per byte, lowest group first, high bit set on every byte but the last. Encoders
produce the shortest form; decoders reject anything else, because a stream that could be
written two ways is a stream whose digests could disagree.
"""

MAX_BYTES = 10
MAX_VALUE = 2**64 - 1


class VarintError(ValueError):
    pass


def encode(value: int) -> bytes:
    if value < 0 or value > MAX_VALUE:
        raise VarintError(f"value out of range for uvarint: {value}")
    out = bytearray()
    while True:
        group = value & 0x7F
        value >>= 7
        if value:
            out.append(group | 0x80)
        else:
            out.append(group)
            return bytes(out)


def decode(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Return (value, new_offset). Rejects non-minimal, overlong and out-of-range encodings."""
    value = 0
    shift = 0
    pos = offset
    while True:
        if pos >= len(data):
            raise VarintError("truncated uvarint")
        if pos - offset >= MAX_BYTES:
            raise VarintError("uvarint longer than ten bytes")
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            break
        shift += 7
    # A final zero group after at least one byte means the number was padded; that is the
    # redundant encoding the spec forbids. The single byte 0x00 is the legitimate zero.
    if pos - offset > 1 and byte == 0:
        raise VarintError("non-minimal uvarint (redundant trailing zero group)")
    if value > MAX_VALUE:
        raise VarintError("uvarint value above 2^64 - 1")
    return value, pos
