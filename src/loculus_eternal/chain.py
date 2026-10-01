"""Reading the LoculusEternal contract and verifying blob lists against it.

The contract's `head` is the durable commitment to the ordered list of versioned hashes;
events are the convenient way to obtain that list. A list obtained from anywhere is checked
by recomputing the chain, so the recovery command never has to trust the source of the list,
and never has to rely on event logs still being served.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable

from eth_utils import keccak
from web3 import Web3

# The minimal ABI the recovery command needs. Kept here so recovery does not depend on the
# Foundry build output being present.
ABI = [
    {"type": "function", "name": "blobCount", "inputs": [], "outputs": [{"type": "uint64"}], "stateMutability": "view"},
    {"type": "function", "name": "head", "inputs": [], "outputs": [{"type": "bytes32"}], "stateMutability": "view"},
    {"type": "function", "name": "publisher", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
    {"type": "function", "name": "appPointer", "inputs": [], "outputs": [{"type": "bytes32"}], "stateMutability": "view"},
    {"type": "function", "name": "successor", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
    {
        "type": "event",
        "name": "BlobPublished",
        "anonymous": False,
        "inputs": [
            {"name": "seq", "type": "uint64", "indexed": True},
            {"name": "versionedHash", "type": "bytes32", "indexed": False},
        ],
    },
    {
        "type": "event",
        "name": "BatchCommitted",
        "anonymous": False,
        "inputs": [
            {"name": "firstSeq", "type": "uint64", "indexed": False},
            {"name": "lastSeq", "type": "uint64", "indexed": False},
            {"name": "lastBlobChunkCount", "type": "uint32", "indexed": False},
            {"name": "appPointer", "type": "bytes32", "indexed": False},
        ],
    },
]


class ChainError(RuntimeError):
    pass


class ManifestMismatch(ChainError):
    """A candidate blob list does not reproduce the contract's head."""


def chain_head(hashes: Iterable[bytes]) -> bytes:
    """Fold the versioned hashes into the chain exactly as the contract does."""
    h = bytes(32)
    for vh in hashes:
        h = keccak(h + vh)
    return h


@dataclass(frozen=True)
class ChainState:
    block_number: int
    blob_count: int
    head: bytes
    app_pointer: bytes
    publisher: str
    successor: str


@dataclass(frozen=True)
class BlobRef:
    """One published blob: where in the stream it sits and where on the chain it landed."""

    seq: int
    versioned_hash: bytes
    block_number: int | None = None
    block_timestamp: int | None = None

    def to_json(self) -> dict:
        return {
            "seq": self.seq,
            "versionedHash": "0x" + self.versioned_hash.hex(),
            "blockNumber": self.block_number,
            "blockTimestamp": self.block_timestamp,
        }

    @classmethod
    def from_json(cls, d: dict) -> "BlobRef":
        return cls(
            seq=int(d["seq"]),
            versioned_hash=bytes.fromhex(d["versionedHash"][2:]),
            block_number=d.get("blockNumber"),
            block_timestamp=d.get("blockTimestamp"),
        )


def verify_manifest(refs: list[BlobRef], state: ChainState) -> None:
    """Raise ManifestMismatch unless refs is exactly the list the contract committed to."""
    if [r.seq for r in refs] != list(range(len(refs))):
        raise ManifestMismatch("blob list is not a contiguous sequence from zero")
    if len(refs) != state.blob_count:
        raise ManifestMismatch(f"blob list has {len(refs)} entries, contract says {state.blob_count}")
    if chain_head(r.versioned_hash for r in refs) != state.head:
        raise ManifestMismatch("blob list does not reproduce the contract's head")


class ChainReader:
    """Reads contract state and event logs with retries and adaptive paging.

    `eth_getLogs` ranges are paged. The page size halves after any error and doubles after
    any success, within [1, max_page], because providers cap ranges differently and change
    their caps without notice.
    """

    def __init__(self, w3: Web3, contract_address: str, *, max_page: int = 5000, retries: int = 5, backoff: float = 0.5, sleep: Callable[[float], None] = time.sleep):
        self.w3 = w3
        self.contract = w3.eth.contract(address=Web3.to_checksum_address(contract_address), abi=ABI)
        self.max_page = max_page
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep
        self._block_timestamps: dict[int, int] = {}

    def _retry(self, what: str, fn: Callable):
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                return fn()
            except Exception as e:  # any transport or node error: back off and try again
                last = e
                self.sleep(self.backoff * (2**attempt))
        raise ChainError(f"{what} failed after {self.retries} attempts: {last}") from last

    def state_at_finalized(self) -> ChainState:
        block = self._retry("reading the finalized block", lambda: self.w3.eth.get_block("finalized"))
        n = block["number"]
        code = self._retry("reading the contract code", lambda: self.w3.eth.get_code(self.contract.address, block_identifier=n))
        if not code:
            raise ChainError(f"no contract code at {self.contract.address} in finalized block {n}: wrong address, wrong chain, or the deployment is not final yet")
        f = self.contract.functions
        return ChainState(
            block_number=n,
            blob_count=self._retry("reading blobCount", lambda: f.blobCount().call(block_identifier=n)),
            head=bytes(self._retry("reading head", lambda: f.head().call(block_identifier=n))),
            app_pointer=bytes(self._retry("reading appPointer", lambda: f.appPointer().call(block_identifier=n))),
            publisher=self._retry("reading publisher", lambda: f.publisher().call(block_identifier=n)),
            successor=self._retry("reading successor", lambda: f.successor().call(block_identifier=n)),
        )

    def block_timestamp(self, block_number: int) -> int:
        if block_number not in self._block_timestamps:
            block = self._retry(f"reading block {block_number}", lambda: self.w3.eth.get_block(block_number))
            self._block_timestamps[block_number] = block["timestamp"]
        return self._block_timestamps[block_number]

    def blob_refs_from_logs(self, from_block: int, to_block: int) -> list[BlobRef]:
        """Collect BlobPublished events over a block range, in sequence order."""
        refs: list[BlobRef] = []
        page = self.max_page
        start = from_block
        event = self.contract.events.BlobPublished()
        while start <= to_block:
            end = min(to_block, start + page - 1)
            try:
                logs = event.get_logs(from_block=start, to_block=end)
            except Exception:
                if page > 1:
                    page //= 2
                    continue
                # Even a single block fails: back off and retry that one block until it works.
                logs = self._retry(f"reading logs for block {start}", lambda: event.get_logs(from_block=start, to_block=start))
            for log in logs:
                refs.append(
                    BlobRef(
                        seq=log["args"]["seq"],
                        versioned_hash=bytes(log["args"]["versionedHash"]),
                        block_number=log["blockNumber"],
                        block_timestamp=self.block_timestamp(log["blockNumber"]),
                    )
                )
            start = end + 1
            page = min(self.max_page, page * 2)
        refs.sort(key=lambda r: r.seq)
        return refs
