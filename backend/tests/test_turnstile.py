from __future__ import annotations

import base64
from urllib.parse import parse_qs

import httpx
import pytest

from app.auth.turnstile import LOGIN_ACTION, TurnstileUnavailableError, verify_turnstile_token
from app.core.settings import Settings

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings() -> Settings:
    return Settings(
        domain="chat.example.com",
        environment="test",
        service_role="api",
        secret_key=VALID_KEY,
        database_url="postgresql+asyncpg://kaede:secret@postgres/kaede",
        dragonfly_url="redis://dragonfly:6379/0",
        email_backend="disabled",
        turnstile_enabled=True,
        turnstile_site_key="0x4AAAAAAExampleSiteKey",
        turnstile_secret="0x4AAAAAAExampleSecret",
    )


@pytest.mark.asyncio
async def test_turnstile_binds_token_to_action_and_hostname() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert request.url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
        assert form["response"] == ["single-use-token"]
        assert form["remoteip"] == ["192.0.2.1"]
        assert form["secret"] == ["0x4AAAAAAExampleSecret"]
        return httpx.Response(
            200,
            json={
                "success": True,
                "action": LOGIN_ACTION,
                "hostname": "chat.example.com",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await verify_turnstile_token(
            settings(), "single-use-token", "192.0.2.1", action=LOGIN_ACTION, client=client
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "action": LOGIN_ACTION, "hostname": "chat.example.com"},
        {"success": True, "action": "login", "hostname": "chat.example.com"},
        {"success": True, "action": "turnstile-spin-v2", "hostname": "evil.example"},
    ],
)
async def test_turnstile_rejects_failed_or_misbinding_results(result: dict[str, object]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=result))
    ) as client:
        assert not await verify_turnstile_token(
            settings(), "single-use-token", "192.0.2.1", action=LOGIN_ACTION, client=client
        )


@pytest.mark.asyncio
async def test_turnstile_fails_closed_when_provider_is_unavailable() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(503))
    ) as client:
        with pytest.raises(TurnstileUnavailableError):
            await verify_turnstile_token(
                settings(),
                "single-use-token",
                "192.0.2.1",
                action=LOGIN_ACTION,
                client=client,
            )
