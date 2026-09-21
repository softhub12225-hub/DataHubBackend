"""C27: onboarding and QS records cannot masquerade as official source evidence.

WHY THESE TESTS LOOK LIKE THIS
==============================
Step 4 asserted that role separation prevented spreadsheet values from becoming
published facts. It was reasoned, not tested, and it was wrong. So every claim here
is exercised against the live database, as the role that would make the attempt, and
each negative test is paired with the positive case that proves it is not passing
vacuously.

The `chain` fixture builds a complete, legitimate evidence chain --
domain -> mapping -> source -> fetch -> snapshot -> extraction -> claim -> provenance
-> canonical row -- because a suite full of refusals proves nothing if the happy path
is also refused. If `test_the_legitimate_chain_publishes` fails, treat every other
result in this module as meaningless until it passes again.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.db.enums import EVIDENCE_ELIGIBLE_CLASSES, PublicationEligibility
from app.domains.verification.policy import bindings_for
from tests.integration.conftest import Graph, expect_violation

pytestmark = pytest.mark.integration


def scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar_one()


def sha(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


@dataclass
class Chain:
    """Ids of one complete legitimate evidence chain."""

    actor: uuid.UUID
    target: uuid.UUID
    domain: uuid.UUID
    mapping: uuid.UUID
    source: uuid.UUID
    snapshot: uuid.UUID
    extraction: uuid.UUID
    claim: uuid.UUID
    university: uuid.UUID


def _build_chain(
    conn: Connection,
    *,
    eligibility: str = PublicationEligibility.OFFICIAL_VERIFIED.value,
    category: str = "UNIVERSITY_HOME",
    host: str = "northgate.ac.uk",
    authorization: uuid.UUID | None = None,
) -> Chain:
    """Everything needed to publish one fact, honestly.

    Deliberately written out rather than hidden in helpers: the point of this module
    is what the chain requires, and a reader needs to see it.
    """
    ids = {
        name: uuid.uuid4()
        for name in (
            "actor",
            "list",
            "target",
            "domain",
            "mapping",
            "source",
            "attempt",
            "run",
            "snapshot",
            "extraction",
            "claim",
            "university",
        )
    }
    url = f"https://{host}/"
    url_hash = sha(url + str(ids["source"]))

    conn.execute(
        text(
            "INSERT INTO app_user (id, email, display_name) "
            "VALUES (:id, :email, 'Eligibility Reviewer')"
        ),
        {"id": ids["actor"], "email": f"c27-{ids['actor'].hex[:8]}@example.test"},
    )
    conn.execute(
        text(
            "INSERT INTO target_list (id, list_name, list_version, file_name, "
            "file_sha256, file_byte_size, sheet_name, imported_row_count) "
            "VALUES (:id, 'Invented List', :ver, 'invented.xlsx', :sha, 1024, 's', 1)"
        ),
        {"id": ids["list"], "ver": ids["list"].hex[:8], "sha": sha(str(ids["list"]))},
    )
    conn.execute(
        text(
            "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
            "latest_list_id, destination_code) "
            "VALUES (:id, :key, :list, :list, 'GB')"
        ),
        {"id": ids["target"], "key": f"northgate-{ids['target'].hex[:8]}", "list": ids["list"]},
    )
    # A verified host: a recorded human decision with a method and an actor.
    conn.execute(
        text(
            "INSERT INTO official_domain (id, target_institution_id, host, "
            "verification_status, verification_method, verification_evidence, "
            "verified_at, verified_by, covers_subdomains) "
            "VALUES (:id, :target, :host, 'VERIFIED_OFFICIAL', 'GOVERNMENT_REGISTRY', "
            "'listed in the invented national register', now(), :actor, true)"
        ),
        {"id": ids["domain"], "target": ids["target"], "host": host, "actor": ids["actor"]},
    )
    # PROMOTION ORDER, and it is forced by the design rather than incidental:
    #   1. the source exists, unclassified (NOT_ELIGIBLE)   -- because the mapping's
    #      promoted_source_id is a foreign key to it;
    #   2. the mapping is promoted, pointing at that source;
    #   3. only then is the source classified -- because `source_eligibility_is_earned`
    #      requires a promoted mapping to already vouch for it.
    # There is no ordering in which a source can be born classified.
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) "
            "VALUES (:id, :url, :hash, :stype, 'MONTHLY', 'STATIC')"
        ),
        {
            "id": ids["source"],
            "url": url,
            "hash": url_hash,
            "stype": (
                "authorized_ranking"
                if eligibility == PublicationEligibility.AUTHORIZED_RANKING.value
                else "university_site"
            ),
        },
    )
    conn.execute(
        text(
            "INSERT INTO source_mapping (id, target_institution_id, source_category, "
            "url, normalized_url, url_sha256, host, official_domain_id, "
            "verification_status, verified_at, verified_by, promoted_source_id) "
            "VALUES (:id, :target, :cat, :url, :url, :sha, :host, :domain, "
            "        :status, now(), :actor, :source)"
        ),
        {
            "id": ids["mapping"],
            "target": ids["target"],
            "cat": category,
            "url": url,
            "sha": url_hash,
            "host": host,
            "domain": ids["domain"],
            "status": (
                "AUTHORIZED_EXTERNAL"
                if eligibility == PublicationEligibility.AUTHORIZED_EXTERNAL.value
                else "VERIFIED_OFFICIAL"
            ),
            "actor": ids["actor"],
            "source": ids["source"],
        },
    )
    if eligibility != PublicationEligibility.NOT_ELIGIBLE.value:
        conn.execute(
            text(
                "UPDATE source SET publication_eligibility = :elig, "
                "  eligibility_authorization_id = :auth, eligibility_set_by = :actor, "
                "  eligibility_set_at = now(), "
                "  eligibility_reason = 'promoted from a verified mapping' "
                "WHERE id = :id"
            ),
            {
                "elig": eligibility,
                "auth": authorization,
                "actor": ids["actor"],
                "id": ids["source"],
            },
        )
    # The fetch that produced the observation.
    conn.execute(
        text(
            "INSERT INTO fetch_attempt (id, source_id, cycle_key) " "VALUES (:id, :source, :cycle)"
        ),
        {"id": ids["attempt"], "source": ids["source"], "cycle": ids["attempt"].hex[:12]},
    )
    conn.execute(
        text(
            "INSERT INTO fetch_run (id, source_id, attempt_id, started_at, status, fetcher) "
            "VALUES (:id, :source, :attempt, now(), 'OK', 'STATIC')"
        ),
        {"id": ids["run"], "source": ids["source"], "attempt": ids["attempt"]},
    )
    blob = sha(f"body-{ids['snapshot']}")
    conn.execute(
        text(
            "INSERT INTO content_blob (content_hash, storage_key, first_observed_at) "
            "VALUES (:hash, :key, now())"
        ),
        {"hash": blob, "key": f"blobs/{blob}"},
    )
    conn.execute(
        text(
            "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, "
            "observed_at, requested_url, fetcher) "
            "VALUES (:id, :run, :source, :hash, now(), :url, 'STATIC')"
        ),
        {
            "id": ids["snapshot"],
            "run": ids["run"],
            "source": ids["source"],
            "hash": blob,
            "url": url,
        },
    )
    conn.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, "
            "extractor_version, status) "
            "VALUES (:id, :snap, 'invented-extractor', '1.0', 'OK')"
        ),
        {"id": ids["extraction"], "snap": ids["snapshot"]},
    )
    # Step 5C.6: publication authority is scoped to a field, so an eligible source with
    # no `source_field_binding` rows may publish nothing. Promotion writes them from the
    # mapping's `source_category`; the fixture does the same, from the same policy table,
    # so the chain models a promoted source rather than an impossible one.
    for entity_type, field_path in bindings_for(category):
        conn.execute(
            text(
                "INSERT INTO source_field_binding (source_id, entity_type, field_path, "
                "responsibility) VALUES (:source, :entity, :field, 'PRIMARY') "
                "ON CONFLICT DO NOTHING"
            ),
            {"source": ids["source"], "entity": entity_type, "field": field_path},
        )

    return Chain(
        actor=ids["actor"],
        target=ids["target"],
        domain=ids["domain"],
        mapping=ids["mapping"],
        source=ids["source"],
        snapshot=ids["snapshot"],
        extraction=ids["extraction"],
        claim=ids["claim"],
        university=ids["university"],
    )


@pytest.fixture
def chain(conn: Connection) -> Chain:
    return _build_chain(conn)


def _claim(
    conn: Connection,
    chain: Chain,
    *,
    entity_type: str = "university",
    field_path: str = "name_en",
    value: str = '"Northgate Institute of Technology"',
    entity_id: uuid.UUID | None = None,
) -> uuid.UUID:
    claim_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO field_claim (id, extraction_id, entity_type, entity_id, "
            "field_path, proposed_field_status, value_normalized, observed_at) "
            "VALUES (:id, :extraction, :etype, :eid, :fpath, 'PUBLISHED', "
            "        CAST(:value AS jsonb), now())"
        ),
        {
            "id": claim_id,
            "extraction": chain.extraction,
            "etype": entity_type,
            "eid": entity_id,
            "fpath": field_path,
            "value": value,
        },
    )
    return claim_id


def _provenance(
    conn: Connection,
    chain: Chain,
    *,
    entity_id: uuid.UUID,
    entity_type: str = "university",
    field_path: str = "name_en",
    value: str | None = '"Northgate Institute of Technology"',
    field_status: str = "PUBLISHED",
    claim_id: uuid.UUID | None = None,
    cite_source: bool = True,
    cite_snapshot: bool = True,
) -> None:
    conn.execute(
        text(
            "INSERT INTO field_provenance (entity_type, entity_id, field_path, "
            "root_type, root_id, root_version_no, field_status, value, risk_level, "
            "source_id, snapshot_id, claim_id, published_at) "
            "VALUES (:etype, :eid, :fpath, 'university', :eid, 1, :status, "
            "        CAST(:value AS jsonb), 'LOW', :source, :snap, :claim, now())"
        ),
        {
            "etype": entity_type,
            "eid": entity_id,
            "fpath": field_path,
            "status": field_status,
            "value": value,
            "source": chain.source if cite_source else None,
            "snap": chain.snapshot if cite_snapshot else None,
            "claim": claim_id,
        },
    )


# ===========================================================================
# The happy path. If this breaks, nothing else in this module means anything.
# ===========================================================================


def test_the_legitimate_chain_publishes(conn: Connection, chain: Chain) -> None:
    """An official source, fetched, extracted, claimed and cited -- publishes.

    This is the non-vacuity guard for every refusal below.
    """
    name = '"Northgate Institute of Technology"'
    claim_id = _claim(conn, chain, value=name)
    conn.execute(
        text(
            "INSERT INTO university (id, canonical_id, destination_code, name_en) "
            "VALUES (:id, :cid, 'GB', 'Northgate Institute of Technology')"
        ),
        {"id": chain.university, "cid": f"northgate-{chain.university.hex[:8]}"},
    )
    _provenance(conn, chain, entity_id=chain.university, value=name, claim_id=claim_id)

    # The governed-column trigger is DEFERRED, so it runs here.
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    assert (
        scalar(conn, "SELECT name_en FROM university WHERE id = :id", id=chain.university)
        == "Northgate Institute of Technology"
    )


# ===========================================================================
# A target-scope source cannot be evidence
# ===========================================================================


def test_a_target_scope_only_source_cannot_support_a_published_fact(
    conn: Connection,
) -> None:
    """The rule the client asked for, stated as a refusal."""
    chain = _build_chain(conn, eligibility=PublicationEligibility.TARGET_SCOPE_ONLY.value)
    conn.execute(
        text(
            "INSERT INTO university (id, canonical_id, destination_code, name_en) "
            "VALUES (:id, :cid, 'GB', 'x')"
        ),
        {"id": chain.university, "cid": f"ts-{chain.university.hex[:8]}"},
    )
    with expect_violation(conn, "may not support a published fact"):
        _provenance(conn, chain, entity_id=chain.university, value='"x"')


def test_a_not_eligible_source_cannot_support_a_published_fact(
    conn: Connection,
) -> None:
    """The default class refuses too, so an unclassified source is inert."""
    chain = _build_chain(conn, eligibility=PublicationEligibility.NOT_ELIGIBLE.value)
    with expect_violation(conn, "may not support a published fact"):
        _provenance(conn, chain, entity_id=uuid.uuid4(), value='"x"')


def test_a_worker_cannot_claim_from_a_target_scope_source(conn: Connection) -> None:
    """The same rule on the worker side, so a claim cannot be staged for blessing."""
    chain = _build_chain(conn, eligibility=PublicationEligibility.TARGET_SCOPE_ONLY.value)
    with expect_violation(conn, "may not support a published fact"):
        _claim(conn, chain)


def test_target_list_tables_cannot_be_named_as_provenance(conn: Connection) -> None:
    """There is no FK path from the audit spine into the onboarding plane.

    `field_provenance` can only anchor to `source`, `snapshot` or `field_claim`, so a
    `target_list_entry` row is not merely refused as evidence -- it is unnameable.
    """
    onboarding = {
        "target_list",
        "target_list_entry",
        "target_institution",
        "target_list_diff",
        "official_domain",
        "source_mapping",
        "source_degree_scope",
        "source_discipline_scope",
    }
    rows = conn.execute(
        text(
            "SELECT kcu.column_name, ccu.table_name "
            "  FROM information_schema.table_constraints tc "
            "  JOIN information_schema.key_column_usage kcu "
            "    ON kcu.constraint_name = tc.constraint_name "
            "  JOIN information_schema.constraint_column_usage ccu "
            "    ON ccu.constraint_name = tc.constraint_name "
            " WHERE tc.constraint_type = 'FOREIGN KEY' "
            "   AND tc.table_name IN ('field_provenance', 'field_claim', 'snapshot', "
            "                         'extraction', 'field_current', 'entity_version')"
        )
    ).all()
    reached = {row[1] for row in rows}
    assert reached.isdisjoint(onboarding), (
        "the audit spine gained a foreign key into the onboarding plane: "
        f"{sorted(reached & onboarding)}"
    )


# ===========================================================================
# Ranking: eligible for ranking facts only
# ===========================================================================


def _authorization(conn: Connection, *, display_allowed: bool, expired: bool = False) -> uuid.UUID:
    auth_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source_authorization (id, scope, grantor, granted_at, "
            "expires_at, display_allowed) "
            "VALUES (:id, 'ranking', 'Invented Rankings Ltd', "
            "        coalesce(CAST(:granted AS date), current_date - 10), "
            "        :expires, :allowed)"
        ),
        # An expired licence must still satisfy `expires_at >= granted_at`, so it is
        # granted long ago and expired recently rather than expiring before it began.
        {
            "id": auth_id,
            "granted": "2024-01-01" if expired else None,
            "expires": "2024-06-30" if expired else None,
            "allowed": display_allowed,
        },
    )
    return auth_id


def test_a_ranking_source_cannot_support_a_university_name(conn: Connection) -> None:
    """A ranking licence covers ranking data, not an institution's name."""
    auth = _authorization(conn, display_allowed=True)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_RANKING.value,
        category="AUTHORIZED_RANKING",
        authorization=auth,
    )
    with expect_violation(conn, "AUTHORIZED_RANKING and cannot support"):
        _provenance(conn, chain, entity_id=uuid.uuid4(), value='"Some University"')


def test_a_ranking_fact_requires_a_ranking_source(conn: Connection, chain: Chain) -> None:
    """Conversely: an ordinary official source cannot publish a rank."""
    with expect_violation(conn, "must be.*AUTHORIZED_RANKING"):
        _provenance(
            conn,
            chain,
            entity_id=uuid.uuid4(),
            entity_type="ranking_entry",
            field_path="rank_value",
            value="14",
        )


def test_a_ranking_source_needs_a_live_display_allowed_authorization(
    conn: Connection,
) -> None:
    """U9, as a database rule rather than only a feature flag."""
    auth = _authorization(conn, display_allowed=False)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_RANKING.value,
        category="AUTHORIZED_RANKING",
        authorization=auth,
    )
    with expect_violation(conn, "no live display-allowed authorisation"):
        _provenance(
            conn,
            chain,
            entity_id=uuid.uuid4(),
            entity_type="ranking_entry",
            field_path="rank_value",
            value="14",
        )


def test_an_expired_ranking_licence_stops_publication(conn: Connection) -> None:
    auth = _authorization(conn, display_allowed=True, expired=True)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_RANKING.value,
        category="AUTHORIZED_RANKING",
        authorization=auth,
    )
    with expect_violation(conn, "no live display-allowed authorisation"):
        _provenance(
            conn,
            chain,
            entity_id=uuid.uuid4(),
            entity_type="ranking_entry",
            field_path="rank_value",
            value="14",
        )


def test_a_licensed_ranking_source_can_publish_a_rank(conn: Connection) -> None:
    """Non-vacuity guard for the four refusals above."""
    auth = _authorization(conn, display_allowed=True)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_RANKING.value,
        category="AUTHORIZED_RANKING",
        authorization=auth,
    )
    _provenance(
        conn,
        chain,
        entity_id=uuid.uuid4(),
        entity_type="ranking_entry",
        field_path="rank_value",
        value="14",
    )
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM field_provenance WHERE entity_type = 'ranking_entry'",
        )
        == 1
    )


# ===========================================================================
# Authorized external: only within its scope
# ===========================================================================


def test_an_authorized_external_source_is_refused_outside_its_scope(
    conn: Connection,
) -> None:
    """An authorisation with no scope rows permits nothing."""
    auth = _authorization(conn, display_allowed=True)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_EXTERNAL.value,
        host="apply.some-saas.example.com",
        authorization=auth,
    )
    with expect_violation(conn, "not authorised for"):
        _provenance(
            conn,
            chain,
            entity_id=uuid.uuid4(),
            entity_type="tuition",
            field_path="amount",
            value="1234",
        )


def test_an_authorized_external_source_publishes_within_its_scope(
    conn: Connection,
) -> None:
    """Non-vacuity guard, and the mechanism: a `source_field_binding` row."""
    auth = _authorization(conn, display_allowed=True)
    chain = _build_chain(
        conn,
        eligibility=PublicationEligibility.AUTHORIZED_EXTERNAL.value,
        host="apply.some-saas.example.com",
        authorization=auth,
    )
    conn.execute(
        text(
            "INSERT INTO source_field_binding (source_id, entity_type, field_path, "
            "responsibility) VALUES (:source, 'application_deadline', 'deadline_text', "
            "'PRIMARY')"
        ),
        {"source": chain.source},
    )
    _provenance(
        conn,
        chain,
        entity_id=uuid.uuid4(),
        entity_type="application_deadline",
        field_path="deadline_text",
        value='"15 January 2027"',
    )
    assert (
        scalar(
            conn,
            "SELECT count(*) FROM field_provenance " "WHERE entity_type = 'application_deadline'",
        )
        == 1
    )


# ===========================================================================
# Eligibility is earned, not asserted
# ===========================================================================


def test_a_source_cannot_be_classified_without_a_mapping_vouching_for_it(
    conn: Connection,
) -> None:
    """This is what stops the QS workbook being labelled an official source."""
    actor = uuid.uuid4()
    conn.execute(
        text("INSERT INTO app_user (id, email, display_name) VALUES (:id, :e, 'R')"),
        {"id": actor, "e": f"earn-{actor.hex[:8]}@example.test"},
    )
    with expect_violation(conn, "no promoted source_mapping vouches for it"):
        conn.execute(
            text(
                "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
                "fetch_strategy, publication_eligibility, eligibility_set_by, "
                "eligibility_set_at) VALUES (:id, :url, :hash, 'university_site', "
                "'MONTHLY', 'STATIC', 'OFFICIAL_VERIFIED', :actor, now())"
            ),
            {
                "id": uuid.uuid4(),
                "url": "https://insights.qs.com/hubfs/rankings.xlsx",
                "hash": sha("qs-file"),
                "actor": actor,
            },
        )


def test_an_unclassified_source_registers_normally(conn: Connection) -> None:
    """Non-vacuity guard: registering a source is still ordinary work."""
    source_id = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO source (id, url, url_hash, source_type, crawl_frequency, "
            "fetch_strategy) VALUES (:id, :url, :hash, 'university_site', 'MONTHLY', "
            "'STATIC')"
        ),
        {"id": source_id, "url": "https://northgate.ac.uk/x", "hash": sha("plain")},
    )
    assert (
        scalar(
            conn,
            "SELECT publication_eligibility FROM source WHERE id = :id",
            id=source_id,
        )
        == PublicationEligibility.NOT_ELIGIBLE.value
    )


def test_repointing_a_classified_source_at_another_url_is_refused(
    conn: Connection, chain: Chain
) -> None:
    """Otherwise a legitimately classified source could be swapped for a QS file."""
    with expect_violation(conn, "url does not match the mapping"):
        conn.execute(
            text("UPDATE source SET url_hash = :hash WHERE id = :id"),
            {"hash": sha("somewhere-else"), "id": chain.source},
        )


def test_the_mapping_eligibility_column_cannot_be_written(conn: Connection, chain: Chain) -> None:
    """GENERATED, so no role can set it -- including the table owner."""
    with expect_violation(conn, "can only be updated to DEFAULT"):
        conn.execute(
            text(
                "UPDATE source_mapping SET publication_eligibility = 'OFFICIAL_VERIFIED' "
                "WHERE id = :id"
            ),
            {"id": chain.mapping},
        )


@pytest.mark.parametrize(
    ("verification_status", "category", "expected"),
    [
        ("CANDIDATE", "UNIVERSITY_HOME", "NOT_ELIGIBLE"),
        ("REJECTED", "UNIVERSITY_HOME", "NOT_ELIGIBLE"),
        ("LEGACY", "UNIVERSITY_HOME", "NOT_ELIGIBLE"),
        ("VERIFIED_OFFICIAL", "UNIVERSITY_HOME", "OFFICIAL_VERIFIED"),
        ("VERIFIED_OFFICIAL", "TUITION_FEES", "OFFICIAL_VERIFIED"),
        ("AUTHORIZED_EXTERNAL", "APPLICATION_DEADLINES", "AUTHORIZED_EXTERNAL"),
        # Category wins: a ranking page is a ranking source wherever it is hosted.
        ("VERIFIED_OFFICIAL", "AUTHORIZED_RANKING", "AUTHORIZED_RANKING"),
        ("AUTHORIZED_EXTERNAL", "AUTHORIZED_RANKING", "AUTHORIZED_RANKING"),
        ("CANDIDATE", "AUTHORIZED_RANKING", "NOT_ELIGIBLE"),
    ],
)
def test_the_derivation_is_what_it_claims(
    conn: Connection, verification_status: str, category: str, expected: str
) -> None:
    """The generated column, over the combinations that matter."""
    derived = scalar(
        conn,
        "SELECT CASE "
        "  WHEN :status NOT IN ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') "
        "       THEN 'NOT_ELIGIBLE' "
        "  WHEN :cat = 'AUTHORIZED_RANKING' THEN 'AUTHORIZED_RANKING' "
        "  WHEN :status = 'AUTHORIZED_EXTERNAL' THEN 'AUTHORIZED_EXTERNAL' "
        "  ELSE 'OFFICIAL_VERIFIED' END",
        status=verification_status,
        cat=category,
    )
    assert derived == expected


def test_the_derivation_matches_the_database_for_every_combination(
    conn: Connection,
) -> None:
    """The SQL above is a copy of the generated expression; prove they agree.

    Runs the real column over the full cross product of both enums, so a future edit
    to one and not the other is caught rather than assumed away.
    """
    rows = conn.execute(
        text(
            "WITH combos AS ("
            "  SELECT v AS verification_status, s AS source_category "
            "    FROM unnest(enum_range(NULL::official_verification_status)) v "
            "    CROSS JOIN unnest(enum_range(NULL::source_category)) s"
            ") "
            "SELECT count(*) AS total, "
            "       count(*) FILTER (WHERE derived = 'NOT_ELIGIBLE') AS not_eligible, "
            "       count(*) FILTER (WHERE derived = 'AUTHORIZED_RANKING') AS ranking "
            "  FROM (SELECT CASE "
            "          WHEN verification_status NOT IN "
            "               ('VERIFIED_OFFICIAL', 'AUTHORIZED_EXTERNAL') "
            "               THEN 'NOT_ELIGIBLE' "
            "          WHEN source_category = 'AUTHORIZED_RANKING' "
            "               THEN 'AUTHORIZED_RANKING' "
            "          WHEN verification_status = 'AUTHORIZED_EXTERNAL' "
            "               THEN 'AUTHORIZED_EXTERNAL' "
            "          ELSE 'OFFICIAL_VERIFIED' END AS derived "
            "        FROM combos) d"
        )
    ).one()
    # 5 statuses x 15 categories; 3 untrusted statuses x 15 = 45 ineligible;
    # 2 trusted statuses x the ranking category = 2.
    assert rows.total == 75
    assert rows.not_eligible == 45
    assert rows.ranking == 2


# ===========================================================================
# Provenance must cite something
# ===========================================================================


def test_a_published_fact_cannot_cite_nothing(conn: Connection) -> None:
    """Before C27 this was allowed, which made eligibility bypassable outright."""
    # The BEFORE INSERT trigger runs before the CHECK, so its message surfaces
    # first. Both refuse the row; asserting on the trigger is asserting on what
    # an operator would actually see.
    with expect_violation(conn, "resolves to no source"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "published_at) VALUES ('university', :id, 'name_en', 'university', :id, "
                "1, 'PUBLISHED', '\"x\"'::jsonb, 'LOW', now())"
            ),
            {"id": uuid.uuid4()},
        )


def test_not_checked_may_cite_nothing(conn: Connection) -> None:
    """The one coherent exemption: no observation to cite."""
    entity = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO field_provenance (entity_type, entity_id, field_path, "
            "root_type, root_id, root_version_no, field_status, risk_level, "
            "published_at) VALUES ('university', :id, 'city', 'university', :id, 1, "
            "'NOT_CHECKED', 'LOW', now())"
        ),
        {"id": entity},
    )
    assert (
        scalar(conn, "SELECT count(*) FROM field_provenance WHERE entity_id = :id", id=entity) == 1
    )


def test_provenance_cannot_cite_a_snapshot_from_another_source(
    conn: Connection, chain: Chain
) -> None:
    """A citation must hang together, or the class check guards the wrong thing."""
    other = _build_chain(conn, host="rivermouth.ac.uk")
    with expect_violation(conn, "did not come from source"):
        conn.execute(
            text(
                "INSERT INTO field_provenance (entity_type, entity_id, field_path, "
                "root_type, root_id, root_version_no, field_status, value, risk_level, "
                "source_id, snapshot_id, published_at) "
                "VALUES ('university', :id, 'name_en', 'university', :id, 1, "
                "'PUBLISHED', '\"x\"'::jsonb, 'LOW', :source, :snap, now())"
            ),
            {"id": uuid.uuid4(), "source": chain.source, "snap": other.snapshot},
        )


def test_provenance_cannot_cite_a_claim_about_another_field(conn: Connection, chain: Chain) -> None:
    claim_id = _claim(conn, chain, field_path="city", value='"Northgate"')
    with expect_violation(conn, "about a different field"):
        _provenance(conn, chain, entity_id=uuid.uuid4(), field_path="name_en", claim_id=claim_id)


# ===========================================================================
# Governed canonical columns require provenance
# ===========================================================================


def test_a_governed_column_cannot_be_written_without_provenance(
    conn: Connection,
) -> None:
    """The client's literal scenario: a spreadsheet string typed into a name."""
    with expect_violation(conn, "without matching published provenance"):
        conn.execute(
            text(
                "INSERT INTO university (id, canonical_id, destination_code, name_en) "
                "VALUES (:id, :cid, 'GB', 'Imperial College London')"
            ),
            {"id": uuid.uuid4(), "cid": f"gov-{uuid.uuid4().hex[:8]}"},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_a_governed_column_cannot_be_updated_to_an_uncited_value(
    conn: Connection, chain: Chain
) -> None:
    """Publishing honestly once does not license a later edit."""
    name = '"Northgate Institute of Technology"'
    claim_id = _claim(conn, chain, value=name)
    conn.execute(
        text(
            "INSERT INTO university (id, canonical_id, destination_code, name_en) "
            "VALUES (:id, :cid, 'GB', 'Northgate Institute of Technology')"
        ),
        {"id": chain.university, "cid": f"upd-{chain.university.hex[:8]}"},
    )
    _provenance(conn, chain, entity_id=chain.university, value=name, claim_id=claim_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with expect_violation(conn, "without matching published provenance"):
        conn.execute(
            text("UPDATE university SET name_en = 'Imperial College London' WHERE id = :id"),
            {"id": chain.university},
        )
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_a_governed_numeric_column_compares_numerically(conn: Connection, chain: Chain) -> None:
    """`to_jsonb(1234.00::numeric)` keeps its scale, so a textual compare would
    reject an honest 1234.0 from the claim."""
    conn.execute(
        text(
            "INSERT INTO field_provenance (entity_type, entity_id, field_path, "
            "root_type, root_id, root_version_no, field_status, value, risk_level, "
            "source_id, snapshot_id, published_at) "
            "VALUES ('tuition', :id, 'amount_min', 'university', :root, 1, 'PUBLISHED', "
            "        '1234.0'::jsonb, 'LOW', :source, :snap, now())"
        ),
        {
            "id": chain.university,
            "root": chain.university,
            "source": chain.source,
            "snap": chain.snapshot,
        },
    )
    # Different textual scale, same number: must be accepted.
    assert scalar(
        conn,
        "SELECT (('1234.0'::jsonb) #>> '{}')::numeric "
        "     = (to_jsonb(1234.00::numeric) #>> '{}')::numeric",
    )


def test_an_ungoverned_column_still_needs_no_provenance(conn: Connection) -> None:
    """Honest about coverage: eight columns are governed, not the whole plane.

    This test exists so the limit is visible in the suite rather than only in prose.
    If a future revision governs `city`, this test should be changed deliberately --
    not deleted in confusion.
    """
    name = '"Northgate"'
    chain = _build_chain(conn)
    claim_id = _claim(conn, chain, value=name)
    conn.execute(
        text(
            "INSERT INTO university (id, canonical_id, destination_code, name_en, city) "
            "VALUES (:id, :cid, 'GB', 'Northgate', 'A City Nobody Cited')"
        ),
        {"id": chain.university, "cid": f"ung-{chain.university.hex[:8]}"},
    )
    _provenance(conn, chain, entity_id=chain.university, value=name, claim_id=claim_id)
    conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    assert (
        scalar(conn, "SELECT city FROM university WHERE id = :id", id=chain.university)
        == "A City Nobody Cited"
    )


# ===========================================================================
# The publisher's reach
# ===========================================================================


def test_the_publisher_cannot_read_the_onboarding_plane(role_engines: dict[str, Any]) -> None:
    """The read half of the attack the client described, removed.

    `app_publisher` publishes reviewed claims; it never needs the client's scope
    list. Taking the read away means a hand-copy needs a second credential.
    """
    from sqlalchemy import text as sql

    blocked = [
        "target_list",
        "target_list_entry",
        "target_institution",
        "target_list_diff",
        "official_domain",
        "source_mapping",
        "source_degree_scope",
        "source_discipline_scope",
        "target_source_coverage",
        "audit_log",
    ]
    engine = role_engines["app_publisher"]
    with engine.connect() as connection:
        granted = {
            row.table_name
            for row in connection.execute(
                sql(
                    "SELECT DISTINCT table_name FROM information_schema.table_privileges "
                    "WHERE grantee = 'app_publisher' AND privilege_type = 'SELECT' "
                    "  AND table_name = ANY(:tables)"
                ),
                {"tables": blocked},
            ).all()
        }
    assert granted == set(), f"app_publisher can still read: {sorted(granted)}"


def test_the_publisher_still_appends_to_the_audit_chain(role_engines: dict[str, Any]) -> None:
    """Non-vacuity guard: the revoke removed SELECT, not INSERT."""
    from sqlalchemy import text as sql

    with role_engines["app_publisher"].connect() as connection:
        privileges = {
            row.privilege_type
            for row in connection.execute(
                sql(
                    "SELECT privilege_type FROM information_schema.table_privileges "
                    "WHERE grantee = 'app_publisher' AND table_name = 'audit_log'"
                )
            ).all()
        }
    assert privileges == {"INSERT"}


def test_the_publisher_cannot_disable_a_trigger(role_engines: dict[str, Any]) -> None:
    """Triggers are enforcement here only because this is true."""
    from sqlalchemy.exc import ProgrammingError

    with role_engines["app_publisher"].connect() as connection:
        transaction = connection.begin()
        with pytest.raises(ProgrammingError, match="must be owner"):
            connection.execute(text("ALTER TABLE university DISABLE TRIGGER ALL"))
        transaction.rollback()


def test_the_publisher_cannot_enter_replica_mode(role_engines: dict[str, Any]) -> None:
    """`session_replication_role = replica` would skip ordinary triggers."""
    from sqlalchemy.exc import DatabaseError

    with role_engines["app_publisher"].connect() as connection:
        transaction = connection.begin()
        with pytest.raises(DatabaseError, match="permission denied to set parameter"):
            connection.execute(text("SET session_replication_role = replica"))
        transaction.rollback()


def test_the_governed_tuition_columns_followed_the_u14_rename(conn: Connection) -> None:
    """The failure mode this test exists for is silent.

    C27 froze the governed column list into revision 0017 as `('amount')`, and U14
    dropped that column. Nothing in PostgreSQL updates a trigger's `TG_ARGV` when a
    column disappears: the trigger would have kept firing on every tuition write,
    looked up a key that is no longer in `to_jsonb(NEW)`, taken the "a NULL column
    asserts nothing" branch, and passed. Tuition would have left the governed set
    without a single error anywhere.

    Revision 0019 therefore drops and recreates it. This asserts the live arguments,
    because a frozen literal in an old migration is history, not a description of
    now, and something has to notice when the two diverge.
    """
    raw = conn.execute(
        text(
            "SELECT encode(tgargs, 'escape') FROM pg_trigger "
            " WHERE tgname = 'tuition_governed_fields_need_provenance'"
        )
    ).scalar_one()
    governed = [part for part in raw.split("\\000") if part]
    assert governed == ["amount_kind", "amount_min", "amount_max"], governed

    columns = {
        row[0]
        for row in conn.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name = 'tuition'")
        )
    }
    assert "amount" not in columns, "the dropped column came back"
    assert {"amount_kind", "amount_min", "amount_max"} <= columns


def test_a_governed_tuition_amount_still_needs_provenance(
    conn: Connection, chain: Chain, graph: Graph
) -> None:
    """The rename did not weaken the rule it was carrying.

    A fee written on the publisher's own authority is refused at commit, exactly as
    `university.name_en` is -- which is the whole point of governing the column.
    """
    conn.execute(
        text(
            "INSERT INTO tuition (id, offering_id, academic_year, student_category_id, "
            "amount_field_status, amount_kind, amount_min, amount_max, currency_code, "
            "billing_unit_code) VALUES (:id, :off, '2027/28', :cat, 'PUBLISHED', "
            "'RANGE', 28000, 32000, 'GBP', 'PER_YEAR')"
        ),
        {
            "id": uuid.uuid4(),
            "off": graph["offering_a"],
            "cat": graph["student_category_intl"],
        },
    )
    with expect_violation(conn, "without matching published provenance"):
        conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def test_the_new_gates_are_enable_always(conn: Connection) -> None:
    """So they fire even if a replica session ever becomes reachable."""
    states = {
        row[0]: row[1]
        for row in conn.execute(
            text(
                "SELECT tgname, tgenabled FROM pg_trigger "
                " WHERE NOT tgisinternal AND tgname IN ("
                "   'field_provenance_requires_eligible_evidence', "
                "   'field_claim_requires_eligible_evidence', "
                "   'source_eligibility_is_earned', "
                "   'university_governed_fields_need_provenance', "
                "   'entity_alias_governed_fields_need_provenance', "
                "   'ranking_entry_governed_fields_need_provenance', "
                "   'tuition_governed_fields_need_provenance')"
            )
        ).all()
    }
    assert len(states) == 7, f"missing gates: {sorted(states)}"
    always = {name for name, enabled in states.items() if enabled == "A"}
    assert always == set(states), f"not ENABLE ALWAYS: {sorted(set(states) - always)}"


def test_the_eligible_class_set_agrees_between_python_and_the_database(
    conn: Connection,
) -> None:
    """The trigger's frozen literal and `EVIDENCE_ELIGIBLE_CLASSES` must not drift."""
    body = scalar(
        conn,
        "SELECT prosrc FROM pg_proc WHERE proname = 'app_field_provenance_evidence_is_eligible'",
    )
    for member in EVIDENCE_ELIGIBLE_CLASSES:
        assert member.value in body, f"{member.value} missing from the trigger"
    assert PublicationEligibility.TARGET_SCOPE_ONLY.value not in body.split("NOT IN")[1][:200]
