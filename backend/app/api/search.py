from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bots import (
    bot_guild_installation_payload,
    bot_runtime_grant_payload,
    installation_for_guild,
    redact_bot_message_payload,
    require_bot_installation_intent,
    require_installation_scope,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.bots.auth import BotPrincipal, require_bot
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.core.types import EntityRef
from app.db.models import User
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    require_guild_federation_access,
)
from app.search.meili import SearchUnavailable
from app.search.schemas import FederatedMessageSearchRequest, MessageSearchRequest
from app.search.service import federated_search_payload, local_search, search_with_authority

router = APIRouter(tags=["message search"])


def _search_disabled() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "SEARCH_DISABLED_BY_INSTANCE",
            "message": "Message search is disabled on this instance.",
        },
    )


def _invalid_bot_search_projection() -> HTTPException:
    return HTTPException(status_code=503, detail={"code": "SEARCH_UNAVAILABLE"})


def _validated_bot_guild_search_item(
    item: object,
    guild_ref: tuple[int, str],
) -> tuple[dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
    """Fail closed if an internal or remote result loses exact guild linkage."""

    if not isinstance(item, dict):
        raise _invalid_bot_search_projection()
    raw_message = item.get("message")
    raw_channel = item.get("channel")
    raw_guild = item.get("guild")
    if not all(isinstance(value, dict) for value in (raw_message, raw_channel, raw_guild)):
        raise _invalid_bot_search_projection()
    message = dict(cast(dict[str, object], raw_message))
    channel = dict(cast(dict[str, object], raw_channel))
    projected_guild_payload = dict(cast(dict[str, object], raw_guild))
    try:
        message_channel = EntityRef(f"{message['channel_id']}@{message['channel_domain']}").resolve(
            guild_ref[1]
        )
        channel_ref = EntityRef(f"{channel['id']}@{channel['origin_domain']}").resolve(guild_ref[1])
        channel_guild = EntityRef(f"{channel['guild_id']}@{channel['guild_domain']}").resolve(
            guild_ref[1]
        )
        projected_guild = EntityRef(
            f"{projected_guild_payload['id']}@{projected_guild_payload['origin_domain']}"
        ).resolve(guild_ref[1])
    except (KeyError, TypeError, ValueError) as exc:
        raise _invalid_bot_search_projection() from exc
    if (
        message_channel != channel_ref
        or channel_guild != guild_ref
        or projected_guild != guild_ref
        or message.get("e2ee") is not None
    ):
        raise _invalid_bot_search_projection()
    return dict(item), message, channel, projected_guild_payload


@router.post("/api/v1/search/messages")
async def search_messages(
    body: MessageSearchRequest,
    response: Response,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.search_enabled:
        raise _search_disabled()
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["message_search"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    try:
        return await search_with_authority(session, redis, settings, auth.user, body)
    except SearchUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "SEARCH_UNAVAILABLE",
                "message": "Message search is temporarily unavailable. Try again shortly.",
            },
        ) from exc


@router.post("/api/v1/bots/guilds/{guild_ref}/messages/search")
async def bot_search_guild_messages(
    guild_ref: EntityRef,
    body: MessageSearchRequest,
    principal: Annotated[BotPrincipal, Depends(require_bot)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Search plaintext guild history at the exact guild authority."""

    if not settings.search_enabled:
        raise _search_disabled()
    if body.limit > 25:
        raise HTTPException(status_code=422, detail={"code": "BOT_SEARCH_LIMIT_INVALID"})
    guild, installation = await installation_for_guild(
        session,
        settings,
        principal,
        guild_ref,
        "messages.history",
    )
    require_installation_scope(principal, installation, "messages.content")
    require_bot_installation_intent(principal, installation, "message_content")
    expected_scope = (guild.id, guild.origin_domain)
    if (
        body.scope != "guild"
        or body.scope_ref is None
        or (body.scope_ref.resolve(settings.domain) != expected_scope)
    ):
        raise HTTPException(status_code=400, detail={"code": "BOT_SEARCH_SCOPE_INVALID"})
    try:
        result = await search_with_authority(
            session,
            redis,
            settings,
            principal.user,
            body,
        )
    except SearchUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "SEARCH_UNAVAILABLE"},
        ) from exc

    raw_results = result.get("results")
    if not isinstance(raw_results, list) or len(raw_results) > body.limit:
        raise _invalid_bot_search_projection()
    runtime = bot_runtime_grant_payload(installation)
    attachments = (
        "attachments.read" in principal.scopes and "attachments.read" in installation.granted_scopes
    )
    guild_runtime = bot_guild_installation_payload(installation)
    projected: list[dict[str, object]] = []
    for item in raw_results:
        safe, message, channel, item_guild = _validated_bot_guild_search_item(
            item,
            expected_scope,
        )
        safe["message"] = (
            redact_bot_message_payload(
                message,
                can_read_content=True,
                can_read_attachments=attachments,
                principal=principal,
                direct_message=False,
            )
            | runtime
        )
        safe["channel"] = channel | runtime
        safe["guild"] = item_guild | guild_runtime
        projected.append(safe)
    result["results"] = projected
    return result


@router.post("/_kaede/v1/search/messages")
async def federated_search_messages(
    body: FederatedMessageSearchRequest,
    principal: FederationPrincipal = Depends(authenticate_federation),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.search_enabled:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_SEARCH_UNSUPPORTED"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "message-search",
        capacity=600,
        refill_per_minute=600,
    )
    if body.scope == "guild":
        require_guild_federation_access(principal)
    actor_id, actor_domain = body.actor_ref.resolve(settings.domain)
    if actor_domain != principal.origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_SEARCH_ACTOR_INVALID"})
    actor = await session.get(User, (actor_id, actor_domain))
    if actor is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND"})
    request = MessageSearchRequest.model_validate(
        body.model_dump(exclude={"actor_ref"}, mode="json")
    )
    try:
        return federated_search_payload(
            await local_search(
                session,
                redis,
                settings,
                actor,
                request,
                dm_authority=settings.domain if request.scope == "dms" else None,
            )
        )
    except SearchUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_FED_SEARCH_UNAVAILABLE", "retry_after_ms": 2000},
        ) from exc
