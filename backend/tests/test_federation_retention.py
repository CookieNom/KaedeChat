from datetime import UTC, datetime
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
from app.federation import terminal_rooms
from app.federation.delivery import cleanup_federation_retention
from app.federation.guilds import (
    purge_orphaned_replicated_guilds,
    replicated_guild_sync_candidates,
)
from app.media import tombstones as media_tombstones

LOCAL_DOMAIN = "alpha.localhost"
REMOTE_DOMAIN = "beta.localhost"


async def test_guild_event_retention_preserves_message_owned_proxy_receipts(
    postgres_schema,
) -> None:
    from sqlalchemy import text

    for table in ("guild_events", "messages", "channels"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "INSERT INTO channels (id, origin_domain, guild_id, guild_domain) VALUES "
            "(10,'home.example',1,'home.example'), (11,'home.example',2,'home.example')"
        )
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO guild_events (guild_id,guild_domain,seq,created_at) VALUES "
            "(1,'home.example',1,'2026-07-01'),(1,'home.example',2,'2026-07-01'),(1,'home.example',3,'2026-07-01'),(1,'home.example',4,'2026-07-01'),(1,'home.example',5,'2026-08-01')"
        )
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO messages "
            "(id,origin_domain,channel_id,channel_domain,proxy_commit_seq) VALUES "
            "(100,'home.example',10,'home.example',1), "
            "(101,'other.example',10,'home.example',2), "
            "(102,'home.example',11,'home.example',3)"
        )
    )
    await postgres_schema.execute(
        tasks.prunable_guild_events_statement(datetime(2026, 8, 1, tzinfo=UTC))
    )
    assert set(await postgres_schema.scalars(text("SELECT seq FROM guild_events"))) == {1, 5}


def config() -> Settings:
    return cast(
        Settings,
        SimpleNamespace(
            domain=LOCAL_DOMAIN,
            federation_event_retention_days=30,
            federation_clock_skew_seconds=300,
        ),
    )


def remote_guild() -> Guild:
    return Guild(
        id=42,
        origin_domain=REMOTE_DOMAIN,
        name="Remote guild",
        owner_id=7,
        owner_domain=REMOTE_DOMAIN,
    )


async def test_sync_sweep_candidates_require_a_local_membership(postgres_schema) -> None:
    from sqlalchemy import text

    for table in ("guilds", "guild_members"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "INSERT INTO guilds (id,origin_domain,sync_status) VALUES "
            "(1,'beta.localhost','stale'),(2,'beta.localhost','failed'),(3,'alpha.localhost','stale'),(4,'beta.localhost','ready'),(5,'beta.localhost','stale'),(6,'beta.localhost','failed'),(7,'beta.localhost','stale')"
        )
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO guild_members (guild_id,guild_domain,user_id,user_domain) VALUES "
            "(1,'beta.localhost',9,'remote.example'),(2,'wrong.example',9,'alpha.localhost'),(3,'alpha.localhost',9,'alpha.localhost'),(4,'beta.localhost',9,'alpha.localhost'),(5,'beta.localhost',9,'alpha.localhost'),(6,'beta.localhost',9,'alpha.localhost'),(7,'beta.localhost',9,'alpha.localhost')"
        )
    )
    assert (
        await postgres_schema.execute(replicated_guild_sync_candidates(LOCAL_DOMAIN, limit=2))
    ).all() == [(REMOTE_DOMAIN, 5), (REMOTE_DOMAIN, 6)]


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
    trace = []
    session.delete.side_effect = lambda *_args: trace.append("guild")
    purge_channel = AsyncMock(side_effect=lambda *_args, **_kwargs: trace.append("channel"))
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
    purge_channel.assert_awaited_once_with(session, config(), channel, reconcile=False)
    session.delete.assert_awaited_once_with(guild)  # type: ignore[attr-defined]

    assert trace == ["channel", "guild"]


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
    monkeypatch: pytest.MonkeyPatch, postgres_schema
) -> None:
    from datetime import timedelta

    from sqlalchemy import text

    now = datetime(2026, 9, 1, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(_zone):
            return now

    monkeypatch.setattr(federation_delivery, "datetime", Clock)
    for table in (
        "instances",
        "peer_keys",
        "federation_events",
        "federation_inbox",
        "remote_media_tombstones",
        "attachments",
        "remote_media_cache",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "INSERT INTO instances (domain,is_self) VALUES "
            "('alpha.localhost',true),('beta.localhost',false)"
        )
    )
    cutoff = now - timedelta(days=30)
    await postgres_schema.execute(
        text(
            "INSERT INTO peer_keys (domain,key_id,expired_at) VALUES "
            "('beta.localhost','old',:old),('beta.localhost','boundary',:cutoff),('beta.localhost','recent',:recent),('beta.localhost','active',NULL)"
        ),
        dict(
            old=cutoff - timedelta(microseconds=1),
            cutoff=cutoff,
            recent=cutoff + timedelta(microseconds=1),
        ),
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federation_events "
            "(origin_domain,event_id,envelope_bytes,expires_at) VALUES "
            "('beta.localhost','retained',100,:future),('beta.localhost','expired',200,:past)"
        ),
        dict(future=now + timedelta(days=1), past=now - timedelta(seconds=1)),
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federation_inbox (origin_domain,event_id,received_at) VALUES "
            "('beta.localhost','retained',:old),('beta.localhost','expired',:old),('beta.localhost','boundary',:cutoff)"
        ),
        dict(old=cutoff - timedelta(seconds=1), cutoff=cutoff),
    )
    monkeypatch.setattr(
        terminal_rooms, "cleanup_terminal_room_deletions", AsyncMock(return_value=0)
    )
    monkeypatch.setattr(
        media_tombstones, "cleanup_media_tombstone_sources", AsyncMock(return_value=0)
    )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        assert await cleanup_federation_retention(session, config()) == 3
        assert set(await session.scalars(text("SELECT key_id FROM peer_keys"))) == {
            "boundary",
            "recent",
            "active",
        }
        assert set(await session.scalars(text("SELECT event_id FROM federation_inbox"))) == {
            "retained",
            "boundary",
        }
        assert (
            await session.execute(
                text(
                    "SELECT federation_inbox_events,federation_inbox_event_bytes FROM "
                    "instances WHERE is_self"
                )
            )
        ).one() == (2, 100)
