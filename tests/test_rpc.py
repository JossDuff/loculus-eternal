"""The throttled, retrying RPC connection used against metered endpoints."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from loculus_eternal.rpc import RETRY_METHODS, ThrottledHTTPProvider, connect


class CountingNode:
    """A JSON-RPC server that answers eth_chainId, failing with 429 the first `fail` times."""

    def __init__(self, fail: int = 0):
        self.fail = fail
        self.calls = []
        node = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                node.calls.append(body["method"])
                if node.fail > 0:
                    node.fail -= 1
                    self.send_response(429)
                    self.end_headers()
                    return
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": "0xaa36a7"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def test_P3_requests_are_spaced_to_the_configured_rate():
    node = CountingNode()
    try:
        slept = []
        clock = {"t": 100.0}
        provider = ThrottledHTTPProvider(node.url, max_rps=4, sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)), clock=lambda: clock["t"])
        for _ in range(5):
            provider.make_request("eth_chainId", [])
        assert provider.requests_made == 5
        # The first request goes at once; the next four each wait a quarter second.
        assert len(slept) == 4 and all(abs(s - 0.25) < 1e-9 for s in slept)
    finally:
        node.close()


def test_P3_a_rate_limited_request_is_retried_with_backoff():
    node = CountingNode(fail=3)
    try:
        slept = []
        provider = ThrottledHTTPProvider(node.url, max_rps=0, retries=5, backoff_factor=0.01, sleep=slept.append)
        # web3 sleeps for the backoff inside its retry loop; the throttle itself is off here.
        result = provider.make_request("eth_chainId", [])
        assert result["result"] == "0xaa36a7"
        assert node.calls == ["eth_chainId"] * 4
    finally:
        node.close()


def test_P3_retry_covers_the_methods_this_project_uses():
    for method in ("eth_blobBaseFee", "eth_getTransactionReceipt", "eth_sendRawTransaction", "eth_estimateGas", "eth_getLogs", "eth_getBlockByNumber", "eth_getTransactionCount"):
        assert method in RETRY_METHODS, method


def test_P3_connect_returns_a_working_web3():
    node = CountingNode()
    try:
        w3 = connect(node.url, max_rps=100)
        assert w3.eth.chain_id == 11155111
    finally:
        node.close()
