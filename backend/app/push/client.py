from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.federation import canonical_json
from app.core.settings import Settings
from app.federation.client import federation_signing_headers
from app.federation.network import FederationNetworkError, ensure_peer

RELAY_WAKE_PATH = "/_kaede/push/v1/wakes"


async def send_relay_wake(
    session: AsyncSession,
    settings: Settings,
    payload: dict[str, Any],
) -> httpx.Response:
    """Send one idempotent wake to the operator-pinned relay transport URL."""

    await ensure_peer(session, settings, settings.push_relay_origin)
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
            return await client.post(
                f"{settings.push_relay_url}{RELAY_WAKE_PATH}",
                content=body,
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise FederationNetworkError("push relay request failed") from exc


async def revoke_relay_subscription(
    session: AsyncSession,
    settings: Settings,
    subscription_id: str,
) -> None:
    path = f"/_kaede/push/v1/subscriptions/{subscription_id}"
    await ensure_peer(session, settings, settings.push_relay_origin)
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
