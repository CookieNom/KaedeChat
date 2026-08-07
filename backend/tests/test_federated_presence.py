from __future__ import annotations

from typing import Any

import pytest

from app.db.models import Guild, User
from app.federation import presence
from app.federation.schemas import PresenceFederationRequest


class PresenceSession:
    def __init__(self, user: User, guild: Guild) -> None:
        self.user = user
        self.guild = guild

    async def get(self, _model: object, key: object) -> User | None:
        return self.user if key == (self.user.id, self.user.origin_domain) else None

    async def scalars(self, _statement: object) -> list[Guild]:
        return [self.guild]


class PresenceRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> int:
        self.calls.append(args)
        return 1


@pytest.mark.asyncio
async def test_remote_presence_is_ttl_bound_and_projected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(id=7, origin_domain="remote.test", is_local=False, username="maple")
    guild = Guild(
        id=9,
        origin_domain="home.test",
        name="Guild",
        owner_id=1,
        owner_domain="home.test",
    )
    session = PresenceSession(user, guild)
    redis = PresenceRedis()
    projected: list[tuple[str, dict[str, object], int]] = []

    async def publish(
        _redis: object,
        topic: str,
        data: dict[str, object],
        *,
        user_domain: str,
        user_id: int,
        generation: int,
    ) -> bool:
        assert (user_domain, user_id) == ("remote.test", 7)
        projected.append((topic, data, generation))
        return True

    monkeypatch.setattr(presence.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(presence, "publish_presence", publish)
    payload = PresenceFederationRequest(
        user_id="7",
        user_domain="remote.test",
        status="idle",
        observed_at=1_000_000_000,
        expires_at=1_090,
    )

    assert await presence.receive_presence(  # type: ignore[arg-type]
        session, redis, object(), payload
    )
    assert len(redis.calls) == 1
    assert projected == [
        (
            "guild:home.test:9",
            {"user_id": "7", "user_domain": "remote.test", "status": "idle"},
            1_000_000_000,
        )
    ]


@pytest.mark.asyncio
async def test_remote_presence_rejects_expired_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(presence.time, "time", lambda: 1_000.0)
    payload = PresenceFederationRequest(
        user_id="7",
        user_domain="remote.test",
        status="online",
        observed_at=999_000_000,
        expires_at=999,
    )

    assert not await presence.receive_presence(  # type: ignore[arg-type]
        object(), object(), object(), payload
    )
