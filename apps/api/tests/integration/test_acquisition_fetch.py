"""Transport behaviour, against a local fixture server (Step 5B sections 8-14, 19-21).

**No test here contacts a university.** Every URL is the loopback fixture server, and
the one thing that would let the real fetcher reach loopback -- the resolver -- is
injected per test rather than switched on by a flag. The SSRF tests do the opposite:
they use the same injected resolver, which delegates anything that is not the fixture
server's own address to the genuine guard, so a redirect to `127.0.0.1:6379` is refused
by production code rather than by the test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from app.db.enums import FetchStatus
from app.domains.acquisition.fetcher import (
    ConditionalHeaders,
    FetchPolicy,
    StaticHttpFetcher,
)
from app.domains.acquisition.netsafety import (
    UnsafeTargetError,
    VettedTarget,
    check_url_shape,
    resolve_and_validate,
)
from tests.integration.fixtures_http import (
    ETAG_ONE,
    HTML_ONE,
    HTML_TWO,
    LAST_MODIFIED_ONE,
    PDF_BYTES,
    TEST_MAX_BYTES,
    FixtureServer,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def server() -> Any:
    with FixtureServer() as fixture:
        yield fixture


def fixture_resolver(server: FixtureServer) -> Callable[..., Awaitable[VettedTarget]]:
    """Permit the fixture server's own address; delegate everything else.

    This is the whole test seam. It means the redirect-into-private tests exercise the
    real `resolve_and_validate`, because `127.0.0.1:6379` is not the fixture's address
    and falls through to it.
    """
    allowed_port = int(server.base_url.rsplit(":", 1)[1])

    async def resolve(url: str, *, timeout_seconds: float = 5.0) -> VettedTarget:
        scheme, host, port = check_url_shape_permissive(url)
        if host == "127.0.0.1" and port == allowed_port:
            return VettedTarget(host=host, port=port, scheme=scheme, addresses=("127.0.0.1",))
        return await resolve_and_validate(url, timeout_seconds=timeout_seconds)

    return resolve


def check_url_shape_permissive(url: str) -> tuple[str, str, int]:
    """Scheme/host/port without the IP-literal refusal, for the fixture's own URL."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return (
        parts.scheme.lower(),
        (parts.hostname or "").lower(),
        parts.port or (443 if parts.scheme == "https" else 80),
    )


def make_fetcher(server: FixtureServer, **policy: Any) -> StaticHttpFetcher:
    defaults: dict[str, Any] = {
        "max_bytes": TEST_MAX_BYTES,
        "connect_timeout_seconds": 2.0,
        "read_timeout_seconds": 3.0,
        "total_timeout_seconds": 6.0,
        "verify_tls": False,
    }
    defaults.update(policy)
    return StaticHttpFetcher(FetchPolicy(**defaults), resolver=fixture_resolver(server))


def fetch(server: FixtureServer, path: str, **kwargs: Any) -> Any:
    fetcher = kwargs.pop("fetcher", None) or make_fetcher(server)
    return asyncio.run(fetcher.fetch(server.url(path), **kwargs))


# ===========================================================================
# 200s, bytes and hashes
# ===========================================================================


def test_a_page_is_fetched_and_hashed(server: FixtureServer) -> None:
    """Non-vacuity guard for everything below."""
    outcome = fetch(server, "/ok")
    assert outcome.status is FetchStatus.OK
    assert outcome.http_status == 200
    assert outcome.content == HTML_ONE
    assert outcome.byte_size == len(HTML_ONE)
    import hashlib

    assert outcome.content_hash == hashlib.sha256(HTML_ONE).hexdigest()
    assert outcome.content_type is not None and outcome.content_type.startswith("text/html")


def test_only_technical_metadata_is_read_from_the_page(server: FixtureServer) -> None:
    """Requirement 20. A title and a charset, and nothing that is a university fact."""
    outcome = fetch(server, "/ok")
    assert outcome.technical_metadata["title"] == "Fees 2027"
    assert outcome.technical_metadata["declared_charset"] == "utf-8"
    assert set(outcome.technical_metadata) <= {"title", "declared_charset", "content_type"}


def test_identical_bytes_hash_identically(server: FixtureServer) -> None:
    """Requirement 22, at the transport layer: the key is deterministic."""
    first = fetch(server, "/ok")
    second = fetch(server, "/ok")
    assert first.content_hash == second.content_hash
    from app.domains.acquisition.storage import storage_key_for

    assert storage_key_for(first.content_hash) == storage_key_for(second.content_hash)


def test_changed_bytes_hash_differently(server: FixtureServer) -> None:
    first = fetch(server, "/changing")
    second = fetch(server, "/changing")
    assert first.content == HTML_ONE
    assert second.content == HTML_TWO
    assert first.content_hash != second.content_hash


# ===========================================================================
# Conditional requests
# ===========================================================================


def test_an_etag_304_carries_no_body(server: FixtureServer) -> None:
    """Requirement 12. The server said nothing changed; nothing is invented."""
    outcome = fetch(server, "/etag", conditional=ConditionalHeaders(etag=ETAG_ONE))
    assert outcome.status is FetchStatus.UNCHANGED
    assert outcome.http_status == 304
    assert outcome.content is None
    assert outcome.content_hash is None
    assert outcome.conditional_request_sent is True
    assert server.log.last_headers()["if-none-match"] == ETAG_ONE


def test_a_last_modified_304_also_works(server: FixtureServer) -> None:
    outcome = fetch(
        server, "/modified", conditional=ConditionalHeaders(last_modified=LAST_MODIFIED_ONE)
    )
    assert outcome.status is FetchStatus.UNCHANGED
    assert outcome.http_status == 304
    assert outcome.content is None


def test_a_200_with_identical_bytes_is_not_a_304(server: FixtureServer) -> None:
    """The distinction requirement 9 asks for, at the transport layer.

    Both observations saw the same bytes. Only one of them was told so by the server,
    and the difference is visible: a 200 carries a body and a hash, a 304 carries
    neither.
    """
    first = fetch(server, "/etag")
    assert first.status is FetchStatus.OK
    assert first.http_status == 200
    assert first.content_hash is not None

    second = fetch(server, "/etag", conditional=ConditionalHeaders(etag=first.etag))
    assert second.status is FetchStatus.UNCHANGED
    assert second.http_status == 304
    assert second.content_hash is None
    # Same page, same bytes, two genuinely different transport observations.
    assert first.etag == ETAG_ONE


def test_no_validator_means_an_unconditional_request(server: FixtureServer) -> None:
    """A fabricated `If-None-Match` would invite a 304 we could not interpret."""
    outcome = fetch(server, "/ok", conditional=ConditionalHeaders())
    assert outcome.conditional_request_sent is False
    assert "if-none-match" not in server.log.last_headers()


# ===========================================================================
# Redirects
# ===========================================================================


def test_a_redirect_is_followed_and_the_chain_kept(server: FixtureServer) -> None:
    outcome = fetch(server, "/redirect-once")
    assert outcome.status is FetchStatus.OK
    assert outcome.content == HTML_ONE
    assert len(outcome.redirect_chain) == 1
    assert outcome.redirect_chain[0]["status"] == 302
    assert outcome.effective_url.endswith("/ok")
    assert outcome.requested_url.endswith("/redirect-once")


def test_a_multi_hop_chain_is_kept_in_order(server: FixtureServer) -> None:
    """Requirement 13. Every hop, so a reviewer can see where a page actually went."""
    outcome = fetch(server, "/redirect-chain")
    assert outcome.status is FetchStatus.OK
    assert [hop["status"] for hop in outcome.redirect_chain] == [301, 302, 302]
    assert outcome.redirect_chain[0]["from"].endswith("/redirect-chain")
    assert outcome.redirect_chain[-1]["to"].endswith("/ok")


def test_a_redirect_loop_is_bounded(server: FixtureServer) -> None:
    outcome = fetch(server, "/redirect-loop", fetcher=make_fetcher(server, max_redirects=3))
    assert outcome.status is FetchStatus.HTTP_ERROR
    assert outcome.error_class == "TooManyRedirects"
    # Three hops followed, and the fourth recorded but refused: the chain says where
    # it was still trying to go, which is what a reviewer wants to see.
    assert len(outcome.redirect_chain) == 4
    assert server.log.count("/redirect-loop") == 4


def test_a_redirect_into_a_private_network_is_refused(server: FixtureServer) -> None:
    """Requirement 14, against the real guard.

    The fixture resolver only permits its own address, so `127.0.0.1:6379` falls
    through to `resolve_and_validate` -- this is production code refusing, not the
    test. The chain is kept, so the attempt is visible rather than silently lost.
    """
    outcome = fetch(server, "/redirect-private")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_class == "UnsafeTarget"
    assert "loopback" in outcome.error_detail or "IP literal" in outcome.error_detail
    assert len(outcome.redirect_chain) == 1
    assert "127.0.0.1:6379" in outcome.redirect_chain[0]["to"]


def test_a_redirect_to_cloud_metadata_is_refused(server: FixtureServer) -> None:
    """The one that leaks credentials rather than merely reaching something internal."""
    outcome = fetch(server, "/redirect-metadata")
    assert outcome.status is FetchStatus.BLOCKED
    assert outcome.error_class == "UnsafeTarget"
    assert "169.254.169.254" in outcome.error_detail


def test_a_redirect_to_an_unsafe_scheme_is_refused(server: FixtureServer) -> None:
    """Requirement 17. `Location: file:///etc/passwd` is not a page."""
    outcome = fetch(server, "/redirect-scheme")
    assert outcome.status is FetchStatus.BLOCKED
    assert "not http or https" in outcome.error_detail


# ===========================================================================
# Size, type and status
# ===========================================================================


def test_an_oversized_body_is_refused_mid_stream(server: FixtureServer) -> None:
    """Requirement 18. Abandoned during transfer, not buffered and then rejected."""
    outcome = fetch(server, "/oversized")
    assert outcome.status is FetchStatus.HTTP_ERROR
    assert outcome.error_class == "OversizedResponse"
    assert outcome.content is None


def test_an_oversized_declared_length_is_refused_before_transfer(
    server: FixtureServer,
) -> None:
    outcome = fetch(server, "/oversized-declared")
    assert outcome.status is FetchStatus.HTTP_ERROR
    assert outcome.error_class == "OversizedResponse"
    assert "Content-Length" in outcome.error_detail


def test_an_unexpected_content_type_is_refused(server: FixtureServer) -> None:
    outcome = fetch(server, "/bad-mime")
    assert outcome.status is FetchStatus.HTTP_ERROR
    assert outcome.error_class == "UnsupportedContentType"
    assert "application/zip" in outcome.error_detail


def test_a_pdf_is_downloaded_and_not_parsed(server: FixtureServer) -> None:
    """Requirement 19. Bytes and a hash; no OCR, no tables, no claims."""
    outcome = fetch(server, "/pdf")
    assert outcome.status is FetchStatus.OK
    assert outcome.content == PDF_BYTES
    assert outcome.content_type == "application/pdf"
    # No page-derived metadata at all for a PDF: the title extractor is HTML-only,
    # and nothing else reads the bytes.
    assert outcome.technical_metadata == {"content_type": "application/pdf"}


@pytest.mark.parametrize(
    ("path", "status", "error"),
    [
        ("/404", FetchStatus.HTTP_ERROR, "HTTP404"),
        ("/500", FetchStatus.HTTP_ERROR, "HTTP500"),
        ("/403", FetchStatus.BLOCKED, "HTTP403"),
        ("/429", FetchStatus.RATE_LIMITED, "RateLimited"),
    ],
)
def test_http_statuses_are_classified(
    server: FixtureServer, path: str, status: FetchStatus, error: str
) -> None:
    outcome = fetch(server, path)
    assert outcome.status is status
    assert outcome.error_class == error


def test_a_429_carries_its_retry_after(server: FixtureServer) -> None:
    """Requirement 20 of the test list. The server's number, taken as given."""
    outcome = fetch(server, "/429")
    # Step 5B.2: a throttle, not a refusal.
    assert outcome.status is FetchStatus.RATE_LIMITED
    assert outcome.retry_after_seconds == 120.0


def test_a_slow_response_times_out(server: FixtureServer) -> None:
    server.state["slow_seconds"] = 3.0
    outcome = fetch(
        server,
        "/slow",
        fetcher=make_fetcher(server, read_timeout_seconds=0.5, total_timeout_seconds=2.0),
    )
    assert outcome.status is FetchStatus.TIMEOUT
    assert outcome.duration_ms is not None


# ===========================================================================
# The guard itself
# ===========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://127.1/x",
        "http://0177.0.0.1/x",
        "http://2130706433/x",
        "http://[::1]/x",
        "http://[::ffff:127.0.0.1]/x",
        "http://10.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.100.100.200/x",
        "http://[fd00::1]/x",
        "http://metadata.google.internal/x",
        "file:///etc/passwd",
        "gopher://example.ac.uk/x",
        "https://user:pass@example.ac.uk/x",
    ],
)
def test_unsafe_targets_are_refused_before_any_connection(url: str) -> None:
    """Requirements 15-17, including the Step 4 numeric-host fixes.

    Shape-only, so no DNS is consulted and nothing is contacted: a URL that cannot
    pass this never reaches the resolver, let alone a socket.
    """
    with pytest.raises(UnsafeTargetError):
        check_url_shape(url)


def test_an_ordinary_university_url_passes_the_shape_check() -> None:
    """Non-vacuity: the guard is not simply refusing everything."""
    assert check_url_shape("https://www.imperial.ac.uk/study/apply/") == (
        "https",
        "www.imperial.ac.uk",
        443,
    )
