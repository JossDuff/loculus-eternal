"""A web3 provider wrapper that fails on demand, to test behaviour when the RPC flaps."""

from __future__ import annotations

from web3.providers.base import BaseProvider


class FlakyProvider(BaseProvider):
    """Delegates to another provider but raises on a configurable set of calls.

    `fail_methods` names the JSON-RPC methods that may fail; `fail_count` is how many of
    those calls fail before the provider recovers. Every call is recorded in `calls`.
    """

    def __init__(self, inner: BaseProvider, fail_methods: set[str], fail_count: int):
        super().__init__()
        self.inner = inner
        self.fail_methods = fail_methods
        self.fail_count = fail_count
        self.calls: list[str] = []
        self.failures = 0

    def make_request(self, method, params):
        self.calls.append(method)
        if method in self.fail_methods and self.fail_count > 0:
            self.fail_count -= 1
            self.failures += 1
            raise ConnectionError(f"simulated RPC outage on {method}")
        return self.inner.make_request(method, params)

    def is_connected(self, show_traceback: bool = False) -> bool:
        return self.inner.is_connected(show_traceback)
