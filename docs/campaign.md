# The Sepolia campaign

The last phase before anything touches mainnet. Its purpose is to run the whole system once
for real, on a public testnet, with the full dataset, and to come back with measurements and
surprises rather than estimates. When it ends, the container format, the contract and the
IPFS profile are frozen.

## What is needed before it starts

| Input | Why | Who |
|---|---|---|
| A Sepolia JSON-RPC endpoint | the upload and recovery commands read and write the chain through it | team |
| A beacon API serving historical Sepolia blobs, or a semi-supernode | the upload command reads its own blobs back; recovery from a clean machine needs a source for blobs older than 18 days | team |
| Sepolia ETH in the publisher wallet | the genesis batch is about 310 blobs in 52 transactions; the wallet must hold the maximum fee up front even though the likely cost is far lower | team |
| Where the Kubo nodes run | the `[ipfs]` endpoints; a single node is enough for the campaign | team |
| The donation pot decision | the contract cannot change after deployment and freezes at the end of the campaign | Pathoplexus |

## Steps

1. **Deploy.** From `contracts/`:

   ```
   PUBLISHER=0x<publisher address> forge script script/Deploy.s.sol --rpc-url $RPC --broadcast --private-key $DEPLOYER_KEY --gas-estimate-multiplier 800
   ```

   The multiplier matters when the network runs a fork Foundry's local simulation does not
   know: Sepolia moved to Glamsterdam on 2026-10-06, gas was repriced, and the default
   estimate ran out of gas. Unused gas is refunded, so a generous limit costs nothing.
   Record the address and the deployment block in the config and in `docs/testnet-report.md`.
   Verify the source on a block explorer so the bytecode is checkable.

2. **Configure.** Copy `config/sepolia.example.toml` to `loculus-eternal.toml` in the checkout
   and fill in the marked lines. Export the publisher key.

3. **Rehearse the refusals.** `loculus-eternal upload --config loculus-eternal.toml --check`
   should report about 294,000 entries and the estimated cost. Run `--dry-run` once. Try a
   wrong key once to see the refusal.

4. **Genesis.** `loculus-eternal upload --config loculus-eternal.toml`. Expect the run to take
   on the order of an hour: most of it is encoding, the snapshot, and waiting for finality on
   each of the 52 transactions. Record every number the run prints in the report.

5. **Deltas.** Over the following week or two, run the command whenever the backend has new
   releases (the `--check` output says). Record each run's size, cost and duration. At least
   one delta should be run from a second machine with an empty data directory, to prove the
   published set really comes from the chain.

6. **Recovery from a clean machine.** On a machine that has never seen the data directory,
   with a config naming only public sources (the beacon API, Blobscan, and the IPFS snapshot),
   run `loculus-eternal recover`. Record the sources that answered, the time taken, and
   confirm every digest matches. Repeat once with the beacon source removed, so only
   archives and IPFS serve, and once from IPFS alone.

7. **Interrupt on purpose.** Kill the upload command once after its first transaction of a
   multi-transaction batch and run it again; then delete the journal after another such kill
   and run again, to see the torn-batch path on a real network.

8. **Write it up.** Fill in `docs/testnet-report.md`: costs, timings, every operational
   surprise, and what should change before mainnet.

9. **Freeze.** Mark `docs/container-spec.md`, `docs/contract.md` and `docs/ipfs-profile.md`
   as frozen, bump the package version, and tag the release. The tooling records in the
   mainnet genesis will carry that version.

## What a full-scale rehearsal on anvil measured

Run on 2026-10-05 with the real dataset (every organism's release feed as downloaded on
2026-09-30), on a 14-core machine with 30 GB of memory, against anvil and one Kubo node:

| Step | Measured |
|---|---|
| Entries in the genesis batch | 294,558 in 15 organisms |
| Blobs and transactions | 310 blobs in 52 transactions |
| `--check` (read feeds, encode) | 12.8 minutes, peak memory 1.7 GB |
| Publish: encode | 13 minutes |
| Publish: build and pin the snapshot | 3.5 minutes |
| Publish: send 52 transactions (anvil, immediate finality) | 33 minutes, about 38 seconds each, mostly cell-proof computation |
| Whole publish run | 49 minutes, peak memory 2.1 GB |
| A rerun with nothing new | 3.3 minutes, almost all of it decoding the stream to learn the published set |
| Execution gas per transaction | 43,378 to 64,283, mean 44,110 |
| Recovery from a local copy, including decode and digest check | 3.7 minutes |
| Recovery from IPFS alone | 3.8 minutes |
| Disk used during the run | about 2 times the decompressed dataset: spill files plus snapshot build; keep 40 GB free |

Every digest matched, every per-organism count matched the backend's open count, and the
two recoveries produced identical files.

What it means for Sepolia: on a real network each transaction also waits for finality, about
13 minutes, so with the sending rule as it stood (next transaction only after the previous
is final) genesis would take about 11 hours of mostly waiting. See the sending decision in
the plan file.

## Two forks

Mainnet is on Fusaka and will move to Glamsterdam some months after this project goes live;
Sepolia moved to Glamsterdam on 2026-10-06, so the campaign runs on the later fork and the
test suite's anvil runs on the earlier one. The code is written to survive both without a
change: every gas figure comes from the node's estimate at the time of sending, never from
a constant; blob transactions carry the Fusaka sidecar, which Glamsterdam keeps; and the
one protocol number the command relies on, blobs per transaction, is a configuration value
(`upload.max_blobs_per_transaction`, 6 today) to be re-checked at each fork. Genesis on
mainnet happens under Fusaka prices, which are the ones in `docs/design.md`.

## What the campaign cannot tell us

Sepolia blob fees do not predict mainnet fees, and Sepolia archives are thinner than
mainnet's. The campaign proves the mechanism and the operations; the mainnet cost table in
`docs/design.md` stays the cost estimate.
