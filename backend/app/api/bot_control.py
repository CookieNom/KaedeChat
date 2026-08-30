from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.applications import WorkerTokenRequest, authenticated_worker_assertion
from app.api.dependencies import get_redis, get_session, get_snowflake
from app.bots.auth import encode_urlsafe, issue_bot_token
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator

router = APIRouter(prefix="/api/v1", tags=["bot application control"])
BOT_HOME_TOKEN_LIMIT = ClientRateLimit("bot-home-token", 30, 60)


@router.post("/bot-workers/home-token")
async def create_application_home_token(
    payload: WorkerTokenRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    """Mint a DPoP token for app-owned resources at the application home.

    The route deliberately has no installation fallback and cannot mint for a
    mirrored application. Runtime APIs continue to use the separately fenced
    ``/bots/token`` route and ``require_bot`` dependency.
    """

    await enforce_keyed_rate_limit(
        redis,
        response,
        BOT_HOME_TOKEN_LIMIT,
        identity=f"application-home:{payload.application_ref}",
    )
    path = "/api/v1/bot-workers/home-token"
    worker, application, _ = await authenticated_worker_assertion(
        payload,
        session,
        redis,
        snowflake,
        settings,
        expected_audience=f"https://{settings.domain}{path}",
        replay_scope="application-home-token",
        local_application_only=True,
    )
    thumbprint = encode_urlsafe(hashlib.sha256(worker.public_key).digest())
    token, raw = await issue_bot_token(
        session,
        token_id=await snowflake.mint(),
        worker=worker,
        application=application,
        dpop_thumbprint=thumbprint,
        target_domain=settings.domain,
    )
    await session.commit()
    return {
        "access_token": raw,
        "token_type": "Bot",
        "expires_in": max(1, int((token.expires_at - datetime.now(UTC)).total_seconds())),
        "dpop_thumbprint": thumbprint,
    }
