from __future__ import annotations

import queue
import threading

import pytest

from nuvio_updater import commands as cmd
from nuvio_updater.telegram import CallbackPress, TextCommand

from .test_telegram import Api, callback_update, client_for, message_update


class TestWindowHelpers:
    @pytest.mark.parametrize(
        "hour,expected",
        [(0, "00:00-02:00"), (4, "04:00-06:00"), (22, "22:00-00:00")],
    )
    def test_window_for_hour(self, hour, expected):
        assert cmd.window_for_hour(hour) == expected


class TestKeyboards:
    def test_release_keyboard_offers_the_three_actions(self):
        data = [b.data for row in cmd.release_keyboard() for b in row]
        assert set(data) == {cmd.UPDATE_NOW, cmd.SKIP, cmd.SCHEDULE}

    def test_schedule_keyboard_covers_the_whole_day(self):
        rows = cmd.schedule_keyboard("04:00-06:00")
        windows = [
            b.data[len(cmd.SET_WINDOW_PREFIX):]
            for row in rows
            for b in row
            if b.data.startswith(cmd.SET_WINDOW_PREFIX)
        ]
        assert [w for w in windows if w != cmd.WINDOW_ALWAYS] == [
            f"{h:02d}" for h in range(0, 24, 2)
        ]
        assert cmd.WINDOW_ALWAYS in windows

    def test_schedule_keyboard_ticks_the_active_window(self):
        rows = cmd.schedule_keyboard("04:00-06:00")
        ticked = [b.label for row in rows for b in row if "✓" in b.label]
        assert ticked == ["04-06 ✓"]

    def test_schedule_keyboard_ticks_immediately(self):
        rows = cmd.schedule_keyboard(cmd.WINDOW_ALWAYS)
        ticked = [b.label for row in rows for b in row if "✓" in b.label]
        assert len(ticked) == 1 and "Immediately" in ticked[0]

    def test_all_callback_data_within_telegram_limit(self):
        # Button.__post_init__ enforces this; constructing every keyboard proves
        # none of the real payloads are over.
        for kb in (cmd.release_keyboard(), cmd.menu_keyboard(),
                   cmd.schedule_keyboard("04:00-06:00")):
            for row in kb:
                for button in row:
                    assert len(button.data.encode()) <= 64


class TestCommandFromUpdate:
    @pytest.mark.parametrize("code", [cmd.UPDATE_NOW, cmd.SKIP, cmd.MENU, cmd.SCHEDULE, cmd.STATUS])
    def test_known_buttons(self, code):
        press = CallbackPress("cb1", code, "1", 5, "ant")
        command = cmd.command_from_update(press)
        assert command is not None
        assert command.kind == code
        assert command.callback_id == "cb1"
        assert command.message_id == 5

    def test_set_window_splits_the_argument(self):
        press = CallbackPress("cb1", f"{cmd.SET_WINDOW_PREFIX}04", "1", 5, "ant")
        command = cmd.command_from_update(press)
        assert command is not None
        assert command.kind == cmd.SET_WINDOW_PREFIX
        assert command.arg == "04"

    def test_set_window_always(self):
        press = CallbackPress("cb1", f"{cmd.SET_WINDOW_PREFIX}always", "1", 5, "ant")
        command = cmd.command_from_update(press)
        assert command is not None and command.arg == "always"

    def test_unknown_callback_ignored(self):
        assert cmd.command_from_update(CallbackPress("cb1", "wat", "1", 5, "ant")) is None

    @pytest.mark.parametrize(
        "text,kind",
        [("/menu", cmd.MENU), ("/start", cmd.MENU), ("/status", cmd.STATUS),
         ("/update", cmd.UPDATE_NOW), ("/schedule", cmd.SCHEDULE)],
    )
    def test_typed_commands(self, text, kind):
        command = cmd.command_from_update(TextCommand(text, "1", "ant"))
        assert command is not None
        assert command.kind == kind
        assert command.source == "typed"
        assert command.message_id is None  # replies are sent fresh, not edited

    def test_unknown_typed_command_ignored(self):
        assert cmd.command_from_update(TextCommand("/launch", "1", "ant")) is None


class TestAckText:
    def test_busy_update_now_says_so(self):
        command = cmd.Command(cmd.UPDATE_NOW)
        assert "already running" in cmd.ack_text(command, busy=True)

    def test_busy_does_not_affect_other_commands(self):
        assert cmd.ack_text(cmd.Command(cmd.STATUS), busy=True) == "Fetching status"


class TestListener:
    def build(self, api, busy=None):
        return cmd.CommandListener(
            client_for(api), queue.Queue(), busy=busy or threading.Event()
        )

    def test_press_is_acked_then_enqueued(self):
        api = Api({"getUpdates": [callback_update(1, cmd.UPDATE_NOW)], "answerCallbackQuery": True})
        listener = self.build(api)

        accepted = listener.poll_once()

        assert [c.kind for c in accepted] == [cmd.UPDATE_NOW]
        assert listener.commands.get_nowait().kind == cmd.UPDATE_NOW
        # The ack must go out before the main thread ever sees the command.
        assert api.methods() == ["getUpdates", "answerCallbackQuery"]

    def test_update_now_is_refused_while_installing(self):
        busy = threading.Event()
        busy.set()
        api = Api({"getUpdates": [callback_update(1, cmd.UPDATE_NOW)], "answerCallbackQuery": True})
        listener = self.build(api, busy=busy)

        assert listener.poll_once() == []
        assert listener.commands.empty()
        assert "already running" in api.body_for("answerCallbackQuery")["text"]

    def test_other_commands_still_work_while_installing(self):
        busy = threading.Event()
        busy.set()
        api = Api({"getUpdates": [callback_update(1, cmd.STATUS)], "answerCallbackQuery": True})
        listener = self.build(api, busy=busy)
        assert [c.kind for c in listener.poll_once()] == [cmd.STATUS]

    def test_typed_command_needs_no_ack(self):
        api = Api({"getUpdates": [message_update(1, "/status")]})
        listener = self.build(api)
        assert [c.kind for c in listener.poll_once()] == [cmd.STATUS]
        assert "answerCallbackQuery" not in api.methods()

    def test_foreign_chat_never_reaches_the_queue(self):
        api = Api({"getUpdates": [callback_update(1, cmd.UPDATE_NOW, chat="999999")]})
        listener = self.build(api)
        assert listener.poll_once() == []
        assert listener.commands.empty()

    def test_offset_advances_between_polls(self):
        api = Api({"getUpdates": [callback_update(5, cmd.STATUS)], "answerCallbackQuery": True})
        listener = self.build(api)
        listener.poll_once()
        api.results["getUpdates"] = []
        listener.poll_once()
        assert api.calls[-1][1]["offset"] == 6

    def test_failed_ack_does_not_lose_the_command(self):
        api = Api({"getUpdates": [callback_update(1, cmd.SKIP)]})
        api.fail = "answerCallbackQuery"
        listener = self.build(api)
        assert [c.kind for c in listener.poll_once()] == [cmd.SKIP]
