# Test plan

Every behaviour this project promises has an ID here. A test that covers a behaviour carries
the ID in its name (`test_F3_partial_final_blob_round_trip`, `invariant_C2_head_matches_events`).
Any commit that adds a mechanism adds its behaviours here in the same commit. Coverage is
audited against this list before each phase's pull request merges.

IDs are a family letter and a number. The families are reserved now and filled in by the
phase that builds them.

| Family | Component | Filled in during |
|---|---|---|
| `F` | Container format: records, batches, index, canonical JSON, materialisation, vectors | container format |
| `C` | Contract `LoculusEternal` | contract |
| `K` | KZG wrapper and anvil harness | anvil harness and KZG |
| `R` | Recovery command: manifest acquisition, sources, store, decoder | recovery command |
| `U` | Upload command: published set, sync, eligibility, planner, submitter, journal | upload command |
| `I` | IPFS profile, pinning, IPFS source | IPFS |

## Layers

Each family's tests fall into these layers, listed from cheapest to most expensive:

- **Vector conformance.** The component reproduces every golden vector in `vectors/`
  byte-for-byte. Vectors are generated, never hand-edited.
- **Behaviour.** Unit and property tests for one stated behaviour.
- **Harness.** Tests against a real anvil chain with real type-3 transactions, and against a
  local Kubo for IPFS.
- **Fault injection.** Sources that withhold, corrupt, or mislabel; RPC that flaps; processes
  killed mid-run.
- **Process.** Checks that guard the repository itself: committed vectors equal regenerated
  vectors; no human-facing text cites milestone or section numbers; no secrets in the tree.

## Behaviours

### F — container format

*To be filled in during the container format phase.*

### C — contract

*To be filled in during the contract phase.*

### K — KZG and harness

*To be filled in during the anvil harness and KZG phase.*

### R — recovery command

*To be filled in during the recovery command phase.*

### U — upload command

*To be filled in during the upload command phase.*

### I — IPFS

*To be filled in during the IPFS phase.*

## Process checks (in force from the groundwork phase)

- **P1 — no numbered references.** `grep -rnE 'M[0-9]|§' README.md CLAUDE.md docs/ loculus-eternal-PLAN.md src/ tests/ contracts/src/` returns nothing.
- **P2 — no secrets.** No private key, token, or password appears anywhere in the tree; the publisher key is read only from `LOCULUS_ETERNAL_PUBLISHER_KEY`.
