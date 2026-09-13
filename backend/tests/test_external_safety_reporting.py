from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

from app.admin.auth import AdminPrincipal
from app.api import admin_portal
from app.api.admin_portal import ExternalReportSubmission


def case():
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=42,
        source="photodna",
        evidence={"content_sha256": "a" * 64},
        target_type="attachment",
        target_ref="7@remote.test",
        message_ref="8@remote.test",
        reporter_id=None,
        reporter_domain=None,
        category="illegal_content",
        description="Automated match",
        encryption_mode="plaintext",
        status="submitted",
        assigned_admin_id=None,
        assigned_admin_domain=None,
        resolution=None,
        created_at=now,
        updated_at=now,
        resolved_at=None,
    )


def principal(capabilities=frozenset({"reports.manage"})):
    return AdminPrincipal(
        SimpleNamespace(id=1, origin_domain="home.test"), frozenset(), capabilities
    )


def test_external_submission_rejects_blank_fields_and_invalid_times():
    valid = dict(destination="Authority", reference="receipt-1", submitted_at=datetime.now(UTC))
    for invalid in (
        {"destination": " "},
        {"reference": " "},
        {"submitted_at": datetime.now()},
        {"submitted_at": datetime.now(UTC) + timedelta(days=1)},
    ):
        with pytest.raises(ValidationError):
            ExternalReportSubmission(**(valid | invalid))


@pytest.mark.asyncio
async def test_submission_appends_without_overwriting_evidence_and_is_audited(monkeypatch):
    report = case()
    session = SimpleNamespace(
        get=AsyncMock(return_value=report), commit=AsyncMock(), refresh=AsyncMock()
    )
    audit = AsyncMock()
    monkeypatch.setattr(admin_portal, "audit", audit)
    payload = ExternalReportSubmission(
        destination="Authority", reference="receipt-1", submitted_at=datetime.now(UTC)
    )
    for _ in range(2):
        await admin_portal.record_external_submission(42, payload, principal(), session, None)
    assert report.evidence["content_sha256"] == "a" * 64
    assert len(report.evidence["external_submissions"]) == 2
    assert report.evidence["external_submissions"][0]["recorded_by"] == "1@home.test"
    assert audit.await_count == 2
    assert session.get.call_args.kwargs["with_for_update"] is True


@pytest.mark.asyncio
async def test_export_includes_origin_evidence_and_audits_access(monkeypatch):
    report = case()
    session = SimpleNamespace(get=AsyncMock(return_value=report), commit=AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(admin_portal, "audit", audit)
    monkeypatch.setattr(admin_portal, "report_identity_lookup", AsyncMock(return_value={}))
    response = Response()
    package = await admin_portal.export_report(
        42, principal(), session, None, SimpleNamespace(domain="home.test"), response
    )
    assert package["reporting_instance"] == "home.test"
    assert package["report"]["target_ref"] == "7@remote.test"
    assert package["report"]["evidence"]["content_sha256"] == "a" * 64
    assert response.headers["Cache-Control"] == "no-store"
    audit.assert_awaited_once()
    with pytest.raises(HTTPException) as error:
        await admin_portal.export_report(
            42,
            principal(frozenset({"reports.read"})),
            session,
            None,
            SimpleNamespace(domain="home.test"),
            Response(),
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_submission_requires_management_permission_and_existing_case():
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    payload = ExternalReportSubmission(
        destination="Authority", reference="receipt-1", submitted_at=datetime.now(UTC)
    )
    with pytest.raises(HTTPException) as error:
        await admin_portal.record_external_submission(
            42, payload, principal(frozenset({"reports.read"})), session, None
        )
    assert error.value.status_code == 403
    session.get.assert_not_awaited()
    with pytest.raises(HTTPException) as error:
        await admin_portal.record_external_submission(42, payload, principal(), session, None)
    assert error.value.status_code == 404


def test_remote_history_identity_is_optional_and_requires_a_qualified_reference():
    from app.api.media import remote_history_uploader

    assert remote_history_uploader({}, "origin.test") is None
    assert remote_history_uploader({"X-Kaede-Media-Uploader": "123@users.test"}, "origin.test") == (
        123,
        "users.test",
    )
    for value in ("123", "invalid@users.test"):
        with pytest.raises(HTTPException) as error:
            remote_history_uploader({"X-Kaede-Media-Uploader": value}, "origin.test")
        assert error.value.status_code == 503
