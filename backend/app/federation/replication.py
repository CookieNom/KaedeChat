from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import validate_e2ee_envelope
from app.chat.events import publish_dispatch, user_topic
from app.chat.payloads import render_message_payload
from app.chat.privacy import require_can_direct_message
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
from app.federation.network import normalize_domain
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
    instance = await session.get(Instance, profile.origin_domain)
    if instance is None:
        await session.execute(
            pg_insert(Instance)
            .values(domain=profile.origin_domain, is_self=False)
            .on_conflict_do_nothing(index_elements=["domain"])
        )
    user = await session.get(
        User, (database_snowflake(profile.id, "user id"), profile.origin_domain)
    )
    if user is None:
        await session.execute(
            pg_insert(User)
            .values(
                id=int(profile.id),
                origin_domain=profile.origin_domain,
                is_local=False,
                username=profile.username,
                display_name=profile.display_name,
                avatar_hash=profile.avatar_hash,
                banner_hash=profile.banner_hash,
                bio=profile.bio,
                custom_status=profile.custom_status,
                profile_version=profile.profile_version,
            )
            .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
        )
        user = await session.get(User, (int(profile.id), profile.origin_domain))
    if user is None:
        raise RuntimeError("remote user insert did not converge")
    if user.is_local:
        raise ValueError("remote user profile changed an immutable identity")
    if not user.profile_resolved:
        user.username = profile.username
        user.profile_resolved = True
        user.profile_version = profile.profile_version
        user.display_name = profile.display_name
        user.avatar_hash = profile.avatar_hash
        user.banner_hash = profile.banner_hash
        user.bio = profile.bio
        user.custom_status = profile.custom_status
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
    local database without accepting remote mutations.  Every other profile must
    already have been learned from its own origin; the delegated copy may identify
    that cached user but cannot create or update it.

    Until nested user-signed profile proofs are part of the protocol, rejecting an
    unknown third-party identity is preferable to letting one authenticated peer
    populate another origin's namespace.
    """

    authority = normalize_domain(authority_origin)
    if profile.origin_domain in {authority, settings.domain}:
        return await upsert_remote_user(session, settings, profile)

    user = await session.get(
        User,
        (database_snowflake(profile.id, "user id"), profile.origin_domain),
    )
    if user is None:
        raise ValueError("third-party profile must be resolved from its authoritative origin first")
    if user.is_local or user.username != profile.username:
        raise ValueError("third-party profile does not match its authoritative identity")
    return user


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
                width=raw.get("width") if isinstance(raw.get("width"), int) else None,
                height=raw.get("height") if isinstance(raw.get("height"), int) else None,
                blurhash=raw.get("blurhash") if isinstance(raw.get("blurhash"), str) else None,
                scan_status="clean",
                purpose="attachment",
                finalized_at=message.created_at,
                variants=(raw.get("variants") if isinstance(raw.get("variants"), dict) else {}),
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
    if await session.get(Instance, origin) is None:
        if origin == settings.domain:
            raise RuntimeError("self instance is not bootstrapped")
        await session.execute(
            pg_insert(Instance)
            .values(domain=origin, is_self=False)
            .on_conflict_do_nothing(index_elements=["domain"])
        )
    participants = [
        await upsert_remote_user(session, settings, profile) for profile in participant_profiles
    ]
    conversation_id = database_snowflake(conversation.get("id"), "conversation id")
    pair_key = str(conversation["pair_key"])
    authority_domain = str(conversation["authority_domain"])
    await session.scalar(
        pg_insert(DMConversation)
        .values(
            id=conversation_id,
            origin_domain=origin,
            pair_key=pair_key,
            type="direct",
            authority_domain=authority_domain,
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
        or existing.type != "direct"
    ):
        raise ValueError("DM conversation identity conflicts with an existing conversation")
    channel = await session.get(Channel, (conversation_id, origin))
    if channel is None:
        channel = Channel(
            id=conversation_id,
            origin_domain=origin,
            guild_id=None,
            guild_domain=None,
            type=1,
            name=None,
            position=0,
            rate_limit_per_user=0,
            created_floor_id=conversation_id,
        )
        session.add(channel)
        await session.flush()
    elif channel.guild_id is not None or channel.type != 1:
        raise ValueError("DM conversation ID conflicts with a non-DM channel")
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
    expected_participants = {
        (participant.id, participant.origin_domain) for participant in participants
    }
    if stored_participants != expected_participants:
        raise ValueError("DM conversation participant set is inconsistent")
    return channel


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
    if not isinstance(raw_mention_refs, list) or len(raw_mention_refs) > 100:
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
    if conversation is None or conversation.type != "direct":
        raise ValueError("DM conversation is not replicated")
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
        referenced = await session.get(Message, (referenced_id, referenced_domain))
        if referenced is None or (
            referenced.channel_id,
            referenced.channel_domain,
        ) != (channel.id, channel.origin_domain):
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
    for recipient in local_recipients:
        await require_can_direct_message(session, author, recipient)
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
        if existing is None or (
            existing.channel_id,
            existing.channel_domain,
            existing.author_id,
            existing.author_domain,
            existing.content,
            existing.message_type,
            existing.flags,
            existing.client_nonce,
            existing.referenced_message_id,
            existing.referenced_message_domain,
            existing.mention_user_refs,
            existing.created_at,
        ) != (
            channel.id,
            channel.origin_domain,
            author.id,
            author.origin_domain,
            message_content,
            message_type,
            flags,
            client_nonce,
            referenced_id,
            referenced_domain,
            mention_refs,
            created_at,
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
