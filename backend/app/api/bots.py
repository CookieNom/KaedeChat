from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.calls import (
    active_bot_call_capability_bindings,
    bot_call_response,
    local_dm_participants,
    mint_dm_call_token,
    notify_call,
    project_call_transition,
    propagate_call_create,
    require_call_bot_capability,
    require_call_policy,
)
from app.api.channels import (
    MessageAdmissionOptions,
    MessageMutationOptions,
    add_reaction,
    bulk_delete_messages,
    clear_all_reactions,
    clear_reaction_group,
    create_message,
    crosspost_message,
    delete_announcement_follow,
    delete_message,
    edit_message,
    finalize_poll,
    follow_announcement_channel,
    list_announcement_follows,
    list_channel_pins,
    list_messages,
    list_pins,
    list_poll_voters,
    list_reaction_users,
    pin_message,
    reaction_path_emoji,
    remove_own_reaction,
    remove_user_reaction,
    resolve_forwarded_message,
    source_announcement_follow,
    typing,
    unpin_message,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, get_snowflake
from app.api.dms import open_direct_message_for
from app.api.guilds import (
    create_channel,
    create_role,
    delete_overwrite,
    list_overwrites,
    put_overwrite,
    sync_channel_permissions,
)
from app.api.invites import (
    create_invite,
    get_managed_invite,
    list_channel_invites,
    list_invites,
    local_get_invite_target_users,
    local_get_invite_target_users_job_status,
    local_update_invite_target_users,
    parse_invite_management_code,
    revoke_invite,
)
from app.api.management import (
    assign_role,
    remove_role,
    reorder_channels,
    reorder_roles,
    replace_member_roles,
    update_guild,
)
from app.api.management import (
    delete_channel as delete_guild_channel,
)
from app.api.management import (
    delete_role as delete_guild_role,
)
from app.api.management import (
    update_channel as update_guild_channel,
)
from app.api.management import (
    update_role as update_guild_role,
)
from app.api.media import (
    authorized_attachment,
    commit_guild_asset,
    commit_role_icon,
    create_emoji,
    create_sticker,
    delete_emoji,
    delete_guild_asset,
    delete_role_icon,
    delete_sticker,
    issue_image_asset_ticket,
    local_manageable_role,
)
from app.api.moderation import (
    ban_instance,
    ban_member,
    kick_member,
    list_audit_logs,
    list_bans,
    list_instance_bans,
    list_members,
    remove_ban,
    remove_instance_ban,
    update_member,
)
from app.api.scheduled_events import (
    ScheduledEventCreate,
    ScheduledEventPatch,
    commit_scheduled_event_image,
    create_scheduled_event,
    create_scheduled_event_image_ticket_for,
    delete_scheduled_event,
    delete_scheduled_event_image,
    get_scheduled_event,
    list_scheduled_event_users,
    list_scheduled_events,
    patch_scheduled_event,
    scheduled_event_for_guild,
)
from app.api.voice import (
    channel_voice_occupancy,
    disconnect_member_voice,
    move_member_voice,
    require_bot_voice_member_channel_access,
    update_member_voice_moderation,
)
from app.api.webhook_e2ee import (
    get_webhook_e2ee_participation,
    grant_webhook_e2ee_participation,
    revoke_webhook_e2ee_participation,
)
from app.api.webhooks import (
    WebhookCreate,
    WebhookPatch,
    commit_webhook_avatar,
    create_webhook,
    create_webhook_avatar_ticket,
    delete_webhook,
    delete_webhook_avatar,
    get_webhook,
    list_channel_webhooks,
    list_webhooks,
    patch_webhook,
    rotate_webhook,
    target_follower_webhook,
)
from app.bots.auth import BotPrincipal, require_application_home_bot, require_bot
from app.bots.dm_capability import (
    BotDMCapabilityApplyRequest,
    BotDMCapabilityAuthorityUnavailable,
    BotDMCapabilityProofInvalid,
    BotDMCapabilitySourceRejected,
    bot_dm_capability_fence_expectation,
    fence_bot_dm_capability,
    fetch_bot_dm_capability_proof,
    refresh_bot_dm_capability_proof,
    stored_bot_dm_capability_payload,
    usable_dm_capability,
    validate_bot_dm_capability_at_source,
)
from app.bots.e2ee import require_bot_e2ee_participation
from app.bots.installations import (
    installation_accessible_channel,
    installation_allows_channel,
    qualified_channel_restrictions,
    usable_guild_installation,
)
from app.bots.runtime_control import (
    build_current_application_runtime_proof,
    queue_application_runtime_snapshots,
)
from app.bots.target_contract import target_policy_allows
from app.bots.worker_targets import worker_target_allowed
from app.chat.audit_payloads import AuditLogEntryPayload
from app.chat.channel_access import effective_channel_nsfw
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.forwarding import (
    FORWARD_SOURCE_AUTHORIZATION_EVENT,
    FORWARDABLE_MESSAGE_TYPES,
    build_forward_source_authorization_content,
)
from app.chat.payloads import (
    channel_payload,
    emoji_payload,
    guild_payload,
    role_payload,
    sticker_payload,
    user_payload,
)
from app.chat.permissions import get_permissions, require_permissions
from app.chat.privacy import blocked_between, lock_dm_policy
from app.chat.schemas import (
    BanCreate,
    BotForwardSourceAuthorizationCreate,
    ChannelCreate,
    ChannelFollowCreate,
    ChannelPositionBatch,
    ChannelUpdate,
    DMOpenRequest,
    GuildUpdate,
    InstanceBanCreate,
    InviteCreate,
    MemberRoleSet,
    MemberUpdate,
    MessageBulkDelete,
    MessageCreate,
    MessageEdit,
    OverwritePut,
    ReactionCreate,
    RoleCreate,
    RolePositionBatch,
    RoleUpdate,
    parse_actor_intent_headers,
)
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.model_validation import UnambiguousInputModel
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import wake_federation_destinations as wake_application_runtime_deliveries
from app.core.types import EntityRef, Snowflake
from app.db.bot_models import (
    BotApplication,
    BotDMCapability,
    BotE2EEParticipation,
    BotInstallation,
    BotInstanceRule,
)
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Emoji,
    EmojiRoleRestriction,
    Guild,
    GuildMember,
    GuildScheduledEvent,
    Message,
    Poll,
    Role,
    Sticker,
    User,
    Webhook,
)
from app.federation.client import signed_request
from app.federation.events import build_envelope
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
)
from app.federation.replication import profile_from_user
from app.federation.schemas import EventEnvelope
from app.federation.users import resolve_handle
from app.media.schemas import (
    AssetCommitRequest,
    EmojiCommitRequest,
    GuildAssetKind,
    StickerCommitRequest,
    StickerTicketRequest,
    UploadTicketRequest,
)
from app.media.service import (
    attachment_payload,
    create_upload_ticket,
    require_image_type,
    require_sticker_type,
    ticket_payload,
)
from app.voice.rooms import dm_room_name, participant_identity
from app.voice.schemas import (
    BotActiveCallResponse,
    BotCallResponse,
    BotVoiceTokenRequest,
    CallAction,
    CallCreate,
    VoiceChannelStatusUpdate,
    VoiceModerationUpdate,
    VoiceMoveRequest,
    VoiceTokenResponse,
)
from app.voice.service import require_voice_enabled
from app.voice.state import (
    BOT_CAPABILITY_BINDINGS_FIELD,
    create_call,
    get_active_call,
    get_call,
    is_call_accepted,
    transition_call,
)

router = APIRouter(prefix="/api/v1/bots", tags=["bot api"])

BotChannelGrant = BotInstallation | BotDMCapability


class BotInviteTargetUsersPut(UnambiguousInputModel):
    """Exact JSON boundary for Discord-style targeted community invites."""

    model_config = ConfigDict(extra="forbid")

    target_user_ids: list[EntityRef] = Field(max_length=1_000)


def bot_message_grant_ids(grant: BotChannelGrant) -> tuple[int | None, int | None]:
    """Map one authenticated runtime grant to the shared message admission fields."""

    if isinstance(grant, BotDMCapability):
        return None, grant.id
    return grant.id, None


def bot_runtime_grant_payload(grant: BotChannelGrant) -> dict[str, str]:
    """Expose the exact qualified runtime and component-view lineage."""

    if isinstance(grant, BotDMCapability):
        return {
            "bot_dm_capability_id": grant.grant_id,
            "bot_dm_capability_revision": str(grant.revision),
            "bot_dm_capability_lineage_ref": (f"{grant.id}@{grant.conversation_domain}"),
            "bot_installation_ref": (
                f"{grant.source_installation_id}@{grant.source_installation_domain}"
            ),
            "bot_installation_type": grant.source_kind,
        }
    return {"bot_installation_id": str(grant.id)}


def bind_bot_thread_runtime_grant(
    rendered: dict[str, object],
    grant: BotChannelGrant,
) -> dict[str, object]:
    """Pin a thread Channel and every embedded Message to one runtime grant."""

    binding = bot_runtime_grant_payload(grant)
    rendered.update(binding)
    for key in ("starter_message", "message"):
        nested = rendered.get(key)
        if isinstance(nested, dict):
            rendered[key] = dict(nested) | binding
    return rendered


def bot_dm_capability_bootstrap_payload(
    capability: BotDMCapability,
    channel: Channel,
) -> dict[str, object]:
    """Render restart-safe lineage without exposing a mutable handle or user profile."""

    return {
        "grant_id": capability.grant_id,
        "revision": str(capability.revision),
        "authority_origin": f"https://{capability.authority_domain}",
        "channel_ref": f"{channel.id}@{channel.origin_domain}",
        "installation_ref": (
            f"{capability.source_installation_id}@{capability.source_installation_domain}"
        ),
        "installation_type": capability.source_kind,
        "expires_at": capability.expires_at.isoformat(),
        "channel": channel_payload(channel) | bot_runtime_grant_payload(capability),
    }


async def locked_active_principal_application(
    session: AsyncSession,
    principal: BotPrincipal,
) -> BotApplication:
    """Serialize app-home capability mutations with runtime suspension."""

    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == principal.application.id,
            BotApplication.origin_domain == principal.application.origin_domain,
            BotApplication.bot_user_id == principal.user.id,
            BotApplication.bot_user_domain == principal.user.origin_domain,
            BotApplication.status == "active",
        )
        .with_for_update()
    )
    if application is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    return application


async def locked_application_target_rules(
    session: AsyncSession,
    application: BotApplication,
) -> dict[str, str]:
    """Read the app-authoritative instance rules while its row lock is held."""

    return {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule)
            .where(
                BotInstanceRule.application_id == application.id,
                BotInstanceRule.application_domain == application.origin_domain,
            )
            .with_for_update()
        )
    }


def require_application_dm_target_allowed(
    application: BotApplication,
    rules: dict[str, str],
    target_domain: str,
) -> None:
    if target_domain == application.origin_domain:
        return
    if not target_policy_allows(application.target_policy, rules, target_domain):
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_TARGET_NOT_ALLOWED"},
        )


def require_worker_dm_target(principal: BotPrincipal, target_domain: str) -> None:
    """Keep DM discovery inside the worker's own runtime boundary."""

    if not worker_target_allowed(
        principal.worker.target_domains,
        application_domain=principal.application.origin_domain,
        target_domain=target_domain,
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_TARGET_NOT_DELEGATED"})


async def current_bot_dm_runtime_proofs(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    *,
    source_domain: str,
    authority_domain: str,
) -> tuple[EventEnvelope, EventEnvelope]:
    """Build stable-content A proofs while the active application row is locked."""

    source_proof, _ = await build_current_application_runtime_proof(
        session,
        settings,
        application_ref=(application.id, application.origin_domain),
        target_domain=source_domain,
    )
    if authority_domain == source_domain:
        return source_proof, source_proof
    authority_proof, _ = await build_current_application_runtime_proof(
        session,
        settings,
        application_ref=(application.id, application.origin_domain),
        target_domain=authority_domain,
    )
    return source_proof, authority_proof


def user_auth(principal: BotPrincipal) -> AuthenticatedUser:
    # Existing message services only consume auth.user. Keeping the adapter
    # here preserves one permission and federation implementation.
    return cast(AuthenticatedUser, principal)


def parsed_bot_actor_intent_headers(
    actor_intent_header: str | None,
    actor_intents_header: str | None,
) -> tuple[dict[str, object] | None, dict[str, dict[str, object]]]:
    """Decode the two compatible actor-intent header encodings once."""

    try:
        return parse_actor_intent_headers(actor_intent_header, actor_intents_header)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_ACTOR_INTENT_INVALID"}) from exc


def _payload_mentions_user(rendered: dict[str, object], user_ref: tuple[int, str]) -> bool:
    raw_mentions = rendered.get("mention_user_refs")
    if not isinstance(raw_mentions, list):
        return False
    for raw in raw_mentions:
        if not isinstance(raw, dict):
            continue
        try:
            if (int(str(raw.get("id"))), str(raw.get("origin_domain"))) == user_ref:
                return True
        except (TypeError, ValueError):
            continue
    return False


def bot_message_content_exempt(
    rendered: dict[str, object],
    *,
    bot_user_ref: tuple[int, str] | None,
    bot_application_ref: tuple[int, str] | None,
    direct_message: bool,
    interaction_context: bool = False,
) -> bool:
    """Return whether this message is exempt from Discord's content intent.

    Exceptions are properties of the selected bot and individual message, not
    installation grants. Keeping them separate prevents cross-installation
    scope/intent composition.
    """

    if direct_message or interaction_context:
        return True
    if bot_user_ref is not None:
        try:
            if (
                int(str(rendered.get("author_id"))),
                str(rendered.get("author_domain")),
            ) == bot_user_ref:
                return True
        except (TypeError, ValueError):
            pass
        if _payload_mentions_user(rendered, bot_user_ref):
            return True
    if bot_application_ref is not None:
        try:
            return (
                int(str(rendered.get("application_id"))),
                str(rendered.get("application_domain")),
            ) == bot_application_ref
        except (TypeError, ValueError):
            return False
    return False


def bot_installation_has_intent(
    principal: BotPrincipal,
    installation: BotChannelGrant,
    intent: str,
) -> bool:
    """Intersect a worker credential intent with its exact runtime grant."""

    return intent in getattr(principal, "intents", ()) and intent in (
        getattr(installation, "granted_intents", None) or ()
    )


def require_bot_installation_intent(
    principal: BotPrincipal,
    installation: BotChannelGrant,
    intent: str,
) -> None:
    if not bot_installation_has_intent(principal, installation, intent):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_INTENT_REQUIRED", "intent": intent},
        )


def bot_can_read_ambient_message_content(
    principal: BotPrincipal,
    installation: BotChannelGrant,
) -> bool:
    """Apply Discord's HTTP message-content scope and intent intersection."""

    return (
        "messages.content" in getattr(principal, "scopes", ())
        and "messages.content" in (getattr(installation, "granted_scopes", None) or ())
        and bot_installation_has_intent(principal, installation, "message_content")
    )


def _redact_snapshot_payload(
    snapshot: dict[str, object],
    *,
    can_read_content: bool,
    can_read_attachments: bool,
) -> dict[str, object]:
    rendered = dict(snapshot)
    if not can_read_content:
        rendered["content"] = None
        rendered["embeds"] = []
        rendered["components"] = []
        rendered.pop("poll", None)
        rendered["content_unavailable"] = True
    if not can_read_attachments:
        rendered["attachments"] = []
        rendered["attachments_unavailable"] = True
    nested_snapshots = rendered.get("message_snapshots")
    if isinstance(nested_snapshots, list):
        safe_nested: list[dict[str, object]] = []
        for wrapper in nested_snapshots:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("message"), dict):
                continue
            safe_nested.append(
                {
                    **wrapper,
                    "message": _redact_snapshot_payload(
                        dict(wrapper["message"]),
                        can_read_content=can_read_content,
                        can_read_attachments=can_read_attachments,
                    ),
                }
            )
        rendered["message_snapshots"] = safe_nested
    return rendered


def redact_bot_message_payload(
    rendered: dict[str, object],
    *,
    can_read_content: bool,
    can_read_attachments: bool,
    principal: BotPrincipal | None = None,
    bot_user_ref: tuple[int, str] | None = None,
    bot_application_ref: tuple[int, str] | None = None,
    direct_message: bool = False,
    interaction_context: bool = False,
    can_read_e2ee: bool = False,
    unavailable: bool = False,
    include_reference: bool = True,
) -> dict[str, object]:
    """Apply bot content grants to one message and its resolved reference.

    Discord type-21 thread starters keep the source body in
    ``referenced_message``.  Keeping this projection in one bounded helper
    prevents REST and Gateway paths from accidentally treating that nested
    message as metadata.
    """

    if principal is not None:
        bot_user_ref = (principal.user.id, principal.user.origin_domain)
        bot_application_ref = (
            principal.application.id,
            principal.application.origin_domain,
        )
    effective_content = can_read_content or bot_message_content_exempt(
        rendered,
        bot_user_ref=bot_user_ref,
        bot_application_ref=bot_application_ref,
        direct_message=direct_message,
        interaction_context=interaction_context,
    )
    encrypted = rendered.get("e2ee") is not None
    if unavailable or not effective_content or (encrypted and not can_read_e2ee):
        rendered["content"] = None
        rendered["e2ee"] = None
        rendered["embeds"] = []
        rendered["components"] = []
        rendered.pop("poll", None)
        rendered["forwarded_message_ref"] = None
        rendered["forwarded_message_id"] = None
        rendered["forwarded_message_domain"] = None
        rendered["content_unavailable"] = True
    if unavailable or not can_read_attachments:
        rendered["attachments"] = []
        rendered["attachments_unavailable"] = True
    snapshot_content = effective_content and not unavailable
    snapshot_attachments = can_read_attachments and not unavailable
    raw_snapshot = rendered.get("forward_snapshot")
    if isinstance(raw_snapshot, dict):
        rendered["forward_snapshot"] = _redact_snapshot_payload(
            raw_snapshot,
            can_read_content=snapshot_content,
            can_read_attachments=snapshot_attachments,
        )
    raw_forwarded = rendered.get("forwarded_message")
    if isinstance(raw_forwarded, dict):
        rendered["forwarded_message"] = _redact_snapshot_payload(
            raw_forwarded,
            can_read_content=snapshot_content,
            can_read_attachments=snapshot_attachments,
        )
    raw_snapshots = rendered.get("message_snapshots")
    if isinstance(raw_snapshots, list):
        safe_snapshots: list[dict[str, object]] = []
        for wrapper in raw_snapshots:
            if not isinstance(wrapper, dict) or not isinstance(wrapper.get("message"), dict):
                continue
            safe_snapshots.append(
                {
                    **wrapper,
                    "message": _redact_snapshot_payload(
                        dict(wrapper["message"]),
                        can_read_content=snapshot_content,
                        can_read_attachments=snapshot_attachments,
                    ),
                }
            )
        rendered["message_snapshots"] = safe_snapshots
    referenced = rendered.get("referenced_message")
    if include_reference and isinstance(referenced, dict):
        rendered["referenced_message"] = redact_bot_message_payload(
            dict(referenced),
            can_read_content=can_read_content,
            can_read_attachments=can_read_attachments,
            principal=principal,
            bot_user_ref=bot_user_ref,
            bot_application_ref=bot_application_ref,
            direct_message=direct_message,
            interaction_context=interaction_context,
            can_read_e2ee=can_read_e2ee,
            unavailable=unavailable,
            include_reference=False,
        )
    return rendered


def redact_bot_thread_payload(
    rendered: dict[str, object],
    *,
    can_read_history: bool,
    can_read_content: bool,
    can_read_attachments: bool,
    principal: BotPrincipal | None = None,
    bot_user_ref: tuple[int, str] | None = None,
    bot_application_ref: tuple[int, str] | None = None,
    direct_message: bool = False,
    can_read_e2ee: bool = False,
) -> dict[str, object]:
    unavailable = not can_read_history or (
        (bool(rendered.get("e2ee_required")) or rendered.get("encryption_mode") == "e2ee")
        and not can_read_e2ee
    )
    for key in ("starter_message", "message"):
        starter = rendered.get(key)
        if not isinstance(starter, dict):
            continue
        rendered[key] = redact_bot_message_payload(
            dict(starter),
            can_read_content=can_read_content,
            can_read_attachments=can_read_attachments,
            principal=principal,
            bot_user_ref=bot_user_ref,
            bot_application_ref=bot_application_ref,
            direct_message=direct_message,
            can_read_e2ee=can_read_e2ee,
            unavailable=unavailable,
        )
    return rendered


def require_bot_resource_authority(
    settings: Settings,
    *,
    resource_domain: str,
    resource_ref: EntityRef,
) -> None:
    """Reject bot operations received by a non-authoritative replica.

    Bot access tokens and DPoP proofs are audience-bound to one target and must
    not be forwarded through the human guild-management RPC. Qualified refs let
    the SDK route the same request directly to the resource authority.
    """

    if resource_domain == settings.domain:
        return
    raise HTTPException(
        status_code=409,
        detail={
            "code": "BOT_RESOURCE_AUTHORITY_REQUIRED",
            "resource_ref": str(resource_ref),
            "authority_domain": resource_domain,
        },
    )


def require_standard_installation_token(principal: BotPrincipal) -> None:
    """Keep an exact DM token from crossing into guild/user-install resources."""

    if principal.dm_capability_grant_id is not None:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_DM_GRANT_RESOURCE_MISMATCH"},
        )


async def installation_for_channel(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    channel_ref: EntityRef,
    scope: str,
    installation_id: int | None = None,
) -> tuple[Channel, BotInstallation | BotDMCapability]:
    channel_id, channel_domain = channel_ref.resolve(settings.domain)
    channel = await session.get(Channel, (channel_id, channel_domain))
    if channel is None or channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    require_bot_resource_authority(
        settings,
        resource_domain=channel.origin_domain,
        resource_ref=EntityRef(f"{channel.id}@{channel.origin_domain}"),
    )
    if channel.guild_id is None:
        participant = await session.get(
            DMParticipant,
            (
                channel.id,
                channel.origin_domain,
                principal.user.id,
                principal.user.origin_domain,
            ),
        )
        if participant is None:
            # Keep non-participant conversations indistinguishable from
            # resources that do not exist.
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        if principal.dm_capability_grant_id is None:
            # Every current bot DM is opened from an install-authority proof.
            # Never let a stripped capability header downgrade that exact
            # conversation to an unrelated local guild installation.
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_REQUIRED"},
            )
        try:
            source_ref = EntityRef(principal.installation_ref or "")
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_INVALID"},
            ) from None
        if source_ref.domain is None or principal.installation_type not in {"guild", "user"}:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_INVALID"},
            )
        capability = await session.scalar(
            select(BotDMCapability)
            .join(
                User,
                (User.id == BotDMCapability.target_user_id)
                & (User.origin_domain == BotDMCapability.target_user_domain),
            )
            .where(
                BotDMCapability.grant_id == principal.dm_capability_grant_id,
                BotDMCapability.revision == principal.dm_capability_revision,
                BotDMCapability.source_kind == principal.installation_type,
                BotDMCapability.source_installation_id == source_ref.id,
                BotDMCapability.source_installation_domain == source_ref.domain,
                BotDMCapability.application_id == principal.application.id,
                BotDMCapability.application_domain == principal.application.origin_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                BotDMCapability.conversation_id == channel.id,
                BotDMCapability.conversation_domain == channel.origin_domain,
                usable_dm_capability(at=datetime.now(UTC)),
                User.account_type == "human",
                User.disabled_at.is_(None),
            )
        )
        if capability is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_REQUIRED"},
            )
        for required_scope in {scope, "dm.send"}:
            principal.require_scope(required_scope)
            if required_scope not in capability.granted_scopes:
                raise HTTPException(
                    status_code=403,
                    detail={"code": "BOT_SCOPE_REQUIRED", "scope": required_scope},
                )
        return channel, capability
    require_standard_installation_token(principal)
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == channel.guild_id,
            BotInstallation.guild_domain == channel.guild_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if scope not in installation.granted_scopes or scope not in principal.scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    if not await installation_allows_channel(session, installation, channel):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_CHANNEL_RESTRICTED"},
        )
    return channel, installation


async def require_bot_channel_e2ee_access(
    session: AsyncSession,
    channel: Channel,
    installation: BotChannelGrant,
    device_id: str | None,
    *,
    worker_id: int,
) -> BotE2EEParticipation | None:
    """Require one active, consented MLS device for encrypted bot content."""

    if not isinstance(device_id, str):
        device_id = None
    if getattr(channel, "encryption_mode", "plaintext") != "e2ee" and not bool(
        getattr(channel, "e2ee_required", False)
    ):
        return None
    participation, _ = await require_bot_e2ee_participation(
        session,
        installation,
        channel,
        device_id,
        worker_id=worker_id,
    )
    return participation


async def optional_bot_channel_e2ee_access(
    session: AsyncSession,
    channel: Channel,
    installation: BotChannelGrant,
    device_id: str | None,
    *,
    worker_id: int,
) -> BotE2EEParticipation | None:
    """Return exact MLS access for metadata, redacting when this room is not joined."""

    try:
        return await require_bot_channel_e2ee_access(
            session,
            channel,
            installation,
            device_id,
            worker_id=worker_id,
        )
    except HTTPException as exc:
        code: str | None = (
            exc.detail.get("code")
            if isinstance(exc.detail, dict) and isinstance(exc.detail.get("code"), str)
            else None
        )
        if exc.status_code == 409 and code in {
            "BOT_E2EE_PARTICIPANT_REQUIRED",
            "E2EE_REKEY_REQUIRED",
        }:
            return None
        raise


async def require_bot_attachment_e2ee_access(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    attachment: Attachment,
    installation: BotChannelGrant,
    device_id: str | None,
    *,
    message: Message | None = None,
    channel: Channel | None = None,
) -> None:
    """Fence encrypted attachment metadata and bytes to one current MLS device."""

    if channel is None:
        if message is not None:
            channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        elif (
            attachment.upload_channel_id is not None
            and attachment.upload_channel_domain is not None
        ):
            channel = await session.get(
                Channel,
                (attachment.upload_channel_id, attachment.upload_channel_domain),
            )
    if channel is None:
        if attachment.encryption_mode == "e2ee":
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        return
    if channel.encryption_mode != "e2ee" and not channel.e2ee_required:
        return
    try:
        _, current_installation = await installation_for_channel(
            session,
            settings,
            principal,
            EntityRef(f"{channel.id}@{channel.origin_domain}"),
            "attachments.read",
            installation.id if isinstance(installation, BotInstallation) else None,
        )
    except HTTPException as exc:
        if exc.status_code not in {403, 404}:
            raise
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"}) from None
    if (
        type(current_installation) is not type(installation)
        or current_installation.id != installation.id
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    participation = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        device_id,
        worker_id=principal.worker.id,
    )
    if message is not None and not await bot_messages_after_history_floor(
        session,
        participation,
        [{"id": str(message.id), "origin_domain": message.origin_domain}],
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})


async def bot_messages_after_history_floor(
    session: AsyncSession,
    participation: BotE2EEParticipation | None,
    messages: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Hide immutable history predating a bot's room-consent boundary."""

    if participation is None or participation.history_floor_message_id is None:
        return messages
    floor = await session.get(
        Message,
        (
            participation.history_floor_message_id,
            participation.history_floor_message_domain,
        ),
    )
    if floor is None or floor.created_at is None:
        return []
    refs: set[tuple[int, str]] = set()
    for message in messages:
        raw_id = message.get("id")
        raw_domain = message.get("origin_domain")
        if isinstance(raw_domain, str) and str(raw_id).isdigit():
            refs.add((int(str(raw_id)), raw_domain))
    if not refs:
        return []
    rows = list(
        (
            await session.execute(
                select(Message.id, Message.origin_domain, Message.created_at).where(
                    Message.channel_id == participation.channel_id,
                    Message.channel_domain == participation.channel_domain,
                    tuple_(Message.id, Message.origin_domain).in_(refs),
                )
            )
        ).tuples()
    )
    allowed = {
        (message_id, message_domain)
        for message_id, message_domain, created_at in rows
        if created_at > floor.created_at
        or (
            created_at == floor.created_at
            and (message_id, message_domain) > (floor.id, floor.origin_domain)
        )
    }
    return [
        message
        for message in messages
        if (int(str(message["id"])), str(message["origin_domain"])) in allowed
    ]


async def require_bot_forward_source_access(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    source_message_ref: EntityRef,
    *,
    e2ee_device_id: str | None,
    installation_id: int | None = None,
) -> tuple[Message, Channel, BotInstallation | BotDMCapability, BotE2EEParticipation | None]:
    """Authorize a forward against one exact source installation and history floor."""

    source_id, source_domain = source_message_ref.resolve(settings.domain)
    source_message = await session.get(Message, (source_id, source_domain))
    if source_message is None or source_message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    source_channel_ref = EntityRef(f"{source_message.channel_id}@{source_message.channel_domain}")
    source_channel, source_installation = await installation_for_channel(
        session,
        settings,
        principal,
        source_channel_ref,
        "messages.history",
        installation_id,
    )
    participation = await require_bot_channel_e2ee_access(
        session,
        source_channel,
        source_installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    if not await bot_messages_after_history_floor(
        session,
        participation,
        [{"id": str(source_message.id), "origin_domain": source_message.origin_domain}],
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    source_projection: dict[str, object] = {
        "author_id": str(source_message.author_id),
        "author_domain": source_message.author_domain,
        "application_id": (
            str(source_message.application_id)
            if source_message.application_id is not None
            else None
        ),
        "application_domain": source_message.application_domain,
        "mention_user_refs": list(source_message.mention_user_refs or []),
    }
    granted_scopes = set(source_installation.granted_scopes or [])
    can_read_content = bot_can_read_ambient_message_content(
        principal, source_installation
    ) or bot_message_content_exempt(
        source_projection,
        bot_user_ref=(principal.user.id, principal.user.origin_domain),
        bot_application_ref=(
            principal.application.id,
            principal.application.origin_domain,
        ),
        direct_message=source_channel.guild_id is None,
        interaction_context=source_message.message_type in {20, 23},
    )
    if not can_read_content:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_MESSAGE_CONTENT_REQUIRED"},
        )
    has_attachments = await session.scalar(
        select(
            exists().where(
                Attachment.message_id == source_message.id,
                Attachment.message_domain == source_message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    if has_attachments and not (
        "attachments.read" in principal.scopes and "attachments.read" in granted_scopes
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_ATTACHMENT_ACCESS_REQUIRED"},
        )
    return source_message, source_channel, source_installation, participation


def bot_e2ee_sender_device_id(payload: MessageCreate | MessageEdit) -> str | None:
    envelope = payload.e2ee
    if not isinstance(envelope, dict):
        return None
    device_id = envelope.get("sender_device_id")
    return device_id if isinstance(device_id, str) else None


async def render_bot_message_response(
    session: AsyncSession,
    principal: BotPrincipal,
    channel: Channel,
    installation: BotChannelGrant,
    rendered: dict[str, object],
    *,
    e2ee_device_id: str | None = None,
    interaction_context: bool = False,
) -> dict[str, object]:
    """Apply one exact runtime grant to every bot Message REST response."""

    participation = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    if not await bot_messages_after_history_floor(session, participation, [rendered]):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    granted_scopes = set(installation.granted_scopes or [])
    return redact_bot_message_payload(
        dict(rendered),
        can_read_content=bot_can_read_ambient_message_content(principal, installation),
        can_read_attachments=(
            "attachments.read" in principal.scopes and "attachments.read" in granted_scopes
        ),
        principal=principal,
        direct_message=channel.guild_id is None,
        interaction_context=interaction_context,
        can_read_e2ee=participation is not None,
    ) | bot_runtime_grant_payload(installation)


async def installation_for_guild(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    scope: str,
) -> tuple[Guild, BotInstallation]:
    require_standard_installation_token(principal)
    principal.require_scope(scope)
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    require_bot_resource_authority(
        settings,
        resource_domain=guild.origin_domain,
        resource_ref=EntityRef(f"{guild.id}@{guild.origin_domain}"),
    )
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if scope not in installation.granted_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    return guild, installation


async def installation_for_guild_any_scope(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    scope: str,
    *compatible_scopes: str,
) -> tuple[Guild, BotInstallation]:
    """Resolve a guild installation accepting an explicitly published alias.

    New resource-specific grants stay least-privilege while installations
    issued under Kaede's older documented scopes keep working.
    """

    require_standard_installation_token(principal)
    accepted = (scope, *compatible_scopes)
    if not any(item in principal.scopes for item in accepted):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.get(Guild, (guild_id, guild_domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    require_bot_resource_authority(
        settings,
        resource_domain=guild.origin_domain,
        resource_ref=EntityRef(f"{guild.id}@{guild.origin_domain}"),
    )
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.guild_id == guild.id,
            BotInstallation.guild_domain == guild.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    if not any(item in installation.granted_scopes for item in accepted):
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )
    return guild, installation


def require_installation_scope(
    principal: BotPrincipal,
    installation: BotChannelGrant,
    scope: str,
) -> None:
    principal.require_scope(scope)
    if scope not in installation.granted_scopes:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
        )


async def exact_installation_by_id(
    session: AsyncSession,
    principal: BotPrincipal,
    installation_id: int | None,
    *scopes: str,
) -> BotInstallation:
    """Resolve the caller-selected, active installation for non-guild actions."""

    if installation_id is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTALLATION_REQUIRED"})
    for scope in scopes:
        principal.require_scope(scope)
    installation = await session.scalar(
        select(BotInstallation).where(
            BotInstallation.id == installation_id,
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            usable_guild_installation(),
        )
    )
    if installation is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    for scope in scopes:
        require_installation_scope(principal, installation, scope)
    return installation


async def installation_attachment(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    attachment_ref: EntityRef,
    scope: str,
    *,
    require_bound_message: bool,
    installation_id: int | None = None,
) -> tuple[Attachment, BotChannelGrant]:
    """Resolve an attachment without crossing installation or target boundaries.

    An installation may inspect its own unbound upload tickets. Once an
    attachment is bound to a message, ``attachments.read`` follows ordinary
    channel access and can therefore read human-authored media too. The
    durable upload owner is still checked when present, preventing one guild
    installation from laundering its quota into another installation.
    """

    principal.require_scope(scope)
    attachment_id, attachment_domain = attachment_ref.resolve(settings.domain)
    attachment = await session.get(Attachment, (attachment_id, attachment_domain))
    if attachment is None or attachment.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    owning_installation: BotChannelGrant | None = None
    if attachment.bot_installation_id is not None:
        owning_installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.id == attachment.bot_installation_id,
                BotInstallation.application_id == principal.application.id,
                BotInstallation.application_domain == principal.application.origin_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                usable_guild_installation(),
            )
        )
        if owning_installation is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    elif attachment.bot_dm_capability_id is not None:
        owning_installation = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.id == attachment.bot_dm_capability_id,
                BotDMCapability.grant_id == principal.dm_capability_grant_id,
                BotDMCapability.application_id == principal.application.id,
                BotDMCapability.application_domain == principal.application.origin_domain,
                BotDMCapability.bot_user_id == principal.user.id,
                BotDMCapability.bot_user_domain == principal.user.origin_domain,
                usable_dm_capability(at=datetime.now(UTC)),
            )
        )
        if owning_installation is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})

    if attachment.message_id is not None and attachment.message_domain is not None:
        message = await session.get(Message, (attachment.message_id, attachment.message_domain))
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        channel_ref = EntityRef(f"{message.channel_id}@{message.channel_domain}")
        try:
            _, channel_installation = await installation_for_channel(
                session,
                settings,
                principal,
                channel_ref,
                scope,
                installation_id,
            )
        except HTTPException as exc:
            if exc.status_code not in {403, 404}:
                raise
            # Do not reveal that a guessed attachment is bound to a channel
            # outside this exact installation's current authority.
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"}) from None
        if owning_installation is not None and (
            type(owning_installation) is not type(channel_installation)
            or owning_installation.id != channel_installation.id
        ):
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        return attachment, channel_installation

    if (
        require_bound_message
        or owning_installation is None
        or (
            isinstance(owning_installation, BotInstallation)
            and installation_id != owning_installation.id
        )
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    require_installation_scope(principal, owning_installation, scope)
    return attachment, owning_installation


async def require_owned_attachments_for_installation(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    installation: BotChannelGrant,
    attachment_ids: list[int],
) -> None:
    require_installation_scope(principal, installation, "attachments.write")
    if not attachment_ids:
        return
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
        (
            attachment.bot_dm_capability_id
            if isinstance(installation, BotDMCapability)
            else attachment.bot_installation_id
        )
        != installation.id
        or (attachment.uploader_id, attachment.uploader_domain)
        != (principal.user.id, principal.user.origin_domain)
        for attachment in rows
    ):
        # A single indistinguishable error avoids turning attachment IDs into
        # an ownership or cross-installation oracle.
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})


def visible_presence(raw: object) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = None
        if isinstance(value, dict) and value.get("status") in {"online", "idle", "dnd"}:
            return str(value["status"])
    return "offline"


@router.get("/@me")
async def bot_identity(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
) -> dict[str, object]:
    return {
        "user": user_payload(principal.user),
        "application_ref": (f"{principal.application.id}@{principal.application.origin_domain}"),
        "worker_id": str(principal.worker.authority_id),
        "scopes": sorted(principal.scopes),
        "intents": sorted(principal.intents),
        "token_expires_at": principal.token.expires_at.isoformat(),
    }


@router.get("/guilds")
async def bot_guilds(
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[dict[str, object]]:
    principal.require_scope("guilds.read")
    rows = (
        await session.execute(
            select(BotInstallation, Guild)
            .join(
                Guild,
                (Guild.id == BotInstallation.guild_id)
                & (Guild.origin_domain == BotInstallation.guild_domain),
            )
            .where(
                BotInstallation.application_id == principal.application.id,
                BotInstallation.application_domain == principal.application.origin_domain,
                BotInstallation.bot_user_id == principal.user.id,
                BotInstallation.bot_user_domain == principal.user.origin_domain,
                usable_guild_installation(),
                BotInstallation.granted_scopes.contains(["guilds.read"]),
            )
            .order_by(Guild.id)
        )
    ).all()
    return [_bot_guild_payload(guild, installation) for installation, guild in rows]


def _bot_guild_payload(guild: Guild, installation: BotInstallation) -> dict[str, object]:
    """Bind a guild projection to the worker's exact installation ceiling."""

    return guild_payload(guild) | bot_guild_installation_payload(installation)


def bot_guild_installation_payload(
    installation: BotInstallation,
) -> dict[str, object]:
    """Render the exact target-owned grant attached to every bot guild view."""

    return {
        "installation_id": str(installation.id),
        "granted_scopes": installation.granted_scopes,
        "granted_intents": installation.granted_intents,
        "channel_restrictions": list(
            qualified_channel_restrictions(
                installation.channel_restrictions or [],
                authority_domain=installation.guild_domain,
            )
        ),
        "capability_revision": str(installation.grant_revision),
        "e2ee_mode": installation.e2ee_mode,
    }


@router.get("/guilds/{guild_ref}")
async def bot_get_guild(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.read"
    )
    return _bot_guild_payload(guild, installation)


@router.get("/guilds/{guild_ref}/channels")
async def bot_guild_channels(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild(
        session, settings, principal, guild_ref, "channels.read"
    )
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
                Channel.type.not_in({10, 11, 12}),
            )
            .order_by(Channel.position, Channel.id)
        )
    )
    result: list[dict[str, object]] = []
    for channel in channels:
        channel_permissions = await get_permissions(
            session, redis, guild, principal.user, channel=channel
        )
        if channel_permissions & Permission.VIEW_CHANNEL:
            result.append(channel_payload(channel) | {"permissions": str(int(channel_permissions))})
    return result


@router.get("/channels/{channel_ref}")
async def bot_get_channel(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "channels.read",
        installation_id,
    )
    permissions = Permission(0)
    if channel.guild_id is not None:
        guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        permissions = Permission(
            await get_permissions(session, redis, guild, principal.user, channel=channel)
        )
        if not permissions & Permission.VIEW_CHANNEL:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    if channel.type in {10, 11, 12} and channel.guild_id is not None:
        from app.api.threads import rendered_thread

        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        rendered = await rendered_thread(session, redis, guild, principal.user, channel)
        participation = (
            await optional_bot_channel_e2ee_access(
                session,
                channel,
                installation,
                e2ee_device_id,
                worker_id=principal.worker.id,
            )
            if e2ee_device_id is not None
            else None
        )
        can_read_e2ee = participation is not None
        if can_read_e2ee:
            starters = [
                item
                for key in ("starter_message", "message")
                if isinstance((item := rendered.get(key)), dict)
            ]
            visible_starters = await bot_messages_after_history_floor(
                session,
                participation,
                starters,
            )
            can_read_e2ee = bool(starters) and len(visible_starters) == len(starters)
        return bind_bot_thread_runtime_grant(
            redact_bot_thread_payload(
                rendered,
                can_read_history=(
                    "messages.history" in principal.scopes
                    and "messages.history" in installation.granted_scopes
                ),
                can_read_content=bot_can_read_ambient_message_content(principal, installation),
                can_read_attachments=(
                    "attachments.read" in principal.scopes
                    and "attachments.read" in installation.granted_scopes
                ),
                principal=principal,
                can_read_e2ee=can_read_e2ee,
            ),
            installation,
        )
    return (
        channel_payload(channel)
        | {
            "permissions": str(int(permissions)),
        }
        | bot_runtime_grant_payload(installation)
    )


@router.get("/guilds/{guild_ref}/members")
async def bot_guild_members(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=100, ge=1, le=1000),
    after: EntityRef | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
) -> list[dict[str, object]]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "members.read"
    )
    require_bot_installation_intent(principal, installation, "guild_members")
    members = await list_members(
        guild_ref, limit, after, query, user_auth(principal), session, redis, settings
    )
    can_read_presence = (
        "guild_presences" in principal.intents and "guild_presences" in installation.granted_intents
    )
    if can_read_presence and members:
        keys: list[str] = []
        for member in members:
            raw_user = member.get("user")
            if not isinstance(raw_user, dict):
                keys.append("")
                continue
            keys.append(f"presence:{raw_user.get('origin_domain', '')}:{raw_user.get('id', '')}")
        async with redis.pipeline(transaction=False) as pipeline:
            for key in keys:
                if key:
                    pipeline.get(key)
                else:
                    pipeline.get("presence:invalid")
            raw_presences = list(await pipeline.execute())
        for member, raw_presence in zip(members, raw_presences, strict=True):
            member["presence"] = visible_presence(raw_presence)
    return members


@router.get("/guilds/{guild_ref}/roles")
async def bot_guild_roles(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild(session, settings, principal, guild_ref, "roles.read")
    permissions = await get_permissions(session, redis, guild, principal.user)
    if not permissions & Permission.VIEW_CHANNEL:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    roles = list(
        await session.scalars(
            select(Role)
            .where(Role.guild_id == guild.id, Role.guild_domain == guild.origin_domain)
            .order_by(Role.position, Role.id)
        )
    )
    return [role_payload(role) for role in roles]


@router.patch("/guilds/{guild_ref}")
async def bot_update_guild(
    guild_ref: EntityRef,
    payload: GuildUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.manage"
    )
    rendered = await update_guild(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
        reason,
    )
    return rendered | bot_guild_installation_payload(installation)


@router.post("/guilds/{guild_ref}/channels", status_code=201)
async def bot_create_channel(
    guild_ref: EntityRef,
    payload: ChannelCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await create_channel(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.patch("/guilds/{guild_ref}/channels/{channel_ref}")
async def bot_update_channel(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: ChannelUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await update_guild_channel(
        guild_ref,
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
        reason,
    )


@router.put(
    "/guilds/{guild_ref}/channels/{channel_ref}/voice-status",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def bot_update_voice_channel_status(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: VoiceChannelStatusUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "channels.manage"
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    from app.api.voice import update_voice_channel_status_for_actor

    await update_voice_channel_status_for_actor(
        channel_ref,
        payload,
        principal.user,
        session,
        redis,
        snowflake,
        settings,
        reason=reason,
        expected_guild_ref=guild_ref,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch(
    "/guilds/{guild_ref}/channels",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def bot_reorder_channels(
    guild_ref: EntityRef,
    payload: ChannelPositionBatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    await reorder_channels(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/guilds/{guild_ref}/channels/{channel_ref}")
async def bot_delete_channel(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "channels.manage")
    return await delete_guild_channel(
        guild_ref,
        channel_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/channels/{channel_ref}/overwrites")
async def bot_list_channel_overwrites(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, str]]:
    await installation_for_channel(
        session, settings, principal, channel_ref, "channels.overwrites.read"
    )
    return await list_overwrites(
        guild_ref,
        channel_ref,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/channels/{channel_ref}/overwrites")
async def bot_put_channel_overwrite(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: OverwritePut,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, str]:
    await installation_for_channel(
        session, settings, principal, channel_ref, "channels.overwrites.manage"
    )
    return await put_overwrite(
        guild_ref,
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete(
    "/guilds/{guild_ref}/channels/{channel_ref}/overwrites/{target_type}/{target_ref}",
    status_code=204,
)
async def bot_delete_channel_overwrite(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    target_type: str,
    target_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "channels.overwrites.manage"
    )
    return await delete_overwrite(
        guild_ref,
        channel_ref,
        target_type,
        target_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/channels/{channel_ref}/permissions/sync")
async def bot_sync_channel_permissions(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    await installation_for_channel(
        session, settings, principal, channel_ref, "channels.overwrites.manage"
    )
    return await sync_channel_permissions(
        guild_ref,
        channel_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/roles")
async def bot_create_role(
    guild_ref: EntityRef,
    payload: RoleCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await create_role(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.patch("/guilds/{guild_ref}/roles/{role_ref}")
async def bot_update_role(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    payload: RoleUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    if_match: str | None = Header(default=None, alias="If-Match"),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await update_guild_role(
        guild_ref,
        role_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        if_match,
        reason,
    )


@router.patch("/guilds/{guild_ref}/roles")
async def bot_reorder_roles(
    guild_ref: EntityRef,
    payload: RolePositionBatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await reorder_roles(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/roles/{role_ref}", status_code=204)
async def bot_delete_role(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await delete_guild_role(
        guild_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/assets/{kind}", status_code=201)
async def bot_create_guild_asset_ticket(
    guild_ref: EntityRef,
    kind: GuildAssetKind,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.assets.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    await require_permissions(
        session,
        redis,
        guild,
        principal.user,
        required_permissions("guild.asset.manage"),
    )
    purpose = "guild_icon" if kind == "icon" else "guild_banner"
    return await issue_image_asset_ticket(
        session,
        redis,
        snowflake,
        settings,
        principal.user,
        payload,
        response,
        purpose=purpose,
        bot_installation=installation,
    )


@router.put("/guilds/{guild_ref}/assets/{kind}")
async def bot_commit_guild_asset(
    guild_ref: EntityRef,
    kind: GuildAssetKind,
    payload: AssetCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.assets.manage"
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await commit_guild_asset(
        guild_ref,
        kind,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete("/guilds/{guild_ref}/assets/{kind}")
async def bot_delete_guild_asset(
    guild_ref: EntityRef,
    kind: GuildAssetKind,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "guilds.assets.manage"
    )
    rendered = await delete_guild_asset(
        guild_ref,
        kind,
        user_auth(principal),
        session,
        redis,
        settings,
    )
    return rendered | bot_guild_installation_payload(installation)


@router.post("/guilds/{guild_ref}/roles/{role_ref}/icon", status_code=201)
async def bot_create_role_icon_ticket(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "roles.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    await require_permissions(
        session,
        redis,
        guild,
        principal.user,
        required_permissions("role.update"),
    )
    await local_manageable_role(session, settings, guild, principal.user, role_ref)
    return await issue_image_asset_ticket(
        session,
        redis,
        snowflake,
        settings,
        principal.user,
        payload,
        response,
        purpose="role_icon",
        bot_installation=installation,
        max_bytes=settings.media_max_emoji_bytes,
        too_large_code="ROLE_ICON_TOO_LARGE",
    )


@router.put("/guilds/{guild_ref}/roles/{role_ref}/icon")
async def bot_commit_role_icon(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    payload: AssetCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "roles.manage"
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await commit_role_icon(
        guild_ref,
        role_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete("/guilds/{guild_ref}/roles/{role_ref}/icon")
async def bot_delete_role_icon(
    guild_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await delete_role_icon(
        guild_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/members/{user_ref}/roles/{role_ref}", status_code=204)
async def bot_assign_role(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await assign_role(
        guild_ref,
        user_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.put("/guilds/{guild_ref}/members/{user_ref}/roles")
async def bot_replace_member_roles(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: MemberRoleSet,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await replace_member_roles(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}/roles/{role_ref}", status_code=204)
async def bot_remove_role(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    role_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "roles.manage")
    return await remove_role(
        guild_ref,
        user_ref,
        role_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/channels/{channel_ref}/attachments", status_code=201)
async def bot_create_attachment_ticket(
    channel_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "attachments.write",
        installation_id,
    )
    if channel.guild_id is not None:
        if not isinstance(installation, BotInstallation):
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        guild = await session.get(Guild, (installation.guild_id, installation.guild_domain))
        if guild is None:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        permissions = await get_permissions(session, redis, guild, principal.user, channel=channel)
        if not permissions & Permission.ATTACH_FILES:
            raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    expected_mode = "e2ee" if channel.encryption_mode == "e2ee" else "plaintext"
    if channel.encryption_mode == "e2ee" and installation.e2ee_mode != "participant":
        raise HTTPException(status_code=409, detail={"code": "BOT_E2EE_DISABLED"})
    if expected_mode == "e2ee" and channel.encryption_state != "active":
        raise HTTPException(status_code=409, detail={"code": "E2EE_REKEY_REQUIRED"})
    if payload.encryption_mode != expected_mode:
        raise HTTPException(
            status_code=409,
            detail={
                "code": (
                    "E2EE_ATTACHMENT_REQUIRED" if expected_mode == "e2ee" else "E2EE_NOT_ENABLED"
                )
            },
        )
    if expected_mode == "e2ee":
        await require_bot_channel_e2ee_access(
            session,
            channel,
            installation,
            e2ee_device_id,
            worker_id=principal.worker.id,
        )
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=principal.user.id,
        user_domain=principal.user.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode=payload.encryption_mode,
        encryption_protocol=payload.encryption_protocol,
        duration_secs=payload.duration_secs,
        waveform=payload.waveform,
        bot_installation=installation,
    )
    attachment.upload_channel_id = channel.id
    attachment.upload_channel_domain = channel.origin_domain
    await session.commit()
    return ticket_payload(attachment, upload_url) | bot_runtime_grant_payload(installation)


@router.get("/attachments/{attachment_ref}")
async def bot_attachment_status(
    attachment_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    attachment, installation = await installation_attachment(
        session,
        settings,
        principal,
        attachment_ref,
        "attachments.read",
        require_bound_message=False,
        installation_id=installation_id,
    )
    message: Message | None = None
    channel: Channel | None = None
    if attachment.message_id is not None and attachment.message_domain is not None:
        message = await session.get(Message, (attachment.message_id, attachment.message_domain))
        if message is None or message.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if channel is None:
            raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
        if channel.guild_id is not None:
            guild = await session.get(Guild, (channel.guild_id, channel.guild_domain))
            if guild is None:
                raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
            permissions = await get_permissions(
                session, redis, guild, principal.user, channel=channel
            )
            if permissions & (Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY) != (
                Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
            ):
                raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await require_bot_attachment_e2ee_access(
        session,
        settings,
        principal,
        attachment,
        installation,
        e2ee_device_id,
        message=message,
        channel=channel,
    )
    return attachment_payload(attachment) | bot_runtime_grant_payload(installation)


@router.get("/attachments/{attachment_ref}/{variant}")
async def bot_download_attachment(
    attachment_ref: EntityRef,
    variant: str,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> RedirectResponse:
    if variant not in {"original", "thumbnail_128", "thumbnail_512", "thumbnail_1024", "poster"}:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_VARIANT_NOT_FOUND"})
    attachment, installation = await installation_attachment(
        session,
        settings,
        principal,
        attachment_ref,
        "attachments.read",
        require_bound_message=True,
        installation_id=installation_id,
    )
    if attachment.message_id is None or attachment.message_domain is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    message = await session.get(Message, (attachment.message_id, attachment.message_domain))
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    if channel.guild_id is not None and (channel.guild_id, channel.guild_domain) != (
        installation.guild_id,
        installation.guild_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    _, channel_installation = await installation_for_channel(
        session,
        settings,
        principal,
        EntityRef(f"{channel.id}@{channel.origin_domain}"),
        "attachments.read",
        installation_id,
    )
    if channel_installation.id != installation.id:
        raise HTTPException(status_code=404, detail={"code": "MEDIA_NOT_FOUND"})
    await require_bot_attachment_e2ee_access(
        session,
        settings,
        principal,
        attachment,
        installation,
        e2ee_device_id,
        message=message,
        channel=channel,
    )
    return await authorized_attachment(
        attachment.origin_domain,
        attachment.id,
        response,
        variant,
        user_auth(principal),
        session,
        redis,
        settings,
        snowflake,
    )


@router.get("/dm-capabilities")
async def list_bot_dm_capabilities(
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: int = Query(default=100, ge=1, le=100),
    after: str | None = Query(default=None, min_length=48, max_length=48),
) -> dict[str, object]:
    """List opaque, refreshable DM grants retained at application home A."""

    principal.require_scope("dm.send")
    query = (
        select(BotDMCapability, Channel)
        .join(
            Channel,
            (Channel.id == BotDMCapability.conversation_id)
            & (Channel.origin_domain == BotDMCapability.conversation_domain),
        )
        .where(
            BotDMCapability.application_id == principal.application.id,
            BotDMCapability.application_domain == principal.application.origin_domain,
            BotDMCapability.bot_user_id == principal.user.id,
            BotDMCapability.bot_user_domain == principal.user.origin_domain,
            BotDMCapability.status.in_(["active", "suspended"]),
            BotDMCapability.conversation_id.is_not(None),
            Channel.type == 1,
            Channel.guild_id.is_(None),
            Channel.unavailable.is_(False),
        )
        .order_by(BotDMCapability.grant_id)
        .limit(limit + 1)
    )
    if after is not None:
        if not after.startswith("kbdg_"):
            raise HTTPException(status_code=400, detail={"code": "BOT_DM_GRANT_INVALID"})
        query = query.where(BotDMCapability.grant_id > after)
    rows = list((await session.execute(query)).all())
    page = rows[:limit]
    return {
        "items": [
            bot_dm_capability_bootstrap_payload(capability, channel) for capability, channel in page
        ],
        "next_after": page[-1][0].grant_id if len(rows) > limit and page else None,
    }


async def _commit_local_bot_dm_capability_fence(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    capability: BotDMCapability,
) -> None:
    expectation = bot_dm_capability_fence_expectation(capability)
    _fenced, channels = await fence_bot_dm_capability(
        session,
        redis,
        settings,
        expectation,
    )
    await session.commit()
    if channels:
        await publish_e2ee_policy_updates(session, redis, settings, channels)


async def relay_refreshed_bot_dm_capability(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    capability: BotDMCapability,
    proof: dict[str, object],
    runtime_proof: EventEnvelope,
) -> None:
    """Synchronously bind B's renewed proof to the existing C conversation."""

    if capability.conversation_id is None or capability.conversation_domain is None:
        raise HTTPException(status_code=409, detail={"code": "BOT_DM_GRANT_UNBOUND"})

    if capability.authority_domain == settings.domain:
        try:
            source_payload = stored_bot_dm_capability_payload(capability)
            source_proof = EventEnvelope.model_validate(capability.proof)
            await validate_bot_dm_capability_at_source(
                session,
                settings,
                source_proof,
                source_payload,
            )
            if source_payload.installation.domain == settings.domain:
                from app.api.bot_dm_federation import require_current_bot_dm_entitlement

                try:
                    await require_current_bot_dm_entitlement(
                        session,
                        settings,
                        source_payload,
                    )
                except HTTPException as exc:
                    raise BotDMCapabilitySourceRejected(
                        "installation authority no longer recognizes the DM grant"
                    ) from exc
        except BotDMCapabilitySourceRejected:
            await _commit_local_bot_dm_capability_fence(session, redis, settings, capability)
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_FENCED"},
            ) from None
        except BotDMCapabilityAuthorityUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"code": "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"},
            ) from None
        except (BotDMCapabilityProofInvalid, ValueError):
            raise HTTPException(
                status_code=502,
                detail={"code": "BOT_DM_INSTALLATION_PROOF_INVALID"},
            ) from None
        bot = await session.get(
            User,
            (capability.bot_user_id, capability.bot_user_domain),
        )
        target = await session.get(
            User,
            (capability.target_user_id, capability.target_user_domain),
        )
        if (
            bot is None
            or bot.account_type != "bot"
            or target is None
            or target.account_type != "human"
            or target.disabled_at is not None
        ):
            await _commit_local_bot_dm_capability_fence(session, redis, settings, capability)
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
        if target.origin_domain == settings.domain:
            await lock_dm_policy(session, bot, target)
            if await blocked_between(session, bot, target):
                await _commit_local_bot_dm_capability_fence(session, redis, settings, capability)
                raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
            return
        authorization_payload = {
            "participants": [profile_from_user(user) for user in (bot, target)],
            "bot_capability": proof,
            "bot_runtime_proof": runtime_proof.model_dump(mode="json"),
        }
        try:
            authorization = await signed_request(
                session,
                settings,
                "POST",
                target.origin_domain,
                "/_kaede/v1/dm/authorize",
                payload=authorization_payload,
                request_timeout=8,
                max_response_bytes=16 * 1024,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "BOT_DM_AUTHORITY_UNAVAILABLE"},
            ) from None
        if authorization.status_code != 200:
            if authorization.status_code == 403:
                await _commit_local_bot_dm_capability_fence(session, redis, settings, capability)
                raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
            if authorization.status_code == 429 or authorization.status_code >= 500:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "BOT_DM_AUTHORITY_UNAVAILABLE"},
                )
            raise HTTPException(status_code=502, detail={"code": "BOT_DM_AUTHORITY_INVALID"})
        return
    request = BotDMCapabilityApplyRequest(
        proof=proof,
        runtime_proof=runtime_proof.model_dump(mode="json"),
        grant_id=capability.grant_id,
        revision=str(capability.revision),
        conversation_ref=f"{capability.conversation_id}@{capability.conversation_domain}",
    )
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            capability.authority_domain,
            "/_kaede/v1/bot-dm/capabilities/apply",
            payload=request.model_dump(mode="json"),
            request_timeout=8,
            max_response_bytes=16 * 1024,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_AUTHORITY_UNAVAILABLE"},
        ) from None
    if response.status_code in {429} or response.status_code >= 500:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_AUTHORITY_UNAVAILABLE"},
        )
    if response.status_code == 403:
        try:
            rejection = decode_federation_response_json(response, max_response_bytes=16 * 1024)
        except FederationNetworkError:
            rejection = None
        detail = rejection.get("detail") if isinstance(rejection, dict) else None
        rejection_code = detail.get("code") if isinstance(detail, dict) else None
        if rejection_code == "BOT_DM_GRANT_FENCED":
            await _commit_local_bot_dm_capability_fence(
                session,
                redis,
                settings,
                capability,
            )
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_FENCED"},
            )
        raise HTTPException(status_code=502, detail={"code": "BOT_DM_AUTHORITY_INVALID"})
    if response.status_code in {401, 404, 409}:
        raise HTTPException(status_code=502, detail={"code": "BOT_DM_AUTHORITY_INVALID"})
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail={"code": "BOT_DM_AUTHORITY_INVALID"})
    try:
        rendered = decode_federation_response_json(response, max_response_bytes=16 * 1024)
        if not isinstance(rendered, dict) or rendered != {
            "grant_id": capability.grant_id,
            "revision": str(capability.revision),
            "conversation_ref": (f"{capability.conversation_id}@{capability.conversation_domain}"),
            "expires_at_ms": str(int(capability.expires_at.timestamp() * 1000)),
        }:
            raise ValueError("DM authority changed refreshed capability identity")
    except (FederationNetworkError, ValueError):
        raise HTTPException(status_code=502, detail={"code": "BOT_DM_AUTHORITY_INVALID"}) from None


@router.post("/dm-capabilities/{grant_id}/refresh")
async def refresh_bot_dm_capability(
    grant_id: str,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Refresh one opaque grant by immutable identity, then apply it at C."""

    principal.require_scope("dm.send")
    application = await locked_active_principal_application(session, principal)
    capability = await session.scalar(
        select(BotDMCapability)
        .where(
            BotDMCapability.grant_id == grant_id,
            BotDMCapability.application_id == principal.application.id,
            BotDMCapability.application_domain == principal.application.origin_domain,
            BotDMCapability.bot_user_id == principal.user.id,
            BotDMCapability.bot_user_domain == principal.user.origin_domain,
            usable_dm_capability(),
            BotDMCapability.conversation_id.is_not(None),
        )
        .with_for_update()
    )
    if capability is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_DM_GRANT_NOT_FOUND"})
    require_worker_dm_target(principal, capability.source_installation_domain)
    require_worker_dm_target(principal, capability.authority_domain)
    target_rules = await locked_application_target_rules(session, application)
    require_application_dm_target_allowed(
        application,
        target_rules,
        capability.source_installation_domain,
    )
    require_application_dm_target_allowed(
        application,
        target_rules,
        capability.authority_domain,
    )
    runtime_destinations = await queue_application_runtime_snapshots(
        session,
        settings,
        application,
        additional_target_domains={
            capability.source_installation_domain,
            capability.authority_domain,
        },
    )
    await session.commit()
    await wake_application_runtime_deliveries(runtime_destinations)

    application = await locked_active_principal_application(session, principal)
    target_rules = await locked_application_target_rules(session, application)
    capability = await session.scalar(
        select(BotDMCapability)
        .where(
            BotDMCapability.grant_id == grant_id,
            BotDMCapability.application_id == principal.application.id,
            BotDMCapability.application_domain == principal.application.origin_domain,
            BotDMCapability.bot_user_id == principal.user.id,
            BotDMCapability.bot_user_domain == principal.user.origin_domain,
            usable_dm_capability(),
            BotDMCapability.conversation_id.is_not(None),
        )
        .with_for_update()
    )
    if capability is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_DM_GRANT_NOT_FOUND"})
    require_worker_dm_target(principal, capability.source_installation_domain)
    require_worker_dm_target(principal, capability.authority_domain)
    require_application_dm_target_allowed(
        application,
        target_rules,
        capability.source_installation_domain,
    )
    require_application_dm_target_allowed(
        application,
        target_rules,
        capability.authority_domain,
    )
    source_runtime_proof, authority_runtime_proof = await current_bot_dm_runtime_proofs(
        session,
        settings,
        application,
        source_domain=capability.source_installation_domain,
        authority_domain=capability.authority_domain,
    )
    try:
        proof, _payload, capability = await refresh_bot_dm_capability_proof(
            session,
            settings,
            snowflake,
            redis,
            capability,
            source_runtime_proof=source_runtime_proof,
            authority_runtime_proof=authority_runtime_proof,
        )
    except BotDMCapabilitySourceRejected:
        await relay_refreshed_bot_dm_capability(
            session,
            redis,
            settings,
            capability,
            capability.proof,
            authority_runtime_proof,
        )
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"}) from None
    except PermissionError:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"}) from None
    except BotDMCapabilityAuthorityUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"},
        ) from None
    except BotDMCapabilityProofInvalid:
        raise HTTPException(
            status_code=502,
            detail={"code": "BOT_DM_INSTALLATION_PROOF_INVALID"},
        ) from None
    await relay_refreshed_bot_dm_capability(
        session,
        redis,
        settings,
        capability,
        proof.model_dump(mode="json"),
        authority_runtime_proof,
    )
    channel = await session.get(
        Channel,
        (capability.conversation_id, capability.conversation_domain),
    )
    if channel is None or channel.unavailable:
        raise HTTPException(status_code=409, detail={"code": "BOT_DM_GRANT_UNBOUND"})
    await session.commit()
    return bot_dm_capability_bootstrap_payload(capability, channel)


@router.post("/dms")
async def bot_open_direct_message(
    payload: DMOpenRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_application_home_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_ref_raw: str | None = Header(
        default=None,
        alias="X-Kaede-Bot-Installation",
    ),
    installation_kind: str = Header(
        default="guild",
        alias="X-Kaede-Bot-Installation-Type",
    ),
) -> dict[str, object]:
    principal.require_scope("dm.send")
    if installation_ref_raw is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_INSTALLATION_REQUIRED"})
    try:
        installation_ref = EntityRef(installation_ref_raw)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"code": "BOT_INSTALLATION_REF_INVALID"},
        ) from None
    if installation_ref.domain is None or installation_kind not in {"guild", "user"}:
        raise HTTPException(
            status_code=400,
            detail={"code": "BOT_INSTALLATION_REF_INVALID"},
        )
    require_worker_dm_target(principal, installation_ref.domain)
    source_kind: Literal["guild", "user"] = "guild" if installation_kind == "guild" else "user"
    application = await locked_active_principal_application(session, principal)
    target_rules = await locked_application_target_rules(session, application)
    require_application_dm_target_allowed(
        application,
        target_rules,
        installation_ref.domain,
    )
    requester_key = f"{principal.user.origin_domain}:{principal.user.id}"
    target = await resolve_handle(session, settings, redis, requester_key, payload.handle)
    first_handle = f"{principal.user.username}@{principal.user.origin_domain}"
    second_handle = f"{target.username}@{target.origin_domain}"
    pair_key = dm_pair_key(first_handle, second_handle)
    authority_domain = dm_authority_domain(first_handle, second_handle)
    require_worker_dm_target(principal, authority_domain)
    require_application_dm_target_allowed(
        application,
        target_rules,
        authority_domain,
    )
    runtime_destinations = await queue_application_runtime_snapshots(
        session,
        settings,
        application,
        additional_target_domains={installation_ref.domain, authority_domain},
    )
    # Persist A's target epochs and durable async controls before any remote
    # admission call. If privacy/install admission later fails, B/C can still
    # converge and A will never forget a signed generation it already exposed.
    await session.commit()
    await wake_application_runtime_deliveries(runtime_destinations)

    application = await locked_active_principal_application(session, principal)
    target_rules = await locked_application_target_rules(session, application)
    require_worker_dm_target(principal, installation_ref.domain)
    require_worker_dm_target(principal, authority_domain)
    require_application_dm_target_allowed(
        application,
        target_rules,
        installation_ref.domain,
    )
    require_application_dm_target_allowed(
        application,
        target_rules,
        authority_domain,
    )
    source_runtime_proof, authority_runtime_proof = await current_bot_dm_runtime_proofs(
        session,
        settings,
        application,
        source_domain=installation_ref.domain,
        authority_domain=authority_domain,
    )
    try:
        proof, capability, _ = await fetch_bot_dm_capability_proof(
            session,
            settings,
            snowflake,
            redis,
            source_kind=source_kind,
            installation_ref=installation_ref,
            application_ref=EntityRef(f"{application.id}@{application.origin_domain}"),
            bot=principal.user,
            target=target,
            pair_key=pair_key,
            authority_domain=authority_domain,
            source_runtime_proof=source_runtime_proof,
            authority_runtime_proof=authority_runtime_proof,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"}) from None
    except BotDMCapabilityAuthorityUnavailable:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"},
        ) from None
    except BotDMCapabilityProofInvalid:
        raise HTTPException(
            status_code=502,
            detail={"code": "BOT_DM_INSTALLATION_PROOF_INVALID"},
        ) from None
    result = await open_direct_message_for(
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        resolved_target=target,
        bot_capability=(proof, capability, authority_runtime_proof),
    )
    result["bot_installation_ref"] = str(installation_ref)
    result["bot_installation_type"] = source_kind
    result["bot_dm_capability_id"] = capability.grant_id
    result["bot_dm_capability_revision"] = str(capability.revision)
    result["bot_dm_capability_expires_at"] = capability.expires_at.isoformat()
    return result


async def bot_dm_call_context(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    channel_ref: EntityRef,
) -> tuple[Channel, DMConversation, BotDMCapability]:
    channel, grant = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "voice.connect",
    )
    if not isinstance(grant, BotDMCapability) or channel.guild_id is not None or channel.type != 1:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    conversation = await session.get(
        DMConversation,
        (channel.id, channel.origin_domain),
    )
    if conversation is None or conversation.authority_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    return channel, conversation, grant


def require_call_channel(
    record: dict[str, object],
    channel: Channel,
    call_ref: EntityRef,
    settings: Settings,
) -> None:
    call_id, call_domain = call_ref.resolve(settings.domain)
    if (
        call_domain != settings.domain
        or str(record.get("id")) != str(call_id)
        or str(record.get("authority_domain")) != settings.domain
        or str(record.get("channel_id")) != str(channel.id)
        or str(record.get("channel_domain")) != channel.origin_domain
    ):
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})


@router.post(
    "/channels/{channel_ref}/calls",
    response_model=BotCallResponse,
    status_code=201,
)
async def bot_start_call(
    channel_ref: EntityRef,
    payload: CallCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BotCallResponse:
    require_voice_enabled(settings)
    channel, conversation, capability = await bot_dm_call_context(
        session, settings, principal, channel_ref
    )
    participants = await local_dm_participants(session, channel.id, channel.origin_domain)
    identities = {participant_identity(item.id, item.origin_domain) for item in participants}
    caller = participant_identity(principal.user.id, principal.user.origin_domain)
    call_id = await snowflake.mint()
    record: dict[str, Any] = {
        "id": str(call_id),
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
        "authority_domain": settings.domain,
        "room": dm_room_name(channel.id, call_id),
        "state": "ringing",
        "created_at": int(time.time()),
        "ended_at": None,
        "caller": caller,
        "participants": sorted(identities),
    }
    bindings = await active_bot_call_capability_bindings(
        session,
        settings,
        channel,
        identities,
        preferred=capability,
    )
    record[BOT_CAPABILITY_BINDINGS_FIELD] = bindings
    require_call_bot_capability(record, capability)
    await require_call_policy(
        session,
        settings,
        record,
        principal.user,
        participants,
    )
    if not await create_call(
        redis,
        record,
        identities,
        settings,
        accepted={caller},
    ):
        raise HTTPException(status_code=409, detail={"code": "CALL_ALREADY_ACTIVE"})
    await notify_call(
        session,
        redis,
        sorted(identities),
        "CALL_CREATE",
        record,
        settings,
    )
    if payload.ring:
        await notify_call(
            session,
            redis,
            sorted(identities - {caller}),
            "CALL_RING",
            record,
            settings,
        )
    await propagate_call_create(
        session,
        settings,
        record,
        actor=principal.user,
        state_version=conversation.state_version if conversation.type == "group" else None,
        exclude_domains={principal.user.origin_domain},
    )
    return bot_call_response(record, capability)


@router.get(
    "/channels/{channel_ref}/calls/active",
    response_model=BotActiveCallResponse,
)
async def bot_active_call(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BotActiveCallResponse:
    channel, _conversation, capability = await bot_dm_call_context(
        session, settings, principal, channel_ref
    )
    record = await get_active_call(redis, channel.origin_domain, channel.id)
    if record is None or record.get("state") == "ended":
        return BotActiveCallResponse(call=None)
    require_call_bot_capability(record, capability)
    await require_call_policy(session, settings, record, principal.user)
    identity = participant_identity(principal.user.id, principal.user.origin_domain)
    joined = identity == record.get("caller") or await is_call_accepted(
        redis,
        settings.domain,
        int(record["id"]),
        identity,
    )
    return BotActiveCallResponse(call=bot_call_response(record, capability), joined=joined)


@router.post(
    "/channels/{channel_ref}/calls/{call_ref}",
    response_model=BotCallResponse,
)
async def bot_act_on_call(
    channel_ref: EntityRef,
    call_ref: EntityRef,
    payload: CallAction,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BotCallResponse:
    channel, _conversation, capability = await bot_dm_call_context(
        session, settings, principal, channel_ref
    )
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    require_call_channel(record, channel, call_ref, settings)
    require_call_bot_capability(record, capability)
    await require_call_policy(session, settings, record, principal.user)
    identity = participant_identity(principal.user.id, principal.user.origin_domain)
    accepted, changed, result = await transition_call(
        redis,
        authority,
        call_id,
        identity,
        payload.action,
        settings,
    )
    if not accepted:
        code = "CALL_NOT_FOUND" if result == "missing" else "CALL_INVALID_TRANSITION"
        raise HTTPException(status_code=404 if result == "missing" else 409, detail={"code": code})
    updated = cast(dict[str, Any], result)
    event = {"accept": "CALL_ACCEPT", "decline": "CALL_DECLINE", "end": "CALL_END"}[payload.action]
    await project_call_transition(
        redis,
        session,
        settings,
        updated,
        event,
        changed=changed,
    )
    return bot_call_response(updated, capability)


@router.post(
    "/channels/{channel_ref}/calls/{call_ref}/voice/token",
    response_model=VoiceTokenResponse,
)
async def bot_call_voice_token(
    channel_ref: EntityRef,
    call_ref: EntityRef,
    payload: BotVoiceTokenRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> VoiceTokenResponse:
    require_voice_enabled(settings)
    channel, _conversation, capability = await bot_dm_call_context(
        session, settings, principal, channel_ref
    )
    if payload.listen:
        require_installation_scope(principal, capability, "voice.listen")
    if payload.speak:
        require_installation_scope(principal, capability, "voice.speak")
    if payload.stream:
        require_installation_scope(principal, capability, "voice.stream")
    call_id, authority = call_ref.resolve(settings.domain)
    record = await get_call(redis, authority, call_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "CALL_NOT_FOUND"})
    require_call_channel(record, channel, call_ref, settings)
    require_call_bot_capability(record, capability)
    return await mint_dm_call_token(
        session,
        redis,
        settings,
        record,
        principal.user,
        sender_device_id=payload.sender_device_id,
        connection_id=payload.connection_id or secrets.token_urlsafe(32),
        takeover=payload.takeover,
        client_kind="bot",
        bot_capability=capability,
        bot_worker=principal.worker,
        allow_listen=payload.listen,
        allow_speak=payload.speak,
        allow_stream=payload.stream,
    )


async def bot_invite_management_scope(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    code: str,
) -> tuple[Guild, str]:
    """Bind a bot invite operation to its installed guild authority."""

    guild, _installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "invites.manage",
    )
    return guild, bot_invite_code_for_guild(guild, code)


def bot_invite_code_for_guild(guild: Guild, code: str) -> str:
    """Reject an invite code explicitly qualified to another authority."""

    bare_code, supplied_authority = parse_invite_management_code(code)
    if supplied_authority is not None and supplied_authority != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return bare_code


@router.post("/guilds/{guild_ref}/invites")
async def bot_create_invite(
    guild_ref: EntityRef,
    payload: InviteCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "invites.manage")
    return await create_invite(
        guild_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/invites")
async def bot_list_invites(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "invites.read",
        "invites.manage",
    )
    return await list_invites(guild_ref, user_auth(principal), session, redis, settings)


@router.get("/guilds/{guild_ref}/invites/{code}")
async def bot_get_invite(
    guild_ref: EntityRef,
    code: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, _installation = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "invites.read",
        "invites.manage",
    )
    bare_code = bot_invite_code_for_guild(guild, code)
    return await get_managed_invite(
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        bare_code,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get("/guilds/{guild_ref}/channels/{channel_ref}/invites")
async def bot_list_channel_invites(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _installation = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "invites.read",
        "invites.manage",
    )
    channel_id, channel_domain = channel_ref.resolve(guild.origin_domain)
    if channel_domain != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    return await list_channel_invites(
        EntityRef(f"{channel_id}@{channel_domain}"),
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete("/guilds/{guild_ref}/invites/{code}")
async def bot_revoke_invite(
    guild_ref: EntityRef,
    code: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _guild, bare_code = await bot_invite_management_scope(
        session,
        settings,
        principal,
        guild_ref,
        code,
    )
    return await revoke_invite(
        bare_code,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/invites/{code}/target-users")
async def bot_get_invite_target_users(
    guild_ref: EntityRef,
    code: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, bare_code = await bot_invite_management_scope(
        session,
        settings,
        principal,
        guild_ref,
        code,
    )
    return await local_get_invite_target_users(
        bare_code,
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/invites/{code}/target-users")
async def bot_update_invite_target_users(
    guild_ref: EntityRef,
    code: str,
    payload: BotInviteTargetUsersPut,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, bare_code = await bot_invite_management_scope(
        session,
        settings,
        principal,
        guild_ref,
        code,
    )
    return await local_update_invite_target_users(
        bare_code,
        [str(user_ref) for user_ref in payload.target_user_ids],
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/invites/{code}/target-users/job-status")
async def bot_get_invite_target_users_job_status(
    guild_ref: EntityRef,
    code: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, bare_code = await bot_invite_management_scope(
        session,
        settings,
        principal,
        guild_ref,
        code,
    )
    return await local_get_invite_target_users_job_status(
        bare_code,
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        user_auth(principal),
        session,
        redis,
        settings,
    )


def _deny_bot_channel_restriction(*, not_found_code: str | None = None) -> NoReturn:
    if not_found_code is not None:
        raise HTTPException(status_code=404, detail={"code": not_found_code})
    raise HTTPException(status_code=403, detail={"code": "BOT_CHANNEL_RESTRICTED"})


async def _bot_scheduled_event_allowed(
    session: AsyncSession,
    guild: Guild,
    installation: BotInstallation,
    event: GuildScheduledEvent,
) -> bool:
    """Treat a channel-backed event as one indivisible restricted resource."""

    if event.entity_type not in {1, 2}:
        return event.entity_type == 3 and event.channel_id is None and event.channel_domain is None
    if event.channel_id is None or event.channel_domain is None:
        return False
    return (
        await installation_accessible_channel(
            session,
            installation,
            guild,
            EntityRef(f"{event.channel_id}@{event.channel_domain}"),
        )
        is not None
    )


async def _bot_scheduled_event_payload_allowed(
    session: AsyncSession,
    guild: Guild,
    installation: BotInstallation,
    payload: dict[str, object],
) -> bool:
    """Apply the same event channel ceiling to already-rendered list rows."""

    raw_type = payload.get("entity_type")
    if raw_type not in {1, 2}:
        return (
            raw_type == 3
            and payload.get("channel_id") is None
            and payload.get("channel_domain") is None
        )
    raw_id = payload.get("channel_id")
    raw_domain = payload.get("channel_domain")
    if not isinstance(raw_id, str) or not isinstance(raw_domain, str):
        return False
    try:
        channel_ref = EntityRef(f"{raw_id}@{raw_domain}")
    except ValueError:
        return False
    return (
        await installation_accessible_channel(
            session,
            installation,
            guild,
            channel_ref,
        )
        is not None
    )


async def _require_bot_scheduled_event(
    session: AsyncSession,
    guild: Guild,
    installation: BotInstallation,
    event_ref: EntityRef,
    *,
    for_update: bool = False,
    not_found: bool = False,
) -> GuildScheduledEvent:
    event = await scheduled_event_for_guild(
        session,
        guild,
        event_ref,
        for_update=for_update,
    )
    if not await _bot_scheduled_event_allowed(session, guild, installation, event):
        _deny_bot_channel_restriction(
            not_found_code="SCHEDULED_EVENT_NOT_FOUND" if not_found else None
        )
    return event


async def _require_bot_requested_channel(
    session: AsyncSession,
    guild: Guild,
    installation: BotInstallation,
    channel_ref: EntityRef,
) -> Channel:
    channel = await installation_accessible_channel(
        session,
        installation,
        guild,
        channel_ref,
    )
    if channel is None:
        _deny_bot_channel_restriction()
    return channel


@router.get("/guilds/{guild_ref}/scheduled-events")
async def bot_list_scheduled_events(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    with_user_count: bool = Query(default=False),
) -> list[dict[str, object]]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.read"
    )
    rendered = await list_scheduled_events(
        guild_ref,
        with_user_count,
        user_auth(principal),
        session,
        redis,
        settings,
    )
    return [
        item
        for item in rendered
        if await _bot_scheduled_event_payload_allowed(
            session,
            guild,
            installation,
            item,
        )
    ]


@router.post("/guilds/{guild_ref}/scheduled-events")
async def bot_create_scheduled_event(
    guild_ref: EntityRef,
    payload: ScheduledEventCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    if payload.channel_id is not None:
        await _require_bot_requested_channel(
            session,
            guild,
            installation,
            payload.channel_id,
        )
    return await create_scheduled_event(
        guild_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/scheduled-events/{event_ref}")
async def bot_get_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    with_user_count: bool = Query(default=False),
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.read"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        not_found=True,
    )
    return await get_scheduled_event(
        guild_ref,
        event_ref,
        with_user_count,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.patch("/guilds/{guild_ref}/scheduled-events/{event_ref}")
async def bot_patch_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: ScheduledEventPatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        for_update=True,
    )
    if "channel_id" in payload.model_fields_set and payload.channel_id is not None:
        await _require_bot_requested_channel(
            session,
            guild,
            installation,
            payload.channel_id,
        )
    return await patch_scheduled_event(
        guild_ref,
        event_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/scheduled-events/{event_ref}", status_code=204)
async def bot_delete_scheduled_event(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        for_update=True,
    )
    return await delete_scheduled_event(
        guild_ref,
        event_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/scheduled-events/{event_ref}/users")
async def bot_list_scheduled_event_users(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=100, ge=1, le=100),
    before: EntityRef | None = None,
    after: EntityRef | None = None,
    with_member: bool = Query(default=False),
) -> list[dict[str, object]]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.read"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        not_found=True,
    )
    return await list_scheduled_event_users(
        guild_ref,
        event_ref,
        limit,
        before,
        after,
        with_member,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.post(
    "/guilds/{guild_ref}/scheduled-events/{event_ref}/image/tickets",
    status_code=201,
)
async def bot_create_scheduled_event_image_ticket(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    event = await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        for_update=True,
    )
    return await create_scheduled_event_image_ticket_for(
        session,
        redis,
        response,
        settings,
        snowflake,
        guild,
        event,
        principal.user,
        payload,
        bot_installation=installation,
    )


@router.put("/guilds/{guild_ref}/scheduled-events/{event_ref}/image")
async def bot_commit_scheduled_event_image(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    payload: AssetCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        for_update=True,
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await commit_scheduled_event_image(
        guild_ref,
        event_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/scheduled-events/{event_ref}/image")
async def bot_delete_scheduled_event_image(
    guild_ref: EntityRef,
    event_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "events.manage"
    )
    await _require_bot_scheduled_event(
        session,
        guild,
        installation,
        event_ref,
        for_update=True,
    )
    return await delete_scheduled_event_image(
        guild_ref,
        event_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/channels/{channel_ref}/webhooks")
async def bot_create_webhook(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    payload: WebhookCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "webhooks.manage"
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    return await create_webhook(
        guild_ref,
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/webhooks")
async def bot_list_webhooks(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    _, installation = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "webhooks.read",
        "webhooks.manage",
    )
    can_manage = (
        "webhooks.manage" in principal.scopes and "webhooks.manage" in installation.granted_scopes
    )
    return await list_webhooks(
        guild_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        can_manage,
    )


@router.get("/guilds/{guild_ref}/channels/{channel_ref}/webhooks")
async def bot_list_channel_webhooks(
    guild_ref: EntityRef,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, installation = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "webhooks.read",
        "webhooks.manage",
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    return await list_channel_webhooks(
        guild_ref,
        channel_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        (
            "webhooks.manage" in principal.scopes
            and "webhooks.manage" in installation.granted_scopes
        ),
    )


async def bot_guild_webhook(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    guild_ref: EntityRef,
    webhook_id: int,
    *,
    manage: bool = False,
) -> tuple[Guild, BotInstallation, EntityRef, Channel]:
    guild, installation = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "webhooks.manage" if manage else "webhooks.read",
        *(() if manage else ("webhooks.manage",)),
    )
    webhook = await session.get(Webhook, webhook_id)
    if webhook is not None and (webhook.guild_id, webhook.guild_domain) == (
        guild.id,
        guild.origin_domain,
    ):
        channel_ref = EntityRef(f"{webhook.channel_id}@{webhook.channel_domain}")
    else:
        follow = await target_follower_webhook(session, webhook_id, settings.domain)
        if follow is None:
            raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
        channel_ref = EntityRef(f"{follow.target_channel_id}@{follow.target_channel_domain}")
    channel = await installation_accessible_channel(
        session,
        installation,
        guild,
        channel_ref,
    )
    if channel is None:
        _deny_bot_channel_restriction(not_found_code="WEBHOOK_NOT_FOUND")
    return (
        guild,
        installation,
        EntityRef(f"{webhook_id}@{guild.origin_domain}"),
        channel,
    )


@router.get("/guilds/{guild_ref}/webhooks/{webhook_id}")
async def bot_get_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
    )
    return await get_webhook(
        webhook_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        (
            "webhooks.manage" in principal.scopes
            and "webhooks.manage" in installation.granted_scopes
        ),
    )


@router.patch("/guilds/{guild_ref}/webhooks/{webhook_id}")
async def bot_update_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    payload: WebhookPatch,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    if "channel_id" in payload.model_fields_set and payload.channel_id is not None:
        await _require_bot_requested_channel(
            session,
            guild,
            installation,
            payload.channel_id,
        )
    return await patch_webhook(
        webhook_ref,
        payload,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/webhooks/{webhook_id}/rotate")
async def bot_rotate_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, _, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    return await rotate_webhook(
        webhook_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/webhooks/{webhook_id}", status_code=204)
async def bot_delete_webhook(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    _, _, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    return await delete_webhook(
        webhook_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/webhooks/{webhook_id}/e2ee/channels/{channel_ref}")
async def bot_get_webhook_e2ee_participation(
    guild_ref: EntityRef,
    webhook_id: int,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation, webhook_ref, _ = await bot_guild_webhook(
        session, settings, principal, guild_ref, webhook_id, manage=True
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    return await get_webhook_e2ee_participation(
        webhook_ref,
        channel_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.put("/guilds/{guild_ref}/webhooks/{webhook_id}/e2ee/channels/{channel_ref}")
async def bot_grant_webhook_e2ee_participation(
    guild_ref: EntityRef,
    webhook_id: int,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation, webhook_ref, _ = await bot_guild_webhook(
        session, settings, principal, guild_ref, webhook_id, manage=True
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    return await grant_webhook_e2ee_participation(
        webhook_ref,
        channel_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/webhooks/{webhook_id}/e2ee/channels/{channel_ref}")
async def bot_revoke_webhook_e2ee_participation(
    guild_ref: EntityRef,
    webhook_id: int,
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    guild, installation, webhook_ref, _ = await bot_guild_webhook(
        session, settings, principal, guild_ref, webhook_id, manage=True
    )
    await _require_bot_requested_channel(
        session,
        guild,
        installation,
        channel_ref,
    )
    return await revoke_webhook_e2ee_participation(
        webhook_ref,
        channel_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/webhooks/{webhook_id}/avatar/tickets", status_code=201)
async def bot_create_webhook_avatar_ticket(
    guild_ref: EntityRef,
    webhook_id: int,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    _, installation, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    require_installation_scope(principal, installation, "attachments.write")
    return await create_webhook_avatar_ticket(
        webhook_ref,
        payload,
        response,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.put("/guilds/{guild_ref}/webhooks/{webhook_id}/avatar")
async def bot_commit_webhook_avatar(
    guild_ref: EntityRef,
    webhook_id: int,
    payload: AssetCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, installation, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await commit_webhook_avatar(
        webhook_ref,
        payload,
        response,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/webhooks/{webhook_id}/avatar")
async def bot_delete_webhook_avatar(
    guild_ref: EntityRef,
    webhook_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, _, webhook_ref, _ = await bot_guild_webhook(
        session,
        settings,
        principal,
        guild_ref,
        webhook_id,
        manage=True,
    )
    return await delete_webhook_avatar(
        webhook_ref,
        guild_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/emojis")
async def bot_list_emojis(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.read", "guilds.read"
    )
    rows = await session.scalars(
        select(Emoji)
        .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
        .order_by(Emoji.name, Emoji.id)
    )
    emojis = list(rows)
    restrictions = list(
        await session.scalars(
            select(EmojiRoleRestriction).where(
                EmojiRoleRestriction.guild_id == guild.id,
                EmojiRoleRestriction.guild_domain == guild.origin_domain,
            )
        )
    )
    role_refs: dict[tuple[int, str], list[str]] = {}
    for restriction in restrictions:
        role_refs.setdefault((restriction.emoji_id, restriction.emoji_domain), []).append(
            f"{restriction.role_id}@{restriction.role_domain}"
        )
    return [
        emoji_payload(
            emoji,
            sorted(role_refs.get((emoji.id, emoji.origin_domain), [])),
        )
        for emoji in emojis
    ]


@router.post("/guilds/{guild_ref}/emojis/tickets", status_code=201)
async def bot_create_emoji_ticket(
    guild_ref: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    require_image_type(payload.content_type)
    if payload.encryption_mode != "plaintext":
        raise HTTPException(status_code=409, detail={"code": "E2EE_NOT_ENABLED"})
    if payload.size > settings.media_max_emoji_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "EMOJI_TOO_LARGE", "max_bytes": settings.media_max_emoji_bytes},
        )
    permissions = await get_permissions(session, redis, guild, principal.user)
    if not permissions & Permission.CREATE_GUILD_EXPRESSIONS:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=principal.user.id,
        user_domain=principal.user.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="emoji",
        bot_installation=installation,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/guilds/{guild_ref}/emojis", status_code=201)
async def bot_create_emoji(
    guild_ref: EntityRef,
    payload: EmojiCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    _, installation = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await create_emoji(
        guild_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/emojis/{emoji_id}", status_code=204)
async def bot_delete_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    return await delete_emoji(
        guild_ref,
        emoji_id,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/stickers")
async def bot_list_stickers(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, _ = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.read", "guilds.read"
    )
    rows = await session.scalars(
        select(Sticker)
        .where(Sticker.guild_id == guild.id, Sticker.guild_domain == guild.origin_domain)
        .order_by(Sticker.name, Sticker.id)
    )
    return [sticker_payload(sticker) for sticker in rows]


@router.post("/guilds/{guild_ref}/stickers/tickets", status_code=201)
async def bot_create_sticker_ticket(
    guild_ref: EntityRef,
    payload: StickerTicketRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    require_installation_scope(principal, installation, "attachments.write")
    require_sticker_type(payload.content_type)
    if payload.encryption_mode != "plaintext":
        raise HTTPException(status_code=409, detail={"code": "E2EE_NOT_ENABLED"})
    if payload.size > settings.media_max_sticker_bytes:
        raise HTTPException(
            status_code=413,
            detail={"code": "STICKER_TOO_LARGE", "max_bytes": settings.media_max_sticker_bytes},
        )
    if payload.remove_background and not settings.media_sticker_background_removal_enabled:
        raise HTTPException(
            status_code=409,
            detail={"code": "STICKER_BACKGROUND_REMOVAL_UNAVAILABLE"},
        )
    permissions = await get_permissions(session, redis, guild, principal.user)
    if not permissions & Permission.CREATE_GUILD_EXPRESSIONS:
        raise HTTPException(status_code=403, detail={"code": "MISSING_PERMISSIONS"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=principal.user.id,
        user_domain=principal.user.origin_domain,
    )
    transform: dict[str, object] = {
        "sticker": True,
        "remove_background": payload.remove_background,
    }
    if payload.crop is not None:
        transform["crop"] = payload.crop.model_dump()
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        principal.user,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="sticker",
        media_transform=transform,
        bot_installation=installation,
    )
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/guilds/{guild_ref}/stickers", status_code=201)
async def bot_create_sticker(
    guild_ref: EntityRef,
    payload: StickerCommitRequest,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    _, installation = await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    await require_owned_attachments_for_installation(
        session,
        settings,
        principal,
        installation,
        [int(payload.attachment_id)],
    )
    return await create_sticker(
        guild_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/stickers/{sticker_id}", status_code=204)
async def bot_delete_sticker(
    guild_ref: EntityRef,
    sticker_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild_any_scope(
        session, settings, principal, guild_ref, "expressions.manage", "emojis.manage"
    )
    return await delete_sticker(
        guild_ref,
        sticker_id,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.patch("/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def bot_update_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceModerationUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "voice.moderate"
    )
    await require_bot_voice_member_channel_access(
        session,
        redis,
        settings,
        guild,
        user_ref,
        installation,
    )
    return await update_member_voice_moderation(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}/voice", status_code=204)
async def bot_disconnect_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "voice.moderate"
    )
    await require_bot_voice_member_channel_access(
        session,
        redis,
        settings,
        guild,
        user_ref,
        installation,
    )
    return await disconnect_member_voice(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.post("/guilds/{guild_ref}/members/{user_ref}/voice/move", status_code=204)
async def bot_move_member_voice(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: VoiceMoveRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild, installation = await installation_for_guild(
        session, settings, principal, guild_ref, "voice.moderate"
    )
    await require_bot_voice_member_channel_access(
        session,
        redis,
        settings,
        guild,
        user_ref,
        installation,
        target_channel_ref=payload.channel_id,
    )
    return await move_member_voice(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/channels/{channel_ref}/messages")
async def bot_list_messages(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    before: EntityRef | None = None,
    after: EntityRef | None = None,
    around: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> list[dict[str, object]]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.history",
        installation_id,
    )
    can_read_e2ee = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    messages = await list_messages(
        channel_ref,
        before,
        after,
        around,
        limit,
        user_auth(principal),
        session,
        redis,
        settings,
    )
    messages = await bot_messages_after_history_floor(session, can_read_e2ee, messages)
    can_read_content = bot_can_read_ambient_message_content(principal, installation)
    can_read_attachments = (
        "attachments.read" in principal.scopes and "attachments.read" in installation.granted_scopes
    )
    return [
        redact_bot_message_payload(
            message,
            can_read_content=can_read_content,
            can_read_attachments=can_read_attachments,
            principal=principal,
            direct_message=channel.guild_id is None,
            can_read_e2ee=can_read_e2ee is not None,
        )
        | bot_runtime_grant_payload(installation)
        for message in messages
    ]


@router.get("/channels/{channel_ref}/messages/{message_ref}/forwarded")
async def bot_resolve_forwarded_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.history",
        installation_id,
    )
    can_read_e2ee = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    rendered = await resolve_forwarded_message(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        settings,
    )
    if not await bot_messages_after_history_floor(session, can_read_e2ee, [rendered]):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    return redact_bot_message_payload(
        rendered,
        can_read_content=bot_can_read_ambient_message_content(principal, installation),
        can_read_attachments=(
            "attachments.read" in principal.scopes
            and "attachments.read" in installation.granted_scopes
        ),
        principal=principal,
        direct_message=channel.guild_id is None,
        can_read_e2ee=can_read_e2ee is not None,
    ) | bot_runtime_grant_payload(installation)


@router.post("/channels/{channel_ref}/messages/{message_ref}/forward-authorize")
async def bot_forward_source_authorize(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    payload: BotForwardSourceAuthorizationCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    """Issue a source-authority proof after exact bot grant/floor checks."""

    source, channel, _installation, _participation = await require_bot_forward_source_access(
        session,
        settings,
        principal,
        message_ref,
        e2ee_device_id=e2ee_device_id,
        installation_id=installation_id,
    )
    if (source.channel_id, source.channel_domain) != channel_ref.resolve(settings.domain) or (
        channel.id,
        channel.origin_domain,
    ) != channel_ref.resolve(settings.domain):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    if (
        await session.get(Poll, (source.id, source.origin_domain)) is not None
        or source.message_type not in FORWARDABLE_MESSAGE_TYPES
    ):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_NOT_FORWARDABLE"})
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == source.id,
                Attachment.message_domain == source.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id, Attachment.origin_domain)
        )
    )
    source_nsfw = await effective_channel_nsfw(session, channel)
    if source_nsfw is None:
        raise HTTPException(status_code=409, detail={"code": "FORWARD_CONTEXT_UNSUPPORTED"})
    destination_id, destination_domain = payload.destination_channel_id.resolve(settings.domain)
    try:
        content = build_forward_source_authorization_content(
            source,
            attachments,
            requester_ref=f"{principal.user.id}@{principal.user.origin_domain}",
            requester_type="bot",
            source_channel_ref=f"{channel.id}@{channel.origin_domain}",
            destination_channel_ref=f"{destination_id}@{destination_domain}",
            destination_encryption_mode=payload.destination_encryption_mode,
            source_nsfw=source_nsfw,
            nonce=payload.client_nonce,
            application_ref=(f"{principal.application.id}@{principal.application.origin_domain}"),
            e2ee_device_id=e2ee_device_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "FORWARD_SOURCE_PROOF_UNAVAILABLE"},
        ) from exc
    authorization = await build_envelope(
        session,
        settings,
        FORWARD_SOURCE_AUTHORIZATION_EVENT,
        principal.user,
        content,
        context={"source_channel_ref": f"{channel.id}@{channel.origin_domain}"},
        authority_attested_actor=principal.user.origin_domain != settings.domain,
    )
    return {"authorization": authorization}


@router.post("/channels/{channel_ref}/messages")
async def bot_create_message(
    channel_ref: EntityRef,
    payload: MessageCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.send",
        installation_id,
    )
    if payload.forwarded_message_id is not None and payload.forward_source_proof is None:
        await require_bot_forward_source_access(
            session,
            settings,
            principal,
            payload.forwarded_message_id,
            e2ee_device_id=e2ee_device_id or bot_e2ee_sender_device_id(payload),
        )
    if payload.attachment_ids:
        await require_owned_attachments_for_installation(
            session,
            settings,
            principal,
            installation,
            [int(item) for item in payload.attachment_ids],
        )
    bot_installation_id, bot_dm_capability_id = bot_message_grant_ids(installation)
    rendered = await create_message(
        channel_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        MessageAdmissionOptions(
            application_id=principal.application.id,
            application_domain=principal.application.origin_domain,
            bot_installation_id=bot_installation_id,
            bot_dm_capability_id=bot_dm_capability_id,
            bot_worker_id=principal.worker.id,
            forward_source_e2ee_device_id=e2ee_device_id,
        ),
    )
    return await render_bot_message_response(
        session,
        principal,
        channel,
        installation,
        rendered,
        e2ee_device_id=bot_e2ee_sender_device_id(payload),
    )


@router.patch("/channels/{channel_ref}/messages/{message_ref}")
async def bot_edit_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    payload: MessageEdit,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.edit.own",
        installation_id,
    )
    if payload.attachment_ids is not None:
        await require_owned_attachments_for_installation(
            session,
            settings,
            principal,
            installation,
            [int(item) for item in payload.attachment_ids],
        )
    bot_installation_id, bot_dm_capability_id = bot_message_grant_ids(installation)
    rendered = await edit_message(
        channel_ref,
        message_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        settings,
        snowflake,
        MessageMutationOptions(
            application_id=principal.application.id,
            application_domain=principal.application.origin_domain,
            bot_installation_id=bot_installation_id,
            bot_dm_capability_id=bot_dm_capability_id,
            bot_worker_id=principal.worker.id,
        ),
    )
    return await render_bot_message_response(
        session,
        principal,
        channel,
        installation,
        rendered,
        e2ee_device_id=bot_e2ee_sender_device_id(payload),
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}",
    status_code=204,
)
async def bot_delete_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    message_id, message_domain = message_ref.resolve(settings.domain)
    message = await session.get(Message, (message_id, message_domain))
    if message is None or (message.channel_id, message.channel_domain) != channel_ref.resolve(
        settings.domain
    ):
        raise HTTPException(status_code=404, detail={"code": "MESSAGE_NOT_FOUND"})
    scope = (
        "messages.delete.own"
        if (message.author_id, message.author_domain)
        == (principal.user.id, principal.user.origin_domain)
        else "moderation.messages"
    )
    await installation_for_channel(
        session, settings, principal, channel_ref, scope, installation_id
    )
    return await delete_message(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.post("/channels/{channel_ref}/messages/bulk-delete", status_code=204)
async def bot_bulk_delete_messages(
    channel_ref: EntityRef,
    payload: MessageBulkDelete,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "moderation.messages",
        installation_id,
    )
    return await bulk_delete_messages(
        channel_ref, payload, user_auth(principal), session, redis, settings
    )


@router.post(
    "/channels/{channel_ref}/messages/{message_ref}/reactions",
    status_code=204,
)
async def bot_add_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    payload: ReactionCreate,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "reactions.write", installation_id
    )
    return await add_reaction(
        channel_ref,
        message_ref,
        payload,
        response,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/@me",
    status_code=204,
)
async def bot_add_own_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "reactions.write", installation_id
    )
    return await add_reaction(
        channel_ref,
        message_ref,
        ReactionCreate(emoji=reaction_path_emoji(emoji)),
        response,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/@me",
    status_code=204,
)
async def bot_remove_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    response: Response,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "reactions.write", installation_id
    )
    return await remove_own_reaction(
        channel_ref,
        message_ref,
        response,
        emoji,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}",
)
async def bot_reaction_users(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> dict[str, object]:
    await installation_for_channel(
        session, settings, principal, channel_ref, "reactions.read", installation_id
    )
    return await list_reaction_users(
        channel_ref,
        message_ref,
        emoji,
        after,
        limit,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/{user_ref}",
    status_code=204,
)
async def bot_remove_user_reaction(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    user_ref: EntityRef,
    emoji: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "messages.manage", installation_id
    )
    return await remove_user_reaction(
        channel_ref,
        message_ref,
        user_ref,
        emoji,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions",
    status_code=204,
)
async def bot_clear_all_reactions(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "moderation.messages",
        installation_id,
    )
    return await clear_all_reactions(
        channel_ref, message_ref, user_auth(principal), session, redis, settings
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reaction-groups/{emoji}",
    status_code=204,
    include_in_schema=False,
)
@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}",
    status_code=204,
)
async def bot_clear_reaction_group(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    emoji: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "moderation.messages",
        installation_id,
    )
    return await clear_reaction_group(
        channel_ref,
        message_ref,
        emoji,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put(
    "/channels/{channel_ref}/messages/{message_ref}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def bot_add_poll_vote(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    answer_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "polls.write", installation_id
    )
    raise HTTPException(
        status_code=403,
        detail={
            "code": "BOT_POLL_VOTE_UNSUPPORTED",
            "message": "Applications cannot vote in polls.",
        },
    )


@router.delete(
    "/channels/{channel_ref}/messages/{message_ref}/polls/answers/{answer_id}/@me",
    status_code=204,
)
async def bot_remove_poll_vote(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    answer_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "polls.write", installation_id
    )
    raise HTTPException(
        status_code=403,
        detail={
            "code": "BOT_POLL_VOTE_UNSUPPORTED",
            "message": "Applications cannot vote in polls.",
        },
    )


@router.get(
    "/channels/{channel_ref}/messages/{message_ref}/polls/answers/{answer_id}",
)
async def bot_list_poll_voters(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    answer_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    after: EntityRef | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> dict[str, object]:
    await installation_for_channel(
        session, settings, principal, channel_ref, "polls.read", installation_id
    )
    return await list_poll_voters(
        channel_ref,
        message_ref,
        answer_id,
        after,
        limit,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.post("/channels/{channel_ref}/messages/{message_ref}/polls/expire")
async def bot_finalize_poll(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session, settings, principal, channel_ref, "polls.write", installation_id
    )
    rendered = await finalize_poll(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )
    return await render_bot_message_response(
        session,
        principal,
        channel,
        installation,
        rendered,
        e2ee_device_id=e2ee_device_id,
    )


@router.post("/channels/{channel_ref}/followers")
async def bot_follow_announcement_channel(
    channel_ref: EntityRef,
    payload: ChannelFollowCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require_scope("webhooks.manage")
    await installation_for_channel(session, settings, principal, channel_ref, "channels.read")
    return await follow_announcement_channel(
        channel_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )


@router.get("/channels/{channel_ref}/followers")
async def bot_list_announcement_follows(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor_intent_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intent")] = None,
    actor_intents_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intents")] = None,
) -> list[dict[str, object]]:
    await installation_for_channel(session, settings, principal, channel_ref, "channels.read")
    actor_intent, actor_intents = parsed_bot_actor_intent_headers(
        actor_intent_header,
        actor_intents_header,
    )
    return await list_announcement_follows(
        channel_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        actor_intent=actor_intent,
        actor_intents=actor_intents,
    )


@router.delete("/channels/{channel_ref}/followers/{follow_ref}", status_code=204)
async def bot_delete_announcement_follow(
    channel_ref: EntityRef,
    follow_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    actor_intent_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intent")] = None,
    actor_intents_header: Annotated[str | None, Header(alias="X-Kaede-Actor-Intents")] = None,
) -> Response:
    source_ref = channel_ref.resolve(settings.domain)
    follow = await source_announcement_follow(
        session,
        source_ref,
        follow_ref,
        local_domain=settings.domain,
    )
    if follow is None:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_FOLLOW_NOT_FOUND"})
    principal.require_scope("webhooks.manage")
    await installation_for_channel(session, settings, principal, channel_ref, "channels.read")
    actor_intent, actor_intents = parsed_bot_actor_intent_headers(
        actor_intent_header,
        actor_intents_header,
    )
    return await delete_announcement_follow(
        channel_ref,
        follow_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        actor_intent=actor_intent,
        actor_intents=actor_intents,
    )


@router.post("/channels/{channel_ref}/messages/{message_ref}/crosspost")
async def bot_crosspost_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.send",
        installation_id,
    )
    rendered = await crosspost_message(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
    )
    return await render_bot_message_response(
        session,
        principal,
        channel,
        installation,
        rendered,
        e2ee_device_id=e2ee_device_id,
    )


@router.get("/channels/{channel_ref}/pins")
async def bot_list_pins(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> list[dict[str, object]]:
    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.history",
        installation_id,
    )
    can_read_e2ee = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    pins = await list_pins(channel_ref, user_auth(principal), session, redis, settings)
    pins = await bot_messages_after_history_floor(session, can_read_e2ee, pins)
    can_read_content = bot_can_read_ambient_message_content(principal, installation)
    can_read_attachments = (
        "attachments.read" in principal.scopes and "attachments.read" in installation.granted_scopes
    )
    return [
        redact_bot_message_payload(
            message,
            can_read_content=can_read_content,
            can_read_attachments=can_read_attachments,
            principal=principal,
            direct_message=channel.guild_id is None,
            can_read_e2ee=can_read_e2ee is not None,
        )
        | bot_runtime_grant_payload(installation)
        for message in pins
    ]


@router.get("/channels/{channel_ref}/messages/pins")
async def bot_list_channel_pins(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=50),
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
) -> dict[str, object]:
    """Expose Discord's paginated message-pin object to bot runtimes."""

    channel, installation = await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.history",
        installation_id,
    )
    can_read_e2ee = await require_bot_channel_e2ee_access(
        session,
        channel,
        installation,
        e2ee_device_id,
        worker_id=principal.worker.id,
    )
    visible_items: list[dict[str, object]] = []
    scan_before = before
    for _ in range(5):
        page = await list_channel_pins(
            channel_ref,
            scan_before,
            50,
            user_auth(principal),
            session,
            redis,
            settings,
        )
        raw_items = cast(list[dict[str, object]], page["items"])
        messages = [cast(dict[str, object], item["message"]) for item in raw_items]
        visible = await bot_messages_after_history_floor(session, can_read_e2ee, messages)
        visible_refs = {
            (str(message.get("id")), str(message.get("origin_domain"))) for message in visible
        }
        visible_items.extend(
            item
            for item in raw_items
            if (
                str(cast(dict[str, object], item["message"]).get("id")),
                str(cast(dict[str, object], item["message"]).get("origin_domain")),
            )
            in visible_refs
        )
        if len(visible_items) > limit or not page["has_more"]:
            break
        if not raw_items:
            raise RuntimeError("pin pagination did not advance")
        next_before = datetime.fromisoformat(str(raw_items[-1]["pinned_at"]))
        if next_before.tzinfo is None or (scan_before is not None and next_before >= scan_before):
            raise RuntimeError("pin pagination did not advance")
        scan_before = next_before
    can_read_content = bot_can_read_ambient_message_content(principal, installation)
    can_read_attachments = (
        "attachments.read" in principal.scopes and "attachments.read" in installation.granted_scopes
    )
    items: list[dict[str, object]] = []
    for item in visible_items[:limit]:
        message = cast(dict[str, object], item["message"])
        items.append(
            {
                "pinned_at": item["pinned_at"],
                "message": redact_bot_message_payload(
                    message,
                    can_read_content=can_read_content,
                    can_read_attachments=can_read_attachments,
                    principal=principal,
                    direct_message=channel.guild_id is None,
                    can_read_e2ee=can_read_e2ee is not None,
                )
                | bot_runtime_grant_payload(installation),
            }
        )
    return {"items": items, "has_more": len(visible_items) > limit}


@router.put("/channels/{channel_ref}/pins/{message_ref}", status_code=204)
@router.put("/channels/{channel_ref}/messages/pins/{message_ref}", status_code=204)
async def bot_pin_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "messages.manage", installation_id
    )
    return await pin_message(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        snowflake,
        reason,
    )


@router.delete("/channels/{channel_ref}/pins/{message_ref}", status_code=204)
@router.delete("/channels/{channel_ref}/messages/pins/{message_ref}", status_code=204)
async def bot_unpin_message(
    channel_ref: EntityRef,
    message_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_channel(
        session, settings, principal, channel_ref, "messages.manage", installation_id
    )
    return await unpin_message(
        channel_ref,
        message_ref,
        user_auth(principal),
        session,
        redis,
        settings,
        snowflake,
        reason,
    )


@router.post("/channels/{channel_ref}/typing", status_code=204)
async def bot_typing(
    channel_ref: EntityRef,
    response: Response,
    request: Request,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    installation_id: int | None = Header(default=None, alias="X-Kaede-Bot-Installation"),
) -> Response:
    await installation_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "messages.send",
        installation_id,
    )
    return await typing(
        channel_ref,
        response,
        request,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get("/channels/{channel_ref}/voice/occupancy")
async def bot_voice_occupancy(
    channel_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await installation_for_channel(session, settings, principal, channel_ref, "voice.states.read")
    return await channel_voice_occupancy(
        channel_ref, user_auth(principal), session, redis, settings
    )


@router.patch("/guilds/{guild_ref}/members/{user_ref}")
async def bot_update_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: MemberUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await update_member(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/members/{user_ref}", status_code=204)
async def bot_kick_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.members")
    return await kick_member(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/bans")
async def bot_list_bans(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
) -> list[dict[str, object]]:
    await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "moderation.bans",
        "moderation.members",
    )
    return await list_bans(
        guild_ref,
        limit,
        after,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/bans/{user_ref}", status_code=204)
async def bot_ban_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    payload: BanCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "moderation.bans",
        "moderation.members",
    )
    return await ban_member(
        guild_ref,
        user_ref,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/bans/{user_ref}", status_code=204)
async def bot_unban_member(
    guild_ref: EntityRef,
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "moderation.bans",
        "moderation.members",
    )
    return await remove_ban(
        guild_ref,
        user_ref,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/instance-bans")
async def bot_list_instance_bans(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=50, ge=1, le=1000),
    after: str | None = Query(default=None, max_length=253),
) -> list[dict[str, object]]:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.bans")
    return await list_instance_bans(
        guild_ref,
        limit,
        after,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.put("/guilds/{guild_ref}/instance-bans/{instance_domain}", status_code=204)
async def bot_ban_instance(
    guild_ref: EntityRef,
    instance_domain: str,
    payload: InstanceBanCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.bans")
    return await ban_instance(
        guild_ref,
        instance_domain,
        payload,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.delete("/guilds/{guild_ref}/instance-bans/{instance_domain}", status_code=204)
async def bot_unban_instance(
    guild_ref: EntityRef,
    instance_domain: str,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    await installation_for_guild(session, settings, principal, guild_ref, "moderation.bans")
    return await remove_instance_ban(
        guild_ref,
        instance_domain,
        user_auth(principal),
        session,
        redis,
        snowflake,
        settings,
        reason,
    )


@router.get("/guilds/{guild_ref}/audit-logs", response_model_exclude_unset=True)
async def bot_list_audit_logs(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = Query(default=50, ge=1, le=100),
    before: Snowflake | None = None,
    after: Snowflake | None = None,
    user_id: EntityRef | None = None,
    action_type: int | None = Query(default=None, ge=0, le=2_147_483_647),
    target_type: str | None = Query(
        default=None, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_.-]*$"
    ),
) -> list[AuditLogEntryPayload]:
    # The installation grant is only the outer capability. Delegating to the
    # human route performs the live VIEW_AUDIT_LOG check for the bot member.
    await installation_for_guild(session, settings, principal, guild_ref, "audit_logs.read")
    return await list_audit_logs(
        guild_ref,
        limit,
        before,
        after,
        user_id,
        action_type,
        target_type,
        user_auth(principal),
        session,
        redis,
        settings,
    )


@router.get("/users/{user_ref}")
async def bot_get_user(
    user_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    principal.require_scope("members.read")
    user_id, user_domain = user_ref.resolve(settings.domain)
    user = await session.get(User, (user_id, user_domain))
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    shared_guild = await session.scalar(
        select(GuildMember.guild_id)
        .join(
            BotInstallation,
            (BotInstallation.guild_id == GuildMember.guild_id)
            & (BotInstallation.guild_domain == GuildMember.guild_domain),
        )
        .where(
            GuildMember.user_id == user.id,
            GuildMember.user_domain == user.origin_domain,
            BotInstallation.application_id == principal.application.id,
            BotInstallation.application_domain == principal.application.origin_domain,
            BotInstallation.bot_user_id == principal.user.id,
            BotInstallation.bot_user_domain == principal.user.origin_domain,
            usable_guild_installation(),
            BotInstallation.granted_scopes.contains(["members.read"]),
        )
        .limit(1)
    )
    if shared_guild is None and (user.id, user.origin_domain) != (
        principal.user.id,
        principal.user.origin_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    payload = user_payload(user)
    payload["handle"] = f"{user.username}@{user.origin_domain}"
    return payload
