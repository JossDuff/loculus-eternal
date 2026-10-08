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
| `H` | Health page and check | health page |

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

- **F1 — minimal varint.** `uvarint` encodes the minimal LEB128 form; decoding rejects a redundant trailing zero group, more than ten bytes, or a value above 2⁶⁴ − 1.
- **F2 — record framing.** A record's length prefix covers the type byte and the payload; a record truncated by the end of the stream is an error, not a silent stop.
- **F3 — header.** The header is at position 0 with the fixed magic; a different magic or an unknown major version is refused; any minor version of a known major is accepted; chain ID and contract address round-trip.
- **F4 — canonical JSON.** Entry, schema, manifest and index payloads are RFC 8785 canonical JSON: sorted keys, no whitespace, shortest number form, UTF-8; re-canonicalising a payload leaves it unchanged.
- **F5 — chunk packing.** Every field element is a zero byte followed by 31 stream bytes; a partially filled last blob is zero past its data; the last-blob chunk count equals the number of elements that carry data; unpacking inverts packing.
- **F6 — batch layout.** A batch begins at chunk 0 of a blob (after the header in batch 0); its body's length and digest equal the values declared in the batch header; the manifest is the last record; bytes from the manifest to the end of the blob are zero.
- **F7 — codecs.** Codec 0 is identity; codec 1 is a zstd frame without dictionary; an unknown codec is refused; a decoded body whose length differs from the declared uncompressed length is refused.
- **F8 — manifest.** The manifest repeats the batch number, first blob sequence, previous manifest digest and body digest from the batch header, states the blob count after the batch, and carries one cumulative artifact digest per organism published so far; a decoder recomputes every one of these and refuses the batch on any mismatch.
- **F9 — materialisation.** Each organism's file is its entry payloads, one per line, sorted by accession (bytewise) then version (numeric); it contains nothing else; a duplicate accessionVersion keeps the first occurrence and is reported.
- **F10 — index.** An index lists every accessionVersion published up to and including its own batch, grouped by organism and accession with versions sorted; it is itself codec-encoded; the encoder emits one when the compressed body bytes since the last index reach 16 MiB; a reader that starts from the latest index and the bodies after it obtains the same published set as a full replay.
- **F11 — torn batch.** A batch without a complete, verifying manifest contributes nothing; the decoder resumes at the first following blob boundary that holds a batch header with the expected batch number and previous-manifest digest; the skipped blobs are reported.
- **F20 — dead blobs.** The decoder names the blobs no reader needs: those of a torn batch that a complete batch follows, never blob 0, never a torn range at the end of the stream; the stream decodes the same with those blobs absent.
- **F12 — unknown inner records.** An inner record of unknown type within a known major version is skipped by its length and reported; an unknown outer record type makes the batch torn.
- **F13 — round trip.** For any list of entries, encoding to blobs and decoding back yields the same entries, the same materialised files and a verifying manifest (property test).
- **F14 — vectors regenerate.** Running the generator reproduces every file in `vectors/` byte-for-byte.
- **F15 — projection.** An entry carries `organism` and the six data keys; its metadata carries `accession`, `version` and `accessionVersion` and none of the four removed fields; an encoder refuses an entry that violates this.
- **F16 — schema records.** The first batch that publishes an organism carries a schema record for it; a later batch carries one only if the field set, segments or genes changed; the record lists each sorted.
- **F17 — tooling records.** A tooling record carries a relative path and the file's bytes and round-trips exactly.
- **F19 — withdrawals.** A withdrawal record names accessionVersions; materialised files and cumulative digests exclude them while the bytes stay decodable; a withdrawal applies whether the entry came before or after it; the encoder refuses to publish a withdrawn accessionVersion and accepts a batch of withdrawals alone; the index lists withdrawn versions; manifests carry withdrawn counts.
- **F18 — bounded memory.** Encoding and decoding spill entries to sorted runs on disk: the stream bytes, manifests and materialised files are identical whatever the sort buffer size, down to a buffer that forces a spill after every entry; a body whose entries are out of order still materialises sorted; the decoder's entry iterator yields keys with payloads in materialisation order without loading the set. A hostile body (an oversized length prefix, a non-object payload, an organism name that is a path) tears the batch, allocates nothing beyond the declared length, writes nowhere outside the spill directory, and leaves no spilled chunks behind; closing the decoded stream removes the spilled entries.

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

- **K1 — real blob.** The wrapper recomputes the commitment and versioned hash of a real Ethereum mainnet blob (fixture in `tests/fixtures/`) and they match what the network recorded; a flipped bit, a short blob, or a non-canonical element does not verify; the vendored trusted setup matches its pinned digest.
- **K2 — packed blobs are polynomials.** Every blob produced by the chunk packer is a valid blob (all elements canonical) with a version-1 versioned hash.
- **K3 — openings.** A single-element opening proof verifies against the commitment at the element's evaluation point and fails for a wrong value or a wrong index; cell proofs have the post-Fusaka shape (128 cells of 2,048 bytes, 128 proofs).
- **K4 — harness.** The anvil harness starts a chain with the contract deployed and the publisher funded; the finalized block is two behind the latest; time can be warped.
- **K5 — real blob transactions.** A type-3 transaction built from blob bytes is included; the node's versioned hashes equal the locally computed ones; a batch larger than six blobs spans several transactions in order with only the last one committing the batch. (Covers C14.)
- **K6 — beacon stub.** The stub serves `GET /eth/v1/beacon/blobs/{slot}` in the real response shape, honours the `versioned_hashes` filter, returns 404 for an unknown slot and 400 for a non-numeric block id, and stops serving a slot once told to forget it.
- **K7 — end to end.** Two batches encoded from the vector entries are published on anvil, the blob list is rebuilt from events and verified against `head`, every blob is fetched from the stub by slot and verified, and the decoded stream reproduces the entries, tooling and index with verifying manifests.

### R — recovery command

- **R1 — verified blob list.** The ordered list of versioned hashes obtained from event logs reproduces the contract's `head` and `blobCount` at a finalized block; a list with a swapped pair, a dropped entry or a changed hash is rejected; a rejected manifest source is followed by the next one.
- **R2 — verify before write.** Bytes offered for a blob are written to the store only after their recomputed versioned hash matches; a source returning corrupted bytes is counted as rejected and the next source is tried; the store on disk contains nothing from a rejected candidate.
- **R3 — wrong blob.** A valid blob served under another blob's hash is not accepted for that hash.
- **R4 — withholding.** When no source supplies a blob, the run continues, the store keeps everything else, and `missing.json` lists the blob with every source tried and each outcome; a source outage is recorded as an error attempt, not a crash.
- **R5 — RPC flaps.** Transient failures of chain reads during a run are retried with backoff and the run completes.
- **R6 — resume.** A run interrupted after a partial store resumes without refetching what is already verified; a region in `chunks.dat` not recorded in `have.json` is treated as absent; the store refuses a second concurrent opener.
- **R7 — decode step.** The materialised per-organism files are byte-identical to the encoder's and the report lists batches, torn batches, per-file digests against the last manifest, warnings and tooling paths.
- **R8 — recovered copy is a source.** A store exported to the local-directory layout serves a second recovery completely.
- **R9 — single-source recovery.** With every source but one disabled (beacon, Blobscan, blob-archiver, or local), the recovered files are byte-identical.
- **R10 — history expiry.** A manifest file handed over out of band verifies against `head` and the run completes without ever asking the node for logs.
- **R11 — adapter shapes.** The beacon, Blobscan and blob-archiver adapters each return the verified blob from their respective response shapes; the beacon adapter sends the `versioned_hashes` filter and derives the slot from the block timestamp.
- **R12 — adaptive log paging.** `eth_getLogs` paging halves after an error and doubles after a success, and still covers the whole range.
- **R15 — dead blobs are not missing.** A missing blob that the decode step shows to be dead is marked unneeded in `missing.json`, and the run ends with SUCCESS and a note instead of INCOMPLETE; without a decode, or when the missing blob is blob 0 or in a torn tail, every missing blob counts.
- **R14 — withdrawn entries.** Recovery output excludes withdrawn entries and the report names the withdrawn identifiers; the command offers no option that produces the withdrawn data.
- **R13 — command line.** `loculus-eternal recover --config file.toml` runs the whole recovery from a TOML file and exits non-zero when blobs are missing or the list cannot be verified.

### U — upload command

- **U16 — all organisms.** `backend.organisms` must be written, as `"all"` or a list of names; with `"all"` the upload command asks the backend's API description for its organism list at the start of each run, so an organism added to the backend is published by the next run without a configuration change; a published organism the backend no longer serves is reported with all its entries vanished, but an organism merely left out of a configured list is not synced and never reported vanished; a backend that does not enumerate organisms is a plain error that points at listing them in the config.
- **U17 — environment references.** `${NAME}` in a config string is replaced from the environment, so a URL carrying an API key never lives in the file; a missing variable is a configuration error.
- **U1 — sync and eligibility.** The release feed is fetched with `compression=zstd`, cached by ETag and reused on 304; only entries whose terms are OPEN are eligible and a restricted entry becomes eligible when it opens; eligible lines are projected to the published form; a line whose top-level shape differs from the pinned schema stops the run before anything is published; the real released lines in `tests/fixtures/released/` pass the check.
- **U2 — published set from the chain.** A fresh data directory on another machine derives what is published from the contract and the stream, publishes only the delta, and a rerun with nothing new sends no transaction and exits 0; the recovered files afterwards contain exactly the published entries.
- **U3 — check and dry run.** `--check` reports the pending count and estimated cost and `--dry-run` additionally encodes, simulates and checks fees and balance; neither sends anything or leaves a journal behind.
- **U4 — refusals.** The dry run refuses, with a maintainer-readable reason and nothing spent, when the key is not the publisher, when the wallet cannot cover the maximum fee, and when the blob base fee is above the configured limit.
- **U5 — journal resume.** A run interrupted after a transaction was sent resumes from the journal, sends only the remaining transactions, and the recovered stream has no torn batch and no duplicate entry.
- **U6 — fee escalation.** A transaction not included within the window is replaced at the same nonce with both fees raised, a bounded number of times, and the attempts are recorded; only the head of the line is replaced, since nothing behind it can be included first.
- **U18 — batch-end estimate.** The batch-end transaction is sent only after every earlier transaction is included and with its own gas estimate from the node, because its storage write and event make it costlier than the mid-batch ones; a transaction whose predecessors are all included is always simulated against the live state; a replacement sent for higher fees keeps the gas limit of the attempt it replaces.
- **U19 — revert without movement.** A transaction that reverts while the contract's blob count still sits where the earliest unincluded transaction expects it (an out-of-gas revert, and the transactions behind it that reverted on the sequence guard because of it) is sent again at a new nonce with a fresh estimate, up to three times; the reverted attempts stay in the journal as the record of that spending; only a revert that leaves the chain past the batch's assumptions abandons it.
- **U20 — abandon ends the run.** When a batch is set aside, the run ends with a failure result instead of planning another batch on a lagging finalized view; the journal is kept under `abandoned/`, and the blob files with it when any transaction was sent; a dry run or a refusal, which sent nothing, leaves no blob files behind.
- **U21 — result line.** Every run's last log line begins with `RESULT:` and says SUCCESS, NOTHING TO DO, CHECK COMPLETE, DRY RUN PASSED, REFUSED or FAILED, followed by the reason; a backend problem ends with FAILED and the reason, never a bare traceback; the recovery command ends the same way; exit codes match.
- **U22 — progress lines.** While a long feed is read, a progress line every ten thousand lines gives the lines read and the entries found new so far; the new count includes the line just read, so a feed of only open, unpublished entries shows equal numbers.
- **U15 — pipelined sending.** A batch's transactions are sent back to back with consecutive nonces, up to a configured number in flight, and all are included in order; finality is awaited once at the end; nothing is marked published before every transaction's block is final.
- **U7 — finality before anything counts.** Blobs enter the local store and the report only after the including block is finalized.
- **U8 — torn batch, lost journal.** When the journal is lost after a partial batch, the next run reports the torn blobs, starts a fresh batch at the next blob boundary, and the recovered stream decodes with the torn range reported and every entry present; once the torn blobs other than blob 0 are gone from every source, recovery still succeeds and a later upload still knows the published set; the success line says how many blobs on chain belong to the abandoned upload.
- **U9 — concurrent upload.** If the contract's blob count moves between planning and sending, the simulation reports the sequence mismatch and nothing is sent; if another upload with the same key runs while a batch is in flight and takes one of its nonces, the batch is set aside and the next run starts a fresh one after the torn blobs.
- **U10 — command line.** `loculus-eternal upload --config file.toml` reads the key only from `LOCULUS_ETERNAL_PUBLISHER_KEY`, refuses without it, publishes with it, and is a no-op the second time; `--check` works without the key.
- **U12 — pointer carried forward.** A batch end rewrites the on-chain pointer with the value it already has, so a pointer set by hand or by a snapshot is never wiped by a data upload.
- **U14 — vanished entries.** Published entries that no longer appear in the backend feed are reported and never withdrawn on their own; with the maintainer's explicit confirmation the next batch carries withdrawal records for them, after which recoveries and snapshots exclude them.
- **U13 — chain mismatch.** A configured chain id that differs from the RPC endpoint's is a configuration error before anything is read or sent.
- **U11 — tooling.** The spec and source files are published with the first batch and again only when the package version in the published set differs from the current one.

### I — IPFS

- **I1 — blob object CID.** A blob object's CID is computable locally from the bytes (CIDv1, raw, sha2-256) and Kubo's `block put` with the profile's parameters returns the same CID; the on-chain pointer is the SHA-256 of the CID's binary form.
- **I2 — snapshot determinism.** Two independent Kubo nodes adding the same snapshot files with the profile's parameters return the same directory CID, which does not depend on the directory's name and does change when the file set changes.
- **I3 — publishing.** A published batch adds every blob object and a snapshot (manifest, spec, compressed per-organism files) to every configured endpoint; the batch-end transaction stores the pointer to that snapshot; the manifest lists every blob with its CID and the chain's head; the compressed files decompress to the published entries.
- **I4 — recovery from IPFS alone.** With only an IPFS source and the snapshot's manifest as the blob list, and event logs unavailable, the recovered files are byte-identical and the snapshot's compressed files decompress to them.
- **I5 — IPFS is a hedge.** When no endpoint is reachable the batch still publishes with the previous pointer carried forward and the report records the failure; with `required` set the run refuses instead.
- **I6 — pointer check.** A snapshot whose CID does not hash to the contract's pointer is refused both as a manifest source and as a blob source.
- **I7 — one snapshot per node.** Once the batch carrying the new pointer is final, the previous snapshot is unpinned on every endpoint that holds the new one; blob objects keep their own pins; a run with nothing new changes no pins.
- **I8 — no orphan pins.** A batch refused after its snapshot and blob objects were added has those pins removed from every endpoint that took them, except blob 0's object when the batch was the first attempt at genesis, since the stream header lives there; a fee refusal happens before any IPFS work.
- **I12 — needed blob objects stay pinned.** Before a batch is published, every earlier blob object a reader needs that an endpoint no longer pins is added again; dead blobs are left out and listed in the manifest without a CID; the IPFS source reads such a manifest.
- **I9 — per-endpoint bookkeeping.** An endpoint that was unreachable for one batch still ends with exactly the latest snapshot once it takes a new one, because the upload command remembers what each endpoint holds.
- **I10 — configuration.** `required = true` without endpoints is a configuration error.
- **I11 — upload-side IPFS source.** An `ipfs` source without a snapshot CID in the upload configuration follows the machine's latest snapshot, is checked against the chain's pointer, and can supply the stream's own blobs back to the upload command.

### H — health page

- **H1 — healthy deployment.** Against a sound deployment the check reads the chain, verifies the blob list, verifies every blob from every source, walks the stream, checks the snapshot on every IPFS node against the pointer, judges every source complete when it served every needed blob, and says healthy; it never consults the backend.
- **H2 — unavailable blob.** A blob that no configured source can supply fails the check, names the blob, and the batch holding it is reported torn; while another source still has it, the check stays healthy and the miss is visible per source. A blob of an abandoned upload that a later batch skips (never blob 0) is a note, not a problem, and the structure walker names those dead blobs.
- **H3 — corrupting source.** Corrupt bytes from a source are rejected and noted; the check stays healthy when another copy verifies.
- **H4 — stale or foreign snapshot.** A snapshot CID that does not hash to the chain's pointer is a failing problem; IPFS reads are offline with a short deadline, so an endpoint that lacks the content answers in seconds instead of searching the network for minutes.
- **H5 — server.** The page is served from one HTML file, a check can be started and polled, and unknown paths are 404.
- **H6 — once.** `health --once` prints the report as JSON and exits 0 only when healthy.
- **H7 — public configuration and progress.** Every report, including the idle one, carries the contract, network, explorer link and the check's inputs with each URL reduced to scheme and host, so an API key in an RPC or archive URL never reaches the page; while a check runs the report names the current step and a line of detail, and the page shows the repository link, the quickstart commands and the Ethereum mark.

## Process checks (in force from the groundwork phase)

- **P1 — no numbered references.** No human-facing file (README, CLAUDE.md, docs, the plan file, source, tests, contract sources) contains a milestone reference (the letter M followed by a digit) or a section sign. The check is a grep for those two patterns and must return nothing.
- **P3 — metered endpoints.** Every JSON-RPC connection spaces its requests to a configured maximum per second and retries a failed request with growing pauses, for every method the project uses including sending a signed transaction, which is idempotent; a run that still fails on the endpoint ends with a plain message and a journal that resumes.
- **P2 — no secrets.** No private key, token, or password appears anywhere in the tree; the publisher key is read only from `LOCULUS_ETERNAL_PUBLISHER_KEY`.
