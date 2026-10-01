"""blob-archiver source: the older `GET /eth/v1/beacon/blob_sidecars/{slot}` shape.

`base/blob-archiver` and its ports write every blob to object storage and re-serve them
behind the sidecar endpoint, which returns `{"data": [{"index", "blob", "kzg_commitment",
…}]}`. The source accepts both that shape and the newer plain `blobs` shape, since some
archivers have migrated.
"""

from __future__ import annotations

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError
from loculus_eternal.sources.beacon import SECONDS_PER_SLOT


class BlobArchiverSource:
    def __init__(self, base_url: str, *, genesis_time: int, seconds_per_slot: int = SECONDS_PER_SLOT, timeout: float = 60.0, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.genesis_time = genesis_time
        self.seconds_per_slot = seconds_per_slot
        self.client = client or httpx.Client(timeout=timeout)
        self.name = f"blob-archiver({self.base_url})"

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        slot = ctx.slot(self.genesis_time, self.seconds_per_slot)
        if slot is None:
            raise SourceError("blob-archiver source needs the block timestamp to compute the slot")
        for path in (f"/eth/v1/beacon/blob_sidecars/{slot}", f"/eth/v1/beacon/blobs/{slot}"):
            try:
                r = self.client.get(self.base_url + path, headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                raise SourceError(str(e)) from e
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                raise SourceError(f"HTTP {r.status_code} from {path}")
            try:
                data = r.json()["data"]
                return [bytes.fromhex((item["blob"] if isinstance(item, dict) else item)[2:]) for item in data]
            except (ValueError, KeyError, TypeError) as e:
                raise SourceError(f"malformed response from {path}: {e}") from e
        return []
