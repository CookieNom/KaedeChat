from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import channel_encryption_policy_payload
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.models import (
    Attachment,
    AuditLogEntry,
    Ban,
    Channel,
    Emoji,
    Guild,
    GuildInstanceBan,
    GuildMember,
    MemberRole,
    Message,
    Relationship,
    Role,
    ThreadMember,
    User,
)
from app.media.payloads import attachment_payload as media_attachment_payload


def resource_version(value: object) -> str | None:
    updated_at = getattr(value, "updated_at", None)
    return updated_at.isoformat() if updated_at is not None else None


def materialize_channel_created_at(channel: Channel) -> datetime:
    """Give pre-flush channels the same immutable time on every instance.

    ``created_at`` is database-defaulted, but channel payloads and federation
    mutations are deliberately rendered before the flush that allocates that
    default. Channel IDs already carry the authoritative millisecond, so use it
    once and persist the value on the model instead of publishing a receipt-time
    timestamp that could make forum Date Posted ordering diverge.
    """

    created_at = channel.created_at
    if created_at is None:
        timestamp_ms = EPOCH_MS + (channel.id >> (WORKER_BITS + SEQUENCE_BITS))
        created_at = datetime.fromtimestamp(timestamp_ms / 1000, UTC)
        channel.created_at = created_at
    return created_at


def public_user_display_name(user: User) -> str:
    """Return a user-facing label without exposing internal placeholders."""

    if not user.profile_resolved:
        return f"Remote user · {user.origin_domain}"
    return user.display_name or user.username


def user_payload(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "origin_domain": user.origin_domain,
        "username": user.username,
        "display_name": user.display_name,
        "avatar_hash": user.avatar_hash,
        "banner_hash": user.banner_hash,
        "bio": user.bio,
        "custom_status": user.custom_status,
        "profile_version": str(user.profile_version),
        "e2ee_device_generation": str(getattr(user, "e2ee_device_generation", 0)),
        "profile_resolved": user.profile_resolved,
        "handle": f"{user.username}@{user.origin_domain}",
        "account_type": user.account_type,
        "bot": user.account_type == "bot",
    }


def guild_payload(guild: Guild) -> dict[str, object]:
    return {
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
        "unavailable": guild.unavailable,
        # Replica health is safe client state. Expose the stable code only;
        # ``sync_error`` can contain operator-facing diagnostics and must stay
        # server-side.
        "sync_status": guild.sync_status,
        "sync_error_code": guild.sync_error_code,
        "version": resource_version(guild),
    }


def emoji_payload(emoji: Emoji) -> dict[str, object]:
    return {
        "id": str(emoji.id),
        "origin_domain": emoji.origin_domain,
        "guild_id": str(emoji.guild_id),
        "guild_domain": emoji.guild_domain,
        "name": emoji.name,
        "animated": emoji.animated,
        "media_hash": emoji.media_hash,
        "version": resource_version(emoji),
    }


def channel_payload(channel: Channel) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": str(channel.id),
        "origin_domain": channel.origin_domain,
        "guild_id": str(channel.guild_id) if channel.guild_id is not None else None,
        "guild_domain": channel.guild_domain,
        "type": channel.type,
        "created_at": materialize_channel_created_at(channel).isoformat(),
        "name": channel.name,
        "topic": channel.topic,
        "position": channel.position,
        "parent_id": str(channel.parent_id) if channel.parent_id is not None else None,
        "parent_domain": channel.parent_domain,
        "permissions_synced": channel.permissions_synced,
        "rate_limit_per_user": channel.rate_limit_per_user,
        "flags": channel.flags,
        "owner_id": str(channel.owner_id) if channel.owner_id is not None else None,
        "owner_domain": channel.owner_domain,
        "archived": channel.archived,
        "locked": channel.locked,
        "invitable": channel.invitable,
        "auto_archive_duration": channel.auto_archive_duration,
        "archive_timestamp": (
            channel.archive_timestamp.isoformat() if channel.archive_timestamp is not None else None
        ),
        "message_count": channel.message_count,
        "total_message_sent": channel.total_message_sent,
        "member_count": (
            min(50, channel.member_count) if channel.member_count is not None else None
        ),
        "starter_message_id": (
            str(channel.starter_message_id) if channel.starter_message_id is not None else None
        ),
        "starter_message_domain": channel.starter_message_domain,
        "default_auto_archive_duration": channel.default_auto_archive_duration,
        "default_thread_rate_limit_per_user": channel.default_thread_rate_limit_per_user,
        "available_tags": channel.available_tags,
        "applied_tag_ids": channel.applied_tag_ids,
        # Discord calls the request field ``applied_tags``. Return both during
        # the compatibility window; both contain IDs, never duplicate state.
        "applied_tags": channel.applied_tag_ids,
        "default_reaction_emoji": channel.default_reaction_emoji,
        "default_sort_order": channel.default_sort_order,
        "default_forum_layout": channel.default_forum_layout,
        "e2ee_required": channel.e2ee_required,
        "federated_history_policy": channel.federated_history_policy,
        "encryption_mode": channel.encryption_mode,
        "encryption_state": channel.encryption_state,
        "encryption_policy_generation": str(channel.encryption_policy_generation),
        "encryption_protocol": channel.encryption_protocol,
        "encryption_suite": channel.encryption_suite,
        "encryption_group_id": channel.encryption_group_id,
        "encryption_epoch": (
            str(channel.encryption_epoch) if channel.encryption_epoch is not None else None
        ),
        "encryption_activated_at": (
            channel.encryption_activated_at.isoformat()
            if channel.encryption_activated_at is not None
            else None
        ),
        "encryption_policy": channel_encryption_policy_payload(channel),
        "search_available": channel.encryption_mode == "plaintext",
        "last_message_id": (
            str(channel.last_thread_id)
            if channel.type == 15 and channel.last_thread_id is not None
            else str(channel.last_message_id)
            if channel.last_message_id is not None
            else str(channel.starter_message_id)
            if channel.type in {10, 11, 12}
            and channel.starter_message_id == channel.id
            and channel.starter_message_domain == channel.origin_domain
            else None
        ),
        "last_message_domain": (
            channel.last_thread_domain
            if channel.type == 15
            else channel.last_message_domain
            if channel.last_message_id is not None
            else channel.starter_message_domain
            if channel.type in {10, 11, 12}
            and channel.starter_message_id == channel.id
            and channel.starter_message_domain == channel.origin_domain
            else None
        ),
        "version": resource_version(channel),
    }
    if channel.type in {10, 11, 12}:
        payload["thread_metadata"] = {
            "archived": bool(channel.archived),
            "auto_archive_duration": channel.auto_archive_duration,
            "archive_timestamp": (
                channel.archive_timestamp.isoformat()
                if channel.archive_timestamp is not None
                else None
            ),
            "locked": bool(channel.locked),
            "invitable": channel.invitable,
            "create_timestamp": channel.created_at.isoformat(),
        }
    return payload


def thread_member_payload(member: ThreadMember) -> dict[str, object]:
    return {
        "id": str(member.thread_id),
        "thread_domain": member.thread_domain,
        "guild_id": str(member.guild_id),
        "guild_domain": member.guild_domain,
        "user_id": str(member.user_id),
        "user_domain": member.user_domain,
        "join_timestamp": member.joined_at.isoformat(),
        "flags": member.flags,
        "notification_level": member.notification_level,
    }


async def rich_thread_member_payload(
    session: AsyncSession,
    member: ThreadMember,
) -> dict[str, object]:
    """Render a thread member with Discord's optional guild member envelope."""

    rendered = thread_member_payload(member)
    guild_member = await session.get(
        GuildMember,
        (member.guild_id, member.guild_domain, member.user_id, member.user_domain),
    )
    user = await session.get(User, (member.user_id, member.user_domain))
    if guild_member is not None and user is not None:
        role_ids = list(
            await session.scalars(
                select(MemberRole.role_id).where(
                    MemberRole.guild_id == member.guild_id,
                    MemberRole.guild_domain == member.guild_domain,
                    MemberRole.user_id == member.user_id,
                    MemberRole.user_domain == member.user_domain,
                )
            )
        )
        rendered["member"] = member_payload(guild_member, user, role_ids)
    else:
        rendered["member"] = None
    # Presence is nullable even for GUILD_MEMBERS deliveries and Kaede does not
    # synthesize presence from durable membership state.
    rendered["presence"] = None
    return rendered


def thread_source_starter_payload(
    thread: Channel,
    source: dict[str, object],
) -> dict[str, object]:
    """Project a parent source message as Discord's type-21 child starter."""

    # Type 21 is a system wrapper, not a second copy of the source body. Keep
    # stable identity/attribution/time fields on top and resolve the actual
    # source only through referenced_message when history policy permits it.
    return {
        "id": str(source["id"]),
        "origin_domain": source["origin_domain"],
        "channel_id": str(thread.id),
        "channel_domain": thread.origin_domain,
        "author_id": source.get("author_id"),
        "author_domain": source.get("author_domain"),
        "author": source.get("author"),
        "content": None,
        "e2ee": None,
        "encryption_policy_generation": str(thread.encryption_policy_generation),
        "encryption_epoch": None,
        "message_type": 21,
        "flags": 0,
        "client_nonce": None,
        "referenced_message_id": None,
        "referenced_message_domain": None,
        "message_reference": {
            "type": 0,
            "message_id": str(source["id"]),
            "message_domain": source["origin_domain"],
            "channel_id": source["channel_id"],
            "channel_domain": source["channel_domain"],
            "guild_id": str(thread.guild_id),
            "guild_domain": thread.guild_domain,
        },
        "referenced_message": None if source.get("deleted_at") is not None else source,
        "mention_user_refs": [],
        "attachments": [],
        "embeds": [],
        "components": [],
        "reaction_counts": {},
        "reacted_emoji": [],
        "webhook_id": None,
        "webhook": None,
        "edited_at": None,
        "deleted_at": None,
        "created_at": source.get("created_at") or thread.created_at.isoformat(),
    }


def role_payload(role: Role) -> dict[str, object]:
    return {
        "id": str(role.id),
        "origin_domain": role.origin_domain,
        "guild_id": str(role.guild_id),
        "guild_domain": role.guild_domain,
        "name": role.name,
        "color": role.color,
        "permissions": str(role.permissions),
        "position": role.position,
        "hoist": role.hoist,
        "mentionable": role.mentionable,
        "version": resource_version(role),
    }


def member_payload(
    member: GuildMember,
    user: User,
    role_ids: list[int] | None = None,
    *,
    include_private_authority_state: bool = False,
) -> dict[str, object]:
    return {
        "guild_id": str(member.guild_id),
        "guild_domain": member.guild_domain,
        "user": user_payload(user),
        "nickname": member.nickname,
        "joined_at": member.joined_at.isoformat(),
        "timeout_until": member.timeout_until.isoformat() if member.timeout_until else None,
        "timeout_indefinite": member.timeout_indefinite,
        # Legacy replicas may still contain authority-only voice flags. Never
        # expose them from a remote guild; zero retains old-client shape.
        "voice_flags": member.voice_flags if include_private_authority_state else 0,
        "member_version": str(member.member_version),
        "role_ids": [str(role_id) for role_id in (role_ids or [])],
    }


def ban_payload(ban: Ban, user: User) -> dict[str, object]:
    return {
        "guild_id": str(ban.guild_id),
        "guild_domain": ban.guild_domain,
        "user": user_payload(user),
        "reason": ban.reason,
        "actor_id": str(ban.actor_id),
        "created_at": ban.created_at.isoformat(),
        "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
    }


def instance_ban_payload(ban: GuildInstanceBan) -> dict[str, object]:
    return {
        "guild_id": str(ban.guild_id),
        "guild_domain": ban.guild_domain,
        "instance_domain": ban.instance_domain,
        "reason": ban.reason,
        "actor_id": str(ban.actor_id),
        "actor_domain": ban.actor_domain,
        "created_at": ban.created_at.isoformat(),
        "expires_at": ban.expires_at.isoformat() if ban.expires_at else None,
    }


def audit_payload(entry: AuditLogEntry) -> dict[str, object]:
    return {
        "id": str(entry.id),
        "guild_id": str(entry.guild_id),
        "guild_domain": entry.guild_domain,
        "actor_id": str(entry.actor_id),
        "actor_domain": entry.actor_domain,
        "action_type": entry.action_type,
        "target_type": entry.target_type,
        "target_ref": entry.target_ref,
        "reason": entry.reason,
        "changes": entry.changes,
        "created_at": entry.created_at.isoformat(),
    }


def relationship_payload(relationship: Relationship, target: User) -> dict[str, object]:
    return {
        "type": relationship.type,
        "user": user_payload(target),
        "created_at": relationship.created_at.isoformat(),
        "updated_at": relationship.updated_at.isoformat(),
    }


def dm_channel_payload(
    channel: Channel,
    recipients: list[User],
    *,
    conversation: object | None = None,
    history: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        **channel_payload(channel),
        "recipients": [user_payload(user) for user in recipients],
    }
    if conversation is not None:
        owner_id = getattr(conversation, "owner_id", None)
        payload.update(
            {
                "conversation_type": str(getattr(conversation, "type", "direct")),
                "owner_id": str(owner_id) if owner_id is not None else None,
                "owner_domain": getattr(conversation, "owner_domain", None),
            }
        )
    if history is not None:
        payload.update(history)
    return payload


def attachment_payload(attachment: Attachment) -> dict[str, object]:
    return media_attachment_payload(attachment, include_lifecycle=False)


def message_payload(
    message: Message,
    author: User | None = None,
    attachments: list[Attachment] | None = None,
) -> dict[str, object]:
    webhook = (
        {
            "id": str(message.webhook_id) if message.webhook_id is not None else None,
            "name": message.webhook_name,
            "avatar_hash": message.webhook_avatar_hash,
        }
        if message.webhook_name is not None
        else None
    )
    return {
        "id": str(message.id),
        "origin_domain": message.origin_domain,
        "channel_id": str(message.channel_id),
        "channel_domain": message.channel_domain,
        "author_id": str(message.author_id),
        "author_domain": message.author_domain,
        "author": user_payload(author) if author is not None and webhook is None else None,
        "content": None if message.deleted_at is not None else message.content,
        "e2ee": None if message.deleted_at is not None else message.e2ee,
        "encryption_policy_generation": str(message.encryption_policy_generation),
        "encryption_epoch": (
            str(message.encryption_epoch) if message.encryption_epoch is not None else None
        ),
        "message_type": message.message_type,
        "flags": message.flags,
        "client_nonce": message.client_nonce,
        "referenced_message_id": (
            str(message.referenced_message_id)
            if message.referenced_message_id is not None
            else None
        ),
        "referenced_message_domain": message.referenced_message_domain,
        "mention_user_refs": message.mention_user_refs,
        "attachments": [attachment_payload(item) for item in (attachments or [])],
        "webhook_id": str(message.webhook_id) if message.webhook_id is not None else None,
        "webhook": webhook,
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
        "created_at": message.created_at.isoformat(),
    }


async def render_message_payload(
    session: AsyncSession,
    message: Message,
    author: User | None = None,
) -> dict[str, object]:
    """Render a stored message with its complete, ordered attachment set."""

    if author is None:
        author = await session.get(User, (message.author_id, message.author_domain))
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id)
        )
    )
    rendered = message_payload(message, author, attachments)
    if message.flags & (1 << 5) or message.message_type == 18:
        thread = await session.get(Channel, (message.id, message.origin_domain))
        if thread is not None and thread.type in {10, 11, 12} and not thread.unavailable:
            rendered["thread"] = channel_payload(thread)
            if message.message_type == 18:
                rendered["message_reference"] = {
                    "type": 0,
                    "message_id": None,
                    "message_domain": None,
                    "channel_id": str(thread.id),
                    "channel_domain": thread.origin_domain,
                    "guild_id": str(thread.guild_id),
                    "guild_domain": thread.guild_domain,
                }
    return rendered
