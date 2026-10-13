"""Blockscout source: `GET {base}/api/v2/blobs/{versionedHash}`, a JSON object whose
`blob_data` field is the blob as hex.

Blockscout's public explorers (eth.blockscout.com, eth-sepolia.blockscout.com) archive every
blob on the network indefinitely and serve it by versioned hash, so no slot or block
information is needed. A blob is indexed as soon as it lands but its bytes follow a week or
two later: until then the record exists with `blob_data` null, which this source treats as
"not here yet" rather than as an error, so the next source is asked.
"""

from __future__ import annotations

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError

PUBLIC_API = "https://eth.blockscout.com"


class BlockscoutSource:
    def __init__(self, base_url: str = PUBLIC_API, *, timeout: float = 60.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.name = f"blockscout({self.base_url})"

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        errors: list[str] = []
        for vh in wanted:
            try:
                r = self.client.get(f"{self.base_url}/api/v2/blobs/0x{vh.hex()}", headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                errors.append(f"0x{vh.hex()[:10]}…: {e}")
                continue
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                errors.append(f"0x{vh.hex()[:10]}…: HTTP {r.status_code}")
                continue
            try:
                hexdata = r.json()["blob_data"]
                if hexdata is None:
                    continue    # indexed, bytes not archived yet
                out.append(bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                errors.append(f"0x{vh.hex()[:10]}…: malformed response ({e})")
        if not out and errors and len(errors) == len(wanted):
            raise SourceError("; ".join(errors))
        return out
