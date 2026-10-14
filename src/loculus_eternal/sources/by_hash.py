"""The shape shared by archives that are looked up by versioned hash alone.

Blobscan and Blockscout both answer one HTTP request per blob, keyed by the versioned hash,
so they need no slot or block information. What differs between them is the URL and where
the hex sits in the JSON; everything else lives here once: the loop, how an absent blob is
told from a failing server, and the pacing that public explorers demand.

A public explorer answers in a second or two, so the blobs of one call are requested a few
at a time rather than one after another. The concurrency is small enough to stay under the
explorers' limits, and a caller that wants the speed-up asks for a few dozen blobs per call.

Public explorers meter clients and answer a burst with HTTP 429, naming the wait either as
`Retry-After` (seconds, or an HTTP date) or as `x-ratelimit-reset` (milliseconds until the
window resets, as Blockscout sends it). The requests of one call wait together for that one
reset, capped at a minute, and are sent once more. A 429 after the reset they waited for
means the window is not opening for this client: the call gives up, and the blobs not yet
asked for are errors without a request. So a throttling archive costs a call at most one
wait, and never parks a run.

A request that fails is told apart from a blob the archive does not have: the failures of
the last call are kept per hash in `last_errors`, so a caller can count that blob as an
error rather than as missing. Only when every request of a call failed does the call itself
fail, since then nothing is known about any of the blobs.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError

MAX_THROTTLE_WAIT = 60.0        # the longest a call waits for a window to reset
DEFAULT_THROTTLE_WAIT = 5.0     # when a 429 names no wait
CONCURRENCY = 4                 # requests in flight per call; Blockscout allows 180 per half minute


class _Throttle:
    """What one call knows about the archive's rate limit: the reset it is waiting for, shared
    by the call's requests, and whether the call has given up."""

    def __init__(self, sleep: Callable[[float], None], clock: Callable[[], float]):
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._reset_at: float | None = None
        self.gave_up = False

    def wait_for_reset(self, headers) -> bool:
        """Called on a 429. Waits until the reset the archive named, together with every other
        request of the call that was throttled, and returns True to retry. Returns False, and
        the call has given up, when the 429 came after a reset this call already waited for."""
        with self._lock:
            now = self._clock()
            if self._reset_at is None:
                self._reset_at = now + throttle_wait(headers, now=time.time)
            elif now >= self._reset_at:
                self.gave_up = True
                return False
            delay = self._reset_at - now
        if delay > 0:
            self._sleep(delay)
        return True


class HashLookupSource:
    kind = "archive"

    def __init__(self, base_url: str, *, timeout: float = 60.0, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic, concurrency: int = CONCURRENCY):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)   # httpx clients are safe to share between threads
        self.sleep = sleep
        self.clock = clock
        self.concurrency = max(1, concurrency)
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
        self.last_errors = {}
        throttle = _Throttle(self.sleep, self.clock)
        with ThreadPoolExecutor(max_workers=min(self.concurrency, max(1, len(wanted))), thread_name_prefix=self.kind) as pool:
            answers = list(pool.map(lambda vh: self._fetch_one(vh, throttle), wanted))
        out: list[bytes] = []
        errors: dict[bytes, str] = {}
        for vh, (blob, error) in zip(wanted, answers):
            if blob is not None:
                out.append(blob)
            elif error is not None:
                errors[vh] = error
        self.last_errors = errors
        if errors and len(errors) == len(wanted):
            raise SourceError("; ".join(f"0x{vh.hex()[:10]}…: {msg}" for vh, msg in errors.items()))
        return out

    def _fetch_one(self, vh: bytes, throttle: _Throttle) -> tuple[bytes | None, str | None]:
        """One blob's request: the blob, or why there is none. Neither when the archive
        simply lacks it."""
        if throttle.gave_up:
            return None, "rate limited: the archive kept throttling after its window reset"
        url = self.url_for(vh)
        try:
            r = self._get(url)
            if r.status_code == 429:
                if not throttle.wait_for_reset(r.headers):
                    return None, "rate limited: the archive kept throttling after its window reset"
                r = self._get(url)
        except httpx.HTTPError as e:
            return None, str(e)
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        try:
            hexdata = self.extract(r.json())
            if not hexdata or hexdata == "0x":
                return None, None    # the record exists, the bytes are not there (yet)
            return bytes.fromhex(hexdata[2:] if hexdata.startswith("0x") else hexdata), None
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            return None, f"malformed response ({e})"


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
