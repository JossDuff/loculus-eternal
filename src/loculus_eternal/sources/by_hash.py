"""The shape shared by archives that are looked up by versioned hash alone.

Blobscan and Blockscout both answer one HTTP request per blob, keyed by the versioned hash,
so they need no slot or block information. What differs between them is the URL and where
the hex sits in the JSON; everything else lives here once: the loop, how an absent blob is
told from a failing server, and the pacing that public explorers demand.

Public explorers meter clients and answer a burst with HTTP 429, naming the wait either as
`Retry-After` (seconds) or as `x-ratelimit-reset` (milliseconds until the window resets). A
throttled request waits that long, capped, and is sent once more; a second 429 is an error
like any other. A run over a few hundred blobs is therefore slowed, not broken.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError

MAX_THROTTLE_WAIT = 60.0
DEFAULT_THROTTLE_WAIT = 5.0


class HashLookupSource:
    kind = "archive"

    def __init__(self, base_url: str, *, timeout: float = 60.0, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.sleep = sleep
        self.name = f"{self.kind}({self.base_url})"

    def url_for(self, vh: bytes) -> str:
        raise NotImplementedError

    def extract(self, body: Any) -> str | None:
        """The blob's hex from a 200 response body, or None when the archive has the record
        but not the bytes."""
        raise NotImplementedError

    def _get(self, url: str) -> httpx.Response:
        r = self.client.get(url, headers={"Accept": "application/json"})
        if r.status_code != 429:
            return r
        self.sleep(throttle_wait(r.headers))
        return self.client.get(url, headers={"Accept": "application/json"})

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        errors: list[str] = []
        for vh in wanted:
            label = f"0x{vh.hex()[:10]}…"
            try:
                r = self._get(self.url_for(vh))
            except httpx.HTTPError as e:
                errors.append(f"{label}: {e}")
                continue
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                errors.append(f"{label}: HTTP {r.status_code}")
                continue
            try:
                hexdata = self.extract(r.json())
                if not hexdata or hexdata == "0x":
                    continue    # the record exists, the bytes are not there (yet)
                out.append(bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                errors.append(f"{label}: malformed response ({e})")
        if not out and errors:
            # Nothing came back and something went wrong: say so, rather than letting an
            # outage look like an archive that simply has none of these blobs.
            raise SourceError("; ".join(errors))
        return out


def throttle_wait(headers) -> float:
    """How long a 429 asks us to wait, in seconds, capped so a misbehaving server cannot
    park a run for an hour."""
    retry_after = headers.get("retry-after")
    reset_ms = headers.get("x-ratelimit-reset")
    try:
        if retry_after is not None:
            wait = float(retry_after)
        elif reset_ms is not None:
            wait = float(reset_ms) / 1000.0
        else:
            wait = DEFAULT_THROTTLE_WAIT
    except ValueError:
        wait = DEFAULT_THROTTLE_WAIT
    return max(0.0, min(wait, MAX_THROTTLE_WAIT))
