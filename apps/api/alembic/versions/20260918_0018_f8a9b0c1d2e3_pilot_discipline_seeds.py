"""Pilot discipline seeds: the three top-level codes, and nothing else

`discipline` has been empty since Step 3. The pilot covers Business, Computer
Science, Data and Engineering, so the collection workbook needs *something* to map
onto -- but predefining a whole subject taxonomy now would be inventing structure the
client has not asked for and that no source has yet justified.

So three top-level rows, and no children:

    BUSINESS            商科
    COMPUTER_AND_DATA   计算机与数据
    ENGINEERING         工程

WHY COMPUTER SCIENCE AND DATA ARE ONE CODE
==========================================
The PRD names them separately, and universities do not agree on the boundary. UCL
puts Data Science inside Computer Science; other institutions run it from a statistics
department or a business school. A top-level split would therefore force a judgement
at collection time that the sources themselves do not support, and the judgement would
be ours rather than the institution's.

One code loses nothing recoverable: the workbook keeps `discipline_hint` verbatim
("Computer Science", "Artificial Intelligence", "Data Science", "MSc Business
Analytics"), so the distinction is preserved as the university stated it. If the
client later wants them separated, that is a child-row migration plus a mapping pass
over hints we already hold -- additive, and informed by real data instead of a guess.

WHAT IS DELIBERATELY NOT HERE
=============================
No second level. A programme is attached to a top-level discipline for filtering, and
its real subject lives in the verbatim hint until someone with the collected corpus in
front of them decides what the second level should be.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# No import from app.* (C21/C22): frozen literals only.

revision: str = "f8a9b0c1d2e3"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: (code, name_en, name_zh). Top level only -- every `parent_id` is NULL.
PILOT_DISCIPLINES: tuple[tuple[str, str, str], ...] = (
    ("BUSINESS", "Business and Management", "商科与管理"),
    ("COMPUTER_AND_DATA", "Computer Science and Data", "计算机与数据"),
    ("ENGINEERING", "Engineering", "工程"),
)


def _insert(table: str, columns: str, rows: Sequence[tuple[Any, ...]], conflict: str) -> None:
    """Insert seed rows idempotently. Same idiom as revision 0014."""
    if not rows:
        return
    placeholders = ", ".join(
        "(" + ", ".join(f":p{r}_{c}" for c in range(len(row))) + ")" for r, row in enumerate(rows)
    )
    params = {f"p{r}_{c}": value for r, row in enumerate(rows) for c, value in enumerate(row)}
    op.get_bind().execute(
        sa.text(
            f"INSERT INTO {table} ({columns}) VALUES {placeholders} "
            f"ON CONFLICT {conflict} DO NOTHING"
        ),
        params,
    )


def upgrade() -> None:
    _insert("discipline", "code, name_en, name_zh", PILOT_DISCIPLINES, "(code)")


def downgrade() -> None:
    # Only the seeds, and only where nothing references them. A discipline a
    # programme has been attached to is no longer seed data, and RESTRICT on
    # `program_discipline.discipline_id` will refuse rather than cascade.
    op.get_bind().execute(
        sa.text("DELETE FROM discipline WHERE code = ANY(:codes) AND parent_id IS NULL"),
        {"codes": [code for code, _en, _zh in PILOT_DISCIPLINES]},
    )
