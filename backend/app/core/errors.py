from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MAX_ERROR_MESSAGE_LENGTH = 500
PUBLIC_CACHE_PATHS = {
    "/.well-known/kaede/server",
    "/_kaede/v1/keys",
}


class ErrorIssue(BaseModel):
    location: list[str]
    message: str
    type: str


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    trace_id: str
    permissions: str | None = None
    retry_after_ms: int | None = None
    errors: list[ErrorIssue] | None = None


API_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status_code: {"model": ErrorEnvelope, "description": "Kaede API error"}
    for status_code in (400, 401, 403, 404, 409, 413, 422, 429, 500, 502, 503)
}


def apply_response_cache_policy(path: str, response: Response) -> None:
    if path.startswith("/api/v1/link-previews/media/") and response.status_code == 200:
        response.headers["Cache-Control"] = "private, max-age=900"
        if "Pragma" in response.headers:
            del response.headers["Pragma"]
        return
    if path in PUBLIC_CACHE_PATHS and response.status_code == 200:
        response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
        if "Pragma" in response.headers:
            del response.headers["Pragma"]
        return
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def request_trace_id(request: Request) -> str:
    trace_id = getattr(request.state, "trace_id", None)
    if isinstance(trace_id, str):
        return trace_id
    trace_id = secrets.token_hex(16)
    request.state.trace_id = trace_id
    return trace_id


def status_message(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Request failed"


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
    extensions: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "code": code,
        "message": message,
        "trace_id": request_trace_id(request),
    }
    if extensions is not None:
        body.update(extensions)
    return JSONResponse(body, status_code=status_code, headers=headers)


def http_exception_response(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = f"HTTP_{exc.status_code}"
    message = status_message(exc.status_code)
    extensions: dict[str, object] = {}
    if isinstance(exc.detail, dict):
        candidate_code = exc.detail.get("code")
        if isinstance(candidate_code, str) and ERROR_CODE_RE.fullmatch(candidate_code):
            code = candidate_code
        candidate_message = exc.detail.get("message")
        if isinstance(candidate_message, str) and candidate_message:
            message = candidate_message[:MAX_ERROR_MESSAGE_LENGTH]
        permissions = exc.detail.get("permissions")
        if isinstance(permissions, str):
            extensions["permissions"] = permissions
        retry_after_ms = exc.detail.get("retry_after_ms")
        if (
            isinstance(retry_after_ms, int)
            and not isinstance(retry_after_ms, bool)
            and 0 <= retry_after_ms <= 86_400_000
        ):
            extensions["retry_after_ms"] = retry_after_ms
    elif isinstance(exc.detail, str) and exc.detail:
        message = exc.detail[:MAX_ERROR_MESSAGE_LENGTH]
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=exc.headers,
        extensions=extensions,
    )


def validation_exception_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    issues = [
        {
            "location": [str(part) for part in error["loc"]],
            "message": str(error["msg"])[:200],
            "type": str(error["type"])[:100],
        }
        for error in exc.errors()[:100]
    ]
    return error_response(
        request,
        status_code=422,
        code="VALIDATION_ERROR",
        message="Request validation failed",
        extensions={"errors": issues},
    )


def parse_upstream_error(body: Any, fallback_code: str) -> dict[str, object]:
    """Return a safe HTTPException detail from a current or legacy peer response."""
    candidate = body
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        candidate = body["detail"]
    if not isinstance(candidate, dict):
        return {"code": fallback_code}

    code = candidate.get("code")
    detail: dict[str, object] = {
        "code": code if isinstance(code, str) and ERROR_CODE_RE.fullmatch(code) else fallback_code
    }
    message = candidate.get("message")
    if isinstance(message, str) and message:
        detail["message"] = message[:MAX_ERROR_MESSAGE_LENGTH]
    retry_after_ms = candidate.get("retry_after_ms")
    if (
        isinstance(retry_after_ms, int)
        and not isinstance(retry_after_ms, bool)
        and 0 <= retry_after_ms <= 86_400_000
    ):
        detail["retry_after_ms"] = retry_after_ms
    return detail
