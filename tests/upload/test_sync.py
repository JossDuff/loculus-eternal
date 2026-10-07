"""Syncing from a Loculus backend: eligibility, projection, caching, and schema drift."""

import json
from pathlib import Path

import pytest

from loculus_eternal.format.encode import Entry
from loculus_eternal.testkit import BackendStub, released_line
from loculus_eternal.upload.sync import BackendClient, SyncError, check_shape, is_eligible, iter_lines, select_new_entries

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


def test_config_expands_environment_references(tmp_path, monkeypatch):
    from loculus_eternal import config as configuration

    cfg = tmp_path / "c.toml"
    cfg.write_text('[chain]\nrpc_url = "https://rpc.example/v3/${TEST_RPC_KEY}"\ncontract = "0x0000000000000000000000000000000000000001"\nchain_id = 11155111\n')
    monkeypatch.setenv("TEST_RPC_KEY", "sekrit")
    assert configuration.load(cfg).chain.rpc_url == "https://rpc.example/v3/sekrit"
    monkeypatch.delenv("TEST_RPC_KEY")
    with pytest.raises(SystemExit, match="TEST_RPC_KEY"):
        configuration.load(cfg)
