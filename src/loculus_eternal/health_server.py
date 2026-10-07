"""A small local web server for the health page.

`GET /` serves the page; `GET /api/report` returns the latest report (or the one in
progress) as JSON; `POST /api/check` starts a check unless one is running. Checks run in a
background thread so the page can show progress. Standard library only.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from loculus_eternal.config import Config
from loculus_eternal.health import HealthCheck, Report

PAGE = Path(__file__).with_name("health_page.html")


class HealthServer:
    def __init__(self, config: Config, host: str = "127.0.0.1", port: int = 8765):
        self.config = config
        self._lock = threading.Lock()
        self._report: Report | None = None
        self._running = False
        self._page = PAGE.read_text(encoding="utf-8")
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
                return {"verdict": "idle", "running": self._running, "contract": self.config.chain.contract}
            d = self._report.to_json()
            d["running"] = self._running
            return d

    def start_check(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._report = Report(started_at=__import__("time").time())
        threading.Thread(target=self._run, daemon=True).start()
        return True

    def _run(self) -> None:
        live = self._report

        def log(msg: str) -> None:
            with self._lock:
                live.log.append(f"{__import__('time').strftime('%H:%M:%S')} {msg}")

        try:
            report = HealthCheck(self.config, log=log).run()
        except Exception as e:  # the page must always get a result
            report = Report(started_at=live.started_at, finished_at=__import__("time").time(), verdict="failing", problems=[f"the check itself failed: {e}"], log=list(live.log))
        with self._lock:
            # Keep the live log lines the page already showed, then the finished report's own.
            report.log = live.log + [l for l in report.log if l not in live.log]
            self._report = report
            self._running = False

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
