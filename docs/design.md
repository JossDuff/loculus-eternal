# Loculus Eternal — design

This document is the *why*. It explains what problem Loculus Eternal solves for Pathoplexus,
what it promises, what it deliberately does not promise, and why each design choice was made.
The normative *what* lives in `docs/container-spec.md`, `docs/contract.md` and
`docs/ipfs-profile.md`; the roadmap and the dated record of every decision live in
`loculus-eternal-PLAN.md`.

## The problem

Pathoplexus is an open database of pathogen genome sequences and their metadata, built on the
Loculus software. Its value is that the data is public and stays public. But a database is
only as durable as the organisation that runs it: funding ends, domains lapse, disks fail,
maintainers move on. We want Pathoplexus's released data to be **recoverable and verifiable
forever**, even if Pathoplexus itself disappears, without asking anyone to trust us.

Making a dataset outlive its publisher means solving two separate problems:

- **Correctness.** Someone holding a copy of the bytes must be able to prove, without trusting
  anyone, that they are the bytes the publisher committed to.
- **Availability.** Someone must be able to obtain the bytes from *somewhere*.

Writing the whole dataset into Ethereum state would solve both, and would cost roughly ten
thousand times too much for a dataset of several gigabytes. Loculus Eternal solves correctness
completely with Ethereum and solves availability by hedging.

## The idea in one paragraph

Every time a Pathoplexus maintainer runs `loculus-eternal upload`, the newly released entries
are packed into Ethereum **blobs** (the cheap, temporary data lane added by EIP-4844). Each
blob has a cryptographic commitment called its **versioned hash**, which Ethereum itself
checks against the blob's bytes when the transaction is included. A tiny, immutable contract
called `LoculusEternal` records every versioned hash in order, chained together into a single
32-byte `head`. Ethereum forgets the blob bytes after about eighteen days, but the versioned
hashes stay in its history forever, so anyone who later obtains a copy of a blob from anywhere
can recompute its commitment and check it against the chain. The same bytes are also pinned on
IPFS and are ingested, as a matter of course, by public archives that store every blob on the
network. A recovery command, given only the contract address, rebuilds the whole dataset from
whatever sources still have the bytes, verifying every one against the chain before trusting
it.

## What is promised

1. **Correctness, unconditionally.** Every published blob's versioned hash is on Ethereum in
   publication order. Anyone holding a candidate blob can verify it. Anyone holding a single
   31-byte chunk plus a 48-byte KZG opening can verify that chunk without the rest of the blob.
   This needs no trust in Pathoplexus, in any archive, or in IPFS.
2. **A complete manifest, forever.** The chain alone is enough to enumerate every blob ever
   published, in order, and to check any list of blob hashes someone hands you. Even if
   Ethereum nodes one day stop serving old event logs, the `head` in contract storage lets a
   consumer verify a list obtained from anywhere else.
3. **Authorization.** Only the holder of the publisher key can append to the record.

## What is not promised, and how it is hedged

**Availability is not guaranteed.** Nobody is obliged to keep the bytes. Instead the bytes are
arranged to exist in as many independent places as possible, each of which is safe to use
because every byte is verified:

- **Ethereum consensus nodes** keep blob bytes for 4096 epochs, about eighteen days.
- **Public blob archives** (Blobscan and others) ingest every blob on the network for their
  own reasons. They keep ours by default; each would have to act deliberately to drop them.
- **IPFS pinners** keep the bytes for as long as at least one node pins them. Anyone can pin.
  On its own this is fragile; as one hedge among several it is cheap and useful.
- **Local copies.** Anyone who runs the recovery command once holds a verified copy, and can
  serve it to others through the same interfaces the recovery command reads from.

Loss requires every archive, every pinner, and every local holder to lose the same bytes.
Nothing prevents that; the design makes it unlikely, and makes partial loss both detectable
and repairable from any surviving copy.

**Nothing can ever be removed.** Once a record is published it is in Ethereum's history and in
archives we do not control. A revocation can be appended, and the recovery output will mark
the entry revoked, but the original bytes remain retrievable by anyone. If a submitter uploads
something that must legally disappear, the only remedy is to stop including it in our own
IPFS snapshots and to document publicly that it is disowned. Pathoplexus accepted this
trade-off on 2026-09-30: its data use terms already contain no deletion provision after
release, and a backup that could be edited would not be a backup.

**Restricted data is never published.** Pathoplexus lets submitters mark data Restricted-Use
for up to a year, during which it may only be shared onward under the same terms. A permanent
public medium cannot honour that, so only entries whose terms are Open at upload time are
eligible. A restricted entry becomes eligible on the day it turns open, and is picked up by the
next upload.

**The publisher is trusted for content, not for integrity.** A compromised publisher key can
append garbage; it cannot alter or remove anything already published. Garbage is identifiable
by its position in the stream and can be disowned socially or superseded by a successor
deployment. Nothing proves that the published bytes are the *right* dataset; that rests on
public comparison with Pathoplexus's other channels.

## What gets published

Pathoplexus organises data per **organism** (fifteen today, from dengue to mpox). Each
sequence entry has a permanent **accession** such as `PP_006VLCT`; corrections create new
numbered **versions** of the same accession, and the pair `PP_006VLCT.2` is called an
**accessionVersion**. Versions are never edited in place and old versions stay retrievable, so
the set of accessionVersions only grows. That is exactly what an append-only stream can
mirror: one `ENTRY` record per accessionVersion, published once.

Each entry is published as the backend serves it in its bulk release feed: the full metadata
object, the submitted (unaligned) nucleotide sequences, and the pipeline-produced aligned
nucleotide sequences, insertions, and aligned amino-acid sequences. Four metadata fields are
removed because they change over time without a new version and are derivable: the entry's
status relative to later versions, and the three data-use-terms fields, which are always the
open-data values for anything we publish. The exact schema is pinned in
`docs/container-spec.md`.

Why publish the pipeline-produced fields at all, when only the submitted material is
immutable? Because the point of a backup is that someone can use it. A copy that already
contains aligned sequences is usable without running a bioinformatics pipeline; the raw
submitted material is in there too for anyone who wants to reprocess. The processed fields are
a snapshot as of first publication, tagged with the `pipelineVersion` that produced them, and
are not republished when Pathoplexus reprocesses old entries. That keeps the stream strictly
append-only and the cost proportional to new data. A record type is reserved for republishing
processed fields should that decision ever change.

## The pieces

### The contract

`LoculusEternal` is immutable: no upgrade path, no admin, no pause. It holds five things: the
publisher address, the number of blobs ever published, the `head` of a hash chain over their
versioned hashes, an application pointer, and a write-once successor address.

Its `publish` function is called from a blob-carrying transaction. It reads each attached
blob's versioned hash straight from the transaction with the `BLOBHASH` opcode, so the
publisher is trusted only for *whether* to append, never for *what* the hash is. It folds each
hash into the chain, `head = keccak256(head ‖ versionedHash)`, and emits one event per blob.
The caller also states how many 31-byte chunks of the final blob carry data, whether this
transaction completes a batch, and the new application pointer.

Two small guards matter operationally. The caller passes the sequence number it expects the
first new blob to get, and the call reverts if the contract disagrees. That means two
maintainers running the upload command at the same time, or a stale resubmission after a
restart, cannot interleave blobs into the stream. And the publisher address can be rotated by
the current publisher, which covers planned handover and pre-emptive rotation before a key is
lost, though not compromise.

**Why a hash chain and not a Merkle tree.** A hash chain is five lines of Solidity and one
storage slot. Verifying a list of hashes against it costs one hash per blob, which is linear
in the dataset, and that is fine because every consumer of this data performs a full recovery
anyway. A Merkle Mountain Range would let a light client prove one blob's membership in
logarithmic work, at the price of a larger contract, more state, and more surface in something
that can never be changed. Nobody has asked for light-client proofs, and minimalism is itself
a longevity property.

**Why events and storage both.** Events are the convenient index: a recovery run normally
reads them with `eth_getLogs`. Storage is the durable one: Ethereum's history-expiry roadmap
means old logs may one day not be served by default nodes, but contract storage at any block
always will be, and a hash list obtained from an IPFS snapshot or a friend can be checked
against `head`.

**The application pointer** is one 32-byte value the publisher sets to the digest of the
current IPFS snapshot's address. The contract never interprets it. It is a claim, not a proof:
it tells a consumer what the publisher says the current view is, and lets them notice a stale
or tampered snapshot served by an intermediary. The verifying path is always to recover the
stream and decode it locally. **The successor** is a write-once address left as a breadcrumb
if the contract ever has to be replaced.

### The container format

The chain commits to an ordered stream of 31-byte chunks and nothing else. Everything above
that is the container format's job, and the format's own integrity is protected by living
inside the committed stream. The format is specified before any code is written, with golden
vectors that every component tests against, because once the first byte is on mainnet the
format can be extended but never changed.

Records are length-prefixed and self-delimiting. A **batch** is one upload's worth of records:
a `BATCH_BEGIN`, a compressed body of `ENTRY` records (plus `SCHEMA` and `TOOLING` records
when they change), and a `BATCH_MANIFEST` that lists per-organism counts, the digest of each
materialised per-organism output file, the previous manifest's digest, and the on-chain blob
count expected after the batch. Any decoder can therefore prove its output is what the
publisher intended. Every batch starts at a blob boundary, so if an upload is interrupted
after some blobs are on-chain but before the manifest, a decoder can skip the torn blobs and
resynchronise at the next batch.

**Why zstd.** Sequence data is highly redundant and compresses many-fold; the measured
figures are in the cost table below. Every blob avoided is money saved, and zstd is an IETF
standard with many independent implementations, so a decoder in 2040 can still read it. The
codec is recorded per batch, so raw remains possible.

**Why the stream describes itself.** A stranger who finds only the contract address in the
future needs to know how to interpret the bytes. The container spec and the recovery command's
own source are published into the stream as `TOOLING` records at genesis and on every tagged
release, so the data does not outlive its documentation. The cost is a handful of blobs per
release.

**Why an index, and why by size.** To know what is already published, the upload command,
and any consumer who wants one entry without the whole dataset, would otherwise have to replay
every batch from the beginning. An `INDEX` record lists every accessionVersion published so
far and the batch it lives in, so a reader starts from the last index and replays only the
batches after it. The index grows with the dataset, so it is emitted not on every upload but
whenever the compressed bytes appended since the last index exceed a threshold; that bounds
how much any reader has to replay regardless of how often uploads happen.

### The upload command

`loculus-eternal upload` is a one-off command, not a service. A Pathoplexus maintainer runs it
whenever there is new released data; running it again with nothing new does nothing; it is
safe to interrupt and rerun. It first learns what is already published by reading the
contract and decoding the stream's latest index and later batches, keeping a local cache only
as an accelerator, so it works from any machine and cannot drift from what is really
on-chain. It then fetches each organism's release feed, selects the open entries not yet
published, packs them into a batch, simulates every transaction, and only then sends them, one
blob-carrying transaction at a time, waiting for finality before recording anything as
published. A journal on disk records each transaction's progress so a restart resumes rather
than duplicates. Finally it pins the same bytes on IPFS and writes a report.

The command reads its key only from the environment variable
`LOCULUS_ETERNAL_PUBLISHER_KEY`, makes no assumptions about where it runs, and writes its
error messages for the people who will actually run it.

### The recovery command

`loculus-eternal recover` trusts nothing but the chain. It reads the blob count and `head` at a
finalized block, obtains the ordered list of versioned hashes from any source and checks it
against `head`, then fetches each blob from an ordered list of sources: consensus nodes while
they still have it, public archives, IPFS by the fixed per-blob address, and local
directories. Every candidate blob's commitment is recomputed from its bytes and compared with
the versioned hash before a single byte is written. Blobs that no source can supply are
reported as a first-class result, with every source tried. A separate decoding step walks the
stream, materialises per-organism NDJSON files identical in shape to the backend's release
feed, and checks them against the manifest digests.

### IPFS

Every pinner must derive the same address from the same bytes, so the profile is fixed: each
blob is one raw IPFS block, and each snapshot is a directory holding the manifest, the
materialised files, and the container spec, built with fixed chunking parameters. The
application pointer on-chain is the digest of the snapshot's address. Which nodes pin is
configuration, never code; the runbook recommends two team-operated nodes plus one pinning
service so that neither a single machine nor the team's infrastructure is a single point of
failure.

## Dataset size and cost

Measured on 2026-09-30 from the live backend: every organism's full release feed was
downloaded, restricted entries were removed, the published projection was applied, and the
result was compressed with zstd at level 19 (the level the batch encoder will use). Blob
counts use 126,976 usable bytes per blob; transaction counts use six blobs per transaction.

### Size today

| Organism | Released records | Open | Restricted | Raw feed (MB) | Open, projected (MB) | zstd-19 (MB) | Blobs |
|---|---:|---:|---:|---:|---:|---:|---:|
| `andv` | 736 | 671 | 65 | 12.0 | 10.1 | 0.1 | 1 |
| `cchf` | 8,737 | 8,735 | 2 | 173.9 | 172.6 | 1.1 | 9 |
| `dengue` | 63,074 | 58,659 | 4,415 | 1,480.4 | 1,339.9 | 8.3 | 66 |
| `ebola-bdbv` | 909 | 107 | 802 | 42.3 | 4.9 | 0.0 | 1 |
| `ebola-sudan` | 636 | 636 | 0 | 29.0 | 28.9 | 0.1 | 1 |
| `ebola-zaire` | 12,036 | 11,985 | 51 | 562.1 | 558.1 | 1.1 | 9 |
| `hmpv` | 18,982 | 18,515 | 467 | 409.2 | 390.3 | 2.0 | 16 |
| `marburg` | 616 | 602 | 14 | 19.5 | 18.8 | 0.1 | 1 |
| `measles` | 54,914 | 52,782 | 2,132 | 1,305.3 | 1,211.2 | 3.6 | 29 |
| `mpox` | 17,799 | 16,153 | 1,646 | 7,693.0 | 6,946.7 | 9.9 | 79 |
| `rsv-a` | 53,085 | 52,720 | 365 | 1,431.2 | 1,409.4 | 5.5 | 44 |
| `rsv-b` | 40,664 | 40,307 | 357 | 1,132.9 | 1,113.3 | 4.3 | 34 |
| `west-nile` | 27,427 | 27,276 | 151 | 682.7 | 674.5 | 2.2 | 18 |
| `yellow-fever` | 2,368 | 2,366 | 2 | 60.4 | 60.0 | 0.4 | 4 |
| `zika` | 3,044 | 3,044 | 0 | 71.8 | 71.4 | 0.5 | 4 |
| **all** | **305,027** | **294,558** | **10,469** | **15,105.7** | **14,010.0** | **39.3** | **310** |

Compression is about 357:1 overall, because aligned sequences of one organism are
nearly identical to each other. Compressed per organism as here; a single stream compresses
at least as well. The genesis batch is therefore about **310 blobs in 52
transactions**, and the raw feed of about 15.1 GB becomes about 39 MB on-chain.

### Cost of the genesis batch

Blob gas is 131,072 per blob and is priced by the blob base fee, which floats with demand and
has a floor of 1 wei. Execution gas per transaction is an allowance of 120,000 at
1 gwei until the contract phase measures it. ETH at $2,683 (spot on 2026-09-30).
The mainnet blob base fee on 2026-09-30 was about 0.007 gwei.

| Blob base fee | Blob fees (ETH) | Execution fees (ETH) | Total (ETH) | Total (USD) |
|---|---:|---:|---:|---:|
| 0.007 gwei (today) | 0.0003 | 0.0062 | 0.0065 | 18 |
| 0.1 gwei | 0.0041 | 0.0062 | 0.0103 | 28 |
| 1 gwei | 0.0406 | 0.0062 | 0.0469 | 126 |
| 10 gwei | 0.4063 | 0.0062 | 0.4126 | 1,107 |
| 50 gwei (congested) | 2.0316 | 0.0062 | 2.0379 | 5,468 |

At today's blob prices the whole genesis batch costs less than a nice dinner, and the ordinary
execution gas of the 52 transactions is the larger part of it. The blob base fee only matters
when blob demand exceeds the network target: at 10 gwei the batch is about a thousand dollars.
Nothing about a backup is urgent, so the upload command reports the current blob base fee in its
dry run and the maintainer can simply wait for a quiet period.

### Growth

Pathoplexus grows mostly through automated ingest from INSDC. The per-record cost is tiny after
compression: about 133 compressed bytes per open record on average, so ten
thousand new records are roughly 11 blobs. One upload per
week or per month is well within a single transaction most of the time. The index record, which
lists every published accessionVersion, is about 12 bytes per entry after compression (an estimate
to be replaced by a measurement in the container format phase), so a full index today is about
3.5 MB, or 28 blobs; that is the cost the size
threshold for emitting an index has to justify.

### Recovery download

A full recovery downloads every blob once: 310 blobs × 131,072 bytes ≈ 41 MB
from whichever sources have them, then decompresses to about 14.0 GB of NDJSON.


## Operational surprises we expect

- **Archives churn.** One public blob archive shut down in 2025. Source lists are
  configuration, and the runbook tells maintainers to review them.
- **Ethereum parameters move.** Blob counts per transaction and per block, retention, and the
  shape of blob transactions themselves have changed at recent forks and will again. Constants
  in the code carry the date they were last verified.
- **The backend schema drifts.** The pinned schema is a snapshot. The upload command compares
  the live feed against it and refuses to publish on unexpected structure, and `SCHEMA`
  records in the stream carry each organism's field list so a decoder never depends on this
  repository.

## Open question: a donation pot

Requirement from Pathoplexus: an address anyone can send ETH to, whose only purpose is paying
the gas of uploads. No refunds to donors, no way to take the ETH out for anything else, and
the pot is used first with the maintainer's wallet covering any shortfall.

The constraint is that Ethereum always charges gas to the account that sends the transaction,
at the start of the transaction, and blob transactions must be sent by an ordinary account.
A contract cannot literally pay first. The proposed design is that `LoculusEternal` accepts
plain ETH transfers and, at the end of every `publish` call, refunds the publisher the actual
cost of that call, computed on-chain from the gas used, the block's base fee with a capped
tip, and the blob gas at the block's blob base fee, limited to whatever the pot holds. The
refund lands in the same transaction, so the net effect per upload is exactly "pot first,
wallet covers the shortfall", with the one difference that the wallet must hold the full
maximum fee for the instant the transaction runs. There would be no withdraw function at all.
The tip cap exists so that a careless or compromised publisher key cannot drain the pot by
paying absurd priority fees. This is parked and must be decided before the contract is
written, because the contract cannot be changed afterwards.

## What is deliberately not built

No storage providers, bonds, challenges, slashing, or custody proofs: availability is hedged,
not enforced. No zero-knowledge proofs of any kind: the contract sees every blob commitment
directly, so there is nothing to prove. No Merkle tree over chunks: KZG openings already prove
chunk membership. No generic dataset abstraction: this is for Pathoplexus, and generalisation
can follow if it works. No upgradeable contract, admin role, or pause. No daemon or scheduler.
