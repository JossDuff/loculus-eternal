"""Blockscout source: `GET {base}/api/v2/blobs/{versionedHash}`, a JSON object whose
`blob_data` field is the blob as hex.

Blockscout's public explorers (eth.blockscout.com, eth-sepolia.blockscout.com) archive every
blob on the network indefinitely and serve it by versioned hash, so no slot or block
information is needed. A blob is indexed as soon as it lands but its bytes follow a week or
two later: until then the record exists with `blob_data` null, which is "not here yet"
rather than an error, so the next source is asked.
"""

from __future__ import annotations

from loculus_eternal.sources.by_hash import HashLookupSource


class BlockscoutSource(HashLookupSource):
    kind = "blockscout"

    def url_for(self, vh: bytes) -> str:
        return f"{self.base_url}/api/v2/blobs/0x{vh.hex()}"

    def extract(self, body):
        return body["blob_data"]
