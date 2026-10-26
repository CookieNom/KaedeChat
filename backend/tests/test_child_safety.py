from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.admin.auth import AdminPrincipal
from app.safety import api, service, storage


def settings():
    return SimpleNamespace(
        safety_evidence_key_bytes=bytes(range(32)),
        media_attachments_bucket="private-media",
        domain="home.test",
    )


def case(**changes):
    now = datetime.now(UTC)
    return SimpleNamespace(
        **{
            "id": 42,
            "status": "confirmed",
            "removal_objects": [],
            "removal_completed_at": None,
            "source": "photodna",
            "subject_ref": None,
            "subject_snapshot": None,
            "message_ref": None,
            "message_snapshot": {},
            "submissions": [],
            "holds": [],
            "retention_extensions": [],
            "completed_submission_at": None,
            "retain_until": None,
            "closed_at": None,
            "disposed_at": None,
            "created_at": now,
            "details": storage.encrypt_metadata(settings(), {}, "case:42"),
            **changes,
        }
    )


def principal(*capabilities):
    return AdminPrincipal(
        SimpleNamespace(id=1, origin_domain="home.test"), frozenset(), frozenset(capabilities)
    )


def test_evidence_encryption_binds_key_and_detects_tampering():
    config = settings()
    encrypted = storage.encrypt(config, b"synthetic private bytes", "evidence/42/abc")
    assert b"synthetic private bytes" not in encrypted
    assert storage.decrypt(config, encrypted, "evidence/42/abc") == b"synthetic private bytes"
    for changed, context in (
        (encrypted, "evidence/43/abc"),
        (encrypted[:-1] + bytes([encrypted[-1] ^ 1]), "evidence/42/abc"),
    ):
        with pytest.raises(storage.StorageError):
            storage.decrypt(config, changed, context)
    config.safety_evidence_key_bytes = b"x" * 32
    with pytest.raises(storage.StorageError):
        storage.decrypt(config, encrypted, "evidence/42/abc")


@pytest.mark.asyncio
async def test_preservation_writes_only_verified_ciphertext_to_reserved_prefix(monkeypatch):
    objects = {}

    class Objects:
        async def put(self, bucket, key, data, content_type):
            assert bucket == "private-media"
            assert key.startswith("evidence/")
            assert content_type == "application/octet-stream"
            objects[key] = data

        async def get(self, bucket, key, *, max_bytes):
            assert len(objects[key]) <= max_bytes
            return objects[key]

    monkeypatch.setattr(storage, "S3Storage", lambda _settings: Objects())
    evidence_store = storage.EvidenceStorage(settings())
    await evidence_store.put("evidence/42/abc", b"synthetic media")
    assert objects["evidence/42/abc"] != b"synthetic media"
    with pytest.raises(ValueError):
        await evidence_store.put("attachments/42", b"synthetic media")
    with pytest.raises(storage.StorageError):
        await evidence_store.get("evidence/42/abc", size=len(b"synthetic media"), sha256="0" * 64)


def test_message_snapshot_is_encrypted_for_the_case():
    import base64

    sealed = service.snapshot_message(
        settings(),
        42,
        SimpleNamespace(
            content="private report text",
            e2ee=None,
            embeds=[],
            mention_user_refs=[],
            edited_at=None,
        ),
    )
    assert set(sealed) == {"sealed"}
    assert "private report text" not in str(sealed)
    restored = storage.decrypt_metadata(
        settings(), base64.b64decode(sealed["sealed"]), "message:42"
    )
    assert restored["content"] == "private report text"


@pytest.mark.asyncio
async def test_account_deletion_preserves_original_and_current_identity_without_credentials():
    now = datetime(2026, 10, 26, tzinfo=UTC)
    user = SimpleNamespace(
        id=7,
        origin_domain="home.test",
        username="current-name",
        display_name="Current name",
        email="current@example.test",
        email_verified_at=now,
        created_at=now - timedelta(days=30),
        is_local=True,
        password_hash=b"password-secret",
        mfa_secret="mfa-secret",
    )
    original = service.account_snapshot(user) | {"email": "original@example.test"}
    retained = case(
        subject_ref="7@home.test",
        subject_snapshot=storage.encrypt_metadata(settings(), original, "subject:42"),
        details=storage.encrypt_metadata(settings(), {"review_note": "preserve me"}, "case:42"),
    )
    original_ciphertext = retained.subject_snapshot
    session_fields = {
        "device_name": "My phone",
        "user_agent": "Kaede Test Client",
        "ip_address": "192.0.2.7",
        "created_at": now - timedelta(days=2),
        "last_used_at": now,
        "expires_at": now + timedelta(days=1),
        "absolute_expires_at": now + timedelta(days=7),
        "revoked_at": None,
    }
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[retained]),
        execute=AsyncMock(return_value=SimpleNamespace(mappings=lambda: [session_fields])),
    )

    await service.snapshot_subject_before_deletion(session, settings(), user)

    assert retained.subject_snapshot == original_ciphertext
    assert storage.decrypt_metadata(settings(), retained.subject_snapshot, "subject:42") == original
    details = service.case_details(settings(), retained)
    assert details["review_note"] == "preserve me"
    deletion = details["account_at_deletion"]
    assert deletion["account"]["email"] == "current@example.test"
    assert deletion["account"]["username"] == "current-name"
    assert deletion["sessions"] == [
        {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in session_fields.items()
        }
    ]
    statement = session.execute.await_args.args[0]
    assert set(statement.selected_columns.keys()) == set(session_fields)
    for excluded in ("password_hash", "mfa_secret", "refresh_token_hash", "previous_token_hash"):
        assert excluded not in repr(details)
    for private_value in (b"current@example.test", b"192.0.2.7", b"Kaede Test Client"):
        assert private_value not in retained.details

    deletion_ciphertext = retained.details
    user.email = "subsequently-changed@example.test"
    await service.snapshot_subject_before_deletion(session, settings(), user)
    assert retained.subject_snapshot == original_ciphertext
    assert retained.details == deletion_ciphertext
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_account_deletion_without_retained_cases_does_not_read_session_metadata():
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]), execute=AsyncMock())
    await service.snapshot_subject_before_deletion(
        session, settings(), SimpleNamespace(id=7, origin_domain="home.test")
    )
    session.execute.assert_not_awaited()


def test_calendar_year_and_disposal_obligations():
    leap_day = datetime(2024, 2, 29, 12, tzinfo=UTC)
    assert service.add_calendar_year(leap_day) == datetime(2025, 2, 28, 12, tzinfo=UTC)
    now = datetime.now(UTC)
    retained = case(closed_at=now)
    assert not service.can_dispose(retained, now=now)  # Submission is still missing.
    retained.completed_submission_at = now - timedelta(days=366)
    retained.retain_until = now + timedelta(seconds=1)
    assert not service.can_dispose(retained, now=now)
    retained.retain_until = now
    retained.holds = [{"id": "hold", "released_at": None}]
    assert not service.can_dispose(retained, now=now)
    retained.holds = [{"id": "hold", "released_at": now.isoformat()}]
    assert not service.can_dispose(retained, now=now)  # Storage cleanup still pending.
    retained.removal_completed_at = now
    assert service.can_dispose(retained, now=now)
    retained.status = "pending"
    assert not service.can_dispose(retained, now=now)
    assert service.can_dispose(case(status="dismissed", closed_at=now), now=now)


def test_manual_submission_validates_completion_and_reference():
    valid = {"reference": "receipt", "submitted_at": datetime.now(UTC)}
    for invalid in (
        {"reference": " "},
        {"submitted_at": datetime.now()},
        {"submitted_at": datetime.now(UTC) + timedelta(days=1)},
    ):
        with pytest.raises(ValidationError):
            api.Submission(**(valid | invalid))


@pytest.mark.asyncio
async def test_manual_receipt_sets_minimum_without_shortening_and_rejects_unknown_evidence(
    monkeypatch,
):
    retained = case(retain_until=datetime(2028, 1, 1, tzinfo=UTC))
    monkeypatch.setattr(api, "load_case", AsyncMock(return_value=retained))
    monkeypatch.setattr(api, "case_payload", AsyncMock(return_value={}))
    audit = AsyncMock()
    monkeypatch.setattr(api, "audit", audit)
    session = SimpleNamespace(scalars=AsyncMock(return_value=["stored"]), commit=AsyncMock())
    completed = datetime(2024, 2, 29, 12, tzinfo=UTC)
    await api.record_submission(
        42,
        api.Submission(
            reference="receipt",
            submitted_at=completed,
            evidence_ids=["stored"],
        ),
        principal("safety.manage"),
        session,
        settings(),
        None,
    )
    assert retained.retain_until == datetime(2028, 1, 1, tzinfo=UTC)
    assert retained.completed_submission_at == completed
    assert retained.submissions[0]["evidence_ids"] == ["stored"]
    audit.assert_awaited_once()
    for payload in (
        api.Submission(reference="receipt", submitted_at=completed),
        api.Submission(reference="other", submitted_at=completed, evidence_ids=["unknown"]),
    ):
        with pytest.raises(HTTPException):
            await api.record_submission(
                42, payload, principal("safety.manage"), session, settings(), None
            )
    assert len(retained.submissions) == 1


@pytest.mark.asyncio
async def test_retention_cannot_be_shortened_and_holds_are_audited(monkeypatch):
    retained = case(retain_until=datetime.now(UTC) + timedelta(days=400))
    monkeypatch.setattr(api, "load_case", AsyncMock(return_value=retained))
    monkeypatch.setattr(api, "case_payload", AsyncMock(return_value={}))
    audit = AsyncMock()
    monkeypatch.setattr(api, "audit", audit)
    session = SimpleNamespace(commit=AsyncMock())
    admin = principal("safety.manage")
    with pytest.raises(HTTPException):
        await api.extend_retention(
            42,
            api.Retention(
                retain_until=retained.retain_until - timedelta(days=1),
                reason="mistaken date",
            ),
            admin,
            session,
            settings(),
            None,
        )
    await api.place_hold(
        42,
        api.Hold(reason="Preservation request", reference="request-1"),
        admin,
        session,
        settings(),
        None,
    )
    hold_id = retained.holds[0]["id"]
    assert service.active_holds(retained)
    await api.release_hold(
        42, hold_id, api.Reason(reason="Request released"), admin, session, settings(), None
    )
    assert not service.active_holds(retained)
    assert audit.await_count == 2


@pytest.mark.asyncio
async def test_generic_report_capabilities_cannot_access_safety_case_or_evidence():
    session = SimpleNamespace(get=AsyncMock())
    general_admin = principal("reports.read", "reports.manage")
    with pytest.raises(HTTPException) as error:
        await api.get_case(42, general_admin, session, settings())
    assert error.value.status_code == 403
    with pytest.raises(HTTPException) as error:
        await api.download_evidence(42, "evidence-id", general_admin, session, settings(), None)
    assert error.value.status_code == 403
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_report_cannot_suspend_alleged_sender(monkeypatch):
    retained = case(status="pending")
    monkeypatch.setattr(api, "load_case", AsyncMock(return_value=retained))
    with pytest.raises(HTTPException) as error:
        await api.review_case(
            42,
            api.Review(
                decision="confirmed",
                reason="Matched disclosed evidence",
                suspend_account=True,
            ),
            principal("safety.manage", "users.manage"),
            None,
            settings(),
            None,
            None,
        )
    assert error.value.detail["code"] == "SAFETY_SUBJECT_UNVERIFIED"


@pytest.mark.asyncio
async def test_confirming_disclosed_copy_requires_and_records_source_attestation(monkeypatch):
    from app.safety import enforcement

    retained = case(status="pending", message_ref="8@home.test")
    session = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(api, "load_case", AsyncMock(return_value=retained))
    monkeypatch.setattr(api, "case_payload", AsyncMock(return_value={}))
    monkeypatch.setattr(api, "audit", AsyncMock())
    apply_review = AsyncMock()
    monkeypatch.setattr(enforcement, "apply_case_review", apply_review)
    finalize = AsyncMock()
    monkeypatch.setattr(enforcement, "finalize_case_review", finalize)
    admin = principal("safety.manage")
    with pytest.raises(HTTPException) as error:
        await api.review_case(
            42,
            api.Review(decision="confirmed", reason="Disclosed copy match"),
            admin,
            session,
            settings(),
            None,
            None,
        )
    assert error.value.detail["code"] == "SAFETY_SOURCE_VERIFICATION_REQUIRED"
    apply_review.assert_not_awaited()
    session.commit.assert_not_awaited()
    await api.review_case(
        42,
        api.Review(decision="confirmed", reason="Verified the source", source_verified=True),
        admin,
        session,
        settings(),
        None,
        None,
    )
    details = service.case_details(settings(), retained)
    assert details["provenance_verified"] is True
    assert details["source_verified_by"] == "1@home.test"
    assert b"source_verified_by" not in retained.details
    assert retained.status == "confirmed"
    apply_review.assert_awaited_once()
    session.commit.assert_awaited_once()
    finalize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("previously_reviewed", [True, False])
@pytest.mark.parametrize("disposed", [True, False])
async def test_dismissal_approval_only_applies_to_exact_reviewed_bytes(
    monkeypatch, previously_reviewed, disposed
):
    from app.media.photodna import PhotoDNAFinding, PhotoDNAMatchFlag

    now = datetime.now(UTC)
    retained = case(
        status="dismissed",
        closed_at=now,
        disposed_at=now if disposed else None,
        reviewed_at=now,
        reviewed_by="1@home.test",
        review_reason="Reviewed earlier bytes",
    )
    monkeypatch.setattr(service, "capture_report", AsyncMock(return_value=retained))
    preserve = AsyncMock()
    monkeypatch.setattr(service, "preserve_evidence", preserve)
    link = SimpleNamespace(snapshot=None)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value="old-id" if previously_reviewed else None),
        get=AsyncMock(return_value=link),
    )
    report = SimpleNamespace(
        id=42,
        source="photodna",
        category="csam",
        description=None,
        target_type="attachment",
        target_ref="8@home.test",
        message_ref=None,
        reporter_id=None,
        encryption_mode="plaintext",
        evidence={},
    )
    attachment = SimpleNamespace(
        id=8,
        origin_domain="home.test",
        report_id=None,
        filename="image.png",
        detected_content_type="image/png",
        content_type="image/png",
        object_key="original/8",
        staging_object_key=None,
        variants={},
        upload_expires_at=None,
    )
    await service.capture_finding(
        session,
        settings(),
        report,
        attachment,
        b"synthetic new bytes",
        PhotoDNAFinding("track", (PhotoDNAMatchFlag("Test", ("A1",), 0, "match"),)),
    )
    if previously_reviewed:
        assert retained.status == "dismissed"
        preserve.assert_not_awaited()
    else:
        assert retained.status == "pending"
        assert retained.closed_at is retained.disposed_at is retained.reviewed_at is None
        assert retained.reviewed_by is retained.review_reason is None
        assert (
            service.case_details(settings(), retained)["previous_reviews"][0]["decision"]
            == "dismissed"
        )
        assert link.snapshot
        preserve.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("disclosed", [False, True])
async def test_only_an_original_upload_match_promotes_a_user_allegation_to_automatic_review(
    monkeypatch, disclosed
):
    from app.media.photodna import PhotoDNAFinding, PhotoDNAMatchFlag

    retained = case(source="user", status="pending", message_ref="10@home.test")
    monkeypatch.setattr(service, "capture_report", AsyncMock(return_value=retained))
    preserve = AsyncMock()
    monkeypatch.setattr(service, "preserve_evidence", preserve)
    attachment = SimpleNamespace(
        id=8,
        origin_domain="home.test",
        report_id=42 if disclosed else None,
        filename="synthetic.png",
        detected_content_type="image/png",
        content_type="image/png",
        object_key="original/8",
        staging_object_key=None,
        variants={},
        upload_expires_at=None,
    )
    await service.capture_finding(
        None,
        settings(),
        SimpleNamespace(id=42),
        attachment,
        b"synthetic media",
        PhotoDNAFinding("track", (PhotoDNAMatchFlag("Test", ("A1",), 0, "match"),)),
    )
    assert retained.source == ("user" if disclosed else "photodna")
    details = service.case_details(settings(), retained)
    assert details["provenance_verified"] is (not disclosed)
    assert details["photodna"]
    assert preserve.await_args.kwargs["provenance"] == (
        "reporter_disclosed" if disclosed else "local_upload"
    )


@pytest.mark.asyncio
async def test_storage_outage_preserves_encrypted_buffer_until_verified_copy(monkeypatch):
    written = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        flush=AsyncMock(),
        add=written.append,
    )
    store = SimpleNamespace(put=AsyncMock(side_effect=storage.StorageError("unavailable")))
    monkeypatch.setattr(service, "EvidenceStorage", lambda _settings: store)
    evidence = await service.preserve_evidence(
        session,
        settings(),
        case(),
        "8@home.test",
        b"synthetic media",
        filename="image.png",
        content_type="image/png",
        provenance="local_upload",
    )
    assert len(written) == 1
    assert evidence.stored_at is not None
    assert b"synthetic media" not in evidence.pending_blob
    assert await service.read_evidence(settings(), evidence) == b"synthetic media"
    session.scalars = AsyncMock(return_value=[evidence])
    before = evidence.pending_blob
    assert await service.drain_buffered_evidence(session, settings()) == 0
    assert evidence.pending_blob == before
    store.put.side_effect = None
    assert await service.drain_buffered_evidence(session, settings()) == 1
    store.put.assert_awaited_with(evidence.object_key, b"synthetic media")
    assert evidence.pending_blob is None


@pytest.mark.asyncio
async def test_invalid_evidence_key_cannot_create_plaintext_outage_buffer(monkeypatch):
    written = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None), flush=AsyncMock(), add=written.append
    )
    store = SimpleNamespace(put=AsyncMock(side_effect=storage.StorageError("unavailable")))
    monkeypatch.setattr(service, "EvidenceStorage", lambda _settings: store)
    config = settings()
    config.safety_evidence_key_bytes = b"invalid"
    with pytest.raises(ValueError):
        await service.preserve_evidence(
            session,
            config,
            case(),
            None,
            b"synthetic media",
            filename="image.png",
            content_type="image/png",
            provenance="federated_origin",
        )
    assert written[0].stored_at is None
    assert written[0].pending_blob is None


@pytest.mark.asyncio
async def test_no_evidence_deletion_before_obligations(monkeypatch):
    storage_factory = Mock()
    monkeypatch.setattr(service, "EvidenceStorage", storage_factory)
    with pytest.raises(HTTPException):
        await service.dispose_evidence(None, settings(), case())
    storage_factory.assert_not_called()


@pytest.mark.asyncio
async def test_eligible_disposal_erases_snapshots_and_narratives_but_retains_digest(monkeypatch):
    now = datetime.now(UTC)
    retained = case(
        status="dismissed",
        closed_at=now,
        subject_ref="7@home.test",
        message_ref="8@home.test",
        attachment_ref="9@home.test",
        subject_snapshot=b"sealed subject",
        message_snapshot={"sealed": "old"},
        reviewed_by="1@home.test",
        review_reason="private review narrative",
        submissions=[{"reference": "receipt", "submitted_at": now.isoformat(), "notes": "private"}],
        holds=[
            {
                "id": "hold",
                "reason": "private",
                "reference": "private",
                "released_at": now.isoformat(),
            }
        ],
        retention_extensions=[{"retain_until": now.isoformat(), "reason": "private"}],
    )
    evidence = SimpleNamespace(
        object_key="evidence/42/stored", sha256="a" * 64, filename="private.png", deleted_at=None
    )
    report = SimpleNamespace(evidence={"content": "private"}, description="private")
    subject = SimpleNamespace(
        deleted_at=now,
        email="private@example.test",
        email_verified_at=now,
        display_name="private",
        bio="private",
        custom_status="private",
    )
    link = SimpleNamespace(report_id=42, snapshot=b"sealed")
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[evidence], [link]]),
        get=AsyncMock(side_effect=[report, subject]),
        scalar=AsyncMock(return_value=None),
    )
    store = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(service, "EvidenceStorage", lambda _settings: store)
    await service.dispose_evidence(session, settings(), retained)
    store.delete.assert_awaited_once_with("evidence/42/stored")
    assert evidence.deleted_at and evidence.sha256 == "a" * 64
    assert evidence.filename == "deleted-evidence"
    assert retained.subject_snapshot is retained.subject_ref is retained.message_ref is None
    assert retained.attachment_ref is retained.review_reason is retained.reviewed_by is None
    assert retained.message_snapshot == service.case_details(settings(), retained) == {}
    assert report.evidence == {} and report.description is None
    assert subject.email is subject.display_name is subject.bio is subject.custom_status is None
    assert "private" not in str(
        retained.submissions + retained.holds + retained.retention_extensions
    )
    assert retained.submissions[0]["reference"] == "receipt"
