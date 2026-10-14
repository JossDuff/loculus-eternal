"""Blobscan source: `GET {base}/blobs/{versionedHash}/data`, which returns a JSON string of hex.

Blobscan is a public, donation-funded archive that stores every mainnet blob, now pinned on
IPFS. It is looked up by versioned hash, so it needs no slot or block information.
"""

from __future__ import annotations

from loculus_eternal.sources.by_hash import HashLookupSource


class BlobscanSource(HashLookupSource):
    kind = "blobscan"

    def url_for(self, vh: bytes) -> str:
        return f"{self.base_url}/blobs/0x{vh.hex()}/data"

    def extract(self, body):
        return body if isinstance(body, str) else body["data"]
