"""Fetching released data from a Loculus backend and selecting what is eligible to publish.

`GET {backend}/{organism}/get-released-data?compression=zstd` streams every released
accessionVersion as NDJSON. The response is cached on disk by ETag so that a rerun with an
unchanged backend does not download again. Only entries whose data use terms are OPEN are
eligible; a restricted entry becomes eligible the day it turns open. Each eligible line is
projected to the published form, and the line's shape is checked against the pinned schema
so that a backend change is caught before it reaches the stream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import httpx
import zstandard

from loculus_eternal.format.encode import DATA_KEYS, Entry
from loculus_eternal.format.records import FormatError

EXPECTED_TOP_LEVEL = ("metadata", *DATA_KEYS)


class SyncError(RuntimeError):
    pass


@dataclass
class OrganismFeed:
    organism: str
    etag: str | None
    total_records: int | None
    path: Path            # cached compressed NDJSON
    from_cache: bool


class BackendClient:
    def __init__(self, base_url: str, cache_dir: Path, *, client: httpx.Client | None = None, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.client = client or httpx.Client(timeout=timeout)
        self._etags_path = self.cache_dir / "etags.json"
        self._etags: dict[str, str] = json.loads(self._etags_path.read_text()) if self._etags_path.exists() else {}

    def fetch(self, organism: str) -> OrganismFeed:
        """Download the organism's release feed unless the cached copy is still current."""
        cached = self.cache_dir / f"{organism}.ndjson.zst"
        headers = {"Accept": "application/x-ndjson"}
        if organism in self._etags and cached.exists():
            headers["If-None-Match"] = self._etags[organism]
        url = f"{self.base_url}/{organism}/get-released-data"
        try:
            with self.client.stream("GET", url, params={"compression": "zstd"}, headers=headers) as r:
                if r.status_code == 304:
                    return OrganismFeed(organism, self._etags.get(organism), None, cached, from_cache=True)
                if r.status_code != 200:
                    raise SyncError(f"{url} returned HTTP {r.status_code}")
                tmp = cached.with_suffix(".tmp")
                with open(tmp, "wb") as f:
                    for chunk in r.iter_raw():
                        f.write(chunk)
                tmp.replace(cached)
                etag = r.headers.get("etag")
                total = r.headers.get("x-total-records")
        except httpx.HTTPError as e:
            raise SyncError(f"cannot reach {url}: {e}") from e
        if etag:
            self._etags[organism] = etag
            self._etags_path.write_text(json.dumps(self._etags, indent=1))
        return OrganismFeed(organism, etag, int(total) if total else None, cached, from_cache=False)


def iter_lines(feed: OrganismFeed) -> Iterator[dict]:
    """Decode the cached feed line by line without holding it all in memory."""
    with open(feed.path, "rb") as f, zstandard.ZstdDecompressor().stream_reader(f) as reader:
        buffer = b""
        while True:
            chunk = reader.read(1 << 20)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                if line:
                    yield json.loads(line)
        if buffer.strip():
            yield json.loads(buffer)


def check_shape(organism: str, line: dict) -> None:
    """Refuse a line whose top-level shape differs from the pinned schema."""
    keys = tuple(line)
    if set(keys) != set(EXPECTED_TOP_LEVEL):
        unexpected = sorted(set(keys) - set(EXPECTED_TOP_LEVEL))
        missing = sorted(set(EXPECTED_TOP_LEVEL) - set(keys))
        raise SyncError(f"{organism}: released line has an unexpected shape (unexpected keys {unexpected}, missing {missing}); the backend schema has changed and the pinned schema must be reviewed before publishing")
    m = line["metadata"]
    for k in ("accession", "version", "accessionVersion", "dataUseTerms"):
        if k not in m:
            raise SyncError(f"{organism}: released line lacks metadata.{k}")


def is_eligible(line: dict) -> bool:
    return line["metadata"].get("dataUseTerms") == "OPEN"


@dataclass
class SyncStats:
    organism: str
    total: int = 0
    open: int = 0
    restricted: int = 0
    already_published: int = 0
    new: int = 0
    seen: set = field(default_factory=set)       # every accessionVersion the feed contained, any terms
    vanished: list = field(default_factory=list)  # published, not withdrawn, and no longer in the feed


def iter_new_entries(organism: str, feed: OrganismFeed, is_published, stats: SyncStats) -> Iterator[dict]:
    """Yield the projection of every eligible, not-yet-published line, filling `stats` as it
    goes. `is_published(accession, version)` is answered from the chain-derived set. Nothing
    is held: the caller consumes the entries as they are produced."""
    for line in iter_lines(feed):
        stats.total += 1
        check_shape(organism, line)
        stats.seen.add(line["metadata"]["accessionVersion"])
        if not is_eligible(line):
            stats.restricted += 1
            continue
        stats.open += 1
        m = line["metadata"]
        if is_published(m["accession"], int(m["version"])):
            stats.already_published += 1
            continue
        try:
            entry = Entry.from_released_line(organism, line)
        except FormatError as e:
            raise SyncError(f"{organism} {m.get('accessionVersion')}: {e}") from e
        stats.new += 1
        yield entry


def select_new_entries(organism: str, feed: OrganismFeed, is_published) -> tuple[list[dict], SyncStats]:
    """The in-memory form of iter_new_entries, for tests and small feeds."""
    stats = SyncStats(organism)
    return list(iter_new_entries(organism, feed, is_published, stats)), stats


def find_vanished(organism: str, stats: SyncStats, published: dict[str, list[list[int]]], withdrawn: dict[str, list[int]]) -> list[str]:
    """accessionVersions published for this organism, not withdrawn, that the feed no longer
    contains. These are reported, never acted on without the maintainer's say-so."""
    gone = []
    for accession, versions in published.items():
        for version, _ in versions:
            av = f"{accession}.{version}"
            if av not in stats.seen and version not in withdrawn.get(accession, []):
                gone.append(av)
    stats.vanished = sorted(gone)
    return stats.vanished
