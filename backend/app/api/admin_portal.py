from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import AdminPrincipal, require_admin
from app.admin.report_enforcement import (
    publish_message_purge,
    publish_remote_user_guild_removals,
    purge_author_messages,
    remove_remote_user_from_local_guilds,
)
from app.api.admin import (
    affected_peer_domains,
    effective_blocked_destinations,
    lock_block_policy,
    lock_destination_policy,
    reconcile_policy_change,
    wake_policy_reconciliation,
)
from app.api.channels import require_channel_permissions
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.media import redirect_to_object
from app.auth.tokens import AccessTokenStore
from app.bots.developer_projection import commit_developer_application_mutation
from app.bots.e2ee import revoke_bot_e2ee_access
from app.bots.installations import transition_application_installations, usable_guild_installation
from app.bots.runtime_control import active_dm_runtime_target_domains
from app.bots.target_discovery import (
    queue_application_target_snapshots_for_refs,
    wake_application_target_deliveries,
)
from app.chat.channel_access import load_channel_access
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.payloads import user_payload
from app.core.model_validation import UnambiguousInputModel
from app.core.permission_contract import required_permissions
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.bot_models import (
    AbuseReport,
    BotApplication,
    BotInstallation,
    InstanceAdminGrant,
    InstanceAuditEvent,
)
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Instance,
    InstanceBlock,
    InstanceUserRestriction,
    MediaTombstoneSource,
    Message,
    Session,
    User,
)
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.media.schemas import AssetCommitRequest, UploadTicketRequest
from app.media.service import create_upload_ticket, finalize_attachment, ticket_payload
from app.tasks import media_process

router = APIRouter(prefix="/api/v1", tags=["instance administration"])
federation_router = APIRouter(tags=["report federation"])
ADMIN_ROLES = {
    "owner",
    "administrator",
    "trust_safety",
    "bot_reviewer",
    "operations",
    "auditor",
}
REPORT_CREATE_LIMIT = ClientRateLimit("abuse-report-create", 10, 3600)
REPORT_EVIDENCE_UPLOAD_LIMIT = ClientRateLimit("abuse-report-evidence-upload", 10, 3600)
REPORT_CATEGORIES = {
    "spam",
    "harassment",
    "hate",
    "sexual_content",
    "violence",
    "self_harm",
    "impersonation",
    "privacy",
    "malware",
    "illegal_content",
    "other",
}
ACCOUNT_ACTION_SECONDS: dict[str, int | None] = {
    "suspend_24h": 86_400,
    "suspend_7d": 604_800,
    "suspend_30d": 2_592_000,
    "ban_permanent": None,
    # Accepted for clients deployed before permanent suspension was correctly
    # presented as an instance ban.
    "suspend_permanent": None,
}
MESSAGE_ACTION_SECONDS: dict[str, int | None] = {
    "delete_1h": 3_600,
    "delete_24h": 86_400,
    "delete_7d": 604_800,
    "delete_30d": 2_592_000,
    "delete_all": None,
}


class AdminGrantCreate(UnambiguousInputModel):
    user_ref: EntityRef
    role: Literal[
        "administrator",
        "trust_safety",
        "bot_reviewer",
        "operations",
        "auditor",
    ]


class UserStatePatch(UnambiguousInputModel):
    disabled: bool | None = None
    age_assurance_state: Literal["unknown", "adult", "minor"] | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_state_change(self) -> UserStatePatch:
        if not self.model_fields_set & {"disabled", "age_assurance_state"}:
            raise ValueError("at least one user state field is required")
        if any(
            field in self.model_fields_set and getattr(self, field) is None
            for field in ("disabled", "age_assurance_state")
        ):
            raise ValueError("user state fields cannot be null")
        return self


class ApplicationStatePatch(UnambiguousInputModel):
    status: Literal["active", "suspended"]
    reason: str | None = Field(default=None, max_length=500)


class ApplicationDirectoryApprovalPatch(UnambiguousInputModel):
    approved: bool | None = None
    collections: list[Literal["featured", "staff-picks", "new-and-noteworthy"]] | None = Field(
        default=None, max_length=3
    )
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def visible_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if len(cleaned) < 3:
            raise ValueError("directory review reason must contain at least three characters")
        return cleaned

    @model_validator(mode="after")
    def require_directory_change(self) -> ApplicationDirectoryApprovalPatch:
        if not self.model_fields_set & {"approved", "collections"}:
            raise ValueError("approved or collections is required")
        if self.approved is None and "approved" in self.model_fields_set:
            raise ValueError("approved cannot be null")
        if self.collections is None and "collections" in self.model_fields_set:
            raise ValueError("collections cannot be null")
        if self.collections is not None and len(set(self.collections)) != len(self.collections):
            raise ValueError("collections must be unique")
        return self


class ReportCreate(UnambiguousInputModel):
    target_type: Literal[
        "message", "attachment", "user", "bot", "application", "guild", "instance", "invite"
    ]
    target_ref: str = Field(min_length=1, max_length=320)
    category: Literal[
        "spam",
        "harassment",
        "hate",
        "sexual_content",
        "violence",
        "self_harm",
        "impersonation",
        "privacy",
        "malware",
        "illegal_content",
        "other",
    ]
    description: str | None = Field(default=None, max_length=2000)
    message_ref: EntityRef | None = None
    focused_attachment_ref: EntityRef | None = None
    disclosed_content: str | None = Field(default=None, max_length=4000)
    disclosure_acknowledged: bool = False

    @field_validator("disclosed_content")
    @classmethod
    def _meaningful_disclosure(cls, value: str | None) -> str | None:
        # Exact empty text is valid evidence for an E2EE attachment-only
        # message. It remains distinct from None (decryption unavailable).
        # Non-empty whitespace is never meaningful reporter evidence.
        if value is not None and value != "" and not value.strip():
            raise ValueError("disclosed content must contain a non-whitespace character")
        return value

    @model_validator(mode="after")
    def _focused_attachment_requires_message_target(self) -> ReportCreate:
        if self.focused_attachment_ref is not None and self.target_type != "message":
            raise ValueError("focused attachment context requires a message report")
        return self


class FederatedReportCreate(ReportCreate):
    reporter_ref: EntityRef
    source_report_ref: EntityRef


class ReportPatch(UnambiguousInputModel):
    status: Literal[
        "submitted",
        "triaged",
        "in_review",
        "awaiting_remote",
        "needs_information",
        "action_taken",
        "closed_no_action",
        "duplicate",
        "reopened",
    ]
    resolution: str | None = Field(default=None, max_length=2000)


class ReportActionCreate(UnambiguousInputModel):
    account_action: Literal[
        "none",
        "suspend_24h",
        "suspend_7d",
        "suspend_30d",
        "ban_permanent",
        "suspend_permanent",
    ] = "none"
    message_action: Literal[
        "none",
        "delete_reported",
        "delete_1h",
        "delete_24h",
        "delete_7d",
        "delete_30d",
        "delete_all",
    ] = "none"
    reason: str = Field(min_length=3, max_length=500)

    @field_validator("reason")
    @classmethod
    def _meaningful_reason(cls, value: str) -> str:
        if len(value.strip()) < 3:
            raise ValueError("reason must contain at least three non-whitespace characters")
        return value.strip()

    @model_validator(mode="after")
    def _requires_enforcement(self) -> ReportActionCreate:
        if self.account_action == "none" and self.message_action == "none":
            raise ValueError("at least one enforcement action is required")
        return self


class ReportAttachmentEvidenceTicketCreate(UploadTicketRequest):
    disclosure_acknowledged: bool


class ReportAttachmentEvidenceCommit(AssetCommitRequest):
    disclosure_acknowledged: bool


class InstanceBlockCreate(UnambiguousInputModel):
    domain: str = Field(min_length=1, max_length=253)
    level: Literal["silence", "suspend"]
    include_subdomains: bool = False
    reason: str | None = Field(default=None, max_length=500)


async def audit(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    principal: AdminPrincipal,
    action: str,
    target_type: str,
    target_ref: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> None:
    session.add(
        InstanceAuditEvent(
            id=await snowflake.mint(),
            actor_id=principal.user.id,
            actor_domain=principal.user.origin_domain,
            actor_kind="admin",
            action=action,
            target_type=target_type,
            target_ref=target_ref,
            detail=metadata or {},
        )
    )


def grant_payload(grant: InstanceAdminGrant, user: User) -> dict[str, object]:
    return {
        "id": str(grant.id),
        "role": grant.role,
        "user": user_payload(user),
        "generation": str(grant.generation),
        "created_at": grant.created_at.isoformat(),
        "expires_at": grant.expires_at.isoformat() if grant.expires_at else None,
    }


def reporter_report_payload(report: AbuseReport) -> dict[str, object]:
    return {
        "id": str(report.id),
        "target_type": report.target_type,
        "target_ref": report.target_ref,
        "category": report.category,
        "description": report.description,
        "status": report.status,
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
    }


def report_identity_payload(user: User | None) -> dict[str, object] | None:
    if user is None:
        return None
    return {
        "ref": f"{user.id}@{user.origin_domain}",
        "username": user.username,
        "display_name": user.display_name,
        "handle": f"{user.username}@{user.origin_domain}",
        "profile_resolved": user.profile_resolved,
    }


def report_payload(
    report: AbuseReport,
    *,
    reporter_user: User | None = None,
    subject_user: User | None = None,
) -> dict[str, object]:
    return {
        "id": str(report.id),
        "source": report.source,
        "severity": "critical" if isinstance(report.evidence.get("photodna"), dict) else None,
        "reporter_ref": (
            f"{report.reporter_id}@{report.reporter_domain}"
            if report.reporter_id is not None and report.reporter_domain is not None
            else None
        ),
        "reporter_user": report_identity_payload(reporter_user),
        "subject_user": report_identity_payload(subject_user),
        "target_type": report.target_type,
        "target_ref": report.target_ref,
        "category": report.category,
        "description": report.description,
        "message_ref": report.message_ref,
        "evidence": report.evidence,
        "encryption_mode": report.encryption_mode,
        "status": report.status,
        "assigned_admin_ref": (
            f"{report.assigned_admin_id}@{report.assigned_admin_domain}"
            if report.assigned_admin_id is not None and report.assigned_admin_domain
            else None
        ),
        "resolution": report.resolution,
        "created_at": report.created_at.isoformat(),
        "updated_at": report.updated_at.isoformat(),
        "resolved_at": report.resolved_at.isoformat() if report.resolved_at else None,
    }


def report_identity_refs(report: AbuseReport, local_domain: str) -> set[tuple[int, str]]:
    refs: set[tuple[int, str]] = set()
    if report.reporter_id is not None and report.reporter_domain is not None:
        refs.add((report.reporter_id, report.reporter_domain))
    subject_ref = report_subject_ref(report)
    if subject_ref is not None:
        with suppress(ValueError):
            refs.add(EntityRef(subject_ref).resolve(local_domain))
    return refs


async def report_identity_lookup(
    session: AsyncSession,
    reports: list[AbuseReport],
    local_domain: str,
) -> dict[tuple[int, str], User]:
    refs: set[tuple[int, str]] = set()
    for report in reports:
        refs.update(report_identity_refs(report, local_domain))
    if not refs:
        return {}
    users = list(
        await session.scalars(select(User).where(tuple_(User.id, User.origin_domain).in_(refs)))
    )
    return {(user.id, user.origin_domain): user for user in users}


def report_payload_with_lookup(
    report: AbuseReport,
    users: dict[tuple[int, str], User],
    local_domain: str,
) -> dict[str, object]:
    reporter_user = (
        users.get((report.reporter_id, report.reporter_domain))
        if report.reporter_id is not None and report.reporter_domain is not None
        else None
    )
    subject_ref = report_subject_ref(report)
    subject_user: User | None = None
    if subject_ref is not None:
        with suppress(ValueError):
            subject_user = users.get(EntityRef(subject_ref).resolve(local_domain))
    return report_payload(report, reporter_user=reporter_user, subject_user=subject_user)


def report_subject_ref(report: AbuseReport) -> str | None:
    if report.target_type == "user":
        return report.target_ref
    key = "uploader_ref" if report.target_type == "attachment" else "author_ref"
    value = report.evidence.get(key)
    return value if isinstance(value, str) and value else None


def report_message_evidence(
    message: Message,
    *,
    disclosed_content: str | None,
    disclosure_acknowledged: bool,
    attachments: list[Attachment] | None = None,
    focused_attachment: Attachment | None = None,
) -> tuple[dict[str, object], str]:
    """Build report evidence without ever accepting or persisting room keys."""

    evidence: dict[str, object] = {
        "author_ref": f"{message.author_id}@{message.author_domain}",
        "channel_ref": f"{message.channel_id}@{message.channel_domain}",
        "created_at": message.created_at.isoformat(),
    }
    attachment_rows = attachments or []
    if attachment_rows:
        evidence["attachments"] = [
            {
                "attachment_ref": f"{item.id}@{item.origin_domain}",
                "uploader_ref": f"{item.uploader_id}@{item.uploader_domain}",
                "filename": item.filename,
                "content_type": item.detected_content_type or item.content_type,
                "size": item.size,
                "encryption_mode": item.encryption_mode,
            }
            for item in attachment_rows
        ]
    if focused_attachment is not None:
        evidence.update(
            {
                "attachment_ref": (f"{focused_attachment.id}@{focused_attachment.origin_domain}"),
                "uploader_ref": (
                    f"{focused_attachment.uploader_id}@{focused_attachment.uploader_domain}"
                ),
                "attachment_created_at": focused_attachment.created_at.isoformat(),
                "filename": focused_attachment.filename,
                "content_type": (
                    focused_attachment.detected_content_type or focused_attachment.content_type
                ),
                "size": focused_attachment.size,
                "attachment_encryption_mode": focused_attachment.encryption_mode,
            }
        )
    if message.e2ee is None:
        if disclosed_content is not None or disclosure_acknowledged:
            raise HTTPException(
                status_code=422,
                detail={"code": "REPORT_DISCLOSURE_UNEXPECTED"},
            )
        evidence["content"] = message.content
        return evidence, "plaintext"
    if disclosed_content is None or not disclosure_acknowledged:
        raise HTTPException(
            status_code=422,
            detail={"code": "E2EE_REPORT_DISCLOSURE_REQUIRED"},
        )
    canonical_envelope = json.dumps(
        message.e2ee,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    evidence.update(
        {
            "content": disclosed_content,
            "ciphertext_sha256": hashlib.sha256(canonical_envelope).hexdigest(),
            "disclosure": {
                "source": "reporter_client_decrypted",
                "reporter_acknowledged": True,
                "server_verified": False,
            },
        }
    )
    return evidence, "e2ee_user_disclosed"


def report_attachment_evidence(
    message: Message,
    attachment: Attachment,
) -> tuple[dict[str, object], str]:
    """Build server-verified attachment metadata without retaining media bytes or keys."""

    evidence: dict[str, object] = {
        "author_ref": f"{message.author_id}@{message.author_domain}",
        "channel_ref": f"{message.channel_id}@{message.channel_domain}",
        "message_ref": f"{message.id}@{message.origin_domain}",
        "created_at": message.created_at.isoformat(),
        "attachment_ref": f"{attachment.id}@{attachment.origin_domain}",
        "uploader_ref": f"{attachment.uploader_id}@{attachment.uploader_domain}",
        "attachment_created_at": attachment.created_at.isoformat(),
        "filename": attachment.filename,
        "content_type": attachment.detected_content_type or attachment.content_type,
        "size": attachment.size,
        "attachment_encryption_mode": attachment.encryption_mode,
    }
    if message.e2ee is None:
        evidence["content"] = message.content
    encrypted = message.e2ee is not None or attachment.encryption_mode == "e2ee"
    return evidence, "e2ee_metadata" if encrypted else "plaintext"


@router.get("/administration/@me")
async def administration_identity(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
) -> dict[str, object]:
    return {
        "user": user_payload(principal.user),
        "roles": sorted(principal.roles),
        "capabilities": sorted(principal.capabilities),
    }


@router.get("/administration/overview")
async def administration_overview(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, int]:
    principal.require("admin.read")
    counts: dict[str, int] = {}
    for name, model in (
        ("local_users", User),
        ("known_instances", Instance),
        ("applications", BotApplication),
        ("active_installations", BotInstallation),
        ("open_reports", AbuseReport),
        ("blocked_instances", InstanceBlock),
    ):
        statement = select(func.count()).select_from(model)
        if name == "local_users":
            statement = statement.where(User.is_local.is_(True))
        elif name == "active_installations":
            statement = statement.where(usable_guild_installation())
        elif name == "open_reports":
            statement = statement.where(
                AbuseReport.status.not_in(("action_taken", "closed_no_action", "duplicate")),
                AbuseReport.evidence["report_authority"].astext.is_(None),
            )
        counts[name] = int(await session.scalar(statement) or 0)
    return counts


@router.get("/administration/operators")
async def list_operators(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    principal.require("admin.read")
    rows = (
        await session.execute(
            select(InstanceAdminGrant, User)
            .join(
                User,
                (User.id == InstanceAdminGrant.user_id)
                & (User.origin_domain == InstanceAdminGrant.user_domain),
            )
            .where(InstanceAdminGrant.revoked_at.is_(None))
            .order_by(InstanceAdminGrant.role, func.lower(User.username))
        )
    ).all()
    return [grant_payload(grant, user) for grant, user in rows]


@router.post("/administration/operators", status_code=201)
async def add_operator(
    payload: AdminGrantCreate,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if "owner" not in principal.roles:
        raise HTTPException(status_code=403, detail={"code": "OWNER_REQUIRED"})
    user_id, user_domain = payload.user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain))
    if user is None or not user.is_local or user.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "LOCAL_USER_NOT_FOUND"})
    existing = await session.scalar(
        select(InstanceAdminGrant).where(
            InstanceAdminGrant.user_id == user.id,
            InstanceAdminGrant.user_domain == user.origin_domain,
            InstanceAdminGrant.role == payload.role,
            InstanceAdminGrant.revoked_at.is_(None),
        )
    )
    if existing is not None:
        return grant_payload(existing, user)
    grant = InstanceAdminGrant(
        id=await snowflake.mint(),
        user_id=user.id,
        user_domain=user.origin_domain,
        user_is_local=True,
        role=payload.role,
        granted_by_id=principal.user.id,
        granted_by_domain=principal.user.origin_domain,
    )
    session.add(grant)
    await audit(
        session,
        snowflake,
        principal,
        "admin.operator.grant",
        "user",
        f"{user.id}@{user.origin_domain}",
        metadata={"role": payload.role},
    )
    await session.commit()
    return grant_payload(grant, user)


@router.delete("/administration/operators/{grant_id}", status_code=204)
async def revoke_operator(
    grant_id: int,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
) -> Response:
    if "owner" not in principal.roles:
        raise HTTPException(status_code=403, detail={"code": "OWNER_REQUIRED"})
    grant = await session.get(InstanceAdminGrant, grant_id, with_for_update=True)
    if grant is None or grant.revoked_at is not None:
        raise HTTPException(status_code=404, detail={"code": "ADMIN_GRANT_NOT_FOUND"})
    if grant.role == "owner":
        raise HTTPException(status_code=409, detail={"code": "OWNER_CLI_MANAGED"})
    grant.revoked_at = datetime.now(UTC)
    grant.generation += 1
    await audit(
        session,
        snowflake,
        principal,
        "admin.operator.revoke",
        "user",
        f"{grant.user_id}@{grant.user_domain}",
        metadata={"role": grant.role},
    )
    await session.commit()
    return Response(status_code=204)


@router.get("/administration/users")
async def list_users(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    query: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, object]]:
    principal.require("admin.read")
    statement = select(User).where(User.is_local.is_(True))
    if query:
        statement = statement.where(func.lower(User.username).contains(query.lower()))
    users = list(await session.scalars(statement.order_by(User.id.desc()).limit(limit)))
    return [
        user_payload(user)
        | {
            "disabled_at": user.disabled_at.isoformat() if user.disabled_at else None,
            "suspended_until": (user.suspended_until.isoformat() if user.suspended_until else None),
            "age_assurance_state": getattr(user, "age_assurance_state", "unknown"),
        }
        for user in users
    ]


@router.patch("/administration/users/{user_ref}")
async def patch_user_state(
    user_ref: EntityRef,
    payload: UserStatePatch,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require("users.manage")
    user_id, user_domain = user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain), with_for_update=True)
    if user is None or not user.is_local:
        raise HTTPException(status_code=404, detail={"code": "LOCAL_USER_NOT_FOUND"})
    if user.id == principal.user.id and payload.disabled is True:
        raise HTTPException(status_code=409, detail={"code": "CANNOT_DISABLE_SELF"})
    if payload.age_assurance_state is not None and user.account_type != "human":
        raise HTTPException(status_code=400, detail={"code": "HUMAN_USER_REQUIRED"})
    target_ref = f"{user.id}@{user.origin_domain}"
    if payload.disabled is not None:
        user.disabled_at = datetime.now(UTC) if payload.disabled else None
        user.suspended_until = None
        await audit(
            session,
            snowflake,
            principal,
            "admin.user.disable" if payload.disabled else "admin.user.enable",
            "user",
            target_ref,
            metadata={"reason": payload.reason},
        )
    if payload.age_assurance_state is not None:
        previous_age_state = getattr(user, "age_assurance_state", "unknown")
        user.age_assurance_state = payload.age_assurance_state
        await audit(
            session,
            snowflake,
            principal,
            "admin.user.age_assurance.update",
            "user",
            target_ref,
            metadata={
                "old_state": previous_age_state,
                "new_state": payload.age_assurance_state,
                "reason": payload.reason,
            },
        )
    await session.commit()
    return user_payload(user) | {
        "disabled_at": user.disabled_at.isoformat() if user.disabled_at else None,
        "suspended_until": (user.suspended_until.isoformat() if user.suspended_until else None),
        "age_assurance_state": getattr(user, "age_assurance_state", "unknown"),
    }


def _administration_application_payload(
    application: BotApplication,
    settings: Settings,
) -> dict[str, object]:
    return {
        "ref": f"{application.id}@{application.origin_domain}",
        "name": application.name,
        "status": application.status,
        "directory_enabled": application.directory_enabled,
        "directory_approved": application.directory_approved,
        "directory_collections": list(application.directory_collections),
        "team_ref": f"{application.team_id}@{application.team_domain}",
        "state_authority": application.origin_domain,
        "can_manage_state": application.origin_domain == settings.domain,
        "created_at": application.created_at.isoformat(),
        "updated_at": application.updated_at.isoformat(),
    }


@router.patch("/administration/applications/{application_ref}/directory")
async def patch_application_directory_approval(
    application_ref: EntityRef,
    payload: ApplicationDirectoryApprovalPatch,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require("bots.manage")
    app_id, app_domain = application_ref.resolve(settings.domain)
    app = await session.get(BotApplication, (app_id, app_domain), with_for_update=True)
    if app is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    if app.origin_domain != settings.domain:
        raise HTTPException(status_code=409, detail={"code": "APPLICATION_HOME_INSTANCE_REQUIRED"})
    if payload.approved is True:
        from app.api.application_directory import directory_readiness_errors

        readiness_errors = await directory_readiness_errors(session, app)
        if readiness_errors:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "APPLICATION_DIRECTORY_NOT_READY",
                    "missing": readiness_errors,
                },
            )
    if payload.approved is not None:
        app.directory_approved = payload.approved
    if payload.collections is not None:
        app.directory_collections = cast(list[str], payload.collections)
    action = (
        "admin.application.directory.approve"
        if payload.approved is True
        else "admin.application.directory.revoke"
        if payload.approved is False
        else "admin.application.directory.collections.update"
    )
    await audit(
        session,
        snowflake,
        principal,
        action,
        "application",
        f"{app.id}@{app.origin_domain}",
        metadata={
            "reason": payload.reason,
            "collections": list(app.directory_collections),
        },
    )
    await commit_developer_application_mutation(session, settings, app)
    return _administration_application_payload(app, settings)


@router.get("/administration/applications")
async def administration_applications(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    principal.require("admin.read")
    rows = list(
        await session.scalars(
            select(BotApplication).order_by(BotApplication.updated_at.desc()).limit(500)
        )
    )
    return [_administration_application_payload(app, settings) for app in rows]


@router.patch("/administration/applications/{application_ref}")
async def patch_application_state(
    application_ref: EntityRef,
    payload: ApplicationStatePatch,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require("bots.manage")
    app_id, app_domain = application_ref.resolve(settings.domain)
    app = await session.get(BotApplication, (app_id, app_domain), with_for_update=True)
    if app is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    if app.origin_domain != settings.domain:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_HOME_INSTANCE_REQUIRED"},
        )
    previous_status = app.status
    runtime_target_domains = (
        await active_dm_runtime_target_domains(session, settings, app)
        if app.origin_domain == settings.domain
        and payload.status == "suspended"
        and previous_status != "suspended"
        else set()
    )
    paused_channels = (
        await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            application_ref=(app.id, app.origin_domain),
        )
        if payload.status == "suspended" and app.status != "suspended"
        else []
    )
    app.status = payload.status
    app.revocation_generation += 1
    changed_installations = await transition_application_installations(
        session,
        app,
        previous_status=previous_status,
        next_status=payload.status,
    )
    target_destinations = (
        await queue_application_target_snapshots_for_refs(
            session,
            settings,
            {(app.id, app.origin_domain)},
        )
        if changed_installations
        else set()
    )
    await audit(
        session,
        snowflake,
        principal,
        f"admin.application.{payload.status}",
        "application",
        f"{app.id}@{app.origin_domain}",
        metadata={"reason": payload.reason},
    )
    if app.origin_domain == settings.domain:
        await commit_developer_application_mutation(
            session,
            settings,
            app,
            runtime_target_domains=runtime_target_domains,
        )
    else:
        await session.commit()
    await wake_application_target_deliveries(target_destinations)
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    return {"ref": f"{app.id}@{app.origin_domain}", "status": app.status}


@router.post("/reports", status_code=201)
async def create_report(
    payload: ReportCreate,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if not auth.user.is_local or auth.user.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "LOCAL_HUMAN_ACCOUNT_REQUIRED"})
    await enforce_keyed_rate_limit(
        redis,
        response,
        REPORT_CREATE_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    message_context_target = payload.target_type in {"message", "attachment"}
    if message_context_target and payload.message_ref is None:
        raise HTTPException(status_code=422, detail={"code": "REPORT_MESSAGE_REF_REQUIRED"})
    if not message_context_target and payload.message_ref is not None:
        raise HTTPException(status_code=422, detail={"code": "REPORT_MESSAGE_REF_UNEXPECTED"})
    if payload.target_type != "message" and payload.focused_attachment_ref is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_FOCUSED_ATTACHMENT_UNEXPECTED"},
        )
    if payload.target_type != "message" and (
        payload.disclosed_content is not None or payload.disclosure_acknowledged
    ):
        raise HTTPException(status_code=422, detail={"code": "REPORT_DISCLOSURE_UNEXPECTED"})
    evidence: dict[str, object] = {}
    message_ref: str | None = None
    report_encryption_mode = "plaintext"
    report_authority = settings.domain
    if payload.message_ref is not None:
        message_id, message_domain = payload.message_ref.resolve(settings.domain)
        message = await session.get(Message, (message_id, message_domain))
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        access = await load_channel_access(
            session,
            settings,
            auth.user,
            EntityRef(f"{message.channel_id}@{message.channel_domain}"),
        )
        await require_channel_permissions(
            session,
            redis,
            access,
            auth.user,
            required_permissions("message.list"),
        )
        message_ref = f"{message.id}@{message.origin_domain}"
        report_authority = message.channel_domain
        if payload.target_type == "message":
            if payload.target_ref != message_ref:
                raise HTTPException(status_code=422, detail={"code": "REPORT_TARGET_MISMATCH"})
            attachments = list(
                await session.scalars(
                    select(Attachment)
                    .where(
                        Attachment.message_id == message.id,
                        Attachment.message_domain == message.origin_domain,
                        Attachment.purpose == "attachment",
                        Attachment.deleted_at.is_(None),
                    )
                    .order_by(Attachment.created_at, Attachment.origin_domain, Attachment.id)
                )
            )
            focused_attachment: Attachment | None = None
            if payload.focused_attachment_ref is not None:
                focused_id, focused_domain = payload.focused_attachment_ref.resolve(settings.domain)
                focused_attachment = next(
                    (
                        item
                        for item in attachments
                        if (item.id, item.origin_domain) == (focused_id, focused_domain)
                    ),
                    None,
                )
                if focused_attachment is None:
                    raise HTTPException(
                        status_code=404,
                        detail={"code": "ATTACHMENT_NOT_FOUND"},
                    )
            evidence, report_encryption_mode = report_message_evidence(
                message,
                disclosed_content=payload.disclosed_content,
                disclosure_acknowledged=payload.disclosure_acknowledged,
                attachments=attachments,
                focused_attachment=focused_attachment,
            )
        else:
            try:
                attachment_id, attachment_domain = EntityRef(payload.target_ref).resolve(
                    settings.domain
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "REPORT_TARGET_INVALID"},
                ) from exc
            attachment_ref = f"{attachment_id}@{attachment_domain}"
            if payload.target_ref != attachment_ref:
                raise HTTPException(status_code=422, detail={"code": "REPORT_TARGET_MISMATCH"})
            attachment = await session.get(Attachment, (attachment_id, attachment_domain))
            if (
                attachment is None
                or attachment.deleted_at is not None
                or attachment.purpose != "attachment"
                or (attachment.message_id, attachment.message_domain)
                != (message.id, message.origin_domain)
            ):
                raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
            evidence, report_encryption_mode = report_attachment_evidence(message, attachment)
    report_id = await snowflake.mint()
    report_status = "submitted"
    if message_ref is not None and report_authority != settings.domain:
        outbound = FederatedReportCreate(
            **payload.model_dump(),
            reporter_ref=EntityRef(f"{auth.user.id}@{auth.user.origin_domain}"),
            source_report_ref=EntityRef(f"{report_id}@{settings.domain}"),
        )
        try:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                report_authority,
                "/_kaede/v1/reports",
                payload=outbound.model_dump(mode="json"),
                max_response_bytes=64 * 1024,
            )
        except (FederationNetworkError, RuntimeError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_UNAVAILABLE"},
            ) from exc
        if upstream.status_code != 201:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_REJECTED"},
            )
        try:
            forwarded = decode_federation_response_json(
                upstream,
                max_response_bytes=64 * 1024,
            )
            if not isinstance(forwarded, dict):
                raise TypeError
            forwarded_report_id = int(forwarded["id"])
        except (FederationNetworkError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_INVALID_RESPONSE"},
            ) from exc
        evidence.update(
            {
                "report_authority": report_authority,
                "forwarded_report_id": str(forwarded_report_id),
            }
        )
        report_status = "awaiting_remote"
    report = AbuseReport(
        id=report_id,
        reporter_id=auth.user.id,
        reporter_domain=auth.user.origin_domain,
        reporter_is_local=True,
        target_type=payload.target_type,
        target_ref=payload.target_ref,
        category=payload.category,
        description=payload.description,
        message_ref=message_ref,
        evidence=evidence,
        encryption_mode=report_encryption_mode,
        status=report_status,
    )
    session.add(report)
    await session.commit()
    return reporter_report_payload(report)


@federation_router.post("/_kaede/v1/reports", status_code=201)
async def create_federated_report(
    payload: FederatedReportCreate,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Receive a user report at the instance authoritative for its channel."""

    if payload.target_type != "message" or payload.message_ref is None:
        raise HTTPException(status_code=422, detail={"code": "KAED_FED_REPORT_TARGET_INVALID"})
    reporter_id, reporter_domain = payload.reporter_ref.resolve(settings.domain)
    source_report_id, source_report_domain = payload.source_report_ref.resolve(settings.domain)
    if reporter_domain != principal.origin or source_report_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_REPORTER_ORIGIN_MISMATCH"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "abuse-reports",
        capacity=30,
        refill_per_minute=5,
    )
    source_report_ref = f"{source_report_id}@{source_report_domain}"
    existing = await session.scalar(
        select(AbuseReport).where(
            AbuseReport.source == "user",
            AbuseReport.reporter_is_local.is_(False),
            AbuseReport.evidence["source_report_ref"].astext == source_report_ref,
        )
    )
    if existing is not None:
        return reporter_report_payload(existing)
    reporter = await session.get(User, (reporter_id, reporter_domain))
    if (
        reporter is None
        or reporter.is_local
        or reporter.account_type != "human"
        or reporter.origin_domain != principal.origin
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_REPORTER_NOT_FOUND"})
    message_id, message_domain = payload.message_ref.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if (
        message is None
        or message.deleted_at is not None
        or message.channel_domain != settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_REPORT_MESSAGE_NOT_FOUND"})
    message_ref = f"{message.id}@{message.origin_domain}"
    if payload.target_ref != message_ref:
        raise HTTPException(status_code=422, detail={"code": "KAED_FED_REPORT_TARGET_MISMATCH"})
    access = await load_channel_access(
        session,
        settings,
        reporter,
        EntityRef(f"{message.channel_id}@{message.channel_domain}"),
    )
    await require_channel_permissions(
        session,
        redis,
        access,
        reporter,
        required_permissions("message.list"),
    )
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.purpose == "attachment",
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.created_at, Attachment.origin_domain, Attachment.id)
        )
    )
    focused_attachment: Attachment | None = None
    if payload.focused_attachment_ref is not None:
        focused_id, focused_domain = payload.focused_attachment_ref.resolve(settings.domain)
        focused_attachment = next(
            (
                item
                for item in attachments
                if (item.id, item.origin_domain) == (focused_id, focused_domain)
            ),
            None,
        )
        if focused_attachment is None:
            raise HTTPException(status_code=404, detail={"code": "KAED_FED_REPORT_MEDIA_NOT_FOUND"})
    evidence, encryption_mode = report_message_evidence(
        message,
        disclosed_content=payload.disclosed_content,
        disclosure_acknowledged=payload.disclosure_acknowledged,
        attachments=attachments,
        focused_attachment=focused_attachment,
    )
    evidence["source_report_ref"] = source_report_ref
    report = AbuseReport(
        id=await snowflake.mint(),
        reporter_id=reporter.id,
        reporter_domain=reporter.origin_domain,
        reporter_is_local=False,
        target_type="message",
        target_ref=message_ref,
        category=payload.category,
        description=payload.description,
        message_ref=message_ref,
        evidence=evidence,
        encryption_mode=encryption_mode,
    )
    session.add(report)
    await session.commit()
    return reporter_report_payload(report)


async def owned_encrypted_media_evidence_report(
    session: AsyncSession,
    report_id: int,
    auth: AuthenticatedUser,
    *,
    for_update: bool,
) -> AbuseReport:
    report = await session.get(AbuseReport, report_id, with_for_update=for_update)
    if (
        report is None
        or report.source != "user"
        or report.target_type not in {"message", "attachment"}
        or (report.reporter_id, report.reporter_domain) != (auth.user.id, auth.user.origin_domain)
        or report.evidence.get("attachment_encryption_mode") != "e2ee"
        or report.encryption_mode not in {"e2ee_metadata", "e2ee_user_disclosed"}
    ):
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})
    return report


def forwarded_report_target(report: AbuseReport) -> tuple[str, int] | None:
    authority = report.evidence.get("report_authority")
    report_id = report.evidence.get("forwarded_report_id")
    if not isinstance(authority, str) or not isinstance(report_id, str):
        return None
    try:
        return normalize_domain(authority), int(report_id)
    except (FederationNetworkError, ValueError):
        return None


async def federated_encrypted_media_evidence_report(
    session: AsyncSession,
    report_id: int,
    principal: FederationPrincipal,
    *,
    for_update: bool,
) -> tuple[AbuseReport, User]:
    report = await session.get(AbuseReport, report_id, with_for_update=for_update)
    if (
        report is None
        or report.source != "user"
        or report.reporter_is_local is not False
        or report.reporter_domain != principal.origin
        or report.target_type != "message"
        or report.evidence.get("attachment_encryption_mode") != "e2ee"
        or report.encryption_mode not in {"e2ee_metadata", "e2ee_user_disclosed"}
    ):
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_REPORT_NOT_FOUND"})
    reporter = await session.get(User, (report.reporter_id, report.reporter_domain))
    if reporter is None or reporter.is_local or reporter.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_REPORTER_NOT_FOUND"})
    return report, reporter


def attach_disclosed_evidence(report: AbuseReport, attachment: Attachment) -> None:
    evidence = dict(report.evidence)
    evidence.update(
        {
            "disclosed_attachment_ref": f"{attachment.id}@{attachment.origin_domain}",
            "disclosed_filename": attachment.filename,
            "disclosed_content_type": attachment.content_type,
            "disclosed_size": attachment.size,
            "attachment_disclosure": {
                "source": "reporter_client_decrypted",
                "reporter_acknowledged": True,
                "server_verified": False,
            },
        }
    )
    report.evidence = evidence
    report.encryption_mode = "e2ee_user_disclosed"


@router.post(
    "/reports/{report_id}/attachment-evidence",
    status_code=201,
)
async def create_report_attachment_evidence_ticket(
    report_id: int,
    payload: ReportAttachmentEvidenceTicketCreate,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if not payload.disclosure_acknowledged:
        raise HTTPException(
            status_code=422,
            detail={"code": "E2EE_ATTACHMENT_DISCLOSURE_REQUIRED"},
        )
    if payload.encryption_mode != "plaintext" or payload.encryption_protocol is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_EVIDENCE_MUST_BE_PLAINTEXT"},
        )
    await enforce_keyed_rate_limit(
        redis,
        response,
        REPORT_EVIDENCE_UPLOAD_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    report = await owned_encrypted_media_evidence_report(
        session,
        report_id,
        auth,
        for_update=True,
    )
    forwarded_target = forwarded_report_target(report)
    if forwarded_target is not None:
        authority, forwarded_report_id = forwarded_target
        try:
            upstream = await signed_request(
                session,
                settings,
                "POST",
                authority,
                f"/_kaede/v1/reports/{forwarded_report_id}/attachment-evidence",
                payload=payload.model_dump(mode="json"),
                max_response_bytes=64 * 1024,
            )
        except (FederationNetworkError, RuntimeError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_UNAVAILABLE"},
            ) from exc
        if upstream.status_code != 201:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_EVIDENCE_AUTHORITY_REJECTED"},
            )
        try:
            result = decode_federation_response_json(upstream, max_response_bytes=64 * 1024)
        except FederationNetworkError as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_INVALID_RESPONSE"},
            ) from exc
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_INVALID_RESPONSE"},
            )
        return result
    existing = await session.scalar(
        select(Attachment)
        .where(
            Attachment.report_id == report.id,
            Attachment.deleted_at.is_(None),
        )
        .with_for_update()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "REPORT_EVIDENCE_ALREADY_CREATED"},
        )
    evidence_attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        auth.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode="plaintext",
        report_id=report.id,
    )
    await session.commit()
    return ticket_payload(evidence_attachment, upload_url)


@router.put("/reports/{report_id}/attachment-evidence", status_code=202)
async def commit_report_attachment_evidence(
    report_id: int,
    payload: ReportAttachmentEvidenceCommit,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if not payload.disclosure_acknowledged:
        raise HTTPException(
            status_code=422,
            detail={"code": "E2EE_ATTACHMENT_DISCLOSURE_REQUIRED"},
        )
    report = await owned_encrypted_media_evidence_report(
        session,
        report_id,
        auth,
        for_update=True,
    )
    forwarded_target = forwarded_report_target(report)
    if forwarded_target is not None:
        authority, forwarded_report_id = forwarded_target
        try:
            upstream = await signed_request(
                session,
                settings,
                "PUT",
                authority,
                f"/_kaede/v1/reports/{forwarded_report_id}/attachment-evidence",
                payload=payload.model_dump(mode="json"),
                max_response_bytes=64 * 1024,
            )
        except (FederationNetworkError, RuntimeError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_UNAVAILABLE"},
            ) from exc
        if upstream.status_code != 202:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_EVIDENCE_AUTHORITY_REJECTED"},
            )
        try:
            result = decode_federation_response_json(upstream, max_response_bytes=64 * 1024)
            if not isinstance(result, dict):
                raise TypeError
            disclosed = result["evidence"]
            if not isinstance(disclosed, dict):
                raise TypeError
            disclosed_ref = disclosed["attachment_ref"]
        except (FederationNetworkError, KeyError, TypeError) as exc:
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_INVALID_RESPONSE"},
            ) from exc
        if not isinstance(disclosed_ref, str):
            raise HTTPException(
                status_code=502,
                detail={"code": "REPORT_AUTHORITY_INVALID_RESPONSE"},
            )
        evidence = dict(report.evidence)
        evidence["disclosed_attachment_ref"] = disclosed_ref
        evidence["attachment_disclosure"] = {
            "source": "reporter_client_decrypted",
            "reporter_acknowledged": True,
            "server_verified": False,
            "stored_by_authority": authority,
        }
        report.evidence = evidence
        report.encryption_mode = "e2ee_user_disclosed"
        await session.commit()
        return result
    evidence_attachment = await finalize_attachment(
        session,
        settings,
        auth.user,
        int(payload.attachment_id),
        required_purpose="attachment",
    )
    if evidence_attachment.report_id != report.id:
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_EVIDENCE_NOT_FOUND"},
        )
    attach_disclosed_evidence(report, evidence_attachment)
    await materialize_updated_at(session, report)
    await session.commit()
    await enqueue_best_effort(
        media_process,
        evidence_attachment.id,
        evidence_attachment.origin_domain,
    )
    return {
        "report": reporter_report_payload(report),
        "evidence": {
            "attachment_ref": report.evidence["disclosed_attachment_ref"],
            "scan_status": evidence_attachment.scan_status,
        },
    }


@federation_router.post(
    "/_kaede/v1/reports/{report_id}/attachment-evidence",
    status_code=201,
)
async def create_federated_report_attachment_evidence_ticket(
    report_id: int,
    payload: ReportAttachmentEvidenceTicketCreate,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if not payload.disclosure_acknowledged:
        raise HTTPException(
            status_code=422,
            detail={"code": "E2EE_ATTACHMENT_DISCLOSURE_REQUIRED"},
        )
    if payload.encryption_mode != "plaintext" or payload.encryption_protocol is not None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_EVIDENCE_MUST_BE_PLAINTEXT"},
        )
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "abuse-report-evidence",
        capacity=20,
        refill_per_minute=2,
    )
    report, reporter = await federated_encrypted_media_evidence_report(
        session,
        report_id,
        principal,
        for_update=True,
    )
    existing = await session.scalar(
        select(Attachment)
        .where(Attachment.report_id == report.id, Attachment.deleted_at.is_(None))
        .with_for_update()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "REPORT_EVIDENCE_ALREADY_CREATED"},
        )
    evidence_attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        reporter,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode="plaintext",
        report_id=report.id,
    )
    await session.commit()
    return ticket_payload(evidence_attachment, upload_url)


@federation_router.put(
    "/_kaede/v1/reports/{report_id}/attachment-evidence",
    status_code=202,
)
async def commit_federated_report_attachment_evidence(
    report_id: int,
    payload: ReportAttachmentEvidenceCommit,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if not payload.disclosure_acknowledged:
        raise HTTPException(
            status_code=422,
            detail={"code": "E2EE_ATTACHMENT_DISCLOSURE_REQUIRED"},
        )
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "abuse-report-evidence",
        capacity=20,
        refill_per_minute=2,
    )
    report, reporter = await federated_encrypted_media_evidence_report(
        session,
        report_id,
        principal,
        for_update=True,
    )
    evidence_attachment = await finalize_attachment(
        session,
        settings,
        reporter,
        int(payload.attachment_id),
        required_purpose="attachment",
    )
    if evidence_attachment.report_id != report.id:
        raise HTTPException(status_code=404, detail={"code": "REPORT_EVIDENCE_NOT_FOUND"})
    attach_disclosed_evidence(report, evidence_attachment)
    await materialize_updated_at(session, report)
    await session.commit()
    await enqueue_best_effort(
        media_process,
        evidence_attachment.id,
        evidence_attachment.origin_domain,
    )
    return {
        "report": reporter_report_payload(report),
        "evidence": {
            "attachment_ref": report.evidence["disclosed_attachment_ref"],
            "scan_status": evidence_attachment.scan_status,
        },
    }


@router.get("/reports/@me")
async def my_reports(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    reports = list(
        await session.scalars(
            select(AbuseReport)
            .where(
                AbuseReport.reporter_id == auth.user.id,
                AbuseReport.reporter_domain == auth.user.origin_domain,
            )
            .order_by(AbuseReport.id.desc())
            .limit(100)
        )
    )
    return [reporter_report_payload(report) for report in reports]


@router.get("/administration/reports")
async def list_reports(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    status: str | None = Query(default=None, max_length=24),
) -> list[dict[str, object]]:
    principal.require("reports.read")
    statement = select(AbuseReport).where(AbuseReport.evidence["report_authority"].astext.is_(None))
    if status:
        statement = statement.where(AbuseReport.status == status)
    reports = list(await session.scalars(statement.order_by(AbuseReport.id.desc()).limit(500)))
    users = await report_identity_lookup(session, reports, settings.domain)
    return [report_payload_with_lookup(report, users, settings.domain) for report in reports]


@router.get("/administration/reports/{report_id}/attachment/{variant}")
async def view_report_attachment(
    report_id: int,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    variant: str = Path(pattern=r"^(original|thumbnail_128|thumbnail_512|thumbnail_1024|poster)$"),
    attachment_ref: Annotated[EntityRef | None, Query()] = None,
) -> RedirectResponse:
    principal.require("reports.read")
    report = await session.get(AbuseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})
    if report.source != "user" or report.target_type not in {"message", "attachment"}:
        raise HTTPException(status_code=404, detail={"code": "REPORT_ATTACHMENT_NOT_FOUND"})
    disclosed_ref = report.evidence.get("disclosed_attachment_ref")
    focused_ref = report.evidence.get("attachment_ref")
    reported_attachment_refs: set[str] = set()
    attachment_rows = report.evidence.get("attachments")
    if isinstance(attachment_rows, list):
        for item in attachment_rows:
            if not isinstance(item, dict):
                continue
            value = item.get("attachment_ref")
            if isinstance(value, str) and value:
                reported_attachment_refs.add(value)
    if isinstance(focused_ref, str) and focused_ref:
        reported_attachment_refs.add(focused_ref)
    if report.target_type == "attachment":
        reported_attachment_refs.add(report.target_ref)
    requested_ref: str | None = None
    if attachment_ref is not None:
        try:
            requested_id, requested_domain = attachment_ref.resolve(settings.domain)
            requested_ref = f"{requested_id}@{requested_domain}"
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "REPORT_ATTACHMENT_NOT_FOUND"},
            ) from exc
        if requested_ref not in reported_attachment_refs:
            raise HTTPException(
                status_code=404,
                detail={"code": "REPORT_ATTACHMENT_NOT_FOUND"},
            )
    preview_ref: str
    if (
        report.encryption_mode == "e2ee_user_disclosed"
        and isinstance(disclosed_ref, str)
        and (requested_ref is None or requested_ref == focused_ref)
    ):
        uses_disclosed_evidence = True
        preview_ref = disclosed_ref
    else:
        uses_disclosed_evidence = False
        preview_ref = requested_ref or (
            report.target_ref
            if report.target_type == "attachment"
            else focused_ref
            if isinstance(focused_ref, str)
            else ""
        )
    try:
        attachment_id, attachment_domain = EntityRef(preview_ref).resolve(settings.domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_ATTACHMENT_NOT_FOUND"},
        ) from exc
    if attachment_domain != settings.domain:
        raise HTTPException(
            status_code=409,
            detail={"code": "REMOTE_REPORT_ATTACHMENT"},
        )
    attachment = await session.get(
        Attachment,
        (attachment_id, attachment_domain),
        with_for_update={"read": True},
    )
    source_is_bound = False
    if attachment is not None:
        if uses_disclosed_evidence:
            source_is_bound = attachment.report_id == report.id
        else:
            source_is_bound = (
                f"{attachment.message_id}@{attachment.message_domain}" == report.message_ref
            )
    if (
        attachment is None
        or attachment.deleted_at is not None
        or attachment.purpose != "attachment"
        or not source_is_bound
        or attachment.encryption_mode != "plaintext"
        or await session.get(MediaTombstoneSource, (attachment_id, attachment_domain)) is not None
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "REPORT_ATTACHMENT_NOT_FOUND"},
        )
    response = redirect_to_object(settings, attachment, variant, public=False)
    await audit(
        session,
        snowflake,
        principal,
        "admin.report.attachment_view",
        "report",
        str(report.id),
        metadata={
            "attachment_ref": preview_ref,
            "reported_attachment_ref": requested_ref or focused_ref or report.target_ref,
            "variant": variant,
            "reporter_disclosed": uses_disclosed_evidence,
        },
    )
    await session.commit()
    return response


@router.patch("/administration/reports/{report_id}")
async def patch_report(
    report_id: int,
    payload: ReportPatch,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
) -> dict[str, object]:
    principal.require("reports.manage")
    report = await session.get(AbuseReport, report_id, with_for_update=True)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})
    report.status = payload.status
    report.resolution = payload.resolution
    report.assigned_admin_id = principal.user.id
    report.assigned_admin_domain = principal.user.origin_domain
    report.resolved_at = (
        datetime.now(UTC)
        if payload.status in {"action_taken", "closed_no_action", "duplicate"}
        else None
    )
    await audit(
        session,
        snowflake,
        principal,
        "admin.report.update",
        "report",
        str(report.id),
        metadata={"status": payload.status},
    )
    await session.commit()
    # ``updated_at`` is populated by the database's ON UPDATE expression.
    # SQLAlchemy expires that attribute even with expire_on_commit disabled;
    # refresh before the synchronous payload helper reads it.
    await session.refresh(report)
    return report_payload(report)


@router.post("/administration/reports/{report_id}/actions")
async def enforce_report(
    report_id: int,
    payload: ReportActionCreate,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require("reports.manage")
    report = await session.get(AbuseReport, report_id, with_for_update=True)
    if report is None:
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})
    subject_ref = report_subject_ref(report)
    if subject_ref is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_ACTION_TARGET_UNAVAILABLE"},
        )
    try:
        user_id, user_domain = EntityRef(subject_ref).resolve(settings.domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "REPORT_ACTION_TARGET_INVALID"},
        ) from exc
    target = await session.get(User, (user_id, user_domain), with_for_update=True)
    if target is None or target.account_type != "human":
        raise HTTPException(
            status_code=409,
            detail={"code": "HUMAN_ENFORCEMENT_REQUIRED"},
        )

    now = datetime.now(UTC)
    revoked_session_ids: list[str] = []
    remote_restriction: InstanceUserRestriction | None = None
    removed_remote_memberships = []
    if payload.account_action != "none":
        principal.require("users.manage")
        if target.id == principal.user.id and target.origin_domain == principal.user.origin_domain:
            raise HTTPException(status_code=409, detail={"code": "CANNOT_SUSPEND_SELF"})
        duration = ACCOUNT_ACTION_SECONDS[payload.account_action]
        if target.is_local:
            if duration is None:
                target.disabled_at = now
                target.suspended_until = None
                revoked_session_ids = list(
                    await session.scalars(
                        update(Session)
                        .where(
                            Session.user_id == target.id,
                            Session.user_domain == target.origin_domain,
                            Session.revoked_at.is_(None),
                        )
                        .values(revoked_at=now)
                        .returning(Session.id)
                    )
                )
            elif target.disabled_at is None:
                requested_until = now + timedelta(seconds=duration)
                target.suspended_until = max(
                    requested_until,
                    target.suspended_until or requested_until,
                )
        else:
            remote_restriction = await session.get(
                InstanceUserRestriction,
                (target.id, target.origin_domain),
                with_for_update=True,
            )
            if remote_restriction is None:
                remote_restriction = InstanceUserRestriction(
                    user_id=target.id,
                    user_domain=target.origin_domain,
                    restriction_type="banned" if duration is None else "suspended",
                    expires_at=(now + timedelta(seconds=duration) if duration else None),
                    reason=payload.reason,
                    actor_id=principal.user.id,
                    actor_domain=principal.user.origin_domain,
                )
                session.add(remote_restriction)
            elif duration is None:
                remote_restriction.restriction_type = "banned"
                remote_restriction.expires_at = None
                remote_restriction.reason = payload.reason
                remote_restriction.actor_id = principal.user.id
                remote_restriction.actor_domain = principal.user.origin_domain
            elif remote_restriction.restriction_type != "banned":
                requested_until = now + timedelta(seconds=duration)
                remote_restriction.restriction_type = "suspended"
                remote_restriction.expires_at = max(
                    requested_until,
                    remote_restriction.expires_at or requested_until,
                )
                remote_restriction.reason = payload.reason
                remote_restriction.actor_id = principal.user.id
                remote_restriction.actor_domain = principal.user.origin_domain
            if duration is None:
                removed_remote_memberships = await remove_remote_user_from_local_guilds(
                    session,
                    settings,
                    principal.user,
                    target,
                )
    purge_result = None
    if payload.message_action != "none":
        created_after: datetime | None = None
        message_ref: tuple[int, str] | None = None
        if payload.message_action == "delete_reported":
            if report.message_ref is None:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "REPORT_HAS_NO_MESSAGE"},
                )
            try:
                message_ref = EntityRef(report.message_ref).resolve(settings.domain)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "REPORT_MESSAGE_REF_INVALID"},
                ) from exc
        else:
            message_seconds = MESSAGE_ACTION_SECONDS[payload.message_action]
            if message_seconds is not None:
                created_after = now - timedelta(seconds=message_seconds)
        purge_result = await purge_author_messages(
            session,
            settings,
            principal.user,
            target,
            deleted_at=now,
            created_after=created_after,
            message_ref=message_ref,
        )
        if purge_result.deleted_count == 0 and payload.account_action == "none":
            if purge_result.skipped_messages:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "REMOTE_MODERATION_REQUIRED"},
                )
            raise HTTPException(status_code=409, detail={"code": "NO_ACTIONABLE_MESSAGES"})

    enforcement = {
        "subject_ref": f"{target.id}@{target.origin_domain}",
        "account_action": payload.account_action,
        "suspended_until": (
            target.suspended_until.isoformat()
            if target.is_local and target.suspended_until
            else remote_restriction.expires_at.isoformat()
            if remote_restriction is not None and remote_restriction.expires_at
            else None
        ),
        "banned": (
            target.disabled_at is not None
            if target.is_local
            else remote_restriction is not None and remote_restriction.restriction_type == "banned"
        ),
        "permanently_suspended": (
            target.disabled_at is not None
            if target.is_local
            else remote_restriction is not None and remote_restriction.restriction_type == "banned"
        ),
        "guild_memberships_removed": len(removed_remote_memberships),
        "message_action": payload.message_action,
        "messages_deleted": purge_result.deleted_count if purge_result else 0,
        "messages_requiring_remote_action": purge_result.skipped_messages if purge_result else 0,
    }
    report.status = "action_taken"
    report.resolution = payload.reason
    report.assigned_admin_id = principal.user.id
    report.assigned_admin_domain = principal.user.origin_domain
    report.resolved_at = now
    await audit(
        session,
        snowflake,
        principal,
        "admin.report.enforce",
        "report",
        str(report.id),
        metadata={**enforcement, "reason": payload.reason},
    )
    await session.commit()
    await session.refresh(report)

    token_store = AccessTokenStore(redis, settings.access_token_ttl_seconds)
    for session_id in revoked_session_ids:
        await token_store.revoke_session(session_id)
    if removed_remote_memberships:
        await publish_remote_user_guild_removals(
            session,
            redis,
            settings,
            removed_remote_memberships,
            target,
        )
    if purge_result is not None:
        await publish_message_purge(redis, purge_result)
    return {"report": report_payload(report), "enforcement": enforcement}


@router.get("/administration/instances/blocks")
async def administration_blocks(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    principal.require("admin.read")
    blocks = list(await session.scalars(select(InstanceBlock).order_by(InstanceBlock.domain)))
    return [
        {
            "domain": block.domain,
            "level": block.level,
            "include_subdomains": block.include_subdomains,
            "reason": block.reason,
        }
        for block in blocks
    ]


@router.put("/administration/instances/blocks", status_code=204)
async def administration_put_block(
    payload: InstanceBlockCreate,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    principal.require("instances.manage")
    domain = normalize_domain(payload.domain)
    if domain == settings.domain:
        raise HTTPException(status_code=400, detail={"code": "CANNOT_BLOCK_SELF"})
    await lock_block_policy(session)
    block = await session.scalar(
        select(InstanceBlock).where(InstanceBlock.domain == domain).with_for_update()
    )
    rules = [(domain, payload.include_subdomains)]
    if block is not None:
        rules.append((block.domain, block.include_subdomains))
    destinations = await affected_peer_domains(session, rules)
    await lock_destination_policy(session, destinations)
    previously_blocked = await effective_blocked_destinations(session, destinations)
    if block is None:
        block = InstanceBlock(domain=domain, level=payload.level)
        session.add(block)
    block.level = payload.level
    block.include_subdomains = payload.include_subdomains
    block.reason = payload.reason
    await session.flush()
    wakes, replica_syncs = await reconcile_policy_change(
        session, settings, destinations, previously_blocked
    )
    await audit(
        session,
        snowflake,
        principal,
        "admin.instance.block",
        "instance",
        domain,
        metadata={"level": payload.level, "include_subdomains": payload.include_subdomains},
    )
    await session.commit()
    await wake_policy_reconciliation(wakes, replica_syncs)
    return Response(status_code=204)


@router.delete("/administration/instances/blocks/{domain}", status_code=204)
async def administration_delete_block(
    domain: str,
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    principal.require("instances.manage")
    normalized = normalize_domain(domain)
    await lock_block_policy(session)
    block = await session.scalar(
        select(InstanceBlock).where(InstanceBlock.domain == normalized).with_for_update()
    )
    if block is None:
        return Response(status_code=204)
    destinations = await affected_peer_domains(session, ((block.domain, block.include_subdomains),))
    await lock_destination_policy(session, destinations)
    previously_blocked = await effective_blocked_destinations(session, destinations)
    await session.delete(block)
    await session.flush()
    wakes, replica_syncs = await reconcile_policy_change(
        session, settings, destinations, previously_blocked
    )
    await audit(
        session,
        snowflake,
        principal,
        "admin.instance.unblock",
        "instance",
        normalized,
    )
    await session.commit()
    await wake_policy_reconciliation(wakes, replica_syncs)
    return Response(status_code=204)


@router.get("/administration/audit")
async def administration_audit(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, object]]:
    principal.require("audit.read")
    events = list(
        await session.scalars(
            select(InstanceAuditEvent).order_by(InstanceAuditEvent.id.desc()).limit(limit)
        )
    )
    return [
        {
            "id": str(event.id),
            "actor_ref": (
                f"{event.actor_id}@{event.actor_domain}"
                if event.actor_id is not None and event.actor_domain
                else None
            ),
            "actor_kind": event.actor_kind,
            "action": event.action,
            "target_type": event.target_type,
            "target_ref": event.target_ref,
            "metadata": event.detail,
            "created_at": event.created_at.isoformat(),
        }
        for event in events
    ]
