"""The container format: how the committed stream of 31-byte chunks is read as records.

The normative description is docs/container-spec.md. This package is one implementation of
it; the golden vectors in vectors/ are the shared truth that any other implementation must
reproduce.
"""

from loculus_eternal.format.records import (
    CODEC_RAW,
    CODEC_ZSTD,
    MAGIC,
    MAJOR,
    MINOR,
    RecordType,
    BatchBegin,
    Header,
)
from loculus_eternal.format.encode import Entry, StreamEncoder, EncodedBatch
from loculus_eternal.format.decode import StreamDecoder, DecodedStream, TornBatch
from loculus_eternal.format.chunks import (
    CHUNK_BYTES,
    ELEMENTS_PER_BLOB,
    BLOB_BYTES,
    BLOB_DATA_BYTES,
    pack_blobs,
    unpack_blobs,
)

__all__ = [
    "CODEC_RAW", "CODEC_ZSTD", "MAGIC", "MAJOR", "MINOR", "RecordType", "BatchBegin", "Header",
    "Entry", "StreamEncoder", "EncodedBatch", "StreamDecoder", "DecodedStream", "TornBatch",
    "CHUNK_BYTES", "ELEMENTS_PER_BLOB", "BLOB_BYTES", "BLOB_DATA_BYTES", "pack_blobs", "unpack_blobs",
]
