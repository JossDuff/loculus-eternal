"""The decoded entries of a stream, kept on disk as one sorted run per organism per batch.

The decoder writes each batch's entries into a run file and asks the store to check the
cumulative digests before the run is accepted; a run that fails is deleted and leaves no
trace. Readers get each organism's entries by a k-way merge of its runs, in materialisation
order, with the earliest batch winning a duplicate key. Only the keys (accession, version,
batch) live in memory, which is a few hundred thousand small tuples for Pathoplexus.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import BinaryIO, Iterator

from loculus_eternal.format.runs import EntryKey, Record, iter_run, merge_runs


def safe_dirname(organism: str) -> str:
    """A filesystem-safe directory name for an organism identifier from an untrusted stream.

    Letters, digits, dot, underscore and hyphen pass through; anything else becomes %XX, and
    a name that would be empty or a path step ("." or "..") is prefixed so it cannot be one.
    """
    name = re.sub(r"[^A-Za-z0-9._-]", lambda m: "%%%02X" % ord(m.group()), organism)
    if name in ("", ".", "..") or name.startswith("-"):
        name = "_" + name
    return name


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

    def iter_sorted(self, organism: str, extra_run: tuple[int, Path] | None = None) -> Iterator[tuple[int, Record]]:
        """(batch, (accession, version, payload)) in materialisation order, the earliest batch
        winning a duplicate key."""
        runs = list(self.runs.get(organism, []))
        if extra_run is not None:
            runs.append(extra_run)
        return merge_runs(runs)

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

    def materialise_to(self, organism: str, out: BinaryIO | None, extra_run: tuple[int, Path] | None = None) -> tuple[str, int]:
        """Write the organism's output file (if `out` is given) while hashing it. Returns the
        SHA-256 hex digest and the number of lines. This is the one definition of the file."""
        h = hashlib.sha256()
        n = 0
        for _, (_, _, payload) in self.iter_sorted(organism, extra_run):
            if out is not None:
                out.write(payload)
                out.write(b"\n")
            h.update(payload)
            h.update(b"\n")
            n += 1
        return h.hexdigest(), n

    def digest(self, organism: str, extra_run: tuple[int, Path] | None = None) -> tuple[str, int]:
        return self.materialise_to(organism, None, extra_run)

    def materialise(self, organism: str) -> bytes:
        import io

        buf = io.BytesIO()
        self.materialise_to(organism, buf)
        return buf.getvalue()

    # --- writes --------------------------------------------------------------------------------

    def organism_dir(self, organism: str) -> Path:
        d = self.directory / safe_dirname(organism)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def staging_path(self, organism: str, batch: int) -> Path:
        return self.organism_dir(organism) / f"batch-{batch}.staging"

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

    def destroy(self) -> None:
        """Remove every run file. The store is a working copy; the stream is the record."""
        shutil.rmtree(self.directory, ignore_errors=True)
        self.runs = {}
        self.index = {}
