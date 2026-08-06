from __future__ import annotations

import json

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app.core.errors import (
    apply_response_cache_policy,
    http_exception_response,
    parse_upstream_error,
    validation_exception_response,
)


def request(trace_id: str = "test-trace") -> Request:
    value = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    value.state.trace_id = trace_id
    return value


def response_body(response: JSONResponse) -> dict[str, object]:
    body = json.loads(response.body)
    assert isinstance(body, dict)
    return body


def test_http_errors_use_top_level_safe_envelope() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            429,
            detail={
                "code": "KAED_RATE_LIMITED",
                "message": "Try later",
                "retry_after_ms": 1000,
                "unsafe": object(),
                "trace_id": "forged",
            },
            headers={"Retry-After": "1"},
        ),
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response_body(response) == {
        "code": "KAED_RATE_LIMITED",
        "message": "Try later",
        "trace_id": "test-trace",
        "retry_after_ms": 1000,
    }


def test_http_errors_replace_invalid_codes_and_messages() -> None:
    response = http_exception_response(
        request(), HTTPException(404, detail={"code": "bad-code", "message": "x" * 600})
    )
    body = response_body(response)
    assert body["code"] == "HTTP_404"
    assert body["message"] == "x" * 500


def test_validation_errors_omit_submitted_input() -> None:
    response = validation_exception_response(
        request(),
        RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "password"),
                    "msg": "Field required",
                    "input": "do-not-reflect",
                }
            ]
        ),
    )
    body = response_body(response)
    assert body == {
        "code": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "trace_id": "test-trace",
        "errors": [
            {
                "location": ["body", "password"],
                "message": "Field required",
                "type": "missing",
            }
        ],
    }
    assert b"do-not-reflect" not in response.body


def test_upstream_error_parser_accepts_current_and_legacy_envelopes() -> None:
    assert parse_upstream_error(
        {"code": "KAED_RATE_LIMITED", "message": "later", "retry_after_ms": 12},
        "UPSTREAM_ERROR",
    ) == {"code": "KAED_RATE_LIMITED", "message": "later", "retry_after_ms": 12}
    assert parse_upstream_error({"detail": {"code": "LEGACY_ERROR"}}, "UPSTREAM_ERROR") == {
        "code": "LEGACY_ERROR"
    }
    assert parse_upstream_error({"code": "unsafe-code"}, "UPSTREAM_ERROR") == {
        "code": "UPSTREAM_ERROR"
    }


def test_cache_policy_prevents_sensitive_response_storage() -> None:
    sensitive = JSONResponse({"ok": True})
    apply_response_cache_policy("/api/v1/users/@me", sensitive)
    assert sensitive.headers["Cache-Control"] == "no-store"
    assert sensitive.headers["Pragma"] == "no-cache"

    public = JSONResponse({"server": "chat.example.com"})
    apply_response_cache_policy("/.well-known/kaede/server", public)
    assert public.headers["Cache-Control"] == "public, max-age=300, must-revalidate"
    assert "Pragma" not in public.headers

    failed_public = JSONResponse({"error": True}, status_code=503)
    apply_response_cache_policy("/_kaede/v1/keys", failed_public)
    assert failed_public.headers["Cache-Control"] == "no-store"
