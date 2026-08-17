from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nuvio_updater.atvloadly import AppRecord
from nuvio_updater.config import Config
from nuvio_updater.github import LatestResult, Release
from nuvio_updater.mcp_backend import InstallResult

BASE_ENV = {
    "ATVLOADLY_URL": "http://atv.test:5533",
    "ATV_DEVICE_ID": "device-abc",
    "ATV_ACCOUNT_ID": "account-abc",
    "QUIET_WINDOW": "always",
    "TZ": "Europe/London",
    "POLL_INTERVAL_MINUTES": "30",
    "MAX_ATTEMPTS": "3",
    "BACKOFF_MINUTES": "15,60,240",
}

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def make_config(tmp_path):
    def _make(**overrides):
        env = dict(BASE_ENV)
        env["STATE_PATH"] = str(tmp_path / "state.json")
        env.update({k: str(v) for k, v in overrides.items()})
        return Config.from_env(env)

    return _make


def make_release(tag="tvos-beta-3.2.6", **overrides):
    data = {
        "tag": tag,
        "name": tag.replace("tvos-beta-", "Beta "),
        "published_at": "2026-08-06T10:00:00Z",
        "html_url": f"https://github.com/bobsupra/NuvioTVOS/releases/tag/{tag}",
        "ipa_url": f"https://github.com/bobsupra/NuvioTVOS/releases/download/{tag}/NuvioTV.ipa",
        "ipa_name": "NuvioTV-unsigned-release.ipa",
        "release_id": 1000,
    }
    data.update(overrides)
    return Release(**data)


def make_record(version="3.2.5", refreshed_at=T0, ok=True, app_id=7, bundle="com.nuvio.app.tv"):
    return AppRecord(
        id=app_id,
        ipa_name="Nuvio TV",
        bundle_identifier=bundle,
        version=version,
        udid="00008110-DEADBEEF",
        account="burner@example.com",
        refreshed_result=ok,
        refreshed_date=refreshed_at,
        expiration_date=(refreshed_at + timedelta(days=7)) if refreshed_at else None,
        installed_date=refreshed_at,
        enabled=True,
    )


class FakeNotifier:
    def __init__(self):
        self.messages: list[str] = []
        self.keyboards: list[object] = []
        self.edits: list[tuple[int, str]] = []
        self.answers: list[tuple[str, str]] = []
        self._next_id = 100

    def send(self, text: str, keyboard=None) -> int:
        self.messages.append(text)
        self.keyboards.append(keyboard)
        self._next_id += 1
        return self._next_id

    def edit(self, message_id: int, text: str, keyboard=None) -> bool:
        self.edits.append((message_id, text))
        self.messages.append(text)
        self.keyboards.append(keyboard)
        return True

    def clear_keyboard(self, message_id: int) -> bool:
        return True

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> bool:
        self.answers.append((callback_id, text))
        return True

    def describe(self) -> str:
        return "fake notifier"

    def last(self) -> str:
        return self.messages[-1] if self.messages else ""

    def button_data(self) -> list[str]:
        """Callback codes on the most recent message that carried buttons."""
        for keyboard in reversed(self.keyboards):
            if keyboard:
                return [b.data for row in keyboard for b in row]
        return []


class FakeWatcher:
    def __init__(self, release=None, etag="etag-1"):
        self.release = release
        self.etag = etag
        self.error: Exception | None = None
        self.not_modified = False
        self.calls: list[str | None] = []

    def latest(self, etag=None):
        self.calls.append(etag)
        if self.error is not None:
            raise self.error
        if self.not_modified and etag == self.etag:
            return LatestResult(release=None, etag=etag, not_modified=True)
        return LatestResult(release=self.release, etag=self.etag, not_modified=False)


class FakeAtvloadly:
    """Returns queued records in order; the final one repeats thereafter.

    ``apps()`` and ``app_record()`` both consume the queue, so a test can script
    a before/after pair simply by passing two records.
    """

    def __init__(self, *records, others=()):
        self.queue = list(records) or [None]
        self.others = list(others)
        self.error: Exception | None = None
        self.logs: dict[int, str] = {}
        self.deleted: list[int] = []
        self.record_calls = 0

    def _next(self):
        self.record_calls += 1
        if self.error is not None:
            raise self.error
        if len(self.queue) > 1:
            return self.queue.pop(0)
        return self.queue[0]

    def apps(self):
        current = self._next()
        return ([current] if current is not None else []) + self.others

    def app_record(self, bundle_ids):
        wanted = (bundle_ids,) if isinstance(bundle_ids, str) else tuple(bundle_ids)
        current = self._next()
        if current is None or current.bundle_identifier not in wanted:
            return None
        return current

    def delete_app(self, app_id):
        self.deleted.append(app_id)
        return True

    def task_log(self, app_id, tail_chars=1500):
        return self.logs.get(app_id, "<no log>")


class FakeBackend:
    def __init__(self, result=None, error=None, busy=False):
        self.result = result or InstallResult(
            queued=True, completed=True, timed_out=False, waited_seconds=120.0
        )
        self.error = error
        self.busy = busy
        self.installs: list[str] = []

    def ping(self) -> str:
        return "fake-mcp 1.0"

    def install_in_progress(self) -> bool:
        return self.busy

    def install_and_wait(self, ipa_url, *, timeout_seconds, poll_seconds, on_progress=None):
        self.installs.append(ipa_url)
        if self.error is not None:
            raise self.error
        return self.result
