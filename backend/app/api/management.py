from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast

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
from app.chat.announcement_guards import announcement_dependencies_exist
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
from app.chat.permissions import require_bot_channel_grant, require_permissions
from app.chat.schemas import (
    ChannelPositionBatch,
    ChannelUpdate,
    GuildUpdate,
    MemberRoleSet,
    RolePositionBatch,
    RoleUpdate,
)
from app.core.channel_types import GUILD_VOICE_CHANNEL_TYPES, validate_voice_channel_limits
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.core.types import EntityRef, EntityReference
from app.db.materialization import materialize_updated_at
from app.db.models import (
    Attachment,
    Channel,
    ChannelOverwrite,
    GuildMember,
    MemberRole,
    Message,
    Role,
    User,
)
from app.federation.guild_management import (
    guild_management_dict_body,
    guild_management_list_body,
    proxy_remote_guild_management,
    qualified_management_ref,
    require_guild_management_status,
)
from app.voice.regions import require_configured_rtc_region

router = APIRouter(prefix="/api/v1/guilds", tags=["guild-management"])


def channel_update_permissions(_payload: ChannelUpdate) -> Permission:
    """Return the exact live permission mask for a mixed channel update."""

    return required_permissions("channel.update")


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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "guild.update",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

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
        await materialize_updated_at(session, guild)
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
            reason=reason,
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "channel.update",
        {
            "channel_ref": qualified_management_ref(channel_id, guild_domain),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

    guild = await local_guild(session, settings, guild_id, for_update=True)
    channel = await guild_channel(session, settings, guild_id, channel_id)
    if channel.type in {10, 11, 12}:
        raise HTTPException(status_code=400, detail={"code": "USE_THREAD_ENDPOINT"})
    require_current_version(channel.updated_at, if_match)
    values = payload.model_dump(exclude_unset=True)
    await require_permissions(
        session,
        redis,
        guild,
        auth.user,
        channel_update_permissions(payload),
        channel=channel,
    )
    original_parent_id = channel.parent_id
    original_permissions_synced = channel.permissions_synced
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
    voice_fields = {
        "bitrate",
        "user_limit",
        "rtc_region",
        "video_quality_mode",
    }
    if channel.type not in GUILD_VOICE_CHANNEL_TYPES and values.keys() & voice_fields:
        raise HTTPException(status_code=400, detail={"code": "VOICE_FIELDS_VOICE_ONLY"})
    try:
        validate_voice_channel_limits(
            channel.type,
            bitrate=payload.bitrate if "bitrate" in values else None,
            user_limit=payload.user_limit if "user_limit" in values else None,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "VOICE_CHANNEL_LIMIT_INVALID", "message": str(exc)},
        ) from exc
    if "rtc_region" in values:
        values["rtc_region"] = require_configured_rtc_region(settings, payload.rtc_region)
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
    target_parent: Channel | None = None
    if "parent_id" in values:
        parent_id = values["parent_id"]
        parent_changed = parent_id != original_parent_id
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
            await require_bot_channel_grant(session, guild, auth.user, parent)
            target_parent = parent
        values["parent_domain"] = settings.domain if parent_id is not None else None
        if parent_changed:
            permission_state_changed = True
            if channel.permissions_synced:
                source = await overwrite_source_channel(session, channel)
                await copy_overwrites(session, source, channel)
                channel.permissions_synced = False
    target_parent_id = values.get("parent_id", channel.parent_id)
    if sync_permissions:
        if target_parent_id is None or channel.type == 4:
            raise HTTPException(status_code=400, detail={"code": "CHANNEL_HAS_NO_CATEGORY"})
        if target_parent is None:
            target_parent = await guild_channel(
                session,
                settings,
                EntityReference(guild.id, guild.origin_domain),
                EntityReference(
                    target_parent_id,
                    channel.parent_domain or channel.origin_domain,
                ),
            )
            if target_parent.type != 4:
                raise HTTPException(status_code=400, detail={"code": "PARENT_NOT_CATEGORY"})
            await require_bot_channel_grant(session, guild, auth.user, target_parent)
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
        e2ee_policy_channels: list[Channel] = []
        if permission_state_changed:
            if original_permissions_synced != channel.permissions_synced:
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
                e2ee_policy_channels.append(channel)
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
            snapshot_required=history_policy_changed or permission_state_changed,
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
            reason=reason,
            changes=changes,
        )
        await session.commit()
        await session.refresh(guild)
        await session.refresh(channel)
        await wake_queued_guild_federation(guild)
        await publish_e2ee_policy_updates(
            session,
            redis,
            settings,
            e2ee_policy_channels,
        )
        if not e2ee_policy_channels:
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


@router.patch("/{guild_id}/channels", status_code=204)
async def reorder_channels(
    guild_id: EntityRef,
    payload: ChannelPositionBatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "channel.reorder",
        {
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "reason": reason,
        },
    )
    if proxied is not None:
        return Response(status_code=204)

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
        )
    )
    by_id = {channel.id: channel for channel in channels}
    requested_ids = {item.id for item in payload.channels}
    if not requested_ids.issubset(by_id):
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_SET_CHANGED"})
    if any(
        item.position is not None and item.position >= len(channels) for item in payload.channels
    ):
        raise HTTPException(status_code=400, detail={"code": "CHANNEL_POSITION_INVALID"})

    next_parent_ids: dict[int, int | None] = {}
    parent_changed_ids: set[int] = set()
    permission_checks: dict[int, Permission] = {}
    bot_fence_ids = set(requested_ids)

    def add_permission_check(channel_id: int, permissions: Permission) -> None:
        permission_checks[channel_id] = (
            permission_checks.get(channel_id, Permission(0)) | permissions
        )

    for item in payload.channels:
        channel = by_id[item.id]
        parent_supplied = "parent_id" in item.model_fields_set
        next_parent_id = item.parent_id if parent_supplied else channel.parent_id
        next_parent_ids[channel.id] = next_parent_id
        parent_changed = parent_supplied and next_parent_id != channel.parent_id
        if parent_changed:
            parent_changed_ids.add(channel.id)
        if channel.type == 4 and parent_supplied and next_parent_id is not None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})
        if next_parent_id is None:
            if item.lock_permissions:
                raise HTTPException(status_code=400, detail={"code": "CHANNEL_HAS_NO_CATEGORY"})
        else:
            parent = by_id.get(next_parent_id)
            if parent is None or parent.type != 4:
                raise HTTPException(status_code=400, detail={"code": "PARENT_NOT_CATEGORY"})
            if parent.id == channel.id:
                raise HTTPException(status_code=400, detail={"code": "INVALID_CHANNEL_PARENT"})

        flags_supplied = "flags" in item.model_fields_set and item.flags is not None
        flags_changed = flags_supplied and item.flags != channel.flags
        if flags_changed and channel.type != 15:
            raise HTTPException(status_code=400, detail={"code": "FORUM_FIELDS_FORUM_ONLY"})

        if parent_changed:
            add_permission_check(
                channel.id,
                Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS,
            )
            if next_parent_id is not None:
                add_permission_check(
                    next_parent_id,
                    Permission.VIEW_CHANNEL | Permission.MANAGE_CHANNELS,
                )
        elif item.position is not None and channel.parent_id is not None:
            add_permission_check(channel.parent_id, Permission.MANAGE_CHANNELS)
        if item.lock_permissions:
            add_permission_check(channel.id, Permission.MANAGE_ROLES)
            if next_parent_id is not None:
                bot_fence_ids.add(next_parent_id)
        if flags_changed:
            add_permission_check(channel.id, Permission.MANAGE_CHANNELS)

    if len(parent_changed_ids) > 1:
        raise HTTPException(
            status_code=400,
            detail={
                "code": 40009,
                "message": "Only one channel can have a parent_id modified at a time",
            },
        )

    await require_bot_channel_grant(
        session,
        guild,
        auth.user,
        *(by_id[channel_id] for channel_id in sorted(bot_fence_ids | set(permission_checks))),
    )

    locked_channel_ids = requested_ids | set(permission_checks)
    locked_channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.origin_domain == guild.origin_domain,
                Channel.id.in_(locked_channel_ids),
                Channel.unavailable.is_(False),
                Channel.type.not_in({10, 11, 12}),
            )
            .with_for_update()
        )
    )
    if {channel.id for channel in locked_channels} != locked_channel_ids:
        raise HTTPException(status_code=409, detail={"code": "CHANNEL_SET_CHANGED"})
    for channel_id, permissions in sorted(permission_checks.items()):
        await require_permissions(
            session,
            redis,
            guild,
            auth.user,
            permissions,
            channel=by_id[channel_id],
        )

    position_groups: dict[int, list[Channel]] = {}
    for item in payload.channels:
        if item.position is not None:
            position_groups.setdefault(item.position, []).append(by_id[item.id])
    positioned_ids = {channel.id for group in position_groups.values() for channel in group}
    final_channels = [channel for channel in channels if channel.id not in positioned_ids]
    previous_group_end = 0
    for requested_position, group in sorted(position_groups.items()):
        ordered_group = sorted(group, key=lambda channel: channel.id)
        insertion_index = min(
            max(requested_position, previous_group_end),
            len(final_channels),
        )
        final_channels[insertion_index:insertion_index] = ordered_group
        previous_group_end = insertion_index + len(ordered_group)

    changed: list[Channel] = []
    audit_changes: list[dict[str, object]] = []
    permission_state_changed = False
    permission_changed_ids: set[int] = set()
    e2ee_policy_channels: list[Channel] = []
    original_state = {
        channel.id: (
            channel.position,
            channel.parent_id,
            channel.permissions_synced,
            channel.flags,
        )
        for channel in channels
    }
    for item in payload.channels:
        channel = by_id[item.id]
        parent_supplied = "parent_id" in item.model_fields_set
        next_parent_id = next_parent_ids[channel.id]
        parent_changed = channel.id in parent_changed_ids
        lock_changed = item.lock_permissions is True and not channel.permissions_synced
        flags_changed = item.flags is not None and item.flags != channel.flags
        if not parent_changed and not lock_changed and not flags_changed:
            continue
        old_sync = channel.permissions_synced
        if channel.type != 4 and (parent_changed or lock_changed):
            if item.lock_permissions:
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
            channel_permission_changed = parent_changed or (old_sync != channel.permissions_synced)
            if channel_permission_changed:
                permission_state_changed = True
                permission_changed_ids.add(channel.id)
                if channel.encryption_mode == "e2ee" and channel.encryption_state == "active":
                    channel.encryption_state = "rekeying"
                    e2ee_policy_channels.append(channel)
        if parent_supplied:
            channel.parent_id = next_parent_id
            channel.parent_domain = guild.origin_domain if next_parent_id is not None else None
        if flags_changed:
            channel.flags = cast(int, item.flags)

    for position, channel in enumerate(final_channels):
        channel.position = position
        old_position, old_parent_id, old_sync, old_flags = original_state[channel.id]
        if (
            old_position == channel.position
            and old_parent_id == channel.parent_id
            and old_sync == channel.permissions_synced
            and old_flags == channel.flags
        ):
            continue
        audit_changes.append(
            {
                "key": f"{channel.id}@{channel.origin_domain}",
                "old_value": {
                    "position": old_position,
                    "parent_id": str(old_parent_id) if old_parent_id is not None else None,
                    "permissions_synced": old_sync,
                    "flags": old_flags,
                },
                "new_value": {
                    "position": channel.position,
                    "parent_id": (
                        str(channel.parent_id) if channel.parent_id is not None else None
                    ),
                    "permissions_synced": channel.permissions_synced,
                    "flags": channel.flags,
                },
            }
        )
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
                snapshot_required=channel.id in permission_changed_ids,
            )
        await add_audit_entry(
            session,
            snowflake,
            guild,
            auth.user,
            11,
            target_type="channel_order",
            target_ref={"guild_id": str(guild.id)},
            reason=reason,
            changes=audit_changes,
        )
        await session.commit()
        await session.refresh(guild)
        for channel in changed:
            await session.refresh(channel)
        await wake_queued_guild_federation(guild)
        await publish_e2ee_policy_updates(
            session,
            redis,
            settings,
            e2ee_policy_channels,
        )
        e2ee_policy_refs = {(channel.id, channel.origin_domain) for channel in e2ee_policy_channels}
        for channel in changed:
            if (channel.id, channel.origin_domain) in e2ee_policy_refs:
                continue
            await publish_dispatch(
                redis,
                guild_topic(guild.origin_domain, guild.id),
                "CHANNEL_UPDATE",
                channel_payload(channel),
            )

    return Response(status_code=204)


@router.delete("/{guild_id}/channels/{channel_id}")
async def delete_channel(
    guild_id: EntityRef,
    channel_id: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "channel.delete",
        {
            "channel_ref": qualified_management_ref(channel_id, guild_domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

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
    if await announcement_dependencies_exist(
        session,
        {(channel.id, channel.origin_domain)},
    ):
        raise HTTPException(
            status_code=409,
            detail={"code": "CHANNEL_HAS_ANNOUNCEMENT_DEPENDENCIES"},
        )
    rendered_deleted = channel_payload(channel)
    await add_audit_entry(
        session,
        snowflake,
        guild,
        auth.user,
        12,
        target_type="channel",
        target_ref={"id": str(channel.id), "name": channel.name},
        reason=reason,
    )
    await queue_guild_mutation(
        session,
        settings,
        guild,
        auth.user,
        "guild.channel.delete",
        {"channel": {"id": str(channel.id), "origin_domain": channel.origin_domain}},
    )
    # Guild settings hold composite references to channels. Clear any setting
    # that points at the channel before deleting it so the database invariant
    # remains valid and clients do not retain a dead channel reference.
    for id_field, domain_field in (
        ("afk_channel_id", "afk_channel_domain"),
        ("system_channel_id", "system_channel_domain"),
        ("rules_channel_id", "rules_channel_domain"),
        ("public_updates_channel_id", "public_updates_channel_domain"),
        ("safety_alerts_channel_id", "safety_alerts_channel_domain"),
    ):
        if (getattr(guild, id_field), getattr(guild, domain_field)) == (
            channel.id,
            channel.origin_domain,
        ):
            setattr(guild, id_field, None)
            setattr(guild, domain_field, None)
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
    return rendered_deleted


@router.patch("/{guild_id}/roles")
async def reorder_roles(
    guild_id: EntityRef,
    payload: RolePositionBatch,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> list[dict[str, object]]:
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "role.reorder",
        {"data": payload.model_dump(mode="json"), "reason": reason},
    )
    if proxied is not None:
        return cast(list[dict[str, object]], guild_management_list_body(proxied, 200))

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
        await materialize_updated_at(session, *changed)
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
            reason=reason,
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "role.update",
        {
            "resource_ref": qualified_management_ref(role_id, guild_domain),
            "data": payload.model_dump(mode="json", exclude_unset=True),
            "if_match": if_match,
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

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
        await materialize_updated_at(session, role)
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
            reason=reason,
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "role.delete",
        {
            "resource_ref": qualified_management_ref(role_id, guild_domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

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
        reason=reason,
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
    icon_attachment = await session.scalar(
        select(Attachment)
        .where(Attachment.asset_binding == f"role:{role.origin_domain}:{role.id}:icon")
        .with_for_update()
    )
    if icon_attachment is not None:
        icon_attachment.asset_binding = None
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
    if icon_attachment is not None:
        from app.tasks import media_local_purge

        await enqueue_best_effort(
            media_local_purge, icon_attachment.id, icon_attachment.origin_domain
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.role.assign",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "role_ref": qualified_management_ref(role_id, guild_domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

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
        # Discord-style temporary invite memberships become permanent as soon
        # as the member receives any explicit role.
        member.temporary = False
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
            reason=reason,
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> dict[str, object]:
    """Atomically replace the explicitly assigned roles for one member."""

    _, guild_domain = guild_id.resolve(settings.domain)
    remote_data = payload.model_dump(mode="json")
    remote_data["role_ids"] = [
        qualified_management_ref(role_ref, guild_domain) for role_ref in payload.role_ids
    ]
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.role.replace",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "data": remote_data,
            "reason": reason,
        },
    )
    if proxied is not None:
        return guild_management_dict_body(proxied, 200)

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

    if added:
        member.temporary = False
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
        reason=reason,
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
    reason: Annotated[str | None, Header(alias="X-Audit-Log-Reason", max_length=512)] = None,
) -> Response:
    _, guild_domain = guild_id.resolve(settings.domain)
    proxied = await proxy_remote_guild_management(
        session,
        settings,
        guild_id,
        auth.user,
        "member.role.remove",
        {
            "user_ref": qualified_management_ref(user_id, settings.domain),
            "role_ref": qualified_management_ref(role_id, guild_domain),
            "reason": reason,
        },
    )
    if proxied is not None:
        require_guild_management_status(proxied, 204)
        return Response(status_code=204)

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
            reason=reason,
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
