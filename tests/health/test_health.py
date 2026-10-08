"""The health check and its page against anvil, an archive stub, a backend stub and Kubo."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from loculus_eternal import config as configuration
from loculus_eternal import kzg
from loculus_eternal.health import HealthCheck
from loculus_eternal.health_server import HealthServer
from loculus_eternal.testkit import Anvil, ArchiveStub, BackendStub, preconditions_met, released_line
from loculus_eternal.testkit.kubo import Kubo, kubo_available
from loculus_eternal.upload.upload import Uploader

pytestmark = pytest.mark.skipif(not (preconditions_met() and kubo_available()), reason="anvil, forge and Docker (for Kubo) are needed")
ROOT = Path(__file__).resolve().parents[2]


class Miner:
    def __init__(self, anvil):
        self.anvil, self.stop = anvil, threading.Event()
        self.t = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                self.anvil.mine(1)
            except Exception:
                pass
            self.stop.wait(0.2)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("health")
    with Anvil() as anvil, ArchiveStub() as archive, BackendStub() as backend, Kubo() as kubo:
        miner = Miner(anvil)
        miner.t.start()
        try:
            anvil.mine(3)
            repo = tmp / "repo"
            (repo / "docs").mkdir(parents=True)
            (repo / "docs" / "container-spec.md").write_text("# spec\n")
            cfg_path = repo / "loculus-eternal.toml"
            cfg_path.write_text(
                f"""
[chain]
rpc_url = "{anvil.url}"
contract = "{anvil.contract.address}"
chain_id = {anvil.chain_id}
max_requests_per_second = 1000
beacon_genesis_time = 0
seconds_per_slot = 1
deployment_block = 1

[backend]
url = "{backend.url}"
organisms = "all"

[upload]
data_dir = "{tmp / 'upload-data'}"
tooling_paths = ["docs/container-spec.md"]
inclusion_timeout_blocks = 3
finality_timeout_seconds = 60

[ipfs]
endpoints = ["{kubo.api_url}"]
spec_path = "docs/container-spec.md"

[[sources]]
type = "beacon"
endpoints = ["{archive.url}"]

[[sources]]
type = "blobscan"
url = "{archive.url}"

[[sources]]
type = "ipfs"
endpoints = ["{kubo.api_url}"]
"""
            )
            backend.set_lines("zika", [released_line("zika", "PP_1", 1), released_line("zika", "PP_2", 1), released_line("zika", "PP_3", 1, open_terms=False)])
            backend.set_lines("mpox", [released_line("mpox", "PP_4", 1)])
            report = Uploader(configuration.load(cfg_path), account=anvil.publisher, log=lambda s: None, poll_interval=0.1).run()
            assert report.outcome == "published", report.message
            # The archive now has the blobs, as the network would.
            from loculus_eternal.store import BlobStore

            with BlobStore(tmp / "upload-data" / "stream") as store:
                for tx in report.transactions:
                    archive.add(tx["blockTimestamp"], [(bytes.fromhex(vh[2:]), store.read_blob(seq)) for seq, vh in zip(tx["seqs"], tx["versionedHashes"])])
            snapshot_cid = report.ipfs["snapshotCid"]
            # A third party's config: the same deployment, the published snapshot, no key needed.
            viewer_cfg = tmp / "viewer.toml"
            viewer_cfg.write_text(cfg_path.read_text().replace(f'type = "ipfs"\nendpoints = ["{kubo.api_url}"]\n', f'type = "ipfs"\nendpoints = ["{kubo.api_url}"]\nsnapshot_cid = "{snapshot_cid}"\n'))
            anvil.mine(3)
            yield {"anvil": anvil, "archive": archive, "backend": backend, "cfg": viewer_cfg, "snapshot": snapshot_cid, "report": report, "tmp": tmp}
        finally:
            miner.stop.set()


def check(world, cfg_path=None):
    return HealthCheck(configuration.load(cfg_path or world["cfg"]), log=lambda s: None).run()


def test_H1_a_sound_deployment_is_recoverable(world):
    r = check(world)
    assert r.verdict == "recoverable", r.problems
    assert r.chain["blobCount"] == world["report"].batch["blobCountAfter"] and r.blob_list["verified"]
    assert r.sources["verifiedFromAnySource"] == r.sources["blobs"] and r.sources["unavailable"] == []
    assert set(r.sources["matrix"]) and all(m["corrupt"] == 0 for m in r.sources["matrix"].values())
    assert r.stream["batches"] == 1 and r.stream["entriesTotal"] == 3 and set(r.stream["organisms"]) == {"mpox", "zika"}
    assert not hasattr(r, "ipfs"), "IPFS is checked as a blob source, not separately"
    assert r.sources["needed"] == r.sources["blobs"] and all(m["complete"] and m["reachable"] for m in r.sources["matrix"].values())
    assert any(k.startswith("ipfs") and m["verified"] == r.sources["blobs"] for k, m in r.sources["matrix"].items())
    assert not hasattr(r, "backend"), "the check judges the record on its own, never against the live database"
    assert r.finished_at and r.log


def test_H2_a_needed_blob_nobody_serves_makes_it_not_recoverable_and_names_it(world):
    archive = world["archive"]
    victim_seq = world["report"].batch["blobCountAfter"] - 1
    vh = bytes.fromhex(world["report"].transactions[-1]["versionedHashes"][-1][2:])
    archive.withhold.add(vh)
    # The IPFS copy still has it, so first prove it still passes thanks to IPFS...
    r = check(world)
    assert r.verdict == "recoverable" and r.sources["matrix"][[k for k in r.sources["matrix"] if k.startswith("beacon")][0]]["missing"] == 1
    # ...then take IPFS out of the picture: now no source serves it.
    cfg = world["tmp"] / "no-ipfs.toml"
    text = world["cfg"].read_text()
    cut = text.index('[[sources]]\ntype = "ipfs"')
    cfg.write_text(text[:cut])
    try:
        r = check(world, cfg)
    finally:
        archive.withhold.discard(vh)
    assert r.verdict == "not recoverable"
    assert any(f"[{victim_seq}]" in p for p in r.problems)
    assert r.sources["unavailable"] == [victim_seq]
    assert r.stream["torn"], "the batch holding the missing blob is reported torn"


def test_H3_a_corrupting_source_is_noted_but_does_not_fail_the_check(world):
    archive = world["archive"]
    vh = bytes.fromhex(world["report"].transactions[0]["versionedHashes"][0][2:])
    archive.corrupt.add(vh)
    try:
        r = check(world)
    finally:
        archive.corrupt.discard(vh)
    assert r.verdict == "recoverable"
    assert any("corrupt" in n for n in r.notes)
    assert sum(m["corrupt"] for m in r.sources["matrix"].values()) >= 1


def test_H4_a_snapshot_that_is_not_the_publishers_is_a_problem(world):
    from loculus_eternal.ipfs import KuboClient

    a = KuboClient(configuration.load(world["cfg"]).ipfs.endpoints[0])
    impostor = a.add_directory("snapshot", {"manifest.json": a.cat(f"{world['snapshot']}/manifest.json"), "container-spec.md": b"tampered\n"})
    cfg = world["tmp"] / "impostor.toml"
    cfg.write_text(world["cfg"].read_text().replace(world["snapshot"], impostor))
    r = check(world, cfg)
    assert r.verdict == "recoverable", "a stale CID is not a loss of the data"
    ipfs_row = next(m for k, m in r.sources["matrix"].items() if k.startswith("ipfs"))
    assert not ipfs_row["complete"] and ipfs_row["verified"] == 0 and "appPointer" in ipfs_row.get("lastError", "")


def test_H5_the_server_serves_the_page_and_runs_checks(world):
    server = HealthServer(configuration.load(world["cfg"]), port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        page = httpx.get(f"{server.url}/")
        assert page.status_code == 200 and "Loculus Eternal health" in page.text and "<script>" in page.text
        assert "github.com/JossDuff/loculus-eternal" in page.text and "Secured by Ethereum" in page.text and "loculus-eternal recover --config" in page.text
        idle = httpx.get(f"{server.url}/api/report").json()
        assert idle["verdict"] == "idle" and idle["config"]["contract"] == world["anvil"].contract.address
        assert httpx.post(f"{server.url}/api/check").status_code == 202
        deadline = time.time() + 120
        while time.time() < deadline:
            r = httpx.get(f"{server.url}/api/report").json()
            if not r["running"] and r["verdict"] != "idle":
                break
            time.sleep(0.2)
        assert r["verdict"] == "recoverable", r.get("problems")
        assert r["log"] and r["stream"]["entriesTotal"] == 3
        assert r["progress"] == {}, "a finished report carries no progress"
        assert httpx.get(f"{server.url}/nothing").status_code == 404
    finally:
        server.shutdown()


def test_H7_the_page_never_shows_a_credential_from_a_url(world, tmp_path):
    from loculus_eternal.health_server import public_config, public_url

    assert public_url("https://sepolia.infura.io/v3/0123456789abcdef") == "https://sepolia.infura.io"
    assert public_url("https://archive.example.com:8443/path?key=secret") == "https://archive.example.com:8443"
    assert public_url("http://127.0.0.1:5001") == "http://127.0.0.1:5001"
    text = world["cfg"].read_text().replace("[[sources]]", "[[sources]]\ntype = \"blobscan\"\nurl = \"https://api.example.com/v1/SECRETKEY\"\n\n[[sources]]", 1)
    cfg_path = tmp_path / "keyed.toml"
    cfg_path.write_text(text)
    cfg = configuration.load(cfg_path)
    public = public_config(cfg)
    dumped = json.dumps(public)
    assert "SECRETKEY" not in dumped and public["contract"] == cfg.chain.contract and public["network"]
    assert any(s["type"] == "blobscan" and s["endpoints"] == ["https://api.example.com"] for s in public["sources"])
    assert any(s["type"] == "ipfs" and s["snapshotCid"] == world["snapshot"] for s in public["sources"])
    assert "backend" not in public and public["repository"].startswith("https://github.com/")
    # The check reports its steps in order while it runs, with detail during the long fetch.
    seen = []
    HealthCheck(configuration.load(world["cfg"]), log=lambda s: None, progress=lambda p: seen.append((p["step"], p["detail"]))).run()
    steps = [st for st, _ in seen]
    assert [st for i, st in enumerate(steps) if i == 0 or st != steps[i - 1]] == ["contract check", "blob check"]
    assert any(d and "blobs asked for" in d for _, d in seen) and not any(d and "backend" in d for _, d in seen)


def test_H6_once_prints_json_and_exits_by_verdict(world):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    proc = subprocess.run([sys.executable, "-m", "loculus_eternal.cli", "health", "--config", str(world["cfg"]), "--once"], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["verdict"] == "recoverable" and report["chain"]["blobCount"] > 0
    assert "reading the contract" in proc.stderr
