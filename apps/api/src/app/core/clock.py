"""Time access, centralised so that it is both UTC-correct and testable.

Every timestamp the platform stores is UTC and timezone-aware. Source-domain time
semantics (an application deadline stated as "23:59 GMT", a Beijing-time review SLA)
are a property of the *data* and are carried by explicit columns on the relevant
tables, never by the process clock -- see ARCHITECTURE.md D3 and section 4.5.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

# Operational scheduling zone: the PRD fixes source comparison at 06:00/12:00/18:00
# Beijing time and the review SLA at 22:00 the same day.
SCHEDULER_TIMEZONE = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    """Current instant as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def now_in(zone: tzinfo) -> datetime:
    """Current instant rendered in ``zone``.

    Use only for scheduling and display. Never for values that get persisted.
    """
    return datetime.now(zone)


def to_utc(value: datetime) -> datetime:
    """Normalise an aware datetime to UTC.

    Naive datetimes are rejected rather than silently assumed to be UTC: guessing a
    zone is how deadline data quietly becomes wrong.
    """
    if value.tzinfo is None:
        raise ValueError("naive datetime rejected; attach a timezone before conversion")
    return value.astimezone(UTC)
