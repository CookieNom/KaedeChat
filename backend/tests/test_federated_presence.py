from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app.db.models import Guild, User
from app.federation import presence
from app.federation.schemas import PresenceFederationRequest
from app.tasks import presence_fanout_projection, presence_fanout_state_is_current


class PresenceSession:
    def __init__(
        self,
        user: User,
        guilds: list[Guild],
        *,
        local_friend_ids: list[int] | None = None,
    ) -> None:
        self.user = user
        self.guilds = guilds
        self.local_friend_ids = local_friend_ids or []

    async def get(self, _model: object, key: object) -> User | None:
        return self.user if key == (self.user.id, self.user.origin_domain) else None

    async def scalars(self, statement: object) -> list[Any]:
        return self.guilds if "guilds" in str(statement) else self.local_friend_ids

    async def scalar(self, _statement: object) -> None:
        return None


class PresenceRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def eval(self, *args: Any) -> int:
        self.calls.append(args)
        return 1


@pytest.mark.asyncio
async def test_presence_fanout_includes_remote_friends_without_a_shared_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DestinationSession:
        def __init__(self) -> None:
            self.results = iter(
                [
                    ["member.test"],
                    ["guild.test"],
                    ["friend.test", "member.test"],
                ]
            )

        async def scalars(self, _statement: object) -> list[str]:
            return next(self.results)

    user = User(id=7, origin_domain="home.test", is_local=True, username="maple")

    async def no_block(_session: object, _domain: str) -> None:
        return None

    monkeypatch.setattr(presence, "matching_block", no_block)
    destinations = await presence.presence_destinations(  # type: ignore[arg-type]
        DestinationSession(),
        type("Settings", (), {"domain": "home.test"})(),
        user,
    )

    assert destinations == {"member.test", "guild.test", "friend.test"}


@pytest.mark.asyncio
async def test_presence_fanout_suppresses_silenced_guild_only_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DestinationSession:
        def __init__(self) -> None:
            self.results = iter(
                [
                    ["guild-only.test", "friend.test"],
                    [],
                    ["friend.test"],
                ]
            )

        async def scalars(self, _statement: object) -> list[str]:
            return next(self.results)

    async def block(_session: object, domain: str) -> object:
        assert domain in {"guild-only.test", "friend.test"}
        return type("Block", (), {"level": "silence"})()

    monkeypatch.setattr(presence, "matching_block", block)
    user = User(id=7, origin_domain="home.test", is_local=True, username="maple")

    assert await presence.presence_destinations(  # type: ignore[arg-type]
        DestinationSession(),
        type("Settings", (), {"domain": "home.test"})(),
        user,
    ) == {"friend.test"}


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
    session = PresenceSession(user, [guild])
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
        session, redis, type("Settings", (), {"domain": "home.test"})(), payload
    )
    assert len(redis.calls) == 1
    assert projected == [
        (
            "guild:home.test:9",
            {
                "user_id": "7",
                "user_domain": "remote.test",
                "status": "idle",
                "activities": [],
                "since": None,
                "afk": False,
                "client_status": {"web": "idle"},
            },
            1_000_000_000,
        )
    ]


@pytest.mark.asyncio
async def test_remote_friend_presence_is_projected_without_a_shared_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(id=7, origin_domain="remote.test", is_local=False, username="maple")
    session = PresenceSession(user, [], local_friend_ids=[11, 3, 11])
    redis = PresenceRedis()
    projected: list[str] = []

    async def publish(
        _redis: object,
        topic: str,
        _data: dict[str, object],
        *,
        user_domain: str,
        user_id: int,
        generation: int,
    ) -> bool:
        assert (user_domain, user_id, generation) == ("remote.test", 7, 1_000_000_000)
        projected.append(topic)
        return True

    monkeypatch.setattr(presence.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(presence, "publish_presence", publish)
    payload = PresenceFederationRequest(
        user_id="7",
        user_domain="remote.test",
        status="online",
        observed_at=1_000_000_000,
        expires_at=1_090,
    )

    assert await presence.receive_presence(  # type: ignore[arg-type]
        session, redis, type("Settings", (), {"domain": "home.test"})(), payload
    )
    assert projected == ["user:home.test:3", "user:home.test:11"]


@pytest.mark.asyncio
async def test_silenced_remote_presence_projects_to_friends_but_not_shared_guilds(
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

    class MixedSession:
        def __init__(self) -> None:
            self.guild_queries = 0

        async def get(self, _model: object, key: object) -> User | None:
            return user if key == (user.id, user.origin_domain) else None

        async def scalars(self, statement: object) -> list[Any]:
            sql = str(statement)
            if "guilds" in sql:
                self.guild_queries += 1
                return [guild]
            return [11]

        async def scalar(self, _statement: object) -> None:
            return None

    session = MixedSession()
    redis = PresenceRedis()
    projected: list[str] = []

    async def publish(
        _redis: object,
        topic: str,
        _data: dict[str, object],
        **_kwargs: object,
    ) -> bool:
        projected.append(topic)
        return True

    monkeypatch.setattr(presence.time, "time", lambda: 1_000.0)
    monkeypatch.setattr(presence, "publish_presence", publish)
    payload = PresenceFederationRequest(
        user_id="7",
        user_domain="remote.test",
        status="online",
        observed_at=1_000_000_000,
        expires_at=1_090,
    )

    assert await presence.receive_presence(  # type: ignore[arg-type]
        session,
        redis,
        type("Settings", (), {"domain": "home.test"})(),
        payload,
        include_guilds=False,
    )
    assert session.guild_queries == 0
    assert projected == ["user:home.test:11"]


@pytest.mark.asyncio
async def test_remote_presence_checks_user_admission_after_friend_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(id=7, origin_domain="remote.test", is_local=False, username="maple")

    class RestrictedFriendSession(PresenceSession):
        async def scalar(self, _statement: object) -> object:
            return type(
                "Restriction",
                (),
                {"restriction_type": "suspended", "expires_at": None},
            )()

    session = RestrictedFriendSession(user, [], local_friend_ids=[11])
    redis = PresenceRedis()
    monkeypatch.setattr(presence.time, "time", lambda: 1_000.0)
    payload = PresenceFederationRequest(
        user_id="7",
        user_domain="remote.test",
        status="online",
        observed_at=1_000_000_000,
        expires_at=1_090,
    )

    with pytest.raises(HTTPException) as caught:
        await presence.receive_presence(  # type: ignore[arg-type]
            session,
            redis,
            type("Settings", (), {"domain": "home.test"})(),
            payload,
            include_guilds=False,
        )

    assert caught.value.detail["code"] == "USER_SUSPENDED_FROM_INSTANCE"
    assert redis.calls == []


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


def test_presence_worker_drops_stale_fanout_and_accepts_final_offline_state() -> None:
    current = (
        '{"status":"online","generation":4,"expires_at":1090,'
        '"activities":[{"name":"Build queue","type":0,"state":"Federating"}],'
        '"since":123,"afk":true}'
    )
    invisible = '{"status":"invisible","generation":5,"expires_at":1090}'

    assert presence_fanout_state_is_current(current, "online", 4)
    assert not presence_fanout_state_is_current(current, "online", 3)
    assert presence_fanout_state_is_current(invisible, "offline", 5)
    assert presence_fanout_state_is_current(None, "offline", 6)
    assert not presence_fanout_state_is_current(None, "online", 6)
    assert presence_fanout_projection(current, "online", 4) == (
        [{"name": "Build queue", "type": 0, "state": "Federating"}],
        123,
        True,
    )


def test_remote_presence_lua_stores_validated_python_json_verbatim() -> None:
    assert "cjson." not in presence.SET_REMOTE_PRESENCE_SCRIPT
    assert "redis.call('SET', KEYS[2], ARGV[2])" in presence.SET_REMOTE_PRESENCE_SCRIPT


def test_federated_presence_rejects_undocumented_bot_activity_fields() -> None:
    with pytest.raises(ValueError):
        PresenceFederationRequest(
            user_id="7",
            user_domain="remote.test",
            status="online",
            activities=[{"name": "Build", "type": 0, "secrets": {"join": "leak"}}],
            observed_at=1_000_000_000,
            expires_at=1_090,
        )
    with pytest.raises(ValueError):
        PresenceFederationRequest(
            user_id="7",
            user_domain="remote.test",
            status="online",
            activities=[{"name": "Build", "type": True}],
            observed_at=1_000_000_000,
            expires_at=1_090,
        )
