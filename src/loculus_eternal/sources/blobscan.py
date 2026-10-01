"""Blobscan source: `GET {base}/blobs/{versionedHash}/data`, which returns a JSON string of hex.

Blobscan is a public, donation-funded archive that stores every blob on the network. It is
looked up by versioned hash, so it needs no slot or block information.
"""

from __future__ import annotations

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError

PUBLIC_API = "https://api.blobscan.com"


class BlobscanSource:
    def __init__(self, base_url: str = PUBLIC_API, *, timeout: float = 60.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.name = f"blobscan({self.base_url})"

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        errors: list[str] = []
        for vh in wanted:
            try:
                r = self.client.get(f"{self.base_url}/blobs/0x{vh.hex()}/data")
            except httpx.HTTPError as e:
                errors.append(f"0x{vh.hex()[:10]}…: {e}")
                continue
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                errors.append(f"0x{vh.hex()[:10]}…: HTTP {r.status_code}")
                continue
            try:
                body = r.json()
                hexdata = body if isinstance(body, str) else body["data"]
                out.append(bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                errors.append(f"0x{vh.hex()[:10]}…: malformed response ({e})")
        if not out and errors and len(errors) == len(wanted):
            raise SourceError("; ".join(errors))
        return out
