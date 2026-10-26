"""Review behavior across independently detected files and retained encrypted bytes."""

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.db.bot_models import AbuseReport
from app.db.models import Attachment, Message
from app.media import tombstones
from app.safety import enforcement, service, storage
from app.safety.models import ChildSafetyCase, ChildSafetyEvidence, ChildSafetyReport


def config():
    return SimpleNamespace(
        domain="home.test",
        media_attachments_bucket="private",
        media_remote_cache_bucket="remote-cache",
        safety_evidence_key_bytes=b"k" * 32,
    )


def retained(case_id, **overrides):
    return ChildSafetyCase(
        **{
            "id": case_id,
            "source": "photodna",
            "status": "pending",
            "details": storage.encrypt_metadata(config(), {}, f"case:{case_id}"),
            "message_snapshot": {},
            **overrides,
        }
    )


def message():
    return Message(
        id=10,
        origin_domain="home.test",
        content="Original message",
        e2ee=None,
        embeds=[{"description": "Original embed"}],
        components=[],
        sticker_items=[],
        forward_snapshot=None,
        poll_result={"total_votes": 7},
        mention_user_refs=[],
        mention_role_refs=[],
        mention_everyone=False,
        flags=0,
        message_type=46,
    )


def attachment(attachment_id, **overrides):
    return Attachment(
        **{
            "id": attachment_id,
            "origin_domain": "home.test",
            "uploader_id": 7,
            "uploader_domain": "home.test",
            "message_id": 10,
            "message_domain": "home.test",
            "scan_status": "pending",
            "encryption_mode": "plaintext",
            "object_key": f"scanning/home.test/{attachment_id}",
            "filename": "synthetic.bin",
            "content_type": "application/octet-stream",
            "size": 3,
            **overrides,
        }
    )


def buffered(case_id, item, data):
    key = f"evidence/{case_id}/synthetic"
    return ChildSafetyEvidence(
        id=f"{case_id:032x}",
        case_id=case_id,
        attachment_ref=f"{item.id}@home.test",
        object_key=key,
        pending_blob=storage.encrypt(config(), data, key),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        stored_at=datetime.now(UTC),
        provenance="local_upload",
        filename="synthetic.bin",
        content_type="application/octet-stream",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_state", ["missing", "intact", "corrupted"])
async def test_remote_evidence_uses_only_a_verified_locally_cached_original(
    monkeypatch, cached_state
):
    item = attachment(1, origin_domain="remote.test", scan_status="clean")
    case = retained(21)
    cached = SimpleNamespace(
        object_key="remote.test/1/original/hash",
        scan_status="clean",
        size=3,
        content_sha256=hashlib.sha256(b"abc").hexdigest(),
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=["cache/original", "cache/thumb"]),
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=None if cached_state == "missing" else cached),
    )
    read = AsyncMock(return_value=b"bad" if cached_state == "corrupted" else b"abc")
    preserve = AsyncMock()
    monkeypatch.setattr(enforcement, "S3Storage", lambda _: SimpleNamespace(get=read))
    monkeypatch.setattr(enforcement, "preserve_evidence", preserve)
    if cached_state == "corrupted":
        with pytest.raises(storage.StorageError, match="integrity"):
            await enforcement.preserve_attachment(session, config(), case, item)
        preserve.assert_not_awaited()
        return
    await enforcement.preserve_attachment(session, config(), case, item)
    if cached_state == "missing":
        read.assert_not_awaited()
        preserve.assert_not_awaited()
    else:
        read.assert_awaited_once_with("remote-cache", cached.object_key, max_bytes=3)
        assert preserve.await_args.args[3:5] == ("1@remote.test", b"abc")
        assert preserve.await_args.kwargs["provenance"] == "federated_origin"


@pytest.mark.asyncio
async def test_confirmation_expires_every_cached_remote_variant_after_preservation(monkeypatch):
    item = attachment(1, origin_domain="remote.test", message_id=None, message_domain=None)
    case = retained(21, attachment_ref="1@remote.test")
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[], [item], [], [], ["cache/original", "cache/thumb"]]),
        execute=AsyncMock(),
        info={},
    )
    transitions = []

    async def preserve(*_args):
        transitions.append("preserved")

    async def discard(*_args):
        assert transitions == ["preserved"]
        transitions.append("discarded")
        item.deleted_at = datetime.now(UTC)

    monkeypatch.setattr(enforcement, "preserve_attachment", preserve)
    monkeypatch.setattr(enforcement, "discard_attachment", discard)
    monkeypatch.setattr(enforcement, "record_client_event", AsyncMock())
    monkeypatch.setattr(
        tombstones, "queue_terminal_attachment_tombstone", AsyncMock(return_value=set())
    )
    await enforcement.apply_case_review(session, config(), case, "confirmed", None, None)
    assert {item["key"] for item in case.removal_objects} == {"cache/original", "cache/thumb"}
    statement = session.execute.await_args.args[0]
    params = statement.compile().params
    assert "UPDATE remote_media_cache" in str(statement)
    assert params["origin_domain_1"] == "remote.test" and params["attachment_id_1"] == 1
    assert params["expires_at"] <= datetime.now(UTC)
    assert session.info["safety_remote_media_expired"] is True
    assert transitions == ["preserved", "discarded"]


@pytest.mark.asyncio
@pytest.mark.parametrize("authored_edit", [None, datetime(2026, 1, 2, tzinfo=UTC)])
async def test_two_prebind_findings_restore_only_after_both_dismissed_and_read_db_buffer(
    monkeypatch,
    authored_edit,
):
    original = message()
    original.edited_at = authored_edit
    attachments = [attachment(1), attachment(2)]
    cases = [retained(21), retained(22)]
    evidence = [
        buffered(case.id, item, b"abc") for case, item in zip(cases, attachments, strict=True)
    ]
    project = AsyncMock()
    monkeypatch.setattr(enforcement, "project_message", project)
    monkeypatch.setattr(enforcement, "case_for_attachment", AsyncMock(side_effect=cases))
    await enforcement.protect_bound_attachments(
        SimpleNamespace(scalars=AsyncMock(return_value=attachments)),
        config(),
        original,
        attachments,
    )
    assert original.safety_state == "pending_review"
    assert original.message_type == 0 and original.poll_result is None
    for case in cases:
        snapshot = service.case_details(config(), case)["message_snapshots"]["10@home.test"]
        assert snapshot["content"] == "Original message"
        assert snapshot["message_type"] == 46
        assert b"Original message" not in case.details

    put = AsyncMock()
    monkeypatch.setattr(enforcement, "S3Storage", lambda _settings: SimpleNamespace(put=put))
    for index, case in enumerate(cases):
        session = SimpleNamespace(
            scalars=AsyncMock(side_effect=[[evidence[index]], attachments]),
            get=AsyncMock(return_value=original),
            scalar=AsyncMock(return_value=cases[1].id if index == 0 else None),
        )
        await enforcement.apply_case_review(session, config(), case, "dismissed", None, None)
        case.status = "dismissed"
        assert put.await_args.args[2] == b"abc"
        assert put.await_args.args[1].startswith(f"reviewed/home.test/{attachments[index].id}/")
        if index == 0:
            assert original.safety_state == "pending_review"
            assert not any(call.args[-1] == "released" for call in project.await_args_list)
    assert original.safety_state is None
    assert original.content == "Original message"
    assert original.message_type == 46 and original.poll_result == {"total_votes": 7}
    assert original.edited_at == authored_edit
    assert project.await_args.args[-1] == "released"


@pytest.mark.asyncio
async def test_confirmed_prebind_case_cannot_be_downgraded_by_pending_case(monkeypatch):
    original = message()
    attachments = [attachment(1), attachment(2)]
    cases = [retained(21, status="confirmed"), retained(22)]
    monkeypatch.setattr(enforcement, "project_message", AsyncMock())
    monkeypatch.setattr(enforcement, "case_for_attachment", AsyncMock(side_effect=cases))
    await enforcement.protect_bound_attachments(
        SimpleNamespace(scalars=AsyncMock(return_value=attachments)),
        config(),
        original,
        attachments,
    )
    assert original.safety_state == "removed"
    assert original.content == enforcement.REMOVED_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("encryption_mode", ["plaintext", "e2ee"])
async def test_confirmation_preserves_and_revokes_shared_object_and_source_copies(
    monkeypatch, encryption_mode
):
    mode = {
        "encryption_mode": encryption_mode,
        "encryption_protocol": "kaede-file-v1" if encryption_mode == "e2ee" else None,
        "scan_status": "encrypted" if encryption_mode == "e2ee" else "clean",
        "message_id": None,
        "message_domain": None,
    }
    original = attachment(1, **mode, object_key="home.test/shared")
    alias = attachment(2, **mode, object_key=original.object_key)
    source_copy = attachment(
        3,
        **mode,
        object_key="home.test/copy",
        source_attachment_id=1,
        source_attachment_domain="home.test",
    )
    items = [original, alias, source_copy]
    objects = {original.object_key: b"abc", source_copy.object_key: b"abc"}

    class Objects:
        async def get(self, _bucket, key, **_kwargs):
            return objects[key]

        async def put(self, _bucket, key, data, _content_type):
            objects[key] = data

    monkeypatch.setattr(enforcement, "S3Storage", lambda _settings: Objects())
    monkeypatch.setattr(storage, "S3Storage", lambda _settings: Objects())
    terminal = AsyncMock(return_value=set())
    monkeypatch.setattr(tombstones, "queue_terminal_attachment_tombstone", terminal)
    monkeypatch.setattr(enforcement, "record_client_event", AsyncMock())
    captured = []
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[], [original], [alias, source_copy], []]),
        scalar=AsyncMock(return_value=None),
        add=captured.append,
        flush=AsyncMock(),
        info={},
    )
    await enforcement.apply_case_review(
        session, config(), retained(21, attachment_ref="1@home.test"), "confirmed", None, None
    )
    assert len(captured) == 3
    assert all(isinstance(row, ChildSafetyEvidence) and row.stored_at for row in captured)
    assert all(item.deleted_at for item in items)
    assert {item.scan_status for item in items} == {
        "encrypted" if encryption_mode == "e2ee" else "quarantined"
    }
    assert terminal.await_count == 3
    for row in captured:
        assert b"abc" not in objects[row.object_key]
        assert await service.read_evidence(config(), row) == b"abc"
    # The root finalizer removes ordinary objects only after the verdict commits.
    assert objects[original.object_key] == b"abc"


@pytest.mark.asyncio
async def test_direct_confirmation_rejects_unverified_disclosed_message_link():
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]), get=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await enforcement.apply_case_review(
            session,
            config(),
            retained(21, message_ref="10@home.test"),
            "confirmed",
            None,
            None,
        )
    assert error.value.detail["code"] == "SAFETY_SOURCE_VERIFICATION_REQUIRED"
    session.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_intake_seals_content_before_scrubbing_ordinary_report(monkeypatch):
    report = AbuseReport(
        id=21,
        source="user",
        category="csam",
        target_type="message",
        target_ref="10@home.test",
        message_ref="10@home.test",
        reporter_id=None,
        encryption_mode="plaintext",
        description="private description",
        evidence={"content": "private plaintext", "context": ["private context"]},
    )
    original = message()
    original.author_id = 7
    original.author_domain = "home.test"
    captured = []
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[original, None, None]),
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
        add=captured.append,
        flush=AsyncMock(),
    )
    await enforcement.intake_report(session, config(), report)
    case = next(row for row in captured if isinstance(row, ChildSafetyCase))
    link = next(row for row in captured if isinstance(row, ChildSafetyReport))
    assert report.evidence == {} and report.description is None
    assert b"private plaintext" not in case.details + link.snapshot
    snapshot = storage.decrypt_metadata(config(), link.snapshot, "report:21")
    assert snapshot["evidence"]["content"] == "private plaintext"
    assert snapshot["description"] == "private description"
    assert "sealed" in case.message_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["photodna", "user"])
async def test_legacy_import_retains_metadata_without_inventing_bytes_or_provenance(source):
    report = AbuseReport(
        id=21,
        source=source,
        category="illegal_content",
        target_type="attachment",
        target_ref="1@home.test",
        reporter_id=None,
        encryption_mode="plaintext",
        description="historical description",
        evidence={"photodna": {"provider": "microsoft_photodna"}},
    )
    captured = []
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[report]),
        scalar=AsyncMock(return_value=None),
        get=AsyncMock(return_value=None),
        add=captured.append,
        flush=AsyncMock(),
    )
    assert await service.import_legacy_reports(session, config()) == 1
    case = next(row for row in captured if isinstance(row, ChildSafetyCase))
    assert case.source == "photodna" and case.status == "pending"
    details = service.case_details(config(), case)
    assert details["legacy_bytes_unavailable"] is True
    assert details["provenance_verified"] is False
    assert not any(isinstance(row, ChildSafetyEvidence) for row in captured)
    assert report.evidence == {} and report.description is None
    assert report.category == ("illegal_content" if source == "photodna" else "csam")
