"""The recovery command end to end against anvil: manifest from logs or file, every source but
one disabled, faults, RPC flaps, interruption and resume, and the command line."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from web3 import Web3

from loculus_eternal import kzg
from loculus_eternal.chain import BlobRef, ChainReader, ManifestMismatch, chain_head, verify_manifest
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import StreamEncoder
from loculus_eternal.format.gen_vectors import genesis_entries, second_batch_entries, tooling
from loculus_eternal.format.records import CODEC_ZSTD
from loculus_eternal.recover import ManifestSource, Recovery, RecoveryConfig
from loculus_eternal.sources import BeaconSource, BlobArchiverSource, BlobscanSource, LocalDirectorySource
from loculus_eternal.store import BlobStore
from loculus_eternal.testkit import Anvil, ArchiveStub, FlakyProvider, preconditions_met, publish_blobs

pytestmark = pytest.mark.skipif(not preconditions_met(), reason="anvil and forge are needed")


@pytest.fixture(scope="module")
def published():
    """A chain with two batches published, an archive stub holding every blob, and the
    materialised files the encoder produced, for comparison."""
    with Anvil() as anvil, ArchiveStub() as stub:
        contract = bytes.fromhex(anvil.contract.address[2:])
        enc = StreamEncoder(anvil.chain_id, contract)
        b0 = enc.encode_batch(genesis_entries(), tooling=tooling(), codec=CODEC_ZSTD)
        dec0 = StreamDecoder(b0.blobs).decode()
        b1 = enc.encode_batch(second_batch_entries(), previous_entries=dec0.payloads, codec=CODEC_ZSTD, force_index=True)
        all_blobs = []
        for batch in (b0, b1):
            for receipt, hashes in publish_blobs(anvil, batch.blobs, last_blob_chunk_count=batch.last_blob_chunk_count, is_batch_end=True):
                slot = anvil.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
                by_hash = {kzg.blob_to_versioned_hash(b): b for b in batch.blobs}
                stub.add(slot, [(h, by_hash[h]) for h in hashes])
                all_blobs.extend(by_hash[h] for h in hashes)
        anvil.mine(2)
        expected = StreamDecoder(all_blobs).decode()
        yield {"anvil": anvil, "stub": stub, "blobs": all_blobs, "expected": expected, "deployment_block": 1}


def config(published, tmp_path, sources, **kw) -> RecoveryConfig:
    anvil = published["anvil"]
    return RecoveryConfig(
        rpc_url=anvil.url,
        contract=anvil.contract.address,
        data_dir=tmp_path / "data",
        out_dir=tmp_path / "out",
        sources=sources,
        deployment_block=published["deployment_block"],
        log=lambda s: None,
        **kw,
    )


def assert_recovered_matches(published, out_dir: Path):
    expected = published["expected"]
    for org in expected.entries:
        assert (out_dir / f"{org}.ndjson").read_bytes() == expected.materialise(org)
    report = json.loads((out_dir / "recovery-report.json").read_text())
    assert report["allArtifactsMatch"] and report["torn"] == []
    assert [b["batch"] for b in report["batches"]] == [0, 1]


@pytest.mark.parametrize("which", ["beacon", "blobscan", "blob-archiver", "local"])
def test_R9_recovery_from_each_single_source_is_byte_identical(published, tmp_path, which):
    stub = published["stub"]
    if which == "local":
        d = tmp_path / "local"
        d.mkdir()
        for b in published["blobs"]:
            (d / LocalDirectorySource.filename(kzg.blob_to_versioned_hash(b))).write_bytes(b)
        source = LocalDirectorySource(d)
    elif which == "beacon":
        source = BeaconSource([stub.url], genesis_time=0, seconds_per_slot=1)
    elif which == "blobscan":
        source = BlobscanSource(stub.url)
    else:
        source = BlobArchiverSource(stub.url, genesis_time=0, seconds_per_slot=1)
    report = Recovery(config(published, tmp_path, [source])).run()
    assert report.manifest_source == "event logs"
    assert report.missing == [] and report.blobs_present == report.blobs_total == len(published["blobs"])
    assert_recovered_matches(published, tmp_path / "out")


def test_R1_manifest_from_logs_is_verified_and_a_bad_file_is_rejected_first(published, tmp_path):
    anvil = published["anvil"]
    reader = ChainReader(anvil.w3, anvil.contract.address)
    state = reader.state_at_finalized()
    refs = reader.blob_refs_from_logs(0, state.block_number)
    assert len(refs) == state.blob_count and chain_head(r.versioned_hash for r in refs) == state.head
    verify_manifest(refs, state)
    # Tampered lists are rejected: a swapped pair, a dropped entry, a changed hash.
    swapped = [BlobRef(0, refs[1].versioned_hash), BlobRef(1, refs[0].versioned_hash)] + refs[2:]
    with pytest.raises(ManifestMismatch):
        verify_manifest(swapped, state)
    with pytest.raises(ManifestMismatch):
        verify_manifest(refs[:-1], state)
    with pytest.raises(ManifestMismatch):
        verify_manifest([BlobRef(0, bytes(32))] + refs[1:], state)
    # A bad manifest file comes first in the config; the logs still rescue the run.
    bad = tmp_path / "bad-manifest.json"
    bad.write_text(json.dumps({"blobs": [r.to_json() for r in refs[:-1]]}))
    cfg = config(published, tmp_path, [BlobscanSource(published["stub"].url)], manifest_sources=[ManifestSource(file=bad), ManifestSource(logs=True)])
    report = Recovery(cfg).run()
    assert report.manifest_source == "event logs" and len(report.manifest_errors) == 1
    assert report.missing == []


def test_R10_manifest_from_file_alone_works_when_logs_are_unavailable(published, tmp_path):
    """The history-expiry path: a handed-over manifest verifies against head, and the run
    never asks the node for logs."""
    anvil = published["anvil"]
    reader = ChainReader(anvil.w3, anvil.contract.address)
    state = reader.state_at_finalized()
    refs = reader.blob_refs_from_logs(0, state.block_number)
    handed_over = tmp_path / "manifest.json"
    handed_over.write_text(json.dumps({"blobs": [r.to_json() for r in refs]}))
    flaky = FlakyProvider(Web3.HTTPProvider(anvil.url), fail_methods={"eth_getLogs"}, fail_count=10**6)
    w3 = Web3(flaky)
    cfg = config(published, tmp_path, [BlobscanSource(published["stub"].url)], manifest_sources=[ManifestSource(file=handed_over)])
    report = Recovery(cfg, w3=w3).run()
    assert report.manifest_source == f"file {handed_over}"
    assert "eth_getLogs" not in flaky.calls
    assert report.missing == []
    assert_recovered_matches(published, tmp_path / "out")


def test_R4_missing_blob_is_reported_with_every_source_tried_and_rest_is_recovered(published, tmp_path):
    stub = published["stub"]
    victim = kzg.blob_to_versioned_hash(published["blobs"][-1])
    stub.withhold.add(victim)
    try:
        cfg = config(published, tmp_path, [BeaconSource([stub.url], genesis_time=0, seconds_per_slot=1), BlobscanSource(stub.url)])
        report = Recovery(cfg).run()
    finally:
        stub.withhold.discard(victim)
    assert len(report.missing) == 1
    m = report.missing[0]
    assert m["versionedHash"] == "0x" + victim.hex() and m["seq"] == len(published["blobs"]) - 1
    assert [t["source"].split("(")[0] for t in m["tried"]] == ["beacon", "blobscan"]
    assert report.blobs_present == len(published["blobs"]) - 1
    missing_file = json.loads((tmp_path / "data" / "missing.json").read_text())
    assert len(missing_file) == 1
    # The decode step ran on what there is: the last batch is torn, the first is intact.
    decode = report.decode
    assert [b["batch"] for b in decode["batches"]] == [0] and len(decode["torn"]) == 1
    assert "zika" in decode["files"]


def test_R2_corrupting_source_first_still_recovers_from_the_second(published, tmp_path):
    stub = published["stub"]
    victim = kzg.blob_to_versioned_hash(published["blobs"][0])
    stub.corrupt.add(victim)
    try:
        d = tmp_path / "local"
        d.mkdir()
        (d / LocalDirectorySource.filename(victim)).write_bytes(published["blobs"][0])
        cfg = config(published, tmp_path, [BlobscanSource(stub.url), LocalDirectorySource(d)])
        report = Recovery(cfg).run()
    finally:
        stub.corrupt.discard(victim)
    assert report.candidates_rejected == 1 and report.missing == []
    assert_recovered_matches(published, tmp_path / "out")


def test_R5_rpc_flaps_mid_run_are_retried(published, tmp_path):
    anvil = published["anvil"]
    # Four consecutive failures on the first chain read: the fifth attempt succeeds, and the
    # reader backs off between attempts instead of hammering the node.
    flaky = FlakyProvider(Web3.HTTPProvider(anvil.url), fail_methods={"eth_getLogs", "eth_getBlockByNumber", "eth_call"}, fail_count=4)
    w3 = Web3(flaky)
    cfg = config(published, tmp_path, [BlobscanSource(published["stub"].url)])
    reader_sleeps = []
    recovery = Recovery(cfg, w3=w3)
    recovery.reader.sleep = reader_sleeps.append
    report = recovery.run()
    assert flaky.failures == 4 and report.missing == []
    assert len(reader_sleeps) == 4 and reader_sleeps == sorted(reader_sleeps)
    assert report.manifest_source == "event logs"
    assert_recovered_matches(published, tmp_path / "out")


def test_R12_log_paging_halves_on_error_and_covers_the_range(published):
    anvil = published["anvil"]
    flaky = FlakyProvider(Web3.HTTPProvider(anvil.url), fail_methods={"eth_getLogs"}, fail_count=3)
    reader = ChainReader(Web3(flaky), anvil.contract.address, max_page=8)
    state = reader.state_at_finalized()
    refs = reader.blob_refs_from_logs(0, state.block_number)
    verify_manifest(refs, state)
    assert flaky.calls.count("eth_getLogs") > 3


def test_R6_interrupted_run_resumes_without_refetching(published, tmp_path):
    stub = published["stub"]
    source = BlobscanSource(stub.url)
    # First run: the second half of the blobs is withheld, so the store ends up partial.
    second_half = [kzg.blob_to_versioned_hash(b) for b in published["blobs"][len(published["blobs"]) // 2 :]]
    stub.withhold.update(second_half)
    try:
        first = Recovery(config(published, tmp_path, [source], decode=False)).run()
    finally:
        stub.withhold.difference_update(second_half)
    assert 0 < first.blobs_present < first.blobs_total
    before = len(stub.requests)
    second = Recovery(config(published, tmp_path, [source])).run()
    assert second.missing == [] and second.blobs_present == second.blobs_total
    assert second.blobs_fetched == first.blobs_total - first.blobs_present, "only the missing blobs were fetched"
    requested = [r for r in stub.requests[before:] if "/blobs/0x" in r]
    assert len(requested) == second.blobs_fetched
    assert_recovered_matches(published, tmp_path / "out")


def test_R7_decode_report_lists_batches_files_digests_and_tooling(published, tmp_path):
    report = Recovery(config(published, tmp_path, [BlobscanSource(published["stub"].url)])).run()
    d = report.decode
    assert d["header"]["chainId"] == published["anvil"].chain_id
    assert d["batches"][1]["hasIndex"] is True
    assert set(d["files"]) == set(published["expected"].entries)
    assert all(f["matchesManifest"] for f in d["files"].values())
    assert d["tooling"] == sorted(p for p, _ in tooling())


def test_R8_a_recovered_store_serves_another_recovery(published, tmp_path):
    first = Recovery(config(published, tmp_path / "a", [BlobscanSource(published["stub"].url)], decode=False)).run()
    assert first.missing == []
    with BlobStore(tmp_path / "a" / "data") as store:
        store.export_blobs(tmp_path / "handoff")
    second = Recovery(config(published, tmp_path / "b", [LocalDirectorySource(tmp_path / "handoff")])).run()
    assert second.missing == []
    assert_recovered_matches(published, tmp_path / "b" / "out")


def test_R13_command_line_runs_from_a_toml_config(published, tmp_path):
    anvil, stub = published["anvil"], published["stub"]
    cfg = tmp_path / "recover.toml"
    cfg.write_text(
        f"""
[chain]
rpc_url = "{anvil.url}"
contract = "{anvil.contract.address}"
chain_id = {anvil.chain_id}
beacon_genesis_time = 0
seconds_per_slot = 1
deployment_block = 1

[recover]
data_dir = "{tmp_path / 'data'}"
out_dir = "{tmp_path / 'out'}"

[[sources]]
type = "beacon"
endpoints = ["{stub.url}"]

[[sources]]
type = "blobscan"
url = "{stub.url}"
"""
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    proc = subprocess.run([sys.executable, "-m", "loculus_eternal.cli", "recover", "--config", str(cfg)], capture_output=True, text=True, env=env)
    assert proc.returncode == 0, proc.stderr + proc.stdout
    assert "verified against head" in proc.stdout
    assert_recovered_matches(published, tmp_path / "out")
    run_report = json.loads((tmp_path / "data" / "run-report.json").read_text())
    assert run_report["missing"] == []
