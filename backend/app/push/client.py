from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.federation import canonical_json
from app.core.settings import Settings
from app.federation.client import federation_signing_headers
from app.federation.network import FederationNetworkError, ensure_peer
from app.federation.security import lock_block_policy_shared, matching_block

RELAY_WAKE_PATH = "/_kaede/push/v1/wakes"


async def _require_relay_exchange_allowed(session: AsyncSession, destination: str) -> None:
    """Apply global suspension while leaving guild-only silence irrelevant."""

    await lock_block_policy_shared(session)
    block = await matching_block(session, destination)
    if block is not None and block.level == "suspend":
        raise FederationNetworkError("federation with the push relay is suspended")


async def _prepare_relay_exchange(session: AsyncSession, settings: Settings) -> None:
    destination = settings.push_relay_origin
    await _require_relay_exchange_allowed(session, destination)
    await ensure_peer(session, settings, destination)
    if destination == settings.domain:
        # The co-hosted relay verifies this signature in a separate transaction.
        # Publish discovery and release its locks before contacting the relay.
        await session.commit()
        await _require_relay_exchange_allowed(session, destination)


async def send_relay_wake(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, Any],
) -> httpx.Response:
    """Send one idempotent wake to the operator-pinned relay transport URL."""

    await _prepare_relay_exchange(session, settings)
    body = canonical_json(payload)
    headers = await federation_signing_headers(
        session,
        settings,
        "POST",
        settings.push_relay_origin,
        RELAY_WAKE_PATH,
        body,
    )
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.post(
                f"{settings.push_relay_url}{RELAY_WAKE_PATH}",
                content=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise FederationNetworkError("push relay request failed") from exc
    if response.status_code == 401:
        # A relay refreshing the home's key can temporarily reject a valid wake.
        # Keep it in the outbox for the next sweep until its normal expiry.
        raise FederationNetworkError("push relay authentication failed (HTTP 401)")
    return response


async def revoke_relay_subscription(
    session: AsyncSession,
    settings: Settings,
    subscription_id: str,
) -> None:
    path = f"/_kaede/push/v1/subscriptions/{subscription_id}"
    await _prepare_relay_exchange(session, settings)
    headers = await federation_signing_headers(
        session, settings, "DELETE", settings.push_relay_origin, path, b""
    )
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
            response = await client.delete(f"{settings.push_relay_url}{path}", headers=headers)
        if response.status_code not in {204, 404}:
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise FederationNetworkError("push relay revocation failed") from exc
