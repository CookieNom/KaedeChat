from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from anyio import CapacityLimiter
from fastapi import HTTPException, Response

import app.api.channels as channel_api
import app.api.media as media_api
from app.api.invites import get_invite
from app.core.rate_limits import CLIENT_RATE_LIMITS


class FakeRedis:
    def __init__(self, results: list[list[int]]) -> None:
        self.results = results
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> list[int]:
        self.calls.append(args)
        return self.results.pop(0)


async def test_typing_limit_runs_before_channel_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, dict[str, object]]] = []

    async def reject_typing(
        _redis: object,
        _response: Response,
        limit: object,
        **identity: object,
    ) -> None:
        calls.append((limit, identity))
        raise HTTPException(status_code=429, detail={"code": "RATE_LIMITED"})

    async def unexpected_lookup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("channel lookup ran before typing admission")

    monkeypatch.setattr(channel_api, "enforce_client_rate_limit", reject_typing)
    monkeypatch.setattr(channel_api, "load_channel_access", unexpected_lookup)
    user = SimpleNamespace(id=42, origin_domain="alpha.localhost")

    with pytest.raises(HTTPException) as raised:
        await channel_api.typing(
            cast(Any, "123"),
            Response(),
            auth=cast(Any, SimpleNamespace(user=user)),
            session=cast(Any, object()),
            redis=cast(Any, object()),
            settings=cast(Any, object()),
        )

    assert raised.value.status_code == 429
    assert calls == [
        (
            CLIENT_RATE_LIMITS["typing"],
            {"user_id": 42, "user_domain": "alpha.localhost"},
        )
    ]


async def test_local_invite_preview_rejection_precedes_database_lookup() -> None:
    class NoLookupSession:
        async def get(self, _model: object, _key: object) -> None:
            raise AssertionError("local invite lookup ran before source admission")

    redis = FakeRedis([[0, 0, 750, 5_000]])
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.7"),
    )
    settings = SimpleNamespace(proxy_secret=None, domain="alpha.localhost")

    with pytest.raises(HTTPException) as raised:
        await get_invite(
            "missing",
            cast(Any, request),
            Response(),
            session=cast(Any, NoLookupSession()),
            redis=cast(Any, redis),
            settings=cast(Any, settings),
        )

    assert raised.value.status_code == 429
    assert cast(dict[str, object], raised.value.detail)["code"] == "RATE_LIMITED"
    assert redis.calls[0][2] == "rate:client:invite-preview:203.0.113.7"


async def test_remote_media_rate_rejection_precedes_process_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = CapacityLimiter(1)
    monkeypatch.setattr(media_api, "remote_media_fetch_limiter", limiter)
    redis = FakeRedis([[0, 0, 750, 5_000]])

    with pytest.raises(HTTPException) as raised:
        async with media_api.remote_media_fetch_admission(
            cast(Any, redis),
            Response(),
            user_id=42,
            user_domain="alpha.localhost",
        ):
            raise AssertionError("rate-limited work was admitted")

    assert raised.value.status_code == 429
    assert cast(dict[str, object], raised.value.detail)["code"] == "RATE_LIMITED"
    assert limiter.borrowed_tokens == 0


async def test_remote_media_capacity_rejection_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = CapacityLimiter(1)
    holder = object()
    limiter.acquire_on_behalf_of_nowait(holder)
    monkeypatch.setattr(media_api, "remote_media_fetch_limiter", limiter)
    redis = FakeRedis([[1, 9, 0, 1_000]])
    try:
        with pytest.raises(HTTPException) as raised:
            async with media_api.remote_media_fetch_admission(
                cast(Any, redis),
                Response(),
                user_id=42,
                user_domain="alpha.localhost",
            ):
                raise AssertionError("work was admitted beyond process capacity")
    finally:
        limiter.release_on_behalf_of(holder)

    assert raised.value.status_code == 503
    assert cast(dict[str, object], raised.value.detail) == {
        "code": "REMOTE_MEDIA_BUSY",
        "retry_after_ms": 1_000,
    }
    assert raised.value.headers == {"Retry-After": "1"}


async def test_remote_media_admission_releases_its_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = CapacityLimiter(1)
    monkeypatch.setattr(media_api, "remote_media_fetch_limiter", limiter)
    redis = FakeRedis([[1, 9, 0, 1_000]])

    async with media_api.remote_media_fetch_admission(
        cast(Any, redis),
        Response(),
        user_id=42,
        user_domain="alpha.localhost",
    ):
        assert limiter.borrowed_tokens == 1

    assert limiter.borrowed_tokens == 0
    assert redis.calls[0][2] == "rate:client:remote-media-fetch:alpha.localhost:42"


async def test_remote_media_cache_hit_skips_fetch_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        message_id=9,
        message_domain="beta.localhost",
        deleted_at=None,
        updated_at=None,
    )
    message = SimpleNamespace(
        channel_id=7,
        channel_domain="beta.localhost",
        deleted_at=None,
    )
    cached = SimpleNamespace(
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scan_status="clean",
        object_key="beta.localhost/8/original",
        last_accessed_at=None,
    )

    class CacheHitSession:
        def __init__(self) -> None:
            self.commits = 0

        async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
            if model is media_api.Attachment:
                return attachment
            if model is media_api.Message:
                return message
            if model is media_api.RemoteMediaTombstone:
                return None
            if model is media_api.RemoteMediaCache:
                return cached
            raise AssertionError("unexpected model lookup")

        async def scalar(self, _statement: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

    class NoRateLimitRedis:
        async def eval(self, *_args: object) -> list[int]:
            raise AssertionError("cache hit consumed a remote-fetch rate token")

    class FakeStorage:
        def __init__(self, _settings: object) -> None:
            pass

        def presign(self, *_args: object, **_kwargs: object) -> str:
            return "https://objects.invalid/cached"

    def unexpected_admission(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("cache hit entered fetch admission")

    async def fake_access(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(guild=None, channel=SimpleNamespace(), participants=[])

    monkeypatch.setattr(media_api, "remote_media_fetch_admission", unexpected_admission)
    monkeypatch.setattr(media_api, "load_channel_access", fake_access)
    monkeypatch.setattr(media_api, "S3Storage", FakeStorage)
    session = CacheHitSession()
    response = await media_api.authorized_attachment(
        "beta.localhost",
        cast(Any, 8),
        Response(),
        variant="original",
        auth=cast(
            Any,
            SimpleNamespace(user=SimpleNamespace(id=42, origin_domain="alpha.localhost")),
        ),
        session=cast(Any, session),
        redis=cast(Any, NoRateLimitRedis()),
        settings=cast(
            Any,
            SimpleNamespace(
                domain="alpha.localhost",
                media_remote_cache_bucket="remote-cache",
            ),
        ),
    )

    assert response.status_code == 302
    assert response.headers["cache-control"] == "private, no-store"
    assert session.commits == 1
    assert cached.last_accessed_at is not None
