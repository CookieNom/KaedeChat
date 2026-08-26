from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import installation_for_channel, user_auth
from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.bots.auth import BotPrincipal, require_bot
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.tracker.schemas import (
    TrackerBoardUpdate,
    TrackerLaneCreate,
    TrackerLaneMove,
    TrackerLaneUpdate,
    TrackerTaskCreate,
    TrackerTaskMove,
    TrackerTaskUpdate,
)
from app.tracker.service import (
    create_lane,
    create_task,
    delete_lane,
    delete_task,
    get_board,
    move_lane,
    move_task,
    update_board,
    update_lane,
    update_task,
)

router = APIRouter(prefix="/api/v1/channels", tags=["tracker"])
bot_router = APIRouter(prefix="/api/v1/bots/channels", tags=["bot tracker"])


@router.get("/{channel_ref}/tracker")
async def get_tracker_board(
    channel_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await get_board(session, redis, settings, auth, channel_ref)


@router.patch("/{channel_ref}/tracker")
async def patch_tracker_board(
    channel_ref: EntityRef,
    payload: TrackerBoardUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    return await update_board(session, redis, settings, auth, channel_ref, payload, if_match)


@router.post("/{channel_ref}/tracker/lanes", status_code=status.HTTP_201_CREATED)
async def post_tracker_lane(
    channel_ref: EntityRef,
    payload: TrackerLaneCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await create_lane(session, redis, snowflake, settings, auth, channel_ref, payload)


@router.patch("/{channel_ref}/tracker/lanes/{lane_ref}")
async def patch_tracker_lane(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    payload: TrackerLaneUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    return await update_lane(
        session, redis, settings, auth, channel_ref, lane_ref, payload, if_match
    )


@router.delete("/{channel_ref}/tracker/lanes/{lane_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tracker_lane(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    await delete_lane(session, redis, settings, auth, channel_ref, lane_ref, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{channel_ref}/tracker/lanes/{lane_ref}/move")
async def post_tracker_lane_move(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    payload: TrackerLaneMove,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    return await move_lane(session, redis, settings, auth, channel_ref, lane_ref, payload, if_match)


@router.post("/{channel_ref}/tracker/tasks", status_code=status.HTTP_201_CREATED)
async def post_tracker_task(
    channel_ref: EntityRef,
    payload: TrackerTaskCreate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    return await create_task(session, redis, snowflake, settings, auth, channel_ref, payload)


@router.patch("/{channel_ref}/tracker/tasks/{task_ref}")
async def patch_tracker_task(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    payload: TrackerTaskUpdate,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    return await update_task(
        session, redis, settings, auth, channel_ref, task_ref, payload, if_match
    )


@router.delete("/{channel_ref}/tracker/tasks/{task_ref}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_tracker_task(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    await delete_task(session, redis, settings, auth, channel_ref, task_ref, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{channel_ref}/tracker/tasks/{task_ref}/move")
async def post_tracker_task_move(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    payload: TrackerTaskMove,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    return await move_task(session, redis, settings, auth, channel_ref, task_ref, payload, if_match)


async def bot_auth_for_channel(
    session: AsyncSession,
    settings: Settings,
    principal: BotPrincipal,
    channel_ref: EntityRef,
    *scopes: str,
) -> AuthenticatedUser:
    for scope in scopes:
        await installation_for_channel(session, settings, principal, channel_ref, scope)
    return user_auth(principal)


@bot_router.get("/{channel_ref}/tracker")
async def bot_get_tracker_board(
    channel_ref: EntityRef,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.read")
    return await get_board(session, redis, settings, auth, channel_ref)


@bot_router.patch("/{channel_ref}/tracker")
async def bot_patch_tracker_board(
    channel_ref: EntityRef,
    payload: TrackerBoardUpdate,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(
        session,
        settings,
        principal,
        channel_ref,
        "tasks.manage",
        "tasks.read",
    )
    return await update_board(session, redis, settings, auth, channel_ref, payload, if_match)


@bot_router.post("/{channel_ref}/tracker/lanes", status_code=status.HTTP_201_CREATED)
async def bot_post_tracker_lane(
    channel_ref: EntityRef,
    payload: TrackerLaneCreate,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.manage")
    return await create_lane(session, redis, snowflake, settings, auth, channel_ref, payload)


@bot_router.patch("/{channel_ref}/tracker/lanes/{lane_ref}")
async def bot_patch_tracker_lane(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    payload: TrackerLaneUpdate,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.manage")
    return await update_lane(
        session, redis, settings, auth, channel_ref, lane_ref, payload, if_match
    )


@bot_router.delete(
    "/{channel_ref}/tracker/lanes/{lane_ref}", status_code=status.HTTP_204_NO_CONTENT
)
async def bot_remove_tracker_lane(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.manage")
    await delete_lane(session, redis, settings, auth, channel_ref, lane_ref, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@bot_router.post("/{channel_ref}/tracker/lanes/{lane_ref}/move")
async def bot_post_tracker_lane_move(
    channel_ref: EntityRef,
    lane_ref: EntityRef,
    payload: TrackerLaneMove,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.manage")
    return await move_lane(session, redis, settings, auth, channel_ref, lane_ref, payload, if_match)


@bot_router.post("/{channel_ref}/tracker/tasks", status_code=status.HTTP_201_CREATED)
async def bot_post_tracker_task(
    channel_ref: EntityRef,
    payload: TrackerTaskCreate,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.write")
    return await create_task(session, redis, snowflake, settings, auth, channel_ref, payload)


@bot_router.patch("/{channel_ref}/tracker/tasks/{task_ref}")
async def bot_patch_tracker_task(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    payload: TrackerTaskUpdate,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.write")
    return await update_task(
        session, redis, settings, auth, channel_ref, task_ref, payload, if_match
    )


@bot_router.delete(
    "/{channel_ref}/tracker/tasks/{task_ref}", status_code=status.HTTP_204_NO_CONTENT
)
async def bot_remove_tracker_task(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> Response:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.write")
    await delete_task(session, redis, settings, auth, channel_ref, task_ref, if_match)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@bot_router.post("/{channel_ref}/tracker/tasks/{task_ref}/move")
async def bot_post_tracker_task_move(
    channel_ref: EntityRef,
    task_ref: EntityRef,
    payload: TrackerTaskMove,
    principal: BotPrincipal = Depends(require_bot),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    if_match: str | None = Header(default=None, alias="If-Match"),
) -> dict[str, object]:
    auth = await bot_auth_for_channel(session, settings, principal, channel_ref, "tasks.write")
    return await move_task(session, redis, settings, auth, channel_ref, task_ref, payload, if_match)
