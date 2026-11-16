"""Syncing from a Loculus backend: eligibility, projection, caching, and schema drift."""

import json
from pathlib import Path

import pytest

from loculus_eternal.format.encode import Entry
from loculus_eternal.testkit import BackendStub, released_line
from loculus_eternal.upload.sync import BackendClient, OrganismFeed, SyncError, check_shape, is_eligible, iter_lines, select_new_entries

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "released"


@pytest.fixture
def backend():
    with BackendStub() as b:
        yield b


def test_U1_only_open_entries_are_eligible_and_published_ones_are_skipped(backend, tmp_path):
    backend.set_lines("zika", [released_line("zika", "PP_1", 1), released_line("zika", "PP_2", 1, open_terms=False), released_line("zika", "PP_3", 1)])
    client = BackendClient(backend.url, tmp_path / "feeds")
    feed = client.fetch("zika")
    assert feed.total_records == 3 and not feed.from_cache
    new, stats = select_new_entries("zika", feed, lambda acc, ver: acc == "PP_1")
    assert [e["metadata"]["accession"] for e in new] == ["PP_3"]
    assert (stats.total, stats.open, stats.restricted, stats.already_published, stats.new) == (3, 2, 1, 1, 1)
    for e in new:
        Entry.validate(e)
        assert "dataUseTerms" not in e["metadata"] and e["organism"] == "zika"


def test_U1_restricted_entry_becomes_eligible_when_it_opens(backend, tmp_path):
    backend.set_lines("zika", [released_line("zika", "PP_9", 1, open_terms=False)])
    client = BackendClient(backend.url, tmp_path / "feeds")
    new, _ = select_new_entries("zika", client.fetch("zika"), lambda a, v: False)
    assert new == []
    backend.open_entry("zika", "PP_9.1")
    new, _ = select_new_entries("zika", client.fetch("zika"), lambda a, v: False)
    assert len(new) == 1 and new[0]["metadata"]["dataBecameOpenAt"] == "2026-10-01T00:00:00Z"


def test_U1_unchanged_feed_is_served_from_the_cache_with_304(backend, tmp_path):
    backend.set_lines("mpox", [released_line("mpox", "PP_5", 1)])
    client = BackendClient(backend.url, tmp_path / "feeds")
    first = client.fetch("mpox")
    second = client.fetch("mpox")
    assert second.from_cache and second.etag == first.etag
    assert any(h.get("If-None-Match") == first.etag for _, h in backend.requests)
    assert [l["metadata"]["accession"] for l in iter_lines(second)] == ["PP_5"]
    assert "?compression=zstd" in backend.requests[-1][0]
    # A new client in the same cache directory remembers the ETag across processes.
    third = BackendClient(backend.url, tmp_path / "feeds").fetch("mpox")
    assert third.from_cache


def test_U1_schema_drift_is_refused_before_anything_is_published(backend, tmp_path):
    line = released_line("zika", "PP_1", 1)
    line["newTopLevelSection"] = {}
    backend.set_lines("zika", [line])
    client = BackendClient(backend.url, tmp_path / "feeds")
    with pytest.raises(SyncError, match="unexpected shape"):
        select_new_entries("zika", client.fetch("zika"), lambda a, v: False)
    bad = released_line("zika", "PP_2", 1)
    del bad["aminoAcidInsertions"]
    with pytest.raises(SyncError, match="missing"):
        check_shape("zika", bad)
    assert is_eligible(released_line("zika", "PP_3", 1)) and not is_eligible(released_line("zika", "PP_3", 1, open_terms=False))


@pytest.mark.parametrize("organism", ["andv", "zika"])
def test_U1_real_released_lines_pass_the_shape_check_and_project(organism):
    line = json.loads((FIXTURES / f"{organism}.ndjson").read_text())
    check_shape(organism, line)
    entry = Entry.from_released_line(organism, line)
    Entry.validate(entry)
    for k in ("versionStatus", "dataUseTerms", "dataUseTermsRestrictedUntil", "dataUseTermsUrl"):
        assert k not in entry["metadata"]
    assert entry["metadata"]["pipelineVersion"] == line["metadata"]["pipelineVersion"]
    assert entry["metadata"]["dataBecameOpenAt"] == line["metadata"]["dataBecameOpenAt"]
    assert set(entry["unalignedNucleotideSequences"]) == set(line["unalignedNucleotideSequences"])


def test_U17_config_expands_environment_references(tmp_path, monkeypatch):
    from loculus_eternal import config as configuration

    cfg = tmp_path / "c.toml"
    cfg.write_text('[chain]\nrpc_url = "https://rpc.example/v3/${TEST_RPC_KEY}"\ncontract = "0x0000000000000000000000000000000000000001"\nchain_id = 11155111\n')
    monkeypatch.setenv("TEST_RPC_KEY", "sekrit")
    assert configuration.load(cfg).chain.rpc_url == "https://rpc.example/v3/sekrit"
    monkeypatch.delenv("TEST_RPC_KEY")
    with pytest.raises(SystemExit, match="TEST_RPC_KEY"):
        configuration.load(cfg)


def test_U16_organisms_are_asked_from_the_backend_and_are_not_a_setting(backend, tmp_path):
    from loculus_eternal import config as configuration

    backend.set_lines("zika", [released_line("zika", "PP_1", 1)])
    backend.set_lines("marburg", [released_line("marburg", "PP_2", 1)])
    client = BackendClient(backend.url, tmp_path / "feeds")
    assert client.organisms() == ["marburg", "zika"]
    cfg = tmp_path / "c.toml"
    base = f'[chain]\nrpc_url = "http://x"\ncontract = "0x0000000000000000000000000000000000000001"\nchain_id = 1\n[backend]\nurl = "{backend.url}"\n'
    cfg.write_text(base)
    assert configuration.load(cfg).backend.url == backend.url, "the backend table needs nothing about organisms"
    # A config written for an earlier version says so plainly, whichever form it used: a list
    # left there would otherwise look as if it still limited what is published.
    for retired in ('organisms = "all"\n', 'organisms = ["zika"]\n'):
        cfg.write_text(base + retired)
        with pytest.raises(SystemExit, match="organisms is no longer configured.*remove the key"):
            configuration.load(cfg)
    backend.enumerate_organisms = False
    with pytest.raises(SyncError, match="cannot go on without that list"):
        BackendClient(backend.url, tmp_path / "feeds2").organisms()


def test_U22_progress_counts_the_line_just_read(backend, tmp_path, monkeypatch):
    from loculus_eternal.upload import sync

    monkeypatch.setattr(sync, "PROGRESS_EVERY", 2)
    backend.set_lines("zika", [released_line("zika", f"PP_{i}", 1) for i in range(1, 5)])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    stats = sync.SyncStats("zika")
    seen = []
    entries = list(sync.iter_new_entries("zika", feed, lambda acc, ver: False, stats, lambda read, total: seen.append((read, total, stats.new))))
    # Every line is open and unpublished, so the new count keeps pace with the lines read.
    assert len(entries) == 4 and seen == [(2, 4, 2), (4, 4, 4)]


def test_U23_an_unknown_key_in_a_source_table_is_a_configuration_error(tmp_path):
    from loculus_eternal import config as configuration

    cfg = tmp_path / "loculus-eternal.toml"
    cfg.write_text(
        """
[chain]
rpc_url = "http://127.0.0.1:1"
contract = "0x0000000000000000000000000000000000000001"
chain_id = 31337
beacon_genesis_time = 0

[[sources]]
type = "beacon"
endpoint = ["http://127.0.0.1:2"]
"""
    )
    with pytest.raises(configuration.ConfigError, match="unknown key.*endpoint.*takes endpoints"):
        configuration.load(cfg)


def test_U23_a_hash_keyed_archive_without_a_url_uses_the_public_instance_of_its_network(tmp_path):
    from loculus_eternal import config as configuration

    cfg = tmp_path / "loculus-eternal.toml"
    body = """
[chain]
rpc_url = "http://127.0.0.1:1"
contract = "0x0000000000000000000000000000000000000001"
chain_id = CHAIN
beacon_genesis_time = 0

[[sources]]
type = "blobscan"

[[sources]]
type = "blockscout"
"""
    cfg.write_text(body.replace("CHAIN", "11155111"))
    assert [s.base_url for s in configuration.load(cfg).sources] == ["https://api.sepolia.blobscan.com", "https://eth-sepolia.blockscout.com"]
    cfg.write_text(body.replace("CHAIN", "1"))
    assert [s.base_url for s in configuration.load(cfg).sources] == ["https://api.blobscan.com", "https://eth.blockscout.com"]
    cfg.write_text(body.replace("CHAIN", "31337"))
    with pytest.raises(configuration.ConfigError, match="needs a url"):
        configuration.load(cfg)
    cfg.write_text(body.replace("CHAIN", "1").replace('type = "blockscout"', 'type = "blockscout"\nurl = ""'))
    with pytest.raises(configuration.ConfigError, match="empty url"):
        configuration.load(cfg)


# --- the copy a run publishes: minimum age, and what leaves or returns ------------------------


def aged(accession: str, submitted_at, version: int = 1, **kw) -> dict:
    return released_line("zika", accession, version, extra={"submittedAtTimestamp": submitted_at}, **kw)


def test_U26_only_entries_submitted_by_the_cutoff_are_eligible(backend, tmp_path):
    cutoff = 1_700_000_000
    backend.set_lines("zika", [aged("PP_1", cutoff - 1), aged("PP_2", cutoff), aged("PP_3", cutoff + 1), aged("PP_4", cutoff + 1, open_terms=False), aged("PP_1", cutoff + 5, version=2)])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    new, stats = select_new_entries("zika", feed, lambda acc, ver: False, submitted_cutoff=cutoff)
    # The cutoff itself is old enough; each version is judged by its own submission time; a
    # restricted entry is counted as restricted whatever its age.
    assert [e["metadata"]["accessionVersion"] for e in new] == ["PP_1.1", "PP_2.1"]
    assert (stats.total, stats.open, stats.restricted, stats.held_back, stats.new) == (5, 4, 1, 2, 2)
    assert stats.eligible == {"PP_1.1", "PP_2.1"}
    # Without a minimum age nothing is held back and the time is not even looked at.
    new, stats = select_new_entries("zika", feed, lambda acc, ver: False)
    assert len(new) == 4 and stats.held_back == 0


@pytest.mark.parametrize("value", [None, "1700000000", 1.7e9, True])
def test_U26_a_line_without_a_usable_submission_time_is_an_error_not_a_guess(backend, tmp_path, value):
    backend.set_lines("zika", [aged("PP_1", value)])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    with pytest.raises(SyncError, match="PP_1.1.*submittedAtTimestamp"):
        select_new_entries("zika", feed, lambda acc, ver: False, submitted_cutoff=1_700_000_000)


def test_U25_what_to_withdraw_and_reinstate_follows_from_the_eligible_entries(backend, tmp_path):
    from loculus_eternal.upload.sync import find_changes

    backend.set_lines("zika", [released_line("zika", "PP_1", 1), released_line("zika", "PP_2", 1, open_terms=False), released_line("zika", "PP_3", 2), released_line("zika", "PP_5", 1)])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    published = {"PP_1": [[1, 0]], "PP_2": [[1, 0]], "PP_3": [[1, 0], [2, 1]], "PP_4": [[1, 0]], "PP_5": [[1, 0]]}
    withdrawn = {"PP_3": [2], "PP_4": [1], "PP_5": [1]}
    _, stats = select_new_entries("zika", feed, lambda acc, ver: [ver, 0] in published.get(acc, []) or [ver, 1] in published.get(acc, []))
    gone, back = find_changes(stats, published, withdrawn)
    # PP_2.1 is in the feed but restricted, PP_3.1 is not in it at all: both leave. PP_4.1 is
    # gone and already withdrawn: nothing to do. PP_3.2 and PP_5.1 are withdrawn and back.
    assert gone == ["PP_2.1", "PP_3.1"] and back == ["PP_3.2", "PP_5.1"]
    assert (stats.to_withdraw, stats.to_reinstate) == (gone, back)


@pytest.mark.parametrize("value,ok", [("90", True), ("0", True), ("-1", False), ("1.5", False), ('"90"', False), ("true", False)])
def test_U26_min_age_days_is_a_whole_number_of_days(tmp_path, value, ok):
    from loculus_eternal import config as configuration

    cfg = tmp_path / "loculus-eternal.toml"
    cfg.write_text(
        f"""
[chain]
rpc_url = "http://127.0.0.1:1"
contract = "0x0000000000000000000000000000000000000001"
chain_id = 31337
beacon_genesis_time = 0

[backend]
url = "http://127.0.0.1:2"
min_age_days = {value}
"""
    )
    if ok:
        assert configuration.load(cfg).backend.min_age_days == int(value)
    else:
        with pytest.raises(configuration.ConfigError, match="min_age_days"):
            configuration.load(cfg)
    # Left out, there is no delay.
    cfg.write_text(cfg.read_text().replace(f"min_age_days = {value}\n", ""))
    assert configuration.load(cfg).backend.min_age_days == 0


def test_U27_the_count_the_backend_reports_is_checked_against_the_lines_read(backend, tmp_path):
    backend.set_lines("zika", [released_line("zika", f"PP_{i}", 1) for i in range(1, 4)])
    client = BackendClient(backend.url, tmp_path / "feeds")
    feed = client.fetch("zika")
    client.check_complete(feed, 3)
    assert feed.path.exists()
    with pytest.raises(SyncError, match="zika.*holds 3 released entries and 2 arrived.*run again"):
        client.check_complete(feed, 2)
    # The copy that failed the check is gone, so the next fetch is a download, not "unchanged".
    assert not feed.path.exists()
    again = client.fetch("zika")
    assert not again.from_cache and again.total_records == 3
    # A backend that reports no count cannot be checked either way.
    client.check_complete(OrganismFeed("zika", None, None, again.path, from_cache=False), 0)
    assert again.path.exists()


@pytest.mark.parametrize(
    "table,line,message",
    [
        ("backend", "min_age_day = 90", r"\[backend\] has unknown key\(s\) min_age_day; it takes min_age_days, url"),
        ("upload", "min_age_days = 90", r"\[upload\] has unknown key\(s\) min_age_days; .*min_age_days belongs under \[backend\]"),
        ("upload", "max_blob_fee = 5", r"\[upload\] has unknown key\(s\) max_blob_fee; it takes"),
        ("backend", "data_dir = 'x'", r"data_dir belongs under \[upload\]"),
    ],
)
def test_U23_an_unknown_key_in_the_backend_or_upload_table_is_a_configuration_error(tmp_path, table, line, message):
    from loculus_eternal import config as configuration

    tables = {"backend": 'url = "http://127.0.0.1:2"\n', "upload": 'data_dir = "upload-data"\n'}
    tables[table] += line + "\n"
    cfg = tmp_path / "loculus-eternal.toml"
    cfg.write_text(
        f"""
[chain]
rpc_url = "http://127.0.0.1:1"
contract = "0x0000000000000000000000000000000000000001"
chain_id = 31337
beacon_genesis_time = 0

[backend]
{tables["backend"]}
[upload]
{tables["upload"]}"""
    )
    # A misspelt or misplaced key would otherwise leave its default in force without a word.
    with pytest.raises(configuration.ConfigError, match=message):
        configuration.load(cfg)


def test_U23_the_shipped_example_config_uses_only_known_keys(monkeypatch):
    from loculus_eternal import config as configuration

    monkeypatch.setenv("RPC_API", "http://127.0.0.1:1")
    example = Path(__file__).resolve().parents[2] / "config" / "sepolia.example.toml"
    # The example is the template for the real deployment, so it carries the delay.
    assert configuration.load(example).backend.min_age_days == 90


@pytest.mark.parametrize("name", ["PP_1.2", "PP_1.01", "PP_2.1", "PP_1", ""])
def test_U1_a_line_whose_name_is_not_its_accession_and_version_stops_the_run(backend, tmp_path, name):
    line = released_line("zika", "PP_1", 1)
    line["metadata"]["accessionVersion"] = name
    backend.set_lines("zika", [released_line("zika", "PP_7", 1), line])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    # Already published or not, the line is refused: had it been let through as published,
    # PP_1.1 would have looked absent from the feed and been withdrawn.
    for published in (True, False):
        with pytest.raises(SyncError, match="zika: released line names itself .* but carries accession 'PP_1' and version 1"):
            select_new_entries("zika", feed, lambda acc, ver: published)


def test_U1_a_version_that_is_not_a_whole_number_fails_the_name_check(backend, tmp_path):
    line = released_line("zika", "PP_1", 1)
    line["metadata"]["version"] = 1.0
    backend.set_lines("zika", [line])
    feed = BackendClient(backend.url, tmp_path / "feeds").fetch("zika")
    with pytest.raises(SyncError, match="names itself 'PP_1.1' but carries accession 'PP_1' and version 1.0"):
        select_new_entries("zika", feed, lambda acc, ver: True)


def test_U28_sizes_and_durations_read_as_a_person_would_say_them():
    from loculus_eternal.upload.upload import estimated_seconds, human_duration, human_size

    assert [human_size(n) for n in (0, 1, 999, 1_000, 414_400, 2_500_000, 45_613_056, 14_123_456_789)] == ["0 bytes", "1 byte", "999 bytes", "1 kB", "414 kB", "3 MB", "46 MB", "14.1 GB"]
    # Just under a thousand of one unit reads as one of the next, never as "1000 kB".
    assert [human_size(n) for n in (999_499, 999_500, 999_499_999, 999_500_000)] == ["999 kB", "1 MB", "999 MB", "1.0 GB"]
    assert [human_duration(s) for s in (0.4, 1, 45, 119, 120, 150, 210, 696, 1656, 7140)] == ["0 seconds", "1 second", "45 seconds", "119 seconds", "2 minutes", "3 minutes", "4 minutes", "12 minutes", "28 minutes", "119 minutes"]
    assert [human_duration(s) for s in (7170, 7200, 7260, 9000)] == ["2 hours", "2 hours", "2 hours 1 minute", "2 hours 30 minutes"]
    # A clock that stepped backwards gives no time, not a negative one.
    assert human_duration(-3) == "0 seconds"
    # Fifty-eight transactions at one per twelve-second block, then two and a half epochs.
    assert estimated_seconds(58, 12) == (696, 960)
    # The encoder's own progress line gives sizes the same way.
    import re

    from loculus_eternal.format.encode import StreamEncoder
    from loculus_eternal.format.gen_vectors import CHAIN_ID, CONTRACT, genesis_entries

    said = []
    StreamEncoder(CHAIN_ID, CONTRACT, progress=said.append).encode_batch(genesis_entries())
    size = r"\d+(\.\d)? (bytes|kB|MB|GB)"
    assert any(re.fullmatch(rf"encoding: body is {size}, {size} compressed; computing cumulative digests", line) for line in said), said
