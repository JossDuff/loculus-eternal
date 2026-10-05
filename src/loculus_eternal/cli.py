"""The `loculus-eternal` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from loculus_eternal import config as configuration
from loculus_eternal.recover import ManifestSource, Recovery, RecoveryConfig


def recovery_config(cfg: configuration.Config, skip_decode: bool) -> RecoveryConfig:
    from loculus_eternal.sources import IpfsSource

    manifest_sources = []
    for m in cfg.recover.manifest_sources:
        if m == "logs":
            manifest_sources.append(ManifestSource(logs=True))
        elif m.startswith("ipfs:"):
            # Reuse a configured IPFS source for the same snapshot when there is one, so the
            # manifest and the blobs come from the same place; otherwise use [ipfs].endpoints.
            cid = m[len("ipfs:") :]
            existing = next((s for s in cfg.sources if isinstance(s, IpfsSource) and s.snapshot_cid == cid), None)
            if existing is None:
                if cfg.ipfs is None or not cfg.ipfs.endpoints:
                    raise SystemExit(f"configuration error: manifest source {m!r} needs an ipfs source for that snapshot or [ipfs].endpoints")
                existing = IpfsSource(cfg.ipfs.endpoints, cid)
            manifest_sources.append(ManifestSource(ipfs=existing))
        else:
            manifest_sources.append(ManifestSource(file=Path(m)))
    return RecoveryConfig(
        rpc_url=cfg.chain.rpc_url,
        contract=cfg.chain.contract,
        data_dir=cfg.recover.data_dir,
        out_dir=cfg.recover.out_dir,
        sources=cfg.sources,
        manifest_sources=manifest_sources,
        deployment_block=cfg.chain.deployment_block,
        decode=not (cfg.recover.skip_decode or skip_decode),
    )


def cmd_recover(args: argparse.Namespace) -> int:
    cfg = configuration.load(args.config)
    rc = recovery_config(cfg, args.skip_decode)
    report = Recovery(rc).run()
    rc.data_dir.mkdir(parents=True, exist_ok=True)
    (rc.data_dir / "run-report.json").write_text(json.dumps(report.to_json(), indent=1))
    if report.manifest_source is None:
        print("FAILED: no verified blob list", file=sys.stderr)
        return 2
    if report.missing:
        print(f"INCOMPLETE: {len(report.missing)} blobs missing; see {rc.data_dir / 'missing.json'}", file=sys.stderr)
        return 1
    if report.decode and not report.decode.get("allArtifactsMatch"):
        print("DECODED WITH ERRORS: see recovery-report.json", file=sys.stderr)
        return 1
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    from loculus_eternal.upload.upload import Uploader

    cfg = configuration.load(args.config)
    mode = "check" if args.check else "dry-run" if args.dry_run else "publish"
    if mode == "check" and not args.with_key:
        uploader = Uploader(cfg)
    else:
        uploader = Uploader.with_key(cfg, configuration.publisher_key())
    report = uploader.run(mode)
    if report.outcome in ("published", "nothing-to-publish", "checked", "dry-run-ok", "resumed"):
        return 0
    if report.outcome == "failed":
        print(f"publishing stopped: {report.message}", file=sys.stderr)
        return 3
    print(report.message, file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="loculus-eternal", description="Permanent, verifiable backup of Pathoplexus released data in Ethereum blobs.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="publish every newly eligible entry as one batch; a no-op when there is nothing new")
    up.add_argument("--config", type=Path, required=True, help="TOML config file (see docs/runbook.md)")
    up.add_argument("--check", action="store_true", help="report how many entries await publication and the estimated cost; send nothing")
    up.add_argument("--dry-run", action="store_true", help="do everything except send: encode, simulate, check fees and balance")
    up.add_argument("--with-key", action="store_true", help="with --check, also read the key so the wallet balance is reported")
    up.set_defaults(func=cmd_upload)

    rec = sub.add_parser("recover", help="rebuild the dataset from the contract address and the configured sources")
    rec.add_argument("--config", type=Path, required=True, help="TOML config file (see README)")
    rec.add_argument("--skip-decode", action="store_true", help="fetch and verify blobs only; do not materialise the dataset")
    rec.set_defaults(func=cmd_recover)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
