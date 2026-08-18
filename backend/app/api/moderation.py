from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import delete, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import local_guild
from app.bots.installations import (
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    revoke_installations_for_guild_instance,
    revoke_installations_for_guild_member,
)
from app.chat.audit import add_audit_entry
from app.chat.e2ee_membership import pause_guild_e2ee_for_membership_change
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    queue_guild_access_revocation,
    queue_guild_instance_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import (
    guild_member,
    highest_role,
    require_can_manage_member,
    role_rank,
)
from app.chat.moderation_status import guild_self_moderation_status, sanitize_timeout_reason
from app.chat.payloads import audit_payload, ban_payload, instance_ban_payload, member_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import BanCreate, InstanceBanCreate, MemberUpdate
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, Snowflake
from app.db.models import (
    AuditLogEntry,
    Ban,
    Channel,
    Guild,
    GuildInstanceBan,
    GuildMember,
    Instance,
    MemberRole,
    Message,
    Role,
    User,
)
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.schemas import GuildSelfModerationStatus

router = APIRouter(prefix="/api/v1/guilds", tags=["moderation"])


def audit_reason(value: str | None) -> str | None:
    if value is not None and len(value) > 512:
        raise HTTPException(status_code=400, detail={"code": "AUDIT_REASON_TOO_LONG"})
    return value


def future_expiry(value: datetime | None, *, code: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(status_code=400, detail={"code": f"{code}_REQUIRES_TIMEZONE"})
    normalized = value.astimezone(UTC)
    if normalized <= datetime.now(UTC):
        raise HTTPException(status_code=400, detail={"code": f"{code}_MUST_BE_FUTURE"})
    return normalized


def active_expiry(column: InstrumentedAttribute[datetime | None]) -> ColumnElement[bool]:
    return or_(column.is_(None), column > datetime.now(UTC))


@router.get("/{guild_id}/members/@me/moderation-status")
async def self_moderation_status(
    guild_id: EntityRef,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Return timeout details only to the member they describe.

    Remote guild state is fetched on demand over a signed request. The reason
    is never inserted into the local guild replica or a guild-topic event.
    """

    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["self_moderation_status"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    resolved_id, resolved_domain = guild_id.resolve(settings.domain)
    guild = await session.get(Guild, (resolved_id, resolved_domain))
    member = await session.get(
        GuildMember,
        (resolved_id, resolved_domain, auth.user.id, auth.user.origin_domain),
    )
    if guild is None or member is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
    if resolved_domain == settings.domain:
        return guild_self_moderation_status(member).model_dump(mode="json")
    fallback = guild_self_moderation_status(member)
    # The broad replica projection intentionally has timing but not a reason.
    # Avoid a federation round trip for the overwhelmingly common inactive
    # state and preserve quiet rolling-upgrade behavior.
    if not fallback.timed_out:
        return fallback.model_dump(mode="json")
    fallback = GuildSelfModerationStatus(
        guild_id=fallback.guild_id,
        guild_domain=fallback.guild_domain,
        timed_out=True,
        timeout_until=fallback.timeout_until,
        timeout_indefinite=fallback.timeout_indefinite,
        reason=None,
        details_available=False,
    )
    peer = await session.get(Instance, resolved_domain)
    if peer is None or "member-self-moderation/1" not in (peer.capabilities or []):
        # Rolling upgrades must converge without a user action. Queue only a
        # domain-level discovery refresh (never a user or timeout identifier),
        # deduped for an hour, and return the replica timing immediately.
        discovery_key = f"federation:self-moderation-capability-refresh:{resolved_domain}"
        if await redis.set(discovery_key, "1", ex=60 * 60, nx=True):
            # Lazy import keeps task registration out of the API import graph.
            from app.tasks import federation_self_moderation_capability_refresh

            if not await enqueue_best_effort(
                federation_self_moderation_capability_refresh,
                resolved_domain,
            ):
                await redis.delete(discovery_key)
        return fallback.model_dump(mode="json")
    try:
        upstream = await signed_request(
            session,
            settings,
            "GET",
            resolved_domain,
            f"/_kaede/v1/guilds/{resolved_id}/members/{auth.user.id}/moderation-status",
            max_response_bytes=8 * 1024,
        )
    except (FederationNetworkError, RuntimeError):
        return fallback.model_dump(mode="json")
    if upstream.status_code != 200:
        # Old/mixed-version homes and transient failures must not make the
        # timing projection disappear. The private reason remains unknown.
        return fallback.model_dump(mode="json")
    try:
        status_payload = GuildSelfModerationStatus.model_validate(
            decode_federation_response_json(upstream, max_response_bytes=8 * 1024)
        )
    except (FederationNetworkError, ValidationError):
        return fallback.model_dump(mode="json")
    if (int(status_payload.guild_id), status_payload.guild_domain) != (
        resolved_id,
        resolved_domain,
    ):
        return fallback.model_dump(mode="json")
    if status_payload.timeout_until is not None and status_payload.timeout_until <= datetime.now(
        UTC
    ):
        status_payload = GuildSelfModerationStatus(
            guild_id=status_payload.guild_id,
            guild_domain=status_payload.guild_domain,
            timed_out=False,
        )
    return status_payload.model_dump(mode="json")


@router.get("/{guild_id}/members")
async def list_members(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: EntityRef | None = None,
    query: str | None = Query(default=None, min_length=1, max_length=100),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    resolved_guild_id, resolved_guild_domain = guild_id.resolve(settings.domain)
    guild = await session.scalar(
        select(Guild).where(
            Guild.id == resolved_guild_id,
            Guild.origin_domain == resolved_guild_domain,
        )
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    await require_permissions(session, redis, guild, auth.user, required_permissions("member.list"))
    conditions: list[ColumnElement[bool]] = [
        GuildMember.guild_id == guild.id,
        GuildMember.guild_domain == guild.origin_domain,
    ]
    if after is not None:
        after_id, after_domain = after.resolve(settings.domain)
        conditions.append(
            tuple_(GuildMember.user_id, GuildMember.user_domain) > (after_id, after_domain)
        )
    if query is not None:
        search = query.strip().lstrip("@").strip()
        if search:
            conditions.append(
                or_(
                    User.username.icontains(search, autoescape=True),
                    User.display_name.icontains(search, autoescape=True),
                    User.origin_domain.icontains(search, autoescape=True),
                    GuildMember.nickname.icontains(search, autoescape=True),
                )
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
        member_payload(
            member,
            user,
            roles[(member.user_id, member.user_domain)],
            include_private_authority_state=guild.origin_domain == settings.domain,
        )
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
        if {"timeout_until", "timeout_indefinite"} & payload.model_fields_set:
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
        if {"timeout_until", "timeout_indefinite"} & payload.model_fields_set:
            needed |= Permission.MODERATE_MEMBERS
        await require_permissions(session, redis, guild, auth.user, needed)
    values = payload.model_dump(exclude_unset=True)
    timeout_changed = bool({"timeout_until", "timeout_indefinite"} & payload.model_fields_set)
    timeout = values.get("timeout_until")
    timeout_indefinite = values.get("timeout_indefinite")
    if timeout_indefinite is True and timeout is not None:
        raise HTTPException(status_code=400, detail={"code": "TIMEOUT_MODE_CONFLICT"})
    if timeout_indefinite is True:
        values["timeout_until"] = None
    elif "timeout_until" in payload.model_fields_set and timeout is None:
        values["timeout_indefinite"] = False
    if timeout is not None:
        now = datetime.now(UTC)
        if timeout.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_REQUIRES_TIMEZONE"})
        if timeout > now + timedelta(days=28):
            raise HTTPException(status_code=400, detail={"code": "TIMEOUT_TOO_LONG"})
        if timeout <= now:
            values["timeout_until"] = None
            values["timeout_indefinite"] = False
        else:
            values["timeout_until"] = timeout.astimezone(UTC)
            values["timeout_indefinite"] = False
    if timeout_changed:
        timed_out = bool(values.get("timeout_indefinite", member.timeout_indefinite)) or bool(
            values.get("timeout_until", member.timeout_until)
        )
        values["timeout_reason"] = (
            sanitize_timeout_reason(audit_reason(reason)) if timed_out else None
        )
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
                    "timeout_indefinite": member.timeout_indefinite,
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
    return member_payload(
        member,
        target_user,
        roles,
        include_private_authority_state=True,
    )


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
    revoked_installations = await revoke_installations_for_guild_member(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=user_number,
        user_domain=user_domain,
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
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
    await pause_guild_e2ee_for_membership_change(session, guild)
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
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
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
    expires_at = future_expiry(payload.expires_at, code="BAN_EXPIRY")
    revoked_installations = await revoke_installations_for_guild_member(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=user_number,
        user_domain=user_domain,
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
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
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["guild_id", "guild_domain", "user_id", "user_domain"],
            set_={
                "reason": reason,
                "actor_id": auth.user.id,
                "actor_domain": auth.user.origin_domain,
                "expires_at": expires_at,
            },
        )
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.ban.add",
        {
            "user": {"id": str(user_number), "origin_domain": user_domain},
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
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
        await pause_guild_e2ee_for_membership_change(session, guild)
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
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
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
    conditions: list[ColumnElement[bool]] = [
        Ban.guild_id == guild.id,
        Ban.guild_domain == guild.origin_domain,
        active_expiry(Ban.expires_at),
    ]
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


@router.get("/{guild_id}/instance-bans")
async def list_instance_bans(
    guild_id: EntityRef,
    limit: int = Query(default=50, ge=1, le=1000),
    after: str | None = Query(default=None, max_length=253),
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.list")
    )
    conditions: list[ColumnElement[bool]] = [
        GuildInstanceBan.guild_id == guild.id,
        GuildInstanceBan.guild_domain == guild.origin_domain,
        active_expiry(GuildInstanceBan.expires_at),
    ]
    if after is not None:
        conditions.append(GuildInstanceBan.instance_domain > normalize_domain(after))
    rows = await session.scalars(
        select(GuildInstanceBan)
        .where(*conditions)
        .order_by(GuildInstanceBan.instance_domain)
        .limit(limit)
    )
    return [instance_ban_payload(item) for item in rows]


@router.put("/{guild_id}/instance-bans/{instance_domain}", status_code=204)
async def ban_instance(
    guild_id: EntityRef,
    instance_domain: str,
    payload: InstanceBanCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    header_reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    domain = normalize_domain(instance_domain)
    if domain == settings.domain:
        raise HTTPException(status_code=400, detail={"code": "CANNOT_BAN_HOME_INSTANCE"})
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.put")
    )
    expires_at = future_expiry(payload.expires_at, code="INSTANCE_BAN_EXPIRY")
    reason = audit_reason(header_reason or payload.reason)

    # Serialize this mass membership mutation with other guild administration.
    # The actor must outrank every affected member; a bulk action must not be a
    # hierarchy bypass around the ordinary member-ban endpoint.
    if (guild.owner_id, guild.owner_domain) != (auth.user.id, auth.user.origin_domain):
        actor_role = await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
        actor_rank = role_rank(actor_role)
        affected_member = await session.scalar(
            select(GuildMember.user_id)
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain == domain,
            )
            .limit(1)
        )
        # Every member implicitly has @everyone. An actor whose highest role is
        # also @everyone may not use the bulk endpoint to bypass the ordinary
        # equal-rank member hierarchy rule.
        if affected_member is not None and actor_role.id == guild.id:
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
        blocking_role = await session.scalar(
            select(Role.id)
            .join(
                MemberRole,
                (MemberRole.role_id == Role.id)
                & (MemberRole.role_domain == Role.origin_domain)
                & (MemberRole.guild_id == guild.id)
                & (MemberRole.guild_domain == guild.origin_domain),
            )
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                MemberRole.user_domain == domain,
                or_(
                    Role.position > actor_rank[0],
                    (Role.position == actor_rank[0]) & (Role.id <= -actor_rank[1]),
                ),
            )
            .limit(1)
        )
        if blocking_role is not None:
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})

    # Ensure the composite FK remains valid even for a first-contact domain.
    await session.execute(
        pg_insert(Instance)
        .values(domain=domain, is_self=False, federation_mode="open")
        .on_conflict_do_nothing(index_elements=["domain"])
    )
    await session.execute(
        pg_insert(GuildInstanceBan)
        .values(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            instance_domain=domain,
            reason=reason,
            actor_id=auth.user.id,
            actor_domain=auth.user.origin_domain,
            expires_at=expires_at,
        )
        .on_conflict_do_update(
            index_elements=["guild_id", "guild_domain", "instance_domain"],
            set_={
                "reason": reason,
                "actor_id": auth.user.id,
                "actor_domain": auth.user.origin_domain,
                "created_at": datetime.now(UTC),
                "expires_at": expires_at,
            },
        )
    )
    revoked_installations = await revoke_installations_for_guild_instance(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        instance_domain=domain,
    )
    deleted_role_refs = await cleanup_installation_roles(
        session,
        settings,
        guild,
        auth.user,
        revoked_installations,
    )
    removed_refs = list(
        await session.execute(
            delete(GuildMember)
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain == domain,
            )
            .returning(GuildMember.user_id, GuildMember.user_domain)
        )
    )
    if removed_refs:
        await pause_guild_e2ee_for_membership_change(session, guild)
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.members.origin.remove",
        {"origin_domain": domain},
        snapshot_required=True,
    )
    await queue_guild_instance_access_revocation(
        session,
        settings,
        guild,
        instance_domain=domain,
        reason="instance_banned",
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        25,
        target_type="instance",
        target_ref={"domain": domain},
        reason=reason,
        changes=[
            {"key": "expires_at", "old_value": None, "new_value": str(expires_at)},
            {"key": "members_removed", "old_value": None, "new_value": len(removed_refs)},
            {
                "key": "bot_installations_revoked",
                "old_value": None,
                "new_value": len(revoked_installations),
            },
            {
                "key": "bot_roles_deleted",
                "old_value": None,
                "new_value": len(deleted_role_refs),
            },
        ],
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
    for user_id, user_domain in removed_refs:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_REMOVE",
            {
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
                "user_id": str(user_id),
                "user_domain": user_domain,
            },
        )
    return Response(status_code=204)


@router.delete("/{guild_id}/instance-bans/{instance_domain}", status_code=204)
async def remove_instance_ban(
    guild_id: EntityRef,
    instance_domain: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: str | None = Header(default=None, alias="X-Audit-Log-Reason"),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    domain = normalize_domain(instance_domain)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("instance_ban.remove")
    )
    removed = await session.scalar(
        delete(GuildInstanceBan)
        .where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == domain,
        )
        .returning(GuildInstanceBan.instance_domain)
    )
    if removed is not None:
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            26,
            target_type="instance",
            target_ref={"domain": domain},
            reason=audit_reason(reason),
        )
        await session.commit()
    return Response(status_code=204)


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
