"""Archive-shaped blob servers for tests, with knobs for every way a source can misbehave.

One stub serves four shapes at once:

    /blobs/{versionedHash}/data              Blobscan: a JSON string of hex
    /api/v2/blobs/{versionedHash}            Blockscout: {"blob_data": hex or null, ...}
    /eth/v1/beacon/blob_sidecars/{slot}      blob-archiver: {"data": [{"index", "blob", ...}]}
    /eth/v1/beacon/blobs/{slot}              beacon v4 shape, as BeaconStub serves

Faults are configured per versioned hash: `withhold` makes the stub pretend it has no such
blob, `corrupt` flips a byte, `swap` serves another blob's bytes under this hash. `fail_next`
makes the next N requests return HTTP 500 regardless, to imitate an outage; `throttle_next`
answers the next N with HTTP 429 and a reset header, as a metered explorer does. `pending`
lists hashes Blockscout has indexed but not yet archived: served with `blob_data` null.
`empty` lists hashes served as an empty hex string by both hash-keyed shapes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class ArchiveStub:
    def __init__(self):
        self._lock = threading.Lock()
        self.blobs: dict[bytes, bytes] = {}          # versioned hash -> blob
        self.slots: dict[int, list[bytes]] = {}       # slot -> versioned hashes in order
        self.withhold: set[bytes] = set()
        self.corrupt: set[bytes] = set()
        self.swap: dict[bytes, bytes] = {}            # serve blobs[swap[vh]] under vh
        self.pending: set[bytes] = set()
        self.empty: set[bytes] = set()
        self.fail_next = 0
        self.throttle_next = 0
        self.throttle_reset_ms = 1500
        self.requests: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with stub._lock:
                    stub.requests.append(self.path)
                    if stub.fail_next > 0:
                        stub.fail_next -= 1
                        return self._reply(500, {"message": "simulated outage"})
                    if stub.throttle_next > 0:
                        stub.throttle_next -= 1
                        return self._reply(429, {"message": "Too Many Requests"}, {"x-ratelimit-limit": "180", "x-ratelimit-reset": str(stub.throttle_reset_ms)})
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "blobs" and parts[2] == "data":
                    vh = bytes.fromhex(parts[1][2:])
                    if vh in stub.empty:
                        return self._reply(200, "")
                    blob = stub._serve(vh)
                    if blob is None:
                        return self._reply(404, {"message": "Blob not found"})
                    return self._reply(200, "0x" + blob.hex())
                if len(parts) == 4 and parts[:3] == ["api", "v2", "blobs"]:
                    vh = bytes.fromhex(parts[3][2:])
                    if vh in stub.empty:
                        return self._reply(200, {"blob_data": "", "hash": parts[3]})
                    blob = None if vh in stub.pending else stub._serve(vh)
                    if blob is None and vh not in stub.pending:
                        return self._reply(404, {"message": "Not found"})
                    data = None if blob is None else "0x" + blob.hex()
                    return self._reply(200, {"blob_data": data, "hash": parts[3], "kzg_commitment": None, "kzg_proof": None, "transaction_hashes": []})
                if parts[:3] == ["eth", "v1", "beacon"] and len(parts) == 5 and parts[3] in ("blob_sidecars", "blobs"):
                    if not (parts[4].isascii() and parts[4].isdigit()):
                        return self._reply(400, {"message": "slot required"})
                    hashes = stub.slots.get(int(parts[4]))
                    if hashes is None:
                        return self._reply(404, {"message": "slot not found"})
                    wanted = {h.lower() for h in parse_qs(url.query).get("versioned_hashes", [])}
                    items = []
                    for i, vh in enumerate(hashes):
                        if wanted and "0x" + vh.hex() not in wanted:
                            continue
                        blob = stub._serve(vh)
                        if blob is None:
                            continue
                        if parts[3] == "blob_sidecars":
                            items.append({"index": str(i), "blob": "0x" + blob.hex(), "kzg_commitment": "0x" + "00" * 48, "kzg_proof": "0x" + "00" * 48})
                        else:
                            items.append("0x" + blob.hex())
                    return self._reply(200, {"execution_optimistic": False, "finalized": True, "data": items})
                return self._reply(404, {"message": "not found"})

            def _reply(self, status, body, headers=None):
                payload = json.dumps(body).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _serve(self, vh: bytes) -> bytes | None:
        """What the stub returns for a hash, after applying its configured faults."""
        with self._lock:
            if vh in self.withhold or vh not in self.blobs:
                return None
            blob = self.blobs[self.swap[vh]] if vh in self.swap else self.blobs[vh]
            if vh in self.corrupt:
                b = bytearray(blob)
                b[5000] ^= 0x01
                blob = bytes(b)
            return blob

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def add(self, slot: int, pairs: list[tuple[bytes, bytes]]) -> None:
        with self._lock:
            for vh, blob in pairs:
                self.blobs[vh] = blob
                self.slots.setdefault(slot, []).append(vh)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
