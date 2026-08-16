from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import validate_e2ee_envelope
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import dm_channel_payload, render_message_payload
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
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Instance,
    Message,
    MessageProjection,
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
from app.federation.identity_storage import admit_remote_user_identity
from app.federation.network import ensure_remote_instance_record, normalize_domain
from app.federation.schemas import MAX_DATABASE_SNOWFLAKE, RemoteUserProfile
from app.media.processing import normalize_declared_type, sanitize_filename


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
    created_at: datetime,
    webhook_name: str | None = None,
    webhook_avatar_hash: str | None = None,
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
        flags,
        client_nonce,
        referenced_message_id,
        referenced_message_domain,
        mention_user_refs,
        webhook_name,
        webhook_avatar_hash,
        created_at,
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
            user.profile_resolved = True
            user.profile_version = profile.profile_version
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
                display_name=profile.display_name,
                avatar_hash=profile.avatar_hash,
                banner_hash=profile.banner_hash,
                bio=profile.bio,
                custom_status=profile.custom_status,
                profile_version=profile.profile_version,
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
        rendered[name] = {
            "content_type": content_type,
            "size": size,
            "width": width,
            "height": height,
            **(
                {"processing_version": processing_version} if processing_version is not None else {}
            ),
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
        "username": user.username,
        "display_name": user.display_name,
        "avatar_hash": user.avatar_hash,
        "banner_hash": user.banner_hash,
        "bio": user.bio,
        "custom_status": user.custom_status,
        "profile_version": user.profile_version,
    }


async def replicate_message_attachments(
    session: AsyncSession,
    settings: Settings,
    message: Message,
    author: User,
    raw_attachments: object,
) -> list[Attachment]:
    """Validate stable remote attachment references without trusting scan claims or URLs."""

    if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
        raise ValueError("message attachment list is invalid")
    seen: set[tuple[int, str]] = set()
    rendered: list[Attachment] = []
    for raw in raw_attachments:
        if not isinstance(raw, dict):
            raise ValueError("message attachment is invalid")
        attachment_id = database_snowflake(raw.get("id"), "attachment id")
        origin = normalize_domain(str(raw.get("origin_domain", "")))
        if origin != author.origin_domain:
            raise ValueError("attachment origin does not match the message author")
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
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= settings.media_max_attachment_bytes
        ):
            raise ValueError("attachment size is invalid")
        width, height = remote_media_dimensions(raw)
        blurhash = sanitized_remote_blurhash(raw.get("blurhash"))
        variants = sanitized_remote_variants(
            raw.get("variants", {}),
            max_bytes=settings.media_max_attachment_bytes,
        )
        existing = await session.get(Attachment, ref)
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
                blurhash=blurhash,
                scan_status="clean",
                purpose="attachment",
                finalized_at=message.created_at,
                variants=variants,
            )
            session.add(existing)
        else:
            if (
                existing.uploader_id,
                existing.uploader_domain,
                existing.filename,
                existing.content_type,
                existing.size,
            ) != (author.id, author.origin_domain, filename_raw, content_type, size):
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
    federated = await admit_federated_dm_conversation(
        session,
        settings,
        authority_domain=authority_domain,
        pair_key=pair_key,
        participant_domains=participant_domains,
    )
    if await session.get(Instance, origin) is None:
        if origin == settings.domain:
            raise RuntimeError("self instance is not bootstrapped")
        await ensure_remote_instance_record(session, settings, origin)
    if conversation_type not in {"direct", "group"}:
        raise ValueError("unsupported DM conversation type")
    participants = [
        await upsert_remote_user(session, settings, profile) for profile in participant_profiles
    ]
    participant_refs = {(participant.id, participant.origin_domain) for participant in participants}
    if len(participant_refs) != len(participants):
        raise ValueError("DM conversation participants must be unique")
    conversation_id = database_snowflake(conversation.get("id"), "conversation id")
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
        or raw_message.get("client_nonce") is not None
        or raw_message.get("edited_at") is not None
        or raw_message.get("deleted_at") is not None
        or raw_message.get("flags") != 4
    ):
        raise ValueError("group DM notice fields are invalid")
    raw_mentions = raw_message.get("mention_user_refs")
    if raw_mentions != []:
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
    if message_type == GROUP_DM_MEMBER_ADDED:
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


async def replicate_dm_message(
    session: AsyncSession,
    settings: Settings,
    content: dict[str, Any],
    *,
    event_timestamp_ms: int,
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
    if origin_domain != author.origin_domain:
        raise ValueError("DM message snowflake was not minted by its author instance")
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
    if message_content is None and e2ee is None and not raw_attachments:
        raise ValueError("DM message requires content, encrypted content, or an attachment")
    message_type = raw.get("message_type", 0)
    flags = raw.get("flags", 0)
    client_nonce = raw.get("client_nonce")
    if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
        raise ValueError("DM message type is invalid")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise ValueError("DM message flags are invalid")
    if client_nonce is not None and (
        not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
    ):
        raise ValueError("DM message client nonce is invalid")
    if raw.get("edited_at") is not None or raw.get("deleted_at") is not None:
        raise ValueError("DM create event contains mutation timestamps")
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
    created_at = datetime.fromisoformat(str(raw["created_at"]))
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
    await lock_federated_dm_authority(session, conversation.authority_domain)
    author_participates = await session.get(
        DMParticipant,
        (channel.id, channel.origin_domain, author.id, author.origin_domain),
    )
    if author_participates is None:
        raise ValueError("DM author is not a conversation participant")
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
        if referenced is not None and (
            referenced.channel_id,
            referenced.channel_domain,
        ) != (channel.id, channel.origin_domain):
            raise ValueError("DM message reference is not in the conversation")
        if referenced is None and not opaque_dm_history_ref_allowed(
            conversation,
            (referenced_id, referenced_domain),
            participant_domains={domain for _identifier, domain in participant_refs},
            local_domain=settings.domain,
            remote_available=await dm_authority_history_available(
                session,
                conversation,
                local_domain=settings.domain,
            ),
        ):
            raise ValueError("DM message reference is not in the conversation")
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
            attachments=raw_attachments,
            client_nonce=client_nonce,
        ),
        protected_refs=(
            {(referenced_id, referenced_domain)}
            if referenced_id is not None and referenced_domain is not None
            else None
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
            message_type=message_type,
            flags=flags,
            client_nonce=client_nonce,
            referenced_message_id=referenced_id,
            referenced_message_domain=referenced_domain,
            mention_user_refs=mention_refs,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
        .returning(Message.id)
    )
    if inserted is None:
        existing = await session.get(Message, (message_id, origin_domain))
        if existing is None or replicated_message_create_fingerprint(
            channel_id=existing.channel_id,
            channel_domain=existing.channel_domain,
            author_id=existing.author_id,
            author_domain=existing.author_domain,
            content=existing.content,
            e2ee=existing.e2ee,
            message_type=existing.message_type,
            flags=existing.flags,
            client_nonce=existing.client_nonce,
            referenced_message_id=existing.referenced_message_id,
            referenced_message_domain=existing.referenced_message_domain,
            mention_user_refs=existing.mention_user_refs,
            created_at=existing.created_at,
        ) != replicated_message_create_fingerprint(
            channel_id=channel.id,
            channel_domain=channel.origin_domain,
            author_id=author.id,
            author_domain=author.origin_domain,
            content=message_content,
            e2ee=e2ee,
            message_type=message_type,
            flags=flags,
            client_nonce=client_nonce,
            referenced_message_id=referenced_id,
            referenced_message_domain=referenced_domain,
            mention_user_refs=mention_refs,
            created_at=created_at,
        ):
            raise ValueError("DM message snowflake conflicts with another message")
        await replicate_message_attachments(session, settings, existing, author, raw_attachments)
        await advance_channel_cursor(session, channel, message_id, origin_domain)
        return None
    message = await session.get(Message, (message_id, origin_domain))
    if message is None:
        raise RuntimeError("replicated message disappeared")
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
