from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_session, require_user
from app.chat.channel_access import load_channel_access
from app.chat.permissions import calculate_permissions
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.types import EntityRef
from app.db.models import Attachment, MediaTombstoneSource, Message
from app.media.digest_revocation import DIGEST_REVOCATION_STATUSES
from app.safety.models import ChildSafetyCase, ChildSafetyEvidence, SafetyClientEvent

router = APIRouter(prefix="/api/v1/safety", tags=["child safety"])


class CachedContent(UnambiguousInputModel):
    message_refs: list[EntityRef] = Field(default_factory=list, max_length=200)
    attachment_refs: list[EntityRef] = Field(default_factory=list, max_length=200)


async def readable_message(
    session: AsyncSession, settings: Settings, auth: AuthenticatedUser, message: Message
) -> bool:
    try:
        access = await load_channel_access(
            session,
            settings,
            auth.user,
            EntityRef(f"{message.channel_id}@{message.channel_domain}"),
        )
        if access.guild is not None:
            permissions, _ = await calculate_permissions(
                session, access.guild, auth.user, channel=access.channel
            )
            required = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
            if permissions & required != required:
                return False
        return message.deleted_at is None
    except HTTPException:
        return False


async def readable_public_asset(
    session: AsyncSession, settings: Settings, attachment: Attachment
) -> bool:
    if (
        attachment.origin_domain != settings.domain
        or attachment.purpose == "attachment"
        or not attachment.asset_binding
        or attachment.scan_status != "clean"
        or attachment.deleted_at is not None
        or not attachment.content_sha256
    ):
        return False
    if (
        await session.get(MediaTombstoneSource, (attachment.id, attachment.origin_domain))
        is not None
    ):
        return False
    return (
        await session.scalar(
            select(Attachment.id)
            .where(
                Attachment.origin_domain == settings.domain,
                Attachment.content_sha256 == attachment.content_sha256,
                Attachment.scan_status.in_(DIGEST_REVOCATION_STATUSES),
            )
            .limit(1)
        )
        is None
    )


async def global_content_state(session: AsyncSession, kind: str, reference: str) -> str | None:
    verdict = await session.scalar(
        select(ChildSafetyCase.status)
        .outerjoin(ChildSafetyEvidence, ChildSafetyEvidence.case_id == ChildSafetyCase.id)
        .where(
            ChildSafetyCase.message_ref == reference
            if kind == "message"
            else or_(
                ChildSafetyCase.attachment_ref == reference,
                ChildSafetyEvidence.attachment_ref == reference,
            ),
            or_(
                ChildSafetyCase.status == "confirmed",
                and_(ChildSafetyCase.status == "pending", ChildSafetyCase.source == "photodna"),
            ),
        )
        .order_by((ChildSafetyCase.status == "confirmed").desc(), ChildSafetyCase.id.desc())
        .limit(1)
    )
    return (
        "removed" if verdict == "confirmed" else "pending_review" if verdict == "pending" else None
    )


@router.post("/content-status")
async def content_status(
    payload: CachedContent,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Return metadata-only cache revocations even after membership ends.

    Unknown references and current access loss require purging old caches, but
    are reversible after a fresh authenticated fetch. Remote authority history
    intentionally need not have a local Message or Attachment replica.
    """
    items = []
    for kind, refs in (("message", payload.message_refs), ("attachment", payload.attachment_refs)):
        for ref in dict.fromkeys(refs):
            resolved = ref.resolve(settings.domain)
            canonical = f"{resolved[0]}@{resolved[1]}"
            state = None
            reason = "This content is no longer available."
            row: Message | Attachment | None
            message: Message | None
            attachment: Attachment | None = None
            if kind == "message":
                message = await session.get(Message, resolved)
                row = message
            else:
                attachment = await session.get(Attachment, resolved)
                row = attachment
                message = (
                    await session.get(Message, (attachment.message_id, attachment.message_domain))
                    if attachment is not None and attachment.message_id is not None
                    else None
                )
            # Positive safety verdicts outlive replicas and memberships. An old
            # member must still revoke a cached copy after losing room access.
            state = await global_content_state(session, kind, canonical)
            if message is not None and message.safety_state:
                if state != "removed":
                    state = message.safety_state
                if state == message.safety_state and await readable_message(
                    session, settings, auth, message
                ):
                    reason = message.safety_reason or reason
            if state is None:
                if row is None or row.deleted_at is not None:
                    state = "unavailable"
                elif message is not None:
                    if not await readable_message(session, settings, auth, message):
                        state = "unavailable"
                elif attachment is not None:
                    owned = (attachment.uploader_id, attachment.uploader_domain) == (
                        auth.user.id,
                        auth.user.origin_domain,
                    )
                    public = await readable_public_asset(session, settings, attachment)
                    if (not owned and not public) or attachment.scan_status not in {
                        "clean",
                        "encrypted",
                    }:
                        state = "unavailable"
            # A report hides content only for its reporter, across their devices.
            recorded = await session.scalar(
                select(SafetyClientEvent.state)
                .where(
                    SafetyClientEvent.user_id == auth.user.id,
                    SafetyClientEvent.user_domain == auth.user.origin_domain,
                    SafetyClientEvent.state.in_(["hidden", "removed"]),
                    (
                        SafetyClientEvent.message_refs
                        if kind == "message"
                        else SafetyClientEvent.attachment_refs
                    ).contains([canonical]),
                )
                .order_by(SafetyClientEvent.state.desc())
                .limit(1)
            )
            if recorded == "removed":
                state = "removed"
            elif recorded == "hidden" and state != "removed":
                state, reason = "hidden", "You reported this content for a child-safety review."
            # A complete reconciliation must clear stale temporary holds after a
            # device missed the live release. Clients keep removed/hidden marks.
            if state is None:
                state, reason = "released", "This content is available."
            if state:
                items.append(
                    {
                        "message_refs": [canonical] if kind == "message" else [],
                        "attachment_refs": [canonical] if kind == "attachment" else [],
                        "state": state,
                        "reason": reason,
                    }
                )
    return {"items": items}
