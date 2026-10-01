"""Process checks P1 and P2 from the test plan: they guard the repository itself."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUMAN_FACING = ["README.md", "CLAUDE.md", "loculus-eternal-PLAN.md", "docs", "src", "tests", "contracts/src"]
# The copied research note predates this repository and keeps its own numbering.
EXEMPT = {ROOT / "docs" / "research" / "blob-sourcing.md"}
# A milestone reference is the letter M followed by a digit at a word start; the section
# sign is never allowed. Hex digests and identifiers like "PP_M1" are not word-start matches.
# The pattern is assembled from pieces so this file does not itself contain either form.
NUMBERED = re.compile(r"(?<![A-Za-z0-9_])" + "M" + r"[0-9]|" + chr(0xA7))


def _files():
    for item in HUMAN_FACING:
        p = ROOT / item
        if p.is_file():
            yield p
        elif p.is_dir():
            yield from (f for f in p.rglob("*") if f.is_file() and f.suffix in {".md", ".py", ".sol", ".toml"})


def test_P1_no_numbered_references():
    offenders = []
    for f in _files():
        if f in EXEMPT:
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if NUMBERED.search(line):
                offenders.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()[:80]}")
    assert not offenders, "milestone or section numbers in human-facing text:\n" + "\n".join(offenders)


def test_P2_publisher_key_only_from_the_environment():
    hits = []
    for f in _files():
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r"0x[0-9a-fA-F]{64}", text):
            # A 32-byte hex literal is a digest in a vector or doc, never a key; flag only
            # the ones labelled as keys.
            context = text[max(0, m.start() - 60) : m.start()].lower()
            if "key" in context and "digest" not in context and "hash" not in context:
                hits.append(f"{f.relative_to(ROOT)}: {m.group()[:12]}...")
    assert not hits, "possible private key literal:\n" + "\n".join(hits)
