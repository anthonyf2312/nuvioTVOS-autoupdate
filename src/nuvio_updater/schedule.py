"""Time-window and retry-backoff helpers.

Pure functions only -- no I/O -- so the scheduling rules can be tested directly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Sequence

_ALWAYS_TOKENS = {"", "always", "*", "24/7", "any"}


def parse_clock(raw: str) -> dtime:
    """Parse ``HH:MM`` (or ``HH:MM:SS``). ``24:00`` normalises to midnight."""
    text = raw.strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(f"expected HH:MM, got {raw!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
    except ValueError as exc:
        raise ValueError(f"expected HH:MM, got {raw!r}") from exc

    if (hour, minute, second) == (24, 0, 0):
        return dtime(0, 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ValueError(f"time out of range: {raw!r}")
    return dtime(hour, minute, second)


@dataclass(frozen=True)
class QuietWindow:
    """A daily window during which installs are allowed to *start*.

    An install already underway is never interrupted by the window closing --
    the window only gates the decision to begin.
    """

    start: dtime | None
    end: dtime | None

    @property
    def always_open(self) -> bool:
        # A degenerate window (start == end) means "no restriction", which makes
        # "00:00-24:00" behave the way anyone would expect it to.
        return self.start is None or self.end is None or self.start == self.end

    @classmethod
    def parse(cls, raw: str | None) -> "QuietWindow":
        text = (raw or "").strip().lower()
        if text in _ALWAYS_TOKENS:
            return cls(None, None)
        if "-" not in text:
            raise ValueError(f"expected HH:MM-HH:MM or 'always', got {raw!r}")
        start_raw, _, end_raw = text.partition("-")
        return cls(parse_clock(start_raw), parse_clock(end_raw))

    def contains(self, moment: datetime) -> bool:
        """Is ``moment`` (already in the target timezone) inside the window?"""
        if self.always_open:
            return True
        assert self.start is not None and self.end is not None
        now = moment.time()
        if self.start < self.end:
            return self.start <= now < self.end
        # Window wraps past midnight, e.g. 23:00-02:00.
        return now >= self.start or now < self.end

    def describe(self) -> str:
        if self.always_open:
            return "always"
        assert self.start is not None and self.end is not None
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"


def resolve_window(default: QuietWindow, override: str | None) -> QuietWindow:
    """The window actually in force.

    A value set from Telegram wins over ``QUIET_WINDOW`` in the environment. A
    stored override that no longer parses falls back to the environment default
    rather than failing the tick -- the loop matters more than the preference.
    """
    if not override:
        return default
    try:
        return QuietWindow.parse(override)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Stored quiet-window override %r is invalid; using %s", override, default.describe()
        )
        return default


def parse_backoff(raw: str) -> tuple[int, ...]:
    """Parse a comma-separated minute list such as ``15,60,240``."""
    values: list[int] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        minutes = int(chunk)
        if minutes < 0:
            raise ValueError(f"backoff minutes must be >= 0, got {minutes}")
        values.append(minutes)
    if not values:
        raise ValueError("backoff list must contain at least one value")
    return tuple(values)


def backoff_delay(attempts_made: int, backoff_minutes: Sequence[int]) -> timedelta:
    """Delay before the next attempt, given how many attempts have already failed.

    The last entry is reused if there are more attempts than entries.
    """
    if attempts_made < 1:
        return timedelta(0)
    index = min(attempts_made - 1, len(backoff_minutes) - 1)
    return timedelta(minutes=backoff_minutes[index])
