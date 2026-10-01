"""Beacon API source: `GET /eth/v1/beacon/blobs/{slot}?versioned_hashes=…`.

Works against any consensus node (or hosted provider) for blobs still inside the retention
window, and against anything that mimics the shape, such as the test stub. Several
endpoints may be listed; each is asked only for what the previous ones did not supply.
"""

from __future__ import annotations

import httpx

from loculus_eternal.sources.base import BlobContext, SourceError, verify_candidates

MAINNET_GENESIS_TIME = 1606824023
SEPOLIA_GENESIS_TIME = 1655733600
SECONDS_PER_SLOT = 12


class BeaconSource:
    def __init__(self, endpoints: list[str], *, genesis_time: int, seconds_per_slot: int = SECONDS_PER_SLOT, timeout: float = 60.0, client: httpx.Client | None = None):
        if not endpoints:
            raise ValueError("at least one beacon endpoint is required")
        self.endpoints = [e.rstrip("/") for e in endpoints]
        self.genesis_time = genesis_time
        self.seconds_per_slot = seconds_per_slot
        self.client = client or httpx.Client(timeout=timeout)
        self.name = "beacon(" + ", ".join(self.endpoints) + ")"

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        slot = ctx.slot(self.genesis_time, self.seconds_per_slot)
        if slot is None:
            raise SourceError("beacon source needs the block timestamp to compute the slot")
        remaining = set(wanted)
        out: list[bytes] = []
        errors: list[str] = []
        for endpoint in self.endpoints:
            if not remaining:
                break
            params = [("versioned_hashes", "0x" + vh.hex()) for vh in sorted(remaining)]
            try:
                r = self.client.get(f"{endpoint}/eth/v1/beacon/blobs/{slot}", params=params, headers={"Accept": "application/json"})
            except httpx.HTTPError as e:
                errors.append(f"{endpoint}: {e}")
                continue
            if r.status_code == 404:
                errors.append(f"{endpoint}: slot {slot} not found (pruned or not yet known)")
                continue
            if r.status_code != 200:
                errors.append(f"{endpoint}: HTTP {r.status_code}")
                continue
            try:
                data = r.json()["data"]
                blobs = [bytes.fromhex(item[2:] if isinstance(item, str) else item["blob"][2:]) for item in data]
            except (ValueError, KeyError, TypeError) as e:
                errors.append(f"{endpoint}: malformed response ({e})")
                continue
            out.extend(blobs)
            accepted, _ = verify_candidates(blobs, remaining)
            remaining -= set(accepted)
        if not out and errors and len(errors) == len(self.endpoints):
            raise SourceError("; ".join(errors))
        return out
