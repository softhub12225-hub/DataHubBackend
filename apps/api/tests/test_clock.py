"""Time handling. Small module, high blast radius if wrong."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.core.clock import SCHEDULER_TIMEZONE, now_in, to_utc, utcnow


def test_utcnow_is_timezone_aware_utc() -> None:
    value = utcnow()
    assert value.tzinfo is not None
    assert value.utcoffset() == UTC.utcoffset(None)


def test_scheduler_timezone_is_beijing() -> None:
    """The PRD's 06:00/12:00/18:00 comparison windows are Beijing time."""
    assert str(SCHEDULER_TIMEZONE) == "Asia/Shanghai"


def test_now_in_returns_the_requested_zone() -> None:
    assert now_in(SCHEDULER_TIMEZONE).tzinfo == SCHEDULER_TIMEZONE


def test_to_utc_converts_without_changing_the_instant() -> None:
    london = datetime(2027, 1, 15, 23, 59, tzinfo=ZoneInfo("Europe/London"))
    converted = to_utc(london)
    assert converted.tzinfo == UTC
    assert converted == london


def test_to_utc_rejects_naive_datetimes() -> None:
    """Guessing a zone is how deadline data quietly becomes wrong."""
    with pytest.raises(ValueError, match="naive datetime rejected"):
        to_utc(datetime(2027, 1, 15, 23, 59))  # noqa: DTZ001 -- the case under test


def test_summer_time_offset_is_respected() -> None:
    """A July deadline in London is BST; a naive UTC assumption loses an hour."""
    summer = datetime(2027, 7, 15, 23, 59, tzinfo=ZoneInfo("Europe/London"))
    assert to_utc(summer).hour == 22
