"""The console's HTTP surface: authentication, CSRF and the actor's origin.

WHY THESE ARE SEPARATE FROM `test_reviewer_console.py`
======================================================
Those tests call the service layer directly, inside a transaction that is rolled back.
These go through the real ASGI app, because the properties under test only exist at the
HTTP boundary: the CSRF double-submit, the status code a client uses to decide whether to
send someone back to the login screen, and the absence of any way to name an actor.

They deliberately need **no fixture rows**. Every case here is a refusal, and a refusal
that depended on seeded data would be a weaker test -- it would prove the endpoint rejects
*this* request rather than that it rejects an unauthenticated one. That also avoids the
commit/cleanup problem: the app manages its own connection, so anything they wrote would
outlive the test.

They use `httpx.AsyncClient` over `ASGITransport`, like `test_health.py`, rather than
starlette's `TestClient`. That is not a style preference: importing `TestClient` emits an
`anyio.abc.BlockingPortal` deprecation that this project's warning filters turn into an
error, so the module would not even import.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient

from app.api.review.deps import CSRF_COOKIE
from app.core.config import Settings, get_settings
from app.core.db import dispose_engines
from app.domains.identity.sessions import CSRF_HEADER

pytestmark = pytest.mark.integration

PREFIX = "/api/v1/review"


@pytest.fixture(autouse=True)
async def _dispose_engines_between_tests() -> AsyncIterator[None]:
    """Drop the cached async engines after every test in this module.

    `app.core.db` caches one `AsyncEngine` per role at module level, which is right for a
    server process and wrong across tests: pytest-asyncio gives each test its own event
    loop, and a pooled asyncpg connection created on a previous loop deadlocks when the
    next test tries to use it. The symptom is a suite that passes its first
    database-touching test and then hangs forever with no output.
    """
    yield
    await dispose_engines()


def client_with_cookies(client: AsyncClient, **cookies: str) -> AsyncClient:
    """The same client with cookies set on the instance.

    httpx deprecates per-request `cookies=`, and this project turns warnings into errors,
    so setting them on the instance is the only supported route.
    """
    for name, value in cookies.items():
        client.cookies.set(name, value)
    return client


UNKNOWN_EMAIL = "nobody-at-all@example.test"
SOME_PASSWORD = "whatever-this-is-not"
NIL_UUID = "00000000-0000-0000-0000-000000000000"


# ===========================================================================
# 1. Nothing is readable without a session
# ===========================================================================


@pytest.mark.parametrize("path", ["/dashboard", "/institutions", "/auth/me", "/operations"])
async def test_reads_require_a_session(client: AsyncClient, path: str) -> None:
    response = await client.get(f"{PREFIX}{path}")
    assert response.status_code == 401


async def test_an_unparseable_cookie_is_refused_rather_than_accepted(
    client: AsyncClient, settings: Settings
) -> None:
    """A garbage token must land on 'no session', never on an error that lets it through."""
    scoped = client_with_cookies(
        client, **{settings.session_cookie_name: "not-a-real-session-token"}
    )
    response = await scoped.get(f"{PREFIX}/auth/me")
    assert response.status_code == 401


# ===========================================================================
# 2. Login
# ===========================================================================


async def test_login_with_an_unknown_account_says_nothing_specific(
    client: AsyncClient,
) -> None:
    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": UNKNOWN_EMAIL, "password": SOME_PASSWORD}
    )
    assert response.status_code == 401
    # `register_exception_handlers` wraps every failure in the shared envelope, so an
    # HTTPException's `detail` arrives as `error.message`.
    message = response.json()["error"]["message"]
    # The message must not reveal whether the account exists.
    assert "no account with that email" in message
    assert "wrong password" not in message.lower()


async def test_a_failed_login_sets_no_cookies(client: AsyncClient) -> None:
    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": UNKNOWN_EMAIL, "password": SOME_PASSWORD}
    )
    assert response.status_code == 401
    assert "set-cookie" not in {name.lower() for name in response.headers}


async def test_the_login_response_never_echoes_the_password(client: AsyncClient) -> None:
    distinctive = "a-very-distinctive-password-value"
    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": UNKNOWN_EMAIL, "password": distinctive}
    )
    assert distinctive not in response.text


# ===========================================================================
# 3. CSRF (section C)
# ===========================================================================


async def test_a_mutating_request_without_the_csrf_header_is_refused(
    client: AsyncClient,
) -> None:
    """Checked before authentication is even consulted."""
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "some-csrf-value"})
    response = await scoped.post(f"{PREFIX}/auth/logout")
    assert response.status_code == 403
    assert CSRF_HEADER in response.json()["error"]["message"]


async def test_a_csrf_header_that_does_not_match_the_cookie_is_refused(
    client: AsyncClient,
) -> None:
    """The whole point of double-submit: the header must equal the cookie."""
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "the-real-value"})
    response = await scoped.post(
        f"{PREFIX}/auth/logout", headers={CSRF_HEADER: "a-guess-from-another-origin"}
    )
    assert response.status_code == 403


async def test_a_csrf_header_with_no_cookie_is_refused(client: AsyncClient) -> None:
    response = await client.post(f"{PREFIX}/auth/logout", headers={CSRF_HEADER: "invented"})
    assert response.status_code == 403


async def test_login_itself_does_not_require_csrf(client: AsyncClient) -> None:
    """There is no session to protect yet, and requiring it would make login impossible."""
    response = await client.post(
        f"{PREFIX}/auth/login", json={"email": UNKNOWN_EMAIL, "password": SOME_PASSWORD}
    )
    assert response.status_code == 401  # refused on credentials, not on CSRF


@pytest.mark.parametrize(
    "path",
    [f"/responsibilities/{NIL_UUID}/preview", f"/promotions/{NIL_UUID}/preview"],
)
async def test_an_unauthenticated_decision_post_is_refused(client: AsyncClient, path: str) -> None:
    """401, not 403 -- and that ordering is correct, not an oversight.

    FastAPI resolves dependencies in signature order, and these routes take the session
    before the CSRF guard. So an anonymous request is refused for having no session rather
    than for having no CSRF token. That is the right answer: CSRF exists to stop another
    origin *spending a session the browser already holds*, and where there is no session
    there is nothing to spend. Both refuse; neither reaches the decision service.
    """
    response = await client.post(
        path if path.startswith(PREFIX) else f"{PREFIX}{path}",
        json={"decision": "VERIFIED", "reason": "x", "manifest_sha256": "0" * 64},
    )
    assert response.status_code == 401


async def test_the_csrf_guard_protects_the_one_route_with_no_session_dependency(
    client: AsyncClient,
) -> None:
    """Logout takes no session, so CSRF is the check that actually runs there."""
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "a-value"})
    assert (await scoped.post(f"{PREFIX}/auth/logout")).status_code == 403


# ===========================================================================
# 4. The actor is never a client-supplied value (section C)
# ===========================================================================


async def test_naming_an_actor_in_the_body_authenticates_nothing(
    client: AsyncClient,
) -> None:
    """Extra keys are ignored; the session remains the only thing that establishes who."""
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "value"})
    response = await scoped.post(
        f"{PREFIX}/responsibilities/{NIL_UUID}/preview",
        headers={CSRF_HEADER: "value"},
        json={
            "decision": "VERIFIED",
            "reason": "x",
            "manifest_sha256": "0" * 64,
            "actor_id": "bc9a9fb3-a9a2-4299-8554-4fa7c676df0c",
            "reviewer_id": "bc9a9fb3-a9a2-4299-8554-4fa7c676df0c",
        },
    )
    assert response.status_code == 401


async def test_the_api_document_exposes_no_actor_parameter(client: AsyncClient) -> None:
    """A regression guard with a long half-life.

    If somebody later adds an actor field to a request model this fails, which is the
    moment to ask why the server would need the client to tell it who is acting.
    """
    document = (await client.get("/openapi.json")).json()
    schemas = document.get("components", {}).get("schemas", {})
    for name in ("DecisionRequest", "ApplyRequest"):
        assert name in schemas, f"{name} missing from the API document"
        properties = set(schemas[name].get("properties", {}))
        assert not (properties & {"actor", "actor_id", "reviewer", "reviewer_id", "user_id"})


async def test_the_api_document_carries_no_secrets(client: AsyncClient, settings: Settings) -> None:
    """Section R, asserted at the boundary the browser can actually reach."""
    body = (await client.get("/openapi.json")).text
    for secret in (
        settings.session_secret.get_secret_value(),
        settings.database.api_password.get_secret_value(),
    ):
        if secret and secret != "change-me":
            assert secret not in body


# ===========================================================================
# 5. Logout regressions (found by exercising the running console)
# ===========================================================================


async def test_logout_returns_204_with_no_body(client: AsyncClient) -> None:
    """A 204 must carry no body.

    Found live: the BFF proxy read `response.text()` unconditionally and handed the
    resulting empty string to `new NextResponse(body, {status: 204})`, which throws for a
    null-body status. The API was returning 204 correctly and the console turned it into a
    500 -- a logout that looked broken and had in fact worked.
    """
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "v"})
    response = await scoped.post(f"{PREFIX}/auth/logout", headers={CSRF_HEADER: "v"})
    assert response.status_code == 204
    assert response.content == b""


async def test_logout_clears_both_cookies(client: AsyncClient) -> None:
    """The Set-Cookie headers must be on the response that is actually returned.

    Found live: `clear_session_cookies` was applied to FastAPI's injected `Response` and
    the handler then returned a *new* one, discarding the delete-cookie headers. The
    session row was revoked while the browser kept its cookie -- so the next request
    presented a revoked token instead of no token, which is a worse failure mode than
    either.
    """
    settings = get_settings()
    scoped = client_with_cookies(client, **{CSRF_COOKIE: "v"})
    response = await scoped.post(f"{PREFIX}/auth/logout", headers={CSRF_HEADER: "v"})

    cleared = response.headers.get_list("set-cookie")
    assert cleared, "logout must clear the cookies it set"
    names = {header.split("=", 1)[0] for header in cleared}
    assert settings.session_cookie_name in names
    assert CSRF_COOKIE in names
