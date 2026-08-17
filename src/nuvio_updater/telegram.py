"""Telegram Bot API client.

Only the transport lives here — message wording belongs in :mod:`nuvio_updater.notify`
and command semantics in :mod:`nuvio_updater.commands`.

Two HTTP clients are used deliberately. Sends want a short timeout so a
misbehaving API call cannot stall the update loop, while ``getUpdates`` holds a
long poll open for ~25 s. Sharing one client would make every poll time out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
MAX_CALLBACK_DATA_BYTES = 64


class TelegramError(Exception):
    """A Telegram API call failed in a way the caller should know about."""


@dataclass(frozen=True)
class Button:
    label: str
    data: str

    def __post_init__(self) -> None:
        encoded = len(self.data.encode("utf-8"))
        if encoded > MAX_CALLBACK_DATA_BYTES:
            raise ValueError(
                f"callback_data {self.data!r} is {encoded} bytes; "
                f"Telegram allows {MAX_CALLBACK_DATA_BYTES}"
            )

    def to_api(self) -> dict[str, str]:
        return {"text": self.label, "callback_data": self.data}


Keyboard = Sequence[Sequence[Button]]


def keyboard_markup(keyboard: Keyboard | None) -> dict[str, Any] | None:
    if keyboard is None:
        return None
    return {"inline_keyboard": [[b.to_api() for b in row] for row in keyboard]}


@dataclass(frozen=True)
class CallbackPress:
    """A user tapped an inline button."""

    callback_id: str
    data: str
    chat_id: str
    message_id: int | None
    user: str


@dataclass(frozen=True)
class TextCommand:
    """A user typed a ``/command``."""

    text: str
    chat_id: str
    user: str


Update = CallbackPress | TextCommand


def parse_update(raw: dict[str, Any]) -> Update | None:
    """Turn a raw Telegram update into a press or a typed command.

    Anything else (edits, photos, joins) is ignored.
    """
    callback = raw.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        sender = callback.get("from") or {}
        data = callback.get("data")
        if not callback.get("id") or not isinstance(data, str):
            return None
        return CallbackPress(
            callback_id=str(callback["id"]),
            data=data,
            chat_id=str(chat.get("id", "")),
            message_id=message.get("message_id"),
            user=str(sender.get("username") or sender.get("id") or "?"),
        )

    message = raw.get("message")
    if isinstance(message, dict):
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return None
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        return TextCommand(
            # Strip any @botname suffix Telegram appends in groups.
            text=text.split("@", 1)[0].strip().lower(),
            chat_id=str(chat.get("id", "")),
            user=str(sender.get("username") or sender.get("id") or "?"),
        )

    return None


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        poll_client: httpx.Client | None = None,
    ):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self._client = client or httpx.Client(timeout=timeout)
        self._poll_client = poll_client
        self._poll_timeout = timeout

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.bot_token}/{method}"

    def _poller(self, long_poll_seconds: float) -> httpx.Client:
        if self._poll_client is None:
            # Read timeout must outlast the long poll itself, with margin.
            self._poll_client = httpx.Client(
                timeout=httpx.Timeout(long_poll_seconds + 15.0, connect=10.0)
            )
        return self._poll_client

    def _call(
        self, method: str, payload: dict[str, Any], *, client: httpx.Client | None = None
    ) -> Any:
        client = client or self._client
        try:
            response = client.post(self._url(method), json=payload)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} failed: {exc}") from exc

        try:
            body = response.json() if response.content else {}
        except ValueError as exc:
            raise TelegramError(f"{method} returned non-JSON: {response.text[:200]}") from exc

        if response.status_code != 200 or not body.get("ok"):
            raise TelegramError(
                f"{method} failed (HTTP {response.status_code}): "
                f"{body.get('description') or response.text[:200]}"
            )
        return body.get("result")

    # ---- reads -----------------------------------------------------------

    def get_me(self) -> str:
        result = self._call("getMe", {}) or {}
        return f"@{result.get('username', '?')} ({result.get('first_name', '?')})"

    def get_updates(self, offset: int | None, long_poll_seconds: float = 25.0) -> tuple[
        list[Update], int | None
    ]:
        """Long-poll for updates.

        Returns the updates addressed to the configured chat, and the offset to
        pass next time. Updates from any other chat are dropped but still
        advance the offset, so a stranger cannot wedge the queue.
        """
        payload: dict[str, Any] = {
            "timeout": int(long_poll_seconds),
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset

        raw_updates = self._call(
            "getUpdates", payload, client=self._poller(long_poll_seconds)
        ) or []

        accepted: list[Update] = []
        next_offset = offset
        for raw in raw_updates:
            if not isinstance(raw, dict):
                continue
            update_id = raw.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1

            parsed = parse_update(raw)
            if parsed is None:
                continue
            if parsed.chat_id != self.chat_id:
                log.warning(
                    "Ignoring Telegram update from unauthorised chat %s (user %s)",
                    parsed.chat_id,
                    parsed.user,
                )
                continue
            accepted.append(parsed)

        return accepted, next_offset

    def drain_backlog(self) -> int | None:
        """Skip anything queued while we were not running.

        Replaying a stale "Update now" from days ago on boot would be both
        surprising and potentially disruptive, so the backlog is discarded.
        """
        raw_updates = self._call("getUpdates", {"offset": -1, "timeout": 0}) or []
        offset = None
        for raw in raw_updates:
            update_id = raw.get("update_id") if isinstance(raw, dict) else None
            if isinstance(update_id, int):
                offset = update_id + 1
        if offset is not None:
            log.info("Discarded %d queued Telegram update(s) from before startup", len(raw_updates))
        return offset

    # ---- writes ----------------------------------------------------------

    def send_message(self, text: str, keyboard: Keyboard | None = None) -> int | None:
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[: MAX_MESSAGE_CHARS - 20].rstrip() + "\n…(truncated)"
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        markup = keyboard_markup(keyboard)
        if markup is not None:
            payload["reply_markup"] = markup
        result = self._call("sendMessage", payload) or {}
        return result.get("message_id")

    def edit_message_text(
        self, message_id: int, text: str, keyboard: Keyboard | None = None
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        payload["reply_markup"] = keyboard_markup(keyboard) or {"inline_keyboard": []}
        self._call("editMessageText", payload)
        return True

    def clear_keyboard(self, message_id: int) -> bool:
        """Strip buttons so a handled message cannot be pressed twice."""
        self._call(
            "editMessageReplyMarkup",
            {
                "chat_id": self.chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": []},
            },
        )
        return True

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> bool:
        """Stop the client-side spinner. Must happen within ~10s of the press."""
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:200]
        if alert:
            payload["show_alert"] = True
        self._call("answerCallbackQuery", payload)
        return True

    def close(self) -> None:
        self._client.close()
        if self._poll_client is not None:
            self._poll_client.close()
