"""Matching a returned pilot workbook back to target institutions.

This module is the **matching contract only**. The pilot import itself is not
implemented, and must not be until the client supplies the actual workbook -- writing
an importer against a guessed file shape is how a mismatch becomes silent data
corruption. What exists here is the rule the importer will obey, expressed as code so
it can be tested now and cannot be quietly weakened later.

THE RULE
========
`target_institution_id` is the only thing that resolves a row. It is a UUID we
generated, printed in the first column of every sheet, and it identifies exactly one
`target_institution`.

When it is present and valid, the row is resolved. When it is absent, altered, or
names an institution we do not have, the row is **not resolved** -- it becomes a
manual-resolution item. A name is used only to *suggest* which institution a human
should confirm, and a suggestion is never applied automatically.

WHY THERE IS NO FUZZY FALLBACK
==============================
Every tempting fallback is wrong in a way that is silent:

* **Exact name match.** The QS list writes ``Essex, University of``; the university
  writes ``University of Essex``. Both are correct, neither matches the other.
* **Normalised name match.** `naming.normalize_institution_name` folds case and
  punctuation only, so it does not bridge that gap either -- deliberately.
* **Fuzzy / trigram match.** The target list contains institutions whose names differ
  by one token: ``University of Canterbury`` (New Zealand) and
  ``Canterbury Christ Church University``; several ``University of London`` colleges;
  ``University of California, Berkeley`` and ``University of California, Davis``. A
  similarity threshold that joins the first pair also joins the second, and the result
  is an official fee attached to the wrong institution.
* **Row order.** A returned workbook may be sorted, filtered or have rows inserted.

A wrong match publishes one university's tuition under another university's name.
That is precisely the failure this platform exists to prevent, and it is invisible
once committed -- unlike an unresolved row, which sits in a list waiting for someone
to answer it. So the trade is made deliberately: this module would rather hand back
work than guess.

`entity_alias` is the right long-term home for known alternative names, populated by
human decisions. Once an alias exists it is an *exact* match against a recorded fact,
not a similarity score. That is the supported way to make future imports match more
often.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, text

from app.domains.onboarding.naming import normalize_institution_name


class MatchOutcome(StrEnum):
    """How a workbook row resolved.

    Only `RESOLVED` may be imported without a human. Everything else is a
    worklist item, and the importer must treat them identically: skip the row, keep
    the data, and report it.
    """

    RESOLVED = "RESOLVED"
    """The id was present, well-formed, and names a target institution we have."""

    MISSING_ID = "MISSING_ID"
    """No id in the row. A name suggestion may be attached, for a human to confirm."""

    MALFORMED_ID = "MALFORMED_ID"
    """Something is in the id cell but it is not a UUID -- truncated, reformatted by
    Excel, or a note typed over it."""

    UNKNOWN_ID = "UNKNOWN_ID"
    """A well-formed UUID that matches no target institution. Usually a workbook from
    a different environment, or an institution removed from the target list."""

    AMBIGUOUS_NAME = "AMBIGUOUS_NAME"
    """No usable id, and the name could be more than one institution. The candidates
    are reported; none is chosen."""


#: Outcomes that a human must resolve. Kept as a constant so the importer cannot
#: accidentally treat a new outcome as importable by forgetting to list it.
NEEDS_MANUAL_RESOLUTION: frozenset[MatchOutcome] = frozenset(
    {
        MatchOutcome.MISSING_ID,
        MatchOutcome.MALFORMED_ID,
        MatchOutcome.UNKNOWN_ID,
        MatchOutcome.AMBIGUOUS_NAME,
    }
)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    """An institution a human might confirm a row against. Never auto-applied."""

    target_institution_id: uuid.UUID
    qs_name: str | None
    destination_code: str | None
    #: Why this is being suggested, in words a reviewer can act on.
    reason: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    """The outcome of resolving one workbook row."""

    outcome: MatchOutcome
    target_institution_id: uuid.UUID | None
    #: Populated for the manual-resolution outcomes. Suggestions only.
    candidates: tuple[MatchCandidate, ...] = ()
    #: Operator-facing explanation, safe to render in a worklist verbatim.
    message: str = ""

    @property
    def is_resolved(self) -> bool:
        return self.outcome is MatchOutcome.RESOLVED

    @property
    def needs_manual_resolution(self) -> bool:
        return self.outcome in NEEDS_MANUAL_RESOLUTION


def resolve_row(
    connection: Connection,
    *,
    raw_id: object,
    raw_name: str | None = None,
    sheet: str = "",
    row_number: int | None = None,
) -> MatchResult:
    """Resolve one workbook row to a target institution, or hand it to a human.

    `raw_id` is whatever was in the `target_institution_id` cell -- a string, a UUID,
    None, or something an operator typed over it. `raw_name` is an optional name from
    the same row, used only to build suggestions.

    This function never returns a resolved match on the strength of a name.
    """
    where = f"{sheet} row {row_number}" if sheet and row_number else "this row"

    parsed = _parse_id(raw_id)
    if parsed is None:
        if raw_id is None or (isinstance(raw_id, str) and not raw_id.strip()):
            candidates = _suggest_by_name(connection, raw_name)
            outcome = (
                MatchOutcome.AMBIGUOUS_NAME if len(candidates) > 1 else MatchOutcome.MISSING_ID
            )
            return MatchResult(
                outcome=outcome,
                target_institution_id=None,
                candidates=candidates,
                message=(
                    f"{where} has no target_institution_id. "
                    + _describe_candidates(candidates)
                    + " Confirm which institution was meant; nothing was imported."
                ),
            )
        return MatchResult(
            outcome=MatchOutcome.MALFORMED_ID,
            target_institution_id=None,
            candidates=_suggest_by_name(connection, raw_name),
            message=(
                f"{where} has {raw_id!r} in target_institution_id, which is not a UUID. "
                "The cell was probably edited or reformatted. Restore it from the "
                "exported template; nothing was imported."
            ),
        )

    found = connection.execute(
        text("SELECT id FROM target_institution WHERE id = :id"), {"id": parsed}
    ).one_or_none()
    if found is None:
        return MatchResult(
            outcome=MatchOutcome.UNKNOWN_ID,
            target_institution_id=None,
            candidates=_suggest_by_name(connection, raw_name),
            message=(
                f"{where} names target_institution {parsed}, which does not exist here. "
                "The workbook may have been exported from a different environment. "
                "Nothing was imported."
            ),
        )

    return MatchResult(
        outcome=MatchOutcome.RESOLVED,
        target_institution_id=parsed,
        message=f"{where} resolved to target_institution {parsed}.",
    )


def _parse_id(raw: object) -> uuid.UUID | None:
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    try:
        return uuid.UUID(candidate)
    except ValueError:
        return None


def _suggest_by_name(connection: Connection, raw_name: str | None) -> tuple[MatchCandidate, ...]:
    """Suggest institutions whose most recent QS name folds to the same match key.

    **Exact on the normalised key, never fuzzy.** This is a lookup to save a reviewer
    typing, not a matching algorithm: if it returns one candidate that is still a
    suggestion a human confirms, and if it returns several the row is ambiguous.
    """
    if not raw_name or not raw_name.strip():
        return ()
    try:
        key = normalize_institution_name(raw_name)
    except ValueError:
        return ()

    rows = connection.execute(
        text(
            "WITH latest AS ("
            "  SELECT DISTINCT ON (e.target_institution_id)"
            "         e.target_institution_id, e.qs_name, e.qs_name_normalized"
            "    FROM target_list_entry e"
            "    JOIN target_list l ON l.id = e.target_list_id"
            "   ORDER BY e.target_institution_id, l.imported_at DESC, e.recorded_at DESC"
            ") "
            "SELECT ti.id, la.qs_name, ti.destination_code "
            "  FROM target_institution ti "
            "  JOIN latest la ON la.target_institution_id = ti.id "
            " WHERE ti.match_key = :key OR la.qs_name_normalized = :key "
            " ORDER BY la.qs_name "
            " LIMIT 10"
        ),
        {"key": key},
    ).all()

    return tuple(
        MatchCandidate(
            target_institution_id=row[0],
            qs_name=row[1],
            destination_code=row[2],
            reason="the supplied name folds to this institution's match key",
        )
        for row in rows
    )


def _describe_candidates(candidates: tuple[MatchCandidate, ...]) -> str:
    if not candidates:
        return "No institution could be suggested from the name."
    if len(candidates) == 1:
        only = candidates[0]
        return (
            f"It may be {only.qs_name!r} ({only.target_institution_id}), "
            "but that is a suggestion, not a match."
        )
    listed = "; ".join(f"{c.qs_name!r} ({c.target_institution_id})" for c in candidates)
    return f"{len(candidates)} institutions could match: {listed}."


__all__ = [
    "NEEDS_MANUAL_RESOLUTION",
    "MatchCandidate",
    "MatchOutcome",
    "MatchResult",
    "resolve_row",
]
