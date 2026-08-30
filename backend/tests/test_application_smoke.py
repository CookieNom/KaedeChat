from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest
from pydantic import ValidationError


def test_application_emoji_availability_is_read_only() -> None:
    from app.api.application_assets import ApplicationEmojiPatch

    assert ApplicationEmojiPatch.model_validate({"name": "renamed"}).name == "renamed"
    with pytest.raises(ValidationError):
        ApplicationEmojiPatch.model_validate({"name": "renamed", "available": False})


def test_api_and_gateway_register_all_routes() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "KAEDE_DOMAIN": "smoke.localhost",
            "KAEDE_ENVIRONMENT": "test",
            "KAEDE_SECRET_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
            "KAEDE_DATABASE_URL": "postgresql+asyncpg://test:test@postgres/test",
            "KAEDE_DRAGONFLY_URL": "redis://dragonfly:6379/0",
        }
    )
    program = textwrap.dedent(
        """
        import asyncio

        import httpx

        from app.gateway import app as gateway
        from app.main import app

        schema = app.openapi()
        operation_ids = [
            operation["operationId"]
            for path in schema["paths"].values()
            for method, operation in path.items()
            if method in {"get", "post", "put", "patch", "delete"}
        ]
        assert len(operation_ids) == len(set(operation_ids))
        assert "delete" in schema["paths"]["/api/v1/guilds/{guild_id}/members/@me"]
        assert "put" in schema["paths"]["/api/v1/guilds/{guild_id}/owner"]
        assert "delete" in schema["paths"]["/api/v1/guilds/{guild_id}"]
        assert "delete" in schema["paths"]["/_kaede/v1/guilds/{guild_id}/members/@me"]
        assert "get" in schema["paths"]["/api/v1/users/@me/guild-navigation"]
        assert "put" in schema["paths"]["/api/v1/users/@me/guild-navigation"]
        parity_routes = {
            ("get", "/api/v1/guilds/{guild_id}/audit-logs"),
            ("get", "/api/v1/guilds/{guild_ref}/auto-moderation/rules"),
            ("post", "/api/v1/bots/guilds/{guild_ref}/bulk-bans"),
            ("get", "/api/v1/bots/applications/@me/assets"),
            ("get", "/api/v1/bots/applications/@me/emojis"),
            ("get", "/api/v1/guilds/{guild_ref}/scheduled-events"),
            ("put", "/api/v1/guilds/{guild_ref}/scheduled-events/{event_ref}/users/@me"),
            ("post", "/api/v1/bots/guilds/{guild_ref}/scheduled-events"),
            (
                "get",
                "/api/v1/bots/guilds/{guild_ref}/scheduled-events/{event_ref}/users",
            ),
            ("post", "/api/v1/bots/channels/{channel_ref}/send-soundboard-sound"),
            (
                "post",
                "/api/v1/bots/channels/{channel_ref}/soundboard-playback-grants",
            ),
            ("post", "/api/v1/bots/interactions/{interaction_id}/callback"),
            (
                "put",
                "/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
            ),
            (
                "delete",
                "/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me",
            ),
            (
                "delete",
                "/api/v1/channels/{channel_id}/messages/{message_id}/reactions/{emoji}",
            ),
            (
                "put",
                "/api/v1/bots/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/@me",
            ),
            (
                "delete",
                "/api/v1/bots/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}/@me",
            ),
            (
                "delete",
                "/api/v1/bots/channels/{channel_ref}/messages/{message_ref}/reactions/{emoji}",
            ),
            (
                "post",
                "/_kaede/v1/channels/{target_channel_id}/announcement-follow-authorize",
            ),
            (
                "post",
                "/_kaede/v1/channels/{target_channel_id}/announcement-crossposts",
            ),
            ("post", "/_kaede/v1/dms/{conversation_id}/forward-resolve"),
        }
        for method, path in parity_routes:
            assert method in schema["paths"][path], f"missing {method.upper()} {path}"
        discord_200_posts = {
            "/api/v1/guilds/{guild_ref}/auto-moderation/rules",
            "/api/v1/bots/guilds/{guild_ref}/auto-moderation/rules",
            "/api/v1/guilds/{guild_ref}/scheduled-events",
            "/api/v1/bots/guilds/{guild_ref}/scheduled-events",
            "/api/v1/stage-instances",
            "/api/v1/bots/stage-instances",
            "/api/v1/guilds/{guild_id}/roles",
            "/api/v1/bots/guilds/{guild_ref}/roles",
            "/api/v1/channels/{channel_id}/messages",
            "/api/v1/bots/channels/{channel_ref}/messages",
            "/api/v1/users/@me/channels",
            "/api/v1/users/@me/channels/group",
            "/api/v1/bots/dms",
            "/api/v1/guilds/{guild_id}/invites",
            "/api/v1/bots/guilds/{guild_ref}/invites",
            "/api/v1/guilds/{guild_id}/channels/{channel_id}/webhooks",
            "/api/v1/bots/guilds/{guild_ref}/channels/{channel_ref}/webhooks",
            "/api/v1/channels/{channel_id}/followers",
            "/api/v1/bots/channels/{channel_ref}/followers",
            "/api/v1/bots/interactions/{interaction_id}/followups",
        }
        for path in discord_200_posts:
            responses = schema["paths"][path]["post"]["responses"]
            assert "200" in responses and "201" not in responses, path
        callback = schema["paths"][
            "/api/v1/bots/interactions/{interaction_id}/callback"
        ]["post"]
        assert {"200", "204"} <= callback["responses"].keys()
        assert "201" not in callback["responses"]
        assert "content" not in callback["responses"]["204"]
        assert callback["responses"]["200"]["content"]["application/json"]["schema"] == {
            "type": "object"
        }
        with_response = next(
            item for item in callback["parameters"] if item["name"] == "with_response"
        )
        assert with_response["schema"]["default"] is False
        for path in (
            "/api/v1/channels/{channel_ref}/send-soundboard-sound",
            "/api/v1/bots/channels/{channel_ref}/send-soundboard-sound",
        ):
            responses = schema["paths"][path]["post"]["responses"]
            assert "204" in responses and "200" not in responses, path
        for path in (
            "/api/v1/webhooks/{webhook_id}",
            "/api/v1/webhooks/{webhook_id}/{path_token}",
        ):
            responses = schema["paths"][path]["post"]["responses"]
            assert "200" in responses and "201" not in responses, path
        operation = schema["paths"]["/api/v1/channels/{channel_id}/messages"]["get"]
        parameter = next(item for item in operation["parameters"] if item["name"] == "channel_id")
        assert parameter["schema"]["type"] == "string"
        validation_schema = operation["responses"]["422"]["content"]["application/json"]["schema"]
        assert validation_schema["$ref"].endswith("/ErrorEnvelope")
        gateway.openapi()

        async def check_http_contract():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                missing = await client.get(
                    "/not-found", headers={"X-Kaede-Trace-Id": "smoke-trace"}
                )
                assert missing.json() == {
                    "code": "HTTP_404",
                    "message": "The requested item could not be found or is no longer available.",
                    "trace_id": "smoke-trace",
                }
                assert missing.headers["Cache-Control"] == "no-store"
                discovery = await client.get("/.well-known/kaede/server")
                assert discovery.status_code == 200
                assert discovery.headers["Cache-Control"].startswith("public,")
                assert discovery.json()["versions"] == ["1", "2"]
                assert "request-nonce/1" in discovery.json()["capabilities"]
                ambiguous = await client.post(
                    "/_kaede/v1/does-not-exist",
                    content=b'{"value":1,"value":2}',
                    headers={"Content-Type": "application/json"},
                )
                assert ambiguous.status_code == 400
                assert ambiguous.json()["code"] == "KAED_FED_INVALID_JSON"
                oversized_delete = await client.request(
                    "DELETE",
                    "/api/v1/does-not-exist",
                    content=b"x" * (2 * 1024 * 1024 + 1),
                )
                assert oversized_delete.status_code == 413
                assert oversized_delete.json()["code"] == "REQUEST_BODY_TOO_LARGE"

        asyncio.run(check_http_contract())
        """
    )
    subprocess.run(  # noqa: S603 - fixed interpreter executes the in-process smoke fixture
        [sys.executable, "-c", program],
        check=True,
        cwd=os.getcwd(),
        env=environment,
        timeout=20,
    )
