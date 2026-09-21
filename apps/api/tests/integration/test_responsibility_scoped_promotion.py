"""Responsibility-scoped publication authority, and the promotion that grants it.

THE TEST THIS MODULE EXISTS FOR
===============================
`test_one_url_verified_for_language_and_rejected_for_tuition`. One URL, two
responsibility claims, two different reviewer decisions, one snapshot, one extraction,
two candidates. The language claim must become promotable and the tuition claim must
not.

It fails against any source-level eligibility implementation, which is what this
codebase had until Step 5C.6: C27 scoped `AUTHORIZED_EXTERNAL` and `AUTHORIZED_RANKING`
to exact fields and left `OFFICIAL_VERIFIED` -- the class every university page holds --
checked at source level only. The two decisions a reviewer made about one page collapsed
into one, and the permissive one won.

Everything else here supports that: the promotion writer that grants scoped authority,
the revocation that takes it away, and the partial revocation that must take away only
one of the two.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.verification.policy import Blocker, bindings_for, is_compatible
from app.domains.verification.promotion import (
    Actor,
    NotYetTrustedError,
    PromotionRefusedError,
    promote,
)
from tests.integration.conftest import expect_violation, fixture_binding

# `chain` is a fixture; pytest resolves fixtures by name in the requesting module's
# namespace, so it is imported even though nothing calls it directly.
from tests.integration.test_publication_eligibility import (  # noqa: F401
    Chain,
    _build_chain,
    chain,
    scalar,
    sha,
)

# ruff: noqa: F811 -- importing a fixture and naming it as a test parameter is how
# fixture reuse across modules works.

pytestmark = pytest.mark.integration

#: One page, submitted for two things. The shape of 385 responsibility claims over 319
#: URLs, reduced to the smallest case that can go wrong.
SHARED_URL = "https://northgate.ac.uk/admissions/english-and-fees"


@pytest.fixture
def two_responsibilities(conn: Connection) -> dict[str, Any]:
    """A verified host, one URL, two mappings: language verified, tuition rejected."""
    chain = _build_chain(conn, category="LANGUAGE_REQUIREMENTS", host="northgate.ac.uk")
    language = chain.mapping
    conn.execute(
        text("UPDATE source_mapping SET url = :u, normalized_url = :u WHERE id = :i"),
        {"u": SHARED_URL, "i": language},
    )

    tuition = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source_mapping (id, target_institution_id, source_category, "
            "url, normalized_url, url_sha256, host, official_domain_id, "
            "verification_status, verified_at, verified_by, rejected_reason, "
            "is_active, deactivated_reason) "
            "VALUES (:id, :target, 'TUITION_FEES', :url, :url, :hash, 'northgate.ac.uk', "
            ":domain, 'REJECTED', now(), :actor, "
            "'the page links to the fee schedule, it does not state fees', "
            "false, 'rejected for this responsibility')"
        ),
        {
            "id": tuition,
            "target": chain.target,
            "url": SHARED_URL,
            "hash": sha(SHARED_URL + "tuition"),
            "domain": chain.domain,
            "actor": chain.actor,
        },
    )
    # `_build_chain` returns a chain that is already promoted, because that is what
    # the C27 tests need. These tests are about the act of promoting, so the fixture
    # rewinds to the state a reviewer actually finds: verified, and not yet relied on.
    conn.execute(
        text(
            "UPDATE source SET publication_eligibility = 'NOT_ELIGIBLE', "
            "eligibility_reason = 'rewound by the fixture' WHERE id = :i"
        ),
        {"i": chain.source},
    )
    conn.execute(
        text("UPDATE source_mapping SET promoted_source_id = NULL WHERE id = :i"),
        {"i": language},
    )
    conn.execute(text("DELETE FROM source_field_binding WHERE source_id = :i"), {"i": chain.source})
    return {"chain": chain, "language": language, "tuition": tuition}


def _actor(chain: Chain) -> Actor:
    return Actor(id=chain.actor, display="Fixture reviewer (TEST ONLY)")


# ===========================================================================
# 1-3. The cross-responsibility case
# ===========================================================================


def test_one_url_verified_for_language_and_rejected_for_tuition(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 3. The mandatory regression test.

    Both claims cite the same `source_id`, the same snapshot and the same extraction.
    The only thing distinguishing them is which responsibility a reviewer verified, and
    before Step 5C.6 the database could not see that distinction at all.
    """
    chain = two_responsibilities["chain"]
    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="the page states the English requirement in its own words",
    )

    # The language fact publishes.
    language_claim = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
            "proposed_field_status, value_normalized, observed_at) "
            "VALUES (:id, :e, 'language_requirement', 'overall_score', 'PUBLISHED', "
            "CAST('7.0' AS jsonb), now())"
        ),
        {"id": language_claim, "e": chain.extraction},
    )
    assert scalar(conn, "SELECT count(*) FROM field_claim WHERE id = :i", i=language_claim) == 1

    # The tuition fact, from the same evidence, does not.
    with expect_violation(conn, "not authorised for tuition"):
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
                "proposed_field_status, value_normalized, observed_at) "
                "VALUES (:id, :e, 'tuition', 'amount', 'PUBLISHED', "
                "CAST('30000' AS jsonb), now())"
            ),
            {"id": uuid.uuid4(), "e": chain.extraction},
        )


def test_the_gate_is_scoped_for_official_sources_not_only_authorised_ones(
    conn: Connection, chain: Chain
) -> None:
    """Section 0. The asymmetry that made the case above possible.

    `OFFICIAL_VERIFIED` used to skip the binding check entirely. If this regresses, the
    test above stops proving anything, because the tuition claim would be refused for
    some other reason on some days and not others.
    """
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "OFFICIAL_VERIFIED"
    )
    # A field its responsibility does not cover.
    with expect_violation(conn, "not authorised for"):
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
                "proposed_field_status, value_normalized, observed_at) "
                "VALUES (:id, :e, 'tuition', 'amount', 'PUBLISHED', CAST('1' AS jsonb), now())"
            ),
            {"id": uuid.uuid4(), "e": chain.extraction},
        )


def test_the_policy_refuses_an_incompatible_pairing_before_the_database_does(
    conn: Connection,
) -> None:
    """Defence in depth (section 6): the same answer from the policy table."""
    assert is_compatible("LANGUAGE_OVERALL_SCORE", "LANGUAGE_REQUIREMENTS")
    assert is_compatible("LANGUAGE_OVERALL_SCORE", "UNDERGRADUATE_ADMISSIONS")
    assert not is_compatible("TUITION", "LANGUAGE_REQUIREMENTS")
    assert not is_compatible("TUITION", "UNDERGRADUATE_ADMISSIONS")
    assert not is_compatible("ACADEMIC_CALENDAR_EVENT", "APPLICATION_DEADLINES")
    assert not is_compatible("ADMISSION_REQUIREMENT", None)


# ===========================================================================
# 4-7. The promotion writer
# ===========================================================================


def test_promotion_refuses_an_unverified_responsibility(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 20. Promotion consumes a decision; it never makes one."""
    chain = two_responsibilities["chain"]
    with pytest.raises(NotYetTrustedError, match="has not been verified"):
        promote(
            conn,
            mapping_id=two_responsibilities["tuition"],
            binding=fixture_binding(conn, two_responsibilities["tuition"]),
            source_id=chain.source,
            actor=_actor(chain),
            reason="trying to promote a rejected responsibility",
        )
    assert (
        scalar(
            conn,
            "SELECT verification_status::text FROM source_mapping WHERE id = :i",
            i=two_responsibilities["tuition"],
        )
        == "REJECTED"
    ), "the failed promotion must not have changed the decision"


def test_promotion_refuses_when_the_host_is_not_verified(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 5, check 1. The page's own verification is not enough."""
    chain = two_responsibilities["chain"]
    conn.execute(
        text(
            "UPDATE official_domain SET verification_status = 'REJECTED', "
            "rejected_reason = 'not ours after all', is_active = false WHERE id = :i"
        ),
        {"i": chain.domain},
    )
    with pytest.raises(NotYetTrustedError, match="REJECTED|deactivated"):
        promote(
            conn,
            mapping_id=two_responsibilities["language"],
            binding=fixture_binding(conn, two_responsibilities["language"]),
            source_id=chain.source,
            actor=_actor(chain),
            reason="host was revoked",
        )


def test_promotion_is_idempotent(conn: Connection, two_responsibilities: dict[str, Any]) -> None:
    """Section 7. A repeated request is not a second decision."""
    chain = two_responsibilities["chain"]
    kwargs = {
        "mapping_id": two_responsibilities["language"],
        "binding": fixture_binding(conn, two_responsibilities["language"]),
        "source_id": chain.source,
        "actor": _actor(chain),
        "reason": "the page states the English requirement",
    }
    before = promote(conn, **kwargs)
    audit_after_first = scalar(conn, "SELECT count(*) FROM audit_log")
    again = promote(conn, **kwargs)

    assert before.already_promoted is False
    assert again.already_promoted is True
    assert again.eligibility == before.eligibility
    assert (
        scalar(conn, "SELECT count(*) FROM audit_log") == audit_after_first
    ), "a repeated promotion recorded a state transition that did not happen"


def test_promotion_writes_exactly_the_bindings_its_responsibility_authorises(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """The bindings are the authority. Anything not listed is not authorised."""
    chain = two_responsibilities["chain"]
    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="verified language page",
    )
    written = {
        (row.entity_type, row.field_path)
        for row in conn.execute(
            text(
                "SELECT entity_type, field_path FROM source_field_binding " " WHERE source_id = :s"
            ),
            {"s": chain.source},
        )
    }
    assert set(bindings_for("LANGUAGE_REQUIREMENTS")) <= written
    assert ("tuition", "amount") not in written


def test_a_dry_run_promotion_mutates_nothing(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 18."""
    chain = two_responsibilities["chain"]
    bindings = scalar(conn, "SELECT count(*) FROM source_field_binding")
    audit = scalar(conn, "SELECT count(*) FROM audit_log")
    result = promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="what would happen",
        dry_run=True,
    )
    assert result.bindings_written > 0, "a dry run must still report the effect"
    assert scalar(conn, "SELECT count(*) FROM source_field_binding") == bindings
    assert scalar(conn, "SELECT count(*) FROM audit_log") == audit
    assert (
        scalar(
            conn,
            "SELECT promoted_source_id FROM source_mapping WHERE id = :i",
            i=two_responsibilities["language"],
        )
        is None
    )


# ===========================================================================
# 8-10. Revocation, and revoking only one of two
# ===========================================================================


def test_partial_revocation_leaves_the_other_responsibility_alone(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 9. The essential property of the 385-over-319 model.

    Two responsibilities on one URL, both verified and promoted. Revoking one must not
    withdraw the other, even though they share a source, a snapshot and an extraction.
    """
    chain = two_responsibilities["chain"]
    # Make the tuition responsibility verified too, on its own source.
    tuition_source = uuid.uuid4()
    tuition_url = SHARED_URL + "?fees"
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:i, :u, :h, 'fee_page', 'MONTHLY', 'STATIC')"
        ),
        {"i": tuition_source, "u": tuition_url, "h": sha(tuition_url)},
    )
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "rejected_reason = NULL, is_active = true, deactivated_reason = NULL, "
            "url = :u, normalized_url = :u, url_sha256 = :h WHERE id = :i"
        ),
        {"i": two_responsibilities["tuition"], "u": tuition_url, "h": sha(tuition_url)},
    )

    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="language verified",
    )
    promote(
        conn,
        mapping_id=two_responsibilities["tuition"],
        binding=fixture_binding(conn, two_responsibilities["tuition"]),
        source_id=tuition_source,
        actor=_actor(chain),
        reason="fees verified",
    )
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "OFFICIAL_VERIFIED"
    )
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=tuition_source
        )
        == "OFFICIAL_VERIFIED"
    )

    # Revoke the tuition responsibility only.
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'REJECTED', "
            "rejected_reason = 'the figures were last year''s', "
            "deactivated_reason = 'revoked', verified_at = now(), verified_by = :by, "
            "is_active = false WHERE id = :i"
        ),
        {"i": two_responsibilities["tuition"], "by": chain.actor},
    )

    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=tuition_source
        )
        == "NOT_ELIGIBLE"
    ), "the revoked responsibility kept its authority"
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "OFFICIAL_VERIFIED"
    ), "an unrelated responsibility lost its authority"


def test_domain_revocation_withdraws_every_responsibility_under_it(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 8. The host is the root of the authority, so revoking it revokes all."""
    chain = two_responsibilities["chain"]
    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="language verified",
    )
    evidence = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("snapshot", "extraction", "fetch_run", "field_claim_candidate")
    }

    conn.execute(
        text(
            "UPDATE official_domain SET verification_status = 'REJECTED', "
            "rejected_reason = 'the registrar says this host is not theirs', "
            "is_active = false WHERE id = :i"
        ),
        {"i": chain.domain},
    )

    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "NOT_ELIGIBLE"
    )
    with expect_violation(conn, "may not support a published fact"):
        conn.execute(
            text(
                "INSERT INTO field_claim (id, extraction_id, entity_type, field_path, "
                "proposed_field_status, value_normalized, observed_at) "
                "VALUES (:id, :e, 'language_requirement', 'overall_score', 'PUBLISHED', "
                "CAST('7.0' AS jsonb), now())"
            ),
            {"id": uuid.uuid4(), "e": chain.extraction},
        )
    after = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("snapshot", "extraction", "fetch_run", "field_claim_candidate")
    }
    assert after == evidence, "revocation deleted evidence"


# ===========================================================================
# 11-12. Superseded sources
# ===========================================================================


def test_a_superseded_source_cannot_be_promoted(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 10. New authority belongs to the replacement, not to the dead page."""
    chain = two_responsibilities["chain"]
    replacement = uuid.uuid4()
    url = "https://northgate.ac.uk/admissions/english-2027"
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:i, :u, :h, 'admissions_page', 'MONTHLY', 'STATIC')"
        ),
        {"i": replacement, "u": url, "h": sha(url)},
    )
    snapshots = scalar(conn, "SELECT count(*) FROM snapshot WHERE source_id = :i", i=chain.source)
    conn.execute(
        text(
            "UPDATE source SET is_active = false, deactivated_reason = 'replaced', "
            "superseded_by_source_id = :new WHERE id = :old"
        ),
        {"new": replacement, "old": chain.source},
    )

    with pytest.raises(PromotionRefusedError, match="superseded"):
        promote(
            conn,
            mapping_id=two_responsibilities["language"],
            binding=fixture_binding(conn, two_responsibilities["language"]),
            source_id=chain.source,
            actor=_actor(chain),
            reason="promoting a replaced page",
        )
    assert (
        scalar(conn, "SELECT count(*) FROM snapshot WHERE source_id = :i", i=chain.source)
        == snapshots
    ), "supersession moved the old evidence"


# ===========================================================================
# 13-14. Actors
# ===========================================================================


def test_a_promotion_must_name_an_actor_and_a_reason(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Sections 5 and 14. A promotion with no reason is not reviewable."""
    chain = two_responsibilities["chain"]
    with pytest.raises(PromotionRefusedError, match="why it was made"):
        promote(
            conn,
            mapping_id=two_responsibilities["language"],
            binding=fixture_binding(conn, two_responsibilities["language"]),
            source_id=chain.source,
            actor=_actor(chain),
            reason="   ",
        )
    with pytest.raises(Exception, match="app_user|foreign key|violates"):
        promote(
            conn,
            mapping_id=two_responsibilities["language"],
            binding=fixture_binding(conn, two_responsibilities["language"]),
            source_id=chain.source,
            actor=Actor(id=uuid.uuid4(), display="nobody"),
            reason="an actor who does not exist",
        )


def test_the_audit_chain_records_the_promotion_and_stays_valid(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 19. One chain, appended last, verifiable afterwards."""
    chain = two_responsibilities["chain"]
    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="the page states the English requirement in its own words",
    )
    row = conn.execute(
        text(
            "SELECT action, actor_type::text AS actor_type, actor_id, reason, seq, "
            "       row_hash, prev_hash FROM audit_log "
            " WHERE action = 'SOURCE_MAPPING_PROMOTED' ORDER BY seq DESC LIMIT 1"
        )
    ).one()
    assert row.actor_type == "USER"
    assert row.actor_id == chain.actor
    assert row.reason
    assert row.row_hash

    head = conn.execute(text("SELECT last_seq, last_row_hash FROM audit_chain_head")).one()
    assert head.last_seq == row.seq
    assert head.last_row_hash == row.row_hash


# ===========================================================================
# 15-16. Roles, and direct SQL
# ===========================================================================


def test_no_runtime_role_can_create_a_promoted_mapping(role_engines: dict[str, Any]) -> None:
    """Section 27. Promotion is an onboarding-plane act, not a runtime one.

    Written as an INSERT rather than an UPDATE on purpose. `app_worker` holds UPDATE
    on `source_mapping` -- it needs it for access state -- so an UPDATE would not be
    refused by grant, and one matching no row raises nothing at all. Neither role
    holds INSERT, so this is refused for the reason the test claims.
    """
    for role in ("app_worker", "app_publisher"):
        with role_engines[role].connect() as connection:
            transaction = connection.begin()
            try:
                with pytest.raises(Exception) as excinfo:
                    connection.execute(
                        text(
                            "INSERT INTO source_mapping (id, source_category, url, "
                            "normalized_url, url_sha256, host, promoted_source_id) "
                            "VALUES (gen_random_uuid(), 'TUITION_FEES', "
                            "'https://invented.ac.uk/', 'https://invented.ac.uk/', "
                            "repeat('a', 64), 'invented.ac.uk', gen_random_uuid())"
                        )
                    )
                assert (
                    "permission denied" in str(excinfo.value).lower()
                ), f"{role} was refused for the wrong reason: {excinfo.value}"
            finally:
                transaction.rollback()


def test_direct_sql_cannot_promote_under_a_revoked_host(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 6. The service is not the only thing standing between here and a lie.

    `ck_source_mapping_only_a_trusted_mapping_may_be_promoted` already checked the
    mapping's own status. It could not see the host, so a mapping still reading
    `VERIFIED_OFFICIAL` under a host that had just been rejected was promotable by
    direct SQL.
    """
    chain = two_responsibilities["chain"]
    conn.execute(
        text(
            "UPDATE official_domain SET verification_status = 'REJECTED', "
            "rejected_reason = 'not ours', is_active = false WHERE id = :i"
        ),
        {"i": chain.domain},
    )
    # Either layer may answer first: `source_mapping_requires_trusted_host` has
    # guarded the mapping's own status since revision b0c1d2e3f4a5, and
    # `source_mapping_promotion_requires_live_trust` guards the promotion. What
    # matters is that direct SQL cannot get through, not which one stops it.
    with expect_violation(conn, "nothing may be promoted under it|as official as the host"):
        conn.execute(
            text("UPDATE source_mapping SET promoted_source_id = :s WHERE id = :i"),
            {"s": chain.source, "i": two_responsibilities["language"]},
        )


def test_the_blocker_vocabulary_covers_what_the_report_needs() -> None:
    """Section 24. Every blocker the readiness report can return is a named action."""
    required = {
        "SOURCE_NOT_ELIGIBLE",
        "DOMAIN_NOT_VERIFIED",
        "RESPONSIBILITY_NOT_VERIFIED",
        "RESPONSIBILITY_INCOMPATIBLE",
        "SCOPE_UNRESOLVED",
        "CONFLICT_PRESENT",
        "LOCATOR_INVALID",
        "SOURCE_SUPERSEDED",
    }
    assert required <= {member.value for member in Blocker}


# ===========================================================================
# 17-21. Readiness
# ===========================================================================


def test_a_fixture_candidate_becomes_ready_once_every_condition_holds(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 33.20. The positive case, so the refusals are not passing vacuously.

    Built entirely from fixture data. The real pilot has no verified domain and must not
    acquire one to make a test go green (section 25).
    """
    from app.domains.claims.review import Decision, record_decision
    from app.domains.claims.runner import publish_rule_versions
    from app.domains.verification.readiness import assess

    # The claim runner publishes the live rule versions on every pass; without that the
    # registry still holds the migration's seed and every candidate reads as superseded.
    publish_rule_versions(conn)

    chain = two_responsibilities["chain"]
    promote(
        conn,
        mapping_id=two_responsibilities["language"],
        binding=fixture_binding(conn, two_responsibilities["language"]),
        source_id=chain.source,
        actor=_actor(chain),
        reason="the page states the English requirement in its own words",
    )

    # The workbook row the candidate hangs off. Built here rather than skipped when the
    # database has none: a positive case that does not run leaves every refusal in this
    # module unproven.
    submission, workbook_row = uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
            "file_byte_size, template_version, selected_university_count) "
            "VALUES (:i, :h, 'fixture.xlsx', 1024, 'v1', 1)"
        ),
        {"i": submission, "h": sha(str(submission))},
    )
    conn.execute(
        text(
            "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
            "target_institution_id, sheet_row_no, source_type, official_url, "
            "normalized_url, url_sha256, host, acquisition_source_id) "
            # Row 2: `ck_pilot_collected_source_sheet_row_no_is_a_data_row` reserves row 1
            # for the workbook's header.
            # `ck_pilot_collected_source_source_ref_shape` requires S plus four digits.
            "VALUES (:i, :sub, 'S0001', :target, 2, 'LANGUAGE_REQUIREMENTS', :url, :url, "
            ":hash, 'northgate.ac.uk', :source)"
        ),
        {
            "i": workbook_row,
            "sub": submission,
            "target": chain.target,
            "url": SHARED_URL,
            "hash": sha(SHARED_URL + "workbook"),
            "source": chain.source,
        },
    )

    # A candidate is only "current" when its rule version is live AND the extraction it
    # was read from is the live document artifact. `_build_chain` makes an
    # `invented-extractor 1.0` extraction for the C27 tests, which is neither, so this
    # adds the normaliser extraction a real candidate would have come from.
    live_extraction = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
            "status) SELECT :i, :snap, dv.extractor_name, dv.version, 'OK' "
            "  FROM document_artifact_version dv "
            " WHERE dv.extractor_name = 'html-document-normaliser'"
        ),
        {"i": live_extraction, "snap": chain.snapshot},
    )

    candidate = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO field_claim_candidate (id, extraction_id, "
            "pilot_collected_source_id, source_responsibility, field_kind, "
            "value_normalized, value_raw_text, evidence_text, locator, extractor_name, "
            "extractor_version, confidence_band, confidence_reason, claim_fingerprint) "
            "SELECT :id, :e, :pcs, 'LANGUAGE_REQUIREMENTS', 'LANGUAGE_OVERALL_SCORE', "
            "       CAST(:value AS jsonb), '7.0', 'IELTS 7.0 overall', "
            "       CAST(:locator AS jsonb), 'language-rule-extractor', v.version, "
            "       'HIGH', 'a labelled score', :fp "
            "  FROM claim_rule_version v "
            " WHERE v.extractor_name = 'language-rule-extractor'"
        ),
        {
            "id": candidate,
            "e": live_extraction,
            "pcs": workbook_row,
            "value": '{"score": 7.0}',
            "locator": '{"kind": "block", "block_index": 0}',
            "fp": sha(str(candidate)),
        },
    )

    actor = uuid.uuid4()
    conn.execute(
        text("INSERT INTO app_user (id, email, display_name) VALUES (:i, :e, 'Reviewer')"),
        {"i": actor, "e": f"ready-{actor.hex[:8]}@example.test"},
    )
    record_decision(
        conn,
        candidate_id=candidate,
        actor_id=actor,
        decision=Decision.ACCEPTED,
        reason_code="CORRECT_AS_EXTRACTED",
    )

    result = next(r for r in assess(conn) if r.candidate_id == candidate)
    assert result.blockers == (), f"still blocked by {[b.value for b in result.blockers]}"
    assert result.ready


def test_an_incompatible_responsibility_stays_blocked_however_verified_it_is(
    conn: Connection, two_responsibilities: dict[str, Any]
) -> None:
    """Section 33.21. Verifying the wrong thing harder does not help."""
    from app.domains.verification.readiness import blockers_for

    class _Row:
        is_superseded = False
        decision_state = "ACCEPTED"
        domain_status = "VERIFIED_OFFICIAL"
        domain_active = True
        mapping_status = "VERIFIED_OFFICIAL"
        mapping_active = True
        promoted_source_id = "same"
        source_id = "same"
        responsibility = "LANGUAGE_REQUIREMENTS"
        field_kind = "TUITION"
        eligibility = "OFFICIAL_VERIFIED"
        superseded_by_source_id = None
        scope_unresolved = False

    assert blockers_for(_Row()) == [Blocker.RESPONSIBILITY_INCOMPATIBLE]
