from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from redis.asyncio import Redis
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    queue_guild_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import guild_member, require_can_manage_member
from app.chat.payloads import audit_payload, ban_payload, member_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import BanCreate, MemberUpdate
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, Snowflake
from app.db.models import (
    AuditLogEntry,
    Ban,
    Channel,
    GuildMember,
    MemberRole,
    Message,
    User,
)

router = APIRouter(prefix="/api/v1/guilds", tags=["moderation"])


def audit_reason(value: str | None) -> str | None:
    if value is not None and len(value) > 512:
        raise HTTPException(status_code=400, detail={"code": "AUDIT_REASON_TOO_LONG"})
    return value


@router.get("/{guild_id}/members")
async def list_members(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.list"))
    conditions = [
        GuildMember.guild_id == guild.id,
        GuildMember.guild_domain == guild.origin_domain,
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(
            tuple_(GuildMember.user_id, GuildMember.user_domain) > (after_id, after_domain)
        )
    rows = (
        await session.execute(
            select(GuildMember, User)
            .join(
                User,
                (User.id == GuildMember.user_id) & (User.origin_domain == GuildMember.user_domain),
            )
            .where(*conditions)
            .order_by(GuildMember.user_id, GuildMember.user_domain)
            .limit(limit)
        )
    ).all()
    refs = [(member.user_id, member.user_domain) for member, _ in rows]
    roles: dict[tuple[int, str], list[int]] = {ref: [] for ref in refs}
    if refs:
        assignments = await session.scalars(
            select(MemberRole).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                tuple_(MemberRole.user_id, MemberRole.user_domain).in_(refs),
            )
        )
        for assignment in assignments:
            roles[(assignment.user_id, assignment.user_domain)].append(assignment.role_id)
    return [
        member_payload(member, user, roles[(member.user_id, member.user_domain)])
        for member, user in rows
    ]


@router.patch("/{guild_id}/members/{user_id}")
async def update_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: MemberUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    self_update = (user_number, user_domain) == (auth.user.id, auth.user.origin_domain)
    if self_update:
        member = await guild_member(session, guild, user_number, user_domain)
        if "timeout_until" in payload.model_fields_set:
            raise HTTPException(status_code=403, detail={"code": "CANNOT_TIMEOUT_SELF"})
        await require_permissions(
            session, redis, guild, auth.user, required_permissions("member.nickname.self")
        )
    else:
        member = await require_can_manage_member(
            session, guild, auth.user, user_number, user_domain
        )
        needed = Permission(0)
        if "nickname" in payload.model_fields_set:
            needed |= Permission.MANAGE_NICKNAMES
        if "timeout_until" in payload.model_fields_set:
            needed |= Permission.MODERATE_MEMBERS
        await require_permissions(session, redis, guild, auth.user, needed)
    values = payload.model_dump(exclude_unset=True)
    timeout = values.get("timeout_until")
    if timeout is not None:
        now = datetime.now(UTC)
        if timeout.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_REQUIRES_TIMEZONE"})
        if timeout > now + timedelta(days=28):
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_TOO_LONG"})
        if timeout <= now:
            values["timeout_until"] = None
    changes: list[dict[str, object]] = []
    for field, value in values.items():
        old = getattr(member, field)
        if old != value:
            changes.append({"key": field, "old_value": str(old), "new_value": str(value)})
            setattr(member, field, value)
    if changes:
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.update",
            {
                "member": {
                    "user": {"id": str(user_number), "origin_domain": user_domain},
                    "nickname": member.nickname,
                    "timeout_until": (
                        member.timeout_until.isoformat() if member.timeout_until else None
                    ),
                    "member_version": str(member.member_version),
                }
            },
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            24,
            target_type="member",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            reason=audit_reason(reason),
            changes=changes,
        )
        await session.commit()
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(user_number),
                "user_domain": user_domain,
            },
        )
    target_user = await session.scalar(
        select(User).where(User.id == user_number, User.origin_domain == user_domain)
    )
    if target_user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    roles = list(
        await session.scalars(
            select(MemberRole.role_id).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == user_number,
                MemberRole.user_domain == user_domain,
            )
        )
    )
    return member_payload(member, target_user, roles)


@router.delete("/{guild_id}/members/{user_id}", status_code=204)
async def kick_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.kick"))
    member = await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        20,
        target_type="member",
        target_ref={"id": str(user_number), "origin_domain": user_domain},
        reason=audit_reason(reason),
    )
    await session.delete(member)
    await queue_guild_access_revocation(
        session,
        settings,
        guild,
        user_id=user_number,
        user_domain=user_domain,
        reason="member_kicked",
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.member.remove",
        {"user": {"id": str(user_number), "origin_domain": user_domain}},
        snapshot_required=True,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBER_REMOVE",
        {
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
            "user_id": str(user_number),
            "user_domain": user_domain,
        },
    )
    if user_domain == settings.domain:
        await publish_dispatch(
            redis,
            user_topic(user_domain, user_number),
            "GUILD_DELETE",
            {"id": str(guild.id), "origin_domain": guild.origin_domain},
        )
    return Response(status_code=204)


@router.put("/{guild_id}/bans/{user_id}", status_code=204)
async def ban_member(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: BanCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    header_reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.ban"))
    user = await session.scalar(
        select(User).where(User.id == user_number, User.origin_domain == user_domain)
    )
    if user is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == user_number,
            GuildMember.user_domain == user_domain,
        )
    )
    if member is not None:
        await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    elif (user_number, user_domain) == (guild.owner_id, guild.owner_domain):
        raise HTTPException(status_code=403, detail={"code": "OWNER_IMMUNE"})
    reason = audit_reason(header_reason or payload.reason)
    await session.execute(
        pg_insert(Ban)
        .values(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user_number,
            user_domain=user_domain,
            reason=reason,
            actor_id=auth.user.id,
            actor_domain=auth.user.origin_domain,
        )
        .on_conflict_do_update(
            index_elements=["guild_id", "guild_domain", "user_id", "user_domain"],
            set_={
                "reason": reason,
                "actor_id": auth.user.id,
                "actor_domain": auth.user.origin_domain,
            },
        )
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.ban.add",
        {"user": {"id": str(user_number), "origin_domain": user_domain}},
    )
    if payload.delete_message_seconds:
        cutoff = datetime.now(UTC) - timedelta(seconds=payload.delete_message_seconds)
        deleted_at = datetime.now(UTC)
        channel_ids = select(Channel.id).where(
            Channel.guild_id == guild.id, Channel.guild_domain == guild.origin_domain
        )
        await session.execute(
            update(Message)
            .where(
                Message.channel_id.in_(channel_ids),
                Message.channel_domain == guild.origin_domain,
                Message.author_id == user_number,
                Message.author_domain == user_domain,
                Message.created_at >= cutoff,
                Message.deleted_at.is_(None),
            )
            .values(content=None, e2ee=None, deleted_at=deleted_at)
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.message.purge",
            {
                "author": {"id": str(user_number), "origin_domain": user_domain},
                "created_after": cutoff.isoformat(),
                "deleted_at": deleted_at.isoformat(),
            },
        )
    if member is not None:
        await session.delete(member)
        await queue_guild_access_revocation(
            session,
            settings,
            guild,
            user_id=user_number,
            user_domain=user_domain,
            reason="member_banned",
        )
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.remove",
            {"user": {"id": str(user_number), "origin_domain": user_domain}},
            snapshot_required=True,
        )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        22,
        target_type="user",
        target_ref={"id": str(user_number), "origin_domain": user_domain},
        reason=reason,
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    if member is not None:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_REMOVE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(user_number),
                "user_domain": user_domain,
            },
        )
        if user_domain == settings.domain:
            await publish_dispatch(
                redis,
                user_topic(user_domain, user_number),
                "GUILD_DELETE",
                {"id": str(guild.id), "origin_domain": guild.origin_domain},
            )
    return Response(status_code=204)


@router.delete("/{guild_id}/bans/{user_id}", status_code=204)
async def remove_ban(
    guild_id: EntityRef,
    user_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    user_number, user_domain = user_id.resolve(settings.domain)
    await require_permissions(session, redis, guild, auth.user, required_permissions("ban.remove"))
    result = await session.execute(
        delete(Ban)
        .where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == user_number,
            Ban.user_domain == user_domain,
        )
        .returning(Ban.user_id)
    )
    if result.scalar_one_or_none() is not None:
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.ban.remove",
            {"user": {"id": str(user_number), "origin_domain": user_domain}},
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            23,
            target_type="user",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            reason=audit_reason(reason),
        )
        await session.commit()
        await wake_queued_guild_federation(guild)
    return Response(status_code=204)


@router.get("/{guild_id}/bans")
async def list_bans(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(session, redis, guild, auth.user, required_permissions("ban.list"))
    conditions = [Ban.guild_id == guild.id, Ban.guild_domain == guild.origin_domain]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(tuple_(Ban.user_id, Ban.user_domain) > (after_id, after_domain))
    rows = (
        await session.execute(
            select(Ban, User)
            .join(User, (User.id == Ban.user_id) & (User.origin_domain == Ban.user_domain))
            .where(*conditions)
            .order_by(Ban.user_id, Ban.user_domain)
            .limit(limit)
        )
    ).all()
    return [ban_payload(ban, user) for ban, user in rows]


@router.get("/{guild_id}/audit-logs")
async def list_audit_logs(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=100),
    before: Snowflake | None = None,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.audit.list")
    )
    conditions = [
        AuditLogEntry.guild_id == guild.id,
        AuditLogEntry.guild_domain == guild.origin_domain,
    ]
    if before is not None:
        conditions.append(AuditLogEntry.id < before)
    entries = await session.scalars(
        select(AuditLogEntry).where(*conditions).order_by(AuditLogEntry.id.desc()).limit(limit)
    )
    return [audit_payload(entry) for entry in entries]
