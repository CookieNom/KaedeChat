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
    assert response.headers["X-RateLimit-Limit"] == "5"
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


def test_rate_limit_matrix_covers_every_normative_expensive_route() -> None:
    assert set(CLIENT_RATE_LIMITS) == {
        "message_send",
        "typing",
        "dm_open",
        "dm_group_create",
        "dm_group_mutate",
        "friend_request",
        "reaction",
        "invite_create",
        "invite_accept",
        "invite_preview",
        "invite_preview_destination",
        "invite_preview_global",
        "guild_create",
        "upload_ticket",
        "remote_media_fetch",
        "gif_search",
        "link_preview",
        "link_preview_media",
        "message_search",
        "self_moderation_status",
    }
    assert all(item.limit > 0 and item.period_seconds > 0 for item in CLIENT_RATE_LIMITS.values())
