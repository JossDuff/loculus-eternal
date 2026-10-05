"""IPFS: CID rules against real Kubo nodes, snapshot publishing from the upload command,
and recovery from IPFS alone."""

import hashlib
import json
import zstandard
import pytest

from loculus_eternal import config as configuration
from loculus_eternal.format.chunks import pack_blobs
from loculus_eternal.ipfs import KuboClient, app_pointer, blob_cid, cid_bytes, cid_v1, parse_manifest, publish_blobs, CODEC_RAW
from loculus_eternal.recover import ManifestSource, Recovery, RecoveryConfig
from loculus_eternal.sources import IpfsSource
from loculus_eternal.sources.base import BlobContext, SourceChain, SourceError
from loculus_eternal.testkit import Anvil, ArchiveStub, BackendStub, preconditions_met, released_line
from loculus_eternal.testkit.kubo import Kubo, kubo_available
from loculus_eternal.upload.upload import Uploader

pytestmark = pytest.mark.skipif(not (kubo_available() and preconditions_met()), reason="Docker (for Kubo), anvil and forge are needed")


@pytest.fixture(scope="module")
def kubo_pair():
    with Kubo() as a, Kubo() as b:
        yield KuboClient(a.api_url), KuboClient(b.api_url)


def test_I1_blob_object_cid_is_computable_locally_and_kubo_agrees(kubo_pair):
    a, _ = kubo_pair
    blob = pack_blobs(b"loculus eternal " * 5000)[0]
    local = blob_cid(blob)
    assert local == cid_v1(CODEC_RAW, hashlib.sha256(blob).digest())
    assert local.startswith("bafkrei")
    assert publish_blobs(a, [blob]) == [local]
    assert a.block_get(local) == blob and a.is_pinned(local)
    assert len(cid_bytes(local)) == 36 and cid_bytes(local)[:4] == bytes([0x01, 0x55, 0x12, 0x20])
    assert app_pointer(local) == hashlib.sha256(cid_bytes(local)).digest()


def test_I2_snapshot_directory_cid_is_the_same_on_two_nodes(kubo_pair):
    a, b = kubo_pair
    files = {"manifest.json": b'{\n "x": 1\n}\n', "container-spec.md": b"# spec\n", "zika.ndjson.zst": zstandard.ZstdCompressor(level=19).compress(b'{"a":1}\n' * 50000)}
    ca, cb = a.add_directory("snapshot", files), b.add_directory("snapshot", files)
    assert ca == cb and ca.startswith("bafybei")
    assert a.cat(f"{ca}/manifest.json") == files["manifest.json"]
    assert zstandard.ZstdDecompressor().decompress(b.cat(f"{ca}/zika.ndjson.zst")) == b'{"a":1}\n' * 50000
    # A different file set is a different directory, and the name of the directory does not matter.
    assert a.add_directory("elsewhere", files) == ca
    assert a.add_directory("snapshot", {**files, "extra": b"x"}) != ca


# --- through the upload command ------------------------------------------------------------------


@pytest.fixture
def world(tmp_path, kubo_pair):
    import threading

    a, b = kubo_pair

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

    with Anvil() as anvil, ArchiveStub() as archive, BackendStub() as backend:
        miner = Miner(anvil)
        miner.t.start()
        try:
            anvil.mine(3)
            repo = tmp_path / "repo"
            (repo / "docs").mkdir(parents=True)
            (repo / "docs" / "container-spec.md").write_text("# Container specification (sample)\n")
            cfg_path = repo / "loculus-eternal.toml"
            cfg_path.write_text(
                f"""
[chain]
rpc_url = "{anvil.url}"
contract = "{anvil.contract.address}"
chain_id = {anvil.chain_id}
beacon_genesis_time = 0
seconds_per_slot = 1
deployment_block = 1

[backend]
url = "{backend.url}"
organisms = ["zika", "mpox"]

[upload]
data_dir = "{tmp_path / 'upload-data'}"
tooling_paths = ["docs/container-spec.md"]
inclusion_timeout_blocks = 3
finality_timeout_seconds = 60

[ipfs]
endpoints = ["{a.api_url}", "{b.api_url}"]
spec_path = "docs/container-spec.md"

[[sources]]
type = "beacon"
endpoints = ["{archive.url}"]
"""
            )
            backend.set_lines("zika", [released_line("zika", "PP_1", 1), released_line("zika", "PP_2", 1)])
            backend.set_lines("mpox", [released_line("mpox", "PP_3", 1)])
            yield {"anvil": anvil, "archive": archive, "backend": backend, "cfg_path": cfg_path, "tmp": tmp_path, "kubo": (a, b)}
        finally:
            miner.stop.set()


def uploader(world):
    return Uploader(configuration.load(world["cfg_path"]), account=world["anvil"].publisher, log=lambda s: None, poll_interval=0.1)


def test_I3_upload_publishes_blob_objects_and_a_snapshot_the_chain_points_at(world):
    anvil = world["anvil"]
    a, b = world["kubo"]
    report = uploader(world).run()
    assert report.outcome == "published", report.message
    snap = report.ipfs
    assert snap["snapshotCid"] and all(e["snapshot"] == "ok" for e in snap["endpoints"]) and all(e["blobs"] == report.batch["blobs"] for e in snap["endpoints"])
    assert bytes(anvil.contract.functions.appPointer().call()) == bytes.fromhex(snap["appPointer"][2:]) == app_pointer(snap["snapshotCid"])
    manifest = parse_manifest(a.cat(f"{snap['snapshotCid']}/manifest.json"))
    assert manifest["blobCount"] == report.batch["blobCountAfter"] and manifest["head"] == "0x" + bytes(anvil.contract.functions.head().call()).hex()
    assert [b_["seq"] for b_ in manifest["blobs"]] == list(range(manifest["blobCount"]))
    for entry in manifest["blobs"]:
        blob = b.block_get(entry["cid"])
        assert blob_cid(blob) == entry["cid"]
    assert set(snap["files"]) == {"manifest.json", "container-spec.md", "mpox.ndjson.zst", "zika.ndjson.zst"}
    zika = zstandard.ZstdDecompressor().decompress(a.cat(f"{snap['snapshotCid']}/zika.ndjson.zst"), max_output_size=10**8)
    assert [json.loads(l)["metadata"]["accessionVersion"] for l in zika.splitlines()] == ["PP_1.1", "PP_2.1"]
    assert (world["tmp"] / "upload-data" / "snapshot-cid.txt").read_text().strip() == snap["snapshotCid"]


def test_I4_recovery_from_ipfs_alone_with_logs_unavailable(world, tmp_path):
    from web3 import Web3

    from loculus_eternal.testkit import FlakyProvider

    anvil = world["anvil"]
    a, _ = world["kubo"]
    first = uploader(world).run()
    assert first.outcome == "published"
    world["backend"].add("zika", released_line("zika", "PP_1", 2))
    second = uploader(world).run()
    assert second.outcome == "published" and second.ipfs["snapshotCid"] != first.ipfs["snapshotCid"]
    cid = second.ipfs["snapshotCid"]

    source = IpfsSource([a.api_url], cid)
    flaky = FlakyProvider(Web3.HTTPProvider(anvil.url), fail_methods={"eth_getLogs"}, fail_count=10**6)
    cfg = RecoveryConfig(rpc_url=anvil.url, contract=anvil.contract.address, data_dir=tmp_path / "rec-data", out_dir=tmp_path / "rec-out", sources=[source], manifest_sources=[ManifestSource(ipfs=source)], deployment_block=1, log=lambda s: None)
    report = Recovery(cfg, w3=Web3(flaky)).run()
    assert report.manifest_source == f"IPFS snapshot {cid}" and report.missing == []
    assert report.decode["allArtifactsMatch"]
    recovered = (tmp_path / "rec-out" / "zika.ndjson").read_bytes()
    assert [json.loads(l)["metadata"]["accessionVersion"] for l in recovered.splitlines()] == ["PP_1.1", "PP_1.2", "PP_2.1"]
    # The snapshot's compressed file decompresses to exactly the recovered file.
    assert zstandard.ZstdDecompressor().decompress(a.cat(f"{cid}/zika.ndjson.zst"), max_output_size=10**8) == recovered


def test_I5_unreachable_ipfs_does_not_block_publishing_and_keeps_the_pointer(world):
    anvil = world["anvil"]
    first = uploader(world).run()
    assert first.outcome == "published"
    pointer_before = bytes(anvil.contract.functions.appPointer().call())
    a, b = world["kubo"]
    text = world["cfg_path"].read_text().replace(f'endpoints = ["{a.api_url}", "{b.api_url}"]', 'endpoints = ["http://127.0.0.1:9", "http://127.0.0.1:10"]')
    world["cfg_path"].write_text(text)
    world["backend"].add("mpox", released_line("mpox", "PP_9", 1))
    second = uploader(world).run()
    assert second.outcome == "published", second.message
    assert second.ipfs["snapshotCid"] is None and all(e["snapshot"].startswith("error") for e in second.ipfs["endpoints"])
    assert bytes(anvil.contract.functions.appPointer().call()) == pointer_before
    world["cfg_path"].write_text(text + "\n")
    required = text.replace("spec_path", "required = true\nspec_path")
    world["cfg_path"].write_text(required)
    world["backend"].add("mpox", released_line("mpox", "PP_10", 1))
    third = uploader(world).run()
    assert third.outcome == "refused" and "required" in third.message


def test_I6_snapshot_that_does_not_match_the_pointer_is_refused(world):
    anvil = world["anvil"]
    a, _ = world["kubo"]
    report = uploader(world).run()
    assert report.outcome == "published"
    impostor = a.add_directory("snapshot", {"manifest.json": a.cat(f"{report.ipfs['snapshotCid']}/manifest.json"), "container-spec.md": b"tampered\n"})
    source = IpfsSource([a.api_url], impostor)
    source.set_app_pointer(bytes(anvil.contract.functions.appPointer().call()))
    with pytest.raises(SourceError, match="appPointer"):
        source.blob_refs()
    result = SourceChain([source]).acquire(BlobContext(1, 1), [bytes(32)])
    assert result.attempts[0].outcome == "error" and "appPointer" in result.attempts[0].detail
    genuine = IpfsSource([a.api_url], report.ipfs["snapshotCid"])
    genuine.set_app_pointer(bytes(anvil.contract.functions.appPointer().call()))
    assert len(genuine.blob_refs()) == report.batch["blobCountAfter"]
