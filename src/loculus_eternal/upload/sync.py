"""Fetching released data from a Loculus backend and selecting what is eligible to publish.

`GET {backend}/{organism}/get-released-data?compression=zstd` streams every released
accessionVersion as NDJSON. The response is cached on disk by ETag so that a rerun with an
unchanged backend does not download again. Only entries whose data use terms are OPEN are
eligible; a restricted entry becomes eligible the day it turns open. With a minimum age
configured, an entry is eligible only once that version was submitted long enough ago. Each
eligible line is projected to the published form, and the line's shape is checked against the
pinned schema so that a backend change is caught before it reaches the stream.

The eligible entries of a feed are the copy of the database a run publishes. The record is
made to hold exactly them: what is eligible and unpublished is published, what is published
and no longer eligible is withdrawn, and what is withdrawn and eligible again is reinstated.
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
        raw = json.loads(self._etags_path.read_text()) if self._etags_path.exists() else {}
        # Each organism remembers its ETag and the record count the backend reported, so a
        # cached feed can still show progress as x of y.
        self._etags: dict[str, dict] = {k: (v if isinstance(v, dict) else {"etag": v, "total": None}) for k, v in raw.items()}

    def organisms(self) -> list[str]:
        """Every organism the backend serves, from the enum in its OpenAPI description."""
        url = f"{self.base_url}/api-docs"
        try:
            r = self.client.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            raise SyncError(f"cannot reach {url} to list the backend's organisms: {e}") from e
        if r.status_code != 200:
            raise SyncError(f"{url} returned HTTP {r.status_code}; list the organisms in the config instead")
        try:
            names = r.json()["components"]["schemas"]["Organism"]["enum"]
        except (ValueError, KeyError, TypeError) as e:
            raise SyncError(f"{url} does not enumerate organisms the way Loculus does ({e}); list them in the config instead") from e
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise SyncError(f"{url} does not list organisms as a list of names; list them in the config instead")
        return sorted(names)

    def fetch(self, organism: str) -> OrganismFeed:
        """Download the organism's release feed unless the cached copy is still current."""
        cached = self.cache_dir / f"{organism}.ndjson.zst"
        headers = {"Accept": "application/x-ndjson"}
        remembered = self._etags.get(organism)
        if remembered and cached.exists():
            headers["If-None-Match"] = remembered["etag"]
        url = f"{self.base_url}/{organism}/get-released-data"
        try:
            with self.client.stream("GET", url, params={"compression": "zstd"}, headers=headers) as r:
                if r.status_code == 304:
                    return OrganismFeed(organism, remembered["etag"], remembered.get("total"), cached, from_cache=True)
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
        count = int(total) if total else None
        if etag:
            self._etags[organism] = {"etag": etag, "total": count}
            self._etags_path.write_text(json.dumps(self._etags, indent=1))
        return OrganismFeed(organism, etag, count, cached, from_cache=False)

    def check_complete(self, feed: OrganismFeed, lines_read: int) -> None:
        """Refuse a feed that held a different number of entries than the backend said it
        would. An entry missing from a feed is withdrawn from the record, so a response that
        arrived short or empty (a proxy answering for a backend that is down, a migration half
        done) must stop the run, not be taken for a database that lost those entries. The
        cached copy is dropped with it, or the next run would be answered "unchanged" and
        read the same short file again. A backend that reports no count cannot be checked."""
        if feed.total_records is None or lines_read == feed.total_records:
            return
        self._etags.pop(feed.organism, None)
        self._etags_path.write_text(json.dumps(self._etags, indent=1))
        feed.path.unlink(missing_ok=True)
        raise SyncError(f"{feed.organism}: the backend said its feed holds {feed.total_records:,} released entries and {lines_read:,} arrived. Publishing from an incomplete feed would withdraw the entries that did not arrive, so nothing was sent; the cached copy was discarded, run again")


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


def submitted_by(organism: str, metadata: dict, cutoff: int) -> bool:
    """Whether this version was submitted at or before the cutoff, a Unix time. Every version
    of an accession carries its own submission time, so a revision waits its own term. A line
    without a usable time is an error, never a guess in either direction."""
    at = metadata.get("submittedAtTimestamp")
    if isinstance(at, bool) or not isinstance(at, int):
        raise SyncError(f"{organism} {metadata.get('accessionVersion')}: the minimum age rule needs metadata.submittedAtTimestamp as a whole number of seconds, and it is {at!r}")
    return at <= cutoff


@dataclass
class SyncStats:
    organism: str
    total: int = 0
    open: int = 0
    restricted: int = 0
    held_back: int = 0          # open, but submitted too recently for the minimum age
    already_published: int = 0
    new: int = 0
    eligible: set = field(default_factory=set)        # every accessionVersion of the feed that is eligible: the copy
    to_withdraw: list = field(default_factory=list)   # published, not withdrawn, and not in the copy
    to_reinstate: list = field(default_factory=list)  # withdrawn, and in the copy again


PROGRESS_EVERY = 10_000


def iter_new_entries(organism: str, feed: OrganismFeed, is_published, stats: SyncStats, progress=None, submitted_cutoff: int | None = None) -> Iterator[dict]:
    """Yield the projection of every eligible, not-yet-published line, filling `stats` as it
    goes. `is_published(accession, version)` is answered from the chain-derived set. Nothing
    is held: the caller consumes the entries as they are produced. `progress(read, total)` is
    called every PROGRESS_EVERY lines so a long feed shows signs of life. `submitted_cutoff`
    is the latest submission time, as a Unix time, that is old enough to publish; None means
    no minimum age."""
    for line in iter_lines(feed):
        stats.total += 1
        check_shape(organism, line)
        entry = _classify(organism, line, is_published, stats, submitted_cutoff)
        # Report after the line is classified, so the "new" count includes this line.
        if progress is not None and stats.total % PROGRESS_EVERY == 0:
            progress(stats.total, feed.total_records)
        if entry is not None:
            yield entry


def _classify(organism: str, line: dict, is_published, stats: SyncStats, submitted_cutoff: int | None):
    """Count the line as restricted, held back, already published or new; return the entry
    if new."""
    if not is_eligible(line):
        stats.restricted += 1
        return None
    stats.open += 1
    m = line["metadata"]
    if submitted_cutoff is not None and not submitted_by(organism, m, submitted_cutoff):
        stats.held_back += 1
        return None
    stats.eligible.add(m["accessionVersion"])
    if is_published(m["accession"], int(m["version"])):
        stats.already_published += 1
        return None
    try:
        entry = Entry.from_released_line(organism, line)
    except FormatError as e:
        raise SyncError(f"{organism} {m.get('accessionVersion')}: {e}") from e
    stats.new += 1
    return entry


def select_new_entries(organism: str, feed: OrganismFeed, is_published, submitted_cutoff: int | None = None) -> tuple[list[dict], SyncStats]:
    """The in-memory form of iter_new_entries, for tests and small feeds."""
    stats = SyncStats(organism)
    return list(iter_new_entries(organism, feed, is_published, stats, submitted_cutoff=submitted_cutoff)), stats


def find_changes(stats: SyncStats, published: dict[str, list[list[int]]], withdrawn: dict[str, list[int]]) -> tuple[list[str], list[str]]:
    """What it takes, beyond publishing the new entries, for the record to hold exactly the
    eligible entries of the feed just read: the accessionVersions that are published, not
    withdrawn and not eligible, to withdraw; and those that are withdrawn and eligible, to
    reinstate. `published` and `withdrawn` are this organism's, as the stream has them. Call
    it once the feed has been read to the end."""
    gone = []
    for accession, versions in published.items():
        for version, _ in versions:
            av = f"{accession}.{version}"
            if av not in stats.eligible and version not in withdrawn.get(accession, []):
                gone.append(av)
    back = [f"{accession}.{version}" for accession, versions in withdrawn.items() for version in versions if f"{accession}.{version}" in stats.eligible]
    stats.to_withdraw, stats.to_reinstate = sorted(gone), sorted(back)
    return stats.to_withdraw, stats.to_reinstate
