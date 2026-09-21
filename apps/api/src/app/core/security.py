"""Transport-level security concerns available at bootstrap.

Authentication, RBAC and segregation-of-duties enforcement are deliberately absent:
they belong with the identity module and must not be stubbed out here. Shipping a
placeholder auth dependency now would invite code to be written against it.

What does belong here today: response hardening, CORS wiring, and a redaction helper
so no code path has an excuse to log a secret.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings
from app.core.logging import REQUEST_ID_HEADER

# Applied to every response. The console is server-rendered and the API serves JSON,
# so a restrictive default costs nothing here.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    # The API returns JSON only; nothing should ever be executed from this origin.
    # Scraped HTML snapshots are rendered from a separate origin (ARCHITECTURE.md
    # section 10) and are not covered by this policy.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


def configure_security(app: FastAPI, settings: Settings) -> None:
    """Install CORS and security headers.

    CORS origins are an explicit allow-list from configuration; wildcards are never
    used because the console sends credentials.
    """
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
        max_age=600,
    )


def redact(value: str | SecretStr | None, *, keep: int = 0) -> str:
    """Render a sensitive value safe for logs.

    ``keep`` exposes a short suffix to make values distinguishable during debugging
    without disclosing them; it is clamped so short secrets are never mostly shown.
    """
    if value is None:
        return "<unset>"
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not raw:
        return "<empty>"
    keep = min(keep, max(0, len(raw) // 4))
    return "***" if keep == 0 else f"***{raw[-keep:]}"
