"""Formatting helpers."""

from datetime import datetime, timedelta, timezone

import pytest

from hsmcli.utils import format_datetime, format_time_left, truncate


def _iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


# ── format_datetime ───────────────────────────────────────────────────────

def test_format_datetime_handles_z_suffix():
    assert format_datetime("2026-08-05T14:30:00Z") == "2026-08-05 14:30"


def test_format_datetime_handles_offset():
    assert format_datetime("2026-08-05T14:30:00+00:00") == "2026-08-05 14:30"


@pytest.mark.parametrize("bad", ["", None, "not a date", 12345])
def test_format_datetime_passes_through_unparseable(bad):
    """Callers print the result, so a bad value shows raw rather than
    vanishing or raising."""
    assert format_datetime(bad) in ("", bad)


# ── format_time_left ──────────────────────────────────────────────────────

def test_time_left_minutes_only():
    assert format_time_left(_iso(minutes=42, seconds=30)) == "42m left"


def test_time_left_with_hours_pads_minutes():
    assert format_time_left(_iso(hours=1, minutes=12, seconds=30)) == "1h12m left"


def test_time_left_zero_pads():
    assert format_time_left(_iso(hours=2, seconds=30)) == "2h00m left"


def test_time_left_expired():
    assert format_time_left(_iso(minutes=-5)) == "expired"


def test_time_left_exactly_now_is_expired():
    assert format_time_left(datetime.now(timezone.utc).isoformat()) == "expired"


def test_time_left_naive_timestamp_assumed_utc():
    """AWS-lab expires_at sometimes arrives without a zone; treating it as
    local time would shift the countdown by hours."""
    naive = (datetime.now(timezone.utc) + timedelta(minutes=30)
             ).replace(tzinfo=None).isoformat()
    assert format_time_left(naive) == "29m left"


@pytest.mark.parametrize("bad", ["", None, "not a date", 12345])
def test_time_left_empty_on_unparseable(bad):
    """'' lets the caller fall back to printing the raw value."""
    assert format_time_left(bad) == ""


# ── truncate ──────────────────────────────────────────────────────────────

def test_truncate_leaves_short_strings():
    assert truncate("abc", 10) == "abc"


def test_truncate_at_exact_limit():
    assert truncate("abcde", 5) == "abcde"


def test_truncate_adds_ellipsis():
    assert truncate("abcdef", 5) == "abcd…"
    assert len(truncate("abcdef", 5)) == 5


def test_truncate_non_strings():
    assert truncate(None) == ""
    assert truncate(42) == "42"
