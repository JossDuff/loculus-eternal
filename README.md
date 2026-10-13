# Loculus Eternal

A permanent, verifiable backup of [Pathoplexus](https://pathoplexus.org) released data,
published in Ethereum blobs and recorded in an immutable contract.

Anyone can rebuild the whole dataset from two facts: the contract address and the chain it
lives on. Every byte is checked against Ethereum before it is trusted.

## Quickstart

You need [uv](https://docs.astral.sh/uv/) and an Ethereum RPC endpoint.

```
git clone https://github.com/JossDuff/loculus-eternal
cd loculus-eternal
cp config/sepolia.example.toml loculus-eternal.toml   # then edit the lines marked CHANGE
uv run loculus-eternal recover --config loculus-eternal.toml
```

The current deployment is a test on Sepolia:

```
contract = "0x7b19365a0e811901Cb53aaA8bfaA82854c46914D"
chain_id = 11155111
```

## Uploading

For the Pathoplexus maintainer. Put the publisher private key in the environment, never in a file:

```
export LOCULUS_ETERNAL_PUBLISHER_KEY=0x…
uv run loculus-eternal upload --config loculus-eternal.toml --check   # what would be published, and the cost
uv run loculus-eternal upload --config loculus-eternal.toml           # publish it
```

The upload works out what is already published by reading the chain, so it runs from any
machine. With nothing new it does nothing. If it is interrupted, run it again.

The full procedure is in [the runbook](docs/runbook.md).

## Recovering

For anyone. No key is needed. The config file names your RPC endpoint, the contract, and
where to look for blob bytes:

```toml
[chain]
rpc_url = "https://your-ethereum-rpc"
contract = "0x…"
chain_id = 1                      # 1 for mainnet, 11155111 for Sepolia

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

```
uv run loculus-eternal recover --config loculus-eternal.toml
```

The dataset lands in `recovered/`, one file per organism. The command exits 0 only when
every blob was found and every file matches the publisher's digest. Anything missing is
listed in `recovery-data/missing.json`. It is safe to interrupt and run again.

## Health check

```
uv run loculus-eternal health --config loculus-eternal.toml
```

Opens a page at http://127.0.0.1:8765 that checks the contract, fetches every blob from
every source, and answers one question: is the dataset still recoverable? Add `--once` to
print the result as JSON instead, for scripts. More in
[the health page doc](docs/health-page.md).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
