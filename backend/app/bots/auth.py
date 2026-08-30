from __future__ import annotations

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
from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis, get_session
from app.auth.security import token_hash
from app.bots.dm_capability import dm_capability_runtime_ready, usable_dm_capability
from app.bots.installations import active_standard_installation_exists
from app.bots.runtime_control import (
    application_runtime_projection_exists,
    application_runtime_projection_ready,
)
from app.bots.worker_targets import (
    worker_target_allowed,
    worker_target_allowed_expression,
)
from app.core.base64url import decode_base64url, encode_base64url
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotToken,
    BotWorker,
)
from app.db.models import User

BOT_ACCESS_PREFIX = "kb1_at_"
BOT_ACCESS_TTL = timedelta(minutes=8)
PROOF_CLOCK_SKEW_SECONDS = 60
BOT_WORKER_REQUEST_LIMIT = ClientRateLimit("bot-worker", 600, 60)
BOT_APPLICATION_REQUEST_LIMIT = ClientRateLimit("bot-application", 1200, 60)


def encode_urlsafe(value: bytes) -> str:
    return encode_base64url(value)


def decode_urlsafe(value: str, *, length: int | None = None) -> bytes:
    return decode_base64url(value, size=length)


def worker_assertion_message(
    application_ref: str,
    worker_id: int,
    audience: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
    *,
    dm_capability_grant_id: str | None = None,
    dm_capability_revision: int | None = None,
) -> bytes:
    if (dm_capability_grant_id is None) != (dm_capability_revision is None):
        raise ValueError("worker DM capability assertion binding is incomplete")
    if dm_capability_grant_id is not None and dm_capability_revision is not None:
        return (
            f"kaede-worker-assertion-v2\n{application_ref}\n{worker_id}\n{audience}\n"
            f"{issued_at}\n{expires_at}\n{nonce}\n{dm_capability_grant_id}\n"
            f"{dm_capability_revision}"
        ).encode()
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
    dm_capability_grant_id: str | None = None
    dm_capability_revision: int | None = None
    installation_ref: str | None = None
    installation_type: str | None = None
    interaction_token: str | None = None

    def require_scope(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_SCOPE_REQUIRED", "scope": scope},
            )


def worker_runtime_ready(
    application: BotApplication,
    worker: BotWorker | None,
    runtime_target: BotApplicationTarget | None,
    *,
    target_domain: str,
    dm_capability: BotDMCapability | None = None,
    now: datetime | None = None,
) -> bool:
    """Validate the live worker, target, and optional exact DM delegation."""

    current = now or datetime.now(UTC)
    if (
        worker is None
        or (worker.application_id, worker.application_domain)
        != (application.id, application.origin_domain)
        or worker.revoked_at is not None
        or (worker.expires_at is not None and worker.expires_at <= current)
        or application.status != "active"
        or not worker_target_allowed(
            worker.target_domains,
            application_domain=application.origin_domain,
            target_domain=target_domain,
        )
        or not application_runtime_projection_ready(
            application,
            runtime_target,
            target_domain=target_domain,
        )
    ):
        return False
    if dm_capability is None:
        return True
    return dm_capability_runtime_ready(
        application,
        runtime_target,
        dm_capability,
        target_domain=target_domain,
        now=current,
    )


async def issue_bot_token(
    session: AsyncSession,
    *,
    token_id: int,
    worker: BotWorker,
    application: BotApplication,
    dpop_thumbprint: str,
    target_domain: str,
    dm_capability: BotDMCapability | None = None,
) -> tuple[BotToken, str]:
    runtime_target = None
    if application.origin_domain != target_domain or dm_capability is not None:
        runtime_target = await session.scalar(
            select(BotApplicationTarget)
            .where(
                BotApplicationTarget.application_id == application.id,
                BotApplicationTarget.application_domain == application.origin_domain,
                BotApplicationTarget.target_domain == target_domain,
            )
            .with_for_update()
        )
    if not worker_runtime_ready(
        application,
        worker,
        runtime_target,
        target_domain=target_domain,
        dm_capability=dm_capability,
    ):
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    raw = f"{BOT_ACCESS_PREFIX}{secrets.token_urlsafe(32)}"
    now = datetime.now(UTC)
    scopes = set(worker.scopes).intersection(application.default_scopes)
    intents = set(worker.intents).intersection(application.default_intents)
    expires_at = now + BOT_ACCESS_TTL
    if dm_capability is not None:
        if dm_capability.expires_at <= now:
            raise ValueError("cannot issue a token for an expired DM capability")
        scopes.intersection_update(dm_capability.granted_scopes)
        intents.intersection_update(dm_capability.granted_intents)
        expires_at = min(expires_at, dm_capability.expires_at)
    token = BotToken(
        id=token_id,
        token_hash=token_hash(raw),
        application_id=application.id,
        application_domain=application.origin_domain,
        worker_id=worker.id,
        dpop_thumbprint=dpop_thumbprint,
        scopes=sorted(scopes),
        intents=sorted(intents),
        dm_capability_id=dm_capability.id if dm_capability is not None else None,
        dm_capability_revision=(dm_capability.revision if dm_capability is not None else None),
        issued_at=now,
        expires_at=expires_at,
    )
    session.add(token)
    await session.flush()
    return token, raw


async def _authenticate_bot(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    *,
    require_active_installation: bool,
    application_authority: str | None = None,
    runtime_target_domain: str | None = None,
) -> BotPrincipal:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bot "):
        raise HTTPException(status_code=401, detail={"code": "BOT_AUTHENTICATION_REQUIRED"})
    raw = authorization[4:]
    if not raw.startswith(BOT_ACCESS_PREFIX) or len(raw) > 160:
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    now = datetime.now(UTC)
    conditions = [
        BotToken.token_hash == token_hash(raw),
        BotToken.revoked_at.is_(None),
        BotToken.expires_at > now,
        BotWorker.revoked_at.is_(None),
        (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > now),
        BotApplication.status == "active",
        User.account_type == "bot",
        User.disabled_at.is_(None),
    ]
    active_capability = exists(
        select(BotDMCapability.id).where(
            BotDMCapability.id == BotToken.dm_capability_id,
            BotDMCapability.revision == BotToken.dm_capability_revision,
            BotDMCapability.application_id == BotApplication.id,
            BotDMCapability.application_domain == BotApplication.origin_domain,
            BotDMCapability.bot_user_id == User.id,
            BotDMCapability.bot_user_domain == User.origin_domain,
            BotDMCapability.conversation_id.is_not(None),
            usable_dm_capability(at=now),
        )
    )
    if require_active_installation:
        if runtime_target_domain is None:
            raise RuntimeError("runtime bot authentication is missing its target authority")
        conditions.append(
            or_(
                and_(
                    BotToken.dm_capability_id.is_(None),
                    active_standard_installation_exists(
                        application_id=BotApplication.id,
                        application_domain=BotApplication.origin_domain,
                        bot_user_id=User.id,
                        bot_user_domain=User.origin_domain,
                        current_instance_domain=runtime_target_domain,
                    ),
                ),
                and_(BotToken.dm_capability_id.is_not(None), active_capability),
            )
        )
        conditions.append(
            or_(
                BotApplication.origin_domain == runtime_target_domain,
                application_runtime_projection_exists(runtime_target_domain),
            )
        )
        conditions.append(worker_target_allowed_expression(runtime_target_domain))
    if application_authority is not None:
        conditions.append(BotApplication.origin_domain == application_authority)
    row = (
        await session.execute(
            select(BotToken, BotWorker, BotApplication, User)
            .join(
                BotWorker,
                (BotWorker.id == BotToken.worker_id)
                & (BotWorker.application_id == BotToken.application_id)
                & (BotWorker.application_domain == BotToken.application_domain),
            )
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
            .where(*conditions)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    token, worker, application, user = row
    if user.account_type != "bot" or user.disabled_at is not None:
        # Retain a runtime fence as well as the SQL predicate so a stale ORM
        # identity can never turn an administratively disabled bot back on.
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    capability: BotDMCapability | None = None
    requested_capability = request.headers.get("X-Kaede-Bot-DM-Capability")
    requested_installation = request.headers.get(
        "X-Kaede-Bot-Source-Installation"
    ) or request.headers.get("X-Kaede-Bot-Installation")
    requested_installation_type = request.headers.get("X-Kaede-Bot-Installation-Type")
    if token.dm_capability_id is not None:
        capability = await session.scalar(
            select(BotDMCapability).where(
                BotDMCapability.id == token.dm_capability_id,
                BotDMCapability.revision == token.dm_capability_revision,
                BotDMCapability.application_id == application.id,
                BotDMCapability.application_domain == application.origin_domain,
                BotDMCapability.bot_user_id == user.id,
                BotDMCapability.bot_user_domain == user.origin_domain,
                usable_dm_capability(at=now),
            )
        )
        expected_installation = (
            f"{capability.source_installation_id}@{capability.source_installation_domain}"
            if capability is not None
            else None
        )
        if (
            capability is None
            or (requested_capability is not None and requested_capability != capability.grant_id)
            or (
                requested_installation is not None
                and requested_installation != expected_installation
            )
            or (
                requested_installation_type is not None
                and requested_installation_type != capability.source_kind
            )
        ):
            raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
        requested_capability = capability.grant_id
        requested_installation = expected_installation
        requested_installation_type = capability.source_kind
    elif requested_capability is not None:
        # A broad installation token can never be upgraded into a DM lease by
        # attaching a bearer-like header after issuance.
        raise HTTPException(status_code=401, detail={"code": "BOT_TOKEN_INVALID"})
    if require_active_installation:
        if runtime_target_domain is None:
            raise RuntimeError("runtime bot authentication is missing its target authority")
        runtime_target = None
        if application.origin_domain != runtime_target_domain or capability is not None:
            runtime_target = await session.scalar(
                select(BotApplicationTarget).where(
                    BotApplicationTarget.application_id == application.id,
                    BotApplicationTarget.application_domain == application.origin_domain,
                    BotApplicationTarget.target_domain == runtime_target_domain,
                )
            )
        if not worker_runtime_ready(
            application,
            worker,
            runtime_target,
            target_domain=runtime_target_domain,
            dm_capability=capability,
            now=now,
        ):
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
        dm_capability_grant_id=requested_capability,
        dm_capability_revision=capability.revision if capability is not None else None,
        installation_ref=requested_installation,
        installation_type=requested_installation_type,
        interaction_token=request.headers.get("X-Kaede-Interaction-Token"),
    )


async def require_bot(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BotPrincipal:
    """Authenticate a runtime bot at a currently installed target."""

    return await _authenticate_bot(
        request,
        session,
        redis,
        require_active_installation=True,
        runtime_target_domain=settings.domain,
    )


async def require_application_home_bot(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> BotPrincipal:
    """Authenticate a worker only for its application's home control plane.

    Application media and other app-owned resources exist independently of a
    local guild/user installation.  This dependency keeps those routes usable
    for remotely installed apps while refusing mirrored application rows.  A
    token authenticated here still fails every runtime route unless the same
    application also has a current installation on that target.
    """

    return await _authenticate_bot(
        request,
        session,
        redis,
        require_active_installation=False,
        application_authority=settings.domain,
    )
