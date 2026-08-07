from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.management import require_current_version
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    queue_guild_access_revocation,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import guild_payload
from app.chat.schemas import GuildOwnershipTransfer
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildMember,
    Message,
    ReadState,
    RemoteMediaCache,
    User,
)
from app.federation.client import signed_request
from app.federation.guilds import apply_guild_access_revocation

router = APIRouter(prefix="/api/v1/guilds", tags=["guild-lifecycle"])
log = structlog.get_logger()


async def _locked_guild(session: AsyncSession, settings: Settings, guild_ref: EntityRef) -> Guild:
    guild_id, guild_domain = guild_ref.resolve(settings.domain)
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
        .with_for_update()
    )
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    return guild


def _is_owner(guild: Guild, user: User) -> bool:
    return (guild.owner_id, guild.owner_domain) == (user.id, user.origin_domain)


async def _publish_guild_removed(
    redis: Redis,
    guild: Guild,
    *,
    user_id: int,
    user_domain: str,
) -> None:
    await publish_dispatch(
        redis,
        user_topic(user_domain, user_id),
        "GUILD_DELETE",
        {"id": str(guild.id), "origin_domain": guild.origin_domain},
    )
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


@router.delete("/{guild_id}/members/@me", status_code=204)
async def leave_guild(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    actor_id = auth.user.id
    actor_domain = auth.user.origin_domain
    guild = await _locked_guild(session, settings, guild_id)
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, actor_id, actor_domain),
    )
    if member is None:
        raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
    if _is_owner(guild, auth.user):
        raise HTTPException(
            status_code=409,
            detail={"code": "OWNER_MUST_TRANSFER_OR_DELETE_GUILD"},
        )

    if guild.origin_domain != settings.domain:
        # Do not hold a database lock while waiting on another instance.
        remote_guild_id = guild.id
        remote_guild_domain = guild.origin_domain
        await session.rollback()
        response = await signed_request(
            session,
            settings,
            "DELETE",
            remote_guild_domain,
            f"/_kaede/v1/guilds/{remote_guild_id}/members/@me",
            payload={"user": {"id": str(actor_id), "domain": actor_domain}},
        )
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail={"code": "NOT_A_GUILD_MEMBER"})
        if response.status_code != 204:
            raise HTTPException(status_code=502, detail={"code": "FEDERATION_GUILD_LEAVE_FAILED"})
        guild = await _locked_guild(session, settings, guild_id)
        await apply_guild_access_revocation(
            session,
            settings,
            guild,
            user_id=actor_id,
            user_domain=actor_domain,
        )
    else:
        await session.delete(member)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.remove",
            {"user": {"id": str(actor_id), "origin_domain": actor_domain}},
            snapshot_required=True,
        )

    await session.commit()
    await wake_queued_guild_federation(guild)
    await _publish_guild_removed(
        redis,
        guild,
        user_id=actor_id,
        user_domain=actor_domain,
    )
    return Response(status_code=204)


@router.put("/{guild_id}/owner")
async def transfer_guild_ownership(
    guild_id: EntityRef,
    payload: GuildOwnershipTransfer,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    guild = await _locked_guild(session, settings, guild_id)
    if guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    require_current_version(guild.updated_at, if_match)
    if not _is_owner(guild, auth.user):
        raise HTTPException(status_code=403, detail={"code": "GUILD_OWNER_REQUIRED"})
    target_id, target_domain = payload.owner_id.resolve(settings.domain)
    if target_domain != settings.domain:
        raise HTTPException(
            status_code=400,
            detail={"code": "OWNER_TRANSFER_REQUIRES_LOCAL_MEMBER"},
        )
    if (target_id, target_domain) == (guild.owner_id, guild.owner_domain):
        raise HTTPException(status_code=409, detail={"code": "ALREADY_GUILD_OWNER"})
    target = await session.get(User, (target_id, target_domain))
    member = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, target_id, target_domain),
    )
    if target is None or not target.is_local or member is None:
        raise HTTPException(status_code=404, detail={"code": "GUILD_MEMBER_NOT_FOUND"})

    guild.permission_generation += 1
    transferred = {
        **guild_payload(guild),
        "owner_id": str(target_id),
        "owner_domain": target_domain,
    }
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.update",
        {"guild": transferred},
        snapshot_required=True,
    )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        27,
        target_type="user",
        target_ref={"id": str(target_id), "origin_domain": target_domain},
        changes=[
            {
                "key": "owner",
                "old_value": {
                    "id": str(guild.owner_id),
                    "origin_domain": guild.owner_domain,
                },
                "new_value": {"id": str(target_id), "origin_domain": target_domain},
            }
        ],
    )
    guild.owner_id = target_id
    guild.owner_domain = target_domain
    await session.commit()
    await session.refresh(guild)
    await wake_queued_guild_federation(guild)
    rendered = guild_payload(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_UPDATE",
        rendered,
    )
    return rendered


async def _prepare_guild_content_deletion(
    session: AsyncSession, settings: Settings, guild: Guild
) -> list[tuple[int, str]]:
    channel_refs = select(Channel.id, Channel.origin_domain).where(
        Channel.guild_id == guild.id,
        Channel.guild_domain == guild.origin_domain,
    )
    message_refs = select(Message.id, Message.origin_domain).where(
        tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs)
    )
    attachments = list(
        await session.scalars(
            select(Attachment).where(
                tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs)
            )
        )
    )
    local_purge: list[tuple[int, str]] = []
    remote_refs: list[tuple[int, str]] = []
    for attachment in attachments:
        if attachment.origin_domain == settings.domain:
            attachment.message_id = None
            attachment.message_domain = None
            local_purge.append((attachment.id, attachment.origin_domain))
        else:
            remote_refs.append((attachment.id, attachment.origin_domain))
    if remote_refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(RemoteMediaCache.attachment_id, RemoteMediaCache.origin_domain).in_(
                    remote_refs
                )
            )
            .values(expires_at=datetime.now(UTC))
        )
    await session.execute(
        update(ReadState)
        .where(tuple_(ReadState.channel_id, ReadState.channel_domain).in_(channel_refs))
        .values(last_message_id=None, last_message_domain=None, mention_count=0)
    )
    await session.execute(
        update(Channel)
        .where(Channel.guild_id == guild.id, Channel.guild_domain == guild.origin_domain)
        .values(last_message_id=None, last_message_domain=None)
    )
    await session.execute(
        update(Message)
        .where(
            tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs),
            Message.referenced_message_id.is_not(None),
        )
        .values(referenced_message_id=None, referenced_message_domain=None)
    )
    await session.flush()
    await session.execute(
        delete(Message).where(tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs))
    )
    return local_purge


@router.delete("/{guild_id}", status_code=204)
async def delete_guild(
    guild_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    guild = await _locked_guild(session, settings, guild_id)
    if guild.origin_domain != settings.domain:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    require_current_version(guild.updated_at, if_match)
    if not _is_owner(guild, auth.user):
        raise HTTPException(status_code=403, detail={"code": "GUILD_OWNER_REQUIRED"})

    members = list(
        await session.scalars(
            select(User)
            .join(
                GuildMember,
                (GuildMember.user_id == User.id) & (GuildMember.user_domain == User.origin_domain),
            )
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
            )
        )
    )
    for member in members:
        if member.origin_domain != settings.domain:
            await queue_guild_access_revocation(
                session,
                settings,
                guild,
                user_id=member.id,
                user_domain=member.origin_domain,
                reason="guild_deleted",
            )
    attachment_purges = await _prepare_guild_content_deletion(session, settings, guild)
    await session.delete(guild)
    await session.commit()
    await wake_queued_guild_federation(guild)

    for member in members:
        if member.origin_domain == settings.domain:
            await publish_dispatch(
                redis,
                user_topic(member.origin_domain, member.id),
                "GUILD_DELETE",
                {"id": str(guild.id), "origin_domain": guild.origin_domain},
            )
    from app.tasks import media_local_purge

    for attachment_id, origin_domain in attachment_purges:
        await enqueue_best_effort(media_local_purge, attachment_id, origin_domain)
    log.info(
        "guild_deleted",
        guild_id=str(guild.id),
        guild_domain=guild.origin_domain,
        actor_id=str(auth.user.id),
    )
    return Response(status_code=204)
