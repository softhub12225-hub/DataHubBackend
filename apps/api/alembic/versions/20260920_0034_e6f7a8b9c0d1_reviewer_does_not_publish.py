"""Step 5C.7B: the reviewer role no longer carries proposal:publish

THE CONFLICT
============
`role.reviewer` was seeded with `proposal:publish`, and its description reads "Reviews,
returns, corrects and publishes". That is not an oversight -- the publication
transaction (ARCHITECTURE §7 step 1) confirms `actor` holds `proposal:publish` and then
re-validates segregation of duties *per proposal*: a manually created proposal may not
be published by its creator, and a high-risk item corrected by the actor needs a second
review round with a different corrector (C2/D7).

So the architecture's separation is **"you may not publish your own work"**, enforced at
publication time, not **"reviewers do not publish"**.

WHY THE PERMISSION IS REMOVED ANYWAY
====================================
Step 5C.7B section 13 asks for the permission to be removed unless an existing invariant
proves it does not authorise publication. The invariant above does the opposite: it
confirms the permission *is* the publication authorisation, gated by a same-actor check.
That is a weaker separation than one role granting review and another granting
publication, and the step prefers the stronger one.

Section 13 also says where it should go instead: an existing distinct application
publisher role, or -- if none exists -- nowhere. There is none. The six roles are
`admin`, `consultant_readonly`, `data_editor`, `ops`, `reviewer` and `service_client`,
and `admin` does not hold `proposal:publish` either. So the permission is left assigned
to **no role**.

WHAT THIS COSTS, STATED PLAINLY
===============================
Nobody can publish. That is currently free: `change_proposal` has no rows, the
publication transaction is not implemented, and no code reads this permission. It stops
being free the moment publication is built, and at that point somebody has to decide
which role holds it -- which is the decision this defers rather than makes. The
permission row itself is kept, so the answer is a single `role_permission` insert.

The database role `app_publisher` is untouched and remains the only role able to write
canonical tables. It is a different axis and no application permission widens it.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-09-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION = "proposal:publish"
ROLE = "reviewer"

#: The description is corrected with the grant, so the role does not go on claiming a
#: capability it no longer has. A description that outlives its permission is how the
#: next reader concludes the grant was removed by mistake.
NEW_DESCRIPTION = (
    "Reviews, returns and corrects. Verifies official sources and their "
    "responsibilities. Does NOT publish: proposal:publish is deliberately assigned to "
    "no role until the publication transaction exists and an owner is chosen "
    "(Step 5C.7B). Cannot approve own edits, nor own high-risk corrections (D7)."
)
OLD_DESCRIPTION = (
    "Reviews, returns, corrects and publishes. Cannot approve own edits, nor own "
    "high-risk corrections (D7)."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM role_permission WHERE role_code = :role AND permission_code = :perm"
        ).bindparams(role=ROLE, perm=PERMISSION)
    )
    op.execute(
        sa.text(
            "UPDATE role SET description = :d, updated_at = now() WHERE code = :role"
        ).bindparams(d=NEW_DESCRIPTION, role=ROLE)
    )
    # The permission itself stays. Deleting it would make re-assigning it a migration
    # that invents a permission, rather than one that makes a choice.
    op.execute(
        sa.text(
            "UPDATE permission SET description = :d, updated_at = now() WHERE code = :perm"
        ).bindparams(
            d=(
                "Execute the publication transaction. Held by NO role as of Step 5C.7B: "
                "review and publication are separate authorities, and the owner is "
                "chosen when the publication transaction is built."
            ),
            perm=PERMISSION,
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO role_permission (role_code, permission_code) "
            "VALUES (:role, :perm) ON CONFLICT DO NOTHING"
        ).bindparams(role=ROLE, perm=PERMISSION)
    )
    op.execute(
        sa.text(
            "UPDATE role SET description = :d, updated_at = now() WHERE code = :role"
        ).bindparams(d=OLD_DESCRIPTION, role=ROLE)
    )
