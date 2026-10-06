"""Timezone handling: store UTC, show Europe/London (BST in summer, GMT in winter)."""
from datetime import datetime, timezone

import pytest

from tracker.classify import Classification
from tracker.timeutil import fmt_london, parse_dt, to_london, to_utc_iso


def test_graph_z_suffix_and_seven_digit_fraction():
    dt = parse_dt("2026-07-01T10:00:00.0000000Z")
    assert dt == datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc)


def test_naive_string_means_london_time_summer():
    # 2pm in a UK email in July is BST (UTC+1)
    assert to_utc_iso("2026-07-01T14:00:00") == "2026-07-01T13:00:00+00:00"


def test_naive_string_means_london_time_winter():
    assert to_utc_iso("2026-01-15T14:00:00") == "2026-01-15T14:00:00+00:00"


def test_explicit_offset_is_respected():
    assert to_utc_iso("2026-10-13T14:00:00+01:00") == "2026-10-13T13:00:00+00:00"
    assert to_utc_iso("2026-10-13T14:00:00-05:00") == "2026-10-13T19:00:00+00:00"


def test_graph_utc_values_parsed_as_utc_not_london():
    # Calendar times are requested with outlook.timezone="UTC" and have no offset in the string
    assert to_utc_iso("2026-07-01T13:00:00.0000000", timezone.utc) == "2026-07-01T13:00:00+00:00"


@pytest.mark.parametrize("utc_in, expected", [
    ("2026-03-28T12:00:00+00:00", "12:00"),   # GMT, day before clocks go forward
    ("2026-03-29T12:00:00+00:00", "13:00"),   # BST begins 29 March 2026
    ("2026-10-24T12:00:00+00:00", "13:00"),   # still BST
    ("2026-10-25T12:00:00+00:00", "12:00"),   # GMT again after 25 October
])
def test_london_display_across_dst_changes(utc_in, expected):
    assert to_london(utc_in).strftime("%H:%M") == expected


def test_clock_change_night_is_unambiguous_in_utc():
    # 00:30 UTC on 25 Oct is 01:30 BST; 01:30 UTC is 01:30 GMT (same wall-clock time, an hour apart)
    assert to_london("2026-10-25T00:30:00+00:00").strftime("%H:%M %Z") == "01:30 BST"
    assert to_london("2026-10-25T01:30:00+00:00").strftime("%H:%M %Z") == "01:30 GMT"


def test_fmt_london_handles_none_and_naive_utc():
    assert fmt_london(None) == ""
    assert fmt_london("2026-07-01T09:00:00", "%H:%M") == "10:00"  # a naive input is read as UTC, shown as BST


def test_round_trip_is_lossless():
    original = "2026-08-12T12:52:30+00:00"
    assert to_utc_iso(to_london(original)) == original


@pytest.mark.parametrize("claude_value, expected", [
    ("2026-10-13T14:00:00+01:00", "2026-10-13T13:00:00+00:00"),
    ("2026-10-13T14:00:00", "2026-10-13T13:00:00+00:00"),      # no zone given: UK local
    ("2026-12-02T09:30:00", "2026-12-02T09:30:00+00:00"),
    ("not a date", None),
    ("", None),
    (None, None),
])
def test_claude_interview_datetime_is_normalised_to_utc(claude_value, expected):
    c = Classification(is_job_related=True, category="interview_scheduled", interview_datetime=claude_value, confidence=0.9)
    assert c.interview_datetime == expected
