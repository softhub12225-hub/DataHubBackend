"""Turning one fetch outcome into immutable evidence (Step 5B sections 15-18).

THE UNVERIFIED-EVIDENCE BOUNDARY
================================
Everything written here comes from a source whose `publication_eligibility` is
`NOT_ELIGIBLE`, and that is the expected state, not a defect. What we capture is real
evidence of what a page served at a moment in time: the bytes, their hash, the
transport that produced them, and when we looked. It is simply not yet evidence anyone
may cite.

The boundary is not maintained by this module choosing to behave. It is C27, unchanged:
`field_claim` and `field_provenance` have triggers that refuse evidence whose source
class does not permit the fact. A snapshot taken from a `PENDING` candidate can be
stored, listed, read and compared; the moment anything tries to turn it into a published
fact, the database refuses. Tests assert both halves.

Nothing here creates a `field_claim`, an `extraction`, a `change_proposal` or any
canonical row. Acquisition ends at the snapshot.

WRITE ORDER
===========
Object storage first, then one transaction (see `storage.py` for why that way round),
and inside the transaction the **fenced attempt transition first of all**. A worker
that lost its lease therefore aborts before a run, a snapshot or a blob row exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.enums import FetchStatus
from app.domains.acquisition.fetcher import ConditionalHeaders, FetchOutcome
from app.domains.acquisition.lease import Lease, take_ownership_for_finalisation
from app.domains.acquisition.storage import EvidenceStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecordedFetch:
    """What the terminal transaction wrote."""

    fetch_run_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    content_hash: str | None
    #: True when these bytes had never been seen before. False is the ordinary case
    #: for an unchanged page, and it is a storage fact, not an observation fact -- the
    #: snapshot is written either way.
    blob_created: bool
    status: FetchStatus


#: How much of an exception message reaches the database. A short sanitised line is
#: for the operator scanning a cycle report; the traceback belongs in the structured
#: log, where it is searchable and is not part of an immutable record forever.
MAX_INTERNAL_ERROR_DETAIL = 300


def record_internal_error(
    connection: Connection,
    *,
    lease: Lease,
    error: BaseException,
    fetcher: str = "STATIC",
) -> uuid.UUID:
    """Write a terminal `INTERNAL_ERROR` run for an attempt our own code broke.

    WHY THIS EXISTS AT ALL
    ======================
    Before Step 5B.2, an unexpected exception propagated out of the cycle: the page
    had no terminal run, the attempt sat `RUNNING` until the sweeper reclaimed it, and
    every page after it in the cycle went unfetched. The failure was ours, and the
    visible consequence was 279 university pages not being looked at.

    WHY IT IS NOT `HTTP_ERROR` OR `TIMEOUT`
    =======================================
    Because it is not either of those, and a health model that cannot tell "the site
    is broken" from "we are broken" will send an operator to email a university about
    a bug in this repository. `INTERNAL_ERROR` also earns **no automatic retry**: the
    cost of our defect should not be paid in requests to someone else's server.

    NO SNAPSHOT, NO BLOB
    ====================
    An internal failure observed nothing. Writing a snapshot would assert we saw bytes
    we cannot produce, which is precisely what the evidence chain exists to prevent.
    The fenced transition is still the transaction's first write, so a worker that
    lost its lease writes nothing here either.
    """
    take_ownership_for_finalisation(connection, lease)

    detail = " ".join(str(error).split())[:MAX_INTERNAL_ERROR_DETAIL]
    run_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
            "finished_at, status, fetcher, error_class, worker_name, "
            "conditional_request_sent) "
            "VALUES (:id, :attempt, :source, :no, now(), now(), 'INTERNAL_ERROR', "
            ":fetcher, :error, :worker, false)"
        ),
        {
            "id": run_id,
            "attempt": lease.attempt_id,
            "source": lease.source_id,
            "no": lease.attempt_no,
            "fetcher": fetcher,
            # The class name, not the message: a type is a stable thing to group a
            # cycle report by, and an f-string of someone's message is not.
            "error": type(error).__name__[:64],
            "worker": lease.worker,
        },
    )
    logger.error(
        "acquisition_internal_error",
        source_id=str(lease.source_id),
        attempt_id=str(lease.attempt_id),
        error_class=type(error).__name__,
        detail=detail,
        exc_info=error,
    )
    return run_id


def conditional_headers_for(connection: Connection, source_id: uuid.UUID) -> ConditionalHeaders:
    """What we last saw for this source, so the server can answer 304.

    Reads the most recent snapshot. If the last observation carried no validator the
    request goes out unconditional, which is correct: a fabricated `If-None-Match`
    would invite a 304 we could not honestly interpret.
    """
    row = connection.execute(
        text(
            "SELECT etag, last_modified, content_hash FROM snapshot "
            " WHERE source_id = :s ORDER BY observed_at DESC, id DESC LIMIT 1"
        ),
        {"s": source_id},
    ).one_or_none()
    if row is None:
        return ConditionalHeaders()
    return ConditionalHeaders(
        etag=row.etag, last_modified=row.last_modified, content_hash=row.content_hash
    )


def record_outcome(
    connection: Connection,
    *,
    lease: Lease,
    outcome: FetchOutcome,
    store: EvidenceStore,
    fetcher: str = "STATIC",
    previous_content_hash: str | None = None,
) -> RecordedFetch:
    """Write the terminal record for one attempt. Caller owns the transaction.

    Object storage has already happened by the time the transaction opens, because a
    row referencing bytes that were never stored is the failure this ordering exists
    to make impossible.
    """
    stored_hash: str | None = None
    blob_created = False
    if outcome.status is FetchStatus.OK and outcome.content is not None:
        stored = store.put(outcome.content, content_type=_base_type(outcome.content_type))
        stored_hash = stored.content_hash
        if stored.content_hash != outcome.content_hash:  # pragma: no cover - defensive
            raise RuntimeError(
                "hash mismatch between fetcher and store: "
                f"{outcome.content_hash} vs {stored.content_hash}"
            )

    # Fenced first. A lost lease raises here, and nothing below runs.
    take_ownership_for_finalisation(connection, lease)

    if stored_hash is not None:
        blob_created = _ensure_blob(
            connection,
            content_hash=stored_hash,
            storage_key=store_key_of(store, stored_hash),
            content_type=_base_type(outcome.content_type),
            byte_size=outcome.byte_size,
        )

    unchanged_hash = previous_content_hash if outcome.status is FetchStatus.UNCHANGED else None
    run_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO fetch_run (id, attempt_id, source_id, attempt_no, started_at, "
            "finished_at, status, http_status, fetcher, error_class, duration_ms, "
            "worker_name, unchanged_content_hash, conditional_request_sent, "
            "bytes_downloaded, effective_url, redirect_chain) "
            "VALUES (:id, :attempt, :source, :no, :started, :finished, :status, "
            ":http, :fetcher, :error, :duration, :worker, :unchanged, :conditional, "
            ":bytes, :effective, :chain)"
        ),
        {
            "id": run_id,
            "attempt": lease.attempt_id,
            "source": lease.source_id,
            "no": lease.attempt_no,
            "started": outcome.started_at,
            "finished": outcome.finished_at or utcnow(),
            "status": outcome.status.value,
            "http": outcome.http_status,
            "fetcher": fetcher,
            "error": _error_text(outcome),
            "duration": outcome.duration_ms,
            "worker": lease.worker,
            "unchanged": unchanged_hash,
            "conditional": outcome.conditional_request_sent,
            "bytes": outcome.byte_size,
            # Recorded on the run as well as the snapshot: a fetch that redirected
            # and then failed writes no snapshot, and losing the trail is exactly
            # what made UBC's four-hop timeout undiagnosable in the smoke run.
            "effective": (
                redact_url(outcome.effective_url)
                if outcome.effective_url != outcome.requested_url
                else None
            ),
            "chain": _json(_redacted_chain(outcome.redirect_chain)),
        },
    )

    snapshot_id: uuid.UUID | None = None
    if stored_hash is not None:
        # A snapshot means "we saw these bytes". A 304 saw none, so it gets none --
        # the run records that we looked and the server said nothing had changed.
        snapshot_id = uuid.uuid4()
        connection.execute(
            text(
                "INSERT INTO snapshot (id, fetch_run_id, source_id, content_hash, "
                "requested_url, effective_url, http_status, fetcher, response_headers, "
                "etag, last_modified, content_type, redirect_chain, technical_metadata, "
                "observed_at) VALUES (:id, :run, :source, :hash, :requested, :effective, "
                ":http, :fetcher, :headers, :etag, :modified, :ctype, :chain, :meta, :at)"
            ),
            {
                "id": snapshot_id,
                "run": run_id,
                "source": lease.source_id,
                "hash": stored_hash,
                "requested": redact_url(outcome.requested_url),
                "effective": (
                    redact_url(outcome.effective_url)
                    if outcome.effective_url != outcome.requested_url
                    else None
                ),
                "http": outcome.http_status,
                "fetcher": fetcher,
                "headers": _json(_bounded_headers(outcome.response_headers)),
                "etag": outcome.etag,
                "modified": outcome.last_modified,
                "ctype": outcome.content_type,
                "chain": _json(_redacted_chain(outcome.redirect_chain)),
                "meta": _json(outcome.technical_metadata or None),
                "at": outcome.started_at,
            },
        )

    logger.info(
        "acquisition_recorded",
        source_id=str(lease.source_id),
        attempt_id=str(lease.attempt_id),
        status=outcome.status.value,
        http_status=outcome.http_status,
        snapshot=bool(snapshot_id),
        blob_created=blob_created,
    )
    return RecordedFetch(
        fetch_run_id=run_id,
        snapshot_id=snapshot_id,
        content_hash=stored_hash,
        blob_created=blob_created,
        status=outcome.status,
    )


def _ensure_blob(
    connection: Connection,
    *,
    content_hash: str,
    storage_key: str,
    content_type: str | None,
    byte_size: int | None,
) -> bool:
    """Insert the content identity if these bytes are new. Returns whether it was.

    `ON CONFLICT DO NOTHING` rather than a read-then-write: two workers fetching
    identical bytes concurrently is normal (a shared boilerplate page, a redirect
    ending in the same document), and a check-then-insert would make that a
    unique-violation rather than a no-op.

    `first_observed_at` is never updated. It says when these bytes were first seen,
    and a later observation does not change that; when they were *last* seen is a
    question about `snapshot` (C12).
    """
    result = connection.execute(
        text(
            "INSERT INTO content_blob (content_hash, storage_key, content_type, "
            "byte_size, first_observed_at) "
            "VALUES (:hash, :key, :ctype, :size, now()) "
            "ON CONFLICT (content_hash) DO NOTHING RETURNING content_hash"
        ),
        {"hash": content_hash, "key": storage_key, "ctype": content_type, "size": byte_size},
    ).one_or_none()
    return result is not None


def store_key_of(store: EvidenceStore, content_hash: str) -> str:
    from app.domains.acquisition.storage import storage_key_for

    del store  # the key is content-derived; the store only decides where the root is
    return storage_key_for(content_hash)


def _error_text(outcome: FetchOutcome) -> str | None:
    if outcome.status in (FetchStatus.OK, FetchStatus.UNCHANGED):
        return None
    parts = [outcome.error_class or "UnknownError"]
    if outcome.error_detail:
        parts.append(outcome.error_detail[:200])
    return ": ".join(parts)[:128]


def _base_type(content_type: str | None) -> str | None:
    return content_type.split(";")[0].strip().lower() if content_type else None


#: Query-parameter names whose *values* are masked before a URL is persisted.
#:
#: A redirect can append a session identifier -- a CMS bouncing through
#: `?JSESSIONID=...` is ordinary behaviour -- and storing that would put a session
#: token in the evidence record, which section 15 forbids. Only the value is masked;
#: the parameter name, its position and every other parameter survive byte-exact,
#: because a query string is part of the address and rewriting it would change which
#: page the record claims we fetched.
#:
#: Matched on the whole name, case-insensitively. Deliberately short: a broad
#: heuristic would eventually mask a meaningful parameter, and UBC's
#: `?tree=2%2C0%2C0%2C0` is exactly the kind it would catch.
_REDACTED_QUERY_PARAMS: frozenset[str] = frozenset(
    {
        "access_token",
        "auth",
        "authorization",
        "code",
        "id_token",
        "jsessionid",
        "key",
        "password",
        "refresh_token",
        "session",
        "sessionid",
        "sid",
        "sig",
        "signature",
        "token",
    }
)

REDACTED = "[redacted]"


def redact_url(url: str | None) -> str | None:
    """Mask credential-bearing query values, leaving everything else untouched.

    Returns the URL unchanged when it carries no such parameter, which is the case
    for every URL in the client's workbook -- so the common path stores exactly what
    was requested.
    """
    if not url or "?" not in url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    if not any(name.lower() in _REDACTED_QUERY_PARAMS for name, _ in pairs):
        return url
    masked = [
        (name, REDACTED if name.lower() in _REDACTED_QUERY_PARAMS else value)
        for name, value in pairs
    ]
    return urlunsplit(parts._replace(query=urlencode(masked)))


#: Headers worth keeping. A whole header block belongs in object storage if anywhere;
#: PostgreSQL keeps the ones that carry provenance or explain the transport.
#:
#: Everything else is dropped, which is what keeps `Set-Cookie`, `Authorization` and
#: every vendor tracing header out of the database. An allowlist rather than a
#: denylist on purpose: a new header a CDN starts sending is excluded by default
#: rather than stored until someone notices.
_KEPT_HEADERS: frozenset[str] = frozenset(
    {
        "content-type",
        "content-length",
        "etag",
        "last-modified",
        "date",
        "server",
        "cache-control",
        "expires",
        "content-language",
        "content-disposition",
        "retry-after",
        "location",
    }
)


def _bounded_headers(headers: dict[str, str]) -> dict[str, str] | None:
    """The provenance-bearing headers, truncated. Not the whole block.

    A response can carry kilobytes of CSP and tracing headers, and putting all of it
    in PostgreSQL would bloat every backup for nothing anyone reads.
    """
    kept = {key: value[:512] for key, value in headers.items() if key.lower() in _KEPT_HEADERS}
    return kept or None


def _redacted_chain(chain: list[dict[str, object]]) -> list[dict[str, object]] | None:
    """The redirect chain with credential-bearing query values masked."""
    if not chain:
        return None
    return [
        {
            key: redact_url(value) if key in ("from", "to") and isinstance(value, str) else value
            for key, value in hop.items()
        }
        for hop in chain
    ]


def _json(value: object) -> object:
    """Pass a Python structure to a JSONB parameter without SQLAlchemy type coercion."""
    if value is None:
        return None
    import json

    return json.dumps(value)


__all__ = ["REDACTED", "RecordedFetch", "conditional_headers_for", "record_outcome", "redact_url"]
