from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.api.guilds import (
    copy_overwrites,
    guild_channel,
    local_guild,
    overwrite_source_channel,
)
from app.chat.audit import add_audit_entry
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import (
    guild_role,
    highest_role,
    require_can_manage_member,
    require_can_manage_role,
    role_rank,
)
from app.chat.payloads import channel_payload, guild_payload, member_payload, role_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import (
    ChannelPositionBatch,
    ChannelUpdate,
    GuildUpdate,
    RolePositionBatch,
    RoleUpdate,
)
from app.core.permission_contract import required_permissions
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef, EntityReference
from app.db.models import Channel, ChannelOverwrite, GuildMember, MemberRole, Message, Role, User

router = APIRouter(prefix="/api/v1/guilds", tags=["guild-management"])


async def render_member_update(session: AsyncSession, member: GuildMember) -> dict[str, object]:
    user = await session.get(User, (member.user_id, member.user_domain))
    if user is None:
        raise RuntimeError("guild member user disappeared")
    role_ids = list(
        await session.scalars(
            select(MemberRole.role_id).where(
                MemberRole.guild_id == member.guild_id,
                MemberRole.guild_domain == member.guild_domain,
                MemberRole.user_id == member.user_id,
                MemberRole.user_domain == member.user_domain,
            )
        )
    )
    return member_payload(member, user, role_ids)


def require_current_version(updated_at: datetime, if_match: str | None) -> None:
    if if_match is None:
        raise HTTPException(
            status_code=428,
            detail={"code": "SETTINGS_VERSION_REQUIRED", "current_version": updated_at.isoformat()},
        )
    current = updated_at.isoformat()
    if if_match.strip('"') != current:
        raise HTTPException(
            status_code=412,
            detail={"code": "SETTINGS_VERSION_CONFLICT", "current_version": current},
        )


@router.patch("/{guild_id}")
async def update_guild(
    guild_id: EntityRef,
    payload: GuildUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    require_current_version(guild.updated_at, if_match)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("guild.update")
    )
    changes: list[dict[str, object]] = []
    history_policy_changed = False
    for field, value in payload.model_dump(exclude_unset=True).items():
        old = getattr(guild, field)
        if old != value:
            changes.append({"key": field, "old_value": old, "new_value": value})
            setattr(guild, field, value)
            history_policy_changed = history_policy_changed or field == "federated_history_policy"
    if changes:
        if history_policy_changed:
            guild.history_policy_generation += 1
            from app.federation.history import revoke_history_exports

            await revoke_history_exports(session, guild)
            # The revocation query autoflushes the guild update. PostgreSQL's
            # server-managed ``updated_at`` is then expired, so refresh it
            # before the synchronous event serializer reads the version.
            await session.refresh(guild)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.update",
            {"guild": guild_payload(guild)},
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            1,
            target_type="guild",
            target_ref={"id": str(guild.id)},
            changes=changes,
        )
        await session.commit()
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis, guild_topic(guild.origin_domain, guild.id), "GUILD_UPDATE", guild_payload(guild)
        )
    return guild_payload(guild)


@router.patch("/{guild_id}/channels/{channel_id}")
async def update_channel(
    guild_id: EntityRef,
    channel_id: EntityRef,
    payload: ChannelUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    require_current_version(channel.updated_at, if_match)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.update"),
        channel=channel,
    )
    original_parent_id = channel.parent_id
    original_permissions_synced = channel.permissions_synced
    values = payload.model_dump(exclude_unset=True)
    sync_permissions = bool(values.pop("sync_permissions", False))
    permission_state_changed = False
    if "parent_id" in values:
        parent_id = values["parent_id"]
        if parent_id == channel.id:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})
        if parent_id is not None:
            if channel.type == 4:
                raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})
            parent = await guild_channel(
                session,
                settings,
                EntityReference(guild.id),
                EntityReference(int(parent_id)),
            )
            if parent.type != 4:
                raise HTTPException(status_code=400, detail={"code": "PARENT_NOT_CATEGORY"})
        values["parent_domain"] = settings.domain if parent_id is not None else None
        if parent_id != channel.parent_id and channel.permissions_synced:
            source = await overwrite_source_channel(session, channel)
            await copy_overwrites(session, source, channel)
            channel.permissions_synced = False
            permission_state_changed = True
    target_parent_id = values.get("parent_id", channel.parent_id)
    if sync_permissions:
        if target_parent_id is None or channel.type == 4:
            raise HTTPException(status_code=400, detail={"code": "CHANNEL_HAS_NO_CATEGORY"})
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("channel.permissions.sync"),
            channel=channel,
        )
        await session.execute(
            delete(ChannelOverwrite).where(
                ChannelOverwrite.channel_id == channel.id,
                ChannelOverwrite.channel_domain == channel.origin_domain,
            )
        )
        permission_state_changed = permission_state_changed or (
            not channel.permissions_synced or target_parent_id != original_parent_id
        )
        channel.permissions_synced = True
    changes: list[dict[str, object]] = []
    history_policy_changed = False
    for field, value in values.items():
        old = getattr(channel, field)
        if old != value:
            changes.append({"key": field, "old_value": old, "new_value": value})
            setattr(channel, field, value)
            history_policy_changed = history_policy_changed or field == "federated_history_policy"
    if changes or permission_state_changed:
        if permission_state_changed:
            changes.append(
                {
                    "key": "permissions_synced",
                    "old_value": original_permissions_synced,
                    "new_value": channel.permissions_synced,
                }
            )
            guild.permission_generation += 1
        if history_policy_changed:
            guild.history_policy_generation += 1
            from app.federation.history import revoke_history_exports

            await revoke_history_exports(session, guild)
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.channel.update",
            {"channel": federation_channel_state(channel)},
            channel=channel,
            snapshot_required=history_policy_changed,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            11,
            target_type="channel",
            target_ref={"id": str(channel.id)},
            changes=changes,
        )
        await session.commit()
        await session.refresh(guild)
        await session.refresh(channel)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "CHANNEL_UPDATE",
            channel_payload(channel),
        )
    return channel_payload(channel)


@router.patch("/{guild_id}/channels")
async def reorder_channels(
    guild_id: EntityRef,
    payload: ChannelPositionBatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("channel.reorder")
    )
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.unavailable.is_(False),
            )
            .order_by(Channel.position, Channel.id)
            .with_for_update()
        )
    )
    by_id = {channel.id: channel for channel in channels}
    requested_ids = {item.id for item in payload.channels}
    if requested_ids != set(by_id):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_SET_CHANGED"})

    for item in payload.channels:
        channel = by_id[item.id]
        if channel.type == 4 and item.parent_id is not None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})
        if item.parent_id is None:
            continue
        parent = by_id.get(item.parent_id)
        if parent is None or parent.type != 4:
            raise HTTPException(status_code=400, detail={"code": "PARENT_NOT_CATEGORY"})
        if parent.id == channel.id:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})

    changed: list[Channel] = []
    audit_changes: list[dict[str, object]] = []
    permission_state_changed = False
    for item in payload.channels:
        channel = by_id[item.id]
        next_parent_domain = settings.domain if item.parent_id is not None else None
        parent_changed = channel.parent_id != item.parent_id
        sync_changed = item.sync_permissions and not channel.permissions_synced
        if channel.position == item.position and not parent_changed and not sync_changed:
            continue
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            required_permissions("channel.update"),
            channel=channel,
        )
        old_sync = channel.permissions_synced
        if channel.type != 4 and (parent_changed or sync_changed):
            if item.sync_permissions:
                if item.parent_id is None:
                    raise HTTPException(status_code=400, detail={"code": "CHANNEL_HAS_NO_CATEGORY"})
                await session.execute(
                    delete(ChannelOverwrite).where(
                        ChannelOverwrite.channel_id == channel.id,
                        ChannelOverwrite.channel_domain == channel.origin_domain,
                    )
                )
                channel.permissions_synced = True
            elif parent_changed and channel.permissions_synced:
                source = await overwrite_source_channel(session, channel)
                await copy_overwrites(session, source, channel)
                channel.permissions_synced = False
            permission_state_changed = permission_state_changed or (
                old_sync != channel.permissions_synced
            )
        audit_changes.append(
            {
                "key": f"{channel.id}@{channel.origin_domain}",
                "old_value": {
                    "position": channel.position,
                    "parent_id": str(channel.parent_id) if channel.parent_id is not None else None,
                    "permissions_synced": old_sync,
                },
                "new_value": {
                    "position": item.position,
                    "parent_id": str(item.parent_id) if item.parent_id is not None else None,
                    "permissions_synced": channel.permissions_synced,
                },
            }
        )
        channel.position = item.position
        channel.parent_id = item.parent_id
        channel.parent_domain = next_parent_domain
        changed.append(channel)

    if changed:
        if permission_state_changed:
            guild.permission_generation += 1
        for channel in changed:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.channel.update",
                {"channel": federation_channel_state(channel)},
                channel=channel,
            )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            11,
            target_type="channel_order",
            target_ref={"guild_id": str(guild.id)},
            changes=audit_changes,
        )
        await session.commit()
        await session.refresh(guild)
        for channel in changed:
            await session.refresh(channel)
        await wake_queued_guild_federation(guild)
        for channel in changed:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "CHANNEL_UPDATE",
                channel_payload(channel),
            )

    return [
        channel_payload(channel)
        for channel in sorted(channels, key=lambda item: (item.position, item.id))
    ]


@router.delete("/{guild_id}/channels/{channel_id}", status_code=204)
async def delete_channel(
    guild_id: EntityRef,
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        required_permissions("channel.delete"),
        channel=channel,
    )
    message_exists = await session.scalar(
        select(Message.id)
        .where(
            Message.channel_id == channel.id,
            Message.channel_domain == channel.origin_domain,
        )
        .limit(1)
    )
    if message_exists is not None:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_NOT_EMPTY"})
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        12,
        target_type="channel",
        target_ref={"id": str(channel.id), "name": channel.name},
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.delete",
        {"channel": {"id": str(channel.id), "origin_domain": channel.origin_domain}},
    )
    await session.delete(channel)
    await session.commit()
    await session.refresh(guild)
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "CHANNEL_DELETE",
        {
            "id": str(channel.id),
            "origin_domain": channel.origin_domain,
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
        },
    )
    return Response(status_code=204)


@router.patch("/{guild_id}/roles")
async def reorder_roles(
    guild_id: EntityRef,
    payload: RolePositionBatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, object]]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("role.reorder")
    )
    roles = list(
        await session.scalars(
            select(Role)
            .where(
                Role.guild_id == guild.id,
                Role.guild_domain == guild.origin_domain,
                Role.id.in_([item.id for item in payload.roles]),
            )
            .with_for_update()
        )
    )
    if len(roles) != len(payload.roles) or any(role.id == guild.id for role in roles):
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    by_id = {role.id: role for role in roles}
    changes: list[dict[str, object]] = []
    changed: list[Role] = []
    for item in payload.roles:
        role = by_id[item.id]
        require_current_version(role.updated_at, item.version)
        await require_can_manage_role(session, guild, auth.user, role)
        if (guild.owner_id, guild.owner_domain) != (auth.user.id, auth.user.origin_domain):
            actor_role = await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
            if (item.position, -role.id) >= role_rank(actor_role):
                raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
        if role.position == item.position:
            continue
        changes.append(
            {"key": str(role.id), "old_value": role.position, "new_value": item.position}
        )
        role.position = item.position
        changed.append(role)
    if changed:
        guild.permission_generation += 1
        for role in changed:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.role.update",
                {"role": role_payload(role)},
                snapshot_required=True,
            )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            33,
            target_type="role",
            target_ref={"ids": [str(role.id) for role in changed]},
            changes=changes,
        )
        await session.commit()
        await session.refresh(guild)
        for role in changed:
            await session.refresh(role)
        await wake_queued_guild_federation(guild)
        for role in changed:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_ROLE_UPDATE",
                role_payload(role),
            )
    return [role_payload(role) for role in roles]


@router.patch("/{guild_id}/roles/{role_id}")
async def update_role(
    guild_id: EntityRef,
    role_id: EntityRef,
    payload: RoleUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    actor_permissions = await require_permissions(
        session, redis, guild, auth.user, required_permissions("role.update")
    )
    role_number, role_domain = role_id.resolve(settings.domain)
    if role_domain != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    role = await guild_role(session, guild, role_number)
    require_current_version(role.updated_at, if_match)
    await require_can_manage_role(session, guild, auth.user, role)
    if (
        payload.permissions is not None
        and (payload.permissions ^ role.permissions) & ~actor_permissions
    ):
        raise HTTPException(status_code=403, detail={"code": "CANNOT_MANAGE_PERMISSIONS"})
    if role.id == guild.id and payload.position is not None:
        raise HTTPException(status_code=400, detail={"code": "EVERYONE_POSITION_IMMUTABLE"})
    if payload.position is not None and (guild.owner_id, guild.owner_domain) != (
        auth.user.id,
        auth.user.origin_domain,
    ):
        actor_role = await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
        if (payload.position, -role.id) >= role_rank(actor_role):
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
    changes: list[dict[str, object]] = []
    for field, value in payload.model_dump(exclude_unset=True).items():
        old = getattr(role, field)
        if old != value:
            changes.append({"key": field, "old_value": old, "new_value": value})
            setattr(role, field, value)
    if changes:
        guild.permission_generation += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.role.update",
            {"role": role_payload(role)},
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            31,
            target_type="role",
            target_ref={"id": str(role.id)},
            changes=changes,
        )
        await session.commit()
        await session.refresh(guild)
        await session.refresh(role)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_ROLE_UPDATE",
            role_payload(role),
        )
    return role_payload(role)


@router.delete("/{guild_id}/roles/{role_id}", status_code=204)
async def delete_role(
    guild_id: EntityRef,
    role_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(session, redis, guild, auth.user, required_permissions("role.delete"))
    role_number, role_domain = role_id.resolve(settings.domain)
    if role_domain != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    role = await guild_role(session, guild, role_number)
    if role.id == guild.id:
        raise HTTPException(status_code=400, detail={"code": "EVERYONE_ROLE_IMMUTABLE"})
    await require_can_manage_role(session, guild, auth.user, role)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        32,
        target_type="role",
        target_ref={"id": str(role.id), "name": role.name},
    )
    guild.permission_generation += 1
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.role.delete",
        {"role": {"id": str(role.id), "origin_domain": role.origin_domain}},
        snapshot_required=True,
    )
    await session.execute(
        delete(ChannelOverwrite).where(
            ChannelOverwrite.target_id == role.id,
            ChannelOverwrite.target_domain == role.origin_domain,
            ChannelOverwrite.target_type == "role",
        )
    )
    await session.delete(role)
    await session.commit()
    await session.refresh(guild)
    await wake_queued_guild_federation(guild)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_ROLE_DELETE",
        {
            "id": str(role.id),
            "origin_domain": role.origin_domain,
            "guild_id": str(guild.id),
            "guild_domain": guild.origin_domain,
        },
    )
    return Response(status_code=204)


@router.put("/{guild_id}/members/{user_id}/roles/{role_id}", status_code=204)
async def assign_role(
    guild_id: EntityRef,
    user_id: EntityRef,
    role_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("member.role.update")
    )
    user_number, user_domain = user_id.resolve(settings.domain)
    role_number, role_domain = role_id.resolve(settings.domain)
    if role_domain != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    member = await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    role = await guild_role(session, guild, role_number)
    if role.id == guild.id:
        raise HTTPException(status_code=400, detail={"code": "EVERYONE_ROLE_IMPLICIT"})
    await require_can_manage_role(session, guild, auth.user, role)
    inserted = await session.scalar(
        pg_insert(MemberRole)
        .values(
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            user_id=user_number,
            user_domain=user_domain,
            role_id=role.id,
            role_domain=role.origin_domain,
        )
        .on_conflict_do_nothing()
        .returning(MemberRole.role_id)
    )
    if inserted is not None:
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.role.add",
            {
                "user": {"id": str(user_number), "origin_domain": user_domain},
                "role": {"id": str(role.id), "origin_domain": role.origin_domain},
                "member_version": str(member.member_version),
            },
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            25,
            target_type="member",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            changes=[{"key": "roles", "added": str(role.id)}],
        )
        rendered_member = await render_member_update(session, member)
        await session.commit()
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            rendered_member,
        )
    return Response(status_code=204)


@router.delete("/{guild_id}/members/{user_id}/roles/{role_id}", status_code=204)
async def remove_role(
    guild_id: EntityRef,
    user_id: EntityRef,
    role_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> Response:
    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("member.role.update")
    )
    user_number, user_domain = user_id.resolve(settings.domain)
    role_number, role_domain = role_id.resolve(settings.domain)
    if role_domain != guild.origin_domain:
        raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
    member = await require_can_manage_member(session, guild, auth.user, user_number, user_domain)
    role = await guild_role(session, guild, role_number)
    await require_can_manage_role(session, guild, auth.user, role)
    result = await session.execute(
        delete(MemberRole)
        .where(
            MemberRole.guild_id == guild.id,
            MemberRole.guild_domain == guild.origin_domain,
            MemberRole.user_id == user_number,
            MemberRole.user_domain == user_domain,
            MemberRole.role_id == role.id,
            MemberRole.role_domain == role.origin_domain,
        )
        .returning(MemberRole.role_id)
    )
    if result.scalar_one_or_none() is not None:
        member.member_version += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.member.role.remove",
            {
                "user": {"id": str(user_number), "origin_domain": user_domain},
                "role": {"id": str(role.id), "origin_domain": role.origin_domain},
                "member_version": str(member.member_version),
            },
            snapshot_required=True,
        )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            25,
            target_type="member",
            target_ref={"id": str(user_number), "origin_domain": user_domain},
            changes=[{"key": "roles", "removed": str(role.id)}],
        )
        rendered_member = await render_member_update(session, member)
        await session.commit()
        await session.refresh(guild)
        await wake_queued_guild_federation(guild)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            rendered_member,
        )
    return Response(status_code=204)
