from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from nuvio_updater.schedule import QuietWindow, backoff_delay, parse_backoff, parse_clock

LONDON = ZoneInfo("Europe/London")


def at(hour, minute=0, day=6, month=8):
    return datetime(2026, month, day, hour, minute, tzinfo=LONDON)


class TestParseClock:
    @pytest.mark.parametrize(
        "raw,expected",
        [("04:00", (4, 0)), ("4:5", (4, 5)), ("23:59", (23, 59)), ("24:00", (0, 0))],
    )
    def test_valid(self, raw, expected):
        parsed = parse_clock(raw)
        assert (parsed.hour, parsed.minute) == expected

    @pytest.mark.parametrize("raw", ["", "4", "25:00", "04:61", "abc", "04:00:00:00"])
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_clock(raw)


class TestQuietWindow:
    def test_normal_window_boundaries(self):
        window = QuietWindow.parse("04:00-06:00")
        assert not window.contains(at(3, 59))
        assert window.contains(at(4, 0))  # start is inclusive
        assert window.contains(at(5, 59))
        assert not window.contains(at(6, 0))  # end is exclusive

    def test_window_crossing_midnight(self):
        window = QuietWindow.parse("23:00-02:00")
        assert window.contains(at(23, 30))
        assert window.contains(at(0, 30))
        assert window.contains(at(1, 59))
        assert not window.contains(at(2, 0))
        assert not window.contains(at(12, 0))

    @pytest.mark.parametrize("raw", ["always", "", "  ", "*", "24/7", "00:00-24:00", "05:00-05:00"])
    def test_always_open(self, raw):
        window = QuietWindow.parse(raw)
        assert window.always_open
        assert window.contains(at(13, 37))

    def test_holds_across_bst_and_gmt(self):
        # The window is expressed in local time, so it must land at 04:00 local
        # in both British Summer Time and GMT.
        window = QuietWindow.parse("04:00-06:00")
        summer = datetime(2026, 8, 6, 4, 30, tzinfo=LONDON)
        winter = datetime(2026, 12, 6, 4, 30, tzinfo=LONDON)
        assert summer.utcoffset() != winter.utcoffset()  # sanity: the offsets really differ
        assert window.contains(summer)
        assert window.contains(winter)

    def test_describe(self):
        assert QuietWindow.parse("04:00-06:00").describe() == "04:00-06:00"
        assert QuietWindow.parse("always").describe() == "always"

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            QuietWindow.parse("04:00 to 06:00")


class TestBackoff:
    def test_parse(self):
        assert parse_backoff("15,60,240") == (15, 60, 240)
        assert parse_backoff(" 5 , 10 ") == (5, 10)

    @pytest.mark.parametrize("raw", ["", "   ", "abc", "-5"])
    def test_parse_invalid(self, raw):
        with pytest.raises(ValueError):
            parse_backoff(raw)

    def test_delay_progression(self):
        schedule = (15, 60, 240)
        assert backoff_delay(1, schedule) == timedelta(minutes=15)
        assert backoff_delay(2, schedule) == timedelta(minutes=60)
        assert backoff_delay(3, schedule) == timedelta(minutes=240)

    def test_delay_reuses_last_entry_when_exhausted(self):
        assert backoff_delay(9, (15, 60)) == timedelta(minutes=60)

    def test_no_delay_before_first_attempt(self):
        assert backoff_delay(0, (15,)) == timedelta(0)
