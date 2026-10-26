from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import user_topic
from app.chat.postcommit import queue_postcommit_dispatch
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import AbuseReport
from app.db.models import Attachment, Message, User
from app.media.storage import StorageError
from app.safety.models import (
    ChildSafetyCase,
    ChildSafetyEvidence,
    ChildSafetyReport,
    SafetyClientEvent,
)
from app.safety.storage import (
    EvidenceStorage,
    decrypt,
    decrypt_metadata,
    encrypt,
    encrypt_metadata,
)

if TYPE_CHECKING:
    from app.media.photodna import PhotoDNAFinding


def add_calendar_year(value: datetime) -> datetime:
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, day=28)


def account_snapshot(user: User) -> dict[str, Any]:
    return {
        "ref": f"{user.id}@{user.origin_domain}",
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email if user.is_local else None,
        "email_verified_at": user.email_verified_at.isoformat()
        if user.is_local and user.email_verified_at
        else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "is_local": user.is_local,
        "identity_source": "local_account" if user.is_local else "federated_origin_assertion",
        "captured_at": datetime.now(UTC).isoformat(),
    }


def report_snapshot(report: AbuseReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "source": report.source,
        "category": report.category,
        "description": report.description,
        "target_type": report.target_type,
        "target_ref": report.target_ref,
        "message_ref": report.message_ref,
        "reporter_ref": f"{report.reporter_id}@{report.reporter_domain}"
        if report.reporter_id is not None
        else None,
        "encryption_mode": report.encryption_mode,
        "evidence": report.evidence,
    }


def snapshot_message(settings: Settings, case_id: int, message: Message) -> dict[str, Any]:
    snapshot = {
        name: getattr(message, name, None)
        for name in (
            "content",
            "message_type",
            "e2ee",
            "embeds",
            "components",
            "forward_snapshot",
            "poll_result",
            "forward_ref",
            "flags",
            "mention_user_refs",
            "mention_role_refs",
            "mention_everyone",
            "sticker_items",
            "interaction_metadata",
            "message_reference",
            "tts",
        )
        if hasattr(message, name)
    }
    snapshot["edited_at"] = message.edited_at.isoformat() if message.edited_at else None
    return {
        "sealed": base64.b64encode(
            encrypt_metadata(settings, snapshot, f"message:{case_id}")
        ).decode("ascii")
    }


def case_details(settings: Settings, case: ChildSafetyCase) -> dict[str, Any]:
    return decrypt_metadata(settings, case.details, f"case:{case.id}")


async def capture_report(
    session: AsyncSession,
    settings: Settings,
    report: AbuseReport,
    *,
    subject: User | None = None,
    message: Message | None = None,
) -> ChildSafetyCase:
    # A common target lock makes two simultaneous reports join the same case.
    target = report.message_ref or report.target_ref
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"safety-case:{target}", 0)))
    )
    linked = await session.get(ChildSafetyReport, report.id)
    if linked is not None:
        case = await session.get(ChildSafetyCase, linked.case_id, with_for_update=True)
        if case is None:
            raise RuntimeError("child-safety report lost its case")
        if case.disposed_at is not None:
            return case
        if message is not None and not case.message_snapshot:
            case.message_snapshot = snapshot_message(settings, case.id, message)
        if subject is not None and case.subject_snapshot is None:
            case.subject_snapshot = encrypt_metadata(
                settings, account_snapshot(subject), f"subject:{case.id}"
            )
        return case
    subject_ref = (
        f"{subject.id}@{subject.origin_domain}"
        if subject is not None
        else report.evidence.get("author_ref") or report.evidence.get("uploader_ref")
    )
    attachment_ref = report.evidence.get("attachment_ref")
    if attachment_ref is None and report.target_type == "attachment":
        attachment_ref = report.target_ref
    condition = (
        ChildSafetyCase.message_ref == report.message_ref
        if report.message_ref
        else ChildSafetyCase.attachment_ref == report.target_ref
    )
    case = await session.scalar(
        select(ChildSafetyCase)
        .where(
            condition,
            ChildSafetyCase.disposed_at.is_(None),
            ChildSafetyCase.status != "dismissed",
        )
        .order_by(ChildSafetyCase.id)
        .limit(1)
        .with_for_update()
    )
    now = datetime.now(UTC)
    if case is None:
        case = ChildSafetyCase(
            id=report.id,
            source=report.source,
            status="pending",
            subject_ref=subject_ref if isinstance(subject_ref, str) else None,
            message_ref=report.message_ref,
            attachment_ref=attachment_ref if isinstance(attachment_ref, str) else None,
            details=encrypt_metadata(settings, report_snapshot(report), f"case:{report.id}"),
            message_snapshot=snapshot_message(settings, report.id, message)
            if message is not None
            else {},
            submissions=[],
            holds=[],
            retention_extensions=[],
            created_at=now,
            updated_at=now,
        )
        session.add(case)
        await session.flush()
    if subject is None and case.subject_ref:
        try:
            subject = await session.get(User, EntityRef(case.subject_ref).resolve(settings.domain))
        except ValueError:
            subject = None
    if subject is not None and case.subject_snapshot is None:
        case.subject_snapshot = encrypt_metadata(
            settings, account_snapshot(subject), f"subject:{case.id}"
        )
    if message is not None and not case.message_snapshot:
        case.message_snapshot = snapshot_message(settings, case.id, message)
    if message is not None:
        details = case_details(settings, case)
        verified = (
            message.e2ee is None
            and message.author_domain == settings.domain
            and report.encryption_mode == "plaintext"
        )
        details["provenance_verified"] = verified
        details["evidence_provenance"] = (
            "local_upload"
            if verified
            else "reporter_disclosed"
            if message.e2ee is not None
            else "federated_origin"
        )
        case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    session.add(
        ChildSafetyReport(
            report_id=report.id,
            case_id=case.id,
            snapshot=encrypt_metadata(settings, report_snapshot(report), f"report:{report.id}"),
        )
    )
    await session.flush()
    return case


async def preserve_evidence(
    session: AsyncSession,
    settings: Settings,
    case: ChildSafetyCase,
    attachment_ref: str | None,
    data: bytes,
    *,
    filename: str,
    content_type: str,
    provenance: str,
) -> ChildSafetyEvidence:
    if case.disposed_at is not None or case.closed_at is not None:
        raise HTTPException(status_code=409, detail={"code": "SAFETY_CASE_CLOSED"})
    digest = hashlib.sha256(data).hexdigest()
    existing = await session.scalar(
        select(ChildSafetyEvidence)
        .where(
            ChildSafetyEvidence.case_id == case.id,
            ChildSafetyEvidence.attachment_ref == attachment_ref,
            ChildSafetyEvidence.sha256 == digest,
            ChildSafetyEvidence.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if existing is not None and existing.stored_at is not None:
        await read_evidence(settings, existing)
        return existing
    evidence = existing
    if evidence is None:
        # Deterministic identity also recovers an encrypted PUT after a DB rollback.
        evidence_id = hashlib.sha256(f"{case.id}:{attachment_ref}:{digest}".encode()).hexdigest()[
            :32
        ]
        evidence = ChildSafetyEvidence(
            id=evidence_id,
            case_id=case.id,
            attachment_ref=attachment_ref,
            provenance=provenance,
            object_key=f"evidence/{case.id}/{evidence_id}",
            sha256=digest,
            size=len(data),
            filename=filename[:255],
            content_type=content_type[:255],
        )
        session.add(evidence)
        await session.flush()
    try:
        await EvidenceStorage(settings).put(evidence.object_key, data)
    except StorageError:
        # Preserve the exact finding in the caller's DB transaction even while
        # object storage is unavailable. Never buffer unencrypted material.
        evidence.pending_blob = encrypt(settings, data, evidence.object_key)
    else:
        evidence.pending_blob = None
    evidence.stored_at = datetime.now(UTC)
    await session.flush()
    return evidence


async def read_evidence(settings: Settings, evidence: ChildSafetyEvidence) -> bytes:
    if evidence.deleted_at is not None or evidence.stored_at is None:
        raise StorageError("preserved evidence is unavailable")
    if evidence.pending_blob is not None:
        data = decrypt(settings, evidence.pending_blob, evidence.object_key)
        if len(data) != evidence.size or hashlib.sha256(data).hexdigest() != evidence.sha256:
            raise StorageError("buffered evidence integrity check failed")
        return data
    return await EvidenceStorage(settings).get(
        evidence.object_key, size=evidence.size, sha256=evidence.sha256
    )


async def drain_buffered_evidence(
    session: AsyncSession, settings: Settings, *, limit: int = 20
) -> int:
    rows = await session.scalars(
        select(ChildSafetyEvidence)
        .where(
            ChildSafetyEvidence.pending_blob.is_not(None),
            ChildSafetyEvidence.deleted_at.is_(None),
        )
        .order_by(ChildSafetyEvidence.stored_at, ChildSafetyEvidence.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    copied = 0
    for evidence in rows:
        data = await read_evidence(settings, evidence)
        try:
            await EvidenceStorage(settings).put(evidence.object_key, data)
        except StorageError:
            continue
        # Clearing the buffer commits only after the replacement is verified.
        evidence.pending_blob = None
        copied += 1
    await session.flush()
    return copied


async def capture_finding(
    session: AsyncSession,
    settings: Settings,
    report: AbuseReport,
    attachment: Attachment,
    data: bytes,
    finding: PhotoDNAFinding,
    *,
    provenance: str = "local_upload",
    subject: User | None = None,
    message: Message | None = None,
) -> ChildSafetyCase:
    from app.media.photodna import photodna_report_values

    case = await capture_report(session, settings, report, subject=subject, message=message)
    if attachment.report_id is not None:
        provenance = "reporter_disclosed"
    attachment_ref = f"{attachment.id}@{attachment.origin_domain}"
    digest = hashlib.sha256(data).hexdigest()
    details = case_details(settings, case)
    if case.status == "dismissed":
        previously_reviewed = await session.scalar(
            select(ChildSafetyEvidence.id)
            .where(
                ChildSafetyEvidence.case_id == case.id,
                ChildSafetyEvidence.attachment_ref == attachment_ref,
                ChildSafetyEvidence.sha256 == digest,
            )
            .limit(1)
        )
        if previously_reviewed is not None:
            return case
        details["previous_reviews"] = [
            *details.get("previous_reviews", []),
            {
                "decision": case.status,
                "reviewed_at": case.reviewed_at.isoformat() if case.reviewed_at else None,
                "reviewed_by": case.reviewed_by,
                "reason": case.review_reason,
                "disposed_at": case.disposed_at.isoformat() if case.disposed_at else None,
            },
        ]
        case.status = "pending"
        case.closed_at = None
        case.disposed_at = None
        case.reviewed_at = None
        case.reviewed_by = None
        case.review_reason = None
        case.message_ref = report.message_ref
        case.attachment_ref = report.evidence.get("attachment_ref") or (
            report.target_ref if report.target_type == "attachment" else None
        )
        case.subject_ref = (
            f"{subject.id}@{subject.origin_domain}"
            if subject is not None
            else f"{message.author_id}@{message.author_domain}"
            if message is not None
            else report.evidence.get("author_ref") or report.evidence.get("uploader_ref")
        )
        if subject is None and case.subject_ref:
            try:
                subject = await session.get(
                    User, EntityRef(case.subject_ref).resolve(settings.domain)
                )
            except ValueError:
                subject = None
        if subject is not None:
            case.subject_snapshot = encrypt_metadata(
                settings, account_snapshot(subject), f"subject:{case.id}"
            )
        if message is not None:
            case.message_snapshot = snapshot_message(settings, case.id, message)
        # A retained digest approval applies only to the bytes actually reviewed.
        link = await session.get(ChildSafetyReport, report.id)
        if link is not None:
            link.snapshot = encrypt_metadata(
                settings, report_snapshot(report), f"report:{report.id}"
            )
    await preserve_evidence(
        session,
        settings,
        case,
        attachment_ref,
        data,
        filename=attachment.filename,
        content_type=attachment.detected_content_type or attachment.content_type,
        provenance=provenance,
    )
    from app.safety.removal import remember_local_objects

    remember_local_objects(settings, case, attachment)
    details["photodna"] = photodna_report_values(
        report_id=report.id,
        attachment_ref=attachment_ref,
        finding=finding,
        detected_content_type=attachment.detected_content_type or attachment.content_type,
        content_sha256=digest,
    )["evidence"]
    details["provenance"] = provenance
    details["evidence_provenance"] = provenance
    details["provenance_verified"] = provenance == "local_upload"
    # A match on a reporter's separate upload does not authenticate the alleged
    # original. Keep that allegation private until staff verify its source.
    if provenance != "reporter_disclosed":
        case.source = "photodna"
    case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    return case


async def snapshot_subject_before_deletion(
    session: AsyncSession, settings: Settings, user: User
) -> None:
    from app.db.models import Session

    rows = list(
        await session.scalars(
            select(ChildSafetyCase)
            .where(
                ChildSafetyCase.subject_ref == f"{user.id}@{user.origin_domain}",
                ChildSafetyCase.disposed_at.is_(None),
            )
            .with_for_update()
        )
    )
    retained = [(case, case_details(settings, case)) for case in rows]
    if not any(
        case.subject_snapshot is None or "account_at_deletion" not in details
        for case, details in retained
    ):
        return
    current_account = account_snapshot(user)
    deletion_snapshot: dict[str, Any] = {"account": current_account}
    if any("account_at_deletion" not in details for _, details in retained):
        sessions = await session.execute(
            select(
                Session.device_name,
                Session.user_agent,
                Session.ip_address,
                Session.created_at,
                Session.last_used_at,
                Session.expires_at,
                Session.absolute_expires_at,
                Session.revoked_at,
            )
            .where(Session.user_id == user.id, Session.user_domain == user.origin_domain)
            .order_by(Session.created_at, Session.last_used_at)
        )
        deletion_snapshot["sessions"] = [
            {
                name: value.isoformat() if isinstance(value, datetime) else value
                for name, value in row.items()
            }
            for row in sessions.mappings()
        ]
    for case, details in retained:
        if case.subject_snapshot is None:
            case.subject_snapshot = encrypt_metadata(
                settings, current_account, f"subject:{case.id}"
            )
        if "account_at_deletion" not in details:
            details["account_at_deletion"] = deletion_snapshot
            case.details = encrypt_metadata(settings, details, f"case:{case.id}")


async def import_legacy_reports(
    session: AsyncSession, settings: Settings, *, limit: int = 50
) -> int:
    reports = list(
        await session.scalars(
            select(AbuseReport)
            .where(
                or_(
                    AbuseReport.source == "photodna",
                    AbuseReport.evidence["photodna"]["provider"].astext == "microsoft_photodna",
                ),
                ~exists(
                    select(ChildSafetyReport.report_id).where(
                        ChildSafetyReport.report_id == AbuseReport.id
                    )
                ),
            )
            .order_by(AbuseReport.id)
            .limit(limit)
        )
    )
    imported = 0
    for report in reports:
        # Match the normal intake lock order: target before any report mutation.
        target = report.message_ref or report.target_ref
        await session.scalar(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"safety-case:{target}", 0)))
        )
        if await session.get(ChildSafetyReport, report.id) is not None:
            continue
        case = await capture_report(session, settings, report)
        details = case_details(settings, case)
        details["legacy_reports"] = [
            *details.get("legacy_reports", []),
            {
                "report_id": str(report.id),
                "imported_at": datetime.now(UTC).isoformat(),
                "evidence_availability": "metadata_only",
                "provenance_verified": False,
            },
        ]
        if case.id == report.id:
            case.source = "photodna"
            case.status = "pending"
            details["provenance_verified"] = False
            details["legacy_imported"] = True
            details["legacy_bytes_unavailable"] = True
            details["description"] = (
                "Imported historical PhotoDNA finding. "
                "The original matched bytes were not retained."
            )
        case.details = encrypt_metadata(settings, details, f"case:{case.id}")
        if report.source == "user":
            report.category = "csam"
        report.evidence = {}
        report.description = None
        imported += 1
    await session.flush()
    return imported


def active_holds(case: ChildSafetyCase) -> list[dict[str, Any]]:
    return [hold for hold in case.holds if not hold.get("released_at")]


def can_dispose(case: ChildSafetyCase, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return bool(
        case.disposed_at is None
        and case.closed_at is not None
        and case.status in {"confirmed", "dismissed"}
        and (case.status == "dismissed" or case.completed_submission_at is not None)
        and (case.status == "dismissed" or case.removal_completed_at is not None)
        and not active_holds(case)
        and (case.retain_until is None or case.retain_until <= now)
    )


async def dispose_evidence(
    session: AsyncSession, settings: Settings, case: ChildSafetyCase
) -> None:
    if not can_dispose(case):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_RETENTION_ACTIVE"})
    evidence = list(
        await session.scalars(
            select(ChildSafetyEvidence)
            .where(
                ChildSafetyEvidence.case_id == case.id,
                ChildSafetyEvidence.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    storage = EvidenceStorage(settings)
    for item in evidence:
        await storage.delete(item.object_key)
        item.pending_blob = None
        item.deleted_at = datetime.now(UTC)
        item.filename = "deleted-evidence"
    links = list(
        await session.scalars(select(ChildSafetyReport).where(ChildSafetyReport.case_id == case.id))
    )
    for link in links:
        link.snapshot = encrypt_metadata(settings, {}, f"report:{link.report_id}")
        report = await session.get(AbuseReport, link.report_id, with_for_update=True)
        if report is not None:
            report.evidence = {}
            report.description = None
    case.subject_snapshot = None
    case.removal_objects = []
    case.message_snapshot = {}
    case.details = encrypt_metadata(settings, {}, f"case:{case.id}")
    case.reviewed_by = None
    case.review_reason = None
    case.submissions = [
        {
            key: item[key]
            for key in ("id", "reference", "submitted_at", "evidence_ids")
            if key in item
        }
        for item in case.submissions
    ]
    case.holds = [
        {key: item[key] for key in ("id", "created_at", "released_at") if key in item}
        for item in case.holds
    ]
    case.retention_extensions = [
        {key: item[key] for key in ("retain_until", "recorded_at") if key in item}
        for item in case.retention_extensions
    ]
    case.disposed_at = datetime.now(UTC)
    if case.subject_ref:
        try:
            user = await session.get(User, EntityRef(case.subject_ref).resolve(settings.domain))
        except ValueError:
            user = None
        retained = await session.scalar(
            select(ChildSafetyCase.id)
            .where(
                ChildSafetyCase.subject_ref == case.subject_ref,
                ChildSafetyCase.id != case.id,
                ChildSafetyCase.disposed_at.is_(None),
            )
            .limit(1)
        )
        if user is not None and user.deleted_at is not None and retained is None:
            user.email = None
            user.email_verified_at = None
            user.display_name = None
            user.bio = None
            user.custom_status = None
    case.subject_ref = None
    case.message_ref = None
    case.attachment_ref = None


async def record_client_event(
    session: AsyncSession,
    snowflake: SnowflakeGenerator | None = None,
    user_refs: list[tuple[int, str]] | None = None,
    *,
    message_refs: list[str],
    attachment_refs: list[str],
    state: str,
    reason: str,
) -> list[dict[str, Any]]:
    del snowflake
    recipients = sorted(set(user_refs or []), key=lambda item: (item[1], item[0]))
    for user_id, user_domain in recipients:
        # Allocate sequence numbers only after serializing this recipient's commits.
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"safety-client:{user_domain}:{user_id}", 0)
                )
            )
        )
    rows = [
        SafetyClientEvent(
            user_id=user_id,
            user_domain=user_domain,
            message_refs=list(dict.fromkeys(message_refs)),
            attachment_refs=list(dict.fromkeys(attachment_refs)),
            state=state,
            reason=reason[:500],
        )
        for user_id, user_domain in recipients
    ]
    session.add_all(rows)
    await session.flush()
    for row in rows:
        queue_postcommit_dispatch(
            session,
            user_topic(row.user_domain, row.user_id),
            "CONTENT_SAFETY_UPDATE",
            client_event_payload(row),
        )
    return [client_event_payload(row) for row in rows]


def client_event_payload(row: SafetyClientEvent) -> dict[str, Any]:
    return {
        "sequence": str(row.sequence),
        "message_refs": row.message_refs,
        "attachment_refs": row.attachment_refs,
        "state": row.state,
        "reason": row.reason,
    }


def new_hold_id() -> str:
    return uuid.uuid4().hex
