"""Build, sign and send real blob-carrying (type-3) transactions.

eth-account computes the commitments and the post-Fusaka cell proofs from the blob bytes
and signs the transaction; the node receives the network wrapper with blobs attached. The
versioned hashes it derives are checked here against our own KZG wrapper so that the two
can never silently disagree.
"""

from __future__ import annotations

from loculus_eternal import kzg

MAX_BLOBS_PER_TX = 6


def send_blob_transaction(w3, account, to: str, data: bytes, blobs: list[bytes], *, max_fee_per_blob_gas: int = 10**10, gas: int = 1_000_000, timeout: int = 60):
    """Send one type-3 transaction and wait for its receipt. Returns (receipt, versioned_hashes)."""
    if not 1 <= len(blobs) <= MAX_BLOBS_PER_TX:
        raise ValueError(f"a blob transaction carries 1 to {MAX_BLOBS_PER_TX} blobs, got {len(blobs)}")
    versioned_hashes = [kzg.blob_to_versioned_hash(b) for b in blobs]
    base_fee = w3.eth.get_block("latest")["baseFeePerGas"]
    tx = {
        "type": 3,
        "chainId": w3.eth.chain_id,
        "from": account.address,
        "to": to,
        "value": 0,
        "data": data,
        "gas": gas,
        "maxFeePerGas": base_fee * 2 + 10**9,
        "maxPriorityFeePerGas": 10**9,
        "maxFeePerBlobGas": max_fee_per_blob_gas,
        "nonce": w3.eth.get_transaction_count(account.address),
    }
    signed = account.sign_transaction(tx, blobs=blobs)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    sent = w3.eth.get_transaction(tx_hash)
    on_chain = [bytes(h) for h in sent["blobVersionedHashes"]]
    if on_chain != versioned_hashes:
        raise RuntimeError("the node's versioned hashes differ from the locally computed ones")
    return receipt, versioned_hashes


def publish_blobs(anvil, blobs: list[bytes], *, last_blob_chunk_count: int, is_batch_end: bool, app_pointer: bytes = b"\x00" * 32):
    """Publish a batch's blobs through the contract in transactions of at most six blobs.

    Returns the list of (receipt, versioned_hashes) per transaction. Only the final
    transaction carries the batch-end flag and the pointer.
    """
    results = []
    expected_seq = anvil.contract.functions.blobCount().call()
    for start in range(0, len(blobs), MAX_BLOBS_PER_TX):
        group = blobs[start : start + MAX_BLOBS_PER_TX]
        last = start + len(group) == len(blobs)
        data = bytes.fromhex(
            anvil.contract.encode_abi(
                "publish",
                args=[
                    expected_seq,
                    last_blob_chunk_count if last else 4096,
                    is_batch_end and last,
                    app_pointer if (is_batch_end and last) else b"\x00" * 32,
                ],
            )[2:]
        )
        receipt, hashes = send_blob_transaction(anvil.w3, anvil.publisher, anvil.contract.address, data, group)
        if receipt["status"] != 1:
            raise RuntimeError(f"publish reverted in transaction {receipt['transactionHash'].hex()}")
        results.append((receipt, hashes))
        expected_seq += len(group)
    return results
