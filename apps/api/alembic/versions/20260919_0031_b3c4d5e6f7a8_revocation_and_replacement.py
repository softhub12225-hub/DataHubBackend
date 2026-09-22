"""Step 5C.5: withdrawing trust stops future publication, and a dead URL can be replaced

THE GAP THIS CLOSES
===================
`source.publication_eligibility` is a **stored copy** of a decision made two tables away.
`app_source_eligibility_is_earned` checks, at the moment it is set, that a promoted
`source_mapping` on a verified `official_domain` vouches for it. Nothing re-checked it
afterwards.

So revoking a domain did not stop anything. Rejecting the host, deactivating it, or
superseding it left every source under it still reading `OFFICIAL_VERIFIED`, and C27's
`field_claim_requires_eligible_evidence` reads exactly that column -- so new claims would
have kept being publishable from a host that had just been declared not the
institution's. Section 17 requires the opposite, and it was the one part of the model
that had been deferred rather than built.

WITHDRAWAL IS NOT DELETION
==========================
The triggers below set dependent sources back to `NOT_ELIGIBLE`. They do not touch:

* `snapshot`, `extraction`, `fetch_run`, `content_blob` -- the evidence, which is
  append-only and stays exactly as captured;
* `field_claim`, `field_provenance`, `change_event` -- history, which `app_forbid_mutation`
  refuses to change at all;
* `official_domain` history -- a rejected row keeps its `rejected_reason`, a superseded
  one keeps `superseded_by_id`.

What stops is the *future*: the next `field_claim` citing that source is refused. Facts
already published remain, with their provenance intact and now visibly sourced from a
host whose verification was withdrawn, which is what an auditor needs to see. Rewriting
them would destroy the record of what was believed and when.

REPLACING A DEAD URL
====================
Section 18. 319 pilot sources include hosts that do not resolve and paths that 404, and
an official URL can be dead. When a reviewer supplies a replacement, the old source must
**not** be edited to point at the new URL: its snapshots were fetched from the old one,
and repointing it would make the evidence say it came from somewhere it did not.

`source.superseded_by_source_id` records the relationship instead. The old row keeps its
URL, its `url_hash` and every fetch it ever made; the new row is an ordinary new source
with its own acquisition history. The pair is navigable in both directions and neither
one lies about where its bytes came from.

A source may only be superseded once it is inactive, because a replaced source that is
still being fetched is two sources for one thing.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two statuses that carry publication trust. A host in any other state, or an
#: inactive one, vouches for nothing.
TRUSTED = "('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')"

WITHDRAW_FROM_DOMAIN = f"""
CREATE OR REPLACE FUNCTION app_withdraw_eligibility_on_domain_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
BEGIN
    -- Still trusted and still active: nothing to withdraw.
    IF NEW.verification_status::text IN {TRUSTED} AND NEW.is_active THEN
        RETURN NULL;
    END IF;

    UPDATE public.source s
       SET publication_eligibility = 'NOT_ELIGIBLE',
           eligibility_authorization_id = NULL,
           eligibility_set_at = now(),
           eligibility_reason = format(
               'withdrawn: official_domain %s is now %s (active=%s)',
               NEW.host, NEW.verification_status, NEW.is_active)
      FROM public.source_mapping sm
     WHERE sm.official_domain_id = NEW.id
       AND sm.promoted_source_id = s.id
       AND s.publication_eligibility <> 'NOT_ELIGIBLE';

    RETURN NULL;
END;
$fn$;
"""

WITHDRAW_FROM_MAPPING = f"""
CREATE OR REPLACE FUNCTION app_withdraw_eligibility_on_mapping_change()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
BEGIN
    IF NEW.verification_status::text IN {TRUSTED} AND NEW.is_active THEN
        RETURN NULL;
    END IF;

    UPDATE public.source s
       SET publication_eligibility = 'NOT_ELIGIBLE',
           eligibility_authorization_id = NULL,
           eligibility_set_at = now(),
           eligibility_reason = format(
               'withdrawn: source_mapping %s is now %s (active=%s)',
               NEW.id, NEW.verification_status, NEW.is_active)
     WHERE s.id = NEW.promoted_source_id
       AND s.publication_eligibility <> 'NOT_ELIGIBLE';

    RETURN NULL;
END;
$fn$;
"""


def upgrade() -> None:
    # --- replacement (section 18) --------------------------------------------------
    op.add_column(
        "source",
        sa.Column("superseded_by_source_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_source_superseded_by_source_id_source"),
        "source",
        "source",
        ["superseded_by_source_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_source_superseded_by_source_id"),
        "source",
        ["superseded_by_source_id"],
        unique=False,
    )
    op.create_check_constraint(
        op.f("ck_source_not_its_own_successor"),
        "source",
        "superseded_by_source_id IS NULL OR superseded_by_source_id <> id",
    )
    op.create_check_constraint(
        op.f("ck_source_superseded_source_is_inactive"),
        "source",
        "superseded_by_source_id IS NULL OR is_active = false",
    )
    op.execute(
        "COMMENT ON COLUMN source.superseded_by_source_id IS "
        "'The source that replaced this one, when a reviewer supplied a new URL for a "
        "dead page. The OLD row is never edited to point at the new URL: its snapshots "
        "were fetched from the old one, and repointing it would make the evidence say it "
        "came from somewhere it did not. Set only on an inactive source, because a "
        "replaced source that is still fetched is two sources for one thing.'"
    )

    # --- revocation (section 17) ---------------------------------------------------
    op.execute(WITHDRAW_FROM_DOMAIN)
    op.execute(WITHDRAW_FROM_MAPPING)
    op.execute(
        """
        CREATE TRIGGER official_domain_withdraws_eligibility
        AFTER UPDATE OF verification_status, is_active ON official_domain
        FOR EACH ROW
        WHEN (OLD.verification_status IS DISTINCT FROM NEW.verification_status
              OR OLD.is_active IS DISTINCT FROM NEW.is_active)
        EXECUTE FUNCTION app_withdraw_eligibility_on_domain_change();
        """
    )
    op.execute(
        """
        CREATE TRIGGER source_mapping_withdraws_eligibility
        AFTER UPDATE OF verification_status, is_active ON source_mapping
        FOR EACH ROW
        WHEN (OLD.verification_status IS DISTINCT FROM NEW.verification_status
              OR OLD.is_active IS DISTINCT FROM NEW.is_active)
        EXECUTE FUNCTION app_withdraw_eligibility_on_mapping_change();
        """
    )
    # ENABLE ALWAYS, for the same reason the C27 triggers are: a withdrawal that a
    # replication session could skip is not a withdrawal.
    for table, trigger in (
        ("official_domain", "official_domain_withdraws_eligibility"),
        ("source_mapping", "source_mapping_withdraws_eligibility"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS source_mapping_withdraws_eligibility ON source_mapping")
    op.execute("DROP TRIGGER IF EXISTS official_domain_withdraws_eligibility ON official_domain")
    op.execute("DROP FUNCTION IF EXISTS app_withdraw_eligibility_on_mapping_change()")
    op.execute("DROP FUNCTION IF EXISTS app_withdraw_eligibility_on_domain_change()")
    op.drop_constraint(op.f("ck_source_superseded_source_is_inactive"), "source", type_="check")
    op.drop_constraint(op.f("ck_source_not_its_own_successor"), "source", type_="check")
    op.drop_index(op.f("ix_source_superseded_by_source_id"), table_name="source")
    op.drop_constraint(
        op.f("fk_source_superseded_by_source_id_source"), "source", type_="foreignkey"
    )
    op.drop_column("source", "superseded_by_source_id")
