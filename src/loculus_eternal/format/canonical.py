"""Canonical JSON (RFC 8785) for every structured payload in the stream.

Canonical form means the same object always produces the same bytes, which is what lets a
digest over an entry or a manifest mean anything. The rfc8785 package does the work; this
module pins the interface so the rest of the code never calls json.dumps for stream bytes.
"""

import json
from typing import Any

import rfc8785


def dumps(obj: Any) -> bytes:
    return rfc8785.dumps(obj)


def loads(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"))


def is_canonical(data: bytes) -> bool:
    try:
        return dumps(loads(data)) == data
    except (ValueError, TypeError):
        return False
