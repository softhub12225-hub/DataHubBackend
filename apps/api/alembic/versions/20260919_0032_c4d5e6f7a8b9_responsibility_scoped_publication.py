"""Step 5C.6: publication authority is scoped to a field, not to a source

THE HOLE
========
C27's gate was asymmetric and nobody had noticed, because the asymmetry is in the
*permissive* direction for the class every university page will hold.

    AUTHORIZED_RANKING   -> scoped to ranking entity types, and a live licence
    AUTHORIZED_EXTERNAL  -> scoped to an exact (entity_type, field_path) binding
    OFFICIAL_VERIFIED    -> source level only. No field check at all.

So once a page was eligible, *every* claim from it was eligible. The pilot makes that
concrete: 385 responsibility claims over 319 URLs, so one URL is submitted as both
`LANGUAGE_REQUIREMENTS` and `TUITION_FEES`. A reviewer verifying the language claim and
rejecting the tuition one has made two decisions, and a source-level gate honours only
the first -- the tuition claim would publish on the strength of the language
verification, from the same source, the same snapshot, the same extraction.

THE FIX IS SYMMETRY, NOT A NEW MECHANISM
========================================
`source_field_binding` already exists and already scopes `AUTHORIZED_EXTERNAL` to exact
fields. `OFFICIAL_VERIFIED` now requires the same thing. No new table, no second
authority system: the binding check simply stops being conditional on one class.

The consequence is that an official source publishes nothing until its bindings exist,
and bindings are written by promotion from the verified mapping's `source_category`
(`app.domains.verification.policy.PUBLICATION_BINDINGS`). Verification first, promotion
second, publication third -- each one a separate act.

AND A DATABASE INVARIANT FOR PROMOTION
======================================
Section 6 asks for defence in depth rather than trust in the service layer.
`ck_source_mapping_only_a_trusted_mapping_may_be_promoted` already refuses
`promoted_source_id` on an untrusted mapping. What it could not see is the *host*: a
mapping whose own status is `VERIFIED_OFFICIAL` under a domain that has since been
rejected or deactivated was still promotable. `source_mapping_promotion_requires_live_trust`
closes that, on INSERT and on UPDATE, for every writer including the owner.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c4d5e6f7a8b9"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The two statuses that carry publication trust. Restated here rather than imported
#: from revision b3c4d5e6f7a8: a migration that reaches into another migration's module
#: breaks the moment that file is squashed (C21/C22 -- frozen literals only).
TRUSTED = "('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')"

#: The binding check, applied to both official and authorised-external sources. Kept as
#: one block so the two classes cannot drift apart again.
SCOPED_GATE = """
CREATE OR REPLACE FUNCTION app_field_claim_evidence_is_eligible()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
DECLARE
    rec           record;
    v_found       boolean := false;
    v_snapshot_id uuid := NULL;
    v_claim_id    uuid := NULL;
BEGIN
    SELECT s.id INTO v_snapshot_id
      FROM public.extraction e
      JOIN public.snapshot s ON s.id = e.snapshot_id
     WHERE e.id = NEW.extraction_id;

    IF v_snapshot_id IS NULL THEN
        RAISE EXCEPTION 'field_claim cites extraction % with no snapshot', NEW.extraction_id
            USING ERRCODE = 'restrict_violation';
    END IF;

    FOR rec IN SELECT * FROM app_eligibility_of_evidence(NULL::uuid, v_snapshot_id, v_claim_id)
    LOOP
        v_found := true;

        IF rec.eligibility NOT IN ('OFFICIAL_VERIFIED', 'AUTHORIZED_EXTERNAL', 'AUTHORIZED_RANKING') THEN
            RAISE EXCEPTION
                'field_claim: source % is %, which may not support a published fact',
                rec.source_id, rec.eligibility
                USING ERRCODE = 'restrict_violation',
                      HINT = 'A TARGET_SCOPE_ONLY source (the client target list, QS '
                             'name/rank/score) establishes project scope only. '
                             'Classify an official source and cite that instead.';
        END IF;

        IF rec.eligibility = 'AUTHORIZED_RANKING'
           AND NEW.entity_type NOT IN ('ranking_entry', 'ranking_edition', 'ranking_publisher') THEN
            RAISE EXCEPTION
                'field_claim: source % is AUTHORIZED_RANKING and cannot support %.%',
                rec.source_id, NEW.entity_type, NEW.field_path
                USING ERRCODE = 'restrict_violation',
                      HINT = 'A ranking licence covers ranking data. It does not make '
                             'the publisher authoritative for a university name, a '
                             'programme or a fee.';
        END IF;

        IF NEW.entity_type IN ('ranking_entry', 'ranking_edition', 'ranking_publisher')
           AND rec.eligibility <> 'AUTHORIZED_RANKING' THEN
            RAISE EXCEPTION
                'field_claim: % is a ranking fact, so its source must be '
                'AUTHORIZED_RANKING, not %',
                NEW.entity_type, rec.eligibility
                USING ERRCODE = 'restrict_violation',
                      HINT = 'Ranking publication requires a confirmed licence (D8/U9).';
        END IF;

        IF rec.eligibility = 'AUTHORIZED_RANKING' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.source_authorization sa
                 WHERE sa.id = rec.authorization_id
                   AND sa.display_allowed
                   AND (sa.expires_at IS NULL OR sa.expires_at >= current_date)
            ) THEN
                RAISE EXCEPTION
                    'field_claim: source % has no live display-allowed authorisation',
                    rec.source_id
                    USING ERRCODE = 'restrict_violation',
                          HINT = 'Record the licence on source_authorization with '
                                 'display_allowed, and keep expires_at in the future.';
            END IF;
        END IF;

        -- Step 5C.6. Authority is scoped to a FIELD, for an official source exactly as
        -- for an authorised third party. Before this, a page verified as the language
        -- requirements page could publish a fee: the two decisions a reviewer made
        -- about one URL collapsed into one.
        IF rec.eligibility IN ('OFFICIAL_VERIFIED', 'AUTHORIZED_EXTERNAL') THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.source_field_binding sfb
                 WHERE sfb.source_id = rec.source_id
                   AND sfb.entity_type = NEW.entity_type
                   AND sfb.field_path = NEW.field_path
            ) THEN
                RAISE EXCEPTION
                    'field_claim: source % is % but is not authorised for %.%',
                    rec.source_id, rec.eligibility, NEW.entity_type, NEW.field_path
                    USING ERRCODE = 'restrict_violation',
                          HINT = 'Publication authority is scoped to a field. Promote the '
                                 'verified source_mapping whose responsibility covers this '
                                 'field; that writes the source_field_binding rows. An '
                                 'eligibility with no scope permits nothing.';
            END IF;
        END IF;
    END LOOP;

    IF NOT v_found THEN
        RAISE EXCEPTION 'field_claim resolves to no source' USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$fn$;
"""

PROMOTION_INVARIANT = """
CREATE OR REPLACE FUNCTION app_promotion_requires_live_trust()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
DECLARE
    v_domain record;
BEGIN
    IF NEW.promoted_source_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Only when a promotion is being ESTABLISHED. Retiring an already-promoted mapping
    -- is not an act of promotion: `is_active = false` is how a page is withdrawn, and
    -- `source_mapping_withdraws_eligibility` takes the eligibility away for it. Firing
    -- here on that update would make withdrawal impossible, which is the opposite of
    -- what the invariant is for.
    IF TG_OP = 'UPDATE' AND NEW.promoted_source_id IS NOT DISTINCT FROM OLD.promoted_source_id THEN
        RETURN NEW;
    END IF;

    IF NOT NEW.is_active THEN
        RAISE EXCEPTION 'source_mapping %: an inactive mapping may not be promoted', NEW.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.official_domain_id IS NULL THEN
        RAISE EXCEPTION
            'source_mapping %: promotion requires a verified official_domain, and this '
            'mapping names none', NEW.id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Verify the host first. Promotion follows trust; it does not '
                         'create it.';
    END IF;

    SELECT verification_status::text AS status, is_active, host
      INTO v_domain
      FROM public.official_domain
     WHERE id = NEW.official_domain_id
       FOR SHARE;

    IF v_domain.status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')
       OR NOT v_domain.is_active THEN
        RAISE EXCEPTION
            'source_mapping %: host % is % (active=%), so nothing may be promoted under it',
            NEW.id, v_domain.host, v_domain.status, v_domain.is_active
            USING ERRCODE = 'restrict_violation',
                  HINT = 'A revoked or superseded host authorises nothing, including a '
                         'mapping whose own status still reads verified.';
    END IF;

    RETURN NEW;
END;
$fn$;
"""


#: Revoking a PROMOTED mapping was impossible.
#: `ck_source_mapping_only_a_trusted_mapping_may_be_promoted` refuses a
#: `promoted_source_id` on anything not `VERIFIED_OFFICIAL`/`AUTHORIZED_EXTERNAL`, so the
#: single `UPDATE` that rejects a mapping violated it -- the reviewer had to know to clear
#: the promotion in the same statement, and the withdrawal trigger Step 5C.5 added could
#: never fire because the update never landed. Losing trust now clears the promotion as
#: part of the same change, which is what makes revocation a single act.
#:
#: The withdrawal trigger is redefined with it, because it has to read
#: OLD.promoted_source_id: by the time it runs, NEW has been cleared by this one.
UNPROMOTE_ON_LOSS_OF_TRUST = f"""
CREATE OR REPLACE FUNCTION app_unpromote_on_loss_of_trust()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp
AS $fn$
BEGIN
    IF NEW.verification_status::text IN {TRUSTED} AND NEW.is_active THEN
        RETURN NEW;
    END IF;
    NEW.promoted_source_id := NULL;
    RETURN NEW;
END;
$fn$;
"""

WITHDRAW_FROM_MAPPING_V2 = f"""
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

    -- OLD, not NEW: `app_unpromote_on_loss_of_trust` has already cleared NEW's
    -- promotion by the time this runs, and the source it pointed at is exactly the
    -- source whose eligibility has to go.
    UPDATE public.source s
       SET publication_eligibility = 'NOT_ELIGIBLE',
           eligibility_authorization_id = NULL,
           eligibility_set_at = now(),
           eligibility_reason = format(
               'withdrawn: source_mapping %s is now %s (active=%s)',
               NEW.id, NEW.verification_status, NEW.is_active)
     WHERE s.id = COALESCE(OLD.promoted_source_id, NEW.promoted_source_id)
       AND s.publication_eligibility <> 'NOT_ELIGIBLE';

    RETURN NULL;
END;
$fn$;
"""


def upgrade() -> None:
    op.execute(SCOPED_GATE)
    op.execute(PROMOTION_INVARIANT)
    op.execute(UNPROMOTE_ON_LOSS_OF_TRUST)
    op.execute(WITHDRAW_FROM_MAPPING_V2)
    op.execute(
        """
        CREATE TRIGGER source_mapping_unpromotes_on_loss_of_trust
        BEFORE UPDATE OF verification_status, is_active ON source_mapping
        FOR EACH ROW
        WHEN (OLD.verification_status IS DISTINCT FROM NEW.verification_status
              OR OLD.is_active IS DISTINCT FROM NEW.is_active)
        EXECUTE FUNCTION app_unpromote_on_loss_of_trust();
        """
    )
    op.execute(
        "ALTER TABLE source_mapping ENABLE ALWAYS TRIGGER "
        "source_mapping_unpromotes_on_loss_of_trust"
    )
    op.execute(
        """
        CREATE TRIGGER source_mapping_promotion_requires_live_trust
        BEFORE INSERT OR UPDATE OF promoted_source_id
        ON source_mapping
        FOR EACH ROW
        EXECUTE FUNCTION app_promotion_requires_live_trust();
        """
    )
    op.execute(
        "ALTER TABLE source_mapping ENABLE ALWAYS TRIGGER "
        "source_mapping_promotion_requires_live_trust"
    )
    op.execute(
        "COMMENT ON FUNCTION app_field_claim_evidence_is_eligible() IS "
        "'C27. Refuses a field_claim whose evidence chain does not reach a source with a "
        "publication class, and -- since Step 5C.6 -- refuses one whose source is not "
        "authorised for that exact (entity_type, field_path). The scope check applies to "
        "OFFICIAL_VERIFIED as well as AUTHORIZED_EXTERNAL: 385 responsibility claims over "
        "319 URLs means one page can be verified for one thing and rejected for another, "
        "and a source-level gate honours only the first decision.'"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS source_mapping_unpromotes_on_loss_of_trust ON source_mapping"
    )
    op.execute("DROP FUNCTION IF EXISTS app_unpromote_on_loss_of_trust()")
    op.execute(
        "DROP TRIGGER IF EXISTS source_mapping_promotion_requires_live_trust ON source_mapping"
    )
    op.execute("DROP FUNCTION IF EXISTS app_promotion_requires_live_trust()")
    # The previous revision's gate is restored by its own definition; re-stating the
    # unscoped version here would leave two copies of C27 to drift apart.
