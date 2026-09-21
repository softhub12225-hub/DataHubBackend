"""Step 4 hardening: publication eligibility (C27)

WHY THIS REVISION EXISTS
========================
Step 4 claimed that database role separation prevented client-spreadsheet and QS
values from becoming published university facts. **The claim was wrong, and the
client caught it.**

Role separation stops ``app_api`` and ``app_worker`` writing canonical tables at all.
It does not stop ``app_publisher``, which holds ``SELECT`` on the onboarding tables
*and* ``INSERT``/``UPDATE`` on the canonical ones. Verified against a live database
before writing this revision:

* ``app_publisher`` could ``SELECT qs_name FROM target_list_entry``            -- ALLOWED
* ``app_publisher`` could ``INSERT INTO university`` with no provenance at all -- ALLOWED
* ``app_publisher`` could ``INSERT INTO field_provenance`` with source_id,
  snapshot_id and claim_id all NULL                                           -- ALLOWED

So a publication service with a bug could read a QS name and publish it, and the
grant matrix would not notice. Grants classify *tables*; this rule is about a value's
*origin*, which no grant can express.

WHAT THIS REVISION ADDS
=======================
An explicit eligibility class on every source, and checks that bite where evidence is
cited and where canonical values are written.

1. ``publication_eligibility`` enum, and ``source.publication_eligibility`` defaulting
   to ``NOT_ELIGIBLE`` -- a source substantiates nothing until classified.
2. ``source_mapping.publication_eligibility``, **generated** from
   ``verification_status`` and ``source_category``. PostgreSQL refuses to write a
   generated column for every role including the owner, so this link in the chain is
   not merely constrained but unassertable.
3. ``source_eligibility_is_earned``: a class above ``NOT_ELIGIBLE`` requires an active
   promoted mapping whose derived class matches and whose ``url_sha256`` equals the
   source's ``url_hash``. This is what stops ``app_api`` simply labelling the QS
   workbook ``OFFICIAL_VERIFIED``, and it closes the swap where a classified source is
   later repointed at another URL.
4. ``field_provenance_requires_eligible_evidence`` and
   ``field_claim_requires_eligible_evidence``: every anchor resolves to exactly one
   source -- ``snapshot.source_id`` is NOT NULL and
   ``claim -> extraction -> snapshot -> source`` is NOT NULL end to end -- and that
   source's class must permit the fact. ``TARGET_SCOPE_ONLY`` and ``NOT_ELIGIBLE`` are
   refused outright; ``AUTHORIZED_RANKING`` only for ranking entity types, and only
   with a live display-allowed authorisation; ``AUTHORIZED_EXTERNAL`` only where a
   ``source_field_binding`` row covers the exact field.
5. ``*_governed_fields_need_provenance``: four DEFERRABLE constraint triggers
   requiring that eight named canonical columns match a ``PUBLISHED``
   ``field_provenance`` row before the transaction commits. This is the one that
   closes the client's literal scenario -- ``UPDATE university SET name_en = '<qs
   string>'`` now fails unless an eligible source was already cited for that exact
   value.
6. ``app_publisher`` loses ``SELECT`` on the onboarding plane, on
   ``target_source_coverage`` (a plain view, so revoking its base tables alone would
   still leave ``qs_name`` readable through it) and on ``audit_log``, where it needs
   only ``INSERT``.

Triggers are genuine enforcement here, not advisory: ``app_publisher`` owns no table,
so ``ALTER TABLE ... DISABLE TRIGGER`` fails with "must be owner", and it cannot
``SET session_replication_role``. Both were verified, not assumed. The new triggers
are additionally ``ENABLE ALWAYS`` so they fire even in a replica session.

WHAT IT STILL DOES NOT DO -- READ THIS BEFORE TRUSTING IT
=========================================================
**Value fidelity is not enforced and cannot be.** Nothing in a database can tell
where a *string* came from. A publication service that reads ``qs_name``, writes it to
``university.name_en`` and cites a genuinely official source for that exact string
produces a row indistinguishable from an honest one. The governed-column triggers
require *a* citation; they cannot know the citation is truthful.

The mitigations are: the publisher can no longer read the onboarding tables at all, so
it must be given the value from elsewhere; and the Step 9 publication transaction must
compare each value it publishes against the cited claim's ``value_normalized`` rather
than trusting its own inputs. That comparison is the real defence against
hand-copying, and it belongs in the publication service, which this revision
deliberately does not build.

**Coverage is eight columns, not two hundred.** The governed-column set is frozen and
small: ``university.name_en``/``name_zh``, ``entity_alias.value``,
``ranking_entry.rank_value``/``rank_low``/``rank_high``/``score``, ``tuition.amount``.
Every other canonical column can still be written without provenance. Extending the
set is cheap -- add to ``GOVERNED_CANONICAL_COLUMNS`` and install one more trigger --
but the honest statement today is that provenance is mandatory for the eight values
most likely to carry a laundered string, not for the whole canonical plane.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# No import from app.* (C21/C22). Every list below is a FROZEN LITERAL: three
# previous revisions read live application state and broke fresh-database builds
# while already-upgraded databases stayed green.

revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PUBLICATION_ELIGIBILITY_VALUES: tuple[str, ...] = (
    "TARGET_SCOPE_ONLY",
    "OFFICIAL_VERIFIED",
    "AUTHORIZED_EXTERNAL",
    "AUTHORIZED_RANKING",
    "NOT_ELIGIBLE",
)

#: Classes that may support a published fact at all.
EVIDENCE_ELIGIBLE: tuple[str, ...] = (
    "OFFICIAL_VERIFIED",
    "AUTHORIZED_EXTERNAL",
    "AUTHORIZED_RANKING",
)

#: Entity types that constitute ranking facts. An AUTHORIZED_RANKING source may
#: substantiate these and nothing else; every other class is refused *for* these.
RANKING_ENTITY_TYPES: tuple[str, ...] = (
    "ranking_entry",
    "ranking_edition",
    "ranking_publisher",
)

#: The four values `source_mapping.publication_eligibility` can derive to.
MAPPING_ELIGIBILITY_VALUES: tuple[str, ...] = (
    "NOT_ELIGIBLE",
    "OFFICIAL_VERIFIED",
    "AUTHORIZED_EXTERNAL",
    "AUTHORIZED_RANKING",
)

#: Canonical columns that may not be written without matching provenance.
#: Deliberately small and frozen -- see the module docstring on coverage.
GOVERNED_CANONICAL_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("university", ("name_en", "name_zh")),
    ("entity_alias", ("value",)),
    ("ranking_entry", ("rank_value", "rank_low", "rank_high", "score")),
    ("tuition", ("amount",)),
)

#: What `app_publisher` loses SELECT on. It has no legitimate need for any of it: the
#: publication service publishes reviewed claims, and never needs to read the client's
#: scope list. Removing the read means the hand-copy attack requires a second
#: credential rather than one.
ONBOARDING_READ_REVOKE: tuple[str, ...] = (
    "target_list",
    "target_list_entry",
    "target_institution",
    "target_list_diff",
    "official_domain",
    "source_mapping",
    "source_degree_scope",
    "source_discipline_scope",
    "target_source_coverage",
)


def _quoted(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _roles_exist_guard(body: str) -> str:
    """Skip role DDL where the runtime roles were never created (as revision 0013)."""
    return f"""
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api')
           AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker')
           AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
{body}
        END IF;
    END
    $$;
    """


def _as_plpgsql(statements: Sequence[str]) -> str:
    return "\n".join(
        f"            EXECUTE '{statement.replace(chr(39), chr(39) * 2)}';"
        for statement in statements
    )


# ---------------------------------------------------------------------------
# Resolving a source from any evidence anchor
#
# Every anchor terminates at exactly one source row, and the chain is NOT NULL the
# whole way: snapshot.source_id, extraction.snapshot_id and field_claim.extraction_id
# are all NOT NULL (verified). So there is no anchor from which a source cannot be
# resolved, which is what makes the gate complete rather than best-effort.
# ---------------------------------------------------------------------------

RESOLVE_FN = """
CREATE OR REPLACE FUNCTION app_eligibility_of_evidence(
    p_source_id   uuid,
    p_snapshot_id uuid,
    p_claim_id    uuid
) RETURNS TABLE (source_id uuid, eligibility text, authorization_id uuid) AS $$
BEGIN
    RETURN QUERY
    WITH anchored AS (
        SELECT p_source_id AS sid WHERE p_source_id IS NOT NULL
        UNION
        SELECT s.source_id FROM public.snapshot s WHERE s.id = p_snapshot_id
        UNION
        SELECT s.source_id
          FROM public.field_claim fc
          JOIN public.extraction e ON e.id = fc.extraction_id
          JOIN public.snapshot s   ON s.id = e.snapshot_id
         WHERE fc.id = p_claim_id
    )
    SELECT src.id,
           src.publication_eligibility::text,
           src.eligibility_authorization_id
      FROM anchored a
      JOIN public.source src ON src.id = a.sid;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
"""


def _eligibility_check_body(
    *, entity_type_expr: str, field_path_expr: str, source_col: str, label: str
) -> str:
    """The class rules, shared verbatim by the provenance and claim triggers.

    Written once because two copies of an authorisation rule is one copy too many:
    the worker-side and publisher-side gates must agree exactly or the weaker one
    becomes the real policy.
    """
    return f"""
    -- One coherent citation, then one source, then the class rules.
    FOR rec IN SELECT * FROM app_eligibility_of_evidence({source_col}, v_snapshot_id, v_claim_id)
    LOOP
        v_found := true;

        IF rec.eligibility NOT IN ({_quoted(EVIDENCE_ELIGIBLE)}) THEN
            RAISE EXCEPTION
                '{label}: source % is %, which may not support a published fact',
                rec.source_id, rec.eligibility
                USING ERRCODE = 'restrict_violation',
                      HINT = 'A TARGET_SCOPE_ONLY source (the client target list, QS '
                             'name/rank/score) establishes project scope only. '
                             'Classify an official source and cite that instead.';
        END IF;

        -- AUTHORIZED_RANKING is for ranking facts and nothing else...
        IF rec.eligibility = 'AUTHORIZED_RANKING'
           AND {entity_type_expr} NOT IN ({_quoted(RANKING_ENTITY_TYPES)}) THEN
            RAISE EXCEPTION
                '{label}: source % is AUTHORIZED_RANKING and cannot support %.%',
                rec.source_id, {entity_type_expr}, {field_path_expr}
                USING ERRCODE = 'restrict_violation',
                      HINT = 'A ranking licence covers ranking data. It does not make '
                             'the publisher authoritative for a university name, a '
                             'programme or a fee.';
        END IF;

        -- ...and conversely a ranking fact needs a ranking authorisation.
        IF {entity_type_expr} IN ({_quoted(RANKING_ENTITY_TYPES)})
           AND rec.eligibility <> 'AUTHORIZED_RANKING' THEN
            RAISE EXCEPTION
                '{label}: % is a ranking fact, so its source must be '
                'AUTHORIZED_RANKING, not %',
                {entity_type_expr}, rec.eligibility
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
                    '{label}: source % has no live display-allowed authorisation',
                    rec.source_id
                    USING ERRCODE = 'restrict_violation',
                          HINT = 'Record the licence on source_authorization with '
                                 'display_allowed, and keep expires_at in the future.';
            END IF;
        END IF;

        -- An authorised third party is authorised for something specific. Scope is
        -- the existing 字段归责 matrix, reused rather than duplicated.
        IF rec.eligibility = 'AUTHORIZED_EXTERNAL' THEN
            IF NOT EXISTS (
                SELECT 1 FROM public.source_field_binding sfb
                 WHERE sfb.source_id = rec.source_id
                   AND sfb.entity_type = {entity_type_expr}
                   AND sfb.field_path = {field_path_expr}
            ) THEN
                RAISE EXCEPTION
                    '{label}: source % is AUTHORIZED_EXTERNAL but is not authorised '
                    'for %.%',
                    rec.source_id, {entity_type_expr}, {field_path_expr}
                    USING ERRCODE = 'restrict_violation',
                          HINT = 'Add a source_field_binding row for this exact field. '
                                 'An authorisation with no scope permits nothing.';
            END IF;
        END IF;
    END LOOP;
    """


PROVENANCE_FN = f"""
CREATE OR REPLACE FUNCTION app_field_provenance_evidence_is_eligible() RETURNS trigger AS $$
DECLARE
    rec           record;
    v_found       boolean := false;
    v_snapshot_id uuid := NEW.snapshot_id;
    v_claim_id    uuid := NEW.claim_id;
BEGIN
    -- NOT_CHECKED asserts no observation, so it cites none and needs no class. The
    -- accompanying CHECK already forbids every other status from citing nothing.
    IF NEW.field_status = 'NOT_CHECKED' THEN
        RETURN NEW;
    END IF;

    -- A citation must hang together. Without this a row could name an eligible
    -- source and an unrelated snapshot, and the class check would pass on the
    -- source while the evidence came from somewhere else entirely.
    IF NEW.source_id IS NOT NULL AND NEW.snapshot_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.snapshot s
             WHERE s.id = NEW.snapshot_id AND s.source_id = NEW.source_id
        ) THEN
            RAISE EXCEPTION
                'field_provenance cites snapshot % which did not come from source %',
                NEW.snapshot_id, NEW.source_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    IF NEW.claim_id IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.field_claim fc
             WHERE fc.id = NEW.claim_id
               AND fc.entity_type = NEW.entity_type
               AND fc.field_path = NEW.field_path
        ) THEN
            RAISE EXCEPTION
                'field_provenance for %.% cites claim %, which is about a different field',
                NEW.entity_type, NEW.field_path, NEW.claim_id
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

{_eligibility_check_body(
    entity_type_expr="NEW.entity_type",
    field_path_expr="NEW.field_path",
    source_col="NEW.source_id",
    label="field_provenance",
)}

    IF NOT v_found THEN
        RAISE EXCEPTION
            'field_provenance for %.% cites evidence that resolves to no source',
            NEW.entity_type, NEW.field_path
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
"""


CLAIM_FN = f"""
CREATE OR REPLACE FUNCTION app_field_claim_evidence_is_eligible() RETURNS trigger AS $$
DECLARE
    rec           record;
    v_found       boolean := false;
    v_snapshot_id uuid := NULL;
    v_claim_id    uuid := NULL;
BEGIN
    -- A claim always chains to exactly one source through its extraction.
    SELECT s.id INTO v_snapshot_id
      FROM public.extraction e
      JOIN public.snapshot s ON s.id = e.snapshot_id
     WHERE e.id = NEW.extraction_id;

    IF v_snapshot_id IS NULL THEN
        RAISE EXCEPTION 'field_claim cites extraction % with no snapshot', NEW.extraction_id
            USING ERRCODE = 'restrict_violation';
    END IF;

{_eligibility_check_body(
    entity_type_expr="NEW.entity_type",
    field_path_expr="NEW.field_path",
    source_col="NULL::uuid",
    label="field_claim",
)}

    IF NOT v_found THEN
        RAISE EXCEPTION 'field_claim resolves to no source' USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
"""


EARNED_FN = """
CREATE OR REPLACE FUNCTION app_source_eligibility_is_earned() RETURNS trigger AS $$
DECLARE
    v_mapping record;
BEGIN
    IF NEW.publication_eligibility = 'NOT_ELIGIBLE' THEN
        RETURN NEW;
    END IF;

    -- TARGET_SCOPE_ONLY is a label for something that is not an official source at
    -- all, so it needs no mapping and earns nothing. It exists to be refused later.
    IF NEW.publication_eligibility = 'TARGET_SCOPE_ONLY' THEN
        RETURN NEW;
    END IF;

    SELECT sm.id, sm.publication_eligibility, sm.url_sha256, sm.is_active,
           od.verification_status::text AS domain_status, od.is_active AS domain_active
      INTO v_mapping
      FROM public.source_mapping sm
      LEFT JOIN public.official_domain od ON od.id = sm.official_domain_id
     WHERE sm.promoted_source_id = NEW.id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'source % cannot be % : no promoted source_mapping vouches for it',
            NEW.id, NEW.publication_eligibility
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Eligibility is earned by promoting a verified source_mapping, '
                         'not asserted. This is what stops a spreadsheet being labelled '
                         'an official source.';
    END IF;

    IF v_mapping.publication_eligibility <> NEW.publication_eligibility::text THEN
        RAISE EXCEPTION
            'source % claims % but its mapping derives %',
            NEW.id, NEW.publication_eligibility, v_mapping.publication_eligibility
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The mapping must vouch for THIS url. Without this, a source classified from a
    -- legitimate mapping could later be repointed at a ranking file or a spreadsheet
    -- and keep its class.
    IF v_mapping.url_sha256 <> NEW.url_hash THEN
        RAISE EXCEPTION
            'source % url does not match the mapping that vouches for it',
            NEW.id
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Re-map and re-verify the new URL rather than editing a '
                         'classified source in place.';
    END IF;

    IF NOT v_mapping.is_active OR v_mapping.domain_status IS NULL
       OR v_mapping.domain_status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL')
       OR NOT v_mapping.domain_active THEN
        RAISE EXCEPTION
            'source % rests on a mapping or host that is no longer trusted', NEW.id
            USING ERRCODE = 'restrict_violation';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
"""


# ---------------------------------------------------------------------------
# Governed canonical columns
#
# DEFERRABLE INITIALLY DEFERRED, so the check runs at COMMIT. A publication
# transaction legitimately writes the canonical row and its provenance in either
# order, and an immediate trigger would force an artificial ordering on Step 9.
# ---------------------------------------------------------------------------

GOVERNED_FN = """
CREATE OR REPLACE FUNCTION app_governed_column_requires_provenance() RETURNS trigger AS $$
DECLARE
    v_column   text;
    v_expected jsonb;
    v_row      jsonb := to_jsonb(NEW);
    v_ok       boolean;
BEGIN
    FOREACH v_column IN ARRAY TG_ARGV
    LOOP
        v_expected := v_row -> v_column;

        -- A NULL column asserts nothing, so it needs no provenance. The value/status
        -- biconditionals elsewhere already stop NULL masquerading as PUBLISHED.
        IF v_expected IS NULL OR jsonb_typeof(v_expected) = 'null' THEN
            CONTINUE;
        END IF;

        IF jsonb_typeof(v_expected) = 'number' THEN
            -- Compared numerically: to_jsonb(1234.00::numeric) keeps its scale, so a
            -- textual comparison would reject an honest 1234.0 from the claim.
            SELECT EXISTS (
                SELECT 1 FROM public.field_provenance fp
                 WHERE fp.entity_type = TG_TABLE_NAME
                   AND fp.entity_id = NEW.id
                   AND fp.field_path = v_column
                   AND fp.field_status = 'PUBLISHED'
                   AND fp.value IS NOT NULL
                   AND jsonb_typeof(fp.value) = 'number'
                   AND (fp.value #>> '{}')::numeric = (v_expected #>> '{}')::numeric
            ) INTO v_ok;
        ELSE
            SELECT EXISTS (
                SELECT 1 FROM public.field_provenance fp
                 WHERE fp.entity_type = TG_TABLE_NAME
                   AND fp.entity_id = NEW.id
                   AND fp.field_path = v_column
                   AND fp.field_status = 'PUBLISHED'
                   AND fp.value = v_expected
            ) INTO v_ok;
        END IF;

        IF NOT v_ok THEN
            RAISE EXCEPTION
                '%.% was written without matching published provenance',
                TG_TABLE_NAME, v_column
                USING ERRCODE = 'restrict_violation',
                      DETAIL = 'value: ' || coalesce(v_expected #>> '{}', '<null>'),
                      HINT = 'Append a field_provenance row citing an eligible source '
                             'for this exact value, in the same transaction. A governed '
                             'value may not be written on the publisher''s own authority.';
        END IF;
    END LOOP;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp;
"""


def upgrade() -> None:
    # --- 1. the class vocabulary -------------------------------------------
    op.execute(
        f"CREATE TYPE publication_eligibility AS ENUM "
        f"({_quoted(PUBLICATION_ELIGIBILITY_VALUES)})"
    )

    # --- 2. source: the written class --------------------------------------
    op.execute(
        "ALTER TABLE source "
        "  ADD COLUMN publication_eligibility publication_eligibility NOT NULL "
        "      DEFAULT 'NOT_ELIGIBLE', "
        "  ADD COLUMN eligibility_authorization_id uuid "
        "      REFERENCES source_authorization (id) ON DELETE RESTRICT, "
        "  ADD COLUMN eligibility_set_by uuid REFERENCES app_user (id) ON DELETE RESTRICT, "
        "  ADD COLUMN eligibility_set_at timestamptz, "
        "  ADD COLUMN eligibility_reason text"
    )
    op.execute(
        "COMMENT ON COLUMN source.publication_eligibility IS "
        "'C27: what this source may substantiate. Earned by promoting a verified "
        "source_mapping, never asserted. AUTHORIZED_RANKING additionally requires a "
        "live display-allowed authorisation at citation time.'"
    )
    op.execute(
        "COMMENT ON COLUMN source.eligibility_reason IS "
        "'Why this class was assigned; shown to a reviewer'"
    )
    op.execute(
        "ALTER TABLE source "
        "  ADD CONSTRAINT ck_source_authorized_eligibility_names_its_grant CHECK ("
        "    publication_eligibility NOT IN ('AUTHORIZED_EXTERNAL', 'AUTHORIZED_RANKING') "
        "    OR eligibility_authorization_id IS NOT NULL), "
        "  ADD CONSTRAINT ck_source_grant_only_for_authorized_eligibility CHECK ("
        "    publication_eligibility IN ('AUTHORIZED_EXTERNAL', 'AUTHORIZED_RANKING') "
        "    OR eligibility_authorization_id IS NULL), "
        "  ADD CONSTRAINT ck_source_eligibility_records_actor_and_time CHECK ("
        "    publication_eligibility = 'NOT_ELIGIBLE' "
        "    OR (eligibility_set_by IS NOT NULL AND eligibility_set_at IS NOT NULL))"
    )
    op.create_index("ix_source_publication_eligibility", "source", ["publication_eligibility"])

    # --- 3. source_mapping: the derived, unassertable class -----------------
    # Enum columns compared against bare literals, no ::text cast: the cast is only
    # STABLE and a generated expression must be IMMUTABLE (C15). Proven against
    # PostgreSQL 16 before this revision was written.
    op.execute(
        "ALTER TABLE source_mapping ADD COLUMN publication_eligibility varchar(24) "
        "GENERATED ALWAYS AS ("
        "  CASE "
        "    WHEN verification_status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') "
        "         THEN 'NOT_ELIGIBLE' "
        "    WHEN source_category = 'AUTHORIZED_RANKING' THEN 'AUTHORIZED_RANKING' "
        "    WHEN verification_status = 'AUTHORIZED_EXTERNAL' THEN 'AUTHORIZED_EXTERNAL' "
        "    ELSE 'OFFICIAL_VERIFIED' "
        "  END) STORED NOT NULL"
    )
    op.execute(
        "ALTER TABLE source_mapping ADD CONSTRAINT "
        "ck_source_mapping_publication_eligibility_known CHECK ("
        f"publication_eligibility IN ({_quoted(MAPPING_ELIGIBILITY_VALUES)}))"
    )
    op.execute(
        "COMMENT ON COLUMN source_mapping.publication_eligibility IS "
        "'C27, GENERATED from verification_status and source_category. No role can "
        "write it, including the table owner.'"
    )

    # --- 4. provenance must cite something unless nothing was checked -------
    op.execute(
        "ALTER TABLE field_provenance ADD CONSTRAINT "
        "ck_field_provenance_asserted_status_cites_evidence CHECK ("
        "  field_status = 'NOT_CHECKED' "
        "  OR num_nonnulls(source_id, snapshot_id, claim_id) >= 1)"
    )

    # --- 5. the ranking licence gets a real foreign key ---------------------
    op.execute(
        "ALTER TABLE ranking_edition ADD CONSTRAINT "
        "fk_ranking_edition_authorization_id_source_authorization "
        "FOREIGN KEY (authorization_id) REFERENCES source_authorization (id) "
        "ON DELETE RESTRICT"
    )

    # --- 6. the gates ------------------------------------------------------
    op.execute(RESOLVE_FN)
    op.execute(PROVENANCE_FN)
    op.execute(CLAIM_FN)
    op.execute(EARNED_FN)
    op.execute(GOVERNED_FN)

    op.execute(
        "CREATE TRIGGER field_provenance_requires_eligible_evidence "
        "BEFORE INSERT ON field_provenance FOR EACH ROW "
        "EXECUTE FUNCTION app_field_provenance_evidence_is_eligible()"
    )
    op.execute(
        "CREATE TRIGGER field_claim_requires_eligible_evidence "
        "BEFORE INSERT ON field_claim FOR EACH ROW "
        "EXECUTE FUNCTION app_field_claim_evidence_is_eligible()"
    )
    op.execute(
        "CREATE TRIGGER source_eligibility_is_earned "
        "BEFORE INSERT OR UPDATE OF publication_eligibility, url_hash ON source "
        "FOR EACH ROW EXECUTE FUNCTION app_source_eligibility_is_earned()"
    )
    # ENABLE ALWAYS so a replica session cannot skip them. app_publisher cannot set
    # session_replication_role today (verified), but a defence that depends on a
    # privilege staying revoked is weaker than one that does not.
    for trigger, table in (
        ("field_provenance_requires_eligible_evidence", "field_provenance"),
        ("field_claim_requires_eligible_evidence", "field_claim"),
        ("source_eligibility_is_earned", "source"),
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")

    for table, columns in GOVERNED_CANONICAL_COLUMNS:
        args = ", ".join(f"'{column}'" for column in columns)
        trigger = f"{table}_governed_fields_need_provenance"
        op.execute(
            f"CREATE CONSTRAINT TRIGGER {trigger} "
            f"AFTER INSERT OR UPDATE ON {table} "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
            f"EXECUTE FUNCTION app_governed_column_requires_provenance({args})"
        )
        op.execute(f"ALTER TABLE {table} ENABLE ALWAYS TRIGGER {trigger}")

    op.execute(
        "REVOKE EXECUTE ON FUNCTION app_eligibility_of_evidence(uuid, uuid, uuid) FROM PUBLIC"
    )

    # --- 7. hygiene on the pre-existing trigger functions ------------------
    # Pinning search_path on a trigger function is defensive hygiene, not a fix for a
    # demonstrated bypass: the one candidate bypass (shadowing official_domain in
    # pg_temp) was tested and is blocked by the foreign key, which resolves by table
    # OID regardless of search_path. Pinned anyway, because the next function added
    # here might read a table no foreign key covers.
    for function in (
        "app_forbid_mutation()",
        "app_forbid_canonical_id_change()",
        "app_audit_log_verify_chain()",
        "app_source_mapping_requires_trusted_host()",
    ):
        op.execute(f"ALTER FUNCTION {function} SET search_path = pg_catalog, public, pg_temp")

    # --- 8. the publisher stops being able to read the client's list -------
    op.execute(
        _roles_exist_guard(
            _as_plpgsql(
                [
                    f"REVOKE SELECT ON {', '.join(ONBOARDING_READ_REVOKE)} " "FROM app_publisher",
                    # It appends to the audit chain; it never needs to read it.
                    "REVOKE SELECT ON audit_log FROM app_publisher",
                ]
            )
        )
    )


def downgrade() -> None:
    op.execute(
        _roles_exist_guard(
            _as_plpgsql(
                [
                    f"GRANT SELECT ON {', '.join(ONBOARDING_READ_REVOKE)} TO app_publisher",
                    "GRANT SELECT ON audit_log TO app_publisher",
                ]
            )
        )
    )

    for table, _columns in reversed(GOVERNED_CANONICAL_COLUMNS):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_governed_fields_need_provenance ON {table}")
    op.execute("DROP TRIGGER IF EXISTS source_eligibility_is_earned ON source")
    op.execute("DROP TRIGGER IF EXISTS field_claim_requires_eligible_evidence ON field_claim")
    op.execute(
        "DROP TRIGGER IF EXISTS field_provenance_requires_eligible_evidence ON field_provenance"
    )
    op.execute("DROP FUNCTION IF EXISTS app_governed_column_requires_provenance()")
    op.execute("DROP FUNCTION IF EXISTS app_source_eligibility_is_earned()")
    op.execute("DROP FUNCTION IF EXISTS app_field_claim_evidence_is_eligible()")
    op.execute("DROP FUNCTION IF EXISTS app_field_provenance_evidence_is_eligible()")
    op.execute("DROP FUNCTION IF EXISTS app_eligibility_of_evidence(uuid, uuid, uuid)")

    op.execute(
        "ALTER TABLE ranking_edition DROP CONSTRAINT IF EXISTS "
        "fk_ranking_edition_authorization_id_source_authorization"
    )
    op.execute(
        "ALTER TABLE field_provenance DROP CONSTRAINT IF EXISTS "
        "ck_field_provenance_asserted_status_cites_evidence"
    )
    op.execute(
        "ALTER TABLE source_mapping DROP CONSTRAINT IF EXISTS "
        "ck_source_mapping_publication_eligibility_known"
    )
    op.execute("ALTER TABLE source_mapping DROP COLUMN IF EXISTS publication_eligibility")
    op.drop_index("ix_source_publication_eligibility", table_name="source")
    for constraint in (
        "ck_source_eligibility_records_actor_and_time",
        "ck_source_grant_only_for_authorized_eligibility",
        "ck_source_authorized_eligibility_names_its_grant",
    ):
        op.execute(f"ALTER TABLE source DROP CONSTRAINT IF EXISTS {constraint}")
    op.execute(
        "ALTER TABLE source "
        "  DROP COLUMN IF EXISTS eligibility_reason, "
        "  DROP COLUMN IF EXISTS eligibility_set_at, "
        "  DROP COLUMN IF EXISTS eligibility_set_by, "
        "  DROP COLUMN IF EXISTS eligibility_authorization_id, "
        "  DROP COLUMN IF EXISTS publication_eligibility"
    )
    op.execute("DROP TYPE IF EXISTS publication_eligibility")
