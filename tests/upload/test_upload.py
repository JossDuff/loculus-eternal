"""The upload command end to end on anvil: genesis, no-op reruns, deltas, published set from
the chain, dry-run refusals, journal resume, fee escalation, torn batches, and the CLI."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from loculus_eternal import config as configuration
from loculus_eternal import kzg
from loculus_eternal.recover import Recovery, RecoveryConfig
from loculus_eternal.sources import BeaconSource
from loculus_eternal.testkit import Anvil, ArchiveStub, BackendStub, preconditions_met, released_line
from loculus_eternal.testkit.anvil import dev_account
from loculus_eternal.upload.submitter import Refused
from loculus_eternal.upload.upload import Uploader

pytestmark = pytest.mark.skipif(not preconditions_met(), reason="anvil and forge are needed")

ROOT = Path(__file__).resolve().parents[2]


def bulky_line(organism: str, accession: str) -> dict:
    """A released line whose sequence is pseudo-random, so it stays large after zstd: nine of
    these make a batch of more than six blobs, which is what the multi-transaction tests need."""
    import random

    rng = random.Random(accession)
    seq = "".join(rng.choice("ACGT") for _ in range(400_000))
    line = released_line(organism, accession, 1)
    line["unalignedNucleotideSequences"] = {"main": seq}
    line["alignedNucleotideSequences"] = {"main": seq}
    return line


class Miner:
    """Mines a block every few hundred milliseconds so finality (latest minus two) advances."""

    def __init__(self, anvil, interval=0.2):
        self.anvil, self.interval, self._stop = anvil, interval, threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.anvil.mine(1)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)


@pytest.fixture
def world(tmp_path):
    with Anvil() as anvil, ArchiveStub() as archive, BackendStub() as backend, Miner(anvil):
        anvil.mine(3)  # the deployment must be final before the first run reads the contract
        tooling_dir = tmp_path / "repo"
        (tooling_dir / "docs").mkdir(parents=True)
        (tooling_dir / "docs" / "container-spec.md").write_text("# spec\n")
        (tooling_dir / "pyproject.toml").write_text('[project]\nname = "loculus-eternal"\nversion = "0.0.1"\n')
        cfg_path = tooling_dir / "loculus-eternal.toml"
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
organisms = ["zika", "mpox"]

[upload]
data_dir = "{tmp_path / 'upload-data'}"
tooling_paths = ["docs/container-spec.md", "pyproject.toml"]
max_blob_fee_gwei = 5
inclusion_timeout_blocks = 3
escalation_attempts = 4
finality_timeout_seconds = 60

[recover]
data_dir = "{tmp_path / 'recovery-data'}"
out_dir = "{tmp_path / 'recovered'}"

[[sources]]
type = "beacon"
endpoints = ["{archive.url}"]
"""
        )
        backend.set_lines("zika", [released_line("zika", "PP_1", 1), released_line("zika", "PP_2", 1), released_line("zika", "PP_3", 1, open_terms=False)])
        backend.set_lines("mpox", [released_line("mpox", "PP_4", 1, extra={"clade": "IIb"})])
        yield {"anvil": anvil, "archive": archive, "backend": backend, "cfg_path": cfg_path, "tmp": tmp_path}


def uploader(world, **kw) -> Uploader:
    cfg = configuration.load(world["cfg_path"])
    return Uploader(cfg, account=world["anvil"].publisher, log=lambda s: None, poll_interval=0.1, **kw)


def register_in_archive(world, report):
    """After a publish, imitate the network: the archive now has those blobs by slot."""
    data_dir = Path(configuration.load(world["cfg_path"]).upload.data_dir)
    from loculus_eternal.store import BlobStore

    with BlobStore(data_dir / "stream") as store:
        for tx in report.transactions:
            pairs = [(bytes.fromhex(vh[2:]), store.read_blob(seq)) for seq, vh in zip(tx["seqs"], tx["versionedHashes"])]
            world["archive"].add(tx["blockTimestamp"], pairs)


def recover(world, tmp_path):
    cfg = configuration.load(world["cfg_path"])
    rc = RecoveryConfig(rpc_url=cfg.chain.rpc_url, contract=cfg.chain.contract, data_dir=tmp_path / "rec-data", out_dir=tmp_path / "rec-out", sources=cfg.sources, deployment_block=1, max_requests_per_second=1000, log=lambda s: None)
    return Recovery(rc).run()


def test_U2_genesis_then_noop_then_delta_published_from_chain_derived_set(world, tmp_path):
    first = uploader(world).run()
    assert first.outcome == "published" and first.new_entries == 3
    assert first.tooling_published and first.batch["batch"] == 0 and first.batch["schemasPublished"] == ["mpox", "zika"]
    assert first.chain["blobCount"] == 0 and world["anvil"].contract.functions.blobCount().call() == first.batch["blobCountAfter"]
    assert all(t["isBatchEnd"] == (i == len(first.transactions) - 1) for i, t in enumerate(first.transactions))
    register_in_archive(world, first)

    second = uploader(world).run()
    assert second.outcome == "nothing-to-publish" and second.transactions == []
    assert second.chain["blobCount"] == first.batch["blobCountAfter"]

    # New data arrives and a restricted entry opens; a fresh data directory (as on another
    # machine) must still see what is published, by reading the chain and the stream.
    world["backend"].add("zika", released_line("zika", "PP_1", 2))
    world["backend"].open_entry("zika", "PP_3.1")
    new_dir = tmp_path / "other-machine"
    cfg_text = world["cfg_path"].read_text().replace(str(tmp_path / "upload-data"), str(new_dir))
    world["cfg_path"].write_text(cfg_text)
    third = uploader(world).run()
    assert third.outcome == "published" and third.new_entries == 2
    assert third.batch["batch"] == 1 and third.batch["firstBlobSeq"] == first.batch["blobCountAfter"]
    assert not third.tooling_published, "same version: tooling is not republished"
    register_in_archive(world, third)

    report = recover(world, tmp_path)
    assert report.missing == [] and report.decode["allArtifactsMatch"]
    zika = [json.loads(l) for l in (tmp_path / "rec-out" / "zika.ndjson").read_text().splitlines()]
    assert [m["metadata"]["accessionVersion"] for m in zika] == ["PP_1.1", "PP_1.2", "PP_2.1", "PP_3.1"]
    assert report.decode["tooling"] == ["docs/container-spec.md", "pyproject.toml"]
    assert [b["batch"] for b in report.decode["batches"]] == [0, 1]


def test_U3_check_and_dry_run_send_nothing(world):
    anvil = world["anvil"]
    checked = Uploader(configuration.load(world["cfg_path"]), log=lambda s: None).run("check")
    assert checked.outcome == "checked" and checked.new_entries == 3 and checked.batch["blobs"] >= 1
    assert anvil.contract.functions.blobCount().call() == 0
    dry = uploader(world).run("dry-run")
    assert dry.outcome == "dry-run-ok" and dry.dry_run["transactions"] == 1
    assert dry.dry_run["gasEstimate"] > 21000 and dry.dry_run["maxCostWei"] <= dry.dry_run["balanceWei"]
    assert anvil.contract.functions.blobCount().call() == 0
    data_dir = Path(configuration.load(world["cfg_path"]).upload.data_dir)
    assert not (data_dir / "journal.json").exists() and list((data_dir / "abandoned").glob("*.json"))
    # A dry run sent nothing, so its blob files are not kept: only the journal of what it planned.
    assert not list((data_dir / "abandoned").rglob("*.blob"))


def test_U4_dry_run_refuses_wrong_key_low_balance_and_high_blob_fee(world):
    anvil = world["anvil"]
    cfg = configuration.load(world["cfg_path"])
    stranger = dev_account(5)
    anvil.fund(stranger.address, 10**20)
    wrong = Uploader(cfg, account=stranger, log=lambda s: None, poll_interval=0.1).run()
    assert wrong.outcome == "refused" and "not the contract's publisher" in wrong.message

    saved = anvil.w3.eth.get_balance(anvil.publisher.address)
    anvil.fund(anvil.publisher.address, 10**12)
    poor = uploader(world).run()
    assert poor.outcome == "refused" and "holds" in poor.message
    anvil.fund(anvil.publisher.address, saved)

    world["cfg_path"].write_text(world["cfg_path"].read_text().replace("max_blob_fee_gwei = 5", "max_blob_fee_gwei = 0"))
    expensive = uploader(world).run()
    assert expensive.outcome == "refused" and "blob base fee" in expensive.message
    assert anvil.contract.functions.blobCount().call() == 0


def test_U5_interrupted_multi_transaction_batch_resumes_from_the_journal(world, tmp_path):
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_B{i}") for i in range(9)])  # more than six blobs: two transactions
    up = uploader(world)

    def crash_after_first(tx):
        if tx.index == 0:
            raise KeyboardInterrupt("simulated crash right after the first transaction was sent")

    up.submitter.after_send = crash_after_first
    with pytest.raises(KeyboardInterrupt):
        up.run()
    data_dir = up.data_dir
    journal = json.loads((data_dir / "journal.json").read_text())
    assert [t["status"] for t in journal["txs"]] == ["sent", "planned"]
    time.sleep(0.5)
    assert anvil.contract.functions.blobCount().call() == 6, "the first transaction landed before the crash"

    resumed = uploader(world).run()
    assert resumed.outcome in ("resumed", "nothing-to-publish") and len(resumed.transactions) == 2
    assert [t["attempts"] for t in resumed.transactions] == [1, 1]
    assert anvil.contract.functions.blobCount().call() == resumed.chain["blobCount"] and anvil.contract.functions.blobCount().call() > 6
    assert not (data_dir / "journal.json").exists() and list((data_dir / "done").glob("*.json"))
    register_in_archive(world, resumed)
    report = recover(world, tmp_path)
    assert report.missing == [] and report.decode["allArtifactsMatch"] and report.decode["torn"] == []
    assert report.decode["files"]["mpox"]["entries"] == 9


def test_U6_unincluded_transaction_is_replaced_with_higher_fees(world):
    up = uploader(world)
    up.submitter.drop_sends = 2
    report = up.run()
    assert report.outcome == "published", report.message
    assert report.transactions[0]["attempts"] == 3
    done = json.loads(next((up.data_dir / "done").glob("*.json")).read_text())
    fees = [a["max_fee_per_blob_gas"] for a in done["txs"][0]["attempts"]]
    assert fees == sorted(fees) and fees[1] > fees[0] and fees[2] > fees[1]
    gas_fees = [a["max_fee_per_gas"] for a in done["txs"][0]["attempts"]]
    assert gas_fees[2] > gas_fees[1] > gas_fees[0]
    limits = [a["gas_limit"] for a in done["txs"][0]["attempts"]]
    assert limits[0] and limits == [limits[0]] * 3


def test_U7_blobs_enter_the_local_store_only_after_finality(world):
    up = uploader(world)
    seen = []

    def spy(tx):
        store_dir = up.data_dir / "stream"
        seen.append((store_dir / "have.json").exists() and json.loads((store_dir / "have.json").read_text()))

    up.submitter.after_send = spy
    report = up.run()
    assert report.outcome == "published"
    assert seen == [False] or seen == [{}], "nothing in the store when the transaction was merely sent"
    have = json.loads((up.data_dir / "stream" / "have.json").read_text())
    assert sorted(int(k) for k in have) == list(range(report.batch["blobCountAfter"]))
    tx = report.transactions[-1]
    assert tx["block"] <= world["anvil"].finalized_block_number()


def test_U8_torn_batch_with_lost_journal_is_skipped_and_the_stream_stays_decodable(world, tmp_path):
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_T{i}") for i in range(9)])
    up = uploader(world)

    def crash(tx):
        if tx.index == 0:
            raise KeyboardInterrupt

    up.submitter.after_send = crash
    with pytest.raises(KeyboardInterrupt):
        up.run()
    time.sleep(0.5)
    assert anvil.contract.functions.blobCount().call() == 6
    # The journal and pending blobs are lost (new machine, wiped directory).
    lost = tmp_path / "lost"
    world["cfg_path"].write_text(world["cfg_path"].read_text().replace(str(tmp_path / "upload-data"), str(lost)))
    # The torn blobs must be obtainable from the network for the published view; the old
    # journal tells us which block they landed in, as the archive would know anyway.
    journal = json.loads((up.data_dir / "journal.json").read_text())
    receipt = anvil.w3.eth.get_transaction_receipt(journal["txs"][0]["attempts"][0]["tx_hash"])
    slot = anvil.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    pairs = [(bytes.fromhex(vh[2:]), (Path(journal["blobs_dir"]) / f"{seq}.blob").read_bytes()) for seq, vh in zip(journal["txs"][0]["seqs"], journal["txs"][0]["versioned_hashes"])]
    world["archive"].add(slot, pairs)

    fresh = uploader(world).run()
    assert fresh.outcome == "published" and fresh.chain["tornBlobs"] == 6
    assert fresh.batch["batch"] == 0 and fresh.batch["firstBlobSeq"] == 6
    assert anvil.contract.functions.blobCount().call() == 6 + fresh.batch["blobs"]
    assert "6 of them from an abandoned upload" in fresh.message
    register_in_archive(world, fresh)
    report = recover(world, tmp_path)
    assert report.missing == []
    assert [t["first_blob"] for t in report.decode["torn"]] == [0] and report.decode["torn"][0]["last_blob"] == 5
    assert report.decode["allArtifactsMatch"] and report.decode["files"]["mpox"]["entries"] == 9

    # The network forgets the torn blobs, all but blob 0 which holds the stream header.
    with world["archive"]._lock:
        for vh, _ in pairs[1:]:
            del world["archive"].blobs[vh]
    again = recover(world, tmp_path / "second")
    assert sorted(m["seq"] for m in again.missing) == [1, 2, 3, 4, 5] and all(m["needed"] is False for m in again.missing)
    assert again.missing_needed == [] and again.decode["allArtifactsMatch"] and again.decode["files"]["mpox"]["entries"] == 9
    # A later upload from yet another machine still learns the published set.
    world["cfg_path"].write_text(world["cfg_path"].read_text().replace(str(lost), str(tmp_path / "third")))
    world["backend"].add("mpox", released_line("mpox", "PP_T_LATE", 1))
    later = uploader(world).run()
    assert later.outcome == "published" and later.new_entries == 1 and later.batch["batch"] == 1


def test_U9_another_upload_in_between_is_detected_before_sending(world):
    anvil = world["anvil"]
    up = uploader(world)
    # Someone else appends a blob between planning and the dry run.
    original_dry_run = up.submitter.dry_run

    def race_then_dry_run(journal):
        from loculus_eternal.format.chunks import pack_blobs
        from loculus_eternal.testkit import publish_blobs

        publish_blobs(anvil, pack_blobs(b"\x05" * 1000), last_blob_chunk_count=33, is_batch_end=True)
        return original_dry_run(journal)

    up.submitter.dry_run = race_then_dry_run
    report = up.run()
    assert report.outcome == "refused" and "blob count moved" in report.message
    assert anvil.contract.functions.blobCount().call() == 1
    assert not (up.data_dir / "journal.json").exists()


def test_U10_command_line_publishes_with_the_key_from_the_environment(world, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    env.pop(configuration.PUBLISHER_KEY_ENV, None)
    cmd = [sys.executable, "-m", "loculus_eternal.cli", "upload", "--config", str(world["cfg_path"])]
    missing = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert missing.returncode != 0 and configuration.PUBLISHER_KEY_ENV in missing.stderr
    env[configuration.PUBLISHER_KEY_ENV] = world["anvil"].publisher.key.hex()
    checked = subprocess.run(cmd + ["--check"], capture_output=True, text=True, env=env)
    assert checked.returncode == 0 and "planned batch 0" in checked.stdout and "estimated cost" in checked.stdout
    published = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert published.returncode == 0, published.stderr
    assert "published batch 0" in published.stdout
    again = subprocess.run(cmd, capture_output=True, text=True, env=env)
    assert again.returncode == 0 and "nothing to publish" in again.stdout
    assert world["anvil"].contract.functions.blobCount().call() >= 1


def test_U11_tooling_is_republished_when_the_version_changes(world):
    first = uploader(world).run()
    assert first.tooling_published
    register_in_archive(world, first)
    world["backend"].add("zika", released_line("zika", "PP_7", 1))
    same = uploader(world).run()
    assert same.outcome == "published" and not same.tooling_published
    register_in_archive(world, same)
    (world["cfg_path"].parent / "pyproject.toml").write_text('[project]\nname = "loculus-eternal"\nversion = "0.0.2"\n')
    world["backend"].add("zika", released_line("zika", "PP_8", 1))
    bumped = uploader(world).run()
    assert bumped.outcome == "published" and bumped.tooling_published


def test_U12_batch_end_carries_the_existing_pointer_forward(world):
    anvil = world["anvil"]
    pointer = b"\x42" * 32
    tx = anvil.contract.functions.setAppPointer(pointer).build_transaction({"from": anvil.publisher.address, "nonce": anvil.w3.eth.get_transaction_count(anvil.publisher.address), "chainId": anvil.chain_id})
    anvil.w3.eth.wait_for_transaction_receipt(anvil.w3.eth.send_raw_transaction(anvil.publisher.sign_transaction(tx).raw_transaction))
    anvil.mine(3)
    report = uploader(world).run()
    assert report.outcome == "published"
    assert bytes(anvil.contract.functions.appPointer().call()) == pointer


def test_U3_check_refuses_while_an_interrupted_batch_is_waiting(world):
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_C{i}") for i in range(9)])
    up = uploader(world)

    def crash(tx):
        if tx.index == 0:
            raise KeyboardInterrupt

    up.submitter.after_send = crash
    with pytest.raises(KeyboardInterrupt):
        up.run()
    checked = Uploader(configuration.load(world["cfg_path"]), log=lambda s: None).run("check")
    assert checked.outcome == "refused" and "interrupted batch" in checked.message
    dry = uploader(world).run("dry-run")
    assert dry.outcome == "refused" and "interrupted batch" in dry.message
    resumed = uploader(world).run()
    assert resumed.outcome in ("resumed", "nothing-to-publish") and len(resumed.transactions) == 2


def test_U5_lost_broadcast_response_does_not_send_a_second_copy(world):
    """The node accepts the transaction but the response never arrives: the journal already
    holds the attempt, so the resume waits for that hash instead of using a new nonce."""
    anvil = world["anvil"]
    up = uploader(world)
    real_send = up.w3.eth.send_raw_transaction

    def send_then_lose_response(raw):
        real_send(raw)
        raise ConnectionError("response lost")

    up.w3.eth.send_raw_transaction = send_then_lose_response
    report = up.run()
    assert report.outcome == "published", report.message
    assert report.transactions[0]["attempts"] == 1
    assert anvil.w3.eth.get_transaction_count(anvil.publisher.address) == 1 + 0  # one publish, nothing else from this key
    assert anvil.contract.functions.blobCount().call() == report.batch["blobCountAfter"]


def test_U5_finished_journal_left_by_a_crash_is_completed_not_an_error(world):
    up = uploader(world)
    original_finish = up._finish

    def crash_before_bookkeeping(journal, store, report):
        raise KeyboardInterrupt

    up._finish = crash_before_bookkeeping
    with pytest.raises(KeyboardInterrupt):
        up.run()
    journal = json.loads((up.data_dir / "journal.json").read_text())
    assert all(t["status"] == "finalized" for t in journal["txs"])
    again = uploader(world).run()
    assert again.outcome in ("resumed", "nothing-to-publish") and len(again.transactions) == 1
    assert not (up.data_dir / "journal.json").exists()
    have = json.loads((up.data_dir / "stream" / "have.json").read_text())
    assert len(have) == journal["blob_count_after"]


def test_U9_another_upload_mid_batch_sets_the_batch_aside(world):
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_R{i}") for i in range(9)])
    up = uploader(world)

    def race_after_first(tx):
        # Between our two transactions, someone else appends a blob: our second transaction
        # is already signed with the old expected sequence and will revert when mined.
        if tx.index == 0:
            from loculus_eternal.format.chunks import pack_blobs
            from loculus_eternal.testkit import publish_blobs

            time.sleep(0.5)
            publish_blobs(anvil, pack_blobs(b"\x09" * 500), last_blob_chunk_count=17, is_batch_end=True)

    stranger_results = []

    def race_after_first_and_record(tx):
        if tx.index == 0:
            from loculus_eternal.format.chunks import pack_blobs
            from loculus_eternal.testkit import publish_blobs

            time.sleep(0.5)
            blobs = pack_blobs(b"\x09" * 500)
            stranger_results.append((blobs, publish_blobs(anvil, blobs, last_blob_chunk_count=17, is_batch_end=True)))

    up.submitter.after_send = race_after_first_and_record
    # The other upload uses the same publisher key, so it consumes the nonce our second
    # transaction was going to use; the submitter notices and sets the batch aside.
    report = up.run()
    assert report.outcome == "failed" and "another upload ran" in report.message, report.message
    assert not (up.data_dir / "journal.json").exists()
    aside = list((up.data_dir / "abandoned").glob("*.json"))
    assert aside

    # The torn blobs (ours and the stranger's) are on chain; the next run needs them from a
    # source. The archive would have them from the network; here we hand them over.
    journal = json.loads(aside[0].read_text())
    first = journal["txs"][0]
    receipt = anvil.w3.eth.get_transaction_receipt(first["included_tx"])
    slot = anvil.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
    from loculus_eternal import kzg as _kzg

    assert not Path(journal["blobs_dir"]).exists(), "abandoning removes the pending blob files"
    for blobs, results in stranger_results:
        s_receipt, s_hashes = results[0]
        s_slot = anvil.w3.eth.get_block(s_receipt["blockNumber"])["timestamp"]
        world["archive"].add(s_slot, list(zip(s_hashes, blobs)))
    # Our own six blobs are gone with the pending files; regenerate them deterministically
    # from the same inputs, exactly as the encoder did.
    from loculus_eternal.format.encode import StreamEncoder
    from loculus_eternal.format.records import CODEC_ZSTD
    from loculus_eternal.upload.sync import BackendClient, select_new_entries

    client = BackendClient(world["backend"].url, up.data_dir / "feeds")
    entries = []
    for org in ("zika", "mpox"):
        new, _ = select_new_entries(org, client.fetch(org), lambda a, v: False)
        entries.extend(new)
    cfg = configuration.load(world["cfg_path"])
    tooling_files = [(p.relative_to(cfg.path.parent).as_posix(), p.read_bytes()) for pat in cfg.upload.tooling_paths for p in sorted(cfg.path.parent.glob(pat))]
    regenerated = StreamEncoder(anvil.chain_id, bytes.fromhex(anvil.contract.address[2:])).encode_batch(entries, tooling=tooling_files, codec=CODEC_ZSTD)
    ours = [(_kzg.blob_to_versioned_hash(b), b) for b in regenerated.blobs[:6]]
    assert ["0x" + vh.hex() for vh, _ in ours] == first["versioned_hashes"], "regenerated blobs match what was published"
    world["archive"].add(slot, ours)

    fresh = uploader(world).run()
    assert fresh.outcome == "published", fresh.message
    assert fresh.chain["tornBlobs"] == 7 and fresh.batch["firstBlobSeq"] == 7


def test_U13_chain_id_mismatch_is_a_configuration_error(world):
    text = world["cfg_path"].read_text().replace(f"chain_id = {world['anvil'].chain_id}", "chain_id = 1")
    world["cfg_path"].write_text(text)
    with pytest.raises(SystemExit, match="chain_id 1"):
        uploader(world)


def test_U14_vanished_entries_are_reported_and_withdrawn_only_on_confirmation(world, tmp_path):
    anvil = world["anvil"]
    first = uploader(world).run()
    assert first.outcome == "published" and first.new_entries == 3
    register_in_archive(world, first)
    # The backend removes one published entry outright.
    world["backend"].set_lines("zika", [released_line("zika", "PP_2", 1), released_line("zika", "PP_3", 1, open_terms=False)])
    quiet = uploader(world).run()
    assert quiet.outcome == "nothing-to-publish" and quiet.vanished == {"zika": ["PP_1.1"]} and "withdraw-vanished" in quiet.message
    assert anvil.contract.functions.blobCount().call() == first.batch["blobCountAfter"], "nothing was published without confirmation"
    checked = Uploader(configuration.load(world["cfg_path"]), log=lambda s: None).run("check")
    assert checked.vanished == {"zika": ["PP_1.1"]}

    confirmed = uploader(world).run(withdraw_vanished=True)
    assert confirmed.outcome == "published", confirmed.message
    assert confirmed.withdrawn == {"zika": ["PP_1.1"]} and confirmed.batch["withdrawn"] == 1 and confirmed.new_entries == 0
    register_in_archive(world, confirmed)
    report = recover(world, tmp_path)
    assert report.missing == [] and report.decode["allArtifactsMatch"]
    zika = [json.loads(l)["metadata"]["accessionVersion"] for l in (tmp_path / "rec-out" / "zika.ndjson").read_text().splitlines()]
    assert zika == ["PP_2.1"] and report.decode["withdrawn"] == {"zika": {"PP_1": [1]}}
    assert report.decode["files"]["zika"]["withdrawn"] == 1
    # Once withdrawn, the entry is no longer "vanished": a further run has nothing to say.
    again = uploader(world).run()
    assert again.outcome == "nothing-to-publish" and again.vanished == {}


def test_U15_batch_transactions_are_pipelined_and_final_once(world, tmp_path):
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_P{i}") for i in range(14)])  # more than twelve blobs: three transactions
    up = uploader(world)
    up.submitter.policy.max_in_flight = 2
    sent_at = []

    def note(tx):
        sent_at.append((tx.index, tx.nonce, anvil.contract.functions.blobCount().call()))

    up.submitter.after_send = note
    report = up.run()
    assert report.outcome == "published", report.message
    assert len(report.transactions) == 3
    nonces = [n for _, n, _ in sent_at]
    assert nonces == [nonces[0], nonces[0] + 1, nonces[0] + 2], "consecutive nonces"
    # With a window of two, the second was sent before the first had to be included... on anvil
    # automine includes instantly, so what we can assert is ordering and that finality came once.
    blocks = [t["block"] for t in report.transactions]
    assert blocks == sorted(blocks)
    assert anvil.contract.functions.blobCount().call() == report.batch["blobCountAfter"]
    done = json.loads(next((up.data_dir / "done").glob("*.json")).read_text())
    assert all(t["status"] == "finalized" for t in done["txs"])
    register_in_archive(world, report)
    rec = recover(world, tmp_path)
    assert rec.missing == [] and rec.decode["allArtifactsMatch"]


def test_U16_all_organisms_follows_the_backend_and_reports_a_dropped_one(world, tmp_path):
    cfg = world["cfg_path"]
    cfg.write_text(cfg.read_text().replace('organisms = ["zika", "mpox"]', 'organisms = "all"'))
    first = uploader(world).run()
    assert first.outcome == "published" and sorted(s["organism"] for s in first.sync) == ["mpox", "zika"]
    register_in_archive(world, first)
    # The backend gains an organism: the next run publishes it with no config change.
    world["backend"].set_lines("hmpv", [released_line("hmpv", "PP_H1", 1)])
    second = uploader(world).run()
    assert second.outcome == "published" and second.new_entries == 1 and "hmpv" in {s["organism"] for s in second.sync}
    register_in_archive(world, second)
    # The backend drops an organism: its published entries are reported as vanished.
    with world["backend"]._lock:
        del world["backend"].lines["mpox"]
    third = uploader(world).run()
    assert third.outcome == "nothing-to-publish" and third.vanished == {"mpox": ["PP_4.1"]}
    # A backend that cannot enumerate organisms is a plain error before anything is read.
    world["backend"].enumerate_organisms = False
    fourth = uploader(world).run()
    assert fourth.outcome == "failed" and "list them in the config" in fourth.message


def test_U16_an_organism_left_out_of_a_configured_list_is_not_reported_vanished(world, tmp_path):
    first = uploader(world).run()
    assert first.outcome == "published" and sorted(s["organism"] for s in first.sync) == ["mpox", "zika"]
    register_in_archive(world, first)
    cfg = world["cfg_path"]
    cfg.write_text(cfg.read_text().replace('organisms = ["zika", "mpox"]', 'organisms = ["zika"]'))
    # mpox is still served and still published; the maintainer simply chose not to sync it.
    second = uploader(world).run(withdraw_vanished=True)
    assert second.outcome == "nothing-to-publish" and second.vanished == {} and second.withdrawn == {}
    assert [s["organism"] for s in second.sync] == ["zika"]


def test_U18_batch_end_transaction_waits_for_its_predecessors_and_gets_its_own_estimate(world, tmp_path):
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_E{i}") for i in range(9)])  # two transactions
    up = uploader(world)
    simulated = []
    original = up.submitter.simulate

    def spy(tx, fees):
        simulated.append((tx.index, [t["status"] for t in json.loads((up.data_dir / "journal.json").read_text())["txs"][: tx.index]] if (up.data_dir / "journal.json").exists() else []))
        return original(tx, fees)

    up.submitter.simulate = spy
    report = up.run()
    assert report.outcome == "published", report.message
    indices = [i for i, _ in simulated]
    assert indices.count(len(report.transactions) - 1) >= 1, "the batch-end transaction was simulated on its own"
    last_sim = [pred for i, pred in simulated if i == len(report.transactions) - 1][-1]
    assert all(s in ("included", "finalized") for s in last_sim), "and only after every predecessor was included"
    assert up.data_dir.joinpath("reports").exists()


def test_U19_a_revert_that_moved_nothing_is_resent_not_abandoned(world, tmp_path):
    """The batch-end transaction is sent with too little gas on purpose: it reverts on chain
    without touching the contract, and the submitter sends it again with a fresh estimate."""
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_G{i}") for i in range(9)])
    up = uploader(world)
    starved = []

    def starve_once(tx):
        if tx.is_batch_end and not starved:
            starved.append(tx.index)
            return 30_000  # enough to pass intrinsic checks, far too little to run
        return None

    up.submitter.gas_override = starve_once
    report = up.run()
    assert report.outcome == "published", report.message
    assert starved and report.transactions[-1]["attempts"] == 1
    done = json.loads(next((up.data_dir / "done").glob("*.json")).read_text())
    assert done["txs"][-1]["reverts"] == 1
    reverted = done["txs"][-1]["reverted_attempts"]
    assert len(reverted) == 1 and reverted[0]["gas_limit"] == 30_000 and reverted[0]["tx_hash"] != done["txs"][-1]["included_tx"]
    assert anvil.contract.functions.blobCount().call() == report.batch["blobCountAfter"]
    register_in_archive(world, report)
    rec = recover(world, tmp_path)
    assert rec.missing == [] and rec.decode["allArtifactsMatch"] and rec.decode["torn"] == []


def test_U19_a_mid_batch_revert_does_not_take_the_transactions_behind_it_down(world, tmp_path):
    """The second of four pipelined transactions is starved of gas. It reverts, and the third,
    already in flight, reverts on the sequence guard behind it (the fourth is the batch end
    and waits). The contract never moved, so both are sent again and the batch completes."""
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_M{i}") for i in range(9)])
    up = uploader(world)
    up.submitter.policy.max_blobs_per_transaction = 2  # eight or so blobs: four transactions
    starved = []

    def starve_second_once(tx):
        if tx.index == 1 and not starved:
            starved.append(tx.index)
            return 30_000
        return None

    up.submitter.gas_override = starve_second_once
    report = up.run()
    assert report.outcome == "published", report.message
    assert len(report.transactions) >= 4 and starved
    done = json.loads(next((up.data_dir / "done").glob("*.json")).read_text())
    assert [t["reverts"] for t in done["txs"][:3]] == [0, 1, 1] and all(t["status"] == "finalized" for t in done["txs"])
    assert anvil.contract.functions.blobCount().call() == report.batch["blobCountAfter"]
    register_in_archive(world, report)
    rec = recover(world, tmp_path)
    assert rec.missing == [] and rec.decode["allArtifactsMatch"] and rec.decode["torn"] == []


def test_U20_an_abandoned_batch_ends_the_run_and_keeps_its_blobs(world):
    """Another upload with the same key breaks the batch mid-way: the run reports failure,
    keeps the journal and blob files under abandoned/, and plans nothing more."""
    anvil = world["anvil"]
    world["backend"].set_lines("mpox", [bulky_line("mpox", f"PP_K{i}") for i in range(9)])
    up = uploader(world)

    def race(tx):
        if tx.index == 0:
            from loculus_eternal.format.chunks import pack_blobs
            from loculus_eternal.testkit import publish_blobs

            time.sleep(0.5)
            publish_blobs(anvil, pack_blobs(b"\x09" * 500), last_blob_chunk_count=17, is_batch_end=True)

    up.submitter.after_send = race
    report = up.run()
    assert report.outcome == "failed" and "another upload ran" in report.message
    aside = list((up.data_dir / "abandoned").glob("batch-*"))
    listing = {p.name: (sorted(q.name for q in p.iterdir()) if p.is_dir() else "file") for p in aside}
    assert any(p.suffix == ".json" for p in aside) and any(p.is_dir() and list(p.glob("*.blob")) for p in aside), f"journal and blob files kept: {listing}"
    assert not (up.data_dir / "journal.json").exists()
    assert report.batch is not None and len(list((up.data_dir / "abandoned").glob("*.json"))) == 1, "no second batch was planned in the same run"
