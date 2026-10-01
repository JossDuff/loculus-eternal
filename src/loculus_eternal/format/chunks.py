"""Packing the stream into blobs and back.

A blob is 4096 field elements of 32 bytes. Each element is one zero byte followed by 31
stream bytes, so that the element's value stays below the BLS12-381 scalar field modulus.
Chunk i of the stream is element i mod 4096 of blob i div 4096.
"""

import math

CHUNK_BYTES = 31
ELEMENT_BYTES = 32
ELEMENTS_PER_BLOB = 4096
BLOB_BYTES = ELEMENT_BYTES * ELEMENTS_PER_BLOB          # 131,072
BLOB_DATA_BYTES = CHUNK_BYTES * ELEMENTS_PER_BLOB       # 126,976


class ChunkError(ValueError):
    pass


def blobs_needed(stream_length: int) -> int:
    return max(1, math.ceil(stream_length / BLOB_DATA_BYTES))


def last_blob_chunk_count(stream_length: int) -> int:
    """How many elements of the final blob carry data, as the contract records per publish."""
    if stream_length == 0:
        raise ChunkError("an empty stream has no last blob")
    remainder = stream_length - (blobs_needed(stream_length) - 1) * BLOB_DATA_BYTES
    return math.ceil(remainder / CHUNK_BYTES)


def pack_blobs(stream: bytes) -> list[bytes]:
    """Pack stream bytes into fully formed blobs; the last blob is zero past the data."""
    if not stream:
        raise ChunkError("refusing to pack an empty stream")
    blobs = []
    for start in range(0, len(stream), BLOB_DATA_BYTES):
        data = stream[start : start + BLOB_DATA_BYTES]
        blob = bytearray(BLOB_BYTES)
        for i in range(0, len(data), CHUNK_BYTES):
            chunk = data[i : i + CHUNK_BYTES]
            element = i // CHUNK_BYTES
            blob[element * ELEMENT_BYTES + 1 : element * ELEMENT_BYTES + 1 + len(chunk)] = chunk
        blobs.append(bytes(blob))
    return blobs


def unpack_blob(blob: bytes) -> bytes:
    """Return the 126,976 stream bytes of one blob, checking every element's high byte is zero."""
    if len(blob) != BLOB_BYTES:
        raise ChunkError(f"blob must be {BLOB_BYTES} bytes, got {len(blob)}")
    out = bytearray(BLOB_DATA_BYTES)
    for element in range(ELEMENTS_PER_BLOB):
        base = element * ELEMENT_BYTES
        if blob[base] != 0:
            raise ChunkError(f"element {element} has a non-zero high byte")
        out[element * CHUNK_BYTES : (element + 1) * CHUNK_BYTES] = blob[base + 1 : base + ELEMENT_BYTES]
    return bytes(out)


def unpack_blobs(blobs: list[bytes]) -> bytes:
    """Concatenate the stream bytes of blobs in order, including any zero padding."""
    return b"".join(unpack_blob(b) for b in blobs)
