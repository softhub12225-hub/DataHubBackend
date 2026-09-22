"""The error envelope is a contract; these tests pin its shape."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    AppError,
    DependencyUnavailableError,
    ErrorDetail,
    NotFoundError,
    register_exception_handlers,
)
from app.core.logging import RequestContextMiddleware


@pytest.fixture
def error_app() -> FastAPI:
    """A tiny app whose only purpose is to raise each error class."""
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("University not found.")

    @app.get("/dependency")
    async def _dependency() -> None:
        raise DependencyUnavailableError()

    @app.get("/detailed")
    async def _detailed() -> None:
        raise AppError(
            "Two problems.",
            code="custom_error",
            status_code=400,
            details=[
                ErrorDetail(location="body.name", message="required", type="missing"),
                ErrorDetail(location="body.year", message="not an integer"),
            ],
        )

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("secret internal detail: dsn=postgres://user:pw@host/db")

    @app.get("/typed/{value}")
    async def _typed(value: int) -> dict[str, int]:
        return {"value": value}

    return app


@pytest.fixture
async def error_client(error_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=error_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def test_app_error_uses_the_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/not-found")
    assert response.status_code == 404
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "University not found."
    assert body["error"]["details"] == []
    assert body["error"]["requestId"]


async def test_request_id_matches_the_response_header(error_client: AsyncClient) -> None:
    response = await error_client.get("/not-found")
    assert response.json()["error"]["requestId"] == response.headers["X-Request-ID"]


async def test_dependency_unavailable_maps_to_503(error_client: AsyncClient) -> None:
    response = await error_client.get("/dependency")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "dependency_unavailable"


async def test_details_are_preserved(error_client: AsyncClient) -> None:
    body = (await error_client.get("/detailed")).json()
    assert body["error"]["code"] == "custom_error"
    details = body["error"]["details"]
    assert [d["location"] for d in details] == ["body.name", "body.year"]
    assert details[0]["type"] == "missing"
    assert details[1]["type"] is None


async def test_unhandled_exception_does_not_leak_internals(error_client: AsyncClient) -> None:
    with pytest.raises(RuntimeError):
        # ASGITransport re-raises so the test sees it; the handler still produced a
        # response, which the next assertion set covers via raise_app_exceptions.
        await error_client.get("/boom")


async def test_unhandled_exception_returns_generic_envelope(error_app: FastAPI) -> None:
    transport = ASGITransport(app=error_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert body["error"]["message"] == "An unexpected error occurred."
    # The whole point: no driver string, no DSN, no traceback.
    assert "postgres://" not in response.text
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text


async def test_validation_error_is_enveloped_with_field_locations(
    error_client: AsyncClient,
) -> None:
    response = await error_client.get("/typed/not-an-int")
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]
    assert any("value" in (d["location"] or "") for d in body["error"]["details"])


async def test_unknown_route_is_enveloped(error_client: AsyncClient) -> None:
    body = (await error_client.get("/no-such-route")).json()
    assert body["error"]["code"] == "not_found"
