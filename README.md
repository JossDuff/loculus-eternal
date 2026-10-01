# Loculus Eternal

Permanent, verifiable backup of [Pathoplexus](https://pathoplexus.org) released data, using
Ethereum blobs for publication, an immutable contract for the record of what was published,
and IPFS plus public blob archives for keeping the bytes around.

**Status: container format in progress.** Nothing has been published yet. The design is in
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
