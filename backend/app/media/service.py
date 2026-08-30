from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.dm_capability import usable_dm_capability
from app.bots.installations import usable_guild_installation, usable_user_installation
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotUserInstallation,
)
from app.db.models import Attachment, User, UserStorageUsage
from app.media.digest_revocation import (
    DIGEST_REVOCATION_STATUSES,
    try_lock_asset_digest,
    valid_content_digest,
)
from app.media.payloads import attachment_payload as render_attachment_payload
from app.media.processing import IMAGE_TYPES, normalize_declared_type, sanitize_filename
from app.media.schemas import validate_voice_attachment_metadata
from app.media.storage import S3Storage, StorageError, media_url_origin

MEDIA_ORIGIN_HEADER = "X-Kaede-Media-Origin"

# Guild-authority uploads issued on behalf of remote human members are not
# charged to a local user-storage row.  Keep the closed purpose allowlist in
# one place so ticket and finalize accounting cannot drift apart as new
# federated guild assets are added.
FEDERATED_GUILD_UPLOAD_PURPOSES = (
    "emoji",
    "guild_banner",
    "guild_icon",
    "role_icon",
    "scheduled_event_image",
    "soundboard",
    "sticker",
    "webhook_attachment",
    "webhook_avatar",
)
FEDERATED_APPLICATION_UPLOAD_PURPOSES = (
    "application_asset",
    "application_emoji",
)
FEDERATED_AUTHORITY_UPLOAD_PURPOSES = (
    *FEDERATED_GUILD_UPLOAD_PURPOSES,
    *FEDERATED_APPLICATION_UPLOAD_PURPOSES,
)


def is_federated_human_authority_upload(user: User, settings: Settings) -> bool:
    """Return whether a local authority is staging media for a remote human.

    Remote actors have no local user-storage ledger at the resource authority.
    Every authority-owned media feature must make the same explicit decision
    before opting into the separately quota-bounded federated upload path.
    """

    return (
        user.account_type == "human" and user.origin_domain != settings.domain and not user.is_local
    )


async def lock_federated_authority_upload_quota(
    session: AsyncSession,
    user: User,
) -> None:
    """Serialize the shared remote-human media quota without a local ledger row."""

    scope = f"kaede-federated-authority-upload:{user.origin_domain}:{user.id}"
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(scope, 0))))


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


def require_image_type(content_type: str | None) -> None:
    if content_type not in IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "IMAGE_ASSET_TYPE_REQUIRED"},
        )


def require_sticker_type(content_type: str | None) -> None:
    # PNG includes APNG on the wire. GIF is Discord's other raster sticker
    # format; Lottie needs a dedicated vector renderer and is not treated as
    # arbitrary JSON by the generic attachment pipeline.
    if content_type not in {"image/png", "image/gif"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={"code": "STICKER_ASSET_TYPE_REQUIRED"},
        )


def require_sticker_asset(attachment: Attachment) -> None:
    require_sticker_type(attachment.detected_content_type)
    variant = attachment.variants.get("thumbnail_512")
    if (
        (attachment.width, attachment.height) != (320, 320)
        or not isinstance(variant, dict)
        or not isinstance(variant.get("animated"), bool)
    ):
        raise HTTPException(status_code=409, detail={"code": "STICKER_MEDIA_INVALID"})
    if variant["animated"] and (
        isinstance(variant.get("duration_ms"), bool)
        or not isinstance(variant.get("duration_ms"), int)
        or not 1 <= variant["duration_ms"] <= 5_000
    ):
        raise HTTPException(status_code=409, detail={"code": "STICKER_MEDIA_INVALID"})


def ticket_payload(attachment: Attachment, upload_url: str) -> dict[str, object]:
    return {
        **attachment_payload(attachment),
        "upload_url": upload_url,
        "media_origin": media_url_origin(upload_url, allow_http=True),
        "upload_method": "PUT",
        "expires_at": (
            attachment.upload_expires_at.isoformat()
            if attachment.upload_expires_at is not None
            else None
        ),
    }


def media_redirect_response(url: str) -> RedirectResponse:
    """Return a redirect with the exact authority-attested storage origin."""

    response = RedirectResponse(url, status_code=302)
    response.headers[MEDIA_ORIGIN_HEADER] = media_url_origin(url, allow_http=True)
    return response


def attachment_variant_is_animated(attachment: Attachment, variant_name: str) -> bool:
    """Use decoder-derived frame metadata instead of guessing from MIME."""

    variant = attachment.variants.get(variant_name)
    return isinstance(variant, dict) and variant.get("animated") is True


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
            usable_guild_installation(),
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


async def locked_bot_user_installation(
    session: AsyncSession,
    user: User,
    installation_id: int,
    *,
    authority_domain: str,
) -> BotUserInstallation:
    """Lock the private-response quota owned by a user installation."""

    row = (
        await session.execute(
            select(BotUserInstallation, BotApplication)
            .join(
                BotApplication,
                (BotApplication.id == BotUserInstallation.application_id)
                & (BotApplication.origin_domain == BotUserInstallation.application_domain),
            )
            .where(
                BotUserInstallation.id == installation_id,
                usable_user_installation(current_instance_domain=authority_domain),
            )
            .with_for_update(of=BotUserInstallation)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    installation = cast(BotUserInstallation, row[0])
    application = cast(BotApplication, row[1])
    if (application.bot_user_id, application.bot_user_domain) != (
        user.id,
        user.origin_domain,
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    return installation


async def locked_bot_dm_capability(
    session: AsyncSession,
    user: User,
    capability_id: int,
) -> BotDMCapability:
    """Lock a live C-bound DM capability as its own quota/revocation ledger."""

    capability = await session.scalar(
        select(BotDMCapability)
        .where(
            BotDMCapability.id == capability_id,
            BotDMCapability.bot_user_id == user.id,
            BotDMCapability.bot_user_domain == user.origin_domain,
            BotDMCapability.conversation_id.is_not(None),
            usable_dm_capability(at=datetime.now(UTC)),
        )
        .with_for_update()
    )
    if capability is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"})
    return capability


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
    duration_secs: float | None = None,
    waveform: str | None = None,
    bot_installation: BotInstallation | BotDMCapability | None = None,
    bot_user_installation: BotUserInstallation | None = None,
    media_transform: dict[str, object] | None = None,
    report_id: int | None = None,
    federated_guild_upload: bool = False,
    federated_application_upload: bool = False,
) -> tuple[Attachment, str]:
    if bot_installation is not None and bot_user_installation is not None:
        raise ValueError("an upload ticket can have only one bot installation owner")
    if federated_guild_upload and federated_application_upload:
        raise ValueError("an upload ticket can have only one federated authority owner")
    federated_upload_purposes = (
        FEDERATED_GUILD_UPLOAD_PURPOSES
        if federated_guild_upload
        else FEDERATED_APPLICATION_UPLOAD_PURPOSES
    )
    federated_authority_upload = federated_guild_upload or federated_application_upload
    if federated_authority_upload and (
        user.origin_domain == settings.domain
        or user.is_local
        or bot_installation is not None
        or bot_user_installation is not None
        or report_id is not None
        or purpose not in federated_upload_purposes
    ):
        raise ValueError("federated authority upload ownership is invalid")
    if federated_authority_upload:
        await lock_federated_authority_upload_quota(session, user)
    if size <= 0 or size > settings.media_max_attachment_bytes:
        raise HTTPException(status_code=413, detail={"code": "ATTACHMENT_TOO_LARGE"})
    declared_type = normalize_declared_type(content_type)
    validate_voice_attachment_metadata(
        content_type=declared_type,
        encryption_mode=encryption_mode,
        duration_secs=duration_secs,
        waveform=waveform,
    )
    remote_report_evidence = report_id is not None and (
        user.origin_domain != settings.domain or not user.is_local
    )
    usage: UserStorageUsage | BotInstallation | BotUserInstallation | BotDMCapability | None
    if remote_report_evidence or federated_authority_upload:
        usage = None
        pending_query = (
            select(func.count())
            .select_from(Attachment)
            .where(
                Attachment.uploader_id == user.id,
                Attachment.uploader_domain == user.origin_domain,
                (
                    Attachment.report_id.is_not(None)
                    if remote_report_evidence
                    else Attachment.purpose.in_(FEDERATED_AUTHORITY_UPLOAD_PURPOSES)
                ),
            )
        )
        pending_bytes = int(
            await session.scalar(
                select(func.coalesce(func.sum(Attachment.size), 0)).where(
                    Attachment.uploader_id == user.id,
                    Attachment.uploader_domain == user.origin_domain,
                    (
                        Attachment.report_id.is_not(None)
                        if remote_report_evidence
                        else Attachment.purpose.in_(FEDERATED_AUTHORITY_UPLOAD_PURPOSES)
                    ),
                    Attachment.finalized_at.is_(None),
                    Attachment.deleted_at.is_(None),
                    Attachment.upload_expires_at > func.now(),
                )
            )
            or 0
        )
        bytes_used = int(
            await session.scalar(
                select(func.coalesce(func.sum(Attachment.size), 0)).where(
                    Attachment.uploader_id == user.id,
                    Attachment.uploader_domain == user.origin_domain,
                    (
                        Attachment.report_id.is_not(None)
                        if remote_report_evidence
                        else Attachment.purpose.in_(FEDERATED_AUTHORITY_UPLOAD_PURPOSES)
                    ),
                    Attachment.finalized_at.is_not(None),
                    Attachment.deleted_at.is_(None),
                )
            )
            or 0
        )
    elif bot_installation is None and bot_user_installation is None:
        human_usage = await locked_usage(session, settings, user)
        usage = human_usage
        pending_query = (
            select(func.count())
            .select_from(Attachment)
            .where(
                Attachment.uploader_id == user.id,
                Attachment.uploader_domain == user.origin_domain,
                Attachment.bot_installation_id.is_(None),
                Attachment.bot_user_installation_id.is_(None),
                Attachment.bot_dm_capability_id.is_(None),
            )
        )
        pending_bytes = human_usage.pending_bytes
        bytes_used = human_usage.bytes_used
    elif bot_installation is not None:
        bot_installation_usage = (
            await locked_bot_dm_capability(session, user, bot_installation.id)
            if isinstance(bot_installation, BotDMCapability)
            else await locked_bot_installation(session, user, bot_installation.id)
        )
        usage = bot_installation_usage
        owner_condition = (
            Attachment.bot_dm_capability_id == bot_installation_usage.id
            if isinstance(bot_installation_usage, BotDMCapability)
            else Attachment.bot_installation_id == bot_installation_usage.id
        )
        pending_query = select(func.count()).select_from(Attachment).where(owner_condition)
        pending_bytes = bot_installation_usage.media_pending_bytes
        bytes_used = bot_installation_usage.media_bytes_used
    else:
        if bot_user_installation is None:
            raise RuntimeError("user installation upload owner unexpectedly disappeared")
        user_installation_usage = await locked_bot_user_installation(
            session,
            user,
            bot_user_installation.id,
            authority_domain=settings.domain,
        )
        usage = user_installation_usage
        pending_query = (
            select(func.count())
            .select_from(Attachment)
            .where(Attachment.bot_user_installation_id == user_installation_usage.id)
        )
        pending_bytes = user_installation_usage.media_pending_bytes
        bytes_used = user_installation_usage.media_bytes_used
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
        bot_user_installation_id=(usage.id if isinstance(usage, BotUserInstallation) else None),
        bot_dm_capability_id=(usage.id if isinstance(usage, BotDMCapability) else None),
        filename=sanitize_filename(filename),
        content_type=declared_type,
        size=size,
        duration_secs=duration_secs,
        waveform=waveform,
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
    elif isinstance(usage, (BotInstallation, BotUserInstallation, BotDMCapability)):
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
    federated_guild_upload: bool = False,
    federated_application_upload: bool = False,
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
    if federated_guild_upload and federated_application_upload:
        raise HTTPException(status_code=403, detail={"code": "ATTACHMENT_NOT_OWNED"})
    federated_upload_purposes = (
        FEDERATED_GUILD_UPLOAD_PURPOSES
        if federated_guild_upload
        else FEDERATED_APPLICATION_UPLOAD_PURPOSES
    )
    federated_authority_upload = federated_guild_upload or federated_application_upload
    if federated_authority_upload and (
        user.origin_domain == settings.domain
        or user.is_local
        or attachment.bot_installation_id is not None
        or attachment.bot_user_installation_id is not None
        or attachment.bot_dm_capability_id is not None
        or attachment.report_id is not None
        or attachment.purpose not in federated_upload_purposes
    ):
        raise HTTPException(status_code=403, detail={"code": "ATTACHMENT_NOT_OWNED"})
    if federated_authority_upload:
        await lock_federated_authority_upload_quota(session, user)
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
    remote_report_evidence = attachment.report_id is not None and (
        user.origin_domain != settings.domain or not user.is_local
    )
    usage: UserStorageUsage | BotInstallation | BotUserInstallation | BotDMCapability | None
    if remote_report_evidence or federated_authority_upload:
        usage = None
        pending_bytes = int(
            await session.scalar(
                select(func.coalesce(func.sum(Attachment.size), 0)).where(
                    Attachment.uploader_id == user.id,
                    Attachment.uploader_domain == user.origin_domain,
                    (
                        Attachment.report_id.is_not(None)
                        if remote_report_evidence
                        else Attachment.purpose.in_(FEDERATED_AUTHORITY_UPLOAD_PURPOSES)
                    ),
                    Attachment.finalized_at.is_(None),
                    Attachment.deleted_at.is_(None),
                    Attachment.upload_expires_at > func.now(),
                )
            )
            or 0
        )
        bytes_used = int(
            await session.scalar(
                select(func.coalesce(func.sum(Attachment.size), 0)).where(
                    Attachment.uploader_id == user.id,
                    Attachment.uploader_domain == user.origin_domain,
                    (
                        Attachment.report_id.is_not(None)
                        if remote_report_evidence
                        else Attachment.purpose.in_(FEDERATED_AUTHORITY_UPLOAD_PURPOSES)
                    ),
                    Attachment.finalized_at.is_not(None),
                    Attachment.deleted_at.is_(None),
                )
            )
            or 0
        )
    elif (
        attachment.bot_installation_id is None
        and attachment.bot_user_installation_id is None
        and attachment.bot_dm_capability_id is None
    ):
        human_usage = await locked_usage(session, settings, user)
        usage = human_usage
        pending_bytes = human_usage.pending_bytes
        bytes_used = human_usage.bytes_used
    elif attachment.bot_installation_id is not None:
        bot_installation_usage = await locked_bot_installation(
            session, user, attachment.bot_installation_id
        )
        usage = bot_installation_usage
        pending_bytes = bot_installation_usage.media_pending_bytes
        bytes_used = bot_installation_usage.media_bytes_used
    elif attachment.bot_dm_capability_id is not None:
        dm_capability_usage = await locked_bot_dm_capability(
            session,
            user,
            attachment.bot_dm_capability_id,
        )
        usage = dm_capability_usage
        pending_bytes = dm_capability_usage.media_pending_bytes
        bytes_used = dm_capability_usage.media_bytes_used
    else:
        if attachment.bot_user_installation_id is None:
            raise RuntimeError("user installation attachment owner unexpectedly disappeared")
        user_installation_usage = await locked_bot_user_installation(
            session,
            user,
            attachment.bot_user_installation_id,
            authority_domain=settings.domain,
        )
        usage = user_installation_usage
        pending_bytes = user_installation_usage.media_pending_bytes
        bytes_used = user_installation_usage.media_bytes_used
    if pending_bytes < attachment.size:
        raise RuntimeError("pending storage accounting underflow")
    if bytes_used + attachment.size > settings.media_user_quota_bytes:
        raise HTTPException(status_code=413, detail={"code": "USER_STORAGE_QUOTA_EXCEEDED"})
    if isinstance(usage, UserStorageUsage):
        usage.pending_bytes -= attachment.size
        usage.bytes_used += attachment.size
    elif isinstance(usage, (BotInstallation, BotUserInstallation, BotDMCapability)):
        usage.media_pending_bytes -= attachment.size
        usage.media_bytes_used += attachment.size
    attachment.finalized_at = now
    return attachment


async def bind_asset(
    session: AsyncSession, attachment: Attachment, binding: str
) -> Attachment | None:
    if (
        attachment.message_id is not None
        or attachment.message_domain is not None
        or attachment.interaction_id is not None
        or attachment.interaction_response_id is not None
        or attachment.report_id is not None
    ):
        raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
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
    usage: UserStorageUsage | BotInstallation | BotUserInstallation | BotDMCapability | None
    if (
        attachment.bot_installation_id is None
        and attachment.bot_user_installation_id is None
        and attachment.bot_dm_capability_id is None
    ):
        usage = await session.scalar(
            select(UserStorageUsage)
            .where(
                UserStorageUsage.user_id == attachment.uploader_id,
                UserStorageUsage.user_domain == attachment.uploader_domain,
            )
            .with_for_update()
        )
    elif attachment.bot_installation_id is not None:
        usage = await session.scalar(
            select(BotInstallation)
            .where(BotInstallation.id == attachment.bot_installation_id)
            .with_for_update()
        )
    elif attachment.bot_dm_capability_id is not None:
        usage = await session.scalar(
            select(BotDMCapability)
            .where(BotDMCapability.id == attachment.bot_dm_capability_id)
            .with_for_update()
        )
    else:
        usage = await session.scalar(
            select(BotUserInstallation)
            .where(BotUserInstallation.id == attachment.bot_user_installation_id)
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
