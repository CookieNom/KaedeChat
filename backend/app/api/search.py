from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.db.models import User
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)
from app.search.meili import SearchUnavailable
from app.search.schemas import FederatedMessageSearchRequest, MessageSearchRequest
from app.search.service import federated_search_payload, local_search, search_with_authority

router = APIRouter(tags=["message search"])


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
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SEARCH_DISABLED_BY_INSTANCE",
                "message": "Message search is disabled on this instance.",
            },
        )
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
            await local_search(session, redis, settings, actor, request)
        )
    except SearchUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "KAED_FED_SEARCH_UNAVAILABLE", "retry_after_ms": 2000},
        ) from exc
