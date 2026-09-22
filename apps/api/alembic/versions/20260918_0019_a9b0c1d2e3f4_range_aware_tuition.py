"""U14: range-aware tuition

WHY THE COLUMN IS REPLACED RATHER THAN EXTENDED
===============================================
``tuition.amount`` was a single ``numeric`` bound to ``amount_field_status`` by a
biconditional. That shape cannot hold what universities actually publish:

    "GBP 28,000-32,000 depending on pathway"
    "from GBP 24,500"
    "fees vary by module selection"

All three are *published* fees, and none of them is a scalar. Every way of forcing
them into one column lies: pick an endpoint and an invented figure gets published as
exact; set ``OFFICIALLY_NOT_PUBLISHED`` and we deny a page that plainly does publish;
leave ``NOT_CHECKED`` and we discard work the collector actually did.

So the *shape* of the amount becomes data. ``amount_kind`` records what the page
published and ``amount_min``/``amount_max`` carry exactly the endpoints it gave --
no more. A RANGE stores two numbers and **no midpoint is ever derived**: a fee of
28,000-32,000 is not 30,000, and a consultant quoting 30,000 would be quoting a
figure no university published. Averaging is a presentation choice for a query layer
that shows its working, never a stored value.

``amount_kind`` is not a field status. ``OFFICIALLY_NOT_PUBLISHED`` still means the
page says nothing about fees; ``VARIABLE`` means the opposite -- the page addresses
fees, just not numerically -- and then ``official_text`` is mandatory, because the
wording is the entire fact.

``tuition`` holds **zero rows**, verified before writing this, so the old column is
dropped outright rather than carried as legacy. There is no data to migrate and no
reader to break, and a nullable ``amount`` left in place would be filled in by
someone eventually.

THE PART THAT IS EASY TO MISS
=============================
C27 installed ``tuition_governed_fields_need_provenance`` over the column list
``('amount')``, frozen into revision 0017 as a literal. Dropping the column does not
update that trigger -- ``TG_ARGV`` still says ``amount``, ``to_jsonb(NEW) -> 'amount'``
becomes SQL NULL, and the loop's "a NULL column asserts nothing" branch skips it. The
trigger would survive the migration, fire on every write, and check nothing. Tuition
would silently leave the governed set.

So the trigger is dropped and recreated here over
``(amount_kind, amount_min, amount_max)``, and a test asserts the live ``TG_ARGV``
matches. A frozen literal in an old revision is correct history; it is not a current
description, and something has to notice when the two diverge.

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# No import from app.* (C21/C22): frozen literals only.

revision: str = "a9b0c1d2e3f4"
down_revision: str | None = "f8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TUITION_AMOUNT_KIND_VALUES: tuple[str, ...] = (
    "EXACT",
    "RANGE",
    "FROM",
    "UP_TO",
    "VARIABLE",
)

#: What replaces ``('amount')`` in the C27 governed set for this table.
GOVERNED_TUITION_COLUMNS: tuple[str, ...] = ("amount_kind", "amount_min", "amount_max")

#: (name, expression). Named without the ``ck_tuition_`` prefix that SQLAlchemy's
#: naming convention adds, so the constraint names match what the model produces.
NEW_CHECKS: tuple[tuple[str, str], ...] = (
    # A published fee states a shape; an unpublished one states nothing.
    (
        "amount_kind_matches_field_status",
        "(amount_field_status = 'PUBLISHED') = (amount_kind IS NOT NULL)",
    ),
    (
        "no_amounts_without_a_kind",
        "amount_kind IS NOT NULL OR (amount_min IS NULL AND amount_max IS NULL)",
    ),
    (
        "exact_has_equal_endpoints",
        "amount_kind <> 'EXACT' OR (amount_min IS NOT NULL AND amount_max IS NOT NULL "
        "AND amount_min = amount_max)",
    ),
    (
        "range_is_ordered",
        "amount_kind <> 'RANGE' OR (amount_min IS NOT NULL AND amount_max IS NOT NULL "
        "AND amount_min <= amount_max)",
    ),
    (
        "from_has_only_a_minimum",
        "amount_kind <> 'FROM' OR (amount_min IS NOT NULL AND amount_max IS NULL)",
    ),
    (
        "up_to_has_only_a_maximum",
        "amount_kind <> 'UP_TO' OR (amount_min IS NULL AND amount_max IS NOT NULL)",
    ),
    # VARIABLE may carry no figure at all, so the wording is the fact.
    (
        "variable_states_its_wording",
        "amount_kind <> 'VARIABLE' OR btrim(coalesce(official_text, '')) <> ''",
    ),
    # The rule that makes a bare number unrepresentable.
    (
        "amounts_require_currency_and_billing_unit",
        "(amount_min IS NULL AND amount_max IS NULL) "
        "OR (currency_code IS NOT NULL AND billing_unit_code IS NOT NULL)",
    ),
    (
        "amounts_non_negative",
        "(amount_min IS NULL OR amount_min >= 0) AND (amount_max IS NULL OR amount_max >= 0)",
    ),
)

OLD_CHECKS: tuple[tuple[str, str], ...] = (
    ("amount_matches_field_status", "(amount_field_status = 'PUBLISHED') = (amount IS NOT NULL)"),
    ("amount_non_negative", "amount IS NULL OR amount >= 0"),
    (
        "amount_requires_currency_and_billing_unit",
        "amount IS NULL OR (currency_code IS NOT NULL AND billing_unit_code IS NOT NULL)",
    ),
)


def _install_governed_trigger(columns: Sequence[str]) -> None:
    """Recreate the C27 constraint trigger over an explicit column list."""
    args = ", ".join("'" + column + "'" for column in columns)
    op.execute(
        "CREATE CONSTRAINT TRIGGER tuition_governed_fields_need_provenance "
        "AFTER INSERT OR UPDATE ON tuition "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION app_governed_column_requires_provenance(" + args + ")"
    )
    # ENABLE ALWAYS: the trigger must fire in a replica session too, matching C27.
    op.execute("ALTER TABLE tuition ENABLE ALWAYS TRIGGER tuition_governed_fields_need_provenance")


def upgrade() -> None:
    values = ", ".join("'" + value + "'" for value in TUITION_AMOUNT_KIND_VALUES)
    op.execute("CREATE TYPE tuition_amount_kind AS ENUM (" + values + ")")

    # The trigger names the column being dropped, so it goes first and comes back
    # last. Dropping the column would otherwise leave it checking a field that no
    # longer exists -- silently, because the function skips absent keys.
    op.execute("DROP TRIGGER tuition_governed_fields_need_provenance ON tuition")

    for name, _ in OLD_CHECKS:
        op.execute("ALTER TABLE tuition DROP CONSTRAINT ck_tuition_" + name)
    op.execute("DROP INDEX ix_tuition_currency_code_amount")
    op.execute("ALTER TABLE tuition DROP COLUMN amount")

    op.execute(
        "ALTER TABLE tuition "
        "  ADD COLUMN amount_kind tuition_amount_kind, "
        "  ADD COLUMN amount_min numeric(14, 2), "
        "  ADD COLUMN amount_max numeric(14, 2)"
    )
    op.execute(
        "COMMENT ON COLUMN tuition.amount_kind IS "
        "'What shape the official page published the fee in (U14)'"
    )
    op.execute(
        "COMMENT ON TABLE tuition IS "
        "'Fee per offering x academic year x student category. Ranges are U14.'"
    )

    for name, expression in NEW_CHECKS:
        op.execute(
            "ALTER TABLE tuition ADD CONSTRAINT ck_tuition_" + name + " CHECK (" + expression + ")"
        )

    op.execute(
        "CREATE INDEX ix_tuition_currency_code_amounts "
        "ON tuition (currency_code, amount_min, amount_max)"
    )

    _install_governed_trigger(GOVERNED_TUITION_COLUMNS)


def downgrade() -> None:
    op.execute("DROP TRIGGER tuition_governed_fields_need_provenance ON tuition")

    for name, _ in NEW_CHECKS:
        op.execute("ALTER TABLE tuition DROP CONSTRAINT ck_tuition_" + name)
    op.execute("DROP INDEX ix_tuition_currency_code_amounts")
    op.execute(
        "ALTER TABLE tuition "
        "  DROP COLUMN amount_kind, DROP COLUMN amount_min, DROP COLUMN amount_max"
    )
    op.execute("DROP TYPE tuition_amount_kind")

    op.execute("ALTER TABLE tuition ADD COLUMN amount numeric(14, 2)")
    for name, expression in OLD_CHECKS:
        op.execute(
            "ALTER TABLE tuition ADD CONSTRAINT ck_tuition_" + name + " CHECK (" + expression + ")"
        )
    op.execute("CREATE INDEX ix_tuition_currency_code_amount ON tuition (currency_code, amount)")

    _install_governed_trigger(("amount",))
