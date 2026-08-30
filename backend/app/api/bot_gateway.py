from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import deque
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import and_, false, func, or_, select
from starlette.requests import Request

from app.api.bots import redact_bot_message_payload, redact_bot_thread_payload
from app.bots.auth import BotPrincipal, require_bot, worker_runtime_ready
from app.bots.dm_capability import (
    dm_capability_runtime_ready,
    stored_bot_dm_capability_payload,
    usable_dm_capability,
)
from app.bots.installations import (
    effective_installation_permissions,
    installation_allows_channel,
    usable_guild_installation,
    usable_user_installation,
)
from app.bots.interaction_dispatch import (
    InteractionCreateDispatchError,
    durable_interaction_create_binding_matches,
    interaction_create_event_fingerprint,
    unseal_interaction_create_event,
)
from app.chat.events import interaction_dispatch_audience, user_topic
from app.chat.permissions import (
    bot_guild_permission_grant_from_installation,
    calculate_permissions,
)
from app.chat.presence import (
    BOT_PRESENCE_STATUSES,
    broadcast_presence_preference,
    normalize_bot_presence_activities,
    normalize_presence_since,
)
from app.core.gateway_ops import EVENT_NAMES, GatewayOp
from app.core.permissions import Permission
from app.core.settings import get_settings
from app.core.types import EntityRef, validate_entity_reference
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotDMGrant,
    BotDMGrantConsent,
    BotE2EEDevice,
    BotE2EEParticipation,
    BotInstallation,
    BotInteraction,
    BotToken,
    BotUserInstallation,
    BotWorker,
    InteractionCreateDispatchOutbox,
)
from app.db.models import (
    Channel,
    DMParticipant,
    FederationEvent,
    FederationOutbox,
    Guild,
    GuildMember,
    Message,
    User,
)
from app.federation.network import normalize_domain

router = APIRouter(tags=["bot gateway"])
IDENTIFY_TIMEOUT_SECONDS = 10
HEARTBEAT_INTERVAL_SECONDS = 30
SESSION_TTL_SECONDS = 90
GATEWAY_COMMAND_LIMIT = 120
GATEWAY_COMMAND_WINDOW_SECONDS = 60.0
PRESENCE_UPDATE_LIMIT = 5
PRESENCE_UPDATE_WINDOW_SECONDS = 20.0
MAX_MEMBER_REQUEST_RESULTS_PER_CHUNK = 1000
AUTHORIZATION_RECHECK_SECONDS = 1.0
INTERACTION_SQL_POLL_SECONDS = 0.75
SESSION_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
if current > tonumber(ARGV[1]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return current
"""


def gateway_effective_permissions(
    installed_permissions: int,
    live_permissions: int,
) -> int:
    """Compatibility helper for the Gateway's installation/live intersection."""

    return effective_installation_permissions(installed_permissions, live_permissions)


RELEASE_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 1 then
  redis.call('DEL', KEYS[1])
else
  redis.call('DECR', KEYS[1])
end
return 1
"""

KNOWN_BOT_EVENT_NAMES = frozenset(EVENT_NAMES)
MODERATION_EVENTS = frozenset({"GUILD_BAN_ADD", "GUILD_BAN_REMOVE", "GUILD_MEMBERS_PRUNED"})
EXPRESSION_EVENTS = frozenset(
    {
        "GUILD_EMOJI_CREATE",
        "GUILD_EMOJI_UPDATE",
        "GUILD_EMOJI_DELETE",
        "GUILD_EMOJIS_UPDATE",
        "GUILD_STICKER_CREATE",
        "GUILD_STICKER_UPDATE",
        "GUILD_STICKER_DELETE",
        "GUILD_STICKERS_UPDATE",
    }
)
INTEGRATION_EVENTS = frozenset(
    {
        "GUILD_INTEGRATIONS_UPDATE",
        "INTEGRATION_CREATE",
        "INTEGRATION_UPDATE",
        "INTEGRATION_DELETE",
        "BOT_INSTALLATION_CREATE",
        "BOT_INSTALLATION_UPDATE",
        "BOT_INSTALLATION_DELETE",
    }
)
SCHEDULED_EVENT_EVENTS = frozenset(
    {
        "GUILD_SCHEDULED_EVENT_CREATE",
        "GUILD_SCHEDULED_EVENT_UPDATE",
        "GUILD_SCHEDULED_EVENT_DELETE",
        "GUILD_SCHEDULED_EVENT_USER_ADD",
        "GUILD_SCHEDULED_EVENT_USER_REMOVE",
    }
)
STAGE_INSTANCE_EVENTS = frozenset(
    {
        "STAGE_INSTANCE_CREATE",
        "STAGE_INSTANCE_UPDATE",
        "STAGE_INSTANCE_DELETE",
    }
)
SOUNDBOARD_EVENTS = frozenset(
    {
        "GUILD_SOUNDBOARD_SOUND_CREATE",
        "GUILD_SOUNDBOARD_SOUND_UPDATE",
        "GUILD_SOUNDBOARD_SOUND_DELETE",
        "GUILD_SOUNDBOARD_SOUNDS_UPDATE",
    }
)
AUTOMOD_CONFIGURATION_EVENTS = frozenset(
    {
        "AUTO_MODERATION_RULE_CREATE",
        "AUTO_MODERATION_RULE_UPDATE",
        "AUTO_MODERATION_RULE_DELETE",
    }
)
CALL_EVENTS = frozenset({"CALL_CREATE", "CALL_RING", "CALL_ACCEPT", "CALL_DECLINE", "CALL_END"})

# These events identify or mutate a Message resource and therefore inherit the
# containing channel's E2EE admission and history-floor policy. Delivery and
# rejection events are deliberately excluded: they carry only sender-side
# correlation/status data and no message content or private resource snapshot.
MESSAGE_E2EE_RESOURCE_EVENTS = frozenset(
    {
        "MESSAGE_CREATE",
        "MESSAGE_UPDATE",
        "MESSAGE_DELETE",
        "MESSAGE_DELETE_BULK",
        "MESSAGE_REACTION_ADD",
        "MESSAGE_REACTION_REMOVE",
        "MESSAGE_REACTION_REMOVE_ALL",
        "MESSAGE_REACTION_REMOVE_EMOJI",
        "MESSAGE_PIN_UPDATE",
        "MESSAGE_POLL_VOTE_ADD",
        "MESSAGE_POLL_VOTE_REMOVE",
    }
)
MESSAGE_STATUS_EVENTS = frozenset({"MESSAGE_DELIVERY_UPDATE", "MESSAGE_SEND_REJECTED"})
SPARSE_MESSAGE_REFERENCE_EVENTS = frozenset(
    {
        "MESSAGE_REACTION_REMOVE_ALL",
        "MESSAGE_REACTION_REMOVE_EMOJI",
        "MESSAGE_POLL_VOTE_ADD",
        "MESSAGE_POLL_VOTE_REMOVE",
        "ATTACHMENT_UPDATE",
    }
)

EVENT_INTENT_BY_NAME: dict[str, str] = {
    "APPLICATION_COMMAND_PERMISSIONS_UPDATE": "interactions",
    "AUTO_MODERATION_ACTION_EXECUTION": "auto_moderation_execution",
    "DM_OPEN_REJECTED": "direct_messages",
    "GUILD_AUDIT_LOG_ENTRY_CREATE": "guild_moderation",
    "VOICE_CHANNEL_START_TIME_UPDATE": "guilds",
    "VOICE_CHANNEL_STATUS_UPDATE": "guilds",
    "WEBHOOKS_UPDATE": "guild_webhooks",
}
EVENT_INTENT_GROUPS: tuple[tuple[frozenset[str], str], ...] = (
    (MODERATION_EVENTS, "guild_moderation"),
    (EXPRESSION_EVENTS, "guild_expressions"),
    (SOUNDBOARD_EVENTS, "guild_expressions"),
    (INTEGRATION_EVENTS, "guild_integrations"),
    (SCHEDULED_EVENT_EVENTS, "guild_scheduled_events"),
    (STAGE_INSTANCE_EVENTS, "guilds"),
    (AUTOMOD_CONFIGURATION_EVENTS, "auto_moderation_configuration"),
)
EVENT_INTENT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TRACKER", "guild_tasks"),
    ("INVITE_", "guild_invites"),
    ("GUILD_MEMBER", "guild_members"),
    ("PRESENCE", "guild_presences"),
    ("VOICE", "guild_voice_states"),
    ("INTERACTION", "interactions"),
    ("THREAD", "guilds"),
    ("CALL", "direct_messages"),
)
DIRECTIONAL_EVENT_INTENTS: tuple[tuple[str, str, str], ...] = (
    ("ATTACHMENT_UPDATE", "direct_messages", "guild_messages"),
    ("CHANNEL", "direct_messages", "guilds"),
    ("MESSAGE_REACTION", "direct_message_reactions", "guild_message_reactions"),
    ("MESSAGE_POLL_VOTE", "direct_message_polls", "guild_message_polls"),
    ("MESSAGE", "direct_messages", "guild_messages"),
    ("TYPING", "direct_message_typing", "guild_message_typing"),
    ("VOICE", "direct_messages", "guild_voice_states"),
)
EVENT_SCOPE_BY_NAME: dict[str, str] = {
    "APPLICATION_COMMAND_PERMISSIONS_UPDATE": "applications.commands",
    "ATTACHMENT_UPDATE": "attachments.read",
    "AUTO_MODERATION_ACTION_EXECUTION": "automod.executions.read",
    "DM_OPEN_REJECTED": "dm.send",
    "GUILD_AUDIT_LOG_ENTRY_CREATE": "audit_logs.read",
    "GUILD_MEMBERS_PRUNED": "moderation.prune",
    "VOICE_CHANNEL_EFFECT_SEND": "soundboard.read",
    "VOICE_CHANNEL_MOVE": "voice.connect",
    "VOICE_TOKEN": "voice.connect",
    "WEBHOOKS_UPDATE": "webhooks.read",
}
EVENT_SCOPE_GROUPS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"GUILD_BAN_ADD", "GUILD_BAN_REMOVE"}), "moderation.bans"),
    (EXPRESSION_EVENTS, "expressions.read"),
    (INTEGRATION_EVENTS, "integrations.read"),
    (SCHEDULED_EVENT_EVENTS, "events.read"),
    (STAGE_INSTANCE_EVENTS, "channels.read"),
    (SOUNDBOARD_EVENTS, "soundboard.read"),
    (AUTOMOD_CONFIGURATION_EVENTS, "automod.rules.read"),
    (CALL_EVENTS, "voice.connect"),
)
EVENT_SCOPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("TRACKER", "tasks.read"),
    ("MESSAGE_REACTION", "reactions.read"),
    ("MESSAGE_POLL_VOTE", "polls.read"),
    ("MESSAGE", "messages.metadata"),
    ("INVITE_", "invites.read"),
    ("GUILD_MEMBER", "members.read"),
    ("PRESENCE", "members.read"),
    ("VOICE", "voice.states.read"),
    ("CHANNEL", "channels.read"),
    ("THREAD", "channels.read"),
    ("TYPING", "channels.read"),
    ("GUILD_ROLE", "roles.read"),
    ("INTERACTION", "applications.commands"),
)


def event_group_value(
    event_type: str, groups: tuple[tuple[frozenset[str], str], ...]
) -> str | None:
    return next((value for events, value in groups if event_type in events), None)


def event_prefix_value(event_type: str, prefixes: tuple[tuple[str, str], ...]) -> str | None:
    return next((value for prefix, value in prefixes if event_type.startswith(prefix)), None)


@dataclass(frozen=True, slots=True)
class GatewayAuthorizationState:
    fingerprint: tuple[object, ...]
    installations: tuple[BotInstallation, ...]
    user_installations: tuple[BotUserInstallation, ...] = ()
    dm_capabilities: tuple[BotDMCapability, ...] = ()
    guild_authorizations: tuple[GatewayGuildAuthorization, ...] = ()
    e2ee_device: BotE2EEDevice | None = None


@dataclass(frozen=True, slots=True)
class GatewayGuildAuthorization:
    """Live guild authority attached to one installation.

    Installation grants remain an upper bound, while the member's current
    effective role permissions can revoke capabilities immediately.
    """

    installation_id: int
    guild_id: int
    guild_domain: str
    permission_generation: int
    member_version: int
    effective_permissions: int


@dataclass(frozen=True, slots=True)
class GatewayInstallationGrant:
    """One indivisible installation capability used for direct-topic events.

    A bot's user topic can receive events for every current guild and user
    installation of its application. Keeping each installation's intent and
    scope grants together prevents two unrelated installations from jointly
    authorizing an event or its sensitive message fields.
    """

    installation_id: int
    user_installation: bool
    intents: frozenset[str]
    scopes: frozenset[str]
    guild_id: int | None = None
    guild_domain: str | None = None
    installation_domain: str | None = None
    installation_revision: int | None = None
    dm_capability_grant_id: str | None = None
    dm_capability_revision: int | None = None
    conversation_id: int | None = None
    conversation_domain: str | None = None


@dataclass(frozen=True, slots=True)
class GatewayEventVisibility:
    message_content: bool
    attachments: bool
    thread_history: bool
    member_deltas: bool


@dataclass(frozen=True, slots=True)
class GatewayEventAuthorization:
    direct_grant: GatewayInstallationGrant | None
    effective_intents: set[str]


type GatewayTopicGrant = tuple[
    set[str],
    set[str],
    int | None,
    frozenset[int],
    int | None,
    tuple[GatewayInstallationGrant, ...],
]


class GatewayProtocolError(ValueError):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(slots=True)
class GatewayBootstrap:
    principal: BotPrincipal
    authorization: GatewayAuthorizationState
    installations: list[BotInstallation]
    user_installations: list[BotUserInstallation]
    dm_capabilities: list[BotDMCapability]
    guilds: list[Guild]
    encrypted_by_topic: dict[str, set[tuple[int, str]]]


@dataclass(slots=True)
class GatewayRuntime:
    websocket: WebSocket
    redis: Redis
    sessionmaker: Any
    principal: BotPrincipal
    authorization_guard: GatewayAuthorizationGuard
    visibility: Any
    guilds: list[Guild]
    topic_grants: dict[str, GatewayTopicGrant]
    encrypted_by_topic: dict[str, set[tuple[int, str]]]
    session_key: str
    interaction_create_ids: set[int] = field(default_factory=set)
    command_timestamps: deque[float] = field(default_factory=deque)
    presence_timestamps: deque[float] = field(default_factory=deque)


def gateway_authorization_fingerprint(
    application: BotApplication,
    worker: BotWorker,
    token: BotToken,
    installations: list[BotInstallation] | tuple[BotInstallation, ...],
    user_installations: list[BotUserInstallation] | tuple[BotUserInstallation, ...] = (),
    dm_capabilities: list[BotDMCapability] | tuple[BotDMCapability, ...] = (),
    guild_authorizations: (
        list[GatewayGuildAuthorization] | tuple[GatewayGuildAuthorization, ...]
    ) = (),
    e2ee_device: BotE2EEDevice | None = None,
) -> tuple[object, ...]:
    """Capture every persisted grant that can authorize a Gateway event."""

    installation_grants = tuple(
        sorted(
            (
                installation.id,
                installation.application_id,
                installation.application_domain,
                installation.guild_id,
                installation.guild_domain,
                installation.bot_user_id,
                installation.bot_user_domain,
                installation.status,
                installation.revoked_at,
                installation.grant_revision,
                tuple(sorted(installation.granted_scopes)),
                tuple(sorted(installation.granted_intents)),
                installation.granted_permissions,
                tuple(sorted(installation.channel_restrictions)),
                installation.e2ee_mode,
            )
            for installation in installations
        )
    )
    user_installation_grants = tuple(
        sorted(
            (
                installation.id,
                installation.application_id,
                installation.application_domain,
                installation.user_id,
                installation.user_domain,
                installation.status,
                installation.revoked_at,
                installation.authority_expires_at,
                installation.grant_revision,
                tuple(sorted(installation.granted_scopes)),
                tuple(sorted(installation.granted_intents)),
                tuple(sorted(installation.contexts)),
            )
            for installation in user_installations
        )
    )
    dm_capability_grants = tuple(
        sorted(
            (
                capability.grant_id,
                capability.source_kind,
                capability.source_installation_id,
                capability.source_installation_domain,
                capability.application_id,
                capability.application_domain,
                capability.bot_user_id,
                capability.bot_user_domain,
                capability.target_user_id,
                capability.target_user_domain,
                capability.conversation_id,
                capability.conversation_domain,
                capability.revision,
                capability.status,
                capability.revoked_at,
                tuple(sorted(capability.granted_scopes)),
                tuple(sorted(capability.granted_intents)),
                tuple(sorted(capability.channel_restrictions)),
                capability.e2ee_mode,
                capability.expires_at,
            )
            for capability in dm_capabilities
        )
    )
    live_guild_grants = tuple(
        sorted(
            (
                authorization.installation_id,
                authorization.guild_id,
                authorization.guild_domain,
                authorization.permission_generation,
                authorization.member_version,
                authorization.effective_permissions,
            )
            for authorization in guild_authorizations
        )
    )
    e2ee_device_grant = (
        ()
        if e2ee_device is None
        else (
            e2ee_device.id,
            e2ee_device.source_id,
            e2ee_device.source_domain,
            e2ee_device.protocol_id,
            e2ee_device.application_id,
            e2ee_device.application_domain,
            e2ee_device.worker_id,
            e2ee_device.generation,
            e2ee_device.trust_state,
            e2ee_device.revoked_at,
            tuple(sorted(e2ee_device.capabilities)),
        )
    )
    return (
        application.id,
        application.origin_domain,
        application.bot_user_id,
        application.bot_user_domain,
        application.status,
        application.manifest_generation,
        application.revocation_generation,
        tuple(sorted(application.default_scopes)),
        tuple(sorted(application.default_intents)),
        worker.id,
        worker.application_id,
        worker.application_domain,
        worker.generation,
        worker.revoked_at,
        worker.expires_at,
        tuple(sorted(worker.scopes)),
        tuple(sorted(worker.intents)),
        tuple(sorted(worker.target_domains)),
        worker.session_limit,
        token.id,
        token.application_id,
        token.application_domain,
        token.worker_id,
        token.revoked_at,
        token.expires_at,
        token.dpop_thumbprint,
        tuple(sorted(token.scopes)),
        tuple(sorted(token.intents)),
        token.dm_capability_id,
        token.dm_capability_revision,
        installation_grants,
        user_installation_grants,
        dm_capability_grants,
        live_guild_grants,
        e2ee_device_grant,
    )


async def current_gateway_authorization(
    session: Any,
    principal: BotPrincipal,
    target_domain: str | None = None,
    e2ee_device_id: str | None = None,
    *,
    authority_domain: str,
) -> GatewayAuthorizationState | None:
    """Reload and validate the complete current authorization for a connection."""

    row = (
        await session.execute(
            select(BotToken, BotWorker, BotApplication, User)
            .join(BotWorker, BotWorker.id == BotToken.worker_id)
            .join(
                BotApplication,
                (BotApplication.id == BotToken.application_id)
                & (BotApplication.origin_domain == BotToken.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotToken.id == principal.token.id,
                BotToken.worker_id == principal.worker.id,
                BotToken.application_id == principal.application.id,
                BotToken.application_domain == principal.application.origin_domain,
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    token, worker, application, user = row
    now = datetime.now(UTC)
    if (
        token.revoked_at is not None
        or token.expires_at <= now
        or worker.revoked_at is not None
        or (worker.expires_at is not None and worker.expires_at <= now)
        or application.status != "active"
        or user.account_type != "bot"
        or user.disabled_at is not None
        or (worker.application_id, worker.application_domain)
        != (application.id, application.origin_domain)
        or (application.bot_user_id, application.bot_user_domain) != (user.id, user.origin_domain)
        or (user.id, user.origin_domain) != (principal.user.id, principal.user.origin_domain)
    ):
        return None
    current_scopes = (
        set(token.scopes).intersection(worker.scopes).intersection(application.default_scopes)
    )
    current_intents = (
        set(token.intents).intersection(worker.intents).intersection(application.default_intents)
    )
    if not set(principal.scopes).issubset(current_scopes) or not set(principal.intents).issubset(
        current_intents
    ):
        return None
    capability_bound = token.dm_capability_id is not None
    if capability_bound != (principal.dm_capability_grant_id is not None):
        return None
    installations = (
        ()
        if capability_bound
        else tuple(
            await session.scalars(
                select(BotInstallation)
                .where(
                    BotInstallation.application_id == application.id,
                    BotInstallation.application_domain == application.origin_domain,
                    BotInstallation.bot_user_id == principal.user.id,
                    BotInstallation.bot_user_domain == principal.user.origin_domain,
                    usable_guild_installation(),
                )
                .order_by(BotInstallation.id)
            )
        )
    )
    user_installations = (
        ()
        if capability_bound
        else tuple(
            await session.scalars(
                select(BotUserInstallation)
                .where(
                    BotUserInstallation.application_id == application.id,
                    BotUserInstallation.application_domain == application.origin_domain,
                    usable_user_installation(current_instance_domain=authority_domain),
                )
                .order_by(BotUserInstallation.id)
            )
        )
    )
    dm_capabilities = (
        tuple(
            await session.scalars(
                select(BotDMCapability)
                .join(
                    Channel,
                    (Channel.id == BotDMCapability.conversation_id)
                    & (Channel.origin_domain == BotDMCapability.conversation_domain),
                )
                .join(
                    DMParticipant,
                    (DMParticipant.conversation_id == BotDMCapability.conversation_id)
                    & (DMParticipant.conversation_domain == BotDMCapability.conversation_domain)
                    & (DMParticipant.user_id == BotDMCapability.bot_user_id)
                    & (DMParticipant.user_domain == BotDMCapability.bot_user_domain),
                )
                .join(
                    User,
                    (User.id == BotDMCapability.target_user_id)
                    & (User.origin_domain == BotDMCapability.target_user_domain),
                )
                .where(
                    BotDMCapability.application_id == application.id,
                    BotDMCapability.application_domain == application.origin_domain,
                    BotDMCapability.bot_user_id == principal.user.id,
                    BotDMCapability.bot_user_domain == principal.user.origin_domain,
                    BotDMCapability.id == token.dm_capability_id,
                    BotDMCapability.revision == token.dm_capability_revision,
                    BotDMCapability.grant_id == principal.dm_capability_grant_id,
                    BotDMCapability.conversation_id.is_not(None),
                    *(
                        (BotDMCapability.authority_domain == target_domain,)
                        if target_domain is not None
                        else ()
                    ),
                    usable_dm_capability(at=now),
                    Channel.guild_id.is_(None),
                    Channel.guild_domain.is_(None),
                    Channel.unavailable.is_(False),
                    User.account_type == "human",
                    User.disabled_at.is_(None),
                )
                .order_by(BotDMCapability.grant_id)
            )
        )
        if capability_bound
        else ()
    )
    if capability_bound:
        if len(dm_capabilities) != 1:
            return None
        capability = dm_capabilities[0]
        current_scopes.intersection_update(capability.granted_scopes)
        current_intents.intersection_update(capability.granted_intents)
        if not set(principal.scopes).issubset(current_scopes) or not set(
            principal.intents
        ).issubset(current_intents):
            return None
    if not installations and not user_installations and not dm_capabilities:
        return None
    if target_domain is not None:
        runtime_target = None
        if application.origin_domain != target_domain or capability_bound:
            runtime_target = await session.scalar(
                select(BotApplicationTarget).where(
                    BotApplicationTarget.application_id == application.id,
                    BotApplicationTarget.application_domain == application.origin_domain,
                    BotApplicationTarget.target_domain == target_domain,
                )
            )
        if not worker_runtime_ready(
            application,
            worker,
            runtime_target,
            target_domain=target_domain,
            dm_capability=(dm_capabilities[0] if capability_bound else None),
            now=now,
        ):
            return None
    e2ee_device = None
    if e2ee_device_id is not None:
        e2ee_device = await session.scalar(
            select(BotE2EEDevice).where(
                BotE2EEDevice.protocol_id == e2ee_device_id,
                BotE2EEDevice.application_id == application.id,
                BotE2EEDevice.application_domain == application.origin_domain,
                BotE2EEDevice.worker_id == worker.id,
                BotE2EEDevice.trust_state == "trusted",
                BotE2EEDevice.revoked_at.is_(None),
            )
        )
        if e2ee_device is None:
            return None
    guild_authorizations: list[GatewayGuildAuthorization] = []
    for installation in installations:
        guild = await session.get(
            Guild,
            (installation.guild_id, installation.guild_domain),
            populate_existing=True,
        )
        if guild is None or guild.unavailable:
            return None
        try:
            live_permissions, member = await calculate_permissions(
                session,
                guild,
                principal.user,
                bot_grant=bot_guild_permission_grant_from_installation(installation),
            )
        except HTTPException:
            # A membership can disappear between the installation query and
            # this live permission calculation. Fail the connection closed.
            return None
        guild_authorizations.append(
            GatewayGuildAuthorization(
                installation_id=installation.id,
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                permission_generation=guild.permission_generation,
                member_version=member.member_version,
                effective_permissions=live_permissions,
            )
        )
    return GatewayAuthorizationState(
        gateway_authorization_fingerprint(
            application,
            worker,
            token,
            installations,
            user_installations,
            dm_capabilities,
            guild_authorizations,
            e2ee_device,
        ),
        installations,
        user_installations,
        dm_capabilities,
        tuple(guild_authorizations),
        e2ee_device,
    )


@dataclass(slots=True)
class GatewayAuthorizationGuard:
    sessionmaker: Any
    principal: BotPrincipal
    expected_fingerprint: tuple[object, ...]
    target_domain: str
    e2ee_device_id: str | None = None
    last_checked: float = 0.0

    async def current(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now - self.last_checked < AUTHORIZATION_RECHECK_SECONDS:
            return True
        self.last_checked = now
        async with self.sessionmaker() as session:
            state = await current_gateway_authorization(
                session,
                self.principal,
                self.target_domain,
                self.e2ee_device_id,
                authority_domain=self.target_domain,
            )
        return state is not None and state.fingerprint == self.expected_fingerprint


def authentication_request(websocket: WebSocket, identify: dict[str, Any]) -> Request:
    token = identify.get("token")
    timestamp = identify.get("timestamp")
    nonce = identify.get("nonce")
    proof = identify.get("proof")
    if not all(isinstance(value, str) for value in (token, nonce, proof)) or not isinstance(
        timestamp, int
    ):
        raise ValueError("identify authentication fields are invalid")
    token = cast(str, token)
    nonce = cast(str, nonce)
    proof = cast(str, proof)
    headers = [
        (b"authorization", f"Bot {token}".encode()),
        (b"x-kaede-bot-timestamp", str(timestamp).encode()),
        (b"x-kaede-bot-nonce", nonce.encode()),
        (b"x-kaede-bot-proof", proof.encode()),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/v1/bots/gateway",
            "raw_path": b"/api/v1/bots/gateway",
            "query_string": b"",
            "headers": headers,
            "client": websocket.client,
            "server": websocket.scope.get("server"),
            "root_path": "",
            "app": websocket.app,
        }
    )


def event_intent(event_type: str, *, direct: bool = False) -> str:
    """Return the canonical intent for a known public event.

    An empty result is fail-closed: a producer from a newer deployment cannot
    accidentally disclose an unclassified event under the broad guild grant.
    """

    if event_type not in KNOWN_BOT_EVENT_NAMES:
        return ""
    directional = next(
        (
            direct_intent if direct else guild_intent
            for prefix, direct_intent, guild_intent in DIRECTIONAL_EVENT_INTENTS
            if event_type.startswith(prefix)
        ),
        None,
    )
    if directional is not None:
        return directional
    if intent := EVENT_INTENT_BY_NAME.get(event_type):
        return intent
    return (
        event_group_value(event_type, EVENT_INTENT_GROUPS)
        or event_prefix_value(event_type, EVENT_INTENT_PREFIXES)
        or "guilds"
    )


def event_scope(event_type: str) -> str:
    """Return the data grant required to receive an event category."""

    if event_type not in KNOWN_BOT_EVENT_NAMES:
        return ""
    if scope := EVENT_SCOPE_BY_NAME.get(event_type):
        return scope
    return (
        event_group_value(event_type, EVENT_SCOPE_GROUPS)
        or event_prefix_value(event_type, EVENT_SCOPE_PREFIXES)
        or "guilds.read"
    )


def event_intents(event_type: str, *, direct: bool = False) -> frozenset[str]:
    """Return canonical and published legacy intents accepted for an event."""

    canonical = event_intent(event_type, direct=direct)
    if not canonical:
        return frozenset()
    intents = {canonical}
    # Emoji/sticker events predate the dedicated Discord-compatible intent and
    # were published under `guilds`. Keep existing installations subscribed.
    if event_type in EXPRESSION_EVENTS or event_type in SOUNDBOARD_EVENTS:
        intents.add("guilds")
    if canonical == "guild_message_reactions":
        intents.add("message_reactions")
    elif canonical == "guild_message_typing":
        intents.add("guild_typing")
    elif canonical == "guild_voice_states":
        intents.add("voice_states")
    return frozenset(intents)


def event_scopes(event_type: str) -> frozenset[str]:
    """Return canonical and broader legacy scopes accepted for an event."""

    canonical = event_scope(event_type)
    if not canonical:
        return frozenset()
    scopes = {canonical}
    if event_type in EXPRESSION_EVENTS:
        scopes.add("guilds.read")
    elif event_type in {"GUILD_BAN_ADD", "GUILD_BAN_REMOVE", "GUILD_MEMBERS_PRUNED"}:
        scopes.add("moderation.members")
    elif event_type == "WEBHOOKS_UPDATE":
        scopes.add("webhooks.manage")
    elif event_type.startswith("INVITE_"):
        scopes.add("invites.manage")
    return frozenset(scopes)


def event_permission_options(event_type: str) -> tuple[Permission, ...]:
    """Return alternative guild permission masks required for sensitive events.

    Intent and OAuth-style scope grants cap which data family an application
    requested. These masks independently enforce the installed bot member's
    Discord-compatible guild permissions.
    """

    if event_type == "GUILD_AUDIT_LOG_ENTRY_CREATE":
        return (Permission.VIEW_AUDIT_LOG,)
    if event_type in {"GUILD_BAN_ADD", "GUILD_BAN_REMOVE"}:
        return (Permission.BAN_MEMBERS, Permission.VIEW_AUDIT_LOG)
    if event_type in AUTOMOD_CONFIGURATION_EVENTS or event_type == (
        "AUTO_MODERATION_ACTION_EXECUTION"
    ):
        return (Permission.MANAGE_GUILD,)
    return ()


def normalized_bot_event_type(event_type: str, data: dict[str, Any]) -> str:
    """Translate shared client projections into stable bot event contracts.

    Current producers publish distinct reaction and pin event names.  Retain
    this translation for queued events from older nodes so rolling upgrades do
    not make ``message_reactions`` depend on ``guild_messages`` or mistake a
    sparse projection for a complete Message resource.
    """

    if event_type == "MESSAGE_UPDATE" and isinstance(data.get("reaction"), str):
        return "MESSAGE_REACTION_REMOVE" if data.get("removed") is True else "MESSAGE_REACTION_ADD"
    if event_type == "MESSAGE_UPDATE" and isinstance(data.get("pinned"), bool):
        return "MESSAGE_PIN_UPDATE"
    return event_type


def guild_context_from_topic(topic: str | None) -> tuple[int, str] | None:
    """Return the authoritative guild context encoded by a subscribed topic."""

    if not isinstance(topic, str) or not topic.startswith("guild:"):
        return None
    parts = topic.split(":", 2)
    if len(parts) != 3:
        return None
    _, domain, raw_id = parts
    try:
        reference = validate_entity_reference(f"{raw_id}@{domain}")
    except ValueError:
        return None
    if reference.id == 0 or reference.domain is None:
        return None
    return reference.id, reference.domain


def payload_guild_reference(data: dict[str, Any]) -> tuple[int, str] | None:
    """Return a canonical guild reference carried by a private user-topic event."""

    raw_id = data.get("guild_id")
    raw_domain = data.get("guild_domain")
    if raw_id is None and raw_domain is None:
        return None
    if not isinstance(raw_id, str) or not isinstance(raw_domain, str):
        raise ValueError("event guild reference is incomplete")
    try:
        reference = validate_entity_reference(f"{raw_id}@{raw_domain}")
    except ValueError as exc:
        raise ValueError("event guild reference is invalid") from exc
    if reference.domain is None:
        raise ValueError("event guild reference is incomplete")
    return reference.id, reference.domain


def private_guild_event(
    event_type: str,
    data: dict[str, Any],
    *,
    direct_topic: bool,
) -> tuple[int, str] | None:
    """Classify guild-private events multiplexed onto a bot-user topic."""

    if not direct_topic or event_type.startswith("INTERACTION"):
        return None
    return payload_guild_reference(data)


def direct_event_channel_reference(event: dict[str, Any]) -> tuple[int, str] | None:
    """Return the strict DM channel reference carried by a direct event.

    Direct channel resources use their own canonical identity fields; every
    other direct event must name its containing channel explicitly.  There is
    deliberately no local-domain fallback because a retained or malformed
    event must not be able to select an unrelated conversation.
    """

    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    resource = event_type in {"CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE"}
    return _payload_channel_reference(data, resource=resource)


def _payload_channel_reference(
    data: dict[str, Any],
    *,
    resource: bool,
) -> tuple[int, str] | None:
    raw_id = data.get("id" if resource else "channel_id")
    raw_domain = data.get("origin_domain" if resource else "channel_domain")
    if not isinstance(raw_id, str) or not isinstance(raw_domain, str):
        return None
    try:
        reference = validate_entity_reference(f"{raw_id}@{raw_domain}")
    except ValueError:
        return None
    if reference.domain is None:
        return None
    return reference.id, reference.domain


def canonical_direct_channel_tombstone(event: dict[str, Any]) -> bool:
    """Allow only the data-free tombstone emitted after a DM removal."""

    data = event.get("d")
    return (
        event.get("t") == "CHANNEL_DELETE"
        and isinstance(data, dict)
        and set(data) == {"id", "origin_domain"}
        and direct_event_channel_reference(event) is not None
    )


def canonical_dm_open_rejection(data: dict[str, Any]) -> tuple[str, str] | None:
    """Validate the channel-less terminal projection before correlation lookup."""

    if set(data) != {"pair_key", "code", "authority_domain"}:
        return None
    pair_key = data.get("pair_key")
    code = data.get("code")
    authority = data.get("authority_domain")
    if (
        not isinstance(pair_key, str)
        or len(pair_key) != 64
        or any(character not in "0123456789abcdef" for character in pair_key)
        or not isinstance(code, str)
        or not 1 <= len(code) <= 64
        or not code.replace("_", "").isalnum()
        or not isinstance(authority, str)
    ):
        return None
    try:
        canonical_authority = normalize_domain(authority)
    except ValueError:
        return None
    if canonical_authority != authority:
        return None
    return pair_key, authority


async def current_dm_open_rejection_access(
    session: Any,
    principal: BotPrincipal,
    data: dict[str, Any],
) -> bool:
    rejection = canonical_dm_open_rejection(data)
    if rejection is None:
        return False
    pair_key, authority = rejection
    request_id = await session.scalar(
        select(FederationEvent.event_id)
        .join(
            FederationOutbox,
            (FederationOutbox.event_origin_domain == FederationEvent.origin_domain)
            & (FederationOutbox.event_id == FederationEvent.event_id),
        )
        .where(
            FederationOutbox.destination == authority,
            FederationEvent.origin_domain == principal.user.origin_domain,
            FederationEvent.event_type == "dm.open.request",
            FederationEvent.envelope["content"]["pair_key"].as_string() == pair_key,
            FederationEvent.envelope["actor"]["id"].as_string() == str(principal.user.id),
            FederationEvent.envelope["actor"]["domain"].as_string() == principal.user.origin_domain,
        )
        .limit(1)
    )
    return request_id is not None


async def current_direct_event_access(
    sessionmaker: Any,
    principal: BotPrincipal,
    topic: str,
    event: dict[str, Any],
    *,
    target_domain: str | None = None,
) -> bool:
    """Fence a direct-event disclosure against current DM participation.

    User-installed interactions are addressed to an exact installation and do
    not require the application bot to be a conversation participant.  Every
    ordinary event does.  A minimal canonical ``CHANNEL_DELETE`` tombstone is
    the sole exception so a bot can observe its own removal after the durable
    participant row has already been deleted.
    """

    if not topic.startswith("user:"):
        return True
    data = event.get("d")
    if not isinstance(data, dict):
        return False
    if event.get("t") == "DM_OPEN_REJECTED":
        async with sessionmaker() as session:
            return await current_dm_open_rejection_access(session, principal, data)
    channel_ref = direct_event_channel_reference(event)
    if channel_ref is None:
        return False
    if str(event.get("t", "")).startswith("INTERACTION"):
        return True
    if canonical_direct_channel_tombstone(event):
        return True
    channel_id, channel_domain = channel_ref
    try:
        guild_ref = private_guild_event(
            str(event.get("t", "")),
            data,
            direct_topic=True,
        )
    except ValueError:
        return False
    async with sessionmaker() as session:
        if guild_ref is not None:
            guild_id, guild_domain = guild_ref
            installation = await session.scalar(
                select(BotInstallation)
                .where(
                    BotInstallation.application_id == principal.application.id,
                    BotInstallation.application_domain == principal.application.origin_domain,
                    BotInstallation.bot_user_id == principal.user.id,
                    BotInstallation.bot_user_domain == principal.user.origin_domain,
                    BotInstallation.guild_id == guild_id,
                    BotInstallation.guild_domain == guild_domain,
                    usable_guild_installation(),
                )
                .limit(1)
            )
            if installation is None:
                return False
            channel = await session.get(
                Channel,
                (channel_id, channel_domain),
                populate_existing=True,
            )
            return bool(
                channel is not None
                and not channel.unavailable
                and (channel.guild_id, channel.guild_domain) == (guild_id, guild_domain)
                and await installation_allows_channel(session, installation, channel)
            )
        active_capability = await session.scalar(
            select(BotDMCapability.id)
            .join(
                Channel,
                (Channel.id == BotDMCapability.conversation_id)
                & (Channel.origin_domain == BotDMCapability.conversation_domain),
            )
            .join(
                DMParticipant,
                (DMParticipant.conversation_id == BotDMCapability.conversation_id)
                & (DMParticipant.conversation_domain == BotDMCapability.conversation_domain)
                & (DMParticipant.user_id == BotDMCapability.bot_user_id)
                & (DMParticipant.user_domain == BotDMCapability.bot_user_domain),
            )
            .join(
                User,
                (User.id == BotDMCapability.target_user_id)
                & (User.origin_domain == BotDMCapability.target_user_domain),
            )
            .where(
                BotDMCapability.application_id == principal.application.id,
                BotDMCapability.application_domain == principal.application.origin_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                BotDMCapability.conversation_id == channel_id,
                BotDMCapability.conversation_domain == channel_domain,
                *(
                    (BotDMCapability.authority_domain == target_domain,)
                    if target_domain is not None
                    else ()
                ),
                usable_dm_capability(at=datetime.now(UTC)),
                Channel.guild_id.is_(None),
                Channel.guild_domain.is_(None),
                Channel.unavailable.is_(False),
                User.account_type == "human",
                User.disabled_at.is_(None),
            )
            .limit(1)
        )
        if active_capability is not None:
            return True
        capability_lineage = await session.scalar(
            select(BotDMCapability.id)
            .where(
                BotDMCapability.application_id == principal.application.id,
                BotDMCapability.application_domain == principal.application.origin_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                BotDMCapability.conversation_id == channel_id,
                BotDMCapability.conversation_domain == channel_domain,
            )
            .limit(1)
        )
        if capability_lineage is not None:
            # A retained DM participant row cannot revive an expired or
            # revoked signed installation capability.
            return False
        participant_id = await session.scalar(
            select(DMParticipant.user_id)
            .join(
                Channel,
                and_(
                    Channel.id == DMParticipant.conversation_id,
                    Channel.origin_domain == DMParticipant.conversation_domain,
                ),
            )
            .where(
                DMParticipant.conversation_id == channel_id,
                DMParticipant.conversation_domain == channel_domain,
                DMParticipant.user_id == principal.user.id,
                DMParticipant.user_domain == principal.user.origin_domain,
                Channel.guild_id.is_(None),
                Channel.guild_domain.is_(None),
                Channel.unavailable.is_(False),
            )
        )
    return participant_id is not None


def narrow_direct_interaction_candidates(
    data: dict[str, Any], candidates: tuple[GatewayInstallationGrant, ...]
) -> tuple[GatewayInstallationGrant, ...]:
    guild_installation = data.get("installation_id")
    user_installation = data.get("user_installation_id")
    capability_grant = data.get("bot_dm_capability_id")
    raw_revision = data.get("installation_revision")
    revision = _canonical_positive_integer(raw_revision)
    if revision is None:
        return ()
    if capability_grant is not None:
        if (
            data.get("integration_type") != "dm_capability"
            or not isinstance(capability_grant, str)
            or data.get("bot_dm_capability_revision") != raw_revision
            or guild_installation is not None
            or user_installation is not None
        ):
            return ()
        return tuple(
            grant
            for grant in candidates
            if grant.dm_capability_grant_id == capability_grant
            and grant.dm_capability_revision == revision
            and grant.installation_revision == revision
        )
    if guild_installation is not None:
        return tuple(
            grant
            for grant in candidates
            if not grant.user_installation
            and grant.dm_capability_grant_id is None
            and str(grant.installation_id) == str(guild_installation)
            and grant.installation_revision == revision
        )
    if user_installation is not None:
        return tuple(
            grant
            for grant in candidates
            if grant.user_installation
            and grant.dm_capability_grant_id is None
            and str(grant.installation_id) == str(user_installation)
            and grant.installation_revision == revision
        )
    return ()


def narrow_direct_conversation_candidates(
    channel_ref: tuple[int, str] | None,
    candidates: tuple[GatewayInstallationGrant, ...],
    capability_grant_id: object = None,
) -> tuple[GatewayInstallationGrant, ...]:
    """Bind capability-backed DM events to their one exact conversation.

    A capability authority can coexist with unrelated guild or user installs
    for the same application. Once a conversation has a signed capability in
    this authorization snapshot, only that capability may project the event;
    its scopes and intents can never be supplemented by another installation.
    """

    if channel_ref is None:
        return candidates
    matching = tuple(
        grant
        for grant in candidates
        if grant.dm_capability_grant_id is not None
        and (grant.conversation_id, grant.conversation_domain) == channel_ref
    )
    if capability_grant_id is not None:
        if not isinstance(capability_grant_id, str):
            return ()
        return tuple(
            grant for grant in matching if grant.dm_capability_grant_id == capability_grant_id
        )
    if matching:
        return matching
    return tuple(grant for grant in candidates if grant.dm_capability_grant_id is None)


def narrow_private_guild_candidates(
    guild_ref: tuple[int, str],
    candidates: tuple[GatewayInstallationGrant, ...],
) -> tuple[GatewayInstallationGrant, ...]:
    """Bind a guild-private user-topic event to its one guild installation."""

    return tuple(
        grant
        for grant in candidates
        if not grant.user_installation and (grant.guild_id, grant.guild_domain) == guild_ref
    )


def local_interaction_installation_matches(
    data: dict[str, Any],
    *,
    installation_id: int | None,
    user_installation_ids: frozenset[int],
    candidates: tuple[GatewayInstallationGrant, ...],
) -> bool:
    guild_installation = data.get("installation_id")
    user_installation = data.get("user_installation_id")
    if guild_installation is not None:
        return bool(
            installation_id is not None
            and guild_installation == str(installation_id)
            and narrow_direct_interaction_candidates(data, candidates)
        )
    if user_installation is not None:
        return bool(
            str(user_installation) in {str(item) for item in user_installation_ids}
            and narrow_direct_interaction_candidates(data, candidates)
        )
    return False


def interaction_installation_candidates(
    principal: BotPrincipal,
    event: dict[str, Any],
    data: dict[str, Any],
    *,
    direct: bool,
    installation_id: int | None,
    user_installation_ids: frozenset[int],
    candidates: tuple[GatewayInstallationGrant, ...],
) -> tuple[GatewayInstallationGrant, ...] | None:
    """Bind an interaction event to exactly one authorized installation."""

    event_type = str(event["t"])
    if not event_type.startswith("INTERACTION"):
        return candidates
    expected_audience = f"{principal.user.id}@{principal.user.origin_domain}"
    if event_type == "INTERACTION_CREATE" and interaction_dispatch_audience(event) != (
        expected_audience
    ):
        return None
    expected_application = f"{principal.application.id}@{principal.application.origin_domain}"
    if data.get("application_ref") != expected_application:
        return None
    if direct:
        return narrow_direct_interaction_candidates(data, candidates) or None
    if not local_interaction_installation_matches(
        data,
        installation_id=installation_id,
        user_installation_ids=user_installation_ids,
        candidates=candidates,
    ):
        return None
    return candidates


def direct_grant_projection_score(
    grant: GatewayInstallationGrant,
    *,
    principal_intents: set[str],
    principal_scopes: set[str],
) -> tuple[int, int, int, str]:
    """Prefer the single installation that can expose the richest safe projection."""

    rich_capabilities = sum(
        (
            int(
                "message_content" in principal_intents
                and "messages.content" in principal_scopes
                and "message_content" in grant.intents
                and "messages.content" in grant.scopes
            ),
            int("attachments.read" in principal_scopes and "attachments.read" in grant.scopes),
            int(
                {"messages.history", "messages.metadata"}.issubset(principal_scopes)
                and {"messages.history", "messages.metadata"}.issubset(grant.scopes)
            ),
            int("guild_members" in grant.intents and "members.read" in grant.scopes),
        )
    )
    return (
        rich_capabilities,
        int(not grant.user_installation),
        -grant.installation_id,
        grant.dm_capability_grant_id or "",
    )


def authorized_direct_grant(
    candidates: tuple[GatewayInstallationGrant, ...],
    *,
    accepted_intents: frozenset[str],
    accepted_scopes: frozenset[str],
    principal_intents: set[str],
    principal_scopes: set[str],
    require_dm_send: bool,
) -> GatewayInstallationGrant | None:
    """Select one indivisible direct-topic grant; grants never compose."""

    authorized = tuple(
        grant
        for grant in candidates
        if accepted_intents.intersection(principal_intents, grant.intents)
        and accepted_scopes.intersection(principal_scopes, grant.scopes)
        and (not require_dm_send or "dm.send" in grant.scopes)
    )
    if not authorized:
        return None
    return max(
        authorized,
        key=lambda grant: direct_grant_projection_score(
            grant,
            principal_intents=principal_intents,
            principal_scopes=principal_scopes,
        ),
    )


def authorize_gateway_event(
    *,
    direct: bool,
    direct_candidates: tuple[GatewayInstallationGrant, ...],
    accepted_intents: frozenset[str],
    accepted_scopes: frozenset[str],
    principal_intents: set[str],
    principal_scopes: set[str],
    granted_intents: set[str],
    granted_scopes: set[str],
    require_dm_send: bool,
) -> GatewayEventAuthorization | None:
    if direct:
        if require_dm_send and "dm.send" not in principal_scopes:
            return None
        grant = authorized_direct_grant(
            direct_candidates,
            accepted_intents=accepted_intents,
            accepted_scopes=accepted_scopes,
            principal_intents=principal_intents,
            principal_scopes=principal_scopes,
            require_dm_send=require_dm_send,
        )
        if grant is None:
            return None
        return GatewayEventAuthorization(grant, set())
    effective_intents = principal_intents.intersection(granted_intents)
    effective_scopes = principal_scopes.intersection(granted_scopes)
    if not accepted_intents.intersection(effective_intents) or not accepted_scopes.intersection(
        effective_scopes
    ):
        return None
    return GatewayEventAuthorization(None, effective_intents)


def event_permissions_allow(event_type: str, granted_permissions: int | None) -> bool:
    options = event_permission_options(event_type)
    if granted_permissions is None or not options:
        return True
    permissions = Permission(granted_permissions)
    return bool(
        permissions & Permission.ADMINISTRATOR
        or any(permissions & required == required for required in options)
    )


def project_gateway_guild_context(
    rendered: dict[str, Any], event_type: str, topic: str | None
) -> None:
    guild_context = guild_context_from_topic(topic)
    if guild_context is None:
        return
    guild_id, guild_domain = guild_context
    # The subscribed topic is the ACL boundary. Project its canonical context
    # into sparse events instead of trusting optional producer fields.
    rendered["guild_id"] = str(guild_id)
    rendered["guild_domain"] = guild_domain
    if event_type.startswith("VOICE") and rendered.get("channel_id") is not None:
        rendered["channel_domain"] = guild_domain


def gateway_event_visibility(
    *,
    direct_grant: GatewayInstallationGrant | None,
    effective_intents: set[str],
    principal_intents: set[str],
    principal_scopes: set[str],
    granted_scopes: set[str],
) -> GatewayEventVisibility:
    if direct_grant is not None:
        return GatewayEventVisibility(
            message_content=(
                "message_content" in principal_intents
                and "messages.content" in principal_scopes
                and "message_content" in direct_grant.intents
                and "messages.content" in direct_grant.scopes
            ),
            attachments=(
                "attachments.read" in principal_scopes and "attachments.read" in direct_grant.scopes
            ),
            thread_history=(
                {"messages.history", "messages.metadata"}.issubset(principal_scopes)
                and {"messages.history", "messages.metadata"}.issubset(direct_grant.scopes)
            ),
            member_deltas=(
                "guild_members" in direct_grant.intents and "members.read" in direct_grant.scopes
            ),
        )
    return GatewayEventVisibility(
        message_content=(
            "message_content" in effective_intents
            and "messages.content" in principal_scopes
            and "messages.content" in granted_scopes
        ),
        attachments=(
            "attachments.read" in principal_scopes and "attachments.read" in granted_scopes
        ),
        thread_history=(
            {"messages.history", "messages.metadata"}.issubset(principal_scopes)
            and {"messages.history", "messages.metadata"}.issubset(granted_scopes)
        ),
        member_deltas=(
            "guild_members" in effective_intents
            and "members.read" in principal_scopes
            and "members.read" in granted_scopes
        ),
    )


def redact_thread_member_deltas(rendered: dict[str, Any], principal: BotPrincipal) -> bool:
    """Keep only the bot's own delta without mutating the producer event."""

    self_ref = f"{principal.user.id}@{principal.user.origin_domain}"
    added = [
        dict(item)
        for item in rendered.get("added_members", [])
        if isinstance(item, dict) and f"{item.get('user_id')}@{item.get('user_domain')}" == self_ref
    ]
    removed_refs = [
        item
        for item in rendered.get("removed_member_refs", [])
        if isinstance(item, dict) and f"{item.get('id')}@{item.get('origin_domain')}" == self_ref
    ]
    legacy_removed = [
        item
        for item in rendered.get("removed_member_ids", [])
        if item in {str(principal.user.id), self_ref}
    ]
    if not added and not removed_refs and not legacy_removed:
        return False
    for item in added:
        item.pop("member", None)
        item.pop("presence", None)
    rendered["added_members"] = added
    rendered["removed_member_ids"] = (
        [str(principal.user.id)] if (removed_refs or legacy_removed) else []
    )
    rendered["removed_member_refs"] = removed_refs
    return True


def redact_gateway_event_payload(
    event_type: str,
    rendered: dict[str, Any],
    *,
    visibility: GatewayEventVisibility,
    principal: BotPrincipal,
    direct: bool,
    can_read_e2ee: bool = False,
) -> dict[str, Any]:
    if event_type == "AUTO_MODERATION_ACTION_EXECUTION" and not visibility.message_content:
        # Discord gates both fields behind the privileged MESSAGE_CONTENT
        # intent. The configured matched keyword remains visible.
        rendered["content"] = ""
        rendered["matched_content"] = None
        rendered["content_digest"] = None
    if event_type in {"MESSAGE_CREATE", "MESSAGE_UPDATE"}:
        rendered = redact_bot_message_payload(
            rendered,
            can_read_content=visibility.message_content,
            can_read_attachments=visibility.attachments,
            principal=principal,
            direct_message=direct,
            can_read_e2ee=can_read_e2ee,
        )
    if event_type.startswith("THREAD"):
        redact_bot_thread_payload(
            rendered,
            can_read_history=visibility.thread_history,
            can_read_content=visibility.message_content,
            can_read_attachments=visibility.attachments,
            principal=principal,
            direct_message=direct,
            can_read_e2ee=can_read_e2ee,
        )
    if event_type == "THREAD_LIST_SYNC" and isinstance(rendered.get("threads"), list):
        rendered["threads"] = [
            redact_bot_thread_payload(
                dict(item),
                can_read_history=visibility.thread_history,
                can_read_content=visibility.message_content,
                can_read_attachments=visibility.attachments,
                principal=principal,
                direct_message=direct,
                can_read_e2ee=can_read_e2ee,
            )
            for item in rendered["threads"]
            if isinstance(item, dict)
        ]
    return rendered


def filtered_event(
    principal: BotPrincipal,
    event: dict[str, Any],
    granted_intents: set[str],
    granted_scopes: set[str],
    *,
    topic: str | None = None,
    installation_id: int | None = None,
    user_installation_ids: frozenset[int] = frozenset(),
    granted_permissions: int | None = None,
    installation_grants: tuple[GatewayInstallationGrant, ...] = (),
    can_read_e2ee: bool = False,
) -> dict[str, Any] | None:
    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    event_type = normalized_bot_event_type(event_type, data)
    direct = isinstance(topic, str) and topic.startswith("user:")
    try:
        private_guild_ref = private_guild_event(
            event_type,
            data,
            direct_topic=direct,
        )
    except ValueError:
        return None
    direct_dm_context = direct and private_guild_ref is None
    accepted_intents = event_intents(event_type, direct=direct_dm_context)
    accepted_scopes = event_scopes(event_type)
    if not accepted_intents or not accepted_scopes:
        return None
    direct_candidates = interaction_installation_candidates(
        principal,
        event,
        data,
        direct=direct,
        installation_id=installation_id,
        user_installation_ids=user_installation_ids,
        candidates=narrow_direct_conversation_candidates(
            direct_event_channel_reference({"t": event_type, "d": data})
            if direct_dm_context
            else None,
            installation_grants,
            data.get("bot_dm_capability_id"),
        ),
    )
    if direct_candidates is None:
        return None
    if private_guild_ref is not None:
        direct_candidates = narrow_private_guild_candidates(
            private_guild_ref,
            direct_candidates,
        )
        if not direct_candidates:
            return None
    principal_intents = set(principal.intents)
    principal_scopes = set(principal.scopes)
    direct_dm_event = direct_dm_context and not event_type.startswith("INTERACTION")
    authorization = authorize_gateway_event(
        direct=direct,
        direct_candidates=direct_candidates,
        accepted_intents=accepted_intents,
        accepted_scopes=accepted_scopes,
        principal_intents=principal_intents,
        principal_scopes=principal_scopes,
        granted_intents=granted_intents,
        granted_scopes=granted_scopes,
        require_dm_send=direct_dm_event,
    )
    if authorization is None or not event_permissions_allow(event_type, granted_permissions):
        return None
    if event_type == "VOICE_CHANNEL_EFFECT_SEND":
        available_scopes = (
            authorization.direct_grant.scopes
            if authorization.direct_grant is not None
            else frozenset(principal_scopes.intersection(granted_scopes))
        )
        if "voice.listen" not in available_scopes:
            return None
    rendered = dict(data)
    selected_grant = authorization.direct_grant
    if selected_grant is not None and selected_grant.dm_capability_grant_id is not None:
        if selected_grant.dm_capability_revision is None:
            return None
        rendered["bot_dm_capability_id"] = selected_grant.dm_capability_grant_id
        rendered["bot_dm_capability_revision"] = str(selected_grant.dm_capability_revision)
        rendered["installation_ref"] = (
            f"{selected_grant.installation_id}@{selected_grant.installation_domain}"
        )
        rendered["installation_type"] = "user" if selected_grant.user_installation else "guild"
    elif selected_grant is not None and not selected_grant.user_installation:
        rendered["bot_installation_id"] = str(selected_grant.installation_id)
    project_gateway_guild_context(rendered, event_type, topic)
    visibility = gateway_event_visibility(
        direct_grant=selected_grant,
        effective_intents=authorization.effective_intents,
        principal_intents=principal_intents,
        principal_scopes=principal_scopes,
        granted_scopes=granted_scopes,
    )
    if (
        event_type == "THREAD_MEMBERS_UPDATE"
        and not visibility.member_deltas
        and not redact_thread_member_deltas(rendered, principal)
    ):
        return None
    rendered = redact_gateway_event_payload(
        event_type,
        rendered,
        visibility=visibility,
        principal=principal,
        direct=direct,
        can_read_e2ee=can_read_e2ee,
    )
    return {
        "op": 0,
        "t": event_type,
        "s": int(event.get("topic_seq", 0)),
        "d": rendered,
    }


def encrypted_message_event(
    event: dict[str, Any],
    encrypted_channels: set[tuple[int, str]] | frozenset[tuple[int, str]],
) -> bool:
    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not event_type.startswith("MESSAGE"):
        return False
    if event_type in MESSAGE_STATUS_EVENTS:
        return False
    if event_type not in MESSAGE_E2EE_RESOURCE_EVENTS:
        # A newly introduced MESSAGE event must be classified explicitly before
        # it can be disclosed from an encrypted channel.
        return True
    if not isinstance(data, dict):
        return True
    if data.get("e2ee") is not None:
        return True
    try:
        channel_ref = (int(data["channel_id"]), str(data["channel_domain"]))
    except (KeyError, TypeError, ValueError):
        return True
    return channel_ref in encrypted_channels


def encrypted_bot_content_event(
    event: dict[str, Any],
    encrypted_channels: set[tuple[int, str]] | frozenset[tuple[int, str]],
) -> bool:
    """Identify content that needs current worker MLS consent."""

    channel_refs = encrypted_bot_content_channel_refs(event, encrypted_channels)
    return channel_refs is None or bool(channel_refs)


def _thread_payloads(event_type: str, data: dict[str, Any]) -> list[dict[str, Any]] | None:
    if event_type in {"THREAD_CREATE", "THREAD_UPDATE"}:
        return [data]
    if event_type != "THREAD_LIST_SYNC":
        return []
    raw_threads = data.get("threads")
    if not isinstance(raw_threads, list) or any(not isinstance(item, dict) for item in raw_threads):
        return None
    return cast(list[dict[str, Any]], raw_threads)


def encrypted_bot_content_channel_refs(
    event: dict[str, Any],
    encrypted_channels: set[tuple[int, str]] | frozenset[tuple[int, str]],
) -> frozenset[tuple[int, str]] | None:
    """Return every encrypted channel whose nested content the event discloses.

    ``None`` denotes a content-bearing shape whose authority reference cannot
    be authenticated. Callers must fail that event closed.
    """

    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str):
        return frozenset()
    if event_type in MESSAGE_STATUS_EVENTS:
        return frozenset()
    if event_type in MESSAGE_E2EE_RESOURCE_EVENTS or event_type == "ATTACHMENT_UPDATE":
        if not isinstance(data, dict):
            return None
        channel_ref = _payload_channel_reference(data, resource=False)
        if channel_ref is None:
            return None
        return (
            frozenset({channel_ref})
            if data.get("e2ee") is not None or channel_ref in encrypted_channels
            else frozenset()
        )
    if event_type.startswith("MESSAGE"):
        # Unknown MESSAGE events are content-bearing until their projection is
        # reviewed and added to one of the explicit sets above.
        return None
    if event_type == "INTERACTION_CREATE":
        if not isinstance(data, dict):
            return None
        channel_ref = _payload_channel_reference(data, resource=False)
        if channel_ref is None:
            return None
        return (
            frozenset({channel_ref})
            if data.get("encrypted_payload") is not None or channel_ref in encrypted_channels
            else frozenset()
        )
    if not isinstance(data, dict):
        return frozenset()
    thread_payloads = _thread_payloads(event_type, data)
    if thread_payloads is None:
        return None
    encrypted_refs: set[tuple[int, str]] = set()
    for thread in thread_payloads:
        nested_messages: list[dict[str, Any]] = []
        for message_field in ("starter_message", "message"):
            value = thread.get(message_field)
            if value is None:
                continue
            if not isinstance(value, dict):
                return None
            nested_messages.append(value)
        if not nested_messages:
            continue
        channel_ref = _payload_channel_reference(thread, resource=True)
        opaque_content = any(message.get("e2ee") is not None for message in nested_messages)
        encrypted_metadata = (
            thread.get("encryption_mode") == "e2ee"
            or thread.get("e2ee_required") is True
            or (channel_ref is not None and channel_ref in encrypted_channels)
        )
        if opaque_content or encrypted_metadata:
            if channel_ref is None:
                return None
            encrypted_refs.add(channel_ref)
    return frozenset(encrypted_refs)


def _encrypted_event_message_refs(
    event: dict[str, Any],
    channel_ref: tuple[int, str],
) -> frozenset[tuple[int, str]] | None:
    """Return nested message identities used to enforce an MLS history floor."""

    event_type = event.get("t")
    data = event.get("d")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    if event_type == "MESSAGE_DELETE_BULK":
        raw_payloads = data.get("ids")
        if not isinstance(raw_payloads, list) or any(
            not isinstance(payload, dict) for payload in raw_payloads
        ):
            return None
        payloads = cast(list[dict[str, Any]], raw_payloads)
    elif event_type in SPARSE_MESSAGE_REFERENCE_EVENTS:
        payloads = [
            {
                "id": data.get("message_id"),
                "origin_domain": data.get("message_domain"),
            }
        ]
    elif event_type in MESSAGE_E2EE_RESOURCE_EVENTS:
        payloads = [data]
    else:
        threads = _thread_payloads(event_type, data)
        if threads is None:
            return None
        payloads = []
        for thread in threads:
            if _payload_channel_reference(thread, resource=True) != channel_ref:
                continue
            payloads.extend(
                message
                for key in ("starter_message", "message")
                if isinstance(message := thread.get(key), dict)
            )
    refs: set[tuple[int, str]] = set()
    for payload in payloads:
        raw_id = payload.get("id")
        raw_domain = payload.get("origin_domain")
        if not isinstance(raw_id, str) or not isinstance(raw_domain, str):
            return None
        try:
            reference = validate_entity_reference(f"{raw_id}@{raw_domain}")
        except ValueError:
            return None
        if reference.domain is None:
            return None
        refs.add((reference.id, reference.domain))
    return frozenset(refs)


async def encrypted_guild_channels(session: Any, guild: Guild) -> set[tuple[int, str]]:
    rows = await session.execute(
        select(Channel.id, Channel.origin_domain).where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            Channel.unavailable.is_(False),
            (Channel.encryption_mode == "e2ee") | Channel.e2ee_required.is_(True),
        )
    )
    return {(int(channel_id), str(domain)) for channel_id, domain in rows}


async def encrypted_direct_channels(session: Any, bot: User) -> set[tuple[int, str]]:
    """Load encrypted conversations the bot currently participates in.

    Direct Gateway events are published on the bot-user topic, so unlike guild
    topics there is no topic identity from which an encrypted channel can be
    inferred. Keep the exact participant-scoped set alongside that topic and
    refresh it on channel policy/membership projections.
    """

    rows = await session.execute(
        select(Channel.id, Channel.origin_domain)
        .join(
            DMParticipant,
            and_(
                DMParticipant.conversation_id == Channel.id,
                DMParticipant.conversation_domain == Channel.origin_domain,
            ),
        )
        .where(
            DMParticipant.user_id == bot.id,
            DMParticipant.user_domain == bot.origin_domain,
            Channel.guild_id.is_(None),
            Channel.guild_domain.is_(None),
            Channel.unavailable.is_(False),
            (Channel.encryption_mode == "e2ee") | Channel.e2ee_required.is_(True),
        )
    )
    return {(int(channel_id), str(domain)) for channel_id, domain in rows}


async def _active_bot_event_participation(
    session: Any,
    principal: BotPrincipal,
    channel: Channel,
    installation_id: int | None,
    e2ee_device_id: str,
) -> BotE2EEParticipation | None:
    if (
        principal.dm_capability_grant_id is not None
        and principal.dm_capability_revision is not None
    ):
        direct_grants = (
            select(BotDMGrant.id)
            .join(
                BotDMCapability,
                BotDMCapability.id == BotDMGrant.dm_capability_id,
            )
            .where(
                BotDMGrant.conversation_id == channel.id,
                BotDMGrant.conversation_domain == channel.origin_domain,
                BotDMGrant.application_id == principal.application.id,
                BotDMGrant.application_domain == principal.application.origin_domain,
                BotDMGrant.consent_state == "active",
                BotDMGrant.revoked_at.is_(None),
                BotDMCapability.grant_id == principal.dm_capability_grant_id,
                BotDMCapability.revision == principal.dm_capability_revision,
                BotDMCapability.application_id == principal.application.id,
                BotDMCapability.application_domain == principal.application.origin_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                BotDMCapability.conversation_id == channel.id,
                BotDMCapability.conversation_domain == channel.origin_domain,
                BotDMCapability.e2ee_mode == "participant",
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
    else:
        # Direct E2EE delivery has no installation inferred from the user topic.
        # It is available only to an exactly capability-bound Gateway token.
        direct_grants = select(BotDMGrant.id).where(false())
    return cast(
        BotE2EEParticipation | None,
        await session.scalar(
            select(BotE2EEParticipation)
            .join(BotE2EEDevice, BotE2EEParticipation.device_id == BotE2EEDevice.id)
            .where(
                BotE2EEDevice.application_id == principal.application.id,
                BotE2EEDevice.application_domain == principal.application.origin_domain,
                BotE2EEDevice.worker_id == principal.worker.id,
                BotE2EEDevice.protocol_id == e2ee_device_id,
                BotE2EEDevice.trust_state == "trusted",
                BotE2EEDevice.revoked_at.is_(None),
                BotE2EEParticipation.channel_id == channel.id,
                BotE2EEParticipation.channel_domain == channel.origin_domain,
                BotE2EEParticipation.status == "active",
                or_(
                    (
                        BotE2EEParticipation.installation_id == installation_id
                        if installation_id is not None
                        else false()
                    ),
                    BotE2EEParticipation.dm_grant_id.in_(direct_grants),
                ),
            )
            .limit(1)
        ),
    )


async def _current_dm_e2ee_consent(
    session: Any,
    channel: Channel,
    participation: BotE2EEParticipation,
) -> bool:
    if participation.dm_grant_id is None:
        return True
    grant = await session.get(BotDMGrant, participation.dm_grant_id)
    if (
        grant is None
        or grant.revoked_at is not None
        or grant.consent_state != "active"
        or participation.consent_generation != grant.consent_generation
    ):
        return False
    participant_refs = set(
        (
            await session.execute(
                select(DMParticipant.user_id, DMParticipant.user_domain)
                .join(
                    User,
                    (User.id == DMParticipant.user_id)
                    & (User.origin_domain == DMParticipant.user_domain),
                )
                .where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                    User.account_type != "bot",
                )
            )
        ).tuples()
    )
    consent_refs = set(
        (
            await session.execute(
                select(BotDMGrantConsent.user_id, BotDMGrantConsent.user_domain).where(
                    BotDMGrantConsent.grant_id == grant.id,
                    BotDMGrantConsent.consent_generation == grant.consent_generation,
                    BotDMGrantConsent.status == "active",
                    BotDMGrantConsent.revoked_at.is_(None),
                )
            )
        ).tuples()
    )
    return participant_refs <= consent_refs


async def _event_above_e2ee_history_floor(
    session: Any,
    event: dict[str, Any],
    channel_ref: tuple[int, str],
    participation: BotE2EEParticipation,
) -> bool:
    if participation.history_floor_message_id is None:
        return True
    message_refs = _encrypted_event_message_refs(event, channel_ref)
    if message_refs is None:
        return False
    floor = await session.get(
        Message,
        (
            participation.history_floor_message_id,
            participation.history_floor_message_domain,
        ),
    )
    if floor is None or floor.created_at is None:
        return False
    for message_ref in message_refs:
        current = await session.get(Message, message_ref)
        if (
            current is None
            or current.created_at is None
            or (current.created_at, current.id, current.origin_domain)
            <= (floor.created_at, floor.id, floor.origin_domain)
        ):
            return False
    return True


async def current_bot_e2ee_event_access(
    sessionmaker: Any,
    principal: BotPrincipal,
    event: dict[str, Any],
    *,
    encrypted_channels: set[tuple[int, str]] | frozenset[tuple[int, str]],
    installation_id: int | None,
    e2ee_device_id: str | None,
) -> bool:
    """Fence opaque Gateway content to this worker's consented MLS device."""

    if e2ee_device_id is None:
        return False
    channel_refs = encrypted_bot_content_channel_refs(event, encrypted_channels)
    if not channel_refs:
        return False
    if channel_refs is None:
        return False
    async with sessionmaker() as session:
        for channel_ref in sorted(channel_refs, key=lambda ref: (ref[1], ref[0])):
            channel = await session.get(Channel, channel_ref)
            if channel is None or channel.encryption_mode != "e2ee":
                return False
            participation = await _active_bot_event_participation(
                session,
                principal,
                channel,
                installation_id,
                e2ee_device_id,
            )
            if (
                participation is None
                or not await _current_dm_e2ee_consent(session, channel, participation)
                or not await _event_above_e2ee_history_floor(
                    session,
                    event,
                    channel_ref,
                    participation,
                )
            ):
                return False
    return True


def _canonical_positive_integer(value: object) -> int | None:
    if (
        not isinstance(value, str)
        or len(value) > 19
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        return None
    parsed = int(value)
    return parsed if 0 < parsed <= (1 << 63) - 1 else None


async def current_interaction_create_access(
    sessionmaker: Any,
    principal: BotPrincipal,
    event: dict[str, Any],
    *,
    authority_domain: str,
) -> bool:
    """Reload the exact invocation lineage immediately before disclosure."""

    if event.get("t") != "INTERACTION_CREATE":
        return True
    data = event.get("d")
    if not isinstance(data, dict):
        return False
    interaction_id = _canonical_positive_integer(data.get("id"))
    revision = _canonical_positive_integer(data.get("installation_revision"))
    token = data.get("token")
    if (
        interaction_id is None
        or revision is None
        or not isinstance(token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None
    ):
        return False
    try:
        event_fingerprint = interaction_create_event_fingerprint(data)
    except ValueError:
        return False
    async with sessionmaker() as session:
        interaction = await session.get(
            BotInteraction,
            interaction_id,
            populate_existing=True,
        )
        now = datetime.now(UTC)
        if (
            interaction is None
            or interaction.status not in {"pending", "deferred"}
            or interaction.expires_at <= now
            or interaction.installation_revision != revision
            or interaction.token_hash is None
            or interaction.dispatch_fingerprint is None
            or not secrets.compare_digest(
                interaction.token_hash,
                hashlib.sha256(token.encode()).digest(),
            )
            or not secrets.compare_digest(
                interaction.dispatch_fingerprint,
                event_fingerprint,
            )
            or (interaction.application_id, interaction.application_domain)
            != (principal.application.id, principal.application.origin_domain)
            or data.get("interaction_ref") != f"{interaction.id}@{interaction.channel_domain}"
            or data.get("application_ref")
            != f"{interaction.application_id}@{interaction.application_domain}"
            or data.get("channel_id") != str(interaction.channel_id)
            or data.get("channel_domain") != interaction.channel_domain
            or data.get("channel_ref") != f"{interaction.channel_id}@{interaction.channel_domain}"
            or data.get("user_ref") != f"{interaction.user_id}@{interaction.user_domain}"
            or data.get("bot_user_ref") != f"{principal.user.id}@{principal.user.origin_domain}"
            or data.get("type") != interaction.interaction_type
            or data.get("context") != interaction.context
            or data.get("integration_type") != interaction.integration_type
            or data.get("command_id")
            != (str(interaction.command_id) if interaction.command_id is not None else None)
            or data.get("expires_at") != interaction.expires_at.isoformat()
            or interaction.created_at is None
            or data.get("ack_deadline")
            != (interaction.created_at + timedelta(seconds=3)).isoformat()
        ):
            return False
        channel = await session.get(
            Channel,
            (interaction.channel_id, interaction.channel_domain),
            populate_existing=True,
        )
        invoker = await session.get(
            User,
            (interaction.user_id, interaction.user_domain),
            populate_existing=True,
        )
        if (
            channel is None
            or channel.unavailable
            or invoker is None
            or invoker.account_type != "human"
            or invoker.disabled_at is not None
            or ((channel.guild_id is not None) != (interaction.context == "guild"))
            or (channel.guild_id, channel.guild_domain)
            != (interaction.guild_id, interaction.guild_domain)
        ):
            return False
        if interaction.command_id is not None:
            command = await session.get(
                ApplicationCommand,
                interaction.command_id,
                populate_existing=True,
            )
            if (
                command is None
                or command.state != "active"
                or (command.application_id, command.application_domain)
                != (interaction.application_id, interaction.application_domain)
            ):
                return False
        if interaction.integration_type == "guild_install":
            installation = await session.scalar(
                select(BotInstallation).where(
                    BotInstallation.id == interaction.installation_id,
                    BotInstallation.application_id == interaction.application_id,
                    BotInstallation.application_domain == interaction.application_domain,
                    BotInstallation.bot_user_id == principal.user.id,
                    BotInstallation.bot_user_domain == principal.user.origin_domain,
                    BotInstallation.guild_id == interaction.guild_id,
                    BotInstallation.guild_domain == interaction.guild_domain,
                    BotInstallation.grant_revision == revision,
                    usable_guild_installation(),
                )
            )
            return bool(
                installation is not None
                and await installation_allows_channel(session, installation, channel)
                and principal.dm_capability_grant_id is None
                and data.get("installation_id") == str(installation.id)
                and data.get("user_installation_id") is None
                and data.get("bot_dm_capability_id") is None
            )
        if interaction.integration_type == "user_install":
            installation = await session.scalar(
                select(BotUserInstallation).where(
                    BotUserInstallation.id == interaction.user_installation_id,
                    BotUserInstallation.application_id == interaction.application_id,
                    BotUserInstallation.application_domain == interaction.application_domain,
                    BotUserInstallation.user_id == interaction.user_id,
                    BotUserInstallation.user_domain == interaction.user_domain,
                    BotUserInstallation.grant_revision == revision,
                    usable_user_installation(
                        current_instance_domain=authority_domain,
                        at=now,
                    ),
                    BotUserInstallation.contexts.contains([interaction.context]),
                )
            )
            return bool(
                installation is not None
                and principal.dm_capability_grant_id is None
                and data.get("installation_id") is None
                and data.get("user_installation_id") == str(installation.id)
                and data.get("bot_dm_capability_id") is None
            )
        if interaction.integration_type != "dm_capability":
            return False
        capability = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.id == interaction.dm_capability_id,
                BotDMCapability.application_id == interaction.application_id,
                BotDMCapability.application_domain == interaction.application_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                BotDMCapability.target_user_id == interaction.user_id,
                BotDMCapability.target_user_domain == interaction.user_domain,
                BotDMCapability.conversation_id == interaction.channel_id,
                BotDMCapability.conversation_domain == interaction.channel_domain,
                BotDMCapability.authority_domain == interaction.channel_domain,
                BotDMCapability.revision == revision,
                usable_dm_capability(at=now),
            )
        )
        if capability is None:
            return False
        runtime_target = await session.get(
            BotApplicationTarget,
            (
                interaction.application_id,
                interaction.application_domain,
                interaction.channel_domain,
            ),
            populate_existing=True,
        )
        try:
            source_ref = validate_entity_reference(str(data.get("installation_ref", "")))
            stored_bot_dm_capability_payload(capability, now=now)
        except ValueError:
            return False
        return bool(
            principal.dm_capability_grant_id == capability.grant_id
            and principal.dm_capability_revision == capability.revision
            and principal.token.dm_capability_id == capability.id
            and principal.token.dm_capability_revision == capability.revision
            and data.get("installation_id") is None
            and data.get("user_installation_id") is None
            and data.get("bot_dm_capability_id") == capability.grant_id
            and data.get("bot_dm_capability_revision") == str(capability.revision)
            and source_ref.domain is not None
            and (source_ref.id, source_ref.domain)
            == (
                capability.source_installation_id,
                capability.source_installation_domain,
            )
            and dm_capability_runtime_ready(
                principal.application,
                runtime_target,
                capability,
                target_domain=interaction.channel_domain,
                now=now,
            )
        )


async def disclose_current_event(
    websocket: WebSocket,
    sessionmaker: Any,
    principal: BotPrincipal,
    topic: str,
    raw_event: dict[str, Any],
    projected_event: dict[str, Any],
    authorization_guard: GatewayAuthorizationGuard,
    *,
    encrypted_channels: set[tuple[int, str]] | frozenset[tuple[int, str]] = frozenset(),
    installation_id: int | None = None,
) -> bool:
    """Apply last-mile durable fences shared by replay and live delivery.

    ``False`` means the connection authorization changed and the caller must
    close it. A stale direct event for a former participant is simply skipped,
    while the connection remains valid for the bot's other installations.
    """

    if not await authorization_guard.current(force=True):
        return False
    if not await current_interaction_create_access(
        sessionmaker,
        principal,
        raw_event,
        authority_domain=authorization_guard.target_domain,
    ):
        return True
    if not await current_direct_event_access(
        sessionmaker,
        principal,
        topic,
        raw_event,
        target_domain=authorization_guard.target_domain,
    ):
        return True
    if encrypted_bot_content_event(raw_event, encrypted_channels):
        current_e2ee = await current_bot_e2ee_event_access(
            sessionmaker,
            principal,
            raw_event,
            encrypted_channels=encrypted_channels,
            installation_id=installation_id,
            e2ee_device_id=authorization_guard.e2ee_device_id,
        )
        if not current_e2ee:
            return True
    if topic:
        projected_event["topic"] = topic
    await websocket.send_json(projected_event)
    return True


def decode_gateway_stream_event(fields: object) -> dict[str, Any] | None:
    if not isinstance(fields, dict):
        return None
    encoded = fields.get("event")
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    if not isinstance(encoded, str) or not isinstance(event := json.loads(encoded), dict):
        return None
    return event


async def send_gateway_replay_gap(
    websocket: WebSocket,
    topic: str,
    after_sequence: int,
    entries: list[tuple[object, object]],
) -> None:
    if after_sequence <= 0 or not entries:
        return
    first = decode_gateway_stream_event(entries[0][1])
    first_sequence = int(first.get("topic_seq", 0)) if first is not None else 0
    if first_sequence <= after_sequence + 1:
        return
    await websocket.send_json(
        {
            "op": 0,
            "t": "GATEWAY_GAP",
            "s": first_sequence,
            "topic": topic,
            "d": {
                "after_sequence": after_sequence,
                "available_from": first_sequence,
                "resync_required": True,
            },
        }
    )


async def replay_topic(
    websocket: WebSocket,
    redis: Redis,
    principal: BotPrincipal,
    topic: str,
    after_sequence: int,
    granted_intents: set[str],
    granted_scopes: set[str],
    installation_id: int | None,
    user_installation_ids: frozenset[int],
    granted_permissions: int | None,
    sessionmaker: Any,
    visibility: Any,
    encrypted_channels: set[tuple[int, str]],
    authorization_guard: GatewayAuthorizationGuard,
    installation_grants: tuple[GatewayInstallationGrant, ...] = (),
) -> bool:
    # Reuse the user Gateway's durable ACL fence without initializing the
    # standalone Gateway service when this API router is imported.
    from app.gateway import event_visibility

    if not await authorization_guard.current(force=True):
        return False
    entries = await redis.xrange(f"dispatch:stream:{topic}", min="-", max="+", count=1000)
    await send_gateway_replay_gap(websocket, topic, after_sequence, entries)
    for _, fields in entries:
        raw = decode_gateway_stream_event(fields)
        if raw is None or int(raw.get("topic_seq", 0)) <= after_sequence:
            continue
        visible, _ = await event_visibility(
            sessionmaker, redis, principal.user, visibility, topic, raw
        )
        if not visible:
            continue
        can_read_e2ee = False
        if encrypted_bot_content_event(raw, encrypted_channels):
            can_read_e2ee = await current_bot_e2ee_event_access(
                sessionmaker,
                principal,
                raw,
                encrypted_channels=encrypted_channels,
                installation_id=installation_id,
                e2ee_device_id=authorization_guard.e2ee_device_id,
            )
            if not can_read_e2ee:
                continue
        event = filtered_event(
            principal,
            raw,
            granted_intents,
            granted_scopes,
            topic=topic,
            installation_id=installation_id,
            user_installation_ids=user_installation_ids,
            granted_permissions=granted_permissions,
            installation_grants=installation_grants,
            can_read_e2ee=can_read_e2ee,
        )
        # A replay can contain many events. Recheck immediately before each
        # disclosure so concurrent grant or DM membership removal cannot drain
        # an already-materialized replay under the old snapshot.
        if event is not None and not await disclose_current_event(
            websocket,
            sessionmaker,
            principal,
            topic,
            raw,
            event,
            authorization_guard,
            encrypted_channels=encrypted_channels,
            installation_id=installation_id,
        ):
            return False
    return True


def requested_gateway_intents(identify: dict[str, Any], principal: BotPrincipal) -> frozenset[str]:
    requested = identify.get("intents", list(principal.intents))
    if (
        not isinstance(requested, list)
        or len(requested) > 32
        or any(not isinstance(item, str) for item in requested)
        or len(set(requested)) != len(requested)
    ):
        raise GatewayProtocolError(4403, "invalid gateway intents")
    return frozenset(requested).intersection(principal.intents)


def requested_gateway_e2ee_device_id(identify: dict[str, Any]) -> str | None:
    """Return the exact MLS device this Gateway connection can disclose to."""

    device_id = identify.get("e2ee_device_id")
    if device_id is None:
        return None
    if not isinstance(device_id, str) or re.fullmatch(r"kbe_[A-Za-z0-9_-]{43}", device_id) is None:
        raise GatewayProtocolError(4403, "invalid E2EE device ID")
    return device_id


async def load_gateway_bootstrap(
    websocket: WebSocket, redis: Redis, identify: dict[str, Any]
) -> GatewayBootstrap:
    target_domain = get_settings().domain
    e2ee_device_id = requested_gateway_e2ee_device_id(identify)
    async with websocket.app.state.sessionmaker() as session:
        principal = await require_bot(
            authentication_request(websocket, identify),
            session,
            redis,
            get_settings(),
        )
        principal = replace(
            principal,
            intents=requested_gateway_intents(identify, principal),
        )
        authorization = await current_gateway_authorization(
            session,
            principal,
            target_domain,
            e2ee_device_id,
            authority_domain=target_domain,
        )
        if authorization is None:
            raise GatewayProtocolError(4009, "bot authorization changed; reconnect")
        installations = list(authorization.installations)
        user_installations = list(authorization.user_installations)
        dm_capabilities = list(authorization.dm_capabilities)
        guilds = [
            guild
            for installation in installations
            if (
                guild := await session.get(
                    Guild, (installation.guild_id, installation.guild_domain)
                )
            )
            is not None
        ]
        encrypted_by_topic = {
            f"guild:{guild.origin_domain}:{guild.id}": await encrypted_guild_channels(
                session, guild
            )
            for guild in guilds
        }
        encrypted_by_topic[
            user_topic(principal.user.origin_domain, principal.user.id)
        ] = await encrypted_direct_channels(session, principal.user)
    return GatewayBootstrap(
        principal,
        authorization,
        installations,
        user_installations,
        dm_capabilities,
        guilds,
        encrypted_by_topic,
    )


async def admit_gateway_session(redis: Redis, principal: BotPrincipal) -> str:
    session_key = (
        f"bot:gateway:sessions:{principal.application.origin_domain}:{principal.worker.id}"
    )
    admitted = await cast(
        Awaitable[object],
        redis.eval(
            SESSION_SCRIPT,
            1,
            session_key,
            str(principal.worker.session_limit),
            str(SESSION_TTL_SECONDS),
        ),
    )
    if not int(cast(int | str | bytes, admitted)):
        raise GatewayProtocolError(4429, "session concurrency exceeded")
    return session_key


def gateway_topic_grants(bootstrap: GatewayBootstrap) -> dict[str, GatewayTopicGrant]:
    principal = bootstrap.principal
    live_permissions = {
        item.installation_id: item.effective_permissions
        for item in bootstrap.authorization.guild_authorizations
    }
    grants: dict[str, GatewayTopicGrant] = {
        f"guild:{installation.guild_domain}:{installation.guild_id}": (
            set(installation.granted_intents),
            set(installation.granted_scopes),
            installation.id,
            frozenset(),
            live_permissions[installation.id],
            (
                GatewayInstallationGrant(
                    installation_id=installation.id,
                    user_installation=False,
                    intents=frozenset(installation.granted_intents),
                    scopes=frozenset(installation.granted_scopes),
                    guild_id=installation.guild_id,
                    guild_domain=installation.guild_domain,
                    installation_domain=installation.guild_domain,
                    installation_revision=installation.grant_revision,
                ),
            ),
        )
        for installation in bootstrap.installations
        if set(installation.granted_intents).intersection(principal.intents)
    }
    direct_installations: list[BotInstallation | BotUserInstallation] = [
        *bootstrap.installations,
        *bootstrap.user_installations,
    ]
    direct_grants = tuple(
        GatewayInstallationGrant(
            installation_id=installation.id,
            user_installation=isinstance(installation, BotUserInstallation),
            intents=frozenset(installation.granted_intents),
            scopes=frozenset(installation.granted_scopes),
            guild_id=(installation.guild_id if isinstance(installation, BotInstallation) else None),
            guild_domain=(
                installation.guild_domain if isinstance(installation, BotInstallation) else None
            ),
            installation_domain=(
                installation.guild_domain
                if isinstance(installation, BotInstallation)
                else installation.user_domain
            ),
            installation_revision=installation.grant_revision,
        )
        for installation in direct_installations
    ) + tuple(
        GatewayInstallationGrant(
            installation_id=capability.source_installation_id,
            installation_domain=capability.source_installation_domain,
            user_installation=capability.source_kind == "user",
            intents=frozenset(capability.granted_intents),
            scopes=frozenset(capability.granted_scopes),
            guild_id=capability.guild_id,
            guild_domain=capability.guild_domain,
            dm_capability_grant_id=capability.grant_id,
            dm_capability_revision=capability.revision,
            installation_revision=capability.revision,
            conversation_id=capability.conversation_id,
            conversation_domain=capability.conversation_domain,
        )
        for capability in bootstrap.dm_capabilities
    )
    if any(grant.intents.intersection(principal.intents) for grant in direct_grants):
        grants[user_topic(principal.user.origin_domain, principal.user.id)] = (
            set(),
            set(),
            None,
            frozenset(installation.id for installation in bootstrap.user_installations),
            None,
            direct_grants,
        )
    return grants


def resume_cursors(identify: dict[str, Any]) -> dict[str, int]:
    cursors = identify.get("cursors", {})
    if (
        not isinstance(cursors, dict)
        or len(cursors) > 1000
        or any(
            not isinstance(topic, str)
            or not topic
            or type(cursor) is not int
            or not 0 <= cursor <= (1 << 63) - 1
            for topic, cursor in cursors.items()
        )
    ):
        raise GatewayProtocolError(4400, "invalid resume cursors")
    return cast(dict[str, int], cursors)


def gateway_ready_event(bootstrap: GatewayBootstrap) -> dict[str, object]:
    principal = bootstrap.principal
    return {
        "op": 0,
        "t": "READY",
        "s": 0,
        "d": {
            "application_ref": f"{principal.application.id}@{principal.application.origin_domain}",
            "worker_id": str(principal.worker.authority_id),
            "intents": sorted(principal.intents),
            "e2ee_device_id": (
                bootstrap.authorization.e2ee_device.protocol_id
                if bootstrap.authorization.e2ee_device is not None
                else None
            ),
            "installations": [
                {
                    "id": str(installation.id),
                    "guild_ref": f"{installation.guild_id}@{installation.guild_domain}",
                    "capability_revision": str(installation.grant_revision),
                }
                for installation in bootstrap.installations
            ],
            "user_installations": [
                {
                    "id": str(installation.id),
                    "user_ref": f"{installation.user_id}@{installation.user_domain}",
                    "capability_revision": str(installation.grant_revision),
                    "contexts": list(installation.contexts),
                }
                for installation in bootstrap.user_installations
            ],
            "dm_capabilities": [
                {
                    "grant_id": capability.grant_id,
                    "installation_ref": (
                        f"{capability.source_installation_id}@"
                        f"{capability.source_installation_domain}"
                    ),
                    "installation_type": capability.source_kind,
                    "channel_ref": (
                        f"{capability.conversation_id}@{capability.conversation_domain}"
                    ),
                    "capability_revision": str(capability.revision),
                    "expires_at": capability.expires_at.isoformat(),
                }
                for capability in bootstrap.dm_capabilities
            ],
        },
    }


async def send_initial_thread_syncs(runtime: GatewayRuntime) -> None:
    from app.api.threads import active_thread_sync_payload

    guild_by_ref = {(guild.id, guild.origin_domain): guild for guild in runtime.guilds}
    for topic, grants in runtime.topic_grants.items():
        guild_context = guild_context_from_topic(topic)
        if guild_context is None or (guild := guild_by_ref.get(guild_context)) is None:
            continue
        async with runtime.sessionmaker() as session:
            sync_payload = await active_thread_sync_payload(
                session, runtime.redis, guild, runtime.principal.user
            )
        event = filtered_event(
            runtime.principal,
            {"t": "THREAD_LIST_SYNC", "d": sync_payload},
            grants[0],
            grants[1],
            topic=topic,
            installation_id=grants[2],
            user_installation_ids=grants[3],
            granted_permissions=grants[4],
            installation_grants=grants[5],
        )
        if event is not None:
            event.update({"s": 0, "topic": topic})
            await runtime.websocket.send_json(event)


async def replay_gateway_topics(runtime: GatewayRuntime, cursors: dict[str, int]) -> None:
    for topic, grants in runtime.topic_grants.items():
        cursor = cursors.get(topic, 0)
        current = await replay_topic(
            runtime.websocket,
            runtime.redis,
            runtime.principal,
            topic,
            cursor,
            grants[0],
            grants[1],
            grants[2],
            grants[3],
            grants[4],
            runtime.sessionmaker,
            runtime.visibility,
            runtime.encrypted_by_topic.get(topic, set()),
            runtime.authorization_guard,
            installation_grants=grants[5],
        )
        if not current:
            raise GatewayProtocolError(4009, "bot authorization changed; reconnect")


async def replay_pending_interaction_creates(runtime: GatewayRuntime) -> None:
    """Recover sealed creates directly from SQL, independent of Redis retention."""

    if not runtime.topic_grants:
        return
    now = datetime.now(UTC)
    settings = get_settings()
    after_id = 0
    while True:
        async with runtime.sessionmaker() as session:
            rows = list(
                (
                    await session.execute(
                        select(InteractionCreateDispatchOutbox, BotInteraction)
                        .join(
                            BotInteraction,
                            BotInteraction.id == InteractionCreateDispatchOutbox.interaction_id,
                        )
                        .where(
                            InteractionCreateDispatchOutbox.interaction_id > after_id,
                            InteractionCreateDispatchOutbox.topic.in_(runtime.topic_grants),
                            InteractionCreateDispatchOutbox.expires_at > now,
                            BotInteraction.expires_at > now,
                            BotInteraction.status.in_(("pending", "deferred")),
                        )
                        .order_by(InteractionCreateDispatchOutbox.interaction_id)
                        .limit(100)
                    )
                ).tuples()
            )
            if not rows:
                return
            for row, interaction in rows:
                after_id = row.interaction_id
                if interaction.id in runtime.interaction_create_ids:
                    continue
                runtime.interaction_create_ids.add(interaction.id)
                try:
                    data = unseal_interaction_create_event(
                        settings,
                        interaction,
                        row,
                    )
                    if not await durable_interaction_create_binding_matches(
                        session,
                        interaction,
                        row,
                        data,
                        authority_domain=settings.domain,
                    ):
                        continue
                except InteractionCreateDispatchError:
                    continue
                raw: dict[str, Any] = {
                    "t": "INTERACTION_CREATE",
                    "d": data,
                    "audience_user_refs": [row.audience_user_ref],
                    "topic_seq": 0,
                }
                grants = runtime.topic_grants[row.topic]
                encrypted_channels = runtime.encrypted_by_topic.get(row.topic, set())
                event = filtered_event(
                    runtime.principal,
                    raw,
                    grants[0],
                    grants[1],
                    topic=row.topic,
                    installation_id=grants[2],
                    user_installation_ids=grants[3],
                    granted_permissions=grants[4],
                    installation_grants=grants[5],
                    can_read_e2ee=False,
                )
                if event is None:
                    continue
                current = await disclose_current_event(
                    runtime.websocket,
                    runtime.sessionmaker,
                    runtime.principal,
                    row.topic,
                    raw,
                    event,
                    runtime.authorization_guard,
                    encrypted_channels=encrypted_channels,
                    installation_id=grants[2],
                )
                if not current:
                    raise GatewayProtocolError(
                        4009,
                        "bot authorization changed; reconnect",
                    )
        if len(rows) < 100:
            return


def _requested_runtime_guild(runtime: GatewayRuntime, raw_guild_id: str) -> Guild | None:
    guild_id = _canonical_positive_integer(raw_guild_id)
    if guild_id is None:
        return None
    matches = [guild for guild in runtime.guilds if guild.id == guild_id]
    return matches[0] if len(matches) == 1 else None


def _admit_bot_gateway_command(runtime: GatewayRuntime, *, presence: bool = False) -> None:
    """Apply Discord's connection-wide and presence-specific send limits."""

    current = time.monotonic()
    command_timestamps = getattr(runtime, "command_timestamps", None)
    if command_timestamps is None:
        command_timestamps = deque()
        runtime.command_timestamps = command_timestamps
    while command_timestamps and current - command_timestamps[0] >= GATEWAY_COMMAND_WINDOW_SECONDS:
        command_timestamps.popleft()
    if len(command_timestamps) >= GATEWAY_COMMAND_LIMIT:
        raise GatewayProtocolError(4008, "gateway command rate limit exceeded")
    if not presence:
        command_timestamps.append(current)
        return
    presence_timestamps = getattr(runtime, "presence_timestamps", None)
    if presence_timestamps is None:
        presence_timestamps = deque()
        runtime.presence_timestamps = presence_timestamps
    while (
        presence_timestamps and current - presence_timestamps[0] >= PRESENCE_UPDATE_WINDOW_SECONDS
    ):
        presence_timestamps.popleft()
    if len(presence_timestamps) >= PRESENCE_UPDATE_LIMIT:
        raise GatewayProtocolError(4008, "presence update rate limit exceeded")
    command_timestamps.append(current)
    presence_timestamps.append(current)


def _runtime_guild_grant(
    runtime: GatewayRuntime,
    guild: Guild,
) -> tuple[str, GatewayTopicGrant | None]:
    topic = f"guild:{guild.origin_domain}:{guild.id}"
    return topic, runtime.topic_grants.get(topic)


async def _current_runtime_guild_installation(
    runtime: GatewayRuntime,
    guild: Guild,
    *,
    scope: str,
    expected_id: int,
) -> BotInstallation | None:
    """Reload the exact live installation immediately before a bot command."""

    from app.api.bots import installation_for_guild

    async with runtime.sessionmaker() as session:
        try:
            _guild, installation = await installation_for_guild(
                session,
                get_settings(),
                runtime.principal,
                EntityRef(f"{guild.id}@{guild.origin_domain}"),
                scope,
            )
        except HTTPException:
            return None
    if installation.id != expected_id:
        raise GatewayProtocolError(4009, "bot authorization changed; reconnect")
    return installation


async def _handle_bot_presence_update(runtime: GatewayRuntime, data: object) -> None:
    if not isinstance(data, dict) or set(data) != {"since", "activities", "status", "afk"}:
        raise GatewayProtocolError(4400, "invalid presence update")
    status = data.get("status")
    afk = data.get("afk")
    if not isinstance(status, str) or status not in BOT_PRESENCE_STATUSES or type(afk) is not bool:
        raise GatewayProtocolError(4400, "invalid presence update")
    try:
        since = normalize_presence_since(data.get("since"))
        activities = normalize_bot_presence_activities(data.get("activities"))
    except ValueError as exc:
        raise GatewayProtocolError(4400, "invalid presence update") from exc
    _admit_bot_gateway_command(runtime, presence=True)

    # A bot sets presence independently on each directly connected guild
    # authority. Only topics backed by a live guild installation are eligible;
    # a user-install or DM grant must never disclose presence into a guild.
    topics = [
        topic
        for guild in runtime.guilds
        if (topic := _runtime_guild_grant(runtime, guild)[0]) in runtime.topic_grants
        and runtime.topic_grants[topic][2] is not None
    ]
    is_identity_authority = bool(getattr(runtime.principal.user, "is_local", False))
    if not topics and not is_identity_authority:
        return
    visible_status, generation = await broadcast_presence_preference(
        runtime.redis,
        runtime.principal.user,
        status,
        topics,
        activities=activities,
        since=since,
        afk=afk,
    )
    if is_identity_authority:
        from app.gateway import schedule_presence_fanout

        schedule_presence_fanout(runtime.principal.user, visible_status, generation)


def _valid_gateway_entity_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        return None
    return value


async def _send_bot_gateway_dispatch(
    runtime: GatewayRuntime,
    topic: str,
    event_type: str,
    data: dict[str, object],
) -> None:
    await runtime.websocket.send_json(
        {
            "op": GatewayOp.DISPATCH,
            "t": event_type,
            "d": data,
            "s": 0,
            "topic": topic,
        }
    )


async def _handle_bot_voice_state_update(runtime: GatewayRuntime, data: object) -> None:
    if not isinstance(data, dict) or set(data) != {
        "guild_id",
        "channel_id",
        "self_mute",
        "self_deaf",
    }:
        raise GatewayProtocolError(4400, "invalid voice state update")
    raw_guild_id = _valid_gateway_entity_id(data.get("guild_id"))
    raw_channel_id = data.get("channel_id")
    if (
        raw_guild_id is None
        or (raw_channel_id is not None and _valid_gateway_entity_id(raw_channel_id) is None)
        or type(data.get("self_mute")) is not bool
        or type(data.get("self_deaf")) is not bool
    ):
        raise GatewayProtocolError(4400, "invalid voice state update")
    _admit_bot_gateway_command(runtime)
    guild = _requested_runtime_guild(runtime, raw_guild_id)
    if guild is None:
        return
    topic, grant = _runtime_guild_grant(runtime, guild)
    if (
        grant is None
        or grant[2] is None
        or "voice.connect" not in grant[1]
        or not ({"guild_voice_states", "voice_states"} & set(runtime.principal.intents))
        or not ({"guild_voice_states", "voice_states"} & grant[0])
    ):
        return
    installation = await _current_runtime_guild_installation(
        runtime,
        guild,
        scope="voice.connect",
        expected_id=grant[2],
    )
    if installation is None:
        return
    if not ({"guild_voice_states", "voice_states"} & set(installation.granted_intents)):
        raise GatewayProtocolError(4009, "bot authorization changed; reconnect")

    from app.api.bot_voice import (
        bot_channel_voice_token_service,
        bot_disconnect_voice,
        bot_update_voice_self_state,
    )
    from app.voice.rooms import participant_identity
    from app.voice.schemas import (
        BotVoiceDisconnectRequest,
        BotVoiceSelfStateRequest,
        BotVoiceTokenRequest,
    )
    from app.voice.state import occupant_for_identity

    settings = get_settings()
    identity = participant_identity(
        runtime.principal.user.id,
        runtime.principal.user.origin_domain,
    )
    occupant = await occupant_for_identity(
        runtime.redis,
        settings.domain,
        identity,
        guild_id=raw_guild_id,
    )

    if raw_channel_id is None:
        if occupant is None or occupant.guild_id != raw_guild_id:
            return
        generation = occupant.participant_metadata.get("generation")
        channel_domain = occupant.participant_metadata.get("channel_domain")
        if (
            not occupant.connection_id
            or type(generation) is not int
            or not isinstance(channel_domain, str)
        ):
            return
        async with runtime.sessionmaker() as session:
            try:
                await bot_disconnect_voice(
                    EntityRef(f"{occupant.channel_id}@{channel_domain}"),
                    BotVoiceDisconnectRequest(
                        connection_id=occupant.connection_id,
                        generation=generation,
                    ),
                    runtime.principal,
                    session,
                    runtime.redis,
                    settings,
                )
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise GatewayProtocolError(4000, "voice authority unavailable") from exc
                return
        await _send_bot_gateway_dispatch(
            runtime,
            topic,
            "VOICE_STATE_UPDATE",
            {
                "guild_id": raw_guild_id,
                "guild_domain": guild.origin_domain,
                "channel_id": None,
                "channel_domain": None,
                "user_id": str(runtime.principal.user.id),
                "user_domain": runtime.principal.user.origin_domain,
                "self_mute": bool(data["self_mute"]),
                "self_deaf": bool(data["self_deaf"]),
                "connected": False,
            },
        )
        return

    channel_ref = EntityRef(f"{raw_channel_id}@{guild.origin_domain}")
    same_channel = (
        occupant is not None
        and occupant.guild_id == raw_guild_id
        and occupant.channel_id == raw_channel_id
    )
    if same_channel and occupant is not None:
        generation = occupant.participant_metadata.get("generation")
        if not occupant.connection_id or type(generation) is not int:
            return
        async with runtime.sessionmaker() as session:
            try:
                updated = await bot_update_voice_self_state(
                    channel_ref,
                    BotVoiceSelfStateRequest(
                        connection_id=occupant.connection_id,
                        generation=generation,
                        self_mute=bool(data["self_mute"]),
                        self_deaf=bool(data["self_deaf"]),
                    ),
                    runtime.principal,
                    session,
                    runtime.redis,
                    settings,
                )
            except HTTPException as exc:
                if exc.status_code >= 500:
                    raise GatewayProtocolError(4000, "voice authority unavailable") from exc
                return
        await _send_bot_gateway_dispatch(
            runtime,
            topic,
            "VOICE_STATE_UPDATE",
            {
                **updated.state.model_dump(mode="json"),
                "guild_id": raw_guild_id,
                "guild_domain": guild.origin_domain,
                "channel_id": raw_channel_id,
                "channel_domain": guild.origin_domain,
                "connected": True,
                "generation": updated.generation,
            },
        )
        return

    async with runtime.sessionmaker() as session:
        try:
            token = await bot_channel_voice_token_service(
                channel_ref,
                BotVoiceTokenRequest(
                    sender_device_id=runtime.authorization_guard.e2ee_device_id,
                    takeover=True,
                    listen="voice.listen" in grant[1],
                    speak="voice.speak" in grant[1],
                    stream=False,
                ),
                runtime.principal,
                session,
                runtime.redis,
                settings,
                self_mute=bool(data["self_mute"]),
                self_deaf=bool(data["self_deaf"]),
            )
        except HTTPException as exc:
            if exc.status_code >= 500:
                raise GatewayProtocolError(4000, "voice authority unavailable") from exc
            return
    await _send_bot_gateway_dispatch(
        runtime,
        topic,
        "VOICE_TOKEN",
        {
            **token.model_dump(mode="json"),
            "guild_id": raw_guild_id,
            "guild_domain": guild.origin_domain,
        },
    )


def _validated_member_request(
    data: object,
) -> tuple[str, str | None, int | None, list[str] | None, bool, str | None]:
    if not isinstance(data, dict) or not set(data) <= {
        "guild_id",
        "query",
        "limit",
        "presences",
        "user_ids",
        "nonce",
    }:
        raise GatewayProtocolError(4400, "invalid guild member request")
    raw_guild_id = _valid_gateway_entity_id(data.get("guild_id"))
    has_query = "query" in data
    has_user_ids = "user_ids" in data
    if raw_guild_id is None or has_query == has_user_ids:
        raise GatewayProtocolError(4400, "invalid guild member request")
    presences = data.get("presences", False)
    if type(presences) is not bool:
        raise GatewayProtocolError(4400, "invalid guild member request")
    nonce = data.get("nonce")
    try:
        valid_nonce = (
            isinstance(nonce, str) and "\x00" not in nonce and len(nonce.encode("utf-8")) <= 32
        )
    except UnicodeEncodeError:
        valid_nonce = False
    if not valid_nonce:
        nonce = None
    if has_query:
        query = data.get("query")
        limit = data.get("limit")
        try:
            query_is_utf8 = isinstance(query, str) and len(query.encode("utf-8")) <= 400
        except UnicodeEncodeError:
            query_is_utf8 = False
        if (
            not isinstance(query, str)
            or not query_is_utf8
            or "\x00" in query
            or len(query) > 100
            or type(limit) is not int
            or not 0 <= limit <= 100
            or (query != "" and limit == 0)
            or "user_ids" in data
        ):
            raise GatewayProtocolError(4400, "invalid guild member request")
        return raw_guild_id, query, limit, None, presences, nonce
    raw_user_ids = data.get("user_ids")
    if isinstance(raw_user_ids, str):
        user_ids = [raw_user_ids]
    elif isinstance(raw_user_ids, list) and all(isinstance(item, str) for item in raw_user_ids):
        user_ids = list(raw_user_ids)
    else:
        raise GatewayProtocolError(4400, "invalid guild member request")
    if (
        not 1 <= len(user_ids) <= 100
        or len(user_ids) != len(set(user_ids))
        or "limit" in data
        or "query" in data
    ):
        raise GatewayProtocolError(4400, "invalid guild member request")
    try:
        for item in user_ids:
            validate_entity_reference(item)
    except (TypeError, ValueError) as exc:
        raise GatewayProtocolError(4400, "invalid guild member request") from exc
    return raw_guild_id, None, None, user_ids, presences, nonce


async def _member_request_refs(
    runtime: GatewayRuntime,
    guild: Guild,
    raw_user_ids: list[str],
) -> tuple[set[tuple[int, str]], list[str]]:
    qualified: set[tuple[int, str]] = set()
    unresolved_ids: set[int] = set()
    unresolved_by_id: dict[int, str] = {}
    for raw in raw_user_ids:
        reference = validate_entity_reference(raw)
        if "@" in raw:
            qualified.add(reference.resolve(guild.origin_domain))
        else:
            unresolved_ids.add(int(raw))
            unresolved_by_id[int(raw)] = raw
    if unresolved_ids:
        async with runtime.sessionmaker() as session:
            rows = list(
                (
                    await session.execute(
                        select(GuildMember.user_id, GuildMember.user_domain).where(
                            GuildMember.guild_id == guild.id,
                            GuildMember.guild_domain == guild.origin_domain,
                            GuildMember.user_id.in_(unresolved_ids),
                        )
                    )
                ).tuples()
            )
        by_id: dict[int, set[str]] = {item: set() for item in unresolved_ids}
        for user_id, user_domain in rows:
            by_id[user_id].add(user_domain)
        for user_id, domains in by_id.items():
            if len(domains) == 1:
                qualified.add((user_id, next(iter(domains))))
    return qualified, [
        unresolved_by_id[user_id] for user_id, domains in by_id.items() if len(domains) != 1
    ] if unresolved_ids else []


def _chunk_presences(members: list[dict[str, object]]) -> list[dict[str, object]]:
    presences: list[dict[str, object]] = []
    for member in members:
        user = member.get("user")
        details = member.pop("presence_details", None)
        if not isinstance(user, dict) or not isinstance(details, dict):
            continue
        status = details.get("status")
        activities = details.get("activities")
        client_status = details.get("client_status")
        user_id = user.get("id")
        user_domain = user.get("origin_domain")
        if (
            isinstance(user_id, str)
            and isinstance(user_domain, str)
            and isinstance(status, str)
            and isinstance(activities, list)
            and isinstance(client_status, dict)
        ):
            presences.append(
                {
                    "user": {"id": user_id, "origin_domain": user_domain},
                    "status": status,
                    "activities": activities,
                    "client_status": client_status,
                }
            )
    return presences


async def _handle_bot_member_request(runtime: GatewayRuntime, data: object) -> None:
    (
        raw_guild_id,
        query,
        limit,
        raw_user_ids,
        include_presences,
        nonce,
    ) = _validated_member_request(data)
    _admit_bot_gateway_command(runtime)
    guild = _requested_runtime_guild(runtime, raw_guild_id)
    if guild is None:
        return
    topic, grant = _runtime_guild_grant(runtime, guild)
    if grant is None or grant[2] is None or "members.read" not in grant[1]:
        return
    if include_presences and (
        "guild_presences" not in runtime.principal.intents or "guild_presences" not in grant[0]
    ):
        raise GatewayProtocolError(4403, "guild presences intent is required")
    entire_member_list = query == "" and limit == 0
    if entire_member_list and (
        "guild_members" not in runtime.principal.intents or "guild_members" not in grant[0]
    ):
        raise GatewayProtocolError(4403, "guild members intent is required")

    from app.gateway import member_payloads

    installation = await _current_runtime_guild_installation(
        runtime,
        guild,
        scope="members.read",
        expected_id=grant[2],
    )
    if installation is None:
        return
    live_intents = set(installation.granted_intents)
    if (include_presences and "guild_presences" not in live_intents) or (
        entire_member_list and "guild_members" not in live_intents
    ):
        raise GatewayProtocolError(4009, "bot authorization changed; reconnect")

    requested_refs: set[tuple[int, str]] | None = None
    not_found: list[str] = []
    if raw_user_ids is not None:
        requested_refs, not_found = await _member_request_refs(runtime, guild, raw_user_ids)

    if entire_member_list:
        async with runtime.sessionmaker() as session:
            member_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(GuildMember)
                    .where(
                        GuildMember.guild_id == guild.id,
                        GuildMember.guild_domain == guild.origin_domain,
                    )
                )
                or 0
            )
        chunk_count = max(
            1,
            (member_count + MAX_MEMBER_REQUEST_RESULTS_PER_CHUNK - 1)
            // MAX_MEMBER_REQUEST_RESULTS_PER_CHUNK,
        )
    else:
        chunk_count = 1

    for chunk_index in range(chunk_count):
        request_limit = (
            MAX_MEMBER_REQUEST_RESULTS_PER_CHUNK
            if entire_member_list
            else (len(requested_refs) if requested_refs is not None else int(limit or 0))
        )
        members = await member_payloads(
            runtime.sessionmaker,
            runtime.redis,
            runtime.principal.user,
            validate_entity_reference(f"{guild.id}@{guild.origin_domain}"),
            query=query or "",
            offset=(
                chunk_index * MAX_MEMBER_REQUEST_RESULTS_PER_CHUNK if entire_member_list else 0
            ),
            limit=request_limit,
            query_prefix=query is not None,
            user_refs=requested_refs,
            include_presence=include_presences,
            include_presence_details=include_presences,
        )
        if members is None:
            return
        if raw_user_ids is not None:
            found = {
                (int(user["id"]), str(user["origin_domain"]))
                for member in members
                if isinstance((user := member.get("user")), dict)
                and isinstance(user.get("id"), str)
                and str(user.get("id")).isascii()
                and str(user.get("id")).isdecimal()
                and isinstance(user.get("origin_domain"), str)
            }
            not_found.extend(
                raw
                for raw in raw_user_ids
                if (
                    validate_entity_reference(raw).resolve(guild.origin_domain) not in found
                    if "@" in raw
                    else not any(user_id == int(raw) for user_id, _domain in found)
                )
                and raw not in not_found
            )
        rendered: dict[str, object] = {
            "guild_id": raw_guild_id,
            "guild_domain": guild.origin_domain,
            "members": members,
            "chunk_index": chunk_index,
            "chunk_count": chunk_count,
            "not_found": not_found,
        }
        if include_presences:
            rendered["presences"] = _chunk_presences(members)
        if nonce is not None:
            rendered["nonce"] = nonce
        await _send_bot_gateway_dispatch(
            runtime,
            topic,
            "GUILD_MEMBERS_CHUNK",
            rendered,
        )


async def _handle_bot_channel_info_request(
    runtime: GatewayRuntime,
    data: object,
) -> None:
    from app.voice.channel_info import (
        validate_channel_info_request,
        visible_guild_channel_info,
    )

    try:
        raw_guild_id, fields = validate_channel_info_request(data)
    except ValueError as exc:
        raise GatewayProtocolError(4400, "invalid channel info request") from exc
    guild = _requested_runtime_guild(runtime, raw_guild_id)
    if guild is None:
        return
    topic = f"guild:{guild.origin_domain}:{guild.id}"
    grant = runtime.topic_grants.get(topic)
    if grant is None or "channels.read" not in grant[1] or "guilds" not in grant[0]:
        return
    settings = get_settings()
    async with runtime.sessionmaker() as session:
        if guild.origin_domain != settings.domain:
            from app.federation.guild_management import proxy_remote_guild_management

            proxied = await proxy_remote_guild_management(
                session,
                settings,
                EntityRef(f"{guild.id}@{guild.origin_domain}"),
                runtime.principal.user,
                "voice_channel_info.get",
                {"fields": list(fields)},
            )
            if proxied is None or not isinstance(proxied.body, dict):
                return
            rendered = dict(proxied.body)
        else:
            rendered = await visible_guild_channel_info(
                session,
                runtime.redis,
                guild,
                runtime.principal.user,
                fields,
            )
            rendered["guild_domain"] = guild.origin_domain
    await runtime.websocket.send_json(
        {"op": GatewayOp.DISPATCH, "t": "CHANNEL_INFO", "d": rendered, "s": 0, "topic": topic}
    )


async def _handle_bot_soundboard_request(
    runtime: GatewayRuntime,
    data: object,
) -> None:
    if not isinstance(data, dict) or set(data) != {"guild_ids"}:
        raise GatewayProtocolError(4400, "invalid soundboard request")
    raw_ids = data.get("guild_ids")
    if (
        not isinstance(raw_ids, list)
        or not 1 <= len(raw_ids) <= 100
        or any(not isinstance(item, str) for item in raw_ids)
        or len(set(raw_ids)) != len(raw_ids)
    ):
        raise GatewayProtocolError(4400, "invalid soundboard request")
    from app.api.soundboard import gateway_soundboard_sounds

    for raw_guild_id in raw_ids:
        guild = _requested_runtime_guild(runtime, raw_guild_id)
        if guild is None:
            continue
        topic = f"guild:{guild.origin_domain}:{guild.id}"
        grant = runtime.topic_grants.get(topic)
        if (
            grant is None
            or "soundboard.read" not in grant[1]
            or "guild_expressions" not in grant[0]
        ):
            continue
        async with runtime.sessionmaker() as session:
            sounds = await gateway_soundboard_sounds(
                session,
                runtime.redis,
                get_settings(),
                guild,
                runtime.principal.user,
                application_id=runtime.principal.application.id,
                application_domain=runtime.principal.application.origin_domain,
            )
        await runtime.websocket.send_json(
            {
                "op": GatewayOp.DISPATCH,
                "t": "GUILD_SOUNDBOARD_SOUNDS_UPDATE",
                "d": {
                    "guild_id": str(guild.id),
                    "guild_domain": guild.origin_domain,
                    "soundboard_sounds": sounds,
                },
                "s": 0,
                "topic": topic,
            }
        )


async def handle_gateway_client_frame(
    runtime: GatewayRuntime, incoming: object, last_heartbeat: float
) -> float:
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.HEARTBEAT:
        _admit_bot_gateway_command(runtime)
        await runtime.websocket.send_json({"op": GatewayOp.HEARTBEAT_ACK})
        return time.monotonic()
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.RESUME:
        _admit_bot_gateway_command(runtime)
        await runtime.websocket.send_json({"op": GatewayOp.HEARTBEAT_ACK})
        return last_heartbeat
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.PRESENCE_UPDATE:
        await _handle_bot_presence_update(runtime, incoming.get("d"))
        return last_heartbeat
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.VOICE_STATE_UPDATE:
        await _handle_bot_voice_state_update(runtime, incoming.get("d"))
        return last_heartbeat
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.REQUEST_MEMBERS:
        await _handle_bot_member_request(runtime, incoming.get("d"))
        return last_heartbeat
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.REQUEST_CHANNEL_INFO:
        _admit_bot_gateway_command(runtime)
        await _handle_bot_channel_info_request(runtime, incoming.get("d"))
        return last_heartbeat
    if isinstance(incoming, dict) and incoming.get("op") == GatewayOp.REQUEST_SOUNDBOARD_SOUNDS:
        _admit_bot_gateway_command(runtime)
        await _handle_bot_soundboard_request(runtime, incoming.get("d"))
        return last_heartbeat
    raise GatewayProtocolError(4400, "unsupported gateway opcode")


def decode_pubsub_event(message: object) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(message, dict):
        return None
    encoded = message.get("data")
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    if not isinstance(encoded, str) or not isinstance(raw := json.loads(encoded), dict):
        return None
    channel = message.get("channel")
    if isinstance(channel, bytes):
        channel = channel.decode()
    topic = (
        channel.removeprefix("dispatch:")
        if isinstance(channel, str) and channel.startswith("dispatch:")
        else ""
    )
    return topic, raw


async def refresh_gateway_encrypted_channels(
    runtime: GatewayRuntime, topic: str, raw: dict[str, Any]
) -> None:
    if raw.get("t") not in {
        "CHANNEL_CREATE",
        "CHANNEL_UPDATE",
        "CHANNEL_DELETE",
        "THREAD_CREATE",
        "THREAD_UPDATE",
        "THREAD_DELETE",
    }:
        return
    direct_topic = user_topic(
        runtime.principal.user.origin_domain,
        runtime.principal.user.id,
    )
    if topic == direct_topic:
        async with runtime.sessionmaker() as session:
            runtime.encrypted_by_topic[topic] = await encrypted_direct_channels(
                session,
                runtime.principal.user,
            )
        return
    guild = next(
        (item for item in runtime.guilds if topic == f"guild:{item.origin_domain}:{item.id}"),
        None,
    )
    if guild is None:
        return
    async with runtime.sessionmaker() as session:
        runtime.encrypted_by_topic[topic] = await encrypted_guild_channels(session, guild)


async def dispatch_live_gateway_event(
    runtime: GatewayRuntime, topic: str, raw: dict[str, Any]
) -> None:
    from app.gateway import event_visibility

    if raw.get("t") == "INTERACTION_CREATE":
        data = raw.get("d")
        raw_id = data.get("id") if isinstance(data, dict) else None
        interaction_id = _canonical_positive_integer(raw_id)
        if interaction_id is None:
            return
        if interaction_id in runtime.interaction_create_ids:
            return
        runtime.interaction_create_ids.add(interaction_id)
    await refresh_gateway_encrypted_channels(runtime, topic, raw)
    visible, _ = await event_visibility(
        runtime.sessionmaker,
        runtime.redis,
        runtime.principal.user,
        runtime.visibility,
        topic,
        raw,
    )
    if not visible:
        return
    grants = runtime.topic_grants.get(topic, (set(), set(), None, frozenset(), None, ()))
    encrypted_channels = runtime.encrypted_by_topic.get(topic, set())
    can_read_e2ee = False
    if encrypted_bot_content_event(raw, encrypted_channels):
        can_read_e2ee = await current_bot_e2ee_event_access(
            runtime.sessionmaker,
            runtime.principal,
            raw,
            encrypted_channels=encrypted_channels,
            installation_id=grants[2],
            e2ee_device_id=runtime.authorization_guard.e2ee_device_id,
        )
        if not can_read_e2ee:
            return
    event = filtered_event(
        runtime.principal,
        raw,
        grants[0],
        grants[1],
        topic=topic,
        installation_id=grants[2],
        user_installation_ids=grants[3],
        granted_permissions=grants[4],
        installation_grants=grants[5],
        can_read_e2ee=can_read_e2ee,
    )
    if event is None:
        return
    current = await disclose_current_event(
        runtime.websocket,
        runtime.sessionmaker,
        runtime.principal,
        topic,
        raw,
        event,
        runtime.authorization_guard,
        encrypted_channels=encrypted_channels,
        installation_id=grants[2],
    )
    if not current:
        raise GatewayProtocolError(4009, "bot authorization changed; reconnect")


async def run_gateway_loop(runtime: GatewayRuntime, pubsub: Any) -> None:
    last_heartbeat = time.monotonic()
    last_interaction_poll = time.monotonic()
    while True:
        if not await runtime.authorization_guard.current():
            raise GatewayProtocolError(4009, "bot authorization changed; reconnect")
        if time.monotonic() - last_heartbeat > HEARTBEAT_INTERVAL_SECONDS * 2:
            raise GatewayProtocolError(4408, "heartbeat timeout")
        await runtime.redis.expire(runtime.session_key, SESSION_TTL_SECONDS)
        incoming_task = asyncio.create_task(runtime.websocket.receive_json())
        pubsub_task = asyncio.create_task(
            pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
        )
        done, pending = await asyncio.wait(
            {incoming_task, pubsub_task},
            timeout=1.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if incoming_task in done:
            last_heartbeat = await handle_gateway_client_frame(
                runtime, incoming_task.result(), last_heartbeat
            )
        if pubsub_task in done and (decoded := decode_pubsub_event(pubsub_task.result())):
            await dispatch_live_gateway_event(runtime, *decoded)
        if time.monotonic() - last_interaction_poll >= INTERACTION_SQL_POLL_SECONDS:
            await replay_pending_interaction_creates(runtime)
            last_interaction_poll = time.monotonic()


@router.websocket("/api/v1/bots/gateway")
async def bot_gateway(websocket: WebSocket) -> None:
    from app.gateway import build_visibility_summary

    await websocket.accept()
    redis = cast(Redis, websocket.app.state.redis)
    session_key: str | None = None
    pubsub = redis.pubsub()
    try:
        await websocket.send_json(
            {"op": 10, "d": {"heartbeat_interval": HEARTBEAT_INTERVAL_SECONDS * 1000}}
        )
        async with asyncio.timeout(IDENTIFY_TIMEOUT_SECONDS):
            identify = await websocket.receive_json()
        if not isinstance(identify, dict) or identify.get("op") != 2:
            raise GatewayProtocolError(4401, "identify required")
        bootstrap = await load_gateway_bootstrap(websocket, redis, identify)
        session_key = await admit_gateway_session(redis, bootstrap.principal)
        authorization_guard = GatewayAuthorizationGuard(
            sessionmaker=websocket.app.state.sessionmaker,
            principal=bootstrap.principal,
            expected_fingerprint=bootstrap.authorization.fingerprint,
            target_domain=get_settings().domain,
            e2ee_device_id=(
                bootstrap.authorization.e2ee_device.protocol_id
                if bootstrap.authorization.e2ee_device is not None
                else None
            ),
        )
        visibility = await build_visibility_summary(
            websocket.app.state.sessionmaker,
            redis,
            bootstrap.principal.user,
            bootstrap.guilds,
        )
        runtime = GatewayRuntime(
            websocket,
            redis,
            websocket.app.state.sessionmaker,
            bootstrap.principal,
            authorization_guard,
            visibility,
            bootstrap.guilds,
            gateway_topic_grants(bootstrap),
            bootstrap.encrypted_by_topic,
            session_key,
        )
        cursors = resume_cursors(identify)
        if not await authorization_guard.current(force=True):
            raise GatewayProtocolError(4009, "bot authorization changed; reconnect")
        if runtime.topic_grants:
            await pubsub.subscribe(*(f"dispatch:{topic}" for topic in runtime.topic_grants))
        await websocket.send_json(gateway_ready_event(bootstrap))
        await send_initial_thread_syncs(runtime)
        await replay_gateway_topics(runtime, cursors)
        await replay_pending_interaction_creates(runtime)
        await run_gateway_loop(runtime, pubsub)
    except GatewayProtocolError as exc:
        await websocket.close(code=exc.code, reason=exc.reason)
    except (TimeoutError, ValueError):
        await websocket.close(code=4401, reason="authentication failed")
    except WebSocketDisconnect:
        pass
    finally:
        await cast(Any, pubsub).aclose()
        if session_key is not None:
            with suppress(Exception):
                await cast(Awaitable[object], redis.eval(RELEASE_SCRIPT, 1, session_key))
