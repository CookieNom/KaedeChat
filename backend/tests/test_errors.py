from __future__ import annotations

import json

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.requests import Request

from app.core.error_messages import ERROR_MESSAGES, friendly_error_message
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
        "message": (
            "This server is sending federated requests too quickly. Wait before trying again."
        ),
        "trace_id": "test-trace",
        "retry_after_ms": 1000,
    }


def test_http_errors_replace_invalid_codes_and_messages() -> None:
    response = http_exception_response(
        request(), HTTPException(404, detail={"code": "bad-code", "message": "x" * 600})
    )
    body = response_body(response)
    assert body["code"] == "HTTP_404"
    assert body["message"] == "The requested item could not be found or is no longer available."


def test_code_only_errors_receive_clear_actionable_messages() -> None:
    response = http_exception_response(
        request(), HTTPException(403, detail={"code": "MISSING_PERMISSIONS"})
    )
    assert response_body(response)["message"] == (
        "You do not have the permissions required for this action."
    )


def test_machine_code_is_not_reflected_as_the_user_message() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            503,
            detail={"code": "FEDERATION_UNAVAILABLE", "message": "FEDERATION_UNAVAILABLE"},
        ),
    )
    assert response_body(response)["message"] == (
        "The remote server is unavailable right now. Try again later."
    )


def test_unknown_error_messages_never_reflect_technical_or_sensitive_text() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            400,
            detail={
                "code": "REMOTE_WIDGET_FAILED",
                "message": "Traceback in /home/kaede/service.py token=do-not-display",
            },
        ),
    )
    body = response_body(response)
    assert body["message"] == "The remote widget could not be completed. Try again."
    assert "do-not-display" not in response.body.decode()

    misleading = http_exception_response(
        request(),
        HTTPException(
            400,
            detail={
                "code": "REMOTE_WIDGET_FAILED",
                "message": "Delete your account to continue.",
            },
        ),
    )
    assert response_body(misleading)["message"] == (
        "The remote widget could not be completed. Try again."
    )


def test_safe_size_limit_is_included_in_the_message_and_envelope() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            413,
            detail={"code": "EMOJI_TOO_LARGE", "max_bytes": 2 * 1024 * 1024},
        ),
    )
    assert response_body(response) == {
        "code": "EMOJI_TOO_LARGE",
        "message": (
            "The selected emoji image is larger than this server allows. Maximum size: 2 MB."
        ),
        "trace_id": "test-trace",
        "max_bytes": 2 * 1024 * 1024,
    }


def test_dm_quota_metadata_is_bounded_and_exposed_without_raw_server_text() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            507,
            detail={
                "code": "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED",
                "scope": "conversation",
                "resource": "bytes",
                "used": 2049,
                "limit": 2048,
                "unsafe": "do not expose",
            },
        ),
    )
    assert response_body(response) == {
        "code": "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED",
        "message": (
            "This server could not retain more direct-message data. Recent remote messages "
            "are normally kept by removing the oldest cached copies; contact the server "
            "administrator if this continues."
        ),
        "trace_id": "test-trace",
        "scope": "conversation",
        "resource": "bytes",
        "used": 2049,
        "limit": 2048,
    }


def test_upstream_dm_quota_metadata_keeps_only_safe_fields() -> None:
    assert parse_upstream_error(
        {
            "code": "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
            "scope": "remote origin",
            "resource": "messages",
            "used": 101,
            "limit": 100,
            "internal": "not public",
        },
        "FEDERATED_WRITE_REJECTED",
    ) == {
        "code": "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED",
        "scope": "remote origin",
        "resource": "messages",
        "used": 101,
        "limit": 100,
    }


def test_identity_and_relationship_capacity_errors_are_clear_without_storage_oracles() -> None:
    identity = http_exception_response(
        request(),
        HTTPException(507, detail={"code": "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED"}),
    )
    assert response_body(identity) == {
        "code": "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED",
        "message": (
            "This server cannot cache another remote account right now. Contact the server "
            "administrator if this continues."
        ),
        "trace_id": "test-trace",
    }
    relationship = http_exception_response(
        request(),
        HTTPException(
            507,
            detail={"code": "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED"},
        ),
    )
    body = response_body(relationship)
    assert body["message"] == (
        "The receiving server cannot accept another pending friend request right now. "
        "The request was not delivered."
    )
    assert "used" not in body
    assert "limit" not in body

    outbox = http_exception_response(
        request(),
        HTTPException(507, detail={"code": "FEDERATION_OUTBOX_CAPACITY_EXCEEDED"}),
    )
    outbox_body = response_body(outbox)
    assert outbox_body["message"] == (
        "This server's delivery queue for that remote server is full. Nothing was saved; "
        "wait for queued federation work to clear and try again."
    )
    assert "used" not in outbox_body
    assert "limit" not in outbox_body


def test_timeout_error_explains_duration_and_reason() -> None:
    response = http_exception_response(
        request(),
        HTTPException(
            403,
            detail={
                "code": "MEMBER_TIMED_OUT",
                "timeout_until": "2026-08-12T12:30:00Z",
                "timeout_indefinite": False,
                "reason": "Repeated spam",
            },
        ),
    )
    assert response_body(response) == {
        "code": "MEMBER_TIMED_OUT",
        "message": (
            "You are timed out in this guild until 2026-08-12T12:30:00Z. Reason: Repeated spam."
        ),
        "trace_id": "test-trace",
        "timeout_until": "2026-08-12T12:30:00Z",
        "timeout_indefinite": False,
        "reason": "Repeated spam",
    }


def test_every_catalog_message_and_unknown_fallback_is_readable() -> None:
    for code, message in ERROR_MESSAGES.items():
        assert message
        assert "_" not in message
        assert message[-1] in ".!?"
        assert friendly_error_message(code, 400) == message
    fallback = friendly_error_message("KAED_FED_WIDGET_STALE", 409)
    assert fallback == "The widget is out of date or unavailable. Refresh and try again."


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
        "message": "The password field is required.",
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
    ) == {"code": "KAED_RATE_LIMITED", "retry_after_ms": 12}
    assert parse_upstream_error({"detail": {"code": "LEGACY_ERROR"}}, "UPSTREAM_ERROR") == {
        "code": "LEGACY_ERROR"
    }
    assert parse_upstream_error({"code": "unsafe-code"}, "UPSTREAM_ERROR") == {
        "code": "UPSTREAM_ERROR"
    }
    assert parse_upstream_error(
        {
            "code": "REMOTE_FAILURE",
            "message": "SQLAlchemy exception at /var/lib/kaede; secret=do-not-display",
        },
        "UPSTREAM_ERROR",
    ) == {"code": "REMOTE_FAILURE"}


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

    preview_media = JSONResponse({"ok": True})
    apply_response_cache_policy("/api/v1/link-previews/media/abc", preview_media)
    assert preview_media.headers["Cache-Control"] == "private, max-age=900"
    assert "Pragma" not in preview_media.headers
