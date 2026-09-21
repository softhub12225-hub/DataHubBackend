"""The source-date column group (architecture D17, corrections C9 / C13 / C14).

Defined exactly once and reused with a per-use prefix, because four tables carry
temporal facts under the same honesty requirement and the constraints must not drift
between them.

What this models, and why it is shaped this way
-----------------------------------------------
Official sources state dates at very different precision, with or without a
timezone::

    15 January 2027                -> year, month, day
    January 2027                   -> year, month
    mid-January                    -> year, month, month_part
    15 January 2027, 23:59 GMT     -> year, month, day, time, zone  (an exact instant)
    rolling / until filled         -> no date at all

So the columns store **the calendar parts the source actually gave**, and nothing
more:

* ``precision`` is GENERATED from which parts are present, so it can never
  contradict the data and nobody can set it wrongly.
* ``instant_utc`` exists **if and only if** the source gave date + time + zone. A
  CHECK enforces it, which makes "no invention" a database guarantee rather than a
  convention (C9).
* ``cal_range`` is a **calendar-local** ``daterange`` — no time, no zone, nothing
  about instants. It exists so that a filter like "deadlines in Q1" can include
  imprecise facts. It is a derived query aid, never source truth, never displayed,
  and deliberately not called ``_utc``: a source that stated no timezone gets no UTC
  representation at all (C13).
* A ``MONTH_PART`` fact covers the **whole stated month** in ``cal_range``. Mapping
  early/mid/late to day ranges is a product interpretation, not a source fact, so it
  is not encoded here (C14). ``month_part`` is kept as structured metadata and
  ``official_text`` remains the authoritative rendering.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column, Computed, Integer, String, Text, Time
from sqlalchemy.dialects.postgresql import DATERANGE, TIMESTAMP

from app.db.enums import MONTH_PART, TemporalPrecision

# `precision` is a generated **text** column rather than an enum, and that is forced
# rather than chosen: PostgreSQL requires a generated expression to be IMMUTABLE, and
# `enum_in` -- the text-to-enum cast -- is only STABLE. A CHECK constrains the values
# instead, so the column is still impossible to set wrongly, which is the property
# that matters. `TemporalPrecision` remains the application-level vocabulary.
PRECISION_SQL_TEMPLATE = """
    CASE
        WHEN {p}_year IS NULL           THEN NULL
        WHEN {p}_time IS NOT NULL       THEN 'DATETIME'
        WHEN {p}_day  IS NOT NULL       THEN 'DATE'
        WHEN {p}_month_part IS NOT NULL THEN 'MONTH_PART'
        WHEN {p}_month IS NOT NULL      THEN 'MONTH'
        ELSE 'YEAR'
    END
"""

# Calendar-local range. Conservative by design: a MONTH_PART fact spans the whole
# month (C14), and a YEAR-only fact spans the whole year. Over-covering can surface a
# deadline that turns out to be elsewhere in the period; it can never hide one.
#
# Every function used is IMMUTABLE (make_date, daterange, mod, date + int, integer
# arithmetic), which is what allows this to be a generated column rather than
# something the application computes and could get wrong.
#
# `mod(x, 12)` rather than `x % 12`: a literal percent sign in DDL text collides with
# the driver's pyformat parameter escaping and reaches PostgreSQL as `%%`.
#
# The range guards on month/day are not redundant with the CHECK constraints:
# PostgreSQL evaluates generated columns BEFORE check constraints, so an unguarded
# make_date() would raise a datatype error instead of letting the named constraint
# report the problem. A genuinely impossible date (30 February) still fails inside
# make_date rather than as a named violation -- the row is refused either way, but
# the message is less specific.
CAL_RANGE_SQL_TEMPLATE = """
    CASE
        WHEN {p}_year IS NULL THEN NULL
        WHEN {p}_day IS NOT NULL
             AND {p}_month BETWEEN 1 AND 12
             AND {p}_day BETWEEN 1 AND 31 THEN daterange(
            make_date({p}_year, {p}_month, {p}_day),
            make_date({p}_year, {p}_month, {p}_day) + 1)
        WHEN {p}_month IS NOT NULL AND {p}_month BETWEEN 1 AND 12 THEN daterange(
            make_date({p}_year, {p}_month, 1),
            make_date({p}_year + {p}_month / 12, mod({p}_month, 12) + 1, 1))
        ELSE daterange(
            make_date({p}_year, 1, 1),
            make_date({p}_year + 1, 1, 1))
    END
"""


def source_date_columns(prefix: str) -> list[Column[Any]]:
    """Columns for one temporal fact, prefixed with ``prefix``.

    ``prefix`` is the fact name without a trailing underscore, e.g. ``"deadline"``
    yields ``deadline_year`` … ``deadline_text``.
    """
    p = prefix
    return [
        Column(f"{p}_year", Integer, nullable=True, comment="Calendar year as stated"),
        Column(f"{p}_month", Integer, nullable=True, comment="1-12, only if stated"),
        Column(f"{p}_day", Integer, nullable=True, comment="1-31, only if stated"),
        Column(
            f"{p}_month_part",
            MONTH_PART,
            nullable=True,
            comment="EARLY/MID/LATE. Metadata only; never narrows cal_range (C14)",
        ),
        Column(f"{p}_time", Time(timezone=False), nullable=True, comment="Local wall time"),
        Column(
            f"{p}_timezone",
            String(64),
            nullable=True,
            comment="IANA zone or fixed offset, exactly as the source declared it",
        ),
        Column(
            f"{p}_precision",
            String(16),
            Computed(PRECISION_SQL_TEMPLATE.format(p=p), persisted=True),
            nullable=True,
            comment="Derived from the stored parts; never assigned. See module docstring",
        ),
        Column(
            f"{p}_instant_utc",
            TIMESTAMP(timezone=True),
            nullable=True,
            comment="Exact instant. NULL unless date + time + zone were all stated",
        ),
        Column(
            f"{p}_cal_range",
            DATERANGE,
            Computed(CAL_RANGE_SQL_TEMPLATE.format(p=p), persisted=True),
            nullable=True,
            comment="CALENDAR-LOCAL query aid. Not UTC, not source truth, never displayed",
        ),
        Column(
            f"{p}_text",
            Text,
            nullable=True,
            comment="Verbatim source wording; the authoritative human rendering",
        ),
    ]


def source_date_constraints(prefix: str) -> list[CheckConstraint]:
    """CHECK constraints for one source-date column group.

    Constraint names are suffixed with the prefix so several groups can coexist on
    one table without colliding.
    """
    p = prefix
    return [
        # Calendar parts degrade in one direction only.
        CheckConstraint(
            f"{p}_month IS NULL OR {p}_year IS NOT NULL",
            name=f"{p}_month_requires_year",
        ),
        CheckConstraint(
            f"{p}_day IS NULL OR {p}_month IS NOT NULL",
            name=f"{p}_day_requires_month",
        ),
        CheckConstraint(
            f"{p}_time IS NULL OR {p}_day IS NOT NULL",
            name=f"{p}_time_requires_day",
        ),
        # A timezone qualifies a time; on its own it says nothing.
        CheckConstraint(
            f"{p}_timezone IS NULL OR {p}_time IS NOT NULL",
            name=f"{p}_timezone_requires_time",
        ),
        # "mid-January" is a month-level statement, so it excludes a day.
        CheckConstraint(
            f"{p}_month_part IS NULL OR ({p}_month IS NOT NULL AND {p}_day IS NULL)",
            name=f"{p}_month_part_excludes_day",
        ),
        CheckConstraint(
            f"{p}_month IS NULL OR ({p}_month BETWEEN 1 AND 12)",
            name=f"{p}_month_range",
        ),
        CheckConstraint(
            f"{p}_day IS NULL OR ({p}_day BETWEEN 1 AND 31)",
            name=f"{p}_day_range",
        ),
        # Reject 30 February rather than storing it. Guarded on month being present
        # so this constraint tests only date validity: without the guard,
        # make_date(2027, NULL, 15) returns NULL and this fires for a missing month
        # too, masking the more specific `day_requires_month` violation.
        CheckConstraint(
            f"{p}_day IS NULL OR {p}_month IS NULL "
            f"OR make_date({p}_year, {p}_month, {p}_day) IS NOT NULL",
            name=f"{p}_day_is_a_real_date",
        ),
        # Constrains the generated text column to the TemporalPrecision vocabulary,
        # standing in for the enum type PostgreSQL will not allow here.
        CheckConstraint(
            f"{p}_precision IS NULL OR {p}_precision IN ("
            + ", ".join(f"'{m.value}'" for m in TemporalPrecision)
            + ")",
            name=f"{p}_precision_known",
        ),
        # THE no-invention constraint (C9): an exact instant exists exactly when the
        # source supplied enough to derive one without guessing.
        CheckConstraint(
            f"({p}_instant_utc IS NOT NULL) = "
            f"({p}_time IS NOT NULL AND {p}_timezone IS NOT NULL)",
            name=f"{p}_instant_requires_time_and_zone",
        ),
    ]


__all__ = [
    "CAL_RANGE_SQL_TEMPLATE",
    "PRECISION_SQL_TEMPLATE",
    "source_date_columns",
    "source_date_constraints",
]
