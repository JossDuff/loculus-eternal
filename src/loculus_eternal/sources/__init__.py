"""Blob sources: anywhere blob bytes might be obtained from, behind one interface.

Every source is untrusted. It may return nothing, a subset, bytes for a different blob, or
garbage. The only thing a source can be asked is "give me the blobs with these versioned
hashes that landed in this block", and the only thing done with its answer is to recompute
each candidate's versioned hash and keep the ones that match. Withholding is the one failure
that cannot be detected, which is why there is a list of sources and not one.
"""

from loculus_eternal.sources.base import BlobContext, BlobSource, SourceChain, SourceError, verify_candidates
from loculus_eternal.sources.beacon import BeaconSource
from loculus_eternal.sources.blobscan import BlobscanSource
from loculus_eternal.sources.blob_archiver import BlobArchiverSource
from loculus_eternal.sources.local import LocalDirectorySource

__all__ = [
    "BlobContext", "BlobSource", "SourceChain", "SourceError", "verify_candidates",
    "BeaconSource", "BlobscanSource", "BlobArchiverSource", "LocalDirectorySource",
]
