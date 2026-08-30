from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guild_feature_access import (
    authorize_bot_guild_feature_grant,
    authorize_human_guild_feature,
    proxy_human_guild_feature,
)
from app.automod.schemas import AutoModActionInput, AutoModRuleCreate, AutoModRuleUpdate
from app.automod.service import (
    create_rule,
    delete_rule,
    get_rule,
    rule_payload,
    update_rule,
)
from app.bots.auth import BotPrincipal, require_bot
from app.bots.installations import installation_allows_channel
from app.chat.audit import normalize_audit_reason
from app.chat.permissions import require_permissions
from app.core.permission_contract import required_permissions
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.models import (
    AutoModAction,
    AutoModRule,
    AutoModRuleExemptChannel,
    Channel,
    Guild,
    User,
)

router = APIRouter(prefix="/api/v1", tags=["auto moderation"])


async def _rules(session: AsyncSession, guild: Guild) -> list[AutoModRule]:
    return list(
        await session.scalars(
            select(AutoModRule)
            .where(
                AutoModRule.guild_id == guild.id,
                AutoModRule.guild_domain == guild.origin_domain,
            )
            .order_by(AutoModRule.id)
            .limit(100)
        )
    )


async def _list_rules(session: AsyncSession, guild: Guild) -> list[dict[str, object]]:
    rules = await _rules(session, guild)
    return [await rule_payload(session, item) for item in rules]


async def _require_action_permissions(
    session: AsyncSession,
    redis: Redis,
    guild: Guild,
    actor: User,
    actions: list[AutoModActionInput] | None,
    *,
    rule: AutoModRule | None = None,
) -> None:
    needs_timeout_permission = bool(
        actions is not None and any(item.type == "timeout" for item in actions)
    )
    if actions is None and rule is not None:
        needs_timeout_permission = (
            await session.scalar(
                select(AutoModAction.rule_id).where(
                    AutoModAction.rule_id == rule.id,
                    AutoModAction.rule_domain == rule.origin_domain,
                    AutoModAction.action_type == "timeout",
                )
            )
            is not None
        )
    if needs_timeout_permission:
        await require_permissions(
            session,
            redis,
            guild,
            actor,
            required_permissions("member.timeout"),
        )


def _deny_bot_automod_channel_access(*, raise_on_denied: bool) -> bool:
    if raise_on_denied:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_CHANNEL_RESTRICTED"},
        )
    return False


async def _require_bot_automod_channel_access(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    guild: Guild,
    actor: User,
    installation: BotInstallation,
    *,
    actions: list[AutoModActionInput] | None,
    exempt_channels: list[EntityRef] | None,
    rule: AutoModRule | None = None,
    raise_on_denied: bool = True,
) -> bool:
    """Bind every effective nested AutoMod channel to the bot installation."""

    channel_refs: set[tuple[int, str]] = set()
    if actions is not None:
        for action in actions:
            if action.type == "send_alert_message" and action.channel_id is not None:
                channel_refs.add(action.channel_id.resolve(settings.domain))
    elif rule is not None:
        stored_actions = await session.scalars(
            select(AutoModAction).where(
                AutoModAction.rule_id == rule.id,
                AutoModAction.rule_domain == rule.origin_domain,
                AutoModAction.action_type == "send_alert_message",
            )
        )
        for stored_action in stored_actions:
            raw_ref = (stored_action.action_metadata or {}).get("channel_id")
            if isinstance(raw_ref, str):
                try:
                    channel_refs.add(EntityRef(raw_ref).resolve(settings.domain))
                except ValueError:
                    return _deny_bot_automod_channel_access(raise_on_denied=raise_on_denied)
    if exempt_channels is not None:
        channel_refs.update(item.resolve(settings.domain) for item in exempt_channels)
    elif rule is not None:
        stored_exemptions = await session.scalars(
            select(AutoModRuleExemptChannel).where(
                AutoModRuleExemptChannel.rule_id == rule.id,
                AutoModRuleExemptChannel.rule_domain == rule.origin_domain,
            )
        )
        channel_refs.update((item.channel_id, item.channel_domain) for item in stored_exemptions)

    for channel_ref in sorted(channel_refs, key=lambda item: (item[1], item[0])):
        channel = await session.get(Channel, channel_ref)
        if channel is None or (channel.guild_id, channel.guild_domain) != (
            guild.id,
            guild.origin_domain,
        ):
            return _deny_bot_automod_channel_access(raise_on_denied=raise_on_denied)
        if not await installation_allows_channel(session, installation, channel):
            return _deny_bot_automod_channel_access(raise_on_denied=raise_on_denied)
        try:
            await require_permissions(
                session,
                redis,
                guild,
                actor,
                required_permissions("guild.automod.create"),
                channel=channel,
            )
        except HTTPException:
            if raise_on_denied:
                raise
            return False
    return True


@router.get("/guilds/{guild_ref}/auto-moderation/rules")
async def list_auto_mod_rules(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    proxied, body = await proxy_human_guild_feature(
        session, settings, guild_ref, auth.user, "automod.list"
    )
    if proxied:
        return cast(list[dict[str, object]], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.automod.list"
    )
    return await _list_rules(session, guild)


@router.get("/guilds/{guild_ref}/auto-moderation/rules/{rule_id}")
async def get_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
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
        "automod.get",
        {"resource_id": rule_id},
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.automod.list"
    )
    return await rule_payload(session, await get_rule(session, guild, rule_id))


@router.post("/guilds/{guild_ref}/auto-moderation/rules")
async def create_auto_mod_rule(
    guild_ref: EntityRef,
    payload: AutoModRuleCreate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "automod.create",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.automod.create"
    )
    await _require_action_permissions(session, redis, guild, auth.user, payload.actions)
    rule = await create_rule(
        session,
        settings,
        snowflake,
        guild,
        auth.user,
        payload,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    rendered = await rule_payload(session, rule)
    return rendered


@router.patch("/guilds/{guild_ref}/auto-moderation/rules/{rule_id}")
async def patch_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
    payload: AutoModRuleUpdate,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "automod.update",
        {
            "resource_id": rule_id,
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.automod.update"
    )
    rule = await get_rule(session, guild, rule_id, for_update=True)
    await _require_action_permissions(session, redis, guild, auth.user, payload.actions, rule=rule)
    rule = await update_rule(
        session,
        settings,
        snowflake,
        guild,
        auth.user,
        rule,
        payload,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    rendered = await rule_payload(session, rule)
    return rendered


@router.delete("/guilds/{guild_ref}/auto-moderation/rules/{rule_id}", status_code=204)
async def remove_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> Response:
    proxied, _ = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "automod.delete",
        {"resource_id": rule_id, "reason": normalize_audit_reason(reason)},
    )
    if proxied:
        return Response(status_code=204)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.automod.delete"
    )
    rule = await get_rule(session, guild, rule_id, for_update=True)
    await delete_rule(
        session,
        settings,
        snowflake,
        guild,
        auth.user,
        rule,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    return Response(status_code=204)


@router.get("/bots/guilds/{guild_ref}/auto-moderation/rules")
async def bot_list_auto_mod_rules(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, object]]:
    guild, installation = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="automod.rules.read",
        operation="guild.automod.list",
    )
    rendered: list[dict[str, object]] = []
    for rule in await _rules(session, guild):
        if await _require_bot_automod_channel_access(
            session,
            redis,
            settings,
            guild,
            principal.user,
            installation,
            actions=None,
            exempt_channels=None,
            rule=rule,
            raise_on_denied=False,
        ):
            rendered.append(await rule_payload(session, rule))
    return rendered


@router.get("/bots/guilds/{guild_ref}/auto-moderation/rules/{rule_id}")
async def bot_get_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    guild, installation = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="automod.rules.read",
        operation="guild.automod.list",
    )
    rule = await get_rule(session, guild, rule_id)
    if not await _require_bot_automod_channel_access(
        session,
        redis,
        settings,
        guild,
        principal.user,
        installation,
        actions=None,
        exempt_channels=None,
        rule=rule,
        raise_on_denied=False,
    ):
        raise HTTPException(
            status_code=404,
            detail={"code": "AUTO_MOD_RULE_NOT_FOUND", "message": "AutoMod rule not found."},
        )
    return await rule_payload(session, rule)


@router.post("/bots/guilds/{guild_ref}/auto-moderation/rules")
async def bot_create_auto_mod_rule(
    guild_ref: EntityRef,
    payload: AutoModRuleCreate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    guild, installation = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="automod.rules.manage",
        operation="guild.automod.create",
    )
    await _require_action_permissions(session, redis, guild, principal.user, payload.actions)
    await _require_bot_automod_channel_access(
        session,
        redis,
        settings,
        guild,
        principal.user,
        installation,
        actions=payload.actions,
        exempt_channels=payload.exempt_channels,
    )
    rule = await create_rule(
        session,
        settings,
        snowflake,
        guild,
        principal.user,
        payload,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    rendered = await rule_payload(session, rule)
    return rendered


@router.patch("/bots/guilds/{guild_ref}/auto-moderation/rules/{rule_id}")
async def bot_patch_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
    payload: AutoModRuleUpdate,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    guild, installation = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="automod.rules.manage",
        operation="guild.automod.update",
    )
    locked_rule = await get_rule(session, guild, rule_id, for_update=True)
    # A restricted bot must not take over a hidden rule by replacing every
    # inaccessible nested channel with an allowed one in the same PATCH.
    await _require_bot_automod_channel_access(
        session,
        redis,
        settings,
        guild,
        principal.user,
        installation,
        actions=None,
        exempt_channels=None,
        rule=locked_rule,
    )
    await _require_action_permissions(
        session,
        redis,
        guild,
        principal.user,
        payload.actions,
        rule=locked_rule,
    )
    await _require_bot_automod_channel_access(
        session,
        redis,
        settings,
        guild,
        principal.user,
        installation,
        actions=payload.actions,
        exempt_channels=payload.exempt_channels,
        rule=locked_rule,
    )
    rule = await update_rule(
        session,
        settings,
        snowflake,
        guild,
        principal.user,
        locked_rule,
        payload,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    rendered = await rule_payload(session, rule)
    return rendered


@router.delete("/bots/guilds/{guild_ref}/auto-moderation/rules/{rule_id}", status_code=204)
async def bot_remove_auto_mod_rule(
    guild_ref: EntityRef,
    rule_id: int,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> Response:
    guild, installation = await authorize_bot_guild_feature_grant(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="automod.rules.manage",
        operation="guild.automod.delete",
    )
    rule = await get_rule(session, guild, rule_id, for_update=True)
    await _require_bot_automod_channel_access(
        session,
        redis,
        settings,
        guild,
        principal.user,
        installation,
        actions=None,
        exempt_channels=None,
        rule=rule,
    )
    await delete_rule(
        session,
        settings,
        snowflake,
        guild,
        principal.user,
        rule,
        redis=redis,
        reason=normalize_audit_reason(reason),
    )
    return Response(status_code=204)
