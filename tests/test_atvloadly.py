from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from nuvio_updater.atvloadly import AppRecord, AtvloadlyClient, AtvloadlyError, verify_install

from .conftest import T0, make_record

APPS_PAYLOAD = [
    {
        "ID": 7,
        "ipa_name": "Nuvio TV",
        "udid": "00008110-00066CCE01C0401E",
        "account": "burner@example.com",
        "installed_date": "2026-08-05T19:16:35.3360339Z",
        "refreshed_date": "2026-08-05T19:16:35.3353268Z",
        "expiration_date": "2026-08-12T19:16:12Z",
        "refreshed_result": True,
        "refreshed_error": 0,
        "bundle_identifier": "com.nuvio.app.tv",
        "version": "3.2.5",
        "enabled": True,
    },
    {
        "ID": 5,
        "ipa_name": "Spotify",
        "bundle_identifier": "com.spotify.client",
        "version": "9.1.42",
        "refreshed_result": True,
        "refreshed_date": "2026-08-04T13:06:02.4530005Z",
        "expiration_date": "2026-08-11T13:05:36Z",
        "enabled": True,
    },
]


def client_with(handler):
    return AtvloadlyClient(
        "http://atv.test:5533", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def envelope(data, code=200):
    return {"code": code, "msg": "success", "data": data}


class TestAppRecordParsing:
    def test_parses_go_timestamps_and_fields(self):
        record = AppRecord.from_api(APPS_PAYLOAD[0])
        assert record.id == 7
        assert record.bundle_identifier == "com.nuvio.app.tv"
        assert record.version == "3.2.5"
        assert record.refreshed_result is True
        assert record.refreshed_date is not None
        assert record.expiration_date is not None
        assert record.expiration_date > record.refreshed_date

    def test_tolerates_missing_fields(self):
        record = AppRecord.from_api({})
        assert record.id == 0
        assert record.version == ""
        assert record.refreshed_date is None
        assert record.refreshed_result is False


class TestClientReads:
    def test_app_record_filters_by_bundle(self):
        client = client_with(lambda r: httpx.Response(200, json=envelope(APPS_PAYLOAD)))
        record = client.app_record("com.nuvio.app.tv")
        assert record is not None and record.id == 7

    def test_app_record_absent(self):
        client = client_with(lambda r: httpx.Response(200, json=envelope(APPS_PAYLOAD)))
        assert client.app_record("com.example.missing") is None

    def test_app_record_prefers_highest_id_on_duplicates(self):
        duplicated = [
            {**APPS_PAYLOAD[0], "ID": 3},
            {**APPS_PAYLOAD[0], "ID": 9},
        ]
        client = client_with(lambda r: httpx.Response(200, json=envelope(duplicated)))
        record = client.app_record("com.nuvio.app.tv")
        assert record is not None and record.id == 9

    def test_error_envelope_raises(self):
        client = client_with(
            lambda r: httpx.Response(200, json={"code": 500, "msg": "device not found"})
        )
        with pytest.raises(AtvloadlyError, match="device not found"):
            client.app_record("com.nuvio.app.tv")

    def test_http_error_raises(self):
        client = client_with(lambda r: httpx.Response(502))
        with pytest.raises(AtvloadlyError, match="HTTP 502"):
            client.apps()

    def test_connection_error_raises_atvloadlyerror(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        with pytest.raises(AtvloadlyError, match="failed"):
            client_with(handler).apps()

    def test_healthy_reflects_status_code(self):
        assert client_with(lambda r: httpx.Response(200)).healthy() is True
        assert client_with(lambda r: httpx.Response(503)).healthy() is False

    def test_healthy_is_false_when_unreachable(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        assert client_with(handler).healthy() is False

    def test_task_log_tail_is_bounded(self):
        client = client_with(lambda r: httpx.Response(200, text="x" * 5000))
        tail = client.task_log(7, tail_chars=100)
        assert len(tail) == 103  # 100 chars plus the "..." marker
        assert tail.startswith("...")

    def test_task_log_missing(self):
        client = client_with(lambda r: httpx.Response(404))
        assert "no log" in client.task_log(0)


NUVIO = ("com.nuvio.app.tv",)
BOTH = ("com.pyksel.nuviotvos", "com.nuvio.app.tv")


class TestVerifyInstall:
    def test_success_when_refreshed_date_moves_forward(self):
        before = [make_record(version="3.2.5", refreshed_at=T0)]
        after = [make_record(version="3.2.6", refreshed_at=T0 + timedelta(minutes=5))]
        result = verify_install(before, after, "3.2.6", NUVIO)
        assert result.ok is True
        assert result.version_mismatch is False
        assert result.bundle_changed is False

    def test_stale_record_reads_as_failure(self):
        # The critical case: when a *new* install fails, atvloadly persists
        # nothing, so the previous row survives with refreshed_result=True. A
        # naive check would call this a success.
        unchanged = [make_record(version="3.2.5", refreshed_at=T0)]
        result = verify_install(unchanged, unchanged, "3.2.6", NUVIO)
        assert result.ok is False
        assert "never completed" in result.reason

    def test_refreshed_date_going_backwards_is_failure(self):
        before = [make_record(refreshed_at=T0)]
        after = [make_record(refreshed_at=T0 - timedelta(minutes=1))]
        assert verify_install(before, after, None, NUVIO).ok is False

    def test_explicit_failure_flag(self):
        before = [make_record(refreshed_at=T0)]
        after = [make_record(refreshed_at=T0 + timedelta(minutes=5), ok=False)]
        result = verify_install(before, after, None, NUVIO)
        assert result.ok is False
        assert "failed" in result.reason

    def test_no_records_at_all_after_install(self):
        result = verify_install([make_record()], [], "3.2.6", NUVIO)
        assert result.ok is False
        assert "never completed" in result.reason

    def test_first_install_with_no_prior_record(self):
        after = [make_record(version="3.2.6", refreshed_at=T0)]
        assert verify_install([], after, "3.2.6", NUVIO).ok is True

    def test_version_mismatch_is_a_warning_not_a_failure(self):
        before = [make_record(version="3.2.5", refreshed_at=T0)]
        after = [make_record(version="3.2.5-hotfix", refreshed_at=T0 + timedelta(minutes=5))]
        result = verify_install(before, after, "3.2.6", NUVIO)
        assert result.ok is True
        assert result.version_mismatch is True

    def test_unrelated_app_refreshing_does_not_count_as_our_install(self):
        # atvloadly's own cron may refresh Spotify while we are installing.
        nuvio = make_record(version="3.2.5", refreshed_at=T0, app_id=7)
        spotify_before = make_record(
            version="9.1", refreshed_at=T0, app_id=5, bundle="com.spotify.client"
        )
        spotify_after = make_record(
            version="9.1", refreshed_at=T0 + timedelta(minutes=1), app_id=5,
            bundle="com.spotify.client",
        )
        nuvio_after = make_record(version="3.2.6", refreshed_at=T0 + timedelta(seconds=30),
                                  app_id=7)
        result = verify_install(
            [nuvio, spotify_before], [nuvio_after, spotify_after], "3.2.6", NUVIO
        )
        assert result.ok is True
        assert result.record is not None and result.record.id == 7


class TestBundleRename:
    """Upstream renamed com.nuvio.app.tv -> com.pyksel.nuviotvos at 3.2.6."""

    def test_new_bundle_is_detected_as_our_install(self):
        before = [make_record(version="3.2.5", refreshed_at=T0, app_id=7)]
        after = before + [
            make_record(
                version="3.2.6", refreshed_at=T0 + timedelta(minutes=5), app_id=8,
                bundle="com.pyksel.nuviotvos",
            )
        ]
        result = verify_install(before, after, "3.2.6", NUVIO)

        # The old bundle-keyed lookup saw only the stale row and called this a
        # failure, even though 3.2.6 had installed.
        assert result.ok is True
        assert result.record is not None and result.record.id == 8
        assert result.bundle_changed is True
        assert result.renamed_from is not None and result.renamed_from.id == 7

    def test_known_new_bundle_is_not_flagged_as_a_rename(self):
        before = [make_record(version="3.2.6", refreshed_at=T0, bundle="com.pyksel.nuviotvos")]
        after = [
            make_record(
                version="3.2.7", refreshed_at=T0 + timedelta(minutes=5),
                bundle="com.pyksel.nuviotvos",
            )
        ]
        result = verify_install(before, after, "3.2.7", BOTH)
        assert result.ok is True
        assert result.bundle_changed is False

    def test_rename_with_no_prior_record_has_nothing_to_clean_up(self):
        after = [make_record(version="3.2.6", refreshed_at=T0, bundle="com.brand.new")]
        result = verify_install([], after, "3.2.6", NUVIO)
        assert result.ok is True
        assert result.bundle_changed is True
        assert result.renamed_from is None
