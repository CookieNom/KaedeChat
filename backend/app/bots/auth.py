from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import Depends, HTTPException, Request, Response
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis, get_session
from app.auth.security import token_hash
from app.bots.installations import active_installation_exists
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.db.bot_models import BotApplication, BotToken, BotWorker
from app.db.models import User

BOT_ACCESS_PREFIX = "kb1_at_"
BOT_ACCESS_TTL = timedelta(minutes=8)
PROOF_CLOCK_SKEW_SECONDS = 60
BOT_WORKER_REQUEST_LIMIT = ClientRateLimit("bot-worker", 600, 60)
BOT_APPLICATION_REQUEST_LIMIT = ClientRateLimit("bot-application", 1200, 60)


def encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_urlsafe(value: str, *, length: int | None = None) -> bytes:
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("value is not canonical URL-safe base64") from exc
    if length is not None and len(decoded) != length:
        raise ValueError("value has an invalid length")
    return decoded


def worker_assertion_message(
    application_ref: str,
    worker_id: int,
    audience: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
) -> bytes:
    return (
        f"kaede-worker-assertion-v1\n{application_ref}\n{worker_id}\n{audience}\n"
        f"{issued_at}\n{expires_at}\n{nonce}"
    ).encode()


def dpop_message(request: Request, token: str, timestamp: int, nonce: str) -> bytes:
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return (
        f"kaede-dpop-v1\n{request.method.upper()}\n{target}\n{timestamp}\n{nonce}\n"
        f"{hashlib.sha256(token.encode()).hexdigest()}"
    ).encode()


@dataclass(frozen=True, slots=True)
class BotPrincipal:
    user: User
    application: BotApplication
    worker: BotWorker
    token: BotToken
    scopes: frozenset[str]
    intents: frozenset[str]

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
            )


async def issue_bot_token(
    session: AsyncSession,
    *,
    token_id: int,
    worker: BotWorker,
    application: BotApplication,
    dpop_thumbprint: str,
) -> tuple[BotToken, str]:
    raw = f"{BOT_ACCESS_PREFIX}{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    token = BotToken(
        id=token_id,
        token_hash=token_hash(raw),
        application_id=application.id,
        application_domain=application.origin_domain,
        worker_id=worker.id,
        dpop_thumbprint=dpop_thumbprint,
        scopes=list(set(worker.scopes).intersection(application.default_scopes)),
        intents=list(set(worker.intents).intersection(application.default_intents)),
        issued_at=now,
        expires_at=now + BOT_ACCESS_TTL,
    )
    session.add(token)
    await session.flush()
    return token, raw


async def require_bot(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> BotPrincipal:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bot "):
        raise HTTPException(status_code=401, detail={"code": "BOT_AUTHENTICATION_REQUIRED"})
    raw = authorization[4:]
    if not raw.startswith(BOT_ACCESS_PREFIX) or len(raw) > 160:
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    now = datetime.now(UTC)
    row = (
        await session.execute(
            select(BotToken, BotWorker, BotApplication, User)
            .join(BotWorker, BotWorker.id == BotToken.worker_id)
            .join(
                BotApplication,
                (BotApplication.id == BotToken.application_id)
                & (BotApplication.origin_domain == BotToken.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotToken.token_hash == token_hash(raw),
                BotToken.revoked_at.is_(None),
                BotToken.expires_at > now,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > now),
                BotApplication.status == "active",
                User.account_type == "bot",
                User.disabled_at.is_(None),
                active_installation_exists(
                    application_id=BotApplication.id,
                    application_domain=BotApplication.origin_domain,
                    bot_user_id=User.id,
                    bot_user_domain=User.origin_domain,
                ),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    token, worker, application, user = row
    if user.account_type != "bot" or user.disabled_at is not None:
        # Retain a runtime fence as well as the SQL predicate so a stale ORM
        # identity can never turn an administratively disabled bot back on.
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    timestamp_raw = request.headers.get("X-Kaede-Bot-Timestamp", "")
    nonce = request.headers.get("X-Kaede-Bot-Nonce", "")
    proof_raw = request.headers.get("X-Kaede-Bot-Proof", "")
    try:
        timestamp = int(timestamp_raw)
        proof = decode_urlsafe(proof_raw, length=64)
    except ValueError:
        raise HTTPException(status_code=401, detail={"code": "BOT_DPOP_INVALID"}) from None
    if (
        not nonce
        or len(nonce) > 128
        or abs(int(time.time()) - timestamp) > PROOF_CLOCK_SKEW_SECONDS
    ):
        raise HTTPException(status_code=401, detail={"code": "BOT_DPOP_INVALID"})
    thumbprint = encode_urlsafe(hashlib.sha256(worker.public_key).digest())
    if not token.dpop_thumbprint or not secrets.compare_digest(token.dpop_thumbprint, thumbprint):
        raise HTTPException(status_code=401, detail={"code": "BOT_DPOP_KEY_MISMATCH"})
    replay_key = f"bot:dpop:{token.id}:{hashlib.sha256(nonce.encode()).hexdigest()}"
    if not await redis.set(replay_key, "1", nx=True, ex=PROOF_CLOCK_SKEW_SECONDS * 2):
        raise HTTPException(status_code=401, detail={"code": "BOT_DPOP_REPLAYED"})
    try:
        Ed25519PublicKey.from_public_bytes(worker.public_key).verify(
            proof, dpop_message(request, raw, timestamp, nonce)
        )
    except (InvalidSignature, ValueError):
        await redis.delete(replay_key)
        raise HTTPException(status_code=401, detail={"code": "BOT_DPOP_INVALID"}) from None
    rate_response = Response()
    await enforce_keyed_rate_limit(
        redis,
        rate_response,
        BOT_APPLICATION_REQUEST_LIMIT,
        identity=f"{application.origin_domain}:{application.id}",
    )
    await enforce_keyed_rate_limit(
        redis,
        rate_response,
        BOT_WORKER_REQUEST_LIMIT,
        identity=f"{application.origin_domain}:{application.id}:{worker.id}",
    )
    token.last_used_at = now
    await session.commit()
    return BotPrincipal(
        user=user,
        application=application,
        worker=worker,
        token=token,
        scopes=frozenset(cast(list[str], token.scopes)),
        intents=frozenset(cast(list[str], token.intents)),
    )
