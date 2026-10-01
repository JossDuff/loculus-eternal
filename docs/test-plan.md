# Test plan

Every behaviour this project promises has an ID here. A test that covers a behaviour carries
the ID in its name (`test_F3_partial_final_blob_round_trip`, `invariant_C2_head_matches_events`).
Any commit that adds a mechanism adds its behaviours here in the same commit. Coverage is
audited against this list before each phase's pull request merges.

IDs are a family letter and a number. The families are reserved now and filled in by the
phase that builds them.

| Family | Component | Filled in during |
|---|---|---|
| `F` | Container format: records, batches, index, canonical JSON, materialisation, vectors | container format |
| `C` | Contract `LoculusEternal` | contract |
| `K` | KZG wrapper and anvil harness | anvil harness and KZG |
| `R` | Recovery command: manifest acquisition, sources, store, decoder | recovery command |
| `U` | Upload command: published set, sync, eligibility, planner, submitter, journal | upload command |
| `I` | IPFS profile, pinning, IPFS source | IPFS |

## Layers

Each family's tests fall into these layers, listed from cheapest to most expensive:

- **Vector conformance.** The component reproduces every golden vector in `vectors/`
  byte-for-byte. Vectors are generated, never hand-edited.
- **Behaviour.** Unit and property tests for one stated behaviour.
- **Harness.** Tests against a real anvil chain with real type-3 transactions, and against a
  local Kubo for IPFS.
- **Fault injection.** Sources that withhold, corrupt, or mislabel; RPC that flaps; processes
  killed mid-run.
- **Process.** Checks that guard the repository itself: committed vectors equal regenerated
  vectors; no human-facing text cites milestone or section numbers; no secrets in the tree.

## Behaviours

### F — container format

*To be filled in during the container format phase.*

### C — contract

- **C1 — append order.** `publish` appends every attached blob in transaction order, emits one `BlobPublished(seq, versionedHash)` per blob with consecutive sequence numbers, and advances `blobCount` by the number of blobs.
- **C2 — head is the chain.** After any sequence of publishes, `head` equals keccak256 folded over every published versioned hash in order, starting from zero.
- **C3 — zero blobs.** A call with no attached blobs reverts and changes nothing.
- **C4 — chunk count bounds.** `lastBlobChunkCount` of 0 or above 4096 reverts; 1 and 4096 are accepted.
- **C5 — sequence guard.** A call whose `expectedFirstSeq` differs from `blobCount` reverts and changes nothing.
- **C6 — authorization.** Only the current publisher can call `publish`, `setAppPointer`, `setSuccessor` or `setPublisher`.
- **C7 — write-once successor.** `setSuccessor` rejects the zero address, succeeds once, and reverts forever after.
- **C8 — batch end.** With `isBatchEnd` true the call sets `appPointer` and emits `BatchCommitted(firstSeq, lastSeq, lastBlobChunkCount, appPointer)`; with it false the pointer is untouched and no commit event is emitted.
- **C9 — publisher rotation.** `setPublisher` rejects the zero address, emits `PublisherChanged(previous, current)`, and the previous key loses access immediately.
- **C10 — standalone re-point.** `setAppPointer` updates the pointer and emits `AppPointerSet` without touching the record.
- **C11 — constructor.** Deployment rejects a zero publisher and emits `PublisherChanged(0, publisher)`; the record starts empty with `head` zero.
- **C12 — invariant.** Under random publishes, rotations and rejected calls, `head` always equals the chain over the blobs the contract accepted and `blobCount` equals their number (handler-based invariant test).
- **C13 — size.** Runtime bytecode stays well under the contract size limit; the number is recorded in `docs/contract.md`.
- **C14 — real blob transactions.** On anvil, `publish` called from a real type-3 transaction records the blobs' versioned hashes exactly as computed off-chain from the blob bytes (covered in the anvil harness phase).

### K — KZG and harness

*To be filled in during the anvil harness and KZG phase.*

### R — recovery command

*To be filled in during the recovery command phase.*

### U — upload command

*To be filled in during the upload command phase.*

### I — IPFS

*To be filled in during the IPFS phase.*

## Process checks (in force from the groundwork phase)

- **P1 — no numbered references.** No human-facing file (README, CLAUDE.md, docs, the plan file, source, tests, contract sources) contains a milestone reference (the letter M followed by a digit) or a section sign. The check is a grep for those two patterns and must return nothing.
- **P2 — no secrets.** No private key, token, or password appears anywhere in the tree; the publisher key is read only from `LOCULUS_ETERNAL_PUBLISHER_KEY`.
