# The `LoculusEternal` contract

**Status: draft, frozen after the Sepolia campaign.** Source: `contracts/src/LoculusEternal.sol`.
Solidity 0.8.30, EVM version Prague, optimizer on, no metadata hash in the bytecode so that
the deployed code is reproducible from the source alone.

The contract is the permanent record of every blob Loculus Eternal publishes. It is
immutable: no proxy, no upgrade path, no admin, no pause. One instance exists per dataset;
the Pathoplexus instance's address is the dataset's identity, and anyone can check that the
code at that address matches a build of this source.

## State

| Name | Type | Meaning |
|---|---|---|
| `publisher` | `address` | The only account that may append. Rotatable by itself. |
| `blobCount` | `uint64` | Blobs ever published; also the sequence number the next blob gets. |
| `head` | `bytes32` | Hash chain over every versioned hash in order: `head = keccak256(head ‖ versionedHash)` per blob, starting from zero. |
| `appPointer` | `string` | The publisher's claim of the current IPFS snapshot: its CID, whole, as a reader would paste it into a gateway. Empty until the first batch is committed. Never interpreted on-chain. |
| `successor` | `address` | Write-once breadcrumb to a replacement deployment. |

## Functions

```solidity
function publish(uint64 expectedFirstSeq, uint32 lastBlobChunkCount, bool isBatchEnd, string calldata newAppPointer) external;
function setAppPointer(string calldata newAppPointer) external;
function setSuccessor(address newSuccessor) external;
function setPublisher(address newPublisher) external;
```

All four are publisher-only. `publish` must be called from a blob-carrying (type-3)
transaction. It reads the versioned hash of every attached blob with the `BLOBHASH` opcode,
stopping at the first zero, folds each into `head`, emits `BlobPublished` for each, and
advances `blobCount`. It reverts if:

- the caller is not the publisher (`NotPublisher`);
- `expectedFirstSeq` differs from `blobCount` (`SequenceMismatch(expected, actual)`), so a
  concurrent or stale upload cannot interleave blobs into the stream;
- `lastBlobChunkCount` is 0 or above 4096 (`BadChunkCount`);
- no blob is attached (`NoBlobs`).

When `isBatchEnd` is true the call also stores `newAppPointer` whole, replacing whatever was
there, and emits `BatchCommitted`. When false, `newAppPointer` is ignored. The contract does
not check that the string is a CID: the pointer is the publisher's claim, and a reader
checks the snapshot it names against `head` before trusting it. A reverted blob transaction still burns its blob gas,
which is why the upload command simulates every call before sending it.

`setSuccessor` rejects the zero address and reverts once a successor is set. `setPublisher`
rejects the zero address and takes effect immediately; it covers planned handover and
rotating a key before it is lost, not recovery from a compromise, since whoever holds the
key can rotate it away.

## Events

```solidity
event BlobPublished(uint64 indexed seq, bytes32 versionedHash);
event BatchCommitted(uint64 firstSeq, uint64 lastSeq, uint32 lastBlobChunkCount, string appPointer);
event AppPointerSet(string appPointer);
event SuccessorSet(address successor);
event PublisherChanged(address indexed previous, address indexed current);
```

`BlobPublished` is the convenient index: a reader collects them in `seq` order to learn the
blob list. `BatchCommitted` marks the transactions that complete a batch, so a reader can
know the stream is coherent up to `lastSeq` without decoding it, and learns how many
31-byte chunks of the final blob carry data.

## Verifying a blob list against the chain

Given a candidate list of versioned hashes `h[0..n)` from any source:

1. Read `blobCount` and `head` from the contract at a finalized block, through any Ethereum
   node (`eth_call`, or read storage slots 0 and 1 directly: `blobCount` is packed after
   `publisher` in slot 0, `head` is slot 1).
2. Check `n == blobCount`.
3. Compute `c = 0x00…00`, then for each `h[i]` in order `c = keccak256(c ‖ h[i])` with both
   operands as 32 raw bytes.
4. Accept the list only if `c == head`.

This works even if the node no longer serves old event logs, which is why `head` exists.

## Finding the snapshot

`appPointer()` returns the current snapshot's CID as a string, so one `eth_call` is all a
reader needs to locate the IPFS copy: no page, no announcement, no derivation rule. It is
stored as a dynamic string, so unlike `blobCount` and `head` it is not readable from a
fixed storage slot; any ABI-aware tool decodes it, for example
`cast call <address> "appPointer()(string)"`. The IPFS profile says what to do with it.

## Deployment

```
PUBLISHER=0x<publisher address> forge script script/Deploy.s.sol \
    --rpc-url $RPC_URL --broadcast --private-key $DEPLOYER_KEY
```

The deployer has no role after deployment; the publisher need not be the deployer. Record
the deployed address, the deployment block, and the exact compiler settings alongside the
address wherever it is published, so a verifier can rebuild the bytecode.

## Size and gas

Measured with Foundry on 2026-10-12, after the pointer became a string (behaviour C13;
real-transaction figures come from the anvil harness phase). Each call is its own
transaction, so storage starts cold; a 59-character CID occupies three storage slots.

| Measurement | Value |
|---|---|
| Runtime bytecode | 3,377 bytes (limit 24,576) |
| Source | 127 lines, 68 without comments and blanks |
| `publish` with 6 blobs, first call ever (storage never written), mid-batch | ~39,000 gas inside the call |
| `publish` with 3 blobs, first batch end ever (pointer slots never written) | ~99,000 gas inside the call |
| `publish` with 3 blobs, mid-batch, storage written before | ~16,500 gas inside the call |
| `publish` with 3 blobs, batch end, storage written before | ~32,500 gas inside the call |

Add the transaction's intrinsic cost (21,000 plus calldata) to get the execution fee of a
publish transaction: about 38,000 gas for a mid-batch transaction and about 55,000 for a
batch end in steady state, well under the 120,000 allowance used in the design doc's cost
table. The one exception is the first batch end ever, about 122,000 gas with its calldata,
because the pointer's three storage slots are written for the first time; the upload
command's gas headroom is sized for that case. Blob gas is separate and is 131,072 per
blob. Storing the CID whole rather than a 32-byte digest costs two more storage slots:
about 3,500 more gas per batch end in steady state and about 44,000 more the first time,
cents at any plausible price.

## Open item

A donation pot (an address whose ETH can only ever pay for uploads) was requested and is
deferred. Because this contract is immutable, adding one later means a separate contract,
which cannot refund inside `publish`. See the plan file for the requirement and the proposed
design.

Behaviour IDs for the contract are `C1`–`C14` in `docs/test-plan.md`.
