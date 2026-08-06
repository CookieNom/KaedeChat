"""Authoritative permission requirements for API operations.

Routers use operation names instead of repeating ad-hoc masks.  Conditional
requirements (attachments, author-vs-moderator actions, and target hierarchy)
remain explicit at the commit point and are described alongside the base mask.
"""

from dataclasses import dataclass

from app.core.permissions import Permission


@dataclass(frozen=True, slots=True)
class EndpointPermissionContract:
    operation: str
    permission: Permission
    scope: str
    conditional: str | None = None


_CONTRACTS = (
    EndpointPermissionContract("guild.update", Permission.MANAGE_GUILD, "guild"),
    EndpointPermissionContract("guild.audit.list", Permission.VIEW_AUDIT_LOG, "guild"),
    EndpointPermissionContract("guild.invite.list", Permission.MANAGE_GUILD, "guild"),
    EndpointPermissionContract("guild.invite.revoke", Permission.MANAGE_GUILD, "guild"),
    EndpointPermissionContract("guild.emoji.manage", Permission.MANAGE_EMOJIS, "guild"),
    EndpointPermissionContract("guild.asset.manage", Permission.MANAGE_GUILD, "guild"),
    EndpointPermissionContract("guild.webhook.list", Permission.MANAGE_WEBHOOKS, "guild"),
    EndpointPermissionContract("channel.create", Permission.MANAGE_CHANNELS, "guild"),
    EndpointPermissionContract("channel.update", Permission.MANAGE_CHANNELS, "channel"),
    EndpointPermissionContract("channel.delete", Permission.MANAGE_CHANNELS, "channel"),
    EndpointPermissionContract("channel.reorder", Permission.MANAGE_CHANNELS, "guild"),
    EndpointPermissionContract("channel.overwrite.list", Permission.MANAGE_ROLES, "channel"),
    EndpointPermissionContract(
        "channel.overwrite.put",
        Permission.MANAGE_ROLES,
        "channel",
        "actor may only grant held bits and manage lower targets",
    ),
    EndpointPermissionContract(
        "channel.overwrite.delete",
        Permission.MANAGE_ROLES,
        "channel",
        "actor must manage the target",
    ),
    EndpointPermissionContract("channel.permissions.sync", Permission.MANAGE_ROLES, "channel"),
    EndpointPermissionContract("role.create", Permission.MANAGE_ROLES, "guild"),
    EndpointPermissionContract("role.reorder", Permission.MANAGE_ROLES, "guild"),
    EndpointPermissionContract(
        "role.update",
        Permission.MANAGE_ROLES,
        "guild",
        "role hierarchy and changed-bit checks apply",
    ),
    EndpointPermissionContract(
        "role.delete", Permission.MANAGE_ROLES, "guild", "role hierarchy applies"
    ),
    EndpointPermissionContract(
        "member.role.update", Permission.MANAGE_ROLES, "guild", "member and role hierarchy apply"
    ),
    EndpointPermissionContract(
        "member.kick", Permission.KICK_MEMBERS, "guild", "member hierarchy applies"
    ),
    EndpointPermissionContract(
        "member.ban", Permission.BAN_MEMBERS, "guild", "member hierarchy applies"
    ),
    EndpointPermissionContract(
        "member.timeout", Permission.MODERATE_MEMBERS, "guild", "member hierarchy applies"
    ),
    EndpointPermissionContract("member.list", Permission.VIEW_CHANNEL, "guild"),
    EndpointPermissionContract("member.nickname.self", Permission.CHANGE_NICKNAME, "guild"),
    EndpointPermissionContract(
        "member.nickname.other",
        Permission.MANAGE_NICKNAMES,
        "guild",
        "member hierarchy applies",
    ),
    EndpointPermissionContract("ban.list", Permission.BAN_MEMBERS, "guild"),
    EndpointPermissionContract("ban.remove", Permission.BAN_MEMBERS, "guild"),
    EndpointPermissionContract("invite.create", Permission.CREATE_INVITE, "channel"),
    EndpointPermissionContract(
        "message.list", Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY, "channel"
    ),
    EndpointPermissionContract(
        "message.create",
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES,
        "channel",
        "ATTACH_FILES is additionally required when attachments are committed",
    ),
    EndpointPermissionContract("message.delete.other", Permission.MANAGE_MESSAGES, "channel"),
    EndpointPermissionContract("message.delete.self", Permission.VIEW_CHANNEL, "channel"),
    EndpointPermissionContract(
        "message.edit.self",
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES,
        "channel",
        "only the author may edit",
    ),
    EndpointPermissionContract("message.bulk_delete", Permission.MANAGE_MESSAGES, "channel"),
    EndpointPermissionContract(
        "reaction.create",
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY | Permission.ADD_REACTIONS,
        "channel",
    ),
    EndpointPermissionContract("reaction.delete.self", Permission.VIEW_CHANNEL, "channel"),
    EndpointPermissionContract("reaction.delete.other", Permission.MANAGE_MESSAGES, "channel"),
    EndpointPermissionContract("pin.update", Permission.MANAGE_MESSAGES, "channel"),
    EndpointPermissionContract(
        "pin.list", Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY, "channel"
    ),
    EndpointPermissionContract(
        "typing.publish", Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES, "channel"
    ),
    EndpointPermissionContract(
        "read_state.update",
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
        "channel",
    ),
    EndpointPermissionContract("webhook.manage", Permission.MANAGE_WEBHOOKS, "channel"),
    EndpointPermissionContract(
        "attachment.create",
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES | Permission.ATTACH_FILES,
        "channel",
    ),
    EndpointPermissionContract(
        "voice.join", Permission.VIEW_CHANNEL | Permission.CONNECT, "channel"
    ),
    EndpointPermissionContract("voice.mute", Permission.MUTE_MEMBERS, "channel"),
    EndpointPermissionContract("voice.deafen", Permission.DEAFEN_MEMBERS, "channel"),
    EndpointPermissionContract("voice.move", Permission.MOVE_MEMBERS, "channel"),
    EndpointPermissionContract("voice.stream", Permission.STREAM, "channel"),
    EndpointPermissionContract("voice.occupancy", Permission.VIEW_CHANNEL, "channel"),
)

PERMISSION_CONTRACT = {contract.operation: contract for contract in _CONTRACTS}


def required_permissions(operation: str) -> Permission:
    try:
        return PERMISSION_CONTRACT[operation].permission
    except KeyError as exc:
        raise RuntimeError(f"unknown permission contract operation: {operation}") from exc
