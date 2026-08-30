from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any, cast

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import (
    validate_channel_encryption_policy,
    validate_channel_encryption_policy_transition,
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_message_encryption_policy,
)
from app.chat.e2ee_controls import apply_e2ee_control_metadata
from app.chat.events import publish_dispatch, user_topic
from app.chat.message_flags import MESSAGE_FLAG_HAS_SNAPSHOT
from app.chat.message_references import validate_message_reference_projection
from app.chat.payloads import dm_channel_payload, render_message_payload
from app.chat.pins import PIN_NOTICE_MESSAGE_TYPE, message_is_pinnable
from app.chat.poll_results import (
    POLL_RESULT_MESSAGE_TYPE,
    validate_poll_result_wire_body,
)
from app.chat.privacy import require_can_direct_message
from app.core.dm import (
    GROUP_DM_MEMBER_ADDED,
    GROUP_DM_MEMBER_LEFT,
    GROUP_DM_MEMBER_REMOVED,
    MAX_GROUP_DM_PARTICIPANTS,
    group_dm_key,
    group_dm_notice_text,
)
from app.core.settings import Settings
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.bot_models import BotApplication
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Instance,
    MediaTombstoneSource,
    Message,
    MessageProjection,
    MessageView,
    Poll,
    RemoteMediaTombstone,
    TerminalRoomDeletion,
    User,
)
from app.federation.dm_storage import (
    admit_federated_dm_conversation,
    admit_federated_dm_message,
    dm_authority_history_available,
    dm_history_metadata,
    dm_message_storage_delta,
    lock_federated_dm_authority,
    opaque_dm_history_ref_allowed,
    register_federated_dm_conversation,
)
from app.federation.events import retained_media_delete_events
from app.federation.identity_storage import admit_remote_user_identity
from app.federation.message_content import (
    add_poll_projection,
    stored_poll_matches_projection,
    stored_view_matches_projection,
    validate_replicated_rich_projection,
)
from app.federation.network import ensure_remote_instance_record, normalize_domain
from app.federation.schemas import MAX_DATABASE_SNOWFLAKE, RemoteUserProfile
from app.media.processing import normalize_declared_type, sanitize_filename
from app.media.schemas import validate_voice_attachment_metadata


def database_snowflake(value: object, field: str) -> int:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal snowflake")
    rendered = value
    if (
        not rendered
        or not rendered.isascii()
        or not rendered.isdecimal()
        or (len(rendered) > 1 and rendered.startswith("0"))
    ):
        raise ValueError(f"{field} must be a decimal snowflake")
    parsed = int(rendered)
    if parsed > MAX_DATABASE_SNOWFLAKE:
        raise ValueError(f"{field} is outside the database range")
    return parsed


def replicated_message_create_fingerprint(
    *,
    channel_id: int,
    channel_domain: str,
    author_id: int,
    author_domain: str,
    content: str | None,
    e2ee: dict[str, Any] | None,
    message_type: int,
    flags: int,
    client_nonce: str | None,
    referenced_message_id: int | None,
    referenced_message_domain: str | None,
    mention_user_refs: list[dict[str, Any]],
    mention_role_refs: list[dict[str, Any]] | None = None,
    mention_everyone: bool = False,
    created_at: datetime,
    tts: bool = False,
    webhook_name: str | None = None,
    webhook_id: int | None = None,
    webhook_domain: str | None = None,
    webhook_avatar_hash: str | None = None,
    webhook_avatar_url: str | None = None,
    embeds: list[dict[str, Any]] | None = None,
    components: list[dict[str, Any]] | None = None,
    application_id: int | None = None,
    application_domain: str | None = None,
    interaction_metadata: dict[str, Any] | None = None,
    view_version: int = 0,
    forwarded_message_id: int | None = None,
    forwarded_message_domain: str | None = None,
    forwarded_channel_id: int | None = None,
    forwarded_channel_domain: str | None = None,
    forward_snapshot: dict[str, Any] | None = None,
    poll_result: dict[str, Any] | None = None,
    sticker_items: list[dict[str, Any]] | None = None,
    message_reference: dict[str, Any] | None = None,
) -> tuple[object, ...]:
    """Bind immutable create fields, including opaque E2EE ciphertext."""

    return (
        channel_id,
        channel_domain,
        author_id,
        author_domain,
        content,
        e2ee,
        message_type,
        tts,
        flags,
        client_nonce,
        referenced_message_id,
        referenced_message_domain,
        message_reference,
        mention_user_refs,
        mention_role_refs or [],
        mention_everyone,
        webhook_name,
        webhook_id,
        webhook_domain,
        webhook_avatar_hash,
        webhook_avatar_url,
        embeds or [],
        components or [],
        application_id,
        application_domain,
        interaction_metadata,
        view_version,
        forwarded_message_id,
        forwarded_message_domain,
        forwarded_channel_id,
        forwarded_channel_domain,
        forward_snapshot,
        poll_result,
        sticker_items or [],
        created_at,
    )


def authoritative_dm_control(
    e2ee: dict[str, Any] | None,
    *,
    message_type: int,
    flags: int,
    message_origin: str,
    event_origin: str,
    conversation_authority: str,
) -> tuple[bool, bool]:
    """Return whether a message is a control and satisfies every authority binding."""

    is_control = e2ee is not None and e2ee.get("operation") in {"welcome", "commit"}
    return is_control, bool(
        is_control
        and message_type == 7
        and flags == 4
        and message_origin == event_origin
        and event_origin == conversation_authority
    )


def validate_snowflake_timestamp(
    identifier: int,
    created_at: datetime,
    field: str,
    *,
    event_timestamp_ms: int,
    tolerance_ms: int = 60_000,
) -> None:
    """Bind an untrusted snowflake to its signed creation timestamp.

    A peer-controlled far-future ID could otherwise pin a channel cursor ahead
    of every legitimate message (or route an insert into a partition that should
    not exist yet). The timestamp and identifier travel inside the independently
    signed envelope, so requiring them to agree preserves delayed delivery while
    preventing cursor/partition poisoning.
    """

    if created_at.tzinfo is None:
        raise ValueError(f"{field} timestamp must include a timezone")
    embedded_ms = EPOCH_MS + (identifier >> (WORKER_BITS + SEQUENCE_BITS))
    created_ms = int(created_at.timestamp() * 1000)
    if abs(embedded_ms - created_ms) > tolerance_ms:
        raise ValueError(f"{field} snowflake timestamp does not match created_at")
    if abs(created_ms - event_timestamp_ms) > tolerance_ms:
        raise ValueError(f"{field} created_at does not match the signed event timestamp")


async def advance_channel_cursor(
    session: AsyncSession,
    channel: Channel,
    message_id: int,
    message_domain: str,
) -> None:
    """Atomically advance a composite message cursor without last-writer regression."""

    await session.execute(
        update(Channel)
        .where(
            Channel.id == channel.id,
            Channel.origin_domain == channel.origin_domain,
            or_(
                Channel.last_message_id.is_(None),
                Channel.last_message_domain.is_(None),
                Channel.last_message_id < message_id,
                and_(
                    Channel.last_message_id == message_id,
                    Channel.last_message_domain < message_domain,
                ),
            ),
        )
        .values(last_message_id=message_id, last_message_domain=message_domain)
        .execution_options(synchronize_session=False)
    )


UNRESOLVED_REMOTE_USERNAME_ATTEMPTS = 8


def unresolved_remote_username(_user_id: int | None = None, _origin: str | None = None) -> str:
    """Return an opaque, random local-only handle within the 32-byte schema bound.

    The arguments remain accepted for rolling compatibility with older callers,
    but deliberately do not influence the alias. A deterministic alias lets a
    malicious peer predict and reserve another identity's placeholder handle.
    ``profile_resolved`` is the only marker clients may use to identify these
    rows; the alias is an internal database implementation detail.
    """

    return f"history_{secrets.token_hex(12)}"


async def insert_unresolved_remote_user(
    session: AsyncSession,
    *,
    user_id: int,
    origin_domain: str,
    introduced_by_domain: str,
) -> User:
    """Insert an opaque remote identity without letting alias collisions abort work.

    ``ON CONFLICT DO NOTHING`` intentionally has no conflict target, so both a
    concurrent composite-ID insert and an unlucky username collision remain
    recoverable. The latter generates a fresh random alias and retries within a
    fixed bound rather than poisoning the surrounding snapshot transaction.
    """

    identity = (user_id, origin_domain)
    existing = await session.get(User, identity)
    if existing is not None:
        return existing
    for _attempt in range(UNRESOLVED_REMOTE_USERNAME_ATTEMPTS):
        await session.execute(
            pg_insert(User)
            .values(
                id=user_id,
                origin_domain=origin_domain,
                is_local=False,
                username=unresolved_remote_username(),
                profile_version=1,
                profile_resolved=False,
                federation_introduced_by_domain=introduced_by_domain,
            )
            .on_conflict_do_nothing()
        )
        inserted = await session.get(User, identity)
        if inserted is not None:
            return inserted
    raise RuntimeError("unresolved remote identity insert did not converge")


async def _resolve_unresolved_remote_profile(
    session: AsyncSession,
    user: User,
    profile: RemoteUserProfile,
) -> bool:
    """Apply an authoritative profile without sacrificing snapshot availability.

    A user's home is authoritative for the composite identity, but a duplicate
    case-insensitive handle from that home must not violate our local uniqueness
    invariant and roll back an otherwise valid guild snapshot. Preflight the
    known conflict and retain the placeholder. The savepoint also contains the
    narrow concurrent-insert race so the caller can continue safely.
    """

    conflicting_id = await session.scalar(
        select(User.id)
        .where(
            User.origin_domain == user.origin_domain,
            func.lower(User.username) == profile.username.lower(),
            User.id != user.id,
        )
        .limit(1)
    )
    if conflicting_id is not None:
        return False
    try:
        async with session.begin_nested():
            user.username = profile.username
            user.account_type = profile.account_type
            user.profile_resolved = True
            user.profile_version = profile.profile_version
            user.e2ee_device_generation = profile.e2ee_device_generation
            user.display_name = profile.display_name
            user.avatar_hash = profile.avatar_hash
            user.banner_hash = profile.banner_hash
            user.bio = profile.bio
            user.custom_status = profile.custom_status
            await session.flush()
    except IntegrityError:
        # The savepoint keeps the outer import/refresh transaction usable. The
        # row is expired by the rollback; refresh it before returning it to a
        # caller that may render or account for the still-opaque identity.
        await session.refresh(user)
        return False
    return True


async def upsert_remote_user(
    session: AsyncSession, settings: Settings, profile: RemoteUserProfile
) -> User:
    if profile.origin_domain == settings.domain:
        user = await session.get(User, (database_snowflake(profile.id, "user id"), settings.domain))
        if user is None or not user.is_local:
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        if user.username != profile.username:
            raise ValueError("local user profile does not match the stored immutable handle")
        return user
    user_id = database_snowflake(profile.id, "user id")
    user, introducer = await admit_remote_user_identity(
        session,
        settings,
        user_id,
        profile.origin_domain,
        introduced_by_domain=profile.origin_domain,
    )
    if user is None:
        await session.execute(
            pg_insert(User)
            .values(
                id=user_id,
                origin_domain=profile.origin_domain,
                is_local=False,
                username=profile.username,
                account_type=profile.account_type,
                display_name=profile.display_name,
                avatar_hash=profile.avatar_hash,
                banner_hash=profile.banner_hash,
                bio=profile.bio,
                custom_status=profile.custom_status,
                profile_version=profile.profile_version,
                e2ee_device_generation=profile.e2ee_device_generation,
                federation_introduced_by_domain=introducer,
            )
            .on_conflict_do_nothing()
        )
        user = await session.get(User, (user_id, profile.origin_domain))
    if user is None:
        # The home may have supplied a case-insensitive username already bound
        # to another composite ID. Preserve the identity as opaque rather than
        # allowing that conflict to abort an otherwise valid snapshot/event.
        user = await insert_unresolved_remote_user(
            session,
            user_id=user_id,
            origin_domain=profile.origin_domain,
            introduced_by_domain=introducer,
        )
    if user.is_local:
        raise ValueError("remote user profile changed an immutable identity")
    if not user.profile_resolved:
        await _resolve_unresolved_remote_profile(session, user, profile)
        return user
    if user.username != profile.username:
        raise ValueError("remote user profile changed an immutable identity")
    if user.account_type != profile.account_type:
        raise ValueError("remote user profile changed an immutable account type")
    user.e2ee_device_generation = max(
        user.e2ee_device_generation,
        profile.e2ee_device_generation,
    )
    if profile.profile_version < user.profile_version:
        return user
    mutable_profile = (
        profile.display_name,
        profile.avatar_hash,
        profile.banner_hash,
        profile.bio,
        profile.custom_status,
    )
    stored_profile = (
        user.display_name,
        user.avatar_hash,
        user.banner_hash,
        user.bio,
        user.custom_status,
    )
    if profile.profile_version == user.profile_version and mutable_profile != stored_profile:
        raise ValueError("remote user profile equivocated at an existing version")
    user.display_name = profile.display_name
    user.avatar_hash = profile.avatar_hash
    user.banner_hash = profile.banner_hash
    user.bio = profile.bio
    user.custom_status = profile.custom_status
    user.profile_version = profile.profile_version
    return user


async def resolve_delegated_profile(
    session: AsyncSession,
    settings: Settings,
    profile: RemoteUserProfile,
    *,
    authority_origin: str,
) -> User:
    """Resolve a profile carried by an authority other than the user's home.

    A guild home is authoritative for its guild, but it is not authoritative for
    mutable profiles homed on a third instance.  Profiles belonging to the guild
    home can be upserted normally, and local profiles are resolved against the
    local database without accepting remote mutations. Every other profile may
    identify an existing authoritative cache entry. If it has not been learned
    from its own origin yet, retain only an opaque composite reference with a
    locally derived placeholder handle; never accept the guild home's mutable
    third-party profile fields.
    """

    authority = normalize_domain(authority_origin)
    if profile.origin_domain in {authority, settings.domain}:
        return await upsert_remote_user(session, settings, profile)

    user_id = database_snowflake(profile.id, "user id")
    user = await session.get(User, (user_id, profile.origin_domain))
    if user is None:
        # Preserve the guild membership as an opaque composite identity. The
        # guild home is not allowed to choose this user's handle/profile; a
        # later direct response from the user's own home upgrades the row.
        existing, introducer = await admit_remote_user_identity(
            session,
            settings,
            user_id,
            profile.origin_domain,
            introduced_by_domain=authority,
        )
        user = existing or await insert_unresolved_remote_user(
            session,
            user_id=user_id,
            origin_domain=profile.origin_domain,
            introduced_by_domain=introducer,
        )
    if user is None:
        raise RuntimeError("unresolved third-party identity insert did not converge")
    if user.is_local:
        raise ValueError("third-party profile does not match its authoritative identity")
    if user.profile_resolved and user.username != profile.username:
        raise ValueError("third-party profile does not match its authoritative identity")
    return user


MAX_REMOTE_MEDIA_DIMENSION = 100_000
MAX_REMOTE_MEDIA_PIXELS = 100_000_000
REMOTE_MEDIA_VARIANTS = {
    "thumbnail_128",
    "thumbnail_512",
    "thumbnail_1024",
    "poster",
}


def remote_media_dimensions(raw: dict[str, Any]) -> tuple[int | None, int | None]:
    width = raw.get("width")
    height = raw.get("height")
    if (width is None) != (height is None):
        raise ValueError("attachment dimensions must either both be present or both be absent")
    if width is None:
        return None, None
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not 1 <= width <= MAX_REMOTE_MEDIA_DIMENSION
        or not 1 <= height <= MAX_REMOTE_MEDIA_DIMENSION
        or width * height > MAX_REMOTE_MEDIA_PIXELS
    ):
        raise ValueError("attachment dimensions are invalid")
    return width, height


def sanitized_remote_variants(raw: object, *, max_bytes: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or len(raw) > 16:
        raise ValueError("attachment variants are invalid")
    rendered: dict[str, Any] = {}
    for name, metadata in raw.items():
        if name not in REMOTE_MEDIA_VARIANTS:
            continue
        if not isinstance(metadata, dict) or len(metadata) > 16:
            raise ValueError("attachment variant metadata is invalid")
        content_type_raw = metadata.get("content_type")
        size = metadata.get("size")
        if not isinstance(content_type_raw, str):
            raise ValueError("attachment variant content type is invalid")
        content_type = normalize_declared_type(content_type_raw)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= max_bytes:
            raise ValueError("attachment variant size is invalid")
        width, height = remote_media_dimensions(metadata)
        processing_version = metadata.get("processing_version")
        if processing_version is not None and (
            isinstance(processing_version, bool)
            or not isinstance(processing_version, int)
            or not 1 <= processing_version <= 2_147_483_647
        ):
            raise ValueError("attachment variant processing version is invalid")
        animated = metadata.get("animated")
        if animated is not None and not isinstance(animated, bool):
            raise ValueError("attachment variant animation state is invalid")
        duration_ms = metadata.get("duration_ms")
        if duration_ms is not None and (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not 1 <= duration_ms <= 3_600_000
        ):
            raise ValueError("attachment variant duration is invalid")
        if duration_ms is not None and animated is not True:
            raise ValueError("attachment variant duration requires animation")
        rendered[name] = {
            "content_type": content_type,
            "size": size,
            "width": width,
            "height": height,
            **(
                {"processing_version": processing_version} if processing_version is not None else {}
            ),
            **({"animated": animated} if animated is not None else {}),
            **({"duration_ms": duration_ms} if duration_ms is not None else {}),
        }
    return rendered


def sanitized_remote_blurhash(raw: object) -> str | None:
    if raw is None:
        return None
    if (
        not isinstance(raw, str)
        or not 6 <= len(raw) <= 128
        or not raw.isascii()
        or any(not 33 <= ord(character) <= 126 for character in raw)
    ):
        raise ValueError("attachment blurhash is invalid")
    return raw


def profile_from_user(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "origin_domain": user.origin_domain,
        "account_type": user.account_type,
        "username": user.username,
        "display_name": user.display_name,
        "avatar_hash": user.avatar_hash,
        "banner_hash": user.banner_hash,
        "bio": user.bio,
        "custom_status": user.custom_status,
        "profile_version": user.profile_version,
        "e2ee_device_generation": getattr(user, "e2ee_device_generation", 0),
    }


def client_user_payload_from_profile(profile: RemoteUserProfile) -> dict[str, object]:
    """Project a strict federation profile onto the public client user shape."""

    return {
        **profile.model_dump(mode="json"),
        "profile_version": str(profile.profile_version),
        "e2ee_device_generation": str(profile.e2ee_device_generation),
        "profile_resolved": True,
        "handle": f"{profile.username}@{profile.origin_domain}",
        "bot": profile.account_type == "bot",
    }


async def replicate_message_attachments(
    session: AsyncSession,
    settings: Settings,
    message: Message,
    author: User,
    raw_attachments: object,
    *,
    allowed_attachment_origins: set[str] | None = None,
) -> list[Attachment]:
    """Validate stable remote attachment references without trusting scan claims or URLs."""

    if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
        raise ValueError("message attachment list is invalid")
    allowed_origins = allowed_attachment_origins or {author.origin_domain}
    seen: set[tuple[int, str]] = set()
    rendered: list[Attachment] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            raise ValueError("message attachment is invalid")
        attachment_id = database_snowflake(raw.get("id"), "attachment id")
        origin = normalize_domain(str(raw.get("origin_domain", "")))
        if origin not in allowed_origins:
            raise ValueError("attachment origin is not authorized for the message")
        ref = (attachment_id, origin)
        if ref in seen:
            raise ValueError("message attachment IDs must be unique")
        seen.add(ref)
        filename_raw = raw.get("filename")
        content_type_raw = raw.get("content_type")
        size = raw.get("size")
        if not isinstance(filename_raw, str) or sanitize_filename(filename_raw) != filename_raw:
            raise ValueError("attachment filename is invalid")
        if not isinstance(content_type_raw, str):
            raise ValueError("attachment content type is invalid")
        content_type = normalize_declared_type(content_type_raw)
        encryption_mode = raw.get("encryption_mode", "plaintext")
        encryption_protocol = raw.get("encryption_protocol")
        content_sha256 = raw.get("content_sha256")
        if encryption_mode == "e2ee":
            if (
                encryption_protocol != "kaede-file-v1"
                or filename_raw != "encrypted-file"
                or content_type != "application/octet-stream"
                or content_sha256 is not None
            ):
                raise ValueError("encrypted attachment metadata is invalid")
            replicated_scan_status = "encrypted"
        elif encryption_mode == "plaintext" and encryption_protocol is None:
            if content_sha256 is not None and (
                not isinstance(content_sha256, str)
                or len(content_sha256) != 64
                or any(character not in "0123456789abcdef" for character in content_sha256)
            ):
                raise ValueError("plaintext attachment integrity digest is invalid")
            replicated_scan_status = "clean"
        else:
            raise ValueError("attachment encryption policy is invalid")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= settings.media_max_attachment_bytes
        ):
            raise ValueError("attachment size is invalid")
        width, height = remote_media_dimensions(raw)
        blurhash = sanitized_remote_blurhash(raw.get("blurhash"))
        duration_secs_raw = raw.get("duration_secs")
        duration_secs = (
            float(duration_secs_raw)
            if isinstance(duration_secs_raw, (int, float))
            and not isinstance(duration_secs_raw, bool)
            else None
        )
        waveform = raw.get("waveform")
        if waveform is not None and not isinstance(waveform, str):
            raise ValueError("attachment waveform is invalid")
        if duration_secs_raw is not None and duration_secs is None:
            raise ValueError("attachment duration is invalid")
        validate_voice_attachment_metadata(
            content_type=content_type,
            encryption_mode=encryption_mode,
            duration_secs=duration_secs,
            waveform=waveform,
        )
        variants = sanitized_remote_variants(
            raw.get("variants", {}),
            max_bytes=settings.media_max_attachment_bytes,
        )
        existing = await session.get(Attachment, ref)
        tombstone = await session.get(RemoteMediaTombstone, (origin, attachment_id))
        durable_tombstone = await session.get(
            MediaTombstoneSource,
            (attachment_id, origin),
        )
        retained_tombstone = (
            bool(await retained_media_delete_events(session, attachment_id, origin))
            if tombstone is None and durable_tombstone is None
            else True
        )
        if retained_tombstone:
            # A signed authority tombstone can arrive before a delayed message
            # create or history replay. Never recreate or render that media.
            if existing is not None and existing.deleted_at is None:
                existing.deleted_at = datetime.now(UTC)
            continue
        if existing is None:
            existing = Attachment(
                id=attachment_id,
                origin_domain=origin,
                message_id=message.id,
                message_domain=message.origin_domain,
                uploader_id=author.id,
                uploader_domain=author.origin_domain,
                filename=filename_raw,
                content_type=content_type,
                size=size,
                object_key=f"remote/{origin}/{attachment_id}/original",
                width=width,
                height=height,
                duration_secs=duration_secs,
                waveform=waveform,
                blurhash=blurhash,
                scan_status=replicated_scan_status,
                encryption_mode=encryption_mode,
                encryption_protocol=encryption_protocol,
                purpose="attachment",
                finalized_at=message.created_at,
                variants=variants,
                content_sha256=content_sha256,
            )
            session.add(existing)
        else:
            if (
                existing.uploader_id,
                existing.uploader_domain,
                existing.filename,
                existing.content_type,
                existing.size,
                existing.encryption_mode,
                existing.encryption_protocol,
                existing.duration_secs,
                existing.waveform,
                existing.content_sha256,
            ) != (
                author.id,
                author.origin_domain,
                filename_raw,
                content_type,
                size,
                encryption_mode,
                encryption_protocol,
                duration_secs,
                waveform,
                content_sha256,
            ):
                raise ValueError("attachment identity conflicts with stored metadata")
            if existing.message_id is not None and (
                existing.message_id,
                existing.message_domain,
            ) != (message.id, message.origin_domain):
                raise ValueError("attachment is already bound to another message")
            existing.message_id = message.id
            existing.message_domain = message.origin_domain
        rendered.append(existing)
    return rendered


async def replicate_conversation(
    session: AsyncSession,
    settings: Settings,
    conversation: dict[str, Any],
    participant_profiles: list[RemoteUserProfile],
) -> Channel:
    origin = str(conversation["origin_domain"])
    pair_key = str(conversation["pair_key"])
    conversation_type = str(conversation.get("type", "direct"))
    authority_domain = str(conversation["authority_domain"])
    participant_domains = {profile.origin_domain for profile in participant_profiles}
    if conversation_type not in {"direct", "group"}:
        raise ValueError("unsupported DM conversation type")
    conversation_id = database_snowflake(conversation.get("id"), "conversation id")
    if conversation_type == "group":
        from app.federation.terminal_rooms import lock_terminal_room

        await lock_terminal_room(session, "group_dm", conversation_id, origin)
        if (
            await session.get(
                TerminalRoomDeletion,
                ("group_dm", conversation_id, origin, settings.domain),
                populate_existing=True,
            )
            is not None
        ):
            raise ValueError("a terminal group DM cannot be recreated")
    federated = await admit_federated_dm_conversation(
        session,
        settings,
        authority_domain=authority_domain,
        pair_key=pair_key,
        participant_domains=participant_domains,
        conversation_type=conversation_type,
    )
    if await session.get(Instance, origin) is None:
        if origin == settings.domain:
            raise RuntimeError("self instance is not bootstrapped")
        await ensure_remote_instance_record(session, settings, origin)
    participants = [
        await upsert_remote_user(session, settings, profile) for profile in participant_profiles
    ]
    participant_refs = {(participant.id, participant.origin_domain) for participant in participants}
    if len(participant_refs) != len(participants):
        raise ValueError("DM conversation participants must be unique")
    raw_encryption_policy = conversation.get("encryption_policy")
    if raw_encryption_policy is None:
        raw_encryption_policy = {
            "mode": "plaintext",
            "state": "plaintext",
            "generation": "0",
        }
    encryption_policy = validate_channel_encryption_policy(raw_encryption_policy)
    owner_id: int | None = None
    owner_domain: str | None = None
    state_version = 0
    channel_name: str | None = None
    if conversation_type == "group":
        if authority_domain != origin or not (1 <= len(participants) <= MAX_GROUP_DM_PARTICIPANTS):
            raise ValueError("group DM authority or participant count is invalid")
        if pair_key != group_dm_key(authority_domain, conversation_id):
            raise ValueError("group DM lookup key is invalid")
        owner = conversation.get("owner")
        if not isinstance(owner, dict):
            raise ValueError("group DM owner is missing")
        owner_id = database_snowflake(owner.get("id"), "group DM owner id")
        owner_domain = normalize_domain(str(owner.get("origin_domain", "")))
        if (owner_id, owner_domain) not in participant_refs:
            raise ValueError("group DM owner is not a participant")
        raw_name = conversation.get("name")
        if raw_name is not None:
            if not isinstance(raw_name, str) or not 1 <= len(raw_name.strip()) <= 100:
                raise ValueError("group DM name is invalid")
            channel_name = raw_name.strip()
        state_version = database_snowflake(
            conversation.get("state_version"), "group DM state version"
        )
        if state_version < 1:
            raise ValueError("group DM state version is invalid")
    await session.scalar(
        pg_insert(DMConversation)
        .values(
            id=conversation_id,
            origin_domain=origin,
            pair_key=pair_key,
            type=conversation_type,
            authority_domain=authority_domain,
            owner_id=owner_id,
            owner_domain=owner_domain,
            state_version=0,
        )
        .on_conflict_do_nothing(index_elements=["pair_key"])
        .returning(DMConversation.id)
    )
    existing = await session.scalar(
        select(DMConversation).where(DMConversation.pair_key == pair_key)
    )
    if existing is None:
        raise RuntimeError("replicated DM conversation insert did not converge")
    if (
        (existing.id, existing.origin_domain) != (conversation_id, origin)
        or existing.pair_key != pair_key
        or existing.authority_domain != authority_domain
        or existing.type != conversation_type
    ):
        raise ValueError("DM conversation identity conflicts with an existing conversation")
    if federated:
        await register_federated_dm_conversation(
            session,
            settings,
            existing,
            participant_domains=participant_domains,
        )
    channel = await session.get(Channel, (conversation_id, origin))
    stored_channel_name = channel.name if channel is not None else None
    if channel is None:
        channel = Channel(
            id=conversation_id,
            origin_domain=origin,
            guild_id=None,
            guild_domain=None,
            type=1,
            name=channel_name,
            position=0,
            rate_limit_per_user=0,
            created_floor_id=conversation_id,
        )
        session.add(channel)
        await session.flush()
    elif channel.guild_id is not None or channel.type != 1:
        raise ValueError("DM conversation ID conflicts with a non-DM channel")
    incoming_generation = int(encryption_policy["generation"])
    validate_channel_encryption_policy_transition(channel, encryption_policy, label="DM")
    channel.encryption_mode = str(encryption_policy["mode"])
    channel.encryption_state = str(encryption_policy["state"])
    channel.encryption_policy_generation = incoming_generation
    channel.encryption_protocol = encryption_policy["protocol"]
    channel.encryption_suite = encryption_policy["suite"]
    channel.encryption_group_id = encryption_policy["group_id"]
    channel.encryption_epoch = encryption_policy["epoch"]
    if conversation_type == "group":
        stored_participants = set(
            (
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == conversation_id,
                        DMParticipant.conversation_domain == origin,
                    )
                )
            ).tuples()
        )
        expected_participants = participant_refs
        if existing.state_version > state_version:
            return channel
        if existing.state_version == state_version and existing.state_version > 0:
            if (
                existing.owner_id,
                existing.owner_domain,
                stored_channel_name,
            ) != (owner_id, owner_domain, channel_name):
                raise ValueError("group DM state version conflicts with stored state")
            if stored_participants != expected_participants:
                raise ValueError("group DM state version conflicts with stored participants")
            return channel
        existing.owner_id = owner_id
        existing.owner_domain = owner_domain
        existing.state_version = state_version
        channel.name = channel_name
    for participant in participants:
        existing_participant = await session.get(
            DMParticipant,
            (conversation_id, origin, participant.id, participant.origin_domain),
        )
        if existing_participant is None:
            session.add(
                DMParticipant(
                    conversation_id=conversation_id,
                    conversation_domain=origin,
                    user_id=participant.id,
                    user_domain=participant.origin_domain,
                )
            )
    await session.flush()
    stored_participants = set(
        (
            await session.execute(
                select(DMParticipant.user_id, DMParticipant.user_domain).where(
                    DMParticipant.conversation_id == conversation_id,
                    DMParticipant.conversation_domain == origin,
                )
            )
        ).tuples()
    )
    expected_participants = participant_refs
    if conversation_type == "group":
        for user_id, user_domain in stored_participants - expected_participants:
            membership = await session.get(
                DMParticipant,
                (conversation_id, origin, user_id, user_domain),
            )
            if membership is not None:
                await session.delete(membership)
        await session.flush()
        stored_participants = expected_participants
    if stored_participants != expected_participants:
        raise ValueError("DM conversation participant set is inconsistent")
    return channel


async def replicate_group_notice(
    session: AsyncSession,
    settings: Settings,
    raw_notice: object,
    conversation: DMConversation,
    channel: Channel,
    before: list[User],
    after: list[User],
    *,
    previous_owner: tuple[int | None, str | None],
    expected_actor: tuple[int, str],
    initial_snapshot: bool = False,
    event_timestamp_ms: int | None = None,
) -> Message | None:
    """Validate and retain an authority-issued group membership notice."""

    if not isinstance(raw_notice, dict):
        raise ValueError("group DM notice is malformed")
    raw_message = raw_notice.get("message")
    if not isinstance(raw_message, dict):
        raise ValueError("group DM notice message is malformed")
    author = await upsert_remote_user(
        session,
        settings,
        RemoteUserProfile.model_validate(raw_notice.get("author")),
    )
    author_ref = (author.id, author.origin_domain)
    if author_ref != expected_actor:
        raise ValueError("group DM notice author does not match the state actor")
    message_id = database_snowflake(raw_message.get("id"), "group DM notice id")
    origin_domain = normalize_domain(str(raw_message.get("origin_domain", "")))
    channel_id = database_snowflake(raw_message.get("channel_id"), "group DM notice channel id")
    channel_domain = normalize_domain(str(raw_message.get("channel_domain", "")))
    message_type = raw_message.get("message_type")
    if isinstance(message_type, bool) or message_type not in {
        GROUP_DM_MEMBER_ADDED,
        GROUP_DM_MEMBER_LEFT,
        GROUP_DM_MEMBER_REMOVED,
    }:
        raise ValueError("group DM notice type is invalid")
    if (
        origin_domain != conversation.authority_domain
        or (channel_id, channel_domain) != (channel.id, channel.origin_domain)
        or (raw_message.get("author_id"), raw_message.get("author_domain"))
        != (str(author.id), author.origin_domain)
        or raw_message.get("e2ee") is not None
        or raw_message.get("attachments", []) != []
        or raw_message.get("referenced_message_id") is not None
        or raw_message.get("referenced_message_domain") is not None
        or raw_message.get("message_reference") is not None
        or raw_message.get("client_nonce") is not None
        or raw_message.get("edited_at") is not None
        or raw_message.get("deleted_at") is not None
        or raw_message.get("flags") != 4
    ):
        raise ValueError("group DM notice fields are invalid")
    raw_mentions = raw_message.get("mention_user_refs")
    if (
        raw_mentions != []
        or raw_message.get("mention_role_refs", []) != []
        or raw_message.get("mention_everyone", False) is not False
    ):
        raise ValueError("group DM notice target is invalid")
    raw_target = raw_notice.get("target")
    if not isinstance(raw_target, dict):
        raise ValueError("group DM notice target is invalid")
    target_ref = (
        database_snowflake(raw_target.get("id"), "group DM notice target id"),
        normalize_domain(str(raw_target.get("origin_domain", ""))),
    )
    before_refs = {(user.id, user.origin_domain) for user in before}
    after_refs = {(user.id, user.origin_domain) for user in after}
    added = after_refs - before_refs
    removed = before_refs - after_refs
    if initial_snapshot:
        # A newly invited user's home receives the current full membership
        # snapshot, not the authority's historical pre-mutation participant
        # set. The signed notice can therefore prove who was just invited, but
        # the local replica cannot reconstruct a one-member diff from an empty
        # database. Accept only an add notice for a local target that is present
        # in the snapshot; normal updates continue to require an exact diff.
        valid_transition = bool(
            message_type == GROUP_DM_MEMBER_ADDED
            and target_ref[1] == settings.domain
            and target_ref in after_refs
            and author_ref in after_refs
        )
    elif message_type == GROUP_DM_MEMBER_ADDED:
        valid_transition = added == {target_ref} and not removed and author_ref in before_refs
    elif message_type == GROUP_DM_MEMBER_LEFT:
        valid_transition = removed == {target_ref} == {author_ref} and not added
    else:
        valid_transition = removed == {target_ref} and not added and author_ref in after_refs

    existing = await session.get(Message, (message_id, origin_domain))
    if existing is not None:
        if (
            existing.channel_id,
            existing.channel_domain,
            existing.author_id,
            existing.author_domain,
            existing.message_type,
            existing.content,
            existing.mention_user_refs,
        ) != (
            channel.id,
            channel.origin_domain,
            author.id,
            author.origin_domain,
            message_type,
            raw_message.get("content"),
            raw_mentions,
        ):
            raise ValueError("group DM notice conflicts with a stored message")
        return None
    if not valid_transition:
        raise ValueError("group DM notice does not match the membership transition")
    users = {(user.id, user.origin_domain): user for user in [*before, *after, author]}
    target = users.get(target_ref)
    if target is None:
        raise ValueError("group DM notice target profile is missing")
    new_owner = None
    if (
        previous_owner[0] is not None
        and previous_owner[1] is not None
        and (previous_owner[0], previous_owner[1]) == target_ref
        and after
    ):
        if conversation.owner_id is None or conversation.owner_domain is None:
            raise ValueError("group DM ownership transfer is missing an owner")
        new_owner = users.get((conversation.owner_id, conversation.owner_domain))
        if new_owner is None:
            raise ValueError("group DM ownership transfer target is missing")
    expected_content = group_dm_notice_text(
        message_type,
        author.display_name or author.username,
        target.display_name or target.username,
        (new_owner.display_name or new_owner.username) if new_owner is not None else None,
    )
    if raw_message.get("content") != expected_content:
        raise ValueError("group DM notice text does not match the membership transition")
    validate_message_encryption_policy(
        channel.encryption_mode or "plaintext",
        content=expected_content,
        e2ee=None,
        policy_generation=channel.encryption_policy_generation or 0,
        policy_epoch=channel.encryption_epoch,
        policy_group_id=channel.encryption_group_id,
    )
    created_at = datetime.fromisoformat(str(raw_message.get("created_at")))
    created_ms = int(created_at.timestamp() * 1000)
    validate_snowflake_timestamp(
        message_id,
        created_at,
        "group DM notice",
        event_timestamp_ms=event_timestamp_ms if event_timestamp_ms is not None else created_ms,
    )
    mention_refs: list[dict[str, Any]] = []
    await admit_federated_dm_message(
        session,
        settings,
        conversation,
        message_id=message_id,
        message_domain=origin_domain,
        delta=dm_message_storage_delta(
            content=expected_content,
            e2ee=None,
            mention_user_refs=mention_refs,
            attachments=[],
        ),
    )
    message = Message(
        id=message_id,
        origin_domain=origin_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=author.id,
        author_domain=author.origin_domain,
        content=expected_content,
        e2ee=None,
        encryption_policy_generation=channel.encryption_policy_generation,
        encryption_epoch=channel.encryption_epoch,
        message_type=message_type,
        flags=4,
        mention_user_refs=mention_refs,
        created_at=created_at,
    )
    session.add(message)
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=mention_refs,
        )
    )
    await advance_channel_cursor(session, channel, message.id, message.origin_domain)
    return message


def dm_message_origin_is_authorized(
    *,
    message_origin: str,
    author_domain: str,
    event_origin: str,
    conversation_type: str,
    conversation_authority: str,
    authority_control: bool,
) -> bool:
    """Preserve author-minted IDs except for closed authority-owned DM events."""

    return (
        message_origin == author_domain
        or authority_control
        or (
            conversation_type == "group"
            and event_origin == conversation_authority
            and message_origin == conversation_authority
        )
    )


async def replicate_dm_message(
    session: AsyncSession,
    settings: Settings,
    content: dict[str, Any],
    *,
    event_timestamp_ms: int,
    event_origin: str,
) -> Message | None:
    author = await upsert_remote_user(
        session, settings, RemoteUserProfile.model_validate(content["author"])
    )
    raw = content["message"]
    message_id = database_snowflake(raw.get("id"), "message id")
    channel_id = database_snowflake(raw.get("channel_id"), "channel id")
    channel_domain = str(raw.get("channel_domain"))
    author_id = database_snowflake(raw.get("author_id"), "author id")
    author_domain = str(raw.get("author_domain"))
    origin_domain = str(raw.get("origin_domain"))
    if (author_id, author_domain) != (author.id, author.origin_domain):
        raise ValueError("DM message author reference does not match its profile")
    message_content = raw.get("content")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    raw_attachments = raw.get("attachments", [])
    if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
        raise ValueError("DM message attachment list is invalid")
    if message_content is not None and (
        not isinstance(message_content, str) or not 1 <= len(message_content) <= 4000
    ):
        raise ValueError("DM message content is invalid")
    if message_content is not None and e2ee is not None:
        raise ValueError("DM message mixes plaintext and encrypted content")
    message_type = raw.get("message_type", 0)
    tts = raw.get("tts", False)
    flags = raw.get("flags", 0)
    client_nonce = raw.get("client_nonce")
    if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
        raise ValueError("DM message type is invalid")
    if not isinstance(tts, bool):
        raise ValueError("DM message TTS marker is invalid")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise ValueError("DM message flags are invalid")
    if client_nonce is not None and (
        not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
    ):
        raise ValueError("DM message client nonce is invalid")
    if raw.get("edited_at") is not None or raw.get("deleted_at") is not None:
        raise ValueError("DM create event contains mutation timestamps")
    created_at = datetime.fromisoformat(str(raw["created_at"]))
    rich = validate_replicated_rich_projection(
        raw,
        message_id=message_id,
        message_origin=origin_domain,
        message_created_at=created_at,
        e2ee=e2ee,
        message_type=message_type,
        label="DM message",
    )
    application_ref = rich.application_ref
    forwarded_ref = rich.forwarded_ref
    forwarded_channel_ref = rich.forwarded_channel_ref
    forward_snapshot = rich.forward_snapshot
    if bool(flags & MESSAGE_FLAG_HAS_SNAPSHOT) != (
        forward_snapshot is not None or rich.has_encrypted_forward
    ):
        raise ValueError("DM message snapshot flag does not match its forward projection")
    if (
        message_content is None
        and e2ee is None
        and not raw_attachments
        and not rich.embeds
        and not rich.components
        and not rich.sticker_items
        and rich.poll is None
        and forwarded_ref is None
        and message_type != PIN_NOTICE_MESSAGE_TYPE
    ):
        raise ValueError(
            "DM message requires content, encrypted content, an attachment, "
            "rich content, or a forward"
        )
    if forwarded_ref is not None and rich.poll is not None:
        raise ValueError("DM poll messages cannot be forwarded")
    if rich.poll is not None:
        raw_poll = raw.get("poll")
        raw_results = raw_poll.get("results") if isinstance(raw_poll, dict) else None
        raw_counts = raw_results.get("answer_counts") if isinstance(raw_results, dict) else None
        if (
            not isinstance(raw_results, dict)
            or raw_results.get("is_finalized") is not False
            or raw_poll.get("finalized_at") is not None
            or not isinstance(raw_counts, list)
            or any(
                not isinstance(item, dict)
                or item.get("count") != 0
                or item.get("me_voted") is not False
                for item in raw_counts
            )
        ):
            raise ValueError("DM message create contains mutable poll results")
    raw_mention_refs = raw.get("mention_user_refs", [])
    if not isinstance(raw_mention_refs, list) or len(raw_mention_refs) > 5_000:
        raise ValueError("DM message mention list is invalid")
    mention_pairs: list[tuple[int, str]] = []
    for item in raw_mention_refs:
        if not isinstance(item, dict):
            raise ValueError("DM message mention reference is invalid")
        mention_pairs.append(
            (
                database_snowflake(item.get("id"), "mentioned user id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
        )
    mention_pairs = list(dict.fromkeys(mention_pairs))
    raw_role_mentions = raw.get("mention_role_refs", [])
    mention_everyone = raw.get("mention_everyone", False)
    if raw_role_mentions != [] or mention_everyone is not False:
        raise ValueError("DM message cannot contain role or everyone mentions")
    mention_role_refs: list[dict[str, Any]] = []
    validate_snowflake_timestamp(
        message_id,
        created_at,
        "DM message",
        event_timestamp_ms=event_timestamp_ms,
    )
    channel = await session.get(Channel, (channel_id, channel_domain))
    if channel is None or channel.guild_id is not None or channel.type != 1:
        raise ValueError("DM channel is not replicated")
    conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
    if conversation is None or conversation.type not in {"direct", "group"}:
        raise ValueError("DM conversation is not replicated")
    is_control, authority_control = authoritative_dm_control(
        e2ee,
        message_type=message_type,
        flags=flags,
        message_origin=origin_domain,
        event_origin=event_origin,
        conversation_authority=conversation.authority_domain,
    )
    if is_control and not authority_control:
        raise ValueError("DM E2EE control did not originate at its conversation authority")
    authority_poll_result = bool(
        message_type == POLL_RESULT_MESSAGE_TYPE
        and rich.poll_result is not None
        and origin_domain == event_origin == conversation.authority_domain
    )
    authority_pin_notice = bool(
        message_type == PIN_NOTICE_MESSAGE_TYPE
        and origin_domain == event_origin == conversation.authority_domain
    )
    if message_type == PIN_NOTICE_MESSAGE_TYPE and not authority_pin_notice:
        raise ValueError("DM pin notice did not originate at its conversation authority")
    if authority_pin_notice and (
        message_content is not None
        or e2ee is not None
        or raw_attachments
        or rich.embeds
        or rich.components
        or rich.sticker_items
        or rich.poll is not None
        or forwarded_ref is not None
        or application_ref is not None
        or rich.interaction_metadata is not None
        or tts
        or flags != 0
        or client_nonce is not None
        or raw_mention_refs
    ):
        raise ValueError("DM pin notice fields are invalid")
    if authority_poll_result:
        validate_poll_result_wire_body(
            raw,
            author_ref=(author.id, author.origin_domain),
            channel_ref=(channel.id, channel.origin_domain),
        )
    if not dm_message_origin_is_authorized(
        message_origin=origin_domain,
        author_domain=author.origin_domain,
        event_origin=event_origin,
        conversation_type=conversation.type,
        conversation_authority=conversation.authority_domain,
        authority_control=authority_control or authority_poll_result or authority_pin_notice,
    ):
        raise ValueError("DM message snowflake was not minted by its author instance")
    await lock_federated_dm_authority(session, conversation.authority_domain)
    raw_policy = content.get("encryption_policy")
    if raw_policy is not None:
        if event_origin != conversation.authority_domain:
            raise ValueError("DM encryption policy did not originate at its authority")
        encryption_policy = validate_channel_encryption_policy(raw_policy)
        validate_channel_encryption_policy_transition(
            channel,
            encryption_policy,
            label="DM",
        )
        channel.encryption_mode = str(encryption_policy["mode"])
        channel.encryption_state = str(encryption_policy["state"])
        channel.encryption_policy_generation = int(encryption_policy["generation"])
        channel.encryption_protocol = encryption_policy["protocol"]
        channel.encryption_suite = encryption_policy["suite"]
        channel.encryption_group_id = encryption_policy["group_id"]
        channel.encryption_epoch = encryption_policy["epoch"]
        if channel.encryption_mode == "e2ee" and channel.encryption_activated_at is None:
            channel.encryption_activated_at = created_at
    if not authority_poll_result and not authority_pin_notice:
        validate_message_encryption_policy(
            channel.encryption_mode,
            content=message_content,
            e2ee=e2ee,
            attachment_count=len(raw_attachments),
            policy_generation=channel.encryption_policy_generation,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    if e2ee is not None and e2ee.get("operation") not in {"welcome", "commit"}:
        validate_e2ee_message_projection(
            e2ee,
            message_id=message_id,
            message_domain=origin_domain,
            edited=False,
        )
    author_participates = await session.get(
        DMParticipant,
        (channel.id, channel.origin_domain, author.id, author.origin_domain),
    )
    if author_participates is None:
        raise ValueError("DM author is not a conversation participant")
    if application_ref is not None:
        application = await session.get(BotApplication, application_ref)
        if (
            application is None
            or application.status != "active"
            or (application.bot_user_id, application.bot_user_domain)
            != (author.id, author.origin_domain)
        ):
            raise ValueError("DM message application is not bound to its bot author")
    participant_refs = set(
        (
            await session.execute(
                select(DMParticipant.user_id, DMParticipant.user_domain).where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                )
            )
        ).tuples()
    )
    if any(pair not in participant_refs for pair in mention_pairs):
        raise ValueError("DM message mentions a user outside the conversation")
    mention_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
    ]

    referenced_id_raw = raw.get("referenced_message_id")
    referenced_domain_raw = raw.get("referenced_message_domain")
    if (referenced_id_raw is None) != (referenced_domain_raw is None):
        raise ValueError("DM message reference is incomplete")
    referenced_id: int | None = None
    referenced_domain: str | None = None
    if referenced_id_raw is not None:
        referenced_id = database_snowflake(referenced_id_raw, "referenced message id")
        referenced_domain = normalize_domain(str(referenced_domain_raw))
        if (referenced_id, referenced_domain) >= (message_id, origin_domain):
            raise ValueError("DM message reference must precede the message")
        referenced = await session.get(Message, (referenced_id, referenced_domain))
        if authority_pin_notice and (
            referenced is None
            or (referenced.channel_id, referenced.channel_domain)
            != (channel.id, channel.origin_domain)
            or not message_is_pinnable(referenced)
        ):
            raise ValueError("DM pin notice source binding is invalid")
        if authority_poll_result:
            poll_result_projection = rich.poll_result
            if poll_result_projection is None:
                raise RuntimeError("validated DM poll result lost its projection")
            source_poll = (
                await session.get(Poll, (referenced.id, referenced.origin_domain))
                if referenced is not None
                else None
            )
            if (
                referenced is None
                or source_poll is None
                or (referenced.channel_id, referenced.channel_domain)
                != (channel.id, channel.origin_domain)
                or (referenced.author_id, referenced.author_domain)
                != (author.id, author.origin_domain)
                or ("e2ee" if referenced.e2ee is not None else "plaintext")
                != poll_result_projection["source_encryption_mode"]
            ):
                raise ValueError("DM poll result source binding is invalid")
            if source_poll.finalized_at is None:
                # The dedicated finalization delta is ordered before type 46,
                # but this convergence fence also closes a source restored from
                # history after that short-lived delta was compacted.
                source_poll.finalized_at = created_at
            elif source_poll.finalized_at > created_at:
                raise ValueError("DM poll result predates source finalization")
        if referenced is not None and (
            referenced.channel_id,
            referenced.channel_domain,
        ) != (channel.id, channel.origin_domain):
            raise ValueError("DM message reference is not in the conversation")
        if (
            referenced is None
            and not authority_pin_notice
            and not opaque_dm_history_ref_allowed(
                conversation,
                (referenced_id, referenced_domain),
                participant_domains={domain for _identifier, domain in participant_refs},
                local_domain=settings.domain,
                remote_available=await dm_authority_history_available(
                    session,
                    conversation,
                    local_domain=settings.domain,
                ),
            )
        ):
            raise ValueError("DM message reference is not in the conversation")
    elif authority_pin_notice:
        raise ValueError("DM pin notice is missing its source message")
    if forwarded_ref is not None and referenced_id is not None:
        raise ValueError("DM forward cannot also be a reply")
    if message_type in {12, 18}:
        raise ValueError("DM message type cannot contain a guild channel reference")
    message_reference = validate_message_reference_projection(
        raw.get("message_reference"),
        message_type=message_type,
        channel_ref=(channel.id, channel.origin_domain),
        guild_ref=None,
        referenced_message_ref=(
            (referenced_id, referenced_domain)
            if referenced_id is not None and referenced_domain is not None
            else None
        ),
        forwarded_message_ref=forwarded_ref,
        forwarded_channel_ref=forwarded_channel_ref,
        has_forward_snapshot=bool(forward_snapshot is not None or rich.has_encrypted_forward),
        label="DM message",
    )
    local_recipients = list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                User.origin_domain == settings.domain,
                User.is_local.is_(True),
            )
        )
    )
    if conversation.type == "direct":
        for recipient in local_recipients:
            await require_can_direct_message(session, author, recipient)
    await admit_federated_dm_message(
        session,
        settings,
        conversation,
        message_id=message_id,
        message_domain=origin_domain,
        delta=dm_message_storage_delta(
            content=message_content,
            e2ee=e2ee,
            mention_user_refs=mention_refs,
            mention_role_refs=mention_role_refs,
            mention_everyone=False,
            attachments=raw_attachments,
            client_nonce=client_nonce,
            forwarded_message_ref=forwarded_ref,
            forward_snapshot=forward_snapshot,
            poll_result=rich.poll_result,
            embeds=rich.embeds,
            components=rich.components,
            sticker_items=rich.sticker_items,
            poll=(raw.get("poll") if rich.poll is not None else None),
            application_ref=application_ref,
            interaction_metadata=rich.interaction_metadata,
            view_version=rich.view_version,
            view_persistent=rich.view_persistent,
            view_expires_at=rich.view_expires_at,
        ),
        protected_refs=(
            {
                reference
                for reference in (
                    (
                        (referenced_id, referenced_domain)
                        if referenced_id is not None and referenced_domain is not None
                        else None
                    ),
                )
                if reference is not None
            }
            or None
        ),
    )
    inserted = await session.scalar(
        pg_insert(Message)
        .values(
            id=message_id,
            origin_domain=origin_domain,
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            author_id=author_id,
            author_domain=author_domain,
            content=message_content,
            e2ee=e2ee,
            embeds=rich.embeds,
            components=rich.components,
            sticker_items=rich.sticker_items,
            application_id=application_ref[0] if application_ref is not None else None,
            application_domain=application_ref[1] if application_ref is not None else None,
            interaction_metadata=rich.interaction_metadata,
            view_version=rich.view_version,
            encryption_policy_generation=channel.encryption_policy_generation,
            encryption_epoch=channel.encryption_epoch,
            message_type=message_type,
            tts=tts,
            flags=flags,
            client_nonce=client_nonce,
            referenced_message_id=referenced_id,
            referenced_message_domain=referenced_domain,
            message_reference=message_reference,
            forwarded_message_id=forwarded_ref[0] if forwarded_ref is not None else None,
            forwarded_message_domain=forwarded_ref[1] if forwarded_ref is not None else None,
            forwarded_channel_id=(
                forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
            ),
            forwarded_channel_domain=(
                forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
            ),
            forward_snapshot=forward_snapshot,
            poll_result=rich.poll_result,
            mention_user_refs=mention_refs,
            mention_role_refs=mention_role_refs,
            mention_everyone=False,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
        .returning(Message.id)
    )
    if inserted is None:
        existing = await session.get(Message, (message_id, origin_domain))
        if (
            existing is None
            or replicated_message_create_fingerprint(
                channel_id=existing.channel_id,
                channel_domain=existing.channel_domain,
                author_id=existing.author_id,
                author_domain=existing.author_domain,
                content=existing.content,
                e2ee=existing.e2ee,
                message_type=existing.message_type,
                tts=bool(existing.tts),
                flags=existing.flags,
                client_nonce=existing.client_nonce,
                referenced_message_id=existing.referenced_message_id,
                referenced_message_domain=existing.referenced_message_domain,
                message_reference=existing.message_reference,
                forwarded_message_id=existing.forwarded_message_id,
                forwarded_message_domain=existing.forwarded_message_domain,
                forwarded_channel_id=existing.forwarded_channel_id,
                forwarded_channel_domain=existing.forwarded_channel_domain,
                forward_snapshot=existing.forward_snapshot,
                mention_user_refs=existing.mention_user_refs,
                mention_role_refs=existing.mention_role_refs,
                mention_everyone=bool(existing.mention_everyone),
                embeds=list(existing.embeds or []),
                components=list(existing.components or []),
                sticker_items=list(existing.sticker_items or []),
                application_id=existing.application_id,
                application_domain=existing.application_domain,
                interaction_metadata=existing.interaction_metadata,
                view_version=int(existing.view_version or 0),
                created_at=existing.created_at,
            )
            != replicated_message_create_fingerprint(
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                author_id=author.id,
                author_domain=author.origin_domain,
                content=message_content,
                e2ee=e2ee,
                message_type=message_type,
                tts=tts,
                flags=flags,
                client_nonce=client_nonce,
                referenced_message_id=referenced_id,
                referenced_message_domain=referenced_domain,
                message_reference=message_reference,
                forwarded_message_id=forwarded_ref[0] if forwarded_ref is not None else None,
                forwarded_message_domain=forwarded_ref[1] if forwarded_ref is not None else None,
                forwarded_channel_id=(
                    forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
                ),
                forwarded_channel_domain=(
                    forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
                ),
                forward_snapshot=forward_snapshot,
                poll_result=rich.poll_result,
                mention_user_refs=mention_refs,
                mention_role_refs=mention_role_refs,
                mention_everyone=False,
                embeds=rich.embeds,
                components=rich.components,
                sticker_items=rich.sticker_items,
                application_id=application_ref[0] if application_ref is not None else None,
                application_domain=application_ref[1] if application_ref is not None else None,
                interaction_metadata=rich.interaction_metadata,
                view_version=rich.view_version,
                created_at=created_at,
            )
            or not await stored_poll_matches_projection(session, existing, rich.poll)
            or not await stored_view_matches_projection(session, existing, rich)
        ):
            raise ValueError("DM message snowflake conflicts with another message")
        await apply_e2ee_control_metadata(
            session,
            existing,
            content.get("e2ee_control"),
            expected_authority=conversation.authority_domain,
        )
        await replicate_message_attachments(session, settings, existing, author, raw_attachments)
        await advance_channel_cursor(session, channel, message_id, origin_domain)
        return None
    message = await session.get(Message, (message_id, origin_domain))
    if message is None:
        raise RuntimeError("replicated message disappeared")
    if rich.poll is not None:
        add_poll_projection(session, message, rich.poll, created_at=created_at)
    if (rich.components or rich.has_encrypted_controls) and application_ref is not None:
        session.add(
            MessageView(
                message_id=message.id,
                message_domain=message.origin_domain,
                application_id=application_ref[0],
                application_domain=application_ref[1],
                integration_type=cast(str, rich.interaction_integration_type),
                installation_id=cast(tuple[int, str], rich.interaction_installation_ref)[0],
                installation_domain=cast(tuple[int, str], rich.interaction_installation_ref)[1],
                installation_revision=cast(int, rich.interaction_installation_revision),
                version=rich.view_version,
                persistent=rich.view_persistent,
                expires_at=rich.view_expires_at,
            )
        )
    await apply_e2ee_control_metadata(
        session,
        message,
        content.get("e2ee_control"),
        expected_authority=conversation.authority_domain,
    )
    await replicate_message_attachments(session, settings, message, author, raw_attachments)
    session.add(
        MessageProjection(
            message_id=message.id,
            message_domain=message.origin_domain,
            channel_id=message.channel_id,
            channel_domain=message.channel_domain,
            mention_user_refs=mention_refs,
        )
    )
    await advance_channel_cursor(session, channel, message.id, message.origin_domain)
    return message


async def publish_replicated_dm_message(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    message: Message,
) -> None:
    rendered = await render_message_payload(session, message)
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        return
    local_participants = list(
        await session.scalars(
            select(User)
            .join(
                DMParticipant,
                (DMParticipant.user_id == User.id)
                & (DMParticipant.user_domain == User.origin_domain),
            )
            .where(
                DMParticipant.conversation_id == channel.id,
                DMParticipant.conversation_domain == channel.origin_domain,
                User.origin_domain == settings.domain,
                User.is_local.is_(True),
            )
        )
    )
    for participant in local_participants:
        await publish_dispatch(
            redis,
            user_topic(settings.domain, participant.id),
            "MESSAGE_CREATE",
            rendered,
        )
    conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
    if conversation is not None and conversation.history_truncated:
        all_participants = list(
            await session.scalars(
                select(User)
                .join(
                    DMParticipant,
                    (DMParticipant.user_id == User.id)
                    & (DMParticipant.user_domain == User.origin_domain),
                )
                .where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                )
            )
        )
        history = dm_history_metadata(
            conversation,
            local_domain=settings.domain,
            remote_available=await dm_authority_history_available(
                session, conversation, local_domain=settings.domain
            ),
        )
        for participant in local_participants:
            await publish_dispatch(
                redis,
                user_topic(settings.domain, participant.id),
                "CHANNEL_UPDATE",
                dm_channel_payload(
                    channel,
                    [
                        user
                        for user in all_participants
                        if (user.id, user.origin_domain)
                        != (participant.id, participant.origin_domain)
                    ],
                    conversation=conversation,
                    history=history,
                ),
            )
