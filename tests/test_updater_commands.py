from __future__ import annotations

import queue
import threading
from datetime import timedelta

import pytest

from nuvio_updater import commands as cmd
from nuvio_updater.schedule import QuietWindow, resolve_window
from nuvio_updater.state import State, StateStore
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
def harness(make_config):
    def _build(*, watcher=None, atv=None, backend=None, seed=None, **config_overrides):
        config = make_config(**config_overrides)
        notifier = FakeNotifier()
        store = StateStore(config.state_path)

        state = State(
            bootstrapped=True,
            last_installed_tag="tvos-beta-3.2.5",
            last_seen_tag="tvos-beta-3.2.5",
        )
        if seed:
            for key, value in seed.items():
                setattr(state, key, value)
        store.save(state)

        updater = Updater(
            config,
            watcher=watcher or FakeWatcher(make_release("tvos-beta-3.2.6")),
            atvloadly=atv if atv is not None else FakeAtvloadly(make_record()),
            backend=backend or FakeBackend(),
            notifier=notifier,
            store=store,
            clock=Clock(),
            sleeper=lambda _: None,
            command_queue=queue.Queue(),
            busy=threading.Event(),
        )
        return updater, notifier

    return _build


def press(kind, arg=None, message_id=55):
    return cmd.Command(kind=kind, arg=arg, callback_id="cb1", message_id=message_id)


# ------------------------------------------------------------------- schedule


class TestResolveWindow:
    def test_override_wins(self):
        default = QuietWindow.parse("04:00-06:00")
        assert resolve_window(default, "22:00-00:00").describe() == "22:00-00:00"

    def test_absent_override_uses_default(self):
        default = QuietWindow.parse("04:00-06:00")
        assert resolve_window(default, None) is default
        assert resolve_window(default, "") is default

    def test_always_override(self):
        assert resolve_window(QuietWindow.parse("04:00-06:00"), "always").always_open

    def test_invalid_override_falls_back_rather_than_raising(self):
        default = QuietWindow.parse("04:00-06:00")
        assert resolve_window(default, "half past nine") is default


class TestSetWindow:
    def test_sets_and_persists_override(self, harness):
        updater, notifier = harness()
        updater.handle_command(press(cmd.SET_WINDOW_PREFIX, "22"))

        state = updater.store.load()
        assert state.quiet_window_override == "22:00-00:00"
        assert updater.window(state).describe() == "22:00-00:00"
        assert "22:00-00:00" in notifier.last()

    def test_always_disables_the_window(self, harness):
        updater, notifier = harness()
        updater.handle_command(press(cmd.SET_WINDOW_PREFIX, cmd.WINDOW_ALWAYS))

        state = updater.store.load()
        assert updater.window(state).always_open
        assert "as soon as" in notifier.last()

    def test_override_beats_env_config(self, harness):
        updater, _ = harness(QUIET_WINDOW="04:00-06:00")
        updater.handle_command(press(cmd.SET_WINDOW_PREFIX, "10"))
        assert updater.window(updater.store.load()).describe() == "10:00-12:00"

    @pytest.mark.parametrize("arg", ["99", "abc", "", None])
    def test_bad_argument_changes_nothing(self, harness, arg):
        updater, _ = harness()
        result = updater.handle_command(press(cmd.SET_WINDOW_PREFIX, arg))
        assert result.outcome == Outcome.ERROR
        assert updater.store.load().quiet_window_override is None

    def test_menu_reflects_the_new_window(self, harness):
        updater, notifier = harness()
        updater.handle_command(press(cmd.SET_WINDOW_PREFIX, "08"))
        updater.handle_command(press(cmd.MENU))
        assert "08:00-10:00" in notifier.last()


# ----------------------------------------------------------------------- skip


class TestSkip:
    def test_skips_the_pending_release(self, harness):
        updater, notifier = harness(
            seed={"last_seen_tag": "tvos-beta-3.2.6"}
        )
        result = updater.handle_command(press(cmd.SKIP))

        state = updater.store.load()
        assert result.outcome == Outcome.SKIPPED
        assert state.is_skipped("tvos-beta-3.2.6")
        assert "Skipped" in notifier.last() and "3.2.6" in notifier.last()

    def test_skipped_release_is_not_installed_on_later_ticks(self, harness):
        backend = FakeBackend()
        updater, _ = harness(
            backend=backend, seed={"last_seen_tag": "tvos-beta-3.2.6"}, QUIET_WINDOW="always"
        )
        updater.handle_command(press(cmd.SKIP))

        result = updater.tick()
        assert result.outcome == Outcome.SKIPPED
        assert backend.installs == []

    def test_a_newer_release_resumes_normally(self, harness):
        backend = FakeBackend()
        watcher = FakeWatcher(make_release("tvos-beta-3.2.6"))
        atv = FakeAtvloadly(
            make_record(version="3.2.5", refreshed_at=T0),
            make_record(version="3.2.7", refreshed_at=T0 + timedelta(minutes=3)),
        )
        updater, _ = harness(
            watcher=watcher, atv=atv, backend=backend,
            seed={"last_seen_tag": "tvos-beta-3.2.6"}, QUIET_WINDOW="always",
        )
        updater.handle_command(press(cmd.SKIP))
        assert updater.tick().outcome == Outcome.SKIPPED

        watcher.release = make_release("tvos-beta-3.2.7")
        assert updater.tick().outcome == Outcome.INSTALLED

    def test_nothing_to_skip_when_up_to_date(self, harness):
        updater, notifier = harness()
        result = updater.handle_command(press(cmd.SKIP))
        assert result.outcome == Outcome.UP_TO_DATE
        assert "Nothing pending" in notifier.last()
        assert updater.store.load().skipped_tags == []

    def test_skip_list_is_bounded(self):
        state = State()
        for i in range(40):
            state.skip(f"tag-{i}")
        assert len(state.skipped_tags) == 20
        assert state.skipped_tags[-1] == "tag-39"

    def test_skipping_twice_does_not_duplicate(self):
        state = State()
        state.skip("t")
        state.skip("t")
        assert state.skipped_tags == ["t"]


# ----------------------------------------------------------------- update now


class TestUpdateNow:
    def _pending(self, harness, **kwargs):
        atv = kwargs.pop("atv", None) or FakeAtvloadly(
            make_record(version="3.2.5", refreshed_at=T0),
            make_record(version="3.2.6", refreshed_at=T0 + timedelta(minutes=3)),
        )
        updater, notifier = harness(atv=atv, QUIET_WINDOW="03:00-03:30", **kwargs)
        # A tick outside the window leaves 3.2.6 pending and caches it.
        updater.tick()
        return updater, notifier

    def test_installs_immediately_outside_the_window(self, harness):
        backend = FakeBackend()
        updater, notifier = self._pending(harness, backend=backend)
        assert updater.tick().outcome == Outcome.WAITING_FOR_WINDOW

        result = updater.handle_command(press(cmd.UPDATE_NOW))

        assert result.outcome == Outcome.INSTALLED
        assert len(backend.installs) == 1
        assert updater.store.load().last_installed_tag == "tvos-beta-3.2.6"
        assert "updated to 3.2.6" in notifier.last()

    def test_resets_an_exhausted_retry_budget(self, harness):
        # Without the reset the button would appear to do nothing after 3 fails.
        stale = make_record(version="3.2.5", refreshed_at=T0)
        backend = FakeBackend()
        updater, _ = self._pending(
            harness, atv=FakeAtvloadly(stale, stale), backend=backend
        )
        state = updater.store.load()
        state.attempts = 99
        updater.store.save(state)

        updater.handle_command(press(cmd.UPDATE_NOW))
        assert len(backend.installs) == 1

    def test_overrides_a_previous_skip(self, harness):
        backend = FakeBackend()
        updater, _ = self._pending(harness, backend=backend)
        updater.handle_command(press(cmd.SKIP))
        assert updater.store.load().is_skipped("tvos-beta-3.2.6")

        result = updater.handle_command(press(cmd.UPDATE_NOW))

        assert result.outcome == Outcome.INSTALLED
        assert not updater.store.load().is_skipped("tvos-beta-3.2.6")

    def test_says_so_when_already_current(self, harness):
        backend = FakeBackend()
        updater, notifier = harness(
            watcher=FakeWatcher(make_release("tvos-beta-3.2.5")), backend=backend
        )
        updater.tick()
        result = updater.handle_command(press(cmd.UPDATE_NOW))

        assert result.outcome == Outcome.UP_TO_DATE
        assert "Already on" in notifier.last()
        assert backend.installs == []

    def test_a_recut_of_the_installed_release_can_be_forced(self, harness):
        # The tick loop leaves an unknown installed fingerprint alone rather than
        # reinstalling. This button is the opposite default -- and the only way
        # back when the build already on the device was itself replaced upstream.
        backend = FakeBackend()
        recut = make_release("tvos-beta-3.2.5", release_id=2)
        updater, notifier = harness(
            watcher=FakeWatcher(recut),
            backend=backend,
            atv=FakeAtvloadly(
                make_record(version="3.2.5", refreshed_at=T0),
                make_record(version="3.2.5", refreshed_at=T0 + timedelta(minutes=3)),
            ),
            seed={
                "last_installed_release": make_release(
                    "tvos-beta-3.2.5", release_id=1
                ).fingerprint,
                "cached_release": recut.to_cache(),
            },
        )
        result = updater.handle_command(press(cmd.UPDATE_NOW))

        assert result.outcome == Outcome.INSTALLED
        assert "Already on" not in notifier.last()
        assert len(backend.installs) == 1
        assert updater.store.load().last_installed_release == recut.fingerprint

    def test_reports_when_no_release_is_cached_yet(self, harness):
        updater, notifier = harness()
        result = updater.handle_command(press(cmd.UPDATE_NOW))
        assert result.outcome == Outcome.ERROR
        assert "No release information" in notifier.last()

    def test_busy_flag_is_set_during_the_install(self, harness):
        seen: list[bool] = []

        class WatchingBackend(FakeBackend):
            def install_and_wait(self, *args, **kwargs):
                seen.append(updater.busy.is_set())
                return super().install_and_wait(*args, **kwargs)

        updater, _ = self._pending(harness, backend=WatchingBackend())
        updater.handle_command(press(cmd.UPDATE_NOW))

        assert seen == [True]
        assert not updater.busy.is_set()  # cleared afterwards


# ------------------------------------------------------------- menu / status


class TestMenuAndStatus:
    def test_status_reports_versions_and_window(self, harness):
        updater, notifier = harness(
            atv=FakeAtvloadly(make_record(version="3.2.5")),
            seed={"last_seen_tag": "tvos-beta-3.2.6"},
            QUIET_WINDOW="04:00-06:00",
        )
        updater.handle_command(press(cmd.STATUS))
        text = notifier.last()
        assert "3.2.5" in text and "3.2.6" in text
        assert "04:00-06:00" in text

    def test_status_survives_atvloadly_being_down(self, harness):
        from nuvio_updater.atvloadly import AtvloadlyError

        atv = FakeAtvloadly(make_record())
        atv.error = AtvloadlyError("refused")
        updater, notifier = harness(atv=atv)
        updater.handle_command(press(cmd.STATUS))
        assert "unknown" in notifier.last()

    def test_status_lists_skipped_versions(self, harness):
        updater, notifier = harness(seed={"skipped_tags": ["tvos-beta-3.2.6"]})
        updater.handle_command(press(cmd.STATUS))
        assert "Skipped" in notifier.last()

    def test_schedule_menu_offers_the_time_grid(self, harness):
        updater, notifier = harness()
        updater.handle_command(press(cmd.SCHEDULE))
        assert any(d.startswith(cmd.SET_WINDOW_PREFIX) for d in notifier.button_data())

    def test_button_press_edits_the_original_message(self, harness):
        updater, notifier = harness()
        updater.handle_command(press(cmd.MENU, message_id=55))
        assert notifier.edits and notifier.edits[-1][0] == 55

    def test_typed_command_sends_a_fresh_message(self, harness):
        updater, notifier = harness()
        updater.handle_command(cmd.Command(cmd.MENU, source="typed"))
        assert notifier.edits == []
        assert "nuvio-autoupdate" in notifier.last()

    def test_unknown_command_is_reported_not_raised(self, harness):
        updater, _ = harness()
        assert updater.handle_command(cmd.Command("nonsense")).outcome == Outcome.ERROR


# ------------------------------------------------------------ bundle renames


class ScriptedAtvloadly:
    """Returns a scripted app list per call, so before/after can differ freely."""

    def __init__(self, *snapshots):
        self.snapshots = list(snapshots)
        self.deleted: list[int] = []
        self.logs: dict[int, str] = {}

    def apps(self):
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]

    def app_record(self, bundle_ids):
        wanted = (bundle_ids,) if isinstance(bundle_ids, str) else tuple(bundle_ids)
        matches = [a for a in self.snapshots[0] if a.bundle_identifier in wanted]
        return matches[-1] if matches else None

    def delete_app(self, app_id):
        self.deleted.append(app_id)
        return True

    def task_log(self, app_id, tail_chars=1500):
        return self.logs.get(app_id, "<no log>")


OLD = "com.nuvio.app.tv"
NEW = "com.pyksel.nuviotvos"


class TestBundleRenameHandling:
    """Reproduces the real 3.2.6 incident: upstream changed the bundle id."""

    def _renaming(self, harness, extra_apps=()):
        old = make_record(version="3.2.5", refreshed_at=T0, app_id=7, bundle=OLD)
        new = make_record(
            version="3.2.6", refreshed_at=T0 + timedelta(minutes=5), app_id=8, bundle=NEW
        )
        atv = ScriptedAtvloadly(
            [old, *extra_apps],
            [old, new, *extra_apps],
        )
        updater, notifier = harness(
            atv=atv, QUIET_WINDOW="always", NUVIO_BUNDLE_IDS=OLD
        )
        return updater, notifier, atv

    def test_install_under_a_new_bundle_is_still_a_success(self, harness):
        updater, notifier, _ = self._renaming(harness)
        result = updater.tick()
        assert result.outcome == Outcome.INSTALLED
        assert updater.store.load().last_installed_tag == "tvos-beta-3.2.6"

    def test_superseded_record_is_deleted(self, harness):
        updater, _, atv = self._renaming(harness)
        updater.tick()
        # Otherwise atvloadly keeps re-signing an app the user no longer runs,
        # burning one of the free account's three slots every week.
        assert atv.deleted == [7]

    def test_new_bundle_is_remembered(self, harness):
        updater, _, _ = self._renaming(harness)
        updater.tick()
        assert updater.store.load().tracked_bundle_id == NEW

    def test_notification_explains_the_rename_and_the_manual_step(self, harness):
        updater, notifier, _ = self._renaming(harness)
        updater.tick()
        text = notifier.last()
        assert "bundle ID" in text
        assert OLD in text and NEW in text
        assert "separate app" in text
        assert "Delete the old Nuvio icon" in text

    def test_warns_when_over_the_app_limit(self, harness):
        spotify = make_record(app_id=5, bundle="com.spotify.client", version="9.1")
        kodi = make_record(app_id=2, bundle="org.xbmc.kodi", version="21.3")
        updater, notifier, _ = self._renaming(harness, extra_apps=(spotify, kodi))
        updater.tick()
        # old is deleted, leaving new + spotify + kodi == 3, which is fine.
        assert "apps are being managed" not in notifier.last()

    def test_warns_when_still_over_after_cleanup(self, harness):
        extras = [
            make_record(app_id=i, bundle=f"com.other.app{i}", version="1.0")
            for i in (2, 5, 9)
        ]
        updater, notifier, _ = self._renaming(harness, extra_apps=tuple(extras))
        updater.tick()
        assert "apps are being managed" in notifier.last()

    def test_a_deletion_failure_does_not_fail_the_update(self, harness):
        updater, notifier, atv = self._renaming(harness)

        def refuse(_app_id):
            from nuvio_updater.atvloadly import AtvloadlyError

            raise AtvloadlyError("record locked")

        atv.delete_app = refuse
        assert updater.tick().outcome == Outcome.INSTALLED
        assert "Delete the old Nuvio icon" in notifier.last()

    def test_stale_record_removed_even_when_the_new_bundle_is_known(self, harness):
        # Regression: once the renamed identifier was added to NUVIO_BUNDLE_IDS
        # the rename stopped looking surprising, so cleanup silently skipped and
        # a fourth record was left behind, over the free account's 3-app limit.
        old = make_record(version="3.2.5", refreshed_at=T0, app_id=7, bundle=OLD)
        new = make_record(
            version="3.2.6", refreshed_at=T0 + timedelta(minutes=5), app_id=9, bundle=NEW
        )
        atv = ScriptedAtvloadly([old], [old, new])
        updater, notifier = harness(
            atv=atv, QUIET_WINDOW="always", NUVIO_BUNDLE_IDS=f"{NEW},{OLD}"
        )

        assert updater.tick().outcome == Outcome.INSTALLED
        assert atv.deleted == [7]
        assert "Delete the old Nuvio icon" in notifier.last()
        assert "apps are being managed" not in notifier.last()

    def test_other_apps_are_never_deleted(self, harness):
        old = make_record(version="3.2.5", refreshed_at=T0, app_id=7, bundle=OLD)
        new = make_record(
            version="3.2.6", refreshed_at=T0 + timedelta(minutes=5), app_id=9, bundle=NEW
        )
        spotify = make_record(app_id=5, bundle="com.spotify.client", version="9.1")
        kodi = make_record(app_id=2, bundle="org.xbmc.kodi-tvos", version="21.3")
        atv = ScriptedAtvloadly([old, spotify, kodi], [old, new, spotify, kodi])
        updater, _ = harness(atv=atv, QUIET_WINDOW="always", NUVIO_BUNDLE_IDS=f"{NEW},{OLD}")

        updater.tick()
        assert atv.deleted == [7]  # Spotify and Kodi untouched

    def test_ordinary_update_deletes_nothing(self, harness):
        before = make_record(version="3.2.6", refreshed_at=T0, app_id=9, bundle=NEW)
        after = make_record(
            version="3.2.7", refreshed_at=T0 + timedelta(minutes=5), app_id=9, bundle=NEW
        )
        atv = ScriptedAtvloadly([before], [after])
        updater, notifier = harness(
            atv=atv, QUIET_WINDOW="always", NUVIO_BUNDLE_IDS=f"{NEW},{OLD}"
        )
        assert updater.tick().outcome == Outcome.INSTALLED
        assert atv.deleted == []
        assert "superseded" not in notifier.last()

    def test_a_known_bundle_is_not_reported_as_a_rename(self, harness):
        before = make_record(version="3.2.6", refreshed_at=T0, app_id=8, bundle=NEW)
        after = make_record(
            version="3.2.7", refreshed_at=T0 + timedelta(minutes=5), app_id=8, bundle=NEW
        )
        atv = ScriptedAtvloadly([before], [after])
        updater, notifier = harness(
            atv=atv, QUIET_WINDOW="always", NUVIO_BUNDLE_IDS=f"{NEW},{OLD}"
        )
        assert updater.tick().outcome == Outcome.INSTALLED
        assert atv.deleted == []
        assert "bundle ID" not in notifier.last()

    def test_tracked_bundle_survives_a_config_that_lags_behind(self, harness):
        # state remembers NEW even though the env still only lists OLD.
        before = make_record(version="3.2.6", refreshed_at=T0, app_id=8, bundle=NEW)
        after = make_record(
            version="3.2.7", refreshed_at=T0 + timedelta(minutes=5), app_id=8, bundle=NEW
        )
        atv = ScriptedAtvloadly([before], [after])
        updater, notifier = harness(
            atv=atv, seed={"tracked_bundle_id": NEW}, QUIET_WINDOW="always",
            NUVIO_BUNDLE_IDS=OLD,
        )
        assert updater.tick().outcome == Outcome.INSTALLED
        assert atv.deleted == []


# ---------------------------------------------------------------- integration


class TestQueueDrivenWait:
    def test_queued_command_is_handled_while_waiting(self, harness):
        updater, notifier = harness()
        updater.commands.put(press(cmd.SET_WINDOW_PREFIX, "06"))

        updater._wait(0.05)

        assert updater.store.load().quiet_window_override == "06:00-08:00"

    def test_wait_returns_when_nothing_is_queued(self, harness):
        updater, _ = harness()
        updater._wait(0.01)  # must not hang

    def test_a_failing_command_does_not_break_the_wait(self, harness):
        updater, _ = harness()

        def explode(_command):
            raise RuntimeError("boom")

        updater.handle_command = explode
        updater.commands.put(press(cmd.MENU))
        updater._wait(0.05)  # swallowed

    def test_release_notification_carries_the_buttons(self, harness):
        updater, notifier = harness(QUIET_WINDOW="03:00-03:30")
        updater.tick()
        assert set(notifier.button_data()) == {cmd.UPDATE_NOW, cmd.SKIP, cmd.SCHEDULE}

    def test_detected_message_shows_the_overridden_window(self, harness):
        updater, notifier = harness(
            seed={"quiet_window_override": "23:00-01:00"}, QUIET_WINDOW="04:00-06:00"
        )
        updater.tick()
        assert "23:00-01:00" in notifier.messages[0]
