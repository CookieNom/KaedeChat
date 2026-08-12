from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from app.core.error_messages import friendly_error_message

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
    max_bytes: int | None = None
    scope: str | None = None
    resource: str | None = None
    used: int | None = None
    limit: int | None = None
    timeout_until: str | None = None
    timeout_indefinite: bool | None = None
    reason: str | None = None
    errors: list[ErrorIssue] | None = None


API_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status_code: {"model": ErrorEnvelope, "description": "Kaede API error"}
    for status_code in (
        400,
        401,
        403,
        404,
        409,
        410,
        412,
        413,
        415,
        422,
        428,
        429,
        500,
        502,
        503,
        504,
        507,
        508,
    )
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
    extensions: dict[str, object] = {}
    if isinstance(exc.detail, dict):
        candidate_code = exc.detail.get("code")
        if isinstance(candidate_code, str) and ERROR_CODE_RE.fullmatch(candidate_code):
            code = candidate_code
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
        max_bytes = exc.detail.get("max_bytes")
        if (
            isinstance(max_bytes, int)
            and not isinstance(max_bytes, bool)
            and 0 < max_bytes <= 10 * 1024 * 1024 * 1024
        ):
            extensions["max_bytes"] = max_bytes
        scope = exc.detail.get("scope")
        if scope in {"conversation", "authority", "remote origin"}:
            extensions["scope"] = scope
        resource = exc.detail.get("resource")
        if resource in {"conversations", "messages", "bytes"}:
            extensions["resource"] = resource
        for field in ("used", "limit"):
            value = exc.detail.get(field)
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= 9_223_372_036_854_775_807
            ):
                extensions[field] = value
        timeout_until = exc.detail.get("timeout_until")
        if isinstance(timeout_until, str) and 1 <= len(timeout_until) <= 64:
            extensions["timeout_until"] = timeout_until
        timeout_indefinite = exc.detail.get("timeout_indefinite")
        if isinstance(timeout_indefinite, bool):
            extensions["timeout_indefinite"] = timeout_indefinite
        reason = exc.detail.get("reason")
        if isinstance(reason, str) and reason.strip():
            extensions["reason"] = " ".join(reason.split())[:MAX_ERROR_MESSAGE_LENGTH]
    message = friendly_error_message(code, exc.status_code)
    size_limit = extensions.get("max_bytes")
    if isinstance(size_limit, int):
        message = f"{message.rstrip('.')}. Maximum size: {_display_bytes(size_limit)}."
    if code == "MEMBER_TIMED_OUT":
        if extensions.get("timeout_indefinite") is True:
            message = "You are timed out indefinitely in this guild."
        elif timeout := extensions.get("timeout_until"):
            message = f"You are timed out in this guild until {timeout}."
        if reason := extensions.get("reason"):
            message = f"{message.rstrip('.')}. Reason: {str(reason).rstrip('.')}."
    return error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        headers=exc.headers,
        extensions=extensions,
    )


def _display_bytes(value: int) -> str:
    size = float(value)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:g} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def validation_exception_response(request: Request, exc: RequestValidationError) -> JSONResponse:
    issues: list[dict[str, object]] = [
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
        message=_validation_summary(issues),
        extensions={"errors": issues},
    )


def _validation_summary(issues: list[dict[str, object]]) -> str:
    if not issues:
        return "Some submitted information is invalid. Check it and try again."
    first = issues[0]
    location = first.get("location")
    field: str | None = None
    if isinstance(location, list):
        for part in reversed(location):
            if isinstance(part, str) and part not in {"body", "path", "query", "header"}:
                field = part.replace("_", " ")
                break
    raw_message = first.get("message")
    issue = str(raw_message).strip() if raw_message is not None else "is invalid"
    if issue.lower().startswith("value error, "):
        issue = issue[13:].strip()
    if not field:
        return "Some submitted information is invalid. Check it and try again."
    if issue.lower() == "field required":
        return f"The {field} field is required."
    issue = issue.rstrip(".")
    return f"Check the {field} field: {issue}."


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
    retry_after_ms = candidate.get("retry_after_ms")
    if (
        isinstance(retry_after_ms, int)
        and not isinstance(retry_after_ms, bool)
        and 0 <= retry_after_ms <= 86_400_000
    ):
        detail["retry_after_ms"] = retry_after_ms
    scope = candidate.get("scope")
    if scope in {"conversation", "authority", "remote origin"}:
        detail["scope"] = scope
    resource = candidate.get("resource")
    if resource in {"conversations", "messages", "bytes"}:
        detail["resource"] = resource
    for field in ("used", "limit"):
        value = candidate.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 9_223_372_036_854_775_807
        ):
            detail[field] = value
    return detail
