"""Step 5C.6: one permission for declaring a source official

WHY NOT `source:manage`
=======================
`source:manage` reads "Register and configure sources and field bindings" and is held by
`data_editor` as well as `admin`. Registering a URL and declaring that URL officially the
institution's are different acts: the first is data entry and the second is the thing the
whole C27 boundary rests on. A data editor who can add a source must not thereby be able
to make it publishable.

WHY NOT A NEW ROLE
==================
Section 12 forbids it, and there is no need: `role.reviewer` already exists with
`is_reviewer_role = true` and the description "Reviews, returns, corrects and publishes".
Verification is review. It gets a permission, not a second role that would have to be
kept in step with the first.

`admin` also receives it, because `admin` already carries `source:manage` and every other
configuration permission; withholding this one would mean an administrator could
configure the trust system but not use it.

WHAT THIS DOES NOT CHANGE
=========================
Nothing about the database roles. `app_api`, `app_worker` and `app_publisher` are a
different axis -- they are who the *process* connects as, and no application permission
widens them. The privilege suite still governs that boundary.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION = "source:verify"
DETAIL = (
    "Record a human verification decision about a source: whether a host is officially "
    "the institution's, and whether a page carries a claimed responsibility. Separate "
    "from source:manage, which registers and configures sources -- adding a URL must "
    "not confer the power to declare it official."
)
#: Who gets it. `reviewer` because verification is review; `admin` because it already
#: holds every other configuration permission.
ROLES = ("reviewer", "admin")


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO permission (code, description) VALUES (:code, :detail) "
            "ON CONFLICT (code) DO UPDATE SET description = EXCLUDED.description"
        ).bindparams(code=PERMISSION, detail=DETAIL)
    )
    for role in ROLES:
        op.execute(
            sa.text(
                "INSERT INTO role_permission (role_code, permission_code) "
                "SELECT :role, :permission "
                " WHERE EXISTS (SELECT 1 FROM role WHERE code = :role) "
                "ON CONFLICT DO NOTHING"
            ).bindparams(role=role, permission=PERMISSION)
        )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM role_permission WHERE permission_code = :code").bindparams(
            code=PERMISSION
        )
    )
    op.execute(sa.text("DELETE FROM permission WHERE code = :code").bindparams(code=PERMISSION))
