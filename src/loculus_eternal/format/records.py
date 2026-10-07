"""Record framing and the binary payloads of the container format.

Every byte in the stream belongs to a record: a minimal LEB128 length, one type byte, then
the payload. Outer records appear directly in the stream; inner records appear only inside a
batch body after it has been decoded with the batch's codec.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator

import zstandard

from loculus_eternal.format import varint

MAGIC = b"LOCULUS-ETERNAL"
MAJOR = 1
MINOR = 0

CODEC_RAW = 0
CODEC_ZSTD = 1
ZSTD_LEVEL = 19

DIGEST_BYTES = 32
ADDRESS_BYTES = 20


class FormatError(ValueError):
    """The bytes do not follow the container specification."""


class RecordType(IntEnum):
    HEADER = 0x01
    BATCH_BEGIN = 0x02
    BODY = 0x03
    BATCH_MANIFEST = 0x04
    INDEX = 0x05
    ENTRY = 0x10
    SCHEMA = 0x11
    TOOLING = 0x12
    DICTIONARY = 0x13
    REPROCESSED = 0x14
    WITHDRAW = 0x15


OUTER_TYPES = {RecordType.HEADER, RecordType.BATCH_BEGIN, RecordType.BODY, RecordType.BATCH_MANIFEST, RecordType.INDEX}
INNER_MIN, INNER_MAX = 0x10, 0x7F


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# --- framing -------------------------------------------------------------------------------


def frame(record_type: int, payload: bytes) -> bytes:
    """Build one record: the length prefix counts the type byte and the payload."""
    return varint.encode(1 + len(payload)) + bytes([record_type]) + payload


def read_record(data: bytes, offset: int) -> tuple[int, bytes, int]:
    """Read the record starting at offset. Returns (type, payload, offset after the record).

    A record that runs past the end of data is an error rather than a quiet stop, because a
    stream that ends mid-record is a torn batch and the caller must know.
    """
    try:
        length, pos = varint.decode(data, offset)
    except varint.VarintError as e:
        raise FormatError(f"bad record length at offset {offset}: {e}") from e
    if length < 1:
        raise FormatError(f"record at offset {offset} has zero length")
    end = pos + length
    if end > len(data):
        raise FormatError(f"record at offset {offset} is truncated (needs {end - len(data)} more bytes)")
    return data[pos], data[pos + 1 : end], end


def iter_records(data: bytes) -> Iterator[tuple[int, bytes]]:
    offset = 0
    while offset < len(data):
        record_type, payload, offset = read_record(data, offset)
        yield record_type, payload


# --- codecs --------------------------------------------------------------------------------


def compress(codec: int, data: bytes) -> bytes:
    if codec == CODEC_RAW:
        return data
    if codec == CODEC_ZSTD:
        return zstandard.ZstdCompressor(level=ZSTD_LEVEL, write_content_size=True).compress(data)
    raise FormatError(f"unknown codec {codec}")


def decompress(codec: int, data: bytes, expected_length: int | None = None) -> bytes:
    if codec == CODEC_RAW:
        out = data
    elif codec == CODEC_ZSTD:
        try:
            # The content size is written into the frame by our encoder, but a stream from
            # another encoder may omit it, so give the decompressor an explicit bound.
            max_output = expected_length if expected_length is not None else -1
            out = zstandard.ZstdDecompressor().decompress(data, max_output_size=max_output)
        except zstandard.ZstdError as e:
            raise FormatError(f"zstd body does not decompress: {e}") from e
    else:
        raise FormatError(f"unknown codec {codec}")
    if expected_length is not None and len(out) != expected_length:
        raise FormatError(f"decoded length {len(out)} differs from declared {expected_length}")
    return out


# --- HEADER --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Header:
    chain_id: int
    contract: bytes
    schema_id: str = "pathoplexus"
    major: int = MAJOR
    minor: int = MINOR

    def encode(self) -> bytes:
        if len(self.contract) != ADDRESS_BYTES:
            raise FormatError("contract address must be 20 bytes")
        schema = self.schema_id.encode("utf-8")
        return (
            MAGIC
            + bytes([self.major, self.minor])
            + struct.pack(">Q", self.chain_id)
            + self.contract
            + varint.encode(len(schema))
            + schema
        )

    @classmethod
    def decode(cls, payload: bytes) -> "Header":
        if payload[: len(MAGIC)] != MAGIC:
            raise FormatError("stream does not start with the Loculus Eternal magic")
        pos = len(MAGIC)
        if len(payload) < pos + 2 + 8 + ADDRESS_BYTES:
            raise FormatError("header too short")
        major, minor = payload[pos], payload[pos + 1]
        if major != MAJOR:
            raise FormatError(f"container major version {major} is not supported (this reader knows {MAJOR})")
        pos += 2
        (chain_id,) = struct.unpack(">Q", payload[pos : pos + 8])
        pos += 8
        contract = payload[pos : pos + ADDRESS_BYTES]
        pos += ADDRESS_BYTES
        n, pos = varint.decode(payload, pos)
        schema = payload[pos : pos + n]
        if len(schema) != n or pos + n != len(payload):
            raise FormatError("header schema id is malformed")
        return cls(chain_id=chain_id, contract=contract, schema_id=schema.decode("utf-8"), major=major, minor=minor)


# --- BATCH_BEGIN ---------------------------------------------------------------------------

_BATCH_BEGIN = struct.Struct(">QQ32sBQQ32s")


@dataclass(frozen=True)
class BatchBegin:
    batch: int
    first_blob_seq: int
    previous_manifest_digest: bytes
    codec: int
    uncompressed_length: int
    compressed_length: int
    body_digest: bytes

    def encode(self) -> bytes:
        return _BATCH_BEGIN.pack(
            self.batch,
            self.first_blob_seq,
            self.previous_manifest_digest,
            self.codec,
            self.uncompressed_length,
            self.compressed_length,
            self.body_digest,
        )

    @classmethod
    def decode(cls, payload: bytes) -> "BatchBegin":
        if len(payload) != _BATCH_BEGIN.size:
            raise FormatError(f"batch header must be {_BATCH_BEGIN.size} bytes, got {len(payload)}")
        return cls(*_BATCH_BEGIN.unpack(payload))


# --- TOOLING -------------------------------------------------------------------------------


def encode_tooling(path: str, content: bytes) -> bytes:
    p = path.encode("utf-8")
    return varint.encode(len(p)) + p + content


def decode_tooling(payload: bytes) -> tuple[str, bytes]:
    n, pos = varint.decode(payload, 0)
    path = payload[pos : pos + n]
    if len(path) != n:
        raise FormatError("tooling record path is truncated")
    return path.decode("utf-8"), payload[pos + n :]
