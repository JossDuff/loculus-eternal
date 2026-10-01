from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol

from loculus_eternal import kzg


class SourceError(Exception):
    """The source could not answer at all (network error, bad response). Not a mismatch."""


@dataclass(frozen=True)
class BlobContext:
    """What a source may need to locate a blob besides its versioned hash."""

    block_number: int | None
    block_timestamp: int | None

    def slot(self, genesis_time: int, seconds_per_slot: int) -> int | None:
        if self.block_timestamp is None:
            return None
        return (self.block_timestamp - genesis_time) // seconds_per_slot


class BlobSource(Protocol):
    name: str

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        """Return candidate blobs for the wanted versioned hashes. Unverified; may be anything."""
        ...


def verify_candidates(candidates: Iterable[bytes], wanted: set[bytes]) -> tuple[dict[bytes, bytes], int]:
    """Keep candidates whose recomputed versioned hash is one we want.

    Returns (verified blobs by versioned hash, number of candidates rejected). A candidate
    is rejected if it is not a valid blob or if its hash is not wanted, which covers both
    corrupted bytes and a source returning the wrong blob.
    """
    accepted: dict[bytes, bytes] = {}
    rejected = 0
    for blob in candidates:
        try:
            vh = kzg.blob_to_versioned_hash(blob)
        except kzg.KzgError:
            rejected += 1
            continue
        if vh in wanted and vh not in accepted:
            accepted[vh] = blob
        else:
            rejected += 1
    return accepted, rejected


@dataclass
class Attempt:
    source: str
    outcome: str  # "ok", "partial", "nothing", "error"
    detail: str = ""


@dataclass
class AcquireResult:
    blobs: dict[bytes, bytes]
    attempts: list[Attempt] = field(default_factory=list)
    rejected: int = 0

    def missing(self, wanted: Iterable[bytes]) -> list[bytes]:
        return [vh for vh in wanted if vh not in self.blobs]


class SourceChain:
    """Asks each source, in order, only for the hashes still missing."""

    def __init__(self, sources: list[BlobSource]):
        self.sources = list(sources)

    def acquire(self, ctx: BlobContext, wanted: list[bytes]) -> AcquireResult:
        result = AcquireResult(blobs={})
        remaining = list(wanted)
        for source in self.sources:
            if not remaining:
                break
            try:
                candidates = source.fetch(ctx, remaining)
            except SourceError as e:
                result.attempts.append(Attempt(source.name, "error", str(e)))
                continue
            accepted, rejected = verify_candidates(candidates, set(remaining))
            result.rejected += rejected
            result.blobs.update(accepted)
            remaining = [vh for vh in remaining if vh not in result.blobs]
            if not accepted:
                outcome = "nothing"
            elif remaining:
                outcome = "partial"
            else:
                outcome = "ok"
            detail = f"{len(accepted)} verified" + (f", {rejected} rejected" if rejected else "")
            result.attempts.append(Attempt(source.name, outcome, detail))
        return result
