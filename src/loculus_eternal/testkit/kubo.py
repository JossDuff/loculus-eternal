"""A Kubo (go-ipfs) node in Docker for tests, offline so it talks to nobody."""

from __future__ import annotations

import shutil
import subprocess
import time

import httpx

IMAGE = "ipfs/kubo:latest"


def kubo_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


class Kubo:
    def __init__(self):
        run = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", "127.0.0.1::5001", IMAGE, "daemon", "--offline", "--init-profile=test"],
            capture_output=True,
            text=True,
            check=True,
        )
        self.container = run.stdout.strip()
        port = subprocess.run(["docker", "port", self.container, "5001/tcp"], capture_output=True, text=True, check=True).stdout.strip().split("\n")[0]
        self.api_url = "http://" + port.replace("0.0.0.0", "127.0.0.1")
        self._wait_ready()

    def _wait_ready(self, timeout: float = 60.0) -> None:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                r = httpx.post(f"{self.api_url}/api/v0/version", timeout=5)
                if r.status_code == 200:
                    return
                last = r.status_code
            except httpx.HTTPError as e:
                last = e
            time.sleep(0.5)
        self.close()
        raise RuntimeError(f"Kubo did not become ready: {last}")

    def close(self) -> None:
        subprocess.run(["docker", "stop", "-t", "2", self.container], capture_output=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
