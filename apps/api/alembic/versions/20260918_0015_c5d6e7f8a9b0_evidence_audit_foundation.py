"""Step 3.5: evidence and audit foundation corrections

Three defects found in review of the foundation the acquisition phase depends on.
Each is corrected here rather than worked around later, because Step 4 writes
directly against all three.

1. AUDIT CHAIN ORDERING
-----------------------
The chain previously chose its predecessor with
``ORDER BY occurred_at DESC, id DESC``. Two problems, both fatal to the guarantee:

* ``occurred_at`` defaults to ``now()``, which is *transaction* time, so two rows
  written in one transaction are exactly equal on it. The tie then broke on a random
  UUIDv4, making the order non-deterministic.
* Under READ COMMITTED, a concurrent uncommitted append is invisible, so two writers
  read the same predecessor. The ``UNIQUE (prev_hash)`` backstop caught the fork,
  but only by failing one writer's whole transaction.

Corrected with an **audit head row and an explicit monotonic sequence**.
``audit_chain_head`` is a single row holding ``last_seq`` and ``last_row_hash``. The
insert trigger does::

    SELECT last_seq, last_row_hash FROM audit_chain_head WHERE singleton FOR UPDATE

**Locking strategy.** That one row is the serialisation point for every append. The
lock is exclusive, so concurrent appenders queue rather than race, and each one in
turn reads a committed predecessor. Consequences, stated plainly:

* *No fork is possible* -- predecessor assignment happens inside the lock, so two
  writers cannot observe the same head.
* *Concurrent appends block instead of failing*, so the application needs no retry
  loop for chain contention. A retry that does happen is still safe: it re-enters
  the lock and gets a fresh predecessor, so a duplicate append becomes a new link
  rather than a conflicting one.
* *No deadlock* from this lock: there is one row, always taken at the same point in
  the trigger, so no cycle can form through it.
* *The lock is held to end of transaction.* Audit appends therefore serialise
  transactions that write them. At the PRD's volume (hundreds of actions a day) this
  is irrelevant; the publication transaction writes its audit row late, so hold time
  is short. If throughput ever matters, the escape hatch is one chain per partition
  (per root entity), not a weaker ordering.

``seq`` is gapless and strictly increasing because it is ``last_seq + 1`` under the
lock -- not a PostgreSQL sequence, which would leak gaps from rolled-back
transactions and could commit out of order relative to the hashes.

The trigger is ``SECURITY DEFINER`` so that no runtime role needs privileges on
``audit_chain_head``: the counter and head hash are unreachable except by appending
to ``audit_log``. ``search_path`` is pinned, which a SECURITY DEFINER function must
always do.

2. FETCH LIFECYCLE
------------------
``fetch_run`` was classified immutable while carrying ``started_at`` /
``finished_at`` / ``status`` -- i.e. it wanted to be updated as work progressed.
Split into model **A**: ``fetch_attempt`` is mutable working state (queued, claimed,
leased, heartbeat), and ``fetch_run`` is the immutable record written once when an
attempt terminates. A crashed worker stops renewing its lease; the sweeper writes a
``fetch_run`` with status ``ABANDONED``, so the crash becomes permanent history
rather than a row stuck in RUNNING.

3. CONTENT vs OBSERVATION
-------------------------
``snapshot`` carried ``UNIQUE (source_id, content_hash)``, which made it
**impossible** to record the same bytes observed twice from one source -- silently
discarding the second observation, and with it the "we checked on this date" record
the PRD requires. Split into ``content_blob`` (content identity, the dedup boundary)
and ``snapshot`` (observation, with no uniqueness on content).

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Audit chain trigger
# ---------------------------------------------------------------------------

AUDIT_HASH_FN = """
CREATE OR REPLACE FUNCTION app_audit_log_hash() RETURNS trigger AS $$
DECLARE
    head_seq  bigint;
    head_hash text;
BEGIN
    -- THE serialisation point. An exclusive row lock, so concurrent appenders queue
    -- and each reads a committed predecessor. This is what makes the predecessor
    -- deterministic; ordering by a timestamp and a random UUID did not.
    SELECT last_seq, last_row_hash
      INTO head_seq, head_hash
      FROM audit_chain_head
     WHERE singleton
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'audit_chain_head is missing; the audit chain cannot advance'
            USING ERRCODE = 'internal_error';
    END IF;

    NEW.seq := head_seq + 1;
    NEW.prev_hash := head_hash;

    -- Computed server-side, so whatever the caller supplied in seq/prev_hash/row_hash
    -- is discarded and the chain cannot be forged. `seq` is part of the digest, which
    -- means re-ordering the chain is detectable and not merely unlikely.
    NEW.row_hash := encode(
        sha256(
            convert_to(
                coalesce(head_hash, '')
                    || NEW.seq::text
                    || NEW.id::text
                    || NEW.actor_type::text
                    || coalesce(NEW.actor_id::text, '')
                    || NEW.action
                    || NEW.object_type
                    || coalesce(NEW.object_id::text, '')
                    || coalesce(NEW.before_state::text, '')
                    || coalesce(NEW.after_state::text, '')
                    || coalesce(NEW.reason, '')
                    || NEW.occurred_at::text,
                'UTF8'
            )
        ),
        'hex'
    );

    UPDATE audit_chain_head
       SET last_seq = NEW.seq,
           last_row_hash = NEW.row_hash,
           updated_at = now()
     WHERE singleton;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
   SECURITY DEFINER
   SET search_path = pg_catalog, public;
"""

# Verification is a function rather than application code so that an operator can
# check the chain with psql alone, and so the check cannot drift from the algorithm
# that produced the hashes.
AUDIT_VERIFY_FN = """
CREATE OR REPLACE FUNCTION app_audit_log_verify_chain()
RETURNS TABLE (bad_seq bigint, reason text) AS $$
DECLARE
    expected_prev text := NULL;
    expected_seq  bigint := 0;
    entry         record;
    recomputed    text;
BEGIN
    FOR entry IN SELECT * FROM audit_log ORDER BY seq LOOP
        expected_seq := expected_seq + 1;

        IF entry.seq <> expected_seq THEN
            RETURN QUERY SELECT entry.seq, format('expected seq %s', expected_seq);
        END IF;

        IF entry.prev_hash IS DISTINCT FROM expected_prev THEN
            RETURN QUERY SELECT entry.seq, 'prev_hash does not match the predecessor'::text;
        END IF;

        recomputed := encode(
            sha256(
                convert_to(
                    coalesce(expected_prev, '')
                        || entry.seq::text
                        || entry.id::text
                        || entry.actor_type::text
                        || coalesce(entry.actor_id::text, '')
                        || entry.action
                        || entry.object_type
                        || coalesce(entry.object_id::text, '')
                        || coalesce(entry.before_state::text, '')
                        || coalesce(entry.after_state::text, '')
                        || coalesce(entry.reason, '')
                        || entry.occurred_at::text,
                    'UTF8'
                )
            ),
            'hex'
        );
        IF recomputed <> entry.row_hash THEN
            RETURN QUERY SELECT entry.seq, 'row_hash does not match the row contents'::text;
        END IF;

        expected_prev := entry.row_hash;
    END LOOP;
    RETURN;
END;
$$ LANGUAGE plpgsql STABLE;
"""


def upgrade() -> None:
    # =====================================================================
    # 1. Audit chain
    # =====================================================================
    op.create_table(
        "audit_chain_head",
        sa.Column("singleton", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("last_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_row_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("singleton IS TRUE", name=op.f("ck_audit_chain_head_exactly_one_row")),
        sa.CheckConstraint("last_seq >= 0", name=op.f("ck_audit_chain_head_last_seq_non_negative")),
        sa.PrimaryKeyConstraint("singleton", name=op.f("pk_audit_chain_head")),
        comment="MUTABLE single-row pointer. The audit append serialisation point.",
    )
    # The one row must exist before any append; the trigger raises without it.
    op.execute("INSERT INTO audit_chain_head (singleton, last_seq) VALUES (true, 0)")

    # Existing rows would have no seq. There are none in any environment yet (the
    # audit log is only written by application code that does not exist), so adding
    # the column NOT NULL without a backfill is safe and keeps the invariant total.
    op.add_column(
        "audit_log",
        sa.Column(
            "seq",
            sa.BigInteger(),
            nullable=False,
            comment="Chain position. Assigned by trigger under the head row lock.",
        ),
    )
    op.create_unique_constraint(op.f("uq_audit_log_seq"), "audit_log", ["seq"])
    op.create_unique_constraint(op.f("uq_audit_log_row_hash"), "audit_log", ["row_hash"])
    op.create_check_constraint(op.f("ck_audit_log_seq_is_positive"), "audit_log", "seq >= 1")

    # Chain order is seq, so the access paths follow it rather than a timestamp.
    op.drop_index(op.f("ix_audit_log_object_type_object_id_occurred_at"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_actor_id_occurred_at"), table_name="audit_log")
    op.create_index(
        op.f("ix_audit_log_object_type_object_id_seq"),
        "audit_log",
        ["object_type", "object_id", "seq"],
    )
    op.create_index(op.f("ix_audit_log_actor_id_seq"), "audit_log", ["actor_id", "seq"])
    op.execute(
        "COMMENT ON TABLE audit_log IS "
        "'APPEND-ONLY, hash-chained. Chain order is seq, never a timestamp.'"
    )

    op.execute(AUDIT_HASH_FN)
    op.execute(AUDIT_VERIFY_FN)

    # =====================================================================
    # 2. Fetch lifecycle
    # =====================================================================
    op.create_table(
        "fetch_attempt",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column(
            "attempt_no",
            sa.Integer(),
            server_default="1",
            nullable=False,
            comment="1 for the first try of a cycle",
        ),
        sa.Column(
            "cycle_key",
            sa.String(length=64),
            nullable=False,
            comment=("Groups retries of one logical check, e.g. 2027-01-15T06 for the 06:00 sweep"),
        ),
        sa.Column("state", sa.String(length=16), server_default="QUEUED", nullable=False),
        sa.Column("scheduled_for", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("claimed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True, comment="Worker identity"),
        sa.Column("heartbeat_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "lease_expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Past this the worker is presumed dead",
        ),
        sa.Column("finalized_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("attempt_no >= 1", name=op.f("ck_fetch_attempt_attempt_no_is_positive")),
        sa.CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'FINALIZED', 'ABANDONED')",
            name=op.f("ck_fetch_attempt_state_known"),
        ),
        sa.CheckConstraint(
            "state <> 'RUNNING' OR (claimed_by IS NOT NULL AND claimed_at IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)",
            name=op.f("ck_fetch_attempt_running_attempt_is_leased"),
        ),
        sa.CheckConstraint(
            "state NOT IN ('FINALIZED', 'ABANDONED') OR finalized_at IS NOT NULL",
            name=op.f("ck_fetch_attempt_terminal_state_has_a_timestamp"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["source.id"],
            name=op.f("fk_fetch_attempt_source_id_source"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_attempt")),
        sa.UniqueConstraint(
            "source_id",
            "cycle_key",
            "attempt_no",
            name=op.f("uq_fetch_attempt_source_cycle_attempt"),
        ),
        comment="MUTABLE working state: queued and in-flight fetches, leased.",
    )
    # The stuck-work query. Partial: healthy attempts are the overwhelming majority.
    op.create_index(
        op.f("ix_fetch_attempt_expired_lease"),
        "fetch_attempt",
        ["lease_expires_at"],
        postgresql_where=sa.text("state = 'RUNNING'"),
    )
    op.create_index(
        op.f("ix_fetch_attempt_queued"),
        "fetch_attempt",
        ["scheduled_for"],
        postgresql_where=sa.text("state = 'QUEUED'"),
    )

    # ABANDONED is how a crashed worker becomes permanent history.
    op.execute("ALTER TYPE fetch_status ADD VALUE IF NOT EXISTS 'ABANDONED'")

    op.add_column(
        "fetch_run",
        sa.Column(
            "attempt_id",
            sa.UUID(),
            nullable=False,
            comment="The working-state row this run concluded",
        ),
    )
    op.add_column(
        "fetch_run", sa.Column("attempt_no", sa.Integer(), server_default="1", nullable=False)
    )
    op.create_foreign_key(
        op.f("fk_fetch_run_attempt_id_fetch_attempt"),
        "fetch_run",
        "fetch_attempt",
        ["attempt_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # One completed run per attempt: this is what keeps retries separate.
    op.create_unique_constraint(op.f("uq_fetch_run_attempt_id"), "fetch_run", ["attempt_id"])
    op.create_check_constraint(
        op.f("ck_fetch_run_attempt_no_is_positive"), "fetch_run", "attempt_no >= 1"
    )
    op.create_check_constraint(
        op.f("ck_fetch_run_a_failure_names_its_error"),
        "fetch_run",
        "status IN ('OK', 'UNCHANGED') OR error_class IS NOT NULL",
    )
    op.execute(
        "COMMENT ON TABLE fetch_run IS "
        "'APPEND-ONLY. One row per COMPLETED attempt, abandonments included.'"
    )

    # =====================================================================
    # 3. Content identity vs observation
    # =====================================================================
    op.create_table(
        "content_blob",
        sa.Column(
            "content_hash",
            sa.String(length=64),
            nullable=False,
            comment="sha256 hex of the bytes; this is the identity",
        ),
        sa.Column(
            "storage_key",
            sa.Text(),
            nullable=False,
            comment="Object-storage key; bodies never live in PostgreSQL",
        ),
        sa.Column("content_type", sa.String(length=128), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column("rendered_text_key", sa.Text(), nullable=True),
        sa.Column("screenshot_key", sa.Text(), nullable=True),
        sa.Column(
            "first_observed_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            comment="When these bytes were first seen",
        ),
        sa.Column(
            "recorded_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size >= 0",
            name=op.f("ck_content_blob_byte_size_non_negative"),
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_content_blob_content_hash_is_sha256_hex"),
        ),
        sa.CheckConstraint(
            "length(btrim(storage_key)) > 0",
            name=op.f("ck_content_blob_storage_key_is_not_blank"),
        ),
        sa.PrimaryKeyConstraint("content_hash", name=op.f("pk_content_blob")),
        comment="APPEND-ONLY content identity. The dedup boundary; one row per sha256.",
    )

    # THE correction: an observation is no longer unique per (source, content), so
    # the same bytes seen twice from one source keep both records.
    op.drop_constraint(op.f("uq_snapshot_source_id_content_hash"), "snapshot", type_="unique")

    # Content-identity columns move to content_blob. No data migration: no snapshot
    # rows exist in any environment yet.
    for column in (
        "storage_key",
        "content_type",
        "byte_size",
        "rendered_text_key",
        "screenshot_key",
        "canonical_url",
    ):
        op.drop_column("snapshot", column)
    op.drop_constraint(op.f("ck_snapshot_content_hash_is_sha256_hex"), "snapshot", type_="check")

    op.add_column(
        "snapshot",
        sa.Column("requested_url", sa.Text(), nullable=False, comment="The URL actually requested"),
    )
    op.add_column(
        "snapshot",
        sa.Column(
            "effective_url",
            sa.Text(),
            nullable=True,
            comment="Final URL after redirects, when it differs from the request",
        ),
    )
    op.add_column("snapshot", sa.Column("http_status", sa.Integer(), nullable=True))
    op.add_column(
        "snapshot",
        sa.Column(
            "fetcher",
            sa.String(length=32),
            nullable=False,
            comment="STATIC / BROWSER / DOCUMENT / MANUAL",
        ),
    )
    op.alter_column(
        "snapshot",
        "content_hash",
        existing_type=sa.String(length=64),
        comment="The bytes this observation saw; many observations may share one blob",
        existing_nullable=False,
    )
    op.create_foreign_key(
        op.f("fk_snapshot_content_hash_content_blob"),
        "snapshot",
        "content_blob",
        ["content_hash"],
        ["content_hash"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_snapshot_http_status_range"),
        "snapshot",
        "http_status IS NULL OR http_status BETWEEN 100 AND 599",
    )
    op.create_check_constraint(
        op.f("ck_snapshot_fetcher_known"),
        "snapshot",
        "fetcher IN ('STATIC', 'BROWSER', 'DOCUMENT', 'MANUAL')",
    )
    op.create_index(
        op.f("ix_snapshot_source_id_observed_at"), "snapshot", ["source_id", "observed_at"]
    )
    op.execute(
        "COMMENT ON TABLE snapshot IS "
        "'APPEND-ONLY observation. Many observations may share one blob.'"
    )

    # =====================================================================
    # 4. Privileges and immutability for the new tables
    # =====================================================================
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_worker') THEN
                -- Working state: the worker claims, heartbeats and finalises.
                EXECUTE 'GRANT SELECT, INSERT, UPDATE ON fetch_attempt TO app_worker';
                -- Content identity is append-only; the worker registers new blobs.
                EXECUTE 'GRANT SELECT, INSERT ON content_blob TO app_worker';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_api') THEN
                -- Read-only: ops needs to see stuck work and evidence.
                EXECUTE 'GRANT SELECT ON fetch_attempt, content_blob TO app_api';
            END IF;
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_publisher') THEN
                EXECUTE 'GRANT SELECT ON fetch_attempt, content_blob TO app_publisher';
            END IF;
            -- audit_chain_head intentionally receives NO grants: the maintaining
            -- trigger is SECURITY DEFINER, so the counter and head hash are
            -- unreachable except by appending to audit_log.
        END
        $$;
        """
    )

    # content_blob is append-only history like the rest of the evidence plane.
    op.execute(
        """
        CREATE TRIGGER content_blob_forbid_mutation
        BEFORE UPDATE OR DELETE ON content_blob
        FOR EACH ROW EXECUTE FUNCTION app_forbid_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS content_blob_forbid_mutation ON content_blob")
    op.execute("DROP FUNCTION IF EXISTS app_audit_log_verify_chain()")

    # Restore the pre-3.5 trigger body so the downgrade leaves a working chain, even
    # though its ordering is the defect this revision fixed.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_audit_log_hash() RETURNS trigger AS $$
        DECLARE
            previous_hash text;
        BEGIN
            SELECT row_hash INTO previous_hash
            FROM audit_log ORDER BY occurred_at DESC, id DESC LIMIT 1;
            NEW.prev_hash := previous_hash;
            NEW.row_hash := encode(
                sha256(convert_to(coalesce(previous_hash, '') || NEW.id::text, 'UTF8')), 'hex'
            );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )

    op.drop_index(op.f("ix_snapshot_source_id_observed_at"), table_name="snapshot")
    op.drop_constraint(op.f("ck_snapshot_fetcher_known"), "snapshot", type_="check")
    op.drop_constraint(op.f("ck_snapshot_http_status_range"), "snapshot", type_="check")
    op.drop_constraint(
        op.f("fk_snapshot_content_hash_content_blob"), "snapshot", type_="foreignkey"
    )
    for column in ("fetcher", "http_status", "effective_url", "requested_url"):
        op.drop_column("snapshot", column)
    op.create_check_constraint(
        op.f("ck_snapshot_content_hash_is_sha256_hex"),
        "snapshot",
        "content_hash ~ '^[0-9a-f]{64}$'",
    )
    op.add_column("snapshot", sa.Column("canonical_url", sa.Text(), nullable=True))
    op.add_column("snapshot", sa.Column("screenshot_key", sa.Text(), nullable=True))
    op.add_column("snapshot", sa.Column("rendered_text_key", sa.Text(), nullable=True))
    op.add_column("snapshot", sa.Column("byte_size", sa.Integer(), nullable=True))
    op.add_column("snapshot", sa.Column("content_type", sa.String(length=128), nullable=True))
    op.add_column("snapshot", sa.Column("storage_key", sa.Text(), nullable=False))
    op.create_unique_constraint(
        op.f("uq_snapshot_source_id_content_hash"), "snapshot", ["source_id", "content_hash"]
    )
    op.drop_table("content_blob")

    op.drop_constraint(op.f("ck_fetch_run_a_failure_names_its_error"), "fetch_run", type_="check")
    op.drop_constraint(op.f("ck_fetch_run_attempt_no_is_positive"), "fetch_run", type_="check")
    op.drop_constraint(op.f("uq_fetch_run_attempt_id"), "fetch_run", type_="unique")
    op.drop_constraint(
        op.f("fk_fetch_run_attempt_id_fetch_attempt"), "fetch_run", type_="foreignkey"
    )
    op.drop_column("fetch_run", "attempt_no")
    op.drop_column("fetch_run", "attempt_id")
    op.drop_index(op.f("ix_fetch_attempt_queued"), table_name="fetch_attempt")
    op.drop_index(op.f("ix_fetch_attempt_expired_lease"), table_name="fetch_attempt")
    op.drop_table("fetch_attempt")
    # The ABANDONED enum member is deliberately left in place: PostgreSQL cannot drop
    # an enum value, and recreating the type would require rewriting every column
    # that uses it. A spare member is harmless.

    op.drop_index(op.f("ix_audit_log_actor_id_seq"), table_name="audit_log")
    op.drop_index(op.f("ix_audit_log_object_type_object_id_seq"), table_name="audit_log")
    op.create_index(
        op.f("ix_audit_log_actor_id_occurred_at"), "audit_log", ["actor_id", "occurred_at"]
    )
    op.create_index(
        op.f("ix_audit_log_object_type_object_id_occurred_at"),
        "audit_log",
        ["object_type", "object_id", "occurred_at"],
    )
    op.drop_constraint(op.f("ck_audit_log_seq_is_positive"), "audit_log", type_="check")
    op.drop_constraint(op.f("uq_audit_log_row_hash"), "audit_log", type_="unique")
    op.drop_constraint(op.f("uq_audit_log_seq"), "audit_log", type_="unique")
    op.drop_column("audit_log", "seq")
    op.drop_table("audit_chain_head")
