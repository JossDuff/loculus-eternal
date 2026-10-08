"""A small local web server for the health page.

`GET /` serves the page; `GET /api/report` returns the latest report (or the one in
progress) as JSON; `POST /api/check` starts a check unless one is running. Checks run in a
background thread so the page can show progress. Standard library only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loculus_eternal.config import Config
from loculus_eternal.health import HealthCheck, Report, public_url, source_endpoints, source_kind
from loculus_eternal.sources import IpfsSource

PAGE = Path(__file__).with_name("health_page.html")
REPOSITORY = "https://github.com/JossDuff/loculus-eternal"
NETWORKS = {1: ("Ethereum mainnet", "https://etherscan.io"), 11155111: ("Sepolia testnet", "https://sepolia.etherscan.io")}


def public_config(config: Config) -> dict:
    """What the page shows about the deployment and the check's inputs, with every URL
    reduced to its host so no credential can leak onto a page meant to be shown around."""
    name, explorer = NETWORKS.get(config.chain.chain_id or 0, (f"chain {config.chain.chain_id}" if config.chain.chain_id else "unknown chain", None))
    sources = []
    for src in config.sources:
        entry = {"type": source_kind(src), "endpoints": [public_url(e) for e in source_endpoints(src)]}
        if isinstance(src, IpfsSource):
            entry["snapshotCid"] = src.snapshot_cid
        sources.append(entry)
    return {
        "contract": config.chain.contract,
        "chainId": config.chain.chain_id,
        "network": name,
        "explorer": f"{explorer}/address/{config.chain.contract}" if explorer else None,
        "rpc": public_url(config.chain.rpc_url),
        "deploymentBlock": config.chain.deployment_block,
        "sources": sources,
        "ipfsEndpoints": [public_url(e) for e in config.ipfs.endpoints] if config.ipfs else [],
        "configFile": str(config.path),
        "repository": REPOSITORY,
    }


class HealthServer:
    def __init__(self, config: Config, host: str = "127.0.0.1", port: int = 8765):
        self.config = config
        self._lock = threading.Lock()
        self._report: Report | None = None
        self._running = False
        self._page = PAGE.read_text(encoding="utf-8")
        self._public = public_config(config)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    return self._reply(200, server._page.encode("utf-8"), "text/html; charset=utf-8")
                if self.path == "/api/report":
                    return self._reply(200, json.dumps(server.snapshot(), default=str).encode(), "application/json")
                return self._reply(404, b"not found", "text/plain")

            def do_POST(self):
                if self.path == "/api/check":
                    started = server.start_check()
                    return self._reply(202 if started else 409, json.dumps({"started": started}).encode(), "application/json")
                return self._reply(404, b"not found", "text/plain")

            def _reply(self, status, body, ctype):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer((host, port), Handler)

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def snapshot(self) -> dict:
        with self._lock:
            if self._report is None:
                d = {"verdict": "idle", "running": self._running, "contract": self.config.chain.contract}
            else:
                d = self._report.to_json()
                d["running"] = self._running
            d["config"] = self._public
            return d

    def start_check(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._report = Report(started_at=time.time())
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        live = self._report

        def log(msg: str) -> None:
            # The log is for whoever runs the server: it goes to the console, not the page.
            line = f"{time.strftime('%H:%M:%S')} {msg}"
            print(line, file=sys.stderr, flush=True)
            with self._lock:
                live.log.append(line)

        def progress(p: dict) -> None:
            with self._lock:
                live.progress = dict(p)

        try:
            report = HealthCheck(self.config, log=log, progress=progress).run()
        except Exception as e:  # the page must always get a result
            report = Report(started_at=live.started_at, finished_at=time.time(), verdict="not recoverable", problems=[f"the check itself failed: {e}"], log=list(live.log))
        with self._lock:
            # Every line went through log() above, so the live copy is the whole log.
            report.log = live.log
            self._report = report
            self._running = False

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
