"""The `loculus-eternal` command line."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from loculus_eternal.recover import ManifestSource, Recovery, RecoveryConfig
from loculus_eternal.sources import BeaconSource, BlobArchiverSource, BlobscanSource, LocalDirectorySource
from loculus_eternal.sources.beacon import MAINNET_GENESIS_TIME, SECONDS_PER_SLOT, SEPOLIA_GENESIS_TIME
from loculus_eternal.sources.blobscan import PUBLIC_API

KNOWN_GENESIS = {1: MAINNET_GENESIS_TIME, 11155111: SEPOLIA_GENESIS_TIME}


def build_sources(spec: list[dict], genesis_time: int, seconds_per_slot: int) -> list:
    """Turn the `[[sources]]` entries of a config file into source objects, in order."""
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
            raise SystemExit(f"unknown source type {kind!r} in config (known: beacon, blobscan, blob-archiver, local)")
    return out


def recovery_config_from_toml(path: Path) -> RecoveryConfig:
    cfg = tomllib.loads(Path(path).read_text())
    chain = cfg["chain"]
    rec = cfg.get("recover", {})
    chain_id = chain.get("chain_id")
    genesis_time = chain.get("beacon_genesis_time", KNOWN_GENESIS.get(chain_id))
    if genesis_time is None:
        raise SystemExit("config needs chain.beacon_genesis_time (or a chain.chain_id of 1 or 11155111)")
    manifest_sources = []
    for m in rec.get("manifest_sources", ["logs"]):
        if m == "logs":
            manifest_sources.append(ManifestSource(logs=True))
        else:
            manifest_sources.append(ManifestSource(file=Path(m)))
    return RecoveryConfig(
        rpc_url=chain["rpc_url"],
        contract=chain["contract"],
        data_dir=Path(rec.get("data_dir", "recovery-data")),
        out_dir=Path(rec.get("out_dir", "recovered")),
        sources=build_sources(cfg.get("sources", []), genesis_time, chain.get("seconds_per_slot", SECONDS_PER_SLOT)),
        manifest_sources=manifest_sources,
        deployment_block=chain.get("deployment_block", 0),
        decode=not rec.get("skip_decode", False),
    )


def cmd_recover(args: argparse.Namespace) -> int:
    config = recovery_config_from_toml(args.config)
    if args.skip_decode:
        config.decode = False
    report = Recovery(config).run()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "run-report.json").write_text(json.dumps(report.to_json(), indent=1))
    if report.manifest_source is None:
        print("FAILED: no verified blob list", file=sys.stderr)
        return 2
    if report.missing:
        print(f"INCOMPLETE: {len(report.missing)} blobs missing; see {config.data_dir / 'missing.json'}", file=sys.stderr)
        return 1
    if report.decode and not report.decode["allArtifactsMatch"]:
        print("DECODED WITH ERRORS: see recovery-report.json", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loculus-eternal", description="Permanent, verifiable backup of Pathoplexus released data in Ethereum blobs.")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("recover", help="rebuild the dataset from the contract address and the configured sources")
    rec.add_argument("--config", type=Path, required=True, help="TOML config file (see docs/runbook.md)")
    rec.add_argument("--skip-decode", action="store_true", help="fetch and verify blobs only; do not materialise the dataset")
    rec.set_defaults(func=cmd_recover)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
