# Loculus Eternal

Permanent, verifiable backup of [Pathoplexus](https://pathoplexus.org) released data, using
Ethereum blobs for publication, an immutable contract for the record of what was published,
and IPFS plus public blob archives for keeping the bytes around.

**Status: upload command in progress.** Nothing has been published yet. The design is in
[`docs/design.md`](docs/design.md); the roadmap and every decision so far are in
[`loculus-eternal-PLAN.md`](loculus-eternal-PLAN.md).

## What it will do

- `loculus-eternal upload` — a one-off command a Pathoplexus maintainer runs whenever there is
  new released data. It works out what is not yet published by reading the chain, packs the new
  entries into Ethereum blobs, records their hashes in the `LoculusEternal` contract, and pins
  the same bytes on IPFS. Running it again with nothing new does nothing.
- `loculus-eternal recover` — rebuilds the whole released dataset from nothing but the contract
  address and network access, fetching blob bytes from anyone who has them and verifying every
  byte against the chain before trusting it.

## How to publish

See [`docs/runbook.md`](docs/runbook.md). In short: a config file naming the chain, the
contract and the backend (with `organisms = "all"`, or a list); the publisher key in the environment variable
`LOCULUS_ETERNAL_PUBLISHER_KEY`; then `loculus-eternal upload --config loculus-eternal.toml`,
with `--check` first to see what would be published and what it costs.

## How to recover

Recovery needs an Ethereum RPC endpoint, the contract address, and a list of places to try
for blob bytes. Put them in a TOML file:

```toml
[chain]
rpc_url = "https://your-ethereum-rpc"
contract = "0x…"                 # the LoculusEternal instance
chain_id = 1                     # 1 mainnet, 11155111 Sepolia; sets the beacon genesis time
deployment_block = 0             # optional: where to start scanning event logs

[recover]
data_dir = "recovery-data"       # verified blobs land here; safe to interrupt and rerun
out_dir = "recovered"            # one NDJSON file per organism plus recovery-report.json
# manifest_sources = ["logs", "manifest.json"]   # where to get the blob list; each is verified against the chain

[[sources]]                      # tried in order; every byte is verified, so order is about speed
type = "beacon"                  # a consensus node or hosted beacon API (recent blobs only)
endpoints = ["https://beacon.example"]

[[sources]]
type = "blobscan"                # public archive, looked up by versioned hash
url = "https://api.blobscan.com"

[[sources]]
type = "blob-archiver"           # anything serving the beacon sidecar shape
url = "https://archive.example"

[[sources]]
type = "local"                   # a directory of <versioned hash>.blob files, e.g. from another recovery
path = "/mnt/blobs"

[[sources]]
type = "ipfs"                    # blob objects located through a published snapshot
endpoints = ["http://127.0.0.1:5001"]
snapshot_cid = "bafybei…"        # the publisher's announced snapshot; checked against the chain's pointer
```

To recover with nothing but IPFS, add `"ipfs:bafybei…"` to `manifest_sources` as well.

Then:

```
loculus-eternal recover --config recover.toml
```

The command reads the contract at a finalized block, obtains the ordered list of blob hashes
and checks it against the contract's `head`, fetches every blob it does not already hold,
verifies each one against its hash before writing it, decodes the stream, writes the
per-organism files, and compares their digests with the publisher's manifest. Missing blobs
are listed in `recovery-data/missing.json` with every source that was tried. Exit status is 0
only when nothing is missing and every digest matches.

## Why

Pathoplexus's data is meant to outlive Pathoplexus. Ethereum keeps the hash of every published
blob forever, so anyone holding a copy of the bytes can prove it is genuine. The bytes
themselves are kept by Ethereum for about eighteen days, by public blob archives for as long
as they operate, by IPFS pinners for as long as they pin, and by anyone who ever ran the
recovery command. The data is lost only if all of them lose the same bytes.

Once published, nothing can be removed. See the design doc for what that means and why it was
accepted.

## Licence

AGPL-3.0-or-later. See [LICENSE](LICENSE).
