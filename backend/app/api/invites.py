from __future__ import annotations

import asyncio
import secrets
import string
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import or_, select
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
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import guild_payload, user_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import InviteCreate
from app.core.errors import parse_upstream_error
from app.core.permission_contract import required_permissions
from app.core.proxy import resolve_client_ip
from app.core.rate_limits import (
    CLIENT_RATE_LIMITS,
    enforce_client_rate_limit,
    enforce_keyed_rate_limit,
)
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference, validate_snowflake
from app.db.models import Ban, Channel, Guild, GuildInstanceBan, GuildMember, Invite, User
from app.federation.client import signed_request
from app.federation.guilds import (
    apply_guild_snapshot,
    begin_remote_guild_join,
    fetch_guild_snapshot,
)
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import (
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    mark_replica_capacity_paused,
    mark_replica_quota_paused,
)
from app.federation.replication import profile_from_user

router = APIRouter(prefix="/api/v1", tags=["invites"])
ALPHABET = string.ascii_letters + string.digits
REMOTE_INVITE_PREVIEW_CONCURRENCY = 16
remote_invite_preview_slots = asyncio.Semaphore(REMOTE_INVITE_PREVIEW_CONCURRENCY)
log = structlog.get_logger()


async def publish_existing_replica_status(
    session: AsyncSession,
    redis: Redis,
    guild_id: int,
    guild_domain: str,
) -> None:
    """Project a committed replica pause without hiding the API's 507 response."""

    guild = await session.get(Guild, (guild_id, guild_domain), populate_existing=True)
    if guild is None:
        # A first-time join rolls the snapshot back completely. There is no
        # navigation entry to update; the initiating request carries the
        # actionable capacity response instead.
        return
    try:
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_UPDATE",
            guild_payload(guild),
        )
    except Exception:
        log.exception(
            "remote_guild_capacity_status_publish_failed",
            guild_id=str(guild.id),
            guild_domain=guild.origin_domain,
        )


async def new_invite_code(session: AsyncSession) -> str:
    for _ in range(10):
        code = "".join(secrets.choice(ALPHABET) for _ in range(8))
        if await session.get(Invite, code) is None:
            return code
    raise RuntimeError("could not allocate an invite code")


def invite_payload(invite: Invite, guild: Guild) -> dict[str, object]:
    return {
        "code": invite.code,
        "guild": guild_payload(guild),
        "channel_id": str(invite.channel_id) if invite.channel_id is not None else None,
        "uses": invite.uses,
        "max_uses": invite.max_uses,
        "expires_at": invite.expires_at.isoformat() if invite.expires_at else None,
        "created_at": invite.created_at.isoformat(),
        "revoked_at": invite.revoked_at.isoformat() if invite.revoked_at else None,
    }


@router.post("/guilds/{guild_id}/invites", status_code=status.HTTP_201_CREATED)
async def create_invite(
    guild_id: EntityRef,
    payload: InviteCreate,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_create"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel_id = payload.channel_id
    if channel_id is not None:
        channel = await session.scalar(
            select(Channel).where(
                Channel.id == channel_id,
                Channel.origin_domain == settings.domain,
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
            )
        )
        if channel is None:
            raise HTTPException(status_code=404, detail={"code": "CHANNEL_NOT_FOUND"})
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("invite.create"),
            channel=channel,
        )
    else:
        await require_permissions(
            session, redis, guild, auth.user, required_permissions("invite.create")
        )
    invite = Invite(
        code=await new_invite_code(session),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        channel_id=channel_id,
        channel_domain=settings.domain if channel_id is not None else None,
        inviter_id=auth.user.id,
        inviter_domain=auth.user.origin_domain,
        max_uses=payload.max_uses,
        expires_at=(
            datetime.now(UTC) + timedelta(seconds=payload.max_age_seconds)
            if payload.max_age_seconds is not None
            else None
        ),
    )
    session.add(invite)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        40,
        target_type="invite",
        target_ref={"code": invite.code},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    return invite_payload(invite, guild)


@router.get("/guilds/{guild_id}/invites")
async def list_invites(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.invite.list")
    )
    invites = list(
        await session.scalars(
            select(Invite)
            .where(
                Invite.guild_id == guild.id,
                Invite.guild_domain == guild.origin_domain,
                Invite.revoked_at.is_(None),
            )
            .order_by(Invite.created_at.desc(), Invite.code)
        )
    )
    return [invite_payload(invite, guild) for invite in invites]


@router.delete("/invites/{code}", status_code=204)
async def revoke_invite(
    code: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    invite = await session.scalar(select(Invite).where(Invite.code == code).with_for_update())
    if invite is None or invite.revoked_at is not None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    # Invite acceptance also takes locks in invite -> guild order. Reuse that
    # order here so revoke and accept cannot deadlock each other.
    guild = await local_guild(
        session,
        settings,
        EntityReference(invite.guild_id),
        for_update=True,
    )
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.invite.revoke")
    )
    invite.revoked_at = datetime.now(UTC)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        42,
        target_type="invite",
        target_ref={"code": invite.code},
    )
    await session.commit()
    await wake_queued_guild_federation(guild)
    return Response(status_code=204)


@router.get("/invites/{code}")
async def get_invite(
    code: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if len(code) > 320:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    source_ip = resolve_client_ip(
        supplied_secret=request.headers.get("X-Kaede-Proxy-Secret"),
        configured_secret=(
            settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
        ),
        forwarded_for=request.headers.get("X-Forwarded-For"),
        direct_host=request.client.host if request.client is not None else None,
    )
    await enforce_keyed_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_preview"],
        identity=source_ip,
    )
    remote_code, separator, raw_domain = code.rpartition("@")
    if separator:
        try:
            domain = normalize_domain(raw_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"}) from None
        if domain == settings.domain:
            code = remote_code
        else:
            await enforce_keyed_rate_limit(
                redis,
                response,
                CLIENT_RATE_LIMITS["invite_preview_destination"],
                identity=domain,
            )
            await enforce_keyed_rate_limit(
                redis,
                response,
                CLIENT_RATE_LIMITS["invite_preview_global"],
                identity="outbound",
            )
            try:
                async with asyncio.timeout(0.1):
                    await remote_invite_preview_slots.acquire()
            except TimeoutError:
                raise HTTPException(
                    status_code=503,
                    detail={"code": "FEDERATION_INVITE_PREVIEW_BUSY"},
                    headers={"Retry-After": "1"},
                ) from None
            try:
                try:
                    resolved = await signed_request(
                        session,
                        settings,
                        "POST",
                        domain,
                        "/_kaede/v1/invites/resolve",
                        payload={"code": remote_code},
                    )
                except FederationInstanceQuotaExceeded as exc:
                    raise HTTPException(
                        status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                        detail=exc.detail(),
                    ) from exc
            finally:
                remote_invite_preview_slots.release()
            if resolved.status_code == 404:
                raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
            if resolved.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"},
                )
            try:
                payload = decode_federation_response_json(resolved)
                if not isinstance(payload, dict) or not isinstance(payload.get("guild"), dict):
                    raise TypeError
            except (FederationNetworkError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502,
                    detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"},
                ) from None
            return {**payload, "code": code, "origin_domain": domain}
    invite = await session.get(Invite, code)
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or (invite.expires_at is not None and invite.expires_at <= now)
        or (invite.max_uses is not None and invite.uses >= invite.max_uses)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await session.scalar(
        select(Guild).where(Guild.id == invite.guild_id, Guild.origin_domain == invite.guild_domain)
    )
    if guild is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    return invite_payload(invite, guild)


@router.post("/invites/{code}")
async def accept_invite(
    code: str,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if len(code) > 320:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["invite_accept"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    remote_code, separator, raw_domain = code.rpartition("@")
    if separator:
        try:
            domain = normalize_domain(raw_domain)
        except FederationNetworkError:
            raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"}) from None
        if domain == settings.domain:
            code = remote_code
        else:
            try:
                resolved = await signed_request(
                    session,
                    settings,
                    "POST",
                    domain,
                    "/_kaede/v1/invites/resolve",
                    payload={"code": remote_code},
                )
            except FederationInstanceQuotaExceeded as exc:
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from exc
            if resolved.status_code == 404:
                raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
            if resolved.status_code != 200:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"}
                )
            try:
                resolved_payload = decode_federation_response_json(resolved)
                if not isinstance(resolved_payload, dict) or not isinstance(
                    resolved_payload.get("guild"), dict
                ):
                    raise TypeError
                resolved_guild_id = validate_snowflake(resolved_payload["guild"]["id"])
            except (FederationNetworkError, KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_INVITE_RESOLVE_FAILED"}
                ) from None
            join_intent_started = await begin_remote_guild_join(
                session,
                settings,
                guild_id=resolved_guild_id,
                guild_domain=domain,
                user_id=auth.user.id,
                user_domain=auth.user.origin_domain,
            )
            if join_intent_started:
                # Make the explicit local intent visible before the remote
                # authority can emit an add event. It remains as a fail-closed
                # pending marker if the network request or snapshot fails.
                await session.commit()
            try:
                joined = await signed_request(
                    session,
                    settings,
                    "POST",
                    domain,
                    f"/_kaede/v1/guilds/{resolved_guild_id}/join",
                    payload={"code": remote_code, "user": profile_from_user(auth.user)},
                )
            except FederationInstanceQuotaExceeded as exc:
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from exc
            if joined.status_code in {403, 404}:
                try:
                    error_body = decode_federation_response_json(joined)
                except FederationNetworkError:
                    error_body = None
                detail = parse_upstream_error(error_body, "INVITE_NOT_FOUND")
                raise HTTPException(status_code=joined.status_code, detail=detail)
            if joined.status_code == status.HTTP_507_INSUFFICIENT_STORAGE:
                try:
                    error_body = decode_federation_response_json(joined)
                except FederationNetworkError:
                    error_body = None
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=parse_upstream_error(error_body, "FEDERATION_GUILD_JOIN_FAILED"),
                )
            if joined.status_code != 200:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                )
            try:
                joined_payload = decode_federation_response_json(joined)
                if not isinstance(joined_payload, dict) or not isinstance(
                    joined_payload.get("guild"), dict
                ):
                    raise TypeError
                guild_id = validate_snowflake(joined_payload["guild"]["id"])
            except (FederationNetworkError, KeyError, TypeError, ValueError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                ) from None
            if guild_id != resolved_guild_id:
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_GUILD_JOIN_FAILED"}
                )
            try:
                async with asyncio.timeout(45):
                    snapshot = await fetch_guild_snapshot(session, settings, domain, guild_id)
                    guild = await apply_guild_snapshot(
                        session,
                        settings,
                        snapshot,
                        expected_origin=domain,
                        expected_guild_id=guild_id,
                        required_member=(auth.user.id, auth.user.origin_domain),
                    )
            except TimeoutError:
                raise HTTPException(
                    status_code=504, detail={"code": "FEDERATION_GUILD_JOIN_TIMEOUT"}
                ) from None
            except FederationReplicaQuotaExceeded as exc:
                # Snapshot application is atomic. Roll back its over-limit
                # rows, then preserve a clear pause marker when this was a
                # refresh of an existing replica. A first-time join has no
                # replica left after rollback, but still receives the precise
                # operator-actionable error instead of a generic 502.
                await session.rollback()
                paused_existing_replica = await mark_replica_quota_paused(
                    session,
                    settings,
                    guild_id,
                    domain,
                    exc,
                )
                await session.commit()
                if paused_existing_replica:
                    await publish_existing_replica_status(
                        session,
                        redis,
                        guild_id,
                        domain,
                    )
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail={"code": REPLICA_QUOTA_ERROR_CODE},
                ) from None
            except (FederationIdentityQuotaExceeded, FederationInstanceQuotaExceeded) as exc:
                await session.rollback()
                paused_existing_replica = await mark_replica_capacity_paused(
                    session,
                    settings,
                    guild_id,
                    domain,
                    error_code=exc.code,
                    internal_error=str(exc),
                )
                await session.commit()
                if paused_existing_replica:
                    await publish_existing_replica_status(
                        session,
                        redis,
                        guild_id,
                        domain,
                    )
                raise HTTPException(
                    status_code=status.HTTP_507_INSUFFICIENT_STORAGE,
                    detail=exc.detail(),
                ) from None
            except (ValueError, RuntimeError):
                raise HTTPException(
                    status_code=502, detail={"code": "FEDERATION_SNAPSHOT_FAILED"}
                ) from None
            await session.commit()
            # Snapshot application performs bulk upserts, and PostgreSQL-managed
            # timestamps can be expired at commit.  Refresh before handing this
            # object to synchronous payload/topic helpers.
            await session.refresh(guild)
            await wake_queued_guild_federation(guild)
            from app.tasks import federation_history_sync

            await enqueue_best_effort(
                federation_history_sync,
                guild.id,
                guild.origin_domain,
                auth.user.id,
            )
            result = guild_payload(guild)
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_CREATE",
                result,
            )
            await publish_dispatch(
                redis,
                user_topic(auth.user.origin_domain, auth.user.id),
                "GUILD_CREATE",
                result,
            )
            return result
    invite = await session.scalar(select(Invite).where(Invite.code == code).with_for_update())
    now = datetime.now(UTC)
    if (
        invite is None
        or invite.revoked_at is not None
        or (invite.expires_at is not None and invite.expires_at <= now)
        or (invite.max_uses is not None and invite.uses >= invite.max_uses)
    ):
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = await local_guild(session, settings, EntityReference(invite.guild_id))
    locked_guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
    )
    if locked_guild is None:
        raise HTTPException(status_code=404, detail={"code": "INVITE_NOT_FOUND"})
    guild = locked_guild
    banned = await session.scalar(
        select(Ban).where(
            Ban.guild_id == guild.id,
            Ban.guild_domain == guild.origin_domain,
            Ban.user_id == auth.user.id,
            Ban.user_domain == auth.user.origin_domain,
            or_(Ban.expires_at.is_(None), Ban.expires_at > now),
        )
    )
    if banned is not None:
        raise HTTPException(status_code=403, detail={"code": "BANNED_FROM_GUILD"})
    instance_banned = await session.scalar(
        select(GuildInstanceBan.instance_domain).where(
            GuildInstanceBan.guild_id == guild.id,
            GuildInstanceBan.guild_domain == guild.origin_domain,
            GuildInstanceBan.instance_domain == auth.user.origin_domain,
            or_(
                GuildInstanceBan.expires_at.is_(None),
                GuildInstanceBan.expires_at > now,
            ),
        )
    )
    if instance_banned is not None:
        raise HTTPException(status_code=403, detail={"code": "INSTANCE_BANNED_FROM_GUILD"})
    member = await session.scalar(
        select(GuildMember).where(
            GuildMember.guild_id == guild.id,
            GuildMember.guild_domain == guild.origin_domain,
            GuildMember.user_id == auth.user.id,
            GuildMember.user_domain == auth.user.origin_domain,
        )
    )
    if member is None:
        member = GuildMember(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=auth.user.id,
            user_domain=auth.user.origin_domain,
            joined_at=now,
        )
        session.add(member)
        invite.uses += 1
        owner = await session.get(User, (guild.owner_id, guild.owner_domain))
        if owner is None or not owner.is_local:
            raise RuntimeError("local guild owner is unavailable")
        await queue_guild_mutation(
            session,
            settings,
            guild,
            owner,
            "guild.member.add",
            {"user": profile_from_user(auth.user), "joined_at": now.isoformat()},
        )
        await session.commit()
        # Guild sequence assignment updates the server-managed resource
        # version; reload it before synchronous dispatch serialization.
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_ADD",
            {"guild_id": str(guild.id), "user": user_payload(auth.user)},
        )
        await publish_dispatch(
            redis,
            user_topic(auth.user.origin_domain, auth.user.id),
            "GUILD_CREATE",
            guild_payload(guild),
        )
    else:
        # expire_on_commit=False keeps the locked Guild usable for the
        # idempotent response. AsyncSession.rollback() would expire it and make
        # the synchronous payload serializer attempt forbidden implicit I/O.
        await session.commit()
    return guild_payload(guild)
