from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminPrincipal, require_admin
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import InstanceAuditEvent
from app.safety.models import (
    ChildSafetyCase,
    ChildSafetyEvidence,
    ChildSafetyReport,
    SafetyClientEvent,
)
from app.safety.service import (
    active_holds,
    add_calendar_year,
    can_dispose,
    case_details,
    client_event_payload,
    dispose_evidence,
    new_hold_id,
    read_evidence,
)
from app.safety.storage import decrypt_metadata, encrypt_metadata


def private_response(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/api/v1", tags=["child safety"], dependencies=[Depends(private_response)]
)
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AdminDep = Annotated[AdminPrincipal, Depends(require_admin)]
SnowflakeDep = Annotated[SnowflakeGenerator, Depends(get_snowflake)]


class Reason(UnambiguousInputModel):
    reason: str = Field(min_length=3, max_length=2000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("provide a reason")
        return value


class Review(Reason):
    decision: Literal["confirmed", "dismissed"]
    suspend_account: bool = False
    source_verified: bool = False


class Submission(UnambiguousInputModel):
    reference: str = Field(min_length=1, max_length=300)
    submitted_at: datetime
    notes: str | None = Field(default=None, max_length=2000)
    evidence_ids: list[str] | None = Field(default=None, max_length=100)

    @field_validator("reference")
    @classmethod
    def meaningful_reference(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provide the completed submission reference")
        return value.strip()

    @field_validator("submitted_at")
    @classmethod
    def completed_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value > datetime.now(UTC):
            raise ValueError("completion time must be timezone-aware and in the past")
        return value.astimezone(UTC)


class Retention(Reason):
    retain_until: datetime

    @field_validator("retain_until")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("retention deadline must include a timezone")
        return value.astimezone(UTC)


class Hold(Reason):
    reference: str | None = Field(default=None, max_length=300)


def actor_ref(principal: AdminPrincipal) -> str:
    return f"{principal.user.id}@{principal.user.origin_domain}"


async def audit(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    principal: AdminPrincipal,
    case: ChildSafetyCase,
    action: str,
    *,
    reason: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    session.add(
        InstanceAuditEvent(
            id=await snowflake.mint(),
            actor_id=principal.user.id,
            actor_domain=principal.user.origin_domain,
            actor_kind="admin",
            action=f"admin.safety.{action}",
            target_type="child_safety_case",
            target_ref=str(case.id),
            # The shared administration audit is readable without safety access.
            # Detailed reasons remain in the restricted case record.
            reason="Restricted child-safety case action" if reason else None,
            detail=detail or {},
        )
    )


async def load_case(
    session: AsyncSession, case_id: int, *, mutable: bool = False
) -> ChildSafetyCase:
    case = await session.get(ChildSafetyCase, case_id, with_for_update=mutable)
    if case is None:
        raise HTTPException(status_code=404, detail={"code": "SAFETY_CASE_NOT_FOUND"})
    if mutable and case.disposed_at is not None:
        raise HTTPException(status_code=409, detail={"code": "SAFETY_EVIDENCE_DISPOSED"})
    return case


async def case_payload(
    session: AsyncSession, settings: Settings, case: ChildSafetyCase
) -> dict[str, Any]:
    details = case_details(settings, case)
    subject = (
        decrypt_metadata(settings, case.subject_snapshot, f"subject:{case.id}")
        if case.subject_snapshot is not None
        else None
    )
    links = list(
        await session.scalars(
            select(ChildSafetyReport.report_id)
            .where(ChildSafetyReport.case_id == case.id)
            .order_by(ChildSafetyReport.report_id)
        )
    )
    evidence = list(
        await session.scalars(
            select(ChildSafetyEvidence)
            .where(
                ChildSafetyEvidence.case_id == case.id,
                ChildSafetyEvidence.deleted_at.is_(None),
            )
            .order_by(ChildSafetyEvidence.id)
        )
    )
    refs = list(dict.fromkeys([case.attachment_ref] if case.attachment_ref else []))
    for item in evidence:
        if item.attachment_ref and item.attachment_ref not in refs:
            refs.append(item.attachment_ref)
    verified = details.get("provenance_verified") is True
    return {
        "id": str(case.id),
        "report_id": str(links[0]) if links else str(case.id),
        "report_ids": [str(value) for value in links],
        "status": case.status,
        "removal_pending": case.status == "confirmed" and case.removal_completed_at is None,
        "removal_completed_at": case.removal_completed_at.isoformat()
        if case.removal_completed_at
        else None,
        "source": case.source,
        "reason": case.review_reason or details.get("description") or "",
        "created_at": case.created_at.isoformat(),
        "message_ref": case.message_ref,
        "attachment_refs": refs,
        "subject": subject,
        "details": details,
        "provenance_verified": verified,
        "can_suspend": verified
        and subject is not None
        and subject.get("is_local") is True
        and case.closed_at is None,
        "evidence": [
            {
                "id": item.id,
                "filename": item.filename,
                "content_type": item.content_type,
                "size": item.size,
                "sha256": item.sha256,
                "preserved_at": item.stored_at.isoformat() if item.stored_at else None,
                "provenance": item.provenance,
            }
            for item in evidence
        ],
        "submissions": case.submissions,
        "holds": case.holds,
        "retention_extensions": case.retention_extensions,
        "retain_until": case.retain_until.isoformat() if case.retain_until else None,
        "closed_at": case.closed_at.isoformat() if case.closed_at else None,
        "disposed_at": case.disposed_at.isoformat() if case.disposed_at else None,
        "can_delete_evidence": can_dispose(case),
    }


@router.get("/administration/child-safety")
async def list_cases(
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    principal.require("safety.read")
    cases = list(
        await session.scalars(
            select(ChildSafetyCase)
            .order_by(ChildSafetyCase.created_at.desc(), ChildSafetyCase.id.desc())
            .limit(500)
        )
    )
    return {"cases": [await case_payload(session, settings, case) for case in cases]}


@router.get("/administration/child-safety/{case_id}")
async def get_case(
    case_id: int,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
) -> dict[str, Any]:
    principal.require("safety.read")
    return await case_payload(session, settings, await load_case(session, case_id))


@router.post("/administration/child-safety/{case_id}/review")
async def review_case(
    case_id: int,
    payload: Review,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    if case.closed_at is not None or case.status != "pending":
        raise HTTPException(status_code=409, detail={"code": "SAFETY_REVIEW_ALREADY_COMPLETED"})
    details = case_details(settings, case)
    if (
        payload.decision == "confirmed"
        and case.message_ref
        and not details.get("provenance_verified")
    ):
        if not payload.source_verified:
            raise HTTPException(
                status_code=409, detail={"code": "SAFETY_SOURCE_VERIFICATION_REQUIRED"}
            )
        details["source_verified_by"] = actor_ref(principal)
        details["source_verified_at"] = datetime.now(UTC).isoformat()
        details["provenance_verified"] = True
        case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    if payload.suspend_account:
        subject = (
            decrypt_metadata(settings, case.subject_snapshot, f"subject:{case.id}")
            if case.subject_snapshot
            else None
        )
        if (
            payload.decision != "confirmed"
            or not case_details(settings, case).get("provenance_verified")
            or not subject
            or not subject.get("is_local")
        ):
            raise HTTPException(status_code=409, detail={"code": "SAFETY_SUBJECT_UNVERIFIED"})
        principal.require("users.manage")
    from app.safety.enforcement import apply_case_review, finalize_case_review

    await apply_case_review(
        session,
        settings,
        case,
        payload.decision,
        principal.user,
        redis,
        suspend_account=payload.suspend_account,
    )
    case.status = payload.decision
    case.reviewed_at = datetime.now(UTC)
    case.reviewed_by = actor_ref(principal)
    case.review_reason = payload.reason
    await audit(
        session,
        snowflake,
        principal,
        case,
        "review",
        reason=payload.reason,
        detail={
            "decision": payload.decision,
            "suspend_account": payload.suspend_account,
            "source_verified": payload.source_verified,
        },
    )
    await session.commit()
    await finalize_case_review(session, settings, case, redis)
    return await case_payload(session, settings, case)


@router.post("/administration/child-safety/{case_id}/submissions")
async def record_submission(
    case_id: int,
    payload: Submission,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    if case.status != "confirmed":
        raise HTTPException(status_code=409, detail={"code": "SAFETY_REVIEW_REQUIRED"})
    if any(item["reference"] == payload.reference for item in case.submissions):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_SUBMISSION_ALREADY_RECORDED"})
    available = set(
        await session.scalars(
            select(ChildSafetyEvidence.id).where(
                ChildSafetyEvidence.case_id == case.id,
                ChildSafetyEvidence.stored_at.is_not(None),
                ChildSafetyEvidence.deleted_at.is_(None),
            )
        )
    )
    selected = set(payload.evidence_ids) if payload.evidence_ids is not None else available
    if not selected <= available:
        raise HTTPException(status_code=422, detail={"code": "SAFETY_EVIDENCE_NOT_PRESERVED"})
    case.submissions = [
        *case.submissions,
        {
            "reference": payload.reference,
            "submitted_at": payload.submitted_at.isoformat(),
            "notes": payload.notes,
            "evidence_ids": sorted(selected),
            "recorded_at": datetime.now(UTC).isoformat(),
            "recorded_by": actor_ref(principal),
        },
    ]
    case.completed_submission_at = max(
        case.completed_submission_at or payload.submitted_at, payload.submitted_at
    )
    minimum = add_calendar_year(case.completed_submission_at)
    case.retain_until = max(case.retain_until or minimum, minimum)
    await audit(
        session,
        snowflake,
        principal,
        case,
        "submission",
        detail={"submitted_at": payload.submitted_at.isoformat()},
    )
    await session.commit()
    return await case_payload(session, settings, case)


@router.post("/administration/child-safety/{case_id}/retention")
async def extend_retention(
    case_id: int,
    payload: Retention,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    minimum = max(datetime.now(UTC), case.retain_until or datetime.now(UTC))
    if payload.retain_until <= minimum:
        raise HTTPException(status_code=422, detail={"code": "SAFETY_RETENTION_MUST_EXTEND"})
    case.retain_until = payload.retain_until
    case.retention_extensions = [
        *case.retention_extensions,
        {
            "retain_until": payload.retain_until.isoformat(),
            "reason": payload.reason,
            "recorded_at": datetime.now(UTC).isoformat(),
            "recorded_by": actor_ref(principal),
        },
    ]
    await audit(
        session,
        snowflake,
        principal,
        case,
        "retention",
        reason=payload.reason,
        detail={"retain_until": payload.retain_until.isoformat()},
    )
    await session.commit()
    return await case_payload(session, settings, case)


@router.post("/administration/child-safety/{case_id}/holds")
async def place_hold(
    case_id: int,
    payload: Hold,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    hold_id = new_hold_id()
    case.holds = [
        *case.holds,
        {
            "id": hold_id,
            "reason": payload.reason,
            "reference": payload.reference,
            "created_at": datetime.now(UTC).isoformat(),
            "created_by": actor_ref(principal),
            "released_at": None,
        },
    ]
    await audit(
        session,
        snowflake,
        principal,
        case,
        "hold",
        reason=payload.reason,
        detail={"hold_id": hold_id},
    )
    await session.commit()
    return await case_payload(session, settings, case)


@router.post("/administration/child-safety/{case_id}/holds/{hold_id}/release")
async def release_hold(
    case_id: int,
    hold_id: str,
    payload: Reason,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    if not any(item["id"] == hold_id for item in active_holds(case)):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_HOLD_NOT_ACTIVE"})
    case.holds = [
        {
            **item,
            "released_at": datetime.now(UTC).isoformat(),
            "released_by": actor_ref(principal),
            "release_reason": payload.reason,
        }
        if item["id"] == hold_id
        else item
        for item in case.holds
    ]
    await audit(
        session,
        snowflake,
        principal,
        case,
        "hold_release",
        reason=payload.reason,
        detail={"hold_id": hold_id},
    )
    await session.commit()
    return await case_payload(session, settings, case)


@router.post("/administration/child-safety/{case_id}/close")
async def close_case(
    case_id: int,
    payload: Reason,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    case = await load_case(session, case_id, mutable=True)
    if case.status == "pending" or (
        case.status == "confirmed" and case.completed_submission_at is None
    ):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_OBLIGATIONS_PENDING"})
    if case.closed_at is not None:
        raise HTTPException(status_code=409, detail={"code": "SAFETY_CASE_ALREADY_CLOSED"})
    case.closed_at = datetime.now(UTC)
    details = case_details(settings, case)
    details["closure_reason"] = payload.reason
    case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    await audit(session, snowflake, principal, case, "close", reason=payload.reason)
    await session.commit()
    return await case_payload(session, settings, case)


@router.delete("/administration/child-safety/{case_id}/evidence")
async def delete_evidence(
    case_id: int,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> dict[str, Any]:
    principal.require("safety.manage")
    principal.require("safety.evidence")
    case = await load_case(session, case_id, mutable=True)
    await dispose_evidence(session, settings, case)
    await audit(session, snowflake, principal, case, "dispose")
    await session.commit()
    return await case_payload(session, settings, case)


@router.get("/administration/child-safety/{case_id}/evidence/{evidence_id}")
async def download_evidence(
    case_id: int,
    evidence_id: str,
    principal: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    snowflake: SnowflakeDep,
) -> StreamingResponse:
    principal.require("safety.evidence")
    case = await load_case(session, case_id, mutable=True)
    evidence = await session.get(ChildSafetyEvidence, evidence_id)
    if (
        evidence is None
        or evidence.case_id != case.id
        or evidence.deleted_at is not None
        or evidence.stored_at is None
    ):
        raise HTTPException(status_code=404, detail={"code": "SAFETY_EVIDENCE_NOT_FOUND"})
    data = await read_evidence(settings, evidence)
    await audit(
        session, snowflake, principal, case, "evidence_read", detail={"evidence_id": evidence.id}
    )
    await session.commit()
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": f'attachment; filename="evidence-{evidence.id}"',
            "Content-Length": str(len(data)),
        },
    )


@router.get("/safety/client-state")
async def client_state(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: SessionDep,
    response: Response,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=200),
) -> dict[str, Any]:
    rows = list(
        await session.scalars(
            select(SafetyClientEvent)
            .where(
                SafetyClientEvent.user_id == auth.user.id,
                SafetyClientEvent.user_domain == auth.user.origin_domain,
                SafetyClientEvent.sequence > after,
            )
            .order_by(SafetyClientEvent.sequence)
            .limit(limit + 1)
        )
    )
    selected = rows[:limit]
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [client_event_payload(row) for row in selected],
        "next_cursor": str(selected[-1].sequence if selected else after),
        "has_more": len(rows) > limit,
    }
