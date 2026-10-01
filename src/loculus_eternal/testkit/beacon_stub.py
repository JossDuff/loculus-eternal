"""A beacon-API-shaped blob server for tests.

Serves `GET /eth/v1/beacon/blobs/{slot}` with the optional repeated `versioned_hashes`
filter, returning `{"execution_optimistic": false, "finalized": true, "data": ["0x…"]}`
exactly as a real node would. Tests register blobs against a slot after a transaction is
included and can later forget them to imitate pruning. Slot numbering follows the convention
the recovery sources use against anvil: slot = block timestamp, with genesis time 0 and one
second per slot.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class BeaconStub:
    def __init__(self):
        self._lock = threading.Lock()
        self._slots: dict[int, list[tuple[bytes, bytes]]] = {}
        self.requests: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append(self.path)
                url = urlparse(self.path)
                prefix = "/eth/v1/beacon/blobs/"
                if not url.path.startswith(prefix):
                    return self._reply(404, {"code": 404, "message": "not found"})
                block_id = url.path[len(prefix) :]
                if not (block_id.isascii() and block_id.isdigit()):
                    return self._reply(400, {"code": 400, "message": "block_id must be a slot number in this stub"})
                wanted = {h.lower() for h in parse_qs(url.query).get("versioned_hashes", [])}
                with stub._lock:
                    entries = stub._slots.get(int(block_id))
                if entries is None:
                    return self._reply(404, {"code": 404, "message": "slot not found"})
                data = ["0x" + blob.hex() for vh, blob in entries if not wanted or "0x" + vh.hex() in wanted]
                return self._reply(200, {"execution_optimistic": False, "finalized": True, "data": data})

            def _reply(self, status: int, body: dict):
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def register(self, slot: int, blobs: list[tuple[bytes, bytes]]) -> None:
        """Add (versioned_hash, blob) pairs to a slot."""
        with self._lock:
            self._slots.setdefault(slot, []).extend(blobs)

    def forget(self, slot: int) -> None:
        """Imitate pruning: the slot is no longer served."""
        with self._lock:
            self._slots.pop(slot, None)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
