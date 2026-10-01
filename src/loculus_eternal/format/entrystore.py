"""The decoded entries of a stream, kept on disk as one sorted run per organism per batch.

The decoder writes each batch's entries into a run file and asks the store to check the
cumulative digests before the run is accepted; a run that fails is deleted and leaves no
trace. Readers get each organism's entries by a k-way merge of its runs, in materialisation
order, with the earliest batch winning a duplicate key. Only the keys (accession, version,
batch) live in memory, which is a few hundred thousand small tuples for Pathoplexus.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import BinaryIO, Callable, Iterator

from loculus_eternal.format.runs import EntryKey, Record, iter_run, merge_runs


class EntryStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # organism -> list of (batch, run path), in batch order
        self.runs: dict[str, list[tuple[int, Path]]] = {}
        # organism -> (accession, version) -> batch
        self.index: dict[str, dict[EntryKey, int]] = {}

    # --- queries -------------------------------------------------------------------------------

    def organisms(self) -> list[str]:
        return sorted(self.index)

    def has(self, organism: str, accession: str, version: int) -> bool:
        return (accession, version) in self.index.get(organism, {})

    def count(self, organism: str) -> int:
        return len(self.index.get(organism, {}))

    def iter_sorted(self, organism: str, extra_run: tuple[int, Path] | None = None, on_duplicate: Callable | None = None) -> Iterator[tuple[int, Record]]:
        """(batch, (accession, version, payload)) in materialisation order."""
        runs = list(self.runs.get(organism, []))
        if extra_run is not None:
            runs.append(extra_run)
        return merge_runs(runs, on_duplicate=on_duplicate)

    def iter_records(self, organism: str) -> Iterator[Record]:
        for _, rec in self.iter_sorted(organism):
            yield rec

    def payload(self, organism: str, accession: str, version: int) -> bytes | None:
        """One entry's payload; a linear scan of the winning run, meant for tests and tools."""
        batch = self.index.get(organism, {}).get((accession, version))
        if batch is None:
            return None
        for b, path in self.runs[organism]:
            if b == batch:
                for a, v, p in iter_run(path):
                    if a == accession and v == version:
                        return p
        return None

    def materialise_to(self, organism: str, out: BinaryIO, extra_run: tuple[int, Path] | None = None) -> str:
        """Write the organism's output file and return its SHA-256 hex digest."""
        h = hashlib.sha256()
        for _, (_, _, payload) in self.iter_sorted(organism, extra_run):
            out.write(payload)
            out.write(b"\n")
            h.update(payload)
            h.update(b"\n")
        return h.hexdigest()

    def digest(self, organism: str, extra_run: tuple[int, Path] | None = None) -> str:
        h = hashlib.sha256()
        for _, (_, _, payload) in self.iter_sorted(organism, extra_run):
            h.update(payload)
            h.update(b"\n")
        return h.hexdigest()

    def materialise(self, organism: str) -> bytes:
        import io

        buf = io.BytesIO()
        self.materialise_to(organism, buf)
        return buf.getvalue()

    # --- writes --------------------------------------------------------------------------------

    def staging_path(self, organism: str, batch: int) -> Path:
        d = self.directory / organism
        d.mkdir(exist_ok=True)
        return d / f"batch-{batch}.staging"

    def accept(self, organism: str, batch: int, staged: Path, keys: list[EntryKey]) -> list[EntryKey]:
        """Take a verified staged run into the store. Returns the keys that were duplicates of
        entries already present (they stay in the file but the earlier batch wins)."""
        final = staged.with_suffix(".run")
        staged.replace(final)
        self.runs.setdefault(organism, []).append((batch, final))
        idx = self.index.setdefault(organism, {})
        duplicates = []
        for k in keys:
            if k in idx:
                duplicates.append(k)
            else:
                idx[k] = batch
        return duplicates

    def discard(self, staged: Path) -> None:
        staged.unlink(missing_ok=True)

    def close(self) -> None:
        pass

    def destroy(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)
