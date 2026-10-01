import json
from pathlib import Path

import pytest

from loculus_eternal.format.gen_vectors import VECTORS_DIR


@pytest.fixture(scope="session")
def vectors() -> dict:
    """Every committed golden vector, keyed by file name."""
    out = {}
    for path in sorted(Path(VECTORS_DIR).glob("*.json")):
        out[path.name] = json.loads(path.read_text(encoding="utf-8"))
    assert out, "no vectors found; run `uv run python -m loculus_eternal.format.gen_vectors`"
    return out
