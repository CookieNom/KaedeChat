from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.installations import installation_has_membership
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import BotInstallation
from app.db.models import Attachment, User, UserStorageUsage
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    try_lock_asset_digest,
    valid_content_digest,
)
from app.media.payloads import attachment_payload as render_attachment_payload
from app.media.processing import normalize_declared_type, sanitize_filename
from app.media.storage import S3Storage, StorageError


def original_object_key(domain: str, attachment_id: int) -> str:
    # Browser-issued PUT credentials are valid for the full ticket lifetime, so
    # this key is staging-only and must never be served after scanning.
    return f"{domain}/{attachment_id}/staging/original"


def clean_object_key(domain: str, attachment_id: int, digest: str) -> str:
    """Return a server-only immutable key for the exact bytes that were scanned."""

    return f"{domain}/{attachment_id}/clean/{digest}/original"


def derived_object_key(domain: str, attachment_id: int, variant: str) -> str:
    return f"{domain}/{attachment_id}/{variant}.webp"


def attachment_payload(attachment: Attachment) -> dict[str, object]:
    return render_attachment_payload(attachment)


async def locked_usage(session: AsyncSession, settings: Settings, user: User) -> UserStorageUsage:
    if user.origin_domain != settings.domain or not user.is_local:
        raise HTTPException(status_code=403, detail={"code": "LOCAL_USER_REQUIRED"})
    await session.execute(
        pg_insert(UserStorageUsage)
        .values(
            user_id=user.id,
            user_domain=settings.domain,
            user_is_local=True,
            bytes_used=0,
            pending_bytes=0,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "user_domain"])
    )
    usage = await session.scalar(
        select(UserStorageUsage)
        .where(
            UserStorageUsage.user_id == user.id,
            UserStorageUsage.user_domain == settings.domain,
        )
        .with_for_update()
    )
    if usage is None:
        raise RuntimeError("storage accounting row did not converge")
    return usage


async def locked_bot_installation(
    session: AsyncSession,
    user: User,
    installation_id: int,
) -> BotInstallation:
    """Lock the durable quota ledger for one bot installation.

    Bot users may be federated identities, so they must never be inserted into
    the local-human ``user_storage_usage`` table.  The installation is the
    stable authority-local billing and revocation boundary instead.
    """

    installation = await session.scalar(
        select(BotInstallation)
        .where(
            BotInstallation.id == installation_id,
            BotInstallation.status == "active",
            installation_has_membership(),
        )
        .with_for_update()
    )
    if (
        installation is None
        or installation.status != "active"
        or (installation.bot_user_id, installation.bot_user_domain) != (user.id, user.origin_domain)
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return installation


async def create_upload_ticket(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    user: User,
    *,
    filename: str,
    content_type: str,
    size: int,
    purpose: str = "attachment",
    encryption_mode: str = "plaintext",
    encryption_protocol: str | None = None,
    bot_installation: BotInstallation | None = None,
    media_transform: dict[str, object] | None = None,
    report_id: int | None = None,
) -> tuple[Attachment, str]:
    if size <= 0 or size > settings.media_max_attachment_bytes:
        raise HTTPException(status_code=413, detail={"code": "ATTACHMENT_TOO_LARGE"})
    declared_type = normalize_declared_type(content_type)
    if bot_installation is None:
        human_usage = await locked_usage(session, settings, user)
        usage: UserStorageUsage | BotInstallation = human_usage
        pending_query = (
            select(func.count())
            .select_from(Attachment)
            .where(
                Attachment.uploader_id == user.id,
                Attachment.uploader_domain == user.origin_domain,
                Attachment.bot_installation_id.is_(None),
            )
        )
        pending_bytes = human_usage.pending_bytes
        bytes_used = human_usage.bytes_used
    else:
        installation_usage = await locked_bot_installation(session, user, bot_installation.id)
        usage = installation_usage
        pending_query = (
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.bot_installation_id == installation_usage.id)
        )
        pending_bytes = installation_usage.media_pending_bytes
        bytes_used = installation_usage.media_bytes_used
    pending_count = await session.scalar(
        pending_query.where(
            Attachment.finalized_at.is_(None),
            Attachment.deleted_at.is_(None),
            Attachment.upload_expires_at > func.now(),
        )
    )
    if (pending_count or 0) >= settings.media_inflight_limit:
        raise HTTPException(status_code=429, detail={"code": "UPLOAD_INFLIGHT_LIMIT"})
    if pending_bytes + size > settings.media_inflight_quota_bytes:
        raise HTTPException(status_code=413, detail={"code": "UPLOAD_INFLIGHT_QUOTA_EXCEEDED"})
    if bytes_used + pending_bytes + size > settings.media_user_quota_bytes:
        raise HTTPException(status_code=413, detail={"code": "USER_STORAGE_QUOTA_EXCEEDED"})
    attachment_id = await snowflake.mint()
    expires_at = datetime.now(UTC) + timedelta(seconds=settings.media_upload_ttl_seconds)
    attachment = Attachment(
        id=attachment_id,
        origin_domain=settings.domain,
        uploader_id=user.id,
        uploader_domain=user.origin_domain,
        bot_installation_id=(usage.id if isinstance(usage, BotInstallation) else None),
        filename=sanitize_filename(filename),
        content_type=declared_type,
        size=size,
        object_key=original_object_key(settings.domain, attachment_id),
        staging_object_key=original_object_key(settings.domain, attachment_id),
        scan_status="pending",
        encryption_mode=encryption_mode,
        encryption_protocol=encryption_protocol,
        purpose=purpose,
        report_id=report_id,
        upload_expires_at=expires_at,
        variants={},
        media_transform=media_transform,
    )
    session.add(attachment)
    if isinstance(usage, UserStorageUsage):
        usage.pending_bytes += size
    else:
        usage.media_pending_bytes += size
    await session.flush()
    try:
        upload_url = S3Storage(settings).presign(
            "PUT",
            settings.media_attachments_bucket,
            attachment.object_key,
            expires=settings.media_upload_ttl_seconds,
            content_length=size,
            content_type=declared_type,
        )
    except StorageError as exc:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"}) from exc
    return attachment, upload_url


async def finalize_attachment(
    session: AsyncSession,
    settings: Settings,
    user: User,
    attachment_id: int,
    *,
    required_purpose: str | None = None,
) -> Attachment:
    attachment = await session.scalar(
        select(Attachment)
        .where(
            Attachment.id == attachment_id,
            Attachment.origin_domain == settings.domain,
        )
        .with_for_update()
    )
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if (attachment.uploader_id, attachment.uploader_domain) != (user.id, user.origin_domain):
        raise HTTPException(status_code=403, detail={"code": "ATTACHMENT_NOT_OWNED"})
    if required_purpose is not None and attachment.purpose != required_purpose:
        raise HTTPException(status_code=400, detail={"code": "ATTACHMENT_PURPOSE_MISMATCH"})
    if attachment.finalized_at is not None:
        return attachment
    now = datetime.now(UTC)
    if attachment.upload_expires_at is None or attachment.upload_expires_at <= now:
        raise HTTPException(status_code=410, detail={"code": "UPLOAD_TICKET_EXPIRED"})
    try:
        metadata = await S3Storage(settings).head(
            settings.media_attachments_bucket, attachment.object_key
        )
    except StorageError as exc:
        raise HTTPException(status_code=400, detail={"code": "UPLOAD_INCOMPLETE"}) from exc
    if metadata.size != attachment.size:
        raise HTTPException(status_code=400, detail={"code": "UPLOAD_SIZE_MISMATCH"})
    try:
        stored_type = normalize_declared_type(metadata.content_type)
    except ValueError:
        stored_type = ""
    if stored_type != attachment.content_type:
        raise HTTPException(status_code=400, detail={"code": "UPLOAD_TYPE_MISMATCH"})
    if attachment.bot_installation_id is None:
        human_usage = await locked_usage(session, settings, user)
        usage: UserStorageUsage | BotInstallation = human_usage
        pending_bytes = human_usage.pending_bytes
        bytes_used = human_usage.bytes_used
    else:
        installation_usage = await locked_bot_installation(
            session, user, attachment.bot_installation_id
        )
        usage = installation_usage
        pending_bytes = installation_usage.media_pending_bytes
        bytes_used = installation_usage.media_bytes_used
    if pending_bytes < attachment.size:
        raise RuntimeError("pending storage accounting underflow")
    if bytes_used + attachment.size > settings.media_user_quota_bytes:
        raise HTTPException(status_code=413, detail={"code": "USER_STORAGE_QUOTA_EXCEEDED"})
    if isinstance(usage, UserStorageUsage):
        usage.pending_bytes -= attachment.size
        usage.bytes_used += attachment.size
    else:
        usage.media_pending_bytes -= attachment.size
        usage.media_bytes_used += attachment.size
    attachment.finalized_at = now
    return attachment


async def bind_asset(
    session: AsyncSession, attachment: Attachment, binding: str
) -> Attachment | None:
    digest = attachment.content_sha256
    if (
        attachment.scan_status != "clean"
        or attachment.deleted_at is not None
        or not valid_content_digest(digest)
    ):
        raise HTTPException(status_code=409, detail={"code": "MEDIA_NOT_AVAILABLE"})
    # Callers already own the destination User/Guild and this Attachment row.
    # A public capability takes digest -> Attachment, so never wait here while
    # holding the inverse row lock. The short, neutral 503 makes the client
    # retry after the capability/verdict transaction releases the fence.
    if not await try_lock_asset_digest(session, digest):
        raise HTTPException(
            status_code=503,
            detail={"code": "MEDIA_NOT_AVAILABLE"},
            headers={"Retry-After": "1"},
        )
    terminal_evidence = await session.scalar(
        select(Attachment.id)
        .where(
            Attachment.origin_domain == attachment.origin_domain,
            Attachment.content_sha256 == digest,
            Attachment.scan_status.in_(DIGEST_REVOCATION_STATUSES),
        )
        .limit(1)
    )
    if terminal_evidence is not None:
        # Do not disclose whether the retained affirmative digest evidence was
        # malware or a PhotoDNA match.
        raise HTTPException(status_code=409, detail={"code": "MEDIA_NOT_AVAILABLE"})
    if attachment.asset_binding not in {None, binding}:
        raise HTTPException(status_code=409, detail={"code": "ASSET_ALREADY_USED"})
    previous = await session.scalar(
        select(Attachment)
        .where(
            Attachment.asset_binding == binding,
            tuple_(Attachment.id, Attachment.origin_domain)
            != (attachment.id, attachment.origin_domain),
        )
        .with_for_update()
    )
    if previous is not None:
        previous.asset_binding = None
        await session.flush()
    attachment.asset_binding = binding
    return previous


async def discard_attachment(
    session: AsyncSession, settings: Settings, attachment: Attachment
) -> None:
    if attachment.bot_installation_id is None:
        usage: UserStorageUsage | BotInstallation | None = await session.scalar(
            select(UserStorageUsage)
            .where(
                UserStorageUsage.user_id == attachment.uploader_id,
                UserStorageUsage.user_domain == attachment.uploader_domain,
            )
            .with_for_update()
        )
    else:
        usage = await session.scalar(
            select(BotInstallation)
            .where(BotInstallation.id == attachment.bot_installation_id)
            .with_for_update()
        )
    if usage is not None:
        if isinstance(usage, UserStorageUsage):
            if attachment.finalized_at is None:
                usage.pending_bytes = max(0, usage.pending_bytes - attachment.size)
            else:
                usage.bytes_used = max(0, usage.bytes_used - attachment.size)
        elif attachment.finalized_at is None:
            usage.media_pending_bytes = max(0, usage.media_pending_bytes - attachment.size)
        else:
            usage.media_bytes_used = max(0, usage.media_bytes_used - attachment.size)
    attachment.deleted_at = datetime.now(UTC)


async def expired_pending_attachments(
    session: AsyncSession, *, limit: int = 100
) -> list[Attachment]:
    return list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.finalized_at.is_(None),
                Attachment.deleted_at.is_(None),
                Attachment.upload_expires_at <= func.now(),
            )
            .order_by(Attachment.upload_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )


async def attachments_for_messages(
    session: AsyncSession, refs: set[tuple[int, str]]
) -> dict[tuple[int, str], list[Attachment]]:
    if not refs:
        return {}
    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id.is_not(None),
                Attachment.message_domain.is_not(None),
                Attachment.deleted_at.is_(None),
                tuple_(Attachment.message_id, Attachment.message_domain).in_(refs),
            )
            .order_by(Attachment.id)
        )
    )
    result: dict[tuple[int, str], list[Attachment]] = {}
    for attachment in rows:
        if attachment.message_id is not None and attachment.message_domain is not None:
            result.setdefault((attachment.message_id, attachment.message_domain), []).append(
                attachment
            )
    return result


async def delete_attachment_row(session: AsyncSession, attachment: Attachment) -> None:
    await session.execute(
        delete(Attachment).where(
            Attachment.id == attachment.id,
            Attachment.origin_domain == attachment.origin_domain,
        )
    )
