from inspect import unwrap
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app import tasks
from app.core.settings import Settings
from app.db.models import Channel, Guild
from app.federation import delivery as federation_delivery
from app.federation import guilds as federation_guilds
from app.federation.delivery import cleanup_federation_retention
from app.federation.guilds import (
    purge_orphaned_replicated_guilds,
    replicated_guild_sync_candidates,
)

LOCAL_DOMAIN = "alpha.localhost"
REMOTE_DOMAIN = "beta.localhost"


def config() -> Settings:
    return cast(
        Settings,
        SimpleNamespace(domain=LOCAL_DOMAIN, federation_event_retention_days=30),
    )


def remote_guild() -> Guild:
    return Guild(
        id=42,
        origin_domain=REMOTE_DOMAIN,
        name="Remote guild",
        owner_id=7,
        owner_domain=REMOTE_DOMAIN,
    )


def test_sync_sweep_candidates_require_a_local_membership() -> None:
    statement = replicated_guild_sync_candidates(LOCAL_DOMAIN, limit=7)
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "guilds.origin_domain != 'alpha.localhost'" in sql
    assert "guilds.sync_status IN ('stale', 'failed')" in sql
    assert "EXISTS (SELECT *" in sql
    assert "guild_members.guild_id = guilds.id" in sql
    assert "guild_members.guild_domain = guilds.origin_domain" in sql
    assert "guild_members.user_domain = 'alpha.localhost'" in sql
    assert "LIMIT 7" in sql


@pytest.mark.asyncio
async def test_duplicate_guild_sync_returns_before_loading_the_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.get = AsyncMock()

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _statement: object) -> bool:
            return False

    class FakeEngine:
        dispose = AsyncMock()

    class FakeRedis:
        aclose = AsyncMock()

    session = FakeSession()
    engine = FakeEngine()
    redis = FakeRedis()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
        dragonfly_url=SimpleNamespace(get_secret_value=lambda: "redis://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: redis)

    result = await unwrap(tasks.federation_guild_sync.original_func)(REMOTE_DOMAIN, 42)

    assert result == 0
    session.get.assert_not_awaited()
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_orphaned_replica_purge_evicts_channels_before_the_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = remote_guild()
    channel = Channel(
        id=43,
        origin_domain=REMOTE_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=43,
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalars=AsyncMock(side_effect=[[guild], [channel]]),
            scalar=AsyncMock(return_value=False),
            delete=AsyncMock(),
        ),
    )
    purge_channel = AsyncMock()
    monkeypatch.setattr(
        federation_guilds,
        "purge_replicated_channel_cache",
        purge_channel,
    )

    assert await purge_orphaned_replicated_guilds(session, config(), limit=5) == 1

    candidate_statement = session.scalars.await_args_list[0].args[0]  # type: ignore[attr-defined]
    candidate_sql = str(candidate_statement.compile(dialect=postgresql.dialect()))
    assert "NOT (EXISTS" in candidate_sql
    assert "FOR UPDATE SKIP LOCKED" in candidate_sql
    purge_channel.assert_awaited_once_with(session, channel, reconcile=False)
    session.delete.assert_awaited_once_with(guild)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_orphaned_replica_purge_preserves_a_concurrently_joined_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = remote_guild()
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalars=AsyncMock(return_value=[guild]),
            scalar=AsyncMock(return_value=True),
            delete=AsyncMock(),
        ),
    )
    purge_channel = AsyncMock()
    monkeypatch.setattr(
        federation_guilds,
        "purge_replicated_channel_cache",
        purge_channel,
    )

    assert await purge_orphaned_replicated_guilds(session, config()) == 0

    assert session.scalars.await_count == 1  # type: ignore[attr-defined]
    purge_channel.assert_not_awaited()
    session.delete.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_retention_cycle_includes_orphaned_replica_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_records = AsyncMock(return_value=3)
    cleanup_replicas = AsyncMock(return_value=2)
    cleanup_users = AsyncMock(return_value=1)
    cleanup_instances = AsyncMock(return_value=4)
    cleanup_membership_intents = AsyncMock(return_value=5)
    monkeypatch.setattr(tasks, "cleanup_federation_retention", cleanup_records)
    monkeypatch.setattr(tasks, "purge_orphaned_replicated_guilds", cleanup_replicas)
    monkeypatch.setattr(tasks, "purge_orphaned_remote_users", cleanup_users)
    monkeypatch.setattr(tasks, "purge_orphaned_remote_instances", cleanup_instances)
    monkeypatch.setattr(
        tasks,
        "purge_stale_remote_guild_membership_intents",
        cleanup_membership_intents,
    )
    session = AsyncMock(spec=AsyncSession)
    settings = config()

    assert await tasks.cleanup_federation_retention_cycle(session, settings) == 15

    cleanup_records.assert_awaited_once_with(session, settings)
    cleanup_replicas.assert_awaited_once_with(session, settings)
    cleanup_users.assert_awaited_once_with(session, settings)
    cleanup_instances.assert_awaited_once_with(session, settings)
    cleanup_membership_intents.assert_awaited_once_with(session)
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_retention_removes_expired_peer_keys_after_event_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = [
        SimpleNamespace(rowcount=0),
        SimpleNamespace(rowcount=0),
        SimpleNamespace(rowcount=2),
        SimpleNamespace(rowcount=3),
        SimpleNamespace(rowcount=5),
        SimpleNamespace(rowcount=7),
    ]
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalar=AsyncMock(return_value=None),
            execute=AsyncMock(side_effect=results),
            commit=AsyncMock(),
        ),
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(
        federation_delivery,
        "reconcile_federation_storage_usage",
        reconcile,
    )

    assert await cleanup_federation_retention(session, config()) == 17

    peer_key_delete = session.execute.await_args_list[-2].args[0]  # type: ignore[attr-defined]
    inbox_delete = session.execute.await_args_list[-3].args[0]  # type: ignore[attr-defined]
    inbox_sql = str(inbox_delete.compile(dialect=postgresql.dialect()))
    assert "DELETE FROM federation_inbox" in inbox_sql
    assert "NOT (EXISTS" in inbox_sql
    assert "federation_events.event_id = federation_inbox.event_id" in inbox_sql
    sql = str(peer_key_delete.compile(dialect=postgresql.dialect()))
    assert "DELETE FROM peer_keys" in sql
    assert "peer_keys.expired_at IS NOT NULL" in sql
    assert "peer_keys.expired_at <" in sql
    reconcile.assert_awaited_once_with(session)
    session.commit.assert_awaited_once()  # type: ignore[attr-defined]
