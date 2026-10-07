# Sepolia campaign report

**Status: running since 2026-10-07.** Filled in as the campaign runs. Numbers here are
measured on Sepolia, not estimated.

## Deployment

| | |
|---|---|
| Contract address | `0x1b141bfbe4db1335c62f8a281b372eea0cfeef21` |
| Deployment block | 11,863,798 (2026-10-07) |
| Deployment transaction | `0x9f6b9e8e9e3a995fa910d5f117927fdd4079435c0c8ad1edccfa6238713de972` |
| Deployer | `0xE30036308E139D4aC1e07D0f3F3D3cd6597fBBD0` (the publisher, a development key) |
| Publisher | `0xE30036308E139D4aC1e07D0f3F3D3cd6597fBBD0` |
| Gas used | 3,925,933 at 0.0011 gwei |
| Compiler and settings | Solidity 0.8.30, Prague, optimizer 10000 runs, no metadata hash |
| Verified on explorer | not yet |

**First surprise, before anything was published.** The first deployment attempt
(`0x7ea631d7…`) ran out of gas. Glamsterdam activated on Sepolia on 2026-10-06 and reprices
gas: the node estimated 3.96 million gas for the deployment where Foundry 1.7.1's local
simulation, which does not know the fork, estimated 580 thousand and added 30%. The retry
with `--gas-estimate-multiplier 800` succeeded. The upload and recovery commands ask the
node for estimates, so they see the new prices; Foundry scripts do not. Mainnet is not on
Glamsterdam yet, so Sepolia gas figures in this report do not predict mainnet.

## Genesis

**Second surprise: the batch-end transaction ran out of gas (2026-10-07).** The first genesis
attempt on the real network sent 59 transactions back to back. 58 were included, publishing
blobs 0 to 347; the 59th, which carries the manifest and the pointer, reverted with exactly
its gas limit used (150,863). The limit had been borrowed from the first transaction's
estimate plus a fixed margin, and under Glamsterdam the pointer's fresh storage write alone
costs about 130,000 gas, so the batch-end transaction needed around 180,000. The 348 blobs
are a torn batch that every reader skips; on Sepolia that cost nothing, on mainnet it would
have been the whole genesis. The submitter now waits for the batch-end transaction's
predecessors to be included and asks the node for its own estimate, resends a transaction
that reverted without moving the contract instead of abandoning the batch, and stops a run
outright when a batch is abandoned rather than planning another on a lagging finalized
view. A third thing surfaced at the same time: Infura's free tier answered the first burst
of fee lookups with HTTP 429, and the run died with a traceback before sending anything; the
connection now spaces its requests and retries.

**Genesis, second attempt**

| | |
|---|---|
| Date | |
| Entries published | |
| Blobs / transactions | |
| Wall-clock duration | |
| Encoding time | |
| Snapshot build and add time | |
| Finality wait, per transaction | |
| Blob gas price range | |
| Execution gas per transaction | |
| Total cost (ETH) | |
| Snapshot CID | |
| Surprises | |

## Deltas

| Date | Entries | Blobs | Transactions | Duration | Cost (ETH) | Machine | Notes |
|---|---|---|---|---|---|---|---|

## Recovery from a clean machine

| Run | Sources configured | Sources that answered | Blobs missing | Duration | Digests match |
|---|---|---|---|---|---|

## Interruptions

| What was interrupted | What the next run did | Torn blobs left | Notes |
|---|---|---|---|

## What should change before mainnet

-

## Frozen

- Container format: not yet
- Contract: not yet
- IPFS profile: not yet
