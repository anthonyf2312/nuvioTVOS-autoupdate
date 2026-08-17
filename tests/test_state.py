from __future__ import annotations

import json
from datetime import timedelta

from nuvio_updater.state import SCHEMA_VERSION, State, StateStore
from nuvio_updater.timeutil import utcnow


class TestStateStore:
    def test_missing_file_yields_fresh_state(self, tmp_path):
        state = StateStore(tmp_path / "state.json").load()
        assert state.last_installed_tag is None
        assert state.bootstrapped is False
        assert state.attempts == 0

    def test_roundtrip(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        state = State(last_installed_tag="tvos-beta-3.2.5", attempts=2, bootstrapped=True)
        store.save(state)
        assert store.load() == state

    def test_creates_parent_directory(self, tmp_path):
        store = StateStore(tmp_path / "nested" / "deeper" / "state.json")
        store.save(State(last_seen_tag="x"))
        assert store.load().last_seen_tag == "x"

    def test_corrupt_file_falls_back_to_fresh_state(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json at all")
        assert StateStore(path).load() == State()

    def test_non_object_file_falls_back(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]")
        assert StateStore(path).load() == State()

    def test_unknown_keys_are_ignored(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"last_seen_tag": "t", "from_the_future": True}))
        state = StateStore(path).load()
        assert state.last_seen_tag == "t"

    def test_save_leaves_no_temp_files_behind(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save(State())
        store.save(State(attempts=1))
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_written_file_is_valid_json(self, tmp_path):
        path = tmp_path / "state.json"
        StateStore(path).save(State(last_seen_tag="tvos-beta-3.2.6"))
        assert json.loads(path.read_text())["last_seen_tag"] == "tvos-beta-3.2.6"

    def test_release_fingerprints_roundtrip(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        state = State(
            last_installed_release="tvos-beta-3.2.7|1|t|u|s",
            last_seen_release="tvos-beta-3.2.8|2|t|u|s",
            last_check_ok_at="2026-08-17T09:00:00Z",
        )
        store.save(state)
        assert store.load() == state


class TestMigration:
    def test_schema_1_drops_the_conditional_request_cache(self, tmp_path):
        # A schema-1 cache body predates Release.release_id, so fingerprinting it
        # would compare against something the wire can never reproduce.
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "last_seen_tag": "tvos-beta-3.2.8",
                    "last_installed_tag": "tvos-beta-3.2.7",
                    "etag": 'W/"abc123"',
                    "cached_release": {"tag": "tvos-beta-3.2.8", "ipa_url": "https://x/y.ipa"},
                }
            )
        )
        state = StateStore(path).load()

        assert state.schema_version == SCHEMA_VERSION
        assert state.etag is None
        assert state.cached_release is None
        assert state.last_seen_tag == "tvos-beta-3.2.8"  # everything else survives
        assert state.last_installed_tag == "tvos-beta-3.2.7"

    def test_current_schema_keeps_its_cache(self, tmp_path):
        path = tmp_path / "state.json"
        StateStore(path).save(State(etag='W/"abc"', cached_release={"tag": "t"}))
        assert StateStore(path).load().etag == 'W/"abc"'


class TestStateBehaviour:
    def test_may_attempt_when_no_backoff_set(self):
        assert State().may_attempt_now(utcnow()) is True

    def test_backoff_blocks_then_expires(self):
        now = utcnow()
        state = State(next_attempt_at=(now + timedelta(minutes=15)).isoformat())
        assert state.may_attempt_now(now) is False
        assert state.may_attempt_now(now + timedelta(minutes=16)) is True

    def test_reset_attempts_clears_backoff_and_error(self):
        state = State(attempts=3, next_attempt_at="2026-01-01T00:00:00Z", last_error="boom")
        state.reset_attempts()
        assert (state.attempts, state.next_attempt_at, state.last_error) == (0, None, None)

    def test_touch_records_heartbeat(self):
        state = State()
        assert state.heartbeat is None
        state.touch()
        assert state.heartbeat is not None
        assert (utcnow() - state.heartbeat).total_seconds() < 5

    def test_unparseable_backoff_does_not_block_forever(self):
        state = State(next_attempt_at="garbage")
        assert state.may_attempt_now(utcnow()) is True
