from __future__ import annotations

from datetime import timezone

import pytest

from nuvio_updater.timeutil import parse_timestamp


class TestParseTimestamp:
    def test_go_seven_digit_fraction(self):
        # atvloadly is a Go service and emits more fractional digits than
        # datetime.fromisoformat accepts. This is the format seen in the wild.
        parsed = parse_timestamp("2026-08-05T19:16:35.3360339Z")
        assert parsed is not None
        assert (parsed.year, parsed.month, parsed.day) == (2026, 8, 5)
        assert (parsed.hour, parsed.minute, parsed.second) == (19, 16, 35)
        assert parsed.microsecond == 336033
        assert parsed.tzinfo == timezone.utc

    def test_no_fraction(self):
        parsed = parse_timestamp("2026-08-12T19:16:12Z")
        assert parsed is not None
        assert parsed.microsecond == 0

    def test_numeric_offset_is_normalised_to_utc(self):
        parsed = parse_timestamp("2026-08-05T20:16:35+01:00")
        assert parsed is not None
        assert parsed.hour == 19
        assert parsed.tzinfo == timezone.utc

    def test_offset_without_colon(self):
        parsed = parse_timestamp("2026-08-05T20:16:35+0100")
        assert parsed is not None
        assert parsed.hour == 19

    def test_naive_input_is_assumed_utc(self):
        parsed = parse_timestamp("2026-08-05T19:16:35")
        assert parsed is not None
        assert parsed.tzinfo == timezone.utc

    @pytest.mark.parametrize(
        "raw",
        [None, "", "   ", "not a date", "0001-01-01T00:00:00Z", "2026-13-45T99:99:99Z"],
    )
    def test_unparseable_returns_none(self, raw):
        assert parse_timestamp(raw) is None

    def test_ordering_is_preserved_across_precisions(self):
        earlier = parse_timestamp("2026-08-05T19:16:35.3360339Z")
        later = parse_timestamp("2026-08-06T19:16:35Z")
        assert earlier is not None and later is not None
        assert earlier < later
