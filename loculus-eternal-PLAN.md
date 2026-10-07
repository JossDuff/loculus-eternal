# Loculus Eternal — Implementation Plan and Decision Record

**Status: living document. First written 2026-09-30 as a draft for an agent starting this
repository from nothing; rewritten the same day after the planning conversation recorded
below.** Read this whole file before doing anything. Every open question has a stated default;
build to the default unless the human has answered otherwise, and record the answer here when
it arrives. Phases are referred to by name, never by number, and nothing in this repository
cites section numbers.

---

## Summary

Pathoplexus (<https://pathoplexus.org>) is an open pathogen-sequence database built on the
Loculus software (<https://loculus.org>). We want its data to remain **verifiable and
recoverable forever**, even if Pathoplexus itself disappears. We do this by (1) writing every
new record into Ethereum EIP-4844 **blobs**, whose commitments are recorded permanently by a
tiny immutable contract on Ethereum L1, (2) publishing the same bytes to **IPFS**, and (3)
shipping a **recovery command** that rebuilds the database from the contract address alone,
fetching bytes from public blob archives, IPFS, or anyone, and cryptographically verifying
every byte against the chain before trusting it. There are **no storage providers, bonds,
challenges, slashing, or zero-knowledge proofs**. Deliverables: one Python command
`loculus-eternal` with an **upload** subcommand and a **recover** subcommand, the contract
`LoculusEternal` (Solidity), and the container format they share. A Pathoplexus maintainer
runs the upload command whenever there is new data.

---

## Lineage (what came before, and what to reuse)

This project descends from `blobsitter` (local checkout at
`/home/joss/dev/pathoplexus/blobsitter`, GitHub `JossDuff/blobsitter`), a generic,
dataset-agnostic "Verifiable Bonded Persistence Protocol" whose scope was: blobs + an
append-only Merkle Mountain Range over 31-byte chunks + SP1 validity proofs + bonded storage
providers accountable via challenges and slashing. **That scope is abandoned.** The
comparison and rationale are in `blobsitter/spec/persistence-v3-draft.md`. Do not port its
design assumptions. Do read it for the Ethereum blob mechanics, which are correct and
unchanged.

This repository is **Pathoplexus-specific by decision**. If it succeeds, generalization will
be replicated back into blobsitter later. Do not add abstraction for datasets other than
Pathoplexus.

Code worth reading in blobsitter (all Rust). **This project is Python (decided), so nothing
is copied verbatim.** Read these for the logic, edge cases, and test ideas, then reimplement;
ignore anything about providers, challenges, custody, or proving:

| Path in blobsitter | What it is | Reuse verdict |
|---|---|---|
| `daemon/src/` follower + blob source trait + verify-before-write ingest + flat chunk store (skip `responder.rs`, `custody.rs`, `prover.rs`, `proofs.rs`) | Chain follower at finality, ordered chain of blob source adapters, KZG verification of every blob against its versioned hash before writing | **Logic of the recovery command.** Reimplement in Python. |
| `spec/research/blob-sourcing-2026-08.md` | Researched facts about beacon endpoints, PeerDAS node requirements, public archives, retention | Copied to `docs/research/blob-sourcing.md`. |
| `testkit/` | Anvil harness: deploys real contract artifacts, builds and sends **real type-3 blob transactions**, beacon-shaped blob stub, chain-time warping | Reimplement the blob-tx construction and beacon stub in Python against anvil. Ignore staking/challenger drivers and the mock verifier. Note it pins anvil to the prague hardfork to avoid post-Fusaka cell proofs; we must not. |
| `tools/kzg-fixture/` | Uses the `c-kzg` crate with Ethereum's embedded trusted setup to compute commitments/openings | Shows the exact c-kzg API calls; the Python binding `ckzg` mirrors them. Its fixtures are synthetic, not mainnet blobs. |
| `carrier/src/` blob-tx assembly; `daemon/src/tx.rs` fee-escalating sender | Building, pricing, and resubmitting type-3 transactions | Reference for the submitter's escalation logic. It has no on-disk journal; ours must. Ignore intent verification and paymaster solvency. |
| `reference/`, `vectors/`, `scripts/gen_vectors.py` | MMR over chunks, Fiat-Shamir, custody sampling, EIP-712 | Do **not** port (hash chain chosen). |
| `contracts/`, `circuits/`, `intents/`, `abi/` | Full v2 contract surface, SP1 circuits, signed-intent package | Do **not** port. |
| `CLAUDE.md` working rules | Spec-first, golden vectors as cross-component truth, test-plan IDs in test names, comments must stand alone | **Adopted** in this repo's `CLAUDE.md`. |

---

## Facts the agent must know

### Ethereum blobs (EIP-4844, post-Fusaka mainnet)

- A **type-3 transaction** carries 1..N blobs. Each blob is 4096 field elements of the
  BLS12-381 scalar field, 32 bytes each (131,072 bytes). Because each element must be below
  the field modulus, the canonical encoding uses **31 data bytes per element with the high
  byte zero**: 126,976 usable bytes per blob. **Chunk = 31 bytes = one field element.**
- Each blob has a KZG **commitment** (48 bytes) and a **versioned hash**
  `0x01 ‖ sha256(commitment)[1:32]`. The versioned hashes are part of the transaction and are
  available to the called contract via the `BLOBHASH(i)` opcode (returns zero past the end).
  Ethereum consensus verifies commitment↔bytes at inclusion. **The versioned hash is the
  correctness anchor for everything in this project.**
- Anyone can recompute a commitment from raw bytes with `c-kzg` (Ethereum's embedded KZG
  trusted setup; no ceremony is ever run by or for this project) and check it against the
  versioned hash. Corruption by any source is therefore always detectable. Withholding is not.
- Blob **bytes** are retained by consensus nodes for 4096 epochs, about **18.2 days**. After
  that they exist only where someone chose to keep them. Versioned hashes live forever in
  execution history.
- Per-transaction blob cap: **6** post-Fusaka (EIP-7594). Per-block target/max after the
  BPO2 fork (Jan 2026): 14/21. Verify both at implementation time; they change by fork.
- Post-Fusaka blob transactions carry **cell proofs** (sidecar wrapper version 1) instead of
  one proof per blob. Any tooling that signs blob transactions must produce that shape.
- Blob transactions **must originate from an EOA**. The upload command holds a funded key.
- Blob gas is priced separately (`blobBaseFee`, floor 1 wei), and a reverted type-3 tx still
  burns blob gas. Simulate before sending.
- **Fetching blob bytes**: beacon API `GET /eth/v1/beacon/blobs/{block_id}` with an optional
  `versioned_hashes` filter (beacon-APIs v4; the older `blob_sidecars` endpoint is
  deprecated). A **self-hosted beacon node must run semi-supernode or supernode custody**
  post-PeerDAS or it cannot serve full blobs. Hosted providers with historical blobs (per
  Arbitrum's list): QuickNode, Ankr, Chainstack, dRPC, Nirvana, Conduit. Public archives:
  Blobscan (`api.blobscan.com`, lookup by versioned hash), `base/blob-archiver` (S3-backed,
  serves the beacon API shape). One public archive (Blocknative) was discontinued in 2025:
  **archives churn, so source lists are configuration, never code.**
- **History expiry (EIP-4444)**: execution-layer receipts/logs from old blocks may not be
  served by default nodes in the future. Never rely on `eth_getLogs` back to genesis as the
  only way to learn the blob list.

### Pathoplexus / Loculus data model

Verified against the live backend `https://backend.pathoplexus.org` on 2026-09-30. The full
pinned schema is in `docs/container-spec.md`.

- Data is organised **per organism**. Pathoplexus has 15 organisms today: andv, cchf, dengue,
  ebola-bdbv, ebola-sudan, ebola-zaire, hmpv, marburg, measles, mpox, rsv-a, rsv-b,
  west-nile, yellow-fever, zika. The list is configuration.
- Each **sequence entry** has an `accession` (such as `PP_006VLCT`) and a `version`;
  `accessionVersion` = `"<accession>.<version>"`. A **revision** creates a new version of the
  same accession; a **revocation** creates a new version flagged `isRevocation`. Older
  versions remain retrievable. **Entries are therefore naturally append-only at the
  accessionVersion level**, which is exactly what an append-only stream wants.
- Bulk download: `GET {backend}/{organism}/get-released-data`, content type
  `application/x-ndjson`, one JSON object per line, optional `compression=zstd` query
  parameter (server responds with `content-encoding: zstd`), `ETag` / `If-None-Match`
  support (304 when unchanged), `X-Total-Records` header. Each line has exactly the top-level
  keys `metadata`, `unalignedNucleotideSequences`, `alignedNucleotideSequences`,
  `nucleotideInsertions`, `alignedAminoAcidSequences`, `aminoAcidInsertions`. The sequence
  objects are keyed by segment (`main` for single-segment organisms; `L`/`M`/`S` for andv and
  cchf; `DENV-1`..`DENV-4` for dengue) and by gene for amino acids.
- Backend-added metadata fields include `accession`, `version`, `accessionVersion`,
  `versionStatus`, `isRevocation`, `dataUseTerms`, `dataUseTermsRestrictedUntil`,
  `dataUseTermsUrl`, `dataBecameOpenAt`, `releasedDate`, `releasedAtTimestamp`,
  `submittedDate`, `submittedAtTimestamp`, `submissionId`, `submitter`, `groupId`, `groupName`,
  `versionComment`, `pipelineVersion`, `displayName`. (The names `releasedAt` / `submittedAt`
  guessed in the first draft do not exist.)
- **Data use terms.** Submitters may mark data **Restricted-Use for up to one year**, after
  which it becomes Open automatically; they may also open it early. Restricted-Use data
  "may be shared onward only under the same terms", and hosting it in an access-restricted
  database with more than 200 users needs Executive Board permission. **Publishing
  restricted data to a public, permanent, unrestrictable medium is incompatible with those
  terms.** The terms contain **no deletion or withdrawal provision** after release, which is
  favourable for an append-only design.
- **Processed vs submitted data.** Aligned sequences, insertions, and some metadata are
  produced by a preprocessing pipeline and **can change when `pipelineVersion` changes**
  without a new accessionVersion. Only the submitted material (unaligned sequences and
  submitted metadata) is immutable per version.

---

## Guarantees (what to promise, in plain words)

Guaranteed:

1. **Correctness.** Every published blob's versioned hash is on Ethereum L1 in order. Anyone
   holding a candidate blob can verify it. Anyone holding one 31-byte chunk plus a 48-byte
   KZG opening can verify that chunk. No trust in Pathoplexus, archives, or IPFS.
2. **Complete manifest.** The chain suffices to enumerate every blob ever published.
3. **Authorization.** Only the publisher key can append.

Not guaranteed, but hedged: **availability**. Sources, in the order the recovery command tries
them: consensus nodes (18 days), public blob archives (which ingest *all* blobs and would each
have to act deliberately to drop ours), IPFS pinners (opt-in; fragile alone), and local copies
made by anyone who ran the recovery command. Loss requires all of them to lose the same bytes.

---

## Architecture

Four pieces, built in the phase order given later.

### The contract (`contracts/`, Solidity + Foundry)

Immutable. No upgradeability, no admin, no pause. One instance for Pathoplexus.

```solidity
contract LoculusEternal {
    address public publisher;             // rotatable by the current publisher
    uint64  public blobCount;             // blobs ever published
    bytes32 public head;                  // hash chain: keccak256(head ‖ versionedHash), starting from 0
    bytes32 public appPointer;            // digest of the current IPFS snapshot CID; never interpreted
    address public successor;             // write-once migration breadcrumb

    event BlobPublished(uint64 indexed seq, bytes32 versionedHash);
    event BatchCommitted(uint64 firstSeq, uint64 lastSeq, uint32 lastBlobChunkCount, bytes32 appPointer);
    event SuccessorSet(address successor);
    event PublisherChanged(address previous, address current);

    /// Called from a type-3 transaction. Reverts unless `expectedFirstSeq == blobCount`, so a
    /// stale or concurrent upload cannot interleave blobs into the stream. Reads every attached
    /// blob's versioned hash via BLOBHASH, appends each to the hash chain, emits one event per
    /// blob. `lastBlobChunkCount` says how many 31-byte chunks of the final blob are data (a
    /// partially filled final blob is otherwise ambiguous). `isBatchEnd` + `newAppPointer` mark
    /// batch completion on-chain so a consumer can know the stream is coherent without parsing it.
    function publish(uint64 expectedFirstSeq, uint32 lastBlobChunkCount, bool isBatchEnd, bytes32 newAppPointer) external;
    function setAppPointer(bytes32 p) external;       // publisher only; standalone re-point
    function setSuccessor(address s) external;        // publisher only; write-once
    function setPublisher(address p) external;        // publisher only; nonzero; emits PublisherChanged
}
```

Design notes:

- Publisher rotation recovers from *loss* of a key (rotate before losing it) but not from
  *compromise*: an attacker holding the key can rotate it away. The recovery command treats
  everything appended as canonical; disowning garbage is a social act, recorded in snapshots.
- `head` is the **durable** manifest commitment; events are the **convenient** index. A
  consumer who obtains the versioned-hash list from *anywhere* (logs, IPFS snapshot, a
  friend) verifies it by recomputing the chain to `head` at the block where `blobCount`
  matched. This is what makes history expiry survivable.
- Reject `lastBlobChunkCount > 4096` and `lastBlobChunkCount == 0`; reject a call with zero
  blobs; reject `expectedFirstSeq != blobCount`.
- Foundry tests use the `vm.blobhashes` cheatcode to set BLOBHASH values in unit tests, and
  the anvil harness for real type-3 transactions.
- The donation pot (open question below) would add a `receive()` function and a refund step
  inside `publish`; nothing else.

### The container format (`src/loculus_eternal/format/` + `docs/container-spec.md` + `vectors/`)

The committed stream is an ordered sequence of 31-byte chunks. The container gives it
structure. **The spec and golden vectors are written BEFORE the library**, and both the
upload and recovery subcommands are tested against the same vectors. Once the first real
byte is published the format can only ever be extended, never changed, so get it reviewed.

Proposed shape (refined in the container format phase):

- **Records** are `varint(length) ‖ u8 type ‖ payload`, self-delimiting. Within a batch they
  straddle chunk, blob, and transaction boundaries freely.
- **Batches start at a blob boundary** (refinement decided in planning, 2026-09-30): every
  `BATCH_BEGIN` sits at chunk 0 of a blob and the final blob of a batch is zero-padded past
  its `lastBlobChunkCount`. Reason: a torn batch (blobs published, no manifest, upload journal
  lost) must be skippable. A decoder that finds no manifest for a batch scans forward blob by
  blob for the next valid `BATCH_BEGIN`; the torn blobs become dead bytes instead of a
  stream-ending ambiguity.
- **Record types (reserve room):**
  - `HEADER` (stream position 0 only): magic, container major/minor version, schema ID.
  - `BATCH_BEGIN`: batch sequence number, first blob sequence number, previous manifest
    digest, codec ID, uncompressed body length, digest of the compressed body.
  - *body*: a compressed (or raw, per codec) run of inner records:
    - `ENTRY`: one accessionVersion of one organism, as **canonical JSON (RFC 8785)** of the
      published fields. Includes organism name.
    - `SCHEMA`: the JSON schema / field list for ENTRY records of this organism, published at
      genesis and whenever it changes. Self-description.
    - `TOOLING`: the container spec and recovery command source, so a future consumer with
      only the contract address can rebuild the tooling. Published at genesis and on every
      tagged release.
    - `DICTIONARY`: reserved for codec dictionaries. Unused in v1.
    - `REPROCESSED`: reserved for a future decision to republish processed fields after a
      pipeline change. Unused in v1.
    - `WITHDRAW`: accessionVersions the publisher withdraws; excluded from materialised output
      and snapshots from that batch on, bytes retained.
  - `BATCH_MANIFEST` (uncompressed, terminates every batch): batch number, per-organism
    entry counts, **the sha256 of each materialized per-organism NDJSON artifact** so any
    decoder can prove its output is what the publisher intended, the previous manifest's
    digest (in-stream chain), the on-chain `blobCount` expected after this batch.
  - `INDEX`: full index (accessionVersion → batch number) so a reader starts from the last
    index plus later batches instead of replaying everything. Emitted by size threshold (see
    decisions).
- **Codec IDs**: `0 = raw`, `1 = zstd (no dictionary)`. Default zstd level 19 per batch body.
- **Deterministic decoding**: the materialized artifact is a pure function of the record set:
  per-organism NDJSON, one canonical-JSON line per accessionVersion, sorted by accession then
  numeric version. No timestamps, counts, or banners in the artifact.
- Decoders **refuse unknown major versions loudly**.
- Test-plan behaviour IDs (`F1..Fn`) are assigned in the container format phase and
  referenced by test names.

### The upload subcommand (`loculus-eternal upload`)

A **one-off, idempotent command** (decided 2026-09-30). A maintainer runs it whenever there
is new data; there is no schedule and no daemon. Re-running with nothing new is a no-op. It is
safe to interrupt and rerun. Steps:

1. **Lock** the data directory.
2. **Learn what is already published from the chain and stream** (decided 2026-09-30): read
   the contract's `blobCount` and `head` at a finalized block; fetch and verify the latest
   `INDEX` and every batch after it using the recovery modules; the local cache is only an
   accelerator keyed by `(blobCount, head)`. Detect a torn batch (blobs after the last
   manifest): resume it if the journal covers it, otherwise report it and start fresh at the
   next blob boundary.
3. **Sync.** For each configured organism, `GET get-released-data` with `If-None-Match`.
   Parse NDJSON. Select entries eligible for publication (**OPEN only**; an entry becomes
   eligible the day it becomes open) whose accessionVersion is not yet published. Project
   each to the published field set.
4. **Nothing new → exit 0** with "nothing to publish".
5. **Plan a batch.** Encode into container records, compress, append `INDEX` when the size
   threshold is reached and `BATCH_MANIFEST`, chunk into 31-byte units, pack into blobs (≤6
   per transaction). Compute versioned hashes and the per-blob IPFS CIDs.
6. **Dry run.** Simulate every transaction against the contract with blob fields, check the
   wallet covers execution + blob fees, refuse if not, with a message written for the
   maintainer.
7. **Publish to Ethereum.** Send the type-3 transactions back to back with consecutive
   nonces (decided 2026-10-05 after the full-scale rehearsal showed a finality wait per
   transaction would make genesis take eleven hours; nonce order makes a reordering revert
   impossible, so the waits bought nothing), a bounded number in flight, with a fee-escalating
   resubmitter for the head of the line. Only the last transaction of the batch sets
   `isBatchEnd = true` and the new `appPointer`. Wait for **finality** once, for every
   transaction, before marking anything published.
   Crash-safety: a journal on disk records planned → sent → included → finalized per
   transaction so a restart resumes rather than duplicating. If a transaction is included but
   a later one fails, the batch is torn on-chain but harmless: the manifest is absent, the
   `BatchCommitted` event is absent, and the next run continues the same batch from the
   journal.
8. **Publish to IPFS.** Add every blob as a per-blob object and the snapshot; pin on the
   configured endpoints; verify the CIDs match the precomputed ones.
9. **Report.** Write the run report (blob seq range, versioned hashes, CIDs, gas paid).

`loculus-eternal upload --check` reports how many eligible entries are unpublished and the
estimated cost, without sending anything.

Configuration: backend URL, organism list, RPC URL, contract address, blob source list, IPFS
endpoints, data directory. Key via environment variable **only**
(`LOCULUS_ETERNAL_PUBLISHER_KEY`), never config or code. No assumptions about where it runs
(hosting decided before mainnet). Operated by the Pathoplexus team, so error messages and the
runbook are written for them.

### The recovery subcommand (`loculus-eternal recover`)

Input: contract address, an RPC URL, and a source list. Trusts nothing but the chain.

1. Read `blobCount` and `head` at a finalized block.
2. Obtain the ordered versioned-hash list from any source (`eth_getLogs`, an IPFS snapshot
   manifest, a local file). Recompute the hash chain; **reject on mismatch** and try the next
   source.
3. For each versioned hash, try sources in order: beacon endpoints (inside 18 days) → archive
   adapters (Blobscan API, blob-archiver API) → IPFS per-blob CID → local directory. For every
   candidate, `blob_to_kzg_commitment` → versioned hash → compare. **Nothing is written before
   it verifies.** Continue past misses; report every missing blob and every source tried.
4. Persist verified blobs into a flat store (chunk *i* at byte offset 31·*i* of the stream)
   and a `missing.json`. Idempotent and resumable.
5. **Decode** (crash-isolated step): walk records, skip torn batches, stop at the last
   complete `BATCH_MANIFEST`, materialize per-organism NDJSON, compare digests with the
   manifest, print a verification report.
6. `loculus-eternal serve` (later): expose the flat store through the blob-archiver API shape
   so a recovered copy is itself a source for others.

### IPFS profile (`docs/ipfs-profile.md`)

Every pinner must derive identical CIDs from identical bytes, so the profile is fixed forever:

- **Per-blob object**: exactly the blob's 131,072 bytes as **one raw block**: CIDv1, codec
  `raw` (0x55), multihash `sha2-256`. No UnixFS, no chunker. The recovery manifest is then
  an address list computable from the bytes alone.
- **Snapshot object**: a UnixFS directory with `manifest.json` (ordered versioned hashes,
  per-blob CIDs, batch numbers, `blobCount`, `head`), the materialized per-organism NDJSON
  files **compressed with zstd** (decided 2026-10-05: the plain files are 14 GB and share
  almost nothing between snapshots, so pinners would carry 14 GB per upload; compressed they
  are about 40 MB), and the container spec. Chunker `size-262144`, CIDv1, raw leaves,
  sha2-256. The `appPointer` on-chain is the sha256 of the snapshot CID's bytes. The current
  snapshot CID is announced off-chain (run report, public page) and verified against the
  pointer; the per-blob CIDs cannot be derived from versioned hashes, so IPFS-only recovery
  starts from the snapshot. Decided 2026-10-05: once the batch carrying a new pointer is
  final, the previous snapshot is unpinned on every endpoint that holds the new one, so a
  node carries every blob object and one snapshot.
- Implementation: Kubo RPC API. Pinning targets are configuration.

---

## Repository layout and standing rules

```
README.md                      what this is, how to verify, how to recover (written for a stranger in 2040)
CLAUDE.md                      working rules
loculus-eternal-PLAN.md        this file
docs/
  design.md                    the WHY, Pathoplexus-specific
  container-spec.md            the WHAT for the stream format (normative) + pinned Pathoplexus schema
  ipfs-profile.md              fixed CID derivation rules
  contract.md                  ABI, events, verification procedure
  test-plan.md                 behaviour IDs referenced by test names
  runbook.md                   for the Pathoplexus maintainer
  research/blob-sourcing.md    copied from blobsitter
vectors/                       golden vectors, generated, never hand-edited
contracts/                     Foundry
src/loculus_eternal/           one Python package
  format/                      container encode/decode + vector generator
  kzg.py                       thin wrapper over ckzg: commitment, versioned hash, chunk packing
  chain.py                     contract reads, hash-chain recompute
  sources/                     blob source adapters behind one interface: beacon, blobscan, blob-archiver, ipfs, local
  store.py                     verify-before-write flat chunk store
  upload/                      upload subcommand
  recover/                     recover subcommand
  ipfs.py                      Kubo client and CID computation
  testkit/                     anvil harness that sends real type-3 transactions
  cli.py                       loculus-eternal entry point
tests/                         pytest; test names carry behaviour IDs
```

Rules (adopted from blobsitter, still correct; the full text is in `CLAUDE.md`):

- **Spec first, vectors second, code third.** Contract, format library, upload, and recovery
  all test against the same vectors. Never edit a vector to make a test pass; regenerate and
  explain the diff.
- **If two documents disagree or something is ambiguous, stop and surface it.** The format
  and the contract are permanent once the first byte is published.
- Comments stand alone in plain language; no section-number or milestone-number citations.
- Test names carry the behaviour ID they cover.
- Keys only in environment variables. No credentials in code, config, or fixtures.
- Small scoped commits; spec changes and code changes in separate commits. One branch and one
  pull request per phase; the human reviews and merges.
- Stack (decided): **Python 3.12+** for everything off-chain, one package managed with
  `uv`/`pyproject.toml`, pinned lockfile (resolved 2026-09-30: `ckzg` 2.1.8, `web3` 8.0.0,
  `eth-account` 0.14.0, `zstandard` 0.25.0, `rfc8785` 0.1.4, `httpx` 0.28.1); **Solidity +
  Foundry** for the contract. Toolchain verified 2026-10-01 in the anvil harness phase:
  `eth-account` 0.14 signs type-3 transactions with the post-Fusaka sidecar (wrapper version 1,
  cell proofs) and `web3` 8 sends them; anvil 1.7.1 defaults to its latest hardfork and
  accepts them; `ckzg` 2.1.8 builds and runs on Python 3.14; the mainnet trusted setup is
  vendored at `src/loculus_eternal/kzg_trusted_setup.txt` with its digest pinned in `kzg.py`.
  The blobsitter Rust code is reference reading only.

---

## Phases

One branch and one pull request each. Each pull request updates `docs/test-plan.md` in the
same commit as any new mechanism, and gets a code review before merge. Order matters:
everything permanent (format, contract, IPFS profile) is frozen before anything that writes
to mainnet.

**Groundwork.** Resolve the open questions with the human. Write `docs/design.md`. Pull one
real record per organism from the live backend and pin the exact schema in
`docs/container-spec.md`. Estimate dataset size and growth from `X-Total-Records` and record
sizes; put the cost table in the design doc. Set up `pyproject.toml`, `uv.lock`, `CLAUDE.md`,
the test-plan skeleton. Deliverable: docs only.

**Container format.** Write the normative spec. Generate golden vectors: canonical JSON of
sample entries, a raw batch, a zstd batch, a multi-blob batch with a partially filled final
blob, a batch carrying an index, a torn batch (no manifest) followed by a valid batch.
Implement encode/decode with round-trip, fuzz, and vector tests. Deliverable: any two
implementations of the spec would agree byte-for-byte.

**Contract.** Implement `LoculusEternal`. Foundry unit tests (BLOBHASH cheatcode: append
order, chain head, zero-blob revert, chunk-count bounds, sequence guard, authorization,
write-once successor, batch end event). Invariant test: `head` equals the chain over all
emitted events. Deploy script. Resolve the donation pot question before starting.
Deliverable: audited-by-reading, under ~150 lines.

**Anvil harness and KZG.** First verify the toolchain signs post-Fusaka blob transactions.
Build the anvil harness that signs and sends real type-3 transactions from Python, plus the
beacon-shaped blob stub. `kzg.py` wrapper with tests against one real Ethereum mainnet blob
(fetched and stored as a fixture with its versioned hash). Deliverable: an end-to-end test
that publishes vector blobs on anvil and reads back matching events.

**Recovery command.** Source interface and adapters (beacon, Blobscan, blob-archiver, local;
IPFS later). Manifest acquisition + hash-chain verification. Verify-before-write flat store.
Decoder with manifest-digest verification. Fault tests: a source that withholds, a source
that returns corrupted bytes, a source that returns the wrong blob, RPC flaps mid-run,
kill/restart mid-run. Deliverable: `loculus-eternal recover` rebuilds the anvil-published
vectors to byte-identical NDJSON with every source but one disabled.

**Upload command.** Published-set acquisition from the chain and stream, sync from a Loculus
backend (recorded fixtures, and a local Loculus dev instance if available), eligibility
filter, batch planner, dry-run, journaled submitter with finality wait, run report, `--check`,
runbook. Round-trip test: upload on anvil → recover → digests match. Deliverable:
`loculus-eternal upload` publishes a real delta on anvil end-to-end.

**IPFS.** Per-blob objects, snapshot directory, pinning, CID verification, IPFS source adapter
in the recovery command. Test against a local Kubo (Docker). Deliverable: recovery succeeds
from IPFS alone.

**Sepolia campaign.** Prerequisite found 2026-10-01 and resolved 2026-10-02 in the streaming
phase: entries now flow from the backend feeds into sorted run files on disk, batch bodies
are compressed and decompressed as streams, and cumulative digests are computed by merging
runs, so memory stays bounded for the 14 GB genesis batch. Deploy to Sepolia. Publish a genesis batch of the full eligible dataset
and several one-off deltas over a week or two. Run the recovery command from a clean machine
using only public sources. Record costs, timings, and every operational surprise in
`docs/testnet-report.md`. Freeze the format, the contract, and the IPFS profile.

**Mainnet genesis and handover.** Resolve hosting first. Deploy. Publish genesis. Set up
alerts (publish failures, balance low, IPFS pin failures, a periodic `--check` that reports
unpublished eligible entries), and the public "how to verify / how to recover" page. Hand the
runbook to the Pathoplexus team, who operate it from then on.

Later, if successful: replicate the generic pieces back into `blobsitter`.

---

## Decisions and open questions

Each open item has a default. The agent builds to the default until told otherwise.

**Eligibility — DECIDED 2026-09-30: only entries whose data use terms are OPEN at publish
time.** Restricted entries are published the day they become open (at most one year after
submission). Rationale: the restricted terms forbid onward sharing except under the same
terms, which a permanent public medium cannot honour.

**Expungement — DECIDED 2026-09-30: "nothing can ever be expunged" is accepted.** Once
published, a record cannot be removed from Ethereum or from archives we do not control.
Revocation stays possible as an appended record, but the bytes remain. If a submitter uploads
something that must legally disappear, the only remedy is to stop serving it in our own
snapshots and document that it is disowned. Written plainly in the design doc and the public
verification page.

**Accumulator — DECIDED 2026-09-30: hash chain.** Simplest possible contract; verification is
linear in the number of blobs, which is fine because every consumer does a full recovery.
Merkle Mountain Range rejected because nobody needs light-client membership proofs.

**Publisher — DECIDED 2026-09-30: a single EOA publisher, rotatable by the current
publisher.** `setPublisher` is publisher-only, rejects the zero address, and emits an event.
This covers planned handover and pre-emptive rotation; it does not cover compromise. No
multisig, no carriers, no paymaster.

**Concurrency guard — DECIDED 2026-09-30 (planning): `publish` takes `expectedFirstSeq` and
reverts unless it equals `blobCount`.** Two maintainers running the upload command at once,
or a stale resubmission, cannot interleave blobs. A revert burns blob gas; stream integrity is
worth it and the dry run catches it first.

**Published fields — DECIDED 2026-09-30 (delegated to the author: "whatever makes the most
sense for a usable backup"). Publish the full released line as of first publication, minus
derived status fields.** Concretely, an ENTRY record contains: organism, the complete
`metadata` object, `unalignedNucleotideSequences`, `alignedNucleotideSequences`,
`nucleotideInsertions`, `alignedAminoAcidSequences`, `aminoAcidInsertions`, exactly as the
backend served them, with these fields **removed** because they change over time without a
new accessionVersion and are re-derivable: `versionStatus` (latest/revised/revoked is computed
from the record set), `dataUseTerms` and `dataUseTermsRestrictedUntil` (always OPEN for
anything we publish). The `pipelineVersion` field **stays** so a consumer knows which pipeline
produced the processed fields. Rationale: a backup that already contains aligned sequences is
usable by anyone without running a bioinformatics pipeline, which is the point of a backup;
the raw submitted material (unaligned sequences + submitted metadata) is in there too, so
anyone who wants to re-process can. Processed fields are a snapshot **as of first
publication** and are **not republished** when Pathoplexus reprocesses old entries with a
newer pipeline; that keeps the stream strictly append-only and the cost proportional to new
data. A `REPROCESSED` record type ID is reserved in the format for a future decision to
republish processed fields; not implemented in v1.
*Sub-item DECIDED 2026-09-30 (delegated: "whichever is best for recovering the data into a
database"):* the backend also serves `dataUseTermsUrl` and `dataBecameOpenAt`.
`dataUseTermsUrl` is **removed**: it is a constant derived from the instance configuration
(the open-data terms URL for everything we publish) and a Loculus database does not store it.
`dataBecameOpenAt` is **kept**: it is the only record of when the entry's terms changed to
open, which a database restore needs to rebuild the data-use-terms history.

**Compression — DECIDED 2026-09-30: zstd per batch body, no dictionary, codec ID recorded
per batch.** Sequence NDJSON compresses very well and every blob avoided is money saved. Zstd
is an IETF standard with many independent implementations.

**Recovery output — DECIDED 2026-09-30: per-organism NDJSON files whose lines match the
published projection of `get-released-data`, plus a verification report.** Other forms (a
loader into a fresh Loculus instance, a Postgres dump, FASTA + TSV) are cheap to add once
NDJSON exists.

**Cadence — DECIDED 2026-09-30: no schedule.** The upload command is a one-off idempotent
command a Pathoplexus maintainer runs whenever there is new data. Each run publishes one
batch containing all newly eligible accessionVersions across all organisms. Rationale: the
maintainer knows when data landed; a schedule adds infrastructure and a failure mode
("nothing ran") without adding data.

**Published-set source of truth — DECIDED 2026-09-30: the chain and the stream.** The upload
command reads `blobCount`/`head`, fetches the latest `INDEX` and later batches, and derives
the published set from them. A local cache is only an accelerator. Rationale: works from any
machine, survives lost local state, and cannot drift from what is actually on-chain.

**Self-description — DECIDED 2026-09-30: yes.** The container spec and the recovery command's
source are published into the stream as `TOOLING` records at genesis and on each tagged
release. Cost is a handful of blobs per release.

**Index cadence — DECIDED 2026-09-30: by size threshold; threshold DECIDED 2026-10-01:
16 MiB.** An `INDEX` is emitted in a batch when the compressed body bytes appended since the
last index, counting the current batch, reach 16 MiB (16,777,216 bytes), so a reader never
replays more than that plus one index. Rationale: with one-off uploads a count-based cadence
("every 30 batches") could mean never; at 16 MiB the index overhead is at most about a fifth
of the data while replay stays around 125 blobs.

**Manifest digests — DECIDED 2026-10-01: cumulative.** Each batch manifest carries, per
organism, the sha256 of the complete materialised output file as it stands after that batch.
A verifier hashes their recovered files and compares with the last manifest. The upload
command therefore decodes the whole stream on every run, which it needs to do anyway to be
robust against lost local state; today that is about 40 MB compressed.

**Permanent state instead of blobs — REJECTED 2026-10-01.** Writing the 39 MB dataset into
contract storage would cost on the order of $70,000 at 1 gwei, or about $25,000 as contract
bytecode, against about $126 in blobs; every future upload would pay the same per-byte rate.
Blobs it is.

**Who pins on IPFS — default kept 2026-09-30:** pinning targets are configuration in the
upload command. The runbook recommends at least two team-operated Kubo nodes plus one pinning
service. Filecoin deals are a possible later addition.

**Language — DECIDED 2026-09-30: Python for all off-chain code.** The Pathoplexus team
operates the upload command day to day and leans Python; the author agrees this is closer to a
script than a system. Loculus's backend is Kotlin and its website TypeScript, but the parts
the team operates (preprocessing pipeline, ingest, deployment scripts) are Python. One
language for upload and recovery means one format implementation and one set of vectors.
Python is also the language a stranger in 2040 is most likely to be able to run.

**Testnet — DECIDED 2026-09-30: Sepolia.** Recovery sources for the campaign: a self-hosted
semi-supernode beacon node plus one hosted historical-blob provider, plus Blobscan.

**Dataset size and growth — MEASURED 2026-09-30.** See the cost table in `docs/design.md`.

**Naming — DECIDED 2026-09-30: Loculus Eternal everywhere.** Repository `loculus-eternal`,
Python package `loculus_eternal`, one console script `loculus-eternal` with subcommands,
contract `LoculusEternal`, environment variable `LOCULUS_ETERNAL_PUBLISHER_KEY`. Pathoplexus
is documented as the first and only instance. Licence: AGPL-3.0 (the repository's LICENSE).

**Sepolia campaign operations — DECIDED 2026-10-07.** Infura (free tier, key in the
environment as `RPC_API`) for the chain; PublicNode's public Sepolia beacon API for recent
blobs; Blobscan's Sepolia archive for history; one Kubo node in Docker on the maintainer's
machine for IPFS. The publisher is a fresh development key, `0xE30036308E139D4aC1e07D0f3F3D3cd6597fBBD0`,
funded with 6 Sepolia ETH. The maintainer runs every command; the agent supplies each with
an explanation and writes `docs/testnet-report.md` from the outputs.

**Hosting — DEFERRED 2026-09-30 to before mainnet genesis.** The upload command is a plain
CLI with file and environment configuration so it can run anywhere. Likely answer: a
maintainer's machine or Pathoplexus's existing infrastructure with the key in their secret
store.

**Donation pot — DECIDED 2026-10-07: no pot. The contract is deployed as it is for the
Sepolia campaign and frozen after it.** A pot, if ever wanted, would be a separate contract.
Earlier record kept below for the reasoning:
**Donation pot — DEFERRED 2026-10-01: a TODO for a later point; the contract is built
without it.** Consequence, stated so nobody is surprised later: since `LoculusEternal` is
immutable, a pot added afterwards must be a separate contract. A separate contract can hold
donations and pay the publisher, but it cannot refund inside `publish` and so cannot bind
its ETH to upload gas as tightly as the proposal below would. If that tightness matters,
the pot has to be in `LoculusEternal` before mainnet genesis.
Original requirement and proposal, kept for that later decision:
Requirement from the human: an address anyone can send ETH to whose only purpose is covering
the gas of uploading; no refund mechanic; the ETH cannot be taken out of the contract for any
other purpose; contract ETH is used first and the wallet covers any shortfall.
Constraint: Ethereum always charges gas to the sending EOA at the start of a transaction, and
blob transactions must come from an EOA, so a contract cannot literally pay first.
Proposed design (not yet accepted): `LoculusEternal` accepts plain ETH via `receive()`, and
at the end of `publish` refunds `msg.sender` the call's actual cost, computed in-call as
execution gas × `min(tx.gasprice, 2 × block.basefee)` plus blobs × 131072 × `block.blobbasefee`,
capped at the contract balance, atomically in the same transaction. Net effect per upload is
"pot first, wallet covers the shortfall"; the wallet must still hold the full max fee for the
duration of the transaction. No withdraw, sweep, or donor-refund function would exist.

---

## What NOT to build

- No storage providers, bonds, stakes, challenges, slashing, custody proofs, or health signals.
- No SNARKs, circuits, provers, verifying keys, or verifier gateways of any kind.
- No paymaster, carrier marketplace, or reimbursement logic beyond the donation pot if it is
  adopted.
- No Merkle tree over 31-byte chunks. KZG openings already prove chunk membership.
- No generic dataset abstraction. Pathoplexus only.
- No upgradeable contract, admin role, or pause.
- No daemon or scheduler. The upload command runs once and exits.
