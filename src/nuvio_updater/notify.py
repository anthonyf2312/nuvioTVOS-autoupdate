"""Notification delivery and message formatting.

Sending must never be able to break the update loop, so every method here
swallows and logs its own failures. Raw HTTP lives in
:mod:`nuvio_updater.telegram`.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Protocol

from .telegram import Keyboard, TelegramClient, TelegramError

log = logging.getLogger(__name__)


def esc(value: object) -> str:
    """Escape a value for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def summarise(text: str, limit: int = 120) -> str:
    """Collapse a notification to one loggable line."""
    stripped = re.sub(r"<[^>]+>", "", text).replace("\n", " / ").strip()
    stripped = re.sub(r"\s+", " ", stripped)
    return stripped[: limit - 1] + "…" if len(stripped) > limit else stripped


def code_block(text: str, limit: int = 1200) -> str:
    trimmed = text.strip()
    if len(trimmed) > limit:
        trimmed = "..." + trimmed[-limit:]
    return f"<pre>{esc(trimmed)}</pre>"


class Notifier(Protocol):
    def send(self, text: str, keyboard: Keyboard | None = None) -> int | None: ...

    def edit(self, message_id: int, text: str, keyboard: Keyboard | None = None) -> bool: ...

    def clear_keyboard(self, message_id: int) -> bool: ...

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> bool: ...

    def describe(self) -> str: ...


class LogNotifier:
    """Fallback used when Telegram is unconfigured, and during dry runs."""

    def __init__(self, reason: str = "notifications not configured"):
        self.reason = reason
        self.sent: list[str] = []
        self.edits: list[tuple[int, str]] = []
        self._next_id = 1

    def send(self, text: str, keyboard: Keyboard | None = None) -> int | None:
        self.sent.append(text)
        buttons = ""
        if keyboard:
            labels = [b.label for row in keyboard for b in row]
            buttons = f"\n[buttons: {', '.join(labels)}]"
        log.info("[notification suppressed: %s]\n%s%s", self.reason, text, buttons)
        message_id = self._next_id
        self._next_id += 1
        return message_id

    def edit(self, message_id: int, text: str, keyboard: Keyboard | None = None) -> bool:
        self.edits.append((message_id, text))
        log.info("[edit suppressed: %s] message %d\n%s", self.reason, message_id, text)
        return True

    def clear_keyboard(self, message_id: int) -> bool:
        return True

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> bool:
        return True

    def describe(self) -> str:
        return f"log only ({self.reason})"


class TelegramNotifier:
    """Composes user-facing behaviour on top of a :class:`TelegramClient`."""

    def __init__(self, client: TelegramClient):
        self.client = client

    def send(self, text: str, keyboard: Keyboard | None = None) -> int | None:
        try:
            message_id = self.client.send_message(text, keyboard)
        except TelegramError as exc:
            log.error("Telegram send failed: %s", exc)
            return None
        # Otherwise `docker logs` holds no record that anything was sent.
        log.info("Telegram sent: %s", summarise(text))
        log.debug("Telegram payload:\n%s", text)
        return message_id

    def edit(self, message_id: int, text: str, keyboard: Keyboard | None = None) -> bool:
        try:
            self.client.edit_message_text(message_id, text, keyboard)
        except TelegramError as exc:
            log.error("Telegram edit failed: %s", exc)
            return False
        log.info("Telegram edited %d: %s", message_id, summarise(text))
        return True

    def clear_keyboard(self, message_id: int) -> bool:
        try:
            self.client.clear_keyboard(message_id)
        except TelegramError as exc:
            log.debug("Clearing keyboard on %d failed: %s", message_id, exc)
            return False
        return True

    def answer(self, callback_id: str, text: str = "", alert: bool = False) -> bool:
        try:
            self.client.answer_callback(callback_id, text, alert)
        except TelegramError as exc:
            log.debug("answerCallbackQuery failed: %s", exc)
            return False
        return True

    def check(self) -> str:
        """Validate credentials. Raises :class:`TelegramError` on failure."""
        return self.client.get_me()

    def describe(self) -> str:
        return f"Telegram chat {self.client.chat_id}"

    def close(self) -> None:
        self.client.close()


def build_notifier(
    client: TelegramClient | None,
    *,
    dry_run: bool = False,
    reason: str = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set",
) -> Notifier:
    if dry_run:
        return LogNotifier("dry run")
    if client is None:
        return LogNotifier(reason)
    return TelegramNotifier(client)


def build_client(
    bot_token: str | None, chat_id: str | None, *, timeout: float = 20.0
) -> TelegramClient | None:
    if not bot_token or not chat_id:
        return None
    return TelegramClient(bot_token, chat_id, timeout=timeout)
