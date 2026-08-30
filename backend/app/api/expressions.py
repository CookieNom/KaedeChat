from __future__ import annotations

import time
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import installation_for_guild_any_scope
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guild_feature_access import proxy_human_guild_feature
from app.api.guilds import local_guild
from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import usable_guild_installation
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.custom_emojis import custom_emoji_refs, validate_custom_emoji_tokens
from app.chat.custom_stickers import resolve_sticker_items
from app.chat.events import guild_topic, publish_dispatch
from app.chat.expression_authorization import (
    EXPRESSION_USE_AUTHORIZATION_EVENT,
    ExpressionOperation,
    build_expression_use_authorization,
    canonical_expression_emoji_tokens,
    expression_actor_intent_resources,
)
from app.chat.expression_events import (
    publish_guild_emojis_update,
    publish_guild_stickers_update,
)
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.hierarchy import guild_role
from app.chat.payloads import emoji_payload, sticker_payload
from app.chat.permissions import require_can_manage_expression, require_permissions
from app.chat.schemas import RequestModel
from app.core.federation import canonical_json
from app.core.permission_contract import required_permissions
from app.core.permissions import ALL_PERMISSIONS
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, BotInstallation
from app.db.materialization import materialize_updated_at
from app.db.models import Emoji, EmojiRoleRestriction, Guild, GuildMember, Role, Sticker, User
from app.federation.actor_intents import (
    consume_actor_intent_nonce,
    validate_human_actor_intent,
    validate_worker_actor_intent,
)
from app.federation.events import build_envelope
from app.federation.replication import upsert_remote_user
from app.federation.schemas import FederationDomain, RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.media.schemas import clean_sticker_description, clean_sticker_name, clean_sticker_tags

router = APIRouter(prefix="/api/v1", tags=["guild expressions"])
federation_router = APIRouter(tags=["expression federation"])


class ExpressionUseAuthorizeRequest(RequestModel):
    actor: RemoteUserProfile
    application_ref: EntityRef | None = None
    actor_intent: dict[str, object] | None = None
    source_authority: FederationDomain
    target_guild_ref: EntityRef
    target_channel_ref: EntityRef
    target_message_ref: EntityRef | None = None
    operation: ExpressionOperation
    operation_id: str = Field(min_length=1, max_length=128)
    emoji_tokens: list[str] = Field(default_factory=list, max_length=256)
    sticker_refs: list[EntityRef] = Field(default_factory=list, max_length=9)
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("emoji_tokens")
    @classmethod
    def canonical_emojis(cls, value: list[str]) -> list[str]:
        return canonical_expression_emoji_tokens(value)

    @model_validator(mode="after")
    def exact_authorities(self) -> ExpressionUseAuthorizeRequest:
        sticker_refs = [str(item) for item in self.sticker_refs]
        if sticker_refs != sorted(set(sticker_refs)):
            raise ValueError("expression sticker refs must be sorted and unique")
        if any(item.domain != self.source_authority for item in self.sticker_refs):
            raise ValueError("expression sticker authority is inconsistent")
        if any(
            reference.origin_domain != self.source_authority
            for token in self.emoji_tokens
            for reference in custom_emoji_refs(token)
        ):
            raise ValueError("expression emoji authority is inconsistent")
        if not self.emoji_tokens and not self.sticker_refs:
            raise ValueError("expression authorization requires an expression")
        if self.target_guild_ref.domain is None or self.target_channel_ref.domain is None:
            raise ValueError("expression targets must be qualified")
        if self.target_guild_ref.domain != self.target_channel_ref.domain:
            raise ValueError("expression target authorities conflict")
        if self.operation == "message.create":
            if self.target_message_ref is not None:
                raise ValueError("message creation cannot target an existing message")
        elif (
            self.target_message_ref is None
            or self.target_message_ref.domain != self.target_channel_ref.domain
        ):
            raise ValueError("expression operation target message is invalid")
        bot = self.actor.account_type == "bot"
        if bot != (self.application_ref is not None and self.actor_intent is not None):
            raise ValueError("expression bot lineage is incomplete")
        return self


async def _require_bot_expression_source_installations(
    session: AsyncSession,
    application: BotApplication,
    actor: User,
    guild_refs: set[tuple[int, str]],
) -> None:
    for guild_id, guild_domain in sorted(guild_refs, key=lambda item: (item[1], item[0])):
        installation = await session.scalar(
            select(BotInstallation).where(
                BotInstallation.application_id == application.id,
                BotInstallation.application_domain == application.origin_domain,
                BotInstallation.bot_user_id == actor.id,
                BotInstallation.bot_user_domain == actor.origin_domain,
                BotInstallation.guild_id == guild_id,
                BotInstallation.guild_domain == guild_domain,
                usable_guild_installation(),
            )
        )
        if installation is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})


async def issue_expression_use_authorization(
    payload: ExpressionUseAuthorizeRequest,
    actor: User,
    application: BotApplication | None,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> dict[str, object]:
    """Recheck source state and mint one exact short-lived S receipt."""

    requester_ref = (actor.id, actor.origin_domain)
    request_fingerprint = canonical_json(payload.model_dump(mode="json"))
    try:
        await consume_actor_intent_nonce(
            redis,
            authority_domain=settings.domain,
            intent_kind="expression-request",
            action=payload.operation,
            actor_ref=requester_ref,
            audience=cast(str, payload.target_channel_ref.domain),
            nonce=payload.nonce,
            expires_at=int(time.time()) + 90,
            fingerprint=request_fingerprint,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "EXPRESSION_AUTHORIZATION_REPLAYED"},
        ) from exc

    await validate_custom_emoji_tokens(
        session,
        actor,
        payload.emoji_tokens,
        target_guild=None,
        target_permissions=ALL_PERMISSIONS,
    )
    sticker_items = await resolve_sticker_items(
        session,
        actor,
        payload.sticker_refs,
        default_domain=settings.domain,
        target_guild=None,
        target_permissions=ALL_PERMISSIONS,
        maximum=9,
    )
    sticker_items.sort(key=lambda item: f"{item['id']}@{item['origin_domain']}")
    source_guild_refs: set[tuple[int, str]] = set()
    for token in payload.emoji_tokens:
        for reference in custom_emoji_refs(token):
            emoji = await session.get(Emoji, (reference.id, reference.origin_domain))
            if emoji is not None:
                source_guild_refs.add((emoji.guild_id, emoji.guild_domain))
    for sticker_reference in payload.sticker_refs:
        sticker = await session.get(Sticker, sticker_reference.resolve(settings.domain))
        if sticker is None:
            raise HTTPException(status_code=400, detail={"code": "CUSTOM_STICKER_NOT_FOUND"})
        source_guild_refs.add((sticker.guild_id, sticker.guild_domain))
    if application is not None:
        await _require_bot_expression_source_installations(
            session,
            application,
            actor,
            source_guild_refs,
        )
    content = build_expression_use_authorization(
        source_authority=settings.domain,
        requester_ref=f"{actor.id}@{actor.origin_domain}",
        requester_type=cast(Literal["human", "bot"], actor.account_type),
        application_ref=(
            f"{application.id}@{application.origin_domain}" if application is not None else None
        ),
        target_guild_ref=str(payload.target_guild_ref),
        target_channel_ref=str(payload.target_channel_ref),
        target_message_ref=(
            str(payload.target_message_ref) if payload.target_message_ref is not None else None
        ),
        operation=payload.operation,
        operation_id=payload.operation_id,
        emoji_tokens=payload.emoji_tokens,
        sticker_items=sticker_items,
        nonce=payload.nonce,
    )
    return await build_envelope(
        session,
        settings,
        EXPRESSION_USE_AUTHORIZATION_EVENT,
        actor,
        content,
        context={
            "source_authority": settings.domain,
            "target_channel_ref": str(payload.target_channel_ref),
        },
        authority_attested_actor=actor.origin_domain != settings.domain,
    )


@federation_router.post("/_kaede/v1/expressions/authorize")
async def federation_authorize_expression_use(
    payload: ExpressionUseAuthorizeRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Recheck S-owned expressions before issuing one exact S-signed receipt."""

    require_guild_federation_access(principal)
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "expression-use-authorize",
        capacity=1_200,
        refill_per_minute=1_200,
    )
    if payload.source_authority != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "EXPRESSION_NOT_FOUND"})
    requester_ref = (int(payload.actor.id), payload.actor.origin_domain)
    if principal.origin not in {
        payload.actor.origin_domain,
        cast(str, payload.target_channel_ref.domain),
    }:
        raise HTTPException(
            status_code=403,
            detail={"code": "EXPRESSION_AUTHORIZATION_RELAY_INVALID"},
        )
    application: BotApplication | None = None
    if payload.actor.account_type == "human":
        if payload.actor_intent is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "USER_ACTOR_INTENT_INVALID"},
            )
        try:
            await validate_human_actor_intent(
                session,
                settings,
                payload.actor_intent,
                expected_action="expression.use.authorize",
                expected_audience=settings.domain,
                expected_actor_ref=requester_ref,
                expected_resources=expression_actor_intent_resources(
                    source_authority=settings.domain,
                    target_guild_ref=str(payload.target_guild_ref),
                    target_channel_ref=str(payload.target_channel_ref),
                    target_message_ref=(
                        str(payload.target_message_ref)
                        if payload.target_message_ref is not None
                        else None
                    ),
                    operation=payload.operation,
                    operation_id=payload.operation_id,
                    emoji_tokens=payload.emoji_tokens,
                    sticker_refs=[str(item) for item in payload.sticker_refs],
                    authorization_nonce=payload.nonce,
                ),
                redis=redis,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "USER_ACTOR_INTENT_INVALID"},
            ) from exc
        actor = await upsert_remote_user(session, settings, payload.actor)
        if actor.account_type != "human":
            raise HTTPException(status_code=403, detail={"code": "USER_NOT_FOUND"})
        await require_remote_user_creation_allowed(session, actor)
    else:
        if payload.application_ref is None or payload.actor_intent is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_ACTOR_INTENT_INVALID"})
        application_ref = payload.application_ref.resolve(settings.domain)
        bot_actor = await session.get(User, requester_ref)
        application = await session.get(BotApplication, application_ref)
        if (
            bot_actor is None
            or bot_actor.account_type != "bot"
            or application is None
            or application.status != "active"
            or (application.bot_user_id, application.bot_user_domain) != requester_ref
        ):
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        actor = bot_actor
        try:
            await validate_worker_actor_intent(
                session,
                settings.domain,
                payload.actor_intent,
                expected_action="expression.use.authorize",
                expected_audience=settings.domain,
                expected_application_ref=application_ref,
                expected_actor_ref=requester_ref,
                expected_resources=expression_actor_intent_resources(
                    source_authority=settings.domain,
                    target_guild_ref=str(payload.target_guild_ref),
                    target_channel_ref=str(payload.target_channel_ref),
                    target_message_ref=(
                        str(payload.target_message_ref)
                        if payload.target_message_ref is not None
                        else None
                    ),
                    operation=payload.operation,
                    operation_id=payload.operation_id,
                    emoji_tokens=payload.emoji_tokens,
                    sticker_refs=[str(item) for item in payload.sticker_refs],
                    authorization_nonce=payload.nonce,
                ),
                runtime_target_domain=cast(str, payload.target_channel_ref.domain),
                redis=redis,
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_ACTOR_INTENT_INVALID"},
            ) from exc

    return await issue_expression_use_authorization(
        payload,
        actor,
        application,
        session,
        redis,
        settings,
    )


class EmojiUpdate(RequestModel):
    name: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]{2,32}$")
    role_ids: list[EntityRef] | None = Field(default=None, max_length=100)

    @field_validator("role_ids")
    @classmethod
    def unique_roles(cls, value: list[EntityRef] | None) -> list[EntityRef] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("emoji role restrictions must be unique")
        return value

    @model_validator(mode="after")
    def not_empty(self) -> EmojiUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one emoji field is required")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("emoji name cannot be null")
        return self


class StickerUpdate(RequestModel):
    name: str | None = Field(default=None, min_length=2, max_length=30)
    description: str | None = Field(default=None, max_length=100)
    tags: list[str] | None = Field(default=None, min_length=1, max_length=10)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return clean_sticker_name(value) if value is not None else None

    @field_validator("description")
    @classmethod
    def clean_description(cls, value: str | None) -> str | None:
        return clean_sticker_description(value)

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return clean_sticker_tags(value)

    @model_validator(mode="after")
    def not_empty(self) -> StickerUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one sticker field is required")
        for field_name in self.model_fields_set & {"name", "tags"}:
            if getattr(self, field_name) is None:
                raise ValueError(f"sticker {field_name} cannot be null")
        return self


async def _authorize_human(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    auth: AuthenticatedUser,
    *,
    manage: bool,
) -> Guild:
    guild = await local_guild(session, settings, guild_ref, for_update=manage)
    if manage:
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("guild.expression.manage"),
        )
    elif (
        await session.get(
            GuildMember,
            (guild.id, guild.origin_domain, auth.user.id, auth.user.origin_domain),
        )
        is None
    ):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


async def _authorize_bot(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild_ref: EntityRef,
    principal: BotPrincipal,
    *,
    manage: bool,
) -> Guild:
    guild, _ = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "expressions.manage" if manage else "expressions.read",
        "emojis.manage" if manage else "guilds.read",
    )
    if manage:
        await require_permissions(
            session,
            redis,
            guild,
            principal.user,
            required_permissions("guild.expression.manage"),
        )
    return guild


async def _editable_human_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
) -> Guild:
    # The target expression determines whether CREATE or MANAGE is required,
    # so load and lock the guild before applying that creator-aware check.
    return await local_guild(session, settings, guild_ref, for_update=True)


async def _editable_bot_guild(
    session: AsyncSession,
    settings: Settings,
    guild_ref: EntityRef,
    principal: BotPrincipal,
) -> Guild:
    guild, _ = await installation_for_guild_any_scope(
        session,
        settings,
        principal,
        guild_ref,
        "expressions.manage",
        "emojis.manage",
    )
    return guild


async def _emoji_roles(session: AsyncSession, emoji: Emoji) -> list[str]:
    rows = (
        await session.execute(
            select(EmojiRoleRestriction.role_id, EmojiRoleRestriction.role_domain)
            .where(
                EmojiRoleRestriction.emoji_id == emoji.id,
                EmojiRoleRestriction.emoji_domain == emoji.origin_domain,
            )
            .order_by(EmojiRoleRestriction.role_domain, EmojiRoleRestriction.role_id)
        )
    ).tuples()
    return [f"{role_id}@{role_domain}" for role_id, role_domain in rows]


async def _get_emoji(session: AsyncSession, guild: Guild, emoji_id: int) -> Emoji:
    emoji = await session.get(Emoji, (emoji_id, guild.origin_domain))
    if emoji is None or (emoji.guild_id, emoji.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "EMOJI_NOT_FOUND", "message": "Guild emoji not found."},
        )
    return emoji


async def _get_sticker(session: AsyncSession, guild: Guild, sticker_id: int) -> Sticker:
    sticker = await session.get(Sticker, (sticker_id, guild.origin_domain))
    if sticker is None or (sticker.guild_id, sticker.guild_domain) != (
        guild.id,
        guild.origin_domain,
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "STICKER_NOT_FOUND", "message": "Guild sticker not found."},
        )
    return sticker


async def _validate_roles(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    role_refs: list[EntityRef],
) -> list[Role]:
    roles: list[Role] = []
    resolved_refs = [role_ref.resolve(settings.domain) for role_ref in role_refs]
    if len(resolved_refs) != len(set(resolved_refs)):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EMOJI_ROLE_DUPLICATE",
                "message": "Choose each emoji restriction role only once.",
            },
        )
    for role_id, role_domain in resolved_refs:
        if role_domain != guild.origin_domain or role_id == guild.id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "EMOJI_ROLE_INVALID",
                    "message": (
                        "Emoji restrictions must reference non-everyone roles in this guild."
                    ),
                },
            )
        role = await guild_role(session, guild, role_id)
        roles.append(role)
    return roles


async def _list_emojis(session: AsyncSession, guild: Guild) -> list[dict[str, object]]:
    emojis = list(
        await session.scalars(
            select(Emoji)
            .where(Emoji.guild_id == guild.id, Emoji.guild_domain == guild.origin_domain)
            .order_by(func.lower(Emoji.name), Emoji.id)
        )
    )
    return [emoji_payload(item, await _emoji_roles(session, item)) for item in emojis]


async def _list_stickers(session: AsyncSession, guild: Guild) -> list[dict[str, object]]:
    stickers = list(
        await session.scalars(
            select(Sticker)
            .where(Sticker.guild_id == guild.id, Sticker.guild_domain == guild.origin_domain)
            .order_by(func.lower(Sticker.name), Sticker.id)
        )
    )
    return [sticker_payload(item) for item in stickers]


async def _patch_emoji(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    actor: User,
    emoji_id: int,
    payload: EmojiUpdate,
    *,
    reason: str | None,
) -> dict[str, object]:
    reason = normalize_audit_reason(reason)
    emoji = await _get_emoji(session, guild, emoji_id)
    changes: list[dict[str, object]] = []
    if payload.name is not None:
        duplicate = await session.scalar(
            select(Emoji.id).where(
                Emoji.guild_id == guild.id,
                Emoji.guild_domain == guild.origin_domain,
                func.lower(Emoji.name) == payload.name.casefold(),
                Emoji.id != emoji.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "EMOJI_NAME_TAKEN", "message": "That emoji name is already used."},
            )
        if payload.name != emoji.name:
            changes.append({"key": "name", "old_value": emoji.name, "new_value": payload.name})
            emoji.name = payload.name
    if "role_ids" in payload.model_fields_set:
        current_role_refs = await _emoji_roles(session, emoji)
        roles = await _validate_roles(session, settings, guild, payload.role_ids or [])
        requested_role_refs = [f"{role.id}@{role.origin_domain}" for role in roles]
        if set(current_role_refs) != set(requested_role_refs):
            changes.append(
                {
                    "key": "roles",
                    "old_value": current_role_refs,
                    "new_value": requested_role_refs,
                }
            )
            await session.execute(
                delete(EmojiRoleRestriction).where(
                    EmojiRoleRestriction.emoji_id == emoji.id,
                    EmojiRoleRestriction.emoji_domain == emoji.origin_domain,
                )
            )
            for role in roles:
                session.add(
                    EmojiRoleRestriction(
                        emoji_id=emoji.id,
                        emoji_domain=emoji.origin_domain,
                        role_id=role.id,
                        role_domain=role.origin_domain,
                        guild_id=guild.id,
                        guild_domain=guild.origin_domain,
                    )
                )
    await materialize_updated_at(session, emoji)
    rendered = emoji_payload(emoji, await _emoji_roles(session, emoji))
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.emoji.update",
        {"emoji": rendered},
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        61,
        target_type="emoji",
        target_ref={
            "id": str(emoji.id),
            "origin_domain": emoji.origin_domain,
            "name": emoji.name,
        },
        reason=reason,
        changes=changes or [{"key": "configuration", "new_value": "unchanged"}],
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    return rendered


async def _patch_sticker(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    actor: User,
    sticker_id: int,
    payload: StickerUpdate,
    *,
    reason: str | None,
) -> dict[str, object]:
    reason = normalize_audit_reason(reason)
    sticker = await _get_sticker(session, guild, sticker_id)
    changes: list[dict[str, object]] = []
    if payload.name is not None:
        duplicate = await session.scalar(
            select(Sticker.id).where(
                Sticker.guild_id == guild.id,
                Sticker.guild_domain == guild.origin_domain,
                func.lower(Sticker.name) == payload.name.casefold(),
                Sticker.id != sticker.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "STICKER_NAME_TAKEN",
                    "message": "That sticker name is already used.",
                },
            )
        if payload.name != sticker.name:
            changes.append({"key": "name", "old_value": sticker.name, "new_value": payload.name})
            sticker.name = payload.name
    if "description" in payload.model_fields_set and payload.description != sticker.description:
        changes.append(
            {
                "key": "description",
                "old_value": sticker.description,
                "new_value": payload.description,
            }
        )
        sticker.description = payload.description
    if payload.tags is not None and payload.tags != sticker.tags:
        changes.append({"key": "tags", "old_value": sticker.tags, "new_value": payload.tags})
        sticker.tags = payload.tags
    await materialize_updated_at(session, sticker)
    rendered = sticker_payload(sticker)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        actor,
        "guild.sticker.update",
        {"sticker": rendered},
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        actor,
        91,
        target_type="sticker",
        target_ref={
            "id": str(sticker.id),
            "origin_domain": sticker.origin_domain,
            "name": sticker.name,
        },
        reason=reason,
        changes=changes or [{"key": "configuration", "new_value": "unchanged"}],
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    return rendered


@router.get("/guilds/{guild_ref}/emojis")
async def list_guild_emojis(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    proxied, body = await proxy_human_guild_feature(
        session, settings, guild_ref, auth.user, "emoji.list"
    )
    if proxied:
        return cast(list[dict[str, object]], body)
    guild = await _authorize_human(session, redis, settings, guild_ref, auth, manage=False)
    return await _list_emojis(session, guild)


@router.get("/guilds/{guild_ref}/emojis/{emoji_id}")
async def get_guild_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session, settings, guild_ref, auth.user, "emoji.get", {"resource_id": emoji_id}
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await _authorize_human(session, redis, settings, guild_ref, auth, manage=False)
    emoji = await _get_emoji(session, guild, emoji_id)
    return emoji_payload(emoji, await _emoji_roles(session, emoji))


@router.patch("/guilds/{guild_ref}/emojis/{emoji_id}")
async def patch_guild_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    payload: EmojiUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "emoji.update",
        {
            "resource_id": emoji_id,
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await _editable_human_guild(session, settings, guild_ref)
    emoji = await _get_emoji(session, guild, emoji_id)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        auth.user,
        creator_id=emoji.creator_id,
        creator_domain=emoji.creator_domain,
    )
    rendered = await _patch_emoji(
        session,
        snowflake,
        settings,
        guild,
        auth.user,
        emoji_id,
        payload,
        reason=reason,
    )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_EMOJI_UPDATE", rendered
    )
    await publish_guild_emojis_update(session, redis, guild)
    return rendered


@router.get("/guilds/{guild_ref}/stickers")
async def list_guild_stickers(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    proxied, body = await proxy_human_guild_feature(
        session, settings, guild_ref, auth.user, "sticker.list"
    )
    if proxied:
        return cast(list[dict[str, object]], body)
    guild = await _authorize_human(session, redis, settings, guild_ref, auth, manage=False)
    return await _list_stickers(session, guild)


@router.get("/guilds/{guild_ref}/stickers/{sticker_id}")
async def get_guild_sticker(
    guild_ref: EntityRef,
    sticker_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "sticker.get",
        {"resource_id": sticker_id},
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await _authorize_human(session, redis, settings, guild_ref, auth, manage=False)
    return sticker_payload(await _get_sticker(session, guild, sticker_id))


@router.patch("/guilds/{guild_ref}/stickers/{sticker_id}")
async def patch_guild_sticker(
    guild_ref: EntityRef,
    sticker_id: int,
    payload: StickerUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "sticker.update",
        {
            "resource_id": sticker_id,
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await _editable_human_guild(session, settings, guild_ref)
    sticker = await _get_sticker(session, guild, sticker_id)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        auth.user,
        creator_id=sticker.creator_id,
        creator_domain=sticker.creator_domain,
    )
    rendered = await _patch_sticker(
        session,
        snowflake,
        settings,
        guild,
        auth.user,
        sticker_id,
        payload,
        reason=reason,
    )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_STICKER_UPDATE", rendered
    )
    await publish_guild_stickers_update(session, redis, guild)
    return rendered


@router.get("/bots/guilds/{guild_ref}/emojis/{emoji_id}")
async def bot_get_guild_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await _authorize_bot(session, redis, settings, guild_ref, principal, manage=False)
    emoji = await _get_emoji(session, guild, emoji_id)
    return emoji_payload(emoji, await _emoji_roles(session, emoji))


@router.patch("/bots/guilds/{guild_ref}/emojis/{emoji_id}")
async def bot_patch_guild_emoji(
    guild_ref: EntityRef,
    emoji_id: int,
    payload: EmojiUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    guild = await _editable_bot_guild(session, settings, guild_ref, principal)
    emoji = await _get_emoji(session, guild, emoji_id)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        principal.user,
        creator_id=emoji.creator_id,
        creator_domain=emoji.creator_domain,
    )
    rendered = await _patch_emoji(
        session,
        snowflake,
        settings,
        guild,
        principal.user,
        emoji_id,
        payload,
        reason=reason,
    )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_EMOJI_UPDATE", rendered
    )
    await publish_guild_emojis_update(session, redis, guild)
    return rendered


@router.get("/bots/guilds/{guild_ref}/stickers/{sticker_id}")
async def bot_get_guild_sticker(
    guild_ref: EntityRef,
    sticker_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild = await _authorize_bot(session, redis, settings, guild_ref, principal, manage=False)
    return sticker_payload(await _get_sticker(session, guild, sticker_id))


@router.patch("/bots/guilds/{guild_ref}/stickers/{sticker_id}")
async def bot_patch_guild_sticker(
    guild_ref: EntityRef,
    sticker_id: int,
    payload: StickerUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    guild = await _editable_bot_guild(session, settings, guild_ref, principal)
    sticker = await _get_sticker(session, guild, sticker_id)
    await require_can_manage_expression(
        session,
        redis,
        guild,
        principal.user,
        creator_id=sticker.creator_id,
        creator_domain=sticker.creator_domain,
    )
    rendered = await _patch_sticker(
        session,
        snowflake,
        settings,
        guild,
        principal.user,
        sticker_id,
        payload,
        reason=reason,
    )
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_STICKER_UPDATE", rendered
    )
    await publish_guild_stickers_update(session, redis, guild)
    return rendered
