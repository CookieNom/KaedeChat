from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.admin_portal import ReportCreate, report_message_evidence, report_payload
from app.db.bot_models import AbuseReport
from app.db.models import Message


def report_message(*, e2ee: dict[str, object] | None, content: str | None) -> Message:
    return cast(
        Message,
        SimpleNamespace(
            author_id=7,
            author_domain="alpha.localhost",
            channel_id=11,
            channel_domain="beta.localhost",
            created_at=datetime(2026, 8, 17, 12, 30, tzinfo=UTC),
            content=content,
            e2ee=e2ee,
        ),
    )


def test_e2ee_report_requires_explicit_decrypted_disclosure() -> None:
    message = report_message(e2ee={"version": 1, "ciphertext": "opaque"}, content=None)

    with pytest.raises(HTTPException) as raised:
        report_message_evidence(
            message,
            disclosed_content=None,
            disclosure_acknowledged=False,
        )

    assert raised.value.status_code == 422
    assert cast(dict[str, Any], raised.value.detail)["code"] == "E2EE_REPORT_DISCLOSURE_REQUIRED"


def test_e2ee_acknowledgement_cannot_turn_decrypt_unavailable_into_empty_text() -> None:
    message = report_message(e2ee={"version": 2, "ciphertext": "opaque"}, content=None)

    with pytest.raises(HTTPException) as raised:
        report_message_evidence(
            message,
            disclosed_content=None,
            disclosure_acknowledged=True,
        )

    assert cast(dict[str, Any], raised.value.detail)["code"] == ("E2EE_REPORT_DISCLOSURE_REQUIRED")


def test_e2ee_report_stores_reporter_supplied_text_and_ciphertext_fingerprint() -> None:
    envelope = {"ciphertext": "opaque", "version": 1}
    message = report_message(e2ee=envelope, content=None)

    evidence, mode = report_message_evidence(
        message,
        disclosed_content="decrypted on the reporting device",
        disclosure_acknowledged=True,
    )

    canonical = json.dumps(
        envelope,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert mode == "e2ee_user_disclosed"
    assert evidence == {
        "author_ref": "7@alpha.localhost",
        "channel_ref": "11@beta.localhost",
        "created_at": "2026-08-17T12:30:00+00:00",
        "content": "decrypted on the reporting device",
        "ciphertext_sha256": hashlib.sha256(canonical).hexdigest(),
        "disclosure": {
            "source": "reporter_client_decrypted",
            "reporter_acknowledged": True,
            "server_verified": False,
        },
    }


def test_e2ee_attachment_only_report_accepts_explicit_empty_decrypted_evidence() -> None:
    envelope = {"ciphertext": "opaque", "version": 2}
    message = report_message(e2ee=envelope, content=None)
    payload = ReportCreate(
        target_type="message",
        target_ref="1@alpha.localhost",
        message_ref="1@alpha.localhost",
        category="illegal_content",
        disclosed_content="",
        disclosure_acknowledged=True,
    )

    evidence, mode = report_message_evidence(
        message,
        disclosed_content=payload.disclosed_content,
        disclosure_acknowledged=payload.disclosure_acknowledged,
    )

    assert mode == "e2ee_user_disclosed"
    assert evidence["content"] == ""
    assert cast(dict[str, Any], evidence["disclosure"])["reporter_acknowledged"] is True


def test_e2ee_empty_text_is_not_a_substitute_for_explicit_consent() -> None:
    message = report_message(e2ee={"version": 2, "ciphertext": "opaque"}, content=None)

    with pytest.raises(HTTPException) as raised:
        report_message_evidence(
            message,
            disclosed_content="",
            disclosure_acknowledged=False,
        )

    assert cast(dict[str, Any], raised.value.detail)["code"] == ("E2EE_REPORT_DISCLOSURE_REQUIRED")


def test_plaintext_report_rejects_unnecessary_disclosed_content() -> None:
    message = report_message(e2ee=None, content="server-readable")

    with pytest.raises(HTTPException) as raised:
        report_message_evidence(
            message,
            disclosed_content="replacement",
            disclosure_acknowledged=True,
        )

    assert cast(dict[str, Any], raised.value.detail)["code"] == "REPORT_DISCLOSURE_UNEXPECTED"


def test_plaintext_report_rejects_empty_client_disclosure_too() -> None:
    message = report_message(e2ee=None, content="server-readable")

    with pytest.raises(HTTPException) as raised:
        report_message_evidence(
            message,
            disclosed_content="",
            disclosure_acknowledged=True,
        )

    assert cast(dict[str, Any], raised.value.detail)["code"] == "REPORT_DISCLOSURE_UNEXPECTED"


def test_report_schema_rejects_whitespace_only_disclosure() -> None:
    with pytest.raises(ValidationError):
        ReportCreate(
            target_type="message",
            target_ref="1@alpha.localhost",
            message_ref="1@alpha.localhost",
            category="spam",
            disclosed_content="   ",
            disclosure_acknowledged=True,
        )


@pytest.mark.parametrize(
    ("source", "reporter_id", "reporter_domain", "expected_reporter"),
    [
        ("user", 42, "alpha.localhost", "42@alpha.localhost"),
        ("photodna", None, None, None),
    ],
)
def test_admin_report_payload_identifies_user_and_automated_sources(
    source: str,
    reporter_id: int | None,
    reporter_domain: str | None,
    expected_reporter: str | None,
) -> None:
    timestamp = datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=99,
            source=source,
            reporter_id=reporter_id,
            reporter_domain=reporter_domain,
            target_type="attachment" if source == "photodna" else "message",
            target_ref="7@alpha.localhost",
            category="illegal_content" if source == "photodna" else "spam",
            description=None,
            message_ref=None,
            evidence={},
            encryption_mode="plaintext",
            status="submitted",
            assigned_admin_id=None,
            assigned_admin_domain=None,
            resolution=None,
            created_at=timestamp,
            updated_at=timestamp,
            resolved_at=None,
        ),
    )

    payload = report_payload(report)

    assert payload["source"] == source
    assert payload["reporter_ref"] == expected_reporter
