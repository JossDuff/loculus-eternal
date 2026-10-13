# Loculus Eternal

A permanent, verifiable backup of any [Loculus](https://github.com/loculus-project/loculus) dataset that anyone can recover.

Data is published in Ethereum blobs and noted in an immutable contract. The data is recovered by fetching the blobs from anyone who has them (a beacon node, a blob archive such as Blobscan, IPFS) and checking every byte against the contract.

```bash
uv run loculus-eternal upload --config loculus-eternal.toml    # publish the loculus dataset
uv run loculus-eternal recover --config loculus-eternal.toml   # anyone can rebuild and verify the whole dataset from Ethereum
```

## Uploading

For the Loculus dataset maintainer. You'll need:

- [uv](https://docs.astral.sh/uv/)
- an Ethereum RPC endpoint ([dRPC](https://drpc.org/chainlist/ethereum-mainnet-rpc), [Alchemy](https://www.alchemy.com/), [Infura](https://www.infura.io/), etc)
- a wallet that holds some Ether on the network you publish to
- a deployed `LoculusEternal` contract with that wallet as its publisher (`contracts/script/Deploy.s.sol` deploys one)
- optional: a [Kubo](https://docs.ipfs.tech/install/command-line/) IPFS node you control, reachable over its RPC API (`http://127.0.0.1:5001` by default). The upload then adds and pins every blob and a snapshot of the dataset there as well. Without it the blobs live only where Ethereum and the public archives keep them.

Copy `config/sepolia.example.toml` to `loculus-eternal.toml` and fill in the lines marked CHANGE. Put the publisher private key in the environment:

```
export LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY=0x…
uv run loculus-eternal upload --config loculus-eternal.toml --check   # Check what would be published and the cost.  Nothing gets uploaded
uv run loculus-eternal upload --config loculus-eternal.toml           # publish it
```

The upload works out what is already published by reading the chain, so it runs from any
machine. With nothing new it does nothing. If it is interrupted, run it again.

## Recovering

For anyone to run. No key is needed. The config file names your RPC endpoint, the contract, and
where to look for blob bytes:

```toml
[chain]
rpc_url = "https://your-ethereum-rpc"
contract = "0x…"
chain_id = 1                      # 1 for Ethereum mainnet, 11155111 for the Sepolia test network

[[sources]]                       # tried in order, add as many as you like
type = "beacon"                   # recent blobs, about 18 days
endpoints = ["https://beacon.example"]

[[sources]]
type = "blobscan"                 # public archive of older blobs
url = "https://api.blobscan.com"

[[sources]]
type = "ipfs"                     # copies pinned on IPFS, found through the contract
endpoints = ["http://127.0.0.1:5001"]
```

The current deployment is a test on Sepolia: contract `0x7b19365a0e811901Cb53aaA8bfaA82854c46914D`, chain id `11155111`.

```
uv run loculus-eternal recover --config loculus-eternal.toml
```

The dataset lands in `recovered/`, one file per organism in [NDJSON](https://github.com/ndjson/ndjson-spec)
(one JSON object per line, in the same shape as the Loculus backend's released-data feed) plus
a `recovery-report.json`. The command exits 0 only when every blob was found and every file
matches the publisher's digest. `recovery-data/` is the command's working state: the verified
blobs, the blob list, and `missing.json` listing anything it could not get and every source it
tried. It is safe to interrupt and run again; it picks up where it left off.

## Health check

For anyone to run. You'll need uv, an Ethereum RPC endpoint and the same config file as for
recovering: the contract and the blob sources to check. No key and no wallet.

```
uv run loculus-eternal health --config loculus-eternal.toml
```

Opens a page at <http://127.0.0.1:8765> that checks the contract, fetches every blob from
every source, and answers one question: "is the dataset still recoverable?" Add `--once` to
print the result as JSON instead, for scripts.

## FAQ

### What's Ethereum?  What's a blockchain?  Why would I want this?

Reasonable questions!  "Blockchain" is a bit of a buzzword term.

A blockchain is a ledger kept by thousands of independent computers that all check each
other's work. Once something is written into it, nobody can quietly change or delete it:
every copy would disagree. That is exactly the property a backup wants and that ordinary
hosting cannot give. A server can be switched off, a bucket can be deleted, a maintainer can
leave; a hash written into Ethereum in 2026 is still there, unchanged, for as long as
Ethereum runs.

[Ethereum](https://ethereum.org/en/what-is-ethereum/) is the largest such network with
general-purpose programs on it. It has run without interruption since 2015, and its
[blobs](#whats-a-blob) give it a cheap lane for bulk data. What you get from using it here:
a record of every published entry that no one can alter, a way for anyone to prove a copy of
the data is genuine, and copies spread across parties who do not know each other. The
dataset outlives its publisher.

### What's a blob?

A blob is a 128 KB chunk of data attached to an Ethereum transaction. Ethereum keeps the
bytes for about eighteen days and keeps the blob's hash forever. Blobs were added in 2024
([EIP-4844](https://eips.ethereum.org/EIPS/eip-4844), the "proto-danksharding" upgrade) to
give rollups a cheap place to post data, and they are much cheaper than writing the same
bytes into a contract. Loculus Eternal uses them as its publication lane: the dataset is
split into blobs, each blob's hash is recorded in the contract, and anyone holding the bytes
can prove they are the ones that were published. More on
[ethereum.org](https://ethereum.org/en/roadmap/danksharding/).

### Who's storing the data?

- **Ethereum itself**, for about eighteen days after each upload, on every consensus node.
- **Blob archives** such as [Blobscan](https://blobscan.com), which keep blobs past that
  window and serve them by hash. (See [What's a blob?](#whats-a-blob).)
- **IPFS**, where the maintainer's nodes pin a copy of every blob and a snapshot of the
  dataset, and anyone else may pin them too.
- **Anyone who ever ran `recover`**, since the verified blobs stay in their `recovery-data/`
  and can be offered as a source to others.

The hashes in the contract are what make all of these interchangeable: a copy from any of
them is checked against the chain before it is trusted. The data is lost only if every one of
them loses the same bytes.

### How can I check if a dataset is still recoverable?

Use the [health check](#health-check)!

### How much does this cost?

For the Pathoplexus dataset, measured in 2026: about 15 GB of released data compresses to
under 50 MB, which is about 375 blobs in 63 transactions.

- **As blobs**, at the usual blob price of well under 0.1 gwei, the whole thing costs a few
  hundredths of an ETH, mostly ordinary transaction fees: tens of dollars. At 1 gwei, a busy
  period, about 0.05 ETH. The upload command shows the live price before sending, and
  nothing about a backup is urgent, so the maintainer can wait for a quiet day.
- **As contract storage**, the way a smart contract would store the same 50 MB, it would be
  about 30 billion gas: tens of ETH, several times that at mainnet gas prices, and the raw
  15 GB would be thousands of ETH. That is why blobs exist.

Each later upload publishes only the new entries, so it costs a fraction of the genesis.

### What does the data look like on-chain?

The published entries are written as one canonical JSON line each, concatenated in a fixed
order and compressed with zstd. Aligned sequences of one organism are nearly identical to
each other, so the compression ratio is over 300 to 1: 15 GB of feed becomes under 50 MB of
blobs. No compression dictionary is used; each batch stands on its own, so a reader needs
nothing but the blobs. Batches are self-describing: every batch carries a header, a manifest
with the digest of every file it produces, and now and then an index so a reader need not
replay the whole stream. Nothing is ever deleted. An entry that Loculus later withdraws stays
in the blobs forever, but a withdrawal record tells every reader to leave it out of the
files it produces.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
