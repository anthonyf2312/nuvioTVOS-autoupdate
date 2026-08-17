from __future__ import annotations

from datetime import timedelta

import pytest

from nuvio_updater.github import GitHubError, NoMatchingAssetError
from nuvio_updater.mcp_backend import InstallConfigurationError, InstallResult, McpError
from nuvio_updater.state import StateStore
from nuvio_updater.updater import Outcome, Updater

from .conftest import (
    T0,
    FakeAtvloadly,
    FakeBackend,
    FakeNotifier,
    FakeWatcher,
    make_record,
    make_release,
)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


@pytest.fixture
def harness(make_config, tmp_path):
    """Assemble an Updater over fakes; overrides go to Config.from_env."""

    def _build(*, watcher=None, atv=None, backend=None, **config_overrides):
        config = make_config(**config_overrides)
        clock = Clock()
        notifier = FakeNotifier()
        updater = Updater(
            config,
            watcher=watcher or FakeWatcher(make_release()),
            atvloadly=atv if atv is not None else FakeAtvloadly(make_record()),
            backend=backend or FakeBackend(),
            notifier=notifier,
            store=StateStore(config.state_path),
            clock=clock,
            sleeper=lambda _: None,
        )
        return updater, notifier, clock

    return _build


def load(updater) -> object:
    return updater.store.load()


# ------------------------------------------------------------------ bootstrap


class TestBootstrap:
    def test_current_version_is_adopted_without_installing(self, harness):
        backend = FakeBackend()
        updater, notifier, _ = harness(
            watcher=FakeWatcher(make_release("tvos-beta-3.2.5")),
            atv=FakeAtvloadly(make_record(version="3.2.5")),
            backend=backend,
        )
        result = updater.tick()

        assert result.outcome == Outcome.BOOTSTRAPPED
        assert load(updater).last_installed_tag == "tvos-beta-3.2.5"
        assert backend.installs == []  # crucially, no pointless reinstall
        assert "watching" in notifier.last()

    def test_behind_latest_leaves_the_update_pending(self, harness):
        updater, notifier, _ = harness(
            watcher=FakeWatcher(make_release("tvos-beta-3.2.6")),
            atv=FakeAtvloadly(make_record(version="3.2.4")),
            backend=FakeBackend(),
            QUIET_WINDOW="00:00-00:01",  # closed, so bootstrap is observed in isolation
        )
        updater.tick()
        state = load(updater)

        assert state.bootstrapped is True
        assert state.last_installed_tag is None
        assert state.last_seen_tag == "tvos-beta-3.2.6"
        assert "3.2.4" in notifier.last() and "3.2.6" in notifier.last()

    def test_missing_app_warns_and_does_not_install(self, harness):
        backend = FakeBackend()
        updater, notifier, _ = harness(atv=FakeAtvloadly(None), backend=backend)
        result = updater.tick()

        assert result.outcome == Outcome.BOOTSTRAPPED
        assert backend.installs == []
        assert "isn't installed" in notifier.last()

    def test_deferred_when_atvloadly_is_down(self, harness):
        from nuvio_updater.atvloadly import AtvloadlyError

        atv = FakeAtvloadly(make_record())
        atv.error = AtvloadlyError("connection refused")
        updater, _, _ = harness(atv=atv)

        result = updater.tick()
        assert result.outcome == Outcome.ERROR
        assert load(updater).bootstrapped is False  # retried next tick


# --------------------------------------------------------------- steady state


def bootstrapped(harness, **kwargs):
    """Build a harness already past bootstrap, sitting on 3.2.5."""
    updater, notifier, clock = harness(**kwargs)
    store = updater.store
    state = store.load()
    state.bootstrapped = True
    state.last_installed_tag = "tvos-beta-3.2.5"
    state.last_seen_tag = "tvos-beta-3.2.5"
    store.save(state)
    notifier.messages.clear()
    return updater, notifier, clock


class TestSteadyState:
    def test_no_new_release_is_a_no_op(self, harness):
        backend = FakeBackend()
        updater, notifier, _ = bootstrapped(
            harness, watcher=FakeWatcher(make_release("tvos-beta-3.2.5")), backend=backend
        )
        assert updater.tick().outcome == Outcome.UP_TO_DATE
        assert backend.installs == []
        assert notifier.messages == []

    def test_new_release_outside_window_notifies_but_waits(self, harness):
        backend = FakeBackend()
        updater, notifier, _ = bootstrapped(
            harness, backend=backend, QUIET_WINDOW="03:00-03:30"
        )
        result = updater.tick()

        assert result.outcome == Outcome.WAITING_FOR_WINDOW
        assert backend.installs == []
        assert len(notifier.messages) == 1
        assert "3.2.6" in notifier.messages[0]

    def test_detection_notifies_exactly_once(self, harness):
        updater, notifier, _ = bootstrapped(harness, QUIET_WINDOW="03:00-03:30")
        updater.tick()
        updater.tick()
        updater.tick()
        assert len(notifier.messages) == 1

    def test_cached_release_is_reused_on_304(self, harness):
        watcher = FakeWatcher(make_release("tvos-beta-3.2.6"))
        updater, _, _ = bootstrapped(harness, watcher=watcher, QUIET_WINDOW="03:00-03:30")
        updater.tick()

        watcher.not_modified = True
        result = updater.tick()

        assert watcher.calls[-1] == "etag-1"  # conditional request was sent
        assert result.outcome == Outcome.WAITING_FOR_WINDOW  # still knows about 3.2.6

    def test_github_failure_is_survivable(self, harness):
        watcher = FakeWatcher(make_release())
        watcher.error = GitHubError("network down")
        updater, notifier, _ = bootstrapped(harness, watcher=watcher)

        assert updater.tick().outcome == Outcome.ERROR
        assert notifier.messages == []  # transient problems stay quiet

    def test_missing_asset_blocks_and_notifies_once(self, harness):
        watcher = FakeWatcher(make_release())
        watcher.error = NoMatchingAssetError("no asset matched")
        updater, notifier, _ = bootstrapped(harness, watcher=watcher)

        assert updater.tick().outcome == Outcome.BLOCKED
        updater.tick()
        assert len(notifier.messages) == 1


class TestRecutRelease:
    """Upstream deleting a release and re-publishing it under the same tag.

    This is what happened to 3.2.8 in Aug 2026: published, pulled, then re-cut
    days later while the server happened to be offline. Keyed on the tag alone
    the replacement is invisible, because the tag never changed.
    """

    def test_a_recut_release_under_the_same_tag_is_detected(self, harness):
        first = make_release("tvos-beta-3.2.6", release_id=1, published_at="2026-08-06T10:00:00Z")
        watcher = FakeWatcher(first)
        updater, notifier, _ = bootstrapped(
            harness, watcher=watcher, QUIET_WINDOW="03:00-03:30"
        )
        assert updater.tick().outcome == Outcome.WAITING_FOR_WINDOW
        assert len(notifier.messages) == 1

        # Deleted and re-created: same tag, new release id and publish time.
        watcher.release = make_release(
            "tvos-beta-3.2.6", release_id=2, published_at="2026-08-09T18:48:53Z"
        )
        assert updater.tick().outcome == Outcome.WAITING_FOR_WINDOW

        assert len(notifier.messages) == 2
        assert "re-released" in notifier.last()
        assert "3.2.6" in notifier.last()

    def test_a_matching_tag_alone_is_not_called_a_recut(self, harness):
        # First tick after upgrading: last_seen_tag was written by the old
        # tag-only code, so it proves nothing about which publication we saw.
        updater, notifier, _ = bootstrapped(
            harness,
            watcher=FakeWatcher(make_release("tvos-beta-3.2.6")),
            QUIET_WINDOW="03:00-03:30",
        )
        state = updater.store.load()
        state.last_seen_tag = "tvos-beta-3.2.6"  # seen, but no fingerprint recorded
        state.last_seen_release = None
        updater.store.save(state)

        updater.tick()

        assert len(notifier.messages) == 1
        assert "released" in notifier.last()
        assert "re-released" not in notifier.last()

    def test_a_recut_release_clears_an_exhausted_attempt_budget(self, harness):
        # The wedged half of the bug: attempts burned on the first publication
        # meant GAVE_UP forever, since only a new tag could reset the budget.
        stale = make_record(version="3.2.5", refreshed_at=T0)
        watcher = FakeWatcher(make_release("tvos-beta-3.2.6", release_id=1))
        updater, notifier, clock = bootstrapped(
            harness, watcher=watcher, atv=FakeAtvloadly(stale), MAX_ATTEMPTS="1"
        )
        updater.tick()
        assert load(updater).attempts == 1
        assert updater.tick().outcome == Outcome.GAVE_UP

        watcher.release = make_release("tvos-beta-3.2.6", release_id=2)
        clock.advance(hours=6)
        result = updater.tick()

        assert result.outcome != Outcome.GAVE_UP
        assert "re-released" in notifier.messages[-2]

    def test_an_unchanged_release_still_notifies_only_once(self, harness):
        # The fingerprint must be stable, or every tick would look like a re-cut.
        updater, notifier, _ = bootstrapped(harness, QUIET_WINDOW="03:00-03:30")
        for _ in range(4):
            updater.tick()
        assert len(notifier.messages) == 1

    def test_state_predating_fingerprints_is_adopted_not_reinstalled(self, harness):
        # Upgrading the bot must not reinstall what is already on the device.
        backend = FakeBackend()
        release = make_release("tvos-beta-3.2.5")
        updater, notifier, _ = bootstrapped(
            harness, watcher=FakeWatcher(release), backend=backend
        )
        assert load(updater).last_installed_release is None  # as written by the old code

        assert updater.tick().outcome == Outcome.UP_TO_DATE
        assert backend.installs == []
        assert notifier.messages == []
        assert load(updater).last_installed_release == release.fingerprint


class TestOfflineReporting:
    def test_a_long_gap_is_reported_once_connectivity_returns(self, harness):
        updater, notifier, clock = bootstrapped(
            harness, QUIET_WINDOW="03:00-03:30", OFFLINE_ALERT_HOURS="6"
        )
        updater.tick()
        notifier.messages.clear()

        clock.advance(hours=30)
        updater.tick()

        assert len(notifier.messages) == 1
        assert "was offline" in notifier.messages[0]
        assert "30 h" in notifier.messages[0]

    def test_a_short_gap_stays_quiet(self, harness):
        updater, notifier, clock = bootstrapped(
            harness, QUIET_WINDOW="03:00-03:30", OFFLINE_ALERT_HOURS="6"
        )
        updater.tick()
        notifier.messages.clear()

        clock.advance(hours=2)
        updater.tick()
        assert notifier.messages == []

    def test_zero_disables_the_alert(self, harness):
        updater, notifier, clock = bootstrapped(
            harness, QUIET_WINDOW="03:00-03:30", OFFLINE_ALERT_HOURS="0"
        )
        updater.tick()
        notifier.messages.clear()

        clock.advance(days=9)
        updater.tick()
        assert notifier.messages == []

    def test_the_first_ever_check_is_not_an_outage(self, harness):
        updater, notifier, _ = bootstrapped(harness, QUIET_WINDOW="03:00-03:30")
        updater.tick()
        assert not any("was offline" in m for m in notifier.messages)


# -------------------------------------------------------------------- install


class TestInstall:
    def test_successful_update(self, harness):
        backend = FakeBackend()
        atv = FakeAtvloadly(
            make_record(version="3.2.5", refreshed_at=T0),
            make_record(version="3.2.6", refreshed_at=T0 + timedelta(minutes=4)),
        )
        updater, notifier, _ = bootstrapped(harness, atv=atv, backend=backend)

        result = updater.tick()
        state = load(updater)

        assert result.outcome == Outcome.INSTALLED
        assert state.last_installed_tag == "tvos-beta-3.2.6"
        assert state.last_installed_release == make_release("tvos-beta-3.2.6").fingerprint
        assert state.attempts == 0
        assert len(backend.installs) == 1
        assert backend.installs[0].endswith(".ipa")
        assert "updated to 3.2.6" in notifier.last()

    def test_second_tick_after_success_is_a_no_op(self, harness):
        backend = FakeBackend()
        atv = FakeAtvloadly(
            make_record(version="3.2.5", refreshed_at=T0),
            make_record(version="3.2.6", refreshed_at=T0 + timedelta(minutes=4)),
        )
        updater, _, _ = bootstrapped(harness, atv=atv, backend=backend)
        updater.tick()
        assert updater.tick().outcome == Outcome.UP_TO_DATE
        assert len(backend.installs) == 1

    def test_unchanged_record_is_treated_as_failure(self, harness):
        # atvloadly reports the task finished, but the row never moved -- which
        # is exactly what a failed new install looks like.
        stale = make_record(version="3.2.5", refreshed_at=T0)
        updater, notifier, _ = bootstrapped(harness, atv=FakeAtvloadly(stale, stale))

        result = updater.tick()
        state = load(updater)

        assert result.outcome == Outcome.WAITING_FOR_BACKOFF
        assert state.last_installed_tag == "tvos-beta-3.2.5"  # unchanged
        assert state.attempts == 1
        assert state.next_attempt_at is not None
        assert notifier.messages == [notifier.messages[0]]  # only the "detected" message

    def test_dry_run_never_writes(self, harness):
        backend = FakeBackend()
        updater, _, _ = bootstrapped(harness, backend=backend, DRY_RUN="true")

        assert updater.tick().outcome == Outcome.DRY_RUN
        assert backend.installs == []
        assert load(updater).last_installed_tag == "tvos-beta-3.2.5"

    def test_busy_atvloadly_defers_without_consuming_an_attempt(self, harness):
        updater, _, _ = bootstrapped(harness, backend=FakeBackend(busy=True))

        assert updater.tick().outcome == Outcome.BUSY
        assert load(updater).attempts == 0

    def test_timeout_with_verified_record_still_counts_as_success(self, harness):
        # get_install_status is global, so a concurrent refresh of another app
        # can keep it busy past our timeout. The record is what decides.
        backend = FakeBackend(
            result=InstallResult(
                queued=True, completed=False, timed_out=True, waited_seconds=1200.0
            )
        )
        atv = FakeAtvloadly(
            make_record(version="3.2.5", refreshed_at=T0),
            make_record(version="3.2.6", refreshed_at=T0 + timedelta(minutes=8)),
        )
        updater, _, _ = bootstrapped(harness, atv=atv, backend=backend)
        assert updater.tick().outcome == Outcome.INSTALLED

    def test_timeout_without_progress_reports_the_timeout(self, harness):
        backend = FakeBackend(
            result=InstallResult(
                queued=True, completed=False, timed_out=True, waited_seconds=1200.0
            )
        )
        stale = make_record(refreshed_at=T0)
        updater, _, _ = bootstrapped(
            harness, atv=FakeAtvloadly(stale, stale), backend=backend, MAX_ATTEMPTS="1"
        )
        result = updater.tick()
        assert result.outcome == Outcome.FAILED
        assert "timed out" in result.detail


class TestRetries:
    def test_backoff_grows_then_gives_up_with_one_notification(self, harness):
        stale = make_record(version="3.2.5", refreshed_at=T0)
        atv = FakeAtvloadly(stale)
        updater, notifier, clock = bootstrapped(harness, atv=atv)
        notifier.messages.clear()

        for _ in range(3):
            updater.tick()
            clock.advance(hours=6)  # step past whatever backoff was set

        state = load(updater)
        assert state.attempts == 3
        failures = [m for m in notifier.messages if "failed" in m]
        assert len(failures) == 1
        assert "Gave up after 3 attempts" in failures[0]

        # Further ticks stay silent until a new release appears.
        assert updater.tick().outcome == Outcome.GAVE_UP
        assert len([m for m in notifier.messages if "failed" in m]) == 1

    def test_backoff_blocks_an_early_retry(self, harness):
        stale = make_record(refreshed_at=T0)
        updater, _, _ = bootstrapped(harness, atv=FakeAtvloadly(stale))
        updater.tick()
        assert updater.tick().outcome == Outcome.WAITING_FOR_BACKOFF
        assert load(updater).attempts == 1  # not incremented while backing off

    def test_a_newer_release_resets_the_attempt_budget(self, harness):
        stale = make_record(refreshed_at=T0)
        watcher = FakeWatcher(make_release("tvos-beta-3.2.6"))
        updater, _, clock = bootstrapped(
            harness, watcher=watcher, atv=FakeAtvloadly(stale), MAX_ATTEMPTS="1"
        )
        updater.tick()
        assert load(updater).attempts == 1

        watcher.release = make_release("tvos-beta-3.2.7")
        clock.advance(hours=6)
        updater.tick()
        assert load(updater).attempts == 1  # reset, then spent on the new tag

    def test_mcp_failure_is_retryable(self, harness):
        backend = FakeBackend(error=McpError("connection reset"))
        updater, _, _ = bootstrapped(harness, backend=backend)

        result = updater.tick()
        assert result.outcome == Outcome.WAITING_FOR_BACKOFF
        assert load(updater).attempts == 1


class TestBlocking:
    def test_configuration_error_gives_up_immediately(self, harness):
        backend = FakeBackend(error=InstallConfigurationError("device_id not found"))
        updater, notifier, _ = bootstrapped(harness, backend=backend)

        result = updater.tick()
        state = load(updater)

        assert result.outcome == Outcome.BLOCKED
        assert state.attempts >= 3  # budget burned; no pointless retries
        assert "misconfigured" in notifier.last()

    def test_missing_app_blocks_install(self, harness):
        backend = FakeBackend()
        updater, notifier, _ = bootstrapped(harness, atv=FakeAtvloadly(None), backend=backend)

        result = updater.tick()
        assert result.outcome == Outcome.BLOCKED
        assert backend.installs == []
        assert "3-app limit" in notifier.last()

    def test_missing_app_is_installable_when_the_guard_is_disabled(self, harness):
        backend = FakeBackend()
        atv = FakeAtvloadly(None, make_record(version="3.2.6", refreshed_at=T0))
        updater, _, _ = bootstrapped(
            harness, atv=atv, backend=backend, REQUIRE_EXISTING_APP="false"
        )
        assert updater.tick().outcome == Outcome.INSTALLED
        assert len(backend.installs) == 1


class TestHeartbeat:
    def test_every_tick_records_a_heartbeat(self, harness):
        updater, _, _ = bootstrapped(harness, watcher=FakeWatcher(make_release("tvos-beta-3.2.5")))
        updater.tick()
        assert load(updater).heartbeat is not None

    def test_heartbeat_survives_an_error_tick(self, harness):
        watcher = FakeWatcher(make_release())
        watcher.error = GitHubError("down")
        updater, _, _ = bootstrapped(harness, watcher=watcher)
        updater.tick()
        assert load(updater).heartbeat is not None
