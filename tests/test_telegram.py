from __future__ import annotations

import json

import httpx
import pytest

from nuvio_updater.telegram import (
    Button,
    CallbackPress,
    TelegramClient,
    TelegramError,
    TextCommand,
    keyboard_markup,
    parse_update,
)

CHAT = "1234567890"
OTHER_CHAT = "999999"


class Api:
    """Scriptable Telegram API: method name -> result payload."""

    def __init__(self, results: dict[str, object] | None = None):
        self.results = results or {}
        self.calls: list[tuple[str, dict]] = []
        self.fail: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = str(request.url).rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, body))
        if self.fail == method:
            return httpx.Response(400, json={"ok": False, "description": "Bad Request"})
        return httpx.Response(200, json={"ok": True, "result": self.results.get(method, {})})

    def body_for(self, method: str) -> dict:
        return next(b for m, b in self.calls if m == method)

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def client_for(api: Api) -> TelegramClient:
    transport = httpx.MockTransport(api.handler)
    return TelegramClient(
        "tok",
        CHAT,
        client=httpx.Client(transport=transport),
        poll_client=httpx.Client(transport=transport),
    )


def callback_update(update_id: int, data: str, chat: str = CHAT, message_id: int = 55) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "data": data,
            "from": {"id": 1, "username": "ant"},
            "message": {"message_id": message_id, "chat": {"id": int(chat)}},
        },
    }


def message_update(update_id: int, text: str, chat: str = CHAT) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "text": text,
            "from": {"id": 1, "username": "ant"},
            "chat": {"id": int(chat)},
        },
    }


class TestButton:
    def test_rejects_oversized_callback_data(self):
        with pytest.raises(ValueError, match="64"):
            Button("x", "y" * 65)

    def test_accepts_data_at_the_limit(self):
        assert Button("x", "y" * 64).data == "y" * 64

    def test_counts_bytes_not_characters(self):
        # Emoji are multi-byte; Telegram's limit is in bytes.
        with pytest.raises(ValueError):
            Button("x", "🕒" * 17)

    def test_markup_shape(self):
        markup = keyboard_markup([[Button("Go", "u"), Button("No", "s")]])
        assert markup == {
            "inline_keyboard": [[
                {"text": "Go", "callback_data": "u"},
                {"text": "No", "callback_data": "s"},
            ]]
        }

    def test_no_keyboard_means_no_markup(self):
        assert keyboard_markup(None) is None


class TestParseUpdate:
    def test_callback(self):
        parsed = parse_update(callback_update(1, "u"))
        assert isinstance(parsed, CallbackPress)
        assert (parsed.data, parsed.chat_id, parsed.message_id) == ("u", CHAT, 55)

    def test_slash_command(self):
        parsed = parse_update(message_update(1, "/status"))
        assert isinstance(parsed, TextCommand)
        assert parsed.text == "/status"

    def test_command_is_lowercased_and_stripped_of_bot_suffix(self):
        parsed = parse_update(message_update(1, "/Status@nuvio_bot"))
        assert isinstance(parsed, TextCommand) and parsed.text == "/status"

    def test_plain_text_is_ignored(self):
        assert parse_update(message_update(1, "hello there")) is None

    @pytest.mark.parametrize("raw", [{}, {"edited_message": {}}, {"callback_query": {}}])
    def test_unusable_updates_ignored(self, raw):
        assert parse_update(raw) is None


class TestGetUpdates:
    def test_returns_updates_and_advances_offset(self):
        api = Api({"getUpdates": [callback_update(10, "u"), callback_update(11, "s")]})
        updates, offset = client_for(api).get_updates(None)
        assert [u.data for u in updates] == ["u", "s"]
        assert offset == 12

    def test_offset_is_sent_back(self):
        api = Api({"getUpdates": []})
        client_for(api).get_updates(42)
        assert api.body_for("getUpdates")["offset"] == 42

    def test_foreign_chat_is_dropped_but_still_advances_offset(self):
        # Otherwise a stranger messaging the bot would wedge the queue forever.
        api = Api({"getUpdates": [callback_update(7, "u", chat=OTHER_CHAT)]})
        updates, offset = client_for(api).get_updates(None)
        assert updates == []
        assert offset == 8

    def test_mixed_authorised_and_foreign(self):
        api = Api({"getUpdates": [
            callback_update(1, "u", chat=OTHER_CHAT),
            callback_update(2, "s"),
        ]})
        updates, offset = client_for(api).get_updates(None)
        assert [u.data for u in updates] == ["s"]
        assert offset == 3

    def test_empty_poll_keeps_offset(self):
        api = Api({"getUpdates": []})
        updates, offset = client_for(api).get_updates(99)
        assert updates == [] and offset == 99

    def test_only_relevant_update_types_requested(self):
        api = Api({"getUpdates": []})
        client_for(api).get_updates(None)
        assert api.body_for("getUpdates")["allowed_updates"] == ["message", "callback_query"]

    def test_api_failure_raises(self):
        api = Api()
        api.fail = "getUpdates"
        with pytest.raises(TelegramError, match="Bad Request"):
            client_for(api).get_updates(None)


class TestDrainBacklog:
    def test_discards_queued_updates_and_returns_next_offset(self):
        api = Api({"getUpdates": [callback_update(30, "u")]})
        assert client_for(api).drain_backlog() == 31
        assert api.body_for("getUpdates")["offset"] == -1

    def test_nothing_queued(self):
        assert client_for(Api({"getUpdates": []})).drain_backlog() is None


class TestSending:
    def test_send_with_keyboard(self):
        api = Api({"sendMessage": {"message_id": 77}})
        message_id = client_for(api).send_message("hi", [[Button("Go", "u")]])
        body = api.body_for("sendMessage")
        assert message_id == 77
        assert body["chat_id"] == CHAT
        assert body["parse_mode"] == "HTML"
        assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "u"

    def test_send_without_keyboard_omits_markup(self):
        api = Api({"sendMessage": {"message_id": 1}})
        client_for(api).send_message("hi")
        assert "reply_markup" not in api.body_for("sendMessage")

    def test_long_message_truncated(self):
        api = Api({"sendMessage": {"message_id": 1}})
        client_for(api).send_message("z" * 9000)
        assert len(api.body_for("sendMessage")["text"]) <= 4096

    def test_edit_clears_buttons_when_none_given(self):
        api = Api({"editMessageText": {}})
        client_for(api).edit_message_text(5, "done")
        assert api.body_for("editMessageText")["reply_markup"] == {"inline_keyboard": []}

    def test_clear_keyboard(self):
        api = Api({"editMessageReplyMarkup": {}})
        client_for(api).clear_keyboard(5)
        assert api.body_for("editMessageReplyMarkup")["reply_markup"] == {"inline_keyboard": []}

    def test_answer_callback(self):
        api = Api({"answerCallbackQuery": True})
        client_for(api).answer_callback("cb1", "Starting…")
        body = api.body_for("answerCallbackQuery")
        assert body["callback_query_id"] == "cb1"
        assert body["text"] == "Starting…"

    def test_answer_callback_omits_empty_text(self):
        api = Api({"answerCallbackQuery": True})
        client_for(api).answer_callback("cb1")
        assert "text" not in api.body_for("answerCallbackQuery")

    def test_get_me(self):
        api = Api({"getMe": {"username": "nuviobot", "first_name": "Nuvio"}})
        assert "nuviobot" in client_for(api).get_me()

    def test_transport_error_becomes_telegramerror(self):
        def boom(request):
            raise httpx.ConnectError("no route")

        client = TelegramClient("tok", CHAT, client=httpx.Client(
            transport=httpx.MockTransport(boom)))
        with pytest.raises(TelegramError, match="failed"):
            client.send_message("hi")
