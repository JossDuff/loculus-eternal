# Loculus Eternal

A permanent, verifiable backup of any [Loculus](https://github.com/loculus-project/loculus) dataset that anyone can recover.

Data is published in Ethereum blobs ([What's a blob?](#whats-a-blob)) and their hashes anchored in an immutable contract. The data is recovered by fetching the blobs from anyone who has them (a beacon node, a blob archive such as Blobscan, IPFS) and checking every byte against the contract.

```bash
uv run loculus-eternal upload --config loculus-eternal.toml    # publish the loculus dataset
uv run loculus-eternal recover --config loculus-eternal.toml   # anyone can rebuild and verify the whole dataset from Ethereum
```

## Uploading

For the Loculus dataset maintainer. You'll need:

- [uv](https://docs.astral.sh/uv/)
- for the one-time contract deployment, [Foundry](https://getfoundry.sh/)
- an Ethereum RPC endpoint (ex: [dRPC](https://drpc.org/chainlist/ethereum-mainnet-rpc), [Alchemy](https://www.alchemy.com/), [Infura](https://www.infura.io/), etc)
- a wallet (ex: [Ambire](https://www.ambire.com/)) that holds some Ether on the network you publish to.  Its private key is the publisher private key below.
- optional: a [Kubo](https://docs.ipfs.tech/install/command-line/) IPFS node you control, reachable over its RPC API (`http://127.0.0.1:5001` by default). The `upload` then adds and pins every blob and a snapshot of the dataset there as well. Without it the blobs live only where Ethereum and the public archives keep them.

Start by cloning the repository:

```
git clone https://github.com/JossDuff/loculus-eternal
cd loculus-eternal
```

### 1. Deploy the contract (once)

Each dataset gets one `LoculusEternal` contract, deployed once onto Ethereum and never changed. The wallet
you name as the publisher is the only one that can ever publish to it. If that wallet ever has
to change, see [What can the publisher change in the contract?](#what-can-the-publisher-change-in-the-contract).

```
export LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY=0x…   # the wallet's private key
export LOCULUS_ETERNAL_PUBLISHER_PUBLIC_KEY=0x…    # the wallet's address
cd contracts
forge script script/Deploy.s.sol \
  --rpc-url "https://your-ethereum-rpc" \
  --private-key "$LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY" \
  --broadcast
cd ..
```

The output names the contract address and the block it was deployed in. Copy both for the
config file in the next step. If the deployment runs out of gas, add `--gas-estimate-multiplier 800`: Foundry's local simulation does not know every network's
gas schedule.

### 2. Upload data (repeatable)

Copy the example config into the repository directory and fill in the lines marked CHANGE,
including the contract address and deployment block from the step above:

```
cp config/sepolia.example.toml loculus-eternal.toml
```

The example is written for the Sepolia test network. For mainnet, set `chain_id = 1` and
use mainnet URLs for the RPC endpoint, the beacon node and Blobscan (`https://api.blobscan.com`).

Then, with the publisher private key in the environment:

```
export LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY=0x…
uv run loculus-eternal upload --config loculus-eternal.toml --check   # Check what would be published and the cost.  Nothing gets uploaded
uv run loculus-eternal upload --config loculus-eternal.toml           # publish it
```

Run it whenever you want to update the dataset on Ethereum. The upload works out what is already published and won't ever double-upload any data. With nothing new it does nothing.  Feel free to run it as frequent or as rarely as you want. If it is interrupted, run it again.

> The first upload of a dataset could take awhile.  It might take over an hour.

## Recovering

For anyone to run. No key is needed. Clone the repository:

```
git clone https://github.com/JossDuff/loculus-eternal
cd loculus-eternal
```

Then write a config file, `loculus-eternal.toml`, naming your RPC endpoint, the contract, and
where to look for blob bytes. The contract address is the one from
[the deploy section](#1-deploy-the-contract-once), ask the dataset's maintainer for it.

```toml
[chain]
rpc_url = "https://your-ethereum-rpc"
contract = "0x…"                  # the dataset's LoculusEternal contract, from whoever deployed it
chain_id = 1                      # 1 for Ethereum mainnet, 11155111 for the Sepolia test network

[[sources]]                       # tried in order, add as many as you like
type = "beacon"                   # Beacon endpoints only hold recent blobs, about 18 days
endpoints = ["https://beacon.example"]

[[sources]]
type = "blobscan"                 # public archive of older blobs
url = "https://api.blobscan.com"  # Blobscan's real mainnet API (api.sepolia.blobscan.com for Sepolia)

[[sources]]
type = "ipfs"                     # copies pinned on IPFS, found through the contract; needs a Kubo node
endpoints = ["http://127.0.0.1:5001"]
```

The IPFS source talks to a [Kubo](https://docs.ipfs.tech/install/command-line/) node over its
RPC API, so it needs a node you run or are allowed to use. The node fetches the blobs from
the IPFS network, so it need not hold them already. Leave that source out if you have no node.

```
uv run loculus-eternal recover --config loculus-eternal.toml
```

The dataset lands in `recovered/`, one file per organism in [NDJSON](https://github.com/ndjson/ndjson-spec)
(one JSON object per line, in the same shape as the Loculus backend's released-data feed) plus
a `recovery-report.json`. The command exits 0 only when every blob was found and every file
matches the publisher's digest. `recovery-data/` is the command's working state: the verified
blobs, the blob list, and `missing.json` listing anything it could not get and every source it
tried. It is safe to interrupt and run again, it picks up where it left off.

## Health check

For anyone to run. No key and no wallet. You'll need:

- [uv](https://docs.astral.sh/uv/)
- an Ethereum RPC endpoint
- the same config file as for [recovering](#recovering): the contract and the blob sources to check

```
uv run loculus-eternal health --config loculus-eternal.toml
```

Opens a page at <http://127.0.0.1:8765> that checks the contract, fetches every blob from
every source, and answers one question: "is the dataset still recoverable?" Add `--once` to
print the result as JSON instead, for scripts.

## FAQ

### What's Ethereum?  What's a blockchain?  Why would I want this?

Reasonable questions!  "Blockchain" is a bit of a buzzword term.

A blockchain is essentially a database kept by thousands of independent computers that all check each
other's work. Once something is written into it, nobody can change or delete it. That is exactly the property a backup wants and that ordinary
hosting cannot give. A server can be switched off, a bucket can be deleted, a maintainer can
leave, but a hash written into Ethereum in 2026 is still there, unchanged, for as long as
Ethereum runs.

[Ethereum](https://ethereum.org/en/what-is-ethereum/) is the largest and most secure blockchain and allows for
general-purpose programs on it. It has run without interruption since 2015, and its
[blobs](#whats-a-blob) give it a cheap lane for bulk data. Using it here gives three things. A record of every published entry that
no one can alter. A way for anyone to prove a copy of the data is genuine. And copies spread
across parties who do not know each other, so the dataset outlives its publisher.

### What's a blob?

A blob is a 128 KB chunk of data attached to an Ethereum transaction. Ethereum keeps the
bytes for about eighteen days and keeps the blob's hash forever. Blobs were added in 2024
([EIP-4844](https://eips.ethereum.org/EIPS/eip-4844), the "proto-danksharding" upgrade) as a cheaper data storage.   Loculus Eternal uses them as its publication lane: the dataset is
split into blobs, each blob's hash is recorded in the contract, and anyone holding the bytes
can prove they are the ones that were published. More on
[ethereum.org](https://ethereum.org/en/roadmap/danksharding/).

### Who's storing the data?

- **Ethereum itself**, for about eighteen days after each upload, on every consensus node.
- **Blob archives** such as [Blobscan](https://blobscan.com), which keep blobs past that
  window and serve them by hash. (See [What's a blob?](#whats-a-blob).)
- **IPFS** (if enabled), where the maintainer's nodes pin a copy of every blob and a snapshot of the
  dataset, and anyone else may pin them too.
- **Anyone who ever ran `recover`**, since the verified blobs stay in their `recovery-data/`
  and can be offered as a source to others.

The hashes in the contract are what make all of these interchangeable: a copy from any of
them is checked against the chain before it is trusted. The data is lost only if every one of
them loses the same bytes.

### How can I check if a dataset is still recoverable?

Use the [health check](#health-check)!

### How much does this cost?

For the [Pathoplexus](https://pathoplexus.org/) dataset, measured in 2026: about 15 GB of released data compresses to under 50 MB, which is about 375 blobs in 63 transactions.

At the usual blob price of well under 0.1 gwei, the whole thing costs a few
hundredths of an ETH, mostly ordinary transaction fees: tens of dollars. At 1 gwei (a busy
period) about 0.05 ETH. The upload command shows the live price before sending, and
nothing about a backup is urgent, so the maintainer can wait for a quiet day.

Each later upload publishes only the new entries, so it costs a fraction of the initial upload.

### What can the publisher change in the contract?

Very little, on purpose. There is no upgrade, no pause and no delete, so a published blob is in
the record for good. The publisher wallet can do exactly three things, each a single call made
with Foundry's `cast` and the private key in the environment.

- **Hand over to a new wallet**, for a team change or to retire a key before it leaks. From the
  current wallet:

  ```
  cast send 0x…contract… "setPublisher(address)" 0x…new wallet address… \
    --rpc-url "https://your-ethereum-rpc" --private-key "$LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY"
  ```

  The old wallet loses access at once. Export the new wallet's private key before the next
  upload. This does not help after a theft, since whoever holds the key can hand over first.
- **Re-point the IPFS snapshot**, with `setAppPointer(string)` and a CID, if the snapshot the
  contract names was lost and you pinned a new one. Every upload sets it anyway.
- **Leave a breadcrumb to a replacement contract**, once, ever, with `setSuccessor(address)`.
  The health check reports it. Nothing follows it automatically. The old contract keeps
  serving its data, and `recover` still works against it.

If the private key is lost, nothing more can be published to that contract, but everything in
it stays recoverable forever. Deploy a new contract and upload the dataset to it afresh.

### What does the data look like on-chain?

The published entries are written as one canonical JSON line each, concatenated in a fixed
order and compressed with zstd. Aligned sequences of one organism are nearly identical to
each other, so the compression ratio is over 300 to 1: 15 GB of feed becomes under 50 MB of
blobs. No compression dictionary is used; each batch stands on its own, so a reader needs
nothing but the blobs. Batches are self-describing: every batch carries a header, a manifest
with the digest of every file it produces, and now and then an index so a reader need not
replay the whole stream. Nothing is ever deleted. An entry that Loculus later withdraws stays
in the blobs forever, but a withdrawal record tells every `recover` to leave it out of the
files it produces.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
