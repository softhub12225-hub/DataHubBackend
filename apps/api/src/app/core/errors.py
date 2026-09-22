"""One error envelope for every failure the API can produce.

Shape (stable, and part of the generated OpenAPI contract)::

    {"error": {"code": "...", "message": "...", "details": [...], "requestId": "..."}}

Three rules this module exists to enforce:

* Clients parse ``error.code``, never prose. Messages may be reworded freely.
* Unexpected exceptions never leak a stack trace or driver message to the client;
  they are logged with the request id and reported as ``internal_error``.
* Nothing is swallowed. Every handled exception is logged at an appropriate level.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, get_request_id

logger = get_logger(__name__)


class ErrorDetail(BaseModel):
    """A single field-level or item-level problem."""

    location: str | None = Field(default=None, description="Dotted path to the offending input")
    message: str = Field(description="Human-readable description of this specific problem")
    type: str | None = Field(default=None, description="Machine-readable problem type")


class ErrorBody(BaseModel):
    code: str = Field(description="Stable, machine-readable error code")
    message: str = Field(description="Human-readable summary; not for programmatic use")
    details: list[ErrorDetail] = Field(default_factory=list)
    request_id: str | None = Field(
        default=None,
        serialization_alias="requestId",
        description="Correlation id, also returned in the X-Request-ID header",
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base class for failures the application raises deliberately.

    Subclasses set ``status_code`` and ``code``; everything else is optional.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: list[ErrorDetail] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or []
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    code = "validation_error"
    message = "The request payload is invalid."


class DependencyUnavailableError(AppError):
    """A required downstream dependency is unreachable or unhealthy."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "dependency_unavailable"
    message = "A required dependency is unavailable."


def _envelope(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            details=details or [],
            request_id=get_request_id(),
        )
    )
    return JSONResponse(
        status_code=status_code,
        content=jsonable_encoder(body, by_alias=True),
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log("app_error", code=exc.code, status_code=exc.status_code, message=exc.message)
        return _envelope(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            ErrorDetail(
                location=".".join(str(part) for part in error.get("loc", ())) or None,
                message=str(error.get("msg", "invalid value")),
                type=str(error.get("type")) if error.get("type") else None,
            )
            for error in exc.errors()
        ]
        logger.info("request_validation_failed", error_count=len(details))
        return _envelope(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="validation_error",
            message="The request payload is invalid.",
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _HTTP_STATUS_CODES.get(exc.status_code, "http_error")
        detail: Any = exc.detail
        message = detail if isinstance(detail, str) else code.replace("_", " ").capitalize()
        if exc.status_code >= 500:
            logger.error("http_exception", status_code=exc.status_code, code=code)
        return _envelope(status_code=exc.status_code, code=code, message=message)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # exc_info so the traceback reaches the log; the client gets nothing but a code.
        logger.error("unhandled_exception", exc_info=exc, error_type=type(exc).__name__)
        return _envelope(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="internal_error",
            message="An unexpected error occurred.",
        )


_HTTP_STATUS_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "internal_error",
    status.HTTP_503_SERVICE_UNAVAILABLE: "dependency_unavailable",
}
