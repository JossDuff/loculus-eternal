# Working rules for this repository

Loculus Eternal publishes Pathoplexus's released sequence data into Ethereum blobs, anchors
every blob's versioned hash in an immutable contract, mirrors the bytes on IPFS, and ships a
recovery command that rebuilds the database from the contract address alone. Read
`docs/design.md` for the why and `loculus-eternal-PLAN.md` for the phase roadmap and the
record of decisions.

## Order of work

Spec first, vectors second, code third. The container format, the contract, and the IPFS
profile are permanent once the first byte is published on mainnet. The normative documents
are `docs/container-spec.md`, `docs/contract.md`, and `docs/ipfs-profile.md`; the golden
vectors in `vectors/` are generated, never hand-edited. Every component tests against the
same vectors. Never edit a vector to make a test pass: regenerate it from the generator and
explain the diff.

## When something is unclear

If two documents disagree, or a rule is ambiguous, stop and ask. Never invent an encoding,
hash rule, constant, or state transition silently. A silent guess here becomes permanent.

## Names, not numbers

Refer to phases of work by name (groundwork, container format, contract, anvil harness and
KZG, recovery command, upload command, IPFS, Sepolia campaign, mainnet genesis and handover).
Never cite milestone numbers or section numbers anywhere a human will read: not in chat, not
in comments, not in commit messages, not in docs. Code comments explain the rule or rationale
in plain language where the reader is. The one allowed identifier is a test-plan behaviour ID
(a letter and a number from `docs/test-plan.md`) in a test name.

## Tests

`docs/test-plan.md` lists every behaviour with an ID. A test that covers a behaviour carries
that ID in its name. Any commit that adds a mechanism updates the test plan in the same
commit. Run `uv run pytest` and, for the contract, `forge test` before committing.

## Secrets

The publisher key is read only from the environment variable `LOCULUS_ETERNAL_PUBLISHER_KEY`.
No credentials in code, configuration files, fixtures, or docs.

## Commits and branches

Small, scoped commits. Spec changes and code changes go in separate commits. One branch and
one pull request per phase; the human reviews and merges.

## Stack

Python 3.12 or newer for everything off-chain, one package `loculus_eternal` managed with
`uv` and a pinned `uv.lock`. Solidity with Foundry for the contract. The Rust predecessor at
`/home/joss/dev/pathoplexus/blobsitter` is reference reading only; nothing is copied from it.
