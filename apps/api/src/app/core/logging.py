"""Structured logging and request correlation.

JSON in deployed environments, human-readable in local development. Every log line
emitted while handling a request carries the same ``request_id`` as the response
header and the error envelope, so a user-reported failure is one grep away.
"""

from __future__ import annotations

import logging
import sys
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any, Final

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER: Final = "X-Request-ID"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """The current request's correlation id, if we are inside a request."""
    return _request_id.get()


def _inject_request_id(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    request_id = _request_id.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


def configure_logging(*, level: str, fmt: str) -> None:
    """Configure structlog and route stdlib logging through it.

    Idempotent: safe to call from the API, a Celery worker and the test suite.
    """
    shared_processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _inject_request_id,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    # structlog emits through stdlib logging rather than writing to stdout directly.
    # That gives one pipeline for our own logs and for uvicorn/celery/sqlalchemy,
    # and it is what `add_logger_name` requires -- it reads `logger.name`, which a
    # plain WriteLogger does not have.
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Rendering happens here, in the handler, for both structlog-native and foreign
    # records. Because the handler is rebuilt on every call, changing the format at
    # runtime takes effect even for already-bound loggers.
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; strip them so lines are not emitted twice.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign or adopt a request id, bind it to the log context, echo it back.

    An inbound ``X-Request-ID`` is honoured so a trace can span the Next.js BFF and
    the API, but it is length-capped and sanitised: it ends up in log output, and
    unbounded client-controlled strings in logs are a log-injection vector.
    """

    MAX_INBOUND_LENGTH = 64

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = self._resolve_request_id(request)
        token = _request_id.set(request_id)
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
            _request_id.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response

    def _resolve_request_id(self, request: Request) -> str:
        inbound = request.headers.get(REQUEST_ID_HEADER)
        if inbound:
            candidate = inbound.strip()[: self.MAX_INBOUND_LENGTH]
            if candidate and all(c.isalnum() or c in "-_" for c in candidate):
                return candidate
        return uuid.uuid4().hex
