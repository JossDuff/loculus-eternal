# IPFS profile

**Status: normative draft, frozen after the Sepolia campaign.** Implementation in
`src/loculus_eternal/ipfs.py`.

IPFS keeps a file for as long as at least one node pins it, and addresses it by a content
identifier (CID) derived from the bytes and from the way they were split and linked. Two
nodes adding the same bytes with different settings get different CIDs. This profile fixes
the settings so that every pinner of Loculus Eternal data agrees on every address.

## Blob objects

Each published blob is one IPFS object holding exactly its 131,072 bytes as a **single raw
block**: CID version 1, codec `raw` (0x55), multihash `sha2-256`. No UnixFS wrapper, no
chunking. The CID is therefore computable from the bytes alone:

```
cid = "b" + base32lower( 0x01 ‖ 0x55 ‖ 0x12 ‖ 0x20 ‖ sha256(blob) )
```

In Kubo: `ipfs block put --cid-codec=raw --mhtype=sha2-256 --pin`. The upload command
computes the CID locally and refuses a node whose answer differs.

A blob object is verified the same way as any blob: recompute its KZG commitment and
compare with the versioned hash on the chain. The CID only locates it.

## The snapshot

After every batch the publisher adds one UnixFS **directory** with these parameters, which
are Kubo's `ipfs add` flags:

```
--cid-version=1 --raw-leaves --chunker=size-262144 --hash=sha2-256 --pin
```

The directory contains, by these exact names:

| Name | Content |
|---|---|
| `manifest.json` | the chain id, the contract address, `blobCount` and `head` after the batch, the list of batches (`batch`, `firstBlobSeq`, `lastBlobSeq`, `manifestDigest`), and every blob in order as `seq`, `versionedHash`, `cid`, plus `blockNumber` and `blockTimestamp` when the publisher knew them at the time (always for blobs of earlier batches, never for the batch the snapshot was built for, since it had not been sent yet) |
| `container-spec.md` | the container specification the stream was written to |
| `<organism>.ndjson.zst` | the materialised file of each organism, as the recovery command writes it, compressed with zstd (one frame, no dictionary) |

All three kinds of member are required; a snapshot without the spec is not a snapshot under
this profile. `manifest.json` is pretty-printed JSON with sorted keys and a trailing newline. The
compressed files are **not** canonical: a different zstd build produces different bytes for
the same content, so a pinner keeps the publisher's bytes by CID rather than regenerating
them. What is verifiable is the decompressed content, whose SHA-256 is the cumulative
`artifactSha256` in the stream's last batch manifest.

The materialised files are rebuildable from the blobs by anyone with the recovery command;
the snapshot is a convenience, and deliberately small (tens of megabytes) rather than the
plain files (gigabytes), so that pinners carry little per upload.

## The on-chain pointer

The contract's `appPointer` is the SHA-256 of the snapshot CID's **binary** form (the bytes
the base32 string encodes: version, codec, multihash), not of the string. A reader who is
handed a snapshot CID checks `sha256(cid_bytes) == appPointer` before using its manifest.
The pointer is written by the batch-end transaction, so it always names the snapshot of the
batch that transaction completed.

Hashing the CID rather than storing it keeps the on-chain word fixed at 32 bytes whatever
the CID's length, at the cost that the CID itself must be learned off-chain: the upload
command writes it to its run report, and the publisher is expected to publish it on a
public page. The chain then verifies it.

## Recovering from IPFS alone

1. Obtain the current snapshot CID from the publisher's page or from anyone, and the
   contract's `appPointer`, `blobCount` and `head` from any Ethereum node.
2. Check the CID against the pointer. Fetch `manifest.json` and check the blob list against
   `head` as for any manifest.
3. Fetch each blob object by its CID, verify it against its versioned hash, store it. The
   recovery command also fills in missing block numbers from event logs when a node still
   serves them, so beacon-style sources can help with blobs the IPFS nodes have lost.
4. Decode. The `.ndjson.zst` files are a shortcut: decompress and compare their SHA-256 with
   the stream's own cumulative digests before trusting them.

The recovery command does this when an `ipfs` source with a `snapshot_cid` is configured.

## Who pins

Pinning targets are configuration in the upload command's `[ipfs]` section: every listed
Kubo endpoint receives every blob object and the snapshot. Each blob object carries its own
pin and is never unpinned. Snapshots are kept one at a time: once the batch whose pointer
names a new snapshot is final, the upload command unpins the previous snapshot on every
endpoint that holds the new one, so a node carries every blob object plus the latest
snapshot and nothing accumulates. An old snapshot's blocks leave the node at its next
garbage collection; nothing in them is lost, since every file is rebuildable from the blob
objects. The upload command remembers which snapshot each endpoint holds, so an endpoint
that was unreachable for one batch still lets go of the older snapshot it has when it next
takes a new one; and a batch that is refused or abandoned after its snapshot and blob
objects were added has those pins removed again. The runbook recommends at least two
team-operated nodes and one pinning service.
IPFS is a hedge beside consensus retention, public archives and local copies, never the
only place the bytes live.
