"""Durable state, persisted as JSON on the mounted volume."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutil import parse_timestamp, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2


@dataclass
class State:
    """Everything the updater needs to remember between ticks."""

    schema_version: int = SCHEMA_VERSION

    # Tag we have confirmed installed. The loop never re-installs this.
    last_installed_tag: str | None = None
    # Most recent tag seen on GitHub, used to fire "detected" exactly once.
    last_seen_tag: str | None = None

    # Release.fingerprint of the publications behind the two tags above. A tag
    # can be deleted and re-cut, so the tag alone cannot say whether the release
    # on GitHub right now is the one we acted on. ``None`` means "recorded
    # before fingerprinting existed"; the two are read with opposite defaults --
    # an unknown *installed* fingerprint is adopted rather than reinstalled,
    # while an unknown *seen* fingerprint counts as unannounced.
    last_installed_release: str | None = None
    last_seen_release: str | None = None

    # Conditional-request cache for the GitHub releases endpoint.
    etag: str | None = None
    cached_release: dict[str, Any] | None = None

    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None
    last_result: str | None = None
    heartbeat_at: str | None = None
    # When GitHub was last reached successfully, so an outage can be reported
    # once it ends. It cannot be reported while it lasts: whatever blocks
    # GitHub blocks Telegram too.
    last_check_ok_at: str | None = None
    bootstrapped: bool = False

    # Bundle identifier Nuvio was last installed under. Upstream renamed it at
    # 3.2.6, so this is learned at runtime rather than assumed from config.
    tracked_bundle_id: str | None = None
    # Set from Telegram; overrides QUIET_WINDOW from the environment.
    quiet_window_override: str | None = None
    # Tags the user chose to skip. A newer release resumes normal behaviour.
    skipped_tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # A hand-edited or older state file may carry the wrong shape here.
        if not isinstance(self.skipped_tags, list):
            log.warning("skipped_tags was %s, resetting to []", type(self.skipped_tags).__name__)
            self.skipped_tags = []
        else:
            self.skipped_tags = [str(t) for t in self.skipped_tags]

    # ---- derived helpers -------------------------------------------------

    def is_skipped(self, tag: str) -> bool:
        return tag in self.skipped_tags

    def skip(self, tag: str) -> None:
        if tag not in self.skipped_tags:
            self.skipped_tags.append(tag)
        # Keep the list from growing without bound over years of releases.
        del self.skipped_tags[:-20]

    @property
    def next_attempt_due(self) -> datetime | None:
        return parse_timestamp(self.next_attempt_at)

    @property
    def heartbeat(self) -> datetime | None:
        return parse_timestamp(self.heartbeat_at)

    @property
    def last_check_ok(self) -> datetime | None:
        return parse_timestamp(self.last_check_ok_at)

    def may_attempt_now(self, now: datetime) -> bool:
        due = self.next_attempt_due
        return due is None or now >= due

    def reset_attempts(self) -> None:
        self.attempts = 0
        self.next_attempt_at = None
        self.last_error = None

    def touch(self) -> None:
        self.heartbeat_at = utcnow().isoformat()


class StateStore:
    """Atomic JSON persistence for :class:`State`.

    Writes go to a temp file in the same directory and are renamed into place,
    so a crash mid-write can never leave a truncated state file behind.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            log.info("No state file at %s -- starting fresh", self.path)
            return State()
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("State file at %s is unreadable (%s) -- starting fresh", self.path, exc)
            return State()
        if not isinstance(raw, dict):
            log.warning("State file at %s is not an object -- starting fresh", self.path)
            return State()

        known = {f for f in State.__dataclass_fields__}
        unknown = set(raw) - known
        if unknown:
            log.warning("Ignoring unknown state keys: %s", ", ".join(sorted(unknown)))
        return self._migrate(State(**{k: v for k, v in raw.items() if k in known}))

    def _migrate(self, state: State) -> State:
        if state.schema_version >= SCHEMA_VERSION:
            return state

        log.info("Migrating state from schema %d to %d", state.schema_version, SCHEMA_VERSION)
        # A schema-1 cache body predates Release.release_id, so fingerprinting it
        # would compare against a value the wire can never produce. Drop the
        # conditional-request cache and take the next check fresh.
        state.etag = None
        state.cached_release = None
        state.schema_version = SCHEMA_VERSION
        return state

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(state), indent=2, sort_keys=True)

        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self.path)
        except BaseException:
            # Never leave a stray temp file behind on failure.
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise
