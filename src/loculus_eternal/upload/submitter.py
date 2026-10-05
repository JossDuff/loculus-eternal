"""Sending a batch's blob transactions with a journal, fee escalation and a finality wait.

The journal on disk records every transaction of the batch as planned, sent, included or
finalized, together with the blob bytes, so that a crash at any point resumes instead of
duplicating. The batch's transactions are sent back to back with consecutive nonces, a
bounded number in flight at once: nonce order guarantees that transaction n+1 can never
execute before n, and a reorganisation that drops n drops n+1 with it and replays both in
order, so the contract's sequence guard holds without waiting between them. Only the head of
the line is replaced with higher fees when it is not included in time, since nothing behind
it can move until it does. Finality is awaited once, at the end, for every transaction, and
nothing is reported as published before that.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import rlp
from eth_utils import keccak
from web3 import Web3
from web3.exceptions import TransactionNotFound

from loculus_eternal import kzg
from loculus_eternal.chain import ABI
from loculus_eternal.format.chunks import ELEMENTS_PER_BLOB
from loculus_eternal.store import _write_atomic

MAX_BLOBS_PER_TX = 6
BLOB_GAS_PER_BLOB = 131_072
NOMINAL_GAS_LIMIT = 120_000   # a publish transaction needs well under this; used before the real estimate
GWEI = 10**9

# Custom errors of LoculusEternal, by selector, so a simulated revert reads as a sentence.
ERROR_SIGNATURES = {
    "NotPublisher()": "the key in the environment is not the contract's publisher",
    "ZeroAddress()": "zero address",
    "NoBlobs()": "the transaction carries no blobs",
    "BadChunkCount(uint32)": "the last-blob chunk count is out of range",
    "SequenceMismatch(uint64,uint64)": "the contract's blob count moved since this batch was planned: another upload ran, or an earlier transaction of this batch was already included",
    "SuccessorAlreadySet()": "successor already set",
}
ERROR_BY_SELECTOR = {keccak(text=sig)[:4].hex(): (sig, text) for sig, text in ERROR_SIGNATURES.items()}


class SubmitError(RuntimeError):
    pass


class Refused(SubmitError):
    """The dry run found a reason not to send anything. Nothing was spent."""


class RevertedOnChain(SubmitError):
    """A sent transaction was included but reverted. Its blob gas is gone; the batch cannot
    continue, because the contract's record has moved past what this batch assumed."""


class NonceTaken(RevertedOnChain):
    """The publisher account spent one of this batch's nonces on another transaction, so the
    batch's remaining transactions can never be included. Another upload ran with the same
    key, from another machine. Nothing was spent here; the batch is set aside."""


@dataclass
class SentAttempt:
    tx_hash: str
    max_fee_per_gas: int
    max_fee_per_blob_gas: int
    at: float
    max_priority_fee_per_gas: int = 0
    sent_block: int | None = None


@dataclass
class PlannedTx:
    index: int
    seqs: list[int]
    versioned_hashes: list[str]
    last_blob_chunk_count: int
    is_batch_end: bool
    app_pointer: str
    status: str = "planned"            # planned -> sent -> included -> finalized
    nonce: int | None = None
    attempts: list[SentAttempt] = field(default_factory=list)
    included_block: int | None = None
    included_tx: str | None = None
    gas_used: int | None = None
    blob_gas_price: int | None = None
    effective_gas_price: int | None = None
    block_timestamp: int | None = None


@dataclass
class Journal:
    batch: int
    first_blob_seq: int
    blob_count_after: int
    body_digest: str
    manifest_digest: str
    created_at: float
    blobs_dir: str
    txs: list[PlannedTx]
    snapshot_cid: str | None = None             # the snapshot this batch's pointer names
    snapshot_endpoints: list[str] = field(default_factory=list)   # endpoints that took the new snapshot
    blob_cids: list[str] = field(default_factory=list)            # the batch's blob objects, for cleanup if abandoned
    ipfs_publish: dict | None = None            # what build_and_publish reported, for cleanup if abandoned

    @property
    def finished(self) -> bool:
        return all(t.status == "finalized" for t in self.txs)

    def save(self, path: Path) -> None:
        # The journal is the crash-safety record; it must survive a power failure too.
        _write_atomic(path, json.dumps(asdict(self), indent=1).encode())

    @classmethod
    def load(cls, path: Path) -> "Journal":
        d = json.loads(path.read_text())
        d["txs"] = [PlannedTx(**{**t, "attempts": [SentAttempt(**a) for a in t["attempts"]]}) for t in d["txs"]]
        return cls(**d)


@dataclass
class FeePolicy:
    max_blob_fee_gwei: float = 5.0
    max_priority_fee_gwei: float = 1.0
    escalation_attempts: int = 8
    inclusion_timeout_blocks: int = 6
    inclusion_timeout_seconds: float = 180.0
    finality_timeout_seconds: float = 1800.0
    poll_interval: float = 6.0
    max_in_flight: int = 8        # transactions sent but not yet included; nodes cap pending blob transactions per account


class Submitter:
    def __init__(self, w3: Web3, account, contract_address: str, journal_path: Path, pending_dir: Path, policy: FeePolicy, *, log: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep):
        self.w3 = w3
        self.account = account
        self.contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=ABI + [_PUBLISH_ABI])
        self.journal_path = journal_path
        self.pending_dir = pending_dir
        self.policy = policy
        self.log = log
        self.sleep = sleep
        # Test hooks. `after_send` is called after each send; `drop_sends` makes that many
        # sends sign and record an attempt without broadcasting it, which is what a
        # transaction evicted from the mempool looks like from here.
        self.after_send: Callable[[PlannedTx], None] | None = None
        self.drop_sends = 0

    # --- planning ---------------------------------------------------------------------------

    def existing_journal(self) -> Journal | None:
        return Journal.load(self.journal_path) if self.journal_path.exists() else None

    def plan(self, batch, blobs: list[bytes], app_pointer: bytes, *, snapshot_cid: str | None = None, snapshot_endpoints: list[str] | None = None, blob_cids: list[str] | None = None, ipfs_publish: dict | None = None) -> Journal:
        """Write the journal and the blob files for a freshly encoded batch."""
        if self.journal_path.exists():
            raise SubmitError(f"a journal already exists at {self.journal_path}; resume or abandon it first")
        blobs_dir = self.pending_dir / f"batch-{batch.batch}-from-{batch.first_blob_seq}"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        txs = []
        for i, start in enumerate(range(0, len(blobs), MAX_BLOBS_PER_TX)):
            group = blobs[start : start + MAX_BLOBS_PER_TX]
            last = start + len(group) == len(blobs)
            seqs = [batch.first_blob_seq + start + j for j in range(len(group))]
            for seq, blob in zip(seqs, group):
                (blobs_dir / f"{seq}.blob").write_bytes(blob)
            txs.append(
                PlannedTx(
                    index=i,
                    seqs=seqs,
                    versioned_hashes=["0x" + kzg.blob_to_versioned_hash(b).hex() for b in group],
                    last_blob_chunk_count=batch.last_blob_chunk_count if last else ELEMENTS_PER_BLOB,
                    is_batch_end=last,
                    app_pointer="0x" + (app_pointer if last else bytes(32)).hex(),
                )
            )
        journal = Journal(
            batch=batch.batch,
            first_blob_seq=batch.first_blob_seq,
            blob_count_after=batch.blob_count_after,
            body_digest=batch.body_digest.hex(),
            manifest_digest=batch.manifest_digest.hex(),
            created_at=time.time(),
            blobs_dir=str(blobs_dir),
            txs=txs,
            snapshot_cid=snapshot_cid,
            snapshot_endpoints=list(snapshot_endpoints or []),
            blob_cids=list(blob_cids or []),
            ipfs_publish=ipfs_publish,
        )
        journal.save(self.journal_path)
        return journal

    def blobs_for(self, journal: Journal, tx: PlannedTx) -> list[bytes]:
        return [(Path(journal.blobs_dir) / f"{seq}.blob").read_bytes() for seq in tx.seqs]

    # --- dry run -------------------------------------------------------------------------------

    def fees(self) -> dict:
        return current_fees(self.w3, self.policy)

    def calldata(self, tx: PlannedTx) -> bytes:
        return bytes.fromhex(self.contract.encode_abi("publish", args=[tx.seqs[0], tx.last_blob_chunk_count, tx.is_batch_end, bytes.fromhex(tx.app_pointer[2:])])[2:])

    def simulate(self, tx: PlannedTx, fees: dict) -> int:
        """Run the call with the blob hashes attached; return the gas estimate or raise Refused
        with the contract's reason."""
        call = {
            "from": self.account.address,
            "to": self.contract.address,
            "data": "0x" + self.calldata(tx).hex(),
            "type": "0x3",
            "blobVersionedHashes": tx.versioned_hashes,
            "maxFeePerBlobGas": hex(fees["maxFeePerBlobGas"]),
            "maxFeePerGas": hex(fees["maxFeePerGas"]),
            "maxPriorityFeePerGas": hex(fees["maxPriorityFeePerGas"]),
        }
        response = self.w3.provider.make_request("eth_estimateGas", [call])
        if "error" in response:
            err = response["error"]
            data = err.get("data") or ""
            if isinstance(data, dict):
                data = data.get("data", "")
            selector = data[2:10] if isinstance(data, str) and data.startswith("0x") else ""
            sig, text = ERROR_BY_SELECTOR.get(selector, (None, None))
            reason = f"{text} ({sig})" if sig else err.get("message", str(err))
            raise Refused(f"transaction {tx.index} of the batch would revert: {reason}")
        return int(response["result"], 16)

    def check_fees(self) -> dict:
        """Refuse when blob space is pricier than the configured ceiling. Cheap; run first."""
        fees = self.fees()
        if fees["blobBaseFee"] > self.policy.max_blob_fee_gwei * GWEI:
            raise Refused(f"the blob base fee is {fees['blobBaseFee'] / GWEI:.3f} gwei, above the configured limit of {self.policy.max_blob_fee_gwei} gwei; wait for a quieter period or raise upload.max_blob_fee_gwei")
        return fees

    def dry_run(self, journal: Journal) -> dict:
        """Check fees, simulate the next transaction, and check the balance covers the batch."""
        fees = self.check_fees()
        pending = [t for t in journal.txs if t.status == "planned"]
        balance = self.w3.eth.get_balance(self.account.address)
        if not pending:
            return {"fees": fees, "transactions": 0, "maxCostWei": 0, "balanceWei": balance}

        def max_cost_for(gas_limit: int) -> int:
            return sum(gas_limit * fees["maxFeePerGas"] + len(t.seqs) * BLOB_GAS_PER_BLOB * fees["maxFeePerBlobGas"] for t in pending)

        def too_poor(max_cost: int) -> Refused:
            return Refused(f"the publisher wallet {self.account.address} holds {balance / 10**18:.6f} ETH but the batch needs up to {max_cost / 10**18:.6f} ETH at current fees")

        # A node caps a gas estimate by what the sender can pay, so an empty wallet would
        # surface as a confusing out-of-gas message; check the balance against a nominal
        # gas allowance first.
        if balance < max_cost_for(NOMINAL_GAS_LIMIT):
            raise too_poor(max_cost_for(NOMINAL_GAS_LIMIT))
        gas = self.simulate(pending[0], fees)
        gas_limit = _gas_limit(gas)
        max_cost = max_cost_for(gas_limit)
        likely_cost = sum(gas * fees["baseFeePerGas"] + len(t.seqs) * BLOB_GAS_PER_BLOB * fees["blobBaseFee"] for t in pending)
        if balance < max_cost:
            raise too_poor(max_cost)
        return {"fees": fees, "transactions": len(pending), "blobs": sum(len(t.seqs) for t in pending), "gasEstimate": gas, "maxCostWei": max_cost, "likelyCostWei": likely_cost, "balanceWei": balance}

    # --- sending ---------------------------------------------------------------------------

    def run(self, journal: Journal) -> Journal:
        """Drive every transaction of the journal to finalized, resuming from any state."""
        while True:
            self._send_and_include_all(journal)
            if self._await_finality_all(journal):
                return journal
            # A reorganisation dropped something before finality; the loop resends it.

    def _send_and_include_all(self, journal: Journal) -> None:
        """Keep up to max_in_flight transactions pending until every one is included."""
        gas: int | None = None
        while True:
            pending = [t for t in journal.txs if t.status == "sent"]
            planned = [t for t in journal.txs if t.status == "planned"]
            if not pending and not planned:
                return
            # Fill the window in order. Only the first transaction of a run is simulated: the
            # ones behind it would fail a simulation against the current state by design,
            # because they assume their predecessors have executed.
            while planned and len(pending) < self.policy.max_in_flight:
                tx = planned.pop(0)
                if gas is None:
                    gas = self.simulate(tx, self.fees()) if not pending and not any(t.status in ("included", "finalized") for t in journal.txs) else self._gas_from_journal(journal)
                self._send(journal, tx, gas=gas)
                pending.append(tx)
            self._poll_inclusion(journal, pending)
            if any(t.status == "sent" for t in journal.txs):
                self.sleep(self.policy.poll_interval)

    def _gas_from_journal(self, journal: Journal) -> int:
        """The gas limit for a resumed batch: what an included transaction used, or a sound default."""
        used = [t.gas_used for t in journal.txs if t.gas_used]
        return _gas_limit(max(used)) if used else _gas_limit(NOMINAL_GAS_LIMIT // 2)

    def _poll_inclusion(self, journal: Journal, pending: list[PlannedTx]) -> None:
        """Record every pending transaction that landed; replace the head of the line if it
        has been waiting too long."""
        for tx in list(pending):
            receipt = self._receipt_for_any(tx)
            if receipt is None:
                continue
            if receipt["status"] != 1:
                raise RevertedOnChain(f"transaction {tx.index} ({receipt['transactionHash'].hex()}) reverted on chain in block {receipt['blockNumber']}; its blob gas is lost")
            tx.status = "included"
            tx.included_block = receipt["blockNumber"]
            tx.included_tx = receipt["transactionHash"].hex()
            tx.gas_used = receipt["gasUsed"]
            tx.effective_gas_price = receipt.get("effectiveGasPrice")
            tx.blob_gas_price = receipt.get("blobGasPrice")
            tx.block_timestamp = self.w3.eth.get_block(receipt["blockNumber"])["timestamp"]
            journal.save(self.journal_path)
            self.log(f"transaction {tx.index}: included in block {tx.included_block}")
            pending.remove(tx)
        if not pending:
            return
        head = pending[0]
        # If the account's confirmed nonce has passed the head's nonce and none of its
        # attempts landed, something else from this key took the nonce: another upload. The
        # count can show a block before its receipts do, so look again before concluding.
        if head.nonce is not None and self.w3.eth.get_transaction_count(self.account.address) > head.nonce:
            self.sleep(min(self.policy.poll_interval, 2.0))
            if self._receipt_for_any(head) is None:
                raise NonceTaken(f"nonce {head.nonce} of transaction {head.index} was used by a transaction that is not part of this batch; another upload ran with the publisher key in between")
            return  # it landed after all; the next poll records it
        last = head.attempts[-1]
        waited_blocks = self.w3.eth.block_number - (last.sent_block if last.sent_block is not None else self.w3.eth.block_number)
        timed_out = waited_blocks >= self.policy.inclusion_timeout_blocks or time.time() - last.at >= self.policy.inclusion_timeout_seconds
        if timed_out:
            if len(head.attempts) > self.policy.escalation_attempts:
                raise SubmitError(f"transaction {head.index} was not included after {len(head.attempts)} attempts; the journal is kept so a later run can resume")
            self.log(f"transaction {head.index}: not included yet, replacing with higher fees")
            self._send(journal, head, escalation=len(head.attempts), gas=_gas_limit(head.gas_used or NOMINAL_GAS_LIMIT // 2))

    def _await_finality_all(self, journal: Journal) -> bool:
        """Wait until the block of the last included transaction is final, then confirm every
        transaction is still where the journal says. Returns False if any was dropped by a
        reorganisation (those are set back to planned for the caller to resend)."""
        included = [t for t in journal.txs if t.status == "included"]
        if not included:
            return True
        last_block = max(t.included_block for t in included)
        started = time.time()
        while self.w3.eth.get_block("finalized")["number"] < last_block:
            if time.time() - started > self.policy.finality_timeout_seconds:
                raise SubmitError(f"transactions were included up to block {last_block} but finality did not arrive within {self.policy.finality_timeout_seconds}s; rerun later to continue waiting")
            self.sleep(self.policy.poll_interval)
        dropped = False
        for tx in included:
            receipt = self._receipt_for_any(tx)
            if receipt is None or receipt["blockNumber"] != tx.included_block:
                self.log(f"transaction {tx.index}: dropped by a reorganisation before finality, resending")
                tx.status = "planned"
                tx.included_block = None
                dropped = True
        if dropped:
            journal.save(self.journal_path)
            return False
        for tx in included:
            tx.status = "finalized"
            self.log(f"transaction {tx.index}: finalized")
        journal.save(self.journal_path)
        return True

    def _send(self, journal: Journal, tx: PlannedTx, escalation: int = 0, gas: int | None = None) -> None:
        if tx.attempts and self._receipt_for_any(tx) is not None:
            tx.status = "sent"
            return  # an earlier attempt landed after all; the inclusion loop will record it
        fees = self.fees()
        if gas is None:
            try:
                gas = _gas_limit(self.simulate(tx, fees))
            except Refused:
                if tx.attempts and self._receipt_for_any(tx) is not None:
                    tx.status = "sent"
                    return
                raise
        factor = 1.25**escalation
        if tx.nonce is None:
            # Consecutive nonces within the batch: the one after the predecessor's, or the
            # account's next pending nonce for the first transaction.
            previous = journal.txs[tx.index - 1] if tx.index > 0 else None
            tx.nonce = previous.nonce + 1 if previous is not None and previous.nonce is not None else self.w3.eth.get_transaction_count(self.account.address, "pending")
        max_fee = int(fees["maxFeePerGas"] * factor)
        priority = int(fees["maxPriorityFeePerGas"] * factor)
        blob_fee = int(fees["maxFeePerBlobGas"] * factor)
        if tx.attempts:
            # A replacement at the same nonce is accepted only if every fee is at least a
            # tenth above the previous attempt, whatever the current base fee happens to be.
            prev = tx.attempts[-1]
            max_fee = max(max_fee, prev.max_fee_per_gas * 9 // 8 + 1)
            priority = max(priority, prev.max_priority_fee_per_gas * 9 // 8 + 1)
            blob_fee = max(blob_fee, prev.max_fee_per_blob_gas * 9 // 8 + 1)
        txn = {
            "type": 3,
            "chainId": self.w3.eth.chain_id,
            "from": self.account.address,
            "to": self.contract.address,
            "value": 0,
            "data": self.calldata(tx),
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "maxFeePerBlobGas": blob_fee,
            "nonce": tx.nonce,
        }
        signed = self.account.sign_transaction(txn, blobs=self.blobs_for(journal, tx))
        tx_hash = blob_transaction_hash(bytes(signed.raw_transaction))
        # Record the attempt before broadcasting. If the broadcast's response is lost, the
        # node may still have the transaction; a resume then waits for this hash at this
        # nonce instead of sending a second copy at a fresh nonce.
        attempt = SentAttempt(tx_hash=tx_hash, max_fee_per_gas=txn["maxFeePerGas"], max_fee_per_blob_gas=txn["maxFeePerBlobGas"], at=time.time(), max_priority_fee_per_gas=txn["maxPriorityFeePerGas"])
        tx.attempts.append(attempt)
        tx.status = "sent"
        journal.save(self.journal_path)
        if self.drop_sends > 0:
            self.drop_sends -= 1
        else:
            try:
                reported = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception as e:
                self.log(f"transaction {tx.index}: broadcast of {tx_hash} failed ({e}); it is journaled and will be waited for or replaced")
            else:
                if "0x" + bytes(reported).hex() != tx_hash:
                    raise SubmitError(f"the node reports transaction hash 0x{bytes(reported).hex()} but the signed payload hashes to {tx_hash}")
        # The inclusion window counts from the block after the broadcast returned.
        attempt.sent_block = self.w3.eth.block_number
        attempt.at = time.time()
        journal.save(self.journal_path)
        self.log(f"transaction {tx.index}: sent {tx_hash} with {len(tx.seqs)} blob(s) (attempt {len(tx.attempts)})")
        if self.after_send:
            self.after_send(tx)

    def _receipt_for_any(self, tx: PlannedTx):
        """The receipt of whichever attempt landed, or None. A transport failure is retried
        and then raised, never mistaken for 'not included'."""
        for attempt in tx.attempts:
            for retry in range(5):
                try:
                    receipt = self.w3.eth.get_transaction_receipt(attempt.tx_hash)
                    break
                except TransactionNotFound:
                    receipt = None
                    break
                except Exception as e:
                    if retry == 4:
                        raise SubmitError(f"cannot reach the node to look up transaction {attempt.tx_hash}: {e}") from e
                    self.sleep(0.5 * 2**retry)
            if receipt is not None:
                return receipt
        return None


def _gas_limit(estimate: int) -> int:
    """A limit with room for the batch-end transaction's extra storage write and event, which
    the first transaction's estimate does not include."""
    return max(estimate + estimate // 4, estimate + 60_000)


def current_fees(w3: Web3, policy: FeePolicy) -> dict:
    """What a publish transaction would offer right now: twice the base fees plus the tip,
    with a floor of one gwei per blob gas so a quiet-time estimate is not absurdly small."""
    block = w3.eth.get_block("latest")
    base = block["baseFeePerGas"]
    blob_base = int(w3.provider.make_request("eth_blobBaseFee", [])["result"], 16)
    tip = int(policy.max_priority_fee_gwei * GWEI)
    return {
        "baseFeePerGas": base,
        "blobBaseFee": blob_base,
        "maxPriorityFeePerGas": tip,
        "maxFeePerGas": 2 * base + tip,
        "maxFeePerBlobGas": max(2 * blob_base, GWEI),
    }


def blob_transaction_hash(raw_network_wrapper: bytes) -> str:
    """The transaction hash of a signed blob transaction.

    What eth-account hands back is the network form, `0x03 ‖ rlp([payload, wrapperVersion,
    blobs, commitments, cellProofs])`. The hash the chain uses covers only the signed payload,
    `keccak256(0x03 ‖ rlp(payload))`, so it has to be recomputed here; eth-account's own
    `hash` attribute is over the wrapper and does not match the node's.
    """
    wrapper = rlp.decode(raw_network_wrapper[1:])
    return "0x" + keccak(b"\x03" + rlp.encode(wrapper[0])).hex()


_PUBLISH_ABI = {
    "type": "function",
    "name": "publish",
    "stateMutability": "nonpayable",
    "inputs": [
        {"name": "expectedFirstSeq", "type": "uint64"},
        {"name": "lastBlobChunkCount", "type": "uint32"},
        {"name": "isBatchEnd", "type": "bool"},
        {"name": "newAppPointer", "type": "bytes32"},
    ],
    "outputs": [],
}
