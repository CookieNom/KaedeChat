from __future__ import annotations

import os
import subprocess
import sys
import textwrap


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
        assert "delete" in schema["paths"]["/api/v1/guilds/{guild_id}/members/@me"]
        assert "put" in schema["paths"]["/api/v1/guilds/{guild_id}/owner"]
        assert "delete" in schema["paths"]["/api/v1/guilds/{guild_id}"]
        assert "delete" in schema["paths"]["/_kaede/v1/guilds/{guild_id}/members/@me"]
        assert "get" in schema["paths"]["/api/v1/users/@me/guild-navigation"]
        assert "put" in schema["paths"]["/api/v1/users/@me/guild-navigation"]
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
