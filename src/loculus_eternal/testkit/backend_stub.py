"""A Loculus-backend-shaped server for tests: `GET /{organism}/get-released-data`.

Serves NDJSON lines in the backend's released shape, honours `compression=zstd`, sends
`ETag` and `X-Total-Records`, and answers `If-None-Match` with 304. Tests add and open
entries between runs to imitate new releases and restricted data turning open.
"""

from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import zstandard


def released_line(organism: str, accession: str, version: int, *, open_terms: bool = True, seq_len: int = 40, extra: dict | None = None) -> dict:
    """One line as the backend serves it: the published fields plus the status and terms
    fields the projection removes."""
    from loculus_eternal.format.gen_vectors import sample_entry

    e = sample_entry(organism, accession, version, seq_len=seq_len, extra=extra)
    m = dict(e["metadata"])
    m["versionStatus"] = "LATEST_VERSION"
    m["dataUseTerms"] = "OPEN" if open_terms else "RESTRICTED"
    m["dataUseTermsRestrictedUntil"] = None if open_terms else "2027-01-01"
    m["dataUseTermsUrl"] = "https://pathoplexus.org/about/terms-of-use/" + ("open-data" if open_terms else "restricted-use")
    line = {"metadata": m}
    for k in ("unalignedNucleotideSequences", "alignedNucleotideSequences", "nucleotideInsertions", "alignedAminoAcidSequences", "aminoAcidInsertions"):
        line[k] = e[k]
    return line


class BackendStub:
    def __init__(self):
        self._lock = threading.Lock()
        self.lines: dict[str, list[dict]] = {}
        self.files: dict[str, tuple] = {}   # organism -> (path to zstd NDJSON, record count): served as-is
        self.enumerate_organisms = True     # False: the API description lacks the Organism enum
        self.requests: list[tuple[str, dict]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlparse(self.path)
                parts = url.path.strip("/").split("/")
                stub.requests.append((self.path, dict(self.headers)))
                if url.path == "/api-docs":
                    with stub._lock:
                        names = sorted(set(stub.lines) | set(stub.files))
                    schema = {"description": "valid names of organisms that this Loculus instance supports", "enum": names} if stub.enumerate_organisms else {"type": "string"}
                    doc = {"openapi": "3.0.1", "paths": {}, "components": {"schemas": {"Organism": schema}}}
                    return self._reply(200, json.dumps(doc).encode(), "application/json")
                if len(parts) != 2 or parts[1] != "get-released-data":
                    return self._reply(404, b'{"detail":"not found"}', "application/json")
                organism = parts[0]
                with stub._lock:
                    file_entry = stub.files.get(organism)
                    lines = stub.lines.get(organism)
                if file_entry is not None:
                    # A pre-compressed feed served byte for byte, for full-scale rehearsals.
                    path, count = file_entry
                    etag = '"' + hashlib.sha256(str(path).encode()).hexdigest()[:24] + "|file\""
                    if self.headers.get("If-None-Match") == etag:
                        self.send_response(304)
                        self.send_header("ETag", etag)
                        self.end_headers()
                        return
                    data = open(path, "rb").read()
                    return self._reply(200, data, "application/x-ndjson", {"ETag": etag, "X-Total-Records": str(count), "Content-Encoding": "zstd"})
                if lines is None:
                    return self._reply(404, b'{"detail":"unknown organism"}', "application/json")
                body = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
                etag = '"' + hashlib.sha256(body).hexdigest()[:24] + "|stub\""
                if self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.end_headers()
                    return
                headers = {"ETag": etag, "X-Total-Records": str(len(lines))}
                if parse_qs(url.query).get("compression", [""])[0].lower() == "zstd":
                    body = zstandard.ZstdCompressor(level=3).compress(body)
                    headers["Content-Encoding"] = "zstd"
                return self._reply(200, body, "application/x-ndjson", headers)

            def _reply(self, status, body, ctype, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def set_file(self, organism: str, path, count: int) -> None:
        """Serve a zstd-compressed NDJSON file as the organism's feed."""
        with self._lock:
            self.files[organism] = (path, count)

    def set_lines(self, organism: str, lines: list[dict]) -> None:
        with self._lock:
            self.lines[organism] = list(lines)

    def add(self, organism: str, line: dict) -> None:
        with self._lock:
            self.lines.setdefault(organism, []).append(line)

    def open_entry(self, organism: str, accession_version: str) -> None:
        """Restricted data turning open: the same line, now with open terms."""
        with self._lock:
            for line in self.lines.get(organism, []):
                m = line["metadata"]
                if m["accessionVersion"] == accession_version:
                    m["dataUseTerms"] = "OPEN"
                    m["dataUseTermsRestrictedUntil"] = None
                    m["dataUseTermsUrl"] = "https://pathoplexus.org/about/terms-of-use/open-data"
                    m["dataBecameOpenAt"] = "2026-10-01T00:00:00Z"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
