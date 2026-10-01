"""Real blob transactions against anvil, and the beacon-shaped stub."""

import json

import httpx
import pytest

from loculus_eternal import kzg
from loculus_eternal.format.decode import StreamDecoder
from loculus_eternal.format.encode import StreamEncoder
from loculus_eternal.format.gen_vectors import genesis_entries, second_batch_entries, tooling
from loculus_eternal.format.records import CODEC_RAW, CODEC_ZSTD
from web3.logs import DISCARD

from loculus_eternal.testkit import Anvil, BeaconStub, preconditions_met, publish_blobs

pytestmark = pytest.mark.skipif(not preconditions_met(), reason="anvil and forge are needed for harness tests")


@pytest.fixture(scope="module")
def anvil():
    with Anvil() as a:
        yield a


def chain_head(hashes: list[bytes]) -> bytes:
    from eth_utils import keccak

    h = bytes(32)
    for vh in hashes:
        h = keccak(h + vh)
    return h


def test_K4_anvil_starts_with_contract_deployed_and_finality_two_behind(anvil):
    assert anvil.contract.functions.publisher().call() == anvil.publisher.address
    assert anvil.contract.functions.blobCount().call() == 0
    anvil.mine(3)
    latest = anvil.w3.eth.block_number
    assert anvil.finalized_block_number() == latest - 2
    before = anvil.w3.eth.get_block("latest")["timestamp"]
    anvil.warp(100)
    assert anvil.w3.eth.get_block("latest")["timestamp"] >= before + 100


def test_K5_real_blob_transaction_records_the_right_versioned_hashes(anvil):
    """The contract, fed by a real type-3 transaction, records exactly the hashes computed
    off-chain from the blob bytes."""
    enc = StreamEncoder(anvil.chain_id, bytes.fromhex(anvil.contract.address[2:]))
    batch = enc.encode_batch(genesis_entries(), tooling=tooling(), codec=CODEC_RAW)
    results = publish_blobs(anvil, batch.blobs, last_blob_chunk_count=batch.last_blob_chunk_count, is_batch_end=True, app_pointer=b"\x11" * 32)
    assert len(results) == 1
    receipt, hashes = results[0]
    assert receipt["status"] == 1 and receipt["type"] == 3
    assert hashes == [kzg.blob_to_versioned_hash(b) for b in batch.blobs]

    events = anvil.contract.events.BlobPublished().process_receipt(receipt, errors=DISCARD)
    assert [e["args"]["seq"] for e in events] == [0]
    assert [bytes(e["args"]["versionedHash"]) for e in events] == hashes
    commits = anvil.contract.events.BatchCommitted().process_receipt(receipt, errors=DISCARD)
    assert len(commits) == 1 and commits[0]["args"]["lastBlobChunkCount"] == batch.last_blob_chunk_count
    assert anvil.contract.functions.blobCount().call() == 1
    assert bytes(anvil.contract.functions.head().call()) == chain_head(hashes)
    assert bytes(anvil.contract.functions.appPointer().call()) == b"\x11" * 32


def test_K5_multi_blob_batch_spans_transactions_and_keeps_order():
    from loculus_eternal.format.gen_vectors import sample_entry

    with Anvil() as fresh:
        enc = StreamEncoder(fresh.chain_id, bytes.fromhex(fresh.contract.address[2:]))
        big = [sample_entry("mpox", f"PP_00090{i}", 1, seq_len=110000) for i in range(4)]  # about 0.9 MB raw: seven blobs
        batch = enc.encode_batch(big, codec=CODEC_RAW)
        assert len(batch.blobs) > 6
        results = publish_blobs(fresh, batch.blobs, last_blob_chunk_count=batch.last_blob_chunk_count, is_batch_end=True)
        assert len(results) == -(-len(batch.blobs) // 6)
        all_hashes = [h for _, hs in results for h in hs]
        assert all_hashes == [kzg.blob_to_versioned_hash(b) for b in batch.blobs]
        assert fresh.contract.functions.blobCount().call() == len(batch.blobs)
        # Only the last transaction committed the batch.
        commits = [len(fresh.contract.events.BatchCommitted().process_receipt(r, errors=DISCARD)) for r, _ in results]
        assert commits == [0] * (len(results) - 1) + [1]


def test_K6_beacon_stub_serves_by_slot_with_filter_and_forgets():
    blob_a = b"\x00" + b"\x01" * 31
    blob_a = (blob_a * 4096)
    blob_b = (b"\x00" + b"\x02" * 31) * 4096
    vh_a, vh_b = kzg.blob_to_versioned_hash(blob_a), kzg.blob_to_versioned_hash(blob_b)
    with BeaconStub() as stub:
        stub.register(1700, [(vh_a, blob_a), (vh_b, blob_b)])
        r = httpx.get(f"{stub.url}/eth/v1/beacon/blobs/1700")
        assert r.status_code == 200
        body = r.json()
        assert body["finalized"] is True and body["data"] == ["0x" + blob_a.hex(), "0x" + blob_b.hex()]
        r = httpx.get(f"{stub.url}/eth/v1/beacon/blobs/1700", params={"versioned_hashes": ["0x" + vh_b.hex()]})
        assert r.json()["data"] == ["0x" + blob_b.hex()]
        assert httpx.get(f"{stub.url}/eth/v1/beacon/blobs/1").status_code == 404
        assert httpx.get(f"{stub.url}/eth/v1/beacon/blobs/head").status_code == 400
        assert httpx.get(f"{stub.url}/eth/v1/beacon/blobs/%C2%B2").status_code == 400
        stub.forget(1700)
        assert httpx.get(f"{stub.url}/eth/v1/beacon/blobs/1700").status_code == 404


def test_K7_end_to_end_publish_vectors_read_events_decode_from_stub():
    """Publish two batches through real transactions, rebuild the blob list from events,
    check the chain head, fetch the bytes from the stub by slot, and decode."""
    with Anvil() as fresh, BeaconStub() as stub:
        contract = bytes.fromhex(fresh.contract.address[2:])
        enc = StreamEncoder(fresh.chain_id, contract)
        b0 = enc.encode_batch(genesis_entries(), tooling=tooling(), codec=CODEC_ZSTD)
        dec0 = StreamDecoder(b0.blobs).decode()
        b1 = enc.encode_batch(second_batch_entries(), previous_entries=dec0.payloads, codec=CODEC_ZSTD, force_index=True)

        published = {}
        for batch in (b0, b1):
            for receipt, hashes in publish_blobs(fresh, batch.blobs, last_blob_chunk_count=batch.last_blob_chunk_count, is_batch_end=True):
                slot = fresh.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
                offset = [kzg.blob_to_versioned_hash(b) for b in batch.blobs]
                pairs = [(h, batch.blobs[offset.index(h)]) for h in hashes]
                stub.register(slot, pairs)
                for h, b in pairs:
                    published[h] = (slot, b)
        fresh.mine(2)

        # Rebuild the manifest from events and verify it against head.
        logs = fresh.contract.events.BlobPublished().get_logs(from_block=0, to_block="finalized")
        ordered = sorted(logs, key=lambda e: e["args"]["seq"])
        hashes = [bytes(e["args"]["versionedHash"]) for e in ordered]
        assert [e["args"]["seq"] for e in ordered] == list(range(len(hashes)))
        assert len(hashes) == fresh.contract.functions.blobCount().call()
        assert chain_head(hashes) == bytes(fresh.contract.functions.head().call())

        # Fetch every blob from the stub by its slot, verify, decode.
        blobs = []
        for e in ordered:
            slot = fresh.w3.eth.get_block(e["blockNumber"])["timestamp"]
            vh = bytes(e["args"]["versionedHash"])
            r = httpx.get(f"{stub.url}/eth/v1/beacon/blobs/{slot}", params={"versioned_hashes": ["0x" + vh.hex()]})
            (data,) = r.json()["data"]
            blob = bytes.fromhex(data[2:])
            assert kzg.verify_blob(blob, vh)
            blobs.append(blob)
        dec = StreamDecoder(blobs).decode()
        assert not dec.torn and [b.batch for b in dec.batches] == [0, 1]
        assert dec.header.chain_id == fresh.chain_id and dec.header.contract == contract
        assert all(dec.verify_artifacts().values())
        assert dec.batches[1].index is not None
        assert dec.tooling == dict(tooling())
