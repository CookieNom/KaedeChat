from __future__ import annotations

import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import validate_e2ee_envelope
from app.chat.permissions import calculate_permissions
from app.core.permissions import ALL_PERMISSIONS, Permission
from app.core.settings import Settings
from app.db.models import (
    Attachment,
    Ban,
    Channel,
    ChannelOverwrite,
    Guild,
    GuildEvent,
    GuildMember,
    MemberRole,
    Message,
    MessageProjection,
    Pin,
    Reaction,
    ReadState,
    RemoteMediaCache,
    Role,
    User,
)
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.replication import (
    advance_channel_cursor,
    database_snowflake,
    replicate_message_attachments,
    resolve_delegated_profile,
    validate_snowflake_timestamp,
)
from app.federation.schemas import RemoteUserProfile
from app.federation.security import validated_event_envelope


class GuildSequenceGap(RuntimeError):
    def __init__(self, expected: int, received: int) -> None:
        self.expected = expected
        self.received = received
        super().__init__(f"guild sequence gap: expected {expected}, received {received}")


GUILD_MUTATION_EVENT_TYPES = frozenset(
    {
        "guild.update",
        "guild.channel.create",
        "guild.channel.update",
        "guild.channel.delete",
        "guild.role.create",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.ban.add",
        "guild.ban.remove",
        "guild.message.update",
        "guild.message.delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.pin.add",
        "guild.pin.remove",
    }
)

HISTORY_ACCESS_MUTATION_EVENT_TYPES = frozenset(
    {
        "guild.update",
        "guild.channel.create",
        "guild.channel.update",
        "guild.channel.delete",
        "guild.role.create",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.ban.add",
    }
)

MAX_SNAPSHOT_PAGES = 100
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_MEMBERS = 100_000
MAX_SNAPSHOT_MEMBER_ROLES = 500_000
MAX_SNAPSHOT_OVERWRITES = 100_000


def guild_event_requires_snapshot(event: dict[str, Any]) -> bool:
    context = event.get("context")
    return isinstance(context, dict) and context.get("snapshot_required") is True


def guild_snapshot_rate_scope(guild_id: int, snapshot_seq: int, *, paginated: bool) -> str:
    """Limit repeated reads without throttling a newly-required revision."""

    mode = "page" if paginated else "start"
    return f"guild-snapshot-{mode}:{guild_id}:{snapshot_seq}"


async def mark_guild_replica_stale(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    guild_domain: str,
    required_seq: int,
) -> bool:
    """Persist a resync marker unless another worker already caught up."""

    if guild_domain == settings.domain:
        return False
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
        .with_for_update()
    )
    if guild is None or guild.last_event_seq >= required_seq:
        return False
    guild.sync_status = "stale"
    return True


def guild_event_channel_ref(event: dict[str, Any]) -> tuple[int, str] | None:
    """Extract a channel scope used for permission-filtered delivery."""

    context = event.get("context")
    if isinstance(context, dict) and context.get("channel_id") is not None:
        try:
            return (
                database_snowflake(context.get("channel_id"), "guild event channel id"),
                normalize_domain(str(context.get("channel_domain", ""))),
            )
        except (FederationNetworkError, TypeError, ValueError):
            return None
    content = event.get("content")
    raw_message = content.get("message") if isinstance(content, dict) else None
    if isinstance(raw_message, dict) and raw_message.get("channel_id") is not None:
        try:
            return (
                database_snowflake(raw_message.get("channel_id"), "guild event channel id"),
                normalize_domain(str(raw_message.get("channel_domain", ""))),
            )
        except (FederationNetworkError, TypeError, ValueError):
            return None
    return None


def guild_history_requires_snapshot(
    *, after_seq: int, latest_seq: int, first_retained_seq: int | None
) -> bool:
    """Return whether retained events cannot continue the requester's cursor."""

    if after_seq > latest_seq:
        return True
    if first_retained_seq is not None:
        return first_retained_seq != after_seq + 1
    return after_seq < latest_seq


async def remote_destinations_with_channel_access(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    channel: Channel,
) -> set[str]:
    remote_users = list(
        await session.scalars(
            select(User)
            .join(
                GuildMember,
                (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
            )
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                User.origin_domain != settings.domain,
            )
        )
    )
    destinations: set[str] = set()
    for user in remote_users:
        permissions, _member = await calculate_permissions(session, guild, user, channel=channel)
        if permissions & Permission.VIEW_CHANNEL:
            destinations.add(user.origin_domain)
    return destinations


async def assign_guild_sequence(session: AsyncSession, guild: Guild) -> int:
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise RuntimeError("guild disappeared while assigning an event sequence")
    seq = locked.next_event_seq
    locked.next_event_seq += 1
    locked.last_event_seq = seq
    return seq


def new_guild_event_id() -> str:
    return f"kcge_{secrets.token_urlsafe(24)}"


def store_guild_event(
    session: AsyncSession,
    guild: Guild,
    seq: int,
    event_id: str,
    envelope: dict[str, Any],
) -> None:
    session.add(
        GuildEvent(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            seq=seq,
            event_id=event_id,
            envelope=envelope,
        )
    )


async def apply_guild_message_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
) -> Message | None:
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    guild = locked
    seq = database_snowflake(
        event.get("seq") or event.get("context", {}).get("seq"), "guild sequence"
    )
    expected = guild.last_event_seq + 1
    if seq <= guild.last_event_seq:
        return None
    if seq != expected:
        guild.sync_status = "stale"
        raise GuildSequenceGap(expected, seq)
    raw = event["content"]["message"]
    author_raw = raw.get("author") or event["content"].get("author")
    if not isinstance(author_raw, dict):
        raise ValueError("guild message author profile is missing")
    author = await resolve_delegated_profile(
        session,
        settings,
        RemoteUserProfile.model_validate(author_raw),
        authority_origin=guild.origin_domain,
    )
    if (str(author.id), author.origin_domain) != (
        str(raw["author_id"]),
        str(raw["author_domain"]),
    ):
        raise ValueError("guild event author mismatch")
    message_id = database_snowflake(raw.get("id"), "message id")
    channel_id = database_snowflake(raw.get("channel_id"), "channel id")
    message_origin = str(raw.get("origin_domain"))
    channel_domain = str(raw.get("channel_domain"))
    if message_origin != guild.origin_domain or channel_domain != guild.origin_domain:
        raise ValueError("guild message references a non-authoritative origin")
    channel = await session.get(Channel, (channel_id, channel_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain)
        != (
            guild.id,
            guild.origin_domain,
        )
    ):
        raise ValueError("guild message channel does not belong to the guild")
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, author.id, author.origin_domain),
    )
    if membership is None:
        raise ValueError("guild message author is not a guild member")
    event_type = event.get("type")
    event_actor = event.get("actor")
    if not isinstance(event_actor, dict):
        raise ValueError("guild message event actor is missing")
    event_actor_ref = (
        database_snowflake(event_actor.get("id"), "guild event actor id"),
        str(event_actor.get("domain")),
    )
    if event_type == "guild.message.create":
        if event_actor_ref != (author.id, author.origin_domain):
            raise ValueError("guild message event actor does not match its author")
    elif event_type == "guild.message.committed":
        if event_actor_ref != (guild.owner_id, guild.owner_domain):
            raise ValueError("guild commit event actor does not match its owner")
    else:
        raise ValueError("unsupported guild message event type")
    content = raw.get("content")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    raw_attachments = raw.get("attachments", [])
    if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4000):
        raise ValueError("guild message content is invalid")
    if content is not None and e2ee is not None:
        raise ValueError("guild message mixes plaintext and encrypted content")
    if content is None and e2ee is None and not raw_attachments:
        raise ValueError("guild message requires content, encrypted content, or an attachment")
    message_type = raw.get("message_type", 0)
    flags = raw.get("flags", 0)
    client_nonce = raw.get("client_nonce")
    if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
        raise ValueError("guild message type is invalid")
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise ValueError("guild message flags are invalid")
    if client_nonce is not None and (
        not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
    ):
        raise ValueError("guild message client nonce is invalid")
    raw_webhook = raw.get("webhook")
    webhook_name: str | None = None
    webhook_avatar_hash: str | None = None
    if message_type == 2:
        if not isinstance(raw_webhook, dict):
            raise ValueError("webhook message attribution is missing")
        webhook_name_value = raw_webhook.get("name")
        webhook_avatar_value = raw_webhook.get("avatar_hash")
        if (
            not isinstance(webhook_name_value, str)
            or not 1 <= len(webhook_name_value) <= 80
            or not webhook_name_value.strip()
        ):
            raise ValueError("webhook message name is invalid")
        if webhook_avatar_value is not None and (
            not isinstance(webhook_avatar_value, str)
            or len(webhook_avatar_value) != 64
            or any(character not in "0123456789abcdef" for character in webhook_avatar_value)
        ):
            raise ValueError("webhook message avatar is invalid")
        webhook_name = webhook_name_value
        webhook_avatar_hash = webhook_avatar_value
    elif raw_webhook is not None:
        raise ValueError("ordinary guild message contains webhook attribution")
    if raw.get("edited_at") is not None or raw.get("deleted_at") is not None:
        raise ValueError("guild create event contains mutation timestamps")
    raw_mention_refs = raw.get("mention_user_refs", [])
    if not isinstance(raw_mention_refs, list) or len(raw_mention_refs) > 100:
        raise ValueError("guild message mention list is invalid")
    mention_pairs: list[tuple[int, str]] = []
    for item in raw_mention_refs:
        if not isinstance(item, dict):
            raise ValueError("guild message mention reference is invalid")
        mention_pairs.append(
            (
                database_snowflake(item.get("id"), "mentioned user id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
        )
    mention_pairs = list(dict.fromkeys(mention_pairs))
    for user_id, user_domain in mention_pairs:
        mentioned_member = await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, user_id, user_domain),
        )
        if mentioned_member is None:
            raise ValueError("guild message mentions a user outside the guild")
    mention_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
    ]
    referenced_id_raw = raw.get("referenced_message_id")
    referenced_domain_raw = raw.get("referenced_message_domain")
    if (referenced_id_raw is None) != (referenced_domain_raw is None):
        raise ValueError("guild message reference is incomplete")
    referenced_id: int | None = None
    referenced_domain: str | None = None
    if referenced_id_raw is not None:
        candidate_id = database_snowflake(referenced_id_raw, "referenced message id")
        candidate_domain = normalize_domain(str(referenced_domain_raw))
        if candidate_domain != guild.origin_domain:
            raise ValueError("guild message reference has a non-authoritative origin")
        referenced = await session.get(Message, (candidate_id, candidate_domain))
        # Structural snapshots intentionally do not backfill pre-join history.
        # Preserve the FK invariant by omitting an otherwise-valid unavailable
        # historical reference; live references are retained normally.
        if referenced is not None:
            if (referenced.channel_id, referenced.channel_domain) != (
                channel.id,
                channel.origin_domain,
            ):
                raise ValueError("guild message reference is outside the channel")
            referenced_id = candidate_id
            referenced_domain = candidate_domain
    created_at = datetime.fromisoformat(str(raw["created_at"]))
    validate_snowflake_timestamp(
        message_id,
        created_at,
        "guild message",
        event_timestamp_ms=int(event["ts"]),
    )
    inserted = await session.scalar(
        pg_insert(Message)
        .values(
            id=message_id,
            origin_domain=message_origin,
            channel_id=channel_id,
            channel_domain=channel_domain,
            author_id=author.id,
            author_domain=author.origin_domain,
            content=content,
            e2ee=e2ee,
            message_type=message_type,
            flags=flags,
            client_nonce=client_nonce,
            referenced_message_id=referenced_id,
            referenced_message_domain=referenced_domain,
            mention_user_refs=mention_refs,
            webhook_name=webhook_name,
            webhook_avatar_hash=webhook_avatar_hash,
            created_at=created_at,
        )
        .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
        .returning(Message.id)
    )
    guild.last_event_seq = seq
    guild.next_event_seq = seq + 1
    guild.sync_status = "ready"
    if inserted is None:
        existing = await session.get(Message, (message_id, message_origin))
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
            existing.webhook_name,
            existing.webhook_avatar_hash,
            existing.created_at,
        ) != (
            channel.id,
            channel.origin_domain,
            author.id,
            author.origin_domain,
            content,
            message_type,
            flags,
            client_nonce,
            referenced_id,
            referenced_domain,
            mention_refs,
            webhook_name,
            webhook_avatar_hash,
            created_at,
        ):
            raise ValueError("guild message snowflake conflicts with another message")
        await replicate_message_attachments(session, settings, existing, author, raw_attachments)
        await advance_channel_cursor(session, channel, message_id, message_origin)
        return None
    message = await session.get(Message, (message_id, message_origin))
    if message is None:
        raise RuntimeError("replicated guild message disappeared")
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


async def apply_guild_member_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
) -> tuple[User, bool] | None:
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    seq = database_snowflake(
        event.get("seq") or event.get("context", {}).get("seq"), "guild sequence"
    )
    if seq <= locked.last_event_seq:
        return None
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict):
        raise ValueError("guild member event content is invalid")
    profile = RemoteUserProfile.model_validate(content.get("user"))
    event_actor = event.get("actor")
    if not isinstance(event_actor, dict) or (
        database_snowflake(event_actor.get("id"), "guild member event actor id"),
        str(event_actor.get("domain")),
    ) != (locked.owner_id, locked.owner_domain):
        raise ValueError("guild member event actor does not match its owner")
    user = await resolve_delegated_profile(
        session,
        settings,
        profile,
        authority_origin=locked.origin_domain,
    )
    joined_at = datetime.fromisoformat(str(content.get("joined_at")))
    if joined_at.tzinfo is None:
        raise ValueError("guild member join timestamp must include a timezone")
    member = await session.get(
        GuildMember,
        (locked.id, locked.origin_domain, user.id, user.origin_domain),
    )
    created = member is None
    if member is None:
        session.add(
            GuildMember(
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                user_id=user.id,
                user_domain=user.origin_domain,
                joined_at=joined_at,
            )
        )
    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"
    return user, created


async def apply_guild_redaction_event(
    session: AsyncSession,
    guild: Guild,
    event: dict[str, Any],
) -> None:
    """Advance a replica across an event it was never authorized to inspect."""

    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None:
        raise ValueError("replicated guild disappeared")
    if event.get("type") != "guild.event.redacted":
        raise ValueError("unsupported guild redaction event type")
    context = event.get("context")
    actor = event.get("actor")
    if not isinstance(context, dict) or (
        database_snowflake(context.get("guild_id"), "guild id"),
        str(context.get("guild_domain")),
    ) != (locked.id, locked.origin_domain):
        raise ValueError("guild redaction references the wrong guild")
    if not isinstance(actor, dict) or (
        database_snowflake(actor.get("id"), "guild redaction actor id"),
        str(actor.get("domain")),
    ) != (locked.owner_id, locked.owner_domain):
        raise ValueError("guild redaction actor does not match its owner")
    seq = database_snowflake(context.get("seq"), "guild sequence")
    if seq <= locked.last_event_seq:
        return
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("original_type"), str):
        raise ValueError("guild redaction content is invalid")
    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"


def _event_ref(raw: object, label: str) -> tuple[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} reference is invalid")
    return (
        database_snowflake(raw.get("id"), f"{label} id"),
        normalize_domain(str(raw.get("origin_domain", ""))),
    )


def _event_datetime(raw: object, label: str, *, optional: bool = False) -> datetime | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{label} timestamp is invalid")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"{label} timestamp is invalid") from None
    if value.tzinfo is None:
        raise ValueError(f"{label} timestamp lacks a timezone")
    return value


async def apply_guild_mutation_event(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    event: dict[str, Any],
) -> tuple[str, dict[str, object]] | None:
    """Apply one retained granular guild mutation to a remote replica."""

    event_type = str(event.get("type", ""))
    if event_type not in GUILD_MUTATION_EVENT_TYPES:
        raise ValueError("unsupported granular guild event type")
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("granular event references an invalid replicated guild")
    context = event.get("context")
    if not isinstance(context, dict) or (
        database_snowflake(context.get("guild_id"), "guild id"),
        normalize_domain(str(context.get("guild_domain", ""))),
    ) != (locked.id, locked.origin_domain):
        raise ValueError("granular event references the wrong guild")
    seq = database_snowflake(context.get("seq"), "guild sequence")
    if seq <= locked.last_event_seq:
        return None
    if seq != locked.last_event_seq + 1:
        locked.sync_status = "stale"
        raise GuildSequenceGap(locked.last_event_seq + 1, seq)
    content = event.get("content")
    if not isinstance(content, dict):
        raise ValueError("granular guild event content is invalid")
    raw_actor = event.get("actor")
    if not isinstance(raw_actor, dict):
        raise ValueError("granular guild event actor is invalid")
    actor_ref = (
        database_snowflake(raw_actor.get("id"), "guild event actor id"),
        normalize_domain(str(raw_actor.get("domain", ""))),
    )
    actor = await session.get(User, actor_ref)
    if actor_ref[1] != locked.origin_domain or actor is None:
        raise ValueError("granular guild event actor is unknown or not authoritative")
    dispatch_type = "GUILD_UPDATE"
    dispatch: dict[str, object] = {
        "guild_id": str(locked.id),
        "guild_domain": locked.origin_domain,
    }

    if event_type == "guild.update":
        raw = content.get("guild")
        if not isinstance(raw, dict) or _event_ref(raw, "guild") != (
            locked.id,
            locked.origin_domain,
        ):
            raise ValueError("guild update identity is invalid")
        name = raw.get("name")
        if not isinstance(name, str) or not 2 <= len(name) <= 100:
            raise ValueError("guild update name is invalid")
        locked.name = name
        for field, maximum in (("description", 500), ("icon_hash", 128), ("banner_hash", 128)):
            value = raw.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > maximum):
                raise ValueError(f"guild update {field} is invalid")
            setattr(locked, field, value)
        history_policy = raw.get("federated_history_policy", "disabled")
        if history_policy not in {"disabled", "full_retained"}:
            raise ValueError("guild history policy is invalid")
        locked.federated_history_policy = str(history_policy)
        locked.history_policy_generation = database_snowflake(
            raw.get("history_policy_generation", "1"), "history policy generation"
        )
        owner_ref = (
            database_snowflake(raw.get("owner_id"), "guild owner id"),
            normalize_domain(str(raw.get("owner_domain", locked.origin_domain))),
        )
        if owner_ref[1] != locked.origin_domain:
            raise ValueError("guild owner must belong to the guild home")
        owner = await session.get(User, owner_ref)
        owner_membership = await session.get(
            GuildMember,
            (locked.id, locked.origin_domain, owner_ref[0], owner_ref[1]),
        )
        if owner is None or owner_membership is None:
            raise ValueError("guild owner is not a guild member")
        locked.owner_id, locked.owner_domain = owner_ref
        locked.permission_generation = database_snowflake(
            raw.get("permission_generation", locked.permission_generation),
            "permission generation",
        )
        dispatch = {**dispatch, **raw}
    elif event_type in {"guild.channel.create", "guild.channel.update"}:
        raw = content.get("channel")
        channel_ref = _event_ref(raw, "channel")
        if not isinstance(raw, dict) or channel_ref[1] != locked.origin_domain:
            raise ValueError("channel mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "channel guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("channel mutation references the wrong guild")
        channel_type = raw.get("type")
        name = raw.get("name")
        topic = raw.get("topic")
        position = raw.get("position")
        slowmode = raw.get("rate_limit_per_user")
        history_policy = raw.get("federated_history_policy", "inherit")
        if isinstance(channel_type, bool) or channel_type not in {0, 2, 4, 5}:
            raise ValueError("channel mutation type is invalid")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("channel mutation name is invalid")
        if topic is not None and (not isinstance(topic, str) or len(topic) > 1024):
            raise ValueError("channel mutation topic is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("channel mutation position is invalid")
        if (
            isinstance(slowmode, bool)
            or not isinstance(slowmode, int)
            or not 0 <= slowmode <= 21600
        ):
            raise ValueError("channel mutation slowmode is invalid")
        if history_policy not in {"inherit", "disabled", "full_retained"}:
            raise ValueError("channel history policy is invalid")
        parent_id = (
            database_snowflake(raw.get("parent_id"), "parent channel id")
            if raw.get("parent_id") is not None
            else None
        )
        parent_domain = (
            normalize_domain(str(raw.get("parent_domain", ""))) if parent_id is not None else None
        )
        permissions_synced = raw.get("permissions_synced", parent_id is not None)
        if (
            not isinstance(permissions_synced, bool)
            or permissions_synced
            and (parent_id is None or channel_type == 4)
        ):
            raise ValueError("channel permission sync state is invalid")
        if parent_id is not None:
            parent = await session.get(Channel, (parent_id, parent_domain))
            if parent is None or (parent.guild_id, parent.guild_domain, parent.type) != (
                locked.id,
                locked.origin_domain,
                4,
            ):
                raise ValueError("channel mutation parent is invalid")
        created_floor_id = database_snowflake(raw.get("created_floor_id"), "channel history floor")
        channel = await session.get(Channel, channel_ref)
        if channel is None:
            channel = Channel(
                id=channel_ref[0],
                origin_domain=channel_ref[1],
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                type=int(channel_type),
                name=name,
                topic=topic,
                position=position,
                parent_id=parent_id,
                parent_domain=parent_domain,
                permissions_synced=permissions_synced,
                rate_limit_per_user=slowmode,
                federated_history_policy=str(history_policy),
                created_floor_id=created_floor_id,
            )
            session.add(channel)
        elif (channel.guild_id, channel.guild_domain) != (locked.id, locked.origin_domain):
            raise ValueError("channel mutation conflicts with another channel")
        else:
            channel.type = int(channel_type)
            channel.name = name
            channel.topic = topic
            channel.position = position
            channel.parent_id = parent_id
            channel.parent_domain = parent_domain
            channel.permissions_synced = permissions_synced
            channel.rate_limit_per_user = slowmode
            channel.federated_history_policy = str(history_policy)
            channel.unavailable = False
        dispatch_type = "CHANNEL_CREATE" if event_type.endswith("create") else "CHANNEL_UPDATE"
        dispatch = dict(raw)
    elif event_type == "guild.channel.delete":
        channel_ref = _event_ref(content.get("channel"), "channel")
        channel = await session.get(Channel, channel_ref)
        if channel is not None:
            if (channel.guild_id, channel.guild_domain) != (locked.id, locked.origin_domain):
                raise ValueError("channel deletion references the wrong guild")
            await purge_replicated_channel_cache(session, channel)
        dispatch_type = "CHANNEL_DELETE"
        dispatch = {
            **dispatch,
            "id": str(channel_ref[0]),
            "origin_domain": channel_ref[1],
        }
    elif event_type in {"guild.role.create", "guild.role.update"}:
        raw = content.get("role")
        role_ref = _event_ref(raw, "role")
        if not isinstance(raw, dict) or role_ref[1] != locked.origin_domain:
            raise ValueError("role mutation identity is invalid")
        if (
            database_snowflake(raw.get("guild_id"), "role guild id"),
            normalize_domain(str(raw.get("guild_domain", ""))),
        ) != (locked.id, locked.origin_domain):
            raise ValueError("role mutation references the wrong guild")
        name = raw.get("name")
        color = raw.get("color")
        position = raw.get("position")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("role mutation name is invalid")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError("role mutation color is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("role mutation position is invalid")
        permissions = database_snowflake(raw.get("permissions"), "role permissions")
        if permissions & ~ALL_PERMISSIONS:
            raise ValueError("role mutation contains unknown permissions")
        if not isinstance(raw.get("hoist"), bool) or not isinstance(raw.get("mentionable"), bool):
            raise ValueError("role mutation flags are invalid")
        role = await session.get(Role, role_ref)
        if role is None:
            role = Role(
                id=role_ref[0],
                origin_domain=role_ref[1],
                guild_id=locked.id,
                guild_domain=locked.origin_domain,
                name=name,
                color=color,
                permissions=permissions,
                position=position,
                hoist=bool(raw["hoist"]),
                mentionable=bool(raw["mentionable"]),
            )
            session.add(role)
        elif (role.guild_id, role.guild_domain) != (locked.id, locked.origin_domain):
            raise ValueError("role mutation conflicts with another role")
        else:
            role.name = name
            role.color = color
            role.permissions = permissions
            role.position = position
            role.hoist = bool(raw["hoist"])
            role.mentionable = bool(raw["mentionable"])
        dispatch = dict(raw)
    elif event_type == "guild.role.delete":
        role_ref = _event_ref(content.get("role"), "role")
        role = await session.get(Role, role_ref)
        if role is not None:
            if role.id == locked.id or (role.guild_id, role.guild_domain) != (
                locked.id,
                locked.origin_domain,
            ):
                raise ValueError("role deletion is invalid")
            await session.delete(role)
        dispatch["deleted_role_id"] = str(role_ref[0])
    elif event_type in {"guild.overwrite.upsert", "guild.overwrite.delete"}:
        raw = content.get("overwrite")
        if not isinstance(raw, dict):
            raise ValueError("overwrite mutation is invalid")
        channel_ref = _event_ref(raw.get("channel"), "channel")
        target_ref = _event_ref(raw.get("target"), "overwrite target")
        target_type = raw.get("target_type")
        allow = (
            database_snowflake(raw.get("allow"), "overwrite allow mask")
            if event_type.endswith("upsert")
            else 0
        )
        deny = (
            database_snowflake(raw.get("deny"), "overwrite deny mask")
            if event_type.endswith("upsert")
            else 0
        )
        if target_type not in {"role", "member"} or allow & deny:
            raise ValueError("overwrite mutation masks or target type are invalid")
        if (allow | deny) & ~ALL_PERMISSIONS:
            raise ValueError("overwrite mutation contains unknown permissions")
        channel = await session.get(Channel, channel_ref)
        if channel is None or (channel.guild_id, channel.guild_domain) != (
            locked.id,
            locked.origin_domain,
        ):
            raise ValueError("overwrite channel is invalid")
        if target_type == "role":
            role_target = await session.get(Role, target_ref)
            valid_target = role_target is not None and (
                role_target.guild_id,
                role_target.guild_domain,
            ) == (
                locked.id,
                locked.origin_domain,
            )
        else:
            member_target = await session.get(
                GuildMember,
                (locked.id, locked.origin_domain, target_ref[0], target_ref[1]),
            )
            valid_target = member_target is not None
        if not valid_target:
            raise ValueError("overwrite target is invalid")
        if event_type.endswith("upsert"):
            await session.execute(
                pg_insert(ChannelOverwrite)
                .values(
                    channel_id=channel.id,
                    channel_domain=channel.origin_domain,
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    target_id=target_ref[0],
                    target_domain=target_ref[1],
                    target_type=target_type,
                    allow=allow,
                    deny=deny,
                )
                .on_conflict_do_update(
                    index_elements=[
                        "channel_id",
                        "channel_domain",
                        "target_id",
                        "target_domain",
                        "target_type",
                    ],
                    set_={"allow": allow, "deny": deny},
                )
            )
        else:
            await session.execute(
                delete(ChannelOverwrite).where(
                    ChannelOverwrite.channel_id == channel.id,
                    ChannelOverwrite.channel_domain == channel.origin_domain,
                    ChannelOverwrite.target_id == target_ref[0],
                    ChannelOverwrite.target_domain == target_ref[1],
                    ChannelOverwrite.target_type == target_type,
                )
            )
        dispatch_type = "CHANNEL_UPDATE"
        dispatch = {"id": str(channel.id), "origin_domain": channel.origin_domain}
    elif event_type == "guild.member.update":
        raw = content.get("member")
        if not isinstance(raw, dict):
            raise ValueError("member mutation is invalid")
        user_ref = _event_ref(raw.get("user"), "member user")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        if member is None:
            raise ValueError("member mutation references an unknown member")
        nickname = raw.get("nickname")
        if nickname is not None and (not isinstance(nickname, str) or len(nickname) > 100):
            raise ValueError("member nickname is invalid")
        member.nickname = nickname
        member.timeout_until = _event_datetime(
            raw.get("timeout_until"), "member timeout", optional=True
        )
        timeout_indefinite = raw.get("timeout_indefinite", False)
        if not isinstance(timeout_indefinite, bool):
            raise ValueError("member indefinite timeout is invalid")
        if timeout_indefinite and member.timeout_until is not None:
            raise ValueError("member timeout modes conflict")
        member.timeout_indefinite = timeout_indefinite
        timeout_reason = raw.get("timeout_reason")
        if timeout_reason is not None and (
            not isinstance(timeout_reason, str) or len(timeout_reason) > 512
        ):
            raise ValueError("member timeout reason is invalid")
        if timeout_reason is not None and not (timeout_indefinite or member.timeout_until):
            raise ValueError("member timeout reason exists without an active timeout")
        member.timeout_reason = timeout_reason
        voice_flags = raw.get("voice_flags")
        if voice_flags is not None:
            if isinstance(voice_flags, bool) or not isinstance(voice_flags, int) or voice_flags < 0:
                raise ValueError("member voice flags are invalid")
            member.voice_flags = voice_flags
        member_version = database_snowflake(raw.get("member_version"), "member version")
        if member_version < member.member_version:
            raise ValueError("member version regressed")
        member.member_version = member_version
        dispatch_type = "GUILD_MEMBER_UPDATE"
        dispatch = dict(raw)
    elif event_type == "guild.member.remove":
        user_ref = _event_ref(content.get("user"), "member user")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        if member is not None:
            if (member.user_id, member.user_domain) == (locked.owner_id, locked.owner_domain):
                raise ValueError("guild owner cannot be removed")
            await session.delete(member)
        dispatch_type = "GUILD_MEMBER_REMOVE"
        dispatch = {**dispatch, "user_id": str(user_ref[0]), "user_domain": user_ref[1]}
    elif event_type == "guild.members.origin.remove":
        origin_domain = normalize_domain(str(content.get("origin_domain", "")))
        if origin_domain == locked.owner_domain:
            raise ValueError("guild owner origin cannot be removed")
        await session.execute(
            delete(GuildMember).where(
                GuildMember.guild_id == locked.id,
                GuildMember.guild_domain == locked.origin_domain,
                GuildMember.user_domain == origin_domain,
            )
        )
        dispatch_type = "GUILD_UPDATE"
        dispatch = {**dispatch, "members_removed_origin": origin_domain}
    elif event_type in {"guild.member.role.add", "guild.member.role.remove"}:
        user_ref = _event_ref(content.get("user"), "member user")
        role_ref = _event_ref(content.get("role"), "role")
        member = await session.get(
            GuildMember, (locked.id, locked.origin_domain, user_ref[0], user_ref[1])
        )
        role = await session.get(Role, role_ref)
        if (
            member is None
            or role is None
            or (role.guild_id, role.guild_domain)
            != (
                locked.id,
                locked.origin_domain,
            )
        ):
            raise ValueError("member-role mutation is invalid")
        if event_type.endswith("add"):
            await session.execute(
                pg_insert(MemberRole)
                .values(
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    role_id=role_ref[0],
                    role_domain=role_ref[1],
                )
                .on_conflict_do_nothing()
            )
        else:
            await session.execute(
                delete(MemberRole).where(
                    MemberRole.guild_id == locked.id,
                    MemberRole.guild_domain == locked.origin_domain,
                    MemberRole.user_id == user_ref[0],
                    MemberRole.user_domain == user_ref[1],
                    MemberRole.role_id == role_ref[0],
                    MemberRole.role_domain == role_ref[1],
                )
            )
        member_version = database_snowflake(content.get("member_version"), "member version")
        if member_version < member.member_version:
            raise ValueError("member version regressed")
        member.member_version = member_version
        dispatch_type = "GUILD_MEMBER_UPDATE"
        dispatch = {
            **dispatch,
            "user_id": str(user_ref[0]),
            "user_domain": user_ref[1],
            "role_id": str(role_ref[0]),
            "role_domain": role_ref[1],
        }
    elif event_type in {"guild.ban.add", "guild.ban.remove"}:
        user_ref = _event_ref(content.get("user"), "banned user")
        user = await session.get(User, user_ref)
        if event_type.endswith("add") and user is not None:
            expires_at = _event_datetime(content.get("expires_at"), "ban expiry", optional=True)
            await session.execute(
                pg_insert(Ban)
                .values(
                    guild_id=locked.id,
                    guild_domain=locked.origin_domain,
                    user_id=user_ref[0],
                    user_domain=user_ref[1],
                    reason=None,
                    actor_id=actor_ref[0],
                    actor_domain=actor_ref[1],
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=["guild_id", "guild_domain", "user_id", "user_domain"],
                    set_={
                        "actor_id": actor_ref[0],
                        "actor_domain": actor_ref[1],
                        "expires_at": expires_at,
                    },
                )
            )
        else:
            await session.execute(
                delete(Ban).where(
                    Ban.guild_id == locked.id,
                    Ban.guild_domain == locked.origin_domain,
                    Ban.user_id == user_ref[0],
                    Ban.user_domain == user_ref[1],
                )
            )
        dispatch = {**dispatch, "user_id": str(user_ref[0]), "user_domain": user_ref[1]}
    elif event_type in {"guild.message.update", "guild.message.delete"}:
        message_ref = _event_ref(content.get("message"), "message")
        message = await session.get(Message, message_ref)
        if message is not None:
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
            if channel is None or (channel.guild_id, channel.guild_domain) != (
                locked.id,
                locked.origin_domain,
            ):
                raise ValueError("message mutation references the wrong guild")
            if event_type.endswith("update"):
                raw_message = content.get("message")
                if not isinstance(raw_message, dict):
                    raise ValueError("message update is invalid")
                value = raw_message.get("content")
                e2ee = validate_e2ee_envelope(raw_message.get("e2ee"))
                if value is not None and (
                    not isinstance(value, str) or not 1 <= len(value) <= 4000
                ):
                    raise ValueError("message update content is invalid")
                if (value is None) == (e2ee is None):
                    raise ValueError("message update must contain one plaintext or encrypted body")
                edited_at = _event_datetime(raw_message.get("edited_at"), "message edit")
                if edited_at is None:
                    raise ValueError("message edit timestamp is invalid")
                if message.deleted_at is not None or (
                    message.edited_at is not None and edited_at < message.edited_at
                ):
                    raise ValueError("message edit regressed authoritative state")
                message.content = value
                message.e2ee = e2ee
                message.edited_at = edited_at
            else:
                deleted_at = _event_datetime(content.get("deleted_at"), "message deletion")
                if deleted_at is None:
                    raise ValueError("message deletion timestamp is invalid")
                message.content = None
                message.e2ee = None
                message.deleted_at = deleted_at
        dispatch_type = "MESSAGE_UPDATE" if event_type.endswith("update") else "MESSAGE_DELETE"
        dispatch = {
            "id": str(message_ref[0]),
            "origin_domain": message_ref[1],
            "channel_id": str(context.get("channel_id", "")),
            "channel_domain": str(context.get("channel_domain", "")),
        }
        if event_type.endswith("update") and isinstance(content.get("message"), dict):
            dispatch.update(content["message"])
    elif event_type == "guild.message.purge":
        author_ref = _event_ref(content.get("author"), "purged author")
        cutoff = _event_datetime(content.get("created_after"), "message purge cutoff")
        deleted_at = _event_datetime(content.get("deleted_at"), "message purge")
        if cutoff is None or deleted_at is None:
            raise ValueError("message purge timestamps are invalid")
        channel_ids = select(Channel.id).where(
            Channel.guild_id == locked.id, Channel.guild_domain == locked.origin_domain
        )
        await session.execute(
            update(Message)
            .where(
                Message.channel_id.in_(channel_ids),
                Message.channel_domain == locked.origin_domain,
                Message.author_id == author_ref[0],
                Message.author_domain == author_ref[1],
                Message.created_at >= cutoff,
                Message.deleted_at.is_(None),
            )
            .values(content=None, e2ee=None, deleted_at=deleted_at)
        )
        dispatch_type = "MESSAGE_DELETE"
        dispatch = {
            **dispatch,
            "purged_author_id": str(author_ref[0]),
            "purged_author_domain": author_ref[1],
        }
    elif event_type in {"guild.reaction.add", "guild.reaction.remove"}:
        message_ref = _event_ref(content.get("message"), "reaction message")
        user_ref = _event_ref(content.get("user"), "reaction user")
        emoji = content.get("emoji")
        if not isinstance(emoji, str) or not 1 <= len(emoji) <= 255:
            raise ValueError("reaction emoji is invalid")
        message = await session.get(Message, message_ref)
        user = await session.get(User, user_ref)
        if message is not None and user is not None:
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
            if channel is None or (channel.guild_id, channel.guild_domain) != (
                locked.id,
                locked.origin_domain,
            ):
                raise ValueError("reaction mutation references the wrong guild")
            if event_type.endswith("add") and message.deleted_at is not None:
                raise ValueError("reaction mutation references a deleted message")
            if event_type.endswith("add"):
                await session.execute(
                    pg_insert(Reaction)
                    .values(
                        message_id=message_ref[0],
                        message_domain=message_ref[1],
                        user_id=user_ref[0],
                        user_domain=user_ref[1],
                        emoji_key=emoji,
                    )
                    .on_conflict_do_nothing()
                )
            else:
                await session.execute(
                    delete(Reaction).where(
                        Reaction.message_id == message_ref[0],
                        Reaction.message_domain == message_ref[1],
                        Reaction.user_id == user_ref[0],
                        Reaction.user_domain == user_ref[1],
                        Reaction.emoji_key == emoji,
                    )
                )
        dispatch_type = "MESSAGE_UPDATE"
        dispatch = {
            "id": str(message_ref[0]),
            "origin_domain": message_ref[1],
            "reaction": emoji,
            "removed": event_type.endswith("remove"),
            "user_id": str(user_ref[0]),
            "user_domain": user_ref[1],
        }
    elif event_type in {"guild.pin.add", "guild.pin.remove"}:
        message_ref = _event_ref(content.get("message"), "pinned message")
        channel_ref = _event_ref(content.get("channel"), "pin channel")
        message = await session.get(Message, message_ref)
        channel = await session.get(Channel, channel_ref)
        if message is not None and channel is not None:
            if (message.channel_id, message.channel_domain) != channel_ref or (
                channel.guild_id,
                channel.guild_domain,
            ) != (locked.id, locked.origin_domain):
                raise ValueError("pin mutation references the wrong channel")
            if event_type.endswith("add"):
                if message.deleted_at is not None:
                    raise ValueError("pin mutation references a deleted message")
                await session.execute(
                    pg_insert(Pin)
                    .values(
                        channel_id=channel_ref[0],
                        channel_domain=channel_ref[1],
                        message_id=message_ref[0],
                        message_domain=message_ref[1],
                        pinned_by_id=actor_ref[0],
                        pinned_by_domain=actor_ref[1],
                    )
                    .on_conflict_do_nothing()
                )
            else:
                await session.execute(
                    delete(Pin).where(
                        Pin.channel_id == channel_ref[0],
                        Pin.channel_domain == channel_ref[1],
                        Pin.message_id == message_ref[0],
                        Pin.message_domain == message_ref[1],
                    )
                )
        dispatch_type = "MESSAGE_UPDATE"
        dispatch = {
            "id": str(message_ref[0]),
            "origin_domain": message_ref[1],
            "channel_id": str(channel_ref[0]),
            "channel_domain": channel_ref[1],
            "pinned": event_type.endswith("add"),
        }

    locked.last_event_seq = seq
    locked.next_event_seq = seq + 1
    locked.sync_status = "ready"
    if event_type in HISTORY_ACCESS_MUTATION_EVENT_TYPES:
        from app.federation.history import purge_ineligible_federated_history

        await purge_ineligible_federated_history(session, settings, locked)
    return dispatch_type, dispatch


async def lock_proxy_nonce(
    session: AsyncSession, guild: Guild, actor: User, channel: Channel, client_nonce: str
) -> None:
    lock_key = (
        f"kaede-proxy:{guild.origin_domain}:{guild.id}:{channel.id}:"
        f"{actor.origin_domain}:{actor.id}:{client_nonce}"
    )
    await session.scalar(select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0))))


async def guild_event_for_message(
    session: AsyncSession, guild: Guild, message: Message
) -> GuildEvent | None:
    result: GuildEvent | None = await session.scalar(
        select(GuildEvent)
        .where(
            GuildEvent.guild_id == guild.id,
            GuildEvent.guild_domain == guild.origin_domain,
            GuildEvent.envelope["content"]["message"]["id"].as_string() == str(message.id),
        )
        .order_by(GuildEvent.seq)
        .limit(1)
    )
    return result


def proxy_event(
    event_type: str, seq: int, actor: User, content: dict[str, object]
) -> dict[str, object]:
    return {
        "event_id": new_guild_event_id(),
        "type": event_type,
        "seq": str(seq),
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": str(actor.id), "domain": actor.origin_domain},
        "content": content,
    }


async def fetch_guild_snapshot(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    guild_id: int,
) -> dict[str, Any]:
    combined: dict[str, Any] | None = None
    member_cursor: tuple[str, int] | None = None
    member_snapshot_at: str | None = None
    snapshot_seq: str | None = None
    total_bytes = 0
    for _page in range(MAX_SNAPSHOT_PAGES):
        query: dict[str, str] = {}
        if member_cursor is not None:
            if member_snapshot_at is None or snapshot_seq is None:
                raise RuntimeError("guild snapshot cursor state is incomplete")
            query = {
                "member_after_domain": member_cursor[0],
                "member_after_id": str(member_cursor[1]),
                "member_snapshot_at": member_snapshot_at,
                "member_snapshot_seq": snapshot_seq,
            }
        response = await signed_request(
            session,
            settings,
            "GET",
            origin,
            f"/_kaede/v1/guilds/{guild_id}/snapshot",
            query=query,
        )
        if response.status_code != 200:
            raise RuntimeError("guild full resynchronization failed")
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("guild snapshot payload is invalid")
        total_bytes += len(response.content)
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise RuntimeError("guild snapshot exceeds its aggregate byte limit")
        members = payload.get("members")
        member_roles = payload.get("member_roles")
        if not isinstance(members, list) or not isinstance(member_roles, list):
            raise RuntimeError("guild snapshot member page is invalid")
        if len(members) > 1_000 or len(member_roles) > 100_000:
            raise RuntimeError("guild snapshot member page exceeds its protocol bound")
        page_snapshot_at = payload.get("member_snapshot_at")
        if not isinstance(page_snapshot_at, str):
            raise RuntimeError("guild snapshot is missing its membership watermark")
        raw_snapshot_seq = payload.get("snapshot_seq")
        if not isinstance(raw_snapshot_seq, str):
            raise RuntimeError("guild snapshot sequence is invalid")
        try:
            parsed_snapshot_at = datetime.fromisoformat(page_snapshot_at)
            database_snowflake(raw_snapshot_seq, "snapshot sequence")
        except ValueError:
            raise RuntimeError("guild snapshot watermark or sequence is invalid") from None
        if parsed_snapshot_at.tzinfo is None:
            raise RuntimeError("guild snapshot membership watermark lacks a timezone")
        if combined is None:
            combined = payload
            combined["members"] = list(members)
            combined["member_roles"] = list(member_roles)
            member_snapshot_at = page_snapshot_at
            snapshot_seq = raw_snapshot_seq
        else:
            if payload.get("snapshot_seq") != combined.get("snapshot_seq"):
                raise RuntimeError("guild changed while its snapshot was paged")
            if page_snapshot_at != member_snapshot_at:
                raise RuntimeError(
                    "guild membership watermark changed while its snapshot was paged"
                )
            for field in ("guild", "roles", "channels", "overwrites"):
                if payload.get(field) != combined.get(field):
                    raise RuntimeError("guild structure changed while its snapshot was paged")
            combined["members"].extend(members)
            combined["member_roles"].extend(member_roles)
        if (
            len(combined["members"]) > MAX_SNAPSHOT_MEMBERS
            or len(combined["member_roles"]) > MAX_SNAPSHOT_MEMBER_ROLES
        ):
            raise RuntimeError("guild snapshot exceeds its aggregate record limit")
        next_cursor = payload.get("next_member_cursor")
        if next_cursor is None:
            combined["next_member_cursor"] = None
            return combined
        if not isinstance(next_cursor, dict):
            raise RuntimeError("guild snapshot returned an invalid member cursor")
        try:
            next_ref = (
                normalize_domain(str(next_cursor.get("user_domain", ""))),
                database_snowflake(next_cursor.get("user_id"), "member cursor id"),
            )
            last_profile = RemoteUserProfile.model_validate(members[-1]["user"])
        except (IndexError, TypeError, ValueError):
            raise RuntimeError("guild snapshot returned an invalid member cursor") from None
        if next_ref != (last_profile.origin_domain, int(last_profile.id)):
            raise RuntimeError("guild snapshot member cursor does not match its page")
        if member_cursor is not None and next_ref <= member_cursor:
            raise RuntimeError("guild snapshot member cursor did not advance")
        member_cursor = next_ref
    raise RuntimeError("guild snapshot exceeded the member page limit")


async def synchronize_guild(
    session: AsyncSession, settings: Settings, guild: Guild
) -> list[Message]:
    applied: list[Message] = []
    for _page in range(100):
        response = await signed_request(
            session,
            settings,
            "GET",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/events",
            query={"after_seq": str(guild.last_event_seq)},
        )
        if response.status_code == 410:
            snapshot = await fetch_guild_snapshot(session, settings, guild.origin_domain, guild.id)
            await apply_guild_snapshot(
                session,
                settings,
                snapshot,
                expected_origin=guild.origin_domain,
                expected_guild_id=guild.id,
            )
            return []
        if response.status_code != 200:
            raise RuntimeError("guild gap fill failed")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise RuntimeError("guild gap fill returned an invalid payload")
        events = payload["events"]
        for raw_event in events:
            envelope = await validated_event_envelope(
                session, settings, guild.origin_domain, raw_event
            )
            event = envelope.model_dump(mode="json")
            if event.get("type") in {"guild.message.create", "guild.message.committed"}:
                message = await apply_guild_message_event(session, settings, guild, event)
                if message is not None:
                    applied.append(message)
            elif event.get("type") == "guild.member.add":
                await apply_guild_member_event(session, settings, guild, event)
            elif event.get("type") == "guild.event.redacted":
                if guild_event_requires_snapshot(event):
                    snapshot = await fetch_guild_snapshot(
                        session, settings, guild.origin_domain, guild.id
                    )
                    await apply_guild_snapshot(
                        session,
                        settings,
                        snapshot,
                        expected_origin=guild.origin_domain,
                        expected_guild_id=guild.id,
                    )
                    return applied
                await apply_guild_redaction_event(session, guild, event)
            elif event.get("type") in GUILD_MUTATION_EVENT_TYPES:
                if guild_event_requires_snapshot(event):
                    snapshot = await fetch_guild_snapshot(
                        session, settings, guild.origin_domain, guild.id
                    )
                    await apply_guild_snapshot(
                        session,
                        settings,
                        snapshot,
                        expected_origin=guild.origin_domain,
                        expected_guild_id=guild.id,
                    )
                    return applied
                await apply_guild_mutation_event(session, settings, guild, event)
            else:
                raise RuntimeError("guild gap fill returned an unsupported event type")
        latest_seq = database_snowflake(payload.get("latest_seq"), "latest guild sequence")
        if guild.last_event_seq >= latest_seq:
            guild.sync_status = "ready"
            guild.unavailable = False
            return applied
        if not events:
            raise RuntimeError("guild gap fill made no progress")
    raise RuntimeError("guild gap fill exceeded the synchronization page limit")


def validate_guild_snapshot(
    snapshot: dict[str, Any], *, expected_origin: str, expected_guild_id: int
) -> None:
    raw_guild = snapshot.get("guild")
    if not isinstance(raw_guild, dict):
        raise ValueError("guild snapshot is missing guild metadata")
    guild_id = database_snowflake(raw_guild.get("id"), "guild id")
    origin = str(raw_guild.get("origin_domain"))
    if (guild_id, origin) != (expected_guild_id, expected_origin):
        raise ValueError("guild snapshot identity does not match the requested guild")
    guild_name = raw_guild.get("name")
    if not isinstance(guild_name, str) or not 2 <= len(guild_name) <= 100:
        raise ValueError("guild snapshot name is invalid")
    for field, maximum in (("description", 500), ("icon_hash", 128), ("banner_hash", 128)):
        value = raw_guild.get(field)
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError(f"guild snapshot {field} is invalid")
    database_snowflake(raw_guild.get("permission_generation"), "permission generation")
    history_policy = raw_guild.get("federated_history_policy", "disabled")
    if history_policy not in {"disabled", "full_retained"}:
        raise ValueError("guild snapshot history policy is invalid")
    database_snowflake(raw_guild.get("history_policy_generation", "1"), "history policy generation")
    snapshot_seq = database_snowflake(snapshot.get("snapshot_seq"), "snapshot sequence")
    del snapshot_seq
    roles = snapshot.get("roles")
    channels = snapshot.get("channels")
    members = snapshot.get("members")
    member_roles = snapshot.get("member_roles")
    overwrites = snapshot.get("overwrites")
    if (
        not isinstance(roles, list)
        or not isinstance(channels, list)
        or not isinstance(members, list)
        or not isinstance(member_roles, list)
        or not isinstance(overwrites, list)
    ):
        raise ValueError("guild snapshot collections are invalid")
    if (
        len(roles) > 10_000
        or len(channels) > 10_000
        or len(members) > MAX_SNAPSHOT_MEMBERS
        or len(member_roles) > MAX_SNAPSHOT_MEMBER_ROLES
        or len(overwrites) > MAX_SNAPSHOT_OVERWRITES
    ):
        raise ValueError("guild snapshot collection exceeds its protocol bound")
    role_refs: set[tuple[int, str]] = set()
    for raw in roles:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot role is invalid")
        ref = (database_snowflake(raw.get("id"), "role id"), str(raw.get("origin_domain")))
        if ref[1] != origin or ref in role_refs:
            raise ValueError("guild snapshot contains an invalid role identity")
        name = raw.get("name")
        color = raw.get("color")
        position = raw.get("position")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("guild snapshot role name is invalid")
        if isinstance(color, bool) or not isinstance(color, int) or not 0 <= color <= 0xFFFFFF:
            raise ValueError("guild snapshot role color is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("guild snapshot role position is invalid")
        permissions = database_snowflake(raw.get("permissions"), "role permissions")
        if permissions & ~ALL_PERMISSIONS:
            raise ValueError("guild snapshot role contains unknown permissions")
        if not isinstance(raw.get("hoist"), bool) or not isinstance(raw.get("mentionable"), bool):
            raise ValueError("guild snapshot role flags are invalid")
        role_refs.add(ref)
    channel_refs: set[tuple[int, str]] = set()
    for raw in channels:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot channel is invalid")
        ref = (
            database_snowflake(raw.get("id"), "channel id"),
            str(raw.get("origin_domain")),
        )
        if ref[1] != origin or ref in channel_refs:
            raise ValueError("guild snapshot contains an invalid channel identity")
        channel_type = raw.get("type")
        name = raw.get("name")
        topic = raw.get("topic")
        position = raw.get("position")
        slowmode = raw.get("rate_limit_per_user")
        history_policy = raw.get("federated_history_policy", "inherit")
        parent_id = raw.get("parent_id")
        permissions_synced = raw.get("permissions_synced", parent_id is not None)
        if isinstance(channel_type, bool) or channel_type not in {0, 2, 4, 5}:
            raise ValueError("guild snapshot channel type is invalid")
        if not isinstance(name, str) or not 1 <= len(name) <= 100:
            raise ValueError("guild snapshot channel name is invalid")
        if topic is not None and (not isinstance(topic, str) or len(topic) > 1024):
            raise ValueError("guild snapshot channel topic is invalid")
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError("guild snapshot channel position is invalid")
        if (
            isinstance(slowmode, bool)
            or not isinstance(slowmode, int)
            or not 0 <= slowmode <= 21_600
        ):
            raise ValueError("guild snapshot channel slowmode is invalid")
        if history_policy not in {"inherit", "disabled", "full_retained"}:
            raise ValueError("guild snapshot channel history policy is invalid")
        if (
            not isinstance(permissions_synced, bool)
            or permissions_synced
            and (parent_id is None or channel_type == 4)
        ):
            raise ValueError("guild snapshot channel permission sync state is invalid")
        database_snowflake(raw.get("created_floor_id"), "channel history floor")
        channel_refs.add(ref)
    for raw in channels:
        parent_id = raw.get("parent_id")
        if parent_id is not None:
            parent_ref = (
                database_snowflake(parent_id, "parent channel id"),
                str(raw.get("parent_domain")),
            )
            if parent_ref not in channel_refs:
                raise ValueError("guild snapshot channel parent is outside the guild")
    member_refs: set[tuple[int, str]] = set()
    for raw in members:
        if not isinstance(raw, dict) or not isinstance(raw.get("user"), dict):
            raise ValueError("guild snapshot member is invalid")
        profile = RemoteUserProfile.model_validate(raw["user"])
        ref = (int(profile.id), profile.origin_domain)
        if ref in member_refs:
            raise ValueError("guild snapshot contains a duplicate member")
        nickname = raw.get("nickname")
        if nickname is not None and (not isinstance(nickname, str) or len(nickname) > 100):
            raise ValueError("guild snapshot member nickname is invalid")
        try:
            joined_at = datetime.fromisoformat(str(raw.get("joined_at")))
            timeout_until = (
                datetime.fromisoformat(str(raw["timeout_until"]))
                if raw.get("timeout_until") is not None
                else None
            )
        except ValueError:
            raise ValueError("guild snapshot member timestamp is invalid") from None
        if joined_at.tzinfo is None or (timeout_until is not None and timeout_until.tzinfo is None):
            raise ValueError("guild snapshot member timestamp lacks a timezone")
        timeout_indefinite = raw.get("timeout_indefinite", False)
        if not isinstance(timeout_indefinite, bool):
            raise ValueError("guild snapshot member indefinite timeout is invalid")
        if timeout_indefinite and timeout_until is not None:
            raise ValueError("guild snapshot member timeout modes conflict")
        timeout_reason = raw.get("timeout_reason")
        if timeout_reason is not None and (
            not isinstance(timeout_reason, str) or len(timeout_reason) > 512
        ):
            raise ValueError("guild snapshot member timeout reason is invalid")
        if timeout_reason is not None and not (timeout_indefinite or timeout_until):
            raise ValueError(
                "guild snapshot member timeout reason exists without an active timeout"
            )
        voice_flags = raw.get("voice_flags")
        if isinstance(voice_flags, bool) or not isinstance(voice_flags, int) or voice_flags < 0:
            raise ValueError("guild snapshot member voice flags are invalid")
        database_snowflake(raw.get("member_version"), "member version")
        member_refs.add(ref)
    owner_ref = (
        database_snowflake(raw_guild.get("owner_id"), "guild owner id"),
        str(raw_guild.get("owner_domain")),
    )
    if owner_ref not in member_refs or owner_ref[1] != origin:
        raise ValueError("guild snapshot does not contain its owner")
    member_role_refs: set[tuple[tuple[int, str], tuple[int, str]]] = set()
    for raw in member_roles:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot member-role assignment is invalid")
        user_ref = (
            database_snowflake(raw.get("user_id"), "member id"),
            str(raw.get("user_domain")),
        )
        role_ref = (
            database_snowflake(raw.get("role_id"), "role id"),
            str(raw.get("role_domain")),
        )
        if user_ref not in member_refs or role_ref not in role_refs:
            raise ValueError("guild snapshot member-role reference is outside the guild")
        assignment = (user_ref, role_ref)
        if assignment in member_role_refs:
            raise ValueError("guild snapshot contains a duplicate member-role assignment")
        member_role_refs.add(assignment)
    overwrite_refs: set[tuple[tuple[int, str], str, tuple[int, str]]] = set()
    for raw in overwrites:
        if not isinstance(raw, dict):
            raise ValueError("guild snapshot overwrite is invalid")
        channel_ref = (
            database_snowflake(raw.get("channel_id"), "channel id"),
            str(raw.get("channel_domain")),
        )
        target_type = raw.get("target_type")
        target_ref = (
            database_snowflake(raw.get("target_id"), "overwrite target id"),
            str(raw.get("target_domain")),
        )
        if channel_ref not in channel_refs or target_type not in {"role", "member"}:
            raise ValueError("guild snapshot overwrite is outside the guild")
        expected_targets = role_refs if target_type == "role" else member_refs
        if target_ref not in expected_targets:
            raise ValueError("guild snapshot overwrite target is outside the guild")
        allow = database_snowflake(raw.get("allow"), "overwrite allow mask")
        deny = database_snowflake(raw.get("deny"), "overwrite deny mask")
        if allow & deny or (allow | deny) & ~ALL_PERMISSIONS:
            raise ValueError("guild snapshot overwrite masks overlap")
        overwrite_ref = (channel_ref, str(target_type), target_ref)
        if overwrite_ref in overwrite_refs:
            raise ValueError("guild snapshot contains a duplicate overwrite")
        overwrite_refs.add(overwrite_ref)


def tombstone_omitted_replicated_channel(channel: Channel) -> None:
    """Keep only the channel identity needed to restore a later snapshot."""

    channel.unavailable = True
    channel.name = None
    channel.topic = None
    channel.position = 0
    channel.parent_id = None
    channel.parent_domain = None
    channel.rate_limit_per_user = 0
    channel.last_message_id = None
    channel.last_message_domain = None


async def purge_replicated_channel_cache(
    session: AsyncSession,
    channel: Channel,
) -> None:
    """Logically and physically evict inaccessible replicated channel data.

    Access is revoked immediately by tombstoning the channel and deleting its
    message rows. Cached remote object bytes are marked expired in the same
    transaction; the storage GC performs retryable physical deletion.
    """

    if channel.guild_domain is None or channel.origin_domain != channel.guild_domain:
        raise ValueError("only replicated guild channels may be purged")
    message_refs = select(Message.id, Message.origin_domain).where(
        Message.channel_id == channel.id,
        Message.channel_domain == channel.origin_domain,
    )
    attachment_refs = select(Attachment.id, Attachment.origin_domain).where(
        tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs)
    )
    refs = list(await session.execute(attachment_refs))
    if refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(
                    RemoteMediaCache.attachment_id,
                    RemoteMediaCache.origin_domain,
                ).in_(refs)
            )
            .values(expires_at=datetime.now(UTC))
        )
    await session.execute(
        update(ReadState)
        .where(
            ReadState.channel_id == channel.id,
            ReadState.channel_domain == channel.origin_domain,
        )
        .values(last_message_id=None, last_message_domain=None, mention_count=0)
    )
    await session.execute(
        update(Message)
        .where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
            Message.referenced_message_id.is_not(None),
        )
        .values(referenced_message_id=None, referenced_message_domain=None)
    )
    channel.last_message_id = None
    channel.last_message_domain = None
    await session.flush()
    await session.execute(
        delete(Message).where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
        )
    )
    tombstone_omitted_replicated_channel(channel)


async def apply_guild_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    user_id: int,
    user_domain: str,
) -> bool:
    """Remove local access even when the home will no longer serve a snapshot."""

    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("access revocation references an invalid replicated guild")
    if normalize_domain(user_domain) != settings.domain:
        raise ValueError("access revocation target does not belong to this instance")
    membership = await session.get(
        GuildMember,
        (locked.id, locked.origin_domain, user_id, user_domain),
    )
    removed = membership is not None
    if membership is not None:
        await session.delete(membership)
        await session.flush()
    remaining_local_member = await session.scalar(
        select(GuildMember.user_id)
        .where(
            GuildMember.guild_id == locked.id,
            GuildMember.guild_domain == locked.origin_domain,
            GuildMember.user_domain == settings.domain,
        )
        .limit(1)
    )
    if remaining_local_member is None:
        locked.unavailable = True
        locked.sync_status = "stale"
        channels = list(
            await session.scalars(
                select(Channel).where(
                    Channel.guild_id == locked.id,
                    Channel.guild_domain == locked.origin_domain,
                )
            )
        )
        for channel in channels:
            await purge_replicated_channel_cache(session, channel)
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([channel.id for channel in channels]),
                ChannelOverwrite.channel_domain == locked.origin_domain,
            )
        )
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, locked)
    return removed


async def apply_guild_instance_access_revocation(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    target_domain: str,
) -> list[int]:
    """Apply an authority-signed revocation for every local guild member."""

    if normalize_domain(target_domain) != settings.domain:
        raise ValueError("instance access revocation was addressed to another instance")
    locked = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked is None or locked.origin_domain == settings.domain:
        raise ValueError("instance revocation references an invalid replicated guild")
    removed = list(
        await session.scalars(
            delete(GuildMember)
            .where(
                GuildMember.guild_id == locked.id,
                GuildMember.guild_domain == locked.origin_domain,
                GuildMember.user_domain == settings.domain,
            )
            .returning(GuildMember.user_id)
        )
    )
    locked.unavailable = True
    locked.sync_status = "stale"
    channels = list(
        await session.scalars(
            select(Channel).where(
                Channel.guild_id == locked.id,
                Channel.guild_domain == locked.origin_domain,
            )
        )
    )
    for channel in channels:
        await purge_replicated_channel_cache(session, channel)
    if channels:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([channel.id for channel in channels]),
                ChannelOverwrite.channel_domain == locked.origin_domain,
            )
        )
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, locked)
    return removed


def guild_snapshot_payload(
    guild: Guild,
    roles: list[Role],
    channels: list[Channel],
    members: Sequence[tuple[GuildMember, User]],
    member_roles: list[MemberRole],
    overwrites: list[ChannelOverwrite],
    *,
    member_snapshot_at: datetime,
    next_member_cursor: tuple[str, int] | None = None,
) -> dict[str, Any]:
    visible_channel_refs = {(channel.id, channel.origin_domain) for channel in channels}
    return {
        "snapshot_seq": str(guild.next_event_seq - 1),
        "member_snapshot_at": member_snapshot_at.isoformat(),
        "next_member_cursor": (
            {
                "user_domain": next_member_cursor[0],
                "user_id": str(next_member_cursor[1]),
            }
            if next_member_cursor is not None
            else None
        ),
        "guild": {
            "id": str(guild.id),
            "origin_domain": guild.origin_domain,
            "name": guild.name,
            "description": guild.description,
            "icon_hash": guild.icon_hash,
            "banner_hash": guild.banner_hash,
            "owner_id": str(guild.owner_id),
            "owner_domain": guild.owner_domain,
            "permission_generation": str(guild.permission_generation),
            "federated_history_policy": guild.federated_history_policy,
            "history_policy_generation": str(guild.history_policy_generation),
        },
        "roles": [
            {
                "id": str(role.id),
                "origin_domain": role.origin_domain,
                "name": role.name,
                "color": role.color,
                "permissions": str(role.permissions),
                "position": role.position,
                "hoist": role.hoist,
                "mentionable": role.mentionable,
            }
            for role in roles
        ],
        "channels": [
            {
                "id": str(channel.id),
                "origin_domain": channel.origin_domain,
                "type": channel.type,
                "name": channel.name,
                "topic": channel.topic,
                "position": channel.position,
                # A child may be visible through an explicit overwrite while its
                # category is hidden. Flatten that child in the peer snapshot
                # instead of leaking the hidden parent or emitting a dangling FK.
                "parent_id": (
                    str(channel.parent_id)
                    if channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                    else None
                ),
                "parent_domain": (
                    channel.parent_domain
                    if channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                    else None
                ),
                "permissions_synced": bool(
                    getattr(channel, "permissions_synced", False)
                    and channel.parent_id is not None
                    and (channel.parent_id, channel.parent_domain) in visible_channel_refs
                ),
                "rate_limit_per_user": channel.rate_limit_per_user,
                "federated_history_policy": channel.federated_history_policy,
                "created_floor_id": str(channel.created_floor_id),
            }
            for channel in channels
        ],
        "members": [
            {
                "user": {
                    "id": str(user.id),
                    "origin_domain": user.origin_domain,
                    "username": user.username,
                    "display_name": user.display_name,
                    "avatar_hash": user.avatar_hash,
                },
                "nickname": member.nickname,
                "joined_at": member.joined_at.isoformat(),
                "timeout_until": (
                    member.timeout_until.isoformat() if member.timeout_until else None
                ),
                "timeout_indefinite": member.timeout_indefinite,
                "timeout_reason": member.timeout_reason,
                "voice_flags": member.voice_flags,
                "member_version": str(member.member_version),
            }
            for member, user in members
        ],
        "member_roles": [
            {
                "user_id": str(item.user_id),
                "user_domain": item.user_domain,
                "role_id": str(item.role_id),
                "role_domain": item.role_domain,
            }
            for item in member_roles
        ],
        "overwrites": [
            {
                "channel_id": str(item.channel_id),
                "channel_domain": item.channel_domain,
                "target_id": str(item.target_id),
                "target_domain": item.target_domain,
                "target_type": item.target_type,
                "allow": str(item.allow),
                "deny": str(item.deny),
            }
            for item in overwrites
        ],
    }


async def apply_guild_snapshot(
    session: AsyncSession,
    settings: Settings,
    snapshot: dict[str, Any],
    *,
    expected_origin: str,
    expected_guild_id: int,
) -> Guild:
    validate_guild_snapshot(
        snapshot, expected_origin=expected_origin, expected_guild_id=expected_guild_id
    )
    raw_guild = snapshot["guild"]
    origin = str(raw_guild["origin_domain"])
    if origin == settings.domain:
        raise HTTPException(status_code=409, detail={"code": "GUILD_IS_LOCAL"})
    users = {
        (
            int(raw["user"]["id"]),
            str(raw["user"]["origin_domain"]),
        ): await resolve_delegated_profile(
            session,
            settings,
            RemoteUserProfile.model_validate(raw["user"]),
            authority_origin=origin,
        )
        for raw in snapshot["members"]
    }
    guild_id = int(raw_guild["id"])
    guild = await session.get(Guild, (guild_id, origin))
    if guild is None:
        guild = Guild(
            id=guild_id,
            origin_domain=origin,
            name=str(raw_guild["name"]),
            owner_id=int(raw_guild["owner_id"]),
            owner_domain=str(raw_guild["owner_domain"]),
        )
        session.add(guild)
        await session.flush()
    else:
        guild = await session.scalar(
            select(Guild)
            .where(Guild.id == guild_id, Guild.origin_domain == origin)
            .with_for_update()
        )
        if guild is None:
            raise RuntimeError("replicated guild disappeared during snapshot application")
    guild.name = str(raw_guild["name"])
    guild.owner_id = int(raw_guild["owner_id"])
    guild.owner_domain = str(raw_guild["owner_domain"])
    guild.description = raw_guild.get("description")
    guild.icon_hash = raw_guild.get("icon_hash")
    guild.banner_hash = raw_guild.get("banner_hash")
    guild.permission_generation = int(raw_guild.get("permission_generation", 1))
    guild.federated_history_policy = str(raw_guild.get("federated_history_policy", "disabled"))
    guild.history_policy_generation = int(raw_guild.get("history_policy_generation", 1))
    guild.last_event_seq = int(snapshot["snapshot_seq"])
    guild.next_event_seq = guild.last_event_seq + 1
    guild.sync_status = "ready"
    guild.unavailable = False
    role_refs = {(int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot["roles"]}
    channel_refs = {(int(raw["id"]), str(raw["origin_domain"])) for raw in snapshot["channels"]}
    member_refs = {
        (int(raw["user"]["id"]), str(raw["user"]["origin_domain"])) for raw in snapshot["members"]
    }
    existing_roles = list(
        await session.scalars(
            select(Role).where(Role.guild_id == guild.id, Role.guild_domain == origin)
        )
    )
    for role in existing_roles:
        if (role.id, role.origin_domain) not in role_refs:
            await session.delete(role)
    existing_channels = list(
        await session.scalars(
            select(Channel).where(Channel.guild_id == guild.id, Channel.guild_domain == origin)
        )
    )
    omitted_channel_ids: list[int] = []
    for channel in existing_channels:
        if (channel.id, channel.origin_domain) not in channel_refs:
            omitted_channel_ids.append(channel.id)
            await purge_replicated_channel_cache(session, channel)
    if omitted_channel_ids:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_(omitted_channel_ids),
                ChannelOverwrite.channel_domain == origin,
            )
        )
    existing_members = list(
        await session.scalars(
            select(GuildMember).where(
                GuildMember.guild_id == guild.id, GuildMember.guild_domain == origin
            )
        )
    )
    for member in existing_members:
        if (member.user_id, member.user_domain) not in member_refs:
            await session.delete(member)
    for raw in snapshot["roles"]:
        loaded_role = await session.get(Role, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_role is None:
            loaded_role = Role(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                name=str(raw["name"]),
                position=int(raw["position"]),
            )
            session.add(loaded_role)
        elif (loaded_role.guild_id, loaded_role.guild_domain) != (guild.id, guild.origin_domain):
            raise ValueError("snapshot role identity conflicts with another guild")
        loaded_role.name = str(raw["name"])
        loaded_role.color = int(raw["color"])
        loaded_role.permissions = int(raw["permissions"])
        loaded_role.position = int(raw["position"])
        loaded_role.hoist = bool(raw["hoist"])
        loaded_role.mentionable = bool(raw["mentionable"])
    for raw in snapshot["channels"]:
        loaded_channel = await session.get(Channel, (int(raw["id"]), str(raw["origin_domain"])))
        if loaded_channel is None:
            loaded_channel = Channel(
                id=int(raw["id"]),
                origin_domain=str(raw["origin_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                type=int(raw["type"]),
                created_floor_id=int(raw["created_floor_id"]),
            )
            session.add(loaded_channel)
        elif (loaded_channel.guild_id, loaded_channel.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ):
            raise ValueError("snapshot channel identity conflicts with another guild")
        loaded_channel.unavailable = False
        loaded_channel.type = int(raw["type"])
        loaded_channel.created_floor_id = int(raw["created_floor_id"])
        loaded_channel.name = raw.get("name")
        loaded_channel.topic = raw.get("topic")
        loaded_channel.position = int(raw["position"])
        loaded_channel.parent_id = int(raw["parent_id"]) if raw.get("parent_id") else None
        loaded_channel.parent_domain = raw.get("parent_domain")
        loaded_channel.permissions_synced = bool(raw.get("permissions_synced", False))
        loaded_channel.rate_limit_per_user = int(raw["rate_limit_per_user"])
        loaded_channel.federated_history_policy = str(
            raw.get("federated_history_policy", "inherit")
        )
    await session.flush()
    if channel_refs:
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id.in_([item[0] for item in channel_refs]),
                ChannelOverwrite.channel_domain == origin,
            )
        )
    for raw in snapshot["overwrites"]:
        session.add(
            ChannelOverwrite(
                channel_id=int(raw["channel_id"]),
                channel_domain=str(raw["channel_domain"]),
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                target_id=int(raw["target_id"]),
                target_domain=str(raw["target_domain"]),
                target_type=str(raw["target_type"]),
                allow=int(raw["allow"]),
                deny=int(raw["deny"]),
            )
        )
    for raw in snapshot["members"]:
        user_ref = (int(raw["user"]["id"]), str(raw["user"]["origin_domain"]))
        user = users[user_ref]
        loaded_member = await session.get(
            GuildMember, (guild.id, origin, user.id, user.origin_domain)
        )
        if loaded_member is None:
            loaded_member = GuildMember(
                guild_id=guild.id,
                guild_domain=origin,
                user_id=user.id,
                user_domain=user.origin_domain,
                joined_at=datetime.fromisoformat(str(raw["joined_at"])),
            )
            session.add(loaded_member)
        loaded_member.nickname = raw.get("nickname")
        loaded_member.timeout_until = (
            datetime.fromisoformat(str(raw["timeout_until"])) if raw.get("timeout_until") else None
        )
        loaded_member.timeout_indefinite = bool(raw.get("timeout_indefinite", False))
        loaded_member.timeout_reason = raw.get("timeout_reason")
        loaded_member.voice_flags = int(raw.get("voice_flags", 0))
        loaded_member.member_version = int(raw.get("member_version", 1))
    await session.flush()
    await session.execute(
        delete(MemberRole).where(MemberRole.guild_id == guild.id, MemberRole.guild_domain == origin)
    )
    for raw in snapshot["member_roles"]:
        session.add(
            MemberRole(
                guild_id=guild.id,
                guild_domain=origin,
                user_id=int(raw["user_id"]),
                user_domain=str(raw["user_domain"]),
                role_id=int(raw["role_id"]),
                role_domain=str(raw["role_domain"]),
            )
        )
    await session.flush()
    from app.federation.history import purge_ineligible_federated_history

    await purge_ineligible_federated_history(session, settings, guild)
    return guild
