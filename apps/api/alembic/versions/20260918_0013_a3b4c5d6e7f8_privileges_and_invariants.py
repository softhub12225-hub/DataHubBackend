"""Domain group 11: privileges, immutability triggers, identity guards

The most load-bearing revision in the set. It installs invariants **I1** and **I2**
in the same migration set that created the tables they protect — deliberately, not
afterwards. A grant applied later leaves a window in which the invariant does not
hold, and `ALTER DEFAULT PRIVILEGES` only affects objects created after it runs.

Three layers, in order of authority:

1. **Privileges** (primary). Runtime roles own nothing. Canonical projections are
   writable only by ``app_publisher``. Immutable history has ``UPDATE`` and
   ``DELETE`` revoked from every application role, ``app_publisher`` included.
2. **Triggers** (defense-in-depth). A mistaken application call — or a mistaken
   migration — must not be able to rewrite published history. These fire for the
   table owner too, which is the point.
3. **Identity guards**. ``canonical_id`` immutability (B6) and the ``audit_log``
   hash chain, both of which need the previous row and therefore cannot be CHECKs.

Deliberately **not** here: segregation of duties, risk classification, the drift
guard, SLA computation, applicant-scope resolution. Each needs other rows, the
acting user's identity, or role membership, and belongs to the service layer (C2).
See ARCHITECTURE.md section 8.2.

### Escape hatch for controlled maintenance

The immutability triggers honour a session setting::

    SET LOCAL app.allow_history_maintenance = 'on';

It exists so a genuine schema migration over a history table — adding a column,
backfilling a hash chain — remains possible without dropping triggers. It is only
useful to a role that already holds ``UPDATE``, which the runtime roles do not, so
this is the owner's tool rather than an application back door. Application code
never sets it.

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# ---------------------------------------------------------------------------
# Table lists FROZEN as of this revision.
#
# These were originally imported from `app.db.classification`, which was wrong: a
# migration must describe the schema **as of its own point in history**. When the
# Step 3.5 revision added three tables to that shared list, this revision started
# trying to grant on tables that would not exist for another two revisions, and a
# fresh database could no longer be built.
#
# `app.db.classification` remains the single source of truth for *current* state,
# which is what the privilege tests assert against. The coupling that matters is
# enforced by a test: every table in the current classification must end up with
# grants from some revision, so a new table cannot be added without one.
# ---------------------------------------------------------------------------

IMMUTABLE_TABLES = (
    "entity_version",
    "field_provenance",
    "audit_log",
    "change_event",
    "entity_relationship",
    "snapshot",
    "extraction",
    "field_claim",
    "claim_resolution",
    "review_decision",
    "conflict_resolution",
    "fetch_run",
)

CANONICAL_TABLES = (
    "university",
    "campus",
    "faculty",
    "faculty_campus",
    "program",
    "program_faculty",
    "program_discipline",
    "program_offering",
    "intake",
    "application_round",
    "application_deadline",
    "admission_requirement",
    "language_requirement",
    "tuition",
    "ranking_publisher",
    "ranking_edition",
    "ranking_entry",
    "entity_alias",
    "fact_absence",
    "entity_head",
    "field_current",
)

REFERENCE_TABLES = (
    "destination",
    "discipline",
    "degree_level",
    "intake_season",
    "currency",
    "billing_unit",
    "test_type",
    "student_category",
    "scope_dimension",
    "applicant_scope",
    "applicant_scope_criterion",
    "qualification_group",
    "qualification_group_member",
    "application_round_type",
)

GOVERNANCE_TABLES = (
    "change_proposal",
    "change_proposal_item",
    "review_task",
    "field_conflict",
)

SOURCE_TABLES = ("source", "source_field_binding", "source_authorization")

IDENTITY_TABLES = (
    "app_user",
    "role",
    "permission",
    "role_permission",
    "user_role",
    "api_client",
)

INFRASTRUCTURE_TABLES = ("outbox_message", "resolution_candidate", "user_session")

ALL_CLASSIFIED_TABLES = (
    IMMUTABLE_TABLES
    + CANONICAL_TABLES
    + REFERENCE_TABLES
    + GOVERNANCE_TABLES
    + SOURCE_TABLES
    + IDENTITY_TABLES
    + INFRASTRUCTURE_TABLES
)

CANONICAL_ID_TABLES = ("university", "program", "program_offering")

revision: str = "a3b4c5d6e7f8"
down_revision: str | None = "f2a3b4c5d6e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _roles_exist_guard(body: str) -> str:
    """Wrap DDL so it is skipped where the runtime roles were never created.

    A throwaway scratch database sometimes uses a single user. The privileges are
    then not applied and the privilege tests skip, which is honest: there is nothing
    to verify.
    """
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


def _as_plpgsql(statements: list[str]) -> str:
    """Render statements as EXECUTE lines, escaping single quotes for the DO block."""
    return "\n".join(
        f"            EXECUTE '{statement.replace(chr(39), chr(39) * 2)}';"
        for statement in statements
    )


def _grant_sql() -> str:
    everything = ", ".join(ALL_CLASSIFIED_TABLES)
    canonical = ", ".join(CANONICAL_TABLES)
    governance = ", ".join(GOVERNANCE_TABLES)
    sources = ", ".join(SOURCE_TABLES)

    statements = [
        # --- Baseline: everyone reads, nobody writes -------------------------
        # Every write privilege below is then granted explicitly, so the default
        # is closed rather than open.
        f"REVOKE ALL ON {everything} FROM app_api, app_worker, app_publisher",
        f"GRANT SELECT ON {everything} TO app_api, app_worker, app_publisher",
        # --- I1: canonical projections are the publisher's alone -------------
        # This is what stops the API or a worker bypassing publication.
        f"GRANT INSERT, UPDATE ON {canonical} TO app_publisher",
        # --- I2: immutable history is INSERT-only ---------------------------
        # Granted only to the role that legitimately appends to each table.
        "GRANT INSERT ON entity_version, field_provenance, change_event, "
        "entity_relationship TO app_publisher",
        "GRANT INSERT ON snapshot, extraction, field_claim, claim_resolution, fetch_run "
        "TO app_worker",
        # Reviewers act through the API, so decisions are appended by app_api.
        "GRANT INSERT ON review_decision, conflict_resolution TO app_api",
        # Every role appends to the audit log; none may alter it.
        "GRANT INSERT ON audit_log TO app_api, app_worker, app_publisher",
        # --- Governance working state ---------------------------------------
        # Mutable, by the roles that own the workflow. No DELETE: a proposal is
        # discarded by status, never removed.
        f"GRANT INSERT, UPDATE ON {governance} TO app_api, app_worker",
        # --- Source registry and resolution queue ---------------------------
        f"GRANT INSERT, UPDATE ON {sources} TO app_api",
        "GRANT UPDATE ON source TO app_worker",
        "GRANT INSERT, UPDATE ON resolution_candidate TO app_api, app_worker",
        # --- Outbox: written by the publisher, drained by the worker ---------
        # The only business-adjacent table any role may DELETE from. It is delivery
        # infrastructure, not history, so pruning a delivered message loses no fact
        # (C7).
        "GRANT INSERT ON outbox_message TO app_publisher",
        "GRANT SELECT, UPDATE, DELETE ON outbox_message TO app_worker",
        # --- Sessions: the API issues and revokes them ----------------------
        "GRANT INSERT, UPDATE, DELETE ON user_session TO app_api",
    ]
    return _roles_exist_guard(_as_plpgsql(statements))


def _revoke_sql() -> str:
    everything = ", ".join(ALL_CLASSIFIED_TABLES)
    return _roles_exist_guard(
        _as_plpgsql([f"REVOKE ALL ON {everything} FROM app_api, app_worker, app_publisher"])
    )


# ---------------------------------------------------------------------------
# Trigger functions
# ---------------------------------------------------------------------------

FORBID_MUTATION_FN = """
CREATE OR REPLACE FUNCTION app_forbid_mutation() RETURNS trigger AS $$
BEGIN
    -- Controlled maintenance escape hatch. Only useful to a role that already holds
    -- UPDATE, which the runtime roles do not.
    IF coalesce(current_setting('app.allow_history_maintenance', true), 'off') = 'on' THEN
        IF TG_OP = 'DELETE' THEN
            RETURN OLD;
        END IF;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION
        '% on % is forbidden: this table is append-only history',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation',
              HINT = 'Correct the record by appending a new row. For controlled schema '
                     'maintenance, SET LOCAL app.allow_history_maintenance = ''on''.';
END;
$$ LANGUAGE plpgsql;
"""

FORBID_CANONICAL_ID_CHANGE_FN = """
CREATE OR REPLACE FUNCTION app_forbid_canonical_id_change() RETURNS trigger AS $$
BEGIN
    -- B6: canonical_id is immutable once created. A CHECK cannot see the previous
    -- value, so this has to be a trigger. External references depend on the id, so
    -- a rename belongs in entity_alias and a replacement in entity_relationship.
    IF NEW.canonical_id IS DISTINCT FROM OLD.canonical_id THEN
        RAISE EXCEPTION
            'canonical_id is immutable (% -> %) on %',
            OLD.canonical_id, NEW.canonical_id, TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation',
                  HINT = 'Record a rename in entity_alias, or a replacement in '
                         'entity_relationship.';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

AUDIT_HASH_FN = """
CREATE OR REPLACE FUNCTION app_audit_log_hash() RETURNS trigger AS $$
DECLARE
    previous_hash text;
BEGIN
    -- Computed server-side so a caller cannot forge the chain: whatever the
    -- application supplies in row_hash is discarded.
    SELECT row_hash INTO previous_hash
    FROM audit_log
    ORDER BY occurred_at DESC, id DESC
    LIMIT 1;

    NEW.prev_hash := previous_hash;
    NEW.row_hash := encode(
        sha256(
            convert_to(
                coalesce(previous_hash, '')
                    || NEW.id::text
                    || NEW.actor_type::text
                    || coalesce(NEW.actor_id::text, '')
                    || NEW.action
                    || NEW.object_type
                    || coalesce(NEW.object_id::text, '')
                    || coalesce(NEW.before_state::text, '')
                    || coalesce(NEW.after_state::text, '')
                    || NEW.occurred_at::text,
                'UTF8'
            )
        ),
        'hex'
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    # --- 1. Trigger functions --------------------------------------------
    op.execute(FORBID_MUTATION_FN)
    op.execute(FORBID_CANONICAL_ID_CHANGE_FN)
    op.execute(AUDIT_HASH_FN)

    # --- 2. Immutability triggers (defense-in-depth for I2) --------------
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_forbid_mutation
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION app_forbid_mutation();
            """
        )

    # --- 3. canonical_id immutability (B6) -------------------------------
    for table in CANONICAL_ID_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_canonical_id_immutable
            BEFORE UPDATE OF canonical_id ON {table}
            FOR EACH ROW EXECUTE FUNCTION app_forbid_canonical_id_change();
            """
        )

    # --- 4. Audit hash chain ---------------------------------------------
    op.execute(
        """
        CREATE TRIGGER audit_log_hash_chain
        BEFORE INSERT ON audit_log
        FOR EACH ROW EXECUTE FUNCTION app_audit_log_hash();
        """
    )

    # --- 5. Privileges (primary enforcement of I1 and I2) ----------------
    op.execute(_grant_sql())


def downgrade() -> None:
    op.execute(_revoke_sql())
    op.execute("DROP TRIGGER IF EXISTS audit_log_hash_chain ON audit_log")
    for table in CANONICAL_ID_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_canonical_id_immutable ON {table}")
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_mutation ON {table}")
    op.execute("DROP FUNCTION IF EXISTS app_audit_log_hash()")
    op.execute("DROP FUNCTION IF EXISTS app_forbid_canonical_id_change()")
    op.execute("DROP FUNCTION IF EXISTS app_forbid_mutation()")
