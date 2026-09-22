"""Source verification safety: what may and may not confer trust (Step 5C.5 section 21).

WHY THESE SIXTEEN
=================
The whole point of this step is that a source becomes publishable only when a person
says so. Every test here is one way that could quietly stop being true -- a page that
returns 200, a title that reads like a university, a `.edu` hostname, a redirect into a
sibling subdomain, a verified domain taken to mean every page under it is verified for
everything.

Each of those is a plausible shortcut and each would be wrong in the same way: it
substitutes a fact about *the network* for a judgement about *the institution*. The
suite exists because "we would never do that" is not an invariant.

WHERE THE POSITIVE CASES ARE
============================
`test_publication_eligibility.py` builds the complete legitimate chain and proves it
publishes. Several of the sixteen are already covered there in their strongest form and
are exercised here against the same fixture rather than restated -- a refusal-only suite
proves nothing if the happy path is also refused.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import Connection, text

from tests.integration.conftest import expect_violation

# `chain` is a fixture. pytest resolves fixtures by name in the requesting module's
# namespace, so it has to be imported here even though nothing calls it directly.
from tests.integration.test_publication_eligibility import (  # noqa: F401
    Chain,
    _build_chain,
    _claim,
    chain,
    scalar,
    sha,
)

# ruff: noqa: F811 -- importing a pytest fixture and then naming it as a test
# parameter is how fixture reuse across modules works.

pytestmark = pytest.mark.integration


# ===========================================================================
# helpers
# ===========================================================================


def _bare_source(conn: Connection, *, url: str, title_evidence: bool = False) -> uuid.UUID:
    """A registered source with no mapping and no verified domain behind it.

    This is what all 319 pilot sources are today: acquired, readable, and vouched for
    by nothing.
    """
    source_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:id, :url, :hash, 'university_site', 'MONTHLY', "
            "'STATIC')"
        ),
        {"id": source_id, "url": url, "hash": sha(url + str(source_id))},
    )
    return source_id


def _mapping(
    conn: Connection,
    chain: Chain,
    *,
    category: str,
    status: str = "CANDIDATE",
    source_id: uuid.UUID | None = None,
    url: str | None = None,
) -> uuid.UUID:
    """A second mapping on the same verified host, for a different responsibility."""
    mapping_id = uuid.uuid4()
    url = url or f"https://northgate.ac.uk/{category.lower()}-{mapping_id.hex[:6]}"
    verified = status in ("VERIFIED_OFFICIAL", "AUTHORIZED_EXTERNAL")
    conn.execute(
        text(
            "INSERT INTO source_mapping (id, target_institution_id, source_category, "
            "url, normalized_url, url_sha256, host, official_domain_id, "
            "verification_status, verified_at, verified_by, promoted_source_id) "
            "VALUES (:id, :target, :cat, :url, :url, :hash, 'northgate.ac.uk', "
            ":domain, :status, :at, :by, :src)"
        ),
        {
            "id": mapping_id,
            "target": chain.target,
            "cat": category,
            "url": url,
            "hash": sha(url),
            "domain": chain.domain,
            "status": status,
            "at": "now()" if verified else None,
            "by": chain.actor if verified else None,
            "src": source_id,
        },
    )
    return mapping_id


# ===========================================================================
# 1-3. Nothing about the network verifies anything
# ===========================================================================


def test_http_200_does_not_verify_a_source(conn: Connection, chain: Chain) -> None:
    """Section 15. A successful fetch means a server answered. That is all it means.

    175 of the 319 pilot pages fetched successfully, which is the most available and
    most useless signal there is.
    """
    source_id = _bare_source(conn, url="https://plausible.ac.uk/admissions")
    attempt, run, snapshot = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    conn.execute(
        text("INSERT INTO fetch_attempt (id, source_id, cycle_key) VALUES (:i, :s, :c)"),
        {"i": attempt, "s": source_id, "c": attempt.hex[:10]},
    )
    conn.execute(
        text(
            "INSERT INTO fetch_run (id, source_id, attempt_id, attempt_no, status, "
            "http_status, fetcher, started_at) VALUES (:i, :s, :a, 1, 'OK', 200, "
            "'STATIC', now())"
        ),
        {"i": run, "s": source_id, "a": attempt},
    )
    conn.execute(
        text(
            "INSERT INTO content_blob (content_hash, byte_size, storage_key, content_type, "
            "first_observed_at) VALUES (:h, 10, :k, 'text/html', now()) "
            "ON CONFLICT DO NOTHING"
        ),
        {"h": sha(str(snapshot)), "k": f"blob/{snapshot.hex}"},
    )
    conn.execute(
        text(
            "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, "
            "requested_url, http_status, fetcher, observed_at) "
            "VALUES (:i, :r, :s, :h, 'https://plausible.ac.uk/admissions', 200, "
            "'STATIC', now())"
        ),
        {"i": snapshot, "r": run, "s": source_id, "h": sha(str(snapshot))},
    )

    assert (
        scalar(conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=source_id)
        == "NOT_ELIGIBLE"
    )
    with expect_violation(conn, "no promoted source_mapping vouches for it"):
        conn.execute(
            text("UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED' WHERE id = :i"),
            {"i": source_id},
        )


def test_an_official_looking_title_does_not_verify_a_source(conn: Connection) -> None:
    """Section 15. A page can say anything about itself.

    The stored title is the best single piece of review evidence there is and it is
    still only evidence: it is what the page claims, asserted by the page.
    """
    source_id = _bare_source(conn, url="https://definitely-real-university.example/")
    with expect_violation(conn, "no promoted source_mapping vouches for it"):
        conn.execute(
            text("UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED' WHERE id = :i"),
            {"i": source_id},
        )


def test_an_edu_hostname_does_not_verify_a_source(conn: Connection) -> None:
    """Section 11. A TLD is a registrar's product, not a verification."""
    source_id = _bare_source(conn, url="https://not-actually-official.edu/tuition")
    with expect_violation(conn, "no promoted source_mapping vouches for it"):
        conn.execute(
            text("UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED' WHERE id = :i"),
            {"i": source_id},
        )


# ===========================================================================
# 4-6. Domain, page and responsibility are three separate decisions
# ===========================================================================


def test_a_verified_domain_does_not_verify_every_responsibility(
    conn: Connection, chain: Chain
) -> None:
    """Section 4. An official admissions page is not thereby a tuition source.

    The host is verified and stays verified. A second responsibility on the same host
    is a separate `source_mapping`, and until somebody verifies *that*, its derived
    eligibility is `NOT_ELIGIBLE` -- computed by the database, not asserted here.
    """
    assert (
        scalar(
            conn,
            "SELECT verification_status::text FROM official_domain WHERE id = :i",
            i=chain.domain,
        )
        == "VERIFIED_OFFICIAL"
    )

    tuition = _mapping(conn, chain, category="TUITION_FEES", status="CANDIDATE")
    assert (
        scalar(conn, "SELECT publication_eligibility FROM source_mapping WHERE id = :i", i=tuition)
        == "NOT_ELIGIBLE"
    )

    # ...while the responsibility that WAS verified on the same host is eligible.
    assert (
        scalar(
            conn,
            "SELECT publication_eligibility FROM source_mapping WHERE id = :i",
            i=chain.mapping,
        )
        == "OFFICIAL_VERIFIED"
    )


def test_a_verified_responsibility_on_an_unverified_domain_is_not_eligible(
    conn: Connection, chain: Chain
) -> None:
    """Section 16. Responsibility trust does not substitute for domain trust."""
    unverified = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, "
            "verification_status) VALUES (:i, :t, :h, 'CANDIDATE')"
        ),
        {"i": unverified, "t": chain.target, "h": f"unverified-{unverified.hex[:8]}.ac.uk"},
    )
    url = f"https://unverified-{unverified.hex[:8]}.ac.uk/fees"
    with expect_violation(conn, "trusted|verified|host"):
        conn.execute(
            text(
                "INSERT INTO source_mapping (id, target_institution_id, source_category, "
                "url, normalized_url, url_sha256, host, official_domain_id, "
                "verification_status, verified_at, verified_by) "
                "VALUES (:i, :t, 'TUITION_FEES', :u, :u, :h, :host, :d, "
                "'VERIFIED_OFFICIAL', now(), :by)"
            ),
            {
                "i": uuid.uuid4(),
                "t": chain.target,
                "u": url,
                "h": sha(url),
                "host": f"unverified-{unverified.hex[:8]}.ac.uk",
                "d": unverified,
                "by": chain.actor,
            },
        )


def test_a_verified_domain_and_responsibility_yield_eligibility(
    conn: Connection, chain: Chain
) -> None:
    """Section 16, the positive case. Without this the refusals above prove nothing."""
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "OFFICIAL_VERIFIED"
    )
    claim_id = _claim(conn, chain)
    assert scalar(conn, "SELECT count(*) FROM field_claim WHERE id = :i", i=claim_id) == 1


def test_the_authorized_external_scope_is_tested_where_its_fixtures_live() -> None:
    """Section 21.7 lives in `test_publication_eligibility.py`, not restated here.

    That module owns the `AUTHORIZED_EXTERNAL` chain and tests the scope in both
    directions. A third, thinner copy here would only add a way for the suite to
    disagree with itself.
    """
    from tests.integration import test_publication_eligibility as owner

    for name in (
        "test_an_authorized_external_source_is_refused_outside_its_scope",
        "test_an_authorized_external_source_publishes_within_its_scope",
    ):
        assert hasattr(owner, name), name


# ===========================================================================
# 7-10. Withdrawal
# ===========================================================================


def test_revocation_stops_future_publication(conn: Connection, chain: Chain) -> None:
    """Section 17, and the gap revision `b3c4d5e6f7a8` closed.

    `source.publication_eligibility` is a stored copy of a decision two tables away.
    Nothing re-checked it after it was set, so rejecting a host left every source under
    it still reading `OFFICIAL_VERIFIED` and still publishing.
    """
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "OFFICIAL_VERIFIED"
    )

    conn.execute(
        text(
            "UPDATE official_domain SET verification_status = 'REJECTED', "
            "rejected_reason = 'the registrar confirmed this host is not ours', "
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
        _claim(conn, chain, field_path="name_zh", value='"\\u5317\\u95e8"')


def test_deactivating_a_mapping_also_withdraws_eligibility(conn: Connection, chain: Chain) -> None:
    """The same withdrawal, one table lower. A page can be retired without the host."""
    conn.execute(
        text(
            "UPDATE source_mapping SET is_active = false, "
            "deactivated_reason = 'page retired by the institution' WHERE id = :i"
        ),
        {"i": chain.mapping},
    )
    assert (
        scalar(
            conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=chain.source
        )
        == "NOT_ELIGIBLE"
    )


def test_revocation_deletes_no_evidence_and_no_history(conn: Connection, chain: Chain) -> None:
    """Section 17. What stops is the future; the record of what was believed stays.

    Rewriting history to match a later decision would destroy exactly what an audit
    needs: what was believed, on what evidence, and when.
    """
    published = _claim(conn, chain)
    before = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("snapshot", "extraction", "fetch_run", "field_claim", "content_blob")
    }

    conn.execute(
        text(
            "UPDATE official_domain SET verification_status = 'REJECTED', "
            "rejected_reason = 'withdrawn after review', is_active = false WHERE id = :i"
        ),
        {"i": chain.domain},
    )

    after = {
        table: scalar(conn, f"SELECT count(*) FROM {table}")
        for table in ("snapshot", "extraction", "fetch_run", "field_claim", "content_blob")
    }
    assert after == before, "revocation removed evidence or history"
    assert scalar(conn, "SELECT count(*) FROM field_claim WHERE id = :i", i=published) == 1
    # The rejected domain keeps saying why, which is the auditable part.
    assert (
        scalar(conn, "SELECT rejected_reason FROM official_domain WHERE id = :i", i=chain.domain)
        == "withdrawn after review"
    )


def test_a_rejected_responsibility_stays_on_the_record(conn: Connection, chain: Chain) -> None:
    """Section 13. A rejection is a decision, not an absence, and it is kept."""
    rejected = _mapping(conn, chain, category="TUITION_FEES", status="CANDIDATE")
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'REJECTED', "
            "rejected_reason = 'this page links to fees, it does not state them', "
            "deactivated_reason = 'rejected for this responsibility', "
            "verified_at = now(), verified_by = :by, is_active = false WHERE id = :i"
        ),
        {"i": rejected, "by": chain.actor},
    )
    row = conn.execute(
        text(
            "SELECT verification_status::text AS status, rejected_reason, "
            "publication_eligibility FROM source_mapping WHERE id = :i"
        ),
        {"i": rejected},
    ).one()
    assert row.status == "REJECTED"
    assert row.rejected_reason
    assert row.publication_eligibility == "NOT_ELIGIBLE"


# ===========================================================================
# 11-14. Redirects, classification, mixed decisions, replacement
# ===========================================================================


def test_a_redirect_target_host_does_not_inherit_verification(
    conn: Connection, chain: Chain
) -> None:
    """Section 19. Fifteen pilot pages ended on a host they did not request.

    Every one stayed inside the same registrable domain, which is the most
    persuasive-looking case there is. `study.northgate.ac.uk` is still a different host
    from `northgate.ac.uk` and needs its own row.
    """
    sibling = "study.northgate.ac.uk"
    assert (
        scalar(conn, "SELECT count(*) FROM official_domain WHERE host = :h", h=sibling) == 0
    ), "the sibling host must not exist merely because its parent is verified"

    url = f"https://{sibling}/apply"
    with expect_violation(conn, "trusted|verified|host|domain"):
        conn.execute(
            text(
                "INSERT INTO source_mapping (id, target_institution_id, source_category, "
                "url, normalized_url, url_sha256, host, verification_status, "
                "verified_at, verified_by) VALUES (:i, :t, 'UNDERGRADUATE_ADMISSIONS', "
                ":u, :u, :h, :host, 'VERIFIED_OFFICIAL', now(), :by)"
            ),
            {
                "i": uuid.uuid4(),
                "t": chain.target,
                "u": url,
                "h": sha(url),
                "host": sibling,
                "by": chain.actor,
            },
        )


def test_an_unclassified_source_stays_ineligible(conn: Connection) -> None:
    """Section 15. An official host does not classify the pages on it.

    A source with no category has nothing to be authoritative *for*, so there is
    nothing a reviewer could have verified.
    """
    source_id = _bare_source(conn, url="https://northgate.ac.uk/unsorted-page")
    conn.execute(
        text("UPDATE source SET source_type = 'unclassified' WHERE id = :i"), {"i": source_id}
    )
    assert (
        scalar(conn, "SELECT publication_eligibility::text FROM source WHERE id = :i", i=source_id)
        == "NOT_ELIGIBLE"
    )
    with expect_violation(conn, "no promoted source_mapping vouches for it"):
        conn.execute(
            text("UPDATE source SET publication_eligibility = 'OFFICIAL_VERIFIED' WHERE id = :i"),
            {"i": source_id},
        )


def test_one_url_can_have_one_responsibility_verified_and_another_rejected(
    conn: Connection, chain: Chain
) -> None:
    """Section 13. Do not discard the page because one responsibility is rejected.

    This is the workbook's real shape: 385 responsibility claims over 319 URLs, and a
    page claimed for both language requirements and tuition may honestly carry one.
    """
    url = "https://northgate.ac.uk/admissions/english-language"
    language = _mapping(conn, chain, category="LANGUAGE_REQUIREMENTS", url=url)
    tuition = _mapping(conn, chain, category="TUITION_FEES", url=url)

    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'VERIFIED_OFFICIAL', "
            "verified_at = now(), verified_by = :by WHERE id = :i"
        ),
        {"i": language, "by": chain.actor},
    )
    conn.execute(
        text(
            "UPDATE source_mapping SET verification_status = 'REJECTED', "
            "rejected_reason = 'the page states no fees', "
            "deactivated_reason = 'rejected for this responsibility', "
            "verified_at = now(), verified_by = :by, is_active = false WHERE id = :i"
        ),
        {"i": tuition, "by": chain.actor},
    )

    assert (
        scalar(conn, "SELECT publication_eligibility FROM source_mapping WHERE id = :i", i=language)
        == "OFFICIAL_VERIFIED"
    )
    assert (
        scalar(conn, "SELECT publication_eligibility FROM source_mapping WHERE id = :i", i=tuition)
        == "NOT_ELIGIBLE"
    )
    assert (
        scalar(conn, "SELECT count(*) FROM source_mapping WHERE url = :u", u=url) == 2
    ), "the rejected responsibility must survive as a decision"


def test_replacing_a_dead_url_preserves_the_old_source_and_its_history(
    conn: Connection, chain: Chain
) -> None:
    """Section 18. The old row is never edited to point at the new URL.

    Its snapshots were fetched from the old URL. Repointing it would make the stored
    evidence say it came from somewhere it did not, which is the one thing an evidence
    plane must never do.
    """
    old_url = scalar(conn, "SELECT url FROM source WHERE id = :i", i=chain.source)
    old_hash = scalar(conn, "SELECT url_hash FROM source WHERE id = :i", i=chain.source)
    snapshots = scalar(conn, "SELECT count(*) FROM snapshot WHERE source_id = :i", i=chain.source)

    replacement = _bare_source(conn, url="https://northgate.ac.uk/admissions/2027")
    conn.execute(
        text(
            "UPDATE source SET is_active = false, "
            "deactivated_reason = 'the page 404s; replaced by the 2027 URL', "
            "superseded_by_source_id = :new WHERE id = :old"
        ),
        {"new": replacement, "old": chain.source},
    )

    row = conn.execute(
        text(
            "SELECT url, url_hash, superseded_by_source_id, is_active "
            "  FROM source WHERE id = :i"
        ),
        {"i": chain.source},
    ).one()
    assert row.url == old_url, "the old source's URL was rewritten"
    assert row.url_hash == old_hash
    assert row.superseded_by_source_id == replacement
    assert row.is_active is False
    assert (
        scalar(conn, "SELECT count(*) FROM snapshot WHERE source_id = :i", i=chain.source)
        == snapshots
    ), "replacing the URL removed the old acquisition history"


def test_a_source_cannot_supersede_itself_or_stay_active(conn: Connection, chain: Chain) -> None:
    """A replaced source that is still fetched is two sources for one thing."""
    with expect_violation(conn, "not_its_own_successor"):
        conn.execute(
            text("UPDATE source SET superseded_by_source_id = id WHERE id = :i"),
            {"i": chain.source},
        )
    replacement = _bare_source(conn, url="https://northgate.ac.uk/successor")
    with expect_violation(conn, "superseded_source_is_inactive"):
        conn.execute(
            text("UPDATE source SET superseded_by_source_id = :new WHERE id = :old"),
            {"new": replacement, "old": chain.source},
        )


# ===========================================================================
# 15-16. Who may decide, and whether the decision is recorded
# ===========================================================================


def test_no_runtime_role_can_produce_an_eligible_source(role_engines: dict[str, Any]) -> None:
    """Section 22. Two different refusals, and it matters which is which.

    `app_api` may INSERT a source -- registering one is its job -- so what stops it
    is `source_eligibility_is_earned`: no promoted mapping vouches for the row, and
    the trigger refuses it for every role including the table's owner.

    `app_worker` never gets that far. It holds SELECT and UPDATE on `source` (it
    needs UPDATE for cooldowns and rate-limit strikes) and no INSERT at all, and on
    `official_domain` it holds SELECT alone -- so it cannot create a source, and it
    cannot create the verified domain that would vouch for one.

    Everything runs inside each role's own transaction and is rolled back, because
    the `conn` fixture's transaction is invisible to these connections and a
    committed fixture row would outlive the test.
    """
    statement = (
        "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
        "fetch_strategy, publication_eligibility) "
        "VALUES (gen_random_uuid(), :url, :hash, 'university_site', 'MONTHLY', "
        "'STATIC', 'OFFICIAL_VERIFIED')"
    )
    expected = {
        "app_api": "vouches for it",
        "app_worker": "permission denied",
    }
    for role, fragment in expected.items():
        with role_engines[role].connect() as connection:
            transaction = connection.begin()
            try:
                with pytest.raises(Exception) as excinfo:
                    connection.execute(
                        text(statement),
                        {
                            "url": f"https://role-{role}.ac.uk/",
                            "hash": sha(f"role-{role}"),
                        },
                    )
                assert (
                    fragment in str(excinfo.value).lower()
                ), f"{role} was refused for the wrong reason: {excinfo.value}"
            finally:
                transaction.rollback()

    # And the worker cannot create the domain that would vouch for anything.
    with role_engines["app_worker"].connect() as connection:
        transaction = connection.begin()
        try:
            with pytest.raises(Exception) as excinfo:
                connection.execute(
                    text(
                        "INSERT INTO official_domain (id, host, verification_status) "
                        "VALUES (gen_random_uuid(), 'worker-invented.ac.uk', "
                        "'VERIFIED_OFFICIAL')"
                    )
                )
            assert "permission denied" in str(excinfo.value).lower(), excinfo.value
        finally:
            transaction.rollback()


def test_a_verification_decision_is_recorded_with_actor_method_and_time(
    conn: Connection, chain: Chain
) -> None:
    """Sections 10 and 17. A verification with no actor is not a verification.

    Asserted against the database rather than the service layer, because a service is
    one caller and a constraint is every caller.
    """
    row = conn.execute(
        text(
            "SELECT verification_status::text AS status, verification_method::text AS method, "
            "       verified_by, verified_at FROM official_domain WHERE id = :i"
        ),
        {"i": chain.domain},
    ).one()
    assert row.status == "VERIFIED_OFFICIAL"
    assert row.method and row.verified_by and row.verified_at

    # The same row could not have been written without them.
    with expect_violation(conn, "records_its_basis"):
        conn.execute(
            text(
                "INSERT INTO official_domain (id, target_institution_id, host, "
                "verification_status) VALUES (:i, :t, 'no-basis.ac.uk', 'VERIFIED_OFFICIAL')"
            ),
            {"i": uuid.uuid4(), "t": chain.target},
        )
    # ...and a rejection must say why.
    with expect_violation(conn, "rejected_domain_has_a_reason"):
        conn.execute(
            text(
                "INSERT INTO official_domain (id, target_institution_id, host, "
                "verification_status, is_active) "
                "VALUES (:i, :t, 'no-reason.ac.uk', 'REJECTED', false)"
            ),
            {"i": uuid.uuid4(), "t": chain.target},
        )


def test_the_audit_chain_is_append_only_and_hashed(conn: Connection, chain: Chain) -> None:
    """Section 21.16. A decision log that can be edited is not a decision log.

    A row is appended first, deliberately: an `UPDATE` matching no row fires no
    trigger and raises nothing, so a test written against an empty table would
    report the log immutable without ever having tried to change one.
    """
    entry = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO audit_log (id, actor_type, actor_id, action, object_type, "
            "object_id, reason) VALUES (:i, 'USER', :actor, 'DOMAIN_VERIFIED', "
            "'official_domain', :obj, 'the registrar lists this host')"
        ),
        {"i": entry, "actor": chain.actor, "obj": chain.domain},
    )
    row = conn.execute(
        text("SELECT seq, row_hash FROM audit_log WHERE id = :i"), {"i": entry}
    ).one()
    assert row.seq and row.row_hash, "the chain trigger did not stamp the row"

    with expect_violation(conn, "append-only|immutable|cannot be|forbid|not allowed"):
        conn.execute(
            text("UPDATE audit_log SET reason = 'rewritten' WHERE id = :i"),
            {"i": entry},
        )
    with expect_violation(conn, "append-only|immutable|cannot be|forbid|not allowed"):
        conn.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": entry})

    after = conn.execute(
        text("SELECT reason, row_hash FROM audit_log WHERE id = :i"), {"i": entry}
    ).one()
    assert after.reason == "the registrar lists this host"
    assert after.row_hash == row.row_hash
