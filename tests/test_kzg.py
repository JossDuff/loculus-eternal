"""The KZG wrapper against a real mainnet blob and against packed stream blobs."""

import json
from pathlib import Path

import pytest

from loculus_eternal import kzg
from loculus_eternal.format.chunks import BLOB_BYTES, pack_blobs

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def mainnet_blob():
    meta = json.loads((FIXTURES / "mainnet_blob.json").read_text())
    blob = (FIXTURES / "mainnet_blob.bin").read_bytes()
    return blob, meta


def test_K1_real_mainnet_blob_commitment_and_versioned_hash(mainnet_blob):
    blob, meta = mainnet_blob
    assert len(blob) == BLOB_BYTES
    commitment = kzg.blob_to_commitment(blob)
    assert commitment.hex() == meta["commitment"][2:]
    assert kzg.commitment_to_versioned_hash(commitment).hex() == meta["versionedHash"][2:]
    assert kzg.verify_blob(blob, bytes.fromhex(meta["versionedHash"][2:]))


def test_K1_corrupted_blob_does_not_verify(mainnet_blob):
    blob, meta = mainnet_blob
    vh = bytes.fromhex(meta["versionedHash"][2:])
    flipped = bytearray(blob)
    flipped[1000] ^= 0x01
    assert not kzg.verify_blob(bytes(flipped), vh)
    assert not kzg.verify_blob(blob[:-1], vh)
    # A non-canonical element (high byte 0xff) is not a field element at all.
    bad = bytearray(blob)
    bad[0] = 0xFF
    assert not kzg.verify_blob(bytes(bad), vh)
    with pytest.raises(kzg.KzgError):
        kzg.blob_to_commitment(bytes(bad))


def test_K1_trusted_setup_is_the_pinned_one():
    import hashlib

    assert hashlib.sha256(kzg.TRUSTED_SETUP_PATH.read_bytes()).hexdigest() == kzg.TRUSTED_SETUP_SHA256
    assert kzg.settings() is kzg.settings()


def test_K2_packed_stream_blobs_are_valid_polynomials():
    stream = bytes(range(256)) * 600
    for blob in pack_blobs(stream):
        vh = kzg.blob_to_versioned_hash(blob)
        assert vh[0] == 0x01 and len(vh) == 32
        assert kzg.verify_blob(blob, vh)


def test_K3_single_element_opening_verifies_and_wrong_value_fails(mainnet_blob):
    blob, meta = mainnet_blob
    commitment = bytes.fromhex(meta["commitment"][2:])
    for index in (0, 1, 4095, 1234):
        proof, y = kzg.compute_opening(blob, index)
        assert y == blob[index * 32 : (index + 1) * 32], "the opened value is the element itself"
        assert kzg.verify_opening(commitment, index, y, proof)
        wrong_value = (int.from_bytes(y, "big") + 1).to_bytes(32, "big")
        assert not kzg.verify_opening(commitment, index, wrong_value, proof)
        assert not kzg.verify_opening(commitment, (index + 1) % 4096, y, proof)


def test_K3_cell_proofs_have_the_post_fusaka_shape(mainnet_blob):
    blob, _ = mainnet_blob
    cells, proofs = kzg.cells_and_proofs(blob)
    assert len(cells) == 128 and len(proofs) == 128
    assert all(len(c) == 2048 for c in cells) and all(len(p) == 48 for p in proofs)


def test_K3_opening_index_out_of_range_is_refused(mainnet_blob):
    blob, meta = mainnet_blob
    commitment = bytes.fromhex(meta["commitment"][2:])
    proof, y = kzg.compute_opening(blob, 4095)
    assert not kzg.verify_opening(commitment, -1, y, proof)
    assert not kzg.verify_opening(commitment, 4096, y, proof)
    with pytest.raises(kzg.KzgError):
        kzg.compute_opening(blob, 4096)


def test_K3_non_canonical_blob_raises_kzg_error_everywhere(mainnet_blob):
    blob, _ = mainnet_blob
    bad = bytearray(blob)
    bad[0] = 0xFF
    with pytest.raises(kzg.KzgError):
        kzg.compute_opening(bytes(bad), 0)
    with pytest.raises(kzg.KzgError):
        kzg.cells_and_proofs(bytes(bad))
