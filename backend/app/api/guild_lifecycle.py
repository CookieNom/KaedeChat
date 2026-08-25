from __future__ import annotations

from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.management import require_current_version
from app.bots.installations import (
    cleanup_installation_roles,
    publish_deleted_installation_roles,
    revoke_installations_for_guild_member,
)
from app.chat.audit import add_audit_entry
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch, user_topic
from app.chat.guild_revision import (
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.payloads import guild_payload
from app.chat.schemas import GuildOwnershipTransfer
from app.chat.thread_membership import (
    cleanup_guild_member_threads,
    publish_guild_thread_member_cleanup,
)
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildMember,
    MediaTombstoneDestination,
    Message,
    ReadState,
    RemoteMediaCache,
    RoomFederationRecipient,
    User,
)
from app.federation.events import build_envelope, queue_event
from app.federation.guilds import apply_guild_access_revocation, mark_remote_guild_departed
from app.federation.terminal_rooms import lock_terminal_room, queue_terminal_room_deletion
from app.media.tombstones import (
    historical_attachment_destinations,
    lock_media_tombstone_ref,
    queue_terminal_attachment_tombstone,
)

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

    deleted_role_refs: list[tuple[int, str]] = []
    removed_thread_members = []
    e2ee_policy_channels: list[Channel] = []
    if guild.origin_domain != settings.domain:
        remote_guild_domain = guild.origin_domain
        await mark_remote_guild_departed(
            session,
            settings,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=actor_id,
            user_domain=actor_domain,
        )
        leave_request = await build_envelope(
            session,
            settings,
            "guild.leave.request",
            auth.user,
            {"user": {"id": str(actor_id), "domain": actor_domain}},
            context={
                "guild_id": str(guild.id),
                "guild_domain": guild.origin_domain,
            },
        )
        await queue_event(session, settings, remote_guild_domain, leave_request)
        await apply_guild_access_revocation(
            session,
            settings,
            guild,
            user_id=actor_id,
            user_domain=actor_domain,
        )
    else:
        removed_thread_members = await cleanup_guild_member_threads(
            session,
            settings,
            guild,
            auth.user,
            [(actor_id, actor_domain)],
        )
        revoked_installations = await revoke_installations_for_guild_member(
            session,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=actor_id,
            user_domain=actor_domain,
        )
        deleted_role_refs = await cleanup_installation_roles(
            session,
            settings,
            guild,
            auth.user,
            revoked_installations,
        )
        await session.delete(member)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.remove",
            {"user": {"id": str(actor_id), "origin_domain": actor_domain}},
            snapshot_required=True,
            e2ee_policy_channels=e2ee_policy_channels,
        )

    await session.commit()
    if guild.origin_domain == settings.domain:
        await wake_queued_guild_federation(guild)
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_deleted_installation_roles(redis, guild, deleted_role_refs)
        await publish_guild_thread_member_cleanup(redis, guild, removed_thread_members)
    else:
        from app.tasks import federation_deliver

        await enqueue_best_effort(federation_deliver, guild.origin_domain)
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
    if (
        target is None
        or not target.is_local
        or target.account_type != "human"
        or target.disabled_at is not None
        or member is None
    ):
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
) -> tuple[list[tuple[int, str]], set[str], set[str]]:
    await lock_terminal_room(session, "guild", guild.id, guild.origin_domain)
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-remote-media-cache-budget", 0))
        )
    )
    channel_refs = select(Channel.id, Channel.origin_domain).where(
        Channel.guild_id == guild.id,
        Channel.guild_domain == guild.origin_domain,
    )
    message_refs = select(Message.id, Message.origin_domain).where(
        tuple_(Message.channel_id, Message.channel_domain).in_(channel_refs)
    )
    emoji_asset_prefix = f"emoji:{guild.origin_domain}:"
    sticker_asset_prefix = f"sticker:{guild.origin_domain}:"
    routed_refs = select(
        MediaTombstoneDestination.attachment_id,
        MediaTombstoneDestination.attachment_domain,
    ).where(
        MediaTombstoneDestination.room_kind == "guild",
        MediaTombstoneDestination.room_id == guild.id,
        MediaTombstoneDestination.room_domain == guild.origin_domain,
    )
    attachment_refs = list(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain).where(
                    or_(
                        tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs),
                        tuple_(Attachment.id, Attachment.origin_domain).in_(routed_refs),
                        Attachment.asset_binding.in_(
                            (
                                f"guild:{guild.origin_domain}:{guild.id}:icon",
                                f"guild:{guild.origin_domain}:{guild.id}:banner",
                            )
                        ),
                        Attachment.asset_binding.startswith(emoji_asset_prefix),
                        Attachment.asset_binding.startswith(sticker_asset_prefix),
                    )
                )
            )
        ).tuples()
    )
    for attachment_id, attachment_domain in sorted(
        attachment_refs, key=lambda ref: (ref[1], ref[0])
    ):
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    attachments = list(
        await session.scalars(
            select(Attachment)
            .where(tuple_(Attachment.id, Attachment.origin_domain).in_(attachment_refs))
            .order_by(Attachment.origin_domain, Attachment.id)
            .with_for_update()
        )
    )
    local_purge: list[tuple[int, str]] = []
    delivery_wakes: set[str] = set()
    room_destinations: set[str] = set()
    for route in list(
        await session.scalars(
            select(MediaTombstoneDestination).where(
                MediaTombstoneDestination.room_kind == "guild",
                MediaTombstoneDestination.room_id == guild.id,
                MediaTombstoneDestination.room_domain == guild.origin_domain,
            )
        )
    ):
        room_destinations.update({route.attachment_domain, route.destination_domain})
    room_destinations.update(
        await session.scalars(
            select(RoomFederationRecipient.destination_domain).where(
                RoomFederationRecipient.room_kind == "guild",
                RoomFederationRecipient.room_id == guild.id,
                RoomFederationRecipient.room_domain == guild.origin_domain,
            )
        )
    )
    remote_refs: list[tuple[int, str]] = []
    for attachment in attachments:
        room_destinations.add(attachment.origin_domain)
        room_destinations.update(await historical_attachment_destinations(session, attachment))
        if attachment.origin_domain == settings.domain:
            delivery_wakes.update(
                await queue_terminal_attachment_tombstone(
                    session,
                    settings,
                    attachment,
                    force_authoritative=True,
                )
            )
            attachment.message_id = None
            attachment.message_domain = None
            attachment.asset_binding = None
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
    room_destinations.discard(settings.domain)
    return local_purge, delivery_wakes, room_destinations


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
    (
        attachment_purges,
        media_destinations,
        historical_room_destinations,
    ) = await _prepare_guild_content_deletion(session, settings, guild)
    terminal_room_destinations = historical_room_destinations | {
        member.origin_domain for member in members if member.origin_domain != settings.domain
    }
    terminal_room_wakes = await queue_terminal_room_deletion(
        session,
        settings,
        room_kind="guild",
        room_id=guild.id,
        room_domain=guild.origin_domain,
        actor=auth.user,
        event_type="guild.instance_access.revoked",
        content={"reason": "guild_deleted"},
        context={"guild_id": str(guild.id), "guild_domain": guild.origin_domain},
        destinations=terminal_room_destinations,
    )
    await session.delete(guild)
    await session.commit()
    await wake_queued_guild_federation(guild)
    from app.tasks import federation_deliver

    for destination in sorted(media_destinations | terminal_room_wakes):
        await enqueue_best_effort(federation_deliver, destination)

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
