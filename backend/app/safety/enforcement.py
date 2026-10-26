"""Private preservation and the public, backwards-compatible safety projection."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import exists, false, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import Exists

from app.chat.events import user_topic
from app.chat.permissions import calculate_permissions
from app.chat.postcommit import queue_postcommit_dispatch
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import AbuseReport
from app.db.models import (
    Attachment,
    Channel,
    DMParticipant,
    Guild,
    GuildMember,
    Message,
    RemoteMediaCache,
    User,
)
from app.media.service import discard_attachment
from app.media.storage import S3Storage, StorageError
from app.safety.models import ChildSafetyCase, ChildSafetyEvidence
from app.safety.service import case_details, preserve_evidence, read_evidence, record_client_event
from app.safety.storage import decrypt_metadata, encrypt_metadata

REVIEW_REASON = (
    "This content is temporarily unavailable because an automated child-safety check "
    "flagged it for review."
)
REMOVED_REASON = "This content was removed following a child-safety review."
SNAPSHOT_FIELDS = (
    "content",
    "e2ee",
    "embeds",
    "components",
    "sticker_items",
    "forward_snapshot",
    "poll_result",
    "mention_user_refs",
    "mention_role_refs",
    "mention_everyone",
    "flags",
    "message_type",
)


def snapshot_message_fields(message: Message) -> dict[str, object]:
    return {
        **{name: getattr(message, name) for name in SNAPSHOT_FIELDS},
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
    }


def ensure_message_mutable(message: Message) -> None:
    if getattr(message, "safety_state", None):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_REVIEW_LOCKED"})


async def ensure_channels_deletable(
    session: AsyncSession, channel_refs: list[tuple[int, str]]
) -> None:
    protected = await session.scalar(
        select(Message.id)
        .where(
            tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs),
            Message.safety_state.is_not(None),
        )
        .limit(1)
    )
    if protected is not None:
        raise HTTPException(status_code=409, detail={"code": "SAFETY_REVIEW_LOCKED"})


async def case_for_attachment(
    session: AsyncSession, attachment: Attachment
) -> ChildSafetyCase | None:
    result = await session.scalar(
        select(ChildSafetyCase)
        .join(ChildSafetyEvidence, ChildSafetyEvidence.case_id == ChildSafetyCase.id)
        .where(
            ChildSafetyEvidence.attachment_ref == f"{attachment.id}@{attachment.origin_domain}",
            ChildSafetyEvidence.stored_at.is_not(None),
            ChildSafetyEvidence.deleted_at.is_(None),
            ChildSafetyCase.status.in_(["pending", "confirmed"]),
            or_(
                ChildSafetyCase.source == "photodna",
                ChildSafetyEvidence.provenance == "reporter_disclosed",
            ),
        )
        .order_by(ChildSafetyCase.id.desc())
        .limit(1)
    )

    return result


def pending_attachment_review() -> Exists:
    return exists(
        select(ChildSafetyEvidence.id)
        .join(ChildSafetyCase)
        .where(
            ChildSafetyEvidence.attachment_ref
            == func.concat(Attachment.id, "@", Attachment.origin_domain),
            ChildSafetyEvidence.stored_at.is_not(None),
            ChildSafetyEvidence.deleted_at.is_(None),
            or_(
                ChildSafetyCase.source == "photodna",
                ChildSafetyEvidence.provenance == "reporter_disclosed",
            ),
            ChildSafetyCase.status == "pending",
        )
    )


async def dismissed_digest(session: AsyncSession, attachment_ref: str, digest: str) -> bool:
    return (
        await session.scalar(
            select(ChildSafetyEvidence.id)
            .join(ChildSafetyCase)
            .where(
                ChildSafetyEvidence.attachment_ref == attachment_ref,
                ChildSafetyEvidence.sha256 == digest,
                ChildSafetyCase.status == "dismissed",
            )
            .limit(1)
        )
        is not None
    )


async def message_recipients(session: AsyncSession, message: Message) -> list[tuple[int, str]]:
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        return []
    if channel.guild_id is None:
        return list(
            (
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == channel.id,
                        DMParticipant.conversation_domain == channel.origin_domain,
                    )
                )
            ).tuples()
        )
    guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
    if guild is None:
        return []
    users = await session.scalars(
        select(User)
        .join(
            GuildMember,
            (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
        )
        .where(GuildMember.guild_id == guild.id, GuildMember.guild_domain == guild.origin_domain)
    )
    recipients = []
    for user in users:
        permissions, _ = await calculate_permissions(session, guild, user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL and permissions & Permission.READ_MESSAGE_HISTORY:
            recipients.append((user.id, user.origin_domain))
    return recipients


async def project_message(
    session: AsyncSession,
    settings: Settings,
    message: Message,
    attachments: list[Attachment],
    state: str,
    *,
    safety_updated_at: datetime | None = None,
) -> None:
    from app.chat.payloads import message_payload

    message.safety_updated_at = safety_updated_at or datetime.now(UTC)
    recipients = await message_recipients(session, message)
    await record_client_event(
        session,
        user_refs=recipients,
        message_refs=[f"{message.id}@{message.origin_domain}"],
        attachment_refs=[f"{a.id}@{a.origin_domain}" for a in attachments],
        state=state,
        reason=message.safety_reason or "The content review is complete.",
    )
    author = await session.get(User, (message.author_id, message.author_domain))
    rendered = message_payload(message, author, attachments)
    for user_id, domain in recipients:
        queue_postcommit_dispatch(session, user_topic(domain, user_id), "MESSAGE_UPDATE", rendered)
    if message.channel_domain == settings.domain and author is not None:
        from app.chat.channel_access import ChannelAccess
        from app.chat.guild_revision import queue_guild_mutation
        from app.chat.postcommit import queue_postcommit_federation_wakes

        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if channel is not None and channel.guild_id is not None:
            guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
            if guild is not None:
                content: dict[str, object] = {"message": rendered}
                if state == "removed":
                    content = {
                        "message": {"id": str(message.id), "origin_domain": message.origin_domain},
                        "deleted_at": datetime.now(UTC).isoformat(),
                        "safety_state": "removed",
                        "safety_reason": REMOVED_REASON,
                        "attachment_refs": [f"{a.id}@{a.origin_domain}" for a in attachments],
                    }
                await queue_guild_mutation(
                    session,
                    settings,
                    guild,
                    author,
                    "guild.message.delete" if state == "removed" else "guild.message.update",
                    content,
                    channel=channel,
                )
                queue_postcommit_federation_wakes(
                    session, list(getattr(guild, "_federation_delivery_wakes", []))
                )
        elif channel is not None:
            from app.api.channels import queue_dm_authority_mutation

            participants = list(
                await session.scalars(
                    select(User).where(tuple_(User.id, User.origin_domain).in_(recipients))
                )
            )
            destinations = await queue_dm_authority_mutation(
                session,
                settings,
                ChannelAccess(channel, None, participants),
                author,
                "dm.message.delete" if state == "removed" else "dm.message.update",
                {
                    "message_id": str(message.id),
                    "message_domain": message.origin_domain,
                    "channel_id": str(channel.id),
                    "channel_domain": channel.origin_domain,
                    "deleted_at": datetime.now(UTC).isoformat(),
                }
                if state == "removed"
                else {"message": rendered},
            )
            queue_postcommit_federation_wakes(session, sorted(destinations))


async def protect_message(
    session: AsyncSession,
    settings: Settings,
    case: ChildSafetyCase,
    message: Message,
    *,
    state: str,
) -> None:
    if message.safety_state == "removed":
        state = "removed"
    details = case_details(settings, case)
    snapshots = dict(details.get("message_snapshots", {}))
    ref = f"{message.id}@{message.origin_domain}"
    if ref not in snapshots and not message.safety_state:
        snapshots[ref] = snapshot_message_fields(message)
        details["message_snapshots"] = snapshots
        case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    if case.message_ref is None:
        case.message_ref = ref
    message.safety_state = state
    message.safety_reason = REVIEW_REASON if state == "pending_review" else REMOVED_REASON
    message.edited_at = datetime.now(UTC)
    message.content = message.safety_reason
    message.e2ee = None
    message.embeds = []
    message.components = []
    message.sticker_items = []
    message.forward_snapshot = None
    message.poll_result = None
    message.mention_user_refs = []
    message.mention_role_refs = []
    message.mention_everyone = False
    # Legacy clients must render the text, even if the original used components V2.
    message.flags = 0
    message.message_type = 0
    attachments = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
            )
        )
    )
    await project_message(session, settings, message, attachments, state)


async def protect_bound_attachments(
    session: AsyncSession, settings: Settings, message: Message, attachments: list[Attachment]
) -> None:
    snapshot = snapshot_message_fields(message)
    for attachment in attachments:
        case = await case_for_attachment(session, attachment)
        if case is not None:
            details = case_details(settings, case)
            snapshots = dict(details.get("message_snapshots", {}))
            snapshots.setdefault(f"{message.id}@{message.origin_domain}", snapshot)
            details["message_snapshots"] = snapshots
            case.details = encrypt_metadata(settings, details, f"case:{case.id}")
            await protect_message(
                session,
                settings,
                case,
                message,
                state="removed" if case.status == "confirmed" else "pending_review",
            )


async def preserve_attachment(
    session: AsyncSession, settings: Settings, case: ChildSafetyCase, attachment: Attachment
) -> None:
    from app.safety.removal import remember_attachment_objects

    await remember_attachment_objects(session, settings, case, attachment)
    existing = await session.scalar(
        select(ChildSafetyEvidence.id).where(
            ChildSafetyEvidence.case_id == case.id,
            ChildSafetyEvidence.attachment_ref == f"{attachment.id}@{attachment.origin_domain}",
            ChildSafetyEvidence.deleted_at.is_(None),
            ChildSafetyEvidence.stored_at.is_not(None),
        )
    )
    if existing is not None:
        return
    if attachment.origin_domain != settings.domain:
        # Preserve an original already held locally without fetching remote
        # content under an administrator's credentials.
        cached = await session.get(
            RemoteMediaCache, (attachment.origin_domain, attachment.id, "original")
        )
        if cached is None or cached.scan_status != "clean":
            return
        data = await S3Storage(settings).get(
            settings.media_remote_cache_bucket, cached.object_key, max_bytes=max(cached.size, 1)
        )
        if len(data) != cached.size or hashlib.sha256(data).hexdigest() != cached.content_sha256:
            raise StorageError("cached evidence integrity check failed")
        provenance = "federated_origin"
    else:
        data = await S3Storage(settings).get(
            settings.media_attachments_bucket,
            attachment.object_key,
            max_bytes=max(attachment.size, 1),
        )
        provenance = "reporter_disclosed" if attachment.report_id else "local_upload"
    await preserve_evidence(
        session,
        settings,
        case,
        f"{attachment.id}@{attachment.origin_domain}",
        data,
        filename=attachment.filename,
        content_type=attachment.content_type,
        provenance=provenance,
    )


async def intake_report(session: AsyncSession, settings: Settings, report: AbuseReport) -> None:
    from app.safety.service import capture_report

    if report.category != "csam":
        return
    try:
        _ = settings.safety_evidence_key_bytes
    except ValueError as exc:
        raise HTTPException(status_code=503, detail={"code": "SAFETY_STORAGE_UNAVAILABLE"}) from exc
    message = (
        await session.get(
            Message, EntityRef(report.message_ref).resolve(settings.domain), with_for_update=True
        )
        if report.message_ref
        else None
    )
    subject = (
        await session.get(User, (message.author_id, message.author_domain)) if message else None
    )
    case = await capture_report(session, settings, report, message=message, subject=subject)
    attachments = (
        list(
            await session.scalars(
                select(Attachment).where(
                    Attachment.message_id == message.id,
                    Attachment.message_domain == message.origin_domain,
                    Attachment.deleted_at.is_(None),
                )
            )
        )
        if message
        else []
    )
    for attachment in attachments:
        if attachment.scan_status in {"clean", "encrypted"}:
            await preserve_attachment(session, settings, case, attachment)
    details = case_details(settings, case)
    details["provenance_verified"] = bool(
        message is not None and message.origin_domain == settings.domain and message.e2ee is None
    )
    case.details = encrypt_metadata(settings, details, f"case:{case.id}")
    # Original content, context and descriptions now live only in encrypted case
    # snapshots. Keep only the fields needed by the consented evidence-upload API.
    report.evidence = {
        key: value
        for key, value in report.evidence.items()
        if key
        in {
            "attachment_ref",
            "attachment_encryption_mode",
            "disclosed_attachment_ref",
            "attachment_disclosure",
            "source_report_ref",
        }
    }
    report.description = None
    if report.reporter_id is not None and report.reporter_domain is not None:
        await record_client_event(
            session,
            user_refs=[(report.reporter_id, report.reporter_domain)],
            message_refs=[report.message_ref] if report.message_ref else [],
            attachment_refs=[f"{a.id}@{a.origin_domain}" for a in attachments],
            state="hidden",
            reason="You reported this content for a child-safety review.",
        )


async def require_regular_report(session: AsyncSession, report: AbuseReport) -> None:
    from app.safety.models import ChildSafetyReport

    linked = await session.get(ChildSafetyReport, report.id)
    if (
        report.category == "csam"
        or report.source == "photodna"
        or report.evidence.get("photodna")
        or linked is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "REPORT_NOT_FOUND"})


async def finalize_case_review(
    session: AsyncSession, settings: Settings, case: ChildSafetyCase, redis: Redis
) -> None:
    from app.chat.postcommit import publish_committed_dispatches
    from app.core.task_wake import enqueue_best_effort
    from app.tasks import (
        _publish_terminal_asset_invalidation,
        media_cache_gc,
        media_process,
        safety_media_removal,
    )

    await publish_committed_dispatches(session, redis)
    if case.status == "confirmed":
        await enqueue_best_effort(safety_media_removal)
    refs = list(
        await session.scalars(
            select(ChildSafetyEvidence.attachment_ref).where(ChildSafetyEvidence.case_id == case.id)
        )
    )
    for ref in refs:
        if not ref:
            continue
        attachment = await session.get(Attachment, EntityRef(ref).resolve(settings.domain))
        if attachment is None or attachment.origin_domain != settings.domain:
            continue
        if case.status == "dismissed" and attachment.scan_status == "pending":
            await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    for invalidation in session.info.pop("safety_asset_invalidations", []):
        await _publish_terminal_asset_invalidation(session, redis, invalidation)
    if session.info.pop("safety_remote_media_expired", False):
        await enqueue_best_effort(media_cache_gc)


async def apply_case_review(
    session: AsyncSession,
    settings: Settings,
    case: ChildSafetyCase,
    decision: str,
    actor: User,
    redis: Redis,
    *,
    suspend_account: bool = False,
) -> None:
    del redis
    evidence = list(
        await session.scalars(
            select(ChildSafetyEvidence).where(
                ChildSafetyEvidence.case_id == case.id, ChildSafetyEvidence.deleted_at.is_(None)
            )
        )
    )
    details = case_details(settings, case)
    if decision == "confirmed" and case.message_ref and not details.get("provenance_verified"):
        raise HTTPException(status_code=409, detail={"code": "SAFETY_SOURCE_VERIFICATION_REQUIRED"})
    refs = {item.attachment_ref for item in evidence if item.attachment_ref}
    if case.attachment_ref:
        refs.add(case.attachment_ref)
    message = (
        await session.get(
            Message, EntityRef(case.message_ref).resolve(settings.domain), with_for_update=True
        )
        if case.message_ref
        else None
    )
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                or_(
                    *[
                        (Attachment.id == EntityRef(ref).resolve(settings.domain)[0])
                        & (Attachment.origin_domain == EntityRef(ref).resolve(settings.domain)[1])
                        for ref in refs
                    ],
                    (
                        (Attachment.message_id == message.id)
                        & (Attachment.message_domain == message.origin_domain)
                    )
                    if message is not None
                    else false(),
                )
            )
            .with_for_update()
        )
    )
    if decision == "dismissed":
        # Only restore bytes that were withheld by automatic detection. Allegations
        # did not withdraw anyone else's copy in the first place.
        for attachment in attachments:
            if attachment.deleted_at is not None or attachment.scan_status != "pending":
                continue
            if attachment.report_id is not None:
                await discard_attachment(session, settings, attachment)
                continue
            item = next(
                (
                    e
                    for e in evidence
                    if e.attachment_ref == f"{attachment.id}@{attachment.origin_domain}"
                ),
                None,
            )
            if item is None or attachment.origin_domain != settings.domain:
                continue
            data = await read_evidence(settings, item)
            reviewed_key = f"reviewed/{attachment.origin_domain}/{attachment.id}/{item.sha256}"
            await S3Storage(settings).put(
                settings.media_attachments_bucket,
                reviewed_key,
                data,
                attachment.content_type,
            )
            attachment.object_key = reviewed_key
        snapshots = details.get("message_snapshots", {})
        if not snapshots and message is not None and case.message_snapshot.get("sealed"):
            snapshots = {
                case.message_ref: decrypt_metadata(
                    settings,
                    base64.b64decode(case.message_snapshot["sealed"]),
                    f"message:{case.id}",
                )
            }
        for ref, snapshot in snapshots.items():
            restored = await session.get(
                Message, EntityRef(ref).resolve(settings.domain), with_for_update=True
            )
            if restored is not None and restored.safety_state == "pending_review":
                other = await session.scalar(
                    select(ChildSafetyCase.id)
                    .where(
                        ChildSafetyCase.id != case.id,
                        ChildSafetyCase.message_ref == ref,
                        or_(
                            ChildSafetyCase.status == "confirmed",
                            (ChildSafetyCase.status == "pending")
                            & (ChildSafetyCase.source == "photodna"),
                        ),
                    )
                    .limit(1)
                )
                if other is not None:
                    continue
                for name in SNAPSHOT_FIELDS:
                    if name in snapshot:
                        setattr(restored, name, snapshot[name])
                restored.safety_state = None
                restored.safety_reason = None
                # MLS authenticates the authored revision, not the review time.
                edited_at = snapshot.get("edited_at")
                restored.edited_at = datetime.fromisoformat(edited_at) if edited_at else None
                await project_message(session, settings, restored, attachments, "released")
        return
    # Copies can share an object or carry its immutable source identity. Revoke
    # every local copy and exact digest, but never infer original-message linkage
    # from a reporter's independently supplied decrypted file.
    originals = [a for a in attachments if a.report_id is None]
    if originals:
        source_refs = [(a.id, a.origin_domain) for a in originals]
        object_keys = [a.object_key for a in originals if a.origin_domain == settings.domain]
        digests = [a.content_sha256 for a in originals if a.content_sha256]
        copies = list(
            await session.scalars(
                select(Attachment)
                .where(
                    or_(
                        tuple_(
                            Attachment.source_attachment_id, Attachment.source_attachment_domain
                        ).in_(source_refs),
                        (Attachment.origin_domain == settings.domain)
                        & Attachment.object_key.in_(object_keys),
                        (Attachment.origin_domain == settings.domain)
                        & Attachment.content_sha256.in_(digests),
                    )
                )
                .with_for_update()
            )
        )
        attachments = list({(a.id, a.origin_domain): a for a in [*attachments, *copies]}.values())
    # Copy and verify every available original before any public withdrawal.
    for attachment in attachments:
        if attachment.deleted_at is None:
            await preserve_attachment(session, settings, case, attachment)
    message_refs = {
        (a.message_id, a.message_domain) for a in attachments if a.message_id is not None
    }
    if message is not None:
        message_refs.add((message.id, message.origin_domain))
    messages = list(
        await session.scalars(
            select(Message)
            .where(
                or_(
                    tuple_(Message.id, Message.origin_domain).in_(message_refs),
                    tuple_(Message.forwarded_message_id, Message.forwarded_message_domain).in_(
                        message_refs
                    ),
                )
            )
            .with_for_update()
        )
    )
    for affected in messages:
        if affected.deleted_at is None:
            await protect_message(session, settings, case, affected, state="removed")
        else:
            await record_client_event(
                session,
                user_refs=await message_recipients(session, affected),
                message_refs=[f"{affected.id}@{affected.origin_domain}"],
                attachment_refs=[
                    f"{a.id}@{a.origin_domain}"
                    for a in attachments
                    if (a.message_id, a.message_domain) == (affected.id, affected.origin_domain)
                ],
                state="removed",
                reason=REMOVED_REASON,
            )
    from app.chat.postcommit import queue_postcommit_federation_wakes
    from app.media.tombstones import queue_terminal_attachment_tombstone
    from app.safety.removal import remember_attachment_objects

    for attachment in attachments:
        await remember_attachment_objects(session, settings, case, attachment)
        if attachment.deleted_at is None:
            await discard_attachment(session, settings, attachment)
        if attachment.origin_domain != settings.domain:
            await session.execute(
                update(RemoteMediaCache)
                .where(
                    RemoteMediaCache.origin_domain == attachment.origin_domain,
                    RemoteMediaCache.attachment_id == attachment.id,
                )
                .values(expires_at=datetime.now(UTC))
            )
            session.info["safety_remote_media_expired"] = True
        if attachment.encryption_mode != "e2ee":
            attachment.scan_status = "quarantined"
        attachment.staging_object_key = attachment.staging_object_key or attachment.object_key
        destinations = await queue_terminal_attachment_tombstone(session, settings, attachment)
        queue_postcommit_federation_wakes(session, sorted(destinations))
        if attachment.asset_binding is not None:
            from app.media.asset_invalidation import invalidate_terminal_asset_binding

            invalidation = await invalidate_terminal_asset_binding(session, settings, attachment)
            if invalidation is not None:
                session.info.setdefault("safety_asset_invalidations", []).append(invalidation)
        if attachment.message_id is None:
            await record_client_event(
                session,
                user_refs=[(attachment.uploader_id, attachment.uploader_domain)],
                message_refs=[],
                attachment_refs=[f"{attachment.id}@{attachment.origin_domain}"],
                state="removed",
                reason=REMOVED_REASON,
            )
    if suspend_account:
        if not details.get("provenance_verified") or not case.subject_ref:
            raise HTTPException(status_code=409, detail={"code": "SAFETY_PROVENANCE_UNVERIFIED"})
        user = await session.get(
            User, EntityRef(case.subject_ref).resolve(settings.domain), with_for_update=True
        )
        if user is None or not user.is_local or user.id == actor.id:
            raise HTTPException(status_code=409, detail={"code": "SAFETY_SUSPENSION_UNAVAILABLE"})
        until = datetime.now(UTC) + timedelta(days=7)
        user.suspended_until = max(user.suspended_until or until, until)
