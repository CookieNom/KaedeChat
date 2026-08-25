from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from app.admin.auth import AdminPrincipal
from app.api import admin_portal
from app.api.admin_portal import (
    ReportAttachmentEvidenceCommit,
    ReportAttachmentEvidenceTicketCreate,
    ReportCreate,
    report_attachment_evidence,
    report_message_evidence,
    report_payload,
)
from app.db.bot_models import AbuseReport
from app.db.models import Attachment, MediaTombstoneSource, Message, User
from app.federation.security import FederationPrincipal
from app.media.jobs import update_report_evidence_status
from app.media.photodna import PhotoDNAFinding, PhotoDNAMatchFlag


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


def test_message_report_bundles_all_attachments_and_highlights_context_attachment() -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    message = report_message(e2ee=None, content="message with media")
    attachments = [
        cast(
            Attachment,
            SimpleNamespace(
                id=9,
                origin_domain="alpha.localhost",
                uploader_id=7,
                uploader_domain="alpha.localhost",
                created_at=timestamp,
                filename="photo.png",
                content_type="image/png",
                detected_content_type="image/png",
                size=1234,
                encryption_mode="plaintext",
            ),
        ),
        cast(
            Attachment,
            SimpleNamespace(
                id=10,
                origin_domain="alpha.localhost",
                uploader_id=7,
                uploader_domain="alpha.localhost",
                created_at=timestamp,
                filename="clip.mp4",
                content_type="video/mp4",
                detected_content_type="video/mp4",
                size=5678,
                encryption_mode="plaintext",
            ),
        ),
    ]

    evidence, mode = report_message_evidence(
        message,
        disclosed_content=None,
        disclosure_acknowledged=False,
        attachments=attachments,
        focused_attachment=attachments[1],
    )

    assert mode == "plaintext"
    assert evidence["content"] == "message with media"
    assert [
        item["attachment_ref"] for item in cast(list[dict[str, Any]], evidence["attachments"])
    ] == [
        "9@alpha.localhost",
        "10@alpha.localhost",
    ]
    assert evidence["attachment_ref"] == "10@alpha.localhost"
    assert evidence["content_type"] == "video/mp4"


def test_focused_attachment_is_only_valid_for_message_reports() -> None:
    with pytest.raises(ValidationError):
        ReportCreate(
            target_type="attachment",
            target_ref="9@alpha.localhost",
            message_ref="1@alpha.localhost",
            focused_attachment_ref="9@alpha.localhost",
            category="spam",
        )


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


def test_attachment_report_schema_carries_its_containing_message() -> None:
    payload = ReportCreate(
        target_type="attachment",
        target_ref="9@alpha.localhost",
        message_ref="1@alpha.localhost",
        category="illegal_content",
    )

    assert payload.target_type == "attachment"
    assert str(payload.message_ref) == "1@alpha.localhost"


@pytest.mark.parametrize(
    ("message_e2ee", "attachment_encryption", "expected_mode", "includes_content"),
    [
        (None, "plaintext", "plaintext", True),
        ({"ciphertext": "opaque"}, "e2ee", "e2ee_metadata", False),
    ],
)
def test_attachment_report_retains_only_server_verified_metadata(
    message_e2ee: dict[str, object] | None,
    attachment_encryption: str,
    expected_mode: str,
    includes_content: bool,
) -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    message = cast(
        Message,
        SimpleNamespace(
            id=1,
            origin_domain="alpha.localhost",
            author_id=7,
            author_domain="alpha.localhost",
            channel_id=2,
            channel_domain="alpha.localhost",
            content="message context" if message_e2ee is None else None,
            e2ee=message_e2ee,
            created_at=timestamp,
        ),
    )
    attachment = cast(
        Attachment,
        SimpleNamespace(
            id=9,
            origin_domain="alpha.localhost",
            uploader_id=7,
            uploader_domain="alpha.localhost",
            created_at=timestamp,
            filename="encrypted-file" if attachment_encryption == "e2ee" else "video.mp4",
            content_type=(
                "application/octet-stream" if attachment_encryption == "e2ee" else "video/mp4"
            ),
            detected_content_type=None,
            size=1234,
            encryption_mode=attachment_encryption,
        ),
    )

    evidence, mode = report_attachment_evidence(message, attachment)

    assert mode == expected_mode
    assert evidence["attachment_ref"] == "9@alpha.localhost"
    assert evidence["uploader_ref"] == "7@alpha.localhost"
    assert ("content" in evidence) is includes_content
    assert "content_sha256" not in evidence
    assert "object_key" not in evidence


class AttachmentReportSession:
    def __init__(self, message: Message, attachment: Attachment) -> None:
        self.message = message
        self.attachment = attachment
        self.added: AbuseReport | None = None

    async def get(self, model: type[object], key: object) -> object | None:
        if model is Message:
            return self.message
        if model is Attachment:
            return self.attachment
        return None

    def add(self, report: AbuseReport) -> None:
        self.added = report

    async def commit(self) -> None:
        assert self.added is not None
        timestamp = datetime(2026, 8, 25, 16, tzinfo=UTC)
        self.added.created_at = timestamp
        self.added.updated_at = timestamp


class ReportSnowflake:
    async def mint(self) -> int:
        return 123


class ReportAttachmentPreviewSession:
    def __init__(self, report: AbuseReport, attachment: Attachment | None) -> None:
        self.report = report
        self.attachment = attachment
        self.committed = False

    async def get(
        self,
        model: type[object],
        key: object,
        **_kwargs: object,
    ) -> object | None:
        if model is AbuseReport:
            return self.report
        if model is Attachment:
            return self.attachment
        if model is MediaTombstoneSource:
            return None
        return None

    async def commit(self) -> None:
        self.committed = True


@pytest.mark.asyncio
async def test_create_attachment_report_validates_message_binding_and_stores_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    reporter = SimpleNamespace(
        id=4,
        origin_domain="alpha.localhost",
        is_local=True,
        account_type="human",
    )
    message = cast(
        Message,
        SimpleNamespace(
            id=1,
            origin_domain="alpha.localhost",
            channel_id=2,
            channel_domain="alpha.localhost",
            author_id=7,
            author_domain="alpha.localhost",
            content="look at this",
            e2ee=None,
            deleted_at=None,
            created_at=timestamp,
        ),
    )
    attachment = cast(
        Attachment,
        SimpleNamespace(
            id=9,
            origin_domain="alpha.localhost",
            message_id=1,
            message_domain="alpha.localhost",
            uploader_id=7,
            uploader_domain="alpha.localhost",
            filename="photo.jpg",
            content_type="image/jpeg",
            detected_content_type="image/jpeg",
            size=2048,
            encryption_mode="plaintext",
            purpose="attachment",
            deleted_at=None,
            created_at=timestamp,
        ),
    )
    session = AttachmentReportSession(message, attachment)
    monkeypatch.setattr(admin_portal, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(admin_portal, "load_channel_access", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_portal, "require_channel_permissions", AsyncMock(return_value=0))

    result = await admin_portal.create_report(
        ReportCreate(
            target_type="attachment",
            target_ref="9@alpha.localhost",
            message_ref="1@alpha.localhost",
            category="illegal_content",
            description="This image should be reviewed",
        ),
        Response(),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, session),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert result["target_type"] == "attachment"
    assert session.added is not None
    assert session.added.message_ref == "1@alpha.localhost"
    assert session.added.evidence["attachment_ref"] == "9@alpha.localhost"
    assert session.added.evidence["content_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_create_message_report_bundles_attachment_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    reporter = cast(
        User,
        SimpleNamespace(
            id=4,
            origin_domain="alpha.localhost",
            is_local=True,
            account_type="human",
        ),
    )
    message = cast(
        Message,
        SimpleNamespace(
            id=1,
            origin_domain="alpha.localhost",
            author_id=7,
            author_domain="alpha.localhost",
            channel_id=2,
            channel_domain="alpha.localhost",
            content="message with media",
            e2ee=None,
            deleted_at=None,
            created_at=timestamp,
        ),
    )
    attachment = cast(
        Attachment,
        SimpleNamespace(
            id=9,
            origin_domain="alpha.localhost",
            message_id=1,
            message_domain="alpha.localhost",
            uploader_id=7,
            uploader_domain="alpha.localhost",
            filename="photo.png",
            content_type="image/png",
            detected_content_type="image/png",
            size=2048,
            encryption_mode="plaintext",
            purpose="attachment",
            deleted_at=None,
            created_at=timestamp,
        ),
    )

    class MessageReportSession:
        def __init__(self) -> None:
            self.added: AbuseReport | None = None

        async def get(self, model: type[object], _key: object) -> object | None:
            return message if model is Message else None

        async def scalars(self, _statement: object) -> list[Attachment]:
            return [attachment]

        def add(self, report: AbuseReport) -> None:
            self.added = report

        async def commit(self) -> None:
            assert self.added is not None
            self.added.status = "submitted"
            self.added.created_at = timestamp
            self.added.updated_at = timestamp

    session = MessageReportSession()
    monkeypatch.setattr(admin_portal, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(admin_portal, "load_channel_access", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_portal, "require_channel_permissions", AsyncMock(return_value=0))

    result = await admin_portal.create_report(
        ReportCreate(
            target_type="message",
            target_ref="1@alpha.localhost",
            message_ref="1@alpha.localhost",
            focused_attachment_ref="9@alpha.localhost",
            category="illegal_content",
        ),
        Response(),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, session),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert result["target_type"] == "message"
    assert session.added is not None
    assert session.added.target_ref == "1@alpha.localhost"
    assert session.added.evidence["attachment_ref"] == "9@alpha.localhost"
    bundled = cast(list[dict[str, Any]], session.added.evidence["attachments"])
    assert bundled[0]["content_type"] == "image/png"


@pytest.mark.asyncio
async def test_remote_guild_message_report_is_forwarded_to_channel_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    reporter = cast(
        User,
        SimpleNamespace(
            id=4,
            origin_domain="alpha.localhost",
            is_local=True,
            account_type="human",
        ),
    )
    message = cast(
        Message,
        SimpleNamespace(
            id=1,
            origin_domain="gamma.localhost",
            author_id=7,
            author_domain="gamma.localhost",
            channel_id=2,
            channel_domain="beta.localhost",
            content="report this remote-guild message",
            e2ee=None,
            deleted_at=None,
            created_at=timestamp,
        ),
    )

    class ForwardingSession:
        def __init__(self) -> None:
            self.added: AbuseReport | None = None

        async def get(self, model: type[object], _key: object) -> object | None:
            return message if model is Message else None

        async def scalars(self, _statement: object) -> list[Attachment]:
            return []

        def add(self, report: AbuseReport) -> None:
            self.added = report

        async def commit(self) -> None:
            assert self.added is not None
            self.added.created_at = timestamp
            self.added.updated_at = timestamp

    session = ForwardingSession()
    send = AsyncMock(return_value=SimpleNamespace(status_code=201))
    monkeypatch.setattr(admin_portal, "signed_request", send)
    monkeypatch.setattr(
        admin_portal,
        "decode_federation_response_json",
        lambda *_args, **_kwargs: {"id": "987"},
    )
    monkeypatch.setattr(admin_portal, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(admin_portal, "load_channel_access", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_portal, "require_channel_permissions", AsyncMock(return_value=0))

    result = await admin_portal.create_report(
        ReportCreate(
            target_type="message",
            target_ref="1@gamma.localhost",
            message_ref="1@gamma.localhost",
            category="harassment",
        ),
        Response(),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, session),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert result["status"] == "awaiting_remote"
    assert session.added is not None
    assert session.added.evidence["report_authority"] == "beta.localhost"
    assert session.added.evidence["forwarded_report_id"] == "987"
    assert send.await_args.args[3:5] == ("beta.localhost", "/_kaede/v1/reports")
    assert send.await_args.kwargs["payload"]["reporter_ref"] == "4@alpha.localhost"


@pytest.mark.asyncio
async def test_channel_authority_stores_forwarded_report_from_remote_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 25, 15, tzinfo=UTC)
    reporter = cast(
        User,
        SimpleNamespace(
            id=4,
            origin_domain="alpha.localhost",
            is_local=False,
            account_type="human",
        ),
    )
    message = cast(
        Message,
        SimpleNamespace(
            id=1,
            origin_domain="gamma.localhost",
            author_id=7,
            author_domain="gamma.localhost",
            channel_id=2,
            channel_domain="beta.localhost",
            content="authority-visible evidence",
            e2ee=None,
            deleted_at=None,
            created_at=timestamp,
        ),
    )

    class AuthoritySession:
        def __init__(self) -> None:
            self.added: AbuseReport | None = None

        async def scalar(self, _statement: object) -> object | None:
            return None

        async def get(self, model: type[object], key: object, **_kwargs: object) -> object | None:
            if model is User and key == (4, "alpha.localhost"):
                return reporter
            if model is Message and key == (1, "gamma.localhost"):
                return message
            return None

        async def scalars(self, _statement: object) -> list[Attachment]:
            return []

        def add(self, report: AbuseReport) -> None:
            self.added = report

        async def commit(self) -> None:
            assert self.added is not None
            self.added.status = "submitted"
            self.added.created_at = timestamp
            self.added.updated_at = timestamp

    session = AuthoritySession()
    monkeypatch.setattr(
        admin_portal,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(admin_portal, "load_channel_access", AsyncMock(return_value=object()))
    monkeypatch.setattr(admin_portal, "require_channel_permissions", AsyncMock(return_value=0))

    result = await admin_portal.create_federated_report(
        admin_portal.FederatedReportCreate(
            target_type="message",
            target_ref="1@gamma.localhost",
            message_ref="1@gamma.localhost",
            reporter_ref="4@alpha.localhost",
            source_report_ref="123@alpha.localhost",
            category="harassment",
        ),
        FederationPrincipal(origin="alpha.localhost", key_id="key"),
        cast(Any, session),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="beta.localhost")),
    )

    assert result["status"] == "submitted"
    assert session.added is not None
    assert session.added.reporter_is_local is False
    assert session.added.reporter_domain == "alpha.localhost"
    assert session.added.evidence["source_report_ref"] == "123@alpha.localhost"
    assert session.added.evidence["content"] == "authority-visible evidence"


@pytest.mark.asyncio
async def test_admin_can_view_only_the_bound_plaintext_report_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=44,
            source="user",
            target_type="attachment",
            target_ref="9@alpha.localhost",
            message_ref="1@alpha.localhost",
            evidence={},
            encryption_mode="plaintext",
        ),
    )
    attachment = cast(
        Attachment,
        SimpleNamespace(
            id=9,
            origin_domain="alpha.localhost",
            message_id=1,
            message_domain="alpha.localhost",
            report_id=None,
            purpose="attachment",
            encryption_mode="plaintext",
            deleted_at=None,
        ),
    )
    actor = cast(User, SimpleNamespace(id=2, origin_domain="alpha.localhost"))
    principal = AdminPrincipal(actor, frozenset({"auditor"}), frozenset({"reports.read"}))
    session = ReportAttachmentPreviewSession(report, attachment)
    response = object()
    monkeypatch.setattr(
        admin_portal,
        "redirect_to_object",
        lambda *_args, **_kwargs: response,
    )
    audit = AsyncMock()
    monkeypatch.setattr(admin_portal, "audit", audit)

    result = await admin_portal.view_report_attachment(
        44,
        principal,
        cast(Any, session),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
        "thumbnail_512",
    )

    assert result is response
    assert session.committed is True
    audit.assert_awaited_once()
    audit_call = audit.await_args
    assert audit_call is not None
    assert audit_call.args[3:6] == (
        "admin.report.attachment_view",
        "report",
        "44",
    )


@pytest.mark.parametrize(
    ("target_ref", "message_ref", "encryption_mode", "expected_code"),
    [
        ("9@remote.example", "1@alpha.localhost", "plaintext", "REMOTE_REPORT_ATTACHMENT"),
        ("9@alpha.localhost", "2@alpha.localhost", "plaintext", "REPORT_ATTACHMENT_NOT_FOUND"),
        ("9@alpha.localhost", "1@alpha.localhost", "e2ee", "REPORT_ATTACHMENT_NOT_FOUND"),
    ],
)
@pytest.mark.asyncio
async def test_admin_report_attachment_preview_rejects_unsafe_media(
    target_ref: str,
    message_ref: str,
    encryption_mode: str,
    expected_code: str,
) -> None:
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=44,
            source="user",
            target_type="attachment",
            target_ref=target_ref,
            message_ref=message_ref,
            evidence={},
            encryption_mode="plaintext",
        ),
    )
    attachment = cast(
        Attachment,
        SimpleNamespace(
            id=9,
            origin_domain="alpha.localhost",
            message_id=1,
            message_domain="alpha.localhost",
            report_id=None,
            purpose="attachment",
            encryption_mode=encryption_mode,
            deleted_at=None,
        ),
    )
    actor = cast(User, SimpleNamespace(id=2, origin_domain="alpha.localhost"))
    principal = AdminPrincipal(actor, frozenset({"auditor"}), frozenset({"reports.read"}))

    with pytest.raises(HTTPException) as raised:
        await admin_portal.view_report_attachment(
            44,
            principal,
            cast(Any, ReportAttachmentPreviewSession(report, attachment)),
            cast(Any, ReportSnowflake()),
            cast(Any, SimpleNamespace(domain="alpha.localhost")),
            "original",
        )

    assert cast(dict[str, Any], raised.value.detail)["code"] == expected_code


@pytest.mark.asyncio
async def test_admin_previews_reporter_disclosed_copy_for_remote_e2ee_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = cast(
        AbuseReport,
        SimpleNamespace(
            id=44,
            source="user",
            target_type="attachment",
            target_ref="9@remote.example",
            message_ref="1@remote.example",
            evidence={"disclosed_attachment_ref": "81@alpha.localhost"},
            encryption_mode="e2ee_user_disclosed",
        ),
    )
    evidence_attachment = cast(
        Attachment,
        SimpleNamespace(
            id=81,
            origin_domain="alpha.localhost",
            message_id=None,
            message_domain=None,
            report_id=44,
            purpose="attachment",
            encryption_mode="plaintext",
            deleted_at=None,
        ),
    )
    actor = cast(User, SimpleNamespace(id=2, origin_domain="alpha.localhost"))
    principal = AdminPrincipal(actor, frozenset({"auditor"}), frozenset({"reports.read"}))
    session = ReportAttachmentPreviewSession(report, evidence_attachment)
    response = object()
    monkeypatch.setattr(
        admin_portal,
        "redirect_to_object",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(admin_portal, "audit", AsyncMock())

    result = await admin_portal.view_report_attachment(
        44,
        principal,
        cast(Any, session),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
        "original",
    )

    assert result is response
    assert session.committed is True


class EvidenceUploadSession:
    def __init__(self, report: AbuseReport, existing: Attachment | None = None) -> None:
        self.report = report
        self.existing = existing
        self.committed = False

    async def get(
        self,
        model: type[object],
        _key: object,
        **_kwargs: object,
    ) -> object | None:
        if model is AbuseReport:
            return self.report
        return None

    async def scalar(self, _statement: object) -> object | None:
        return self.existing

    async def commit(self) -> None:
        self.committed = True


def encrypted_attachment_report() -> AbuseReport:
    timestamp = datetime(2026, 8, 25, 18, tzinfo=UTC)
    return cast(
        AbuseReport,
        SimpleNamespace(
            id=44,
            source="user",
            reporter_id=4,
            reporter_domain="alpha.localhost",
            target_type="attachment",
            target_ref="9@remote.example",
            category="harassment",
            description="review this file",
            message_ref="1@remote.example",
            evidence={"attachment_encryption_mode": "e2ee"},
            encryption_mode="e2ee_metadata",
            status="submitted",
            created_at=timestamp,
            updated_at=timestamp,
        ),
    )


@pytest.mark.asyncio
async def test_reporter_can_create_plaintext_evidence_ticket_only_with_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = encrypted_attachment_report()
    session = EvidenceUploadSession(report)
    reporter = cast(
        User,
        SimpleNamespace(id=4, origin_domain="alpha.localhost", is_local=True),
    )
    evidence_attachment = cast(Attachment, SimpleNamespace(id=81))
    create_ticket = AsyncMock(return_value=(evidence_attachment, "https://upload.invalid"))
    monkeypatch.setattr(admin_portal, "create_upload_ticket", create_ticket)
    monkeypatch.setattr(admin_portal, "ticket_payload", lambda *_args: {"id": "81"})
    monkeypatch.setattr(admin_portal, "enforce_keyed_rate_limit", AsyncMock())

    result = await admin_portal.create_report_attachment_evidence_ticket(
        44,
        ReportAttachmentEvidenceTicketCreate(
            filename="evidence.jpg",
            content_type="image/jpeg",
            size=2048,
            disclosure_acknowledged=True,
        ),
        Response(),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, session),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert result == {"id": "81"}
    assert session.committed is True
    ticket_call = create_ticket.await_args
    assert ticket_call is not None
    assert ticket_call.kwargs["report_id"] == 44
    assert ticket_call.kwargs["encryption_mode"] == "plaintext"

    with pytest.raises(HTTPException) as raised:
        await admin_portal.create_report_attachment_evidence_ticket(
            44,
            ReportAttachmentEvidenceTicketCreate(
                filename="evidence.jpg",
                content_type="image/jpeg",
                size=2048,
                disclosure_acknowledged=False,
            ),
            Response(),
            cast(Any, SimpleNamespace(user=reporter)),
            cast(Any, EvidenceUploadSession(report)),
            cast(Any, object()),
            cast(Any, ReportSnowflake()),
            cast(Any, SimpleNamespace(domain="alpha.localhost")),
        )
    assert cast(dict[str, Any], raised.value.detail)["code"] == (
        "E2EE_ATTACHMENT_DISCLOSURE_REQUIRED"
    )


@pytest.mark.asyncio
async def test_remote_report_evidence_upload_is_prepared_by_channel_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = encrypted_attachment_report()
    report.target_type = "message"
    report.reporter_is_local = True
    report.evidence.update(
        {
            "report_authority": "beta.localhost",
            "forwarded_report_id": "908",
        }
    )
    reporter = cast(
        User,
        SimpleNamespace(id=4, origin_domain="alpha.localhost", is_local=True),
    )
    send = AsyncMock(return_value=SimpleNamespace(status_code=201))
    create_ticket = AsyncMock()
    monkeypatch.setattr(admin_portal, "signed_request", send)
    monkeypatch.setattr(admin_portal, "create_upload_ticket", create_ticket)
    monkeypatch.setattr(admin_portal, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(
        admin_portal,
        "decode_federation_response_json",
        lambda *_args, **_kwargs: {
            "id": "81",
            "upload_url": "https://authority-upload.invalid",
        },
    )

    result = await admin_portal.create_report_attachment_evidence_ticket(
        44,
        ReportAttachmentEvidenceTicketCreate(
            filename="evidence.jpg",
            content_type="image/jpeg",
            size=2048,
            disclosure_acknowledged=True,
        ),
        Response(),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, EvidenceUploadSession(report)),
        cast(Any, object()),
        cast(Any, ReportSnowflake()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert result["id"] == "81"
    assert send.await_args.args[3:5] == (
        "beta.localhost",
        "/_kaede/v1/reports/908/attachment-evidence",
    )
    create_ticket.assert_not_awaited()


@pytest.mark.asyncio
async def test_committing_decrypted_evidence_marks_report_as_reporter_disclosed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = encrypted_attachment_report()
    session = EvidenceUploadSession(report)
    reporter = cast(User, SimpleNamespace(id=4, origin_domain="alpha.localhost"))
    evidence_attachment = cast(
        Attachment,
        SimpleNamespace(
            id=81,
            origin_domain="alpha.localhost",
            report_id=44,
            filename="evidence.jpg",
            content_type="image/jpeg",
            size=2048,
            scan_status="pending",
        ),
    )
    monkeypatch.setattr(
        admin_portal,
        "finalize_attachment",
        AsyncMock(return_value=evidence_attachment),
    )
    wake = AsyncMock()
    monkeypatch.setattr(admin_portal, "enqueue_best_effort", wake)

    result = await admin_portal.commit_report_attachment_evidence(
        44,
        ReportAttachmentEvidenceCommit(
            attachment_id="81",
            disclosure_acknowledged=True,
        ),
        cast(Any, SimpleNamespace(user=reporter)),
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )

    assert report.encryption_mode == "e2ee_user_disclosed"
    assert report.evidence["disclosed_attachment_ref"] == "81@alpha.localhost"
    assert (
        cast(dict[str, Any], report.evidence["attachment_disclosure"])["reporter_acknowledged"]
        is True
    )
    assert cast(dict[str, Any], result["evidence"])["scan_status"] == "pending"
    assert session.committed is True
    wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_disclosed_evidence_safety_match_updates_original_report() -> None:
    report = encrypted_attachment_report()
    report.encryption_mode = "e2ee_user_disclosed"
    session = EvidenceUploadSession(report)
    evidence_attachment = cast(
        Attachment,
        SimpleNamespace(
            id=81,
            origin_domain="alpha.localhost",
            report_id=44,
            uploader_id=4,
            uploader_domain="alpha.localhost",
            detected_content_type="image/jpeg",
            content_sha256="a" * 64,
        ),
    )
    finding = PhotoDNAFinding(
        tracking_id="tracking",
        flags=(
            PhotoDNAMatchFlag(
                source="test",
                violations=("sexual_abuse",),
                match_distance=0,
                match_id="match",
            ),
        ),
    )

    await update_report_evidence_status(
        cast(Any, session),
        evidence_attachment,
        "quarantined",
        finding=finding,
    )

    assert report.category == "illegal_content"
    assert report.evidence["disclosed_attachment_scan_status"] == "quarantined"
    assert isinstance(report.evidence["photodna"], dict)


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
