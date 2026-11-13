"""The `loculus-eternal` command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from requests.exceptions import RequestException

from loculus_eternal import config as configuration
from loculus_eternal.chain import ChainError
from loculus_eternal.recover import ManifestSource, Recovery, RecoveryConfig


def recovery_config(cfg: configuration.Config, skip_decode: bool) -> RecoveryConfig:
    from loculus_eternal.sources import IpfsSource

    manifest_sources = []
    for m in cfg.recover.manifest_sources:
        if m == "logs":
            manifest_sources.append(ManifestSource(logs=True))
        elif m == "ipfs":
            # The snapshot the contract names. Reuse every configured IPFS source, in their
            # order, so the manifest and the blobs come from the same places and a node that
            # is down does not hide the manifest another node holds; with no ipfs source,
            # use [ipfs].endpoints.
            existing = [s for s in cfg.sources if isinstance(s, IpfsSource)]
            if not existing:
                if cfg.ipfs is None or not cfg.ipfs.endpoints:
                    raise SystemExit("configuration error: manifest source 'ipfs' needs an ipfs source or [ipfs].endpoints")
                existing = [IpfsSource(cfg.ipfs.endpoints)]
            manifest_sources.extend(ManifestSource(ipfs=s) for s in existing)
        elif m.startswith("ipfs:"):
            raise SystemExit(f"configuration error: manifest source {m!r} names a snapshot, but the snapshot CID is read from the contract now; write \"ipfs\"")
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
        max_requests_per_second=cfg.chain.max_requests_per_second,
    )


def cmd_recover(args: argparse.Namespace) -> int:
    cfg = configuration.load(args.config)
    rc = recovery_config(cfg, args.skip_decode)
    report = Recovery(rc).run()
    rc.data_dir.mkdir(parents=True, exist_ok=True)
    (rc.data_dir / "run-report.json").write_text(json.dumps(report.to_json(), indent=1))
    if report.manifest_source is None:
        print("RESULT: FAILED: no source produced a blob list that matches the chain", file=sys.stderr)
        return 2
    needed = report.missing_needed
    if needed:
        print(f"RESULT: INCOMPLETE: {len(needed)} blob(s) missing from every source; see {rc.data_dir / 'missing.json'}", file=sys.stderr)
        return 1
    if report.decode and not report.decode.get("allArtifactsMatch"):
        print("RESULT: DECODED WITH ERRORS: a digest did not match; see recovery-report.json", file=sys.stderr)
        return 1
    unneeded = len(report.missing) - len(needed)
    note = f" ({unneeded} blob(s) of an abandoned upload unavailable; no reader needs them)" if unneeded else ""
    print(f"RESULT: SUCCESS: {report.blobs_present} blobs verified, every digest matches{note}; files are in {rc.out_dir}")
    return 0


def discard_typed_ahead(terminal) -> None:
    """Throw away whatever was typed at the terminal before the question was shown. A publish
    can plan for an hour before it asks, and an Enter pressed in that time to see whether the
    terminal is alive would otherwise be read as the answer, and Enter alone means yes."""
    if not terminal.isatty():
        return
    try:
        import termios
    except ImportError:
        # Windows has no termios; its console keeps the keys typed ahead in its own buffer.
        import msvcrt

        while msvcrt.kbhit():
            msvcrt.getwch()
        return
    termios.tcflush(terminal.fileno(), termios.TCIFLUSH)


def ask_to_continue(statement: str, answers, shown) -> bool:
    """Put a statement to a person and ask whether to go on: `shown` is where they read it,
    `answers` where they type. Enter alone means yes; anything that is neither yes nor no is
    asked again; only what is typed after the question appeared counts."""
    discard_typed_ahead(answers)
    print(statement, file=shown)
    while True:
        print("Continue? (Y/n) ", end="", file=shown, flush=True)
        answer = answers.readline()
        if not answer:
            # The input ended without an answer: nobody said yes.
            print(file=shown)
            return False
        answer = answer.strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def confirm_at_terminal(statement: str) -> bool:
    """The question a publish asks before it sends its batch, put to the person at the
    terminal. Answers are typed on standard input, so that must be a terminal. The question
    is written where it will be seen: on standard error when that is the terminal, and
    otherwise (standard error going to a log file or a pipe) on the terminal itself, because
    a question that lands in a log leaves the command waiting on someone who cannot see it.
    With nobody to ask it says so and stops the batch, having shown what was not sent."""
    from loculus_eternal.upload.upload import NobodyToAsk

    nobody = NobodyToAsk("publishing asks for confirmation and there is no terminal to ask on; run it in a terminal, or pass --yes to confirm in advance")
    if sys.stdin is None or not sys.stdin.isatty():
        print(statement, file=sys.stderr)
        raise nobody
    if sys.stderr is not None and sys.stderr.isatty():
        return ask_to_continue(statement, sys.stdin, sys.stderr)
    try:
        terminal = open("/dev/tty", "w")
    except OSError:
        print(statement, file=sys.stderr)
        raise nobody from None
    with terminal:
        return ask_to_continue(statement, sys.stdin, terminal)


def cmd_upload(args: argparse.Namespace) -> int:
    from loculus_eternal.upload.upload import Uploader

    cfg = configuration.load(args.config)
    mode = "check" if args.check else "dry-run" if args.dry_run else "publish"
    # A publish asks the maintainer to confirm the batch it has planned, unless --yes
    # answered in advance. Whether there is a terminal to ask on is found out only when the
    # question comes up: a run that finishes an interrupted batch, or finds nothing to
    # publish, never asks and so needs neither a terminal nor --yes.
    confirm = None if args.yes or mode != "publish" else confirm_at_terminal
    try:
        # Construction already talks to the endpoint (for the chain id), so it is inside the try.
        if mode == "check" and not args.with_key:
            uploader = Uploader(cfg)
        else:
            uploader = Uploader.with_key(cfg, configuration.publisher_key())
        report = uploader.run(mode, confirm=confirm)
    except (RequestException, ChainError) as e:
        # The endpoint stayed down or rate-limited through every retry. Whatever was sent is
        # in the journal; the next run resumes from it.
        print(f"the RPC endpoint failed: {e}\nnothing is lost: run the same command again to resume", file=sys.stderr)
        return 3
    if report.outcome in ("published", "nothing-to-publish", "checked", "dry-run-ok", "resumed"):
        return 0
    if report.outcome == "failed":
        print(f"publishing stopped: {report.message}", file=sys.stderr)
        return 3
    print(report.message, file=sys.stderr)
    return 1


def cmd_health(args: argparse.Namespace) -> int:
    from loculus_eternal.health import HealthCheck
    from loculus_eternal.health_server import HealthServer

    cfg = configuration.load(args.config)
    if args.once:
        report = HealthCheck(cfg, log=lambda m: print(m, file=sys.stderr)).run()
        print(json.dumps(report.to_json(), indent=1, default=str))
        return 0 if report.verdict == "recoverable" else 1
    server = HealthServer(cfg, host=args.host, port=args.port)
    print(f"health page for {cfg.chain.contract} at {server.url} (Ctrl-C to stop)")
    if not args.no_open:
        import webbrowser

        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Progress lines must reach a pipe or a log file as they happen, not when a buffer fills.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(prog="loculus-eternal", description="Permanent, verifiable backup of Pathoplexus released data in Ethereum blobs.")
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("upload", help="make the published record hold exactly the eligible entries of the database, as one batch; a no-op when it already does")
    up.add_argument("--config", type=Path, required=True, help="TOML config file (see the README)")
    up.add_argument("--check", action="store_true", help="report how many entries await publication and the estimated cost; send nothing")
    up.add_argument("--dry-run", action="store_true", help="do everything except send: encode, simulate, check fees and balance")
    up.add_argument("--with-key", action="store_true", help="with --check, also read the key so the wallet balance is reported")
    up.add_argument("--yes", action="store_true", help="answer yes in advance to the question a publish asks before it sends its batch (see --check first for what the batch would do); needed when there is no terminal")
    up.set_defaults(func=cmd_upload)

    health = sub.add_parser("health", help="serve a one-page health check of the published record for anyone to verify")
    health.add_argument("--config", type=Path, required=True, help="TOML config file naming the contract, the chain RPC and the sources")
    health.add_argument("--port", type=int, default=8765)
    health.add_argument("--host", default="127.0.0.1")
    health.add_argument("--once", action="store_true", help="run one check, print the report as JSON, exit 0 only if the dataset is recoverable")
    health.add_argument("--no-open", action="store_true", help="do not open a browser")
    health.set_defaults(func=cmd_health)

    rec = sub.add_parser("recover", help="rebuild the dataset from the contract address and the configured sources")
    rec.add_argument("--config", type=Path, required=True, help="TOML config file (see README)")
    rec.add_argument("--skip-decode", action="store_true", help="fetch and verify blobs only; do not materialise the dataset")
    rec.set_defaults(func=cmd_recover)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
