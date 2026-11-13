"""The question a publish asks at the terminal, and what the command does with no terminal."""

import io

import pytest

from loculus_eternal import cli


def answer(monkeypatch, typed: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(typed))


@pytest.mark.parametrize("typed", ["\n", "y\n", "Y\n", "yes\n", " YES \n"])
def test_U24_enter_or_yes_continues(monkeypatch, capsys, typed):
    answer(monkeypatch, typed)
    assert cli.ask_to_continue("You are publishing a copy of the Loculus database located at http://db.example") is True
    shown = capsys.readouterr()
    # Standard output is often a log file; the question must not disappear into it.
    assert shown.out == "" and "located at http://db.example" in shown.err and shown.err.rstrip().endswith("Continue? (Y/n)")


@pytest.mark.parametrize("typed", ["n\n", "N\n", "no\n"])
def test_U24_no_stops(monkeypatch, typed):
    answer(monkeypatch, typed)
    assert cli.ask_to_continue("statement") is False


def test_U24_an_unclear_answer_is_asked_again(monkeypatch, capsys):
    answer(monkeypatch, "maybe\nwhat\nn\n")
    assert cli.ask_to_continue("statement") is False
    err = capsys.readouterr().err
    assert err.count("Continue? (Y/n)") == 3 and err.count("statement") == 1, "the statement is said once, the question repeated"


def test_U24_input_ending_without_an_answer_stops(monkeypatch):
    answer(monkeypatch, "")
    assert cli.ask_to_continue("statement") is False


def test_U24_publish_without_a_terminal_refuses_before_touching_anything(monkeypatch, capsys, tmp_path):
    cfg = tmp_path / "loculus-eternal.toml"
    # Nothing listens on this port: reaching for the endpoint would fail loudly, not refuse.
    cfg.write_text(
        f"""
[chain]
rpc_url = "http://127.0.0.1:9"
contract = "0x0000000000000000000000000000000000000001"
chain_id = 11155111

[backend]
url = "http://127.0.0.1:9"
organisms = ["zika"]

[upload]
data_dir = "{tmp_path / 'upload-data'}"
"""
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))  # input is there, but it is not a terminal
    monkeypatch.delenv("LOCULUS_ETERNAL_PUBLISHER_PRIVATE_KEY", raising=False)
    assert cli.main(["upload", "--config", str(cfg)]) == 1
    err = capsys.readouterr().err
    assert "no terminal" in err and "--yes" in err
    assert not (tmp_path / "upload-data").exists(), "refused before the data directory was even made"
