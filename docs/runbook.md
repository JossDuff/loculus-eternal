# Runbook: publishing Pathoplexus data with Loculus Eternal

For the person on the Pathoplexus team who runs `loculus-eternal upload`. You do not need
to understand Ethereum internals to follow this; where a step can go wrong, the command says
so in plain words and this document says what to do about it.

## What the command does

`loculus-eternal upload` publishes every released, open-access entry that is not yet in the
permanent record. It is a one-off command: run it whenever you know there is new data, or
on whatever rhythm you like. Running it when nothing is new does nothing and exits 0. It is
safe to interrupt at any point and run again.

One run does, in order:

1. Reads the contract on Ethereum and works out exactly what has already been published, by
   fetching and verifying the published stream. Nothing local is trusted for this.
2. Finishes an interrupted earlier run if there is one.
3. Downloads each organism's release feed from the backend (skipping organisms whose feed has
   not changed), selects the entries that are open and not yet published.
4. Packs them into one batch, simulates the first transaction against the contract, checks the
   current fees and your wallet balance, and refuses with a reason if anything is off.
5. Sends the transactions one by one, raising the fee and resending if one is not picked up,
   and waits until each is final (about 13 minutes on mainnet).
6. Writes a report and keeps a copy of the published blobs locally.

## One-time setup

1. Install: `uv sync` in a checkout of the repository (Python 3.12 or newer, `uv`).
2. Create a config file. Start from this and change the four marked lines:

   ```toml
   [chain]
   rpc_url = "https://your-ethereum-rpc"         # CHANGE: an Ethereum JSON-RPC endpoint
   contract = "0x…"                              # CHANGE: the LoculusEternal address
   chain_id = 1                                  # 1 mainnet, 11155111 Sepolia
   deployment_block = 0                          # CHANGE: block the contract was deployed in

   [backend]
   url = "https://backend.pathoplexus.org"
   organisms = ["andv", "cchf", "dengue", "ebola-bdbv", "ebola-sudan", "ebola-zaire", "hmpv",
                "marburg", "measles", "mpox", "rsv-a", "rsv-b", "west-nile", "yellow-fever", "zika"]

   [upload]
   data_dir = "upload-data"                      # local state; keep it, but losing it is survivable
   tooling_paths = ["docs/container-spec.md", "docs/contract.md", "docs/ipfs-profile.md",
                    "src/loculus_eternal/**/*.py", "pyproject.toml", "uv.lock"]
   max_blob_fee_gwei = 5                         # refuse to publish when blob space is pricier than this
   max_priority_fee_gwei = 1

   [[sources]]                                   # where the command gets the published stream back from
   type = "beacon"                               # CHANGE: a beacon API with historical blobs (see below)
   endpoints = ["https://your-beacon-api"]

   [[sources]]
   type = "blobscan"
   url = "https://api.blobscan.com"
   ```

   Paths in `tooling_paths` are relative to the config file's directory, so keep the file in
   the repository checkout.

3. Put the publisher key in the environment, never in a file:

   ```
   export LOCULUS_ETERNAL_PUBLISHER_KEY=0x…
   ```

   The command refuses to start without it (except `--check`). The key's wallet must hold
   enough ETH for the batch; `--check` tells you how much.

## Running

```
loculus-eternal upload --config loculus-eternal.toml --check     # what would be published and what it costs; sends nothing
loculus-eternal upload --config loculus-eternal.toml --dry-run   # everything except sending
loculus-eternal upload --config loculus-eternal.toml             # publish
```

Exit status 0 means published or nothing to do. Status 1 means the command refused before
spending anything; the message says why. Status 3 means sending started and stopped; the
journal is kept and the next run resumes it.

A report for every run is written to `upload-data/reports/`.

## When the command refuses

**"the key in the environment is not the contract's publisher"**: the wrong key is exported,
or the publisher was rotated to another key. Check `publisher()` on the contract.

**"the publisher wallet holds X ETH but the batch needs up to Y"**: top up the wallet. The
"up to" figure is the maximum fee; the likely cost is much lower and is printed too.

**"the blob base fee is X gwei, above the configured limit"**: blob space is busy. Nothing
about a backup is urgent; try later, or raise `max_blob_fee_gwei` if it stays high for days.

**"the contract's blob count moved since this batch was planned"**: another run published in
between, from another machine or by someone else with the key. Just run again; the new run
will see what they published and publish only the rest.

**"released line has an unexpected shape"**: the backend's data format changed. Do not
publish until someone has reviewed the pinned schema in `docs/container-spec.md` and updated
the code. This check exists so that a format change is caught here and not in the permanent
record.

**"blob N of the published stream could not be obtained from any source"**: the command needs
the whole published stream to know what is already published, and none of the configured
sources had that blob. Add a source that has it (a beacon node with historical blobs, Blobscan,
or a directory of blobs exported from a recovery) and run again.

## When a run is interrupted

If the process dies after a transaction was sent, the next run says "resuming interrupted
batch" and carries on. Nothing is duplicated: each transaction states which position in the
record it expects, and the contract rejects anything out of order.

If the local `upload-data` directory is lost as well, the next run notices blobs on the chain
that belong to no complete batch, reports them as a torn batch, and starts a fresh batch
after them. The torn blobs cost their fees but harm nothing: every reader skips them.

## Sources for reading the stream back

The command reads its own earlier publications back from the network, which is also how it
proves to itself that they are recoverable. A beacon API serves blobs for about 18 days;
after that, only archives and your own local copy have them. The local copy in
`upload-data/stream` is used first, so in normal operation nothing is downloaded. Keep at
least one archive source configured for the day the local copy is lost. Public archives come
and go; if one disappears, replace it in the config.

## Rotating the publisher key

From the current key, call `setPublisher(newAddress)` on the contract (any wallet tool that
can call a contract will do), then export the new key. Do this before a key is at risk, not
after: a key that is already compromised can rotate the role away from you.
