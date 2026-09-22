"""Privilege classification of every domain table.

**Single source of truth.** The privileges migration reads these lists to emit its
GRANT/REVOKE statements, and the privilege tests read the same lists to assert the
result. Adding a table to one without the other is impossible, and a table missing
from all of them fails `test_every_domain_table_is_classified` — which is the point.

The classes exist because the architecture's invariants are privilege-based:

* ``IMMUTABLE_TABLES`` — append-only history and evidence (I2). No ``UPDATE``, no
  ``DELETE``, for any role including ``app_publisher``.
* ``CANONICAL_TABLES`` — curated published state (I1). Writable only by
  ``app_publisher``, so neither the API nor a worker can bypass publication.
* ``REFERENCE_TABLES`` — controlled vocabularies. Read by everyone, changed by an
  administrator only (N1).
* ``GOVERNANCE_TABLES`` — mutable review working state.
* ``SOURCE_TABLES`` — the source registry.
* ``ONBOARDING_TABLES`` — client scope and the official-identity workflow (Step 4).
  Mutable human working state; deliberately unwritable by ``app_publisher``.
* ``PILOT_STAGING_TABLES`` — a hand-filled workbook, as supplied (U12). Not evidence,
  not publication eligible, and invisible to ``app_publisher`` entirely.
* ``IDENTITY_TABLES`` — RBAC.
* ``INFRASTRUCTURE_TABLES`` — the outbox, the resolution queue and sessions. The
  outbox is the one table any role may ``DELETE`` from: it is delivery
  infrastructure, not history, so pruning a delivered message loses no fact (C7).
"""

from __future__ import annotations

IMMUTABLE_TABLES: tuple[str, ...] = (
    "entity_version",
    "field_provenance",
    "audit_log",
    "change_event",
    "entity_relationship",
    "snapshot",
    "extraction",
    "field_claim",
    "field_claim_candidate",
    "field_claim_candidate_review",
    # Step 5C.9. Human scope and conflict resolutions are decisions, so they are history:
    # a reviewer may revisit one, and the earlier judgement is a fact about what was
    # thought at the time rather than an error to overwrite.
    "candidate_scope_resolution",
    "candidate_conflict_resolution",
    "claim_resolution",
    "content_blob",
    "review_decision",
    "conflict_resolution",
    "fetch_run",
    # Step 4. What one version of a client list said, and how it differed from the
    # previous version, are historical statements: revising them would destroy the
    # only record of what scope we were given and when.
    "target_list_entry",
    "target_list_diff",
    # U12. What a collector wrote in a returned workbook. A correction is a new
    # submission, never an edit of the old one -- so the two can be compared, which
    # is impossible if the first was overwritten.
    "pilot_selected_university",
    "pilot_collected_program",
    "pilot_collected_fact",
)

CANONICAL_TABLES: tuple[str, ...] = (
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

REFERENCE_TABLES: tuple[str, ...] = (
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

GOVERNANCE_TABLES: tuple[str, ...] = (
    "change_proposal",
    "change_proposal_item",
    "review_task",
    "field_conflict",
)

SOURCE_TABLES: tuple[str, ...] = (
    "source",
    "source_field_binding",
    "source_authorization",
    # Step 5C.3. One row per extractor naming the version that is live, so SQL can tell
    # a current candidate from a superseded one. Mutable on purpose: "which version is
    # current" has one answer at a time, and the history of which versions existed is
    # already in `field_claim_candidate.extractor_version`, where it cannot be lost.
    "claim_rule_version",
    # Step 5C.4. The other axis of the same question: which parse of a page is live.
    # Separate from the rule version because they are different questions, and because
    # four of the six rules were measured unchanged across the parser change -- see
    # revision a2b3c4d5e6f7. Mutable for the same reason as the row above.
    "document_artifact_version",
)

#: Step 4. Client scope and the official-identity workflow that prepares an
#: institution for collection.
#:
#: Mutable by the API role, because onboarding *is* human working state: a reviewer
#: verifies a host, rejects a candidate, maps a source.
#:
#: These carry no privilege at all for `app_publisher` -- not write, and since C27
#: not read either. **But grants alone never established that a spreadsheet value
#: could not become a published fact, and an earlier version of this comment claimed
#: they did.** `app_publisher` could read these tables and write the canonical ones,
#: so one identity was enough. What actually enforces the rule is
#: `publication_eligibility` plus the triggers on `field_claim` and
#: `field_provenance`; see C27 and `docs/ONBOARDING.md` section 1.
#:
#: The immutable halves of this domain (`target_list_entry`, `target_list_diff`) are
#: classified as history above, not here.
ONBOARDING_TABLES: tuple[str, ...] = (
    "target_list",
    "target_institution",
    "official_domain",
    "source_mapping",
    "source_degree_scope",
    "source_discipline_scope",
)

#: U12. The manual collection staging plane: a returned workbook, as supplied.
#:
#: Mutable only where a decision has to land -- `pilot_submission.import_status` and
#: `pilot_collected_source.verification_state` (U15). The rows recording what the
#: collector actually wrote are classified as history above.
#:
#: `app_publisher` holds no privilege here, for the same reason it holds none on the
#: onboarding tables since C27. That grant is not what makes staging unpublishable
#: though -- what does is that no `pilot_*` table is reachable from
#: `field_provenance` by any foreign key, and that a `field_claim` still requires an
#: extraction of a snapshot of a fetched source. A workbook produces none of those.
PILOT_STAGING_TABLES: tuple[str, ...] = (
    "pilot_submission",
    "pilot_collected_source",
)

IDENTITY_TABLES: tuple[str, ...] = (
    "app_user",
    "role",
    "permission",
    "role_permission",
    "user_role",
    "api_client",
    # Step 5C.7D. One outstanding claim on a provisioned account: knowing an email
    # address must not be enough to claim an identity. Only a token hash is stored.
    "credential_enrollment",
)

INFRASTRUCTURE_TABLES: tuple[str, ...] = (
    "outbox_message",
    "resolution_candidate",
    "user_session",
    # Mutable working state for in-flight fetches (Step 3.5). Separate from the
    # immutable `fetch_run` record so Celery progress never mutates history.
    "fetch_attempt",
    # Single-row serialisation pointer for the audit chain. No runtime role holds
    # privileges on it: the maintaining trigger is SECURITY DEFINER.
    "audit_chain_head",
    # How long a *host* is owed before we ask it for anything again (Step 5B.2).
    # Infrastructure rather than a source table: it records a timing obligation we
    # took on, carries no judgement about any institution, and is discarded freely
    # once it expires.
    "host_cooldown",
)

#: Every table the privileges migration touches.
ALL_CLASSIFIED_TABLES: tuple[str, ...] = (
    IMMUTABLE_TABLES
    + CANONICAL_TABLES
    + REFERENCE_TABLES
    + GOVERNANCE_TABLES
    + SOURCE_TABLES
    + ONBOARDING_TABLES
    + PILOT_STAGING_TABLES
    + IDENTITY_TABLES
    + INFRASTRUCTURE_TABLES
)

#: Indexes that live in a migration rather than on a model.
#:
#: These belong to a *query*, not to a table's structure: trigram, full-text,
#: range-overlap and several partial indexes. Expressing them in SQL is markedly
#: clearer than through the SQLAlchemy shims, and some (operator classes, text-search
#: configurations) do not round-trip through autogenerate at all.
#:
#: Alembic is told to ignore them (see ``alembic/env.py``), so ``alembic check`` does
#: not propose dropping them on every run. The search-index migration asserts its own
#: DDL covers exactly this set, so the two cannot drift.
MIGRATION_OWNED_INDEXES: frozenset[str] = frozenset(
    {
        "ix_university_name_en_trgm",
        "ix_university_name_zh_trgm",
        "ix_program_name_en_trgm",
        "ix_program_name_zh_trgm",
        "ix_entity_alias_value_trgm",
        "ix_program_name_en_fts",
        "ix_university_name_en_fts",
        "ix_application_deadline_cal_range",
        "ix_application_deadline_instant_utc",
        "ix_application_deadline_calendar_parts",
        "ix_application_deadline_open_ended",
        "ix_change_proposal_pending_sla",
        "ix_review_task_open_assignee",
        "ix_fetch_run_failures",
        # Partial: "is anything in cooldown" is a question about a handful of rows out
        # of 319, and autogenerate does not round-trip the WHERE clause (Step 5B.2).
        "ix_source_cooldown_until",
        # Partial: one successful-or-partial extraction result per version, with
        # FAILED excluded so a transient failure can be retried (Step 5C.2 §0).
        "uq_extraction_result_per_version",
        "ix_field_provenance_root_lookup",
        "ix_field_provenance_field_history",
        "ix_entity_version_root_desc",
        "ix_change_event_feed",
        "ix_change_event_high_risk_feed",
        # Step 4: partial indexes serving the onboarding worklist, plus the partial
        # unique index that gives a host at most one trusted owner.
        "ix_target_institution_current_scope",
        "ix_official_domain_candidates",
        "ix_official_domain_one_trusted_owner_per_host",
        "ix_source_mapping_collectable",
        # U15: the verification queue reads only what is still undecided.
        "ix_pilot_collected_source_pending",
        # Step 5A: one physical page per URL per institution. Partial, because the
        # duplicate rows are the other responsibilities that page carries.
        "ix_pilot_collected_source_physical",
    }
)

#: Tables carrying an immutable ``canonical_id`` guarded by trigger (B6).
CANONICAL_ID_TABLES: tuple[str, ...] = ("university", "program", "program_offering")

RUNTIME_ROLES: tuple[str, ...] = ("app_api", "app_worker", "app_publisher")


__all__ = [
    "ALL_CLASSIFIED_TABLES",
    "CANONICAL_ID_TABLES",
    "CANONICAL_TABLES",
    "GOVERNANCE_TABLES",
    "IDENTITY_TABLES",
    "IMMUTABLE_TABLES",
    "INFRASTRUCTURE_TABLES",
    "MIGRATION_OWNED_INDEXES",
    "ONBOARDING_TABLES",
    "PILOT_STAGING_TABLES",
    "REFERENCE_TABLES",
    "RUNTIME_ROLES",
    "SOURCE_TABLES",
]
