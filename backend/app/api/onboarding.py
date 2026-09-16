from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    proxy_authenticated_guild_management,
    require_user,
)
from app.api.guilds import local_guild
from app.api.management import render_member_update
from app.chat.audit import add_audit_entry
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import queue_guild_mutation, wake_queued_guild_federation
from app.chat.hierarchy import require_can_manage_role
from app.chat.onboarding import (
    SELF_ASSIGNABLE_PERMISSIONS,
    OnboardingAnswers,
    OnboardingConfig,
    needs_rules,
    selected_onboarding,
)
from app.chat.payloads import guild_payload
from app.chat.permissions import get_permissions, require_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.models import Channel, ChannelOverwrite, Guild, GuildMember, MemberRole, Role, User

router = APIRouter(prefix="/api/v1/guilds", tags=["onboarding"])


async def membership(session: AsyncSession, guild: Guild, actor: User) -> GuildMember:
    member = await session.get(
        GuildMember, (guild.id, guild.origin_domain, actor.id, actor.origin_domain)
    )
    if member is None or actor.account_type != "human":
        raise HTTPException(404, detail={"code": "GUILD_NOT_FOUND"})
    return member


async def response_for(
    session: AsyncSession, redis: Redis, guild: Guild, actor: User
) -> dict[str, Any]:
    member = await membership(session, guild, actor)
    config = OnboardingConfig.model_validate(guild.onboarding or {})
    permissions = await get_permissions(session, redis, guild, actor)
    can_manage = permissions & (Permission.MANAGE_GUILD | Permission.MANAGE_ROLES) == (
        Permission.MANAGE_GUILD | Permission.MANAGE_ROLES
    )
    state = member.onboarding_state or {}
    return {
        "guild_id": str(guild.id),
        "guild_domain": guild.origin_domain,
        "config": config.model_dump(mode="json"),
        "state": state,
        "can_manage": can_manage,
        "needs_rules": needs_rules(guild.onboarding, state)
        and not bool(permissions & Permission.ADMINISTRATOR),
        "needs_onboarding": config.enabled and not state.get("completed_at") and not can_manage,
    }


async def safe_role(session: AsyncSession, guild: Guild, ref: str) -> Role:
    try:
        identity = EntityRef(ref).resolve(guild.origin_domain)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_ROLE"}) from exc
    role = await session.get(Role, identity)
    if (
        role is None
        or (role.guild_id, role.guild_domain) != (guild.id, guild.origin_domain)
        or role.id == guild.id
        or role.managed
        or role.permissions & ~SELF_ASSIGNABLE_PERMISSIONS
        or await session.scalar(
            select(ChannelOverwrite.channel_id)
            .where(
                ChannelOverwrite.guild_id == guild.id,
                ChannelOverwrite.guild_domain == guild.origin_domain,
                ChannelOverwrite.target_type == "role",
                ChannelOverwrite.target_id == role.id,
                ChannelOverwrite.target_domain == role.origin_domain,
                ChannelOverwrite.allow.bitwise_and(~SELF_ASSIGNABLE_PERMISSIONS) != 0,
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            422,
            detail={
                "code": "ONBOARDING_INVALID_ROLE",
                "message": (
                    "Onboarding roles must belong to this server and cannot grant "
                    "moderation or administration permissions, including channel overrides."
                ),
            },
        )
    return role


@router.get("/{guild_ref}/onboarding")
async def get_onboarding(
    guild_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    proxied, result = await proxy_authenticated_guild_management(
        session, settings, guild_ref, auth, "onboarding.get", {}
    )
    if proxied:
        if result is None:
            raise HTTPException(502, detail={"code": "ONBOARDING_PROXY_FAILED"})
        return cast(dict[str, Any], result.body)
    guild = await local_guild(session, settings, guild_ref)
    return await response_for(session, redis, guild, auth.user)


@router.put("/{guild_ref}/onboarding")
async def update_onboarding(
    guild_ref: EntityRef,
    payload: OnboardingConfig,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    proxied, result = await proxy_authenticated_guild_management(
        session, settings, guild_ref, auth, "onboarding.update", payload.model_dump(mode="json")
    )
    if proxied:
        if result is None:
            raise HTTPException(502, detail={"code": "ONBOARDING_PROXY_FAILED"})
        return cast(dict[str, Any], result.body)
    guild = await local_guild(session, settings, guild_ref, for_update=True)
    await membership(session, guild, auth.user)
    await require_permissions(
        session, redis, guild, auth.user, Permission.MANAGE_GUILD | Permission.MANAGE_ROLES
    )
    previous = OnboardingConfig.model_validate(guild.onboarding or {})
    if payload.revision != previous.revision:
        raise HTTPException(
            409,
            detail={
                "code": "ONBOARDING_CHANGED",
                "message": "Another administrator changed onboarding. Reload before saving.",
            },
        )
    roles = {
        ref
        for question in payload.questions
        for option in question.options
        for ref in option.role_ids
    }
    for ref in roles:
        await require_can_manage_role(
            session, guild, auth.user, await safe_role(session, guild, ref)
        )
    channels = (
        set(payload.default_channel_ids)
        | {
            ref
            for question in payload.questions
            for option in question.options
            for ref in option.channel_ids
        }
        | {item.channel_id for item in payload.guide if item.channel_id}
    )
    for ref in channels:
        try:
            channel = await session.get(Channel, EntityRef(ref).resolve(guild.origin_domain))
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_CHANNEL"}) from exc
        if (
            channel is None
            or channel.unavailable
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
            or channel.type in {4, 10, 11, 12}
        ):
            raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_CHANNEL"})
        await require_permissions(
            session, redis, guild, auth.user, Permission.VIEW_CHANNEL, channel=channel
        )
    payload.revision = previous.revision + 1
    payload.rules_revision = previous.rules_revision + int(
        payload.rules != previous.rules or payload.enabled != previous.enabled
    )
    guild.onboarding = payload.model_dump(mode="json")
    guild.permission_generation += 1
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.update",
        {"guild": guild_payload(guild)},
        snapshot_required=True,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        1,
        target_type="guild",
        target_ref={"id": str(guild.id)},
        changes=[
            {"key": "onboarding", "old_value": previous.model_dump(), "new_value": guild.onboarding}
        ],
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_UPDATE", guild_payload(guild)
    )
    return await response_for(session, redis, guild, auth.user)


@router.put("/{guild_ref}/onboarding/@me")
async def complete_onboarding(
    guild_ref: EntityRef,
    payload: OnboardingAnswers,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    proxied, result = await proxy_authenticated_guild_management(
        session, settings, guild_ref, auth, "onboarding.complete", payload.model_dump(mode="json")
    )
    if proxied:
        if result is None:
            raise HTTPException(502, detail={"code": "ONBOARDING_PROXY_FAILED"})
        return cast(dict[str, Any], result.body)
    guild = await local_guild(session, settings, guild_ref, for_update=True)
    member = await membership(session, guild, auth.user)
    config = OnboardingConfig.model_validate(guild.onboarding or {})
    if not config.enabled:
        raise HTTPException(409, detail={"code": "ONBOARDING_DISABLED"})
    role_refs, channels = selected_onboarding(config, payload)
    old = member.onboarding_state or {}
    if needs_rules(guild.onboarding, old) and not payload.accept_rules:
        raise HTTPException(
            403,
            detail={
                "code": "RULES_ACCEPTANCE_REQUIRED",
                "message": "Read and accept the server rules to continue.",
            },
        )
    roles_by_ref = {}
    for ref in sorted(role_refs):
        role = await safe_role(session, guild, ref)
        roles_by_ref[(role.id, role.origin_domain)] = role
    roles = list(roles_by_ref.values())
    for ref in payload.extra_channel_ids:
        try:
            channel = await session.get(Channel, EntityRef(ref).resolve(guild.origin_domain))
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_CHANNEL"}) from exc
        if (
            channel is None
            or channel.unavailable
            or (channel.guild_id, channel.guild_domain) != (guild.id, guild.origin_domain)
        ):
            raise HTTPException(422, detail={"code": "ONBOARDING_INVALID_CHANNEL"})
        await require_permissions(
            session, redis, guild, auth.user, Permission.VIEW_CHANNEL, channel=channel
        )
        channels.add(f"{channel.id}@{channel.origin_domain}")
    existing = list(
        await session.scalars(
            select(MemberRole).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == auth.user.id,
                MemberRole.user_domain == auth.user.origin_domain,
            )
        )
    )
    existing_by_ref = {f"{role.role_id}@{role.role_domain}": role for role in existing}
    wanted = {f"{role.id}@{role.origin_domain}" for role in roles}
    owned = set(old.get("granted_role_ids", []))
    for ref in owned - wanted:
        if ref in existing_by_ref:
            await session.delete(existing_by_ref[ref])
    granted = owned & wanted
    for role in roles:
        ref = f"{role.id}@{role.origin_domain}"
        if ref not in existing_by_ref:
            session.add(
                MemberRole(
                    guild_id=guild.id,
                    guild_domain=guild.origin_domain,
                    user_id=auth.user.id,
                    user_domain=auth.user.origin_domain,
                    role_id=role.id,
                    role_domain=role.origin_domain,
                )
            )
            granted.add(ref)
    member.onboarding_state = {
        "rules_revision": config.rules_revision,
        "accepted_at": datetime.now(UTC).isoformat()
        if payload.accept_rules
        else old.get("accepted_at"),
        "completed_at": old.get("completed_at") or datetime.now(UTC).isoformat(),
        "answers": payload.answers,
        "channel_ids": sorted(channels),
        "extra_channel_ids": payload.extra_channel_ids,
        "granted_role_ids": sorted(granted),
        "completed_tasks": payload.completed_tasks,
    }
    if roles:
        member.temporary = False
    member.member_version += 1
    await session.flush()
    rendered = await render_member_update(session, member)
    e2ee_channels: list[Channel] = []
    for event, refs in (
        ("guild.member.role.remove", (owned - wanted) & existing_by_ref.keys()),
        ("guild.member.role.add", granted - set(existing_by_ref)),
    ):
        for ref in sorted(refs):
            role_id, role_domain = EntityRef(ref).resolve(guild.origin_domain)
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                event,
                {
                    "user": {"id": str(auth.user.id), "origin_domain": auth.user.origin_domain},
                    "role": {"id": str(role_id), "origin_domain": role_domain},
                    "member_version": str(member.member_version),
                },
                snapshot_required=True,
                e2ee_policy_channels=e2ee_channels,
            )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.member.update",
        {"member": rendered},
        snapshot_required=True,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_e2ee_policy_updates(session, redis, settings, e2ee_channels)
    await publish_dispatch(
        redis, guild_topic(guild.origin_domain, guild.id), "GUILD_MEMBER_UPDATE", rendered
    )
    return await response_for(session, redis, guild, auth.user)
