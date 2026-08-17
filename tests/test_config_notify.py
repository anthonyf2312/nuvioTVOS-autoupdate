from __future__ import annotations

import json

import httpx
import pytest

from nuvio_updater.config import Config, ConfigError
from nuvio_updater.notify import (
    LogNotifier,
    TelegramNotifier,
    build_client,
    build_notifier,
    code_block,
    esc,
    summarise,
)
from nuvio_updater.telegram import TelegramClient, TelegramError

from .conftest import BASE_ENV


def env(**overrides):
    return {**BASE_ENV, **overrides}


class TestConfig:
    def test_defaults_are_applied(self, make_config):
        config = make_config()
        # Both the post-3.2.6 identifier and the historical one are tracked.
        assert config.bundle_ids == ("com.pyksel.nuviotvos", "com.nuvio.app.tv")
        assert config.bundle_id == "com.pyksel.nuviotvos"
        assert config.max_active_apps == 3
        assert config.github_repo == "bobsupra/NuvioTVOS"
        assert config.poll_interval_minutes == 30
        assert config.backoff_minutes == (15, 60, 240)

    def test_bundle_ids_parsed_from_a_list(self, make_config):
        config = make_config(NUVIO_BUNDLE_IDS=" a.b , c.d ,, ")
        assert config.bundle_ids == ("a.b", "c.d")

    def test_legacy_single_bundle_var_still_works(self):
        config = Config.from_env({**BASE_ENV, "NUVIO_BUNDLE_ID": "only.one"})
        assert config.bundle_ids == ("only.one",)

    def test_bundle_ids_wins_over_the_legacy_var(self):
        config = Config.from_env(
            {**BASE_ENV, "NUVIO_BUNDLE_IDS": "new.one", "NUVIO_BUNDLE_ID": "old.one"}
        )
        assert config.bundle_ids == ("new.one",)

    def test_empty_bundle_list_is_rejected(self):
        with pytest.raises(ConfigError, match="NUVIO_BUNDLE_IDS"):
            Config.from_env({**BASE_ENV, "NUVIO_BUNDLE_IDS": " , ,", "NUVIO_BUNDLE_ID": ""})

    def test_mcp_url_is_derived(self, make_config):
        assert make_config().mcp_url == "http://atv.test:5533/mcp"

    def test_trailing_slash_is_trimmed(self, make_config):
        config = make_config(ATVLOADLY_URL="http://atv.test:5533/")
        assert config.mcp_url == "http://atv.test:5533/mcp"

    @pytest.mark.parametrize(
        "tag,expected",
        [
            ("tvos-beta-3.2.5", "3.2.5"),
            ("tvos-beta-3.2.5-rc1", "3.2.5-rc1"),
            ("v1.0.0", None),
        ],
    )
    def test_version_from_tag(self, make_config, tag, expected):
        assert make_config().version_from_tag(tag) == expected

    def test_missing_device_id_is_rejected(self):
        with pytest.raises(ConfigError, match="ATV_DEVICE_ID"):
            Config.from_env(env(ATV_DEVICE_ID=""))

    def test_missing_account_id_is_rejected(self):
        with pytest.raises(ConfigError, match="ATV_ACCOUNT_ID"):
            Config.from_env(env(ATV_ACCOUNT_ID=""))

    @pytest.mark.parametrize("url", ["atv.test:5533", "ftp://atv.test"])
    def test_bad_url_is_rejected(self, url):
        with pytest.raises(ConfigError, match="ATVLOADLY_URL"):
            Config.from_env(env(ATVLOADLY_URL=url))

    def test_blank_url_falls_back_to_the_default(self):
        # An unset or blank variable means "use the default", not "no value".
        assert Config.from_env(env(ATVLOADLY_URL="")).atvloadly_url == "http://192.168.1.180:5533"

    def test_bad_repo_is_rejected(self):
        with pytest.raises(ConfigError, match="GITHUB_REPO"):
            Config.from_env(env(GITHUB_REPO="justaname"))

    def test_bad_window_is_rejected(self):
        with pytest.raises(ConfigError, match="QUIET_WINDOW"):
            Config.from_env(env(QUIET_WINDOW="nightly"))

    def test_bad_timezone_is_rejected(self):
        with pytest.raises(ConfigError, match="TZ"):
            Config.from_env(env(TZ="Mars/Olympus_Mons"))

    def test_bad_regex_is_rejected(self):
        with pytest.raises(ConfigError, match="TAG_VERSION_RE"):
            Config.from_env(env(TAG_VERSION_RE="([unclosed"))

    def test_bad_integer_is_rejected(self):
        with pytest.raises(ConfigError, match="POLL_INTERVAL_MINUTES"):
            Config.from_env(env(POLL_INTERVAL_MINUTES="soon"))

    def test_zero_interval_is_rejected(self):
        with pytest.raises(ConfigError, match=">= 1"):
            Config.from_env(env(POLL_INTERVAL_MINUTES="0"))

    def test_bad_boolean_is_rejected(self):
        with pytest.raises(ConfigError, match="DRY_RUN"):
            Config.from_env(env(DRY_RUN="maybe"))

    @pytest.mark.parametrize("raw,expected", [("yes", True), ("0", False), ("TRUE", True)])
    def test_boolean_forms(self, raw, expected):
        assert Config.from_env(env(DRY_RUN=raw)).dry_run is expected


class TestNotify:
    @staticmethod
    def notifier_for(handler):
        client = TelegramClient(
            "tok", "42", client=httpx.Client(transport=httpx.MockTransport(handler))
        )
        return TelegramNotifier(client)

    def test_escapes_html(self):
        assert esc("<b>&x</b>") == "&lt;b&gt;&amp;x&lt;/b&gt;"

    def test_summarise_strips_markup_and_newlines(self):
        text = "✅ <b>Nuvio TV updated to 3.2.6</b>\nSigned until 2026-08-13\nTook 4.2 min"
        assert summarise(text) == "✅ Nuvio TV updated to 3.2.6 / Signed until 2026-08-13 / Took 4.2 min"

    def test_summarise_truncates(self):
        assert len(summarise("word " * 200, limit=50)) == 50

    def test_code_block_trims_from_the_end(self):
        block = code_block("y" * 500, limit=50)
        assert block.startswith("<pre>...") and block.endswith("</pre>")
        assert len(block) < 120

    def test_log_notifier_records_instead_of_sending(self):
        notifier = LogNotifier("testing")
        assert notifier.send("hello") is not None
        assert notifier.sent == ["hello"]

    def test_log_notifier_returns_distinct_message_ids(self):
        notifier = LogNotifier()
        assert notifier.send("a") != notifier.send("b")

    def test_build_notifier_falls_back_without_a_client(self):
        assert isinstance(build_notifier(None), LogNotifier)

    def test_build_client_needs_both_halves(self):
        assert build_client(None, None) is None
        assert build_client("token", None) is None
        assert build_client(None, "chat") is None
        assert build_client("token", "chat") is not None

    def test_build_notifier_suppresses_sends_in_dry_run(self):
        client = build_client("token", "chat")
        assert isinstance(build_notifier(client, dry_run=True), LogNotifier)

    def test_build_notifier_returns_telegram_when_configured(self):
        assert isinstance(build_notifier(build_client("token", "chat")), TelegramNotifier)

    def test_telegram_posts_html(self):
        captured = {}

        def capture(request):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

        assert self.notifier_for(capture).send("<b>hi</b>") == 9
        assert captured["url"].endswith("/bottok/sendMessage")
        assert captured["body"]["chat_id"] == "42"
        assert captured["body"]["parse_mode"] == "HTML"
        assert captured["body"]["text"] == "<b>hi</b>"

    def test_telegram_send_failure_is_swallowed(self):
        notifier = self.notifier_for(lambda r: httpx.Response(400, json={"ok": False}))
        assert notifier.send("hi") is None

    def test_telegram_transport_error_is_swallowed(self):
        def boom(request):
            raise httpx.ConnectError("no route")

        assert self.notifier_for(boom).send("hi") is None

    def test_edit_failure_is_swallowed(self):
        notifier = self.notifier_for(lambda r: httpx.Response(400, json={"ok": False}))
        assert notifier.edit(1, "hi") is False

    def test_answer_failure_is_swallowed(self):
        notifier = self.notifier_for(lambda r: httpx.Response(400, json={"ok": False}))
        assert notifier.answer("cb1") is False

    def test_overlong_message_is_truncated(self):
        captured = {}

        def capture(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": {}})

        self.notifier_for(capture).send("z" * 9000)
        assert len(captured["text"]) <= 4096

    def test_check_reports_the_bot_identity(self):
        payload = {"ok": True, "result": {"username": "nuviobot", "first_name": "Nuvio"}}
        notifier = self.notifier_for(lambda r: httpx.Response(200, json=payload))
        assert "nuviobot" in notifier.check()

    def test_check_raises_on_bad_token(self):
        notifier = self.notifier_for(
            lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})
        )
        with pytest.raises(TelegramError, match="Unauthorized"):
            notifier.check()
