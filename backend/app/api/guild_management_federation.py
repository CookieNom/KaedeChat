from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ConfigDict, Field, StrictInt, ValidationError, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    federated_authenticated_user as _auth,
)
from app.api.dependencies import (
    get_redis,
    get_session,
    get_snowflake,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.models import Guild
from app.federation.guild_management import (
    GuildManagementRequest,
    GuildManagementResult,
    authorize_guild_management_request,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(tags=["guild management federation"])


class _StrictModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class _Empty(_StrictModel):
    pass


class _Data(_StrictModel):
    data: dict[str, Any]


class _Resource(_StrictModel):
    resource_id: StrictInt = Field(ge=0)


class _CreateOrUpdate(_StrictModel):
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _ResourceMutation(_CreateOrUpdate):
    resource_id: StrictInt = Field(ge=0)


class _ResourceDelete(_Resource):
    reason: str | None = Field(default=None, max_length=512)


class _PruneEstimate(_StrictModel):
    days: StrictInt = Field(default=7, ge=1, le=30)
    include_roles: list[str] = Field(default_factory=list, max_length=100)


class _Prune(_StrictModel):
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _ScheduledList(_StrictModel):
    with_user_count: bool = False


class _RefResource(_StrictModel):
    resource_ref: str = Field(min_length=1, max_length=320)


class _ScheduledGet(_RefResource):
    with_user_count: bool = False


class _ScheduledMutation(_RefResource):
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _ScheduledDelete(_RefResource):
    reason: str | None = Field(default=None, max_length=512)


class _GuildAssetMutation(_CreateOrUpdate):
    kind: Literal["icon", "banner"]


class _GuildAssetDelete(_StrictModel):
    kind: Literal["icon", "banner"]


class _RoleIconMutation(_CreateOrUpdate):
    resource_ref: str = Field(min_length=1, max_length=320)


class _RoleIconDelete(_StrictModel):
    resource_ref: str = Field(min_length=1, max_length=320)


class _VoiceMemberMutation(_ScheduledMutation):
    pass


class _VoiceMemberDelete(_ScheduledDelete):
    pass


class _ScheduledUsers(_RefResource):
    limit: StrictInt = Field(default=100, ge=1, le=100)
    before: str | None = Field(default=None, min_length=1, max_length=320)
    after: str | None = Field(default=None, min_length=1, max_length=320)
    with_member: bool = False


class _WebhookCreate(_CreateOrUpdate):
    channel_ref: str = Field(min_length=1, max_length=320)


class _WebhookChannel(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)


class _ChannelUpdate(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)
    data: dict[str, Any]
    if_match: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


class _VersionedMutation(_StrictModel):
    data: dict[str, Any]
    if_match: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


class _VersionedDelete(_StrictModel):
    if_match: str | None = Field(default=None, max_length=128)


class _ResourceRefMutation(_RefResource):
    data: dict[str, Any]
    if_match: str | None = Field(default=None, max_length=128)
    reason: str | None = Field(default=None, max_length=512)


class _ResourceRefDelete(_RefResource):
    reason: str | None = Field(default=None, max_length=512)


class _ChannelMutation(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _ChannelDelete(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)
    reason: str | None = Field(default=None, max_length=512)


class _OverwriteDelete(_ChannelDelete):
    target_type: Literal["role", "member"]
    target_ref: str = Field(min_length=1, max_length=320)


class _MemberMutation(_StrictModel):
    user_ref: str = Field(min_length=1, max_length=320)
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _MemberDelete(_StrictModel):
    user_ref: str = Field(min_length=1, max_length=320)
    reason: str | None = Field(default=None, max_length=512)


class _MemberRole(_MemberDelete):
    role_ref: str = Field(min_length=1, max_length=320)


class _MemberRoleSet(_StrictModel):
    user_ref: str = Field(min_length=1, max_length=320)
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _CollectionPage(_StrictModel):
    limit: StrictInt = Field(default=50, ge=1, le=1000)
    after: str | None = Field(default=None, min_length=1, max_length=320)


class _InstanceBanMutation(_StrictModel):
    instance_domain: str = Field(min_length=1, max_length=253)
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _InstanceBanDelete(_StrictModel):
    instance_domain: str = Field(min_length=1, max_length=253)
    reason: str | None = Field(default=None, max_length=512)


class _InviteCreate(_StrictModel):
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _InviteCode(_StrictModel):
    code: str = Field(pattern=r"^[A-Za-z0-9]{8}$")


class _InviteRevoke(_InviteCode):
    reason: str | None = Field(default=None, max_length=512)


class _InviteTargetUsers(_InviteCode):
    target_user_ids: list[str] = Field(max_length=1000)
    reason: str | None = Field(default=None, max_length=512)


class _TrackerChannel(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)


class _TrackerData(_TrackerChannel):
    data: dict[str, Any]
    if_match: str | None = Field(default=None, max_length=128)


class _TrackerResource(_TrackerChannel):
    resource_ref: str = Field(min_length=1, max_length=320)
    if_match: str | None = Field(default=None, max_length=128)


class _TrackerResourceData(_TrackerResource):
    data: dict[str, Any]


class _StageChannel(_StrictModel):
    channel_id: str = Field(min_length=1, max_length=320)


class _StageMutation(_StageChannel):
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _StageDelete(_StageChannel):
    reason: str | None = Field(default=None, max_length=512)


class _StageVoiceGet(_StrictModel):
    user_ref: str = Field(min_length=1, max_length=320)


class _StageVoiceSelf(_StrictModel):
    data: dict[str, Any]


class _StageVoiceUser(_StageVoiceGet):
    data: dict[str, Any]


class _VoiceStatusMutation(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)
    data: dict[str, Any]
    reason: str | None = Field(default=None, max_length=512)


class _VoiceStatusQuery(_StrictModel):
    channel_ref: str = Field(min_length=1, max_length=320)


class _VoiceChannelInfoQuery(_StrictModel):
    fields: list[Literal["status", "voice_start_time"]] = Field(min_length=1, max_length=2)

    @model_validator(mode="after")
    def unique_fields(self) -> _VoiceChannelInfoQuery:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("channel info fields must be unique")
        return self


def _guild_ref(guild_id: int) -> EntityRef:
    return EntityRef(str(guild_id))


def _result(
    request: GuildManagementRequest,
    status_code: int,
    body: object = None,
) -> GuildManagementResult:
    return GuildManagementResult(
        request_id=request.request_id,
        operation=request.operation,
        guild=request.guild,
        status_code=status_code,
        body=body,
    )


async def _dispatch_automod(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.automod import (
        create_auto_mod_rule,
        get_auto_mod_rule,
        list_auto_mod_rules,
        patch_auto_mod_rule,
        remove_auto_mod_rule,
    )
    from app.automod.schemas import AutoModRuleCreate, AutoModRuleUpdate

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    body: object
    status_code = 200
    if request.operation == "automod.list":
        _Empty.model_validate(request.payload)
        body = await list_auto_mod_rules(guild_ref, auth, session, redis, settings)
    elif request.operation == "automod.get":
        get_payload = _Resource.model_validate(request.payload)
        body = await get_auto_mod_rule(
            guild_ref, get_payload.resource_id, auth, session, redis, settings
        )
    elif request.operation == "automod.create":
        create_payload = _CreateOrUpdate.model_validate(request.payload)
        create_data = AutoModRuleCreate.model_validate(create_payload.data)
        body = await create_auto_mod_rule(
            guild_ref,
            create_data,
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        status_code = 201
    elif request.operation == "automod.update":
        update_payload = _ResourceMutation.model_validate(request.payload)
        update_data = AutoModRuleUpdate.model_validate(update_payload.data)
        body = await patch_auto_mod_rule(
            guild_ref,
            update_payload.resource_id,
            update_data,
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.reason,
        )
    elif request.operation == "automod.delete":
        delete_payload = _ResourceDelete.model_validate(request.payload)
        await remove_auto_mod_rule(
            guild_ref,
            delete_payload.resource_id,
            auth,
            session,
            redis,
            snowflake,
            settings,
            delete_payload.reason,
        )
        body = None
        status_code = 204
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_bulk_moderation(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.bulk_moderation import (
        BulkBanRequest,
        PruneRequest,
        _qualified_unique_refs,
        bulk_ban_members,
        estimate_prune,
        prune_members,
    )

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    body: object
    if request.operation == "moderation.prune.estimate":
        estimate_payload = _PruneEstimate.model_validate(request.payload)
        body = await estimate_prune(
            guild_ref,
            auth,
            session,
            redis,
            settings,
            estimate_payload.days,
            _qualified_unique_refs(
                [EntityRef(item) for item in estimate_payload.include_roles],
                request.requesting_instance,
                code="PRUNE_ROLE_DUPLICATE",
                label="Included roles",
            ),
        )
    elif request.operation == "moderation.prune":
        prune_payload = _Prune.model_validate(request.payload)
        prune_request = PruneRequest.model_validate(prune_payload.data)
        prune_request = prune_request.model_copy(
            update={
                "include_roles": _qualified_unique_refs(
                    prune_request.include_roles,
                    request.requesting_instance,
                    code="PRUNE_ROLE_DUPLICATE",
                    label="Included roles",
                )
            }
        )
        body = await prune_members(
            guild_ref,
            prune_request,
            auth,
            session,
            redis,
            snowflake,
            settings,
            prune_payload.reason,
        )
    elif request.operation == "moderation.bulk_ban":
        ban_payload = _Prune.model_validate(request.payload)
        ban_request = BulkBanRequest.model_validate(ban_payload.data)
        ban_request = ban_request.model_copy(
            update={
                "user_ids": _qualified_unique_refs(
                    ban_request.user_ids,
                    request.requesting_instance,
                    code="BULK_BAN_USER_DUPLICATE",
                    label="Bulk ban users",
                )
            }
        )
        body = await bulk_ban_members(
            guild_ref,
            ban_request,
            auth,
            session,
            redis,
            snowflake,
            settings,
            ban_payload.reason,
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, 200, body)


async def _dispatch_expression_metadata(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.expressions import (
        EmojiUpdate,
        StickerUpdate,
        get_guild_emoji,
        get_guild_sticker,
        list_guild_emojis,
        list_guild_stickers,
        patch_guild_emoji,
        patch_guild_sticker,
    )

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    body: object
    if request.operation == "emoji.list":
        _Empty.model_validate(request.payload)
        body = await list_guild_emojis(guild_ref, auth, session, redis, settings)
    elif request.operation == "emoji.get":
        emoji_get = _Resource.model_validate(request.payload)
        body = await get_guild_emoji(
            guild_ref, emoji_get.resource_id, auth, session, redis, settings
        )
    elif request.operation == "emoji.update":
        emoji_update = _ResourceMutation.model_validate(request.payload)
        body = await patch_guild_emoji(
            guild_ref,
            emoji_update.resource_id,
            EmojiUpdate.model_validate(emoji_update.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            emoji_update.reason,
        )
    elif request.operation == "sticker.list":
        _Empty.model_validate(request.payload)
        body = await list_guild_stickers(guild_ref, auth, session, redis, settings)
    elif request.operation == "sticker.get":
        sticker_get = _Resource.model_validate(request.payload)
        body = await get_guild_sticker(
            guild_ref, sticker_get.resource_id, auth, session, redis, settings
        )
    elif request.operation == "sticker.update":
        sticker_update = _ResourceMutation.model_validate(request.payload)
        body = await patch_guild_sticker(
            guild_ref,
            sticker_update.resource_id,
            StickerUpdate.model_validate(sticker_update.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            sticker_update.reason,
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, 200, body)


async def _dispatch_expression_media(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.media import (
        create_emoji,
        create_emoji_ticket,
        create_sticker,
        create_sticker_ticket,
        delete_emoji,
        delete_sticker,
    )
    from app.media.schemas import (
        EmojiCommitRequest,
        StickerCommitRequest,
        StickerTicketRequest,
        UploadTicketRequest,
    )

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    response = Response()
    body: object
    status_code = 200
    if request.operation == "emoji.ticket":
        emoji_ticket = _CreateOrUpdate.model_validate(request.payload)
        body = await create_emoji_ticket(
            guild_ref,
            UploadTicketRequest.model_validate(emoji_ticket.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        status_code = 201
    elif request.operation == "emoji.create":
        emoji_create = _CreateOrUpdate.model_validate(request.payload)
        body = await create_emoji(
            guild_ref,
            EmojiCommitRequest.model_validate(emoji_create.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
            emoji_create.reason,
        )
        status_code = 202 if response.status_code == 202 else 201
    elif request.operation == "emoji.delete":
        emoji_delete = _ResourceDelete.model_validate(request.payload)
        await delete_emoji(
            guild_ref,
            emoji_delete.resource_id,
            auth,
            session,
            redis,
            snowflake,
            settings,
            emoji_delete.reason,
        )
        body = None
        status_code = 204
    elif request.operation == "sticker.ticket":
        sticker_ticket = _CreateOrUpdate.model_validate(request.payload)
        body = await create_sticker_ticket(
            guild_ref,
            StickerTicketRequest.model_validate(sticker_ticket.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        status_code = 201
    elif request.operation == "sticker.create":
        sticker_create = _CreateOrUpdate.model_validate(request.payload)
        body = await create_sticker(
            guild_ref,
            StickerCommitRequest.model_validate(sticker_create.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
            sticker_create.reason,
        )
        status_code = 202 if response.status_code == 202 else 201
    elif request.operation == "sticker.delete":
        sticker_delete = _ResourceDelete.model_validate(request.payload)
        await delete_sticker(
            guild_ref,
            sticker_delete.resource_id,
            auth,
            session,
            redis,
            snowflake,
            settings,
            sticker_delete.reason,
        )
        body = None
        status_code = 204
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_guild_media(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.media import (
        commit_guild_asset,
        commit_role_icon,
        create_guild_asset_ticket,
        create_role_icon_ticket,
        delete_guild_asset,
        delete_role_icon,
    )
    from app.media.schemas import AssetCommitRequest, UploadTicketRequest

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    response = Response()
    body: object
    status_code = 200
    if request.operation == "guild_asset.ticket":
        asset_ticket = _GuildAssetMutation.model_validate(request.payload)
        body = await create_guild_asset_ticket(
            guild_ref,
            asset_ticket.kind,
            UploadTicketRequest.model_validate(asset_ticket.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        status_code = 201
    elif request.operation == "guild_asset.commit":
        asset_commit = _GuildAssetMutation.model_validate(request.payload)
        body = await commit_guild_asset(
            guild_ref,
            asset_commit.kind,
            AssetCommitRequest.model_validate(asset_commit.data),
            response,
            auth,
            session,
            redis,
            settings,
        )
        status_code = response.status_code or 200
    elif request.operation == "guild_asset.delete":
        asset_delete = _GuildAssetDelete.model_validate(request.payload)
        body = await delete_guild_asset(
            guild_ref,
            asset_delete.kind,
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation == "role_icon.ticket":
        icon_ticket = _RoleIconMutation.model_validate(request.payload)
        body = await create_role_icon_ticket(
            guild_ref,
            EntityRef(icon_ticket.resource_ref),
            UploadTicketRequest.model_validate(icon_ticket.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        status_code = 201
    elif request.operation == "role_icon.commit":
        icon_commit = _RoleIconMutation.model_validate(request.payload)
        body = await commit_role_icon(
            guild_ref,
            EntityRef(icon_commit.resource_ref),
            AssetCommitRequest.model_validate(icon_commit.data),
            response,
            auth,
            session,
            redis,
            settings,
        )
        status_code = response.status_code or 200
    elif request.operation == "role_icon.delete":
        icon_delete = _RoleIconDelete.model_validate(request.payload)
        body = await delete_role_icon(
            guild_ref,
            EntityRef(icon_delete.resource_ref),
            auth,
            session,
            redis,
            settings,
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_scheduled_event_image(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> tuple[int, object]:
    from app.api.scheduled_events import (
        commit_scheduled_event_image,
        create_scheduled_event_image_ticket,
        delete_scheduled_event_image,
    )
    from app.media.schemas import AssetCommitRequest, UploadTicketRequest

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    if request.operation == "scheduled_event.image.ticket":
        ticket_payload = _ScheduledMutation.model_validate(request.payload)
        body = await create_scheduled_event_image_ticket(
            guild_ref,
            EntityRef(ticket_payload.resource_ref),
            UploadTicketRequest.model_validate(ticket_payload.data),
            Response(),
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        return 201, body
    if request.operation == "scheduled_event.image.commit":
        commit_payload = _ScheduledMutation.model_validate(request.payload)
        response = Response()
        body = await commit_scheduled_event_image(
            guild_ref,
            EntityRef(commit_payload.resource_ref),
            AssetCommitRequest.model_validate(commit_payload.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
            commit_payload.reason,
        )
        return response.status_code or 200, body
    delete_payload = _ScheduledDelete.model_validate(request.payload)
    body = await delete_scheduled_event_image(
        guild_ref,
        EntityRef(delete_payload.resource_ref),
        auth,
        session,
        redis,
        snowflake,
        settings,
        delete_payload.reason,
    )
    return 200, body


async def _dispatch_scheduled_event_subscription(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
) -> tuple[int, None]:
    from app.api.scheduled_events import subscribe_scheduled_event, unsubscribe_scheduled_event

    routes = {
        "scheduled_event.subscribe": subscribe_scheduled_event,
        "scheduled_event.unsubscribe": unsubscribe_scheduled_event,
    }
    payload = _RefResource.model_validate(request.payload)
    await routes[request.operation](
        _guild_ref(int(request.guild.id)),
        EntityRef(payload.resource_ref),
        _auth(actor),
        session,
        redis,
        settings,
    )
    return 204, None


async def _dispatch_scheduled_events(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.scheduled_events import (
        ScheduledEventCreate,
        ScheduledEventPatch,
        create_scheduled_event,
        delete_scheduled_event,
        get_scheduled_event,
        list_scheduled_event_users,
        list_scheduled_events,
        patch_scheduled_event,
    )

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    body: object
    status_code = 200
    if request.operation == "scheduled_event.list":
        list_payload = _ScheduledList.model_validate(request.payload)
        body = await list_scheduled_events(
            guild_ref, list_payload.with_user_count, auth, session, redis, settings
        )
    elif request.operation == "scheduled_event.create":
        create_payload = _CreateOrUpdate.model_validate(request.payload)
        body = await create_scheduled_event(
            guild_ref,
            ScheduledEventCreate.model_validate(create_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        status_code = 201
    elif request.operation == "scheduled_event.get":
        get_payload = _ScheduledGet.model_validate(request.payload)
        body = await get_scheduled_event(
            guild_ref,
            EntityRef(get_payload.resource_ref),
            get_payload.with_user_count,
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation == "scheduled_event.update":
        update_payload = _ScheduledMutation.model_validate(request.payload)
        body = await patch_scheduled_event(
            guild_ref,
            EntityRef(update_payload.resource_ref),
            ScheduledEventPatch.model_validate(update_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.reason,
        )
    elif request.operation == "scheduled_event.delete":
        delete_payload = _ScheduledDelete.model_validate(request.payload)
        await delete_scheduled_event(
            guild_ref,
            EntityRef(delete_payload.resource_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            delete_payload.reason,
        )
        body = None
        status_code = 204
    elif request.operation.startswith("scheduled_event.image."):
        status_code, body = await _dispatch_scheduled_event_image(
            request, actor, session, redis, snowflake, settings
        )
    elif request.operation == "scheduled_event.users":
        users_payload = _ScheduledUsers.model_validate(request.payload)
        body = await list_scheduled_event_users(
            guild_ref,
            EntityRef(users_payload.resource_ref),
            users_payload.limit,
            EntityRef(users_payload.before) if users_payload.before is not None else None,
            EntityRef(users_payload.after) if users_payload.after is not None else None,
            users_payload.with_member,
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation in {"scheduled_event.subscribe", "scheduled_event.unsubscribe"}:
        status_code, body = await _dispatch_scheduled_event_subscription(
            request, actor, session, redis, settings
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_stage_instances(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.stage_instances import (
        StageInstanceCreate,
        StageInstancePatch,
        create_local_stage_instance,
        delete_local_stage_instance,
        get_local_stage_voice_state,
        get_stage_instance,
        patch_local_stage_instance,
        update_local_stage_voice_state,
    )
    from app.voice.schemas import CurrentUserVoiceStateUpdate, UserVoiceStateUpdate

    auth = _auth(actor)
    status_code = 200
    if request.operation == "stage_instance.create":
        stage_create = _CreateOrUpdate.model_validate(request.payload)
        body = await create_local_stage_instance(
            session,
            redis,
            settings,
            snowflake,
            cast(Any, actor),
            StageInstanceCreate.model_validate(stage_create.data),
            reason=stage_create.reason,
        )
        status_code = 201
    elif request.operation == "stage_instance.get":
        stage_channel = _StageChannel.model_validate(request.payload)
        body = await get_stage_instance(
            EntityRef(stage_channel.channel_id),
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation == "stage_instance.update":
        stage_mutation = _StageMutation.model_validate(request.payload)
        body = await patch_local_stage_instance(
            session,
            redis,
            settings,
            snowflake,
            cast(Any, actor),
            EntityRef(stage_mutation.channel_id),
            StageInstancePatch.model_validate(stage_mutation.data),
            reason=stage_mutation.reason,
        )
    elif request.operation == "stage_instance.delete":
        stage_delete = _StageDelete.model_validate(request.payload)
        await delete_local_stage_instance(
            session,
            redis,
            settings,
            snowflake,
            cast(Any, actor),
            EntityRef(stage_delete.channel_id),
            reason=stage_delete.reason,
        )
        body = None
        status_code = 204
    elif request.operation == "stage_voice_state.get":
        voice_get = _StageVoiceGet.model_validate(request.payload)
        user_id, user_domain = EntityRef(voice_get.user_ref).resolve(request.requesting_instance)
        body = await get_local_stage_voice_state(
            session,
            redis,
            settings,
            _guild_ref(int(request.guild.id)),
            cast(Any, actor),
            EntityRef(f"{user_id}@{user_domain}"),
        )
    elif request.operation == "stage_voice_state.self":
        voice_self = _StageVoiceSelf.model_validate(request.payload)
        body = await update_local_stage_voice_state(
            session,
            redis,
            settings,
            _guild_ref(int(request.guild.id)),
            cast(Any, actor),
            EntityRef(f"{cast(Any, actor).id}@{cast(Any, actor).origin_domain}"),
            CurrentUserVoiceStateUpdate.model_validate(voice_self.data),
            current_user=True,
            snowflake=snowflake,
        )
    elif request.operation == "stage_voice_state.user":
        voice_user = _StageVoiceUser.model_validate(request.payload)
        user_id, user_domain = EntityRef(voice_user.user_ref).resolve(request.requesting_instance)
        body = await update_local_stage_voice_state(
            session,
            redis,
            settings,
            _guild_ref(int(request.guild.id)),
            cast(Any, actor),
            EntityRef(f"{user_id}@{user_domain}"),
            UserVoiceStateUpdate.model_validate(voice_user.data),
            current_user=False,
            snowflake=snowflake,
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_webhook_avatar(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> tuple[int, object]:
    from app.api.webhooks import (
        commit_webhook_avatar,
        create_webhook_avatar_ticket,
        delete_webhook_avatar,
    )
    from app.media.schemas import AssetCommitRequest, UploadTicketRequest

    auth = _auth(actor)
    if request.operation == "webhook.avatar.ticket":
        ticket_payload = _ResourceMutation.model_validate(request.payload)
        body = await create_webhook_avatar_ticket(
            EntityRef(str(ticket_payload.resource_id)),
            UploadTicketRequest.model_validate(ticket_payload.data),
            Response(),
            None,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        return 201, body
    if request.operation == "webhook.avatar.commit":
        commit_payload = _ResourceMutation.model_validate(request.payload)
        response = Response()
        body = await commit_webhook_avatar(
            EntityRef(str(commit_payload.resource_id)),
            AssetCommitRequest.model_validate(commit_payload.data),
            response,
            None,
            auth,
            session,
            redis,
            snowflake,
            settings,
            commit_payload.reason,
        )
        return response.status_code or 200, body
    delete_payload = _ResourceDelete.model_validate(request.payload)
    body = await delete_webhook_avatar(
        EntityRef(str(delete_payload.resource_id)),
        None,
        auth,
        session,
        redis,
        snowflake,
        settings,
        delete_payload.reason,
    )
    return 200, body


async def _dispatch_webhooks(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.webhooks import (
        WebhookCreate,
        WebhookPatch,
        create_webhook,
        delete_webhook,
        get_webhook,
        list_channel_webhooks,
        list_webhooks,
        patch_webhook,
        rotate_webhook,
    )

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    body: object
    status_code = 200
    if request.operation == "webhook.create":
        create_payload = _WebhookCreate.model_validate(request.payload)
        body = await create_webhook(
            guild_ref,
            EntityRef(create_payload.channel_ref),
            WebhookCreate.model_validate(create_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        status_code = 201
    elif request.operation == "webhook.list":
        _Empty.model_validate(request.payload)
        body = await list_webhooks(guild_ref, auth, session, redis, settings)
    elif request.operation == "webhook.list_channel":
        channel_payload = _WebhookChannel.model_validate(request.payload)
        body = await list_channel_webhooks(
            guild_ref,
            EntityRef(channel_payload.channel_ref),
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation == "webhook.get":
        get_payload = _Resource.model_validate(request.payload)
        body = await get_webhook(
            EntityRef(str(get_payload.resource_id)),
            None,
            auth,
            session,
            redis,
            settings,
        )
    elif request.operation == "webhook.update":
        update_payload = _ResourceMutation.model_validate(request.payload)
        body = await patch_webhook(
            EntityRef(str(update_payload.resource_id)),
            WebhookPatch.model_validate(update_payload.data),
            None,
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.reason,
        )
    elif request.operation == "webhook.rotate":
        rotate_payload = _ResourceDelete.model_validate(request.payload)
        body = await rotate_webhook(
            EntityRef(str(rotate_payload.resource_id)),
            None,
            auth,
            session,
            redis,
            snowflake,
            settings,
            rotate_payload.reason,
        )
    elif request.operation == "webhook.delete":
        delete_payload = _ResourceDelete.model_validate(request.payload)
        await delete_webhook(
            EntityRef(str(delete_payload.resource_id)),
            None,
            auth,
            session,
            redis,
            snowflake,
            settings,
            delete_payload.reason,
        )
        body = None
        status_code = 204
    elif request.operation.startswith("webhook.e2ee."):
        from app.api.webhook_e2ee import (
            get_webhook_e2ee_participation_local,
            grant_webhook_e2ee_participation_local,
            revoke_webhook_e2ee_participation_local,
        )

        e2ee_payload = _ResourceMutation.model_validate(request.payload)
        if set(e2ee_payload.data) != {"channel_ref"} or not isinstance(
            e2ee_payload.data.get("channel_ref"), str
        ):
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
        channel_ref = EntityRef(cast(str, e2ee_payload.data["channel_ref"]))
        if request.operation == "webhook.e2ee.get":
            body = await get_webhook_e2ee_participation_local(
                e2ee_payload.resource_id,
                channel_ref,
                auth,
                session,
                redis,
                settings,
            )
        elif request.operation == "webhook.e2ee.grant":
            body = await grant_webhook_e2ee_participation_local(
                e2ee_payload.resource_id,
                channel_ref,
                auth,
                session,
                redis,
                snowflake,
                settings,
                e2ee_payload.reason,
            )
        elif request.operation == "webhook.e2ee.revoke":
            body = await revoke_webhook_e2ee_participation_local(
                e2ee_payload.resource_id,
                channel_ref,
                auth,
                session,
                redis,
                snowflake,
                settings,
                e2ee_payload.reason,
            )
        else:
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    elif request.operation.startswith("webhook.avatar."):
        status_code, body = await _dispatch_webhook_avatar(
            request, actor, session, redis, snowflake, settings
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_soundboard(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.soundboard import (
        create_human_soundboard_sound,
        create_human_soundboard_ticket,
        delete_human_soundboard_sound,
        update_human_soundboard_sound,
    )
    from app.media.schemas import UploadTicketRequest
    from app.voice.schemas import SoundboardSoundCreate, SoundboardSoundUpdate

    auth = _auth(actor)
    guild_ref = _guild_ref(int(request.guild.id))
    response = Response()
    body: object
    status_code = 200
    if request.operation == "soundboard.ticket":
        ticket_payload = _CreateOrUpdate.model_validate(request.payload)
        body = await create_human_soundboard_ticket(
            guild_ref,
            UploadTicketRequest.model_validate(ticket_payload.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
        )
        status_code = 201
    elif request.operation == "soundboard.create":
        create_payload = _CreateOrUpdate.model_validate(request.payload)
        body = await create_human_soundboard_sound(
            guild_ref,
            SoundboardSoundCreate.model_validate(create_payload.data),
            response,
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        status_code = 202 if response.status_code == 202 else 201
    elif request.operation == "soundboard.update":
        update_payload = _ScheduledMutation.model_validate(request.payload)
        body = await update_human_soundboard_sound(
            guild_ref,
            EntityRef(update_payload.resource_ref),
            SoundboardSoundUpdate.model_validate(update_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.reason,
        )
    elif request.operation == "soundboard.delete":
        delete_payload = _ScheduledDelete.model_validate(request.payload)
        await delete_human_soundboard_sound(
            guild_ref,
            EntityRef(delete_payload.resource_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            delete_payload.reason,
        )
        body = None
        status_code = 204
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_tracker_lane(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> tuple[int, object]:
    from app.tracker.schemas import TrackerLaneCreate, TrackerLaneMove, TrackerLaneUpdate
    from app.tracker.service import create_lane, delete_lane, move_lane, update_lane

    auth = _auth(actor)
    if request.operation == "tracker.lane.create":
        create_payload = _TrackerData.model_validate(request.payload)
        body = await create_lane(
            session,
            redis,
            snowflake,
            settings,
            auth,
            EntityRef(create_payload.channel_ref),
            TrackerLaneCreate.model_validate(create_payload.data),
        )
        return 201, body
    if request.operation == "tracker.lane.delete":
        delete_payload = _TrackerResource.model_validate(request.payload)
        await delete_lane(
            session,
            redis,
            settings,
            auth,
            EntityRef(delete_payload.channel_ref),
            EntityRef(delete_payload.resource_ref),
            delete_payload.if_match,
        )
        return 204, None
    mutation_payload = _TrackerResourceData.model_validate(request.payload)
    route_args = (
        session,
        redis,
        settings,
        auth,
        EntityRef(mutation_payload.channel_ref),
        EntityRef(mutation_payload.resource_ref),
    )
    if request.operation == "tracker.lane.move":
        body = await move_lane(
            *route_args,
            TrackerLaneMove.model_validate(mutation_payload.data),
            mutation_payload.if_match,
        )
    else:
        body = await update_lane(
            *route_args,
            TrackerLaneUpdate.model_validate(mutation_payload.data),
            mutation_payload.if_match,
        )
    return 200, body


async def _dispatch_tracker_task(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> tuple[int, object]:
    from app.tracker.schemas import TrackerTaskCreate, TrackerTaskMove, TrackerTaskUpdate
    from app.tracker.service import create_task, delete_task, move_task, update_task

    auth = _auth(actor)
    if request.operation == "tracker.task.create":
        create_payload = _TrackerData.model_validate(request.payload)
        body = await create_task(
            session,
            redis,
            snowflake,
            settings,
            auth,
            EntityRef(create_payload.channel_ref),
            TrackerTaskCreate.model_validate(create_payload.data),
        )
        return 201, body
    if request.operation == "tracker.task.delete":
        delete_payload = _TrackerResource.model_validate(request.payload)
        await delete_task(
            session,
            redis,
            settings,
            auth,
            EntityRef(delete_payload.channel_ref),
            EntityRef(delete_payload.resource_ref),
            delete_payload.if_match,
        )
        return 204, None
    mutation_payload = _TrackerResourceData.model_validate(request.payload)
    route_args = (
        session,
        redis,
        settings,
        auth,
        EntityRef(mutation_payload.channel_ref),
        EntityRef(mutation_payload.resource_ref),
    )
    if request.operation == "tracker.task.move":
        body = await move_task(
            *route_args,
            TrackerTaskMove.model_validate(mutation_payload.data),
            mutation_payload.if_match,
        )
    else:
        body = await update_task(
            *route_args,
            TrackerTaskUpdate.model_validate(mutation_payload.data),
            mutation_payload.if_match,
        )
    return 200, body


async def _dispatch_tracker(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    body: object
    if request.operation == "tracker.board.update":
        from app.tracker.schemas import TrackerBoardUpdate
        from app.tracker.service import update_board

        board_payload = _TrackerData.model_validate(request.payload)
        body = await update_board(
            session,
            redis,
            settings,
            _auth(actor),
            EntityRef(board_payload.channel_ref),
            TrackerBoardUpdate.model_validate(board_payload.data),
            board_payload.if_match,
        )
        status_code = 200
    elif request.operation.startswith("tracker.lane."):
        status_code, body = await _dispatch_tracker_lane(
            request, actor, session, redis, snowflake, settings
        )
    elif request.operation.startswith("tracker.task."):
        status_code, body = await _dispatch_tracker_task(
            request, actor, session, redis, snowflake, settings
        )
    else:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, status_code, body)


async def _dispatch_voice(
    request: GuildManagementRequest,
    settings: Settings,
) -> GuildManagementResult:
    from app.voice.regions import configured_voice_regions

    _Empty.model_validate(request.payload)
    return _result(
        request,
        200,
        [region.model_dump(mode="json") for region in configured_voice_regions(settings)],
    )


async def _dispatch_voice_member(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.voice import (
        disconnect_member_voice,
        move_member_voice,
        update_member_voice_moderation,
    )
    from app.voice.schemas import VoiceModerationUpdate, VoiceMoveRequest

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "voice_member.disconnect":
        mutation = _VoiceMemberDelete.model_validate(request.payload)
        await disconnect_member_voice(
            guild_ref,
            EntityRef(mutation.resource_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            mutation.reason,
        )
    else:
        mutation_data = _VoiceMemberMutation.model_validate(request.payload)
        args = (
            guild_ref,
            EntityRef(mutation_data.resource_ref),
        )
        if request.operation == "voice_member.update":
            await update_member_voice_moderation(
                *args,
                VoiceModerationUpdate.model_validate(mutation_data.data),
                auth,
                session,
                redis,
                snowflake,
                settings,
                mutation_data.reason,
            )
        elif request.operation == "voice_member.move":
            await move_member_voice(
                *args,
                VoiceMoveRequest.model_validate(mutation_data.data),
                auth,
                session,
                redis,
                snowflake,
                settings,
                mutation_data.reason,
            )
        else:
            raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})
    return _result(request, 204)


async def _dispatch_voice_message_capability(
    request: GuildManagementRequest,
    guild: Guild,
    session: AsyncSession,
) -> GuildManagementResult:
    from app.chat.voice_messages import guild_voice_message_capability

    _Empty.model_validate(request.payload)
    capability = await guild_voice_message_capability(session, guild)
    return _result(request, 200, capability.model_dump(mode="json"))


async def _dispatch_guild_core(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.guild_lifecycle import delete_guild, transfer_guild_ownership
    from app.api.management import update_guild
    from app.chat.schemas import GuildOwnershipTransfer, GuildUpdate

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "guild.update":
        update_payload = _VersionedMutation.model_validate(request.payload)
        update_body = await update_guild(
            guild_ref,
            GuildUpdate.model_validate(update_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.if_match,
            update_payload.reason,
        )
        return _result(request, 200, update_body)
    if request.operation == "guild.owner.transfer":
        transfer_payload = _VersionedMutation.model_validate(request.payload)
        transfer_body = await transfer_guild_ownership(
            guild_ref,
            GuildOwnershipTransfer.model_validate(transfer_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            transfer_payload.if_match,
            transfer_payload.reason,
        )
        return _result(request, 200, transfer_body)
    delete_payload = _VersionedDelete.model_validate(request.payload)
    await delete_guild(
        guild_ref,
        auth,
        session,
        redis,
        settings,
        delete_payload.if_match,
    )
    return _result(request, 204)


async def _dispatch_channel_core(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.guilds import (
        create_channel,
        delete_overwrite,
        list_overwrites,
        put_overwrite,
        sync_channel_permissions,
    )
    from app.api.management import delete_channel, reorder_channels
    from app.chat.schemas import ChannelCreate, ChannelPositionBatch, OverwritePut

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "channel.create":
        create_payload = _CreateOrUpdate.model_validate(request.payload)
        create_body = await create_channel(
            guild_ref,
            ChannelCreate.model_validate(create_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        return _result(request, 201, create_body)
    if request.operation == "channel.reorder":
        reorder_payload = _CreateOrUpdate.model_validate(request.payload)
        await reorder_channels(
            guild_ref,
            ChannelPositionBatch.model_validate(reorder_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            reorder_payload.reason,
        )
        return _result(request, 204)
    if request.operation == "channel.delete":
        delete_payload = _ChannelDelete.model_validate(request.payload)
        delete_body = await delete_channel(
            guild_ref,
            EntityRef(delete_payload.channel_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            delete_payload.reason,
        )
        return _result(request, 200, delete_body)
    if request.operation == "channel.overwrite.list":
        list_payload = _WebhookChannel.model_validate(request.payload)
        list_body = await list_overwrites(
            guild_ref,
            EntityRef(list_payload.channel_ref),
            auth,
            session,
            redis,
            settings,
        )
        channel_id, channel_domain = EntityRef(list_payload.channel_ref).resolve(
            request.guild.domain
        )
        return _result(
            request,
            200,
            {
                "guild_id": request.guild.id,
                "guild_domain": request.guild.domain,
                "channel_id": str(channel_id),
                "channel_domain": channel_domain,
                "overwrites": list_body,
            },
        )
    if request.operation == "channel.overwrite.put":
        put_payload = _ChannelMutation.model_validate(request.payload)
        put_body = await put_overwrite(
            guild_ref,
            EntityRef(put_payload.channel_ref),
            OverwritePut.model_validate(put_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            put_payload.reason,
        )
        return _result(request, 200, put_body)
    if request.operation == "channel.overwrite.delete":
        overwrite_delete_payload = _OverwriteDelete.model_validate(request.payload)
        await delete_overwrite(
            guild_ref,
            EntityRef(overwrite_delete_payload.channel_ref),
            overwrite_delete_payload.target_type,
            EntityRef(overwrite_delete_payload.target_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            overwrite_delete_payload.reason,
        )
        return _result(request, 204)
    sync_payload = _ChannelDelete.model_validate(request.payload)
    sync_body = await sync_channel_permissions(
        guild_ref,
        EntityRef(sync_payload.channel_ref),
        auth,
        session,
        redis,
        snowflake,
        settings,
        sync_payload.reason,
    )
    return _result(request, 200, sync_body)


async def _dispatch_roles(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.guilds import create_role
    from app.api.management import delete_role, reorder_roles, update_role
    from app.chat.schemas import RoleCreate, RolePositionBatch, RoleUpdate

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "role.create":
        create_payload = _CreateOrUpdate.model_validate(request.payload)
        create_body = await create_role(
            guild_ref,
            RoleCreate.model_validate(create_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        return _result(request, 201, create_body)
    if request.operation == "role.reorder":
        reorder_payload = _CreateOrUpdate.model_validate(request.payload)
        reorder_body = await reorder_roles(
            guild_ref,
            RolePositionBatch.model_validate(reorder_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            reorder_payload.reason,
        )
        return _result(request, 200, reorder_body)
    if request.operation == "role.update":
        update_payload = _ResourceRefMutation.model_validate(request.payload)
        update_body = await update_role(
            guild_ref,
            EntityRef(update_payload.resource_ref),
            RoleUpdate.model_validate(update_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.if_match,
            update_payload.reason,
        )
        return _result(request, 200, update_body)
    delete_payload = _ResourceRefDelete.model_validate(request.payload)
    await delete_role(
        guild_ref,
        EntityRef(delete_payload.resource_ref),
        auth,
        session,
        redis,
        snowflake,
        settings,
        delete_payload.reason,
    )
    return _result(request, 204)


async def _dispatch_members(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.management import assign_role, remove_role, replace_member_roles
    from app.api.moderation import (
        ban_member,
        kick_member,
        list_bans,
        remove_ban,
        update_member,
    )
    from app.chat.schemas import BanCreate, MemberRoleSet, MemberUpdate

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "member.update":
        update_payload = _MemberMutation.model_validate(request.payload)
        update_body = await update_member(
            guild_ref,
            EntityRef(update_payload.user_ref),
            MemberUpdate.model_validate(update_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            update_payload.reason,
        )
        return _result(request, 200, update_body)
    if request.operation == "member.role.assign":
        assign_payload = _MemberRole.model_validate(request.payload)
        await assign_role(
            guild_ref,
            EntityRef(assign_payload.user_ref),
            EntityRef(assign_payload.role_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            assign_payload.reason,
        )
        return _result(request, 204)
    if request.operation == "member.role.replace":
        replace_payload = _MemberRoleSet.model_validate(request.payload)
        replace_body = await replace_member_roles(
            guild_ref,
            EntityRef(replace_payload.user_ref),
            MemberRoleSet.model_validate(replace_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            replace_payload.reason,
        )
        return _result(request, 200, replace_body)
    if request.operation == "member.role.remove":
        remove_role_payload = _MemberRole.model_validate(request.payload)
        await remove_role(
            guild_ref,
            EntityRef(remove_role_payload.user_ref),
            EntityRef(remove_role_payload.role_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            remove_role_payload.reason,
        )
        return _result(request, 204)
    if request.operation == "member.kick":
        kick_payload = _MemberDelete.model_validate(request.payload)
        await kick_member(
            guild_ref,
            EntityRef(kick_payload.user_ref),
            auth,
            session,
            redis,
            snowflake,
            settings,
            kick_payload.reason,
        )
        return _result(request, 204)
    if request.operation == "member.ban.list":
        list_payload = _CollectionPage.model_validate(request.payload)
        list_body = await list_bans(
            guild_ref,
            list_payload.limit,
            EntityRef(list_payload.after) if list_payload.after is not None else None,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, list_body)
    if request.operation == "member.ban":
        ban_payload = _MemberMutation.model_validate(request.payload)
        await ban_member(
            guild_ref,
            EntityRef(ban_payload.user_ref),
            BanCreate.model_validate(ban_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            ban_payload.reason,
        )
        return _result(request, 204)
    unban_payload = _MemberDelete.model_validate(request.payload)
    await remove_ban(
        guild_ref,
        EntityRef(unban_payload.user_ref),
        auth,
        session,
        redis,
        snowflake,
        settings,
        unban_payload.reason,
    )
    return _result(request, 204)


async def _dispatch_instance_bans(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.moderation import ban_instance, list_instance_bans, remove_instance_ban
    from app.chat.schemas import InstanceBanCreate

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "instance_ban.list":
        list_payload = _CollectionPage.model_validate(request.payload)
        list_body = await list_instance_bans(
            guild_ref,
            list_payload.limit,
            list_payload.after,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, list_body)
    if request.operation == "instance_ban.put":
        put_payload = _InstanceBanMutation.model_validate(request.payload)
        await ban_instance(
            guild_ref,
            put_payload.instance_domain,
            InstanceBanCreate.model_validate(put_payload.data),
            auth,
            session,
            redis,
            snowflake,
            settings,
            put_payload.reason,
        )
        return _result(request, 204)
    remove_payload = _InstanceBanDelete.model_validate(request.payload)
    await remove_instance_ban(
        guild_ref,
        remove_payload.instance_domain,
        auth,
        session,
        redis,
        snowflake,
        settings,
        remove_payload.reason,
    )
    return _result(request, 204)


async def _dispatch_invites(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.invites import (
        create_invite,
        get_managed_invite,
        list_channel_invites,
        list_invites,
        local_get_invite_target_users,
        local_get_invite_target_users_job_status,
        local_update_invite_target_users,
        revoke_invite,
    )
    from app.chat.schemas import InviteCreate

    guild_ref = _guild_ref(int(request.guild.id))
    auth = _auth(actor)
    if request.operation == "invite.list":
        _Empty.model_validate(request.payload)
        list_body = await list_invites(guild_ref, auth, session, redis, settings)
        return _result(request, 200, list_body)
    if request.operation == "invite.list_channel":
        list_payload = _WebhookChannel.model_validate(request.payload)
        list_body = await list_channel_invites(
            EntityRef(list_payload.channel_ref),
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, list_body)
    if request.operation == "invite.get":
        get_payload = _InviteCode.model_validate(request.payload)
        body = await get_managed_invite(
            guild_ref,
            get_payload.code,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, body)
    if request.operation == "invite.create":
        create_payload = _InviteCreate.model_validate(request.payload)
        create_body = await create_invite(
            guild_ref,
            InviteCreate.model_validate(create_payload.data),
            Response(),
            auth,
            session,
            redis,
            snowflake,
            settings,
            create_payload.reason,
        )
        return _result(request, 201, create_body)
    if request.operation == "invite.target_users.get":
        target_payload = _InviteCode.model_validate(request.payload)
        body = await local_get_invite_target_users(
            target_payload.code,
            guild_ref,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, body)
    if request.operation == "invite.target_users.status":
        target_payload = _InviteCode.model_validate(request.payload)
        body = await local_get_invite_target_users_job_status(
            target_payload.code,
            guild_ref,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, 200, body)
    if request.operation == "invite.target_users.update":
        target_payload = _InviteTargetUsers.model_validate(request.payload)
        body = await local_update_invite_target_users(
            target_payload.code,
            target_payload.target_user_ids,
            guild_ref,
            auth,
            session,
            redis,
            snowflake,
            settings,
            target_payload.reason,
        )
        return _result(request, 200, body)
    revoke_payload = _InviteRevoke.model_validate(request.payload)
    body = await revoke_invite(
        revoke_payload.code,
        guild_ref,
        auth,
        session,
        redis,
        snowflake,
        settings,
        revoke_payload.reason,
    )
    return _result(request, 200, body)


async def _dispatch_channel(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.management import update_channel
    from app.chat.schemas import ChannelUpdate

    payload = _ChannelUpdate.model_validate(request.payload)
    body = await update_channel(
        _guild_ref(int(request.guild.id)),
        EntityRef(payload.channel_ref),
        ChannelUpdate.model_validate(payload.data),
        _auth(actor),
        session,
        redis,
        snowflake,
        settings,
        payload.if_match,
        payload.reason,
    )
    return _result(request, 200, body)


async def _dispatch_bot_e2ee(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.api.bot_e2ee import dispatch_bot_e2ee_management

    body = await dispatch_bot_e2ee_management(
        request,
        cast(Any, actor),
        session,
        redis,
        snowflake,
        settings,
    )
    return _result(request, 200, body)


async def _dispatch_voice_regions(
    request: GuildManagementRequest,
    _actor: object,
    _session: AsyncSession,
    _redis: Redis,
    _snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    return await _dispatch_voice(request, settings)


async def _dispatch_voice_status(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.db.models import User
    from app.voice.channel_info import voice_channel_status
    from app.voice.schemas import VoiceChannelStatusUpdate
    from app.voice.service import load_voice_channel
    from app.voice.status import set_voice_channel_status, voice_channel_status_payload

    mutation = (
        _VoiceStatusMutation.model_validate(request.payload)
        if request.operation == "voice_status.update"
        else _VoiceStatusQuery.model_validate(request.payload)
    )
    channel_id, channel_domain = EntityRef(mutation.channel_ref).resolve(settings.domain)
    channel, guild = await load_voice_channel(session, channel_id, channel_domain)
    if (guild.id, guild.origin_domain) != (int(request.guild.id), settings.domain):
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    if channel.type != 2:
        raise HTTPException(status_code=400, detail={"code": "VOICE_STATUS_VOICE_ONLY"})
    if request.operation == "voice_status.get":
        from app.chat.permissions import require_permissions
        from app.core.permissions import Permission

        await require_permissions(
            session,
            redis,
            guild,
            cast(User, actor),
            Permission.VIEW_CHANNEL,
            channel=channel,
        )
        current = await voice_channel_status(
            redis,
            guild.origin_domain,
            guild.id,
            channel.id,
        )
        return _result(request, 200, voice_channel_status_payload(guild, channel, current))
    if not isinstance(mutation, _VoiceStatusMutation):
        raise RuntimeError("voice status mutation parsing lost its operation binding")
    data = VoiceChannelStatusUpdate.model_validate(mutation.data)
    body = await set_voice_channel_status(
        session,
        redis,
        snowflake,
        settings,
        guild,
        channel,
        cast(User, actor),
        data.status,
        reason=mutation.reason,
    )
    return _result(request, 200, body)


async def _dispatch_voice_channel_info(
    request: GuildManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    _snowflake: SnowflakeGenerator,
    settings: Settings,
) -> GuildManagementResult:
    from app.db.models import Guild, User
    from app.voice.channel_info import visible_guild_channel_info

    query = _VoiceChannelInfoQuery.model_validate(request.payload)
    guild = await session.get(Guild, (int(request.guild.id), settings.domain))
    if guild is None or guild.unavailable:
        raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
    body = await visible_guild_channel_info(
        session,
        redis,
        guild,
        cast(User, actor),
        query.fields,
    )
    body["guild_domain"] = guild.origin_domain
    return _result(request, 200, body)


ManagementDispatcher = Callable[
    [
        GuildManagementRequest,
        object,
        AsyncSession,
        Redis,
        SnowflakeGenerator,
        Settings,
    ],
    Awaitable[GuildManagementResult],
]

_EXPRESSION_METADATA_OPERATIONS = frozenset(
    {
        "emoji.list",
        "emoji.get",
        "emoji.update",
        "sticker.list",
        "sticker.get",
        "sticker.update",
    }
)
_GUILD_CORE_OPERATIONS = frozenset({"guild.update", "guild.owner.transfer", "guild.delete"})
_CHANNEL_CORE_OPERATIONS = frozenset(
    {
        "channel.create",
        "channel.reorder",
        "channel.delete",
        "channel.overwrite.list",
        "channel.overwrite.put",
        "channel.overwrite.delete",
        "channel.permissions.sync",
    }
)
_ROLE_OPERATIONS = frozenset({"role.create", "role.update", "role.reorder", "role.delete"})
_MEMBER_OPERATIONS = frozenset(
    {
        "member.update",
        "member.role.assign",
        "member.role.replace",
        "member.role.remove",
        "member.kick",
        "member.ban.list",
        "member.ban",
        "member.unban",
    }
)
_INSTANCE_BAN_OPERATIONS = frozenset(
    {"instance_ban.list", "instance_ban.put", "instance_ban.remove"}
)
_INVITE_OPERATIONS = frozenset(
    {
        "invite.list",
        "invite.list_channel",
        "invite.get",
        "invite.create",
        "invite.revoke",
        "invite.target_users.get",
        "invite.target_users.update",
        "invite.target_users.status",
    }
)
_EXACT_MANAGEMENT_DISPATCHERS: dict[str, ManagementDispatcher] = {
    "channel.update": _dispatch_channel,
    "voice.regions": _dispatch_voice_regions,
    "voice_channel_info.get": _dispatch_voice_channel_info,
    "voice_status.get": _dispatch_voice_status,
    "voice_status.update": _dispatch_voice_status,
    **dict.fromkeys(_GUILD_CORE_OPERATIONS, _dispatch_guild_core),
    **dict.fromkeys(_CHANNEL_CORE_OPERATIONS, _dispatch_channel_core),
    **dict.fromkeys(_ROLE_OPERATIONS, _dispatch_roles),
    **dict.fromkeys(_MEMBER_OPERATIONS, _dispatch_members),
    **dict.fromkeys(_INSTANCE_BAN_OPERATIONS, _dispatch_instance_bans),
    **dict.fromkeys(_INVITE_OPERATIONS, _dispatch_invites),
    **dict.fromkeys(_EXPRESSION_METADATA_OPERATIONS, _dispatch_expression_metadata),
}
_PREFIX_MANAGEMENT_DISPATCHERS: tuple[tuple[str, ManagementDispatcher], ...] = (
    ("automod.", _dispatch_automod),
    ("moderation.", _dispatch_bulk_moderation),
    ("emoji.", _dispatch_expression_media),
    ("sticker.", _dispatch_expression_media),
    ("guild_asset.", _dispatch_guild_media),
    ("role_icon.", _dispatch_guild_media),
    ("scheduled_event.", _dispatch_scheduled_events),
    ("stage_instance.", _dispatch_stage_instances),
    ("stage_voice_state.", _dispatch_stage_instances),
    ("webhook.", _dispatch_webhooks),
    ("soundboard.", _dispatch_soundboard),
    ("tracker.", _dispatch_tracker),
    ("voice_member.", _dispatch_voice_member),
    ("bot_e2ee.", _dispatch_bot_e2ee),
)


def _management_dispatcher(operation: str) -> ManagementDispatcher | None:
    exact = _EXACT_MANAGEMENT_DISPATCHERS.get(operation)
    if exact is not None:
        return exact
    return next(
        (
            dispatcher
            for prefix, dispatcher in _PREFIX_MANAGEMENT_DISPATCHERS
            if operation.startswith(prefix)
        ),
        None,
    )


@router.post("/_kaede/v1/guilds/{guild_id}/management")
async def guild_management_authority(
    guild_id: int,
    request: GuildManagementRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "guild-management",
        capacity=240,
        refill_per_minute=240,
    )
    guild, actor = await authorize_guild_management_request(
        session,
        redis,
        settings,
        principal,
        guild_id,
        request,
    )
    try:
        if request.operation == "voice_message.capability":
            result = await _dispatch_voice_message_capability(request, guild, session)
        else:
            dispatcher = _management_dispatcher(request.operation)
            if dispatcher is None:
                raise HTTPException(
                    status_code=400,
                    detail={"code": "KAED_FED_GUILD_MANAGEMENT_OPERATION_UNSUPPORTED"},
                )
            result = await dispatcher(request, actor, session, redis, snowflake, settings)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    return result.model_dump(mode="json")
