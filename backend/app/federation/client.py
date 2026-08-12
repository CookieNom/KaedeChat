from __future__ import annotations

import base64
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import anyio
import httpx
from anyio import WouldBlock
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.federation import (
    SigningInput,
    canonical_json,
    canonical_request_target,
    content_sha256,
    sign_request,
)
from app.core.settings import Settings
from app.db.models import Instance
from app.federation.network import (
    FederationNetworkError,
    bounded_http_request,
    ensure_peer,
    normalize_domain,
    peer_http_client,
)
from app.federation.security import (
    lock_block_policy_shared,
    matching_block,
    self_private_key,
)

MAX_FEDERATION_RESPONSE_BYTES = 4 * 1024 * 1024
# Keep slow or hostile peers from occupying every SQLAlchemy connection in a
# worker while request signing/policy locks remain attached to the caller's
# transaction. The PostgreSQL destination lock below supplies cross-worker
# serialization; this limiter bounds distinct slow destinations per process.
OUTBOUND_FEDERATION_LIMITER = anyio.CapacityLimiter(4)


def silence_blocks_path(path: str) -> bool:
    if path in {
        "/_kaede/v1/users/lookup",
        "/_kaede/v1/users/profile",
        "/_kaede/v1/invites/resolve",
    }:
        return True
    return path.startswith("/_kaede/v1/guilds/") and path.rsplit("/", 1)[-1] in {
        "snapshot",
        "events",
        "proxy",
        "join",
    }


async def federation_signing_headers(
    session: AsyncSession,
    settings: Settings,
    method: str,
    destination: str,
    request_target: str,
    body: bytes,
    *,
    hop: int = 1,
) -> dict[str, str]:
    key_id, private_key = await self_private_key(session, settings)
    timestamp = int(time.time())
    instance = await session.get(Instance, destination)
    use_nonce = instance is not None and "request-nonce/1" in instance.capabilities
    nonce = secrets.token_urlsafe(18) if use_nonce else None
    signing_input = SigningInput(
        method=method,
        request_target=request_target,
        origin=settings.domain,
        destination=destination,
        timestamp=timestamp,
        content_hash=content_sha256(body),
        nonce=nonce,
    )
    signature = base64.b64encode(sign_request(signing_input, private_key)).decode("ascii")
    headers = {
        "Authorization": f'Kaede origin="{settings.domain}",key="{key_id}",sig="{signature}"',
        "X-Kaede-Timestamp": str(timestamp),
        "X-Kaede-Version": "2" if nonce is not None else "1",
        "X-Kaede-Hop": str(hop),
        "Content-Type": "application/json",
    }
    if nonce is not None:
        headers["X-Kaede-Nonce"] = nonce
    return headers


async def signed_request(
    session: AsyncSession,
    settings: Settings,
    method: str,
    destination: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    request_timeout: float = 10,
    hop: int = 1,
    max_response_bytes: int = MAX_FEDERATION_RESPONSE_BYTES,
) -> httpx.Response:
    try:
        OUTBOUND_FEDERATION_LIMITER.acquire_nowait()
    except WouldBlock as exc:
        raise FederationNetworkError("outbound federation is busy; retry shortly") from exc
    try:
        with anyio.fail_after(request_timeout):
            base, client, target, body, headers = await _prepare_signed_request(
                session,
                settings,
                method,
                destination,
                path,
                payload=payload,
                query=query,
                request_timeout=request_timeout,
                hop=hop,
            )
            async with client:
                return await bounded_http_request(
                    client,
                    method,
                    f"{base}{target}",
                    max_response_bytes=max_response_bytes,
                    content=body,
                    headers=headers,
                )
    except httpx.HTTPError as exc:
        raise FederationNetworkError("federation request failed") from exc
    except TimeoutError as exc:
        raise FederationNetworkError("federation request exceeded its deadline") from exc
    finally:
        OUTBOUND_FEDERATION_LIMITER.release()


async def _prepare_signed_request(
    session: AsyncSession,
    settings: Settings,
    method: str,
    destination: str,
    path: str,
    *,
    payload: dict[str, Any] | None,
    query: dict[str, str] | None,
    request_timeout: float,
    hop: int,
) -> tuple[str, httpx.AsyncClient, str, bytes, dict[str, str]]:
    destination = normalize_domain(destination)
    if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        raise FederationNetworkError("unsafe federation request path")
    if not 0 <= hop <= 5:
        raise FederationNetworkError("federation hop count is outside the allowed range")
    # All outbound federation uses the same policy→destination order as block
    # administration. The locks remain attached to the caller's transaction,
    # fencing discovery and the network exchange against a completed block.
    await lock_block_policy_shared(session)
    destination_lock = await session.scalar(
        select(
            func.pg_try_advisory_xact_lock(
                func.hashtextextended(f"kaede-outbox-drain:{destination}", 0)
            )
        )
    )
    if destination_lock is not True:
        raise FederationNetworkError("another request to this peer is already in progress")
    if settings.federation_mode == "allowlist":
        approved = await session.get(Instance, destination)
        if approved is None or approved.is_self or approved.federation_mode != "allowlist":
            raise FederationNetworkError("peer is not allowlisted")
    block = await matching_block(session, destination)
    if block is not None and block.level == "suspend":
        raise FederationNetworkError("federation with this destination is suspended")
    if block is not None and block.level == "silence" and silence_blocks_path(path):
        raise FederationNetworkError("this federation surface is silenced for the destination")
    await ensure_peer(session, settings, destination)
    body = canonical_json(payload) if payload is not None else b""
    query_text = urlencode(sorted((query or {}).items()))
    target = canonical_request_target(path, query_text)
    headers = await federation_signing_headers(
        session, settings, method, destination, target, body, hop=hop
    )
    base, client = await peer_http_client(settings, destination, request_timeout=request_timeout)
    return base, client, target, body, headers


@asynccontextmanager
async def signed_stream_request(
    session: AsyncSession,
    settings: Settings,
    method: str,
    destination: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    query: dict[str, str] | None = None,
    request_timeout: float = 30,
    hop: int = 1,
    max_response_bytes: int = MAX_FEDERATION_RESPONSE_BYTES,
) -> AsyncIterator[httpx.Response]:
    """Yield one signed response stream with encoded-body and size guards."""
    try:
        OUTBOUND_FEDERATION_LIMITER.acquire_nowait()
    except WouldBlock as exc:
        raise FederationNetworkError("outbound federation is busy; retry shortly") from exc
    try:
        with anyio.fail_after(request_timeout):
            base, client, target, body, headers = await _prepare_signed_request(
                session,
                settings,
                method,
                destination,
                path,
                payload=payload,
                query=query,
                request_timeout=request_timeout,
                hop=hop,
            )
            headers = {**headers, "Accept-Encoding": "identity"}
            async with (
                client,
                client.stream(
                    method,
                    f"{base}{target}",
                    content=body,
                    headers=headers,
                ) as response,
            ):
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise FederationNetworkError("peer returned an encoded streaming response")
                declared = response.headers.get("Content-Length")
                if declared is not None:
                    try:
                        if not declared.isascii() or not declared.isdecimal():
                            raise ValueError
                        size = int(declared)
                    except ValueError:
                        raise FederationNetworkError(
                            "peer sent an invalid content length"
                        ) from None
                    if size > max_response_bytes:
                        raise FederationNetworkError("peer response exceeded the size limit")
                yield response
    except httpx.HTTPError as exc:
        raise FederationNetworkError("federation streaming request failed") from exc
    except TimeoutError as exc:
        raise FederationNetworkError("federation streaming request exceeded its deadline") from exc
    finally:
        OUTBOUND_FEDERATION_LIMITER.release()
