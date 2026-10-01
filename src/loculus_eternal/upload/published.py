"""What has already been published, learned from the chain and the stream.

The upload command never trusts a local list of what it published before. It reads the
contract, obtains the verified blob list, fetches and verifies any blobs it does not already
hold (its own earlier uploads are written into the same store at publish time, so normally
nothing needs fetching), and decodes the stream. From that it gets the encoder state to
append the next batch, the set of published accessionVersions, and the previously published
payloads the cumulative digests need. Blobs after the last complete batch are a torn batch:
an upload that was interrupted. The journal decides whether it can be resumed; otherwise the
next batch simply starts at the next blob boundary and the torn blobs stay dead bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from web3 import Web3

from loculus_eternal.chain import BlobRef, ChainReader, ChainState, verify_manifest
from loculus_eternal.format.decode import DecodedStream, StreamDecoder
from loculus_eternal.format.encode import EncoderState
from loculus_eternal.sources.base import SourceChain
from loculus_eternal.store import BlobStore, fill_store


class PublishedSetError(RuntimeError):
    pass


@dataclass
class PublishedView:
    state: ChainState
    refs: list[BlobRef]
    decoded: DecodedStream
    encoder_state: EncoderState
    torn_blobs: int               # blobs on chain after the last complete batch

    def is_published(self, organism: str, accession: str, version: int) -> bool:
        return (accession, version) in self.decoded.entries.get(organism, {})

    def previous_entries(self, organism: str):
        return self.decoded.payloads(organism)


def load_published_view(w3: Web3, contract: str, sources: SourceChain, store: BlobStore, *, deployment_block: int = 0, log=print) -> PublishedView:
    reader = ChainReader(w3, contract)
    state = reader.state_at_finalized()
    refs = reader.blob_refs_from_logs(deployment_block, state.block_number) if state.blob_count else []
    verify_manifest(refs, state)

    # Fill the store with whatever is not already there, verifying every byte.
    _, _, missing = fill_store(store, sources, refs, log=log)
    if missing:
        m = missing[0]
        tried = ", ".join(f"{a['source']}: {a['outcome']}" for a in m["tried"]) or "no sources configured"
        raise PublishedSetError(
            f"blob {m['seq']} of the published stream could not be obtained from any source ({tried}). "
            "The upload command needs the whole stream to know what is already published; add a source that has it."
        )

    decoded = StreamDecoder(store.blobs(state.blob_count)).decode() if state.blob_count else StreamDecoder([]).decode()
    enc_state = decoded.encoder_state()
    # The next batch starts after every blob the contract has recorded, including torn ones.
    enc_state.next_blob_seq = state.blob_count
    torn = state.blob_count - (decoded.batches[-1].blob_count_after if decoded.batches else 0)
    if torn:
        log(f"{torn} blob(s) on chain after the last complete batch: an earlier upload was interrupted")
    return PublishedView(state=state, refs=refs, decoded=decoded, encoder_state=enc_state, torn_blobs=torn)
