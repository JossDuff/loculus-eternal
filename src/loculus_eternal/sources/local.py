"""Local directory source: files named `<versioned hash hex>.blob` holding raw blob bytes.

This is how one recovered copy becomes a source for another, and how a maintainer can hand
over blobs on a disk. The recovery store can export into this layout.
"""

from __future__ import annotations

from pathlib import Path

from loculus_eternal.sources.base import BlobContext


class LocalDirectorySource:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.name = f"local({self.directory})"

    @staticmethod
    def filename(versioned_hash: bytes) -> str:
        return versioned_hash.hex() + ".blob"

    def fetch(self, ctx: BlobContext, wanted: list[bytes]) -> list[bytes]:
        out: list[bytes] = []
        for vh in wanted:
            path = self.directory / self.filename(vh)
            if path.is_file():
                out.append(path.read_bytes())
        return out
