"""Orchestration: watch, decide, install, verify, notify, and obey commands."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from . import commands as cmd
from .atvloadly import (
    _EPOCH,
    AppRecord,
    AtvloadlyClient,
    AtvloadlyError,
    Verification,
    verify_install,
)
from .config import Config
from .github import GitHubError, NoMatchingAssetError, Release, ReleaseWatcher
from .mcp_backend import InstallBackend, InstallConfigurationError, McpError
from .notify import Notifier, code_block, esc
from .schedule import QuietWindow, backoff_delay, resolve_window
from .state import State, StateStore
from .timeutil import describe_gap, format_local, utcnow

log = logging.getLogger(__name__)


class Outcome:
    BOOTSTRAPPED = "bootstrapped"
    UP_TO_DATE = "up-to-date"
    SKIPPED = "skipped"
    WAITING_FOR_WINDOW = "waiting-for-window"
    WAITING_FOR_BACKOFF = "waiting-for-backoff"
    GAVE_UP = "gave-up"
    BUSY = "atvloadly-busy"
    INSTALLED = "installed"
    FAILED = "failed"
    DRY_RUN = "dry-run"
    ERROR = "error"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class TickResult:
    outcome: str
    detail: str = ""


class Updater:
    def __init__(
        self,
        config: Config,
        *,
        watcher: ReleaseWatcher,
        atvloadly: AtvloadlyClient,
        backend: InstallBackend,
        notifier: Notifier,
        store: StateStore,
        clock: Callable[[], datetime] = utcnow,
        sleeper: Callable[[float], None] = time.sleep,
        command_queue: "queue.Queue[cmd.Command] | None" = None,
        busy: threading.Event | None = None,
    ):
        self.config = config
        self.watcher = watcher
        self.atv = atvloadly
        self.backend = backend
        self.notifier = notifier
        self.store = store
        self.clock = clock
        self.sleeper = sleeper
        self.commands = command_queue
        self.busy = busy or threading.Event()

    # ---- window ----------------------------------------------------------

    def window(self, state: State) -> QuietWindow:
        return resolve_window(self.config.quiet_window, state.quiet_window_override)

    # ---- main entry points ----------------------------------------------

    def run_forever(self) -> None:
        interval = self.config.poll_interval_minutes * 60
        log.info(
            "Watching %s every %d min | window %s (%s) | notify: %s%s",
            self.config.github_repo,
            self.config.poll_interval_minutes,
            self.window(self.store.load()).describe(),
            self.config.timezone.key,
            self.notifier.describe(),
            " | DRY RUN" if self.config.dry_run else "",
        )
        while True:
            try:
                result = self.tick()
                log.info(
                    "Tick: %s%s", result.outcome, f" -- {result.detail}" if result.detail else ""
                )
            except Exception:  # noqa: BLE001 - a bad tick must never kill the loop
                log.exception("Unhandled error during tick")
            self._wait(interval)

    def _wait(self, seconds: float) -> None:
        """Sleep until the next tick, but wake early to serve a command."""
        if self.commands is None:
            self.sleeper(seconds)
            return

        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                command = self.commands.get(timeout=remaining)
            except queue.Empty:
                return
            try:
                self.handle_command(command)
            except Exception:  # noqa: BLE001 - a bad command must not kill the loop
                log.exception("Unhandled error handling command %s", command.kind)

    def tick(self) -> TickResult:
        state = self.store.load()
        state.touch()
        try:
            return self._tick(state)
        finally:
            self.store.save(state)

    # ---- tick internals --------------------------------------------------

    def _tick(self, state: State) -> TickResult:
        try:
            release = self._resolve_release(state)
        except NoMatchingAssetError as exc:
            return self._blocked(
                state,
                f"asset-selection: {exc}",
                "⚠️ <b>nuvio-autoupdate can't find an IPA</b>\n"
                f"{esc(exc)}\n\nCheck <code>IPA_ASSET_RE</code>.",
            )
        except GitHubError as exc:
            log.warning("GitHub check failed: %s", exc)
            return TickResult(Outcome.ERROR, str(exc))

        if release is None:
            return TickResult(Outcome.ERROR, "no release resolved")

        self._note_successful_check(state)

        if not state.bootstrapped:
            return self._bootstrap(state, release)

        fingerprint = release.fingerprint

        if release.tag == state.last_installed_tag and state.last_installed_release in (
            None,
            fingerprint,
        ):
            # An install recorded before fingerprinting has no identity to
            # compare against, so adopt the current one rather than reinstalling
            # what is demonstrably already there.
            state.last_installed_release = fingerprint
            state.last_seen_tag = release.tag
            state.last_seen_release = fingerprint
            state.last_result = Outcome.UP_TO_DATE
            return TickResult(Outcome.UP_TO_DATE, release.tag)

        # Skips stay keyed on the tag: "I don't want this version" should
        # survive upstream re-cutting it. "Update now" remains the override.
        if state.is_skipped(release.tag):
            state.last_seen_tag = release.tag
            state.last_seen_release = fingerprint
            state.last_result = Outcome.SKIPPED
            return TickResult(Outcome.SKIPPED, f"{release.tag} skipped by request")

        newly_detected = fingerprint != state.last_seen_release
        if newly_detected:
            # Only claim a re-cut when there is a previous publication to
            # contrast with. Without one -- the first tick after upgrading, say
            # -- the tag matching proves nothing, so announce it plainly.
            recut = release.tag == state.last_seen_tag and state.last_seen_release is not None
            state.last_seen_tag = release.tag
            state.last_seen_release = fingerprint
            state.reset_attempts()
            self._notify_detected(state, release, recut=recut)

        now = self.clock()
        local_now = now.astimezone(self.config.timezone)

        if not self.window(state).contains(local_now):
            return TickResult(
                Outcome.WAITING_FOR_WINDOW,
                f"{release.tag} queued for {self.window(state).describe()}",
            )

        if state.attempts >= self.config.max_attempts:
            return TickResult(Outcome.GAVE_UP, f"{release.tag} after {state.attempts} attempts")

        if not state.may_attempt_now(now):
            return TickResult(
                Outcome.WAITING_FOR_BACKOFF, f"next attempt at {state.next_attempt_at}"
            )

        return self._attempt_install(state, release)

    def _resolve_release(self, state: State) -> Release | None:
        result = self.watcher.latest(state.etag)

        if result.not_modified:
            cached = Release.from_cache(state.cached_release)
            if cached is not None:
                return cached
            # ETag survived but the cached body did not; refetch unconditionally.
            log.info("Cached release missing despite 304 -- refetching")
            result = self.watcher.latest(None)

        if result.release is not None:
            state.etag = result.etag
            state.cached_release = result.release.to_cache()
        return result.release

    def _note_successful_check(self, state: State) -> None:
        """Report an outage once it is over.

        The outage itself cannot be announced -- whatever stops us reaching
        GitHub stops us reaching Telegram too -- so the first check that
        succeeds after a long silence is the only chance to mention it. This is
        how a dead home-server link went unnoticed for days.
        """
        now = self.clock()
        previous = state.last_check_ok
        state.last_check_ok_at = now.isoformat()

        if previous is None or not self.config.offline_alert_hours:
            return
        gap = now - previous
        if gap < timedelta(hours=self.config.offline_alert_hours):
            return

        log.warning("Back online after %s without a successful check", describe_gap(gap))
        self.notifier.send(
            "⚠️ <b>nuvio-autoupdate was offline</b>\n"
            f"No successful check for <b>{esc(describe_gap(gap))}</b>.\n"
            f"Last good check: {esc(format_local(previous, self.config.timezone))}\n"
            "Watching again now — anything missed will be picked up on this tick."
        )

    def _bootstrap(self, state: State, release: Release) -> TickResult:
        """Establish a baseline so first boot never triggers a pointless reinstall."""
        state.bootstrapped = True
        state.last_seen_tag = release.tag
        state.last_seen_release = release.fingerprint

        latest_version = self.config.version_from_tag(release.tag)
        try:
            installed = self.atv.app_record(self.bundles(state))
        except AtvloadlyError as exc:
            state.bootstrapped = False  # try again next tick
            log.warning("Bootstrap deferred, atvloadly unreachable: %s", exc)
            return TickResult(Outcome.ERROR, f"bootstrap deferred: {exc}")

        if installed is None:
            self.notifier.send(
                "⚠️ <b>nuvio-autoupdate started, but Nuvio isn't installed</b>\n"
                f"No app matching <code>{esc(', '.join(self.bundles(state)))}</code> exists in "
                "atvloadly.\n\nAuto-install is held back because installing a new app would "
                "consume another slot against the free Apple ID's 3-app limit. Sideload it "
                "once by hand, then this will keep it updated."
            )
            return TickResult(Outcome.BOOTSTRAPPED, "no existing Nuvio record")

        if latest_version and installed.version == latest_version:
            state.last_installed_tag = release.tag
            state.last_installed_release = release.fingerprint
            self.notifier.send(
                "👀 <b>nuvio-autoupdate is watching</b>\n"
                f"Nuvio TV <b>{esc(installed.version)}</b> is current.\n"
                f"Checks every {self.config.poll_interval_minutes} min, installs during "
                f"{esc(self.window(state).describe())} "
                f"({esc(self.config.timezone.key)}).",
                cmd.menu_keyboard(),
            )
            return TickResult(Outcome.BOOTSTRAPPED, f"up to date at {installed.version}")

        # Behind the latest release: leave last_installed_tag unset so the next
        # quiet window brings it forward.
        self.notifier.send(
            "👀 <b>nuvio-autoupdate is watching</b>\n"
            f"Installed: <b>{esc(installed.version or 'unknown')}</b>\n"
            f"Latest: <b>{esc(latest_version or release.tag)}</b>\n"
            f"Will update during {esc(self.window(state).describe())} "
            f"({esc(self.config.timezone.key)}).",
            cmd.release_keyboard(),
        )
        return TickResult(
            Outcome.BOOTSTRAPPED, f"installed {installed.version}, latest {latest_version}"
        )

    # ---- install ---------------------------------------------------------

    def bundles(self, state: State) -> tuple[str, ...]:
        """Bundle identifiers that count as Nuvio, newest known first."""
        tracked = state.tracked_bundle_id
        if tracked and tracked not in self.config.bundle_ids:
            return (tracked,) + self.config.bundle_ids
        return self.config.bundle_ids

    def _attempt_install(self, state: State, release: Release) -> TickResult:
        """Install ``release`` now. The quiet window is checked by the caller."""
        expected_version = self.config.version_from_tag(release.tag)
        bundles = self.bundles(state)

        try:
            before_all = self.atv.apps()
        except AtvloadlyError as exc:
            return self._record_failure(state, release, f"atvloadly unreachable: {exc}", None)

        before = next(
            iter(
                sorted(
                    (a for a in before_all if a.bundle_identifier in bundles),
                    key=lambda a: (a.refreshed_date or _EPOCH),
                    reverse=True,
                )
            ),
            None,
        )

        if before is None and self.config.require_existing_app:
            return self._blocked(
                state,
                "no existing Nuvio record",
                "⚠️ <b>Update skipped: Nuvio isn't installed</b>\n"
                f"No app matching <code>{esc(', '.join(bundles))}</code> in atvloadly. "
                "Installing it fresh would take another slot against the free Apple ID's "
                "3-app limit, so this is left alone. Sideload it once by hand to resume.",
            )

        if self.config.dry_run:
            log.info(
                "DRY RUN: would install %s (%s) via %s [device=%s account=%s]",
                release.tag,
                release.ipa_name,
                self.config.mcp_url,
                self.config.device_id,
                self.config.account_id,
            )
            log.info("DRY RUN: current record: %s", before)
            return TickResult(Outcome.DRY_RUN, release.tag)

        try:
            if self.backend.install_in_progress():
                log.info("atvloadly is already installing something -- deferring")
                return TickResult(Outcome.BUSY, "another install is running")
        except McpError as exc:
            return self._record_failure(state, release, f"MCP unreachable: {exc}", before)

        state.attempts += 1
        log.info(
            "Installing %s (attempt %d/%d) from %s",
            release.tag,
            state.attempts,
            self.config.max_attempts,
            release.ipa_url,
        )

        self.busy.set()
        try:
            result = self.backend.install_and_wait(
                release.ipa_url,
                timeout_seconds=self.config.install_timeout_minutes * 60,
                poll_seconds=self.config.install_poll_seconds,
                on_progress=lambda elapsed: log.info("  ... installing, %.0fs elapsed", elapsed),
            )
        except InstallConfigurationError as exc:
            return self._blocked(
                state,
                str(exc),
                "⚠️ <b>nuvio-autoupdate is misconfigured</b>\n"
                f"{esc(exc)}\n\nRun <code>--check</code> for the valid ids.",
            )
        except McpError as exc:
            return self._record_failure(state, release, f"install call failed: {exc}", before)
        finally:
            self.busy.clear()

        try:
            after_all = self.atv.apps()
        except AtvloadlyError as exc:
            return self._record_failure(
                state, release, f"could not read back app record: {exc}", before
            )

        verification = verify_install(before_all, after_all, expected_version, bundles)

        if not verification.ok:
            reason = verification.reason
            if result.timed_out:
                reason = f"timed out after {self.config.install_timeout_minutes} min; {reason}"
            return self._record_failure(state, release, reason, before)

        installed = verification.record
        if installed is not None:
            state.tracked_bundle_id = installed.bundle_identifier

        superseded, removed = self._cleanup_superseded(installed, after_all, bundles)
        surplus = self._surplus_apps(after_all, removed)

        state.last_installed_tag = release.tag
        state.last_installed_release = release.fingerprint
        state.reset_attempts()
        state.last_result = Outcome.INSTALLED
        self._notify_success(
            release, verification, result.waited_seconds, superseded, removed, surplus
        )
        return TickResult(Outcome.INSTALLED, release.tag)

    def _cleanup_superseded(
        self, installed: AppRecord | None, after_all: list[AppRecord], bundles: tuple[str, ...]
    ) -> tuple[list[AppRecord], list[AppRecord]]:
        """Drop any other Nuvio record left behind by this install.

        Normally atvloadly upserts and there is nothing to do. When upstream
        changes the bundle identifier it writes a second row instead, and
        leaving that behind keeps atvloadly re-signing an app the user no longer
        runs -- spending one of the free Apple ID's three slots every week.

        Note this is deliberately *not* conditioned on the new bundle being
        unrecognised: once a renamed identifier is added to the configured list
        it stops looking surprising, but the stale row still needs removing.

        Only the atvloadly record goes. The icon on the Apple TV can be removed
        only by hand -- v0.4.6 has no uninstall API.

        Returns ``(superseded, removed)``. They differ when a delete fails, and
        the caller still needs the superseded list: that is precisely when the
        user must be told to clean up manually.
        """
        if installed is None:
            return [], []

        known = set(bundles) | {installed.bundle_identifier}
        superseded = [
            r for r in after_all if r.id != installed.id and r.bundle_identifier in known
        ]

        removed: list[AppRecord] = []
        for record in superseded:
            try:
                self.atv.delete_app(record.id)
            except AtvloadlyError as exc:
                log.warning("Could not delete superseded record %d: %s", record.id, exc)
                continue
            log.info(
                "Removed superseded record %d (%s v%s) after installing %s",
                record.id,
                record.bundle_identifier,
                record.version,
                installed.bundle_identifier,
            )
            removed.append(record)
        return superseded, removed

    def _surplus_apps(self, after_all: list[AppRecord], removed: list[AppRecord]) -> int:
        """How far over the Apple ID's app limit we are, if at all."""
        gone = {r.id for r in removed}
        remaining = [a for a in after_all if a.id not in gone]
        return max(0, len(remaining) - self.config.max_active_apps)

    # ---- commands --------------------------------------------------------

    def handle_command(self, command: cmd.Command) -> TickResult:
        """Execute a Telegram command. Mirrors :meth:`tick`'s load/save discipline."""
        state = self.store.load()
        state.touch()
        try:
            return self._handle_command(state, command)
        finally:
            self.store.save(state)

    def _handle_command(self, state: State, command: cmd.Command) -> TickResult:
        if command.kind == cmd.SET_WINDOW_PREFIX:
            return self._cmd_set_window(state, command)
        if command.kind == cmd.SCHEDULE:
            self._show(command, "🕒 <b>When should updates install?</b>\n"
                       f"Times are {esc(self.config.timezone.key)}.",
                       cmd.schedule_keyboard(self._window_code(state)))
            return TickResult("schedule-menu")
        if command.kind == cmd.MENU:
            self._show(command, self._menu_text(state), cmd.menu_keyboard())
            return TickResult("menu")
        if command.kind == cmd.STATUS:
            self._show(command, self._status_text(state), cmd.menu_keyboard())
            return TickResult("status")
        if command.kind == cmd.SKIP:
            return self._cmd_skip(state, command)
        if command.kind == cmd.UPDATE_NOW:
            return self._cmd_update_now(state, command)

        log.warning("Unhandled command kind: %r", command.kind)
        return TickResult(Outcome.ERROR, f"unhandled command {command.kind}")

    def _cmd_set_window(self, state: State, command: cmd.Command) -> TickResult:
        arg = (command.arg or "").strip()
        if arg == cmd.WINDOW_ALWAYS:
            override = cmd.WINDOW_ALWAYS
        else:
            try:
                hour = int(arg)
                if not 0 <= hour <= 23:
                    raise ValueError(arg)
            except ValueError:
                log.warning("Ignoring bad window argument %r", arg)
                return TickResult(Outcome.ERROR, f"bad window {arg!r}")
            override = cmd.window_for_hour(hour)

        state.quiet_window_override = override
        effective = self.window(state)
        log.info("Quiet window set to %s", effective.describe())

        self._show(
            command,
            "🕒 <b>Schedule updated</b>\n"
            + (
                "Updates will install as soon as they are found."
                if effective.always_open
                else f"Updates will install between <b>{esc(effective.describe())}</b> "
                f"({esc(self.config.timezone.key)})."
            ),
            cmd.schedule_keyboard(self._window_code(state)),
        )
        return TickResult("window-set", effective.describe())

    def _cmd_skip(self, state: State, command: cmd.Command) -> TickResult:
        tag = state.last_seen_tag
        if not tag or tag == state.last_installed_tag:
            self._show(command, "Nothing pending to skip — you're already up to date.")
            return TickResult(Outcome.UP_TO_DATE, "nothing to skip")

        state.skip(tag)
        state.reset_attempts()
        state.last_result = Outcome.SKIPPED
        version = self.config.version_from_tag(tag) or tag
        log.info("Skipping %s by request", tag)

        self._show(
            command,
            f"⏭ <b>Skipped Nuvio TV {esc(version)}</b>\n"
            "It won't be installed. The next release will be offered as normal.",
        )
        return TickResult(Outcome.SKIPPED, tag)

    def _cmd_update_now(self, state: State, command: cmd.Command) -> TickResult:
        release = Release.from_cache(state.cached_release)
        if release is None:
            self._show(command, "⚠️ No release information cached yet — try again shortly.")
            return TickResult(Outcome.ERROR, "no cached release")

        # Deliberately the opposite default from the tick loop: there an unknown
        # installed fingerprint means "leave it alone", here it means "go ahead".
        # The user asked explicitly, and this is the only escape hatch when the
        # build they are running was itself replaced upstream.
        if release.fingerprint == state.last_installed_release:
            version = self.config.version_from_tag(release.tag) or release.tag
            self._show(command, f"✅ Already on <b>{esc(version)}</b> — nothing to install.")
            return TickResult(Outcome.UP_TO_DATE, release.tag)

        # A manual request overrides an earlier skip and any exhausted retry
        # budget; otherwise the button would silently do nothing.
        if state.is_skipped(release.tag):
            state.skipped_tags.remove(release.tag)
        state.reset_attempts()

        version = self.config.version_from_tag(release.tag) or release.tag
        self._show(command, f"⚡ <b>Installing Nuvio TV {esc(version)} now…</b>")

        result = self._attempt_install(state, release)
        log.info("Manual update finished: %s -- %s", result.outcome, result.detail)
        return result

    # ---- command presentation -------------------------------------------

    def _show(self, command: cmd.Command, text: str, keyboard=None) -> None:
        """Update the pressed message in place, or send fresh for typed commands."""
        if command.message_id is not None:
            if self.notifier.edit(command.message_id, text, keyboard):
                return
        self.notifier.send(text, keyboard)

    def _window_code(self, state: State) -> str:
        """The schedule keyboard ticks whichever entry matches this."""
        window = self.window(state)
        return cmd.WINDOW_ALWAYS if window.always_open else window.describe()

    def _menu_text(self, state: State) -> str:
        return (
            "🎛 <b>nuvio-autoupdate</b>\n"
            f"Window: <b>{esc(self.window(state).describe())}</b> "
            f"({esc(self.config.timezone.key)})\n"
            f"Checking every {self.config.poll_interval_minutes} min."
        )

    def _status_text(self, state: State) -> str:
        try:
            record = self.atv.app_record(self.bundles(state))
            installed = record.version if record else "not installed"
            expires = format_local(record.expiration_date, self.config.timezone) if record else "—"
        except AtvloadlyError as exc:
            installed, expires = f"unknown ({exc})", "—"

        latest_tag = state.last_seen_tag or "unknown"
        latest = self.config.version_from_tag(latest_tag) or latest_tag
        lines = [
            "📋 <b>Status</b>",
            f"Installed: <b>{esc(installed)}</b>",
            f"Latest release: <b>{esc(latest)}</b>",
            f"Signed until: {esc(expires)}",
            f"Window: {esc(self.window(state).describe())} ({esc(self.config.timezone.key)})",
            f"Last checked: {esc(format_local(state.last_check_ok, self.config.timezone))}",
        ]
        if state.last_result:
            lines.append(f"Last result: {esc(state.last_result)}")
        if state.last_error:
            lines.append(f"Last error: {esc(state.last_error)}")
        if state.skipped_tags:
            lines.append(f"Skipped: {esc(', '.join(state.skipped_tags[-3:]))}")
        return "\n".join(lines)

    # ---- outcome helpers -------------------------------------------------

    def _record_failure(
        self, state: State, release: Release, reason: str, before: AppRecord | None
    ) -> TickResult:
        state.last_error = reason
        state.last_result = Outcome.FAILED
        log.warning("Install of %s failed (attempt %d): %s", release.tag, state.attempts, reason)

        if state.attempts >= self.config.max_attempts:
            self._notify_failure(release, reason, state.attempts, before)
            return TickResult(Outcome.FAILED, reason)

        delay = backoff_delay(state.attempts, self.config.backoff_minutes)
        state.next_attempt_at = (self.clock() + delay).isoformat()
        log.info("Retrying in %d min (attempt %d)", delay.total_seconds() // 60, state.attempts + 1)
        return TickResult(Outcome.WAITING_FOR_BACKOFF, reason)

    def _blocked(self, state: State, detail: str, message: str) -> TickResult:
        """A problem retrying cannot fix. Notify once, then stay quiet."""
        already_reported = state.last_error == detail
        state.last_error = detail
        state.last_result = Outcome.BLOCKED
        # Burn the attempt budget so this tag is not retried until a new one lands.
        state.attempts = max(state.attempts, self.config.max_attempts)
        if not already_reported:
            self.notifier.send(message)
        return TickResult(Outcome.BLOCKED, detail)

    # ---- messages --------------------------------------------------------

    def _notify_detected(self, state: State, release: Release, *, recut: bool = False) -> None:
        version = self.config.version_from_tag(release.tag) or release.tag
        window = self.window(state)
        lines = [f"🆕 <b>Nuvio TV {esc(version)} {'re-released' if recut else 'released'}</b>"]
        if recut:
            lines.append("Upstream replaced this release, so it counts as new again.")
        lines.append(
            "Installing shortly."
            if window.always_open
            else f"Installing during {esc(window.describe())} "
            f"({esc(self.config.timezone.key)})."
        )
        if release.html_url:
            lines.append(f'<a href="{esc(release.html_url)}">Release notes</a>')
        self.notifier.send("\n".join(lines), cmd.release_keyboard())

    def _notify_success(
        self,
        release: Release,
        verification: Verification,
        waited_seconds: float,
        superseded: list[AppRecord],
        removed: list[AppRecord],
        surplus: int,
    ) -> None:
        after = verification.record
        version = (after.version if after else None) or self.config.version_from_tag(release.tag)
        lines = [f"✅ <b>Nuvio TV updated to {esc(version or release.tag)}</b>"]
        if after is not None:
            lines.append(
                f"Signed until {esc(format_local(after.expiration_date, self.config.timezone))}"
            )
        lines.append(f"Took {waited_seconds / 60:.1f} min")

        # Keyed off what was *detected*, not what was successfully deleted --
        # a failed cleanup is exactly when the manual step matters most.
        renamed = [
            r
            for r in superseded
            if after is not None and r.bundle_identifier != after.bundle_identifier
        ]
        if renamed:
            old = renamed[0]
            gone = any(r.id == old.id for r in removed)
            lines += [
                "",
                "⚠️ <b>Upstream changed the app's bundle ID</b>",
                f"<code>{esc(old.bundle_identifier)}</code> → "
                f"<code>{esc(after.bundle_identifier if after else '?')}</code>",
                "tvOS treats this as a <b>separate app</b>, so it installed alongside the old "
                "one and starts with empty settings and add-ons.",
                (
                    f"I removed the old atvloadly record (v{esc(old.version)}) so it stops "
                    "being re-signed."
                    if gone
                    else f"⚠️ I could not remove the old record (id {old.id}) — delete it in "
                    "atvloadly, or it keeps consuming a signing slot."
                ),
                "👉 <b>Delete the old Nuvio icon on your Apple TV</b> — atvloadly has no "
                "uninstall API, so that part is manual.",
            ]
        elif removed:
            lines.append(f"Tidied up {len(removed)} superseded record(s).")

        if surplus > 0:
            lines += [
                "",
                f"⚠️ <b>{surplus + self.config.max_active_apps} apps are being managed</b>, "
                f"but a free Apple ID keeps only {self.config.max_active_apps} active. "
                "Apple will silently break the excess — remove one.",
            ]

        if version_mismatch := verification.version_mismatch:
            expected = self.config.version_from_tag(release.tag)
            lines.append(
                f"⚠️ Reported version differs from the tag (expected {esc(expected)})."
            )
        if release.html_url:
            lines.append(f'<a href="{esc(release.html_url)}">Release notes</a>')
        self.notifier.send("\n".join(lines))

    def _notify_failure(
        self, release: Release, reason: str, attempts: int, before: AppRecord | None
    ) -> None:
        version = self.config.version_from_tag(release.tag) or release.tag
        lines = [
            f"❌ <b>Nuvio TV {esc(version)} update failed</b>",
            f"Gave up after {attempts} attempt{'s' if attempts != 1 else ''}.",
            f"Reason: {esc(reason)}",
        ]
        tail = self._failure_log(before)
        if tail:
            lines.append(code_block(tail))
        lines.append("Nuvio is untouched. Press below to retry, or wait for the next release.")
        self.notifier.send("\n".join(lines), cmd.release_keyboard())

    def _failure_log(self, before: AppRecord | None) -> str:
        """Pull whatever atvloadly logged for the failed task.

        A failed *new* install has no database row yet, so atvloadly writes it to
        ``task_0.log``; an existing app's log stays under its own id.
        """
        candidates: list[int] = [0]
        if before is not None and before.id:
            candidates.insert(0, before.id)
        for app_id in candidates:
            try:
                text = self.atv.task_log(app_id)
            except AtvloadlyError:
                continue
            if text and not text.startswith("<"):
                return text
        return ""
