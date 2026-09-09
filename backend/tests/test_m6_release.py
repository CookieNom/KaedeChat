from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi import HTTPException, Response

from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit


class FakeRedis:
    def __init__(self, results: list[list[int]]) -> None:
        self.results = results
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> list[int]:
        self.calls.append(args)
        return self.results.pop(0)


async def test_client_bucket_emits_discord_style_headers() -> None:
    redis = FakeRedis([[1, 3, 0, 2000]])
    response = Response()
    await enforce_client_rate_limit(
        cast(Any, redis),
        response,
        CLIENT_RATE_LIMITS["message_send"],
        user_id=42,
        user_domain="alpha.localhost",
    )
    assert response.headers["X-RateLimit-Bucket"] == "message-send"
    assert response.headers["X-RateLimit-Limit"] == str(CLIENT_RATE_LIMITS["message_send"].limit)
    assert response.headers["X-RateLimit-Remaining"] == "3"
    assert response.headers["X-RateLimit-Reset-After"] == "2.000"
    assert redis.calls[0][2] == "rate:client:message-send:alpha.localhost:42"


async def test_client_bucket_rejection_has_retry_headers() -> None:
    redis = FakeRedis([[0, 0, 750, 5000]])
    with pytest.raises(HTTPException) as raised:
        await enforce_client_rate_limit(
            cast(Any, redis),
            Response(),
            CLIENT_RATE_LIMITS["reaction"],
            user_id=42,
            user_domain="alpha.localhost",
        )
    assert raised.value.status_code == 429
    assert raised.value.headers is not None
    assert raised.value.headers["Retry-After"] == "0.750"
    assert cast(dict[str, object], raised.value.detail)["retry_after_ms"] == 750


@pytest.mark.parametrize(
    ("module_name", "route_name", "bucket"),
    [
        ("channels", "create_message", "message_send"),
        ("channels", "typing", "typing"),
        ("dms", "open_direct_message_for", "dm_open"),
        ("dms", "create_group_direct_message", "dm_group_create"),
        ("dms", "mutate_group", "dm_group_mutate"),
        ("relationships", "request_friendship", "friend_request"),
        ("channels", "add_reaction", "reaction"),
        ("invites", "create_invite", "invite_create"),
        ("invites", "accept_invite", "invite_accept"),
        ("invites", "get_invite", "invite_preview"),
        ("invites", "get_invite", "invite_preview_destination"),
        ("invites", "get_invite", "invite_preview_global"),
        ("guilds", "create_guild", "guild_create"),
        ("media", "create_channel_attachment_ticket", "upload_ticket"),
        ("media", "remote_media_fetch_admission", "remote_media_fetch"),
        ("gifs", "list_gifs", "gif_search"),
        ("link_previews", "create_link_preview", "link_preview"),
        ("link_previews", "link_preview_media", "link_preview_media"),
        ("search", "search_messages", "message_search"),
        ("moderation", "self_moderation_status", "self_moderation_status"),
    ],
)
async def test_rate_limit_matrix_covers_every_normative_expensive_route(
    module_name, route_name, bucket
) -> None:
    import inspect
    from importlib import import_module
    from types import SimpleNamespace

    from starlette.requests import Request

    from app.chat.schemas import MessageCreate
    from app.core.types import EntityRef

    expected_bucket = bucket.replace("_", "-")
    identity = {
        "invite_preview": "127.0.0.1",
        "invite_preview_destination": "remote.example",
        "invite_preview_global": "outbound",
    }.get(bucket, "local.example:42")
    expected_key = f"rate:client:{expected_bucket}:{identity}"

    class Redis:
        calls = []

        async def eval(self, _script, _count, key, *args):
            self.calls.append(key)
            return [0, 0, 750, 5000] if key == expected_key else [1, 1, 0, 1000]

    class NoLocalWork:
        def __getattr__(self, name):
            raise AssertionError(f"local {name} reached before rate-limit rejection")

    redis = Redis()
    response = Response()
    route = getattr(import_module(f"app.api.{module_name}"), route_name)
    values = dict(
        auth=SimpleNamespace(user=SimpleNamespace(id=42, origin_domain="local.example")),
        session=NoLocalWork(),
        snowflake=NoLocalWork(),
        redis=redis,
        response=response,
        response_status=response,
        settings=SimpleNamespace(
            domain="local.example", proxy_secret=None, klipy_enabled=True, search_enabled=True
        ),
        payload=MessageCreate(content="hello")
        if route_name == "create_message"
        else SimpleNamespace(),
        body=SimpleNamespace(),
        channel_id=EntityRef("10@local.example"),
        channel_ref=EntityRef("10@local.example"),
        guild_id=EntityRef("20@local.example"),
        message_id=EntityRef("30@local.example"),
        action="rename",
        code="Abcd1234@remote.example",
        user_id=42,
        user_domain="local.example",
        origin_domain="remote.example",
        request=Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}),
    )
    kwargs = {name: values[name] for name in inspect.signature(route).parameters if name in values}
    with pytest.raises(HTTPException) as denied:
        if bucket == "remote_media_fetch":
            async with route(**kwargs):
                pytest.fail("remote fetch admitted after bucket rejection")
        else:
            await route(**kwargs)
    assert denied.value.status_code == 429
    assert denied.value.detail == {"code": "RATE_LIMITED", "retry_after_ms": 750}
    assert denied.value.headers["X-RateLimit-Bucket"] == expected_bucket
    assert denied.value.headers["Retry-After"] == "0.750"
    assert redis.calls[-1] == expected_key
