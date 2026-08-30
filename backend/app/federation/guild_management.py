from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal, Never, get_args

from fastapi import HTTPException
from pydantic import ConfigDict, Field, StrictInt, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.installations import (
    bot_actor_active_installations_statement,
    installation_grants_permissions,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Guild, GuildMember, User
from app.federation.client import signed_request
from app.federation.management_rpc import (
    MANAGEMENT_RPC_DEADLINE_SECONDS,
    MANAGEMENT_RPC_MAX_RESPONSE_BYTES,
    ManagementRPCErrorContract,
    consume_management_request_once,
    request_management_rpc,
    validate_management_json,
    validate_management_request_shape,
)
from app.federation.schemas import FederationDomain, SnowflakeString
from app.federation.security import (
    FederationPrincipal,
    require_guild_federation_access,
)
from app.voice.permissions import (
    STAGE_INSTANCE_MODERATOR_PERMISSIONS,
    STAGE_INSTANCE_VIEW_PERMISSIONS,
    STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
    federated_stage_voice_state_permissions,
)

GUILD_MANAGEMENT_DEADLINE_SECONDS = MANAGEMENT_RPC_DEADLINE_SECONDS
GUILD_MANAGEMENT_MAX_RESPONSE_BYTES = MANAGEMENT_RPC_MAX_RESPONSE_BYTES

_GUILD_MANAGEMENT_ERRORS = ManagementRPCErrorContract(
    unavailable={
        "code": "FEDERATED_GUILD_MANAGEMENT_UNAVAILABLE",
        "message": "The guild home could not complete that request. Try again shortly.",
    },
    failed={
        "code": "FEDERATED_GUILD_MANAGEMENT_FAILED",
        "message": "The guild home rejected that request.",
    },
    invalid_response={
        "code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID",
        "message": "The guild home returned an invalid response.",
    },
    invalid_binding={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
)

GuildManagementOperation = Literal[
    "guild.update",
    "guild.owner.transfer",
    "guild.delete",
    "channel.create",
    "channel.update",
    "channel.reorder",
    "channel.delete",
    "channel.overwrite.list",
    "channel.overwrite.put",
    "channel.overwrite.delete",
    "channel.permissions.sync",
    "role.create",
    "role.update",
    "role.reorder",
    "role.delete",
    "member.update",
    "member.role.assign",
    "member.role.replace",
    "member.role.remove",
    "member.kick",
    "member.ban.list",
    "member.ban",
    "member.unban",
    "instance_ban.list",
    "instance_ban.put",
    "instance_ban.remove",
    "invite.list",
    "invite.list_channel",
    "invite.get",
    "invite.create",
    "invite.revoke",
    "invite.target_users.get",
    "invite.target_users.update",
    "invite.target_users.status",
    "automod.list",
    "automod.get",
    "automod.create",
    "automod.update",
    "automod.delete",
    "moderation.prune.estimate",
    "moderation.prune",
    "moderation.bulk_ban",
    "emoji.list",
    "emoji.get",
    "emoji.update",
    "emoji.ticket",
    "emoji.create",
    "emoji.delete",
    "guild_asset.ticket",
    "guild_asset.commit",
    "guild_asset.delete",
    "role_icon.ticket",
    "role_icon.commit",
    "role_icon.delete",
    "sticker.list",
    "sticker.get",
    "sticker.update",
    "sticker.ticket",
    "sticker.create",
    "sticker.delete",
    "webhook.create",
    "webhook.list",
    "webhook.list_channel",
    "webhook.get",
    "webhook.update",
    "webhook.rotate",
    "webhook.delete",
    "webhook.avatar.ticket",
    "webhook.avatar.commit",
    "webhook.avatar.delete",
    "webhook.e2ee.get",
    "webhook.e2ee.grant",
    "webhook.e2ee.revoke",
    "scheduled_event.list",
    "scheduled_event.create",
    "scheduled_event.get",
    "scheduled_event.update",
    "scheduled_event.delete",
    "scheduled_event.image.ticket",
    "scheduled_event.image.commit",
    "scheduled_event.image.delete",
    "scheduled_event.users",
    "scheduled_event.subscribe",
    "scheduled_event.unsubscribe",
    "stage_instance.create",
    "stage_instance.get",
    "stage_instance.update",
    "stage_instance.delete",
    "stage_voice_state.get",
    "stage_voice_state.self",
    "stage_voice_state.user",
    "soundboard.ticket",
    "soundboard.create",
    "soundboard.update",
    "soundboard.delete",
    "voice.regions",
    "voice_channel_info.get",
    "voice_status.get",
    "voice_status.update",
    "voice_member.update",
    "voice_member.disconnect",
    "voice_member.move",
    "voice_message.capability",
    "tracker.board.update",
    "tracker.lane.create",
    "tracker.lane.update",
    "tracker.lane.move",
    "tracker.lane.delete",
    "tracker.task.create",
    "tracker.task.update",
    "tracker.task.move",
    "tracker.task.delete",
    "bot_e2ee.list",
    "bot_e2ee.grant",
    "bot_e2ee.revoke",
]

# The federation transport is always POST, so admission must follow the public
# operation's semantics rather than the RPC method. Reads, actor-owned cleanup,
# and cryptographic access revocation remain available; shared-resource and
# moderator deletes still require admission. Every current or future operation
# not listed here therefore fails closed as content creation/change.
GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS: frozenset[GuildManagementOperation] = frozenset(
    {
        # Read-only operations.
        "channel.overwrite.list",
        "member.ban.list",
        "instance_ban.list",
        "invite.list",
        "invite.list_channel",
        "invite.get",
        "invite.target_users.get",
        "invite.target_users.status",
        "automod.list",
        "automod.get",
        "moderation.prune.estimate",
        "emoji.list",
        "emoji.get",
        "sticker.list",
        "sticker.get",
        "webhook.list",
        "webhook.list_channel",
        "webhook.get",
        "webhook.e2ee.get",
        "scheduled_event.list",
        "scheduled_event.get",
        "scheduled_event.users",
        "stage_instance.get",
        "stage_voice_state.get",
        "voice.regions",
        "voice_channel_info.get",
        "voice_status.get",
        "voice_message.capability",
        "bot_e2ee.list",
        # Removal-only access/consent cleanup.
        "webhook.e2ee.revoke",
        "scheduled_event.unsubscribe",
        "bot_e2ee.revoke",
    }
)


@dataclass(frozen=True, slots=True)
class BotGuildManagementContract:
    """Installation grants required before dispatching a bot actor operation.

    Each scope tuple is one complete alternative.  Each permission mask is one
    complete alternative; the authoritative dispatcher still applies the
    operation's exact live guild/channel permission and hierarchy checks.
    """

    scope_options: tuple[tuple[str, ...], ...]
    permission_options: tuple[Permission, ...]


def _bot_contract(
    scopes: str | tuple[str, ...],
    permission: Permission,
    *,
    scope_alternatives: tuple[str | tuple[str, ...], ...] = (),
    permission_alternatives: tuple[Permission, ...] = (),
) -> BotGuildManagementContract:
    def normalize(option: str | tuple[str, ...]) -> tuple[str, ...]:
        return (option,) if isinstance(option, str) else option

    return BotGuildManagementContract(
        scope_options=(normalize(scopes), *(normalize(item) for item in scope_alternatives)),
        permission_options=(permission, *permission_alternatives),
    )


_BOT_GUILD_MANAGEMENT_GROUPS: tuple[
    tuple[tuple[GuildManagementOperation, ...], BotGuildManagementContract], ...
] = (
    (("guild.update",), _bot_contract("guilds.manage", Permission.MANAGE_GUILD)),
    (
        ("channel.create", "channel.update", "channel.reorder", "channel.delete"),
        _bot_contract("channels.manage", Permission.MANAGE_CHANNELS),
    ),
    (
        ("channel.overwrite.list",),
        _bot_contract("channels.overwrites.read", Permission.MANAGE_ROLES),
    ),
    (
        (
            "channel.overwrite.put",
            "channel.overwrite.delete",
            "channel.permissions.sync",
        ),
        _bot_contract("channels.overwrites.manage", Permission.MANAGE_ROLES),
    ),
    (
        ("role.create", "role.update", "role.reorder", "role.delete"),
        _bot_contract("roles.manage", Permission.MANAGE_ROLES),
    ),
    (
        ("member.update",),
        _bot_contract(
            "moderation.members",
            Permission.CHANGE_NICKNAME,
            permission_alternatives=(
                Permission.MANAGE_NICKNAMES,
                Permission.MODERATE_MEMBERS,
            ),
        ),
    ),
    (
        ("member.role.assign", "member.role.replace", "member.role.remove"),
        _bot_contract("roles.manage", Permission.MANAGE_ROLES),
    ),
    (("member.kick",), _bot_contract("moderation.members", Permission.KICK_MEMBERS)),
    (
        ("member.ban.list", "member.ban", "member.unban"),
        _bot_contract(
            "moderation.bans",
            Permission.BAN_MEMBERS,
            scope_alternatives=("moderation.members",),
        ),
    ),
    (
        ("instance_ban.list", "instance_ban.put", "instance_ban.remove"),
        _bot_contract("moderation.bans", Permission.BAN_INSTANCES),
    ),
    (
        ("invite.list",),
        _bot_contract(
            "invites.read",
            Permission.MANAGE_GUILD,
            scope_alternatives=("invites.manage",),
        ),
    ),
    (
        ("invite.list_channel",),
        _bot_contract(
            "invites.read",
            Permission.MANAGE_CHANNELS,
            scope_alternatives=("invites.manage",),
        ),
    ),
    (
        ("invite.get",),
        _bot_contract(
            "invites.read",
            Permission(0),
            scope_alternatives=("invites.manage",),
        ),
    ),
    (("invite.create",), _bot_contract("invites.manage", Permission.CREATE_INVITE)),
    (
        (
            "invite.revoke",
            "invite.target_users.get",
            "invite.target_users.update",
            "invite.target_users.status",
        ),
        _bot_contract(
            "invites.manage",
            Permission.MANAGE_GUILD,
            permission_alternatives=(Permission.MANAGE_CHANNELS,),
        ),
    ),
    (
        ("automod.list", "automod.get"),
        _bot_contract("automod.rules.read", Permission.MANAGE_GUILD),
    ),
    (
        ("automod.create", "automod.update", "automod.delete"),
        _bot_contract("automod.rules.manage", Permission.MANAGE_GUILD),
    ),
    (
        ("moderation.prune.estimate", "moderation.prune"),
        _bot_contract(
            "moderation.prune",
            Permission.MANAGE_GUILD | Permission.KICK_MEMBERS,
        ),
    ),
    (
        ("moderation.bulk_ban",),
        _bot_contract(
            "moderation.bans",
            Permission.MANAGE_GUILD | Permission.BAN_MEMBERS,
        ),
    ),
    (
        ("emoji.list", "emoji.get", "sticker.list", "sticker.get"),
        _bot_contract(
            "expressions.read",
            Permission(0),
            scope_alternatives=("guilds.read",),
        ),
    ),
    (
        ("emoji.update", "emoji.delete", "sticker.update", "sticker.delete"),
        _bot_contract(
            "expressions.manage",
            Permission.CREATE_GUILD_EXPRESSIONS,
            scope_alternatives=("emojis.manage",),
            permission_alternatives=(Permission.MANAGE_GUILD_EXPRESSIONS,),
        ),
    ),
    (
        ("emoji.ticket", "sticker.ticket"),
        _bot_contract(
            ("expressions.manage", "attachments.write"),
            Permission.CREATE_GUILD_EXPRESSIONS,
            scope_alternatives=(("emojis.manage", "attachments.write"),),
        ),
    ),
    (
        ("emoji.create", "sticker.create"),
        _bot_contract(
            "expressions.manage",
            Permission.CREATE_GUILD_EXPRESSIONS,
            scope_alternatives=("emojis.manage",),
        ),
    ),
    (
        ("guild_asset.ticket",),
        _bot_contract(
            ("guilds.assets.manage", "attachments.write"),
            Permission.MANAGE_GUILD,
        ),
    ),
    (
        ("guild_asset.commit", "guild_asset.delete"),
        _bot_contract("guilds.assets.manage", Permission.MANAGE_GUILD),
    ),
    (
        ("role_icon.ticket",),
        _bot_contract(("roles.manage", "attachments.write"), Permission.MANAGE_ROLES),
    ),
    (
        ("role_icon.commit", "role_icon.delete"),
        _bot_contract("roles.manage", Permission.MANAGE_ROLES),
    ),
    (
        ("webhook.list", "webhook.list_channel", "webhook.get"),
        _bot_contract(
            "webhooks.read",
            Permission.MANAGE_WEBHOOKS,
            scope_alternatives=("webhooks.manage",),
        ),
    ),
    (
        (
            "webhook.create",
            "webhook.update",
            "webhook.rotate",
            "webhook.delete",
            "webhook.avatar.commit",
            "webhook.avatar.delete",
            "webhook.e2ee.get",
            "webhook.e2ee.grant",
            "webhook.e2ee.revoke",
        ),
        _bot_contract("webhooks.manage", Permission.MANAGE_WEBHOOKS),
    ),
    (
        ("webhook.avatar.ticket",),
        _bot_contract(
            ("webhooks.manage", "attachments.write"),
            Permission.MANAGE_WEBHOOKS,
        ),
    ),
    (
        (
            "scheduled_event.list",
            "scheduled_event.get",
            "scheduled_event.users",
            "scheduled_event.subscribe",
            "scheduled_event.unsubscribe",
        ),
        _bot_contract("events.read", Permission(0)),
    ),
    (
        ("scheduled_event.create",),
        _bot_contract("events.manage", Permission.CREATE_EVENTS),
    ),
    (
        (
            "scheduled_event.update",
            "scheduled_event.delete",
            "scheduled_event.image.commit",
            "scheduled_event.image.delete",
        ),
        _bot_contract(
            "events.manage",
            Permission.CREATE_EVENTS,
            permission_alternatives=(Permission.MANAGE_EVENTS,),
        ),
    ),
    (
        ("scheduled_event.image.ticket",),
        _bot_contract(
            ("events.manage", "attachments.write"),
            Permission.CREATE_EVENTS,
            permission_alternatives=(Permission.MANAGE_EVENTS,),
        ),
    ),
    (
        ("stage_instance.create", "stage_instance.update", "stage_instance.delete"),
        _bot_contract("channels.manage", STAGE_INSTANCE_MODERATOR_PERMISSIONS),
    ),
    (
        ("stage_instance.get",),
        _bot_contract("channels.read", STAGE_INSTANCE_VIEW_PERMISSIONS),
    ),
    (
        ("stage_voice_state.get",),
        # Self reads require no guild permission while other-user reads are
        # checked against CONNECT by the authority after resolving the target.
        _bot_contract("voice.states.read", Permission(0)),
    ),
    (
        ("stage_voice_state.self",),
        _bot_contract(
            "voice.connect",
            Permission(0),
        ),
    ),
    (
        ("stage_voice_state.user",),
        _bot_contract("voice.moderate", STAGE_VOICE_STATE_MODERATOR_PERMISSIONS),
    ),
    (
        ("soundboard.ticket",),
        _bot_contract(
            ("soundboard.manage", "attachments.write"),
            Permission.CREATE_GUILD_EXPRESSIONS,
        ),
    ),
    (
        ("soundboard.create",),
        _bot_contract("soundboard.manage", Permission.CREATE_GUILD_EXPRESSIONS),
    ),
    (
        ("soundboard.update", "soundboard.delete"),
        _bot_contract(
            "soundboard.manage",
            Permission.CREATE_GUILD_EXPRESSIONS,
            permission_alternatives=(Permission.MANAGE_GUILD_EXPRESSIONS,),
        ),
    ),
    (("voice.regions",), _bot_contract("voice.connect", Permission(0))),
    (
        ("voice_channel_info.get", "voice_status.get"),
        _bot_contract("channels.read", Permission.VIEW_CHANNEL),
    ),
    (
        ("voice_status.update",),
        _bot_contract("channels.manage", Permission.SET_VOICE_CHANNEL_STATUS),
    ),
    (
        ("voice_member.update",),
        _bot_contract(
            "voice.moderate",
            Permission.MUTE_MEMBERS,
            permission_alternatives=(Permission.DEAFEN_MEMBERS,),
        ),
    ),
    (
        ("voice_member.disconnect", "voice_member.move"),
        _bot_contract("voice.moderate", Permission.MOVE_MEMBERS),
    ),
    (
        ("voice_message.capability",),
        _bot_contract("guilds.read", Permission(0)),
    ),
    (
        (
            "tracker.board.update",
            "tracker.lane.create",
            "tracker.lane.update",
            "tracker.lane.move",
            "tracker.lane.delete",
        ),
        _bot_contract(
            "tasks.manage",
            Permission.VIEW_CHANNEL | Permission.MANAGE_TRACKER,
        ),
    ),
    (
        ("tracker.task.create",),
        _bot_contract(
            "tasks.write",
            Permission.VIEW_CHANNEL | Permission.CREATE_TRACKER_TASKS,
        ),
    ),
    (
        ("tracker.task.update", "tracker.task.move", "tracker.task.delete"),
        _bot_contract(
            "tasks.write",
            Permission.VIEW_CHANNEL | Permission.EDIT_OWN_TRACKER_TASKS,
            permission_alternatives=(Permission.VIEW_CHANNEL | Permission.MANAGE_TRACKER_TASKS,),
        ),
    ),
)


def _build_bot_guild_management_contracts() -> dict[
    GuildManagementOperation, BotGuildManagementContract
]:
    contracts: dict[GuildManagementOperation, BotGuildManagementContract] = {}
    for operations, contract in _BOT_GUILD_MANAGEMENT_GROUPS:
        for operation in operations:
            if operation in contracts:
                raise RuntimeError(f"duplicate bot guild-management contract: {operation}")
            contracts[operation] = contract
    return contracts


BOT_GUILD_MANAGEMENT_CONTRACTS = _build_bot_guild_management_contracts()
BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS: frozenset[GuildManagementOperation] = frozenset(
    {
        "guild.owner.transfer",
        "guild.delete",
        "bot_e2ee.list",
        "bot_e2ee.grant",
        "bot_e2ee.revoke",
    }
)

_all_guild_management_operations = set(get_args(GuildManagementOperation))
if (
    set(BOT_GUILD_MANAGEMENT_CONTRACTS) & BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS
    or set(BOT_GUILD_MANAGEMENT_CONTRACTS) | BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS
    != _all_guild_management_operations
):
    raise RuntimeError("bot guild-management operation contracts are incomplete")


def qualified_management_ref(ref: EntityRef, default_domain: str) -> str:
    """Serialize an RPC resource identity without authority-dependent defaults."""

    resource_id, resource_domain = ref.resolve(default_domain)
    return f"{resource_id}@{resource_domain}"


def _invalid_guild_management_response() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"},
    )


def require_guild_management_status(
    result: GuildManagementResult,
    *allowed: int,
) -> None:
    if result.status_code not in allowed:
        raise _invalid_guild_management_response()


def guild_management_dict_body(
    result: GuildManagementResult,
    *allowed: int,
) -> dict[str, Any]:
    require_guild_management_status(result, *allowed)
    if not isinstance(result.body, dict):
        raise _invalid_guild_management_response()
    return result.body


def guild_management_list_body(
    result: GuildManagementResult,
    *allowed: int,
) -> list[Any]:
    require_guild_management_status(result, *allowed)
    if not isinstance(result.body, list):
        raise _invalid_guild_management_response()
    return result.body


class GuildManagementRef(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    domain: FederationDomain


class GuildManagementRequest(UnambiguousInputModel):
    """A replay-bounded signed actor call from one instance to a guild home.

    The actor may be a human or an installed bot service principal; public bot
    clients never call this internal federation transport directly.

    ``operation`` is deliberately a closed protocol allowlist. Each authority
    dispatcher must additionally validate ``payload`` with that operation's
    public request model before touching state.
    """

    model_config = ConfigDict(extra="forbid")

    guild: GuildManagementRef
    actor: GuildManagementRef
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kagm_[A-Za-z0-9_-]{32}$")
    issued_at: StrictInt = Field(ge=0)
    deadline: StrictInt = Field(ge=1)
    operation: GuildManagementOperation
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_request(self) -> GuildManagementRequest:
        validate_management_request_shape(
            self.issued_at,
            self.deadline,
            label="guild-management",
        )
        validate_management_json(
            self.payload,
            label="guild-management payload",
        )
        return self


class GuildManagementResult(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^kagm_[A-Za-z0-9_-]{32}$")
    operation: GuildManagementOperation
    guild: GuildManagementRef
    status_code: StrictInt = Field(ge=200, le=299)
    body: Any = None

    @model_validator(mode="after")
    def bounded_body(self) -> GuildManagementResult:
        validate_management_json(
            self.body,
            label="guild-management response",
        )
        if self.status_code == 204 and self.body is not None:
            raise ValueError("empty guild-management responses cannot include a body")
        return self


_GUILD_LIST_200 = frozenset(
    {
        "role.reorder",
        "member.ban.list",
        "instance_ban.list",
        "invite.list",
        "invite.list_channel",
        "automod.list",
        "emoji.list",
        "sticker.list",
        "webhook.list",
        "webhook.list_channel",
        "scheduled_event.list",
        "scheduled_event.users",
        "voice.regions",
    }
)
_GUILD_NONE_204 = frozenset(
    {
        "guild.delete",
        "channel.reorder",
        "channel.overwrite.delete",
        "role.delete",
        "member.role.assign",
        "member.role.remove",
        "member.kick",
        "member.ban",
        "member.unban",
        "instance_ban.put",
        "instance_ban.remove",
        "automod.delete",
        "emoji.delete",
        "sticker.delete",
        "webhook.delete",
        "scheduled_event.delete",
        "scheduled_event.subscribe",
        "scheduled_event.unsubscribe",
        "stage_instance.delete",
        "soundboard.delete",
        "voice_member.update",
        "voice_member.disconnect",
        "voice_member.move",
        "tracker.lane.delete",
        "tracker.task.delete",
    }
)
_GUILD_DICT_201 = frozenset(
    {
        "channel.create",
        "role.create",
        "invite.create",
        "automod.create",
        "emoji.ticket",
        "guild_asset.ticket",
        "role_icon.ticket",
        "sticker.ticket",
        "webhook.create",
        "webhook.avatar.ticket",
        "scheduled_event.create",
        "scheduled_event.image.ticket",
        "stage_instance.create",
        "soundboard.ticket",
        "tracker.lane.create",
        "tracker.task.create",
    }
)
_GUILD_DICT_201_OR_202 = frozenset({"emoji.create", "sticker.create", "soundboard.create"})
_GUILD_DICT_200_OR_202 = frozenset(
    {
        "guild_asset.commit",
        "role_icon.commit",
        "webhook.avatar.commit",
        "scheduled_event.image.commit",
    }
)
_ALL_GUILD_MANAGEMENT_OPERATIONS = frozenset(get_args(GuildManagementOperation))
_EXPLICIT_GUILD_MANAGEMENT_OPERATIONS = (
    _GUILD_LIST_200
    | _GUILD_NONE_204
    | _GUILD_DICT_201
    | _GUILD_DICT_201_OR_202
    | _GUILD_DICT_200_OR_202
)
if sum(
    len(operations)
    for operations in (
        _GUILD_LIST_200,
        _GUILD_NONE_204,
        _GUILD_DICT_201,
        _GUILD_DICT_201_OR_202,
        _GUILD_DICT_200_OR_202,
    )
) != len(_EXPLICIT_GUILD_MANAGEMENT_OPERATIONS):
    raise RuntimeError("guild management result contract categories overlap")
_GUILD_DICT_200 = _ALL_GUILD_MANAGEMENT_OPERATIONS - _EXPLICIT_GUILD_MANAGEMENT_OPERATIONS

GuildManagementBodyKind = Literal["dict", "list", "none"]
_GUILD_MANAGEMENT_RESULT_CONTRACT: dict[
    str,
    tuple[frozenset[int], GuildManagementBodyKind],
] = {
    **{operation: (frozenset({200}), "dict") for operation in _GUILD_DICT_200},
    **{operation: (frozenset({200}), "list") for operation in _GUILD_LIST_200},
    **{operation: (frozenset({204}), "none") for operation in _GUILD_NONE_204},
    **{operation: (frozenset({201}), "dict") for operation in _GUILD_DICT_201},
    **{operation: (frozenset({201, 202}), "dict") for operation in _GUILD_DICT_201_OR_202},
    **{operation: (frozenset({200, 202}), "dict") for operation in _GUILD_DICT_200_OR_202},
}
if set(_GUILD_MANAGEMENT_RESULT_CONTRACT) != set(_ALL_GUILD_MANAGEMENT_OPERATIONS):
    raise RuntimeError("guild management result contract is incomplete")

GuildManagementIdentityKind = Literal[
    "envelope",
    "guild",
    "guild_dict",
    "guild_list",
    "invite",
    "invite_list",
    "member",
    "scheduled_users",
    "stage_voice",
    "tracker",
    "voice_channel_info",
    "webhook_e2ee",
    "bot_e2ee",
    "bulk_ban",
    "channel_overwrites",
]

_IDENTITY_GUILD = frozenset(
    {
        "guild.update",
        "guild.owner.transfer",
        "guild_asset.delete",
    }
)
_IDENTITY_GUILD_DICT = frozenset(
    {
        "channel.create",
        "channel.update",
        "channel.delete",
        "channel.permissions.sync",
        "role.create",
        "role.update",
        "automod.get",
        "automod.create",
        "automod.update",
        "emoji.get",
        "emoji.update",
        "emoji.create",
        "role_icon.commit",
        "role_icon.delete",
        "sticker.get",
        "sticker.update",
        "sticker.create",
        "webhook.create",
        "webhook.get",
        "webhook.update",
        "webhook.rotate",
        "webhook.avatar.commit",
        "webhook.avatar.delete",
        "scheduled_event.create",
        "scheduled_event.get",
        "scheduled_event.update",
        "scheduled_event.image.commit",
        "scheduled_event.image.delete",
        "stage_instance.create",
        "stage_instance.get",
        "stage_instance.update",
        "soundboard.create",
        "soundboard.update",
        "voice_status.get",
        "voice_status.update",
    }
)
_IDENTITY_GUILD_LIST = frozenset(
    {
        "role.reorder",
        "member.ban.list",
        "instance_ban.list",
        "automod.list",
        "emoji.list",
        "sticker.list",
        "webhook.list",
        "webhook.list_channel",
        "scheduled_event.list",
    }
)
_IDENTITY_INVITE = frozenset({"invite.create", "invite.get", "invite.revoke"})
_IDENTITY_INVITE_LIST = frozenset({"invite.list", "invite.list_channel"})
_IDENTITY_MEMBER = frozenset({"member.update", "member.role.replace"})
_IDENTITY_SCHEDULED_USERS = frozenset({"scheduled_event.users"})
_IDENTITY_STAGE_VOICE = frozenset(
    {
        "stage_voice_state.get",
        "stage_voice_state.self",
        "stage_voice_state.user",
    }
)
_IDENTITY_TRACKER = frozenset(
    {
        "tracker.board.update",
        "tracker.lane.create",
        "tracker.lane.update",
        "tracker.lane.move",
        "tracker.task.create",
        "tracker.task.update",
        "tracker.task.move",
    }
)
_IDENTITY_VOICE_CHANNEL_INFO = frozenset({"voice_channel_info.get"})
_IDENTITY_WEBHOOK_E2EE = frozenset(
    {"webhook.e2ee.get", "webhook.e2ee.grant", "webhook.e2ee.revoke"}
)
_IDENTITY_BOT_E2EE = frozenset({"bot_e2ee.list", "bot_e2ee.grant", "bot_e2ee.revoke"})
_IDENTITY_BULK_BAN = frozenset({"moderation.bulk_ban"})
_IDENTITY_CHANNEL_OVERWRITES = frozenset({"channel.overwrite.list"})
# These operations either return no body or use a Discord-shaped aggregate,
# status, capability, or upload-ticket body with no top-level guild resource
# identity. Their exact signed request/operation/guild envelope remains the
# applicable lineage contract; identities nested inside user collections are
# deliberately not treated as guild-owned resources.
_IDENTITY_ENVELOPE = frozenset(
    {
        "guild.delete",
        "channel.reorder",
        "channel.overwrite.put",
        "channel.overwrite.delete",
        "role.delete",
        "member.role.assign",
        "member.role.remove",
        "member.kick",
        "member.ban",
        "member.unban",
        "instance_ban.put",
        "instance_ban.remove",
        "invite.target_users.get",
        "invite.target_users.update",
        "invite.target_users.status",
        "automod.delete",
        "moderation.prune.estimate",
        "moderation.prune",
        "emoji.ticket",
        "emoji.delete",
        "guild_asset.ticket",
        "guild_asset.commit",
        "role_icon.ticket",
        "sticker.ticket",
        "sticker.delete",
        "webhook.delete",
        "webhook.avatar.ticket",
        "scheduled_event.delete",
        "scheduled_event.image.ticket",
        "scheduled_event.subscribe",
        "scheduled_event.unsubscribe",
        "stage_instance.delete",
        "soundboard.ticket",
        "soundboard.delete",
        "voice.regions",
        "voice_member.update",
        "voice_member.disconnect",
        "voice_member.move",
        "voice_message.capability",
        "tracker.lane.delete",
        "tracker.task.delete",
    }
)

_IDENTITY_OPERATION_GROUPS: tuple[tuple[frozenset[str], GuildManagementIdentityKind], ...] = (
    (_IDENTITY_GUILD, "guild"),
    (_IDENTITY_GUILD_DICT, "guild_dict"),
    (_IDENTITY_GUILD_LIST, "guild_list"),
    (_IDENTITY_INVITE, "invite"),
    (_IDENTITY_INVITE_LIST, "invite_list"),
    (_IDENTITY_MEMBER, "member"),
    (_IDENTITY_SCHEDULED_USERS, "scheduled_users"),
    (_IDENTITY_STAGE_VOICE, "stage_voice"),
    (_IDENTITY_TRACKER, "tracker"),
    (_IDENTITY_VOICE_CHANNEL_INFO, "voice_channel_info"),
    (_IDENTITY_WEBHOOK_E2EE, "webhook_e2ee"),
    (_IDENTITY_BOT_E2EE, "bot_e2ee"),
    (_IDENTITY_BULK_BAN, "bulk_ban"),
    (_IDENTITY_CHANNEL_OVERWRITES, "channel_overwrites"),
    (_IDENTITY_ENVELOPE, "envelope"),
)
_GUILD_MANAGEMENT_IDENTITY_CONTRACT = {
    operation: kind for operations, kind in _IDENTITY_OPERATION_GROUPS for operation in operations
}
if sum(len(operations) for operations, _ in _IDENTITY_OPERATION_GROUPS) != len(
    _GUILD_MANAGEMENT_IDENTITY_CONTRACT
):
    raise RuntimeError("guild management identity contract categories overlap")
if set(_GUILD_MANAGEMENT_IDENTITY_CONTRACT) != set(_ALL_GUILD_MANAGEMENT_OPERATIONS):
    raise RuntimeError("guild management identity contract is incomplete")

_PROCESSING_RESPONSE_OPERATIONS = frozenset(
    {
        "emoji.create",
        "role_icon.commit",
        "sticker.create",
        "webhook.avatar.commit",
        "scheduled_event.image.commit",
        "soundboard.create",
    }
)

_DIRECT_RESOURCE_BINDINGS: dict[
    str,
    tuple[tuple[str, ...], tuple[str, str]],
] = {
    "channel.update": (("channel_ref",), ("id", "origin_domain")),
    "channel.delete": (("channel_ref",), ("id", "origin_domain")),
    "channel.permissions.sync": (("channel_ref",), ("id", "origin_domain")),
    "role.update": (("resource_ref",), ("id", "origin_domain")),
    "automod.get": (("resource_id",), ("id", "origin_domain")),
    "automod.update": (("resource_id",), ("id", "origin_domain")),
    "emoji.get": (("resource_id",), ("id", "origin_domain")),
    "emoji.update": (("resource_id",), ("id", "origin_domain")),
    "role_icon.commit": (("resource_ref",), ("id", "origin_domain")),
    "role_icon.delete": (("resource_ref",), ("id", "origin_domain")),
    "sticker.get": (("resource_id",), ("id", "origin_domain")),
    "sticker.update": (("resource_id",), ("id", "origin_domain")),
    "webhook.get": (("resource_id",), ("id", "origin_domain")),
    "webhook.update": (("resource_id",), ("id", "origin_domain")),
    "webhook.rotate": (("resource_id",), ("id", "origin_domain")),
    "webhook.avatar.commit": (("resource_id",), ("id", "origin_domain")),
    "webhook.avatar.delete": (("resource_id",), ("id", "origin_domain")),
    "scheduled_event.get": (("resource_ref",), ("id", "origin_domain")),
    "scheduled_event.update": (("resource_ref",), ("id", "origin_domain")),
    "scheduled_event.image.commit": (("resource_ref",), ("id", "origin_domain")),
    "scheduled_event.image.delete": (("resource_ref",), ("id", "origin_domain")),
    "stage_instance.create": (("data", "channel_id"), ("channel_id", "channel_domain")),
    "stage_instance.get": (("channel_id",), ("channel_id", "channel_domain")),
    "stage_instance.update": (("channel_id",), ("channel_id", "channel_domain")),
    "soundboard.update": (("resource_ref",), ("id", "origin_domain")),
    "voice_status.get": (("channel_ref",), ("id", "origin_domain")),
    "voice_status.update": (("channel_ref",), ("id", "origin_domain")),
}


def _invalid_identity() -> Never:
    raise _invalid_guild_management_response()


def _body_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid_identity()
    return value


def _body_list(value: object) -> list[Any]:
    if not isinstance(value, list):
        _invalid_identity()
    return value


def _body_items(value: object) -> list[dict[str, Any]]:
    return [_body_dict(item) for item in _body_list(value)]


def _require_pair(
    body: dict[str, Any],
    id_field: str,
    domain_field: str,
    expected: tuple[int, str],
) -> None:
    if body.get(id_field) != str(expected[0]) or body.get(domain_field) != expected[1]:
        _invalid_identity()


def _require_optional_authority_ref(
    body: dict[str, Any],
    id_field: str,
    domain_field: str,
    authority: str,
) -> None:
    raw_id = body.get(id_field)
    raw_domain = body.get(domain_field)
    if raw_id is None and raw_domain is None:
        return
    if not isinstance(raw_id, str) or raw_domain != authority:
        _invalid_identity()
    try:
        resource_id, resource_domain = EntityRef(raw_id).resolve(authority)
    except (TypeError, ValueError):
        _invalid_identity()
    if raw_id != str(resource_id) or resource_domain != authority:
        _invalid_identity()


def _request_payload_value(request: GuildManagementRequest, path: tuple[str, ...]) -> object:
    value: object = request.payload
    for field in path:
        if not isinstance(value, dict) or field not in value:
            _invalid_identity()
        value = value[field]
    return value


def _request_ref(
    request: GuildManagementRequest,
    path: tuple[str, ...],
    *,
    default_domain: str | None = None,
) -> tuple[int, str]:
    raw = _request_payload_value(request, path)
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        _invalid_identity()
    try:
        return EntityRef(str(raw)).resolve(default_domain or request.guild.domain)
    except (TypeError, ValueError):
        _invalid_identity()


def _require_guild_fields(body: dict[str, Any], request: GuildManagementRequest) -> None:
    _require_pair(
        body,
        "guild_id",
        "guild_domain",
        (int(request.guild.id), request.guild.domain),
    )


def _validate_scheduled_event_body(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    """Keep channel/entity refs in a signed event under its guild authority."""

    _require_guild_fields(body, request)
    _require_optional_authority_ref(
        body,
        "channel_id",
        "channel_domain",
        request.guild.domain,
    )
    _require_optional_authority_ref(
        body,
        "entity_id",
        "entity_domain",
        request.guild.domain,
    )


def _require_direct_ref(body: dict[str, Any], expected: tuple[int, str]) -> None:
    _require_pair(body, "id", "origin_domain", expected)


def _validate_direct_resource(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    binding = _DIRECT_RESOURCE_BINDINGS.get(request.operation)
    if binding is None:
        return
    payload_path, body_fields = binding
    expected = _request_ref(request, payload_path)
    _require_pair(body, body_fields[0], body_fields[1], expected)


def _validate_invite_body(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    guild = _body_dict(body.get("guild"))
    _require_direct_ref(guild, (int(request.guild.id), request.guild.domain))
    if request.operation in {"invite.get", "invite.revoke"} and body.get(
        "code"
    ) != request.payload.get("code"):
        _invalid_identity()
    channel_path: tuple[str, ...] | None = None
    if request.operation == "invite.list_channel":
        channel_path = ("channel_ref",)
    elif (
        request.operation == "invite.create"
        and isinstance(request.payload.get("data"), dict)
        and "channel_id" in request.payload["data"]
    ):
        channel_path = ("data", "channel_id")
    if channel_path is not None:
        expected_channel = _request_ref(request, channel_path)
        if body.get("channel_id") != str(expected_channel[0]):
            _invalid_identity()

    event_ref: tuple[int, str] | None = None
    raw_event_ref = body.get("scheduled_event_id")
    if raw_event_ref is not None:
        if not isinstance(raw_event_ref, str):
            _invalid_identity()
        try:
            event_ref = EntityRef(raw_event_ref).resolve(request.guild.domain)
        except (TypeError, ValueError):
            _invalid_identity()
        if (
            raw_event_ref != f"{event_ref[0]}@{event_ref[1]}"
            or event_ref[1] != request.guild.domain
        ):
            _invalid_identity()

    nested_event = body.get("guild_scheduled_event")
    if nested_event is not None:
        if event_ref is None:
            _invalid_identity()
        event_body = _body_dict(nested_event)
        _validate_scheduled_event_body(request, event_body)
        _require_direct_ref(event_body, event_ref)


def _validate_member_body(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    _require_guild_fields(body, request)
    expected_user = _request_ref(
        request,
        ("user_ref",),
        default_domain=request.requesting_instance,
    )
    user = _body_dict(body.get("user"))
    _require_direct_ref(user, expected_user)


def _validate_scheduled_users(
    request: GuildManagementRequest,
    items: list[dict[str, Any]],
) -> None:
    expected_event = _request_ref(request, ("resource_ref",))
    for item in items:
        _require_pair(
            item,
            "guild_scheduled_event_id",
            "guild_scheduled_event_domain",
            expected_event,
        )
        member = item.get("member")
        if member is not None:
            _require_guild_fields(_body_dict(member), request)


def _validate_stage_voice(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    _require_guild_fields(body, request)
    _require_optional_authority_ref(
        body,
        "channel_id",
        "channel_domain",
        request.guild.domain,
    )
    raw_channel_id = body.get("channel_id")
    if raw_channel_id is None:
        _invalid_identity()
    if request.operation == "stage_voice_state.self":
        expected_user = (int(request.actor.id), request.actor.domain)
    else:
        expected_user = _request_ref(
            request,
            ("user_ref",),
            default_domain=request.requesting_instance,
        )
    _require_pair(body, "user_id", "user_domain", expected_user)
    if body.get("identity") != f"{expected_user[0]}@{expected_user[1]}":
        _invalid_identity()
    if body.get("room") != f"g.{request.guild.id}.{raw_channel_id}":
        _invalid_identity()
    raw_data = request.payload.get("data")
    if isinstance(raw_data, dict) and raw_data.get("channel_id") is not None:
        _require_pair(
            body,
            "channel_id",
            "channel_domain",
            _request_ref(request, ("data", "channel_id")),
        )


def _validate_role_reorder(
    request: GuildManagementRequest,
    items: list[dict[str, Any]],
) -> None:
    raw_data = request.payload.get("data")
    if not isinstance(raw_data, dict):
        _invalid_identity()
    raw_roles = raw_data.get("roles")
    if not isinstance(raw_roles, list) or not 1 <= len(raw_roles) <= 250:
        _invalid_identity()
    expected: list[tuple[int, str]] = []
    for raw_role in raw_roles:
        if not isinstance(raw_role, dict):
            _invalid_identity()
        raw_id = raw_role.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            _invalid_identity()
        try:
            role_ref = EntityRef(str(raw_id)).resolve(request.guild.domain)
        except (TypeError, ValueError):
            _invalid_identity()
        if role_ref[1] != request.guild.domain:
            _invalid_identity()
        expected.append(role_ref)
    if len(expected) != len(set(expected)) or len(items) != len(expected):
        _invalid_identity()
    for item, role_ref in zip(items, expected, strict=True):
        _require_guild_fields(item, request)
        _require_direct_ref(item, role_ref)


def _validate_channel_overwrites(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    if set(body) != {
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
        "overwrites",
    }:
        _invalid_identity()
    _require_guild_fields(body, request)
    expected_channel = _request_ref(request, ("channel_ref",))
    _require_pair(body, "channel_id", "channel_domain", expected_channel)
    overwrites = _body_items(body.get("overwrites"))
    if len(overwrites) > 1_000:
        _invalid_identity()
    seen: set[tuple[str, int, str]] = set()
    for overwrite in overwrites:
        if set(overwrite) != {
            "target_id",
            "target_domain",
            "target_type",
            "allow",
            "deny",
        }:
            _invalid_identity()
        target_type = overwrite.get("target_type")
        raw_target_id = overwrite.get("target_id")
        target_domain = overwrite.get("target_domain")
        if (
            target_type not in {"role", "member"}
            or not isinstance(raw_target_id, str)
            or not isinstance(target_domain, str)
        ):
            _invalid_identity()
        try:
            target_id, normalized_domain = EntityRef(f"{raw_target_id}@{target_domain}").resolve(
                request.guild.domain
            )
        except (TypeError, ValueError):
            _invalid_identity()
        if (
            raw_target_id != str(target_id)
            or target_domain != normalized_domain
            or (target_type == "role" and target_domain != request.guild.domain)
        ):
            _invalid_identity()
        target = (target_type, target_id, target_domain)
        if target in seen:
            _invalid_identity()
        seen.add(target)
        for field in ("allow", "deny"):
            raw_permissions = overwrite.get(field)
            if not isinstance(raw_permissions, str):
                _invalid_identity()
            try:
                permissions = int(raw_permissions)
            except ValueError:
                _invalid_identity()
            if permissions < 0 or raw_permissions != str(permissions):
                _invalid_identity()


def _validate_tracker(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    expected_channel = _request_ref(request, ("channel_ref",))
    _require_pair(body, "channel_id", "channel_domain", expected_channel)
    if request.operation in {
        "tracker.lane.update",
        "tracker.lane.move",
        "tracker.task.update",
        "tracker.task.move",
    }:
        _require_direct_ref(body, _request_ref(request, ("resource_ref",)))
    if request.operation == "tracker.board.update":
        for item in _body_items(body.get("lanes")):
            _require_pair(item, "channel_id", "channel_domain", expected_channel)
        for item in _body_items(body.get("tasks")):
            _require_pair(item, "channel_id", "channel_domain", expected_channel)


def _validate_voice_channel_info(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    raw_fields = request.payload.get("fields")
    if (
        not isinstance(raw_fields, list)
        or not 1 <= len(raw_fields) <= 2
        or any(
            not isinstance(field, str) or field not in {"status", "voice_start_time"}
            for field in raw_fields
        )
        or len(raw_fields) != len(set(raw_fields))
    ):
        _invalid_identity()
    expected_fields = {
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        *raw_fields,
    }
    if set(body) != {"guild_id", "guild_domain", "channels"}:
        _invalid_identity()
    _require_guild_fields(body, request)
    channels = _body_items(body.get("channels"))
    if len(channels) > 500:
        _invalid_identity()
    seen: set[int] = set()
    for channel in channels:
        if set(channel) != expected_fields:
            _invalid_identity()
        _require_guild_fields(channel, request)
        _require_optional_authority_ref(
            channel,
            "id",
            "origin_domain",
            request.guild.domain,
        )
        raw_channel_id = channel.get("id")
        if not isinstance(raw_channel_id, str):
            _invalid_identity()
        channel_id = int(raw_channel_id)
        if channel_id in seen:
            _invalid_identity()
        seen.add(channel_id)
        if "status" in raw_fields:
            channel_status = channel.get("status")
            if channel_status is not None and (
                not isinstance(channel_status, str) or len(channel_status) > 500
            ):
                _invalid_identity()
        if "voice_start_time" in raw_fields:
            start_time = channel.get("voice_start_time")
            if start_time is not None and (
                isinstance(start_time, bool) or not isinstance(start_time, int) or start_time <= 0
            ):
                _invalid_identity()


def _validate_webhook_e2ee(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    expected_webhook = _request_ref(request, ("resource_id",))
    expected_channel = _request_ref(request, ("data", "channel_ref"))
    if body.get("webhook_ref") != f"{expected_webhook[0]}@{expected_webhook[1]}":
        _invalid_identity()
    if body.get("channel_ref") != f"{expected_channel[0]}@{expected_channel[1]}":
        _invalid_identity()


def _validate_bot_e2ee(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    expected_channel = _request_ref(request, ("channel_ref",))
    expected_application = _request_ref(
        request,
        ("application_ref",),
        default_domain=request.requesting_instance,
    )
    if body.get("channel_ref") != f"{expected_channel[0]}@{expected_channel[1]}":
        _invalid_identity()
    if body.get("application_ref") != (f"{expected_application[0]}@{expected_application[1]}"):
        _invalid_identity()


def _validate_bulk_ban(
    request: GuildManagementRequest,
    body: dict[str, Any],
) -> None:
    if set(body) != {"banned_users", "failed_users", "failed_user_details"}:
        _invalid_identity()
    raw_data = request.payload.get("data")
    if not isinstance(raw_data, dict):
        _invalid_identity()
    raw_requested = raw_data.get("user_ids")
    if not isinstance(raw_requested, list) or not 1 <= len(raw_requested) <= 200:
        _invalid_identity()

    def exact_refs(value: object) -> list[str]:
        if not isinstance(value, list) or len(value) > 200:
            _invalid_identity()
        refs: list[str] = []
        for raw_ref in value:
            if isinstance(raw_ref, bool) or not isinstance(raw_ref, (str, int)):
                _invalid_identity()
            try:
                resource_id, resource_domain = EntityRef(str(raw_ref)).resolve(
                    request.requesting_instance
                )
            except (TypeError, ValueError):
                _invalid_identity()
            qualified = f"{resource_id}@{resource_domain}"
            if str(raw_ref) != qualified:
                _invalid_identity()
            refs.append(qualified)
        if len(refs) != len(set(refs)):
            _invalid_identity()
        return refs

    requested = exact_refs(raw_requested)
    banned = exact_refs(body.get("banned_users"))
    failed = exact_refs(body.get("failed_users"))
    if set(banned).intersection(failed) or set(banned).union(failed) != set(requested):
        _invalid_identity()
    details = body.get("failed_user_details")
    if not isinstance(details, list) or len(details) != len(failed):
        _invalid_identity()
    detail_refs: list[str] = []
    for raw_detail in details:
        detail = _body_dict(raw_detail)
        if set(detail) != {"user_id", "code", "message"}:
            _invalid_identity()
        refs = exact_refs([detail.get("user_id")])
        code = detail.get("code")
        message = detail.get("message")
        if (
            not isinstance(code, str)
            or not 1 <= len(code) <= 128
            or not isinstance(message, str)
            or not 1 <= len(message) <= 1_000
        ):
            _invalid_identity()
        detail_refs.extend(refs)
    if detail_refs != failed:
        _invalid_identity()


def _validate_guild_management_body_identity(
    request: GuildManagementRequest,
    result: GuildManagementResult,
) -> None:
    kind = _GUILD_MANAGEMENT_IDENTITY_CONTRACT[request.operation]
    if kind == "envelope" or (
        result.status_code == 202 and request.operation in _PROCESSING_RESPONSE_OPERATIONS
    ):
        return
    if kind == "guild":
        _require_direct_ref(
            _body_dict(result.body),
            (int(request.guild.id), request.guild.domain),
        )
        return
    if kind == "guild_dict":
        body = _body_dict(result.body)
        if request.operation.startswith("scheduled_event."):
            _validate_scheduled_event_body(request, body)
        else:
            _require_guild_fields(body, request)
        _validate_direct_resource(request, body)
        if request.operation == "webhook.create":
            expected_channel = _request_ref(request, ("channel_ref",))
            _require_pair(body, "channel_id", "channel_domain", expected_channel)
        return
    if kind == "guild_list":
        items = _body_items(result.body)
        if request.operation == "role.reorder":
            _validate_role_reorder(request, items)
            return
        for item in items:
            if request.operation == "scheduled_event.list":
                _validate_scheduled_event_body(request, item)
            else:
                _require_guild_fields(item, request)
        if request.operation == "webhook.list_channel":
            expected_channel = _request_ref(request, ("channel_ref",))
            for item in items:
                _require_pair(item, "channel_id", "channel_domain", expected_channel)
        return
    if kind == "invite":
        _validate_invite_body(request, _body_dict(result.body))
        return
    if kind == "invite_list":
        for item in _body_items(result.body):
            _validate_invite_body(request, item)
        return
    if kind == "member":
        _validate_member_body(request, _body_dict(result.body))
        return
    if kind == "scheduled_users":
        _validate_scheduled_users(request, _body_items(result.body))
        return
    if kind == "stage_voice":
        _validate_stage_voice(request, _body_dict(result.body))
        return
    if kind == "tracker":
        _validate_tracker(request, _body_dict(result.body))
        return
    if kind == "voice_channel_info":
        _validate_voice_channel_info(request, _body_dict(result.body))
        return
    if kind == "webhook_e2ee":
        _validate_webhook_e2ee(request, _body_dict(result.body))
        return
    if kind == "bot_e2ee":
        _validate_bot_e2ee(request, _body_dict(result.body))
        return
    if kind == "bulk_ban":
        _validate_bulk_ban(request, _body_dict(result.body))
        return
    if kind == "channel_overwrites":
        _validate_channel_overwrites(request, _body_dict(result.body))
        return
    raise RuntimeError("unknown guild management identity contract")


def _validate_guild_management_result_shape(
    operation: GuildManagementOperation,
    result: GuildManagementResult,
) -> GuildManagementResult:
    allowed_status, body_kind = _GUILD_MANAGEMENT_RESULT_CONTRACT[operation]
    if result.status_code not in allowed_status:
        raise _invalid_guild_management_response()
    if body_kind == "dict" and not isinstance(result.body, dict):
        raise _invalid_guild_management_response()
    if body_kind == "list" and not isinstance(result.body, list):
        raise _invalid_guild_management_response()
    if body_kind == "none" and result.body is not None:
        raise _invalid_guild_management_response()
    return result


def validate_guild_management_result(
    request: GuildManagementRequest,
    result: GuildManagementResult,
) -> GuildManagementResult:
    if (
        result.request_id != request.request_id
        or result.operation != request.operation
        or result.guild != request.guild
    ):
        raise _invalid_guild_management_response()
    _validate_guild_management_result_shape(request.operation, result)
    _validate_guild_management_body_identity(request, result)
    return result


def new_guild_management_request(
    settings: Settings,
    guild: Guild,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any] | None = None,
) -> GuildManagementRequest:
    issued_at = int(time.time())
    return GuildManagementRequest(
        guild=GuildManagementRef(id=str(guild.id), domain=guild.origin_domain),
        actor=GuildManagementRef(id=str(actor.id), domain=actor.origin_domain),
        requesting_instance=settings.domain,
        request_id=f"kagm_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + GUILD_MANAGEMENT_DEADLINE_SECONDS,
        operation=operation,
        payload=payload or {},
    )


async def remote_management_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
) -> Guild | None:
    """Return a verified remote replica, or ``None`` for a local guild.

    The authority repeats membership and permission checks. The home-side
    membership check prevents this endpoint from becoming a blind cross-peer
    oracle for users who have no local guild relationship.
    """

    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain == settings.domain:
        return None
    if (
        actor.origin_domain != settings.domain
        or not actor.is_local
        or actor.account_type not in {"human", "bot"}
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    guild = await session.get(Guild, (guild_id, guild_domain))
    member = await session.get(
        GuildMember,
        (guild_id, guild_domain, actor.id, actor.origin_domain),
    )
    if guild is None or guild.unavailable or member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def request_guild_management(
    session: AsyncSession,
    settings: Settings,
    request: GuildManagementRequest,
) -> GuildManagementResult:
    """Send one explicitly typed guild-management RPC and preserve API errors."""

    result = await request_management_rpc(
        session,
        settings,
        authority_domain=request.guild.domain,
        path=f"/_kaede/v1/guilds/{request.guild.id}/management",
        payload=request.model_dump(mode="json"),
        response_model=GuildManagementResult,
        response_matches=lambda result: (
            result.request_id == request.request_id
            and result.operation == request.operation
            and result.guild == request.guild
        ),
        label="guild-management",
        errors=_GUILD_MANAGEMENT_ERRORS,
        send=signed_request,
    )
    return validate_guild_management_result(request, result)


async def proxy_remote_guild_management(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any] | None = None,
) -> GuildManagementResult | None:
    """Run an allowlisted operation at a remote guild authority, if needed."""

    guild = await remote_management_guild(session, settings, guild_ref, actor)
    if guild is None:
        return None
    request = new_guild_management_request(settings, guild, actor, operation, payload)
    return await request_guild_management(session, settings, request)


async def proxy_remote_guild_management_body(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    actor: User,
    operation: GuildManagementOperation,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, object]:
    """Return the body plus an explicit remote marker for body-only APIs.

    The marker keeps a successful remote ``204`` distinguishable from a local
    guild, while centralizing request construction and response validation.
    """

    result = await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        actor,
        operation,
        payload,
    )
    if result is None:
        return False, None
    return True, _validate_guild_management_result_shape(operation, result).body


async def authorize_guild_management_request(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    guild_id: int,
    request: GuildManagementRequest,
) -> tuple[Guild, User]:
    """Authenticate actor/guild binding and consume the request id once."""

    require_guild_federation_access(principal)
    if (
        principal.origin == settings.domain
        or request.requesting_instance != principal.origin
        or request.actor.domain != principal.origin
        or request.guild.domain != settings.domain
        or int(request.guild.id) != guild_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_GUILD_MANAGEMENT_CALLER_MISMATCH"},
        )
    await consume_management_request_once(
        redis,
        settings,
        origin=principal.origin,
        namespace="guild-management",
        request_id=request.request_id,
        issued_at=request.issued_at,
        deadline=request.deadline,
        now=int(time.time()),
        expired_code="KAED_FED_GUILD_MANAGEMENT_REQUEST_EXPIRED",
        replayed_code="KAED_FED_GUILD_MANAGEMENT_REQUEST_REPLAYED",
    )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = await session.get(User, (int(request.actor.id), request.actor.domain))
    if (
        guild is None
        or guild.unavailable
        or actor is None
        or actor.is_local
        or actor.account_type not in {"human", "bot"}
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if request.operation not in GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS:
        await require_remote_user_creation_allowed(session, actor)
    if actor.account_type == "bot":
        contract = BOT_GUILD_MANAGEMENT_CONTRACTS.get(request.operation)
        if contract is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_OPERATION_UNSUPPORTED"})

        installations = list(
            await session.scalars(bot_actor_active_installations_statement(guild, actor).limit(2))
        )
        if len(installations) != 1:
            raise HTTPException(status_code=404, detail={"code": "BOT_INSTALLATION_NOT_FOUND"})
        installation = installations[0]
        granted_scopes = set(installation.granted_scopes or [])
        if not any(set(option).issubset(granted_scopes) for option in contract.scope_options):
            missing_options = [
                tuple(scope for scope in option if scope not in granted_scopes)
                for option in contract.scope_options
            ]
            missing = min(missing_options, key=len)
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "BOT_SCOPE_REQUIRED",
                    "scope": missing[0],
                    "scopes": list(missing),
                },
            )
        permission_options = contract.permission_options
        if request.operation in {"stage_voice_state.get", "stage_voice_state.self"}:
            try:
                permission_options = (
                    federated_stage_voice_state_permissions(
                        request.operation,
                        request.payload,
                        actor_id=actor.id,
                        actor_domain=actor.origin_domain,
                        default_domain=request.requesting_instance,
                    ),
                )
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "KAED_FED_BAD_REQUEST"},
                ) from None
        if not any(
            installation_grants_permissions(installation.granted_permissions, required)
            for required in permission_options
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "MISSING_PERMISSIONS",
                    "permissions": [str(int(item)) for item in permission_options],
                },
            )
    return guild, actor
