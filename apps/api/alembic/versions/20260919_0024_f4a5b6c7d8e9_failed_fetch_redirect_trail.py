"""Step 5B.1: keep the redirect trail when a fetch fails

FOUND BY THE FIRST REAL-WEB RUN
===============================
`redirect_chain` and `effective_url` lived only on `snapshot`, and a snapshot is
written only when bytes arrive. So a fetch that redirected and *then* failed recorded
its final HTTP status and lost every hop that led there.

The smoke run produced exactly that case. UBC's academic calendar redirects four times
-- `www.calendar.ubc.ca` to `calendar.ubc.ca` to `vancouver.calendar.ubc.ca/admissions`
-- and a connect on a later hop exceeded the timeout. The record said
`TIMEOUT, http_status 301` and nothing about where it had been going, which is the one
thing an operator needs to tell "the site moved" from "the site is down".

The chain is a fact about the **attempt**, not about the bytes: it describes what
happened on the wire, and that is `fetch_run`'s subject. Recording it on the snapshot
as well is not duplication -- a snapshot must be able to say which URL produced *these*
bytes without joining back through the run.

Both columns are nullable: most fetches redirect nowhere, and an empty array would be
a worse representation of "no redirect" than the absence of one.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# No import from app.* (C21/C22): frozen literals only.

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("fetch_run", sa.Column("effective_url", sa.Text(), nullable=True))
    op.add_column(
        "fetch_run",
        sa.Column(
            "redirect_chain",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("fetch_run", "redirect_chain")
    op.drop_column("fetch_run", "effective_url")
