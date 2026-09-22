"""A deterministic HTTP server for acquisition tests (Step 5B sections 26-27).

WHY A REAL SOCKET RATHER THAN A MOCKED TRANSPORT
================================================
The things under test are transport behaviours: streaming size limits, redirect
revalidation, conditional requests, `Retry-After`, timeouts. A mocked transport would
test that the code calls httpx the way the test expects it to — which is a restatement
of the code, not a check on it. A real server on loopback exercises the actual client,
the actual redirect loop and the actual byte counting.

**No automated test ever contacts a university.** Every URL here is `127.0.0.1`, which
is also why `FetchPolicy.pin_addresses` and the SSRF loopback rule have to be switched
off for these specific tests — the fixture *is* loopback, and the rule that forbids it
is the rule being relied on everywhere else. The tests that check SSRF do the opposite:
they leave the guard on and assert the refusal.
"""

from __future__ import annotations

import gzip
import threading
import time
from dataclasses import dataclass, field
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Deliberately small so an "oversized" test does not move 32 MB through a socket.
TEST_MAX_BYTES = 64 * 1024

HTML_ONE = b"<html><head><title>Fees 2027</title><meta charset='utf-8'></head><body>A</body></html>"
HTML_TWO = b"<html><head><title>Fees 2028</title></head><body>B</body></html>"
PDF_BYTES = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"

ETAG_ONE = '"etag-one"'
LAST_MODIFIED_ONE = "Wed, 15 Jan 2027 10:00:00 GMT"


@dataclass
class RouteLog:
    """What the server was asked, so a test can assert the client's behaviour."""

    paths: list[str] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    def count(self, path: str) -> int:
        return sum(1 for p in self.paths if p == path)

    def last_headers(self) -> dict[str, str]:
        return self.headers[-1] if self.headers else {}


class _Handler(BaseHTTPRequestHandler):
    server_version = "FixtureHTTP/1.0"
    log: RouteLog
    state: dict[str, Any]

    def log_message(self, format: str, *args: Any) -> None:
        """Silence the default stderr access log; pytest output is noisy enough."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        path = self.path
        type(self).log.paths.append(path)
        type(self).log.headers.append({k.lower(): v for k, v in self.headers.items()})
        state = type(self).state

        if path == "/ok":
            self._send(200, HTML_ONE, "text/html; charset=utf-8")
        elif path == "/changing":
            # First call one body, later calls another: "the page changed" as a real
            # observation rather than a reconstructed one.
            body = HTML_ONE if state.get("changing_calls", 0) == 0 else HTML_TWO
            state["changing_calls"] = state.get("changing_calls", 0) + 1
            self._send(200, body, "text/html")
        elif path == "/etag":
            if self.headers.get("If-None-Match") == ETAG_ONE:
                self.send_response(304)
                self.send_header("ETag", ETAG_ONE)
                self.end_headers()
            else:
                self._send(200, HTML_ONE, "text/html", extra={"ETag": ETAG_ONE})
        elif path == "/modified":
            if self.headers.get("If-Modified-Since") == LAST_MODIFIED_ONE:
                self.send_response(304)
                self.end_headers()
            else:
                self._send(200, HTML_ONE, "text/html", extra={"Last-Modified": LAST_MODIFIED_ONE})
        elif path == "/redirect-once":
            self._redirect(302, "/ok")
        elif path == "/redirect-chain":
            self._redirect(301, "/redirect-hop2")
        elif path == "/redirect-hop2":
            self._redirect(302, "/redirect-hop3")
        elif path == "/redirect-hop3":
            self._redirect(302, "/ok")
        elif path == "/redirect-loop":
            self._redirect(302, "/redirect-loop")
        elif path == "/redirect-private":
            # The attack: a public page sending the crawler at an internal address.
            self._redirect(302, "http://127.0.0.1:6379/")
        elif path == "/redirect-metadata":
            self._redirect(302, "http://169.254.169.254/latest/meta-data/")
        elif path == "/redirect-scheme":
            self._redirect(302, "file:///etc/passwd")
        elif path == "/pdf":
            self._send(200, PDF_BYTES, "application/pdf")
        elif path == "/oversized":
            self._send(200, b"x" * (TEST_MAX_BYTES * 3), "text/html")
        elif path == "/oversized-declared":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(TEST_MAX_BYTES * 10))
            self.end_headers()
            self.wfile.write(b"x" * 16)
        elif path == "/bad-mime":
            self._send(200, b"PK\x03\x04", "application/zip")
        elif path == "/404":
            self._send(404, b"gone", "text/html")
        elif path == "/500":
            self._send(500, b"boom", "text/html")
        elif path == "/429":
            self._send(429, b"slow down", "text/html", extra={"Retry-After": "120"})
        elif path == "/403":
            self._send(403, b"forbidden", "text/html")
        elif path == "/429-date":
            # The other legal `Retry-After` form. Computed per request so the date is
            # always in the future, which a frozen literal would stop being.
            when = formatdate(time.time() + 240, usegmt=True)
            self._send(429, b"slow down", "text/html", extra={"Retry-After": when})
        elif path == "/429-garbage":
            # Real servers send this. "soon" parses as neither an integer nor a date,
            # and the client has to fall back to its own conservative number rather
            # than treat the absence as "retry immediately".
            self._send(429, b"slow down", "text/html", extra={"Retry-After": "soon"})
        elif path == "/429-bare":
            self._send(429, b"slow down", "text/html")
        elif path == "/slow":
            time.sleep(state.get("slow_seconds", 2.0))
            self._send(200, HTML_ONE, "text/html")
        elif path == "/flaky":
            # Fails once, then succeeds: retry-after-failure, observably.
            state["flaky_calls"] = state.get("flaky_calls", 0) + 1
            if state["flaky_calls"] == 1:
                self._send(500, b"boom", "text/html")
            else:
                self._send(200, HTML_ONE, "text/html")
        elif path == "/challenge-incapsula":
            # What NUS actually served in the Step 5B.1 smoke run: HTTP 200, 212
            # bytes, an Imperva challenge and no content.
            self._send(
                200,
                b'<html><head><META NAME="robots" CONTENT="noindex,nofollow">'
                b'<script src="/_Incapsula_Resource?SWJIYLWA=5074a744"></script>'
                b"</head><body></body></html>",
                "text/html",
            )
        elif path == "/challenge-cloudflare":
            self._send(
                200,
                b"<!DOCTYPE html><html><head><title>Just a moment...</title>"
                b'<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>'
                b"</head><body></body></html>",
                "text/html",
            )
        elif path == "/mentions-challenge":
            # A real page that happens to contain a marker string. Must NOT be read
            # as a challenge: the detector requires a small body as well.
            body = (
                b"<html><head><title>IT Security</title></head><body>"
                b"<p>Our estate uses _Incapsula_Resource for DDoS protection by our "
                b"provider.</p>" + b"<p>Real prospectus content.</p>" * 400 + b"</body></html>"
            )
            self._send(200, body, "text/html")
        elif path == "/gzip":
            body = gzip.compress(HTML_ONE)
            self._send(200, body, "text/html", extra={"Content-Encoding": "gzip"})
        else:
            self._send(404, b"no route", "text/plain")

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, status: int, location: str) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FixtureServer:
    """A loopback HTTP server with deterministic routes."""

    def __init__(self) -> None:
        self.log = RouteLog()
        self.state: dict[str, Any] = {}
        handler = type("BoundHandler", (_Handler,), {"log": self.log, "state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FixtureServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        # `server_address` is typed loosely enough to be bytes on some platforms.
        address = self._server.server_address
        raw_host = address[0]
        host = raw_host.decode() if isinstance(raw_host, bytes) else str(raw_host)
        return f"http://{host}:{address[1]}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"


__all__ = [
    "ETAG_ONE",
    "HTML_ONE",
    "HTML_TWO",
    "LAST_MODIFIED_ONE",
    "PDF_BYTES",
    "TEST_MAX_BYTES",
    "FixtureServer",
    "RouteLog",
]
