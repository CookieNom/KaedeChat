from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import Field, field_validator
from redis.asyncio import Redis
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import user_auth
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guild_feature_access import (
    authorize_bot_guild_feature,
    authorize_human_guild_feature,
    proxy_human_guild_feature,
)
from app.api.moderation import (
    MemberModerationPostCommit,
    stage_ban_member,
    stage_kick_member,
)
from app.bots.auth import BotPrincipal, require_bot
from app.chat.audit import add_audit_entry, normalize_audit_reason
from app.chat.events import guild_topic, publish_dispatch
from app.chat.hierarchy import highest_role, role_rank
from app.chat.schemas import BanCreate, RequestModel
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.models import Guild, GuildMember, MemberRole, Role, User

router = APIRouter(prefix="/api/v1", tags=["bulk moderation"])


class PruneRequest(RequestModel):
    days: int = Field(default=7, ge=1, le=30)
    include_roles: list[EntityRef] = Field(default_factory=list, max_length=100)
    compute_prune_count: bool = True

    @field_validator("include_roles")
    @classmethod
    def unique_roles(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(value) != len(set(value)):
            raise ValueError("included roles must be unique")
        return value


class BulkBanRequest(RequestModel):
    user_ids: list[EntityRef] = Field(min_length=1, max_length=200)
    delete_message_seconds: int = Field(default=0, ge=0, le=604_800)
    reason: str | None = Field(default=None, max_length=512)

    @field_validator("user_ids")
    @classmethod
    def unique_users(cls, value: list[EntityRef]) -> list[EntityRef]:
        if len(value) != len(set(value)):
            raise ValueError("bulk ban users must be unique")
        return value


def _resolved_unique_refs(
    values: list[EntityRef],
    default_domain: str,
    *,
    code: str,
    label: str,
) -> list[tuple[int, str]]:
    """Resolve bare IDs before checking uniqueness across federation aliases."""

    resolved = [item.resolve(default_domain) for item in values]
    if len(resolved) != len(set(resolved)):
        raise HTTPException(
            status_code=400,
            detail={"code": code, "message": f"{label} must be unique."},
        )
    return resolved


def _qualified_unique_refs(
    values: list[EntityRef],
    default_domain: str,
    *,
    code: str,
    label: str,
) -> list[EntityRef]:
    """Resolve once and serialize identities independently of a remote authority."""

    return [
        EntityRef(f"{resource_id}@{resource_domain}")
        for resource_id, resource_domain in _resolved_unique_refs(
            values,
            default_domain,
            code=code,
            label=label,
        )
    ]


async def _prune_candidates(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    *,
    days: int,
    include_roles: list[EntityRef],
    actor_ref: tuple[int, str],
) -> list[tuple[int, str]]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    role_refs = _resolved_unique_refs(
        include_roles,
        settings.domain,
        code="PRUNE_ROLE_DUPLICATE",
        label="Included roles",
    )
    if any(domain != guild.origin_domain for _, domain in role_refs):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "PRUNE_ROLE_INVALID",
                "message": "Every included role must belong to this guild.",
            },
        )
    if role_refs:
        found_roles = set(
            (
                await session.execute(
                    select(Role.id, Role.origin_domain).where(
                        Role.guild_id == guild.id,
                        Role.guild_domain == guild.origin_domain,
                        Role.origin_domain == guild.origin_domain,
                        Role.id.in_([role_id for role_id, _ in role_refs]),
                    )
                )
            ).tuples()
        )
        if found_roles != set(role_refs):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "PRUNE_ROLE_INVALID",
                    "message": "Every included role must exist in this guild.",
                },
            )
    actor_role: Role | None = None
    if actor_ref != (guild.owner_id, guild.owner_domain):
        actor_role = await highest_role(session, guild, actor_ref[0], actor_ref[1])
        # Everyone implicitly has @everyone. An actor with no higher role
        # cannot prune another member of equal rank.
        if actor_role.id == guild.id:
            return []
    base = (
        select(GuildMember.user_id, GuildMember.user_domain)
        .join(
            User,
            (User.id == GuildMember.user_id) & (User.origin_domain == GuildMember.user_domain),
        )
        .where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            func.coalesce(GuildMember.last_guild_activity_at, GuildMember.joined_at) < cutoff,
            User.account_type != "bot",
            or_(
                GuildMember.user_id != guild.owner_id,
                GuildMember.user_domain != guild.owner_domain,
            ),
            or_(GuildMember.user_id != actor_ref[0], GuildMember.user_domain != actor_ref[1]),
        )
    )
    role_membership = exists().where(
        MemberRole.guild_id == guild.id,
        MemberRole.guild_domain == guild.origin_domain,
        MemberRole.user_id == GuildMember.user_id,
        MemberRole.user_domain == GuildMember.user_domain,
        MemberRole.role_id != guild.id,
    )
    if role_refs:
        unselected_role_membership = exists().where(
            MemberRole.guild_id == guild.id,
            MemberRole.guild_domain == guild.origin_domain,
            MemberRole.user_id == GuildMember.user_id,
            MemberRole.user_domain == GuildMember.user_domain,
            MemberRole.role_id != guild.id,
            or_(
                MemberRole.role_domain != guild.origin_domain,
                MemberRole.role_id.not_in([role_id for role_id, _ in role_refs]),
            ),
        )
        # Discord treats include_roles as an allow-list. Roleless members are
        # eligible (their role set is empty), and a member with roles is
        # eligible only when every non-default role is included. Merely having
        # one selected role must not hide an additional protected role.
        base = base.where(~unselected_role_membership)
    else:
        base = base.where(~role_membership)
    if actor_role is not None:
        actor_position, actor_id_rank = role_rank(actor_role)
        actor_id = -actor_id_rank
        unmanageable_role = exists().where(
            MemberRole.guild_id == guild.id,
            MemberRole.guild_domain == guild.origin_domain,
            MemberRole.user_id == GuildMember.user_id,
            MemberRole.user_domain == GuildMember.user_domain,
            Role.id == MemberRole.role_id,
            Role.origin_domain == MemberRole.role_domain,
            Role.guild_id == guild.id,
            Role.guild_domain == guild.origin_domain,
            or_(
                Role.position > actor_position,
                (Role.position == actor_position) & (Role.id <= actor_id),
            ),
        )
        base = base.where(~unmanageable_role)
    rows = await session.execute(base.order_by(GuildMember.user_domain, GuildMember.user_id))
    return list(rows.tuples())


async def _perform_prune(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    auth: AuthenticatedUser,
    payload: PruneRequest,
    *,
    reason: str | None,
) -> dict[str, object]:
    normalized_reason = normalize_audit_reason(reason)
    postcommits: list[MemberModerationPostCommit] = []
    qualified_include_roles = [
        EntityRef(f"{role_id}@{role_domain}")
        for role_id, role_domain in _resolved_unique_refs(
            payload.include_roles,
            settings.domain,
            code="PRUNE_ROLE_DUPLICATE",
            label="Included roles",
        )
    ]
    try:
        candidates = await _prune_candidates(
            session,
            settings,
            guild,
            days=payload.days,
            include_roles=qualified_include_roles,
            actor_ref=(auth.user.id, auth.user.origin_domain),
        )
        pruned: list[str] = []
        failures: list[dict[str, str]] = []
        guild_ref = EntityRef(f"{guild.id}@{guild.origin_domain}")
        for user_id, user_domain in candidates:
            user_ref = EntityRef(f"{user_id}@{user_domain}")
            try:
                async with session.begin_nested():
                    postcommit = await stage_kick_member(
                        guild_ref,
                        user_ref,
                        auth,
                        session,
                        redis,
                        snowflake,
                        settings,
                        normalized_reason or f"Inactive for at least {payload.days} days",
                        record_kick_audit=False,
                    )
                postcommits.append(postcommit)
                pruned.append(str(user_ref))
            except HTTPException as exc:
                detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
                failures.append(
                    {
                        "user_id": str(user_ref),
                        "code": str(detail.get("code", "PRUNE_FAILED")),
                        "message": str(detail.get("message", "The member could not be pruned.")),
                    }
                )
        summary = {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "pruned": len(pruned),
            "pruned_user_ids": pruned,
            "failed_users": failures,
            "days": payload.days,
        }
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            21,
            target_type="guild",
            target_ref={
                "id": str(guild.id),
                "origin_domain": guild.origin_domain,
                "name": guild.name,
            },
            reason=normalized_reason,
            changes=[
                {"key": "delete_member_days", "new_value": str(payload.days)},
                {"key": "members_removed", "new_value": str(len(pruned))},
                {
                    "key": "include_roles",
                    "new_value": [str(role) for role in qualified_include_roles],
                },
            ],
        )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    for postcommit in postcommits:
        await postcommit.publish(session, redis, snowflake, settings)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBERS_PRUNED",
        summary,
    )
    return summary if payload.compute_prune_count else summary | {"pruned": None}


async def _perform_bulk_ban(
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    guild: Guild,
    auth: AuthenticatedUser,
    payload: BulkBanRequest,
    *,
    reason: str | None,
) -> dict[str, object]:
    guild_ref = EntityRef(f"{guild.id}@{guild.origin_domain}")
    banned: list[str] = []
    failed_user_details: list[dict[str, str]] = []
    postcommits: list[MemberModerationPostCommit] = []
    user_refs = _resolved_unique_refs(
        payload.user_ids,
        settings.domain,
        code="BULK_BAN_USER_DUPLICATE",
        label="Bulk ban users",
    )
    try:
        for user_id, user_domain in user_refs:
            user_ref = EntityRef(f"{user_id}@{user_domain}")
            try:
                async with session.begin_nested():
                    postcommit = await stage_ban_member(
                        guild_ref,
                        user_ref,
                        BanCreate(
                            reason=reason or payload.reason,
                            delete_message_seconds=payload.delete_message_seconds,
                        ),
                        auth,
                        session,
                        redis,
                        snowflake,
                        settings,
                        reason or payload.reason,
                    )
                postcommits.append(postcommit)
                banned.append(str(user_ref))
            except HTTPException as exc:
                detail: dict[str, object] = exc.detail if isinstance(exc.detail, dict) else {}
                failed_user_details.append(
                    {
                        "user_id": str(user_ref),
                        "code": str(detail.get("code", "BULK_BAN_FAILED")),
                        "message": str(detail.get("message", "The user could not be banned.")),
                    }
                )
        failed_users = [item["user_id"] for item in failed_user_details]
        if not banned:
            await session.rollback()
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "BULK_BAN_NONE_SUCCEEDED",
                    "message": "None of the supplied users could be banned.",
                    "failed_users": failed_users,
                    "failed_user_details": failed_user_details,
                },
            )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    for postcommit in postcommits:
        await postcommit.publish(session, redis, snowflake, settings)
    return {
        "banned_users": banned,
        "failed_users": [item["user_id"] for item in failed_user_details],
        "failed_user_details": failed_user_details,
    }


@router.get("/guilds/{guild_ref}/prune/estimate")
async def estimate_prune(
    guild_ref: EntityRef,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    include_roles: Annotated[list[EntityRef] | None, Query()] = None,
) -> dict[str, object]:
    qualified_include_roles = _qualified_unique_refs(
        include_roles or [],
        settings.domain,
        code="PRUNE_ROLE_DUPLICATE",
        label="Included roles",
    )
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "moderation.prune.estimate",
        {
            "days": days,
            "include_roles": [str(item) for item in qualified_include_roles],
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.prune"
    )
    candidates = await _prune_candidates(
        session,
        settings,
        guild,
        days=days,
        include_roles=qualified_include_roles,
        actor_ref=(auth.user.id, auth.user.origin_domain),
    )
    return {"pruned": len(candidates), "days": days}


@router.post("/guilds/{guild_ref}/prune")
async def prune_members(
    guild_ref: EntityRef,
    payload: PruneRequest,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    qualified_payload = payload.model_copy(
        update={
            "include_roles": _qualified_unique_refs(
                payload.include_roles,
                settings.domain,
                code="PRUNE_ROLE_DUPLICATE",
                label="Included roles",
            )
        }
    )
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "moderation.prune",
        {
            "data": qualified_payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.prune"
    )
    return await _perform_prune(
        session, redis, snowflake, settings, guild, auth, qualified_payload, reason=reason
    )


@router.post("/guilds/{guild_ref}/bulk-bans")
async def bulk_ban_members(
    guild_ref: EntityRef,
    payload: BulkBanRequest,
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    qualified_payload = payload.model_copy(
        update={
            "user_ids": _qualified_unique_refs(
                payload.user_ids,
                settings.domain,
                code="BULK_BAN_USER_DUPLICATE",
                label="Bulk ban users",
            )
        }
    )
    proxied, body = await proxy_human_guild_feature(
        session,
        settings,
        guild_ref,
        auth.user,
        "moderation.bulk_ban",
        {
            "data": qualified_payload.model_dump(mode="json", exclude_unset=True),
            "reason": normalize_audit_reason(reason),
        },
    )
    if proxied:
        return cast(dict[str, object], body)
    guild = await authorize_human_guild_feature(
        session, redis, settings, guild_ref, auth.user, "guild.bulk_ban"
    )
    return await _perform_bulk_ban(
        session, redis, snowflake, settings, guild, auth, qualified_payload, reason=reason
    )


@router.get("/bots/guilds/{guild_ref}/prune/estimate")
async def bot_estimate_prune(
    guild_ref: EntityRef,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    days: Annotated[int, Query(ge=1, le=30)] = 7,
    include_roles: Annotated[list[EntityRef] | None, Query()] = None,
) -> dict[str, object]:
    guild = await authorize_bot_guild_feature(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="moderation.prune",
        operation="guild.prune",
    )
    candidates = await _prune_candidates(
        session,
        settings,
        guild,
        days=days,
        include_roles=include_roles or [],
        actor_ref=(principal.user.id, principal.user.origin_domain),
    )
    return {"pruned": len(candidates), "days": days}


@router.post("/bots/guilds/{guild_ref}/prune")
async def bot_prune_members(
    guild_ref: EntityRef,
    payload: PruneRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    guild = await authorize_bot_guild_feature(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="moderation.prune",
        operation="guild.prune",
    )
    return await _perform_prune(
        session,
        redis,
        snowflake,
        settings,
        guild,
        user_auth(principal),
        payload,
        reason=reason,
    )


@router.post("/bots/guilds/{guild_ref}/bulk-bans")
async def bot_bulk_ban_members(
    guild_ref: EntityRef,
    payload: BulkBanRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason")] = None,
) -> dict[str, object]:
    guild = await authorize_bot_guild_feature(
        session,
        redis,
        settings,
        guild_ref,
        principal,
        scope="moderation.bans",
        operation="guild.bulk_ban",
    )
    return await _perform_bulk_ban(
        session,
        redis,
        snowflake,
        settings,
        guild,
        user_auth(principal),
        payload,
        reason=reason,
    )
