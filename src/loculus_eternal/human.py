"""Numbers written the way a person reads them, for log lines and summaries.

Every size and duration a command prints goes through here, so the same quantity reads the
same in an upload, a recovery and on the health page. Error messages that compare an exact
length against an expected one keep their exact byte counts.
"""

from __future__ import annotations


def human_size(n: int) -> str:
    """A byte count as the health page writes one: decimal units, one decimal for GB."""
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{round(n / 1e6)} MB"
    if n >= 1e3:
        return f"{round(n / 1e3)} kB"
    return f"{n} bytes"


def human_duration(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 120:
        return f"{seconds} seconds"
    minutes = round(seconds / 60)
    if minutes < 120:
        return f"{minutes} minutes"
    return f"{minutes // 60} hours {minutes % 60} minutes"
