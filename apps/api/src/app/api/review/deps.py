"""Request-scoped plumbing for the reviewer console: who you are, and one shared engine.

THE ACTOR COMES FROM THE COOKIE, NEVER FROM THE REQUEST BODY
============================================================
Section C. Every endpoint that writes resolves its actor through `current_session`, which
reads the HttpOnly session cookie, looks the row up in `user_session`, and re-reads the
identity's roles and permissions. No endpoint accepts a reviewer id as a parameter. That
is the same rule `verification/authentication.py` established for the CLI -- an actor
argument is a way to attribute a decision to someone who did not make it -- carried into
the browser, where it matters more because the client is not the operator's own terminal.

WHY `run_sync`
==============
The application is async (asyncpg); the verification domain is synchronous, because it
grew up serving a CLI. Rewriting it async would fork the logic into two implementations,
and section A.4 requires exactly one: *the CLI and the UI must call the same service
layer.* SQLAlchemy's `AsyncConnection.run_sync` exists for this -- it hands a synchronous
`Connection` facade to a callable while the real work goes through the async driver. So
the console calls the identical functions the CLI calls, on the same connection, inside
the same transaction.

CSRF
====
Double-submit. Login sets two cookies: the session (HttpOnly, unreadable to script) and a
CSRF token (readable). A mutating request must echo the CSRF value in a header. A page on
another origin can make the browser *send* cookies but cannot read them, so it cannot
produce the header. `SameSite=Lax` is the first line and this is the second, because
SameSite is a browser behaviour and the check should not depend solely on one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any, TypeVar

from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import DatabaseRole, Settings, get_settings
from app.core.db import engine_for
from app.domains.identity import sessions
from app.domains.identity.sessions import CSRF_HEADER, ResolvedSession

T = TypeVar("T")

#: Cookie holding the CSRF token. Readable by script on purpose -- that is what lets the
#: console echo it into a header, and it grants nothing on its own.
CSRF_COOKIE = "datahub_review_csrf"


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


async def db_connection() -> AsyncIterator[AsyncConnection]:
    """One connection per request, as `app_api`. Committed only if the handler returns.

    The console never connects to Postgres from the browser and never holds a database
    credential client-side (section R): this is the only place a connection is opened.
    """
    engine = engine_for(DatabaseRole.API)
    async with engine.begin() as connection:
        yield connection


ConnectionDep = Annotated[AsyncConnection, Depends(db_connection)]


async def run_sync(
    connection: AsyncConnection, fn: Callable[..., T], /, *args: Any, **kwargs: Any
) -> T:
    """Call a synchronous domain function on this request's connection.

    See the module docstring: this is what lets the console reuse the CLI's service layer
    verbatim instead of maintaining a second, divergent copy of the rules.
    """
    return await connection.run_sync(lambda sync_conn: fn(sync_conn, *args, **kwargs))


async def current_session(
    request: Request,
    connection: ConnectionDep,
    config: SettingsDep,
) -> ResolvedSession:
    """Resolve the caller from the session cookie, or refuse with 401.

    Re-reads `is_active` and the permission set on every request, so a deactivated
    reviewer or a revoked role stops working immediately rather than at cookie expiry.
    """
    token = request.cookies.get(config.session_cookie_name, "")
    try:
        return await run_sync(connection, sessions.resolve, token=token)
    except sessions.SessionExpiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Cookie"},
        ) from exc


SessionDep = Annotated[ResolvedSession, Depends(current_session)]


async def verifying_session(session: SessionDep) -> ResolvedSession:
    """A session that additionally carries `source:verify`. For decision endpoints."""
    try:
        session.require("source:verify")
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return session


VerifyingSessionDep = Annotated[ResolvedSession, Depends(verifying_session)]


async def csrf_guard(
    request: Request,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
) -> None:
    """Double-submit check for every mutating request. Raises 403 on mismatch."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    if not cookie or not csrf_header or not _constant_equals(cookie, csrf_header):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"CSRF check failed: send the {CSRF_COOKIE} cookie value in the "
                f"{CSRF_HEADER} header on mutating requests"
            ),
        )


def _constant_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


CsrfDep = Annotated[None, Depends(csrf_guard)]


def set_session_cookies(
    response: Response, *, config: Settings, token: str, csrf_token: str, max_age: int
) -> None:
    """Write both cookies with the attributes section C requires.

    `httponly` on the session so script cannot read it; off for CSRF so script must.
    `secure` is derived from the environment, never configured -- see `Settings`.
    """
    response.set_cookie(
        key=config.session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=config.session_cookie_secure,
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=config.session_cookie_secure,
        path="/",
    )


def clear_session_cookies(response: Response, *, config: Settings) -> None:
    response.delete_cookie(config.session_cookie_name, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


__all__ = [
    "CSRF_COOKIE",
    "ConnectionDep",
    "CsrfDep",
    "SessionDep",
    "SettingsDep",
    "VerifyingSessionDep",
    "clear_session_cookies",
    "current_session",
    "db_connection",
    "run_sync",
    "set_session_cookies",
    "verifying_session",
]
