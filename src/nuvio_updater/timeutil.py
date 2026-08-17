"""Timestamp parsing helpers.

atvloadly is a Go service and serialises timestamps with up to 9 fractional
digits (e.g. ``2026-08-05T19:16:35.3360339Z``). ``datetime.fromisoformat``
only accepts 3 or 6, so anything reading atvloadly timestamps must go through
:func:`parse_timestamp`.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|z|[+-]\d{2}:?\d{2})?$"
)


def parse_timestamp(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime, or ``None``.

    Returns ``None`` for empty values, Go's zero time, and anything unparseable,
    so a malformed timestamp degrades into "unknown" rather than crashing the
    update loop.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text or text.startswith("0001-01-01"):
        return None

    match = _TIMESTAMP_RE.match(text)
    if not match:
        return None

    base = match.group("base").replace(" ", "T")
    frac = (match.group("frac") or "")[:6].ljust(6, "0")
    tz_raw = match.group("tz")

    if tz_raw in (None, "Z", "z"):
        suffix = "+00:00"
    elif ":" in tz_raw:
        suffix = tz_raw
    else:
        suffix = f"{tz_raw[:3]}:{tz_raw[3:]}"

    try:
        parsed = datetime.fromisoformat(f"{base}.{frac}{suffix}")
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def describe_gap(delta: timedelta) -> str:
    """A rough human duration -- "40 min", "7 h", "4 days 3 h"."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, _ = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h"
    days, rem = divmod(hours, 24)
    return f"{days} days" if rem == 0 else f"{days} days {rem} h"


def format_local(moment: datetime | None, tz) -> str:
    if moment is None:
        return "unknown"
    return moment.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
