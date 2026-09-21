"""The read-only body viewer: what a reviewer is shown, and that looking changes nothing.

WHY THE VIEWER EXISTS
=====================
Before this, a reviewer deciding whether a page carries a responsibility saw its host's
verification status, its access class, its title and a candidate count. None of those
answer the question. A page can be live, titled, on a verified host, and still be a news
item or a listing for a different degree level -- so the decision needs the body.

WHAT THESE TESTS PIN
====================
* The artifact chosen is the **current** one. Two artifacts can exist for one snapshot
  because the document normaliser is versioned separately from the claim rules (D55), and
  showing a superseded artifact would show a reviewer text the claim pass never read.
* A page with no stored body says `BODY_EVIDENCE_NOT_AVAILABLE` and names the acquisition
  state, rather than rendering an empty document that looks like a page with nothing on it.
* Duplicate responsibilities on one physical page share the body and are labelled
  separately, because the body is identical and the question is not.
* **Nothing is written.** Not a row, not an audit entry. Looking at evidence is not an
  event, and a viewer that logged every glance would fill an append-only chain with them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, text

from app.domains.acquisition.storage import FilesystemEvidenceStore
from app.domains.extraction.document import (
    Block,
    BlockKind,
    NormalizedDocument,
    Table,
    TableCell,
)
from app.domains.extraction.runner import DERIVED_PREFIX
from app.domains.verification.source_body import (
    NOT_AVAILABLE,
    SourceBodyError,
    load,
    looks_like_chrome,
    render,
    resolve,
)

pytestmark = pytest.mark.integration

#: Tables whose counts must not move when a reviewer reads evidence.
WATCHED = (
    "audit_log",
    "official_domain",
    "source_mapping",
    "pilot_collected_source",
    "snapshot",
    "extraction",
    "field_claim",
    "field_claim_candidate",
)


def _counts(conn: Connection) -> dict[str, int]:
    return {
        table: conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() for table in WATCHED
    }


def _artifact(document: dict[str, Any], root: Path) -> str:
    """Store a normalised-document artifact and return its hash."""
    store = FilesystemEvidenceStore(root, prefix=DERIVED_PREFIX)
    return store.put(
        json.dumps(document).encode("utf-8"), content_type="application/json"
    ).content_hash


def _document(
    *,
    title: str,
    media_type: str = "text/html",
    blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "schema": "normalized-document/2",
        "extractor_name": "html-document-normaliser",
        "extractor_version": "2.0.0",
        "media_type": media_type,
        "title": title,
        "blocks": blocks or [{"kind": "paragraph", "text": "Body text a reviewer can read."}],
        "tables": [],
        "links": [],
        "embeds": [],
        "structured_data": {},
        "encoding": {},
        "statistics": {},
        "warnings": [],
    }


@pytest.fixture
def page(conn: Connection, tmp_path: Path) -> Any:
    """A minimal evidence chain builder: institution, source, pilot rows, artifacts.

    Builds its own everything -- `datahub_test` has an empty evidence plane, and a test
    that needs the real pilot data only passes on one machine.
    """

    class Builder:
        def __init__(self) -> None:
            self.root = tmp_path
            list_id, self.institution = uuid.uuid4(), uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO target_list (id, list_name, list_version, file_name, "
                    "  file_sha256, file_byte_size, sheet_name, imported_row_count) "
                    "VALUES (:i, 'Body List', :v, 'b.xlsx', :s, 1, 's', 1)"
                ),
                {"i": list_id, "v": list_id.hex[:8], "s": list_id.hex * 2},
            )
            conn.execute(
                text(
                    "INSERT INTO target_institution (id, match_key, first_seen_list_id, "
                    "  latest_list_id) VALUES (:i, :k, :l, :l)"
                ),
                {"i": self.institution, "k": f"body university {list_id.hex[:6]}", "l": list_id},
            )
            self.submission = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO pilot_submission (id, file_sha256, original_filename, "
                    "  file_byte_size, template_version, submission_kind, "
                    "  defines_pilot_scope, selected_university_count) "
                    "VALUES (:i, :s, 'w.xlsx', 1, 'v1', 'OFFICIAL_SOURCE_LIST', false, 1)"
                ),
                {"i": self.submission, "s": list_id.hex * 2},
            )
            self.rows = 0

        def source(self, url: str) -> uuid.UUID:
            source_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO source (id, url, url_hash, source_type, "
                    "  crawl_frequency, fetch_strategy) "
                    "VALUES (:i, :u, :h, 'university_site', 'MONTHLY', 'STATIC')"
                ),
                {"i": source_id, "u": url, "h": uuid.uuid4().hex * 2},
            )
            return source_id

        def pilot(
            self,
            source_id: uuid.UUID,
            *,
            url: str,
            responsibility: str,
            ref: str,
            duplicate_of: str | None = None,
            scope: str | None = None,
        ) -> uuid.UUID:
            self.rows += 1
            pilot_id = uuid.uuid4()
            conn.execute(
                text(
                    "INSERT INTO pilot_collected_source (id, submission_id, source_ref, "
                    "  target_institution_id, sheet_row_no, source_type, degree_scope, "
                    "  official_url, normalized_url, url_sha256, host, "
                    "  duplicate_of_source_ref, acquisition_source_id, workbook_column) "
                    "VALUES (:i, :sub, :ref, :inst, :row, :resp, "
                    "  CAST(:scope AS degree_scope), :u, :u, :h, :host, :dup, :src, 'A')"
                ),
                {
                    "i": pilot_id,
                    "sub": self.submission,
                    "ref": ref,
                    "inst": self.institution,
                    "row": self.rows + 1,
                    "resp": responsibility,
                    "scope": scope,
                    "u": url,
                    "h": uuid.uuid4().hex * 2,
                    "host": url.split("/")[2],
                    "dup": duplicate_of,
                    "src": source_id,
                },
            )
            return pilot_id

        def _run(
            self, source_id: uuid.UUID, *, status: str, http: int | None, error: str | None
        ) -> uuid.UUID:
            """A fetch attempt and the run under it. Both are required by the schema."""
            attempt_id, run_id = uuid.uuid4(), uuid.uuid4()
            conn.execute(
                text("INSERT INTO fetch_attempt (id, source_id, cycle_key) " "VALUES (:i, :s, :c)"),
                {"i": attempt_id, "s": source_id, "c": attempt_id.hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO fetch_run (id, source_id, attempt_id, started_at, "
                    "  status, fetcher, http_status, error_class) "
                    "VALUES (:i, :s, :a, now(), CAST(:st AS fetch_status), 'STATIC', "
                    "  :h, :e)"
                ),
                {
                    "i": run_id,
                    "s": source_id,
                    "a": attempt_id,
                    "st": status,
                    "h": http,
                    "e": error,
                },
            )
            return run_id

        def fetched(
            self,
            source_id: uuid.UUID,
            *,
            url: str,
            document: dict[str, Any],
            artifact_version: str = "2.0.0",
            status: str = "OK",
        ) -> tuple[uuid.UUID, uuid.UUID, str]:
            """A successful fetch: attempt, run, snapshot, extraction, stored artifact."""
            run_id = self._run(source_id, status="OK", http=200, error=None)
            snapshot_id, extraction_id = uuid.uuid4(), uuid.uuid4()
            content_hash = uuid.uuid4().hex * 2
            conn.execute(
                text(
                    "INSERT INTO content_blob (content_hash, storage_key, "
                    "  first_observed_at) VALUES (:h, :k, now())"
                ),
                {"h": content_hash, "k": f"blobs/{content_hash}"},
            )
            conn.execute(
                text(
                    "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, "
                    "  observed_at, requested_url, effective_url, http_status, fetcher, "
                    "  content_type) "
                    "VALUES (:i, :r, :s, :h, now(), :u, :u, 200, 'STATIC', 'text/html')"
                ),
                {"i": snapshot_id, "r": run_id, "s": source_id, "h": content_hash, "u": url},
            )
            document_hash = _artifact(document, self.root)
            conn.execute(
                text(
                    "INSERT INTO extraction (id, snapshot_id, extractor_name, "
                    "  extractor_version, status, output, input_content_hash, "
                    "  document_hash, document_storage_key, document_byte_size, "
                    "  recorded_at) "
                    "VALUES (:i, :sn, 'html-document-normaliser', :v, "
                    "  CAST(:st AS extraction_status), CAST(:o AS jsonb), :ch, :dh, :dk, "
                    "  10, now())"
                ),
                {
                    "i": extraction_id,
                    "sn": snapshot_id,
                    "v": artifact_version,
                    "st": status,
                    "o": json.dumps(
                        {"title": document["title"], "media_type": document["media_type"]}
                    ),
                    "ch": content_hash,
                    "dh": document_hash,
                    "dk": f"{DERIVED_PREFIX}/{document_hash[:2]}/{document_hash}",
                },
            )
            return snapshot_id, extraction_id, document_hash

        def failed(
            self, source_id: uuid.UUID, *, status: str, http: int | None, error: str | None
        ) -> None:
            """An attempt that produced no snapshot: dead, blocked, TLS, and so on."""
            self._run(source_id, status=status, http=http, error=error)

        def publish_artifact_version(self, version: str = "2.0.0") -> None:
            conn.execute(
                text(
                    "INSERT INTO document_artifact_version (extractor_name, version) "
                    "VALUES ('html-document-normaliser', :v) "
                    "ON CONFLICT (extractor_name) DO UPDATE SET version = :v"
                ),
                {"v": version},
            )

    builder = Builder()
    builder.publish_artifact_version()
    return builder


# ===========================================================================
# 1. HTML with a body
# ===========================================================================


def test_an_html_page_with_a_body_renders_its_structure(conn: Connection, page: Any) -> None:
    """The reviewer sees the document, with the shape that makes it judgeable."""
    url = "https://www.body.example/fees"
    source = page.source(url)
    snapshot, extraction, document_hash = page.fetched(
        source,
        url=url,
        document=_document(
            title="Tuition fees",
            blocks=[
                {"kind": "heading", "text": "Tuition fees", "level": 1},
                {"kind": "paragraph", "text": "Fees for 2026 are set out below."},
                {"kind": "list", "items": ["Domestic", "International"], "ordered": False},
                {"kind": "table", "table_index": 0},
            ],
        ),
    )
    pilot = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9001")

    evidence = resolve(conn, pilot)
    assert evidence.available is True
    assert evidence.access_class == "BODY_EVIDENCE"
    assert evidence.snapshot_id == snapshot
    assert evidence.extraction_id == extraction
    assert evidence.document_hash == document_hash
    assert evidence.document_artifact_version == "2.0.0"
    assert evidence.responsibility == "TUITION_FEES"
    assert evidence.media_type == "text/html"

    document = load(FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX), evidence)
    body = render(document)
    assert "TITLE: Tuition fees" in body
    assert "# Tuition fees" in body
    assert "Fees for 2026 are set out below." in body
    assert "- Domestic" in body and "- International" in body


def test_a_store_that_lacks_the_document_is_a_named_failure_not_a_crash(
    conn: Connection, page: Any, tmp_path: Path
) -> None:
    """Found by opening the evidence viewer against real ANU data.

    `ARTIFACT_ROOT` was mistyped in the environment, so the store looked in a directory
    that did not exist. The blob store raised `KeyError`, nothing translated it, and the
    console showed HTTP 500 "An unexpected error occurred" -- on the one screen whose
    entire job is to show a reviewer what the stored evidence says.

    The database naming a document the store does not hold is an ordinary operational
    state: a wrong root, a pruned cache, an artifact directory nobody copied. It must
    arrive as `BODY_EVIDENCE_NOT_AVAILABLE`, which the console already knows how to
    render, and it must name the hash so an operator can tell which artifact is missing.
    """
    url = "https://www.body.example/missing-artifact"
    source = page.source(url)
    page.fetched(source, url=url, document=_document(title="Fees", blocks=[]))
    pilot = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9101")

    evidence = resolve(conn, pilot)
    assert evidence.available is True, "the row is sound; only the artifact is missing"
    assert evidence.document_hash is not None

    # An empty root stands in for every way a root can be wrong.
    empty = FilesystemEvidenceStore(tmp_path / "not-the-real-root", prefix=DERIVED_PREFIX)
    with pytest.raises(SourceBodyError) as raised:
        load(empty, evidence)

    message = str(raised.value)
    assert NOT_AVAILABLE in message
    assert evidence.document_hash[:12] in message, "an operator must learn which artifact"
    # The same row loads from the store that does hold it, so the error is about the
    # store and not about the evidence chain.
    assert load(FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX), evidence)


def test_tables_and_headings_survive_rendering() -> None:
    """Shape matters: "is this a fees table or a news item?" is answered by structure."""
    document = NormalizedDocument(
        schema="normalized-document/2",
        extractor_name="html-document-normaliser",
        extractor_version="2.0.0",
        media_type="text/html",
        title="Fees",
        blocks=[
            Block(kind=BlockKind.HEADING, text="Annual fees", level=2),
            Block(kind=BlockKind.TABLE, table_index=0),
            Block(kind=BlockKind.QUOTE, text="Indicative only."),
            Block(kind=BlockKind.PREFORMATTED, text="code sample"),
        ],
        tables=[
            Table(
                caption="2026",
                header_rows=[[TableCell(text="Programme"), TableCell(text="Fee")]],
                rows=[[TableCell(text="Arts"), TableCell(text="AUD 45,000")]],
            )
        ],
    )
    body = render(document)
    assert "## Annual fees" in body
    assert "caption: 2026" in body
    assert "Programme" in body and "AUD 45,000" in body
    assert "> Indicative only." in body
    assert "code sample" in body


# ===========================================================================
# 2. PDF with a body
# ===========================================================================


def test_a_pdf_page_renders_its_normalised_text_per_page(conn: Connection, page: Any) -> None:
    """PDF evidence shows extracted text, with page numbers kept.

    Page boundaries are content: "which page said this" is the first thing anybody asks
    of a PDF, so `PDF_PAGE` blocks are labelled rather than flattened.
    """
    url = "https://www.body.example/handbook.pdf"
    source = page.source(url)
    page.fetched(
        source,
        url=url,
        document=_document(
            title="Admissions handbook",
            media_type="application/pdf",
            blocks=[
                {"kind": "pdf_page", "text": "Entry requirements for 2026.", "level": 1},
                {"kind": "pdf_page", "text": "English language: IELTS 6.5.", "level": 2},
            ],
        ),
    )
    pilot = page.pilot(source, url=url, responsibility="ENTRY_REQUIREMENTS", ref="S9002")

    evidence = resolve(conn, pilot)
    assert evidence.media_type == "application/pdf"
    body = render(load(FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX), evidence))
    assert "--- PDF page 1 ---" in body
    assert "Entry requirements for 2026." in body
    assert "--- PDF page 2 ---" in body
    assert "IELTS 6.5" in body


# ===========================================================================
# 3. Duplicate responsibility on one physical page
# ===========================================================================


def test_two_responsibilities_on_one_page_share_a_body_and_are_labelled_apart(
    conn: Connection, page: Any
) -> None:
    """Same bytes, same snapshot, same artifact -- different question.

    The body cannot distinguish them, so the header must: a reviewer who cannot tell
    which responsibility they are judging will judge the page instead.
    """
    url = "https://www.body.example/courses"
    source = page.source(url)
    snapshot, extraction, document_hash = page.fetched(
        source, url=url, document=_document(title="Programs and Courses")
    )
    physical = page.pilot(source, url=url, responsibility="PROGRAM_CATALOG", ref="S9010")
    duplicate = page.pilot(
        source, url=url, responsibility="UNCLASSIFIED", ref="S9011", duplicate_of="S9010"
    )

    first, second = resolve(conn, physical), resolve(conn, duplicate)
    assert first.snapshot_id == second.snapshot_id == snapshot
    assert first.extraction_id == second.extraction_id == extraction
    assert first.document_hash == second.document_hash == document_hash

    assert first.duplicate_of is None
    assert second.duplicate_of == "S9010"
    assert first.responsibility == "PROGRAM_CATALOG"
    assert second.responsibility == "UNCLASSIFIED"
    assert first.verification_state == second.verification_state == "PENDING"


# ===========================================================================
# 4-5. No body: dead, blocked, and the rest of the ladder
# ===========================================================================


# Every case names an error, because `ck_fetch_run_a_failure_names_its_error` requires
# it: a failed fetch that records no reason is not evidence of anything. The statuses are
# the real `fetch_status` labels -- there is no CONNECTION_ERROR, so a TLS failure arrives
# as INTERNAL_ERROR carrying a TLS error class, which is why the ladder reads the class
# and not only the status.
@pytest.mark.parametrize(
    ("fetch_status", "http", "error", "expected"),
    [
        ("HTTP_ERROR", 404, "HTTP404: Not Found", "DEAD_NOT_FOUND"),
        ("HTTP_ERROR", 410, "HTTP410: Gone", "DEAD_NOT_FOUND"),
        ("BLOCKED", 403, "HTTP403: Forbidden", "BLOCKED"),
        ("NAME_NOT_RESOLVED", None, "DNS_NXDOMAIN", "DEAD_HOST"),
        ("TIMEOUT", None, "READ_TIMEOUT", "TIMEOUT"),
        ("INTERNAL_ERROR", None, "TLS_CERT_INVALID", "TLS_FAILURE"),
        ("HTTP_ERROR", 202, "HTTP202: Accepted", "OTHER_HTTP"),
    ],
)
def test_a_page_with_no_stored_body_reports_the_acquisition_state(
    conn: Connection,
    page: Any,
    fetch_status: str,
    http: int | None,
    error: str | None,
    expected: str,
) -> None:
    """`BODY_EVIDENCE_NOT_AVAILABLE`, and *why*.

    Rendering an empty document would be the dangerous failure: it looks like a page
    that exists and says nothing, which is very different from a page that is not there.
    """
    url = f"https://www.body.example/{fetch_status.lower()}-{http}"
    source = page.source(url)
    page.failed(source, status=fetch_status, http=http, error=error)
    pilot = page.pilot(
        source, url=url, responsibility="ACADEMIC_CALENDAR", ref=f"S9{(http or 0) % 1000:03d}"
    )

    evidence = resolve(conn, pilot)
    assert evidence.available is False
    assert evidence.document_hash is None
    assert evidence.snapshot_id is None
    assert evidence.extraction_id is None
    assert evidence.access_class == expected
    assert evidence.fetch_status == fetch_status
    assert evidence.http_status == http

    with pytest.raises(SourceBodyError, match=NOT_AVAILABLE):
        load(FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX), evidence)


def test_a_source_never_attempted_is_distinguished_from_a_dead_one(
    conn: Connection, page: Any
) -> None:
    """NO_ATTEMPT is not DEAD. One means nobody looked; the other means it is gone."""
    url = "https://www.body.example/never-fetched"
    source = page.source(url)
    pilot = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9404")
    evidence = resolve(conn, pilot)
    assert evidence.access_class == "NO_ATTEMPT"
    assert evidence.available is False


# ===========================================================================
# 6. The current artifact, not a superseded one
# ===========================================================================


def test_a_superseded_artifact_is_not_what_the_reviewer_is_shown(
    conn: Connection, page: Any
) -> None:
    """D55. Showing the old artifact shows text the current claim pass never read.

    Two artifacts exist for one snapshot because the document normaliser is versioned
    independently of the claim rules. Only the published version is current, and the
    reviewer must see what the extractors see.
    """
    url = "https://www.body.example/versioned"
    source = page.source(url)
    old_snapshot, old_extraction, old_hash = page.fetched(
        source,
        url=url,
        document=_document(title="Superseded rendering"),
        artifact_version="1.0.0",
    )
    # A second extraction of the SAME snapshot at the published version.
    document_hash = _artifact(_document(title="Current rendering"), page.root)
    new_extraction = uuid.uuid4()
    conn.execute(
        text(
            "INSERT INTO extraction (id, snapshot_id, extractor_name, extractor_version, "
            "  status, output, input_content_hash, document_hash, document_storage_key, "
            "  document_byte_size, recorded_at) "
            "SELECT :i, :sn, 'html-document-normaliser', '2.0.0', 'OK', "
            "  CAST(:o AS jsonb), e.input_content_hash, :dh, :dk, 10, now() "
            "  FROM extraction e WHERE e.id = :old"
        ),
        {
            "i": new_extraction,
            "sn": old_snapshot,
            "old": old_extraction,
            "o": json.dumps({"title": "Current rendering", "media_type": "text/html"}),
            "dh": document_hash,
            "dk": f"{DERIVED_PREFIX}/{document_hash[:2]}/{document_hash}",
        },
    )
    pilot = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9020")

    evidence = resolve(conn, pilot)
    assert evidence.document_artifact_version == "2.0.0"
    assert evidence.extraction_id == new_extraction, "chose a superseded extraction"
    assert evidence.document_hash == document_hash
    assert evidence.document_hash != old_hash

    body = render(load(FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX), evidence))
    assert "Current rendering" in body
    assert "Superseded rendering" not in body


def test_only_an_artifact_at_a_published_version_is_offered(conn: Connection, page: Any) -> None:
    """When the ONLY artifact is superseded, there is no current body to show.

    Reporting the stale one as current would be worse than reporting none: the reviewer
    would judge text no rule has read since the normaliser changed.
    """
    url = "https://www.body.example/stale-only"
    source = page.source(url)
    page.fetched(source, url=url, document=_document(title="Only old"), artifact_version="1.0.0")
    pilot = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9021")

    evidence = resolve(conn, pilot)
    assert evidence.document_hash is None
    assert evidence.available is False
    assert evidence.snapshot_id is not None, "the snapshot still exists; the artifact does not"


# ===========================================================================
# 7-8. Read-only: no mutation, no audit append
# ===========================================================================


def test_viewing_evidence_writes_nothing(conn: Connection, page: Any) -> None:
    """Sections 7 and 8. Looking at evidence is not an event.

    `audit_log` is watched deliberately: the chain is append-only and exists for
    decisions, so a viewer that recorded every glance would bury them.
    """
    url = "https://www.body.example/readonly"
    source = page.source(url)
    page.fetched(source, url=url, document=_document(title="Read only"))
    with_body = page.pilot(source, url=url, responsibility="TUITION_FEES", ref="S9030")

    dead_url = "https://www.body.example/readonly-dead"
    dead_source = page.source(dead_url)
    page.failed(dead_source, status="HTTP_ERROR", http=404, error="HTTP404: Not Found")
    without_body = page.pilot(
        dead_source, url=dead_url, responsibility="ACADEMIC_CALENDAR", ref="S9031"
    )

    before = _counts(conn)
    store = FilesystemEvidenceStore(page.root, prefix=DERIVED_PREFIX)

    evidence = resolve(conn, with_body)
    render(load(store, evidence))
    render(load(store, evidence), skip_chrome=True)
    render(load(store, evidence), max_blocks=1)
    resolve(conn, without_body)
    with pytest.raises(SourceBodyError):
        load(store, resolve(conn, without_body))
    with pytest.raises(SourceBodyError, match="no pilot_collected_source"):
        resolve(conn, uuid.uuid4())

    assert _counts(conn) == before, f"the viewer wrote something: {before} -> {_counts(conn)}"


def test_read_only_here_is_a_property_of_the_code_not_of_the_role(
    conn: Connection,
) -> None:
    """Stated because the reassuring version of this claim is false, twice over.

    A first draft asserted `app_api` cannot write at all. It can: it holds INSERT and
    UPDATE on `pilot_collected_source`. A second draft narrowed that to "at least it
    cannot reach trust state". It can do that too -- `app_api` holds INSERT and UPDATE on
    both `official_domain` and `source_mapping`, because it is the role that applies a
    reviewer's domain and responsibility decisions.

    So there is no grant standing between this viewer and the review plane. The read-only
    guarantee is entirely behavioural, which is why
    `test_viewing_evidence_writes_nothing` counts rows before and after rather than
    reading a privilege table -- and why that test, not this one, is the one that matters.

    What the grants *do* rule out is the far side of the C27 boundary: no claim rows, no
    canonical facts. Pinned here so the boundary that does exist is not confused with one
    that does not.
    """
    can_write_review_plane = [
        (table, privilege)
        for table in ("pilot_collected_source", "official_domain", "source_mapping")
        for privilege in ("INSERT", "UPDATE")
        if conn.execute(
            text("SELECT has_table_privilege('app_api', :t, :p)"),
            {"t": table, "p": privilege},
        ).scalar()
    ]
    assert can_write_review_plane, (
        "app_api no longer writes the review plane; this test's premise is gone and the "
        "read-only claim should be re-derived rather than assumed"
    )

    # The boundary that is real: nothing publishable, and no claim rows.
    for table in ("field_claim", "field_provenance", "university", "tuition"):
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not conn.execute(
                text("SELECT has_table_privilege('app_api', :t, :p)"),
                {"t": table, "p": privilege},
            ).scalar(), f"app_api can {privilege} {table}"


# ===========================================================================
# 9. The chrome filter, and its honest limits
# ===========================================================================


def test_the_chrome_filter_hides_furniture_and_never_hides_content() -> None:
    """It under-filters on purpose, and says how many it hid.

    `Block.in_chrome` is the reliable signal but is **not serialised into the artifact**,
    so a consumer has only the innermost `container` -- and a `<section>` inside a `<nav>`
    reports `section`. The filter therefore errs towards showing too much: leaving a menu
    on screen is a nuisance, hiding a page's actual text from a reviewer is a defect.
    """
    document = NormalizedDocument(
        schema="normalized-document/2",
        extractor_name="html-document-normaliser",
        extractor_version="2.0.0",
        media_type="text/html",
        title="Fees",
        blocks=[
            Block(kind=BlockKind.LIST, items=["Home", "Study"], container="nav"),
            Block(kind=BlockKind.PARAGRAPH, text="Contact us", container="footer"),
            Block(kind=BlockKind.PARAGRAPH, text="The real fee text.", container="main"),
            Block(kind=BlockKind.PARAGRAPH, text="Nested in a nav.", container="section"),
        ],
    )
    assert looks_like_chrome(document.blocks[0]) is True
    assert looks_like_chrome(document.blocks[1]) is True
    assert looks_like_chrome(document.blocks[2]) is False
    # Under-filtering, stated: a section nested inside a nav is not recognised.
    assert looks_like_chrome(document.blocks[3]) is False

    filtered = render(document, skip_chrome=True)
    assert "The real fee text." in filtered
    assert "Nested in a nav." in filtered, "the filter must never remove real content"
    assert "Home" not in filtered
    assert "2 nav/header/footer block(s) hidden" in filtered

    whole = render(document)
    assert "Home" in whole and "hidden" not in whole


def test_truncation_says_so_rather_than_stopping_silently() -> None:
    document = NormalizedDocument(
        schema="normalized-document/2",
        extractor_name="html-document-normaliser",
        extractor_version="2.0.0",
        media_type="text/html",
        title="Long",
        blocks=[Block(kind=BlockKind.PARAGRAPH, text=f"Block {n}") for n in range(10)],
    )
    body = render(document, max_blocks=3)
    assert "Block 0" in body and "Block 2" in body
    assert "Block 5" not in body
    assert "truncated: showing 3 of 10 blocks" in body
