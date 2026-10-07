# The health page

`loculus-eternal health --config loculus-eternal.toml` starts a local web server and opens
one page that answers a single question for anyone who asks it: **is the published record
still available, and still correct?** It needs the same configuration file as the recovery
command and no key. `--once` runs one check, prints the report as JSON and exits with status
0 only when the verdict is healthy, so a script or a cron job can use it too.

The page trusts nothing. It reads the chain, fetches every blob from every configured
source, verifies each against the chain, and compares what the sources and IPFS claim with
what the contract says. It materialises nothing: the dataset's files are not rebuilt, and a
check over today's stream takes well under a minute. To rebuild the files, run
`loculus-eternal recover`.

## What one check does

The page opens with the facts that matter most: the contract address and network, the
publisher (the only key that can publish), when the last upload landed, how many blobs
verify from the sources, whether the IPFS snapshot verifies, and when this check last ran.
Below them the work is presented as three checks: the **contract check** (the first two rows
below), the **blob check** (the next two) and the **IPFS check** (the last). The check never
consults the live Pathoplexus database: the record is judged on its own, against the chain.
The steps in detail:

| Step | What is checked | What a problem looks like |
|---|---|---|
| Chain | The contract has code at the finalized block; its publisher, blob count, head, pointer and successor; the block and age of the most recent publish | the contract cannot be read; a successor is set (noted) |
| Blob list | The ordered list of versioned hashes from event logs reproduces `head` and `blobCount` | the list does not match the chain, or logs cannot be read |
| Sources | Every configured source is asked for every blob; each candidate is verified by recomputing its KZG commitment | a blob that no source can supply (failing); a source serving corrupt bytes (noted, rejected) |
| Stream | The outer records of the verified blobs: header, batch headers, body lengths and digests, indexes, manifests; the header's chain and contract binding; torn ranges | a torn range at the end (an unfinished or lost upload); a header naming another chain or contract |
| IPFS snapshot | The configured snapshot CID hashes to the chain's pointer; its manifest matches the chain; its blob objects are retrievable and verify; the spec is present. Every read asks the endpoint offline with a short deadline, so the question is whether this node holds the content, answered in seconds | a snapshot that is not the publisher's current one; an endpoint that no longer holds it |

The **verdict** is healthy when the contract is reachable, the blob list verifies, every
blob verifies from at least one source, the stream has no torn tail, and the snapshot matches
the pointer. It is failing when something is unverifiable or wrong, degraded when something
is off but the record itself is intact. Every problem is listed at the top of the page.

Blobs older than the 18-day consensus retention are expected to be missing from a beacon
source; the page says how many of a source's misses fall outside retention so that is not
read as a failure.

## Running it

```
loculus-eternal health --config loculus-eternal.toml            # serve on http://127.0.0.1:8765 and open a browser
loculus-eternal health --config loculus-eternal.toml --once     # one check, JSON on stdout, progress on stderr
```

The configuration needs `[chain]`, the `[[sources]]` to check, and, for the snapshot check,
an `ipfs` source with the published `snapshot_cid`. `[backend]` and `[upload]` are ignored
and no key is needed, which is the point: anyone can run this against anyone's deployment.

The page is one HTML file with inline styles and script and loads nothing from anywhere, so
it works offline against a local node. A check runs when the page opens; the button runs
another. While a check runs, a bar across the top shows which of the three checks it is on
and what it is doing, including how many blobs each source has been asked for so far.

The page opens with a **Deployment** card: the contract address with a link to a block
explorer, the network, the deployment block, and the inputs this check runs with: the RPC
endpoint, every blob source, the IPFS endpoints and the config file. Every URL
is reduced to its scheme and host before it reaches the page, because hosted RPC and archive
URLs carry API keys in their path, and this page is meant to be shown around. A quickstart
card gives the two commands that matter, and the footer links to the repository.

Behaviour IDs for the page are `H1`–`H7` in `docs/test-plan.md`.
