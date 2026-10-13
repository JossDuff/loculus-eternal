"""Blob sources against misbehaving servers, and the verify-before-write store."""

import json

import pytest

from loculus_eternal import kzg
from loculus_eternal.format.chunks import pack_blobs
from loculus_eternal.sources import BeaconSource, BlobArchiverSource, BlobscanSource, BlockscoutSource, LocalDirectorySource, SourceChain
from loculus_eternal.sources.base import BlobContext, SourceError
from loculus_eternal.store import BlobStore, StoreError, VerificationFailed
from loculus_eternal.testkit import ArchiveStub


def make_blobs(n: int) -> list[tuple[bytes, bytes]]:
    stream = b"".join(bytes([i]) * 126976 for i in range(1, n + 1))
    blobs = pack_blobs(stream)
    return [(kzg.blob_to_versioned_hash(b), b) for b in blobs]


@pytest.fixture
def stub():
    with ArchiveStub() as s:
        yield s


CTX = BlobContext(block_number=10, block_timestamp=1700)


def test_R2_corrupted_bytes_are_rejected_and_the_next_source_is_tried(stub):
    pairs = make_blobs(2)
    stub.add(1700, pairs)
    stub.corrupt.add(pairs[0][0])
    good_dir_source = _local_dir_with(pairs, pytest.importorskip("tempfile").mkdtemp())
    chain = SourceChain([BlobscanSource(stub.url), good_dir_source])
    result = chain.acquire(CTX, [vh for vh, _ in pairs])
    assert set(result.blobs) == {vh for vh, _ in pairs}
    assert result.rejected == 1
    assert [a.outcome for a in result.attempts] == ["partial", "ok"]
    assert result.blobs[pairs[0][0]] == pairs[0][1], "the corrupted copy was not the one kept"


def test_R3_wrong_blob_under_a_hash_is_not_accepted_for_it(stub):
    pairs = make_blobs(2)
    stub.add(1700, pairs)
    stub.swap[pairs[0][0]] = pairs[1][0]  # serves blob 1's bytes when asked for blob 0
    chain = SourceChain([BlobscanSource(stub.url)])
    result = chain.acquire(CTX, [pairs[0][0]])
    assert result.blobs == {} and result.rejected == 1
    assert result.missing([pairs[0][0]]) == [pairs[0][0]]


def test_R4_withholding_source_yields_nothing_and_everything_is_reported(stub):
    pairs = make_blobs(1)
    stub.add(1700, pairs)
    stub.withhold.add(pairs[0][0])
    chain = SourceChain([BlobscanSource(stub.url), BeaconSource([stub.url], genesis_time=0, seconds_per_slot=1)])
    result = chain.acquire(CTX, [pairs[0][0]])
    assert result.blobs == {}
    assert [a.source.split("(")[0] for a in result.attempts] == ["blobscan", "beacon"]
    assert all(a.outcome == "nothing" for a in result.attempts)


def test_R4_ipfs_manifest_failure_is_remembered_within_a_run_and_forgotten_at_the_next(tmp_path):
    from loculus_eternal.sources import IpfsSource
    from loculus_eternal.sources.base import BlobContext, SourceError

    cid = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
    src = IpfsSource(["http://127.0.0.1:1"], timeout=2)   # nothing listens there
    src.set_app_pointer(cid)
    with pytest.raises(SourceError, match="cannot read the snapshot manifest"):
        src.fetch(BlobContext(1, 1), [b"\x01" * 32])
    assert src._manifest_error, "the failure is kept so later block groups do not retry the node"
    with pytest.raises(SourceError, match="cannot read the snapshot manifest"):
        src.fetch(BlobContext(2, 2), [b"\x02" * 32])
    src.set_app_pointer(cid)   # the next run starts here
    assert src._manifest_error is None
    # A different pointer means a different snapshot: a cached manifest is dropped.
    src.manifest = {"blobs": []}
    src.set_app_pointer("bafy-another-snapshot")
    assert src.manifest is None


def test_R4_outage_is_an_error_attempt_not_a_crash(stub):
    pairs = make_blobs(1)
    stub.add(1700, pairs)
    stub.fail_next = 1
    chain = SourceChain([BlobscanSource(stub.url), BlobscanSource(stub.url)])
    result = chain.acquire(CTX, [pairs[0][0]])
    assert [a.outcome for a in result.attempts] == ["error", "ok"]
    assert set(result.blobs) == {pairs[0][0]}


def test_R11_every_adapter_shape_returns_the_same_verified_blob(stub, tmp_path):
    pairs = make_blobs(3)
    stub.add(1700, pairs)
    local = _local_dir_with(pairs[:1], tmp_path)
    wanted = [vh for vh, _ in pairs]
    for source in (
        BeaconSource([stub.url], genesis_time=0, seconds_per_slot=1),
        BlobArchiverSource(stub.url, genesis_time=0, seconds_per_slot=1),
        BlobscanSource(stub.url),
        BlockscoutSource(stub.url),
    ):
        result = SourceChain([source]).acquire(CTX, wanted)
        assert set(result.blobs) == set(wanted), source.name
        assert result.rejected == 0
    result = SourceChain([local]).acquire(CTX, wanted)
    assert set(result.blobs) == {pairs[0][0]}


def test_R11_a_blob_blockscout_has_indexed_but_not_archived_yet_is_simply_not_there(stub, tmp_path):
    pairs = make_blobs(2)
    stub.add(1700, pairs)
    stub.pending.add(pairs[0][0])
    wanted = [vh for vh, _ in pairs]
    result = SourceChain([BlockscoutSource(stub.url)]).acquire(CTX, wanted)
    assert set(result.blobs) == {pairs[1][0]} and result.rejected == 0
    # The next source fills the gap, as for any withheld blob.
    result = SourceChain([BlockscoutSource(stub.url), _local_dir_with(pairs, tmp_path)]).acquire(CTX, wanted)
    assert set(result.blobs) == set(wanted)


def test_R11_beacon_filter_is_sent_and_slot_is_derived_from_the_timestamp(stub):
    pairs = make_blobs(2)
    stub.add(1700, pairs)
    src = BeaconSource([stub.url], genesis_time=500, seconds_per_slot=12)
    ctx = BlobContext(block_number=1, block_timestamp=500 + 1700 * 12)
    result = SourceChain([src]).acquire(ctx, [pairs[1][0]])
    assert set(result.blobs) == {pairs[1][0]}
    assert any("/eth/v1/beacon/blobs/1700?" in r and "versioned_hashes=0x" + pairs[1][0].hex() in r for r in stub.requests)
    with pytest.raises(SourceError):
        src.fetch(BlobContext(block_number=1, block_timestamp=None), [pairs[0][0]])


def _local_dir_with(pairs, directory):
    from pathlib import Path

    d = Path(directory) / "blobs"
    d.mkdir(parents=True, exist_ok=True)
    for vh, blob in pairs:
        (d / LocalDirectorySource.filename(vh)).write_bytes(blob)
    return LocalDirectorySource(d)


# --- store ------------------------------------------------------------------------------------


def test_R2_store_writes_nothing_before_verification(tmp_path):
    pairs = make_blobs(2)
    with BlobStore(tmp_path / "store") as store:
        bad = bytearray(pairs[0][1])
        bad[100] ^= 1
        with pytest.raises(VerificationFailed):
            store.write(0, pairs[0][0], bytes(bad))
        assert store.count() == 0 and not store.has(0)
        assert (tmp_path / "store" / "chunks.dat").stat().st_size == 0
        store.write(1, pairs[1][0], pairs[1][1])
        assert store.has(1) and not store.has(0)
        assert store.read_blob(1) == pairs[1][1]
        assert store.blobs(2) == [None, pairs[1][1]]
        # Writing the same blob again is a no-op; a different hash for the same seq is refused.
        store.write(1, pairs[1][0], pairs[1][1])
        with pytest.raises(StoreError):
            store.write(1, pairs[0][0], pairs[0][1])


def test_R6_store_reopens_with_only_committed_blobs_and_exports_local_layout(tmp_path):
    pairs = make_blobs(3)
    d = tmp_path / "store"
    with BlobStore(d) as store:
        store.write(0, *pairs[0])
        store.write(2, *pairs[2])
    # A crash between the data write and the commit leaves bytes nobody trusts: simulate by
    # writing blob 1's region directly without updating have.json.
    with open(d / "chunks.dat", "r+b") as f:
        f.seek(126976)
        f.write(b"\x07" * 126976)
    with BlobStore(d) as store:
        assert sorted(store.have) == [0, 2]
        assert store.blobs(3)[1] is None
        assert store.verify_all() == []
        n = store.export_blobs(tmp_path / "export")
    assert n == 2
    assert LocalDirectorySource(tmp_path / "export").fetch(CTX, [pairs[2][0]]) == [pairs[2][1]]
    have = json.loads((d / "have.json").read_text())
    assert set(have) == {"0", "2"}


def test_R6_store_lock_is_exclusive(tmp_path):
    with BlobStore(tmp_path / "s"):
        with pytest.raises(StoreError, match="another process"):
            BlobStore(tmp_path / "s")


def test_R1_every_configured_ipfs_source_is_a_manifest_source_in_order(tmp_path):
    from loculus_eternal import config as configuration
    from loculus_eternal.cli import recovery_config
    from loculus_eternal.sources import IpfsSource

    cfg_path = tmp_path / "loculus-eternal.toml"
    cfg_path.write_text(
        """
[chain]
rpc_url = "http://127.0.0.1:1"
contract = "0x0000000000000000000000000000000000000001"
chain_id = 31337
beacon_genesis_time = 0

[[sources]]
type = "ipfs"
endpoints = ["http://127.0.0.1:5001"]

[[sources]]
type = "ipfs"
endpoints = ["http://127.0.0.1:5002"]

[recover]
manifest_sources = ["logs", "ipfs"]
"""
    )
    cfg = configuration.load(cfg_path)
    rc = recovery_config(cfg, skip_decode=True)
    # One manifest source per IPFS source, the very objects that fetch the blobs, in the
    # configured order: with the first node down the second still yields the manifest.
    assert rc.manifest_sources[0].logs
    ipfs_sources = [m.ipfs for m in rc.manifest_sources[1:]]
    assert ipfs_sources == [s for s in cfg.sources if isinstance(s, IpfsSource)] and len(ipfs_sources) == 2
    assert [s.endpoints for s in ipfs_sources] == [["http://127.0.0.1:5001"], ["http://127.0.0.1:5002"]]
