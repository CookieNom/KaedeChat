from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bot_federation import refresh_user_bot_application
from app.api.bots import (
    installation_for_guild,
    user_auth,
)
from app.api.channels import (
    MessageAdmissionOptions,
    MessageCreateTransaction,
    MessageMutationOptions,
    create_message,
    delete_message,
    edit_message,
    ensure_poll_result_message,
    load_interaction_permission_channel_access,
    proxy_remote_dm_message_operation,
    proxy_remote_guild_poll_finalize,
    queue_dm_poll_mutation,
    require_dm_send,
    require_owned_e2ee_sender_device,
    resolve_encrypted_rich_mention_projection,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.media import redirect_to_object
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.auth import BotPrincipal, require_bot
from app.bots.command_contract import (
    command_permission_mask,
    valid_chat_input_name,
    valid_context_command_name,
    valid_numeric_command_value,
)
from app.bots.command_permissions import (
    CommandPermissionEntry,
    CommandPermissionsPut,
    command_permission_allowed,
    guild_member_role_refs,
    guild_permission_rows,
    permission_subject,
    select_effective_rows,
)
from app.bots.dm_capability import (
    dm_capability_runtime_ready,
    stored_bot_dm_capability_payload,
    usable_dm_capability,
)
from app.bots.e2ee import (
    has_active_bot_e2ee_participation,
    require_bot_e2ee_participation,
    require_bot_e2ee_worker_participation,
    revoke_bot_e2ee_access,
)
from app.bots.install_config import (
    REQUIRED_USER_INSTALL_SCOPES,
    USER_INSTALL_CONTEXTS,
    USER_INSTALL_SCOPES,
)
from app.bots.installations import (
    installation_allows_channel,
    installation_has_membership,
    usable_user_installation,
)
from app.bots.interaction_authority import (
    EntitySelect,
    InteractiveComponent,
    resolve_component_entities,
    resolve_interactive_component,
    validate_component_submission,
    validate_modal_submission,
)
from app.bots.interaction_dispatch import (
    drain_interaction_create_dispatch_outbox,
    queue_interaction_create_dispatch,
    wake_interaction_create_dispatch_outbox,
)
from app.bots.interaction_events import (
    interaction_response_event_payload,
    publish_interaction_response_event,
    queue_interaction_response_event,
    wake_interaction_dispatch_outbox,
)
from app.bots.interaction_owners import (
    BOT_DM_GUILD_OWNER,
    GUILD_INSTALL_OWNER,
    INTERACTION_EVENT_SNAPSHOT_KEY,
    INTERACTION_INSTALLATION_LINEAGE_KEY,
    USER_INSTALL_OWNER,
    installation_authority_lineage,
    installation_authorizing_integration_owners,
    normalize_authorizing_integration_owners,
    stored_authorizing_integration_owners,
    stored_installation_authority_lineage,
)
from app.bots.target_discovery import (
    queue_application_target_snapshots_for_refs,
    require_application_runtime_enabled,
    wake_application_target_deliveries,
)
from app.bots.user_install_authority import (
    USER_INSTALLATION_AUTHORITY_LEASE,
    FederatedUserInstallationGrant,
    federated_user_installation_lock,
    locked_federated_user_installation,
    reconcile_federated_user_installation,
    require_federated_user_application,
    require_user_install_policy,
    require_user_install_target,
)
from app.chat.allowed_mentions import (
    ResolvedMentions,
    contains_mention_tokens,
    resolve_allowed_mentions_projection,
)
from app.chat.channel_access import (
    ChannelAccess,
    effective_channel_nsfw,
    load_channel_access,
    publish_channel_dispatch,
)
from app.chat.custom_emojis import resolve_rich_custom_emojis, rich_custom_emojis
from app.chat.e2ee import (
    INTERACTION_CONTRACT_ENVELOPE_FIELDS,
    INTERACTION_RESPONSE_ENVELOPE_FIELDS,
    MessageEncryptionPolicyError,
    interaction_routing_component,
    interaction_routing_contract_digest,
    interaction_routing_modal,
    validate_e2ee_envelope,
    validate_interaction_routing_contract,
    validate_message_encryption_policy,
)
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, user_topic
from app.chat.guild_revision import queue_guild_mutation
from app.chat.hierarchy import highest_role, require_can_manage_role, role_rank
from app.chat.interaction_metadata import validate_interaction_metadata
from app.chat.mention_policy import AllowedMentions
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
    PUBLIC_MESSAGE_EDIT_FLAGS,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2 as MESSAGE_CREATE_FLAG_IS_COMPONENTS_V2,
)
from app.chat.payloads import member_payload, render_message_payload, user_payload
from app.chat.permissions import (
    bot_guild_permission_grant_from_installation,
    get_permissions,
    require_permissions,
)
from app.chat.postcommit import publish_committed_dispatches, queue_postcommit_dispatch
from app.chat.rich_content import (
    MESSAGE_LAYOUT_COMPONENT_ADAPTER,
    Embed,
    FileUpload,
    MessageLayoutComponent,
    Modal,
    PollCreate,
    uses_components_v2,
    validate_message_components,
)
from app.chat.schemas import MessageCreate, MessageEdit, meaningful_optional_content
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.file_types import attachment_matches_file_types
from app.core.json_limits import JsonTreeLimits, validate_json_tree
from app.core.model_validation import UnambiguousInputModel
from app.core.permissions import Permission
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, Snowflake
from app.db.bot_models import (
    ApplicationCommand,
    ApplicationCommandPermission,
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallation,
    BotInteraction,
    BotInteractionPoll,
    BotInteractionPollAnswer,
    BotInteractionPollVote,
    BotInteractionResponse,
    BotUserInstallation,
    FederatedInteractionAdmissionGrant,
    FederatedInteractionAttachmentGrant,
    FederatedInteractionResponseLocator,
    InteractionCreateDispatchOutbox,
)
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    Guild,
    GuildMember,
    MediaTombstoneSource,
    MemberRole,
    Message,
    MessageView,
    Poll,
    RemoteMediaTombstone,
    Role,
    User,
    UserSettings,
)
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replication import (
    remote_media_dimensions,
    sanitized_remote_blurhash,
    sanitized_remote_variants,
)
from app.federation.schemas import FederationDomain, SnowflakeString
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.media.payloads import attachment_payload
from app.media.processing import normalize_declared_type, sanitize_filename
from app.media.schemas import UploadTicketRequest
from app.media.service import (
    MEDIA_ORIGIN_HEADER,
    create_upload_ticket,
    discard_attachment,
    finalize_attachment,
    ticket_payload,
)
from app.media.storage import validate_media_url_origin
from app.media.tombstones import lock_media_tombstone_ref
from app.tasks import federation_deliver, media_local_purge, media_process

router = APIRouter(prefix="/api/v1", tags=["application interactions"])
federation_router = APIRouter(tags=["application interaction federation"])
INTERACTION_LIMIT = ClientRateLimit("application-interaction", 30, 60)
INTERACTION_ATTACHMENT_LIMIT = ClientRateLimit("interaction-attachment", 20, 60)
INTERACTION_LIFETIME = timedelta(minutes=15)
USER_INSTALLATION_AUTHORITY_GRACE = USER_INSTALLATION_AUTHORITY_LEASE - INTERACTION_LIFETIME
InteractionInstallation = BotInstallation | BotUserInstallation | BotDMCapability
INTERACTION_EPHEMERAL_FLAG = 1 << 6
INTERACTION_MESSAGE_FLAG_MASK = (
    MESSAGE_FLAG_SUPPRESS_EMBEDS
    | INTERACTION_EPHEMERAL_FLAG
    | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS
    | MESSAGE_FLAG_IS_VOICE_MESSAGE
    | MESSAGE_CREATE_FLAG_IS_COMPONENTS_V2
)
INTERACTION_OPTION_LIMITS = JsonTreeLimits(
    max_depth=8,
    max_nodes=512,
    max_object_members=25,
    max_array_members=100,
    max_key_bytes=100,
    max_string_bytes=64 * 1024,
)


def private_media_proxy_redirect(location: object, media_origin: object) -> RedirectResponse:
    """Forward one authority-signed media capability without changing its trust root."""

    if not isinstance(location, str) or not isinstance(media_origin, str):
        raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"})
    try:
        validate_media_url_origin(location, media_origin, allow_http=True)
    except ValueError:
        raise HTTPException(
            status_code=503,
            detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
        ) from None
    response = RedirectResponse(location, status_code=307)
    response.headers[MEDIA_ORIGIN_HEADER] = media_origin
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization, Cookie"
    return response


async def queue_interaction_response_relays(
    session: AsyncSession,
    settings: Settings,
    *events: tuple[
        BotInteraction,
        BotInteractionResponse,
        Literal["CREATE", "UPDATE", "DELETE"],
    ],
) -> set[str]:
    destinations: set[str] = set()
    for interaction, stored, operation in events:
        destination = await queue_interaction_response_event(
            session,
            settings,
            interaction,
            stored,
            operation,
        )
        if destination is not None:
            destinations.add(destination)
    return destinations


async def wake_interaction_response_relays(destinations: set[str]) -> None:
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
    await wake_interaction_dispatch_outbox()


@dataclass(frozen=True, slots=True)
class InteractionInvocationOptions:
    """Internal authority metadata unavailable in the public request body."""

    federated_locale: str | None = None
    federated_age_assured_adult: bool = False
    federated_age_restricted_dm_commands_enabled: bool = False
    federated_response_grant_id: str | None = None
    federated_expires_at: datetime | None = None
    federated_request_fingerprint: str | None = None
    federated_attachments: tuple[FederatedInteractionAttachment, ...] = ()

    def __post_init__(self) -> None:
        if self.federated_locale is not None and (
            not 2 <= len(self.federated_locale) <= 16
            or re.fullmatch(
                r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*",
                self.federated_locale,
            )
            is None
        ):
            raise ValueError("federated interaction locale is invalid")


def default_interaction_invocation_options() -> InteractionInvocationOptions:
    return InteractionInvocationOptions()


def federated_interaction_request_fingerprint(payload: FederatedInteractionCreate) -> str:
    serialized = json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True, slots=True)
class InteractionInvokerPolicy:
    locale: str
    age_assured_adult: bool
    age_restricted_dm_commands_enabled: bool


async def authoritative_interaction_invoker_policy(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    options: InteractionInvocationOptions,
) -> InteractionInvokerPolicy:
    """Resolve private invocation policy only at the user's home authority."""

    if actor.is_local and actor.origin_domain == settings.domain:
        account_settings = await session.get(UserSettings, (actor.id, actor.origin_domain))
        return InteractionInvokerPolicy(
            locale=account_settings.locale if account_settings is not None else "en-US",
            age_assured_adult=(getattr(actor, "age_assurance_state", "unknown") == "adult"),
            age_restricted_dm_commands_enabled=bool(
                account_settings is not None and account_settings.age_restricted_dm_commands_enabled
            ),
        )
    return InteractionInvokerPolicy(
        locale=options.federated_locale or "en-US",
        age_assured_adult=options.federated_age_assured_adult,
        age_restricted_dm_commands_enabled=(options.federated_age_restricted_dm_commands_enabled),
    )


def user_context_send_permission(channel_type: int) -> Permission:
    return (
        Permission.SEND_MESSAGES_IN_THREADS
        if channel_type in {10, 11, 12}
        else Permission.SEND_MESSAGES
    )


def application_interaction_required_permissions(
    *,
    guild_installed: bool,
    interaction_type: str = "command",
    command_type: str = "chat_input",
    channel_type: int = 0,
) -> Permission:
    """Return the member permissions for one exact application installation path."""

    required = Permission.USE_APPLICATION_COMMANDS if guild_installed else Permission(0)
    if interaction_type == "command" and command_type == "user":
        required |= user_context_send_permission(channel_type)
    return required


def application_interaction_allowed(
    permissions: int,
    *,
    guild_installed: bool,
    interaction_type: str = "command",
    command_type: str = "chat_input",
    channel_type: int = 0,
) -> bool:
    effective = Permission(permissions)
    if effective & Permission.ADMINISTRATOR:
        return True
    required = application_interaction_required_permissions(
        guild_installed=guild_installed,
        interaction_type=interaction_type,
        command_type=command_type,
        channel_type=channel_type,
    )
    return effective & required == required


def require_age_restricted_command(
    command: ApplicationCommand | None,
    access: ChannelAccess,
    policy: InteractionInvokerPolicy,
    *,
    channel_nsfw: bool | None = None,
) -> None:
    if command is None or command.definition.get("nsfw") is not True:
        return
    if not policy.age_assured_adult:
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_COMMAND_AGE_RESTRICTED"},
        )
    if access.guild is not None:
        if not (
            bool(getattr(access.channel, "nsfw", False)) if channel_nsfw is None else channel_nsfw
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "AGE_RESTRICTED_CHANNEL_REQUIRED"},
            )
        return
    if not policy.age_restricted_dm_commands_enabled:
        raise HTTPException(
            status_code=403,
            detail={"code": "AGE_RESTRICTED_DM_COMMANDS_DISABLED"},
        )


def user_install_response_forced_ephemeral(
    interaction: BotInteraction,
    installation: InteractionInstallation,
) -> bool:
    """Apply Discord's user-app public response capability snapshot."""

    if not isinstance(installation, BotUserInstallation) or interaction.guild_id is None:
        return False
    permissions = Permission(interaction.invocation_permissions or 0)
    if permissions & Permission.ADMINISTRATOR:
        return False
    channel_type = getattr(interaction, "invocation_channel_type", None)
    if isinstance(channel_type, bool) or not isinstance(channel_type, int):
        return True
    send_permission = user_context_send_permission(channel_type)
    public_permissions = (
        Permission.USE_APPLICATION_COMMANDS | Permission.USE_EXTERNAL_APPS | send_permission
    )
    return permissions & public_permissions != public_permissions


def user_install_message_permissions(
    interaction: BotInteraction,
    installation: InteractionInstallation,
) -> int | None:
    """Return the callback-only admission capability for a user installation."""

    if not isinstance(installation, BotUserInstallation):
        return None
    # Private-channel user apps are not conversation participants, so zero is
    # an intentional capability sentinel there. Guild callers reach this only
    # after USE_EXTERNAL_APPS was captured in the snapshot.
    return int(interaction.invocation_permissions or 0)


def interaction_command_message_type(interaction: BotInteraction) -> int | None:
    """Return Discord's public message type for an application-command response."""

    if getattr(interaction, "interaction_type", "command") != "command":
        return None
    command_type = getattr(interaction, "command_type", "chat_input")
    if command_type == "chat_input":
        return 20
    if command_type in {"user", "message"}:
        return 23
    raise RuntimeError("interaction has an invalid application command type")


def interaction_original_response_message(
    interaction: BotInteraction,
    message: InteractionMessageCreate,
) -> InteractionMessageCreate:
    """Bind a message-command response to its authority-resolved target.

    Discord exposes this reference only on the original response to a message
    context-menu command.  The application cannot replace the target selected
    by the invoking user.
    """

    if (
        getattr(interaction, "interaction_type", "command") != "command"
        or getattr(interaction, "command_type", "chat_input") != "message"
    ):
        return message
    stored_payload = interaction.payload if isinstance(interaction.payload, dict) else {}
    raw_target = stored_payload.get("target_ref")
    if not isinstance(raw_target, str):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_TARGET_INVALID"})
    try:
        target_id, target_domain = EntityRef(raw_target).resolve(interaction.channel_domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_TARGET_INVALID"},
        ) from exc
    return message.model_copy(
        update={"referenced_message_id": EntityRef(f"{target_id}@{target_domain}")}
    )


def interaction_message_admission_options(
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    *,
    authoritative_mentions: ResolvedMentions,
    automod_actor: User | None,
    automod_permissions: int | None,
    interaction_metadata: dict[str, object],
    transaction: MessageCreateTransaction,
) -> MessageAdmissionOptions:
    """Build the shared, exact-lineage admission contract for public responses."""

    return MessageAdmissionOptions(
        application_id=principal.application.id,
        application_domain=principal.application.origin_domain,
        bot_installation_id=(
            installation.id if isinstance(installation, BotInstallation) else None
        ),
        bot_user_installation_id=(
            installation.id if isinstance(installation, BotUserInstallation) else None
        ),
        bot_dm_capability_id=(
            installation.id if isinstance(installation, BotDMCapability) else None
        ),
        bot_worker_id=principal.worker.id,
        interaction_permissions=user_install_message_permissions(interaction, installation),
        interaction_message_type=interaction_command_message_type(interaction),
        interaction_metadata=interaction_metadata,
        authoritative_mention_refs=authoritative_mentions.recipients,
        authoritative_mention_role_refs=authoritative_mentions.roles,
        authoritative_mention_role_recipient_refs=authoritative_mentions.role_recipients,
        authoritative_mention_everyone=authoritative_mentions.everyone,
        automod_actor=automod_actor,
        automod_permissions=automod_permissions,
        transaction=transaction,
    )


def interaction_metadata_user(user: User) -> dict[str, object]:
    return {
        "id": str(user.id),
        "origin_domain": user.origin_domain,
        "username": user.username,
        "display_name": user.display_name,
        "avatar_hash": user.avatar_hash,
        "bot": user.account_type == "bot",
    }


def add_interaction_metadata_message_ref(
    metadata: dict[str, object],
    prefix: str,
    reference: tuple[int, str],
) -> None:
    metadata[f"{prefix}_id"] = str(reference[0])
    metadata[f"{prefix}_domain"] = reference[1]
    metadata[f"{prefix}_ref"] = f"{reference[0]}@{reference[1]}"


async def interaction_authorizing_owners(
    session: AsyncSession,
    interaction: BotInteraction,
    installation: InteractionInstallation | None = None,
) -> dict[str, str]:
    """Read the immutable owner snapshot, with a legacy-row fallback."""

    try:
        owners = stored_authorizing_integration_owners(interaction)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_LINEAGE_INVALID"},
        ) from exc
    if owners is not None:
        return owners
    if installation is not None:
        try:
            return installation_authorizing_integration_owners(installation)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            ) from exc
    if interaction.integration_type == "guild_install":
        if interaction.guild_id is None or interaction.guild_domain is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        return {GUILD_INSTALL_OWNER: f"{interaction.guild_id}@{interaction.guild_domain}"}
    stored_installation: BotUserInstallation | BotDMCapability | None
    if interaction.integration_type == "user_install":
        stored_installation = await session.get(
            BotUserInstallation,
            interaction.user_installation_id,
        )
    elif interaction.integration_type == "dm_capability":
        stored_installation = await session.get(BotDMCapability, interaction.dm_capability_id)
    else:
        stored_installation = None
    if stored_installation is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    try:
        return installation_authorizing_integration_owners(stored_installation)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_LINEAGE_INVALID"},
        ) from exc


async def interaction_message_metadata(
    session: AsyncSession,
    interaction: BotInteraction,
    *,
    followup: bool,
    depth: int = 0,
) -> dict[str, object]:
    """Build immutable Discord-style metadata from authority-owned lineage."""

    if depth > 2:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    invoker = await session.get(User, (interaction.user_id, interaction.user_domain))
    if invoker is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})
    interaction_ref = interaction.id, interaction.channel_domain
    application_ref = interaction.application_id, interaction.application_domain
    integration_type = interaction.integration_type
    metadata: dict[str, object] = {
        "id": str(interaction.id),
        "origin_domain": interaction.channel_domain,
        "interaction_ref": f"{interaction_ref[0]}@{interaction_ref[1]}",
        "type": interaction.interaction_type,
        "user": interaction_metadata_user(invoker),
        "user_ref": f"{invoker.id}@{invoker.origin_domain}",
        "application_ref": f"{application_ref[0]}@{application_ref[1]}",
        "integration_type": integration_type,
        "authorizing_integration_owners": await interaction_authorizing_owners(
            session,
            interaction,
        ),
    }
    stored_payload = interaction.payload if isinstance(interaction.payload, dict) else {}
    if interaction.interaction_type == "command":
        if interaction.command_name is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        metadata["command_name"] = interaction.command_name
        metadata["command_type"] = interaction.command_type
        raw_target = stored_payload.get("target_ref")
        if interaction.command_type == "user":
            try:
                target_ref = EntityRef(str(raw_target)).resolve(interaction.channel_domain)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "INTERACTION_TARGET_INVALID"},
                ) from exc
            target = await session.get(User, target_ref)
            if target is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "INTERACTION_TARGET_INVALID"},
                )
            metadata["target_user"] = interaction_metadata_user(target)
            metadata["target_user_ref"] = f"{target.id}@{target.origin_domain}"
        elif interaction.command_type == "message":
            try:
                target_ref = EntityRef(str(raw_target)).resolve(interaction.channel_domain)
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "INTERACTION_TARGET_INVALID"},
                ) from exc
            add_interaction_metadata_message_ref(metadata, "target_message", target_ref)
    elif interaction.interaction_type == "component":
        source_ref = (
            (interaction.message_id, interaction.message_domain)
            if interaction.message_id is not None and interaction.message_domain is not None
            else None
        )
        if source_ref is None:
            raw_response_id = stored_payload.get("response_id")
            if raw_response_id is not None and str(raw_response_id).isdigit():
                source_ref = int(str(raw_response_id)), interaction.channel_domain
        if source_ref is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        add_interaction_metadata_message_ref(metadata, "interacted_message", source_ref)
    elif interaction.interaction_type == "modal_submit":
        raw_parent_id = stored_payload.get("triggering_interaction_id")
        if raw_parent_id is None or not str(raw_parent_id).isdigit():
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        parent = await session.get(BotInteraction, int(str(raw_parent_id)))
        if parent is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        metadata["triggering_interaction_metadata"] = await interaction_message_metadata(
            session,
            parent,
            followup=False,
            depth=depth + 1,
        )
    else:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_LINEAGE_INVALID"})

    if followup:
        original = await session.scalar(
            select(BotInteractionResponse).where(
                BotInteractionResponse.interaction_id == interaction.id,
                BotInteractionResponse.sequence == 0,
                BotInteractionResponse.deleted_at.is_(None),
            )
        )
        if original is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"},
            )
        original_ref = (
            (original.message_id, original.message_domain)
            if original.message_id is not None and original.message_domain is not None
            else (original.id, interaction.channel_domain)
        )
        add_interaction_metadata_message_ref(
            metadata,
            "original_response_message",
            original_ref,
        )
    try:
        validated = validate_interaction_metadata(
            metadata,
            message_type=interaction_command_message_type(interaction) or 0,
            application_ref=application_ref,
            referenced_message_ref=(
                EntityRef(str(stored_payload["target_ref"])).resolve(interaction.channel_domain)
                if interaction.interaction_type == "command"
                and interaction.command_type == "message"
                and not followup
                else None
            ),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_LINEAGE_INVALID"},
        ) from exc
    if validated is None:
        raise RuntimeError("interaction metadata validation returned no projection")
    return validated


async def user_install_automod_attribution(
    session: AsyncSession,
    interaction: BotInteraction,
    installation: InteractionInstallation,
) -> tuple[User | None, int | None]:
    """Attribute a public user-installed app message to its installing user."""

    if not isinstance(installation, BotUserInstallation):
        return None, None
    actor = await session.get(User, (installation.user_id, installation.user_domain))
    if actor is None or actor.account_type != "human" or actor.disabled_at is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_INSTALLATION_INVALID"},
        )
    return actor, int(interaction.invocation_permissions or 0)


def require_interaction_installation_scope(
    installation: InteractionInstallation,
    interaction: BotInteraction,
    scope: str,
) -> None:
    try:
        admission = stored_installation_authority_lineage(interaction)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_LINEAGE_INVALID"},
        ) from exc
    granted_scopes = (
        admission.get("granted_scopes") if admission is not None else installation.granted_scopes
    )
    if not isinstance(granted_scopes, list) or scope not in granted_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_INSTALLATION_SCOPE_REQUIRED", "scope": scope},
        )


def validate_interaction_identity(payload: InteractionCreate) -> None:
    if payload.command_name is not None:
        valid_name = (
            valid_chat_input_name(payload.command_name)
            if payload.command_type == "chat_input"
            else valid_context_command_name(payload.command_name)
        )
        if not valid_name:
            raise ValueError("command_name does not match the command type")
    if payload.focused_option is None:
        return
    option_path = payload.focused_option.split(".")
    if not 1 <= len(option_path) <= 3 or not all(
        valid_chat_input_name(name) for name in option_path
    ):
        raise ValueError("focused_option is not a valid command option path")


def validate_command_interaction_shape(payload: InteractionCreate) -> None:
    if payload.command_name is None:
        raise ValueError("command_name is required for command interactions")
    if any(
        item is not None
        for item in (
            payload.message_ref,
            payload.response_id,
            payload.view_version,
            payload.custom_id,
        )
    ):
        raise ValueError("command interactions cannot include component identity")
    autocomplete = payload.interaction_type == "autocomplete"
    if autocomplete and payload.focused_option is None:
        raise ValueError("autocomplete interactions require focused_option")
    if autocomplete and payload.command_type != "chat_input":
        raise ValueError("autocomplete is available only for chat input commands")
    if not autocomplete and (
        payload.focused_option is not None or payload.autocomplete_generation is not None
    ):
        raise ValueError("command interactions cannot include autocomplete identity")
    context_command = payload.command_type in {"user", "message"}
    if not autocomplete and context_command != (payload.target_ref is not None):
        raise ValueError("user and message commands require exactly one target_ref")
    if context_command and payload.options:
        raise ValueError("context commands cannot include slash-command options")
    if payload.command_type == "chat_input" and payload.target_ref is not None:
        raise ValueError("chat input commands cannot include target_ref")
    if payload.values or payload.components:
        raise ValueError("command interactions cannot include component values")
    if (payload.command_id is None) != (payload.integration_type is None):
        raise ValueError("command identity and installation type must be supplied together")
    capability_bound = payload.dm_capability_id is not None
    if capability_bound != (payload.dm_capability_revision is not None):
        raise ValueError("DM capability identity and revision must be supplied together")
    if capability_bound != (payload.integration_type == "dm_capability"):
        raise ValueError("DM capability lineage requires the DM capability integration type")


def validate_component_interaction_shape(payload: InteractionCreate) -> None:
    if (payload.message_ref is None) == (payload.response_id is None):
        raise ValueError("component interactions require exactly one of message_ref or response_id")
    if payload.custom_id is None:
        raise ValueError("component interactions require custom_id")
    if payload.response_id is not None and payload.view_version is None:
        raise ValueError("ephemeral component interactions require view_version")
    if payload.command_name is not None:
        raise ValueError("component interactions cannot include command_name")
    if payload.components or payload.options:
        raise ValueError("component interactions cannot include modal or command data")


def validate_modal_interaction_shape(payload: InteractionCreate) -> None:
    invalid_identity = (
        payload.custom_id is None
        or payload.response_id is None
        or payload.command_name is not None
        or payload.message_ref is not None
        or payload.view_version is not None
        or payload.target_ref is not None
    )
    if invalid_identity:
        raise ValueError("modal submissions require the exact source response and custom_id")
    if (
        (not payload.components and payload.encrypted_payload is None)
        or payload.values
        or payload.options
    ):
        raise ValueError("modal submissions require only their submitted form fields")


def validate_noncommand_interaction_shape(payload: InteractionCreate) -> None:
    if payload.target_ref is not None:
        raise ValueError("component interactions cannot include command targets")
    if payload.command_type != "chat_input":
        raise ValueError("component interactions cannot include a command type")
    if payload.focused_option is not None or payload.autocomplete_generation is not None:
        raise ValueError("component interactions cannot include autocomplete identity")
    if payload.command_id is not None or payload.integration_type is not None:
        raise ValueError("component interactions inherit their installation identity")
    if payload.dm_capability_id is not None or payload.dm_capability_revision is not None:
        raise ValueError("component interactions inherit their DM capability identity")


class StrictInteractionModel(UnambiguousInputModel):
    pass


class InteractionCreate(StrictInteractionModel):
    application_ref: EntityRef
    interaction_type: Literal["command", "component", "modal_submit", "autocomplete"] = "command"
    command_name: str | None = Field(default=None, min_length=1, max_length=32)
    command_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    integration_type: Literal["guild_install", "user_install", "dm_capability"] | None = None
    dm_capability_id: str | None = Field(
        default=None,
        pattern=r"^kbdg_[A-Za-z0-9_-]{43}$",
    )
    dm_capability_revision: int | None = Field(default=None, gt=0, le=2**63 - 1)
    command_type: Literal["chat_input", "user", "message"] = "chat_input"
    target_ref: EntityRef | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    focused_option: str | None = Field(
        default=None,
        min_length=1,
        max_length=98,
    )
    autocomplete_generation: int | None = Field(default=None, gt=0, le=2**63 - 1)
    message_ref: EntityRef | None = None
    response_id: int | None = Field(default=None, gt=0)
    view_version: int | None = Field(default=None, ge=1)
    custom_id: str | None = Field(default=None, min_length=1, max_length=100)
    values: list[str] = Field(default_factory=list, max_length=25)
    components: list[dict[str, Any]] = Field(default_factory=list, max_length=5)
    # Encrypted invocations keep option/modal meaning inside MLS.  This bounded
    # capability list lets the channel authority finalize opaque ciphertext
    # uploads without learning which command field references each file.
    attachment_ids: list[Snowflake] = Field(default_factory=list, max_length=10)
    encrypted_payload: dict[str, Any] | None = None

    @field_validator("options")
    @classmethod
    def bounded_options(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 25:
            raise ValueError("command options may contain at most 25 values")
        validate_json_tree(
            value,
            limits=INTERACTION_OPTION_LIMITS,
            label="command options",
            allow_floats=True,
        )
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > 64 * 1024:
            raise ValueError("command options are too large")
        return value

    @field_validator("encrypted_payload")
    @classmethod
    def valid_encrypted_payload(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return validate_e2ee_envelope(value)

    @field_validator("attachment_ids", mode="before")
    @classmethod
    def valid_attachment_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("attachment IDs must be a list")
        normalized: list[int] = []
        for item in value:
            if isinstance(item, bool):
                raise ValueError("attachment IDs must be positive database-range snowflakes")
            if isinstance(item, int):
                parsed = item
            elif (
                isinstance(item, str)
                and item.isascii()
                and item.isdecimal()
                and not item.startswith("0")
            ):
                parsed = int(item)
            else:
                raise ValueError("attachment IDs must be positive database-range snowflakes")
            if not 0 < parsed <= (1 << 63) - 1:
                raise ValueError("attachment IDs must be positive database-range snowflakes")
            normalized.append(parsed)
        return normalized

    @model_validator(mode="after")
    def valid_interaction_shape(self) -> InteractionCreate:
        validate_interaction_identity(self)
        if self.interaction_type in {"command", "autocomplete"}:
            validate_command_interaction_shape(self)
        elif self.interaction_type == "component":
            validate_component_interaction_shape(self)
            validate_noncommand_interaction_shape(self)
        else:
            validate_modal_interaction_shape(self)
            validate_noncommand_interaction_shape(self)
        validate_json_tree(
            {"values": self.values, "components": self.components},
            limits=INTERACTION_OPTION_LIMITS,
            label="interaction data",
            allow_floats=True,
        )
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("attachment IDs must be unique")
        if self.encrypted_payload is None:
            if self.attachment_ids:
                raise ValueError("opaque attachment IDs require an encrypted interaction")
        elif self.options or self.values or self.components:
            raise ValueError("encrypted interactions cannot include plaintext option data")
        return self


class FederatedInteractionAttachment(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: str = Field(min_length=32, max_length=64)
    metadata_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    attachment: dict[str, object]

    @field_validator("attachment")
    @classmethod
    def bounded_attachment(cls, value: dict[str, object]) -> dict[str, object]:
        validate_json_tree(
            value,
            limits=INTERACTION_OPTION_LIMITS,
            label="federated interaction attachment",
            allow_floats=True,
        )
        return value


class FederatedInteractionCreate(UnambiguousInputModel):
    user_id: str = Field(pattern=r"^[0-9]{1,20}$")
    interaction: InteractionCreate
    response_grant_id: str = Field(min_length=32, max_length=64)
    response_expires_at: datetime
    attachments: list[FederatedInteractionAttachment] = Field(default_factory=list, max_length=10)
    user_installation: FederatedUserInstallationGrant | None = None
    locale: str = Field(
        default="en-US",
        min_length=2,
        max_length=16,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$",
    )
    age_assured_adult: bool = False
    age_restricted_dm_commands_enabled: bool = False

    @model_validator(mode="after")
    def unique_attachments(self) -> FederatedInteractionCreate:
        grants = [item.grant_id for item in self.attachments]
        refs = [
            (
                str(item.attachment.get("id", "")),
                str(item.attachment.get("origin_domain", "")),
            )
            for item in self.attachments
        ]
        if len(grants) != len(set(grants)) or len(refs) != len(set(refs)):
            raise ValueError("federated interaction attachments must be unique")
        return self


class InteractionMessageCreate(MessageCreate):
    """Discord interaction message body with a write-only mention policy."""

    allowed_mentions: AllowedMentions | None = None


class InteractionResponse(UnambiguousInputModel):
    message: InteractionMessageCreate

    @field_validator("message", mode="before")
    @classmethod
    def accept_message_create(cls, value: object) -> object:
        """Keep the internal MessageCreate construction API source-compatible."""

        if isinstance(value, MessageCreate) and not isinstance(value, InteractionMessageCreate):
            # Revalidate through the same wire contract. WireSnowflake fields
            # are integers in a constructed MessageCreate but serialize back
            # to canonical decimal strings before the derived model parses.
            return value.model_dump(mode="json", exclude_unset=True)
        return value


class InteractionCallback(StrictInteractionModel):
    type: Literal[1, 4, 5, 6, 7, 8, 9, 10]
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data")
    @classmethod
    def bounded_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_json_tree(
            value,
            limits=INTERACTION_OPTION_LIMITS,
            label="interaction callback",
            allow_floats=True,
        )
        return value


class InteractionResponseEdit(StrictInteractionModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=4000)
    e2ee: dict[str, object] | None = None
    embeds: list[Embed] | None = Field(default=None, max_length=10)
    components: list[MessageLayoutComponent] | None = Field(default=None, max_length=40)
    flags: int | None = Field(default=None, ge=0, le=2_147_483_647)
    attachment_ids: list[int] | None = Field(default=None, max_length=10)
    allowed_mentions: AllowedMentions | None = None
    # Discord permits a poll to be created while materializing a deferred
    # original response. Polls remain immutable after that first message edit.
    poll: PollCreate | None = None
    view_version: int | None = Field(default=None, ge=1)
    view_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    view_persistent: bool | None = None

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str | None) -> str | None:
        return meaningful_optional_content(value)

    @field_validator("e2ee")
    @classmethod
    def valid_e2ee(cls, value: object) -> dict[str, object] | None:
        return validate_e2ee_envelope(value)

    @model_validator(mode="after")
    def valid_edit(self) -> InteractionResponseEdit:
        if self.e2ee is not None and self.model_fields_set - {
            "e2ee",
            "attachment_ids",
            "flags",
        }:
            raise ValueError("encrypted edits cannot contain rich plaintext fields")
        view_fields = {"view_version", "view_timeout_seconds", "view_persistent"}
        if self.model_fields_set & view_fields and "components" not in self.model_fields_set:
            raise ValueError("view options require a components edit")
        if self.view_persistent is True and self.view_timeout_seconds is not None:
            raise ValueError("a persistent view cannot have a timeout")
        if self.attachment_ids is not None and len(self.attachment_ids) != len(
            set(self.attachment_ids)
        ):
            raise ValueError("attachment IDs must be unique")
        if self.flags is not None and self.flags & ~PUBLIC_MESSAGE_EDIT_FLAGS:
            raise ValueError("interaction response flags contain unsupported bits")
        if self.components is not None:
            validate_message_components(self.components)
            if uses_components_v2(cast(list[object], self.components)) and (
                self.content is not None or bool(self.embeds) or self.poll is not None
            ):
                raise ValueError("Components V2 responses cannot include content, embeds, or polls")
        if "poll" in self.model_fields_set and self.poll is None:
            raise ValueError("poll cannot be null once supplied")
        return self


class InteractionFollowup(UnambiguousInputModel):
    message: InteractionMessageCreate
    ephemeral: bool = False


def deferred_followup_edit(payload: InteractionFollowup) -> InteractionResponseEdit:
    """Translate Discord's deprecated first-follow-up defer compatibility path."""

    message = payload.message
    if (
        message.tts
        or message.voice_message
        or message.sticker_ids
        or message.forwarded_message_id is not None
        or message.forward_source_proof is not None
        or message.forward_snapshot is not None
        or message.client_nonce is not None
        or message.referenced_message_id is not None
        or message.mention_user_ids
        or message.flags & ~PUBLIC_MESSAGE_EDIT_FLAGS
    ):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "DEFERRED_FOLLOWUP_EDIT_INVALID",
                "message": "The first follow-up after a defer accepts original-edit fields only.",
            },
        )
    editable_fields = {
        "content",
        "e2ee",
        "embeds",
        "components",
        "flags",
        "attachment_ids",
        "allowed_mentions",
        "poll",
        "view_timeout_seconds",
        "view_persistent",
    }
    body = message.model_dump(
        mode="json",
        include=editable_fields,
        exclude_unset=True,
    )
    try:
        return InteractionResponseEdit.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "DEFERRED_FOLLOWUP_EDIT_INVALID"},
        ) from exc


class InteractionDefer(UnambiguousInputModel):
    ephemeral: bool = False


class UserInstallationCreate(UnambiguousInputModel):
    application_ref: EntityRef
    scopes: list[str] = Field(
        default_factory=lambda: ["applications.commands", "interactions.respond"],
        min_length=1,
        max_length=4,
    )
    contexts: list[str] = Field(
        default_factory=lambda: ["guild", "bot_dm", "private_channel"],
        min_length=1,
        max_length=3,
    )
    intents: list[str] = Field(default_factory=lambda: ["interactions"], max_length=32)

    @model_validator(mode="after")
    def valid_grant(self) -> UserInstallationCreate:
        self.scopes = list(dict.fromkeys(self.scopes))
        self.contexts = list(dict.fromkeys(self.contexts))
        self.intents = list(dict.fromkeys(self.intents))
        if not set(self.scopes) <= USER_INSTALL_SCOPES:
            raise ValueError("unsupported user-install scope")
        if not set(self.contexts) <= USER_INSTALL_CONTEXTS:
            raise ValueError("unsupported user-install context")
        if not set(self.scopes) >= REQUIRED_USER_INSTALL_SCOPES:
            raise ValueError(
                "user installations require applications.commands and interactions.respond"
            )
        if not set(self.intents) <= SUPPORTED_BOT_INTENTS:
            raise ValueError("unsupported user-install intent")
        if "interactions" not in self.intents:
            raise ValueError("user installations require interactions intent")
        return self


class UserInstallationPatch(UnambiguousInputModel):
    scopes: list[str] | None = Field(default=None, min_length=1, max_length=4)
    contexts: list[str] | None = Field(default=None, min_length=1, max_length=3)
    intents: list[str] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def valid_grant(self) -> UserInstallationPatch:
        if not self.model_fields_set:
            raise ValueError("at least one user-install grant field is required")
        if self.scopes is not None:
            self.scopes = list(dict.fromkeys(self.scopes))
            if not set(self.scopes) <= USER_INSTALL_SCOPES:
                raise ValueError("unsupported user-install scope")
            if not set(self.scopes) >= REQUIRED_USER_INSTALL_SCOPES:
                raise ValueError(
                    "user installations require applications.commands and interactions.respond"
                )
        if self.contexts is not None:
            self.contexts = list(dict.fromkeys(self.contexts))
            if not set(self.contexts) <= USER_INSTALL_CONTEXTS:
                raise ValueError("unsupported user-install context")
        if self.intents is not None:
            self.intents = list(dict.fromkeys(self.intents))
            if not set(self.intents) <= SUPPORTED_BOT_INTENTS:
                raise ValueError("unsupported user-install intent")
            if "interactions" not in self.intents:
                raise ValueError("user installations require interactions intent")
        return self


def command_payload(
    command: ApplicationCommand,
    application: BotApplication,
    *,
    integration_type: Literal["guild_install", "user_install", "dm_capability"],
    interaction_context: Literal["guild", "bot_dm", "private_channel"],
    dm_capability: BotDMCapability | None = None,
) -> dict[str, object]:
    return {
        "id": str(command.authority_id),
        "origin_domain": command.source_domain or command.application_domain,
        "ref": f"{command.authority_id}@{command.source_domain or command.application_domain}",
        "application_ref": f"{application.id}@{application.origin_domain}",
        "application_name": application.name,
        # These are the effective installation and channel context selected by
        # discovery, not merely the command's advertised capabilities. Clients
        # bind encrypted invocations to these authority-selected values.
        "integration_type": integration_type,
        "dm_capability_id": (dm_capability.grant_id if dm_capability is not None else None),
        "dm_capability_revision": (
            str(dm_capability.revision) if dm_capability is not None else None
        ),
        "interaction_context": interaction_context,
        "guild_ref": (
            f"{command.guild_id}@{command.guild_domain}"
            if command.guild_id is not None and command.guild_domain is not None
            else None
        ),
        "_local_command_id": command.id,
        **command.definition,
    }


def public_command_payload(command: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in command.items() if not key.startswith("_")}


def command_context_available(
    command: dict[str, object],
    permissions: int,
    *,
    channel_type: int | None,
    channel_nsfw: bool,
    age_assured_adult: bool,
) -> bool:
    if command.get("nsfw") is True and (
        channel_type is None or not channel_nsfw or not age_assured_adult
    ):
        return False
    command_type = str(command.get("type", "chat_input"))
    if command_type == "user" and channel_type is None:
        return False
    return application_interaction_allowed(
        permissions,
        guild_installed=command.get("integration_type") == "guild_install",
        command_type=command_type,
        channel_type=channel_type or 0,
    )


def command_default_permission_allowed(command: dict[str, object], permissions: int) -> bool:
    effective = Permission(permissions)
    if command.get("default_member_permissions") == "0":
        return bool(effective & Permission.ADMINISTRATOR)
    required = command_permission_mask(command)
    return effective & required == required


def filter_commands_for_permissions(
    commands: list[dict[str, object]],
    permissions: int,
    *,
    channel_type: int | None = None,
    channel_nsfw: bool = False,
    age_assured_adult: bool = False,
) -> list[dict[str, object]]:
    """Hide commands unavailable in the exact guild-channel context."""

    available: list[dict[str, object]] = []
    for command in commands:
        if not command_context_available(
            command,
            permissions,
            channel_type=channel_type,
            channel_nsfw=channel_nsfw,
            age_assured_adult=age_assured_adult,
        ):
            continue
        if command_default_permission_allowed(command, permissions):
            available.append(public_command_payload(command))
    return available


async def filter_guild_commands_for_permissions(
    session: AsyncSession,
    guild: Guild,
    user: User,
    commands: list[dict[str, object]],
    permissions: int,
    *,
    channel: Channel | None = None,
    channel_nsfw: bool = False,
    age_assured_adult: bool = False,
) -> list[dict[str, object]]:
    """Apply defaults and granular overwrites in one authority-side pass."""

    command_ids = {
        value
        for command in commands
        if isinstance((value := command.get("_local_command_id")), int)
    }
    application_refs: set[tuple[int, str]] = set()
    for command in commands:
        raw_ref = command.get("application_ref")
        if isinstance(raw_ref, str):
            try:
                application_refs.add(EntityRef(raw_ref).resolve(guild.origin_domain))
            except ValueError:
                continue
    rows = await guild_permission_rows(session, guild, application_refs, command_ids)
    subject = permission_subject(
        guild,
        user,
        await guild_member_role_refs(session, guild, user),
        channel,
    )
    available: list[dict[str, object]] = []
    for command in commands:
        if not command_context_available(
            command,
            permissions,
            channel_type=channel.type if channel is not None else None,
            channel_nsfw=channel_nsfw,
            age_assured_adult=age_assured_adult,
        ):
            continue
        local_command_id = command.get("_local_command_id")
        raw_application_ref = command.get("application_ref")
        if not isinstance(local_command_id, int) or not isinstance(raw_application_ref, str):
            continue
        try:
            application_ref = EntityRef(raw_application_ref).resolve(guild.origin_domain)
        except ValueError:
            continue
        effective_rows = select_effective_rows(
            rows,
            command_id=local_command_id,
            application_ref=application_ref,
        )
        if command_permission_allowed(
            command,
            permissions,
            effective_rows,
            subject,
            guild,
        ):
            available.append(public_command_payload(command))
    return available


def command_supports_focused_option(command: ApplicationCommand, path: str) -> bool:
    options = command.definition.get("options", [])
    for index, segment in enumerate(path.split(".")):
        if not isinstance(options, list):
            return False
        option = next(
            (item for item in options if isinstance(item, dict) and item.get("name") == segment),
            None,
        )
        if option is None:
            return False
        if index == len(path.split(".")) - 1:
            return option.get("autocomplete") is True and option.get("type") in {
                "string",
                "integer",
                "number",
            }
        options = option.get("options", [])
    return False


async def guild_install_command(
    session: AsyncSession,
    guild: Guild,
    *,
    application_ref: tuple[int, str],
    name: str,
    command_type: str,
    command_id: int | None = None,
) -> ApplicationCommand | None:
    """Resolve the exact guild command, preferring its guild-scoped override."""

    return cast(
        ApplicationCommand | None,
        await session.scalar(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application_ref[0],
                ApplicationCommand.application_domain == application_ref[1],
                ApplicationCommand.name == name,
                ApplicationCommand.type == command_type,
                ApplicationCommand.state == "active",
                ApplicationCommand.contexts.contains(["guild"]),
                ApplicationCommand.integration_types.contains(["guild_install"]),
                *(
                    [
                        or_(
                            (
                                (ApplicationCommand.source_id == command_id)
                                & (ApplicationCommand.source_domain == application_ref[1])
                            ),
                            (
                                (ApplicationCommand.id == command_id)
                                & ApplicationCommand.source_id.is_(None)
                                & (ApplicationCommand.application_domain == application_ref[1])
                            ),
                        )
                    ]
                    if command_id is not None
                    else []
                ),
                (
                    ApplicationCommand.guild_id.is_(None)
                    | (
                        (ApplicationCommand.guild_id == guild.id)
                        & (ApplicationCommand.guild_domain == guild.origin_domain)
                    )
                ),
            )
            .order_by(ApplicationCommand.guild_id.desc().nullslast())
            .limit(1)
        ),
    )


async def require_guild_command_permission(
    session: AsyncSession,
    guild: Guild,
    user: User,
    channel: Channel,
    command: ApplicationCommand,
    effective_permissions: int,
) -> None:
    rows = await guild_permission_rows(
        session,
        guild,
        {(command.application_id, command.application_domain)},
        {command.id},
    )
    subject = permission_subject(
        guild,
        user,
        await guild_member_role_refs(session, guild, user),
        channel,
    )
    effective_rows = select_effective_rows(
        rows,
        command_id=command.id,
        application_ref=(command.application_id, command.application_domain),
    )
    if command_permission_allowed(
        command.definition,
        effective_permissions,
        effective_rows,
        subject,
        guild,
    ):
        return
    raise HTTPException(
        status_code=403,
        detail={"code": "APPLICATION_COMMAND_PERMISSION_DENIED"},
    )


def command_authority_ref(command: ApplicationCommand) -> tuple[int, str]:
    return (
        command.authority_id,
        command.source_domain or command.application_domain,
    )


async def installed_guild_application(
    session: AsyncSession,
    guild: Guild,
    application_ref: tuple[int, str],
) -> BotApplication:
    application = await session.scalar(
        select(BotApplication)
        .join(
            BotInstallation,
            (BotInstallation.application_id == BotApplication.id)
            & (BotInstallation.application_domain == BotApplication.origin_domain),
        )
        .where(
            BotApplication.id == application_ref[0],
            BotApplication.origin_domain == application_ref[1],
            BotApplication.status == "active",
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.status == "active",
            BotInstallation.revoked_at.is_(None),
            installation_has_membership(),
            BotInstallation.granted_scopes.contains(["applications.commands"]),
        )
    )
    if application is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return application


async def permission_scope_command(
    session: AsyncSession,
    guild: Guild,
    application: BotApplication,
    scope_ref: tuple[int, str],
) -> ApplicationCommand | None:
    if scope_ref == (application.id, application.origin_domain):
        return None
    if scope_ref[1] != application.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    command = await session.scalar(
        select(ApplicationCommand).where(
            ApplicationCommand.application_id == application.id,
            ApplicationCommand.application_domain == application.origin_domain,
            ApplicationCommand.state == "active",
            (
                ApplicationCommand.guild_id.is_(None)
                | (
                    (ApplicationCommand.guild_id == guild.id)
                    & (ApplicationCommand.guild_domain == guild.origin_domain)
                )
            ),
            (
                (
                    (ApplicationCommand.source_id == scope_ref[0])
                    & (ApplicationCommand.source_domain == scope_ref[1])
                )
                | (
                    (ApplicationCommand.id == scope_ref[0])
                    & ApplicationCommand.source_id.is_(None)
                    & (ApplicationCommand.application_domain == scope_ref[1])
                )
            ),
        )
    )
    if command is None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    return command


def command_permission_entry_payload(
    row: ApplicationCommandPermission,
) -> dict[str, object]:
    return {
        "id": f"{row.target_id}@{row.target_domain}",
        "type": row.target_type,
        "permission": row.permission,
    }


class FederatedCommandPermissionScope(UnambiguousInputModel):
    """Strict guild-authority response for one application-command permission scope."""

    model_config = ConfigDict(extra="forbid")

    id: EntityRef
    application_id: SnowflakeString
    application_domain: FederationDomain
    application_ref: EntityRef
    application_name: str = Field(min_length=1, max_length=100)
    guild_id: SnowflakeString
    guild_domain: FederationDomain
    guild_ref: EntityRef
    command: dict[str, object] | None
    command_ref: EntityRef | None
    synced: bool
    permissions: list[CommandPermissionEntry] = Field(max_length=100)

    @model_validator(mode="after")
    def coherent_scope(self) -> FederatedCommandPermissionScope:
        application_ref = self.application_ref.resolve(self.application_domain)
        guild_ref = self.guild_ref.resolve(self.guild_domain)
        if (
            self.id.domain is None
            or self.application_ref.domain is None
            or self.guild_ref.domain is None
            or int(self.application_id) <= 0
            or int(self.guild_id) <= 0
            or application_ref != (int(self.application_id), self.application_domain)
            or guild_ref != (int(self.guild_id), self.guild_domain)
        ):
            raise ValueError("federated command permission scope identity is incoherent")

        permission_keys = [(entry.type, str(entry.id)) for entry in self.permissions]
        if any(entry.id.domain is None for entry in self.permissions):
            raise ValueError("federated command permission targets must be qualified")
        if len(permission_keys) != len(set(permission_keys)):
            raise ValueError("federated command permission targets must be unique")
        if any(
            entry.type in {"role", "channel"} and entry.id.domain != self.guild_domain
            for entry in self.permissions
        ):
            raise ValueError("guild command permission targets must use the guild authority")

        if self.command is None:
            if self.command_ref is not None or self.id != self.application_ref or self.synced:
                raise ValueError("application command permission scope is incoherent")
            return self

        expected_command_keys = {
            "id",
            "origin_domain",
            "ref",
            "name",
            "type",
            "guild_ref",
        }
        if set(self.command) != expected_command_keys or self.command_ref is None:
            raise ValueError("federated command permission command is malformed")
        command_id = self.command.get("id")
        command_domain = self.command.get("origin_domain")
        command_ref = self.command.get("ref")
        command_name = self.command.get("name")
        command_type = self.command.get("type")
        command_guild_ref = self.command.get("guild_ref")
        if (
            not isinstance(command_id, str)
            or not command_id.isascii()
            or not command_id.isdecimal()
            or (len(command_id) > 1 and command_id.startswith("0"))
            or int(command_id) <= 0
            or not isinstance(command_domain, str)
            or not isinstance(command_ref, str)
            or not isinstance(command_name, str)
            or not 1 <= len(command_name) <= 32
            or command_type not in {"chat_input", "user", "message"}
        ):
            raise ValueError("federated command permission command is malformed")
        try:
            parsed_command_ref = EntityRef(command_ref)
            parsed_command_guild_ref = (
                EntityRef(command_guild_ref) if isinstance(command_guild_ref, str) else None
            )
        except ValueError:
            raise ValueError("federated command permission command is malformed") from None
        if (
            parsed_command_ref.domain is None
            or parsed_command_ref != self.command_ref
            or parsed_command_ref != self.id
            or parsed_command_ref.id != int(command_id)
            or parsed_command_ref.domain != command_domain
            or command_domain != self.application_domain
            or (parsed_command_guild_ref is not None and parsed_command_guild_ref != self.guild_ref)
            or (command_guild_ref is not None and parsed_command_guild_ref is None)
        ):
            raise ValueError("federated command permission command identity is incoherent")
        return self


def invalid_remote_command_permissions() -> NoReturn:
    raise HTTPException(
        status_code=502,
        detail={"code": "REMOTE_COMMAND_PERMISSIONS_INVALID"},
    )


def validate_remote_command_permissions(
    raw: object,
    *,
    application_ref: tuple[int, str],
    guild_ref: tuple[int, str],
    command_ref: tuple[int, str] | None,
    expected_permissions: list[dict[str, object]] | None = None,
) -> list[dict[str, object]] | dict[str, object]:
    """Validate and bind one signed permission response to its exact request."""

    list_response = command_ref is None
    if list_response:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 131:
            invalid_remote_command_permissions()
        raw_scopes = raw
    else:
        if not isinstance(raw, dict):
            invalid_remote_command_permissions()
        raw_scopes = [raw]
    try:
        scopes = [FederatedCommandPermissionScope.model_validate(item) for item in raw_scopes]
    except (TypeError, ValueError, ValidationError):
        invalid_remote_command_permissions()

    expected_application_ref = f"{application_ref[0]}@{application_ref[1]}"
    expected_guild_ref = f"{guild_ref[0]}@{guild_ref[1]}"
    if any(
        str(scope.application_ref) != expected_application_ref
        or str(scope.guild_ref) != expected_guild_ref
        for scope in scopes
    ):
        invalid_remote_command_permissions()

    scope_refs = [str(scope.id) for scope in scopes]
    if len(scope_refs) != len(set(scope_refs)):
        invalid_remote_command_permissions()
    if list_response:
        application_scopes = [
            scope
            for scope in scopes
            if scope.command is None and str(scope.id) == expected_application_ref
        ]
        if len(application_scopes) != 1:
            invalid_remote_command_permissions()
    else:
        if command_ref is None:
            invalid_remote_command_permissions()
        expected_command_ref = f"{command_ref[0]}@{command_ref[1]}"
        if str(scopes[0].id) != expected_command_ref:
            invalid_remote_command_permissions()

    if expected_permissions is not None:
        returned = [
            (entry.type, str(entry.id), entry.permission) for entry in scopes[0].permissions
        ]
        expected: list[tuple[object, object, object]] = [
            (entry.get("type"), entry.get("id"), entry.get("permission"))
            for entry in expected_permissions
        ]
        if len(returned) != len(expected) or set(returned) != set(expected):
            invalid_remote_command_permissions()

    rendered = [scope.model_dump(mode="json") for scope in scopes]
    return rendered if list_response else rendered[0]


async def command_permission_scope_payload(
    session: AsyncSession,
    guild: Guild,
    application: BotApplication,
    command: ApplicationCommand | None,
) -> dict[str, object]:
    explicit_rows = list(
        await session.scalars(
            select(ApplicationCommandPermission)
            .where(
                ApplicationCommandPermission.application_id == application.id,
                ApplicationCommandPermission.application_domain == application.origin_domain,
                ApplicationCommandPermission.guild_id == guild.id,
                ApplicationCommandPermission.guild_domain == guild.origin_domain,
                (
                    ApplicationCommandPermission.command_id == command.id
                    if command is not None
                    else ApplicationCommandPermission.command_id.is_(None)
                ),
            )
            .order_by(
                ApplicationCommandPermission.target_type,
                ApplicationCommandPermission.target_domain,
                ApplicationCommandPermission.target_id,
            )
        )
    )
    synced = command is not None and not explicit_rows
    rows = explicit_rows
    if synced:
        rows = list(
            await session.scalars(
                select(ApplicationCommandPermission)
                .where(
                    ApplicationCommandPermission.application_id == application.id,
                    ApplicationCommandPermission.application_domain == application.origin_domain,
                    ApplicationCommandPermission.guild_id == guild.id,
                    ApplicationCommandPermission.guild_domain == guild.origin_domain,
                    ApplicationCommandPermission.command_id.is_(None),
                )
                .order_by(
                    ApplicationCommandPermission.target_type,
                    ApplicationCommandPermission.target_domain,
                    ApplicationCommandPermission.target_id,
                )
            )
        )
    scope_ref = (
        command_authority_ref(command)
        if command is not None
        else (application.id, application.origin_domain)
    )
    return {
        "id": f"{scope_ref[0]}@{scope_ref[1]}",
        "application_id": str(application.id),
        "application_domain": application.origin_domain,
        "application_ref": f"{application.id}@{application.origin_domain}",
        "application_name": application.name,
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "guild_ref": f"{guild.id}@{guild.origin_domain}",
        "command": (
            {
                "id": str(command.authority_id),
                "origin_domain": command.source_domain or command.application_domain,
                "ref": f"{scope_ref[0]}@{scope_ref[1]}",
                "name": command.name,
                "type": command.type,
                "guild_ref": (
                    f"{command.guild_id}@{command.guild_domain}"
                    if command.guild_id is not None and command.guild_domain is not None
                    else None
                ),
            }
            if command is not None
            else None
        ),
        "command_ref": (f"{scope_ref[0]}@{scope_ref[1]}" if command is not None else None),
        "synced": synced,
        "permissions": [command_permission_entry_payload(row) for row in rows],
    }


async def require_command_permission_manager(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    command: ApplicationCommand | None,
) -> None:
    effective = await get_permissions(session, redis, guild, actor)
    required = Permission.MANAGE_GUILD | Permission.MANAGE_ROLES
    if effective & required != required:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    if command is None:
        return
    rows = await guild_permission_rows(
        session,
        guild,
        {(command.application_id, command.application_domain)},
        {command.id},
    )
    subject = permission_subject(
        guild,
        actor,
        await guild_member_role_refs(session, guild, actor),
        None,
    )
    if not command_permission_allowed(
        command.definition,
        int(effective),
        select_effective_rows(
            rows,
            command_id=command.id,
            application_ref=(command.application_id, command.application_domain),
        ),
        subject,
        guild,
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_COMMAND_PERMISSION_DENIED"},
        )


async def require_manageable_permission_user(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    target_ref: tuple[int, str],
) -> None:
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, target_ref[0], target_ref[1]),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "MEMBER_NOT_FOUND"})
    actor_ref = (actor.id, actor.origin_domain)
    owner_ref = (guild.owner_id, guild.owner_domain)
    if target_ref == owner_ref:
        raise HTTPException(status_code=403, detail={"code": "OWNER_IMMUNE"})
    if target_ref == actor_ref:
        raise HTTPException(status_code=403, detail={"code": "CANNOT_MANAGE_SELF"})
    if actor_ref == owner_ref:
        return
    actor_role = await highest_role(session, guild, *actor_ref)
    target_role = await highest_role(session, guild, *target_ref)
    if role_rank(actor_role) <= role_rank(target_role):
        raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})


async def normalized_permission_role(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    target_ref: tuple[int, str],
) -> tuple[int, str]:
    if target_ref[1] != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    role = await session.get(Role, target_ref)
    if role is None or (role.guild_id, role.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    await require_can_manage_role(session, guild, actor, role)
    return target_ref


async def normalized_permission_channel(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    target_ref: tuple[int, str],
) -> tuple[int, str]:
    if target_ref == (guild.id - 1, guild.origin_domain):
        permissions = await get_permissions(session, redis, guild, actor)
        if not permissions & Permission.MANAGE_CHANNELS:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
        return target_ref
    target = await session.get(Channel, target_ref)
    if target is None or (target.guild_id, target.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if target.type in {10, 11, 12}:
        if target.parent_id is None or target.parent_domain is None:
            raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
        target_ref = (target.parent_id, target.parent_domain)
        target = await session.get(Channel, target_ref)
        if target is None:
            raise HTTPException(status_code=409, detail={"code": "CHANNEL_PARENT_INVALID"})
    target_permissions = await get_permissions(
        session,
        redis,
        guild,
        actor,
        channel=target,
    )
    if not target_permissions & Permission.MANAGE_CHANNELS:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    return target_ref


async def normalized_command_permission_entries(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    entries: list[CommandPermissionEntry],
) -> list[tuple[str, int, str, bool]]:
    normalized: list[tuple[str, int, str, bool]] = []
    for entry in entries:
        target_ref = entry.id.resolve(guild.origin_domain)
        if entry.type == "role":
            target_ref = await normalized_permission_role(session, guild, actor, target_ref)
        elif entry.type == "user":
            await require_manageable_permission_user(session, guild, actor, target_ref)
        else:
            target_ref = await normalized_permission_channel(
                session,
                redis,
                guild,
                actor,
                target_ref,
            )
        normalized.append((entry.type, *target_ref, entry.permission))
    keys = [(kind, target_id, target_domain) for kind, target_id, target_domain, _ in normalized]
    if len(keys) != len(set(keys)):
        raise HTTPException(
            status_code=422,
            detail={"code": "APPLICATION_COMMAND_PERMISSION_TARGET_DUPLICATE"},
        )
    return normalized


def invalid_command_option(path: tuple[str, ...], message: str) -> NoReturn:
    raise HTTPException(
        status_code=422,
        detail={
            "code": "COMMAND_OPTION_INVALID",
            "option": ".".join(path) or "options",
            "message": message,
        },
    )


def validate_string_command_option(
    definition: dict[str, Any],
    value: object,
    path: tuple[str, ...],
) -> str:
    if not isinstance(value, str):
        invalid_command_option(path, "Enter a text value.")
    minimum = int(definition.get("min_length") or 0)
    maximum = int(definition.get("max_length") or 6000)
    if not minimum <= len(value) <= maximum:
        invalid_command_option(
            path,
            f"Text must contain between {minimum} and {maximum} characters.",
        )
    return value


def validate_entity_command_option(value: object, path: tuple[str, ...]) -> str:
    if not isinstance(value, str):
        invalid_command_option(path, "Choose a valid Kaede entity.")
    try:
        EntityRef(value)
    except (TypeError, ValueError):
        invalid_command_option(path, "Choose a valid Kaede entity.")
    return value


def validate_command_option_type(
    definition: dict[str, Any],
    value: object,
    path: tuple[str, ...],
) -> Any:
    option_type = definition.get("type")
    if option_type == "string":
        return validate_string_command_option(definition, value, path)
    if option_type in {"integer", "number"}:
        integer = option_type == "integer"
        if not valid_numeric_command_value(value, integer=integer):
            invalid_command_option(
                path, "Enter a whole number." if integer else "Enter a finite number."
            )
        return value
    if option_type == "boolean":
        if not isinstance(value, bool):
            invalid_command_option(path, "Choose true or false.")
        return value
    if option_type in {"user", "channel", "role", "mentionable"}:
        return validate_entity_command_option(value, path)
    if option_type == "attachment":
        if isinstance(value, bool) or not str(value).isdigit():
            invalid_command_option(path, "Choose an uploaded file.")
        return str(value)
    invalid_command_option(path, "The command has an unsupported option type.")


def validate_command_option_bounds(
    definition: dict[str, Any],
    value: object,
    path: tuple[str, ...],
) -> None:
    if definition.get("type") not in {"integer", "number"}:
        return
    minimum = definition.get("min_value")
    maximum = definition.get("max_value")
    if minimum is not None and float(cast(int | float, value)) < float(minimum):
        invalid_command_option(path, f"The value must be at least {minimum}.")
    if maximum is not None and float(cast(int | float, value)) > float(maximum):
        invalid_command_option(path, f"The value must be at most {maximum}.")


def command_choice_matches(choice: object, value: object, option_type: object) -> bool:
    if not isinstance(choice, dict) or choice.get("value") != value:
        return False
    choice_value = choice.get("value")
    if option_type == "number":
        return not isinstance(choice_value, bool) and isinstance(choice_value, (int, float))
    return type(choice_value) is type(value)


def validate_command_option_choices(
    definition: dict[str, Any],
    value: object,
    path: tuple[str, ...],
) -> None:
    choices = definition.get("choices", [])
    if (
        isinstance(choices, list)
        and choices
        and not any(
            command_choice_matches(choice, value, definition.get("type")) for choice in choices
        )
    ):
        invalid_command_option(path, "Choose one of the command's allowed values.")


def normalize_leaf_command_options(
    declared: dict[str, dict[str, Any]],
    supplied: dict[str, Any],
    *,
    path: tuple[str, ...],
    require_complete: bool,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, definition in declared.items():
        option_path = (*path, name)
        if name not in supplied:
            if require_complete and definition.get("required") is True:
                invalid_command_option(option_path, "This required option is missing.")
            continue
        value = validate_command_option_type(definition, supplied[name], option_path)
        validate_command_option_bounds(definition, value, option_path)
        validate_command_option_choices(definition, value, option_path)
        normalized[name] = value
    return normalized


def validate_command_option_level(
    definitions: object,
    supplied: object,
    *,
    path: tuple[str, ...],
    require_complete: bool,
) -> dict[str, Any]:
    if not isinstance(definitions, list) or not isinstance(supplied, dict):
        invalid_command_option(path, "Command options must be an object.")
    declared = {
        str(item["name"]): item
        for item in definitions
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    unknown = sorted(set(supplied) - set(declared))
    if unknown:
        invalid_command_option((*path, unknown[0]), "This option is not declared by the command.")
    containers = {
        name: item
        for name, item in declared.items()
        if item.get("type") in {"subcommand", "subcommand_group"}
    }
    if not containers:
        return normalize_leaf_command_options(
            declared,
            supplied,
            path=path,
            require_complete=require_complete,
        )
    selected = [name for name in containers if name in supplied]
    if len(selected) != 1 or len(supplied) != 1:
        invalid_command_option(path, "Choose exactly one subcommand or subcommand group.")
    name = selected[0]
    nested = supplied[name]
    if not isinstance(nested, dict):
        invalid_command_option((*path, name), "Subcommand options must be an object.")
    return {
        name: validate_command_option_level(
            containers[name].get("options", []),
            nested,
            path=(*path, name),
            require_complete=require_complete,
        )
    }


def validate_command_options(
    command: ApplicationCommand,
    values: dict[str, Any],
    *,
    require_complete: bool,
) -> dict[str, Any]:
    """Validate an invocation against its immutable Discord-style definition."""

    return validate_command_option_level(
        command.definition.get("options", []),
        values,
        path=(),
        require_complete=require_complete,
    )


def command_channel_type_requirements(
    command: ApplicationCommand,
    values: dict[str, Any],
    *,
    local_domain: str,
) -> list[tuple[str, tuple[int, str], frozenset[int]]]:
    """Collect advertised channel-type constraints from the selected command leaf."""

    requirements: list[tuple[str, tuple[int, str], frozenset[int]]] = []

    def walk(definitions: object, supplied: object, *, path: tuple[str, ...]) -> None:
        if not isinstance(definitions, list) or not isinstance(supplied, dict):
            return
        for raw_definition in definitions:
            if not isinstance(raw_definition, dict):
                continue
            name = raw_definition.get("name")
            if not isinstance(name, str) or name not in supplied:
                continue
            value = supplied[name]
            option_path = (*path, name)
            if raw_definition.get("type") in {"subcommand", "subcommand_group"}:
                walk(raw_definition.get("options", []), value, path=option_path)
                continue
            raw_types = raw_definition.get("channel_types", [])
            if raw_definition.get("type") != "channel" or not isinstance(raw_types, list):
                continue
            allowed_types = frozenset(
                value
                for value in raw_types
                if isinstance(value, int) and not isinstance(value, bool)
            )
            if not allowed_types or not isinstance(value, str):
                continue
            requirements.append(
                (".".join(option_path), EntityRef(value).resolve(local_domain), allowed_types)
            )

    walk(command.definition.get("options", []), values, path=())
    return requirements


def validate_resolved_command_channel_types(
    requirements: list[tuple[str, tuple[int, str], frozenset[int]]],
    resolved_types: dict[tuple[int, str], int],
) -> None:
    for path, reference, allowed_types in requirements:
        if resolved_types.get(reference) not in allowed_types:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "COMMAND_OPTION_INVALID",
                    "option": path,
                    "message": "Choose a channel type allowed by this command.",
                },
            )


def command_attachment_id(value: object, path: tuple[str, ...]) -> int:
    if isinstance(value, bool) or not str(value).isdigit():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "COMMAND_ATTACHMENT_INVALID",
                "message": (f"Command option {'.'.join(path)} must reference an uploaded file."),
            },
        )
    attachment_id = int(str(value))
    if not 0 < attachment_id <= (1 << 63) - 1:
        raise HTTPException(
            status_code=422,
            detail={"code": "COMMAND_ATTACHMENT_INVALID"},
        )
    return attachment_id


def collect_command_attachment_ids(
    definitions: object,
    supplied: object,
    *,
    path: tuple[str, ...],
) -> list[int]:
    if not isinstance(definitions, list) or not isinstance(supplied, dict):
        return []
    collected: list[int] = []
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        name = definition.get("name")
        if not isinstance(name, str) or name not in supplied:
            continue
        value = supplied[name]
        option_path = (*path, name)
        if definition.get("type") in {"subcommand", "subcommand_group"}:
            collected.extend(
                collect_command_attachment_ids(
                    definition.get("options", []),
                    value,
                    path=option_path,
                )
            )
        elif definition.get("type") == "attachment":
            collected.append(command_attachment_id(value, option_path))
    return collected


def command_attachment_ids(command: ApplicationCommand, values: dict[str, Any]) -> list[int]:
    """Collect attachment option values from the command's declared option tree."""

    collected = collect_command_attachment_ids(
        command.definition.get("options", []),
        values,
        path=(),
    )
    if len(collected) > 10 or len(collected) != len(set(collected)):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "COMMAND_ATTACHMENTS_INVALID",
                "message": "A command can include at most ten distinct uploaded files.",
            },
        )
    return collected


async def bind_invocation_attachments(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    interaction: BotInteraction,
    attachment_ids: list[int],
    file_types_by_id: dict[int, list[str]],
    *,
    expected_encryption_mode: Literal["plaintext", "e2ee"],
) -> tuple[dict[str, dict[str, object]], list[Attachment]]:
    """Finalize channel-scoped invocation uploads and bind them once."""

    for attachment_id in sorted(attachment_ids):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    bound: list[Attachment] = []
    for attachment_id in attachment_ids:
        attachment = await finalize_attachment(
            session,
            settings,
            actor,
            attachment_id,
            required_purpose="attachment",
        )
        if (attachment.upload_channel_id, attachment.upload_channel_domain) != (
            interaction.channel_id,
            interaction.channel_domain,
        ):
            # Conceal whether a capability belongs to another channel.  The
            # upload provenance is immutable even if that channel is deleted.
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        if (
            attachment.message_id is not None
            or attachment.message_domain is not None
            or attachment.interaction_id is not None
            or attachment.interaction_response_id is not None
            or attachment.bot_installation_id is not None
            or attachment.bot_user_installation_id is not None
            or attachment.asset_binding is not None
            or attachment.report_id is not None
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "ATTACHMENT_ALREADY_USED",
                    "message": "One of the selected files is already in use.",
                },
            )
        if attachment.encryption_mode != expected_encryption_mode:
            raise HTTPException(
                status_code=409,
                detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
            )
        accepted_types = file_types_by_id.get(attachment.id, [])
        if accepted_types and not attachment_matches_file_types(
            filename=attachment.filename,
            content_type=attachment.content_type,
            file_types=accepted_types,
        ):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "INTERACTION_ATTACHMENT_TYPE_INVALID",
                    "attachment_id": str(attachment.id),
                    "file_types": accepted_types,
                },
            )
        attachment.interaction_id = interaction.id
        bound.append(attachment)
    return (
        {str(item.id): attachment_payload(item, include_lifecycle=False) for item in bound},
        bound,
    )


async def materialize_federated_interaction_attachments(
    session: AsyncSession,
    settings: Settings,
    interaction: BotInteraction,
    projections: tuple[FederatedInteractionAttachment, ...],
    attachment_ids: list[int],
    file_types_by_id: dict[int, list[str]],
    *,
    expected_encryption_mode: Literal["plaintext", "e2ee"],
) -> tuple[dict[str, dict[str, object]], list[Attachment]]:
    """Bind immutable A-owned invocation media to one C interaction."""

    by_id: dict[int, FederatedInteractionAttachment] = {}
    for projection in projections:
        raw_id = projection.attachment.get("id")
        raw_origin = projection.attachment.get("origin_domain")
        if (
            isinstance(raw_id, bool)
            or not str(raw_id).isdigit()
            or str(raw_origin) != interaction.user_domain
        ):
            raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})
        attachment_id = int(str(raw_id))
        if attachment_id in by_id:
            raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})
        by_id[attachment_id] = projection
    if set(by_id) != set(attachment_ids):
        raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})

    bound: list[Attachment] = []
    for attachment_id in sorted(attachment_ids):
        projection = by_id[attachment_id]
        raw = projection.attachment
        if federated_attachment_metadata_fingerprint(raw) != projection.metadata_fingerprint:
            raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})
        await lock_media_tombstone_ref(session, attachment_id, interaction.user_domain)
        if (
            await session.get(
                RemoteMediaTombstone,
                (interaction.user_domain, attachment_id),
            )
            is not None
            or await session.get(
                MediaTombstoneSource,
                (attachment_id, interaction.user_domain),
            )
            is not None
        ):
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        filename = raw.get("filename")
        content_type_raw = raw.get("content_type")
        size = raw.get("size")
        encryption_mode = raw.get("encryption_mode", "plaintext")
        encryption_protocol = raw.get("encryption_protocol")
        if (
            not isinstance(filename, str)
            or sanitize_filename(filename) != filename
            or not isinstance(content_type_raw, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= settings.media_max_attachment_bytes
            or encryption_mode != expected_encryption_mode
            or (
                encryption_mode == "e2ee"
                and (
                    encryption_protocol != "kaede-file-v1"
                    or filename != "encrypted-file"
                    or normalize_declared_type(content_type_raw) != "application/octet-stream"
                )
            )
            or (encryption_mode == "plaintext" and encryption_protocol is not None)
        ):
            raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})
        content_type = normalize_declared_type(content_type_raw)
        accepted_types = file_types_by_id.get(attachment_id, [])
        if accepted_types and not attachment_matches_file_types(
            filename=filename,
            content_type=content_type,
            file_types=accepted_types,
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_ATTACHMENT_TYPE_INVALID"},
            )
        width, height = remote_media_dimensions(raw)
        duration_raw = raw.get("duration_secs")
        duration_secs = (
            float(duration_raw)
            if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool)
            else None
        )
        waveform = raw.get("waveform")
        if waveform is not None and not isinstance(waveform, str):
            raise HTTPException(status_code=422, detail={"code": "ATTACHMENT_NOT_FOUND"})
        variants = sanitized_remote_variants(
            raw.get("variants", {}),
            max_bytes=settings.media_max_attachment_bytes,
        )
        blurhash = sanitized_remote_blurhash(raw.get("blurhash"))
        existing = await session.get(
            Attachment,
            (attachment_id, interaction.user_domain),
            populate_existing=True,
        )
        immutable = (
            interaction.user_id,
            interaction.user_domain,
            filename,
            content_type,
            size,
            encryption_mode,
            encryption_protocol,
            duration_secs,
            waveform,
        )
        if existing is None:
            existing = Attachment(
                id=attachment_id,
                origin_domain=interaction.user_domain,
                uploader_id=interaction.user_id,
                uploader_domain=interaction.user_domain,
                interaction_id=interaction.id,
                filename=filename,
                content_type=content_type,
                size=size,
                object_key=f"remote/{interaction.user_domain}/{attachment_id}/original",
                width=width,
                height=height,
                duration_secs=duration_secs,
                waveform=waveform,
                blurhash=blurhash,
                scan_status="encrypted" if encryption_mode == "e2ee" else "pending",
                encryption_mode=encryption_mode,
                encryption_protocol=(
                    str(encryption_protocol) if encryption_protocol is not None else None
                ),
                purpose="attachment",
                finalized_at=interaction.created_at,
                variants=variants,
            )
            session.add(existing)
        elif (
            (
                existing.uploader_id,
                existing.uploader_domain,
                existing.filename,
                existing.content_type,
                existing.size,
                existing.encryption_mode,
                existing.encryption_protocol,
                existing.duration_secs,
                existing.waveform,
            )
            != immutable
            or existing.deleted_at is not None
            or existing.interaction_id != interaction.id
        ):
            raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
        mirror = await session.get(
            FederatedInteractionAttachmentGrant,
            projection.grant_id,
            populate_existing=True,
        )
        if mirror is None:
            mirror = FederatedInteractionAttachmentGrant(
                grant_id=projection.grant_id,
                attachment_id=attachment_id,
                attachment_domain=interaction.user_domain,
                destination_domain=settings.domain,
                user_id=interaction.user_id,
                user_domain=interaction.user_domain,
                channel_id=interaction.channel_id,
                channel_domain=interaction.channel_domain,
                interaction_id=interaction.id,
                interaction_domain=settings.domain,
                metadata_fingerprint=projection.metadata_fingerprint,
                admission_grant_id=interaction.response_grant_id,
                expires_at=interaction.expires_at,
                consumed_at=datetime.now(UTC),
            )
            session.add(mirror)
        elif (
            mirror.attachment_id,
            mirror.attachment_domain,
            mirror.destination_domain,
            mirror.user_id,
            mirror.user_domain,
            mirror.channel_id,
            mirror.channel_domain,
            mirror.interaction_id,
            mirror.interaction_domain,
            mirror.metadata_fingerprint,
            mirror.admission_grant_id,
        ) != (
            attachment_id,
            interaction.user_domain,
            settings.domain,
            interaction.user_id,
            interaction.user_domain,
            interaction.channel_id,
            interaction.channel_domain,
            interaction.id,
            settings.domain,
            projection.metadata_fingerprint,
            interaction.response_grant_id,
        ):
            raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
        bound.append(existing)
    return (
        {str(item.id): attachment_payload(item, include_lifecycle=False) for item in bound},
        bound,
    )


def modal_attachment_file_types(
    fields: list[tuple[FileUpload, list[str]]],
) -> tuple[list[int], dict[int, list[str]]]:
    """Parse modal upload capabilities and retain each field's type filters."""

    attachment_ids: list[int] = []
    filters: dict[int, list[str]] = {}
    for component, values in fields:
        for value in values:
            if not value.isdigit():
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "MODAL_ATTACHMENT_INVALID",
                        "message": "A file upload field referenced an invalid upload.",
                    },
                )
            attachment_id = int(value)
            if not 0 < attachment_id <= (1 << 63) - 1:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "MODAL_ATTACHMENT_INVALID"},
                )
            attachment_ids.append(attachment_id)
            filters[attachment_id] = list(component.file_types)
    if len(attachment_ids) > 10 or len(attachment_ids) != len(set(attachment_ids)):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MODAL_ATTACHMENTS_INVALID",
                "message": "A modal can include at most ten distinct uploaded files.",
            },
        )
    return attachment_ids, filters


def command_attachment_file_types(
    command: ApplicationCommand,
    values: dict[str, Any],
) -> dict[int, list[str]]:
    """Map selected attachment capabilities to their registered file filters."""

    filters: dict[int, list[str]] = {}

    def walk(definitions: object, supplied: object) -> None:
        if not isinstance(definitions, list) or not isinstance(supplied, dict):
            return
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            name = definition.get("name")
            if not isinstance(name, str) or name not in supplied:
                continue
            value = supplied[name]
            if definition.get("type") in {"subcommand", "subcommand_group"}:
                walk(definition.get("options", []), value)
                continue
            if definition.get("type") != "attachment" or not str(value).isdigit():
                continue
            raw_types = definition.get("file_types", [])
            filters[int(str(value))] = (
                [item for item in raw_types if isinstance(item, str)]
                if isinstance(raw_types, list)
                else []
            )

    walk(command.definition.get("options", []), values)
    return filters


async def resolve_context_command_target(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    command_type: Literal["user", "message"],
    target_ref: EntityRef,
) -> tuple[str, str, dict[str, object]]:
    """Resolve a context-command target from authority-owned state.

    The client sends only a composite reference.  Keeping the resolved
    projection authority-generated matches Discord's interaction contract and
    prevents a caller from forging a member, user, or message object.
    """

    target_id, target_domain = target_ref.resolve(settings.domain)
    canonical_ref = f"{target_id}@{target_domain}"
    if command_type == "message":
        message = await session.scalar(
            select(Message).where(
                Message.id == target_id,
                Message.origin_domain == target_domain,
                Message.channel_id == access.channel.id,
                Message.channel_domain == access.channel.origin_domain,
                Message.deleted_at.is_(None),
            )
        )
        if message is None:
            raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
        rendered = await render_message_payload(session, message, viewer=actor)
        if access.channel.encryption_mode == "e2ee":
            # Context identity is useful to an encrypted interaction, but the
            # plaintext/ciphertext body and attachment envelope remain inside
            # the E2EE protocol instead of becoming side-channel metadata.
            rendered = {
                key: value
                for key, value in rendered.items()
                if key
                in {
                    "id",
                    "origin_domain",
                    "channel_id",
                    "channel_domain",
                    "author_id",
                    "author_domain",
                    "author",
                    "message_type",
                    "flags",
                    "edited_at",
                    "created_at",
                }
            }
        return canonical_ref, str(target_id), {"messages": {canonical_ref: rendered}}

    user = await session.get(User, (target_id, target_domain))
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    resolved: dict[str, object] = {"users": {canonical_ref: user_payload(user)}}
    if access.guild is None:
        if not any(
            (participant.id, participant.origin_domain) == (target_id, target_domain)
            for participant in access.participants
        ):
            raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
        return canonical_ref, str(target_id), resolved

    member = await session.get(
        GuildMember,
        (access.guild.id, access.guild.origin_domain, target_id, target_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_MEMBER_NOT_FOUND"})
    role_ids = list(
        await session.scalars(
            select(MemberRole.role_id)
            .where(
                MemberRole.guild_id == access.guild.id,
                MemberRole.guild_domain == access.guild.origin_domain,
                MemberRole.user_id == target_id,
                MemberRole.user_domain == target_domain,
            )
            .order_by(MemberRole.role_id)
        )
    )
    resolved["members"] = {canonical_ref: member_payload(member, user, role_ids)}
    return canonical_ref, str(target_id), resolved


def user_installation_payload(
    installation: BotUserInstallation,
    application: BotApplication,
) -> dict[str, object]:
    return {
        "id": str(installation.id),
        "source_ref": (
            f"{installation.source_id}@{installation.source_domain}"
            if installation.source_id is not None and installation.source_domain is not None
            else None
        ),
        "application_ref": f"{application.id}@{application.origin_domain}",
        "application_name": application.name,
        "application_description": application.description,
        "application_icon_hash": application.icon_hash,
        "e2ee_participant_capable": "participant" in application.e2ee_modes,
        "bot_user_ref": f"{application.bot_user_id}@{application.bot_user_domain}",
        "user_ref": f"{installation.user_id}@{installation.user_domain}",
        "scopes": list(installation.granted_scopes),
        "intents": list(installation.granted_intents),
        "contexts": list(installation.contexts),
        "grant_revision": str(installation.grant_revision),
        "status": installation.status,
        "revoked_at": (
            installation.revoked_at.isoformat() if installation.revoked_at is not None else None
        ),
        "created_at": (
            installation.created_at.isoformat() if installation.created_at is not None else None
        ),
        "updated_at": (
            installation.updated_at.isoformat() if installation.updated_at is not None else None
        ),
    }


async def locked_installable_user_application(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application_ref: EntityRef,
) -> BotApplication:
    """Load and lock the application before any user-installation row lock."""

    app_id, app_domain = application_ref.resolve(settings.domain)
    if app_domain != settings.domain:
        try:
            await refresh_user_bot_application(
                session,
                settings,
                snowflake,
                app_id,
                app_domain,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "APPLICATION_MANIFEST_UNAVAILABLE"},
            ) from None
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == app_id,
            BotApplication.origin_domain == app_domain,
        )
        .with_for_update()
    )
    if application is None or application.status != "active":
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    bot = await session.get(User, (application.bot_user_id, application.bot_user_domain))
    if bot is None or bot.account_type != "bot" or bot.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    await require_user_install_target(session, settings, application)
    await require_application_runtime_enabled(session, settings, application)
    command = await session.scalar(
        select(ApplicationCommand.id)
        .where(
            ApplicationCommand.application_id == application.id,
            ApplicationCommand.application_domain == application.origin_domain,
            ApplicationCommand.state == "active",
            ApplicationCommand.guild_id.is_(None),
            ApplicationCommand.integration_types.contains(["user_install"]),
        )
        .limit(1)
    )
    if command is None:
        raise HTTPException(
            status_code=409, detail={"code": "APPLICATION_USER_INSTALL_UNAVAILABLE"}
        )
    return application


async def installable_user_application(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application_ref: EntityRef,
    scopes: list[str],
    intents: list[str],
    contexts: list[str],
) -> BotApplication:
    application = await locked_installable_user_application(
        session,
        settings,
        snowflake,
        application_ref,
    )
    require_user_install_policy(application, scopes, intents, contexts)
    return application


@router.get("/users/@me/application-installations")
async def list_user_installations(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(BotUserInstallation, BotApplication)
            .join(
                BotApplication,
                (BotApplication.id == BotUserInstallation.application_id)
                & (BotApplication.origin_domain == BotUserInstallation.application_domain),
            )
            .where(
                BotUserInstallation.user_id == auth.user.id,
                BotUserInstallation.user_domain == auth.user.origin_domain,
                BotUserInstallation.status != "revoked",
            )
            .order_by(BotApplication.name, BotUserInstallation.id)
        )
    ).all()
    return [
        user_installation_payload(installation, application) for installation, application in rows
    ]


async def owned_user_installation_application_ref(
    session: AsyncSession,
    user: User,
    installation_id: int,
) -> tuple[int, str]:
    """Read only the immutable app identity before taking the app row lock."""

    row = (
        await session.execute(
            select(
                BotUserInstallation.application_id,
                BotUserInstallation.application_domain,
            ).where(
                BotUserInstallation.id == installation_id,
                BotUserInstallation.user_id == user.id,
                BotUserInstallation.user_domain == user.origin_domain,
                BotUserInstallation.status != "revoked",
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "USER_INSTALLATION_NOT_FOUND"})
    return int(row[0]), str(row[1])


async def locked_owned_user_installation(
    session: AsyncSession,
    user: User,
    installation_id: int,
    application_ref: tuple[int, str],
) -> BotUserInstallation:
    application_id, application_domain = application_ref
    installation = await session.scalar(
        select(BotUserInstallation)
        .where(
            BotUserInstallation.id == installation_id,
            BotUserInstallation.application_id == application_id,
            BotUserInstallation.application_domain == application_domain,
            BotUserInstallation.user_id == user.id,
            BotUserInstallation.user_domain == user.origin_domain,
            BotUserInstallation.status != "revoked",
        )
        .with_for_update()
    )
    if installation is None:
        raise HTTPException(status_code=404, detail={"code": "USER_INSTALLATION_NOT_FOUND"})
    return installation


@router.post("/users/@me/application-installations", status_code=201)
async def create_user_installation(
    payload: UserInstallationCreate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    application = await installable_user_application(
        session,
        settings,
        snowflake,
        payload.application_ref,
        payload.scopes,
        payload.intents,
        payload.contexts,
    )
    install_lock = int.from_bytes(
        hashlib.blake2b(
            (
                f"user-install:{application.id}@{application.origin_domain}:"
                f"{auth.user.id}@{auth.user.origin_domain}"
            ).encode(),
            digest_size=8,
        ).digest(),
        byteorder="big",
        signed=True,
    )
    await session.execute(select(func.pg_advisory_xact_lock(install_lock)))
    installation = await session.scalar(
        select(BotUserInstallation)
        .where(
            BotUserInstallation.application_id == application.id,
            BotUserInstallation.application_domain == application.origin_domain,
            BotUserInstallation.user_id == auth.user.id,
            BotUserInstallation.user_domain == auth.user.origin_domain,
        )
        .with_for_update()
    )
    paused_channels: list[Channel] = []
    if installation is None:
        installation_id = await snowflake.mint()
        installation = BotUserInstallation(
            id=installation_id,
            application_id=application.id,
            application_domain=application.origin_domain,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            granted_scopes=payload.scopes,
            granted_intents=payload.intents,
            contexts=payload.contexts,
            grant_revision=1,
            status="active",
        )
        session.add(installation)
    else:
        paused_channels = await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            user_installation_ids=(installation.id,),
        )
        if (installation.source_id, installation.source_domain) != (None, None):
            raise HTTPException(
                status_code=409,
                detail={"code": "USER_INSTALLATION_SOURCE_CONFLICT"},
            )
        installation.granted_scopes = payload.scopes
        installation.granted_intents = payload.intents
        installation.contexts = payload.contexts
        installation.grant_revision += 1
        installation.status = "active"
        installation.revoked_at = None
    destinations = await queue_application_target_snapshots_for_refs(
        session,
        settings,
        {(application.id, application.origin_domain)},
    )
    await materialize_updated_at(session, installation)
    rendered = user_installation_payload(installation, application)
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    await wake_application_target_deliveries(destinations)
    return rendered


@router.patch("/users/@me/application-installations/{installation_id}")
async def update_user_installation(
    installation_id: int,
    payload: UserInstallationPatch,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    application_ref = await owned_user_installation_application_ref(
        session,
        auth.user,
        installation_id,
    )
    application = await locked_installable_user_application(
        session,
        settings,
        snowflake,
        EntityRef(f"{application_ref[0]}@{application_ref[1]}"),
    )
    installation = await locked_owned_user_installation(
        session,
        auth.user,
        installation_id,
        application_ref,
    )
    require_user_install_policy(
        application,
        payload.scopes if payload.scopes is not None else installation.granted_scopes,
        payload.intents if payload.intents is not None else installation.granted_intents,
        payload.contexts if payload.contexts is not None else installation.contexts,
    )
    paused_channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        user_installation_ids=(installation.id,),
    )
    if payload.scopes is not None:
        installation.granted_scopes = payload.scopes
    if payload.contexts is not None:
        installation.contexts = payload.contexts
    if payload.intents is not None:
        installation.granted_intents = payload.intents
    installation.grant_revision += 1
    destinations = await queue_application_target_snapshots_for_refs(
        session,
        settings,
        {(application.id, application.origin_domain)},
    )
    await materialize_updated_at(session, installation)
    rendered = user_installation_payload(installation, application)
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    await wake_application_target_deliveries(destinations)
    return rendered


@router.delete("/users/@me/application-installations/{installation_id}", status_code=204)
async def delete_user_installation(
    installation_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    application_ref = await owned_user_installation_application_ref(
        session,
        auth.user,
        installation_id,
    )
    locked_application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == application_ref[0],
            BotApplication.origin_domain == application_ref[1],
        )
        .with_for_update()
    )
    if locked_application is None:
        raise HTTPException(status_code=404, detail={"code": "USER_INSTALLATION_NOT_FOUND"})
    installation = await locked_owned_user_installation(
        session,
        auth.user,
        installation_id,
        application_ref,
    )
    paused_channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        user_installation_ids=(installation.id,),
    )
    installation.status = "revoked"
    installation.revoked_at = datetime.now(UTC)
    installation.grant_revision += 1
    destinations = await queue_application_target_snapshots_for_refs(
        session,
        settings,
        {(installation.application_id, installation.application_domain)},
    )
    await session.commit()
    await publish_e2ee_policy_updates(session, redis, settings, paused_channels)
    await wake_application_target_deliveries(destinations)
    return Response(status_code=204)


async def _local_application_commands(
    session: AsyncSession,
    guild: Guild,
    *,
    channel: Channel | None = None,
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(ApplicationCommand, BotApplication, BotInstallation)
            .join(
                BotInstallation,
                (BotInstallation.application_id == ApplicationCommand.application_id)
                & (BotInstallation.application_domain == ApplicationCommand.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == ApplicationCommand.application_id)
                & (BotApplication.origin_domain == ApplicationCommand.application_domain),
            )
            .join(
                User,
                (User.id == BotInstallation.bot_user_id)
                & (User.origin_domain == BotInstallation.bot_user_domain),
            )
            .where(
                BotInstallation.guild_id == guild.id,
                BotInstallation.guild_domain == guild.origin_domain,
                BotInstallation.bot_user_id == BotApplication.bot_user_id,
                BotInstallation.bot_user_domain == BotApplication.bot_user_domain,
                BotInstallation.status == "active",
                BotInstallation.revoked_at.is_(None),
                installation_has_membership(),
                BotInstallation.granted_scopes.contains(["applications.commands"]),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.contexts.contains(["guild"]),
                ApplicationCommand.integration_types.contains(["guild_install"]),
                (
                    ApplicationCommand.guild_id.is_(None)
                    | (
                        (ApplicationCommand.guild_id == guild.id)
                        & (ApplicationCommand.guild_domain == guild.origin_domain)
                    )
                ),
            )
            .order_by(
                ApplicationCommand.name,
                BotApplication.name,
                ApplicationCommand.guild_id.desc().nullslast(),
            )
        )
    ).all()
    rendered: list[dict[str, Any]] = []
    installation_access: dict[int, bool] = {}
    for command, application, installation in rows:
        allowed = True
        if channel is not None:
            allowed = installation_access.get(installation.id, False)
            if installation.id not in installation_access:
                allowed = await installation_allows_channel(session, installation, channel)
                installation_access[installation.id] = allowed
        if allowed:
            rendered.append(
                command_payload(
                    command,
                    application,
                    integration_type="guild_install",
                    interaction_context="guild",
                )
            )
    return merge_application_commands(rendered, [])


async def _local_user_application_commands(
    session: AsyncSession,
    user: User,
    *,
    authority_domain: str,
    context: Literal["guild", "bot_dm", "private_channel"],
) -> list[dict[str, object]]:
    rows = (
        await session.execute(
            select(ApplicationCommand, BotApplication)
            .join(
                BotUserInstallation,
                (BotUserInstallation.application_id == ApplicationCommand.application_id)
                & (BotUserInstallation.application_domain == ApplicationCommand.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == ApplicationCommand.application_id)
                & (BotApplication.origin_domain == ApplicationCommand.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotUserInstallation.user_id == user.id,
                BotUserInstallation.user_domain == user.origin_domain,
                usable_user_installation(current_instance_domain=authority_domain),
                BotUserInstallation.granted_scopes.contains(["applications.commands"]),
                BotUserInstallation.contexts.contains([context]),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.contexts.contains([context]),
                ApplicationCommand.integration_types.contains(["user_install"]),
            )
            .order_by(ApplicationCommand.name, BotApplication.name)
        )
    ).all()
    return [
        command_payload(
            command,
            application,
            integration_type="user_install",
            interaction_context=context,
        )
        for command, application in rows
    ]


async def _local_dm_capability_commands(
    session: AsyncSession,
    user: User,
    channel: Channel,
) -> list[dict[str, object]]:
    """Discover commands through one exact live bot-DM capability."""

    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(ApplicationCommand, BotApplication, BotDMCapability)
            .join(
                BotDMCapability,
                (BotDMCapability.application_id == ApplicationCommand.application_id)
                & (BotDMCapability.application_domain == ApplicationCommand.application_domain),
            )
            .join(
                BotApplication,
                (BotApplication.id == ApplicationCommand.application_id)
                & (BotApplication.origin_domain == ApplicationCommand.application_domain),
            )
            .join(
                User,
                (User.id == BotDMCapability.bot_user_id)
                & (User.origin_domain == BotDMCapability.bot_user_domain),
            )
            .where(
                BotDMCapability.target_user_id == user.id,
                BotDMCapability.target_user_domain == user.origin_domain,
                BotDMCapability.conversation_id == channel.id,
                BotDMCapability.conversation_domain == channel.origin_domain,
                BotDMCapability.authority_domain == channel.origin_domain,
                usable_dm_capability(at=now),
                BotDMCapability.granted_scopes.contains(["applications.commands"]),
                BotDMCapability.granted_intents.contains(["interactions"]),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                ApplicationCommand.state == "active",
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.contexts.contains(["bot_dm"]),
            )
            .order_by(
                ApplicationCommand.name,
                BotApplication.name,
                BotDMCapability.source_kind.desc(),
                BotDMCapability.grant_id,
            )
        )
    ).all()
    target_by_application: dict[tuple[int, str], BotApplicationTarget | None] = {}
    seen: set[tuple[int, str, int]] = set()
    commands: list[dict[str, object]] = []
    for command, application, capability in rows:
        source_integration = f"{capability.source_kind}_install"
        identity = application.id, application.origin_domain, command.id
        if source_integration not in command.integration_types or identity in seen:
            continue
        app_ref = application.id, application.origin_domain
        if app_ref not in target_by_application:
            target_by_application[app_ref] = await session.get(
                BotApplicationTarget,
                (*app_ref, channel.origin_domain),
            )
        try:
            stored_bot_dm_capability_payload(capability, now=now)
        except ValueError:
            continue
        if not dm_capability_runtime_ready(
            application,
            target_by_application[app_ref],
            capability,
            target_domain=channel.origin_domain,
            now=now,
        ):
            continue
        seen.add(identity)
        commands.append(
            command_payload(
                command,
                application,
                integration_type="dm_capability",
                interaction_context="bot_dm",
                dm_capability=capability,
            )
        )
    return commands


def merge_application_commands(
    primary: list[dict[str, object]],
    secondary: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Merge command discovery with the primary installation taking precedence."""

    seen: set[tuple[str, str, str]] = set()
    merged: list[dict[str, object]] = []
    for command in [*primary, *secondary]:
        identity = (
            str(command.get("application_ref", "")),
            str(command.get("name", "")),
            str(command.get("type", "chat_input")),
        )
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(command)
    return merged


def private_interaction_context(
    participants: list[User],
    bot: User,
    *,
    direct: bool,
) -> Literal["bot_dm", "private_channel"]:
    """Classify context relative to the selected application, as Discord does."""

    bot_ref = (bot.id, bot.origin_domain)
    return (
        "bot_dm"
        if direct
        and any(
            (participant.id, participant.origin_domain) == bot_ref for participant in participants
        )
        else "private_channel"
    )


async def _private_channel_application_commands(
    session: AsyncSession,
    user: User,
    participants: list[User],
    channel: Channel,
    *,
    authority_domain: str,
    direct: bool,
    user_context_send_allowed: bool = True,
) -> list[dict[str, object]]:
    bot_refs = {
        (participant.id, participant.origin_domain)
        for participant in participants
        if participant.account_type == "bot"
    }
    bot_application_refs: set[str] = set()
    if direct and bot_refs:
        rows = (
            await session.execute(
                select(BotApplication.id, BotApplication.origin_domain).where(
                    tuple_(
                        BotApplication.bot_user_id,
                        BotApplication.bot_user_domain,
                    ).in_(bot_refs)
                )
            )
        ).tuples()
        bot_application_refs = {f"{app_id}@{app_domain}" for app_id, app_domain in rows}
    private_commands = await _local_user_application_commands(
        session,
        user,
        authority_domain=authority_domain,
        context="private_channel",
    )
    bot_dm_commands = await _local_user_application_commands(
        session,
        user,
        authority_domain=authority_domain,
        context="bot_dm",
    )
    capability_commands = (
        await _local_dm_capability_commands(session, user, channel) if direct and bot_refs else []
    )
    commands = [
        command
        for command in private_commands
        if str(command.get("application_ref")) not in bot_application_refs
    ] + merge_application_commands(
        capability_commands,
        [
            command
            for command in bot_dm_commands
            if str(command.get("application_ref")) in bot_application_refs
        ],
    )
    account_settings = await session.get(
        UserSettings,
        (user.id, user.origin_domain),
    )
    age_enabled = bool(
        getattr(user, "age_assurance_state", "unknown") == "adult"
        and account_settings is not None
        and account_settings.age_restricted_dm_commands_enabled
    )
    return [
        command
        for command in commands
        if (command.get("nsfw") is not True or age_enabled)
        and (command.get("type") != "user" or user_context_send_allowed)
    ]


async def _remote_guild_application_commands(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    user: User,
    *,
    channel: Channel | None = None,
) -> list[dict[str, object]]:
    upstream = await signed_request(
        session,
        settings,
        "GET",
        guild.origin_domain,
        f"/_kaede/v1/guilds/{guild.id}/application-commands",
        query={
            "user_id": str(user.id),
            "age_assured_adult": (
                "true" if getattr(user, "age_assurance_state", "unknown") == "adult" else "false"
            ),
            **({"channel_id": str(channel.id)} if channel is not None else {}),
        },
        request_timeout=10,
        max_response_bytes=512 * 1024,
    )
    if upstream.status_code != 200:
        raise HTTPException(status_code=503, detail={"code": "FEDERATED_COMMANDS_UNAVAILABLE"})
    raw = decode_federation_response_json(upstream)
    if not isinstance(raw, list) or len(raw) > 130:
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_COMMANDS_INVALID"})
    commands: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=502, detail={"code": "FEDERATED_COMMANDS_INVALID"})
        commands.append({str(key): value for key, value in item.items()})
    return commands


@router.get("/guilds/{guild_ref}/application-commands")
async def guild_application_commands(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    permissions = await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        Permission.VIEW_CHANNEL,
    )
    if guild.origin_domain != settings.domain:
        return await _remote_guild_application_commands(session, settings, guild, auth.user)
    return await filter_guild_commands_for_permissions(
        session,
        guild,
        auth.user,
        await _local_application_commands(session, guild),
        permissions,
    )


@router.get("/channels/{channel_ref}/application-commands")
async def channel_application_commands(
    channel_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    """Discover commands usable by this user in a guild or private channel."""

    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.guild is not None:
        channel_nsfw = bool(await effective_channel_nsfw(session, access.channel))
        permissions = await require_permissions(
            session,
            redis,
            access.guild,
            auth.user,
            Permission.VIEW_CHANNEL,
            channel=access.channel,
        )
        if access.guild.origin_domain != settings.domain:
            guild_commands = (
                await _remote_guild_application_commands(
                    session,
                    settings,
                    access.guild,
                    auth.user,
                    channel=access.channel,
                )
                if application_interaction_allowed(permissions, guild_installed=True)
                else []
            )
            user_commands = await _local_user_application_commands(
                session,
                auth.user,
                authority_domain=settings.domain,
                context="guild",
            )
            available_user_commands = filter_commands_for_permissions(
                user_commands,
                permissions,
                channel_type=access.channel.type,
                channel_nsfw=channel_nsfw,
                age_assured_adult=(getattr(auth.user, "age_assurance_state", "unknown") == "adult"),
            )
            return merge_application_commands(guild_commands, available_user_commands)
        guild_commands = (
            await _local_application_commands(
                session,
                access.guild,
                channel=access.channel,
            )
            if application_interaction_allowed(permissions, guild_installed=True)
            else []
        )
        user_commands = await _local_user_application_commands(
            session,
            auth.user,
            authority_domain=settings.domain,
            context="guild",
        )
        available_guild_commands = await filter_guild_commands_for_permissions(
            session,
            access.guild,
            auth.user,
            guild_commands,
            permissions,
            channel=access.channel,
            channel_nsfw=channel_nsfw,
            age_assured_adult=(getattr(auth.user, "age_assurance_state", "unknown") == "adult"),
        )
        available_user_commands = filter_commands_for_permissions(
            user_commands,
            permissions,
            channel_type=access.channel.type,
            channel_nsfw=channel_nsfw,
            age_assured_adult=(getattr(auth.user, "age_assurance_state", "unknown") == "adult"),
        )
        return merge_application_commands(available_guild_commands, available_user_commands)
    conversation = await session.get(
        DMConversation,
        (access.channel.id, access.channel.origin_domain),
    )
    user_context_send_allowed = True
    try:
        await require_dm_send(session, access, auth.user)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        user_context_send_allowed = False
    return await _private_channel_application_commands(
        session,
        auth.user,
        access.participants,
        access.channel,
        authority_domain=settings.domain,
        direct=conversation is not None and conversation.type == "direct",
        user_context_send_allowed=user_context_send_allowed,
    )


@federation_router.get("/_kaede/v1/guilds/{guild_id}/application-commands")
async def federation_guild_application_commands(
    guild_id: int,
    user_id: str,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    age_assured_adult: bool = False,
    channel_id: int | None = None,
) -> list[dict[str, object]]:
    if principal.silenced:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_SILENCED"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-command-list", capacity=600, refill_per_minute=600
    )
    if not user_id.isdigit():
        raise HTTPException(status_code=422, detail={"code": "USER_REF_INVALID"})
    user = await session.get(User, (int(user_id), principal.origin))
    guild = await session.get(Guild, (guild_id, settings.domain))
    if user is None or guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    channel: Channel | None = None
    if channel_id is None:
        permissions = await require_permissions(
            session,
            redis,
            guild,
            user,
            Permission.VIEW_CHANNEL,
        )
        channel_nsfw = False
    else:
        channel = await session.get(Channel, (channel_id, settings.domain))
        if (
            channel is None
            or channel.unavailable
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        permissions = await require_permissions(
            session,
            redis,
            guild,
            user,
            Permission.VIEW_CHANNEL,
            channel=channel,
        )
        channel_nsfw = bool(await effective_channel_nsfw(session, channel))
        if not application_interaction_allowed(permissions, guild_installed=True):
            return []
    return await filter_guild_commands_for_permissions(
        session,
        guild,
        user,
        await _local_application_commands(session, guild, channel=channel),
        permissions,
        channel=channel,
        channel_nsfw=channel_nsfw,
        age_assured_adult=age_assured_adult,
    )


async def require_permission_configuration_reader(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
) -> None:
    if not await get_permissions(session, redis, guild, actor) & Permission.MANAGE_GUILD:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})


async def list_local_command_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str],
    *,
    require_manage_guild: bool = True,
) -> list[dict[str, object]]:
    if require_manage_guild:
        await require_permission_configuration_reader(session, redis, guild, actor)
    application = await installed_guild_application(session, guild, application_ref)
    commands = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.state == "active",
                ApplicationCommand.integration_types.contains(["guild_install"]),
                (
                    ApplicationCommand.guild_id.is_(None)
                    | (
                        (ApplicationCommand.guild_id == guild.id)
                        & (ApplicationCommand.guild_domain == guild.origin_domain)
                    )
                ),
            )
            .order_by(
                ApplicationCommand.type,
                ApplicationCommand.name,
                ApplicationCommand.guild_id.desc().nullslast(),
            )
        )
    )
    payloads = [await command_permission_scope_payload(session, guild, application, None)]
    for command in commands:
        payloads.append(
            await command_permission_scope_payload(session, guild, application, command)
        )
    return payloads


async def get_local_command_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str],
    scope_ref: tuple[int, str],
    *,
    require_manage_guild: bool = True,
) -> dict[str, object]:
    if require_manage_guild:
        await require_permission_configuration_reader(session, redis, guild, actor)
    application = await installed_guild_application(session, guild, application_ref)
    command = await permission_scope_command(session, guild, application, scope_ref)
    return await command_permission_scope_payload(session, guild, application, command)


def permission_row_values(
    row: ApplicationCommandPermission,
) -> tuple[str, int, str, bool]:
    return row.target_type, row.target_id, row.target_domain, row.permission


async def put_local_command_permissions(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str],
    scope_ref: tuple[int, str],
    payload: CommandPermissionsPut,
) -> dict[str, object]:
    application = await installed_guild_application(session, guild, application_ref)
    command = await permission_scope_command(session, guild, application, scope_ref)
    await require_command_permission_manager(session, redis, guild, actor, command)
    normalized = await normalized_command_permission_entries(
        session,
        redis,
        guild,
        actor,
        payload.permissions,
    )
    app_rows = list(
        await session.scalars(
            select(ApplicationCommandPermission).where(
                ApplicationCommandPermission.application_id == application.id,
                ApplicationCommandPermission.application_domain == application.origin_domain,
                ApplicationCommandPermission.guild_id == guild.id,
                ApplicationCommandPermission.guild_domain == guild.origin_domain,
                ApplicationCommandPermission.command_id.is_(None),
            )
        )
    )
    synchronized = command is not None and set(normalized) == {
        permission_row_values(row) for row in app_rows
    }
    await session.execute(
        delete(ApplicationCommandPermission).where(
            ApplicationCommandPermission.application_id == application.id,
            ApplicationCommandPermission.application_domain == application.origin_domain,
            ApplicationCommandPermission.guild_id == guild.id,
            ApplicationCommandPermission.guild_domain == guild.origin_domain,
            (
                ApplicationCommandPermission.command_id == command.id
                if command is not None
                else ApplicationCommandPermission.command_id.is_(None)
            ),
        )
    )
    if not synchronized:
        for target_type, target_id, target_domain, permission in normalized:
            session.add(
                ApplicationCommandPermission(
                    id=await snowflake.mint(),
                    application_id=application.id,
                    application_domain=application.origin_domain,
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                    command_id=command.id if command is not None else None,
                    target_id=target_id,
                    target_domain=target_domain,
                    target_type=target_type,
                    permission=permission,
                )
            )
    await session.flush()
    rendered = await command_permission_scope_payload(
        session,
        guild,
        application,
        command,
    )
    queue_postcommit_dispatch(
        session,
        guild_topic(guild.origin_domain, guild.id),
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
        rendered,
        audience_user_refs=(f"{application.bot_user_id}@{application.bot_user_domain}",),
    )
    await session.commit()
    await publish_committed_dispatches(session, redis)
    return rendered


async def command_permission_wire_permissions(
    session: AsyncSession,
    guild: Guild,
    payload: CommandPermissionsPut,
) -> list[dict[str, object]]:
    """Qualify targets and preserve thread-to-parent permission inheritance."""

    rendered: list[dict[str, object]] = []
    for entry in payload.permissions:
        target_ref = entry.id.resolve(guild.origin_domain)
        if (
            entry.type == "channel"
            and target_ref[1] == guild.origin_domain
            and target_ref != (guild.id - 1, guild.origin_domain)
        ):
            target = await session.get(Channel, target_ref)
            if (
                target is not None
                and (target.guild_id, target.guild_domain) == (guild.id, guild.origin_domain)
                and target.type in {10, 11, 12}
                and target.parent_id is not None
                and target.parent_domain == guild.origin_domain
            ):
                target_ref = (target.parent_id, target.parent_domain)
        rendered.append(
            {
                "id": f"{target_ref[0]}@{target_ref[1]}",
                "type": entry.type,
                "permission": entry.permission,
            }
        )
    keys = [(entry["type"], entry["id"]) for entry in rendered]
    if len(keys) != len(set(keys)):
        raise HTTPException(
            status_code=422,
            detail={"code": "APPLICATION_COMMAND_PERMISSION_TARGET_DUPLICATE"},
        )
    return rendered


async def proxy_command_permissions(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    actor: User,
    application_ref: tuple[int, str],
    *,
    command_ref: tuple[int, str] | None = None,
    payload: CommandPermissionsPut | None = None,
) -> list[dict[str, object]] | dict[str, object]:
    suffix = "commands/permissions"
    if command_ref is not None:
        suffix = f"commands/{command_ref[0]}/permissions"
    wire_permissions = (
        await command_permission_wire_permissions(session, guild, payload)
        if payload is not None
        else None
    )
    upstream = await signed_request(
        session,
        settings,
        "PUT" if payload is not None else "GET",
        guild.origin_domain,
        f"/_kaede/v1/applications/{application_ref[0]}/guilds/{guild.id}/{suffix}",
        query={
            "user_id": str(actor.id),
            "application_domain": application_ref[1],
            **({"command_domain": command_ref[1]} if command_ref is not None else {}),
        },
        payload={"permissions": wire_permissions} if wire_permissions is not None else None,
        request_timeout=15,
        max_response_bytes=1024 * 1024,
    )
    if upstream.status_code != 200:
        detail: dict[str, object] = {"code": "REMOTE_COMMAND_PERMISSIONS_UNAVAILABLE"}
        if upstream.status_code in {400, 403, 404, 409, 422, 429}:
            raw = decode_federation_response_json(upstream)
            if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                detail = {str(key): value for key, value in raw["detail"].items()}
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    return validate_remote_command_permissions(
        decode_federation_response_json(upstream),
        application_ref=application_ref,
        guild_ref=(guild.id, guild.origin_domain),
        command_ref=command_ref,
        expected_permissions=wire_permissions,
    )


async def command_permission_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
) -> Guild:
    guild = await session.get(Guild, guild_ref.resolve(settings.domain))
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


@router.get("/bots/applications/@me/guilds/{guild_ref}/commands/permissions")
async def bot_list_application_command_permissions(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    """Read this bot application's command permissions at the guild authority."""

    guild, _installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "applications.commands",
    )
    return await list_local_command_permissions(
        session,
        redis,
        guild,
        principal.user,
        (principal.application.id, principal.application.origin_domain),
        require_manage_guild=False,
    )


@router.get("/bots/applications/@me/guilds/{guild_ref}/commands/{command_ref}/permissions")
async def bot_get_application_command_permissions(
    guild_ref: EntityRef,
    command_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, _installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "applications.commands",
    )
    application_ref = (
        principal.application.id,
        principal.application.origin_domain,
    )
    scope_ref = command_ref.resolve(principal.application.origin_domain)
    return await get_local_command_permissions(
        session,
        redis,
        guild,
        principal.user,
        application_ref,
        scope_ref,
        require_manage_guild=False,
    )


@router.get("/applications/{application_ref}/guilds/{guild_ref}/commands/permissions")
async def list_application_command_permissions(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild = await command_permission_guild(session, settings, guild_ref)
    app_ref = application_ref.resolve(settings.domain)
    if guild.origin_domain != settings.domain:
        raw = await proxy_command_permissions(
            session,
            settings,
            guild,
            auth.user,
            app_ref,
        )
        return cast(list[dict[str, object]], raw)
    return await list_local_command_permissions(session, redis, guild, auth.user, app_ref)


@router.get("/applications/{application_ref}/guilds/{guild_ref}/commands/{command_ref}/permissions")
async def get_application_command_permissions(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    command_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await command_permission_guild(session, settings, guild_ref)
    app_ref = application_ref.resolve(settings.domain)
    scope_ref = command_ref.resolve(settings.domain)
    if guild.origin_domain != settings.domain:
        raw = await proxy_command_permissions(
            session,
            settings,
            guild,
            auth.user,
            app_ref,
            command_ref=scope_ref,
        )
        return cast(dict[str, object], raw)
    return await get_local_command_permissions(
        session,
        redis,
        guild,
        auth.user,
        app_ref,
        scope_ref,
    )


@router.put("/applications/{application_ref}/guilds/{guild_ref}/commands/{command_ref}/permissions")
async def put_application_command_permissions(
    application_ref: EntityRef,
    guild_ref: EntityRef,
    command_ref: EntityRef,
    payload: CommandPermissionsPut,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await command_permission_guild(session, settings, guild_ref)
    app_ref = application_ref.resolve(settings.domain)
    scope_ref = command_ref.resolve(settings.domain)
    if guild.origin_domain != settings.domain:
        raw = await proxy_command_permissions(
            session,
            settings,
            guild,
            auth.user,
            app_ref,
            command_ref=scope_ref,
            payload=payload,
        )
        return cast(dict[str, object], raw)
    return await put_local_command_permissions(
        session,
        redis,
        snowflake,
        guild,
        auth.user,
        app_ref,
        scope_ref,
        payload,
    )


def federated_permission_actor(
    user: User | None,
    principal: FederationPrincipal,
) -> AuthenticatedUser:
    if user is None or user.account_type != "human" or user.origin_domain != principal.origin:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    return AuthenticatedUser(
        user=user,
        grant=cast(Any, None),
        access_token="",
        cookie_authenticated=False,
    )


@federation_router.get(
    "/_kaede/v1/applications/{application_id}/guilds/{guild_id}/commands/permissions"
)
async def federation_list_application_command_permissions(
    application_id: int,
    guild_id: int,
    application_domain: str,
    user_id: str,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    if principal.silenced or not user_id.isdigit():
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-command-permission-list",
        capacity=120,
        refill_per_minute=60,
    )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = federated_permission_actor(
        await session.get(User, (int(user_id), principal.origin)),
        principal,
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return await list_local_command_permissions(
        session,
        redis,
        guild,
        actor.user,
        EntityRef(f"{application_id}@{application_domain}").resolve(settings.domain),
    )


@federation_router.get(
    "/_kaede/v1/applications/{application_id}/guilds/{guild_id}/commands/{command_id}/permissions"
)
async def federation_get_application_command_permissions(
    application_id: int,
    guild_id: int,
    command_id: int,
    application_domain: str,
    command_domain: str,
    user_id: str,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced or not user_id.isdigit():
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-command-permission-get",
        capacity=240,
        refill_per_minute=120,
    )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = federated_permission_actor(
        await session.get(User, (int(user_id), principal.origin)),
        principal,
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return await get_local_command_permissions(
        session,
        redis,
        guild,
        actor.user,
        EntityRef(f"{application_id}@{application_domain}").resolve(settings.domain),
        EntityRef(f"{command_id}@{command_domain}").resolve(settings.domain),
    )


@federation_router.put(
    "/_kaede/v1/applications/{application_id}/guilds/{guild_id}/commands/{command_id}/permissions"
)
async def federation_put_application_command_permissions(
    application_id: int,
    guild_id: int,
    command_id: int,
    application_domain: str,
    command_domain: str,
    user_id: str,
    payload: CommandPermissionsPut,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced or not user_id.isdigit():
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-command-permission-put",
        capacity=60,
        refill_per_minute=30,
    )
    guild = await session.get(Guild, (guild_id, settings.domain))
    actor = federated_permission_actor(
        await session.get(User, (int(user_id), principal.origin)),
        principal,
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_remote_user_creation_allowed(session, actor.user)
    return await put_local_command_permissions(
        session,
        redis,
        snowflake,
        guild,
        actor.user,
        EntityRef(f"{application_id}@{application_domain}").resolve(settings.domain),
        EntityRef(f"{command_id}@{command_domain}").resolve(settings.domain),
        payload,
    )


@dataclass(frozen=True, slots=True)
class InteractionAdmission:
    access: ChannelAccess
    invoker_policy: InteractionInvokerPolicy
    invocation_permissions: int | None
    application_ref: tuple[int, str]


def interaction_remote_authority(access: ChannelAccess, local_domain: str) -> str | None:
    if access.guild is not None and access.guild.origin_domain != local_domain:
        return access.guild.origin_domain
    if access.guild is None and access.channel.origin_domain != local_domain:
        return access.channel.origin_domain
    return None


async def interaction_permissions_snapshot(
    session: AsyncSession,
    redis: Redis,
    access: ChannelAccess,
    user: User,
    payload: InteractionCreate,
) -> int | None:
    if access.guild is None:
        return None
    return int(
        await require_permissions(
            session,
            redis,
            access.guild,
            user,
            Permission.VIEW_CHANNEL,
            channel=access.channel,
        )
    )


async def prepare_interaction_admission(
    channel_ref: EntityRef,
    payload: InteractionCreate,
    response: Response,
    auth: AuthenticatedUser,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    invocation_options: InteractionInvocationOptions,
) -> InteractionAdmission:
    await enforce_keyed_rate_limit(
        redis,
        response,
        INTERACTION_LIMIT,
        identity=f"{auth.user.origin_domain}:{auth.user.id}",
    )
    access = await load_channel_access(session, settings, auth.user, channel_ref)
    if access.channel.type in {10, 11, 12} and access.channel.archived:
        raise HTTPException(status_code=409, detail={"code": "THREAD_ARCHIVED"})
    return InteractionAdmission(
        access=access,
        invoker_policy=await authoritative_interaction_invoker_policy(
            session,
            settings,
            auth.user,
            invocation_options,
        ),
        invocation_permissions=await interaction_permissions_snapshot(
            session,
            redis,
            access,
            auth.user,
            payload,
        ),
        application_ref=payload.application_ref.resolve(settings.domain),
    )


def federated_user_installation_payload(
    installation: BotUserInstallation | None,
    *,
    authority_expires_at: datetime,
) -> dict[str, object] | None:
    if installation is None:
        return None
    return {
        "id": str(installation.source_id or installation.id),
        "application_ref": f"{installation.application_id}@{installation.application_domain}",
        "scopes": list(installation.granted_scopes),
        "intents": list(installation.granted_intents),
        "contexts": list(installation.contexts),
        "grant_revision": str(installation.grant_revision),
        "authority_expires_at": authority_expires_at.astimezone(UTC).isoformat(),
    }


async def proxy_interaction_attachment_selection(
    session: AsyncSession,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> tuple[list[int], dict[int, list[str]]]:
    if payload.encrypted_payload is not None:
        return list(payload.attachment_ids), {}
    if payload.interaction_type == "modal_submit":
        attachment_ids: list[int] = []
        for row in payload.components:
            candidates: list[object] = []
            if row.get("type") == 18:
                candidates.append(row.get("component"))
            elif row.get("type") == 1 and isinstance(row.get("components"), list):
                candidates.extend(row["components"])
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("type") != 19:
                    continue
                raw_values = candidate.get("values")
                if not isinstance(raw_values, list):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "MODAL_SUBMISSION_INVALID"},
                    )
                for raw_value in raw_values:
                    if (
                        not isinstance(raw_value, str)
                        or not raw_value.isascii()
                        or not raw_value.isdecimal()
                        or raw_value.startswith("0")
                        or not 0 < int(raw_value) <= (1 << 63) - 1
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail={"code": "MODAL_SUBMISSION_INVALID"},
                        )
                    attachment_ids.append(int(raw_value))
        if len(attachment_ids) > 10 or len(attachment_ids) != len(set(attachment_ids)):
            raise HTTPException(
                status_code=422,
                detail={"code": "MODAL_SUBMISSION_INVALID"},
            )
        return attachment_ids, {}
    if payload.interaction_type != "command" or payload.command_id is None:
        return [], {}
    command = await session.scalar(
        select(ApplicationCommand).where(
            ApplicationCommand.application_id == application_ref[0],
            ApplicationCommand.application_domain == application_ref[1],
            ApplicationCommand.name == payload.command_name,
            ApplicationCommand.type == payload.command_type,
            ApplicationCommand.state == "active",
            or_(
                (
                    (ApplicationCommand.source_id == payload.command_id)
                    & (ApplicationCommand.source_domain == application_ref[1])
                ),
                (
                    (ApplicationCommand.id == payload.command_id)
                    & ApplicationCommand.source_id.is_(None)
                ),
            ),
        )
    )
    if command is None:
        return [], {}
    attachment_ids = command_attachment_ids(command, payload.options)
    return attachment_ids, command_attachment_file_types(command, payload.options)


def federated_attachment_metadata_fingerprint(metadata: dict[str, object]) -> str:
    serialized = json.dumps(
        metadata,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).hexdigest()


async def prepare_federated_interaction_attachments(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    payload: InteractionCreate,
    admission: InteractionAdmission,
    remote_authority: str,
    response_grant_id: str,
    response_expires_at: datetime,
) -> list[FederatedInteractionAttachment]:
    attachment_ids, file_types = await proxy_interaction_attachment_selection(
        session,
        payload,
        admission.application_ref,
    )
    for attachment_id in sorted(attachment_ids):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    projections: list[FederatedInteractionAttachment] = []
    for attachment_id in attachment_ids:
        attachment = await finalize_attachment(
            session,
            settings,
            actor,
            attachment_id,
            required_purpose="attachment",
        )
        if (
            (attachment.upload_channel_id, attachment.upload_channel_domain)
            != (admission.access.channel.id, admission.access.channel.origin_domain)
            or attachment.message_id is not None
            or attachment.message_domain is not None
            or attachment.interaction_id is not None
            or attachment.interaction_response_id is not None
            or attachment.bot_installation_id is not None
            or attachment.bot_user_installation_id is not None
            or attachment.bot_dm_capability_id is not None
            or attachment.asset_binding is not None
            or attachment.report_id is not None
        ):
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        expected_mode = (
            "e2ee" if admission.access.channel.encryption_mode == "e2ee" else "plaintext"
        )
        if attachment.encryption_mode != expected_mode:
            raise HTTPException(
                status_code=409,
                detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
            )
        accepted_types = file_types.get(attachment.id, [])
        if accepted_types and not attachment_matches_file_types(
            filename=attachment.filename,
            content_type=attachment.content_type,
            file_types=accepted_types,
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_ATTACHMENT_TYPE_INVALID"},
            )
        metadata = attachment_payload(attachment, include_lifecycle=False)
        metadata["content_sha256"] = attachment.content_sha256
        fingerprint = federated_attachment_metadata_fingerprint(metadata)
        existing = await session.scalar(
            select(FederatedInteractionAttachmentGrant)
            .where(
                FederatedInteractionAttachmentGrant.attachment_id == attachment.id,
                FederatedInteractionAttachmentGrant.attachment_domain == attachment.origin_domain,
                FederatedInteractionAttachmentGrant.destination_domain == remote_authority,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None and (
            existing.expires_at <= datetime.now(UTC)
            or existing.interaction_id is not None
            or existing.metadata_fingerprint != fingerprint
            or existing.admission_grant_id != response_grant_id
            or (existing.user_id, existing.user_domain) != (actor.id, actor.origin_domain)
            or (existing.channel_id, existing.channel_domain)
            != (admission.access.channel.id, admission.access.channel.origin_domain)
        ):
            raise HTTPException(status_code=409, detail={"code": "ATTACHMENT_ALREADY_USED"})
        if existing is None:
            existing = FederatedInteractionAttachmentGrant(
                grant_id=secrets.token_urlsafe(32),
                attachment_id=attachment.id,
                attachment_domain=attachment.origin_domain,
                destination_domain=remote_authority,
                user_id=actor.id,
                user_domain=actor.origin_domain,
                channel_id=admission.access.channel.id,
                channel_domain=admission.access.channel.origin_domain,
                metadata_fingerprint=fingerprint,
                admission_grant_id=response_grant_id,
                expires_at=response_expires_at,
            )
            session.add(existing)
        projections.append(
            FederatedInteractionAttachment(
                grant_id=existing.grant_id,
                metadata_fingerprint=fingerprint,
                attachment=metadata,
            )
        )
    return projections


async def proxy_remote_interaction(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    payload: InteractionCreate,
    admission: InteractionAdmission,
    remote_authority: str,
) -> dict[str, object]:
    app_id, app_domain = admission.application_ref
    inherited_lineage = interaction_inherits_authority_installation(payload)
    lease_issued_at = datetime.now(UTC)
    response_expires_at = lease_issued_at + INTERACTION_LIFETIME
    authority_expires_at = response_expires_at + USER_INSTALLATION_AUTHORITY_GRACE
    user_grant = (
        await session.scalar(
            select(BotUserInstallation).where(
                BotUserInstallation.application_id == app_id,
                BotUserInstallation.application_domain == app_domain,
                BotUserInstallation.user_id == auth.user.id,
                BotUserInstallation.user_domain == auth.user.origin_domain,
                usable_user_installation(current_instance_domain=settings.domain),
                BotUserInstallation.granted_scopes.contains(["applications.commands"]),
            )
        )
        if not inherited_lineage and payload.integration_type == "user_install"
        else None
    )
    policy = admission.invoker_policy
    response_grant_id = secrets.token_urlsafe(32)
    attachment_projections = await prepare_federated_interaction_attachments(
        session,
        settings,
        auth.user,
        payload,
        admission,
        remote_authority,
        response_grant_id,
        response_expires_at,
    )
    session.add(
        FederatedInteractionAdmissionGrant(
            grant_id=response_grant_id,
            authority_domain=remote_authority,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            channel_id=admission.access.channel.id,
            channel_domain=remote_authority,
            application_id=app_id,
            application_domain=app_domain,
            expires_at=response_expires_at,
        )
    )
    # A callback can race the HTTP acknowledgement.  Make the exact response
    # admission capability durable before C is allowed to mint an interaction.
    await session.commit()
    upstream = await signed_request(
        session,
        settings,
        "POST",
        remote_authority,
        f"/_kaede/v1/channels/{admission.access.channel.id}/interactions",
        payload={
            "user_id": str(auth.user.id),
            "interaction": payload.model_dump(mode="json"),
            "response_grant_id": response_grant_id,
            "response_expires_at": response_expires_at.isoformat(),
            "attachments": [item.model_dump(mode="json") for item in attachment_projections],
            "user_installation": (
                None
                if inherited_lineage or payload.integration_type == "dm_capability"
                else federated_user_installation_payload(
                    user_grant,
                    authority_expires_at=authority_expires_at,
                )
            ),
            "locale": policy.locale,
            "age_assured_adult": policy.age_assured_adult,
            "age_restricted_dm_commands_enabled": policy.age_restricted_dm_commands_enabled,
        },
        request_timeout=10,
        max_response_bytes=64 * 1024,
        allow_json_floats=True,
        guild_context=admission.access.guild is not None,
    )
    if upstream.status_code != 202:
        detail: dict[str, object] = {"code": "FEDERATED_INTERACTION_UNAVAILABLE"}
        if upstream.status_code in {400, 403, 404, 409, 422, 429, 507}:
            raw = decode_federation_response_json(upstream)
            if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                detail = {str(key): value for key, value in raw["detail"].items()}
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    raw = decode_federation_response_json(upstream)
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail={"code": "FEDERATED_INTERACTION_INVALID"})
    try:
        raw_id = raw.get("id")
        raw_ref = raw.get("interaction_ref")
        if not isinstance(raw_id, str) or not raw_id.isascii() or not raw_id.isdecimal():
            raise ValueError("missing interaction id")
        if not isinstance(raw_ref, str):
            raise ValueError("missing interaction reference")
        interaction_ref = EntityRef(raw_ref).resolve(settings.domain)
        if interaction_ref != (int(raw_id), remote_authority):
            raise ValueError("interaction acknowledgement authority mismatch")
    except (TypeError, ValueError):
        await session.rollback()
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_INTERACTION_INVALID"},
        ) from None
    grant = await session.scalar(
        select(FederatedInteractionAdmissionGrant)
        .where(FederatedInteractionAdmissionGrant.grant_id == response_grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        grant is None
        or grant.authority_domain != remote_authority
        or grant.expires_at <= datetime.now(UTC)
        or (
            grant.interaction_id is not None
            and (grant.interaction_id, grant.interaction_domain) != interaction_ref
        )
    ):
        await session.rollback()
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_INTERACTION_INVALID"},
        )
    if grant.interaction_id is None:
        grant.interaction_id, grant.interaction_domain = interaction_ref
    for projection in sorted(attachment_projections, key=lambda item: item.grant_id):
        attachment_grant = await session.scalar(
            select(FederatedInteractionAttachmentGrant)
            .where(FederatedInteractionAttachmentGrant.grant_id == projection.grant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            attachment_grant is None
            or attachment_grant.destination_domain != remote_authority
            or (
                attachment_grant.interaction_id is not None
                and (
                    attachment_grant.interaction_id,
                    attachment_grant.interaction_domain,
                )
                != interaction_ref
            )
        ):
            await session.rollback()
            raise HTTPException(
                status_code=502,
                detail={"code": "FEDERATED_INTERACTION_INVALID"},
            )
        attachment_grant.interaction_id, attachment_grant.interaction_domain = interaction_ref
    await session.commit()
    return {str(key): value for key, value in raw.items()}


@dataclass(frozen=True, slots=True)
class InteractionSource:
    """Authoritative message/view state used by component and modal invocations."""

    message: Message | None
    ephemeral_response: BotInteractionResponse | None
    ephemeral_parent: BotInteraction | None
    modal_response: BotInteractionResponse | None
    modal_parent: BotInteraction | None
    component_type: int | str | None
    values: list[str]
    components: list[dict[str, object]]
    resolved: dict[str, object]
    modal_file_fields: list[tuple[FileUpload, list[str]]]
    custom_id: str | None
    source_component: dict[str, object] | None
    source_modal: dict[str, object] | None
    installation_type: str | None = None
    installation_id: int | None = None
    installation_domain: str | None = None
    installation_revision: int | None = None

    @property
    def authority_parent(self) -> BotInteraction | None:
        return self.ephemeral_parent or self.modal_parent


def empty_interaction_source(payload: InteractionCreate) -> InteractionSource:
    return InteractionSource(
        message=None,
        ephemeral_response=None,
        ephemeral_parent=None,
        modal_response=None,
        modal_parent=None,
        component_type=None,
        values=[],
        components=[],
        resolved={},
        modal_file_fields=[],
        custom_id=payload.custom_id,
        source_component=None,
        source_modal=None,
    )


async def public_component_source(
    session: AsyncSession,
    settings: Settings,
    access: ChannelAccess,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> tuple[Message, MessageView, object]:
    if payload.message_ref is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    message = await session.get(
        Message,
        payload.message_ref.resolve(settings.domain),
        with_for_update=True,
    )
    if (
        message is None
        or message.deleted_at is not None
        or (message.application_id, message.application_domain) != application_ref
        or (message.channel_id, message.channel_domain)
        != (access.channel.id, access.channel.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    view = await session.get(
        MessageView,
        (message.id, message.origin_domain),
        with_for_update=True,
    )
    if (
        view is None
        or (view.application_id, view.application_domain) != application_ref
        or (
            not view.persistent
            and view.expires_at is not None
            and view.expires_at <= datetime.now(UTC)
        )
        or message.view_version != view.version
        or (payload.view_version is not None and payload.view_version != view.version)
    ):
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_VIEW_EXPIRED"})
    source: object = list(message.components or [])
    envelope = getattr(message, "e2ee", None)
    if isinstance(envelope, dict) and "interaction_contract" in envelope:
        source = envelope["interaction_contract"]
    return message, view, source


async def ephemeral_component_source(
    session: AsyncSession,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> tuple[BotInteractionResponse, BotInteraction, object]:
    row = (
        await session.execute(
            select(BotInteractionResponse, BotInteraction)
            .join(
                BotInteraction,
                BotInteraction.id == BotInteractionResponse.interaction_id,
            )
            .where(BotInteractionResponse.id == payload.response_id)
            .with_for_update(of=BotInteractionResponse)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"},
        )
    response, parent = row
    stored_version = int(response.payload.get("view_version", 1))
    raw_expiry = response.payload.get("view_expires_at")
    try:
        view_expiry = (
            datetime.fromisoformat(str(raw_expiry)) if raw_expiry is not None else parent.expires_at
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_VIEW_INVALID"},
        ) from exc
    if (
        not response.ephemeral
        or response.deleted_at is not None
        or (parent.application_id, parent.application_domain) != application_ref
        or (parent.user_id, parent.user_domain) != (actor.id, actor.origin_domain)
        or (parent.channel_id, parent.channel_domain)
        != (access.channel.id, access.channel.origin_domain)
        or parent.expires_at <= datetime.now(UTC)
        or view_expiry <= datetime.now(UTC)
        or payload.view_version != stored_version
    ):
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_VIEW_EXPIRED"})
    source: object = list(response.payload.get("components", []))
    envelope = response.payload.get("e2ee")
    if isinstance(envelope, dict) and "interaction_contract" in envelope:
        source = envelope["interaction_contract"]
    return response, parent, source


async def resolve_component_interaction_source(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> InteractionSource:
    message: Message | None = None
    ephemeral_response: BotInteractionResponse | None = None
    ephemeral_parent: BotInteraction | None = None
    if payload.message_ref is not None:
        message, public_view, source_components = await public_component_source(
            session,
            settings,
            access,
            payload,
            application_ref,
        )
    else:
        public_view = None
        (
            ephemeral_response,
            ephemeral_parent,
            source_components,
        ) = await ephemeral_component_source(
            session,
            access,
            actor,
            payload,
            application_ref,
        )
    if payload.custom_id is None:
        raise HTTPException(status_code=404, detail={"code": "COMPONENT_NOT_FOUND"})
    encrypted = payload.encrypted_payload is not None
    component: dict[str, object] | InteractiveComponent
    if encrypted:
        if isinstance(source_components, dict):
            try:
                component = interaction_routing_component(
                    source_components,
                    payload.custom_id,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "INTERACTION_VIEW_INVALID"},
                ) from exc
        else:
            component = resolve_interactive_component(source_components, payload.custom_id)
        submission = None
    else:
        submission = validate_component_submission(
            source_components,
            payload.custom_id,
            payload.values,
        )
        component = submission.component
    resolved: dict[str, object] = {}
    if submission is not None and isinstance(submission.component, EntitySelect):
        resolved = await resolve_component_entities(
            session,
            redis,
            settings,
            access,
            actor,
            [(submission.component, submission.values)],
        )
    component_type: object = (
        component["type"]
        if isinstance(component, dict)
        else component.type
        if submission is None
        else submission.component_type
    )
    if not isinstance(component_type, (int, str)):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    return InteractionSource(
        message=message,
        ephemeral_response=ephemeral_response,
        ephemeral_parent=ephemeral_parent,
        modal_response=None,
        modal_parent=None,
        component_type=component_type,
        values=[] if submission is None else submission.values,
        components=[],
        resolved=resolved,
        modal_file_fields=[],
        custom_id=payload.custom_id,
        source_component=(
            (
                dict(component)
                if isinstance(component, dict)
                else component.model_dump(mode="json", exclude_none=True)
            )
            if encrypted
            else None
        ),
        source_modal=None,
        installation_type=(public_view.integration_type if public_view is not None else None),
        installation_id=(public_view.installation_id if public_view is not None else None),
        installation_domain=(public_view.installation_domain if public_view is not None else None),
        installation_revision=(
            public_view.installation_revision if public_view is not None else None
        ),
    )


async def resolve_modal_interaction_source(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> InteractionSource:
    row = (
        await session.execute(
            select(BotInteractionResponse, BotInteraction)
            .join(
                BotInteraction,
                BotInteraction.id == BotInteractionResponse.interaction_id,
            )
            .where(
                BotInteractionResponse.id == payload.response_id,
                BotInteractionResponse.sequence == 0,
                BotInteractionResponse.response_type == 9,
                BotInteractionResponse.deleted_at.is_(None),
            )
            .with_for_update(of=(BotInteractionResponse, BotInteraction))
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INTERACTION_MODAL_EXPIRED",
                "message": "This form was already submitted or has expired. Run it again.",
            },
        )
    response, parent = row
    if (
        (parent.application_id, parent.application_domain) != application_ref
        or (parent.user_id, parent.user_domain) != (actor.id, actor.origin_domain)
        or (parent.channel_id, parent.channel_domain)
        != (access.channel.id, access.channel.origin_domain)
        or parent.expires_at <= datetime.now(UTC)
        or parent.status != "responded"
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "INTERACTION_MODAL_EXPIRED",
                "message": "This form is not available in this channel. Run it again.",
            },
        )
    encrypted = payload.encrypted_payload is not None
    modal: dict[str, object] | Modal
    if encrypted:
        envelope = response.payload.get("e2ee")
        contract = envelope.get("interaction_contract") if isinstance(envelope, dict) else None
        try:
            modal = interaction_routing_modal(contract, payload.custom_id or "")
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_MODAL_INVALID"},
            ) from exc
        submission = None
    else:
        submission = validate_modal_submission(
            response.payload,
            payload.custom_id or "",
            payload.components,
        )
        modal = submission.modal
    resolved = (
        {}
        if submission is None
        else await resolve_component_entities(
            session,
            redis,
            settings,
            access,
            actor,
            submission.entity_fields,
        )
    )
    return InteractionSource(
        message=None,
        ephemeral_response=None,
        ephemeral_parent=None,
        modal_response=response,
        modal_parent=parent,
        component_type=None,
        values=[],
        components=[] if submission is None else submission.components,
        resolved=resolved,
        modal_file_fields=[] if submission is None else submission.file_fields,
        custom_id=payload.custom_id,
        source_component=None,
        source_modal=(
            dict(modal)
            if isinstance(modal, dict)
            else modal.model_dump(mode="json", exclude_none=True)
            if encrypted
            else None
        ),
    )


async def resolve_interaction_source(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
) -> InteractionSource:
    if payload.interaction_type == "component":
        return await resolve_component_interaction_source(
            session,
            redis,
            settings,
            access,
            actor,
            payload,
            application_ref,
        )
    if payload.interaction_type == "modal_submit":
        return await resolve_modal_interaction_source(
            session,
            redis,
            settings,
            access,
            actor,
            payload,
            application_ref,
        )
    return empty_interaction_source(payload)


@dataclass(frozen=True, slots=True)
class InteractionApplicationContext:
    command: ApplicationCommand | None
    installation: BotInstallation | None
    user_installation: BotUserInstallation | None
    dm_capability: BotDMCapability | None
    application: BotApplication
    bot: User
    interaction_context: Literal["guild", "bot_dm", "private_channel"]


async def guild_application_installation(
    session: AsyncSession,
    guild: Guild,
    channel: Channel,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
    authority_parent: BotInteraction | None,
    source_lineage: tuple[str, int, str, int] | None = None,
) -> tuple[BotInstallation | None, BotApplication | None, User | None, ApplicationCommand | None]:
    app_id, app_domain = application_ref
    statement = (
        select(BotInstallation, BotApplication, User)
        .join(
            BotApplication,
            (BotApplication.id == BotInstallation.application_id)
            & (BotApplication.origin_domain == BotInstallation.application_domain),
        )
        .join(
            User,
            (User.id == BotInstallation.bot_user_id)
            & (User.origin_domain == BotInstallation.bot_user_domain),
        )
        .where(
            BotInstallation.application_id == app_id,
            BotInstallation.application_domain == app_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.status == "active",
            BotInstallation.revoked_at.is_(None),
            installation_has_membership(),
            BotInstallation.granted_scopes.contains(["applications.commands"]),
            BotApplication.status == "active",
            User.account_type == "bot",
            User.disabled_at.is_(None),
        )
    )
    selected_type = source_lineage[0] if source_lineage is not None else payload.integration_type
    if selected_type is not None and selected_type != "guild_install":
        return None, None, None, None
    if source_lineage is not None:
        statement = statement.where(
            BotInstallation.id == source_lineage[1],
            BotInstallation.guild_domain == source_lineage[2],
            BotInstallation.grant_revision == source_lineage[3],
        )
    if authority_parent is not None:
        statement = statement.where(
            BotInstallation.id
            == (
                authority_parent.installation_id
                if authority_parent.installation_id is not None
                else -1
            )
        )
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        return None, None, None, None
    installation, application, bot = row
    if not await installation_allows_channel(session, installation, channel):
        return None, None, None, None
    command = None
    if payload.interaction_type in {"command", "autocomplete"}:
        command = await guild_install_command(
            session,
            guild,
            application_ref=application_ref,
            name=payload.command_name or "",
            command_type=payload.command_type,
            command_id=payload.command_id,
        )
    return installation, application, bot, command


async def guild_user_installation(
    session: AsyncSession,
    guild: Guild,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
    authority_parent: BotInteraction | None,
    source_lineage: tuple[str, int, str, int] | None = None,
    *,
    authority_domain: str,
) -> tuple[
    BotUserInstallation | None, BotApplication | None, User | None, ApplicationCommand | None
]:
    app_id, app_domain = application_ref
    statement = select(BotUserInstallation).where(
        BotUserInstallation.application_id == app_id,
        BotUserInstallation.application_domain == app_domain,
        usable_user_installation(current_instance_domain=authority_domain),
        BotUserInstallation.granted_scopes.contains(["applications.commands"]),
        BotUserInstallation.contexts.contains(["guild"]),
    )
    selected_type = source_lineage[0] if source_lineage is not None else payload.integration_type
    if selected_type is not None and selected_type != "user_install":
        return None, None, None, None
    if source_lineage is not None:
        statement = statement.where(
            BotUserInstallation.id == source_lineage[1],
            BotUserInstallation.grant_revision == source_lineage[3],
        )
    else:
        statement = statement.where(
            BotUserInstallation.user_id == actor.id,
            BotUserInstallation.user_domain == actor.origin_domain,
        )
    if authority_parent is not None:
        statement = statement.where(
            BotUserInstallation.id
            == (
                authority_parent.user_installation_id
                if authority_parent.user_installation_id is not None
                else -1
            )
        )
    installation = await session.scalar(statement)
    if installation is None:
        return None, None, None, None
    command = None
    if payload.interaction_type in {"command", "autocomplete"}:
        command = await session.scalar(
            select(ApplicationCommand).where(
                ApplicationCommand.application_id == app_id,
                ApplicationCommand.application_domain == app_domain,
                ApplicationCommand.name == payload.command_name,
                ApplicationCommand.type == payload.command_type,
                ApplicationCommand.state == "active",
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.contexts.contains(["guild"]),
                ApplicationCommand.integration_types.contains(["user_install"]),
                *(
                    [
                        or_(
                            (
                                (ApplicationCommand.source_id == payload.command_id)
                                & (ApplicationCommand.source_domain == app_domain)
                            ),
                            (
                                (ApplicationCommand.id == payload.command_id)
                                & ApplicationCommand.source_id.is_(None)
                            ),
                        )
                    ]
                    if payload.command_id is not None
                    else []
                ),
            )
        )
        if command is None:
            return None, None, None, None
    application = await session.get(BotApplication, application_ref)
    bot = (
        await session.get(
            User,
            (application.bot_user_id, application.bot_user_domain),
        )
        if application is not None
        else None
    )
    return installation, application, bot, command


async def resolve_guild_interaction_application(
    session: AsyncSession,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
    authority_parent: BotInteraction | None,
    source_lineage: tuple[str, int, str, int] | None = None,
    *,
    authority_domain: str,
) -> InteractionApplicationContext:
    if access.guild is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_CONTEXT_INVALID"})
    installation, application, bot, command = await guild_application_installation(
        session,
        access.guild,
        access.channel,
        payload,
        application_ref,
        authority_parent,
        source_lineage,
    )
    command_interaction = payload.interaction_type in {"command", "autocomplete"}
    user_installation = None
    allow_user_fallback = (
        source_lineage is None
        and payload.integration_type in {None, "user_install"}
        or source_lineage is not None
        and source_lineage[0] == "user_install"
    )
    if allow_user_fallback and (installation is None or (command_interaction and command is None)):
        (
            user_installation,
            application,
            bot,
            command,
        ) = await guild_user_installation(
            session,
            access.guild,
            actor,
            payload,
            application_ref,
            authority_parent,
            source_lineage,
            authority_domain=authority_domain,
        )
        if user_installation is not None:
            installation = None
    return require_resolved_interaction_application(
        command,
        installation,
        user_installation,
        None,
        application,
        bot,
        "guild",
        command_interaction=command_interaction,
    )


async def resolve_private_interaction_application(
    session: AsyncSession,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
    authority_parent: BotInteraction | None,
    source_lineage: tuple[str, int, str, int] | None = None,
    *,
    authority_domain: str,
) -> InteractionApplicationContext:
    app_id, app_domain = application_ref
    application = await session.get(BotApplication, application_ref)
    bot = (
        await session.get(
            User,
            (application.bot_user_id, application.bot_user_domain),
        )
        if application is not None
        else None
    )
    interaction_context: Literal["guild", "bot_dm", "private_channel"] = "private_channel"
    if bot is not None:
        conversation = await session.get(
            DMConversation,
            (access.channel.id, access.channel.origin_domain),
        )
        interaction_context = private_interaction_context(
            access.participants,
            bot,
            direct=conversation is not None and conversation.type == "direct",
        )
    selected_type = source_lineage[0] if source_lineage is not None else payload.integration_type
    statement = select(BotUserInstallation).where(
        BotUserInstallation.application_id == app_id,
        BotUserInstallation.application_domain == app_domain,
        usable_user_installation(current_instance_domain=authority_domain),
        BotUserInstallation.granted_scopes.contains(["applications.commands"]),
        BotUserInstallation.contexts.contains([interaction_context]),
    )
    if selected_type not in {None, "user_install"}:
        statement = statement.where(BotUserInstallation.id == -1)
    if source_lineage is not None:
        statement = statement.where(
            BotUserInstallation.id == source_lineage[1],
            BotUserInstallation.grant_revision == source_lineage[3],
        )
    else:
        statement = statement.where(
            BotUserInstallation.user_id == actor.id,
            BotUserInstallation.user_domain == actor.origin_domain,
        )
    if authority_parent is not None:
        statement = statement.where(
            BotUserInstallation.id
            == (
                authority_parent.user_installation_id
                if authority_parent.user_installation_id is not None
                else -1
            )
        )
    user_installation = await session.scalar(statement) if bot is not None else None
    dm_capability: BotDMCapability | None = None
    if (
        application is not None
        and bot is not None
        and interaction_context == "bot_dm"
        and (
            selected_type == "dm_capability"
            or authority_parent is not None
            and authority_parent.dm_capability_id is not None
        )
    ):
        capability_statement = select(BotDMCapability).where(
            BotDMCapability.application_id == app_id,
            BotDMCapability.application_domain == app_domain,
            BotDMCapability.bot_user_id == bot.id,
            BotDMCapability.bot_user_domain == bot.origin_domain,
            BotDMCapability.target_user_id == actor.id,
            BotDMCapability.target_user_domain == actor.origin_domain,
            BotDMCapability.conversation_id == access.channel.id,
            BotDMCapability.conversation_domain == access.channel.origin_domain,
            BotDMCapability.authority_domain == access.channel.origin_domain,
            usable_dm_capability(at=datetime.now(UTC)),
            BotDMCapability.granted_scopes.contains(["applications.commands"]),
            BotDMCapability.granted_intents.contains(["interactions"]),
        )
        if payload.dm_capability_id is not None:
            capability_statement = capability_statement.where(
                BotDMCapability.grant_id == payload.dm_capability_id,
                BotDMCapability.revision == payload.dm_capability_revision,
            )
        if source_lineage is not None:
            capability_statement = capability_statement.where(
                BotDMCapability.id == source_lineage[1],
                BotDMCapability.authority_domain == source_lineage[2],
                BotDMCapability.revision == source_lineage[3],
            )
        if authority_parent is not None:
            capability_statement = capability_statement.where(
                BotDMCapability.id
                == (
                    authority_parent.dm_capability_id
                    if authority_parent.dm_capability_id is not None
                    else -1
                )
            )
        dm_capability = await session.scalar(capability_statement)
        if dm_capability is not None:
            runtime_target = await session.get(
                BotApplicationTarget,
                (app_id, app_domain, access.channel.origin_domain),
            )
            try:
                stored_bot_dm_capability_payload(dm_capability)
            except ValueError:
                dm_capability = None
            if dm_capability is not None and not dm_capability_runtime_ready(
                application,
                runtime_target,
                dm_capability,
                target_domain=access.channel.origin_domain,
            ):
                dm_capability = None
    command = None
    command_interaction = payload.interaction_type in {"command", "autocomplete"}
    if command_interaction:
        command_integration_type = (
            f"{dm_capability.source_kind}_install" if dm_capability is not None else "user_install"
        )
        command = await session.scalar(
            select(ApplicationCommand).where(
                ApplicationCommand.application_id == app_id,
                ApplicationCommand.application_domain == app_domain,
                ApplicationCommand.name == payload.command_name,
                ApplicationCommand.type == payload.command_type,
                ApplicationCommand.state == "active",
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.contexts.contains([interaction_context]),
                ApplicationCommand.integration_types.contains([command_integration_type]),
                *(
                    [
                        or_(
                            (
                                (ApplicationCommand.source_id == payload.command_id)
                                & (ApplicationCommand.source_domain == app_domain)
                            ),
                            (
                                (ApplicationCommand.id == payload.command_id)
                                & ApplicationCommand.source_id.is_(None)
                            ),
                        )
                    ]
                    if payload.command_id is not None
                    else []
                ),
            )
        )
    return require_resolved_interaction_application(
        command,
        None,
        user_installation,
        dm_capability,
        application,
        bot,
        interaction_context,
        command_interaction=command_interaction,
    )


def require_resolved_interaction_application(
    command: ApplicationCommand | None,
    installation: BotInstallation | None,
    user_installation: BotUserInstallation | None,
    dm_capability: BotDMCapability | None,
    application: BotApplication | None,
    bot: User | None,
    interaction_context: Literal["guild", "bot_dm", "private_channel"],
    *,
    command_interaction: bool,
) -> InteractionApplicationContext:
    if (
        application is None
        or bot is None
        or application.status != "active"
        or bot.account_type != "bot"
        or bot.disabled_at is not None
        or sum(item is not None for item in (installation, user_installation, dm_capability)) != 1
        or (command_interaction and command is None)
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "APPLICATION_COMMAND_NOT_FOUND"},
        )
    return InteractionApplicationContext(
        command=command,
        installation=installation,
        user_installation=user_installation,
        dm_capability=dm_capability,
        application=application,
        bot=bot,
        interaction_context=interaction_context,
    )


async def resolve_interaction_application(
    session: AsyncSession,
    access: ChannelAccess,
    actor: User,
    payload: InteractionCreate,
    application_ref: tuple[int, str],
    authority_parent: BotInteraction | None,
    source_lineage: tuple[str, int, str, int] | None = None,
    *,
    authority_domain: str,
) -> InteractionApplicationContext:
    if source_lineage is not None and source_lineage[2] != access.channel.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    if access.guild is not None:
        return await resolve_guild_interaction_application(
            session,
            access,
            actor,
            payload,
            application_ref,
            authority_parent,
            source_lineage,
            authority_domain=authority_domain,
        )
    return await resolve_private_interaction_application(
        session,
        access,
        actor,
        payload,
        application_ref,
        authority_parent,
        source_lineage,
        authority_domain=authority_domain,
    )


@dataclass(frozen=True, slots=True)
class InteractionCreateContext:
    session: AsyncSession
    redis: Redis
    snowflake: SnowflakeGenerator
    settings: Settings
    access: ChannelAccess
    actor: User
    payload: InteractionCreate
    source: InteractionSource
    application: InteractionApplicationContext
    invoker_policy: InteractionInvokerPolicy
    invocation_permissions: int | None
    invocation_options: InteractionInvocationOptions = field(
        default_factory=default_interaction_invocation_options
    )


@dataclass(frozen=True, slots=True)
class PreparedInteractionInvocation:
    message: Message | None
    source_response_id: int | None
    source_view_version: int | None
    target_ref: str | None
    target_id: str | None
    resolved: dict[str, object]
    encrypted: bool
    attachment_ids: list[int]
    attachment_file_types: dict[int, list[str]]
    event_snapshot: dict[str, object]


def selected_interaction_installation(
    application: InteractionApplicationContext,
) -> InteractionInstallation:
    selected = tuple(
        item
        for item in (
            application.installation,
            application.user_installation,
            application.dm_capability,
        )
        if item is not None
    )
    if len(selected) != 1:
        raise RuntimeError("interaction application lost its exact installation lineage")
    return selected[0]


def inherited_interaction_authorizing_owners(
    context: InteractionCreateContext,
) -> dict[str, str] | None:
    """Retain the immutable owners attached to a component or modal lineage."""

    parent = context.source.authority_parent
    if parent is not None:
        return stored_authorizing_integration_owners(parent)
    message = context.source.message
    metadata = message.interaction_metadata if message is not None else None
    if isinstance(metadata, dict) and "authorizing_integration_owners" in metadata:
        return normalize_authorizing_integration_owners(metadata["authorizing_integration_owners"])
    return None


async def command_authorizing_integration_owners(
    context: InteractionCreateContext,
) -> dict[str, str]:
    """Snapshot every install context that authorized this exact invocation."""

    selected = selected_interaction_installation(context.application)
    selected_owners = installation_authorizing_integration_owners(selected)
    try:
        inherited = inherited_interaction_authorizing_owners(context)
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_LINEAGE_INVALID"},
        ) from exc
    if inherited is not None:
        # Exact message/modal lineage remains the security authority. A stale or
        # forged projection must never replace the actual selected owner.
        if any(inherited.get(key) != value for key, value in selected_owners.items()):
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_LINEAGE_INVALID"},
            )
        return inherited

    owners = dict(selected_owners)
    command = context.application.command
    if command is None or context.payload.interaction_type not in {"command", "autocomplete"}:
        return owners
    supported = set(command.integration_types or [])
    application = context.application.application
    guild = context.access.guild
    if guild is not None:
        if USER_INSTALL_OWNER in supported and USER_INSTALL_OWNER not in owners:
            user_installation = await context.session.scalar(
                select(BotUserInstallation).where(
                    BotUserInstallation.application_id == application.id,
                    BotUserInstallation.application_domain == application.origin_domain,
                    BotUserInstallation.user_id == context.actor.id,
                    BotUserInstallation.user_domain == context.actor.origin_domain,
                    usable_user_installation(
                        current_instance_domain=context.settings.domain,
                    ),
                    BotUserInstallation.granted_scopes.contains(["applications.commands"]),
                    BotUserInstallation.contexts.contains(["guild"]),
                )
            )
            if user_installation is not None:
                owners.update(installation_authorizing_integration_owners(user_installation))
        if GUILD_INSTALL_OWNER in supported and GUILD_INSTALL_OWNER not in owners:
            guild_installation = await context.session.scalar(
                select(BotInstallation).where(
                    BotInstallation.application_id == application.id,
                    BotInstallation.application_domain == application.origin_domain,
                    BotInstallation.guild_id == guild.id,
                    BotInstallation.guild_domain == guild.origin_domain,
                    BotInstallation.bot_user_id == context.application.bot.id,
                    BotInstallation.bot_user_domain == context.application.bot.origin_domain,
                    BotInstallation.status == "active",
                    BotInstallation.revoked_at.is_(None),
                    BotInstallation.granted_scopes.contains(["applications.commands"]),
                    installation_has_membership(),
                )
            )
            if guild_installation is not None and await installation_allows_channel(
                context.session,
                guild_installation,
                context.access.channel,
            ):
                owners.update(installation_authorizing_integration_owners(guild_installation))
        return normalize_authorizing_integration_owners(owners)

    if context.application.interaction_context != "bot_dm":
        return normalize_authorizing_integration_owners(owners)
    if USER_INSTALL_OWNER in supported and USER_INSTALL_OWNER not in owners:
        user_installation = await context.session.scalar(
            select(BotUserInstallation).where(
                BotUserInstallation.application_id == application.id,
                BotUserInstallation.application_domain == application.origin_domain,
                BotUserInstallation.user_id == context.actor.id,
                BotUserInstallation.user_domain == context.actor.origin_domain,
                usable_user_installation(
                    current_instance_domain=context.settings.domain,
                ),
                BotUserInstallation.granted_scopes.contains(["applications.commands"]),
                BotUserInstallation.contexts.contains(["bot_dm"]),
            )
        )
        if user_installation is not None:
            owners.update(installation_authorizing_integration_owners(user_installation))
    if GUILD_INSTALL_OWNER in supported and GUILD_INSTALL_OWNER not in owners:
        guild_capability = await context.session.scalar(
            select(BotDMCapability)
            .where(
                BotDMCapability.application_id == application.id,
                BotDMCapability.application_domain == application.origin_domain,
                BotDMCapability.bot_user_id == context.application.bot.id,
                BotDMCapability.bot_user_domain == context.application.bot.origin_domain,
                BotDMCapability.target_user_id == context.actor.id,
                BotDMCapability.target_user_domain == context.actor.origin_domain,
                BotDMCapability.conversation_id == context.access.channel.id,
                BotDMCapability.conversation_domain == context.access.channel.origin_domain,
                BotDMCapability.authority_domain == context.access.channel.origin_domain,
                BotDMCapability.source_kind == "guild",
                usable_dm_capability(at=datetime.now(UTC)),
                BotDMCapability.granted_scopes.contains(["applications.commands"]),
                BotDMCapability.granted_intents.contains(["interactions"]),
            )
            .limit(1)
        )
        if guild_capability is not None:
            owners[GUILD_INSTALL_OWNER] = BOT_DM_GUILD_OWNER
    return normalize_authorizing_integration_owners(owners)


async def interaction_application_permissions_snapshot(
    context: InteractionCreateContext,
) -> int:
    installation = selected_interaction_installation(context.application)
    guild = context.access.guild
    if guild is None:
        permissions = Permission.ATTACH_FILES | Permission.EMBED_LINKS | Permission.MENTION_EVERYONE
        if context.application.interaction_context == "bot_dm":
            permissions |= Permission.USE_EXTERNAL_EMOJIS
        return int(permissions)
    if isinstance(installation, BotInstallation):
        live_permissions = await get_permissions(
            context.session,
            context.redis,
            guild,
            context.application.bot,
            channel=context.access.channel,
            bot_grant=bot_guild_permission_grant_from_installation(installation),
        )
        return int(live_permissions)
    return int(context.invocation_permissions or 0)


async def interaction_invoker_event_projection(
    context: InteractionCreateContext,
) -> dict[str, object]:
    guild = context.access.guild
    if guild is None:
        return {"user": user_payload(context.actor)}
    member = await context.session.get(
        GuildMember,
        (guild.id, guild.origin_domain, context.actor.id, context.actor.origin_domain),
    )
    if member is None:
        raise HTTPException(status_code=409, detail={"code": "GUILD_MEMBER_NOT_FOUND"})
    role_ids = list(
        await context.session.scalars(
            select(MemberRole.role_id)
            .where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == context.actor.id,
                MemberRole.user_domain == context.actor.origin_domain,
            )
            .order_by(MemberRole.role_id)
        )
    )
    projection = member_payload(member, context.actor, role_ids)
    projection["permissions"] = str(context.invocation_permissions or 0)
    return {"member": projection}


async def interaction_event_snapshot(
    context: InteractionCreateContext,
    source_message: Message | None,
    source_ephemeral: tuple[BotInteractionResponse, BotInteraction] | None = None,
) -> dict[str, object]:
    guild = context.access.guild
    snapshot: dict[str, object] = {
        "version": 1,
        "locale": context.invoker_policy.locale,
        "app_permissions": str(await interaction_application_permissions_snapshot(context)),
        "authorizing_integration_owners": (await command_authorizing_integration_owners(context)),
        "attachment_size_limit": context.settings.media_max_attachment_bytes,
        "entitlements": [],
        **(await interaction_invoker_event_projection(context)),
    }
    if guild is not None:
        snapshot["guild_locale"] = guild.preferred_locale
    if source_message is not None:
        snapshot["message"] = await render_message_payload(
            context.session,
            source_message,
            viewer=context.actor,
        )
    elif source_ephemeral is not None:
        response, parent = source_ephemeral
        event_projection = interaction_response_event_payload(parent, response, "CREATE")
        raw_data = event_projection.get("data")
        if not isinstance(raw_data, dict):
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_VIEW_INVALID"},
            )
        data = {str(key): value for key, value in raw_data.items()}
        created_at = response.created_at or parent.created_at
        if created_at is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_VIEW_INVALID"},
            )
        # Ephemeral responses are intentionally represented by their own safe
        # source projection.  It is Message-shaped enough for Discord-style
        # interaction handlers, but is explicitly not a durable channel row.
        snapshot["message"] = {
            "id": str(response.id),
            "origin_domain": parent.channel_domain,
            "response_id": str(response.id),
            "response_ref": f"{response.id}@{parent.channel_domain}",
            "interaction_id": str(parent.id),
            "interaction_ref": f"{parent.id}@{parent.channel_domain}",
            "channel_id": str(parent.channel_id),
            "channel_domain": parent.channel_domain,
            "channel_ref": f"{parent.channel_id}@{parent.channel_domain}",
            "author_id": str(context.application.bot.id),
            "author_domain": context.application.bot.origin_domain,
            "author": user_payload(context.application.bot),
            "application_id": str(parent.application_id),
            "application_domain": parent.application_domain,
            "application_ref": f"{parent.application_id}@{parent.application_domain}",
            "content": data.get("content"),
            "e2ee": data.get("e2ee"),
            "embeds": list(data.get("embeds", [])) if isinstance(data.get("embeds"), list) else [],
            "components": (
                list(data.get("components", [])) if isinstance(data.get("components"), list) else []
            ),
            "attachments": (
                list(data.get("attachments", []))
                if isinstance(data.get("attachments"), list)
                else []
            ),
            "poll": data.get("poll") if isinstance(data.get("poll"), dict) else None,
            "flags": int(data.get("flags", INTERACTION_EPHEMERAL_FLAG)),
            "tts": bool(data.get("tts", False)),
            "message_type": 20,
            "interaction_metadata": await interaction_message_metadata(
                context.session,
                parent,
                followup=response.sequence > 0,
            ),
            "view_version": int(data.get("view_version", 0)),
            "view_persistent": False,
            "view_expires_at": data.get("view_expires_at"),
            "mention_user_refs": [],
            "mention_role_refs": [],
            "mention_everyone": False,
            "sticker_items": [],
            "message_reference": None,
            "message_snapshots": [],
            "webhook": None,
            "created_at": created_at.isoformat(),
            "edited_at": None,
            "deleted_at": None,
            "ephemeral": True,
            "durable": False,
            "sequence": response.sequence,
            "revision": str(int(response.revision or 1)),
        }
    return snapshot


async def resolve_ephemeral_source_response(
    context: InteractionCreateContext,
    response_id: int | None,
) -> tuple[BotInteractionResponse, BotInteraction] | None:
    """Resolve the private source referenced by a component or its modal."""

    source = context.source
    if source.ephemeral_response is not None and source.ephemeral_parent is not None:
        return source.ephemeral_response, source.ephemeral_parent
    if response_id is None:
        return None
    row = (
        await context.session.execute(
            select(BotInteractionResponse, BotInteraction)
            .join(BotInteraction, BotInteraction.id == BotInteractionResponse.interaction_id)
            .where(BotInteractionResponse.id == response_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    response, parent = row
    if (
        not response.ephemeral
        or response.deleted_at is not None
        or (parent.application_id, parent.application_domain)
        != (
            context.application.application.id,
            context.application.application.origin_domain,
        )
        or (parent.user_id, parent.user_domain) != (context.actor.id, context.actor.origin_domain)
        or (parent.channel_id, parent.channel_domain)
        != (context.access.channel.id, context.access.channel.origin_domain)
        or parent.expires_at <= datetime.now(UTC)
    ):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    return response, parent


async def resolve_modal_source_message(
    context: InteractionCreateContext,
) -> tuple[Message | None, int | None, int | None]:
    source = context.source
    message = source.message
    response_id = source.ephemeral_response.id if source.ephemeral_response is not None else None
    view_version = context.payload.view_version if source.ephemeral_response is not None else None
    parent = source.modal_parent
    if parent is None:
        return message, response_id, view_version
    if parent.message_id is not None and parent.message_domain is not None:
        candidate = await context.session.get(
            Message,
            (parent.message_id, parent.message_domain),
        )
        app_ref = (
            context.application.application.id,
            context.application.application.origin_domain,
        )
        if (
            candidate is not None
            and candidate.deleted_at is None
            and (candidate.channel_id, candidate.channel_domain)
            == (context.access.channel.id, context.access.channel.origin_domain)
            and (candidate.application_id, candidate.application_domain) == app_ref
        ):
            message = candidate
    raw_response_id = parent.payload.get("response_id")
    if raw_response_id is None:
        return message, response_id, view_version
    if not str(raw_response_id).isdigit():
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_MODAL_INVALID"})
    raw_view_version = parent.payload.get("view_version")
    return (
        message,
        int(str(raw_response_id)),
        (
            int(str(raw_view_version))
            if raw_view_version is not None and str(raw_view_version).isdigit()
            else None
        ),
    )


async def validate_interaction_command_access(context: InteractionCreateContext) -> None:
    command = context.application.command
    installation = selected_interaction_installation(context.application)
    channel_nsfw = (
        bool(await effective_channel_nsfw(context.session, context.access.channel))
        if context.access.guild is not None
        and command is not None
        and command.definition.get("nsfw") is True
        else None
    )
    require_age_restricted_command(
        command,
        context.access,
        context.invoker_policy,
        channel_nsfw=channel_nsfw,
    )
    command_interaction = context.payload.interaction_type in {"command", "autocomplete"}
    if (
        context.access.guild is None
        and command_interaction
        and context.payload.command_type == "user"
    ):
        await require_dm_send(context.session, context.access, context.actor)
    guild_installed = isinstance(installation, BotInstallation)
    if context.access.guild is not None and not application_interaction_allowed(
        context.invocation_permissions or 0,
        guild_installed=guild_installed,
        interaction_type=context.payload.interaction_type,
        command_type=context.payload.command_type,
        channel_type=context.access.channel.type,
    ):
        required = application_interaction_required_permissions(
            guild_installed=guild_installed,
            interaction_type=context.payload.interaction_type,
            command_type=context.payload.command_type,
            channel_type=context.access.channel.type,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "MISSING_PERMISSIONS",
                "message": "You do not have permission to perform this action.",
                "permissions": str(int(required)),
            },
        )
    if context.access.guild is not None:
        # Application permission checks above use a captured effective mask, so
        # apply the live member-interaction guard explicitly.  User-installed
        # applications use USE_EXTERNAL_APPS for public responses rather than as
        # an invocation prerequisite, but profile quarantine blocks both forms
        # of guild application interaction.
        from app.automod.service import require_member_interactions_allowed

        blocked_action = Permission.USE_APPLICATION_COMMANDS
        if isinstance(installation, BotUserInstallation):
            blocked_action |= Permission.USE_EXTERNAL_APPS
        await require_member_interactions_allowed(
            context.session,
            context.access.guild,
            context.actor,
            blocked_action,
        )
    if (
        context.access.guild is not None
        and command is not None
        and isinstance(installation, BotInstallation)
    ):
        await require_guild_command_permission(
            context.session,
            context.access.guild,
            context.actor,
            context.access.channel,
            command,
            context.invocation_permissions or 0,
        )
    elif (
        context.access.guild is not None
        and command is not None
        and not command_default_permission_allowed(
            command.definition,
            context.invocation_permissions or 0,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_COMMAND_PERMISSION_DENIED"},
        )


async def resolve_interaction_command_target(
    context: InteractionCreateContext,
) -> tuple[str | None, str | None, dict[str, object] | None]:
    payload = context.payload
    if not (
        payload.interaction_type == "command"
        and payload.command_type in {"user", "message"}
        and payload.target_ref is not None
    ):
        return None, None, None
    return await resolve_context_command_target(
        context.session,
        context.settings,
        context.access,
        context.actor,
        cast(Literal["user", "message"], payload.command_type),
        payload.target_ref,
    )


async def validate_interaction_encryption(context: InteractionCreateContext) -> bool:
    channel = context.access.channel
    payload = context.payload
    encrypted = channel.encryption_mode == "e2ee"
    if encrypted and payload.encrypted_payload is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "E2EE_INTERACTION_PAYLOAD_REQUIRED"},
        )
    installation = selected_interaction_installation(context.application)
    if encrypted:
        if not await has_active_bot_e2ee_participation(
            context.session,
            installation,
            channel,
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        await require_owned_e2ee_sender_device(
            context.session,
            context.actor,
            payload.encrypted_payload,
            authority_domain=context.settings.domain,
            channel=channel,
        )
        try:
            validate_message_encryption_policy(
                "e2ee",
                content=None,
                e2ee=payload.encrypted_payload,
                attachment_count=len(payload.attachment_ids),
                policy_generation=channel.encryption_policy_generation,
                policy_epoch=channel.encryption_epoch,
                policy_group_id=channel.encryption_group_id,
            )
        except MessageEncryptionPolicyError as exc:
            raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    if not encrypted and payload.encrypted_payload is not None:
        raise HTTPException(status_code=422, detail={"code": "UNEXPECTED_ENCRYPTED_PAYLOAD"})
    return encrypted


async def validate_interaction_command_options(
    context: InteractionCreateContext,
    *,
    encrypted: bool,
) -> None:
    command = context.application.command
    if command is None or encrypted:
        return
    context.payload.options = validate_command_options(
        command,
        context.payload.options,
        require_complete=context.payload.interaction_type == "command",
    )
    requirements = command_channel_type_requirements(
        command,
        context.payload.options,
        local_domain=context.settings.domain,
    )
    if not requirements:
        return
    references = {reference for _, reference, _ in requirements}
    rows = (
        await context.session.execute(
            select(Channel.id, Channel.origin_domain, Channel.type).where(
                tuple_(Channel.id, Channel.origin_domain).in_(references)
            )
        )
    ).tuples()
    validate_resolved_command_channel_types(
        requirements,
        {
            (channel_id, channel_domain): channel_type
            for channel_id, channel_domain, channel_type in rows
        },
    )


async def interaction_invocation_attachments(
    context: InteractionCreateContext,
    *,
    encrypted: bool,
) -> tuple[list[int], dict[int, list[str]]]:
    payload = context.payload
    command = context.application.command
    attachment_ids = (
        list(payload.attachment_ids)
        if encrypted
        else (
            command_attachment_ids(command, payload.options)
            if payload.interaction_type == "command" and command is not None
            else []
        )
    )
    file_types = (
        command_attachment_file_types(command, payload.options)
        if attachment_ids and command is not None
        else {}
    )
    if context.source.modal_file_fields and not encrypted:
        attachment_ids, file_types = modal_attachment_file_types(context.source.modal_file_fields)
    installation = selected_interaction_installation(context.application)
    if attachment_ids and "attachments.read" not in installation.granted_scopes:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "APPLICATION_ATTACHMENT_SCOPE_REQUIRED",
                "message": "This application must be granted attachment access first.",
            },
        )
    if attachment_ids and context.access.guild is not None:
        await require_permissions(
            context.session,
            context.redis,
            context.access.guild,
            context.actor,
            Permission.ATTACH_FILES,
            channel=context.access.channel,
        )
    return attachment_ids, file_types


async def prepare_interaction_invocation(
    context: InteractionCreateContext,
) -> PreparedInteractionInvocation:
    await validate_interaction_command_access(context)
    message, response_id, view_version = await resolve_modal_source_message(context)
    payload = context.payload
    command = context.application.command
    if (
        payload.interaction_type == "autocomplete"
        and command is not None
        and payload.focused_option is not None
        and not command_supports_focused_option(command, payload.focused_option)
    ):
        raise HTTPException(status_code=422, detail={"code": "FOCUSED_OPTION_INVALID"})
    target_ref, target_id, target_resolved = await resolve_interaction_command_target(context)
    encrypted = await validate_interaction_encryption(context)
    await validate_interaction_command_options(context, encrypted=encrypted)
    attachment_ids, file_types = await interaction_invocation_attachments(
        context,
        encrypted=encrypted,
    )
    source_ephemeral = await resolve_ephemeral_source_response(context, response_id)
    event_snapshot = await interaction_event_snapshot(context, message, source_ephemeral)
    return PreparedInteractionInvocation(
        message=message,
        source_response_id=response_id,
        source_view_version=view_version,
        target_ref=target_ref,
        target_id=target_id,
        resolved=({} if encrypted else dict(target_resolved or context.source.resolved)),
        encrypted=encrypted,
        attachment_ids=attachment_ids,
        attachment_file_types=file_types,
        event_snapshot=event_snapshot,
    )


def new_interaction_record(
    context: InteractionCreateContext,
    prepared: PreparedInteractionInvocation,
    *,
    interaction_id: int,
    token: str,
    now: datetime,
) -> BotInteraction:
    payload = context.payload
    source = context.source
    application = context.application
    installation = selected_interaction_installation(application)
    command = application.command
    guild = context.access.guild
    lineage_payload: dict[str, object] = {}
    if prepared.target_ref is not None:
        lineage_payload["target_ref"] = prepared.target_ref
    if prepared.source_response_id is not None:
        lineage_payload["response_id"] = str(prepared.source_response_id)
    if prepared.source_view_version is not None:
        lineage_payload["view_version"] = prepared.source_view_version
    if source.modal_parent is not None:
        lineage_payload["triggering_interaction_id"] = str(source.modal_parent.id)
    if source.source_component is not None:
        lineage_payload["source_component"] = source.source_component
    if source.source_modal is not None:
        lineage_payload["source_modal"] = source.source_modal
    durable_payload = (
        lineage_payload
        if prepared.encrypted
        else {
            "options": payload.options,
            "focused_option": payload.focused_option,
            "component_type": source.component_type,
            "values": source.values,
            "components": source.components,
            "response_id": (
                str(prepared.source_response_id)
                if prepared.source_response_id is not None
                else None
            ),
            "view_version": prepared.source_view_version,
            "modal_response_id": (
                str(source.modal_response.id) if source.modal_response is not None else None
            ),
            "triggering_interaction_id": (
                str(source.modal_parent.id) if source.modal_parent is not None else None
            ),
            "target_ref": prepared.target_ref,
            "resolved": prepared.resolved or None,
        }
    )
    durable_payload[INTERACTION_EVENT_SNAPSHOT_KEY] = prepared.event_snapshot
    durable_payload[INTERACTION_INSTALLATION_LINEAGE_KEY] = installation_authority_lineage(
        installation
    )
    return BotInteraction(
        id=interaction_id,
        application_id=application.application.id,
        application_domain=application.application.origin_domain,
        installation_id=(
            application.installation.id if application.installation is not None else None
        ),
        user_installation_id=(
            application.user_installation.id if application.user_installation is not None else None
        ),
        dm_capability_id=(
            application.dm_capability.id if application.dm_capability is not None else None
        ),
        guild_id=guild.id if guild is not None else None,
        guild_domain=guild.origin_domain if guild is not None else None,
        channel_id=context.access.channel.id,
        channel_domain=context.access.channel.origin_domain,
        user_id=context.actor.id,
        user_domain=context.actor.origin_domain,
        interaction_type=payload.interaction_type,
        context=application.interaction_context,
        integration_type=(
            "guild_install"
            if application.installation is not None
            else "user_install"
            if application.user_installation is not None
            else "dm_capability"
        ),
        invocation_permissions=context.invocation_permissions,
        invocation_channel_type=(context.access.channel.type if guild is not None else None),
        installation_revision=int(
            installation.grant_revision
            if isinstance(installation, (BotInstallation, BotUserInstallation))
            else installation.revision
        ),
        command_id=(command.id if command is not None else None),
        command_name=command.name if command is not None else None,
        command_type=command.type if command is not None else payload.command_type,
        payload=durable_payload,
        encrypted_payload=payload.encrypted_payload if prepared.encrypted else None,
        message_id=prepared.message.id if prepared.message is not None else None,
        message_domain=(prepared.message.origin_domain if prepared.message is not None else None),
        custom_id=source.custom_id,
        token_hash=hashlib.sha256(token.encode()).digest(),
        response_grant_id=context.invocation_options.federated_response_grant_id,
        request_fingerprint=context.invocation_options.federated_request_fingerprint,
        autocomplete_generation=(
            (payload.autocomplete_generation or interaction_id)
            if payload.interaction_type == "autocomplete"
            else None
        ),
        expires_at=context.invocation_options.federated_expires_at or now + INTERACTION_LIFETIME,
        created_at=now,
    )


async def persist_interaction_record(
    context: InteractionCreateContext,
    prepared: PreparedInteractionInvocation,
    *,
    token: str,
) -> tuple[BotInteraction, list[Attachment], set[str]]:
    now = datetime.now(UTC)
    interaction = new_interaction_record(
        context,
        prepared,
        interaction_id=await context.snowflake.mint(),
        token=token,
        now=now,
    )
    context.session.add(interaction)
    modal_response = context.source.modal_response
    relay_destinations: set[str] = set()
    if modal_response is not None:
        # The locked type-9 response is the durable, one-use modal capability.
        modal_response.deleted_at = now
        if context.source.modal_parent is not None:
            relay_destinations = await queue_interaction_response_relays(
                context.session,
                context.settings,
                (context.source.modal_parent, modal_response, "DELETE"),
            )
    await context.session.flush()
    attachments: list[Attachment] = []
    if prepared.attachment_ids:
        if context.invocation_options.federated_attachments:
            resolved, attachments = await materialize_federated_interaction_attachments(
                context.session,
                context.settings,
                interaction,
                context.invocation_options.federated_attachments,
                prepared.attachment_ids,
                prepared.attachment_file_types,
                expected_encryption_mode="e2ee" if prepared.encrypted else "plaintext",
            )
        else:
            resolved, attachments = await bind_invocation_attachments(
                context.session,
                context.settings,
                context.actor,
                interaction,
                prepared.attachment_ids,
                prepared.attachment_file_types,
                expected_encryption_mode="e2ee" if prepared.encrypted else "plaintext",
            )
        prepared.resolved["attachments"] = resolved
        if not prepared.encrypted:
            stored_payload = dict(interaction.payload)
            stored_payload["resolved"] = prepared.resolved
            interaction.payload = stored_payload
    return interaction, attachments, relay_destinations


def interaction_create_event(
    context: InteractionCreateContext,
    prepared: PreparedInteractionInvocation,
    interaction: BotInteraction,
    token: str,
) -> dict[str, object]:
    payload = context.payload
    source = context.source
    application = context.application
    dm_capability = application.dm_capability
    guild = context.access.guild
    return {
        "id": str(interaction.id),
        "interaction_ref": f"{interaction.id}@{interaction.channel_domain}",
        "token": token,
        "type": payload.interaction_type,
        "context": interaction.context,
        "integration_type": interaction.integration_type,
        "application_ref": (
            f"{application.application.id}@{application.application.origin_domain}"
        ),
        "installation_id": (
            str(application.installation.id) if application.installation is not None else None
        ),
        "user_installation_id": (
            str(application.user_installation.id)
            if application.user_installation is not None
            else None
        ),
        "bot_dm_capability_id": (dm_capability.grant_id if dm_capability is not None else None),
        "bot_dm_capability_revision": (
            str(dm_capability.revision) if dm_capability is not None else None
        ),
        "installation_ref": (
            f"{dm_capability.source_installation_id}@{dm_capability.source_installation_domain}"
            if dm_capability is not None
            else None
        ),
        "installation_type": (dm_capability.source_kind if dm_capability is not None else None),
        "installation_revision": str(interaction.installation_revision),
        "guild_ref": f"{guild.id}@{guild.origin_domain}" if guild is not None else None,
        "channel_ref": f"{context.access.channel.id}@{context.access.channel.origin_domain}",
        "channel_id": str(context.access.channel.id),
        "channel_domain": context.access.channel.origin_domain,
        "message_ref": (
            f"{prepared.message.id}@{prepared.message.origin_domain}"
            if prepared.message is not None
            else None
        ),
        "target_ref": None if prepared.encrypted else prepared.target_ref,
        "target_id": prepared.target_id,
        "resolved": None if prepared.encrypted else prepared.resolved or None,
        "response_id": (
            str(prepared.source_response_id)
            if not prepared.encrypted and prepared.source_response_id is not None
            else None
        ),
        "response_ref": (
            f"{prepared.source_response_id}@{interaction.channel_domain}"
            if not prepared.encrypted and prepared.source_response_id is not None
            else None
        ),
        "view_version": None if prepared.encrypted else prepared.source_view_version,
        "custom_id": source.custom_id,
        "component_type": source.component_type,
        "focused_option": payload.focused_option,
        "autocomplete_generation": (
            str(interaction.autocomplete_generation)
            if interaction.autocomplete_generation is not None
            else None
        ),
        "values": source.values,
        "components": source.components,
        "source_component": source.source_component,
        "source_modal": source.source_modal,
        "user_ref": f"{context.actor.id}@{context.actor.origin_domain}",
        "command": (application.command.definition if application.command is not None else None),
        "command_id": str(interaction.command_id) if interaction.command_id is not None else None,
        "options": None if prepared.encrypted else payload.options,
        "encrypted_payload": payload.encrypted_payload if prepared.encrypted else None,
        "expires_at": interaction.expires_at.isoformat(),
        "ack_deadline": (interaction.created_at + timedelta(seconds=3)).isoformat(),
        "bot_user_ref": f"{application.bot.id}@{application.bot.origin_domain}",
        **prepared.event_snapshot,
    }


def interaction_dispatch_topic(context: InteractionCreateContext) -> str:
    application = context.application
    guild = context.access.guild
    if application.user_installation is not None or guild is None:
        return user_topic(application.bot.origin_domain, application.bot.id)
    return guild_topic(guild.origin_domain, guild.id)


async def persist_and_dispatch_interaction(
    context: InteractionCreateContext,
    prepared: PreparedInteractionInvocation,
) -> dict[str, object]:
    token = secrets.token_urlsafe(32)
    interaction, attachments, relay_destinations = await persist_interaction_record(
        context,
        prepared,
        token=token,
    )
    event = interaction_create_event(context, prepared, interaction, token)
    queue_interaction_create_dispatch(
        context.session,
        context.settings,
        interaction,
        topic=interaction_dispatch_topic(context),
        audience_user_ref=(f"{context.application.bot.id}@{context.application.bot.origin_domain}"),
        event=event,
    )
    await context.session.commit()
    source = context.source
    if source.modal_response is not None and source.modal_parent is not None:
        await publish_interaction_response_event(
            context.redis,
            source.modal_parent,
            source.modal_response,
            "DELETE",
        )
        await wake_interaction_response_relays(relay_destinations)
    delivered = await drain_interaction_create_dispatch_outbox(
        context.session,
        context.redis,
        context.settings,
        interaction_id=interaction.id,
    )
    if not delivered:
        await wake_interaction_create_dispatch_outbox()
    for attachment in attachments:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    return {
        "id": str(interaction.id),
        "interaction_ref": event["interaction_ref"],
        "status": interaction.status,
        "ack_deadline": event["ack_deadline"],
    }


@router.post("/channels/{channel_ref}/interactions", status_code=202)
async def create_interaction(
    channel_ref: EntityRef,
    payload: InteractionCreate,
    response: Response,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    invocation_options: InteractionInvocationOptions = Depends(
        default_interaction_invocation_options
    ),
) -> dict[str, object]:
    if not isinstance(invocation_options, InteractionInvocationOptions):
        invocation_options = default_interaction_invocation_options()
    admission = await prepare_interaction_admission(
        channel_ref,
        payload,
        response,
        auth,
        session,
        redis,
        settings,
        invocation_options,
    )
    access = admission.access
    invoker_policy = admission.invoker_policy
    invocation_permissions = admission.invocation_permissions
    remote_authority = interaction_remote_authority(access, settings.domain)
    if remote_authority is not None:
        return await proxy_remote_interaction(
            session,
            settings,
            auth,
            payload,
            admission,
            remote_authority,
        )
    source = await resolve_interaction_source(
        session,
        redis,
        settings,
        access,
        auth.user,
        payload,
        admission.application_ref,
    )
    authority_parent = source.authority_parent
    app_context = await resolve_interaction_application(
        session,
        access,
        auth.user,
        payload,
        admission.application_ref,
        authority_parent,
        (
            (
                source.installation_type,
                source.installation_id,
                source.installation_domain,
                source.installation_revision,
            )
            if source.installation_type is not None
            and source.installation_id is not None
            and source.installation_domain is not None
            and source.installation_revision is not None
            else None
        ),
        authority_domain=settings.domain,
    )
    create_context = InteractionCreateContext(
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
        access=access,
        actor=auth.user,
        payload=payload,
        source=source,
        application=app_context,
        invoker_policy=invoker_policy,
        invocation_permissions=invocation_permissions,
        invocation_options=invocation_options,
    )
    prepared = await prepare_interaction_invocation(create_context)
    return await persist_and_dispatch_interaction(create_context, prepared)


async def bot_interaction(
    session: AsyncSession,
    principal: BotPrincipal,
    interaction_id: int,
    *required_scopes: str,
    authority_domain: str,
) -> tuple[BotInteraction, InteractionInstallation]:
    row = (
        await session.execute(
            select(BotInteraction, BotInstallation, BotUserInstallation, BotDMCapability)
            .outerjoin(
                BotInstallation,
                (BotInstallation.id == BotInteraction.installation_id)
                & (BotInstallation.application_id == BotInteraction.application_id)
                & (BotInstallation.application_domain == BotInteraction.application_domain)
                & (BotInstallation.bot_user_id == principal.user.id)
                & (BotInstallation.bot_user_domain == principal.user.origin_domain)
                & (BotInstallation.guild_id == BotInteraction.guild_id)
                & (BotInstallation.guild_domain == BotInteraction.guild_domain)
                & (BotInstallation.status == "active")
                & BotInstallation.revoked_at.is_(None),
            )
            .outerjoin(
                BotUserInstallation,
                (BotUserInstallation.id == BotInteraction.user_installation_id)
                & (BotUserInstallation.application_id == BotInteraction.application_id)
                & (BotUserInstallation.application_domain == BotInteraction.application_domain)
                & usable_user_installation(current_instance_domain=authority_domain),
            )
            .outerjoin(
                BotDMCapability,
                (BotDMCapability.id == BotInteraction.dm_capability_id)
                & (BotDMCapability.application_id == BotInteraction.application_id)
                & (BotDMCapability.application_domain == BotInteraction.application_domain)
                & (BotDMCapability.bot_user_id == principal.user.id)
                & (BotDMCapability.bot_user_domain == principal.user.origin_domain)
                & (BotDMCapability.target_user_id == BotInteraction.user_id)
                & (BotDMCapability.target_user_domain == BotInteraction.user_domain)
                & (BotDMCapability.conversation_id == BotInteraction.channel_id)
                & (BotDMCapability.conversation_domain == BotInteraction.channel_domain)
                & (BotDMCapability.authority_domain == BotInteraction.channel_domain)
                & usable_dm_capability(at=datetime.now(UTC)),
            )
            .where(
                BotInteraction.id == interaction_id,
                BotInteraction.application_id == principal.application.id,
                BotInteraction.application_domain == principal.application.origin_domain,
                or_(
                    BotInstallation.id.is_not(None),
                    BotUserInstallation.id.is_not(None),
                    BotDMCapability.id.is_not(None),
                ),
                or_(
                    BotInteraction.installation_id.is_(None),
                    installation_has_membership(),
                ),
            )
            .with_for_update(of=BotInteraction)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    interaction = row[0]
    installations = tuple(
        item
        for item in row[1:]
        if isinstance(item, (BotInstallation, BotUserInstallation, BotDMCapability))
    )
    if len(installations) != 1:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    installation = installations[0]
    provided_token = principal.interaction_token
    if (
        interaction.token_hash is None
        or not isinstance(provided_token, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", provided_token) is None
        or not secrets.compare_digest(
            interaction.token_hash,
            hashlib.sha256(provided_token.encode()).digest(),
        )
    ):
        raise HTTPException(status_code=401, detail={"code": "INTERACTION_TOKEN_INVALID"})
    if isinstance(installation, BotDMCapability):
        try:
            source_ref = EntityRef(principal.installation_ref or "")
            stored_bot_dm_capability_payload(installation)
        except (ValueError, TypeError):
            raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"}) from None
        runtime_target = await session.get(
            BotApplicationTarget,
            (
                interaction.application_id,
                interaction.application_domain,
                interaction.channel_domain,
            ),
            populate_existing=True,
        )
        if (
            principal.dm_capability_grant_id != installation.grant_id
            or principal.dm_capability_revision != installation.revision
            or principal.token.dm_capability_id != installation.id
            or principal.token.dm_capability_revision != installation.revision
            or source_ref.domain is None
            or (source_ref.id, source_ref.domain)
            != (
                installation.source_installation_id,
                installation.source_installation_domain,
            )
            or principal.installation_type != installation.source_kind
            or interaction.integration_type != "dm_capability"
            or interaction.context != "bot_dm"
            or not dm_capability_runtime_ready(
                principal.application,
                runtime_target,
                installation,
                target_domain=interaction.channel_domain,
            )
        ):
            raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    elif principal.dm_capability_grant_id is not None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_NOT_FOUND"})
    if interaction.encrypted_payload is not None:
        channel = await session.get(
            Channel,
            (interaction.channel_id, interaction.channel_domain),
        )
        if channel is None:
            raise HTTPException(
                status_code=409,
                detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
            )
        await require_bot_e2ee_worker_participation(
            session,
            installation,
            channel,
            principal.worker.id,
        )
    for scope in required_scopes:
        require_interaction_installation_scope(
            installation,
            interaction,
            scope,
        )
    if interaction.expires_at <= datetime.now(UTC):
        interaction.status = "expired"
        await session.commit()
        raise HTTPException(status_code=410, detail={"code": "INTERACTION_EXPIRED"})
    if (
        interaction.status == "pending"
        and interaction.created_at is not None
        and interaction.created_at + timedelta(seconds=3) <= datetime.now(UTC)
    ):
        interaction.status = "failed"
        await session.commit()
        raise HTTPException(status_code=410, detail={"code": "INTERACTION_ACK_TIMEOUT"})
    return interaction, installation


def _interaction_attachment_owner_matches(
    attachment: Attachment,
    installation: InteractionInstallation,
) -> bool:
    if isinstance(installation, BotInstallation):
        return (
            attachment.bot_installation_id == installation.id
            and attachment.bot_user_installation_id is None
            and attachment.bot_dm_capability_id is None
        )
    if isinstance(installation, BotUserInstallation):
        return (
            attachment.bot_user_installation_id == installation.id
            and attachment.bot_installation_id is None
            and attachment.bot_dm_capability_id is None
        )
    return (
        attachment.bot_dm_capability_id == installation.id
        and attachment.bot_installation_id is None
        and attachment.bot_user_installation_id is None
    )


def interaction_attachment_encryption_mode(
    interaction: BotInteraction,
) -> Literal["plaintext", "e2ee"]:
    return "e2ee" if interaction.encrypted_payload is not None else "plaintext"


def interaction_response_envelope_id(
    envelope: object,
    *,
    authority_domain: str,
) -> int | None:
    if not isinstance(envelope, dict) or "response_ref" not in envelope:
        return None
    try:
        response_ref = EntityRef(str(envelope["response_ref"])).resolve(authority_domain)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        ) from None
    if response_ref[1] != authority_domain or response_ref[0] <= 0:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        )
    return response_ref[0]


def require_interaction_response_e2ee_binding(
    interaction: BotInteraction,
    envelope: object,
    *,
    response_id: int,
    sequence: int,
    revision: int,
    callback_type: int,
    attachment_ids: list[int],
) -> None:
    """Require response-v1's public identity to match the exact stored revision."""

    if not isinstance(envelope, dict):
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_RESPONSE_E2EE_REQUIRED"},
        )
    required_fields = {
        "version",
        "protocol",
        "suite",
        "group_id",
        "policy_generation",
        "epoch",
        "sender_device_id",
        "operation",
        "ciphertext",
        *INTERACTION_RESPONSE_ENVELOPE_FIELDS,
    }
    has_contract = set(envelope) >= INTERACTION_CONTRACT_ENVELOPE_FIELDS
    allowed_fields = (
        required_fields
        | ({"target_message"} if revision > 1 else set())
        | (set(INTERACTION_CONTRACT_ENVELOPE_FIELDS) if has_contract else set())
    )
    if set(envelope) != allowed_fields:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        )
    if callback_type == 9 and not has_contract or callback_type == 8 and has_contract:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        )
    if has_contract:
        try:
            contract = validate_interaction_routing_contract(
                envelope.get("interaction_contract"),
                callback_type=callback_type,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
            ) from exc
        if envelope.get("interaction_contract_digest") != interaction_routing_contract_digest(
            contract
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
            )
    authority = interaction.channel_domain
    try:
        interaction_ref = EntityRef(str(envelope.get("interaction_ref", ""))).resolve(authority)
        response_ref = EntityRef(str(envelope.get("response_ref", ""))).resolve(authority)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        ) from None
    attachment_refs = sorted(f"{item}@{authority}" for item in attachment_ids)
    expected_operation = "create" if revision == 1 else "edit"
    if (
        interaction_ref != (interaction.id, authority)
        or response_ref != (response_id, authority)
        or envelope.get("sequence") != str(sequence)
        or envelope.get("revision") != str(revision)
        or envelope.get("callback_type") != callback_type
        or envelope.get("attachment_refs") != attachment_refs
        or envelope.get("operation") != expected_operation
        or (expected_operation == "create" and envelope.get("target_message") is not None)
        or (
            expected_operation == "edit"
            and envelope.get("target_message") != f"{response_id}@{authority}"
        )
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
        )


def interaction_response_attachment_ids(
    stored: BotInteractionResponse,
    replacement: list[int] | None,
) -> list[int]:
    if replacement is not None:
        return replacement
    raw = stored.payload.get("attachments", [])
    if not isinstance(raw, list):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    result: list[int] = []
    for item in raw:
        raw_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(raw_id, str) or not raw_id.isascii() or not raw_id.isdecimal():
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_VIEW_INVALID"},
            )
        result.append(int(raw_id))
    return result


async def require_available_interaction_response_id(
    session: AsyncSession,
    envelope: object,
    *,
    authority_domain: str,
) -> int:
    response_id = interaction_response_envelope_id(
        envelope,
        authority_domain=authority_domain,
    )
    if response_id is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_RESPONSE_E2EE_REQUIRED"},
        )
    await session.scalar(select(func.pg_advisory_xact_lock(response_id)))
    if await session.get(BotInteractionResponse, response_id) is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_RESPONSE_ID_CONFLICT"},
        )
    return response_id


async def require_interaction_response_encryption(
    session: AsyncSession,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    *,
    content: object,
    e2ee: object,
    attachment_count: int,
) -> None:
    """Apply the authoritative room policy to public and private responses."""

    # The room may have switched to E2EE after the interaction was created.
    # Revalidate every response against current authority state, including
    # isolated ephemeral responses and edits of plaintext interactions.
    channel = await session.get(Channel, (interaction.channel_id, interaction.channel_domain))
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    try:
        validate_message_encryption_policy(
            channel.encryption_mode,
            content=content,
            e2ee=e2ee,
            attachment_count=attachment_count,
            policy_generation=channel.encryption_policy_generation,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    except MessageEncryptionPolicyError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    if channel.encryption_mode != "e2ee":
        return
    if not isinstance(e2ee, dict):
        raise HTTPException(
            status_code=409,
            detail={"code": "BOT_E2EE_PARTICIPANT_REQUIRED"},
        )
    await require_bot_e2ee_participation(
        session,
        installation,
        channel,
        e2ee.get("sender_device_id") if isinstance(e2ee.get("sender_device_id"), str) else None,
        worker_id=principal.worker.id,
    )


async def require_interaction_response_read_encryption(
    session: AsyncSession,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    stored: BotInteractionResponse,
    device_id: str | None,
) -> None:
    """Fence response ciphertext to the selected device and its history floor."""

    channel = await session.get(Channel, (interaction.channel_id, interaction.channel_domain))
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.encryption_mode != "e2ee" and not channel.e2ee_required:
        return
    participation, _ = await require_bot_e2ee_participation(
        session,
        installation,
        channel,
        device_id if isinstance(device_id, str) else None,
        worker_id=principal.worker.id,
    )
    if participation.history_floor_message_id is None:
        return
    if participation.history_floor_message_domain is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"},
        )
    floor = await session.get(
        Message,
        (
            participation.history_floor_message_id,
            participation.history_floor_message_domain,
        ),
    )
    if floor is None or floor.created_at is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"},
        )
    if stored.message_id is not None and stored.message_domain is not None:
        protected = await session.get(Message, (stored.message_id, stored.message_domain))
        after_floor = (
            protected is not None
            and protected.created_at is not None
            and (
                protected.created_at > floor.created_at
                or (
                    protected.created_at == floor.created_at
                    and (protected.id, protected.origin_domain) > (floor.id, floor.origin_domain)
                )
            )
        )
    else:
        protected_at = stored.created_at or interaction.created_at
        after_floor = protected_at is not None and protected_at > floor.created_at
    if not after_floor:
        raise HTTPException(
            status_code=404,
            detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"},
        )


async def require_owned_interaction_response_attachments(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    installation: InteractionInstallation,
    interaction: BotInteraction,
    attachment_ids: list[int],
) -> None:
    """Validate response-upload ownership for either installation type."""

    if not attachment_ids:
        return
    channel = await session.get(
        Channel,
        (interaction.channel_id, interaction.channel_domain),
    )
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    expected_encryption_mode = "e2ee" if channel.encryption_mode == "e2ee" else "plaintext"
    rows = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.id.in_(attachment_ids),
                Attachment.origin_domain == settings.domain,
                Attachment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    if len(rows) != len(set(attachment_ids)) or any(
        not _interaction_attachment_owner_matches(attachment, installation)
        or (attachment.uploader_id, attachment.uploader_domain)
        != (principal.user.id, principal.user.origin_domain)
        or attachment.encryption_mode != expected_encryption_mode
        for attachment in rows
    ):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})


async def sync_ephemeral_response_attachments(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    installation: InteractionInstallation,
    interaction: BotInteraction,
    stored: BotInteractionResponse,
    attachment_ids: list[int],
) -> tuple[list[dict[str, object]], list[Attachment], list[Attachment]]:
    """Atomically retain, add, and remove private response attachments."""

    desired = set(attachment_ids)
    existing_ids = set(
        await session.scalars(
            select(Attachment.id).where(
                Attachment.interaction_response_id == stored.id,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    # Keep the global tombstone -> attachment order used by every media bind.
    for attachment_id in sorted(desired | existing_ids):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    current = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.interaction_response_id == stored.id,
                Attachment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    current_by_id = {item.id: item for item in current}
    added: list[Attachment] = []
    for attachment_id in sorted(desired):
        if attachment_id in current_by_id:
            continue
        attachment = await finalize_attachment(
            session,
            settings,
            principal.user,
            attachment_id,
            required_purpose="attachment",
        )
        if (
            not _interaction_attachment_owner_matches(attachment, installation)
            or attachment.message_id is not None
            or attachment.message_domain is not None
            or attachment.interaction_id is not None
            or attachment.interaction_response_id is not None
            or attachment.asset_binding is not None
            or attachment.report_id is not None
            or attachment.encryption_mode != interaction_attachment_encryption_mode(interaction)
        ):
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        attachment.interaction_response_id = stored.id
        current_by_id[attachment.id] = attachment
        added.append(attachment)
    removed: list[Attachment] = []
    for attachment in current:
        if attachment.id not in desired:
            await discard_attachment(session, settings, attachment)
            removed.append(attachment)
            current_by_id.pop(attachment.id, None)
    rendered = [
        attachment_payload(current_by_id[attachment_id], include_lifecycle=False)
        for attachment_id in sorted(current_by_id)
    ]
    return rendered, added, removed


async def discard_ephemeral_response_attachments(
    session: AsyncSession,
    settings: Settings,
    stored: BotInteractionResponse,
) -> list[Attachment]:
    ids = set(
        await session.scalars(
            select(Attachment.id).where(
                Attachment.interaction_response_id == stored.id,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    for attachment_id in sorted(ids):
        await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.interaction_response_id == stored.id,
                Attachment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    for attachment in attachments:
        await discard_attachment(session, settings, attachment)
    return attachments


async def interaction_input_attachment(
    session: AsyncSession,
    settings: Settings,
    interaction: BotInteraction,
    attachment_ref: EntityRef,
) -> Attachment:
    attachment_id, attachment_domain = attachment_ref.resolve(settings.domain)
    attachment = await session.scalar(
        select(Attachment).where(
            Attachment.id == attachment_id,
            Attachment.origin_domain == attachment_domain,
            Attachment.interaction_id == interaction.id,
            Attachment.deleted_at.is_(None),
        )
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    return attachment


async def require_interaction_attachment_e2ee_device(
    session: AsyncSession,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    device_id: str | None,
) -> Channel:
    """Bind encrypted interaction media to the worker's current MLS device."""

    channel = await session.get(Channel, (interaction.channel_id, interaction.channel_domain))
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.encryption_mode == "e2ee" or channel.e2ee_required:
        await require_bot_e2ee_participation(
            session,
            installation,
            channel,
            device_id if isinstance(device_id, str) else None,
            worker_id=principal.worker.id,
        )
    return channel


@router.get("/bots/interactions/{interaction_id}/attachments/{attachment_ref}")
async def get_interaction_input_attachment(
    interaction_id: int,
    attachment_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        "attachments.read",
        authority_domain=settings.domain,
    )
    await require_interaction_attachment_e2ee_device(
        session,
        principal,
        interaction,
        installation,
        e2ee_device_id,
    )
    attachment = await interaction_input_attachment(session, settings, interaction, attachment_ref)
    return attachment_payload(attachment)


@router.get("/bots/interactions/{interaction_id}/attachments/{attachment_ref}/{variant}")
async def download_interaction_input_attachment(
    interaction_id: int,
    attachment_ref: EntityRef,
    variant: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> RedirectResponse:
    if variant not in PRIVATE_INTERACTION_MEDIA_VARIANTS:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        "attachments.read",
        authority_domain=settings.domain,
    )
    await require_interaction_attachment_e2ee_device(
        session,
        principal,
        interaction,
        installation,
        e2ee_device_id,
    )
    attachment = await interaction_input_attachment(session, settings, interaction, attachment_ref)
    if attachment.origin_domain != settings.domain:
        grant = await session.scalar(
            select(FederatedInteractionAttachmentGrant).where(
                FederatedInteractionAttachmentGrant.attachment_id == attachment.id,
                FederatedInteractionAttachmentGrant.attachment_domain == attachment.origin_domain,
                FederatedInteractionAttachmentGrant.destination_domain == settings.domain,
                FederatedInteractionAttachmentGrant.interaction_id == interaction.id,
                FederatedInteractionAttachmentGrant.interaction_domain == settings.domain,
                FederatedInteractionAttachmentGrant.expires_at > datetime.now(UTC),
            )
        )
        if grant is None:
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        if grant.admission_grant_id is None:
            raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
        try:
            upstream = await signed_request(
                session,
                settings,
                "GET",
                attachment.origin_domain,
                f"/_kaede/v1/interactions/attachments/{attachment.id}/{variant}",
                query={
                    "grant_id": grant.grant_id,
                    "response_grant_id": grant.admission_grant_id,
                    "interaction_id": str(interaction.id),
                    "interaction_domain": settings.domain,
                },
                request_timeout=8,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "REMOTE_MEDIA_UNAVAILABLE"},
            ) from None
        raw = decode_federation_response_json(upstream)
        location = raw.get("location") if isinstance(raw, dict) else None
        media_origin = raw.get("media_origin") if isinstance(raw, dict) else None
        if upstream.status_code != 200:
            raise HTTPException(status_code=503, detail={"code": "REMOTE_MEDIA_UNAVAILABLE"})
        return private_media_proxy_redirect(location, media_origin)
    return redirect_to_object(settings, attachment, variant, public=False)


@federation_router.get("/_kaede/v1/interactions/attachments/{attachment_id}/{variant}")
async def federation_get_interaction_input_attachment(
    attachment_id: int,
    variant: str,
    grant_id: str = Query(min_length=32, max_length=64),
    response_grant_id: str = Query(min_length=32, max_length=64),
    interaction_id: int = Query(gt=0),
    interaction_domain: str = Query(min_length=1, max_length=253),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if variant not in PRIVATE_INTERACTION_MEDIA_VARIANTS:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "interaction-input-media",
        capacity=240,
        refill_per_minute=120,
    )
    interaction_domain = normalize_domain(interaction_domain)
    if interaction_domain != principal.origin:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    now = datetime.now(UTC)
    admission = await session.scalar(
        select(FederatedInteractionAdmissionGrant)
        .where(
            FederatedInteractionAdmissionGrant.grant_id == response_grant_id,
            FederatedInteractionAdmissionGrant.authority_domain == principal.origin,
            FederatedInteractionAdmissionGrant.expires_at > now,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if admission is None or (
        admission.interaction_id is not None
        and (admission.interaction_id, admission.interaction_domain)
        != (interaction_id, interaction_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    await lock_media_tombstone_ref(session, attachment_id, settings.domain)
    grant = await session.scalar(
        select(FederatedInteractionAttachmentGrant)
        .where(
            FederatedInteractionAttachmentGrant.grant_id == grant_id,
            FederatedInteractionAttachmentGrant.attachment_id == attachment_id,
            FederatedInteractionAttachmentGrant.attachment_domain == settings.domain,
            FederatedInteractionAttachmentGrant.destination_domain == principal.origin,
            FederatedInteractionAttachmentGrant.admission_grant_id == response_grant_id,
            FederatedInteractionAttachmentGrant.expires_at > now,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if grant is not None and (
        (grant.user_id, grant.user_domain) != (admission.user_id, admission.user_domain)
        or (grant.channel_id, grant.channel_domain)
        != (admission.channel_id, admission.channel_domain)
        or (
            grant.interaction_id is not None
            and (grant.interaction_id, grant.interaction_domain)
            != (interaction_id, interaction_domain)
        )
    ):
        grant = None
    attachment: Attachment | None = (
        await session.scalar(
            select(Attachment).where(
                Attachment.id == attachment_id,
                Attachment.origin_domain == settings.domain,
                Attachment.deleted_at.is_(None),
                Attachment.finalized_at.is_not(None),
                Attachment.scan_status.in_(("clean", "encrypted")),
            )
        )
        if grant is not None
        else None
    )
    if attachment is None or grant is None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    metadata = attachment_payload(attachment, include_lifecycle=False)
    metadata["content_sha256"] = attachment.content_sha256
    if federated_attachment_metadata_fingerprint(metadata) != grant.metadata_fingerprint:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if admission.interaction_id is None:
        admission.interaction_id = interaction_id
        admission.interaction_domain = interaction_domain
    if grant.interaction_id is None:
        grant.interaction_id = interaction_id
        grant.interaction_domain = interaction_domain
    grant.consumed_at = grant.consumed_at or now
    await session.commit()
    redirect = redirect_to_object(settings, attachment, variant, public=False)
    location = redirect.headers.get("location")
    media_origin = redirect.headers.get(MEDIA_ORIGIN_HEADER)
    if location is None or media_origin is None:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"})
    return {"location": location, "media_origin": media_origin}


@router.post("/bots/interactions/{interaction_id}/attachments", status_code=201)
async def create_interaction_attachment_ticket(
    interaction_id: int,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        "attachments.write",
        authority_domain=settings.domain,
    )
    if interaction.status not in {"pending", "deferred", "responded"}:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_EXPIRED"})
    channel = await require_interaction_attachment_e2ee_device(
        session,
        principal,
        interaction,
        installation,
        e2ee_device_id,
    )
    expected_encryption_mode = "e2ee" if channel.encryption_mode == "e2ee" else "plaintext"
    if payload.encryption_mode != expected_encryption_mode:
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    await enforce_keyed_rate_limit(
        redis,
        response,
        INTERACTION_ATTACHMENT_LIMIT,
        identity=(
            f"{principal.application.origin_domain}:{principal.application.id}:{installation.id}"
        ),
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=int(payload.size),
        encryption_mode=payload.encryption_mode,
        encryption_protocol=payload.encryption_protocol,
        duration_secs=payload.duration_secs,
        waveform=payload.waveform,
        bot_installation=(
            installation if isinstance(installation, (BotInstallation, BotDMCapability)) else None
        ),
        bot_user_installation=(
            installation if isinstance(installation, BotUserInstallation) else None
        ),
    )
    attachment.upload_channel_id = channel.id
    attachment.upload_channel_domain = channel.origin_domain
    await session.commit()
    return ticket_payload(attachment, upload_url)


def interaction_callback_flags(data: dict[str, Any], *, allowed: int) -> int:
    raw = data.get("flags", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_CALLBACK_FLAGS_INVALID"},
        )
    if raw < 0 or raw & ~allowed:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_CALLBACK_FLAGS_INVALID"},
        )
    return cast(int, raw)


def validated_autocomplete_choices(
    raw: object,
) -> list[dict[str, str | int | float]]:
    """Validate the bounded JSON-safe choice projection before persistence."""

    if not isinstance(raw, list) or len(raw) > 25:
        raise HTTPException(status_code=422, detail={"code": "AUTOCOMPLETE_CHOICES_INVALID"})
    normalized: list[dict[str, str | int | float]] = []
    for choice in raw:
        if not isinstance(choice, dict) or set(choice) != {"name", "value"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "AUTOCOMPLETE_CHOICES_INVALID"},
            )
        name = choice.get("name")
        value = choice.get("value")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or isinstance(value, bool)
            or not isinstance(value, (str, int, float))
            or (isinstance(value, str) and not 1 <= len(value) <= 100)
            or (isinstance(value, int) and not valid_numeric_command_value(value, integer=True))
            or (isinstance(value, float) and not valid_numeric_command_value(value, integer=False))
        ):
            raise HTTPException(
                status_code=422,
                detail={"code": "AUTOCOMPLETE_CHOICES_INVALID"},
            )
        normalized.append({"name": name, "value": value})
    return normalized


def interaction_message_from_data(
    data: dict[str, Any],
    *,
    reject_unknown: bool = True,
) -> InteractionMessageCreate:
    allowed = {
        "content",
        "e2ee",
        "tts",
        "voice_message",
        "flags",
        "embeds",
        "components",
        "poll",
        "attachment_ids",
        "sticker_ids",
        "mention_user_ids",
        "referenced_message_id",
        "view_timeout_seconds",
        "view_persistent",
        "allowed_mentions",
    }
    unknown = sorted(set(data) - allowed)
    if reject_unknown and unknown:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INTERACTION_CALLBACK_DATA_INVALID",
                "field": unknown[0],
            },
        )
    body = {key: value for key, value in data.items() if key in allowed}
    flags = interaction_callback_flags(body, allowed=INTERACTION_MESSAGE_FLAG_MASK)
    if flags & MESSAGE_FLAG_IS_VOICE_MESSAGE:
        body["voice_message"] = True
    body["flags"] = flags & ~INTERACTION_EPHEMERAL_FLAG
    try:
        return InteractionMessageCreate.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_CALLBACK_DATA_INVALID"},
        ) from exc


def ephemeral_message_payload(
    message: MessageCreate,
    *,
    flags: int,
    interaction_expires_at: datetime,
    now: datetime,
    version: int = 1,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    if message.e2ee is not None:
        rendered: dict[str, Any] = {
            "e2ee": message.e2ee,
            "attachments": list(attachments or []),
        }
        contract = message.e2ee.get("interaction_contract")
        if isinstance(contract, dict) and contract.get("kind") == "message":
            timeout_seconds = contract.get("view_timeout_seconds")
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
                )
            rendered.update(
                {
                    "view_version": version,
                    "view_persistent": False,
                    "view_expires_at": min(
                        interaction_expires_at,
                        now + timedelta(seconds=timeout_seconds),
                    ).isoformat(),
                }
            )
        return rendered
    if uses_components_v2(list(message.components)):
        flags |= MESSAGE_FLAG_IS_COMPONENTS_V2
    rendered = message.model_dump(mode="json", exclude_none=True) | {"flags": flags | 64}
    # Attachment IDs are write-only capabilities. Private response events
    # expose only the same sanitized attachment projection as channel messages.
    rendered.pop("attachment_ids", None)
    rendered.pop("allowed_mentions", None)
    rendered["attachments"] = list(attachments or [])
    if message.components:
        timeout = timedelta(seconds=message.view_timeout_seconds or 900)
        rendered["view_version"] = version
        rendered["view_persistent"] = False
        rendered["view_expires_at"] = min(interaction_expires_at, now + timeout).isoformat()
    else:
        rendered.pop("view_timeout_seconds", None)
        rendered.pop("view_persistent", None)
        rendered.pop("view_version", None)
        rendered.pop("view_expires_at", None)
    return rendered


def ephemeral_edit_validation_body(
    merged: dict[str, Any],
) -> tuple[dict[str, Any], object]:
    poll_projection = merged.get("poll")
    validation_body = dict(merged)
    if not isinstance(poll_projection, dict):
        return validation_body, poll_projection
    answers = poll_projection.get("answers")
    validation_body["poll"] = {
        "question": poll_projection.get("question"),
        "answers": [
            {"poll_media": answer.get("poll_media")}
            for answer in answers
            if isinstance(answer, dict)
        ]
        if isinstance(answers, list)
        else [],
        # Duration is immutable and bounded by the interaction deadline; this
        # value is used only to revalidate the retained rich body.
        "duration": 1,
        "allow_multiselect": bool(poll_projection.get("allow_multiselect", False)),
        "layout_type": poll_projection.get("layout_type", 1),
    }
    return validation_body, poll_projection


def validate_ephemeral_edit_flags(
    validated: InteractionMessageCreate,
    flags: int,
) -> int:
    if flags & MESSAGE_FLAG_IS_COMPONENTS_V2 and (
        validated.content is not None or validated.embeds or validated.poll is not None
    ):
        raise HTTPException(status_code=400, detail={"code": "COMPONENTS_V2_BODY_INVALID"})
    if uses_components_v2(list(validated.components)):
        return flags | MESSAGE_FLAG_IS_COMPONENTS_V2
    return flags


def render_ephemeral_component_edit(
    current: dict[str, Any],
    edit: InteractionResponseEdit,
    validated: InteractionMessageCreate,
    *,
    flags: int,
    poll_projection: object,
    interaction_expires_at: datetime,
    now: datetime,
    attachments: list[dict[str, object]] | None,
) -> dict[str, Any]:
    current_version = int(current.get("view_version", 1))
    if edit.view_version != current_version:
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_VIEW_EXPIRED"})
    rendered = ephemeral_message_payload(
        validated,
        flags=flags,
        interaction_expires_at=interaction_expires_at,
        now=now,
        version=current_version + 1,
        attachments=(
            attachments if attachments is not None else list(current.get("attachments", []))
        ),
    )
    if poll_projection is not None:
        rendered["poll"] = poll_projection
    return rendered


def edit_ephemeral_message_payload(
    current: dict[str, Any],
    edit: InteractionResponseEdit,
    *,
    interaction_expires_at: datetime,
    now: datetime,
    attachments: list[dict[str, object]] | None = None,
) -> dict[str, Any]:
    if edit.e2ee is not None:
        rendered: dict[str, Any] = {
            "e2ee": edit.e2ee,
            "attachments": (
                attachments if attachments is not None else list(current.get("attachments", []))
            ),
        }
        contract = edit.e2ee.get("interaction_contract")
        if isinstance(contract, dict) and contract.get("kind") == "message":
            timeout_seconds = contract.get("view_timeout_seconds")
            if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
                )
            rendered.update(
                {
                    "view_version": int(current.get("view_version", 1)) + 1,
                    "view_persistent": False,
                    "view_expires_at": min(
                        interaction_expires_at,
                        now + timedelta(seconds=timeout_seconds),
                    ).isoformat(),
                }
            )
        return rendered
    changes = edit.model_dump(mode="json", exclude_unset=True)
    # Ephemeral messages never fan out notifications. Keep the accepted
    # Discord field write-only rather than retaining policy data in events.
    changes.pop("allowed_mentions", None)
    if edit.components is not None:
        # Preserve discriminator/default fields even for internal SDK adapters
        # that construct component models directly rather than reparsing JSON.
        changes["components"] = [
            row.model_dump(mode="json", exclude_none=True) for row in edit.components
        ]
    # Poll result projections contain server-owned fields and therefore are
    # intentionally not parsed as another PollCreate during ordinary edits.
    # A poll can only enter the payload in the deferred-original branch.
    changes.pop("poll", None)
    merged = dict(current)
    merged.update(changes)
    flags = int(merged.get("flags", 64) or 64) | 64
    validation_body, poll_projection = ephemeral_edit_validation_body(merged)
    validated = interaction_message_from_data(validation_body, reject_unknown=False)
    flags = validate_ephemeral_edit_flags(validated, flags)
    if edit.components is not None:
        return render_ephemeral_component_edit(
            current,
            edit,
            validated,
            flags=flags,
            poll_projection=poll_projection,
            interaction_expires_at=interaction_expires_at,
            now=now,
            attachments=attachments,
        )
    rendered = validated.model_dump(mode="json", exclude_none=True) | {"flags": flags}
    rendered.pop("attachment_ids", None)
    rendered["attachments"] = (
        attachments if attachments is not None else list(current.get("attachments", []))
    )
    for view_field in (
        "view_version",
        "view_timeout_seconds",
        "view_persistent",
        "view_expires_at",
    ):
        if view_field in current:
            rendered[view_field] = current[view_field]
    if poll_projection is not None:
        rendered["poll"] = poll_projection
    return rendered


async def render_interaction_poll_payload(
    session: AsyncSession,
    poll: BotInteractionPoll,
    *,
    viewer: User | None,
) -> dict[str, object]:
    """Render an isolated poll using the same public shape as message polls."""

    answers = list(
        await session.scalars(
            select(BotInteractionPollAnswer)
            .where(BotInteractionPollAnswer.response_id == poll.response_id)
            .order_by(BotInteractionPollAnswer.answer_id)
        )
    )
    count_rows = (
        await session.execute(
            select(BotInteractionPollVote.answer_id, func.count())
            .where(BotInteractionPollVote.response_id == poll.response_id)
            .group_by(BotInteractionPollVote.answer_id)
        )
    ).all()
    counts = {int(answer_id): int(count) for answer_id, count in count_rows}
    viewer_answers: set[int] = set()
    if viewer is not None:
        viewer_answers = set(
            await session.scalars(
                select(BotInteractionPollVote.answer_id).where(
                    BotInteractionPollVote.response_id == poll.response_id,
                    BotInteractionPollVote.user_id == viewer.id,
                    BotInteractionPollVote.user_domain == viewer.origin_domain,
                )
            )
        )
    return {
        "question": dict(poll.question),
        "answers": [
            {
                "answer_id": answer.answer_id,
                "poll_media": {
                    **({"text": answer.text} if answer.text is not None else {}),
                    **({"emoji": answer.emoji} if answer.emoji is not None else {}),
                },
            }
            for answer in answers
        ],
        "expiry": poll.expires_at.isoformat(),
        "allow_multiselect": poll.allow_multiselect,
        "layout_type": poll.layout_type,
        "finalized_at": (poll.finalized_at.isoformat() if poll.finalized_at is not None else None),
        "results": {
            "is_finalized": (poll.finalized_at is not None or poll.expires_at <= datetime.now(UTC)),
            "answer_counts": [
                {
                    "id": answer.answer_id,
                    "count": counts.get(answer.answer_id, 0),
                    "me_voted": answer.answer_id in viewer_answers,
                }
                for answer in answers
            ],
        },
    }


async def create_interaction_poll(
    session: AsyncSession,
    stored: BotInteractionResponse,
    poll_create: PollCreate,
    *,
    interaction_expires_at: datetime,
    viewer: User,
) -> dict[str, object]:
    """Persist an ephemeral response poll with its interaction retention fence."""

    now = datetime.now(UTC)
    poll = BotInteractionPoll(
        response_id=stored.id,
        question=poll_create.question.model_dump(mode="json", exclude_none=True),
        allow_multiselect=poll_create.allow_multiselect,
        layout_type=poll_create.layout_type,
        expires_at=min(
            interaction_expires_at,
            now + timedelta(hours=poll_create.duration),
        ),
    )
    session.add(poll)
    for answer_id, answer in enumerate(poll_create.answers, start=1):
        session.add(
            BotInteractionPollAnswer(
                response_id=stored.id,
                answer_id=answer_id,
                text=answer.poll_media.text,
                emoji=(
                    answer.poll_media.emoji.model_dump(mode="json", exclude_none=True)
                    if answer.poll_media.emoji is not None
                    else None
                ),
            )
        )
    await session.flush()
    return await render_interaction_poll_payload(session, poll, viewer=viewer)


async def interaction_invoker(session: AsyncSession, interaction: BotInteraction) -> User:
    user = await session.get(User, (interaction.user_id, interaction.user_domain))
    if user is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_USER_UNAVAILABLE"})
    return user


async def interaction_response_channel_access(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
) -> ChannelAccess:
    channel_ref = EntityRef(f"{interaction.channel_id}@{interaction.channel_domain}")
    if isinstance(installation, BotUserInstallation):
        return await load_interaction_permission_channel_access(
            session,
            settings,
            channel_ref,
        )
    return await load_channel_access(session, settings, principal.user, channel_ref)


async def resolved_encrypted_interaction_mentions(
    session: AsyncSession,
    settings: Settings,
    interaction: BotInteraction,
    access: ChannelAccess,
    envelope: dict[str, object],
    *,
    referenced: Message | None,
) -> ResolvedMentions:
    """Resolve authenticated rich-v2 routing under invocation-time permissions."""

    resolved = await resolve_encrypted_rich_mention_projection(
        session,
        access,
        envelope,
        actor_permissions=int(interaction.invocation_permissions or 0),
        referenced=referenced,
    )
    if resolved is None:
        raise HTTPException(status_code=409, detail={"code": "E2EE_RICH_CONTEXT_MISMATCH"})
    return resolved


async def resolve_interaction_message_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    message: InteractionMessageCreate,
) -> ResolvedMentions:
    if isinstance(message.e2ee, dict) and "rich_payload_digest" in message.e2ee:
        access = await interaction_response_channel_access(
            session,
            settings,
            principal,
            interaction,
            installation,
        )
        referenced = (
            await session.get(
                Message,
                message.referenced_message_id.resolve(settings.domain),
            )
            if message.referenced_message_id is not None
            else None
        )
        return await resolved_encrypted_interaction_mentions(
            session,
            settings,
            interaction,
            access,
            message.e2ee,
            referenced=referenced,
        )
    if not contains_mention_tokens(message.content, message.components):
        return ResolvedMentions((), (), False)
    access = await interaction_response_channel_access(
        session,
        settings,
        principal,
        interaction,
        installation,
    )
    return await resolve_allowed_mentions_projection(
        session,
        redis,
        settings,
        access,
        principal.user,
        message.allowed_mentions,
        message.content,
        message.components,
        actor_permissions=(
            int(interaction.invocation_permissions or 0)
            if isinstance(installation, BotUserInstallation) and access.guild is not None
            else None
        ),
    )


async def resolve_interaction_edit_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: BotPrincipal,
    interaction: BotInteraction,
    installation: InteractionInstallation,
    edit: InteractionResponseEdit,
    current: Message,
) -> ResolvedMentions | None:
    mention_fields = {"allowed_mentions", "content", "components", "e2ee"}
    if not edit.model_fields_set & mention_fields:
        return None
    try:
        components = (
            list(edit.components or [])
            if "components" in edit.model_fields_set
            else [
                MESSAGE_LAYOUT_COMPONENT_ADAPTER.validate_python(item)
                for item in list(getattr(current, "components", None) or [])
            ]
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_MESSAGE_INVALID"},
        ) from exc
    content = (
        edit.content if "content" in edit.model_fields_set else getattr(current, "content", None)
    )
    envelope = edit.e2ee if "e2ee" in edit.model_fields_set else getattr(current, "e2ee", None)
    if isinstance(envelope, dict) and "rich_payload_digest" in envelope:
        access = await interaction_response_channel_access(
            session,
            settings,
            principal,
            interaction,
            installation,
        )
        referenced = (
            await session.get(
                Message,
                (current.referenced_message_id, current.referenced_message_domain),
            )
            if current.referenced_message_id is not None
            and current.referenced_message_domain is not None
            else None
        )
        return await resolved_encrypted_interaction_mentions(
            session,
            settings,
            interaction,
            access,
            envelope,
            referenced=referenced,
        )
    if not contains_mention_tokens(content, components):
        return ResolvedMentions((), (), False)
    access = await interaction_response_channel_access(
        session,
        settings,
        principal,
        interaction,
        installation,
    )
    return await resolve_allowed_mentions_projection(
        session,
        redis,
        settings,
        access,
        principal.user,
        edit.allowed_mentions,
        content,
        components,
        actor_permissions=(
            int(interaction.invocation_permissions or 0)
            if isinstance(installation, BotUserInstallation) and access.guild is not None
            else None
        ),
    )


def interaction_channel_message_edit(
    edit: InteractionResponseEdit,
    current: Message,
) -> MessageEdit:
    body = edit.model_dump(
        mode="json",
        exclude_unset=True,
        exclude={"allowed_mentions"},
    )
    if not body:
        # An allowed_mentions-only edit still has to traverse the canonical
        # message mutation path. A same-value public flag patch is a typed,
        # federation-safe no-op while the projection recipients are replaced.
        body["flags"] = int(getattr(current, "flags", 0) or 0) & PUBLIC_MESSAGE_EDIT_FLAGS
    try:
        return MessageEdit.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_EDIT_INVALID"},
        ) from exc


async def resolve_interaction_response_rich_emojis(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: BotPrincipal,
    interaction: BotInteraction,
    *,
    components: list[MessageLayoutComponent] | None,
    poll: PollCreate | None,
) -> None:
    """Authorize rich emoji before storing a private interaction response.

    Public responses pass through normal message admission. Ephemeral payloads
    are stored directly, so they need the same authority check here rather than
    trusting application-supplied emoji metadata.
    """

    if not rich_custom_emojis(components, poll):
        return
    target_guild: Guild | None = None
    target_permissions = 0
    if interaction.guild_id is not None and interaction.guild_domain is not None:
        target_guild = await session.get(
            Guild,
            (interaction.guild_id, interaction.guild_domain),
        )
        channel = await session.get(
            Channel,
            (interaction.channel_id, interaction.channel_domain),
        )
        if (
            target_guild is None
            or channel is None
            or (channel.guild_id, channel.guild_domain)
            != (target_guild.id, target_guild.origin_domain)
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_CHANNEL_UNAVAILABLE"},
            )
        try:
            target_permissions = await get_permissions(
                session,
                redis,
                target_guild,
                principal.user,
                channel=channel,
            )
        except HTTPException as exc:
            # A user-installed application need not have a guild-member bot.
            # Its application emoji remain valid; guild emoji still fail the
            # source-membership check in the shared resolver.
            if exc.status_code != 404:
                raise
    await resolve_rich_custom_emojis(
        session,
        principal.user,
        components=components,
        poll=poll,
        default_domain=settings.domain,
        target_guild=target_guild,
        target_permissions=target_permissions,
        trusted_external_domain=principal.application.origin_domain,
    )


async def source_ephemeral_response(
    session: AsyncSession,
    interaction: BotInteraction,
    response_id: int,
    *,
    for_update: bool,
    enforce_version: bool = True,
) -> tuple[BotInteractionResponse, BotInteraction]:
    statement = (
        select(BotInteractionResponse, BotInteraction)
        .join(
            BotInteraction,
            BotInteraction.id == BotInteractionResponse.interaction_id,
        )
        .where(
            BotInteractionResponse.id == response_id,
            BotInteractionResponse.ephemeral.is_(True),
            BotInteractionResponse.deleted_at.is_(None),
        )
    )
    if for_update:
        statement = statement.with_for_update(of=(BotInteractionResponse, BotInteraction))
    row = (await session.execute(statement)).one_or_none()
    if row is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    source, parent = row
    if (
        parent.application_id,
        parent.application_domain,
        parent.user_id,
        parent.user_domain,
        parent.channel_id,
        parent.channel_domain,
    ) != (
        interaction.application_id,
        interaction.application_domain,
        interaction.user_id,
        interaction.user_domain,
        interaction.channel_id,
        interaction.channel_domain,
    ) or parent.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    expected_version = interaction.payload.get("view_version")
    try:
        version_matches = expected_version is None or int(
            source.payload.get("view_version", 1)
        ) == int(str(expected_version))
    except (TypeError, ValueError):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"}) from None
    if enforce_version and not version_matches:
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_VIEW_EXPIRED"})
    return source, parent


async def interaction_response_payload(
    session: AsyncSession,
    interaction: BotInteraction,
    stored: BotInteractionResponse,
) -> dict[str, object]:
    if stored.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    response_identity: dict[str, object] = {
        "response_id": str(stored.id),
        "response_ref": f"{stored.id}@{interaction.channel_domain}",
        "sequence": stored.sequence,
        "revision": str(int(getattr(stored, "revision", 1) or 1)),
    }
    source_response_id = stored.payload.get("source_response_id")
    if stored.response_type in {6, 7} and source_response_id is not None:
        if not str(source_response_id).isdigit():
            raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
        source, parent = await source_ephemeral_response(
            session,
            interaction,
            int(str(source_response_id)),
            for_update=False,
            enforce_version=False,
        )
        rendered = await interaction_response_payload(session, parent, source)
        rendered["interaction_id"] = str(interaction.id)
        rendered.update(response_identity)
        return rendered
    if stored.ephemeral:
        return {
            "id": str(stored.id),
            "interaction_id": str(interaction.id),
            "channel_id": str(interaction.channel_id),
            "channel_domain": interaction.channel_domain,
            "ephemeral": True,
            "response_type": stored.response_type,
            **stored.payload,
            **response_identity,
        }
    if stored.message_id is None or stored.message_domain is None:
        return {
            "id": str(stored.id),
            "interaction_id": str(interaction.id),
            "ephemeral": False,
            "response_type": stored.response_type,
            **stored.payload,
            **response_identity,
        }
    message = await session.get(Message, (stored.message_id, stored.message_domain))
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    rendered = await render_message_payload(session, message)
    rendered["ephemeral"] = False
    rendered["interaction_id"] = str(interaction.id)
    # Public responses are ordinary channel messages, but lifecycle endpoints
    # address the durable interaction response rather than its message ID.
    rendered.update(response_identity)
    return rendered


@dataclass(frozen=True, slots=True)
class InteractionCallbackContext:
    interaction: BotInteraction
    request: InteractionCallback
    response: Response
    principal: BotPrincipal
    installation: InteractionInstallation
    session: AsyncSession
    redis: Redis
    snowflake: SnowflakeGenerator
    settings: Settings


@dataclass(slots=True)
class InteractionCallbackState:
    stored_payload: dict[str, object]
    ephemeral: bool
    message_body: InteractionMessageCreate | None = None
    message_result: dict[str, object] | None = None
    message_ref: tuple[int, str] | None = None
    message_transaction: MessageCreateTransaction | None = None
    updated_ephemeral: BotInteractionResponse | None = None
    updated_ephemeral_parent: BotInteraction | None = None
    private_attachments_added: list[Attachment] = field(default_factory=list)
    private_attachments_removed: list[Attachment] = field(default_factory=list)
    relay_destinations: set[str] = field(default_factory=set)
    private_attachment_ids: list[int] = field(default_factory=list)


def validate_interaction_callback_type(
    interaction: BotInteraction,
    request: InteractionCallback,
    installation: InteractionInstallation,
) -> tuple[int, bool]:
    callback_type = request.type
    if interaction.status != "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_ALREADY_ACKNOWLEDGED"},
        )
    if callback_type == 10:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INTERACTION_CALLBACK_UNSUPPORTED",
                "message": (
                    "PREMIUM_REQUIRED is deprecated. Use a supported message, "
                    "component update, or modal response instead."
                ),
            },
        )
    if callback_type == 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INTERACTION_CALLBACK_INVALID",
                "message": (
                    "PONG is only valid for ping interactions, which this endpoint does not create."
                ),
            },
        )
    autocomplete = interaction.interaction_type == "autocomplete"
    if autocomplete != (callback_type == 8):
        code = "AUTOCOMPLETE_CALLBACK_REQUIRED" if autocomplete else "AUTOCOMPLETE_CALLBACK_INVALID"
        raise HTTPException(status_code=400, detail={"code": code})
    private_response_required = user_install_response_forced_ephemeral(
        interaction,
        installation,
    )
    if callback_type in {6, 7}:
        valid_update_source = interaction.interaction_type in {"component", "modal_submit"} and (
            interaction.message_id is not None or interaction.payload.get("response_id") is not None
        )
        if not valid_update_source:
            raise HTTPException(status_code=400, detail={"code": "UPDATE_CALLBACK_INVALID"})
        if private_response_required and interaction.payload.get("response_id") is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "USER_INSTALL_EPHEMERAL_REQUIRED"},
            )
    if callback_type == 9 and interaction.interaction_type in {"autocomplete", "modal_submit"}:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INTERACTION_CALLBACK_INVALID",
                "message": (
                    "A modal cannot be opened from autocomplete or another modal submission."
                ),
            },
        )
    allowed_flags = {
        4: INTERACTION_MESSAGE_FLAG_MASK,
        5: INTERACTION_EPHEMERAL_FLAG,
        7: PUBLIC_MESSAGE_EDIT_FLAGS,
    }.get(callback_type, 0)
    flags = interaction_callback_flags(request.data, allowed=allowed_flags)
    if callback_type in {4, 5} and private_response_required:
        flags |= INTERACTION_EPHEMERAL_FLAG
    ephemeral = callback_type in {4, 5} and bool(flags & INTERACTION_EPHEMERAL_FLAG)
    return flags, ephemeral


async def create_callback_message(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    *,
    flags: int,
) -> None:
    message = interaction_message_from_data(context.request.data)
    await require_interaction_response_encryption(
        context.session,
        context.principal,
        context.interaction,
        context.installation,
        content=message.content,
        e2ee=message.e2ee,
        attachment_count=len(message.attachment_ids),
    )
    state.message_body = message
    if message.attachment_ids:
        require_interaction_installation_scope(
            context.installation,
            context.interaction,
            "attachments.write",
        )
    if state.ephemeral:
        await resolve_interaction_response_rich_emojis(
            context.session,
            context.redis,
            context.settings,
            context.principal,
            context.interaction,
            components=message.components,
            poll=message.poll,
        )
        state.private_attachment_ids = [int(item) for item in message.attachment_ids]
        state.stored_payload = ephemeral_message_payload(
            message,
            flags=flags,
            interaction_expires_at=context.interaction.expires_at,
            now=datetime.now(UTC),
        )
        return
    message = interaction_original_response_message(context.interaction, message)
    if message.attachment_ids:
        await require_owned_interaction_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            [int(item) for item in message.attachment_ids],
        )
    resolved_mentions = await resolve_interaction_message_mentions(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        context.interaction,
        context.installation,
        message,
    )
    automod_actor, automod_permissions = await user_install_automod_attribution(
        context.session,
        context.interaction,
        context.installation,
    )
    metadata = await interaction_message_metadata(
        context.session,
        context.interaction,
        followup=False,
    )
    state.message_transaction = MessageCreateTransaction()
    state.message_result = await create_message(
        EntityRef(f"{context.interaction.channel_id}@{context.interaction.channel_domain}"),
        message,
        context.response,
        user_auth(context.principal),
        context.session,
        context.redis,
        context.snowflake,
        context.settings,
        interaction_message_admission_options(
            context.principal,
            context.interaction,
            context.installation,
            authoritative_mentions=resolved_mentions,
            automod_actor=automod_actor,
            automod_permissions=automod_permissions,
            interaction_metadata=metadata,
            transaction=state.message_transaction,
        ),
    )
    message_id = state.message_result.get("id")
    message_domain = state.message_result.get("origin_domain")
    if isinstance(message_id, str) and isinstance(message_domain, str):
        state.message_ref = int(message_id), message_domain


async def defer_callback_update(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
) -> None:
    if context.request.data:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INTERACTION_CALLBACK_INVALID",
                "message": "A deferred message update cannot include response data.",
            },
        )
    source_response_id = context.interaction.payload.get("response_id")
    if source_response_id is not None:
        if not str(source_response_id).isdigit():
            raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
        source, _ = await source_ephemeral_response(
            context.session,
            context.interaction,
            int(str(source_response_id)),
            for_update=True,
        )
        state.stored_payload = {
            "source_response_id": str(source.id),
            "view_version": source.payload.get("view_version", 1),
        }
        state.ephemeral = True
        return
    interaction = context.interaction
    if interaction.message_id is None or interaction.message_domain is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_MESSAGE_MISSING"})
    source_message = await context.session.get(
        Message,
        (interaction.message_id, interaction.message_domain),
        with_for_update=True,
    )
    if (
        source_message is None
        or source_message.deleted_at is not None
        or (source_message.application_id, source_message.application_domain)
        != (interaction.application_id, interaction.application_domain)
        or (source_message.channel_id, source_message.channel_domain)
        != (interaction.channel_id, interaction.channel_domain)
    ):
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_MESSAGE_MISSING"})
    state.message_ref = source_message.id, source_message.origin_domain
    state.stored_payload = {}


async def update_ephemeral_callback_message(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    changes: InteractionResponseEdit,
    source_response_id: object,
) -> None:
    if not str(source_response_id).isdigit():
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    source, parent = await source_ephemeral_response(
        context.session,
        context.interaction,
        int(str(source_response_id)),
        for_update=True,
    )
    if changes.e2ee is not None:
        require_interaction_response_e2ee_binding(
            parent,
            changes.e2ee,
            response_id=source.id,
            sequence=source.sequence,
            revision=int(getattr(source, "revision", 1) or 1) + 1,
            callback_type=source.response_type,
            attachment_ids=interaction_response_attachment_ids(
                source,
                changes.attachment_ids,
            ),
        )
    current_version = int(source.payload.get("view_version", 1))
    if changes.components is not None and changes.view_version is None:
        changes.view_version = current_version
    if changes.components is not None:
        await resolve_interaction_response_rich_emojis(
            context.session,
            context.redis,
            context.settings,
            context.principal,
            context.interaction,
            components=changes.components,
            poll=None,
        )
    attachment_projection: list[dict[str, object]] | None = None
    if changes.attachment_ids is not None:
        require_interaction_installation_scope(
            context.installation,
            context.interaction,
            "attachments.write",
        )
        (
            attachment_projection,
            state.private_attachments_added,
            state.private_attachments_removed,
        ) = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            source,
            changes.attachment_ids,
        )
    source.payload = edit_ephemeral_message_payload(
        source.payload,
        changes,
        interaction_expires_at=context.interaction.expires_at,
        now=datetime.now(UTC),
        attachments=attachment_projection,
    )
    state.updated_ephemeral = source
    state.updated_ephemeral_parent = parent
    state.stored_payload = {
        "source_response_id": str(source.id),
        "view_version": source.payload.get("view_version"),
    }
    context.interaction.payload = dict(context.interaction.payload) | {
        "view_version": source.payload.get("view_version"),
    }
    state.ephemeral = True


async def update_public_callback_message(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    changes: InteractionResponseEdit,
) -> None:
    interaction = context.interaction
    if interaction.message_id is None or interaction.message_domain is None:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_MESSAGE_MISSING"})
    message = await context.session.get(
        Message,
        (interaction.message_id, interaction.message_domain),
    )
    if message is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    resolved_mentions = await resolve_interaction_edit_mentions(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        interaction,
        context.installation,
        changes,
        message,
    )
    automod_actor, automod_permissions = await user_install_automod_attribution(
        context.session,
        context.interaction,
        context.installation,
    )
    state.message_result = await edit_message(
        EntityRef(f"{message.channel_id}@{message.channel_domain}"),
        EntityRef(f"{message.id}@{message.origin_domain}"),
        interaction_channel_message_edit(changes, message),
        user_auth(context.principal),
        context.session,
        context.redis,
        context.settings,
        context.snowflake,
        MessageMutationOptions(
            application_id=context.principal.application.id,
            application_domain=context.principal.application.origin_domain,
            bot_installation_id=(
                context.installation.id
                if isinstance(context.installation, BotInstallation)
                else None
            ),
            bot_user_installation_id=(
                context.installation.id
                if isinstance(context.installation, BotUserInstallation)
                else None
            ),
            bot_dm_capability_id=(
                context.installation.id
                if isinstance(context.installation, BotDMCapability)
                else None
            ),
            bot_worker_id=context.principal.worker.id,
            authoritative_mention_refs=(
                resolved_mentions.recipients if resolved_mentions is not None else None
            ),
            authoritative_mention_role_refs=(
                resolved_mentions.roles if resolved_mentions is not None else None
            ),
            authoritative_mention_everyone=(
                resolved_mentions.everyone if resolved_mentions is not None else None
            ),
            automod_actor=automod_actor,
            automod_permissions=automod_permissions,
        ),
    )
    state.message_ref = message.id, message.origin_domain


async def update_callback_message(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
) -> None:
    if "poll" in context.request.data:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLL_EDIT_UNSUPPORTED",
                "message": "A poll can only be added while creating a deferred original response.",
            },
        )
    try:
        changes = InteractionResponseEdit.model_validate(context.request.data)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_CALLBACK_DATA_INVALID"},
        ) from exc
    await require_interaction_response_encryption(
        context.session,
        context.principal,
        context.interaction,
        context.installation,
        content=changes.content,
        e2ee=changes.e2ee,
        attachment_count=len(changes.attachment_ids or []),
    )
    source_response_id = context.interaction.payload.get("response_id")
    if source_response_id is not None:
        await update_ephemeral_callback_message(
            context,
            state,
            changes,
            source_response_id,
        )
    else:
        await update_public_callback_message(context, state, changes)


async def simple_callback_payload(
    context: InteractionCallbackContext,
    *,
    flags: int,
) -> dict[str, object]:
    request = context.request
    if request.type in {8, 9} and "e2ee" in request.data:
        if set(request.data) != {"e2ee"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_CALLBACK_DATA_INVALID"},
            )
        try:
            envelope = validate_e2ee_envelope(request.data["e2ee"])
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
            ) from exc
        if envelope is None:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_RESPONSE_E2EE_CONTEXT_INVALID"},
            )
        await require_interaction_response_encryption(
            context.session,
            context.principal,
            context.interaction,
            context.installation,
            content=None,
            e2ee=envelope,
            attachment_count=0,
        )
        return {"e2ee": cast(dict[str, object], envelope)}
    if request.type in {8, 9}:
        # Autocomplete and modal bodies are private interaction responses too;
        # an encrypted room must never persist their plaintext as an exception.
        await require_interaction_response_encryption(
            context.session,
            context.principal,
            context.interaction,
            context.installation,
            content=request.data,
            e2ee=None,
            attachment_count=0,
        )
    if request.type == 8:
        if set(request.data) != {"choices"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "AUTOCOMPLETE_CHOICES_INVALID"},
            )
        return {"choices": validated_autocomplete_choices(request.data.get("choices", []))}
    if request.type == 9:
        try:
            modal = Modal.model_validate(request.data)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_CALLBACK_DATA_INVALID"},
            ) from exc
        return modal.model_dump(mode="json", exclude_none=True)
    if request.type == 5:
        if set(request.data) - {"flags"}:
            raise HTTPException(
                status_code=422,
                detail={"code": "INTERACTION_CALLBACK_DATA_INVALID"},
            )
        return {"flags": flags} if flags else {}
    raise HTTPException(status_code=400, detail={"code": "INTERACTION_CALLBACK_INVALID"})


async def execute_interaction_callback(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    *,
    flags: int,
) -> None:
    callback_type = context.request.type
    if callback_type == 4:
        await create_callback_message(context, state, flags=flags)
    elif callback_type == 6:
        await defer_callback_update(context, state)
    elif callback_type == 7:
        await update_callback_message(context, state)
    else:
        state.stored_payload = await simple_callback_payload(context, flags=flags)


async def persist_interaction_callback(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
) -> BotInteractionResponse:
    state.stored_payload.pop("allowed_mentions", None)
    envelope = state.stored_payload.get("e2ee")
    isolated = state.ephemeral or context.request.type in {8, 9}
    response_id = (
        await require_available_interaction_response_id(
            context.session,
            envelope,
            authority_domain=context.interaction.channel_domain,
        )
        if isolated and envelope is not None
        else await context.snowflake.mint()
    )
    stored = BotInteractionResponse(
        id=response_id,
        interaction_id=context.interaction.id,
        sequence=0,
        response_type=context.request.type,
        payload=state.stored_payload,
        ephemeral=state.ephemeral,
        message_id=state.message_ref[0] if state.message_ref is not None else None,
        message_domain=state.message_ref[1] if state.message_ref is not None else None,
    )
    context.session.add(stored)
    message = state.message_body
    if state.ephemeral and context.request.type == 4 and message is not None:
        (
            projection,
            state.private_attachments_added,
            state.private_attachments_removed,
        ) = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            stored,
            state.private_attachment_ids,
        )
        stored.payload["attachments"] = projection
        stored.payload.pop("attachment_ids", None)
        if message.poll is not None:
            stored.payload["poll"] = await create_interaction_poll(
                context.session,
                stored,
                message.poll,
                interaction_expires_at=context.interaction.expires_at,
                viewer=await interaction_invoker(context.session, context.interaction),
            )
        stored.payload = dict(stored.payload)
    if isolated and envelope is not None:
        require_interaction_response_e2ee_binding(
            context.interaction,
            envelope,
            response_id=stored.id,
            sequence=0,
            revision=1,
            callback_type=context.request.type,
            attachment_ids=state.private_attachment_ids,
        )
    now = datetime.now(UTC)
    interaction = context.interaction
    interaction.callback_type = context.request.type
    interaction.acknowledged_at = now
    interaction.responded_at = now if context.request.type not in {5, 6} else None
    interaction.status = "deferred" if context.request.type in {5, 6} else "responded"
    if state.message_ref is not None:
        interaction.response_message_id, interaction.response_message_domain = state.message_ref
    relay_events: list[
        tuple[BotInteraction, BotInteractionResponse, Literal["CREATE", "UPDATE", "DELETE"]]
    ] = [(interaction, stored, "CREATE")]
    if state.updated_ephemeral is not None and state.updated_ephemeral_parent is not None:
        relay_events.insert(
            0,
            (state.updated_ephemeral_parent, state.updated_ephemeral, "UPDATE"),
        )
    state.relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        *relay_events,
    )
    await context.session.execute(
        delete(InteractionCreateDispatchOutbox).where(
            InteractionCreateDispatchOutbox.interaction_id == interaction.id
        )
    )
    if state.message_transaction is not None:
        await state.message_transaction.commit(
            context.session,
            context.redis,
            context.settings,
        )
    else:
        await context.session.commit()
    return stored


async def publish_interaction_callback(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    stored: BotInteractionResponse,
) -> None:
    if state.updated_ephemeral is not None and state.updated_ephemeral_parent is not None:
        await publish_interaction_response_event(
            context.redis,
            state.updated_ephemeral_parent,
            state.updated_ephemeral,
            "UPDATE",
        )
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        stored,
        "CREATE",
    )
    await wake_interaction_response_relays(state.relay_destinations)
    for attachment in state.private_attachments_added:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    for attachment in state.private_attachments_removed:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)


async def render_interaction_callback(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    stored: BotInteractionResponse,
) -> dict[str, object] | Response:
    if state.message_result is not None:
        state.message_result["interaction_id"] = str(context.interaction.id)
        state.message_result["response_id"] = str(stored.id)
        return state.message_result
    if (
        context.request.type == 7
        and state.updated_ephemeral is not None
        and state.updated_ephemeral_parent is not None
    ):
        rendered = await interaction_response_payload(
            context.session,
            state.updated_ephemeral_parent,
            state.updated_ephemeral,
        )
        rendered["interaction_id"] = str(context.interaction.id)
        return rendered
    if context.request.type in {1, 5, 6}:
        return Response(status_code=204)
    return await interaction_response_payload(
        context.session,
        context.interaction,
        stored,
    )


def interaction_type_value(interaction_type: str) -> int:
    try:
        return {
            "command": 2,
            "component": 3,
            "autocomplete": 4,
            "modal_submit": 5,
        }[interaction_type]
    except KeyError as exc:
        raise RuntimeError("interaction has an invalid callback type") from exc


async def render_interaction_callback_with_response(
    context: InteractionCallbackContext,
    state: InteractionCallbackState,
    stored: BotInteractionResponse,
) -> dict[str, object]:
    """Render Discord's opt-in callback response without changing legacy routes."""

    rendered = await render_interaction_callback(context, state, stored)
    message = rendered if isinstance(rendered, dict) and context.request.type in {4, 7} else None
    response_message_id = None
    if message is not None:
        raw_message_id = message.get("id")
        if raw_message_id is not None:
            response_message_id = str(raw_message_id)
    interaction_payload: dict[str, object] = {
        "id": str(context.interaction.id),
        "type": interaction_type_value(context.interaction.interaction_type),
        "response_message_loading": context.request.type == 5,
        "response_message_ephemeral": bool(
            stored.ephemeral
            or state.updated_ephemeral is not None
            and state.updated_ephemeral.ephemeral
        ),
    }
    if response_message_id is not None:
        interaction_payload["response_message_id"] = response_message_id
    resource: dict[str, object] = {"type": context.request.type}
    if message is not None:
        resource["message"] = message
    return {
        "interaction": interaction_payload,
        "resource": resource,
    }


async def process_interaction_callback(
    interaction_id: int,
    payload: InteractionCallback,
    response: Response,
    principal: BotPrincipal,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> tuple[InteractionCallbackContext, InteractionCallbackState, BotInteractionResponse]:
    """Execute and publish one callback independently of its HTTP representation."""

    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    flags, ephemeral = validate_interaction_callback_type(
        interaction,
        payload,
        installation,
    )
    context = InteractionCallbackContext(
        interaction=interaction,
        request=payload,
        response=response,
        principal=principal,
        installation=installation,
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
    )
    state = InteractionCallbackState(
        stored_payload=dict(payload.data),
        ephemeral=ephemeral,
    )
    await execute_interaction_callback(context, state, flags=flags)
    stored = await persist_interaction_callback(context, state)
    await publish_interaction_callback(context, state, stored)
    return context, state, stored


@router.post(
    "/bots/interactions/{interaction_id}/callback",
    status_code=204,
    response_model=None,
    responses={
        200: {
            "description": "Interaction callback response wrapper",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    },
)
async def callback_interaction(
    interaction_id: int,
    payload: InteractionCallback,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    with_response: Annotated[bool, Query()] = False,
) -> dict[str, object] | Response:
    context, state, stored = await process_interaction_callback(
        interaction_id,
        payload,
        response,
        principal,
        session,
        redis,
        snowflake,
        settings,
    )
    if not with_response:
        return Response(status_code=204)
    response.status_code = 200
    return await render_interaction_callback_with_response(context, state, stored)


@router.post("/bots/interactions/{interaction_id}/defer", status_code=204)
async def defer_interaction(
    interaction_id: int,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    payload: InteractionDefer | None = None,
) -> Response:
    result = await callback_interaction(
        interaction_id,
        InteractionCallback(
            type=5,
            data={"flags": 64} if (payload or InteractionDefer()).ephemeral else {},
        ),
        response,
        principal,
        session,
        redis,
        snowflake,
        settings,
    )
    if not isinstance(result, Response):
        raise RuntimeError("deferred callback unexpectedly returned a response body")
    return result


@router.post("/bots/interactions/{interaction_id}/response", status_code=201)
async def respond_interaction(
    interaction_id: int,
    payload: InteractionResponse,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    context, state, stored = await process_interaction_callback(
        interaction_id,
        InteractionCallback(
            type=4,
            data=payload.message.model_dump(
                mode="json",
                exclude_none=True,
                exclude_unset=True,
            ),
        ),
        response,
        principal,
        session,
        redis,
        snowflake,
        settings,
    )
    result = await render_interaction_callback(context, state, stored)
    if isinstance(result, Response):
        raise RuntimeError("message callback returned an empty response")
    # Kaede exposed this convenience route before adopting Discord's callback
    # response contract. Keep its message body and creation status stable.
    response.status_code = 201
    return result


async def stored_interaction_response(
    session: AsyncSession,
    interaction_id: int,
    *,
    sequence: int | None = None,
    response_id: int | None = None,
    for_update: bool = False,
) -> BotInteractionResponse:
    statement = select(BotInteractionResponse).where(
        BotInteractionResponse.interaction_id == interaction_id,
        BotInteractionResponse.deleted_at.is_(None),
    )
    if sequence is not None:
        statement = statement.where(BotInteractionResponse.sequence == sequence)
    if response_id is not None:
        statement = statement.where(BotInteractionResponse.id == response_id)
    if for_update:
        statement = statement.with_for_update()
    stored = await session.scalar(statement)
    if stored is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    return stored


@router.get("/bots/interactions/{interaction_id}/responses/@original")
async def get_original_interaction_response(
    interaction_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(session, interaction.id, sequence=0)
    await require_interaction_response_read_encryption(
        session,
        principal,
        interaction,
        installation,
        stored,
        e2ee_device_id,
    )
    return await interaction_response_payload(session, interaction, stored)


@dataclass(frozen=True, slots=True)
class InteractionOriginalEditContext:
    interaction: BotInteraction
    installation: InteractionInstallation
    stored: BotInteractionResponse
    request: InteractionResponseEdit
    principal: BotPrincipal
    session: AsyncSession
    redis: Redis
    snowflake: SnowflakeGenerator
    settings: Settings


def is_deferred_response_materialization(context: InteractionOriginalEditContext) -> bool:
    stored = context.stored
    return (
        context.interaction.status == "deferred"
        and stored.response_type == 5
        and stored.message_id is None
        and stored.message_domain is None
        and not context.interaction.responded_at
    )


def is_deferred_update_materialization(context: InteractionOriginalEditContext) -> bool:
    return (
        context.interaction.status == "deferred"
        and context.stored.response_type == 6
        and not context.interaction.responded_at
    )


async def enqueue_interaction_attachment_changes(
    added: list[Attachment],
    removed: list[Attachment],
) -> None:
    for attachment in added:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    for attachment in removed:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)


def deferred_interaction_message(request: InteractionResponseEdit) -> InteractionMessageCreate:
    body = request.model_dump(mode="json", exclude_unset=True)
    body.pop("view_version", None)
    if request.attachment_ids is not None:
        body["attachment_ids"] = [str(item) for item in request.attachment_ids]
    try:
        return InteractionMessageCreate.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INTERACTION_RESPONSE_EDIT_INVALID"},
        ) from exc


async def materialize_ephemeral_deferred_response(
    context: InteractionOriginalEditContext,
    message: InteractionMessageCreate,
) -> tuple[list[Attachment], list[Attachment]]:
    await resolve_interaction_response_rich_emojis(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        context.interaction,
        components=message.components,
        poll=message.poll,
    )
    projection: list[dict[str, object]] | None = None
    added: list[Attachment] = []
    removed: list[Attachment] = []
    if context.request.attachment_ids is not None:
        projection, added, removed = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            context.stored,
            context.request.attachment_ids,
        )
    context.stored.payload = ephemeral_message_payload(
        message,
        flags=int(context.stored.payload.get("flags", 64) or 64),
        interaction_expires_at=context.interaction.expires_at,
        now=datetime.now(UTC),
        attachments=projection,
    )
    if message.poll is not None:
        context.stored.payload["poll"] = await create_interaction_poll(
            context.session,
            context.stored,
            message.poll,
            interaction_expires_at=context.interaction.expires_at,
            viewer=await interaction_invoker(context.session, context.interaction),
        )
    context.stored.payload = dict(context.stored.payload)
    return added, removed


async def materialize_public_deferred_response(
    context: InteractionOriginalEditContext,
    message: InteractionMessageCreate,
) -> tuple[dict[str, object], MessageCreateTransaction]:
    message = interaction_original_response_message(context.interaction, message)
    if message.attachment_ids:
        await require_owned_interaction_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            [int(item) for item in message.attachment_ids],
        )
    resolved_mentions = await resolve_interaction_message_mentions(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        context.interaction,
        context.installation,
        message,
    )
    automod_actor, automod_permissions = await user_install_automod_attribution(
        context.session,
        context.interaction,
        context.installation,
    )
    metadata = await interaction_message_metadata(
        context.session,
        context.interaction,
        followup=False,
    )
    transaction = MessageCreateTransaction()
    rendered = await create_message(
        EntityRef(f"{context.interaction.channel_id}@{context.interaction.channel_domain}"),
        message,
        Response(),
        user_auth(context.principal),
        context.session,
        context.redis,
        context.snowflake,
        context.settings,
        interaction_message_admission_options(
            context.principal,
            context.interaction,
            context.installation,
            authoritative_mentions=resolved_mentions,
            automod_actor=automod_actor,
            automod_permissions=automod_permissions,
            interaction_metadata=metadata,
            transaction=transaction,
        ),
    )
    context.stored.message_id = int(str(rendered["id"]))
    context.stored.message_domain = str(rendered["origin_domain"])
    context.stored.payload = {}
    context.interaction.response_message_id = context.stored.message_id
    context.interaction.response_message_domain = context.stored.message_domain
    return rendered, transaction


async def materialize_deferred_response(
    context: InteractionOriginalEditContext,
) -> dict[str, object]:
    message = deferred_interaction_message(context.request)
    rendered: dict[str, object] | None = None
    transaction: MessageCreateTransaction | None = None
    added: list[Attachment] = []
    removed: list[Attachment] = []
    if context.stored.ephemeral:
        added, removed = await materialize_ephemeral_deferred_response(context, message)
    else:
        rendered, transaction = await materialize_public_deferred_response(context, message)
    context.interaction.status = "responded"
    context.interaction.responded_at = datetime.now(UTC)
    relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        (context.interaction, context.stored, "UPDATE"),
    )
    if transaction is not None:
        await transaction.commit(context.session, context.redis, context.settings)
    else:
        await context.session.commit()
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        context.stored,
        "UPDATE",
    )
    await wake_interaction_response_relays(relay_destinations)
    await enqueue_interaction_attachment_changes(added, removed)
    if rendered is not None:
        return rendered
    return await interaction_response_payload(
        context.session,
        context.interaction,
        context.stored,
    )


def is_private_source_update(context: InteractionOriginalEditContext) -> bool:
    return (
        context.stored.ephemeral
        and context.stored.response_type in {6, 7}
        and context.stored.payload.get("source_response_id") is not None
    )


async def edit_private_source_response(
    context: InteractionOriginalEditContext,
) -> dict[str, object]:
    raw_id = context.stored.payload.get("source_response_id")
    if raw_id is None or not str(raw_id).isdigit():
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_VIEW_INVALID"})
    source, parent = await source_ephemeral_response(
        context.session,
        context.interaction,
        int(str(raw_id)),
        for_update=True,
    )
    if context.request.e2ee is not None:
        require_interaction_response_e2ee_binding(
            parent,
            context.request.e2ee,
            response_id=source.id,
            sequence=source.sequence,
            revision=int(getattr(source, "revision", 1) or 1) + 1,
            callback_type=source.response_type,
            attachment_ids=interaction_response_attachment_ids(
                source,
                context.request.attachment_ids,
            ),
        )
    current_version = int(source.payload.get("view_version", 1))
    request = context.request
    if request.components is not None and request.view_version is None:
        request.view_version = current_version
    if request.components is not None:
        await resolve_interaction_response_rich_emojis(
            context.session,
            context.redis,
            context.settings,
            context.principal,
            context.interaction,
            components=request.components,
            poll=None,
        )
    projection: list[dict[str, object]] | None = None
    added: list[Attachment] = []
    removed: list[Attachment] = []
    if request.attachment_ids is not None:
        projection, added, removed = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            source,
            request.attachment_ids,
        )
    source.payload = edit_ephemeral_message_payload(
        source.payload,
        request,
        interaction_expires_at=parent.expires_at,
        now=datetime.now(UTC),
        attachments=projection,
    )
    new_version = source.payload.get("view_version", current_version)
    context.stored.payload = {
        "source_response_id": str(source.id),
        "view_version": new_version,
    }
    context.interaction.payload = dict(context.interaction.payload) | {"view_version": new_version}
    if is_deferred_update_materialization(context):
        context.interaction.status = "responded"
        context.interaction.responded_at = datetime.now(UTC)
    relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        (parent, source, "UPDATE"),
        (context.interaction, context.stored, "UPDATE"),
    )
    await context.session.commit()
    await publish_interaction_response_event(context.redis, parent, source, "UPDATE")
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        context.stored,
        "UPDATE",
    )
    await wake_interaction_response_relays(relay_destinations)
    await enqueue_interaction_attachment_changes(added, removed)
    rendered = await interaction_response_payload(context.session, parent, source)
    rendered["interaction_id"] = str(context.interaction.id)
    return rendered


async def edit_ephemeral_original_response(
    context: InteractionOriginalEditContext,
) -> dict[str, object]:
    request = context.request
    if request.components is not None:
        await resolve_interaction_response_rich_emojis(
            context.session,
            context.redis,
            context.settings,
            context.principal,
            context.interaction,
            components=request.components,
            poll=None,
        )
    projection: list[dict[str, object]] | None = None
    added: list[Attachment] = []
    removed: list[Attachment] = []
    if request.attachment_ids is not None:
        projection, added, removed = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            context.stored,
            request.attachment_ids,
        )
    context.stored.payload = edit_ephemeral_message_payload(
        context.stored.payload,
        request,
        interaction_expires_at=context.interaction.expires_at,
        now=datetime.now(UTC),
        attachments=projection,
    )
    relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        (context.interaction, context.stored, "UPDATE"),
    )
    await context.session.commit()
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        context.stored,
        "UPDATE",
    )
    await wake_interaction_response_relays(relay_destinations)
    await enqueue_interaction_attachment_changes(added, removed)
    return await interaction_response_payload(
        context.session,
        context.interaction,
        context.stored,
    )


async def edit_public_original_response(
    context: InteractionOriginalEditContext,
) -> dict[str, object]:
    stored = context.stored
    if stored.message_id is None or stored.message_domain is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "INTERACTION_RESPONSE_NOT_MESSAGE"},
        )
    message = await context.session.get(Message, (stored.message_id, stored.message_domain))
    if message is None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    resolved_mentions = await resolve_interaction_edit_mentions(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        context.interaction,
        context.installation,
        context.request,
        message,
    )
    automod_actor, automod_permissions = await user_install_automod_attribution(
        context.session,
        context.interaction,
        context.installation,
    )
    rendered = await edit_message(
        EntityRef(f"{message.channel_id}@{message.channel_domain}"),
        EntityRef(f"{message.id}@{message.origin_domain}"),
        interaction_channel_message_edit(context.request, message),
        user_auth(context.principal),
        context.session,
        context.redis,
        context.settings,
        context.snowflake,
        MessageMutationOptions(
            application_id=context.principal.application.id,
            application_domain=context.principal.application.origin_domain,
            bot_installation_id=(
                context.installation.id
                if isinstance(context.installation, BotInstallation)
                else None
            ),
            bot_user_installation_id=(
                context.installation.id
                if isinstance(context.installation, BotUserInstallation)
                else None
            ),
            bot_dm_capability_id=(
                context.installation.id
                if isinstance(context.installation, BotDMCapability)
                else None
            ),
            bot_worker_id=context.principal.worker.id,
            authoritative_mention_refs=(
                resolved_mentions.recipients if resolved_mentions is not None else None
            ),
            authoritative_mention_role_refs=(
                resolved_mentions.roles if resolved_mentions is not None else None
            ),
            authoritative_mention_everyone=(
                resolved_mentions.everyone if resolved_mentions is not None else None
            ),
            automod_actor=automod_actor,
            automod_permissions=automod_permissions,
        ),
    )
    changes = context.request.model_dump(mode="json", exclude_unset=True)
    stored.payload = {key: value for key, value in changes.items() if key != "allowed_mentions"}
    if is_deferred_update_materialization(context):
        context.interaction.status = "responded"
        context.interaction.responded_at = datetime.now(UTC)
    relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        (context.interaction, stored, "UPDATE"),
    )
    await context.session.commit()
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        stored,
        "UPDATE",
    )
    await wake_interaction_response_relays(relay_destinations)
    return rendered


async def apply_original_interaction_response_edit(
    context: InteractionOriginalEditContext,
) -> dict[str, object]:
    """Apply an original edit after its interaction capability has been resolved."""

    payload = context.request
    interaction = context.interaction
    installation = context.installation
    stored = context.stored
    await require_interaction_response_encryption(
        context.session,
        context.principal,
        interaction,
        installation,
        content=payload.content,
        e2ee=payload.e2ee,
        attachment_count=len(payload.attachment_ids or []),
    )
    materializing_deferred = is_deferred_response_materialization(context)
    if payload.poll is not None and not materializing_deferred:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLL_EDIT_UNSUPPORTED",
                "message": "A poll can only be added while creating a deferred original response.",
            },
        )
    if not payload.model_fields_set and interaction.status == "responded":
        return await interaction_response_payload(context.session, interaction, stored)
    private_source = is_private_source_update(context)
    if stored.ephemeral and not private_source and payload.e2ee is not None:
        callback_type = 4 if materializing_deferred else stored.response_type
        require_interaction_response_e2ee_binding(
            interaction,
            payload.e2ee,
            response_id=stored.id,
            sequence=stored.sequence,
            revision=int(getattr(stored, "revision", 1) or 1) + 1,
            callback_type=callback_type,
            attachment_ids=interaction_response_attachment_ids(
                stored,
                payload.attachment_ids,
            ),
        )
    if materializing_deferred:
        stored.response_type = 4
        return await materialize_deferred_response(context)
    if private_source:
        return await edit_private_source_response(context)
    if stored.ephemeral:
        return await edit_ephemeral_original_response(context)
    return await edit_public_original_response(context)


@router.patch("/bots/interactions/{interaction_id}/responses/@original")
async def edit_original_interaction_response(
    interaction_id: int,
    payload: InteractionResponseEdit,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    required_scopes = ["interactions.respond"]
    if payload.attachment_ids is not None:
        required_scopes.append("attachments.write")
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        *required_scopes,
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(session, interaction.id, sequence=0, for_update=True)
    return await apply_original_interaction_response_edit(
        InteractionOriginalEditContext(
            interaction=interaction,
            installation=installation,
            stored=stored,
            request=payload,
            principal=principal,
            session=session,
            redis=redis,
            snowflake=snowflake,
            settings=settings,
        )
    )


@router.delete(
    "/bots/interactions/{interaction_id}/responses/@original",
    status_code=204,
)
async def delete_original_interaction_response(
    interaction_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    interaction, _ = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(session, interaction.id, sequence=0, for_update=True)
    if stored.ephemeral:
        removed = await discard_ephemeral_response_attachments(session, settings, stored)
        stored.deleted_at = datetime.now(UTC)
        relay_destinations = await queue_interaction_response_relays(
            session, settings, (interaction, stored, "DELETE")
        )
        await session.commit()
        await publish_interaction_response_event(redis, interaction, stored, "DELETE")
        await wake_interaction_response_relays(relay_destinations)
        for attachment in removed:
            await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
        return Response(status_code=204)
    if stored.message_id is None or stored.message_domain is None:
        stored.deleted_at = datetime.now(UTC)
        relay_destinations = await queue_interaction_response_relays(
            session, settings, (interaction, stored, "DELETE")
        )
        await session.commit()
        await publish_interaction_response_event(redis, interaction, stored, "DELETE")
        await wake_interaction_response_relays(relay_destinations)
        return Response(status_code=204)
    message = await session.get(Message, (stored.message_id, stored.message_domain))
    if message is not None and message.deleted_at is None:
        await delete_message(
            EntityRef(f"{message.channel_id}@{message.channel_domain}"),
            EntityRef(f"{message.id}@{message.origin_domain}"),
            user_auth(principal),
            session,
            redis,
            settings,
        )
    stored.deleted_at = datetime.now(UTC)
    relay_destinations = await queue_interaction_response_relays(
        session, settings, (interaction, stored, "DELETE")
    )
    await session.commit()
    await publish_interaction_response_event(redis, interaction, stored, "DELETE")
    await wake_interaction_response_relays(relay_destinations)
    return Response(status_code=204)


@dataclass(frozen=True, slots=True)
class InteractionFollowupContext:
    interaction: BotInteraction
    installation: InteractionInstallation
    request: InteractionFollowup
    response: Response
    principal: BotPrincipal
    session: AsyncSession
    redis: Redis
    snowflake: SnowflakeGenerator
    settings: Settings


@dataclass(frozen=True, slots=True)
class PreparedInteractionFollowup:
    payload: dict[str, object]
    ephemeral: bool
    message_ref: tuple[int, str] | None
    transaction: MessageCreateTransaction | None


async def require_user_install_followup_capacity(
    session: AsyncSession,
    interaction: BotInteraction,
    _installation: InteractionInstallation,
) -> None:
    owners = await interaction_authorizing_owners(session, interaction, _installation)
    if set(owners) != {USER_INSTALL_OWNER}:
        return
    count = int(
        await session.scalar(
            select(func.count())
            .select_from(BotInteractionResponse)
            .where(
                BotInteractionResponse.interaction_id == interaction.id,
                BotInteractionResponse.sequence > 0,
            )
        )
        or 0
    )
    if count >= 5:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "USER_INSTALL_FOLLOWUP_LIMIT",
                "message": "User-installed commands can send at most five follow-up responses.",
            },
        )


async def prepare_interaction_followup(
    context: InteractionFollowupContext,
) -> PreparedInteractionFollowup:
    message = context.request.message
    await require_interaction_response_encryption(
        context.session,
        context.principal,
        context.interaction,
        context.installation,
        content=message.content,
        e2ee=message.e2ee,
        attachment_count=len(message.attachment_ids),
    )
    ephemeral = context.request.ephemeral or user_install_response_forced_ephemeral(
        context.interaction,
        context.installation,
    )
    if ephemeral:
        await resolve_interaction_response_rich_emojis(
            context.session,
            context.redis,
            context.settings,
            context.principal,
            context.interaction,
            components=message.components,
            poll=message.poll,
        )
        return PreparedInteractionFollowup(
            payload=ephemeral_message_payload(
                message,
                flags=INTERACTION_EPHEMERAL_FLAG,
                interaction_expires_at=context.interaction.expires_at,
                now=datetime.now(UTC),
            ),
            ephemeral=True,
            message_ref=None,
            transaction=None,
        )
    if message.attachment_ids:
        await require_owned_interaction_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            [int(item) for item in message.attachment_ids],
        )
    resolved_mentions = await resolve_interaction_message_mentions(
        context.session,
        context.redis,
        context.settings,
        context.principal,
        context.interaction,
        context.installation,
        message,
    )
    automod_actor, automod_permissions = await user_install_automod_attribution(
        context.session,
        context.interaction,
        context.installation,
    )
    metadata = await interaction_message_metadata(
        context.session,
        context.interaction,
        followup=True,
    )
    transaction = MessageCreateTransaction()
    rendered = await create_message(
        EntityRef(f"{context.interaction.channel_id}@{context.interaction.channel_domain}"),
        message,
        context.response,
        user_auth(context.principal),
        context.session,
        context.redis,
        context.snowflake,
        context.settings,
        interaction_message_admission_options(
            context.principal,
            context.interaction,
            context.installation,
            authoritative_mentions=resolved_mentions,
            automod_actor=automod_actor,
            automod_permissions=automod_permissions,
            interaction_metadata=metadata,
            transaction=transaction,
        ),
    )
    return PreparedInteractionFollowup(
        payload={},
        ephemeral=False,
        message_ref=(int(str(rendered["id"])), str(rendered["origin_domain"])),
        transaction=transaction,
    )


async def next_interaction_response_sequence(
    session: AsyncSession,
    interaction_id: int,
) -> int:
    current = await session.scalar(
        select(func.coalesce(func.max(BotInteractionResponse.sequence), -1)).where(
            BotInteractionResponse.interaction_id == interaction_id
        )
    )
    return int(current or 0) + 1


async def persist_interaction_followup(
    context: InteractionFollowupContext,
    prepared: PreparedInteractionFollowup,
) -> BotInteractionResponse:
    sequence = await next_interaction_response_sequence(
        context.session,
        context.interaction.id,
    )
    envelope = prepared.payload.get("e2ee") if prepared.ephemeral else None
    response_id = (
        await require_available_interaction_response_id(
            context.session,
            envelope,
            authority_domain=context.interaction.channel_domain,
        )
        if envelope is not None
        else await context.snowflake.mint()
    )
    stored = BotInteractionResponse(
        id=response_id,
        interaction_id=context.interaction.id,
        sequence=sequence,
        response_type=4,
        payload=prepared.payload,
        ephemeral=prepared.ephemeral,
        message_id=(prepared.message_ref[0] if prepared.message_ref is not None else None),
        message_domain=(prepared.message_ref[1] if prepared.message_ref is not None else None),
    )
    context.session.add(stored)
    added: list[Attachment] = []
    removed: list[Attachment] = []
    message = context.request.message
    if prepared.ephemeral:
        projection, added, removed = await sync_ephemeral_response_attachments(
            context.session,
            context.settings,
            context.principal,
            context.installation,
            context.interaction,
            stored,
            [int(item) for item in message.attachment_ids],
        )
        stored.payload["attachments"] = projection
        stored.payload.pop("attachment_ids", None)
        if message.poll is not None:
            stored.payload["poll"] = await create_interaction_poll(
                context.session,
                stored,
                message.poll,
                interaction_expires_at=context.interaction.expires_at,
                viewer=await interaction_invoker(context.session, context.interaction),
            )
        stored.payload = dict(stored.payload)
        if envelope is not None:
            require_interaction_response_e2ee_binding(
                context.interaction,
                envelope,
                response_id=stored.id,
                sequence=stored.sequence,
                revision=1,
                callback_type=4,
                attachment_ids=[int(item) for item in message.attachment_ids],
            )
    relay_destinations = await queue_interaction_response_relays(
        context.session,
        context.settings,
        (context.interaction, stored, "CREATE"),
    )
    if prepared.transaction is not None:
        await prepared.transaction.commit(
            context.session,
            context.redis,
            context.settings,
        )
    else:
        await context.session.commit()
    await publish_interaction_response_event(
        context.redis,
        context.interaction,
        stored,
        "CREATE",
    )
    await wake_interaction_response_relays(relay_destinations)
    await enqueue_interaction_attachment_changes(added, removed)
    return stored


@router.post("/bots/interactions/{interaction_id}/followups")
async def create_interaction_followup(
    interaction_id: int,
    payload: InteractionFollowup,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    scopes = ["interactions.respond"]
    if payload.message.attachment_ids:
        scopes.append("attachments.write")
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        *scopes,
        authority_domain=settings.domain,
    )
    if interaction.status not in {"deferred", "responded"}:
        raise HTTPException(status_code=409, detail={"code": "INTERACTION_NOT_ACKNOWLEDGED"})
    if interaction.status == "deferred" and interaction.callback_type == 5:
        stored = await stored_interaction_response(
            session,
            interaction.id,
            sequence=0,
            for_update=True,
        )
        return await apply_original_interaction_response_edit(
            InteractionOriginalEditContext(
                interaction=interaction,
                installation=installation,
                stored=stored,
                request=deferred_followup_edit(payload),
                principal=principal,
                session=session,
                redis=redis,
                snowflake=snowflake,
                settings=settings,
            )
        )
    await require_user_install_followup_capacity(session, interaction, installation)
    context = InteractionFollowupContext(
        interaction=interaction,
        installation=installation,
        request=payload,
        response=response,
        principal=principal,
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
    )
    prepared = await prepare_interaction_followup(context)
    stored = await persist_interaction_followup(context, prepared)
    return await interaction_response_payload(session, interaction, stored)


@router.get("/bots/interactions/{interaction_id}/followups/{followup_id}")
async def get_interaction_followup(
    interaction_id: int,
    followup_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(session, interaction.id, response_id=followup_id)
    if stored.sequence == 0:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    await require_interaction_response_read_encryption(
        session,
        principal,
        interaction,
        installation,
        stored,
        e2ee_device_id,
    )
    return await interaction_response_payload(session, interaction, stored)


@router.patch("/bots/interactions/{interaction_id}/followups/{followup_id}")
async def edit_interaction_followup(
    interaction_id: int,
    followup_id: int,
    payload: InteractionResponseEdit,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    required_scopes = ["interactions.respond"]
    if payload.attachment_ids is not None:
        required_scopes.append("attachments.write")
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        *required_scopes,
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(
        session, interaction.id, response_id=followup_id, for_update=True
    )
    if stored.sequence == 0:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    if payload.poll is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "POLL_EDIT_UNSUPPORTED",
                "message": "Polls cannot be added to an existing follow-up response.",
            },
        )
    await require_interaction_response_encryption(
        session,
        principal,
        interaction,
        installation,
        content=payload.content,
        e2ee=payload.e2ee,
        attachment_count=len(payload.attachment_ids or []),
    )
    if stored.ephemeral and payload.e2ee is not None:
        require_interaction_response_e2ee_binding(
            interaction,
            payload.e2ee,
            response_id=stored.id,
            sequence=stored.sequence,
            revision=int(getattr(stored, "revision", 1) or 1) + 1,
            callback_type=stored.response_type,
            attachment_ids=interaction_response_attachment_ids(
                stored,
                payload.attachment_ids,
            ),
        )
    context = InteractionOriginalEditContext(
        interaction=interaction,
        installation=installation,
        stored=stored,
        request=payload,
        principal=principal,
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
    )
    if not payload.model_fields_set:
        return await interaction_response_payload(session, interaction, stored)
    if stored.ephemeral:
        return await edit_ephemeral_original_response(context)
    return await edit_public_original_response(context)


@router.delete("/bots/interactions/{interaction_id}/followups/{followup_id}", status_code=204)
async def delete_interaction_followup(
    interaction_id: int,
    followup_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    interaction, _ = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    stored = await stored_interaction_response(
        session, interaction.id, response_id=followup_id, for_update=True
    )
    if stored.sequence == 0:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_RESPONSE_NOT_FOUND"})
    removed: list[Attachment] = []
    if stored.ephemeral:
        removed = await discard_ephemeral_response_attachments(session, settings, stored)
    if not stored.ephemeral and stored.message_id is not None and stored.message_domain is not None:
        message = await session.get(Message, (stored.message_id, stored.message_domain))
        if message is not None and message.deleted_at is None:
            await delete_message(
                EntityRef(f"{message.channel_id}@{message.channel_domain}"),
                EntityRef(f"{message.id}@{message.origin_domain}"),
                user_auth(principal),
                session,
                redis,
                settings,
            )
    stored.deleted_at = datetime.now(UTC)
    relay_destinations = await queue_interaction_response_relays(
        session, settings, (interaction, stored, "DELETE")
    )
    await session.commit()
    await publish_interaction_response_event(redis, interaction, stored, "DELETE")
    await wake_interaction_response_relays(relay_destinations)
    for attachment in removed:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    return Response(status_code=204)


async def invoking_user_interaction_poll(
    session: AsyncSession,
    interaction_id: int,
    response_id: int,
    user: User,
    *,
    for_update: bool,
) -> tuple[BotInteraction, BotInteractionResponse, BotInteractionPoll]:
    interaction_statement = select(BotInteraction).where(
        BotInteraction.id == interaction_id,
        BotInteraction.user_id == user.id,
        BotInteraction.user_domain == user.origin_domain,
    )
    if for_update:
        interaction_statement = interaction_statement.with_for_update()
    interaction = await session.scalar(interaction_statement)
    if interaction is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    if interaction.expires_at <= datetime.now(UTC):
        raise HTTPException(
            status_code=410,
            detail={
                "code": "INTERACTION_EXPIRED",
                "message": "This private bot response has expired. Run the command again.",
            },
        )
    response_statement = select(BotInteractionResponse).where(
        BotInteractionResponse.id == response_id,
        BotInteractionResponse.interaction_id == interaction.id,
        BotInteractionResponse.ephemeral.is_(True),
        BotInteractionResponse.deleted_at.is_(None),
    )
    if for_update:
        response_statement = response_statement.with_for_update()
    stored = await session.scalar(response_statement)
    if stored is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    poll_statement = select(BotInteractionPoll).where(BotInteractionPoll.response_id == stored.id)
    if for_update:
        poll_statement = poll_statement.with_for_update()
    poll = await session.scalar(poll_statement)
    if poll is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    return interaction, stored, poll


async def update_interaction_poll_projection(
    session: AsyncSession,
    stored: BotInteractionResponse,
    poll: BotInteractionPoll,
    viewer: User,
) -> None:
    await session.flush()
    stored.payload = dict(stored.payload) | {
        "poll": await render_interaction_poll_payload(session, poll, viewer=viewer)
    }


async def proxy_private_interaction_poll_action(
    session: AsyncSession,
    settings: Settings,
    user: User,
    interaction_ref: tuple[int, str],
    response_ref: tuple[int, str],
    answer_id: int,
    *,
    remove: bool,
) -> Response:
    interaction_id, authority = interaction_ref
    response_id, response_authority = response_ref
    if authority != response_authority:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    locator = await session.scalar(
        select(FederatedInteractionResponseLocator).where(
            FederatedInteractionResponseLocator.response_id == response_id,
            FederatedInteractionResponseLocator.response_domain == authority,
            FederatedInteractionResponseLocator.interaction_id == interaction_id,
            FederatedInteractionResponseLocator.interaction_domain == authority,
            FederatedInteractionResponseLocator.user_id == user.id,
            FederatedInteractionResponseLocator.user_domain == user.origin_domain,
            FederatedInteractionResponseLocator.deleted.is_(False),
            FederatedInteractionResponseLocator.expires_at > datetime.now(UTC),
        )
    )
    if locator is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    try:
        upstream = await signed_request(
            session,
            settings,
            "DELETE" if remove else "PUT",
            authority,
            (
                f"/_kaede/v1/interactions/{interaction_id}/responses/{response_id}/"
                f"polls/answers/{answer_id}/@me"
            ),
            payload={"user_id": str(user.id)},
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_INTERACTION_UNAVAILABLE"},
        ) from None
    if upstream.status_code != 204:
        detail: dict[str, object] = {"code": "FEDERATED_INTERACTION_UNAVAILABLE"}
        raw = decode_federation_response_json(upstream)
        if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
            detail = {str(key): value for key, value in raw["detail"].items()}
        raise HTTPException(status_code=upstream.status_code, detail=detail)
    return Response(status_code=204)


def resolve_interaction_route_ref(value: EntityRef | int, local_domain: str) -> tuple[int, str]:
    if isinstance(value, int):
        return value, local_domain
    return value.resolve(local_domain)


PRIVATE_INTERACTION_MEDIA_VARIANTS = frozenset(
    {"original", "thumbnail_128", "thumbnail_512", "thumbnail_1024", "poster"}
)


async def local_private_response_attachment(
    session: AsyncSession,
    settings: Settings,
    user: User,
    interaction_ref: tuple[int, str],
    response_ref: tuple[int, str],
    attachment_ref: tuple[int, str],
) -> Attachment:
    if not (interaction_ref[1] == response_ref[1] == attachment_ref[1] == settings.domain):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    interaction = await session.scalar(
        select(BotInteraction).where(
            BotInteraction.id == interaction_ref[0],
            BotInteraction.channel_domain == settings.domain,
            BotInteraction.user_id == user.id,
            BotInteraction.user_domain == user.origin_domain,
            BotInteraction.expires_at > datetime.now(UTC),
            BotInteraction.status.in_(("pending", "deferred", "responded")),
        )
    )
    if interaction is None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    stored = await session.scalar(
        select(BotInteractionResponse).where(
            BotInteractionResponse.id == response_ref[0],
            BotInteractionResponse.interaction_id == interaction.id,
            BotInteractionResponse.deleted_at.is_(None),
        )
    )
    attachment: Attachment | None = (
        await session.scalar(
            select(Attachment).where(
                Attachment.id == attachment_ref[0],
                Attachment.origin_domain == attachment_ref[1],
                Attachment.interaction_response_id == stored.id,
                Attachment.deleted_at.is_(None),
                Attachment.scan_status.in_(("clean", "encrypted")),
            )
        )
        if stored is not None
        else None
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    return attachment


@router.get(
    "/interactions/{interaction_id}/responses/{response_id}/attachments/{attachment_ref}/{variant}"
)
async def get_private_interaction_response_attachment(
    interaction_id: EntityRef,
    response_id: EntityRef,
    attachment_ref: EntityRef,
    variant: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    if variant not in PRIVATE_INTERACTION_MEDIA_VARIANTS:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    interaction = interaction_id.resolve(settings.domain)
    response = response_id.resolve(settings.domain)
    attachment = attachment_ref.resolve(settings.domain)
    if interaction[1] == settings.domain:
        local = await local_private_response_attachment(
            session,
            settings,
            auth.user,
            interaction,
            response,
            attachment,
        )
        return redirect_to_object(settings, local, variant, public=False)
    authority = interaction[1]
    locator = await session.scalar(
        select(FederatedInteractionResponseLocator).where(
            FederatedInteractionResponseLocator.response_id == response[0],
            FederatedInteractionResponseLocator.response_domain == authority,
            FederatedInteractionResponseLocator.interaction_id == interaction[0],
            FederatedInteractionResponseLocator.interaction_domain == authority,
            FederatedInteractionResponseLocator.user_id == auth.user.id,
            FederatedInteractionResponseLocator.user_domain == auth.user.origin_domain,
            FederatedInteractionResponseLocator.deleted.is_(False),
            FederatedInteractionResponseLocator.expires_at > datetime.now(UTC),
        )
    )
    if locator is None or response[1] != authority or attachment[1] != authority:
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    try:
        upstream = await signed_request(
            session,
            settings,
            "GET",
            authority,
            (
                f"/_kaede/v1/interactions/{interaction[0]}/responses/{response[0]}/"
                f"attachments/{attachment[0]}@{attachment[1]}/{variant}"
            ),
            query={"user_id": str(auth.user.id)},
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_INTERACTION_UNAVAILABLE"},
        ) from None
    raw = decode_federation_response_json(upstream)
    location = raw.get("location") if isinstance(raw, dict) else None
    media_origin = raw.get("media_origin") if isinstance(raw, dict) else None
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_INTERACTION_INVALID"},
        )
    try:
        return private_media_proxy_redirect(location, media_origin)
    except HTTPException:
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_INTERACTION_INVALID"},
        ) from None


@federation_router.get(
    "/_kaede/v1/interactions/{interaction_id}/responses/{response_id}/attachments/"
    "{attachment_ref}/{variant}"
)
async def federation_get_private_interaction_response_attachment(
    interaction_id: int,
    response_id: int,
    attachment_ref: EntityRef,
    variant: str,
    user_id: str = Query(),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if variant not in PRIVATE_INTERACTION_MEDIA_VARIANTS:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "interaction-private-media",
        capacity=240,
        refill_per_minute=120,
    )
    user = await federated_interaction_invoker(session, principal, user_id)
    attachment = await local_private_response_attachment(
        session,
        settings,
        user,
        (interaction_id, settings.domain),
        (response_id, settings.domain),
        attachment_ref.resolve(settings.domain),
    )
    redirect = redirect_to_object(settings, attachment, variant, public=False)
    location = redirect.headers.get("location")
    media_origin = redirect.headers.get(MEDIA_ORIGIN_HEADER)
    if location is None or media_origin is None:
        raise HTTPException(status_code=503, detail={"code": "MEDIA_STORAGE_UNAVAILABLE"})
    return {"location": location, "media_origin": media_origin}


@router.put(
    "/interactions/{interaction_id}/responses/{response_id}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def add_interaction_poll_vote(
    interaction_id: EntityRef,
    response_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    interaction_ref = resolve_interaction_route_ref(interaction_id, settings.domain)
    response_ref = resolve_interaction_route_ref(response_id, settings.domain)
    if interaction_ref[1] != settings.domain or response_ref[1] != settings.domain:
        return await proxy_private_interaction_poll_action(
            session,
            settings,
            auth.user,
            interaction_ref,
            response_ref,
            answer_id,
            remove=False,
        )
    interaction, stored, poll = await invoking_user_interaction_poll(
        session, interaction_ref[0], response_ref[0], auth.user, for_update=True
    )
    now = datetime.now(UTC)
    if poll.finalized_at is not None or poll.expires_at <= now:
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    answer = await session.get(BotInteractionPollAnswer, (stored.id, answer_id))
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    removed_answers: list[int] = []
    if not poll.allow_multiselect:
        removed_answers = list(
            await session.scalars(
                delete(BotInteractionPollVote)
                .where(
                    BotInteractionPollVote.response_id == stored.id,
                    BotInteractionPollVote.user_id == auth.user.id,
                    BotInteractionPollVote.user_domain == auth.user.origin_domain,
                    BotInteractionPollVote.answer_id != answer_id,
                )
                .returning(BotInteractionPollVote.answer_id)
            )
        )
    inserted = await session.scalar(
        pg_insert(BotInteractionPollVote)
        .values(
            response_id=stored.id,
            answer_id=answer_id,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
        )
        .on_conflict_do_nothing()
        .returning(BotInteractionPollVote.answer_id)
    )
    if inserted is not None or removed_answers:
        await update_interaction_poll_projection(session, stored, poll, auth.user)
        relay_destinations = await queue_interaction_response_relays(
            session, settings, (interaction, stored, "UPDATE")
        )
    else:
        relay_destinations = set()
    await session.commit()
    if inserted is not None or removed_answers:
        await publish_interaction_response_event(redis, interaction, stored, "UPDATE")
        await wake_interaction_response_relays(relay_destinations)
    return Response(status_code=204)


@router.delete(
    "/interactions/{interaction_id}/responses/{response_id}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def remove_interaction_poll_vote(
    interaction_id: EntityRef,
    response_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    interaction_ref = resolve_interaction_route_ref(interaction_id, settings.domain)
    response_ref = resolve_interaction_route_ref(response_id, settings.domain)
    if interaction_ref[1] != settings.domain or response_ref[1] != settings.domain:
        return await proxy_private_interaction_poll_action(
            session,
            settings,
            auth.user,
            interaction_ref,
            response_ref,
            answer_id,
            remove=True,
        )
    interaction, stored, poll = await invoking_user_interaction_poll(
        session, interaction_ref[0], response_ref[0], auth.user, for_update=True
    )
    if poll.finalized_at is not None or poll.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    removed = await session.scalar(
        delete(BotInteractionPollVote)
        .where(
            BotInteractionPollVote.response_id == stored.id,
            BotInteractionPollVote.answer_id == answer_id,
            BotInteractionPollVote.user_id == auth.user.id,
            BotInteractionPollVote.user_domain == auth.user.origin_domain,
        )
        .returning(BotInteractionPollVote.answer_id)
    )
    if removed is not None:
        await update_interaction_poll_projection(session, stored, poll, auth.user)
        relay_destinations = await queue_interaction_response_relays(
            session, settings, (interaction, stored, "UPDATE")
        )
    else:
        relay_destinations = set()
    await session.commit()
    if removed is not None:
        await publish_interaction_response_event(redis, interaction, stored, "UPDATE")
        await wake_interaction_response_relays(relay_destinations)
    return Response(status_code=204)


@router.get(
    "/interactions/{interaction_id}/responses/{response_id}/polls/answers/{answer_id}",
)
async def list_interaction_poll_voters(
    interaction_id: EntityRef,
    response_id: EntityRef,
    answer_id: int = Path(ge=1, le=10),
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    interaction_ref = resolve_interaction_route_ref(interaction_id, settings.domain)
    response_ref = resolve_interaction_route_ref(response_id, settings.domain)
    if interaction_ref[1] != settings.domain or response_ref[1] != settings.domain:
        authority = interaction_ref[1]
        if authority != response_ref[1]:
            raise HTTPException(
                status_code=404,
                detail={"code": "INTERACTION_POLL_NOT_FOUND"},
            )
        locator = await session.scalar(
            select(FederatedInteractionResponseLocator).where(
                FederatedInteractionResponseLocator.response_id == response_ref[0],
                FederatedInteractionResponseLocator.response_domain == authority,
                FederatedInteractionResponseLocator.interaction_id == interaction_ref[0],
                FederatedInteractionResponseLocator.interaction_domain == authority,
                FederatedInteractionResponseLocator.user_id == auth.user.id,
                FederatedInteractionResponseLocator.user_domain == auth.user.origin_domain,
                FederatedInteractionResponseLocator.deleted.is_(False),
                FederatedInteractionResponseLocator.expires_at > datetime.now(UTC),
            )
        )
        if locator is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "INTERACTION_POLL_NOT_FOUND"},
            )
        query = {"user_id": str(auth.user.id), "limit": str(limit)}
        if after is not None:
            query["after"] = str(after)
        try:
            upstream = await signed_request(
                session,
                settings,
                "GET",
                authority,
                (
                    f"/_kaede/v1/interactions/{interaction_ref[0]}/responses/"
                    f"{response_ref[0]}/polls/answers/{answer_id}"
                ),
                query=query,
                request_timeout=8,
                max_response_bytes=128 * 1024,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "FEDERATED_INTERACTION_UNAVAILABLE"},
            ) from None
        raw = decode_federation_response_json(upstream)
        if upstream.status_code != 200 or not isinstance(raw, dict):
            detail: dict[str, object] = {"code": "FEDERATED_INTERACTION_UNAVAILABLE"}
            if isinstance(raw, dict) and isinstance(raw.get("detail"), dict):
                detail = {str(key): value for key, value in raw["detail"].items()}
            raise HTTPException(status_code=upstream.status_code, detail=detail)
        return {str(key): value for key, value in raw.items()}
    _, stored, _ = await invoking_user_interaction_poll(
        session, interaction_ref[0], response_ref[0], auth.user, for_update=False
    )
    answer = await session.get(BotInteractionPollAnswer, (stored.id, answer_id))
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    conditions = [
        BotInteractionPollVote.response_id == stored.id,
        BotInteractionPollVote.answer_id == answer_id,
    ]
    if after is not None:
        conditions.append(
            tuple_(BotInteractionPollVote.user_id, BotInteractionPollVote.user_domain)
            > after.resolve(settings.domain)
        )
    users = list(
        await session.scalars(
            select(User)
            .join(
                BotInteractionPollVote,
                (BotInteractionPollVote.user_id == User.id)
                & (BotInteractionPollVote.user_domain == User.origin_domain),
            )
            .where(*conditions)
            .order_by(BotInteractionPollVote.user_id, BotInteractionPollVote.user_domain)
            .limit(limit + 1)
        )
    )
    page = users[:limit]
    return {
        "users": [user_payload(user) for user in page],
        "next_after": (
            f"{page[-1].id}@{page[-1].origin_domain}" if len(users) > limit and page else None
        ),
    }


async def federated_interaction_invoker(
    session: AsyncSession,
    principal: FederationPrincipal,
    user_id: str,
) -> User:
    if not user_id.isdigit():
        raise HTTPException(status_code=403, detail={"code": "FEDERATION_FORBIDDEN"})
    user = await session.get(User, (int(user_id), principal.origin))
    if user is None or user.account_type != "human" or user.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    return user


def require_interaction_federation_access(
    principal: FederationPrincipal,
    interaction: BotInteraction,
) -> None:
    """Keep DM/user-install interactions available under guild-only silence."""

    if interaction.guild_id is not None:
        require_guild_federation_access(principal)


@federation_router.put(
    "/_kaede/v1/interactions/{interaction_id}/responses/{response_id}/"
    "polls/answers/{answer_id}/@me",
    status_code=204,
)
@federation_router.delete(
    "/_kaede/v1/interactions/{interaction_id}/responses/{response_id}/"
    "polls/answers/{answer_id}/@me",
    status_code=204,
)
async def federation_mutate_interaction_poll_vote(
    request: Request,
    interaction_id: int,
    response_id: int,
    answer_id: int = Path(ge=1, le=10),
    payload: dict[str, str] | None = None,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "interaction-private-poll",
        capacity=180,
        refill_per_minute=90,
    )
    user = await federated_interaction_invoker(
        session,
        principal,
        str((payload or {}).get("user_id", "")),
    )
    interaction, stored, poll = await invoking_user_interaction_poll(
        session,
        interaction_id,
        response_id,
        user,
        for_update=True,
    )
    require_interaction_federation_access(principal, interaction)
    if request.method != "DELETE":
        await require_remote_user_creation_allowed(session, user)
    if poll.finalized_at is not None or poll.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=409, detail={"code": "POLL_FINALIZED"})
    answer = await session.get(BotInteractionPollAnswer, (stored.id, answer_id))
    if answer is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    # The signed request target authenticates the verb; never accept an
    # operation selector from the body.
    remove = request.method == "DELETE"
    changed = False
    if remove:
        changed = (
            await session.scalar(
                delete(BotInteractionPollVote)
                .where(
                    BotInteractionPollVote.response_id == stored.id,
                    BotInteractionPollVote.answer_id == answer_id,
                    BotInteractionPollVote.user_id == user.id,
                    BotInteractionPollVote.user_domain == user.origin_domain,
                )
                .returning(BotInteractionPollVote.answer_id)
            )
            is not None
        )
    else:
        if not poll.allow_multiselect:
            removed = list(
                await session.scalars(
                    delete(BotInteractionPollVote)
                    .where(
                        BotInteractionPollVote.response_id == stored.id,
                        BotInteractionPollVote.user_id == user.id,
                        BotInteractionPollVote.user_domain == user.origin_domain,
                        BotInteractionPollVote.answer_id != answer_id,
                    )
                    .returning(BotInteractionPollVote.answer_id)
                )
            )
        else:
            removed = []
        inserted = await session.scalar(
            pg_insert(BotInteractionPollVote)
            .values(
                response_id=stored.id,
                answer_id=answer_id,
                user_id=user.id,
                user_domain=user.origin_domain,
            )
            .on_conflict_do_nothing()
            .returning(BotInteractionPollVote.answer_id)
        )
        changed = inserted is not None or bool(removed)
    if changed:
        await update_interaction_poll_projection(session, stored, poll, user)
        relay_destinations = await queue_interaction_response_relays(
            session,
            settings,
            (interaction, stored, "UPDATE"),
        )
    else:
        relay_destinations = set()
    await session.commit()
    if changed:
        await publish_interaction_response_event(redis, interaction, stored, "UPDATE")
        await wake_interaction_response_relays(relay_destinations)
    return Response(status_code=204)


@federation_router.get(
    "/_kaede/v1/interactions/{interaction_id}/responses/{response_id}/polls/answers/{answer_id}",
)
async def federation_list_interaction_poll_voters(
    interaction_id: int,
    response_id: int,
    answer_id: int = Path(ge=1, le=10),
    user_id: str = Query(),
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "interaction-private-poll-list",
        capacity=180,
        refill_per_minute=90,
    )
    user = await federated_interaction_invoker(session, principal, user_id)
    interaction, stored, _ = await invoking_user_interaction_poll(
        session,
        interaction_id,
        response_id,
        user,
        for_update=False,
    )
    require_interaction_federation_access(principal, interaction)
    if await session.get(BotInteractionPollAnswer, (stored.id, answer_id)) is None:
        raise HTTPException(status_code=404, detail={"code": "POLL_ANSWER_NOT_FOUND"})
    conditions = [
        BotInteractionPollVote.response_id == stored.id,
        BotInteractionPollVote.answer_id == answer_id,
    ]
    if after is not None:
        conditions.append(
            tuple_(BotInteractionPollVote.user_id, BotInteractionPollVote.user_domain)
            > after.resolve(settings.domain)
        )
    users = list(
        await session.scalars(
            select(User)
            .join(
                BotInteractionPollVote,
                (BotInteractionPollVote.user_id == User.id)
                & (BotInteractionPollVote.user_domain == User.origin_domain),
            )
            .where(*conditions)
            .order_by(BotInteractionPollVote.user_id, BotInteractionPollVote.user_domain)
            .limit(limit + 1)
        )
    )
    page = users[:limit]
    return {
        "users": [user_payload(item) for item in page],
        "next_after": (
            f"{page[-1].id}@{page[-1].origin_domain}" if len(users) > limit and page else None
        ),
    }


@router.post(
    "/bots/interactions/{interaction_id}/responses/{response_id}/polls/expire",
)
async def finalize_interaction_poll(
    interaction_id: int,
    response_id: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    interaction, installation = await bot_interaction(
        session,
        principal,
        interaction_id,
        "interactions.respond",
        authority_domain=settings.domain,
    )
    if response_id == "@original":
        stored = await stored_interaction_response(
            session, interaction.id, sequence=0, for_update=True
        )
    elif response_id.isdigit():
        stored = await stored_interaction_response(
            session,
            interaction.id,
            response_id=int(response_id),
            for_update=True,
        )
    else:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    await require_interaction_response_read_encryption(
        session,
        principal,
        interaction,
        installation,
        stored,
        e2ee_device_id,
    )
    if not stored.ephemeral:
        if stored.message_id is None or stored.message_domain is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "INTERACTION_POLL_NOT_FOUND"},
            )
        message = await session.scalar(
            select(Message)
            .where(
                Message.id == stored.message_id,
                Message.origin_domain == stored.message_domain,
                Message.deleted_at.is_(None),
            )
            .with_for_update()
        )
        poll = await session.scalar(
            select(Poll)
            .where(
                Poll.message_id == stored.message_id,
                Poll.message_domain == stored.message_domain,
            )
            .with_for_update()
        )
        if message is None or poll is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "INTERACTION_POLL_NOT_FOUND"},
            )
        if (message.author_id, message.author_domain) != (
            principal.user.id,
            principal.user.origin_domain,
        ):
            raise HTTPException(
                status_code=403,
                detail={"code": "POLL_AUTHOR_REQUIRED"},
            )
        if poll.finalized_at is None:
            access = await interaction_response_channel_access(
                session,
                settings,
                principal,
                interaction,
                installation,
            )
            if access.channel.origin_domain != settings.domain:
                if access.guild is not None:
                    rendered_remote = await proxy_remote_guild_poll_finalize(
                        session,
                        settings,
                        access,
                        principal.user,
                        EntityRef(f"{message.id}@{message.origin_domain}"),
                    )
                else:
                    remote_result = await proxy_remote_dm_message_operation(
                        session,
                        settings,
                        access,
                        principal.user,
                        "poll.end",
                        EntityRef(f"{message.id}@{message.origin_domain}"),
                    )
                    rendered = remote_result.get("message")
                    if not isinstance(rendered, dict):
                        raise HTTPException(
                            status_code=502,
                            detail={"code": "FEDERATED_WRITE_RESPONSE_INVALID"},
                        )
                    rendered_remote = {str(key): value for key, value in rendered.items()}
                rendered_remote.update(
                    {
                        "ephemeral": False,
                        "interaction_id": str(interaction.id),
                        "response_id": str(stored.id),
                        "response_ref": f"{stored.id}@{interaction.channel_domain}",
                        "sequence": stored.sequence,
                        "revision": str(int(getattr(stored, "revision", 1) or 1)),
                    }
                )
                return rendered_remote
            poll.finalized_at = datetime.now(UTC)
            if access.guild is not None:
                await queue_guild_mutation(
                    session,
                    settings,
                    access.guild,
                    principal.user,
                    "guild.poll.finalize",
                    {
                        "message": {
                            "id": str(message.id),
                            "origin_domain": message.origin_domain,
                        },
                        "finalized_at": poll.finalized_at.isoformat(),
                    },
                    channel=access.channel,
                )
            else:
                await queue_dm_poll_mutation(
                    session,
                    settings,
                    access,
                    principal.user,
                    "dm.poll.finalize",
                    message,
                    finalized_at=poll.finalized_at,
                )
            result_message, result_created = await ensure_poll_result_message(
                session,
                redis,
                settings,
                snowflake,
                message,
                poll,
            )
            await publish_channel_dispatch(
                redis,
                access,
                "MESSAGE_UPDATE",
                await render_message_payload(session, message, viewer=principal.user),
            )
            if result_created:
                await publish_channel_dispatch(
                    redis,
                    access,
                    "MESSAGE_CREATE",
                    result_message,
                )
        return await interaction_response_payload(session, interaction, stored)
    poll = await session.scalar(
        select(BotInteractionPoll)
        .where(BotInteractionPoll.response_id == stored.id)
        .with_for_update()
    )
    if poll is None:
        raise HTTPException(status_code=404, detail={"code": "INTERACTION_POLL_NOT_FOUND"})
    if poll.finalized_at is None:
        poll.finalized_at = datetime.now(UTC)
        await update_interaction_poll_projection(
            session,
            stored,
            poll,
            await interaction_invoker(session, interaction),
        )
        relay_destinations = await queue_interaction_response_relays(
            session, settings, (interaction, stored, "UPDATE")
        )
        await session.commit()
        await publish_interaction_response_event(redis, interaction, stored, "UPDATE")
        await wake_interaction_response_relays(relay_destinations)
    return await interaction_response_payload(session, interaction, stored)


async def materialize_federated_user_installation(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    user: User,
    interaction: InteractionCreate,
    grant: FederatedUserInstallationGrant,
    *,
    now: datetime | None = None,
    response_expires_at: datetime | None = None,
) -> BotUserInstallation:
    """Persist a revision-fenced mirror from the installing user's home.

    The request signature authenticates the user-home grant.  Application
    metadata and command definitions are independently refreshed from the
    application home, so neither home can impersonate the other's authority.
    """

    app_id, app_domain = grant.application_ref.resolve(settings.domain)
    requested_app = interaction.application_ref.resolve(settings.domain)
    if (app_id, app_domain) != requested_app:
        raise HTTPException(status_code=422, detail={"code": "USER_INSTALLATION_MISMATCH"})
    application_ref = app_id, app_domain
    await require_federated_user_application(
        session,
        settings,
        snowflake,
        application_ref,
        grant,
        refresh_remote_application=refresh_user_bot_application,
    )
    source_ref = int(grant.id), user.origin_domain
    await session.execute(
        select(
            func.pg_advisory_xact_lock(
                federated_user_installation_lock(
                    source_ref[0],
                    source_ref[1],
                    application_ref,
                    user,
                )
            )
        )
    )
    installation = await locked_federated_user_installation(
        session,
        user,
        application_ref,
        source_ref,
    )
    installation = await reconcile_federated_user_installation(
        session,
        snowflake,
        user,
        application_ref,
        source_ref,
        grant,
        installation,
        now=now,
        minimum_expires_at=response_expires_at,
        maximum_expires_at=(
            response_expires_at + USER_INSTALLATION_AUTHORITY_GRACE
            if response_expires_at is not None
            else None
        ),
        clock_skew=timedelta(seconds=settings.federation_clock_skew_seconds),
    )
    await session.flush()
    return installation


def interaction_inherits_authority_installation(interaction: InteractionCreate) -> bool:
    """Return whether an authority-owned message/response supplies exact lineage."""

    return interaction.interaction_type in {"component", "modal_submit"}


@federation_router.post("/_kaede/v1/channels/{channel_id}/interactions", status_code=202)
async def federation_create_interaction(
    channel_id: int,
    payload: FederatedInteractionCreate,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    now = datetime.now(UTC)
    if (
        payload.response_expires_at.tzinfo is None
        or payload.response_expires_at <= now
        or payload.response_expires_at
        > now + INTERACTION_LIFETIME + timedelta(seconds=settings.federation_clock_skew_seconds)
    ):
        raise HTTPException(status_code=422, detail={"code": "INTERACTION_EXPIRY_INVALID"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-command-create", capacity=300, refill_per_minute=300
    )
    user = await session.get(User, (int(payload.user_id), principal.origin))
    if user is None or user.account_type != "human":
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    auth = AuthenticatedUser(
        user=user,
        grant=cast(Any, None),
        access_token="",
        cookie_authenticated=False,
    )
    app_id, app_domain = payload.interaction.application_ref.resolve(settings.domain)
    if payload.interaction.interaction_type in {"command", "autocomplete"} and (
        payload.interaction.command_id is None or payload.interaction.integration_type is None
    ):
        raise HTTPException(status_code=422, detail={"code": "INTERACTION_LINEAGE_REQUIRED"})
    channel = await session.get(Channel, (int(channel_id), settings.domain))
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.guild_id is not None:
        require_guild_federation_access(principal)
    await require_remote_user_creation_allowed(session, user)
    request_fingerprint = federated_interaction_request_fingerprint(payload)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-interaction-response-grant:{payload.response_grant_id}",
                    0,
                )
            )
        )
    )
    existing_interaction = await session.scalar(
        select(BotInteraction)
        .where(BotInteraction.response_grant_id == payload.response_grant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing_interaction is not None:
        if (
            existing_interaction.request_fingerprint != request_fingerprint
            or (existing_interaction.user_id, existing_interaction.user_domain)
            != (user.id, user.origin_domain)
            or (existing_interaction.channel_id, existing_interaction.channel_domain)
            != (channel.id, channel.origin_domain)
            or (existing_interaction.application_id, existing_interaction.application_domain)
            != (app_id, app_domain)
            or existing_interaction.expires_at != payload.response_expires_at
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "INTERACTION_RESPONSE_GRANT_CONFLICT"},
            )
        created_at = existing_interaction.created_at or now
        delivered = await drain_interaction_create_dispatch_outbox(
            session,
            redis,
            settings,
            interaction_id=existing_interaction.id,
        )
        if not delivered:
            await wake_interaction_create_dispatch_outbox()
        return {
            "id": str(existing_interaction.id),
            "interaction_ref": f"{existing_interaction.id}@{settings.domain}",
            "status": existing_interaction.status,
            "ack_deadline": (created_at + timedelta(seconds=3)).isoformat(),
        }
    guild_installation = None
    channel_guild_ref: tuple[int, str] | None = None
    if channel is not None and channel.guild_id is not None and channel.guild_domain is not None:
        channel_guild_ref = (channel.guild_id, channel.guild_domain)
        guild_installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.application_id == app_id,
                BotInstallation.application_domain == app_domain,
                BotInstallation.guild_id == channel.guild_id,
                BotInstallation.guild_domain == channel.guild_domain,
                BotInstallation.status == "active",
                BotInstallation.revoked_at.is_(None),
                installation_has_membership(),
                BotInstallation.granted_scopes.contains(["applications.commands"]),
            )
        )
    inherited_lineage = interaction_inherits_authority_installation(payload.interaction)
    requested_user_install = (
        not inherited_lineage and payload.interaction.integration_type == "user_install"
    )
    requested_dm_capability = payload.interaction.integration_type == "dm_capability"
    guild_path_available = (
        guild_installation is not None
        and channel is not None
        and await installation_allows_channel(session, guild_installation, channel)
        and not inherited_lineage
        and not requested_user_install
        and payload.interaction.integration_type != "dm_capability"
    )
    if (
        guild_path_available
        and channel_guild_ref is not None
        and payload.interaction.interaction_type in {"command", "autocomplete"}
    ):
        guild = await session.get(Guild, channel_guild_ref)
        guild_command = (
            await guild_install_command(
                session,
                guild,
                application_ref=(app_id, app_domain),
                name=payload.interaction.command_name or "",
                command_type=payload.interaction.command_type or "chat_input",
                command_id=payload.interaction.command_id,
            )
            if guild is not None
            else None
        )
        guild_path_available = guild_command is not None
    if inherited_lineage and payload.user_installation is not None:
        raise HTTPException(status_code=422, detail={"code": "INTERACTION_LINEAGE_CONFLICT"})
    if (
        not guild_path_available
        and not requested_dm_capability
        and not inherited_lineage
        and payload.user_installation is None
    ):
        # A stale mirror is never an authorization source. Every user-installed
        # remote invocation must carry a fresh grant in the signed request.
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    target_destinations: set[str] = set()
    if (guild_path_available or requested_dm_capability) and payload.user_installation is not None:
        raise HTTPException(status_code=422, detail={"code": "INTERACTION_LINEAGE_CONFLICT"})
    if (
        not guild_path_available
        and not requested_dm_capability
        and not inherited_lineage
        and payload.user_installation is not None
    ):
        installation = await materialize_federated_user_installation(
            session,
            settings,
            snowflake,
            user,
            payload.interaction,
            payload.user_installation,
            now=now,
            response_expires_at=payload.response_expires_at,
        )
        # The materialized grant and target notification share the same
        # transaction. Delivery is only woken after create_interaction commits.
        target_destinations = await queue_application_target_snapshots_for_refs(
            session,
            settings,
            {(installation.application_id, installation.application_domain)},
        )
    result = await create_interaction(
        EntityRef(f"{channel_id}@{settings.domain}"),
        payload.interaction,
        Response(),
        auth,
        session,
        redis,
        snowflake,
        settings,
        InteractionInvocationOptions(
            federated_locale=payload.locale,
            federated_age_assured_adult=payload.age_assured_adult,
            federated_age_restricted_dm_commands_enabled=(
                payload.age_restricted_dm_commands_enabled
            ),
            federated_response_grant_id=payload.response_grant_id,
            federated_expires_at=payload.response_expires_at,
            federated_request_fingerprint=request_fingerprint,
            federated_attachments=tuple(payload.attachments),
        ),
    )
    await wake_application_target_deliveries(target_destinations)
    return result
