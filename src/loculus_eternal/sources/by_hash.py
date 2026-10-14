"""The shape shared by archives that are looked up by versioned hash alone.

Blobscan and Blockscout both answer one HTTP request per blob, keyed by the versioned hash,
so they need no slot or block information. What differs between them is the URL and where
the hex sits in the JSON; everything else lives here once: the loop, how an absent blob is
told from a failing server, and the pacing that public explorers demand.

Public explorers meter clients and answer a burst with HTTP 429, naming the wait either as
`Retry-After` (seconds, or an HTTP date) or as `x-ratelimit-reset` (milliseconds until the
window resets, as Blockscout sends it). A throttled request waits that long and is sent once
more; a second 429 is an error like any other. One call may spend at most a minute waiting
in total: once that is spent, the blobs not yet asked for are errors without a request, so
a source that keeps throttling slows a run by a bounded amount per call and never parks it.

A request that fails is told apart from a blob the archive does not have: the failures of
the last call are kept per hash in `last_errors`, so a caller can count that blob as an
error rather than as missing. Only when every request of a call failed does the call itself
fail, since then nothing is known about any of the blobs.
"""

from __future__ import annotations

import time
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError

MAX_THROTTLE_WAIT = 60.0        # seconds one call may spend waiting on 429s, in total
DEFAULT_THROTTLE_WAIT = 5.0     # when a 429 names no wait


class HashLookupSource:
    kind = "archive"

    def __init__(self, base_url: str, *, timeout: float = 60.0, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.sleep = sleep
        self.name = f"{self.kind}({self.base_url})"
        self.last_errors: dict[bytes, str] = {}    # versioned hash -> why its request failed, last call

    def url_for(self, vh: bytes) -> str:
        raise NotImplementedError

    def extract(self, body: Any) -> str | None:
        """The blob's hex from a 200 response body, or None when the archive has the record
        but not the bytes."""
        raise NotImplementedError

    def _get(self, url: str) -> httpx.Response:
        return self.client.get(url, headers={"Accept": "application/json"})

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        errors: dict[bytes, str] = {}
        budget = MAX_THROTTLE_WAIT
        for vh in wanted:
            if budget <= 0:
                errors[vh] = "rate limited: this call's waiting time is spent"
                continue
            url = self.url_for(vh)
            try:
                r = self._get(url)
                if r.status_code == 429:
                    wait = min(throttle_wait(r.headers), budget)
                    self.sleep(wait)
                    budget -= wait
                    r = self._get(url)
            except httpx.HTTPError as e:
                errors[vh] = str(e)
                continue
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                errors[vh] = f"HTTP {r.status_code}"
                continue
            try:
                hexdata = self.extract(r.json())
                if not hexdata or hexdata == "0x":
                    continue    # the record exists, the bytes are not there (yet)
                out.append(bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata))
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                errors[vh] = f"malformed response ({e})"
        self.last_errors = errors
        if errors and len(errors) == len(wanted):
            raise SourceError("; ".join(f"0x{vh.hex()[:10]}…: {msg}" for vh, msg in errors.items()))
        return out


def throttle_wait(headers, now: Callable[[], float] = time.time) -> float:
    """How long a 429 asks us to wait, in seconds, capped so a misbehaving server cannot
    park a run. `Retry-After` is the standard header, in seconds or as an HTTP date;
    `x-ratelimit-reset` is Blockscout's, in milliseconds."""
    retry_after = headers.get("retry-after")
    reset = headers.get("x-ratelimit-reset")
    wait = DEFAULT_THROTTLE_WAIT
    try:
        if retry_after is not None:
            try:
                wait = float(retry_after)
            except ValueError:
                wait = parsedate_to_datetime(retry_after).timestamp() - now()
        elif reset is not None:
            wait = float(reset) / 1000.0
    except (ValueError, TypeError):
        pass
    return max(0.0, min(wait, MAX_THROTTLE_WAIT))
