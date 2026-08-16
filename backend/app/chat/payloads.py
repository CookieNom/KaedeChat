from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Attachment,
    AuditLogEntry,
    Ban,
    Channel,
    Emoji,
    Guild,
    GuildInstanceBan,
    GuildMember,
    Message,
    Relationship,
    Role,
    User,
)


def resource_version(value: object) -> str | None:
    updated_at = getattr(value, "updated_at", None)
    return updated_at.isoformat() if updated_at is not None else None


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
    return {
        "id": str(channel.id),
        "origin_domain": channel.origin_domain,
        "guild_id": str(channel.guild_id) if channel.guild_id is not None else None,
        "guild_domain": channel.guild_domain,
        "type": channel.type,
        "name": channel.name,
        "topic": channel.topic,
        "position": channel.position,
        "parent_id": str(channel.parent_id) if channel.parent_id is not None else None,
        "parent_domain": channel.parent_domain,
        "permissions_synced": channel.permissions_synced,
        "rate_limit_per_user": channel.rate_limit_per_user,
        "federated_history_policy": channel.federated_history_policy,
        "encryption_mode": channel.encryption_mode,
        "search_available": channel.encryption_mode == "plaintext",
        "last_message_id": (
            str(channel.last_message_id) if channel.last_message_id is not None else None
        ),
        "last_message_domain": channel.last_message_domain,
        "version": resource_version(channel),
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
    return {
        "id": str(attachment.id),
        "origin_domain": attachment.origin_domain,
        "filename": attachment.filename,
        "content_type": attachment.detected_content_type or attachment.content_type,
        "size": attachment.size,
        "width": attachment.width,
        "height": attachment.height,
        "blurhash": attachment.blurhash,
        "scan_status": attachment.scan_status,
        "variants": attachment.variants,
    }


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
    return message_payload(message, author, attachments)
