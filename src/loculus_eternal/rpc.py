"""The JSON-RPC connection, built to live with metered endpoints.

Hosted providers meter requests per second and per day and answer bursts with HTTP 429.
Every connection this project opens therefore does two things: it spaces requests so that
no more than a configured number leave per second, and it retries a failed request with
growing pauses. Retrying is safe for every method used here, including sending a signed
transaction, because resending the same signed bytes cannot produce a second transaction.
"""

from __future__ import annotations

import threading
import time

from requests.exceptions import ConnectionError, HTTPError, Timeout
from web3 import HTTPProvider, Web3
from web3.providers.rpc.utils import REQUEST_RETRY_ALLOWLIST, ExceptionRetryConfiguration

DEFAULT_MAX_REQUESTS_PER_SECOND = 5.0
RETRIES = 8
BACKOFF_FACTOR = 0.5   # pauses of 0.5, 1, 2, 4, 8, 16, 32, 64 seconds

# web3's own allowlist covers receipts, blocks, calls, estimates, logs and sending a signed
# transaction; the blob base fee and a few other reads this project uses are added.
RETRY_METHODS = sorted(set(REQUEST_RETRY_ALLOWLIST) | {"eth_blobBaseFee", "eth_getBalance", "eth_getCode", "eth_chainId", "web3_clientVersion"})


class ThrottledHTTPProvider(HTTPProvider):
    """An HTTP provider that spaces its requests to at most `max_rps` per second."""

    def __init__(self, endpoint_uri: str, *, max_rps: float = DEFAULT_MAX_REQUESTS_PER_SECOND, timeout: float = 120.0, retries: int = RETRIES, backoff_factor: float = BACKOFF_FACTOR, sleep=time.sleep, clock=time.monotonic):
        super().__init__(
            endpoint_uri,
            request_kwargs={"timeout": timeout},
            exception_retry_configuration=ExceptionRetryConfiguration(
                errors=(ConnectionError, HTTPError, Timeout),
                retries=retries,
                backoff_factor=backoff_factor,
                method_allowlist=RETRY_METHODS,
            ),
        )
        self._interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._sleep = sleep
        self._clock = clock
        self.requests_made = 0

    def _wait_turn(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = self._clock()
            wait = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait > 0:
            self._sleep(wait)

    def make_request(self, method, params):
        self._wait_turn()
        self.requests_made += 1
        return super().make_request(method, params)


def connect(rpc_url: str, *, max_rps: float = DEFAULT_MAX_REQUESTS_PER_SECOND, timeout: float = 120.0) -> Web3:
    return Web3(ThrottledHTTPProvider(rpc_url, max_rps=max_rps, timeout=timeout))
