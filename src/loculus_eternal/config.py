"""The TOML configuration file shared by the upload and recover subcommands.

One file describes one deployment: the chain and contract, where to find blob bytes, the
Loculus backend to publish from, and where to keep local state. The publisher key is never
in this file; it comes from the environment variable named below.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from loculus_eternal.sources import BeaconSource, BlobArchiverSource, BlobscanSource, LocalDirectorySource
from loculus_eternal.sources.beacon import MAINNET_GENESIS_TIME, SECONDS_PER_SLOT, SEPOLIA_GENESIS_TIME
from loculus_eternal.sources.blobscan import PUBLIC_API

PUBLISHER_KEY_ENV = "LOCULUS_ETERNAL_PUBLISHER_KEY"
KNOWN_GENESIS = {1: MAINNET_GENESIS_TIME, 11155111: SEPOLIA_GENESIS_TIME}


class ConfigError(SystemExit):
    """A configuration problem, reported to the operator and ending the command."""

    def __init__(self, message: str):
        super().__init__(f"configuration error: {message}")


@dataclass
class ChainConfig:
    rpc_url: str
    contract: str
    chain_id: int | None
    beacon_genesis_time: int
    seconds_per_slot: int
    deployment_block: int


@dataclass
class BackendConfig:
    url: str
    organisms: list[str]


@dataclass
class UploadConfig:
    data_dir: Path
    tooling_paths: list[str]
    max_blob_fee_gwei: float          # refuse to publish when the blob base fee is above this
    max_priority_fee_gwei: float
    finality_timeout_seconds: int
    inclusion_timeout_blocks: int
    escalation_attempts: int


@dataclass
class RecoverSettings:
    data_dir: Path
    out_dir: Path
    manifest_sources: list[str]
    skip_decode: bool


@dataclass
class Config:
    path: Path
    chain: ChainConfig
    sources: list
    backend: BackendConfig | None
    upload: UploadConfig | None
    recover: RecoverSettings
    raw: dict = field(repr=False, default_factory=dict)


def build_sources(spec: list[dict], genesis_time: int, seconds_per_slot: int) -> list:
    """Turn the `[[sources]]` entries into source objects, in the order given."""
    out = []
    for s in spec:
        kind = s.get("type")
        if kind == "beacon":
            out.append(BeaconSource(s["endpoints"], genesis_time=genesis_time, seconds_per_slot=seconds_per_slot))
        elif kind == "blobscan":
            out.append(BlobscanSource(s.get("url", PUBLIC_API)))
        elif kind == "blob-archiver":
            out.append(BlobArchiverSource(s["url"], genesis_time=genesis_time, seconds_per_slot=seconds_per_slot))
        elif kind == "local":
            out.append(LocalDirectorySource(s["path"]))
        else:
            raise ConfigError(f"unknown source type {kind!r} (known: beacon, blobscan, blob-archiver, local)")
    return out


def load(path: str | Path) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        chain_raw = raw["chain"]
        chain_id = chain_raw.get("chain_id")
        genesis = chain_raw.get("beacon_genesis_time", KNOWN_GENESIS.get(chain_id))
        if genesis is None:
            raise ConfigError("chain.beacon_genesis_time is needed unless chain.chain_id is 1 or 11155111")
        chain = ChainConfig(
            rpc_url=chain_raw["rpc_url"],
            contract=chain_raw["contract"],
            chain_id=chain_id,
            beacon_genesis_time=genesis,
            seconds_per_slot=chain_raw.get("seconds_per_slot", SECONDS_PER_SLOT),
            deployment_block=chain_raw.get("deployment_block", 0),
        )
        sources = build_sources(raw.get("sources", []), chain.beacon_genesis_time, chain.seconds_per_slot)
        backend = None
        if "backend" in raw:
            backend = BackendConfig(url=raw["backend"]["url"].rstrip("/"), organisms=list(raw["backend"]["organisms"]))
        upload = None
        if "upload" in raw:
            u = raw["upload"]
            upload = UploadConfig(
                data_dir=Path(u.get("data_dir", "upload-data")),
                tooling_paths=list(u.get("tooling_paths", [])),
                max_blob_fee_gwei=float(u.get("max_blob_fee_gwei", 5.0)),
                max_priority_fee_gwei=float(u.get("max_priority_fee_gwei", 1.0)),
                finality_timeout_seconds=int(u.get("finality_timeout_seconds", 1800)),
                inclusion_timeout_blocks=int(u.get("inclusion_timeout_blocks", 6)),
                escalation_attempts=int(u.get("escalation_attempts", 8)),
            )
        r = raw.get("recover", {})
        recover = RecoverSettings(
            data_dir=Path(r.get("data_dir", "recovery-data")),
            out_dir=Path(r.get("out_dir", "recovered")),
            manifest_sources=list(r.get("manifest_sources", ["logs"])),
            skip_decode=bool(r.get("skip_decode", False)),
        )
    except KeyError as e:
        raise ConfigError(f"{path} is missing {e.args[0]!r}") from e
    return Config(path=path, chain=chain, sources=sources, backend=backend, upload=upload, recover=recover, raw=raw)


def publisher_key() -> str:
    key = os.environ.get(PUBLISHER_KEY_ENV)
    if not key:
        raise ConfigError(f"the publisher key must be in the environment variable {PUBLISHER_KEY_ENV}; it is never read from a file")
    return key
