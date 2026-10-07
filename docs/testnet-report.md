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
