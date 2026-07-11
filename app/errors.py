"""Error envelope: every 4xx/5xx body is {"error": str, "code": int}.

`code` mirrors the HTTP status: one source of truth for the error class.
Structured, machine-readable detail (e.g. the ambiguous-target candidate
list) is carried in additional typed fields alongside the envelope, never
by inventing a parallel numbering scheme.

Enforcement is structural: raising APIError anywhere, a request validation
failure, or an uncaught exception all funnel through the handlers below,
so no route can accidentally emit a bare error shape.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, message: str, extra: dict[str, Any] | None = None):
        self.status_code = status_code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


def envelope(status_code: int, message: str, extra: dict[str, Any] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": message, "code": status_code, **(extra or {})},
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(APIError)
    async def handle_api_error(request: Request, exc: APIError) -> JSONResponse:
        return envelope(exc.status_code, exc.message, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ()))
        detail = f"{loc}: {first.get('msg', 'invalid request')}" if loc else "invalid request"
        return envelope(422, f"invalid request payload: {detail}")

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return envelope(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return envelope(500, "internal server error")
