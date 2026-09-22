"""Step 5C.1: document-level extraction over the existing extraction table

WHAT THIS DOES NOT DO
=====================
It does not add an `extraction` table. One already exists, with `snapshot_id`,
`extractor_name`, `extractor_version`, `status`, `recorded_at`, an append-only trigger,
and `field_claim.extraction_id` pointing at it. That is the lineage Step 5C.2 needs --
`field_claim -> extraction -> snapshot -> source` -- and re-modelling it would break the
one invariant this step was told to preserve.

So this is four columns and one constraint.

WHY THE PAYLOAD IS NOT IN `output`
==================================
`extraction.output` is `jsonb` and could hold a normalised document. For 174 pages
averaging 170 KB of HTML the derived documents run to tens of megabytes, and putting
them in PostgreSQL means every backup, every replica and every `pg_dump` carries
derived data that is reproducible from bytes we already store. The row keeps the
metadata and the pointer; the payload goes to the object store under its own hash.

`output` is left for small derived summaries, which is what it is good at.

WHY THE ARTIFACT HASH IS OVER THE DOCUMENT ALONE
================================================
The artifact is a **pure function of (raw bytes, extractor version)**. It deliberately
contains no snapshot id, no source id and no timestamp, so two sources serving identical
bytes produce one artifact and the determinism test can compare hashes across runs.
Lineage is not lost -- it lives in `extraction.snapshot_id`, which is where it belongs
and where a join can follow it.

IDEMPOTENCY
===========
`uq_extraction_snapshot_extractor_version` makes one result per (snapshot, extractor,
version). Re-running the same extractor over the same snapshot is a no-op rather than a
second row: the result is a pure function of its inputs, so a second row could only ever
be a duplicate. A genuinely different result requires a new `extractor_version`, and the
old row is retained because `extraction` is append-only.

The consequence, stated rather than discovered later: a `FAILED` extraction caused by
something environmental (an unreadable object, say) cannot be retried under the same
version. That is the cost of immutability, and the fix is a version bump, not an UPDATE.

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# No import from app.* (C21/C22): frozen literals only.

revision: str = "c8d9e0f1a2b3"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "extraction",
        sa.Column(
            "input_content_hash",
            sa.String(length=64),
            nullable=True,
            comment="sha256 of the input bytes, denormalised from the snapshot so "
            "'same bytes, same extractor version' is answerable without a join",
        ),
    )
    op.add_column(
        "extraction",
        sa.Column(
            "document_hash",
            sa.String(length=64),
            nullable=True,
            comment="sha256 of the canonical derived document; a pure function of "
            "(input bytes, extractor version) and free of ids and timestamps",
        ),
    )
    op.add_column(
        "extraction",
        sa.Column("document_storage_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "extraction",
        sa.Column("document_byte_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "extraction",
        sa.Column(
            "warnings",
            sa.dialects.postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=True,
            comment="Why a PARTIAL is partial: a malformed JSON-LD block, a charset "
            "fallback. Distinct from error_detail, which explains a FAILED.",
        ),
    )

    op.create_check_constraint(
        "document_hash_is_sha256_hex",
        "extraction",
        "document_hash IS NULL OR document_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        "input_content_hash_is_sha256_hex",
        "extraction",
        "input_content_hash IS NULL OR input_content_hash ~ '^[0-9a-f]{64}$'",
    )
    # A stored document names where it is and how big it is, or it is not stored. The
    # dangling-reference failure C12 exists to prevent, one plane further down.
    op.create_check_constraint(
        "document_is_completely_described",
        "extraction",
        "(document_hash IS NULL AND document_storage_key IS NULL "
        "  AND document_byte_size IS NULL) "
        "OR (document_hash IS NOT NULL AND document_storage_key IS NOT NULL "
        "  AND document_byte_size IS NOT NULL AND document_byte_size >= 0)",
    )
    # The name the model's naming convention generates from these columns. Writing
    # a shorter one here would make `alembic check` propose dropping and recreating
    # it forever.
    op.create_unique_constraint(
        "uq_extraction_snapshot_id_extractor_name_extractor_version",
        "extraction",
        ["snapshot_id", "extractor_name", "extractor_version"],
    )
    op.create_index("ix_extraction_document_hash", "extraction", ["document_hash"])

    op.execute(
        "COMMENT ON TABLE extraction IS "
        "'One deterministic extraction result per (snapshot, extractor, version). "
        "Append-only. The normalised document payload lives in the object store under "
        "document_hash; this row holds the metadata and the lineage. Extraction "
        "confers no trust: a document derived from a NOT_ELIGIBLE source stays "
        "NOT_ELIGIBLE (C27).'"
    )


def downgrade() -> None:
    op.drop_index("ix_extraction_document_hash", table_name="extraction")
    op.drop_constraint(
        op.f("uq_extraction_snapshot_id_extractor_name_extractor_version"),
        "extraction",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_extraction_document_is_completely_described"), "extraction", type_="check"
    )
    op.drop_constraint(
        op.f("ck_extraction_input_content_hash_is_sha256_hex"), "extraction", type_="check"
    )
    op.drop_constraint(
        op.f("ck_extraction_document_hash_is_sha256_hex"), "extraction", type_="check"
    )
    op.drop_column("extraction", "warnings")
    op.drop_column("extraction", "document_byte_size")
    op.drop_column("extraction", "document_storage_key")
    op.drop_column("extraction", "document_hash")
    op.drop_column("extraction", "input_content_hash")
