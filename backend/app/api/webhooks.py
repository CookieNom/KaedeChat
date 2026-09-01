from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Annotated, Any, cast

from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.channels import (
    MessageAdmissionOptions,
    MessageMutationOptions,
    announcement_actor_application,
    announcement_application_for_actor,
    announcement_follow_receipt_content,
    commit_local_message_deletion,
    create_message,
    delete_announcement_follow_from_target,
    edit_message,
    encrypted_rich_routing,
    lock_announcement_mutation,
    lock_message_delete_access,
    lock_message_delete_target,
    publish_follower_webhook_update,
    require_announcement_actor_scope,
    require_editable_message,
    stored_announcement_follow_projection,
    validate_merged_message_edit,
)
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import guild_channel, local_guild
from app.auth.security import decrypt_secret, encrypt_secret
from app.bots.installations import usable_guild_installation
from app.chat.allowed_mentions import (
    ResolvedMentions,
    resolve_allowed_mentions_projection,
)
from app.chat.announcement_identity import federated_follow_key
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.channel_access import ChannelAccess
from app.chat.e2ee import MessageEncryptionPolicyError, validate_message_encryption_policy
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    build_guild_authority_envelope,
    guild_authority_owner,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.mention_policy import AllowedMentions
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
    PUBLIC_MESSAGE_EDIT_FLAGS,
)
from app.chat.payloads import render_message_payload, user_payload
from app.chat.permissions import get_permissions, require_permissions
from app.chat.rich_content import (
    MESSAGE_LAYOUT_COMPONENT_ADAPTER,
    Embed,
    EmbedAuthor,
    EmbedField,
    EmbedFooter,
    MessageLayoutComponent,
    PollCreate,
    uses_components_v2,
    validate_attachment_url_references,
    validate_embed_collection,
    validate_message_components,
    walk_component_tree,
)
from app.chat.schemas import MessageCreate, MessageEdit, RequestModel, meaningful_optional_content
from app.chat.webhook_limits import require_webhook_capacity
from app.core.channel_types import GUILD_WEBHOOK_CHANNEL_TYPES
from app.core.json_limits import JsonTreeLimits, validate_json_tree
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, Snowflake, WireSnowflake
from app.db.bot_models import BotApplication, BotInstallation
from app.db.models import (
    Attachment,
    Channel,
    ChannelFollow,
    FederatedChannelFollow,
    Guild,
    Message,
    MessageProjection,
    User,
    Webhook,
)
from app.federation.events import queue_event
from app.federation.guild_management import (
    GuildManagementOperation,
    GuildManagementResult,
    proxy_remote_guild_management,
)
from app.federation.guilds import remote_destinations_with_channel_access
from app.media.schemas import AssetCommitRequest, UploadTicketRequest
from app.media.service import (
    attachment_payload,
    bind_asset,
    create_upload_ticket,
    discard_attachment,
    finalize_attachment,
    is_federated_human_authority_upload,
    require_image_type,
    ticket_payload,
)
from app.media.tombstones import lock_media_tombstone_ref
from app.tasks import (
    federation_deliver,
    media_local_purge,
    media_process,
)

router = APIRouter(tags=["webhooks"])
WEBHOOK_RATE_SCRIPT = """
local attempts = redis.call('INCR', KEYS[1])
if attempts == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return attempts
"""


async def _proxy_webhook_guild_operation(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    auth: AuthenticatedUser,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> GuildManagementResult | None:
    return await proxy_remote_guild_management(
        session,
        settings,
        guild_ref,
        auth.user,
        operation,
        payload,
    )


async def _webhook_management_target(
    session: AsyncSession,
    settings: Settings,
    auth: AuthenticatedUser,
    webhook_ref: EntityRef,
    guild_ref: EntityRef | None,
    operation: GuildManagementOperation,
    payload: dict[str, Any],
) -> tuple[int, GuildManagementResult | None]:
    webhook_id, authority = webhook_ref.resolve(settings.domain)
    if authority == settings.domain:
        return webhook_id, None
    if guild_ref is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_GUILD_REF_REQUIRED",
                "message": "Remote webhook management requires its qualified guild reference.",
            },
        )
    _, guild_domain = guild_ref.resolve(settings.domain)
    if guild_domain != authority:
        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_GUILD_REF_INVALID"})
    result = await _proxy_webhook_guild_operation(
        session,
        settings,
        guild_ref,
        auth,
        operation,
        {"resource_id": webhook_id, **payload},
    )
    if result is None:
        raise HTTPException(status_code=409, detail={"code": "WEBHOOK_AUTHORITY_INVALID"})
    return webhook_id, result


def valid_webhook_name(value: str) -> str:
    cleaned = value.strip()
    if not 1 <= len(cleaned) <= 80:
        raise ValueError("name must contain between 1 and 80 characters")
    folded = cleaned.casefold()
    if "clyde" in folded or "discord" in folded:
        raise ValueError("name may not contain Discord or Clyde")
    return cleaned


def has_interactive_components(components: list[MessageLayoutComponent]) -> bool:
    return any(
        getattr(component, "custom_id", None) is not None
        for layout in components
        for component in walk_component_tree(layout)
    )


class WebhookCreate(RequestModel):
    name: str

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return valid_webhook_name(value)


class WebhookPatch(RequestModel):
    name: str | None = None
    avatar_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    channel_id: EntityRef | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return valid_webhook_name(value)

    @model_validator(mode="after")
    def has_change(self) -> WebhookPatch:
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "channel_id" in self.model_fields_set and self.channel_id is None:
            raise ValueError("channel_id cannot be null")
        return self


class WebhookAllowedMentions(AllowedMentions):
    """Compatibility name for the shared application-message policy."""


WEBHOOK_MESSAGE_FLAG_MASK = (
    MESSAGE_FLAG_SUPPRESS_EMBEDS
    | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS
    | MESSAGE_FLAG_IS_COMPONENTS_V2
)


class WebhookExecute(RequestModel):
    model_config = ConfigDict(extra="forbid")

    content: str | None = Field(default=None, min_length=1, max_length=2000)
    embeds: list[Embed] = Field(default_factory=list, max_length=10)
    components: list[MessageLayoutComponent] = Field(default_factory=list, max_length=40)
    poll: PollCreate | None = None
    username: str | None = None
    avatar_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    attachment_ids: list[WireSnowflake] = Field(default_factory=list, max_length=10)
    sticker_ids: list[EntityRef] = Field(default_factory=list, max_length=3)
    thread_name: str | None = Field(default=None, min_length=1, max_length=100)
    applied_tags: list[WireSnowflake] = Field(default_factory=list, max_length=5)
    flags: int = Field(default=0, ge=0)
    tts: bool = False
    allowed_mentions: WebhookAllowedMentions | None = None
    e2ee: dict[str, Any] | None = None

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str | None) -> str | None:
        return meaningful_optional_content(value)

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str | None) -> str | None:
        return valid_webhook_name(value) if value is not None else None

    @field_validator("thread_name")
    @classmethod
    def clean_thread_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("thread_name must not be blank")
        return cleaned

    @field_validator("flags")
    @classmethod
    def supported_flags(cls, value: int) -> int:
        if value & ~WEBHOOK_MESSAGE_FLAG_MASK:
            raise ValueError("webhook flags contain unsupported bits")
        return value

    @model_validator(mode="after")
    def complete_message(self) -> WebhookExecute:
        validate_embed_collection(self.embeds)
        validate_message_components(self.components)
        if (
            self.content is None
            and not self.embeds
            and not self.components
            and self.poll is None
            and not self.attachment_ids
            and not self.sticker_ids
            and self.e2ee is None
        ):
            raise ValueError("a webhook message requires content or rich content")
        if len(self.attachment_ids) != len(set(self.attachment_ids)):
            raise ValueError("attachment IDs must be unique")
        if len(self.sticker_ids) != len(set(self.sticker_ids)):
            raise ValueError("sticker IDs must be unique")
        if len(self.applied_tags) != len(set(self.applied_tags)):
            raise ValueError("applied tag IDs must be unique")
        return self


class WebhookMessageEdit(RequestModel):
    content: str | None = Field(default=None, min_length=1, max_length=2000)
    embeds: list[Embed] | None = Field(default=None, max_length=10)
    components: list[MessageLayoutComponent] | None = Field(default=None, max_length=40)
    attachment_ids: list[WireSnowflake] | None = Field(default=None, max_length=10)
    flags: int | None = Field(default=None, ge=0)
    allowed_mentions: WebhookAllowedMentions | None = None
    e2ee: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid_edit(self) -> WebhookMessageEdit:
        if self.embeds is not None:
            validate_embed_collection(self.embeds)
        if self.components is not None:
            validate_message_components(self.components)
        if self.flags is not None and self.flags & ~PUBLIC_MESSAGE_EDIT_FLAGS:
            raise ValueError("webhook edit flags contain unsupported bits")
        if self.attachment_ids is not None and len(self.attachment_ids) != len(
            set(self.attachment_ids)
        ):
            raise ValueError("attachment IDs must be unique")
        return self


def webhook_execution_components(
    payload: WebhookExecute,
    *,
    application_owned: bool,
    with_components: bool,
) -> list[MessageLayoutComponent]:
    """Apply Discord's component opt-in only to ordinary incoming webhooks."""

    return payload.components if application_owned or with_components else []


def validate_webhook_components_v2_body(
    *,
    flags: int,
    content: str | None,
    embeds: Sequence[object],
    components: Sequence[object],
    attachment_ids: Sequence[object],
    poll: object | None,
    sticker_ids: Sequence[object],
) -> bool:
    """Validate Discord's Components V2 webhook body and flag contract.

    Discord's execute-webhook contract is narrower than ordinary message
    creation: a Components V2 webhook body may contain only components, so a
    file upload is rejected even when a component refers to it.
    """

    components_v2 = uses_components_v2(list(components))
    if components_v2 and not flags & MESSAGE_FLAG_IS_COMPONENTS_V2:
        raise HTTPException(
            status_code=400,
            detail={"code": "COMPONENTS_V2_FLAG_REQUIRED"},
        )
    if flags & MESSAGE_FLAG_IS_COMPONENTS_V2 and (
        not components_v2
        or content is not None
        or embeds
        or attachment_ids
        or poll is not None
        or sticker_ids
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "COMPONENTS_V2_BODY_INVALID"},
        )
    return components_v2


def validate_webhook_thread_target(
    *,
    channel_type: int,
    has_thread_id: bool,
    thread_name: str | None,
    applied_tags: Sequence[object],
) -> None:
    """Validate fields whose meaning depends on the webhook's parent channel."""

    if has_thread_id and thread_name is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_THREAD_TARGET_AMBIGUOUS",
                "message": "Choose either an existing thread or a new thread name, not both.",
            },
        )
    if channel_type == 15:
        if not has_thread_id and thread_name is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "WEBHOOK_THREAD_NAME_REQUIRED",
                    "message": "A new thread name is required when posting to a forum webhook.",
                },
            )
    elif thread_name is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_THREAD_NAME_UNEXPECTED",
                "message": "A thread name can only be supplied for a forum webhook.",
            },
        )
    if applied_tags and (channel_type != 15 or has_thread_id):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_APPLIED_TAGS_UNEXPECTED",
                "message": "Applied tags are only valid when creating a forum post.",
            },
        )


class SlackAttachmentField(RequestModel):
    title: str = Field(min_length=1, max_length=256)
    value: str = Field(min_length=1, max_length=1024)
    short: bool = False


class SlackAttachment(RequestModel):
    fallback: str | None = Field(default=None, min_length=1, max_length=4096)
    color: str | None = Field(default=None, min_length=1, max_length=16)
    pretext: str | None = Field(default=None, min_length=1, max_length=4096)
    author_name: str | None = Field(default=None, min_length=1, max_length=256)
    author_link: str | None = Field(default=None, max_length=2048)
    author_icon: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, min_length=1, max_length=256)
    title_link: str | None = Field(default=None, max_length=2048)
    text: str | None = Field(default=None, min_length=1, max_length=4096)
    fields: list[SlackAttachmentField] = Field(default_factory=list, max_length=25)
    footer: str | None = Field(default=None, min_length=1, max_length=2048)
    footer_icon: str | None = Field(default=None, max_length=2048)
    ts: int | str | None = None

    @field_validator("ts", mode="before")
    @classmethod
    def valid_timestamp_wire_type(cls, value: object) -> object:
        if value is not None and type(value) not in {int, str}:
            raise ValueError("Slack attachment timestamps must be integers or decimal strings")
        return value


class SlackWebhookExecute(RequestModel):
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    username: str | None = None
    icon_url: str | None = Field(default=None, max_length=2048, pattern=r"^https://")
    attachments: list[SlackAttachment] = Field(default_factory=list, max_length=10)
    # Discord documents these Slack properties as unsupported. Accepting and
    # ignoring them preserves Slack sender compatibility without inventing
    # unsafe routing or formatting behavior.
    channel: str | None = None
    icon_emoji: str | None = None
    mrkdwn: bool | None = None
    mrkdwn_in: list[str] | None = None

    @field_validator("username")
    @classmethod
    def valid_username(cls, value: str | None) -> str | None:
        return valid_webhook_name(value) if value is not None else None

    @model_validator(mode="after")
    def has_body(self) -> SlackWebhookExecute:
        if self.text is None and not self.attachments:
            raise ValueError("a Slack webhook requires text or attachments")
        return self


GITHUB_WEBHOOK_EVENTS = frozenset(
    {
        "check_run",
        "check_suite",
        "commit_comment",
        "create",
        "delete",
        "discussion",
        "discussion_comment",
        "fork",
        "issue_comment",
        "issues",
        "member",
        "public",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "push",
        "release",
        "watch",
    }
)


async def resolved_webhook_mentions(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    channel: Channel,
    creator: User,
    allowed: WebhookAllowedMentions | None,
    content: str | None,
    components: list[MessageLayoutComponent],
) -> ResolvedMentions:
    return await resolve_allowed_mentions_projection(
        session,
        redis,
        settings,
        ChannelAccess(channel=channel, guild=guild, participants=[]),
        creator,
        allowed,
        content,
        components,
    )


def new_webhook_token() -> str:
    return f"kwh_{secrets.token_urlsafe(32)}"


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class WebhookTokenRecoveryError(ValueError):
    """Stored webhook token cannot be authenticated in its resource context."""


def webhook_token_context(webhook: Webhook) -> bytes:
    return f"webhook-token:{webhook.id}@{webhook.guild_domain}".encode()


def store_webhook_token(webhook: Webhook, token: str, settings: Settings) -> None:
    """Persist execution authentication and manager recovery projections."""

    webhook.token_hash = token_digest(token)
    webhook.token_ciphertext = encrypt_secret(
        token,
        settings.secret_key_bytes,
        context=webhook_token_context(webhook),
    )


def recover_webhook_token(webhook: Webhook, settings: Settings) -> str | None:
    """Recover a manager-visible token and re-bind it to its execution hash."""

    ciphertext = getattr(webhook, "token_ciphertext", None)
    if ciphertext is None:
        return None
    try:
        token = decrypt_secret(
            ciphertext,
            settings.secret_key_bytes,
            context=webhook_token_context(webhook),
        )
    except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
        raise WebhookTokenRecoveryError("webhook token ciphertext is invalid") from exc
    if not hmac.compare_digest(token_digest(token), webhook.token_hash):
        raise WebhookTokenRecoveryError("webhook token hash does not match its ciphertext")
    return token


def webhook_attachment_binding_prefix(webhook_id: int) -> str:
    return f"webhook-stage:{webhook_id}:"


def webhook_message_admission_options(
    webhook: Webhook,
    creator: User,
    settings: Settings,
    *,
    device_id: str | None,
    name: str | None = None,
    avatar_hash: str | None = None,
    avatar_url: str | None = None,
    mentions: ResolvedMentions | None = None,
    tts: bool = False,
    flags: int = 0,
) -> MessageAdmissionOptions:
    """Build the one authority-bound admission capability for webhook writes."""

    application_owned = (
        webhook.type == 3
        and webhook.application_id is not None
        and webhook.application_domain is not None
    )
    return MessageAdmissionOptions(
        webhook_id=webhook.id,
        webhook_name=name or webhook.name,
        webhook_avatar_hash=avatar_hash,
        webhook_avatar_url=avatar_url,
        webhook_channel_id=webhook.channel_id,
        webhook_channel_domain=webhook.channel_domain,
        application_id=webhook.application_id if application_owned else None,
        application_domain=webhook.application_domain if application_owned else None,
        allow_render_only_components=not application_owned,
        authoritative_mention_refs=(mentions.recipients if mentions is not None else None),
        authoritative_mention_role_refs=(mentions.roles if mentions is not None else None),
        authoritative_mention_role_recipient_refs=(
            mentions.role_recipients if mentions is not None else None
        ),
        authoritative_mention_everyone=(mentions.everyone if mentions is not None else None),
        required_attachment_binding_prefix=webhook_attachment_binding_prefix(webhook.id),
        required_attachment_purpose="webhook_attachment",
        federated_guild_upload=is_federated_human_authority_upload(creator, settings),
        skip_client_rate_limit=True,
        webhook_e2ee_device_id=device_id,
        tts=tts,
        message_flags=flags,
    )


def webhook_payload(
    webhook: Webhook,
    *,
    token: str | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": str(webhook.id),
        "origin_domain": webhook.guild_domain,
        "ref": f"{webhook.id}@{webhook.guild_domain}",
        "type": webhook.type,
        "application_id": (
            str(webhook.application_id) if webhook.application_id is not None else None
        ),
        "application_domain": webhook.application_domain,
        "guild_id": str(webhook.guild_id),
        "guild_domain": webhook.guild_domain,
        "channel_id": str(webhook.channel_id),
        "channel_domain": webhook.channel_domain,
        "name": webhook.name,
        "avatar_hash": webhook.avatar_hash,
        "revoked": webhook.revoked_at is not None,
    }
    if token is not None:
        if settings is None:
            raise RuntimeError("token-bearing webhook payloads require instance settings")
        scheme = "https" if settings.environment == "production" else "http"
        result["token"] = token
        result["execution_url"] = (
            f"{scheme}://{webhook.guild_domain}/api/v1/webhooks/{webhook.id}/{token}"
        )
    return result


def managed_webhook_payload(
    webhook: Webhook,
    settings: Settings,
    *,
    token: str | None = None,
    include_token: bool = False,
    recover_token: bool = False,
) -> dict[str, object]:
    """Render a secret only after the caller proves the matching capability.

    Authentication kind is intentionally not consulted here. Human managers
    and applications with ``webhooks.manage`` have the same Discord-style
    management capability, while read-only bot projections remain secret-free.
    """

    if not include_token and not recover_token:
        return webhook_payload(webhook)
    try:
        recovered = (
            token
            if include_token and token is not None
            else recover_webhook_token(webhook, settings)
            if recover_token
            else None
        )
    except WebhookTokenRecoveryError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "WEBHOOK_TOKEN_RECOVERY_FAILED",
                "message": "This webhook token cannot be recovered. Rotate it to create a new URL.",
            },
        ) from exc
    result = webhook_payload(webhook, token=recovered, settings=settings)
    result["token_recovery_required"] = recovered is None
    return result


def webhook_not_found(exc: HTTPException) -> bool:
    return (
        exc.status_code == 404
        and isinstance(exc.detail, dict)
        and exc.detail.get("code") == "WEBHOOK_NOT_FOUND"
    )


async def target_follower_webhook(
    session: AsyncSession,
    webhook_id: int,
    authority_domain: str,
) -> ChannelFollow | FederatedChannelFollow | None:
    ordinary = await session.scalar(
        select(ChannelFollow).where(
            ChannelFollow.id == webhook_id,
            ChannelFollow.active.is_(True),
        )
    )
    if ordinary is not None:
        return ordinary
    federated = await session.get(
        FederatedChannelFollow,
        federated_follow_key(webhook_id, authority_domain, "target"),
    )
    if federated is None or not federated.active:
        return None
    return federated


async def target_follower_webhooks(
    session: AsyncSession,
    guild: Guild,
    *,
    channel: Channel | None = None,
) -> list[ChannelFollow | FederatedChannelFollow]:
    channel_match = and_(
        Channel.id == ChannelFollow.target_channel_id,
        Channel.origin_domain == ChannelFollow.target_channel_domain,
    )
    ordinary_statement = (
        select(ChannelFollow)
        .join(Channel, channel_match)
        .where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            ChannelFollow.active.is_(True),
        )
    )
    federated_match = and_(
        Channel.id == FederatedChannelFollow.target_channel_id,
        Channel.origin_domain == FederatedChannelFollow.target_channel_domain,
    )
    federated_statement = (
        select(FederatedChannelFollow)
        .join(Channel, federated_match)
        .where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            FederatedChannelFollow.local_role == "target",
            FederatedChannelFollow.active.is_(True),
        )
    )
    if channel is not None:
        ordinary_statement = ordinary_statement.where(
            ChannelFollow.target_channel_id == channel.id,
            ChannelFollow.target_channel_domain == channel.origin_domain,
        )
        federated_statement = federated_statement.where(
            FederatedChannelFollow.target_channel_id == channel.id,
            FederatedChannelFollow.target_channel_domain == channel.origin_domain,
        )
    ordinary = list(await session.scalars(ordinary_statement.order_by(ChannelFollow.id)))
    federated = list(await session.scalars(federated_statement.order_by(FederatedChannelFollow.id)))
    return sorted([*ordinary, *federated], key=lambda item: item.id)


async def follower_actor_application(
    session: AsyncSession,
    auth: AuthenticatedUser,
) -> BotApplication | None:
    """Resolve public bot identity for both direct and signed-RPC auth."""

    application = announcement_actor_application(auth)
    if application is None and auth.user.account_type == "bot":
        application = await announcement_application_for_actor(session, auth.user)
    return application


async def authorized_follower_webhook(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    webhook_id: int,
    *,
    scope: str = "webhooks.read",
) -> tuple[ChannelFollow | FederatedChannelFollow, Guild, Channel]:
    follow = await target_follower_webhook(session, webhook_id, settings.domain)
    if follow is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": "Webhook not found."},
        )
    channel = await session.get(
        Channel,
        (follow.target_channel_id, follow.target_channel_domain),
    )
    if (
        channel is None
        or channel.unavailable
        or channel.guild_id is None
        or channel.guild_domain is None
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": "Webhook not found."},
        )
    guild = await local_guild(session, settings, EntityReference(channel.guild_id))
    if (channel.guild_id, channel.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": "Webhook not found."},
        )
    await require_announcement_actor_scope(
        session,
        ChannelAccess(channel=channel, guild=guild, participants=[]),
        auth.user,
        await follower_actor_application(session, auth),
        scope,
    )
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    return follow, guild, channel


async def locked_follower_webhook_management(
    session: AsyncSession,
    redis: Redis,
    auth: AuthenticatedUser,
    follow: ChannelFollow | FederatedChannelFollow,
    guild: Guild,
) -> tuple[ChannelFollow | FederatedChannelFollow, Guild, Channel]:
    """Take the common guild-first lock and recheck a follower mutation."""

    await lock_announcement_mutation(session)
    locked_guild = await session.get(
        Guild,
        (guild.id, guild.origin_domain),
        with_for_update=True,
        populate_existing=True,
    )
    if locked_guild is None or bool(getattr(locked_guild, "unavailable", False)):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    if isinstance(follow, FederatedChannelFollow):
        locked_follow: ChannelFollow | FederatedChannelFollow | None = await session.get(
            FederatedChannelFollow,
            federated_follow_key(follow.id, follow.target_authority_domain, "target"),
            with_for_update=True,
            populate_existing=True,
        )
    else:
        locked_follow = await session.get(
            ChannelFollow,
            follow.id,
            with_for_update=True,
            populate_existing=True,
        )
    if locked_follow is None or not locked_follow.active:
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    channel = await session.get(
        Channel,
        (locked_follow.target_channel_id, locked_follow.target_channel_domain),
        populate_existing=True,
    )
    if (
        channel is None
        or channel.unavailable
        or channel.type != 0
        or channel.encryption_mode == "e2ee"
        or bool(getattr(channel, "e2ee_required", False))
        or (channel.guild_id, channel.guild_domain) != (locked_guild.id, locked_guild.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    await require_announcement_actor_scope(
        session,
        ChannelAccess(channel=channel, guild=locked_guild, participants=[]),
        auth.user,
        await follower_actor_application(session, auth),
        "webhooks.manage",
    )
    await require_permissions(
        session,
        redis,
        locked_guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    return locked_follow, locked_guild, channel


async def follower_webhook_payload(
    session: AsyncSession,
    redis: Redis,
    actor: User,
    follow: ChannelFollow | FederatedChannelFollow,
) -> dict[str, object]:
    source_channel = await session.get(
        Channel,
        (follow.source_channel_id, follow.source_channel_domain),
    )
    source_guild: Guild | None = None
    source_visible = False
    if source_channel is not None and not source_channel.unavailable:
        source_guild = await session.get(
            Guild,
            (source_channel.guild_id, source_channel.guild_domain),
        )
        if source_guild is not None and not source_guild.unavailable:
            try:
                source_permissions = await get_permissions(
                    session,
                    redis,
                    source_guild,
                    actor,
                    channel=source_channel,
                )
            except HTTPException:
                source_permissions = 0
            source_visible = bool(source_permissions & Permission.VIEW_CHANNEL)
    creator = await session.get(User, (follow.creator_id, follow.creator_domain))
    target_channel = await session.get(
        Channel,
        (follow.target_channel_id, follow.target_channel_domain),
    )
    if (
        target_channel is None
        or target_channel.unavailable
        or target_channel.guild_id is None
        or target_channel.guild_domain is None
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": "Webhook not found."},
        )
    result: dict[str, object] = {
        "id": str(follow.id),
        "origin_domain": follow.target_channel_domain,
        "ref": f"{follow.id}@{follow.target_channel_domain}",
        "type": 2,
        "application_id": None,
        "application_domain": None,
        "guild_id": str(target_channel.guild_id),
        "guild_domain": target_channel.guild_domain,
        "channel_id": str(follow.target_channel_id),
        "channel_domain": follow.target_channel_domain,
        "name": getattr(follow, "name", None)
        or (source_guild.name if source_guild is not None else "Channel Follower"),
        "avatar_hash": (
            getattr(follow, "avatar_hash", None)
            if getattr(follow, "avatar_hash", None) is not None
            else (source_guild.icon_hash if source_guild is not None else None)
        ),
        "revoked": not follow.active,
        "federated": isinstance(follow, FederatedChannelFollow),
    }
    if creator is not None:
        result["user"] = user_payload(creator)
    if source_visible and source_guild is not None and source_channel is not None:
        result["source_guild"] = {
            "id": str(source_guild.id),
            "origin_domain": source_guild.origin_domain,
            "name": source_guild.name,
            "icon_hash": source_guild.icon_hash,
        }
        result["source_channel"] = {
            "id": str(source_channel.id),
            "origin_domain": source_channel.origin_domain,
            "name": source_channel.name,
        }
    return result


async def patch_follower_webhook(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    auth: AuthenticatedUser,
    follow: ChannelFollow | FederatedChannelFollow,
    guild: Guild,
    current_channel: Channel,
    payload: WebhookPatch,
    *,
    reason: str | None,
) -> dict[str, object]:
    """Edit Discord-compatible incoming follower identity and destination."""

    follow, guild, current_channel = await locked_follower_webhook_management(
        session,
        redis,
        auth,
        follow,
        guild,
    )

    changes: list[dict[str, object]] = []
    previous_attachment: Attachment | None = None
    previous_channel = current_channel
    if "name" in payload.model_fields_set and payload.name != follow.name:
        changes.append({"key": "name", "old_value": follow.name, "new_value": payload.name})
        follow.name = cast(str, payload.name)
    if "avatar_hash" in payload.model_fields_set:
        if payload.avatar_hash is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "WEBHOOK_AVATAR_UPLOAD_REQUIRED",
                    "message": "Upload follower avatars through the webhook avatar endpoint.",
                },
            )
        if follow.avatar_hash is not None:
            old_hash = follow.avatar_hash
            previous_attachment = await clear_follower_avatar(session, follow)
            changes.append(
                {
                    "key": "avatar_hash",
                    "old_value": old_hash,
                    "new_value": None,
                }
            )

    federation_destination: str | None = None
    if "channel_id" in payload.model_fields_set:
        target = await guild_channel(
            session,
            settings,
            EntityReference(guild.id),
            cast(EntityRef, payload.channel_id),
        )
        if target.type != 0 or target.encryption_mode == "e2ee" or bool(target.e2ee_required):
            raise HTTPException(
                status_code=400,
                detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"},
            )
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("webhook.manage"),
            channel=target,
        )
        await require_announcement_actor_scope(
            session,
            ChannelAccess(channel=target, guild=guild, participants=[]),
            auth.user,
            await follower_actor_application(session, auth),
            "webhooks.manage",
        )
        if (target.id, target.origin_domain) != (
            follow.target_channel_id,
            follow.target_channel_domain,
        ):
            await require_webhook_capacity(
                session,
                guild,
                target,
                adding_to_guild=False,
            )
            changes.append(
                {
                    "key": "channel_id",
                    "old_value": (f"{follow.target_channel_id}@{follow.target_channel_domain}"),
                    "new_value": f"{target.id}@{target.origin_domain}",
                }
            )
            follow.target_channel_id = target.id
            follow.target_channel_domain = target.origin_domain
            if isinstance(follow, FederatedChannelFollow):
                source_projection = stored_announcement_follow_projection(follow)
                signer = await guild_authority_owner(session, settings, guild)
                updated = await build_guild_authority_envelope(
                    session,
                    settings,
                    guild,
                    "guild.announcement.follow.updated",
                    signer,
                    announcement_follow_receipt_content(
                        follow,
                        source_projection,
                    ),
                    context={
                        "guild_id": str(guild.id),
                        "guild_domain": guild.origin_domain,
                        "channel_id": str(target.id),
                        "channel_domain": target.origin_domain,
                    },
                )
                follow.authority_receipt = updated
                federation_destination = follow.source_authority_domain
                await queue_event(
                    session,
                    settings,
                    federation_destination,
                    updated,
                )
            current_channel = target

    if changes:
        rendered = await follower_webhook_payload(session, redis, auth.user, follow)
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            51,
            target_type="webhook",
            target_ref={"id": str(follow.id), "name": rendered["name"]},
            reason=reason,
            changes=changes,
        )
    await session.commit()
    if federation_destination is not None:
        await enqueue_best_effort(federation_deliver, federation_destination)
    if changes:
        await publish_follower_webhook_update(redis, guild, previous_channel)
        if current_channel.id != previous_channel.id:
            await publish_follower_webhook_update(redis, guild, current_channel)
    if previous_attachment is not None:
        await enqueue_best_effort(
            media_local_purge,
            previous_attachment.id,
            previous_attachment.origin_domain,
        )
    return await follower_webhook_payload(session, redis, auth.user, follow)


def webhook_avatar_binding(webhook: Webhook) -> str:
    return f"webhook:{webhook.guild_domain}:{webhook.id}:avatar"


def webhook_avatar_staging_binding(webhook: Webhook, attachment_id: int) -> str:
    return f"webhook-avatar-stage:{webhook.id}:{attachment_id}"


def follower_avatar_binding(follow: ChannelFollow | FederatedChannelFollow) -> str:
    return f"follower:{follow.target_channel_domain}:{follow.id}:avatar"


def follower_avatar_staging_binding(
    follow: ChannelFollow | FederatedChannelFollow,
    attachment_id: int,
) -> str:
    return f"follower-avatar-stage:{follow.id}:{attachment_id}"


async def bot_installation_for_guild(
    session: AsyncSession,
    guild_id: int,
    guild_domain: str,
    actor: User,
) -> BotInstallation | None:
    """Resolve the active member installation owning one bot upload."""

    if getattr(actor, "account_type", "human") != "bot":
        return None
    return cast(
        BotInstallation | None,
        await session.scalar(
            select(BotInstallation).where(
                BotInstallation.guild_id == guild_id,
                BotInstallation.guild_domain == guild_domain,
                BotInstallation.bot_user_id == actor.id,
                BotInstallation.bot_user_domain == actor.origin_domain,
                usable_guild_installation(),
            )
        ),
    )


async def webhook_bot_installation(
    session: AsyncSession,
    webhook: Webhook,
    actor: User,
) -> BotInstallation | None:
    """Resolve the storage owner for a bot-created webhook at guild authority."""

    if getattr(actor, "account_type", "human") != "bot":
        return None
    return await bot_installation_for_guild(
        session,
        webhook.guild_id,
        webhook.guild_domain,
        actor,
    )


def webhook_event_payload(webhook: Webhook, *, channel_id: int | None = None) -> dict[str, object]:
    return {
        "guild_id": str(webhook.guild_id),
        "guild_domain": webhook.guild_domain,
        "channel_id": str(channel_id if channel_id is not None else webhook.channel_id),
        "channel_domain": webhook.channel_domain,
    }


async def publish_webhook_update(
    redis: Redis,
    webhook: Webhook,
    *,
    previous_channel_id: int | None = None,
) -> None:
    topic = guild_topic(webhook.guild_domain, webhook.guild_id)
    if previous_channel_id is not None and previous_channel_id != webhook.channel_id:
        await publish_dispatch(
            redis,
            topic,
            "WEBHOOKS_UPDATE",
            webhook_event_payload(webhook, channel_id=previous_channel_id),
        )
    await publish_dispatch(redis, topic, "WEBHOOKS_UPDATE", webhook_event_payload(webhook))


async def clear_webhook_avatar(
    session: AsyncSession,
    webhook: Webhook,
) -> Attachment | None:
    previous_hash = webhook.avatar_hash
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == webhook_avatar_binding(webhook))
        .with_for_update()
    )
    webhook.avatar_hash = None
    if attachment is not None:
        attachment.asset_binding = None
    if previous_hash is not None:
        # Default webhook avatars are represented by their local digest on
        # each message. Keep historical messages resolvable when an avatar is
        # cleared or replaced, then the old attachment can be purged safely.
        await session.execute(
            update(Message)
            .where(
                Message.webhook_id == webhook.id,
                Message.webhook_domain == webhook.guild_domain,
                Message.webhook_avatar_hash == previous_hash,
            )
            .values(webhook_avatar_hash=None)
        )
    return attachment


async def create_webhook_avatar_ticket_for(
    session: AsyncSession,
    redis: Redis,
    response: Response,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    webhook: Webhook,
    actor: User,
    payload: UploadTicketRequest,
) -> dict[str, object]:
    require_image_type(payload.content_type)
    if payload.size > settings.media_max_emoji_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "WEBHOOK_AVATAR_TOO_LARGE",
                "message": "Webhook avatars must fit within the configured emoji-size limit.",
                "max_bytes": settings.media_max_emoji_bytes,
            },
        )
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=actor.id,
        user_domain=actor.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        actor,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="webhook_avatar",
        bot_installation=await webhook_bot_installation(session, webhook, actor),
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    attachment.asset_binding = webhook_avatar_staging_binding(webhook, attachment.id)
    await session.commit()
    return ticket_payload(attachment, upload_url)


async def create_follower_avatar_ticket_for(
    session: AsyncSession,
    redis: Redis,
    response: Response,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    follow: ChannelFollow | FederatedChannelFollow,
    guild: Guild,
    actor: User,
    payload: UploadTicketRequest,
) -> dict[str, object]:
    require_image_type(payload.content_type)
    if payload.size > settings.media_max_emoji_bytes:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "WEBHOOK_AVATAR_TOO_LARGE",
                "message": "Follower avatars must fit within the configured emoji-size limit.",
                "max_bytes": settings.media_max_emoji_bytes,
            },
        )
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["upload_ticket"],
        user_id=actor.id,
        user_domain=actor.origin_domain,
    )
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        actor,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        purpose="webhook_avatar",
        bot_installation=await bot_installation_for_guild(
            session,
            guild.id,
            guild.origin_domain,
            actor,
        ),
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    attachment.asset_binding = follower_avatar_staging_binding(follow, attachment.id)
    await session.commit()
    return ticket_payload(attachment, upload_url)


async def apply_webhook_avatar(
    session: AsyncSession,
    response: Response,
    settings: Settings,
    webhook: Webhook,
    actor: User,
    payload: AssetCommitRequest,
) -> tuple[dict[str, object], Attachment | None, str | None]:
    attachment = await finalize_attachment(
        session,
        settings,
        actor,
        int(payload.attachment_id),
        required_purpose="webhook_avatar",
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    if attachment.asset_binding != webhook_avatar_staging_binding(webhook, attachment.id):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return (
            {"status": "processing", "attachment": attachment_payload(attachment)},
            None,
            None,
        )
    if attachment.content_sha256 is None:
        raise RuntimeError("clean webhook avatar is missing its content digest")
    require_image_type(attachment.detected_content_type)
    old_hash = webhook.avatar_hash
    attachment.asset_binding = None
    previous = await bind_asset(session, attachment, webhook_avatar_binding(webhook))
    webhook.avatar_hash = attachment.content_sha256
    if old_hash is not None and old_hash != attachment.content_sha256:
        await session.execute(
            update(Message)
            .where(
                Message.webhook_id == webhook.id,
                Message.webhook_domain == webhook.guild_domain,
                Message.webhook_avatar_hash == old_hash,
            )
            .values(webhook_avatar_hash=attachment.content_sha256)
        )
    return webhook_payload(webhook), previous, old_hash


async def apply_follower_avatar(
    session: AsyncSession,
    response: Response,
    settings: Settings,
    follow: ChannelFollow | FederatedChannelFollow,
    actor: User,
    payload: AssetCommitRequest,
) -> tuple[dict[str, object], Attachment | None, str | None]:
    attachment = await finalize_attachment(
        session,
        settings,
        actor,
        int(payload.attachment_id),
        required_purpose="webhook_avatar",
        federated_guild_upload=is_federated_human_authority_upload(actor, settings),
    )
    if attachment.asset_binding != follower_avatar_staging_binding(
        follow,
        attachment.id,
    ):
        raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    if attachment.scan_status != "clean":
        await session.commit()
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
        response.status_code = status.HTTP_202_ACCEPTED
        return (
            {"status": "processing", "attachment": attachment_payload(attachment)},
            None,
            None,
        )
    if attachment.content_sha256 is None:
        raise RuntimeError("clean follower avatar is missing its content digest")
    require_image_type(attachment.detected_content_type)
    old_hash = follow.avatar_hash
    attachment.asset_binding = None
    previous = await bind_asset(session, attachment, follower_avatar_binding(follow))
    follow.avatar_hash = attachment.content_sha256
    return {}, previous, old_hash


async def clear_follower_avatar(
    session: AsyncSession,
    follow: ChannelFollow | FederatedChannelFollow,
) -> Attachment | None:
    attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == follower_avatar_binding(follow))
        .with_for_update()
    )
    follow.avatar_hash = None
    if attachment is not None:
        attachment.asset_binding = None
    return attachment


@router.post("/api/v1/guilds/{guild_id}/channels/{channel_id}/webhooks")
async def create_webhook(
    guild_id: EntityRef,
    channel_id: EntityRef,
    payload: WebhookCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    proxied = await _proxy_webhook_guild_operation(
        session,
        settings,
        guild_id,
        auth,
        "webhook.create",
        {
            "channel_ref": str(channel_id),
            "data": payload.model_dump(mode="json"),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    guild = await local_guild(session, settings, guild_id)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    if channel.type not in GUILD_WEBHOOK_CHANNEL_TYPES:
        raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"})
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    await require_webhook_capacity(
        session,
        guild,
        channel,
        adding_to_guild=True,
    )
    token = new_webhook_token()
    webhook = Webhook(
        id=await snowflake.mint(),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        name=payload.name,
        token_hash=token_digest(token),
        creator_id=auth.user.id,
        creator_domain=auth.user.origin_domain,
    )
    store_webhook_token(webhook, token, settings)
    session.add(webhook)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        50,
        target_type="webhook",
        target_ref={"id": str(webhook.id), "name": webhook.name},
        reason=normalize_audit_reason(reason),
        changes=[
            {"key": "name", "new_value": webhook.name},
            {
                "key": "channel_id",
                "new_value": f"{webhook.channel_id}@{webhook.channel_domain}",
            },
        ],
    )
    await session.commit()
    await publish_webhook_update(redis, webhook)
    return managed_webhook_payload(
        webhook,
        settings,
        token=token,
        include_token=True,
    )


async def authorized_webhook(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    webhook_id: int,
    *,
    for_update: bool = False,
) -> Webhook:
    statement = select(Webhook).where(Webhook.id == webhook_id)
    if for_update:
        statement = statement.with_for_update()
    webhook = await session.scalar(statement)
    if webhook is None or webhook.revoked_at is not None:
        raise HTTPException(
            status_code=404,
            detail={"code": "WEBHOOK_NOT_FOUND", "message": "Webhook not found."},
        )
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    channel = await guild_channel(
        session,
        settings,
        EntityReference(webhook.guild_id),
        EntityReference(webhook.channel_id),
    )
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    return webhook


async def manageable_webhook(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    auth: AuthenticatedUser,
    webhook_id: int,
) -> Webhook:
    return await authorized_webhook(
        session,
        redis,
        settings,
        auth,
        webhook_id,
        for_update=True,
    )


@router.get("/api/v1/webhooks/{webhook_id}")
async def get_webhook(
    webhook_id: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    recover_token: bool = True,
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.get",
        {},
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    try:
        webhook = await authorized_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
        )
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, _, _ = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
        )
        return await follower_webhook_payload(session, redis, auth.user, follow)
    return managed_webhook_payload(webhook, settings, recover_token=recover_token)


@router.get("/api/v1/guilds/{guild_id}/webhooks")
async def list_webhooks(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    recover_tokens: bool = True,
) -> list[dict[str, object]]:
    proxied = await _proxy_webhook_guild_operation(
        session,
        settings,
        guild_id,
        auth,
        "webhook.list",
        {},
    )
    if proxied is not None:
        return cast(list[dict[str, object]], proxied.body)
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.webhook.list")
    )
    rows = list(
        await session.scalars(
            select(Webhook)
            .where(
                Webhook.guild_id == guild.id,
                Webhook.guild_domain == guild.origin_domain,
                Webhook.revoked_at.is_(None),
            )
            .order_by(Webhook.id)
        )
    )
    followers = await target_follower_webhooks(session, guild)
    if auth.user.account_type == "bot":
        channel_access: dict[tuple[int, str], bool] = {}

        async def can_manage_channel(channel_ref: tuple[int, str]) -> bool:
            cached = channel_access.get(channel_ref)
            if cached is not None:
                return cached
            channel = await session.get(Channel, channel_ref)
            if (
                channel is None
                or channel.unavailable
                or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
            ):
                channel_access[channel_ref] = False
                return False
            try:
                permissions = await get_permissions(
                    session,
                    redis,
                    guild,
                    auth.user,
                    channel=channel,
                )
            except HTTPException:
                permissions = 0
            allowed = bool(permissions & Permission.MANAGE_WEBHOOKS)
            channel_access[channel_ref] = allowed
            return allowed

        rows = [
            item
            for item in rows
            if await can_manage_channel((item.channel_id, item.channel_domain))
        ]
        followers = [
            item
            for item in followers
            if await can_manage_channel((item.target_channel_id, item.target_channel_domain))
        ]
    return [
        *[managed_webhook_payload(item, settings, recover_token=recover_tokens) for item in rows],
        *[
            await follower_webhook_payload(session, redis, auth.user, follow)
            for follow in followers
        ],
    ]


@router.get("/api/v1/guilds/{guild_id}/channels/{channel_id}/webhooks")
async def list_channel_webhooks(
    guild_id: EntityRef,
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    recover_tokens: bool = True,
) -> list[dict[str, object]]:
    proxied = await _proxy_webhook_guild_operation(
        session,
        settings,
        guild_id,
        auth,
        "webhook.list_channel",
        {"channel_ref": str(channel_id)},
    )
    if proxied is not None:
        return cast(list[dict[str, object]], proxied.body)
    guild = await local_guild(session, settings, guild_id)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("webhook.manage"),
        channel=channel,
    )
    rows = list(
        await session.scalars(
            select(Webhook)
            .where(
                Webhook.guild_id == guild.id,
                Webhook.guild_domain == guild.origin_domain,
                Webhook.channel_id == channel.id,
                Webhook.channel_domain == channel.origin_domain,
                Webhook.revoked_at.is_(None),
            )
            .order_by(Webhook.id)
        )
    )
    followers = await target_follower_webhooks(session, guild, channel=channel)
    return [
        *[managed_webhook_payload(item, settings, recover_token=recover_tokens) for item in rows],
        *[
            await follower_webhook_payload(session, redis, auth.user, follow)
            for follow in followers
        ],
    ]


@router.patch("/api/v1/webhooks/{webhook_id}")
async def patch_webhook(
    webhook_id: EntityRef,
    payload: WebhookPatch,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.update",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    try:
        webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, follower_guild, follower_channel = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
            scope="webhooks.manage",
        )
        return await patch_follower_webhook(
            session,
            redis,
            snowflake,
            settings,
            auth,
            follow,
            follower_guild,
            follower_channel,
            payload,
            reason=normalize_audit_reason(reason),
        )
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    previous_channel_id = webhook.channel_id
    e2ee_revocation: tuple[Guild | None, list[Channel]] = (None, [])
    previous_attachment: Attachment | None = None
    changes: list[dict[str, object]] = []
    if "name" in payload.model_fields_set and payload.name != webhook.name:
        changes.append({"key": "name", "old_value": webhook.name, "new_value": payload.name})
        webhook.name = cast(str, payload.name)
    if "avatar_hash" in payload.model_fields_set:
        if payload.avatar_hash is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "WEBHOOK_AVATAR_UPLOAD_REQUIRED",
                    "message": "Upload webhook avatars through the webhook avatar endpoint.",
                },
            )
        if webhook.avatar_hash is not None:
            old_hash = webhook.avatar_hash
            previous_attachment = await clear_webhook_avatar(session, webhook)
            changes.append({"key": "avatar_hash", "old_value": old_hash, "new_value": None})
    if "channel_id" in payload.model_fields_set:
        target_ref = cast(EntityRef, payload.channel_id)
        target = await guild_channel(
            session,
            settings,
            EntityReference(webhook.guild_id),
            target_ref,
        )
        if target.type not in GUILD_WEBHOOK_CHANNEL_TYPES:
            raise HTTPException(status_code=400, detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"})
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("webhook.manage"),
            channel=target,
        )
        if (target.id, target.origin_domain) != (webhook.channel_id, webhook.channel_domain):
            from app.api.webhook_e2ee import revoke_webhook_e2ee_access

            e2ee_revocation = await revoke_webhook_e2ee_access(
                session,
                settings,
                webhook,
                auth.user,
            )
            await require_webhook_capacity(
                session,
                guild,
                target,
                adding_to_guild=False,
            )
            changes.append(
                {
                    "key": "channel_id",
                    "old_value": f"{webhook.channel_id}@{webhook.channel_domain}",
                    "new_value": f"{target.id}@{target.origin_domain}",
                }
            )
            webhook.channel_id = target.id
            webhook.channel_domain = target.origin_domain
    if changes:
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            51,
            target_type="webhook",
            target_ref={"id": str(webhook.id), "name": webhook.name},
            reason=normalize_audit_reason(reason),
            changes=changes,
        )
    await session.commit()
    if e2ee_revocation[1]:
        from app.api.webhook_e2ee import publish_webhook_e2ee_revocation

        await publish_webhook_e2ee_revocation(
            session,
            redis,
            e2ee_revocation[0],
            e2ee_revocation[1],
        )
    if changes:
        await publish_webhook_update(redis, webhook, previous_channel_id=previous_channel_id)
    if previous_attachment is not None:
        await enqueue_best_effort(
            media_local_purge, previous_attachment.id, previous_attachment.origin_domain
        )
    return managed_webhook_payload(webhook, settings, recover_token=True)


@router.post("/api/v1/webhooks/{webhook_id}/rotate")
async def rotate_webhook(
    webhook_id: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.rotate",
        {"reason": normalize_audit_reason(reason)},
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    from app.api.webhook_e2ee import (
        publish_webhook_e2ee_revocation,
        revoke_webhook_e2ee_access,
    )

    e2ee_guild, e2ee_channels = await revoke_webhook_e2ee_access(
        session,
        settings,
        webhook,
        auth.user,
    )
    token = new_webhook_token()
    store_webhook_token(webhook, token, settings)
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        51,
        target_type="webhook",
        target_ref={"id": str(webhook.id), "name": webhook.name},
        reason=normalize_audit_reason(reason),
        changes=[{"key": "token", "old_value": "rotated", "new_value": "rotated"}],
    )
    await session.commit()
    await publish_webhook_e2ee_revocation(
        session,
        redis,
        e2ee_guild,
        e2ee_channels,
    )
    await publish_webhook_update(redis, webhook)
    return managed_webhook_payload(
        webhook,
        settings,
        token=token,
        include_token=True,
    )


@router.delete("/api/v1/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> Response:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.delete",
        {"reason": normalize_audit_reason(reason)},
    )
    if proxied is not None:
        return Response(status_code=204)
    try:
        webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, guild, _ = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
            scope="webhooks.manage",
        )
        follower_payload = await follower_webhook_payload(session, redis, auth.user, follow)
        await delete_announcement_follow_from_target(
            session,
            redis,
            settings,
            auth.user,
            await follower_actor_application(session, auth),
            follow,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            52,
            target_type="webhook",
            target_ref={"id": str(follow.id), "name": follower_payload["name"]},
            reason=normalize_audit_reason(reason),
        )
        await session.commit()
        return Response(status_code=204)
    from app.api.webhook_e2ee import (
        publish_webhook_e2ee_revocation,
        revoke_webhook_e2ee_access,
    )

    e2ee_guild, e2ee_channels = await revoke_webhook_e2ee_access(
        session,
        settings,
        webhook,
        auth.user,
    )
    webhook.revoked_at = datetime.now(UTC)
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        52,
        target_type="webhook",
        target_ref={"id": str(webhook.id), "name": webhook.name},
        reason=normalize_audit_reason(reason),
    )
    await session.commit()
    await publish_webhook_e2ee_revocation(
        session,
        redis,
        e2ee_guild,
        e2ee_channels,
    )
    await publish_webhook_update(redis, webhook)
    return Response(status_code=204)


@router.post("/api/v1/webhooks/{webhook_id}/avatar/tickets", status_code=201)
async def create_webhook_avatar_ticket(
    webhook_id: EntityRef,
    payload: UploadTicketRequest,
    response: Response,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.avatar.ticket",
        {"data": payload.model_dump(mode="json")},
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    try:
        webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, guild, _ = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
            scope="webhooks.manage",
        )
        follow, guild, _ = await locked_follower_webhook_management(
            session,
            redis,
            auth,
            follow,
            guild,
        )
        return await create_follower_avatar_ticket_for(
            session,
            redis,
            response,
            settings,
            snowflake,
            follow,
            guild,
            auth.user,
            payload,
        )
    return await create_webhook_avatar_ticket_for(
        session,
        redis,
        response,
        settings,
        snowflake,
        webhook,
        auth.user,
        payload,
    )


@router.put("/api/v1/webhooks/{webhook_id}/avatar")
async def commit_webhook_avatar(
    webhook_id: EntityRef,
    payload: AssetCommitRequest,
    response: Response,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.avatar.commit",
        {
            "data": payload.model_dump(mode="json"),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied is not None:
        response.status_code = proxied.status_code
        return cast(dict[str, object], proxied.body)
    try:
        webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, guild, _ = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
            scope="webhooks.manage",
        )
        locked_follow, guild, channel = await locked_follower_webhook_management(
            session,
            redis,
            auth,
            follow,
            guild,
        )
        rendered, previous, old_hash = await apply_follower_avatar(
            session,
            response,
            settings,
            locked_follow,
            auth.user,
            payload,
        )
        if response.status_code == status.HTTP_202_ACCEPTED:
            return rendered
        if old_hash != locked_follow.avatar_hash:
            await add_audit_entry(
                session,
                snowflake,
                guild,
                auth.user,
                51,
                target_type="webhook",
                target_ref={"id": str(locked_follow.id), "name": locked_follow.name},
                reason=normalize_audit_reason(reason),
                changes=[
                    {
                        "key": "avatar_hash",
                        "old_value": old_hash,
                        "new_value": locked_follow.avatar_hash,
                    }
                ],
            )
        await session.commit()
        await publish_follower_webhook_update(redis, guild, channel)
        if previous is not None:
            await enqueue_best_effort(
                media_local_purge,
                previous.id,
                previous.origin_domain,
            )
        return await follower_webhook_payload(
            session,
            redis,
            auth.user,
            locked_follow,
        )
    rendered, previous, old_hash = await apply_webhook_avatar(
        session, response, settings, webhook, auth.user, payload
    )
    if response.status_code == status.HTTP_202_ACCEPTED:
        return rendered
    if old_hash != webhook.avatar_hash:
        guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            51,
            target_type="webhook",
            target_ref={"id": str(webhook.id), "name": webhook.name},
            reason=normalize_audit_reason(reason),
            changes=[
                {
                    "key": "avatar_hash",
                    "old_value": old_hash,
                    "new_value": webhook.avatar_hash,
                }
            ],
        )
    await session.commit()
    await publish_webhook_update(redis, webhook)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return webhook_payload(webhook)


@router.delete("/api/v1/webhooks/{webhook_id}/avatar")
async def delete_webhook_avatar(
    webhook_id: EntityRef,
    guild_ref: EntityRef | None = Query(default=None),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason", max_length=512),
) -> dict[str, object]:
    local_id, proxied = await _webhook_management_target(
        session,
        settings,
        auth,
        webhook_id,
        guild_ref,
        "webhook.avatar.delete",
        {"reason": normalize_audit_reason(reason)},
    )
    if proxied is not None:
        return cast(dict[str, object], proxied.body)
    try:
        webhook = await manageable_webhook(session, redis, settings, auth, local_id)
    except HTTPException as exc:
        if not webhook_not_found(exc):
            raise
        follow, guild, _ = await authorized_follower_webhook(
            session,
            redis,
            settings,
            auth,
            local_id,
            scope="webhooks.manage",
        )
        locked_follow, guild, channel = await locked_follower_webhook_management(
            session,
            redis,
            auth,
            follow,
            guild,
        )
        old_hash = locked_follow.avatar_hash
        previous = await clear_follower_avatar(session, locked_follow)
        if old_hash is not None:
            await add_audit_entry(
                session,
                snowflake,
                guild,
                auth.user,
                51,
                target_type="webhook",
                target_ref={"id": str(locked_follow.id), "name": locked_follow.name},
                reason=normalize_audit_reason(reason),
                changes=[{"key": "avatar_hash", "old_value": old_hash, "new_value": None}],
            )
        await session.commit()
        if old_hash is not None:
            await publish_follower_webhook_update(redis, guild, channel)
        if previous is not None:
            await enqueue_best_effort(
                media_local_purge,
                previous.id,
                previous.origin_domain,
            )
        return await follower_webhook_payload(
            session,
            redis,
            auth.user,
            locked_follow,
        )
    old_hash = webhook.avatar_hash
    previous = await clear_webhook_avatar(session, webhook)
    if old_hash is not None:
        guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            51,
            target_type="webhook",
            target_ref={"id": str(webhook.id), "name": webhook.name},
            reason=normalize_audit_reason(reason),
            changes=[{"key": "avatar_hash", "old_value": old_hash, "new_value": None}],
        )
    await session.commit()
    if old_hash is not None:
        await publish_webhook_update(redis, webhook)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return webhook_payload(webhook)


def request_webhook_token(path_token: str | None, authorization: str | None) -> str:
    if path_token is not None:
        return path_token
    if authorization is not None and authorization.startswith("Bearer "):
        return authorization.removeprefix("Bearer ")
    return ""


async def token_webhook(
    session: AsyncSession,
    webhook_id: int,
    token: str,
    *,
    for_update: bool = False,
) -> Webhook:
    statement = select(Webhook).where(Webhook.id == webhook_id)
    if for_update:
        statement = statement.with_for_update()
    webhook = await session.scalar(statement)
    if (
        webhook is None
        or webhook.revoked_at is not None
        or not token.startswith("kwh_")
        or not hmac.compare_digest(token_digest(token), webhook.token_hash)
    ):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_NOT_FOUND"})
    return webhook


async def webhook_creator(
    session: AsyncSession,
    settings: Settings,
    webhook: Webhook,
) -> User:
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None or creator.disabled_at is not None:
        raise HTTPException(status_code=410, detail={"code": "WEBHOOK_CREATOR_MISSING"})
    return creator


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/avatar/tickets",
    status_code=201,
)
async def create_webhook_avatar_ticket_with_token(
    webhook_id: Snowflake,
    path_token: str,
    payload: UploadTicketRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    creator = await webhook_creator(session, settings, webhook)
    return await create_webhook_avatar_ticket_for(
        session,
        redis,
        response,
        settings,
        snowflake,
        webhook,
        creator,
        payload,
    )


@router.put("/api/v1/webhooks/{webhook_id}/{path_token}/avatar")
async def commit_webhook_avatar_with_token(
    webhook_id: Snowflake,
    path_token: str,
    payload: AssetCommitRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    creator = await webhook_creator(session, settings, webhook)
    rendered, previous, _ = await apply_webhook_avatar(
        session, response, settings, webhook, creator, payload
    )
    if response.status_code == status.HTTP_202_ACCEPTED:
        return rendered
    await session.commit()
    await publish_webhook_update(redis, webhook)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return webhook_payload(webhook)


@router.delete("/api/v1/webhooks/{webhook_id}/{path_token}/avatar")
async def delete_webhook_avatar_with_token(
    webhook_id: Snowflake,
    path_token: str,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    old_hash = webhook.avatar_hash
    previous = await clear_webhook_avatar(session, webhook)
    await session.commit()
    if old_hash is not None:
        await publish_webhook_update(redis, webhook)
    if previous is not None:
        await enqueue_best_effort(media_local_purge, previous.id, previous.origin_domain)
    return webhook_payload(webhook)


@router.get("/api/v1/webhooks/{webhook_id}/{path_token}")
async def get_webhook_with_token(
    webhook_id: Snowflake,
    path_token: str,
    session: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token)
    return webhook_payload(webhook)


@router.patch("/api/v1/webhooks/{webhook_id}/{path_token}")
async def patch_webhook_with_token(
    webhook_id: Snowflake,
    path_token: str,
    payload: WebhookPatch,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    if "channel_id" in payload.model_fields_set:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "WEBHOOK_TOKEN_CANNOT_MOVE_CHANNEL",
                "message": "Moving a webhook requires an authenticated guild manager.",
            },
        )
    if payload.avatar_hash is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_AVATAR_UPLOAD_REQUIRED",
                "message": "Upload webhook avatars through the webhook avatar endpoint.",
            },
        )
    changed = False
    previous_attachment: Attachment | None = None
    if "name" in payload.model_fields_set and payload.name != webhook.name:
        webhook.name = cast(str, payload.name)
        changed = True
    if "avatar_hash" in payload.model_fields_set and webhook.avatar_hash is not None:
        previous_attachment = await clear_webhook_avatar(session, webhook)
        changed = True
    await session.commit()
    if changed:
        await publish_webhook_update(redis, webhook)
    if previous_attachment is not None:
        await enqueue_best_effort(
            media_local_purge, previous_attachment.id, previous_attachment.origin_domain
        )
    return webhook_payload(webhook)


@router.delete("/api/v1/webhooks/{webhook_id}/{path_token}", status_code=204)
async def delete_webhook_with_token(
    webhook_id: Snowflake,
    path_token: str,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(status_code=410, detail={"code": "WEBHOOK_CREATOR_MISSING"})
    from app.api.webhook_e2ee import (
        publish_webhook_e2ee_revocation,
        revoke_webhook_e2ee_access,
    )

    e2ee_guild, e2ee_channels = await revoke_webhook_e2ee_access(
        session,
        settings,
        webhook,
        creator,
    )
    webhook.revoked_at = datetime.now(UTC)
    await session.commit()
    await publish_webhook_e2ee_revocation(
        session,
        redis,
        e2ee_guild,
        e2ee_channels,
    )
    await publish_webhook_update(redis, webhook)
    return Response(status_code=204)


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/attachments",
    status_code=201,
)
async def create_webhook_attachment_ticket(
    webhook_id: Snowflake,
    path_token: str,
    payload: UploadTicketRequest,
    channel_id: Annotated[EntityRef | None, Query()] = None,
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    e2ee_device_id = e2ee_device_id if isinstance(e2ee_device_id, str) else None
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    target_ref = channel_id or EntityRef(f"{webhook.channel_id}@{webhook.channel_domain}")
    from app.api.webhook_e2ee import (
        require_webhook_e2ee_participation,
        webhook_e2ee_target_channel,
    )

    target = await webhook_e2ee_target_channel(session, settings, webhook, target_ref)
    if payload.encryption_mode == "e2ee":
        await require_webhook_e2ee_participation(
            session,
            webhook,
            target,
            e2ee_device_id,
        )
    elif payload.encryption_mode != "plaintext" or e2ee_device_id is not None:
        raise HTTPException(status_code=409, detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"})
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(status_code=410, detail={"code": "WEBHOOK_CREATOR_MISSING"})
    bot_installation = await webhook_bot_installation(session, webhook, creator)
    attachment, upload_url = await create_upload_ticket(
        session,
        settings,
        snowflake,
        creator,
        filename=payload.filename,
        content_type=payload.content_type,
        size=payload.size,
        encryption_mode=payload.encryption_mode,
        encryption_protocol=payload.encryption_protocol,
        duration_secs=payload.duration_secs,
        waveform=payload.waveform,
        purpose="webhook_attachment",
        bot_installation=bot_installation,
        federated_guild_upload=is_federated_human_authority_upload(creator, settings),
    )
    attachment.upload_channel_id = target.id
    attachment.upload_channel_domain = target.origin_domain
    attachment.asset_binding = f"{webhook_attachment_binding_prefix(webhook.id)}{attachment.id}"
    await session.commit()
    return ticket_payload(attachment, upload_url)


@router.post("/api/v1/webhooks/{webhook_id}/{path_token}", response_model=None)
@router.post("/api/v1/webhooks/{webhook_id}", response_model=None)
async def execute_webhook(
    webhook_id: Snowflake,
    payload: WebhookExecute,
    request: Request,
    wait: bool = Query(default=False),
    thread_id: Annotated[EntityRef | None, Query()] = None,
    with_components: bool = Query(default=False),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", min_length=1, max_length=128
    ),
    path_token: str | None = None,
    authorization: str | None = Header(default=None),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object] | Response:
    del request
    e2ee_device_id = e2ee_device_id if isinstance(e2ee_device_id, str) else None
    supplied = request_webhook_token(path_token, authorization)
    webhook = await token_webhook(session, int(webhook_id), supplied, for_update=True)
    components = webhook_execution_components(
        payload,
        application_owned=webhook.type == 3,
        with_components=with_components,
    )
    validate_webhook_components_v2_body(
        flags=payload.flags,
        content=payload.content,
        embeds=payload.embeds,
        components=components,
        attachment_ids=payload.attachment_ids,
        poll=payload.poll,
        sticker_ids=payload.sticker_ids,
    )
    application_ref = (
        (webhook.application_id, webhook.application_domain)
        if webhook.type == 3
        and webhook.application_id is not None
        and webhook.application_domain is not None
        else None
    )
    if with_components and has_interactive_components(components) and application_ref is None:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "WEBHOOK_COMPONENT_APPLICATION_REQUIRED",
                "message": "Only application-owned webhooks may send interactive components.",
            },
        )
    if (
        payload.content is None
        and not payload.embeds
        and not components
        and payload.poll is None
        and not payload.attachment_ids
        and not payload.sticker_ids
        and payload.e2ee is None
    ):
        raise HTTPException(status_code=400, detail={"code": "MESSAGE_BODY_REQUIRED"})
    rate_key = f"rate:webhook:{webhook.id}"
    attempts = int(
        cast(
            int | str,
            await cast(
                Awaitable[object],
                redis.eval(WEBHOOK_RATE_SCRIPT, 1, rate_key, "2"),
            ),
        )
    )
    if attempts > 5:
        raise HTTPException(
            status_code=429,
            detail={"code": "WEBHOOK_RATE_LIMITED", "retry_after_ms": 2000},
        )
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id), for_update=True)
    webhook_channel = await guild_channel(
        session,
        settings,
        EntityReference(webhook.guild_id),
        EntityReference(webhook.channel_id),
    )
    if webhook_channel.type not in GUILD_WEBHOOK_CHANNEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_REQUIRES_TEXT_CHANNEL"},
        )
    validate_webhook_thread_target(
        channel_type=webhook_channel.type,
        has_thread_id=thread_id is not None,
        thread_name=payload.thread_name,
        applied_tags=payload.applied_tags,
    )
    channel = webhook_channel
    if thread_id is not None:
        thread_ref = thread_id.resolve(settings.domain)
        thread = await session.get(Channel, thread_ref)
        if (
            thread is None
            or thread.type not in {10, 11, 12}
            or (thread.parent_id, thread.parent_domain)
            != (webhook_channel.id, webhook_channel.origin_domain)
            or thread.unavailable
        ):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "WEBHOOK_THREAD_NOT_FOUND",
                    "message": "The requested active thread is not available to this webhook.",
                },
            )
        if thread.locked:
            raise HTTPException(status_code=403, detail={"code": "THREAD_LOCKED"})
        if thread.archived:
            thread.archived = False
            thread.archive_timestamp = datetime.now(UTC)
        channel = thread
    if payload.e2ee is None and e2ee_device_id is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    try:
        validate_message_encryption_policy(
            channel.encryption_mode,
            content=payload.content,
            e2ee=payload.e2ee,
            policy_generation=channel.encryption_policy_generation,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    except MessageEncryptionPolicyError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "WEBHOOK_CREATOR_MISSING",
                "message": "The webhook owner no longer exists, so the webhook cannot execute.",
            },
        )
    resolved_mentions = (
        None
        if payload.e2ee is not None
        else await resolved_webhook_mentions(
            session,
            redis,
            settings,
            guild,
            channel,
            creator,
            payload.allowed_mentions,
            payload.content,
            components,
        )
    )
    nonce = (
        f"w{webhook.id:x}{hashlib.blake2s(idempotency_key.encode(), digest_size=12).hexdigest()}"
        if idempotency_key is not None
        else None
    )
    if webhook_channel.type == 15 and thread_id is None:
        from app.api.threads import ThreadCreate, create_thread_service

        starter = MessageCreate(
            content=payload.content,
            e2ee=payload.e2ee,
            embeds=payload.embeds,
            components=components,
            poll=payload.poll,
            tts=payload.tts,
            flags=payload.flags,
            attachment_ids=[str(item) for item in payload.attachment_ids],
            sticker_ids=payload.sticker_ids,
            mention_user_ids=[],
            client_nonce=nonce,
        )
        thread_result = await create_thread_service(
            EntityRef(f"{webhook_channel.id}@{webhook_channel.origin_domain}"),
            ThreadCreate(
                name=cast(str, payload.thread_name),
                applied_tag_ids=[str(item) for item in payload.applied_tags],
                message=starter,
            ),
            cast(AuthenticatedUser, SimpleNamespace(user=creator)),
            session,
            redis,
            snowflake,
            settings,
            starter_admission_options=webhook_message_admission_options(
                webhook,
                creator,
                settings,
                device_id=e2ee_device_id,
                name=payload.username,
                avatar_hash=None if payload.avatar_url else webhook.avatar_hash,
                avatar_url=payload.avatar_url,
                mentions=resolved_mentions,
                tts=payload.tts,
                flags=payload.flags,
            ),
        )
        starter_result = thread_result.get("message")
        if not isinstance(starter_result, dict):
            raise RuntimeError("forum webhook thread did not produce a starter message")
        return starter_result if wait else Response(status_code=204)
    rendered = await create_message(
        EntityRef(f"{channel.id}@{channel.origin_domain}"),
        MessageCreate(
            content=payload.content,
            e2ee=payload.e2ee,
            embeds=payload.embeds,
            components=components,
            poll=payload.poll,
            tts=payload.tts,
            flags=payload.flags,
            attachment_ids=[str(item) for item in payload.attachment_ids],
            sticker_ids=payload.sticker_ids,
            mention_user_ids=[],
            client_nonce=nonce,
        ),
        Response(),
        cast(AuthenticatedUser, SimpleNamespace(user=creator)),
        session,
        redis,
        snowflake,
        settings,
        webhook_message_admission_options(
            webhook,
            creator,
            settings,
            device_id=e2ee_device_id,
            name=payload.username,
            avatar_hash=None if payload.avatar_url else webhook.avatar_hash,
            avatar_url=payload.avatar_url,
            mentions=resolved_mentions,
            tts=payload.tts,
            flags=payload.flags,
        ),
    )
    return rendered if wait else Response(status_code=204)


WEBHOOK_COMPAT_JSON_LIMITS = JsonTreeLimits(
    max_depth=16,
    max_nodes=8_192,
    max_object_members=512,
    max_array_members=2_048,
    max_key_bytes=128,
    max_string_bytes=64 * 1024,
)


def _clip_webhook_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.replace("\x00", "").strip()
    if not cleaned:
        return None
    return cleaned if len(cleaned) <= maximum else f"{cleaned[: maximum - 1]}…"


def _compat_http_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048:
        return None
    return value if value.startswith(("https://", "http://")) else None


def _slack_color(value: str | None) -> int | None:
    named = {"good": 0x2EB886, "warning": 0xDAA038, "danger": 0xA30200}
    if value is None:
        return None
    if value.casefold() in named:
        return named[value.casefold()]
    candidate = value.removeprefix("#")
    return int(candidate, 16) if re.fullmatch(r"[0-9a-fA-F]{6}", candidate) else None


def _slack_timestamp(value: int | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        seconds = int(value)
        parsed = datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if 0 <= seconds <= 253_402_300_799 else None


def slack_embeds(payload: SlackWebhookExecute) -> list[Embed]:
    result: list[Embed] = []
    for item in payload.attachments:
        description = "\n\n".join(
            part for part in (item.pretext, item.text or item.fallback) if part is not None
        )
        result.append(
            Embed(
                title=item.title,
                description=_clip_webhook_text(description, 4096),
                url=_compat_http_url(item.title_link),
                timestamp=_slack_timestamp(item.ts),
                color=_slack_color(item.color),
                footer=(
                    EmbedFooter(
                        text=item.footer,
                        icon_url=_compat_http_url(item.footer_icon),
                    )
                    if item.footer is not None
                    else None
                ),
                author=(
                    EmbedAuthor(
                        name=item.author_name,
                        url=_compat_http_url(item.author_link),
                        icon_url=_compat_http_url(item.author_icon),
                    )
                    if item.author_name is not None
                    else None
                ),
                fields=[
                    EmbedField(name=field.title, value=field.value, inline=field.short)
                    for field in item.fields
                ],
            )
        )
    return result


def github_webhook_embed(event: str, payload: dict[str, Any]) -> Embed:
    if event not in GITHUB_WEBHOOK_EVENTS:
        raise HTTPException(
            status_code=400,
            detail={"code": "GITHUB_WEBHOOK_EVENT_UNSUPPORTED"},
        )
    try:
        validate_json_tree(
            payload,
            limits=WEBHOOK_COMPAT_JSON_LIMITS,
            label="GitHub webhook payload",
            allow_floats=True,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "GITHUB_WEBHOOK_PAYLOAD_INVALID"},
        ) from exc

    repository = payload.get("repository")
    repository = repository if isinstance(repository, dict) else {}
    sender = payload.get("sender")
    sender = sender if isinstance(sender, dict) else {}
    repo_name = _clip_webhook_text(repository.get("full_name"), 100) or "repository"
    actor = _clip_webhook_text(sender.get("login"), 100) or "GitHub"
    action = _clip_webhook_text(payload.get("action"), 64)
    subject: dict[str, Any] = {}
    for key in (
        "comment",
        "pull_request",
        "issue",
        "release",
        "discussion",
        "check_run",
        "check_suite",
        "review",
    ):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            subject = candidate
            break

    if event == "push":
        commits = payload.get("commits")
        commit_count = len(commits) if isinstance(commits, list) else 0
        ref = _clip_webhook_text(payload.get("ref"), 100) or "a ref"
        title = f"{actor} pushed {commit_count} commit{'s' if commit_count != 1 else ''} to {ref}"
        head = payload.get("head_commit")
        head = head if isinstance(head, dict) else {}
        description = _clip_webhook_text(head.get("message"), 4096)
        target_url = _compat_http_url(payload.get("compare"))
    elif event in {"create", "delete"}:
        ref_type = _clip_webhook_text(payload.get("ref_type"), 32) or "ref"
        ref = _clip_webhook_text(payload.get("ref"), 100) or ref_type
        title = f"{actor} {event}d {ref_type} {ref}"
        description = _clip_webhook_text(payload.get("description"), 4096)
        target_url = _compat_http_url(repository.get("html_url"))
    else:
        rendered_action = f" {action}" if action is not None else ""
        title = f"{actor}{rendered_action} {event.replace('_', ' ')} in {repo_name}"
        description = _clip_webhook_text(
            subject.get("body")
            or subject.get("title")
            or subject.get("name")
            or subject.get("conclusion"),
            4096,
        )
        target_url = _compat_http_url(subject.get("html_url")) or _compat_http_url(
            repository.get("html_url")
        )

    return Embed(
        title=_clip_webhook_text(title, 256),
        description=description,
        url=target_url,
        color=0x24292F,
        author=EmbedAuthor(
            name=actor,
            url=_compat_http_url(sender.get("html_url")),
            icon_url=_compat_http_url(sender.get("avatar_url")),
        ),
        footer=EmbedFooter(text=repo_name),
    )


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/slack",
    response_model=None,
)
async def execute_slack_webhook(
    webhook_id: Snowflake,
    path_token: str,
    payload: SlackWebhookExecute,
    request: Request,
    wait: bool = Query(default=True),
    thread_id: Annotated[EntityRef | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object] | Response:
    return await execute_webhook(
        webhook_id=webhook_id,
        payload=WebhookExecute(
            content=payload.text,
            embeds=slack_embeds(payload),
            username=payload.username,
            avatar_url=payload.icon_url,
        ),
        request=request,
        wait=wait,
        thread_id=thread_id,
        with_components=False,
        idempotency_key=None,
        path_token=path_token,
        authorization=None,
        e2ee_device_id=None,
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
    )


@router.post(
    "/api/v1/webhooks/{webhook_id}/{path_token}/github",
    response_model=None,
)
async def execute_github_webhook(
    webhook_id: Snowflake,
    path_token: str,
    payload: dict[str, Any],
    request: Request,
    wait: bool = Query(default=True),
    thread_id: Annotated[EntityRef | None, Query()] = None,
    github_event: str = Header(alias="X-GitHub-Event", min_length=1, max_length=64),
    github_delivery: str | None = Header(
        default=None,
        alias="X-GitHub-Delivery",
        min_length=1,
        max_length=128,
    ),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object] | Response:
    return await execute_webhook(
        webhook_id=webhook_id,
        payload=WebhookExecute(embeds=[github_webhook_embed(github_event.casefold(), payload)]),
        request=request,
        wait=wait,
        thread_id=thread_id,
        with_components=False,
        idempotency_key=github_delivery,
        path_token=path_token,
        authorization=None,
        e2ee_device_id=None,
        session=session,
        redis=redis,
        snowflake=snowflake,
        settings=settings,
    )


async def token_webhook_message(
    session: AsyncSession,
    webhook: Webhook,
    message_id: EntityRef,
    settings: Settings,
    *,
    for_update: bool = False,
) -> Message:
    message_ref = message_id.resolve(settings.domain)
    statement = select(Message).where(
        Message.id == message_ref[0],
        Message.origin_domain == message_ref[1],
        Message.webhook_id == webhook.id,
        Message.webhook_domain == webhook.guild_domain,
    )
    if for_update:
        statement = statement.with_for_update()
    message = await session.scalar(statement)
    if message is None or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_MESSAGE_NOT_FOUND"})
    return message


def require_webhook_message_thread(
    message: Message,
    thread_id: EntityRef | None,
    settings: Settings,
) -> None:
    if thread_id is not None and thread_id.resolve(settings.domain) != (
        message.channel_id,
        message.channel_domain,
    ):
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_MESSAGE_NOT_FOUND"})


@router.get("/api/v1/webhooks/{webhook_id}/{path_token}/messages/{message_id}")
async def get_webhook_message(
    webhook_id: Snowflake,
    path_token: str,
    message_id: EntityRef,
    thread_id: Annotated[EntityRef | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token)
    message = await token_webhook_message(session, webhook, message_id, settings)
    require_webhook_message_thread(message, thread_id, settings)
    return await render_message_payload(session, message)


@router.patch("/api/v1/webhooks/{webhook_id}/{path_token}/messages/{message_id}")
async def edit_webhook_message(
    webhook_id: Snowflake,
    path_token: str,
    message_id: EntityRef,
    payload: WebhookMessageEdit,
    thread_id: Annotated[EntityRef | None, Query()] = None,
    with_components: bool = Query(default=False),
    e2ee_device_id: str | None = Header(default=None, alias="X-Kaede-E2EE-Device"),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
) -> dict[str, object]:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    message = await token_webhook_message(session, webhook, message_id, settings, for_update=True)
    require_webhook_message_thread(message, thread_id, settings)
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id), for_update=True)
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None or channel.unavailable:
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "WEBHOOK_CREATOR_MISSING",
                "message": "The webhook owner no longer exists, so its message cannot be edited.",
            },
        )
    await require_editable_message(session, message)
    if not payload.model_fields_set:
        return await render_message_payload(session, message)
    prospective_e2ee = payload.e2ee if "e2ee" in payload.model_fields_set else message.e2ee
    if prospective_e2ee is not None:
        application_ref = (
            (webhook.application_id, webhook.application_domain)
            if webhook.type == 3
            and webhook.application_id is not None
            and webhook.application_domain is not None
            else None
        )
        _contract, encrypted_controls, _poll = encrypted_rich_routing(prospective_e2ee)
        if encrypted_controls and application_ref is None:
            raise HTTPException(
                status_code=400,
                detail={"code": "WEBHOOK_COMPONENT_APPLICATION_REQUIRED"},
            )
        shared_edit = MessageEdit.model_validate(
            payload.model_dump(
                mode="json",
                exclude_unset=True,
                exclude={"allowed_mentions"},
            )
        )
        return await edit_message(
            EntityRef(f"{channel.id}@{channel.origin_domain}"),
            EntityRef(f"{message.id}@{message.origin_domain}"),
            shared_edit,
            cast(AuthenticatedUser, SimpleNamespace(user=creator)),
            session,
            redis,
            settings,
            snowflake,
            MessageMutationOptions(
                application_id=(application_ref[0] if application_ref is not None else None),
                application_domain=(application_ref[1] if application_ref is not None else None),
                webhook_id=webhook.id,
                webhook_channel_id=webhook.channel_id,
                webhook_channel_domain=webhook.channel_domain,
                webhook_e2ee_device_id=e2ee_device_id,
                required_attachment_binding_prefix=webhook_attachment_binding_prefix(webhook.id),
                required_attachment_purpose="webhook_attachment",
                allow_render_only_components=application_ref is None,
            ),
        )
    if e2ee_device_id is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "MESSAGE_ENCRYPTION_POLICY_INVALID"},
        )
    prospective_content = (
        payload.content if "content" in payload.model_fields_set else message.content
    )
    prospective_embeds = (
        list(payload.embeds or [])
        if "embeds" in payload.model_fields_set
        else [Embed.model_validate(item) for item in (message.embeds or [])]
    )
    stored_components = [
        MESSAGE_LAYOUT_COMPONENT_ADAPTER.validate_python(item)
        for item in (message.components or [])
    ]
    components_changed = with_components and "components" in payload.model_fields_set
    prospective_components = (
        list(payload.components or []) if components_changed else stored_components
    )
    current_attachment_refs = set(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain).where(
                    Attachment.message_id == message.id,
                    Attachment.message_domain == message.origin_domain,
                    Attachment.deleted_at.is_(None),
                )
            )
        ).tuples()
    )
    prospective_attachment_ids = (
        {int(item) for item in (payload.attachment_ids or [])}
        if "attachment_ids" in payload.model_fields_set
        else {item[0] for item in current_attachment_refs}
    )
    if "attachment_ids" in payload.model_fields_set:
        for attachment_id, attachment_domain in sorted(
            current_attachment_refs
            | {(attachment_id, settings.domain) for attachment_id in prospective_attachment_ids},
            key=lambda item: (item[1], item[0]),
        ):
            await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    current_attachments = list(
        await session.scalars(
            select(Attachment)
            .where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
            .with_for_update()
        )
    )
    try:
        validate_message_encryption_policy(
            channel.encryption_mode,
            content=prospective_content,
            e2ee=None,
            policy_generation=channel.encryption_policy_generation,
            policy_epoch=channel.encryption_epoch,
            policy_group_id=channel.encryption_group_id,
        )
    except MessageEncryptionPolicyError as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code}) from exc
    application_ref = (
        (webhook.application_id, webhook.application_domain)
        if webhook.type == 3
        and webhook.application_id is not None
        and webhook.application_domain is not None
        else None
    )
    if (
        components_changed
        and has_interactive_components(prospective_components)
        and application_ref is None
    ):
        raise HTTPException(
            status_code=400,
            detail={"code": "WEBHOOK_COMPONENT_APPLICATION_REQUIRED"},
        )
    if (
        application_ref is not None
        and (
            message.application_id,
            message.application_domain,
        )
        != application_ref
    ):
        raise HTTPException(status_code=409, detail={"code": "WEBHOOK_APPLICATION_MISMATCH"})
    requested_flags = (
        int(payload.flags or 0)
        if "flags" in payload.model_fields_set
        else message.flags & PUBLIC_MESSAGE_EDIT_FLAGS
    )
    if message.flags & MESSAGE_FLAG_IS_COMPONENTS_V2:
        requested_flags |= MESSAGE_FLAG_IS_COMPONENTS_V2
    validate_merged_message_edit(
        content=prospective_content,
        e2ee=None,
        embeds=prospective_embeds,
        components=prospective_components,
        attachment_count=len(prospective_attachment_ids),
        sticker_items=list(message.sticker_items or []),
        forward_snapshot=None,
        current_flags=int(message.flags or 0),
        requested_flags=requested_flags,
    )
    resolved_mentions: ResolvedMentions | None = None
    mention_refs: list[dict[str, str]] | None = None
    if (
        "allowed_mentions" in payload.model_fields_set
        or "content" in payload.model_fields_set
        or components_changed
    ):
        resolved_mentions = await resolved_webhook_mentions(
            session,
            redis,
            settings,
            guild,
            channel,
            creator,
            payload.allowed_mentions,
            prospective_content,
            prospective_components,
        )
    destinations = await remote_destinations_with_channel_access(session, settings, guild, channel)
    if "content" in payload.model_fields_set:
        message.content = payload.content
    if "embeds" in payload.model_fields_set:
        message.embeds = [
            item.model_dump(mode="json", exclude_none=True) for item in prospective_embeds
        ]
    if components_changed:
        message.components = [
            item.model_dump(mode="json", exclude_none=True) for item in prospective_components
        ]
    message.flags = (message.flags & ~PUBLIC_MESSAGE_EDIT_FLAGS) | requested_flags
    if resolved_mentions is not None:
        mention_refs = [
            {"id": str(user_id), "origin_domain": domain}
            for user_id, domain in resolved_mentions.recipients
        ]
        message.mention_user_refs = mention_refs
        message.mention_role_refs = [
            {"id": str(role_id), "origin_domain": domain}
            for role_id, domain in resolved_mentions.roles
        ]
        message.mention_everyone = resolved_mentions.everyone
        projection = await session.get(
            MessageProjection,
            (message.id, message.origin_domain),
            with_for_update=True,
        )
        if projection is None:
            projection = MessageProjection(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=message.channel_id,
                channel_domain=message.channel_domain,
                mention_user_refs=mention_refs,
            )
            session.add(projection)
        else:
            projection.mention_user_refs = mention_refs
    added_attachments: list[Attachment] = []
    removed_attachments: list[Attachment] = []
    if "attachment_ids" in payload.model_fields_set:
        current_by_id = {item.id: item for item in current_attachments}
        for attachment_id in sorted(prospective_attachment_ids):
            if attachment_id in current_by_id:
                continue
            attachment = await finalize_attachment(
                session,
                settings,
                creator,
                attachment_id,
                required_purpose="webhook_attachment",
                federated_guild_upload=is_federated_human_authority_upload(creator, settings),
            )
            if (
                attachment.message_id is not None
                or attachment.message_domain is not None
                or attachment.interaction_id is not None
                or attachment.interaction_response_id is not None
                or attachment.asset_binding
                != f"{webhook_attachment_binding_prefix(webhook.id)}{attachment.id}"
            ):
                raise HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
            attachment.asset_binding = None
            attachment.message_id = message.id
            attachment.message_domain = message.origin_domain
            added_attachments.append(attachment)
        for attachment in current_attachments:
            if attachment.id not in prospective_attachment_ids:
                await discard_attachment(session, settings, attachment)
                removed_attachments.append(attachment)
    effective_attachments = [
        item for item in current_attachments if item not in removed_attachments
    ] + added_attachments
    try:
        validate_attachment_url_references(
            embeds=prospective_embeds,
            components=prospective_components,
            attachments=effective_attachments,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "ATTACHMENT_REFERENCE_INVALID"},
        ) from exc
    message.edited_at = datetime.now(UTC)
    rendered = await render_message_payload(session, message)
    if mention_refs is not None:
        rendered["mention_user_refs"] = mention_refs
    if destinations:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            creator,
            "guild.message.update",
            {"message": rendered},
            channel=channel,
        )
    await session.commit()
    if destinations:
        await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "MESSAGE_UPDATE", rendered
    )
    for attachment in added_attachments:
        await enqueue_best_effort(media_process, attachment.id, attachment.origin_domain)
    for attachment in removed_attachments:
        await enqueue_best_effort(media_local_purge, attachment.id, attachment.origin_domain)
    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)
    return rendered


@router.delete(
    "/api/v1/webhooks/{webhook_id}/{path_token}/messages/{message_id}",
    status_code=204,
)
async def delete_webhook_message(
    webhook_id: Snowflake,
    path_token: str,
    message_id: EntityRef,
    thread_id: Annotated[EntityRef | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    webhook = await token_webhook(session, int(webhook_id), path_token, for_update=True)
    preview = await token_webhook_message(session, webhook, message_id, settings)
    require_webhook_message_thread(preview, thread_id, settings)
    guild = await local_guild(session, settings, EntityReference(webhook.guild_id))
    channel = await session.get(Channel, (preview.channel_id, preview.channel_domain))
    if (
        channel is None
        or channel.unavailable
        or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
    ):
        raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
    access = await lock_message_delete_access(
        session,
        settings,
        ChannelAccess(channel=channel, guild=guild, participants=[]),
    )
    message = await lock_message_delete_target(session, settings, access, message_id)
    if (message.webhook_id, message.webhook_domain) != (
        webhook.id,
        webhook.guild_domain,
    ) or message.deleted_at is not None:
        raise HTTPException(status_code=404, detail={"code": "WEBHOOK_MESSAGE_NOT_FOUND"})
    creator = await session.get(User, (webhook.creator_id, webhook.creator_domain))
    if creator is None:
        raise HTTPException(
            status_code=410,
            detail={
                "code": "WEBHOOK_CREATOR_MISSING",
                "message": "The webhook owner no longer exists, so its message cannot be deleted.",
            },
        )
    await commit_local_message_deletion(
        session,
        redis,
        settings,
        access,
        creator,
        message,
    )
    return Response(status_code=204)
