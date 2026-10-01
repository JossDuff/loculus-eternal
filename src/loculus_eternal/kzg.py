"""KZG commitments, versioned hashes and openings for blobs, over the c-kzg binding.

The trusted setup is Ethereum's own (the EIP-4844 ceremony output as embedded in every
consensus client). It is vendored here as `kzg_trusted_setup.txt` so this package does not
depend on another library's copy; its SHA-256 is pinned below and checked on load. No
ceremony is ever run by or for this project.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import ckzg

from loculus_eternal.format.chunks import BLOB_BYTES

TRUSTED_SETUP_PATH = Path(__file__).with_name("kzg_trusted_setup.txt")
# SHA-256 of the setup file. It is the mainnet setup with 4096 G1 points and 65 G2 points.
TRUSTED_SETUP_SHA256 = "d39b9f2d047cc9dca2de58f264b6a09448ccd34db967881a6713eacacf0f26b7"

COMMITMENT_BYTES = 48
PROOF_BYTES = 48
VERSION_KZG = 0x01


class KzgError(ValueError):
    pass


@lru_cache(maxsize=1)
def settings():
    """Load the trusted setup once. `precompute=0` keeps load time short; openings are rare."""
    data = TRUSTED_SETUP_PATH.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != TRUSTED_SETUP_SHA256:
        raise KzgError(f"trusted setup file digest {digest} does not match the pinned value")
    return ckzg.load_trusted_setup(str(TRUSTED_SETUP_PATH), 0)


def _check_blob(blob: bytes) -> None:
    if len(blob) != BLOB_BYTES:
        raise KzgError(f"a blob is {BLOB_BYTES} bytes, got {len(blob)}")


def blob_to_commitment(blob: bytes) -> bytes:
    """The 48-byte KZG commitment to a blob. Fails if any element is not a canonical field element."""
    _check_blob(blob)
    try:
        return bytes(ckzg.blob_to_kzg_commitment(blob, settings()))
    except Exception as e:  # ckzg raises its own error types
        raise KzgError(f"blob is not a valid polynomial: {e}") from e


def commitment_to_versioned_hash(commitment: bytes) -> bytes:
    """0x01 followed by the last 31 bytes of sha256(commitment), as EIP-4844 defines it."""
    if len(commitment) != COMMITMENT_BYTES:
        raise KzgError(f"a commitment is {COMMITMENT_BYTES} bytes, got {len(commitment)}")
    return bytes([VERSION_KZG]) + hashlib.sha256(commitment).digest()[1:]


def blob_to_versioned_hash(blob: bytes) -> bytes:
    return commitment_to_versioned_hash(blob_to_commitment(blob))


def verify_blob(blob: bytes, versioned_hash: bytes) -> bool:
    """True if the bytes are the blob behind this versioned hash. Never raises for bad bytes."""
    try:
        return blob_to_versioned_hash(blob) == versioned_hash
    except KzgError:
        return False


def compute_opening(blob: bytes, element_index: int) -> tuple[bytes, bytes]:
    """Prove the value of one field element. Returns (proof, y) where y is the 32-byte element.

    The evaluation point is the root of unity for that index, in the bit-reversed order the
    EIP-4844 polynomial uses, so a verifier can check a single 31-byte chunk against the blob's
    commitment without the rest of the blob.
    """
    _check_blob(blob)
    if not 0 <= element_index < 4096:
        raise KzgError("element index out of range")
    z = _evaluation_point(element_index)
    proof, y = ckzg.compute_kzg_proof(blob, z, settings())
    return bytes(proof), bytes(y)


def verify_opening(commitment: bytes, element_index: int, y: bytes, proof: bytes) -> bool:
    try:
        return bool(ckzg.verify_kzg_proof(commitment, _evaluation_point(element_index), y, proof, settings()))
    except Exception:
        return False


def cells_and_proofs(blob: bytes) -> tuple[list[bytes], list[bytes]]:
    """The 128 cells and cell proofs a post-Fusaka blob transaction carries."""
    _check_blob(blob)
    cells, proofs = ckzg.compute_cells_and_kzg_proofs(blob, settings())
    return [bytes(c) for c in cells], [bytes(p) for p in proofs]


# --- evaluation points -------------------------------------------------------------------------

BLS_MODULUS = 0x73EDA753299D7D483339D80809A1D80553BDA402FFFE5BFEFFFFFFFF00000001
PRIMITIVE_ROOT = 7
FIELD_ELEMENTS_PER_BLOB = 4096


@lru_cache(maxsize=1)
def _roots_of_unity_brp() -> list[int]:
    """Roots of unity in bit-reversal permutation order, matching the blob polynomial's domain."""
    root = pow(PRIMITIVE_ROOT, (BLS_MODULUS - 1) // FIELD_ELEMENTS_PER_BLOB, BLS_MODULUS)
    roots = [1]
    for _ in range(FIELD_ELEMENTS_PER_BLOB - 1):
        roots.append(roots[-1] * root % BLS_MODULUS)
    bits = FIELD_ELEMENTS_PER_BLOB.bit_length() - 1
    return [roots[int(format(i, f"0{bits}b")[::-1], 2)] for i in range(FIELD_ELEMENTS_PER_BLOB)]


def _evaluation_point(element_index: int) -> bytes:
    return _roots_of_unity_brp()[element_index].to_bytes(32, "big")
