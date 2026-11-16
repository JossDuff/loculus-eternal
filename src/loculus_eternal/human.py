"""Numbers written the way a person reads them, for log lines and summaries.

Every size and duration a command prints goes through here, so the same quantity reads the
same in an upload, a recovery and on the health page. Error messages that compare an exact
length against an expected one keep their exact byte counts.
"""

from __future__ import annotations


def _rounded(x: float) -> int:
    """To the nearest whole number, a half going up, as the health page's script rounds."""
    return int(x + 0.5)


def human_size(n: int) -> str:
    """A byte count in decimal units, as the health page writes one. The unit is chosen
    after rounding, so a count just under a thousand of one unit reads as one of the next."""
    if n < 1000:
        return "1 byte" if n == 1 else f"{n} bytes"
    if _rounded(n / 1e3) < 1000:
        return f"{_rounded(n / 1e3)} kB"
    if _rounded(n / 1e6) < 1000:
        return f"{_rounded(n / 1e6)} MB"
    return f"{n / 1e9:.1f} GB"


def human_duration(seconds: float) -> str:
    """Seconds up to two minutes, then minutes up to two hours, then hours and minutes. A
    clock that stepped backwards while something was timed gives no time at all, not less."""
    seconds = _rounded(max(seconds, 0))
    if seconds < 120:
        return "1 second" if seconds == 1 else f"{seconds} seconds"
    minutes = _rounded(seconds / 60)
    if minutes < 120:
        return f"{minutes} minutes"
    hours, rest = divmod(minutes, 60)
    return f"{hours} hours" + (f" {rest} minute{'' if rest == 1 else 's'}" if rest else "")
