from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import array
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
    forum_reaction_payload,
    forum_tags_payload,
    guild_channel,
    local_guild,
    overwrite_source_channel,
    validate_forum_emoji_ids,
)
from app.chat.audit import add_audit_entry
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.events import guild_topic, publish_dispatch
from app.chat.guild_revision import (
    federation_channel_state,
    queue_guild_mutation,
    wake_queued_guild_federation,
)
from app.chat.hierarchy import (
    guild_role,
    highest_role,
    require_can_assign_member_role,
    require_can_manage_role,
    role_reorder_allowed,
)
from app.chat.payloads import channel_payload, guild_payload, member_payload, role_payload
from app.chat.permissions import require_permissions
from app.chat.schemas import (
    ChannelPositionBatch,
    ChannelUpdate,
    GuildUpdate,
    MemberRoleSet,
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
    return member_payload(
        member,
        user,
        role_ids,
        include_private_authority_state=True,
    )


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
    if channel.type in {10, 11, 12}:
        raise HTTPException(status_code=400, detail={"code": "USE_THREAD_ENDPOINT"})
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
    if values.get("e2ee_required") is False and channel.type == 15 and channel.e2ee_required:
        raise HTTPException(
            status_code=409,
            detail={"code": "FORUM_E2EE_REQUIREMENT_IRREVERSIBLE"},
        )
    if (
        values.get("e2ee_required") is True
        and not channel.e2ee_required
        and not settings.e2ee_activation_enabled
    ):
        raise HTTPException(status_code=403, detail={"code": "E2EE_ACTIVATION_DISABLED"})
    sync_permissions = bool(values.pop("sync_permissions", False))
    forum_fields = {
        "available_tags",
        "default_reaction_emoji",
        "default_sort_order",
        "default_forum_layout",
        "e2ee_required",
        "flags",
    }
    if channel.type != 15 and values.keys() & forum_fields:
        raise HTTPException(status_code=400, detail={"code": "FORUM_FIELDS_FORUM_ONLY"})
    if "default_auto_archive_duration" in values and channel.type not in {0, 5, 15}:
        raise HTTPException(status_code=400, detail={"code": "THREAD_DEFAULT_CHANNEL_TYPE_INVALID"})
    if "default_thread_rate_limit_per_user" in values and channel.type not in {0, 15}:
        raise HTTPException(status_code=400, detail={"code": "THREAD_DEFAULT_CHANNEL_TYPE_INVALID"})
    if channel.type != 15 and isinstance(values.get("topic"), str) and len(values["topic"]) > 1024:
        raise HTTPException(status_code=400, detail={"code": "CHANNEL_TOPIC_TOO_LONG"})
    if "available_tags" in values or "default_reaction_emoji" in values:
        await validate_forum_emoji_ids(
            session,
            guild,
            (
                list(payload.available_tags or [])
                if "available_tags" in values
                else list(channel.available_tags or [])
            ),
            (
                payload.default_reaction_emoji
                if "default_reaction_emoji" in values
                else channel.default_reaction_emoji
            ),
        )
    removed_tag_ids: set[int] = set()
    if "available_tags" in values:
        existing_tag_ids = {
            int(str(item["id"]))
            for item in channel.available_tags or []
            if isinstance(item, dict) and str(item.get("id", "")).isdigit()
        }
        next_tags = await forum_tags_payload(
            list(payload.available_tags or []),
            snowflake,
            existing_ids=existing_tag_ids,
        )
        next_tag_ids = {int(str(item["id"])) for item in next_tags}
        removed_tag_ids = existing_tag_ids - next_tag_ids
        values["available_tags"] = next_tags
    if "default_reaction_emoji" in values:
        values["default_reaction_emoji"] = forum_reaction_payload(payload.default_reaction_emoji)
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
    tagged_thread_updates: list[Channel] = []
    if removed_tag_ids:
        applied_expression: Any = Channel.applied_tag_ids
        for removed_id in sorted(removed_tag_ids):
            applied_expression = applied_expression.op("-")(str(removed_id))
        tagged_thread_updates = list(
            await session.scalars(
                update(Channel)
                .where(
                    Channel.parent_id == channel.id,
                    Channel.parent_domain == channel.origin_domain,
                    Channel.type.in_({10, 11, 12}),
                    Channel.applied_tag_ids.op("?|")(
                        array([str(item) for item in sorted(removed_tag_ids)])
                    ),
                )
                .values(applied_tag_ids=applied_expression)
                .returning(Channel)
            )
        )
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
            if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
                channel.encryption_state = "rekeying"
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
        for thread in tagged_thread_updates:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                "guild.channel.update",
                {"channel": federation_channel_state(thread)},
                channel=thread,
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
        for thread in tagged_thread_updates:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "THREAD_UPDATE",
                channel_payload(thread),
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
                Channel.type.not_in({10, 11, 12}),
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
            if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
                channel.encryption_state = "rekeying"
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
    if channel.type in {10, 11, 12}:
        raise HTTPException(status_code=400, detail={"code": "USE_THREAD_ENDPOINT"})
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
    child_thread_exists = await session.scalar(
        select(Channel.id)
        .where(
            Channel.parent_id == channel.id,
            Channel.parent_domain == channel.origin_domain,
            Channel.type.in_({10, 11, 12}),
            Channel.unavailable.is_(False),
        )
        .limit(1)
    )
    if child_thread_exists is not None:
        # Thread parents cannot be physically detached: thread permissions and
        # lifecycle are inherited from the parent. Match the existing
        # non-empty-channel contract and require posts/threads to be removed
        # first.
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
            )
            .with_for_update()
        )
    )
    movable_roles = [role for role in roles if role.id != guild.id]
    requested_ids = {item.id for item in payload.roles}
    if requested_ids != {role.id for role in movable_roles}:
        raise HTTPException(status_code=400, detail={"code": "ROLE_ORDER_INCOMPLETE"})
    expected_positions = set(range(1, len(movable_roles) + 1))
    if {item.position for item in payload.roles} != expected_positions:
        raise HTTPException(status_code=400, detail={"code": "ROLE_ORDER_NOT_CONTIGUOUS"})
    by_id = {role.id: role for role in roles}
    actor_is_owner = (guild.owner_id, guild.owner_domain) == (
        auth.user.id,
        auth.user.origin_domain,
    )
    actor_role = (
        None
        if actor_is_owner
        else await highest_role(session, guild, auth.user.id, auth.user.origin_domain)
    )
    changes: list[dict[str, object]] = []
    changed: list[Role] = []
    for item in payload.roles:
        role = by_id[item.id]
        if role.position == item.position:
            continue
        require_current_version(role.updated_at, item.version)
        if actor_role is not None and not role_reorder_allowed(actor_role, role, item.position):
            raise HTTPException(status_code=403, detail={"code": "ROLE_HIERARCHY"})
        changes.append(
            {"key": str(role.id), "old_value": role.position, "new_value": item.position}
        )
        role.position = item.position
        changed.append(role)
    if changed:
        e2ee_policy_channels: list[Channel] = []
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
                e2ee_policy_channels=e2ee_policy_channels,
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
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        for role in changed:
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "GUILD_ROLE_UPDATE",
                role_payload(role),
            )
    return [role_payload(by_id[item.id]) for item in payload.roles]


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
    if payload.position is not None:
        raise HTTPException(status_code=400, detail={"code": "ROLE_POSITION_BATCH_REQUIRED"})
    changes: list[dict[str, object]] = []
    for field, value in payload.model_dump(exclude_unset=True).items():
        old = getattr(role, field)
        if old != value:
            changes.append({"key": field, "old_value": old, "new_value": value})
            setattr(role, field, value)
    if changes:
        e2ee_policy_channels: list[Channel] = []
        guild.permission_generation += 1
        await queue_guild_mutation(
            session,
            settings,
            guild,
            auth.user,
            "guild.role.update",
            {"role": role_payload(role)},
            snapshot_required=True,
            e2ee_policy_channels=e2ee_policy_channels,
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
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
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
    e2ee_policy_channels: list[Channel] = []
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.role.delete",
        {"role": {"id": str(role.id), "origin_domain": role.origin_domain}},
        snapshot_required=True,
        e2ee_policy_channels=e2ee_policy_channels,
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
    await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
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
    member = await require_can_assign_member_role(
        session, guild, auth.user, user_number, user_domain
    )
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
        e2ee_policy_channels: list[Channel] = []
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
            e2ee_policy_channels=e2ee_policy_channels,
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
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            rendered_member,
        )
    return Response(status_code=204)


@router.put("/{guild_id}/members/{user_id}/roles")
async def replace_member_roles(
    guild_id: EntityRef,
    user_id: EntityRef,
    payload: MemberRoleSet,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    """Atomically replace the explicitly assigned roles for one member."""

    guild = await local_guild(session, settings, guild_id, for_update=True)
    await require_permissions(
        session, redis, guild, auth.user, required_permissions("member.role.update")
    )
    user_number, user_domain = user_id.resolve(settings.domain)
    member = await require_can_assign_member_role(
        session, guild, auth.user, user_number, user_domain
    )

    requested: dict[tuple[int, str], Role] = {}
    for requested_role_ref in payload.role_ids:
        role_number, role_domain = requested_role_ref.resolve(settings.domain)
        if role_domain != guild.origin_domain:
            raise HTTPException(status_code=404, detail={"code": "ROLE_NOT_FOUND"})
        role = await guild_role(session, guild, role_number)
        if role.id == guild.id:
            raise HTTPException(status_code=400, detail={"code": "EVERYONE_ROLE_IMPLICIT"})
        requested[(role.id, role.origin_domain)] = role

    current = {
        (role_id, role_domain)
        for role_id, role_domain in (
            await session.execute(
                select(MemberRole.role_id, MemberRole.role_domain).where(
                    MemberRole.guild_id == guild.id,
                    MemberRole.guild_domain == guild.origin_domain,
                    MemberRole.user_id == user_number,
                    MemberRole.user_domain == user_domain,
                )
            )
        ).all()
    }
    desired = set(requested)
    added = desired - current
    removed = current - desired
    if not added and not removed:
        return await render_member_update(session, member)

    changed_roles = dict(requested)
    for role_id, role_domain in removed:
        existing_role = await session.get(Role, (role_id, role_domain))
        if existing_role is None or (
            existing_role.guild_id,
            existing_role.guild_domain,
        ) != (
            guild.id,
            guild.origin_domain,
        ):
            raise HTTPException(status_code=409, detail={"code": "ROLE_STATE_CHANGED"})
        changed_roles[(existing_role.id, existing_role.origin_domain)] = existing_role
    for changed_role_ref in sorted(added | removed):
        await require_can_manage_role(session, guild, auth.user, changed_roles[changed_role_ref])

    if removed:
        await session.execute(
            delete(MemberRole).where(
                MemberRole.guild_id == guild.id,
                MemberRole.guild_domain == guild.origin_domain,
                MemberRole.user_id == user_number,
                MemberRole.user_domain == user_domain,
                MemberRole.role_id.in_([role_id for role_id, _ in removed]),
                MemberRole.role_domain == guild.origin_domain,
            )
        )
    for role_id, role_domain in sorted(added):
        session.add(
            MemberRole(
                guild_id=guild.id,
                guild_domain=guild.origin_domain,
                user_id=user_number,
                user_domain=user_domain,
                role_id=role_id,
                role_domain=role_domain,
            )
        )

    member.member_version += 1
    e2ee_policy_channels: list[Channel] = []
    for event_type, refs in (
        ("guild.member.role.remove", sorted(removed)),
        ("guild.member.role.add", sorted(added)),
    ):
        for role_id, role_domain in refs:
            await queue_guild_mutation(
                session,
                settings,
                guild,
                auth.user,
                event_type,
                {
                    "user": {"id": str(user_number), "origin_domain": user_domain},
                    "role": {"id": str(role_id), "origin_domain": role_domain},
                    "member_version": str(member.member_version),
                },
                snapshot_required=True,
                e2ee_policy_channels=e2ee_policy_channels,
            )
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        25,
        target_type="member",
        target_ref={"id": str(user_number), "origin_domain": user_domain},
        changes=[
            {
                "key": "roles",
                "added": [str(role_id) for role_id, _ in sorted(added)],
                "removed": [str(role_id) for role_id, _ in sorted(removed)],
            }
        ],
    )
    rendered_member = await render_member_update(session, member)
    await session.commit()
    await session.refresh(guild)
    await wake_queued_guild_federation(guild)
    await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
    await publish_dispatch(
        redis,
        guild_topic(guild.origin_domain, guild.id),
        "GUILD_MEMBER_UPDATE",
        rendered_member,
    )
    return rendered_member


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
    member = await require_can_assign_member_role(
        session, guild, auth.user, user_number, user_domain
    )
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
        e2ee_policy_channels: list[Channel] = []
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
            e2ee_policy_channels=e2ee_policy_channels,
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
        await publish_e2ee_policy_updates(session, redis, settings, e2ee_policy_channels)
        await publish_dispatch(
            redis,
            guild_topic(guild.origin_domain, guild.id),
            "GUILD_MEMBER_UPDATE",
            rendered_member,
        )
    return Response(status_code=204)
