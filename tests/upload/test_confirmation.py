"""The question a publish asks before it sends its batch: how an answer is read, where the
question is shown, and what happens when there is nobody to ask."""

import io
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from loculus_eternal import cli
from loculus_eternal.upload.upload import NobodyToAsk

ROOT = Path(__file__).resolve().parents[2]


def ask(typed: str) -> tuple[bool, str]:
    shown = io.StringIO()
    return cli.ask_to_continue("You are publishing a copy of the Loculus database located at http://db.example", io.StringIO(typed), shown), shown.getvalue()


@pytest.mark.parametrize("typed", ["\n", "y\n", "Y\n", "yes\n", " YES \n"])
def test_U24_enter_or_yes_continues(typed):
    answer, shown = ask(typed)
    assert answer is True
    assert "located at http://db.example" in shown and shown.rstrip().endswith("Continue? (Y/n)")


@pytest.mark.parametrize("typed", ["n\n", "N\n", "no\n"])
def test_U24_no_stops(typed):
    assert ask(typed)[0] is False


def test_U24_an_unclear_answer_is_asked_again():
    answer, shown = ask("maybe\nwhat\nn\n")
    assert answer is False
    assert shown.count("Continue? (Y/n)") == 3 and shown.count("located at") == 1, "the statement is said once, the question repeated"


def test_U24_input_ending_without_an_answer_stops():
    assert ask("")[0] is False


@pytest.mark.parametrize("stdin", [io.StringIO("y\n"), None])
def test_U24_with_no_terminal_there_is_nobody_to_ask(monkeypatch, capsys, stdin):
    # Input that is not a terminal (a pipe, a file) or no input at all: a "y" waiting there
    # was not typed by someone who read the question.
    monkeypatch.setattr("sys.stdin", stdin)
    with pytest.raises(NobodyToAsk, match="no terminal.*--yes"):
        cli.confirm_at_terminal("new entries:  3")
    assert "new entries:  3" in capsys.readouterr().err, "what was not sent is still shown, for the log"


# --- at a real terminal -----------------------------------------------------------------------

# The child makes the pseudo-terminal its controlling terminal, as a shell's is, then asks.
CHILD = """
import fcntl, sys, termios, time
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
from loculus_eternal import cli
time.sleep(0.6)
try:
    print("ANSWER", cli.confirm_at_terminal("statement with the numbers"), flush=True)
except Exception as e:
    print("ANSWER", type(e).__name__, flush=True)
"""


def at_terminal(tmp_path, *, typed_ahead: bytes = b"", answer: bytes, stderr_to_log: bool = False) -> tuple[str, str, str]:
    """Run the question in a child whose terminal is a pseudo-terminal held by this test.
    Returns what the child answered, what appeared on the terminal, and what went to the log
    that standard error was redirected to (empty when it was not)."""
    pty = pytest.importorskip("pty")
    master, slave = pty.openpty()
    log = tmp_path / "run.log"
    with open(log, "wb") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-c", CHILD],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=log_file if stderr_to_log else slave,
            start_new_session=True,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        )
    os.close(slave)
    try:
        if typed_ahead:
            os.write(master, typed_ahead)
        screen = b""
        while b"(Y/n)" not in screen:
            ready, _, _ = select.select([master], [], [], 30)
            assert ready, f"the question never appeared on the terminal; it showed {screen!r}"
            screen += os.read(master, 4096)
        os.write(master, answer)
        out, _ = proc.communicate(timeout=30)
    finally:
        proc.kill()
        os.close(master)
    return out.decode().strip(), screen.decode(errors="replace"), log.read_text()


def test_U24_keys_typed_before_the_question_appeared_are_not_the_answer(tmp_path):
    # An Enter pressed while the batch was still being planned must not confirm it.
    answered, screen, _ = at_terminal(tmp_path, typed_ahead=b"\n\n", answer=b"n\n")
    assert answered == "ANSWER False"
    assert "statement with the numbers" in screen


def test_U24_question_is_shown_on_the_terminal_when_standard_error_goes_to_a_log(tmp_path):
    answered, screen, log = at_terminal(tmp_path, answer=b"y\n", stderr_to_log=True)
    assert answered == "ANSWER True"
    assert "statement with the numbers" in screen and "Continue? (Y/n)" in screen
    assert "Continue?" not in log, "the question is not left waiting in a file nobody is reading"
