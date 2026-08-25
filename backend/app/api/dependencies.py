from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.account_status import account_is_banned, account_is_temporarily_suspended
from app.auth.tokens import AccessGrant, AccessTokenStore
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.db.models import Session as AuthSession
from app.db.models import User


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


def get_snowflake(request: Request) -> SnowflakeGenerator:
    return cast(SnowflakeGenerator, request.app.state.snowflake)


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user: User
    grant: AccessGrant
    access_token: str
    cookie_authenticated: bool


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "AUTHENTICATION_REQUIRED", "message": "Authentication required"},
    )


def suspension_blocks_request(request: Request) -> bool:
    """Keep suspended accounts readable and able to secure/report their account."""

    if request.method not in {"POST", "PUT", "PATCH"}:
        return False
    path = request.url.path
    if path.startswith("/api/v1/auth/"):
        return False
    return not (
        path == "/api/v1/reports"
        or (path.startswith("/api/v1/reports/") and path.endswith("/attachment-evidence"))
    )


async def require_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    authorization = request.headers.get("Authorization", "")
    cookie_token = request.cookies.get("kc_access")
    bearer_token: str | None = None
    if authorization.lower().startswith("bearer "):
        bearer_token = authorization[7:]
        # An explicitly supplied, malformed bearer credential must never fall
        # through to an ambient cookie while also disabling the cookie CSRF
        # branch.
        if not bearer_token:
            raise unauthorized()
    token = bearer_token if bearer_token is not None else cookie_token
    if token is None:
        raise unauthorized()
    grant = await AccessTokenStore(redis, settings.access_token_ttl_seconds).get(token)
    if grant is None:
        raise unauthorized()
    now = datetime.now(UTC)
    user = await session.scalar(
        select(User)
        .join(AuthSession, AuthSession.id == grant.session_id)
        .where(
            User.id == grant.user_id,
            User.origin_domain == grant.user_domain,
            AuthSession.user_id == grant.user_id,
            AuthSession.user_domain == grant.user_domain,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > now,
            AuthSession.absolute_expires_at > now,
        )
    )
    if user is None or account_is_banned(user):
        raise unauthorized()
    cookie_authenticated = bearer_token is None and cookie_token is not None
    if (
        cookie_authenticated
        and request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.headers.get("X-Kaede-Client") != "web"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "CSRF_GUARD", "message": "Missing web client header"},
        )
    suspended_until = user.suspended_until
    if (
        suspension_blocks_request(request)
        and suspended_until is not None
        and account_is_temporarily_suspended(user, now=now)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "ACCOUNT_SUSPENDED",
                "message": (
                    "This account is temporarily suspended from creating or changing content"
                ),
                "suspended_until": suspended_until.isoformat(),
            },
        )
    return AuthenticatedUser(user, grant, token, cookie_authenticated)
