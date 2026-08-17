"""Inline-keyboard commands and the thread that receives them.

The listener owns nothing but the Telegram connection and the outbound queue.
It never touches ``state.json`` -- the main thread stays the sole writer, so
there is no shared mutable state to guard beyond the queue itself and a
"currently installing" flag it only ever reads.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from .telegram import (
    Button,
    CallbackPress,
    Keyboard,
    TelegramClient,
    TelegramError,
    TextCommand,
    Update,
)

log = logging.getLogger(__name__)

# Callback payloads are capped at 64 bytes, so the codes stay terse.
UPDATE_NOW = "u"
SKIP = "s"
MENU = "m"
SCHEDULE = "sc"
STATUS = "st"
SET_WINDOW_PREFIX = "w:"
WINDOW_ALWAYS = "always"

WINDOW_BLOCK_HOURS = 2


@dataclass(frozen=True)
class Command:
    """A request from the user, to be executed on the main thread."""

    kind: str
    arg: str | None = None
    callback_id: str | None = None
    message_id: int | None = None
    source: str = "button"


def window_for_hour(hour: int) -> str:
    """The 2-hour block starting at ``hour``, e.g. 4 -> ``04:00-06:00``."""
    end = (hour + WINDOW_BLOCK_HOURS) % 24
    return f"{hour:02d}:00-{end:02d}:00"


def release_keyboard() -> Keyboard:
    return [
        [Button("⚡ Update now", UPDATE_NOW), Button("⏭ Skip this one", SKIP)],
        [Button("🕒 Schedule", SCHEDULE)],
    ]


def menu_keyboard() -> Keyboard:
    return [
        [Button("⚡ Update now", UPDATE_NOW), Button("📋 Status", STATUS)],
        [Button("🕒 Schedule", SCHEDULE)],
    ]


def schedule_keyboard(current: str) -> Keyboard:
    """Twelve 2-hour blocks, the active one ticked, plus 'Immediately'."""
    rows: list[list[Button]] = []
    for row_start in range(0, 24, 8):
        row: list[Button] = []
        for hour in range(row_start, row_start + 8, WINDOW_BLOCK_HOURS):
            window = window_for_hour(hour)
            mark = " ✓" if window == current else ""
            row.append(
                Button(f"{hour:02d}-{(hour + WINDOW_BLOCK_HOURS) % 24:02d}{mark}",
                       f"{SET_WINDOW_PREFIX}{hour:02d}")
            )
        rows.append(row)

    always_mark = " ✓" if current == WINDOW_ALWAYS else ""
    rows.append(
        [
            Button(f"⚡ Immediately{always_mark}", f"{SET_WINDOW_PREFIX}{WINDOW_ALWAYS}"),
            Button("← Back", MENU),
        ]
    )
    return rows


def command_from_update(update: Update) -> Command | None:
    """Map a Telegram update onto a :class:`Command`, or ``None`` to ignore."""
    if isinstance(update, CallbackPress):
        data = update.data
        if data.startswith(SET_WINDOW_PREFIX):
            return Command(
                kind=SET_WINDOW_PREFIX,
                arg=data[len(SET_WINDOW_PREFIX) :],
                callback_id=update.callback_id,
                message_id=update.message_id,
            )
        if data in (UPDATE_NOW, SKIP, MENU, SCHEDULE, STATUS):
            return Command(
                kind=data, callback_id=update.callback_id, message_id=update.message_id
            )
        log.warning("Unknown callback data: %r", data)
        return None

    if isinstance(update, TextCommand):
        mapping = {
            "/menu": MENU,
            "/start": MENU,
            "/status": STATUS,
            "/update": UPDATE_NOW,
            "/schedule": SCHEDULE,
        }
        kind = mapping.get(update.text)
        if kind is None:
            return None
        return Command(kind=kind, source="typed")

    return None


def ack_text(command: Command, busy: bool) -> str:
    """Short toast shown on the button itself."""
    if busy and command.kind == UPDATE_NOW:
        return "An install is already running"
    return {
        UPDATE_NOW: "Starting update…",
        SKIP: "Skipping",
        SCHEDULE: "Opening schedule",
        MENU: "Opening menu",
        STATUS: "Fetching status",
        SET_WINDOW_PREFIX: "Saving",
    }.get(command.kind, "")


class CommandListener(threading.Thread):
    """Long-polls Telegram and feeds :class:`Command` objects to the main loop."""

    def __init__(
        self,
        client: TelegramClient,
        commands: queue.Queue[Command],
        *,
        busy: threading.Event | None = None,
        long_poll_seconds: float = 25.0,
        error_backoff_seconds: float = 10.0,
    ):
        super().__init__(name="telegram-listener", daemon=True)
        self.client = client
        self.commands = commands
        self.busy = busy or threading.Event()
        self.long_poll_seconds = long_poll_seconds
        self.error_backoff_seconds = error_backoff_seconds
        self._stop = threading.Event()
        self._offset: int | None = None

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> list[Command]:
        """One long-poll cycle. Acks immediately, then enqueues.

        Telegram shows the user an error if a press is not acknowledged within
        roughly ten seconds, which is far shorter than an install takes -- so
        the ack happens here rather than on the main thread.
        """
        updates, self._offset = self.client.get_updates(self._offset, self.long_poll_seconds)

        accepted: list[Command] = []
        for update in updates:
            command = command_from_update(update)
            if command is None:
                continue

            busy = self.busy.is_set()
            if command.callback_id:
                try:
                    self.client.answer_callback(command.callback_id, ack_text(command, busy))
                except TelegramError as exc:
                    log.debug("Failed to ack callback: %s", exc)

            if busy and command.kind == UPDATE_NOW:
                log.info("Ignoring 'update now' -- an install is already running")
                continue

            log.info("Received command: %s%s (%s)", command.kind, command.arg or "", command.source)
            accepted.append(command)
            self.commands.put(command)

        return accepted

    def run(self) -> None:
        try:
            self._offset = self.client.drain_backlog()
        except TelegramError as exc:
            log.warning("Could not drain Telegram backlog: %s", exc)

        log.info("Telegram command listener started")
        while not self._stop.is_set():
            try:
                self.poll_once()
            except TelegramError as exc:
                log.warning("Telegram poll failed (%s); retrying shortly", exc)
                self._stop.wait(self.error_backoff_seconds)
            except Exception:  # noqa: BLE001 - the listener must never die
                log.exception("Unexpected error in Telegram listener")
                self._stop.wait(self.error_backoff_seconds)
        log.info("Telegram command listener stopped")
