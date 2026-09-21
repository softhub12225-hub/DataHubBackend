"""Fetching a page safely, and classifying what happened (Step 5B sections 8-14).

WHAT THIS DOES AND DOES NOT DO
==============================
It retrieves bytes and describes the transport. It does **not** read the page: no
programme name, no fee, no deadline, no test score, no requirement is extracted here
or anywhere reachable from here. The only page-derived values it keeps are the
`<title>` and the declared charset, which are operational aids for the human who has
to decide whether a URL is the page the collector meant — a reviewer looking at a list
of 319 URLs needs *something* beyond the address, and a title is the cheapest honest
signal. Requirement 20 forbids business parsing, not a title.

REDIRECTS ARE FOLLOWED MANUALLY, ON PURPOSE
===========================================
`follow_redirects=True` would have httpx resolve and connect to each hop itself, which
is precisely the check we must not skip. So each hop is: validate the shape, resolve,
refuse every non-public answer, pin the connection, request, and look at the
`Location` again. A hop into a private network is a `BLOCKED` outcome with the chain
preserved, not an exception that loses the trail.

A university page redirecting to an external application portal is allowed and
recorded. It earns that host nothing: `effective_host_differs` is surfaced for the
source reviewer and acted on by no code.

POLITENESS IS A CONSTRAINT, NOT A SETTING TO TUNE AWAY
======================================================
120 hosts serve the pilot's 319 pages, and one of them serves eleven. Per-host
concurrency of one with a configurable floor between requests means a university sees a
paced series rather than a burst. 429 and `Retry-After` are obeyed as stated. There is
no proxy rotation, no user-agent cycling and no CAPTCHA handling, and their absence is
deliberate: those exist to defeat a decision the site has made, and a system whose
whole claim is "we only use what the university published" cannot also be a system that
works around the university saying no.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from app.core.clock import utcnow
from app.core.logging import get_logger
from app.db.enums import FetchStatus
from app.domains.acquisition.netsafety import (
    MAX_REDIRECTS,
    DnsTemporaryError,
    NameNotResolvedError,
    UnsafeTargetError,
    VettedTarget,
    resolve_and_validate,
)

logger = get_logger(__name__)

#: How a URL becomes a vetted connection target. `resolve_and_validate` in production;
#: see `StaticHttpFetcher.__init__` for why this is a parameter rather than a flag.
Resolver = Callable[..., Awaitable[VettedTarget]]

#: Identifies us honestly, and gives a webmaster a way to reach a person. A crawler
#: that hides what it is has already decided to ignore the answer.
USER_AGENT = (
    "OverseasUniDataHub/0.1 (+https://example.invalid/crawler; educational data verification)"
)

#: Hard ceiling on a response body. Well past any prospectus PDF, far short of what
#: would exhaust a worker. Enforced while streaming, so an oversized body is abandoned
#: mid-transfer rather than after it has been buffered.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: Read in chunks so the size limit can be enforced during the transfer.
CHUNK_BYTES = 64 * 1024

#: What we are willing to store. Anything else is recorded and refused: a page that
#: suddenly serves `application/zip` is a finding, not a document.
ALLOWED_CONTENT_TYPES: tuple[str, ...] = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/pdf",
    "application/json",
    "text/xml",
    "application/xml",
)

#: Statuses that mean "go somewhere else". 304 is deliberately absent: it is an answer
#: to a conditional request, not a redirect, and httpx's `is_redirect` conflates them.
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

#: Markers of a bot-protection interstitial served with HTTP 200.
#:
#: Found by the Step 5B.1 smoke run: NUS's academic calendar answered 200 with 212
#: bytes of Imperva challenge, and the pipeline stored it as the page. That is the
#: worst possible failure mode here -- not an error anyone would notice, but a
#: plausible-looking snapshot whose content is not what the university published.
#: A later extractor would find nothing and report the university as publishing
#: nothing, which is a false `OFFICIALLY_NOT_PUBLISHED` waiting to happen.
#:
#: Recognising a block is the opposite of circumventing one. Nothing here retries
#: differently, spoofs anything or attempts to solve a challenge; the response is
#: recorded as BLOCKED, the source stops being scheduled, and a human decides.
_CHALLENGE_MARKERS: tuple[bytes, ...] = (
    b"_Incapsula_Resource",  # Imperva
    b"Just a moment...",  # Cloudflare interstitial title
    b"cf-browser-verification",  # Cloudflare
    b"/cdn-cgi/challenge-platform",  # Cloudflare
    b"Attention Required! | Cloudflare",
    b"DDoS protection by",
)

#: A challenge page is tiny. Requiring both the marker *and* a small body keeps a real
#: prospectus that happens to mention one of these strings from being misread as a
#: block -- the detector must not invent failures any more than it may hide them.
MAX_CHALLENGE_BYTES = 8 * 1024

_TITLE = re.compile(rb"<title[^>]*>(.{0,400}?)</title>", re.IGNORECASE | re.DOTALL)
_META_CHARSET = re.compile(rb'charset=["\']?\s*([a-zA-Z0-9_\-]{2,40})', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """Timeouts, limits and politeness. All configurable; none optional."""

    #: 15s rather than 10s: the Step 5B.1 smoke run timed out connecting to UBC's
    #: calendar, which redirects four times and whose origin is slow from here. The
    #: same URL succeeded at 15s both pinned and unpinned, so this is a distance and
    #: hop-count budget, not a defect in the pinning.
    connect_timeout_seconds: float = 15.0
    read_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 60.0
    max_redirects: int = MAX_REDIRECTS
    max_bytes: int = MAX_RESPONSE_BYTES
    user_agent: str = USER_AGENT
    verify_tls: bool = True
    #: Pin the socket to the address we validated. Off only in tests against a local
    #: fixture server, where the address *is* loopback and pinning is meaningless.
    pin_addresses: bool = True


@dataclass(slots=True)
class FetchOutcome:
    """Everything one attempt observed. The complete input to the terminal write."""

    status: FetchStatus
    requested_url: str
    effective_url: str
    http_status: int | None = None
    error_class: str | None = None
    error_detail: str | None = None
    content: bytes | None = None
    content_hash: str | None = None
    byte_size: int | None = None
    content_type: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    redirect_chain: list[dict[str, object]] = field(default_factory=list)
    technical_metadata: dict[str, object] = field(default_factory=dict)
    conditional_request_sent: bool = False
    retry_after_seconds: float | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    duration_ms: int | None = None
    #: The final host, when it is not the one we asked for. Surfaced to the source
    #: reviewer; nothing acts on it.
    effective_host_differs: bool = False

    @property
    def is_success(self) -> bool:
        return self.status in (FetchStatus.OK, FetchStatus.UNCHANGED)


@dataclass(frozen=True, slots=True)
class ConditionalHeaders:
    """What we already hold, so the server can say "still the same"."""

    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None

    def as_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.etag:
            headers["If-None-Match"] = self.etag
        if self.last_modified:
            headers["If-Modified-Since"] = self.last_modified
        return headers


class StaticHttpFetcher:
    """Plain HTTP acquisition. One instance per worker process.

    Holds no per-source state: politeness is the scheduler's job (`HostGate`), and
    mixing the two here would make a single fetch's behaviour depend on what another
    coroutine did.
    """

    fetcher_name = "STATIC"

    def __init__(
        self,
        policy: FetchPolicy | None = None,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self.policy = policy or FetchPolicy()
        #: The seam the tests use, and the reason there is no `allow_private` flag.
        #:
        #: A boolean that disables SSRF checking would exist in production code, be
        #: settable from configuration eventually, and be exactly the switch someone
        #: flips at 3am. Instead the *resolver* is injectable: the test resolver
        #: permits its own loopback fixture server and delegates everything else to
        #: the real one, so the redirect-into-private tests still run against the
        #: genuine guard. Production never passes this argument.
        self._resolve = resolver or resolve_and_validate

    async def fetch(
        self, url: str, *, conditional: ConditionalHeaders | None = None
    ) -> FetchOutcome:
        """Retrieve `url`, following redirects with a safety check at every hop."""
        started = time.monotonic()
        outcome = FetchOutcome(
            status=FetchStatus.HTTP_ERROR,
            requested_url=url,
            effective_url=url,
            conditional_request_sent=bool(conditional and conditional.as_headers()),
        )
        try:
            await asyncio.wait_for(
                self._fetch_chain(url, conditional, outcome),
                timeout=self.policy.total_timeout_seconds,
            )
        except TimeoutError:
            outcome.status = FetchStatus.TIMEOUT
            outcome.error_class = "TotalTimeout"
            outcome.error_detail = (
                f"exceeded the total budget of {self.policy.total_timeout_seconds}s"
            )
        except UnsafeTargetError as exc:
            # A refusal, not a failure: we resolved the target and declined it. Stays
            # permanent and is never retried automatically -- the answer will not
            # change, and a client that keeps trying an address it has refused is one
            # bad DNS answer away from doing what the refusal prevented.
            outcome.status = FetchStatus.BLOCKED
            outcome.error_class = "UnsafeTarget"
            outcome.error_detail = str(exc)
        except NameNotResolvedError as exc:
            # The name does not exist. Retrying cannot invent it, so this is a URL for
            # a person to look at rather than a schedule to keep.
            outcome.status = FetchStatus.NAME_NOT_RESOLVED
            outcome.error_class = "NameNotResolved"
            outcome.error_detail = str(exc)
        except DnsTemporaryError as exc:
            # We learned nothing about the target, so nothing is concluded about it.
            outcome.status = FetchStatus.DNS_TEMPORARY
            outcome.error_class = "DnsTemporaryFailure"
            outcome.error_detail = str(exc)
        except httpx.TimeoutException as exc:
            outcome.status = FetchStatus.TIMEOUT
            outcome.error_class = type(exc).__name__
            outcome.error_detail = str(exc) or "timed out"
        except ssl.SSLError as exc:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = "SSLError"
            outcome.error_detail = str(exc)
        except httpx.TransportError as exc:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = type(exc).__name__
            outcome.error_detail = str(exc) or "transport error"
        except OversizedResponseError as exc:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = "OversizedResponse"
            outcome.error_detail = str(exc)
        except UnsupportedContentTypeError as exc:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = "UnsupportedContentType"
            outcome.error_detail = str(exc)
        except httpx.HTTPError as exc:  # pragma: no cover - the residual httpx surface
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = type(exc).__name__
            outcome.error_detail = str(exc)

        outcome.finished_at = utcnow()
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        outcome.effective_host_differs = _host_of(outcome.effective_url) != _host_of(url)
        return outcome

    async def _fetch_chain(
        self, url: str, conditional: ConditionalHeaders | None, outcome: FetchOutcome
    ) -> None:
        current = url
        headers = {
            "User-Agent": self.policy.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "gzip, deflate",
        }
        if conditional:
            headers.update(conditional.as_headers())

        for hop in range(self.policy.max_redirects + 1):
            # Every hop, including the first. A `Location` header has been through no
            # validation at all: it arrived from the open internet.
            target = await self._resolve(
                current, timeout_seconds=self.policy.connect_timeout_seconds
            )
            async with self._client(target) as client:
                request = client.build_request("GET", current, headers=headers)
                response = await client.send(request, stream=True)
                try:
                    outcome.http_status = response.status_code
                    outcome.effective_url = current

                    # `httpx.Response.is_redirect` is true for any 3xx, including
                    # 304 -- which is not a redirect, carries no Location and must be
                    # handled as a conditional-request answer. Naming the statuses is
                    # what keeps the two apart.
                    location = response.headers.get("location")
                    if response.status_code in REDIRECT_STATUSES:
                        if not location:
                            outcome.status = FetchStatus.HTTP_ERROR
                            outcome.error_class = "RedirectWithoutLocation"
                            outcome.error_detail = f"{response.status_code} with no Location"
                            return
                        nxt = urljoin(current, location)
                        outcome.redirect_chain.append(
                            {"status": response.status_code, "from": current, "to": nxt}
                        )
                        # The chain records every redirect the server sent, including
                        # the one we decline to follow -- a reviewer needs to see where
                        # it was trying to go, which is exactly the hop we refused.
                        if hop == self.policy.max_redirects:
                            outcome.status = FetchStatus.HTTP_ERROR
                            outcome.error_class = "TooManyRedirects"
                            outcome.error_detail = f"exceeded {self.policy.max_redirects} redirects"
                            return
                        # No separate shape check: `_resolve` performs one as its
                        # first act, and doing it twice through two different code
                        # paths is how the two drift apart.
                        current = nxt
                        continue

                    await self._read_response(response, outcome)
                    return
                finally:
                    await response.aclose()

    def _client(self, target: VettedTarget) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            connect=self.policy.connect_timeout_seconds,
            read=self.policy.read_timeout_seconds,
            write=self.policy.read_timeout_seconds,
            pool=self.policy.connect_timeout_seconds,
        )
        transport: httpx.AsyncBaseTransport | None = None
        if self.policy.pin_addresses:
            transport = PinnedTransport(
                address=target.primary, verify=self.policy.verify_tls, timeout=timeout
            )
        return httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # every hop is revalidated by hand; see the header
            transport=transport,
            verify=self.policy.verify_tls,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )

    async def _read_response(self, response: httpx.Response, outcome: FetchOutcome) -> None:
        outcome.response_headers = {key.lower(): value for key, value in response.headers.items()}
        outcome.etag = response.headers.get("etag")
        outcome.last_modified = response.headers.get("last-modified")
        outcome.content_type = response.headers.get("content-type")
        retry_after = response.headers.get("retry-after")
        if retry_after:
            outcome.retry_after_seconds = _parse_retry_after(retry_after)

        status = response.status_code

        if status == 304:
            # No body, and none is invented. The caller records an UNCHANGED run
            # against the blob it already holds.
            outcome.status = FetchStatus.UNCHANGED
            return

        if status == 429:
            # A throttle, not a refusal. This used to be `BLOCKED`, which set
            # `fetch_eligibility = 'BLOCKED'` permanently and skipped the retry branch
            # entirely -- so the first time any host rate-limited us, that page left
            # the schedule for good and the `Retry-After` we had just parsed was never
            # used (Step 5B.2 §1). The site asked us to wait; waiting is the response.
            outcome.status = FetchStatus.RATE_LIMITED
            outcome.error_class = "RateLimited"
            outcome.error_detail = "the site asked us to slow down (429)"
            return

        if status in (403, 401):
            outcome.status = FetchStatus.BLOCKED
            outcome.error_class = f"HTTP{status}"
            outcome.error_detail = "access refused by the site"
            return

        if status >= 400:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = f"HTTP{status}"
            outcome.error_detail = response.reason_phrase or f"HTTP {status}"
            return

        if status != 200:
            outcome.status = FetchStatus.HTTP_ERROR
            outcome.error_class = f"HTTP{status}"
            outcome.error_detail = f"unexpected status {status}"
            return

        declared = _declared_length(response.headers.get("content-length"))
        if declared is not None and declared > self.policy.max_bytes:
            raise OversizedResponseError(
                f"Content-Length {declared} exceeds the {self.policy.max_bytes}-byte limit"
            )
        base_type = (outcome.content_type or "").split(";")[0].strip().lower()
        if base_type and base_type not in ALLOWED_CONTENT_TYPES:
            raise UnsupportedContentTypeError(f"content type {base_type!r} is not stored")

        digest = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes(CHUNK_BYTES):
            total += len(chunk)
            if total > self.policy.max_bytes:
                # Abandoned mid-transfer: the point of a streaming limit is not
                # buffering the thing we are refusing.
                raise OversizedResponseError(
                    f"response exceeded the {self.policy.max_bytes}-byte limit"
                )
            digest.update(chunk)
            chunks.append(chunk)

        body = b"".join(chunks)

        challenge = _challenge_marker(body, base_type)
        if challenge is not None:
            # A 200 carrying a challenge is a refusal wearing a success code. Recorded
            # as BLOCKED with the bytes discarded: storing them would put a WAF
            # interstitial in the evidence record as though it were the page.
            outcome.status = FetchStatus.BLOCKED
            outcome.error_class = "ChallengeInterstitial"
            outcome.error_detail = (
                f"HTTP 200 with a bot-protection interstitial ({challenge}); "
                f"{total} bytes. Not stored as evidence."
            )
            outcome.byte_size = total
            return

        outcome.content = body
        outcome.content_hash = digest.hexdigest()
        outcome.byte_size = total
        outcome.status = FetchStatus.OK
        outcome.technical_metadata = _technical_metadata(body, base_type)


class OversizedResponseError(RuntimeError):
    """The body exceeded the configured limit and was abandoned."""


class UnsupportedContentTypeError(RuntimeError):
    """The server returned something we do not store."""


class PinnedTransport(httpx.AsyncHTTPTransport):
    """Connect to a validated address while keeping the hostname for SNI and `Host`.

    This is what closes the gap between "we resolved and checked this name" and "we
    opened a socket". httpx would otherwise resolve again at connect time, and the
    second answer is the one an attacker controls.

    Implemented by rewriting the request URL's host to the vetted address and setting
    `Host` and `sni_hostname` back to the original name, which is the documented way
    to do this with httpx and keeps certificate verification against the real name.
    """

    def __init__(self, *, address: str, verify: bool, timeout: httpx.Timeout) -> None:
        super().__init__(verify=verify, retries=0)
        self._address = address
        self._timeout = timeout

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        if original_host and self._address and original_host != self._address:
            literal = f"[{self._address}]" if ":" in self._address else self._address
            request.url = request.url.copy_with(host=literal)
            request.headers["Host"] = original_host
            request.extensions = {**request.extensions, "sni_hostname": original_host}
        return await super().handle_async_request(request)


def _host_of(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except Exception:  # a malformed URL has no host, which is the answer
        return ""


def _declared_length(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_retry_after(value: str) -> float | None:
    """Seconds to wait, from either `Retry-After` form.

    Obeyed as the site stated it. A server saying "come back in 300 seconds" is not
    making a suggestion, and the alternative -- retrying sooner -- is how a temporary
    throttle becomes a permanent block.
    """
    value = value.strip()
    try:
        return max(0.0, float(int(value)))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    delta: float = (when - utcnow()).total_seconds()
    return max(0.0, delta)


def _challenge_marker(body: bytes, base_type: str) -> str | None:
    """The name of the challenge this body is, or None if it is a real response."""
    if base_type not in ("text/html", "application/xhtml+xml", ""):
        return None
    if len(body) > MAX_CHALLENGE_BYTES:
        return None
    for marker in _CHALLENGE_MARKERS:
        if marker in body:
            return marker.decode("ascii", "replace")
    return None


def _technical_metadata(body: bytes, base_type: str) -> dict[str, object]:
    """`<title>` and declared charset. Operational only -- see the module header.

    Bounded on purpose: the first 64 KiB, a 400-character match limit, and the result
    truncated again. A page whose title is a megabyte of markup is trying something,
    and either way none of this is a fact about a university.
    """
    metadata: dict[str, object] = {"content_type": base_type or None}
    if base_type not in ("text/html", "application/xhtml+xml"):
        return metadata

    head = body[:65536]
    match = _TITLE.search(head)
    if match:
        raw = match.group(1)
        try:
            title = raw.decode("utf-8", errors="replace")
        except Exception:  # defensive; decode with replace does not raise
            title = ""
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            metadata["title"] = title[:300]
    charset = _META_CHARSET.search(head)
    if charset:
        metadata["declared_charset"] = charset.group(1).decode("ascii", errors="replace")[:40]
    return metadata


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "CHUNK_BYTES",
    "MAX_CHALLENGE_BYTES",
    "MAX_RESPONSE_BYTES",
    "REDIRECT_STATUSES",
    "USER_AGENT",
    "ConditionalHeaders",
    "FetchOutcome",
    "FetchPolicy",
    "OversizedResponseError",
    "PinnedTransport",
    "Resolver",
    "StaticHttpFetcher",
    "UnsupportedContentTypeError",
]
