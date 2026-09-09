from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import Guild, Instance, PeerKey, User
from app.federation.replica_storage import (
    REPLICA_QUOTA_ERROR_CODE,
    FederationReplicaQuotaExceeded,
    ReplicaStorageUsage,
    admit_replica_storage,
    mark_replica_capacity_paused,
    mark_replica_quota_paused,
    orphaned_remote_instance_candidates,
    orphaned_remote_user_candidates,
    purge_orphaned_remote_users,
    reconcile_replica_storage,
    replica_storage_quota_violation,
)
from migrations.versions.c62f4a9d8e31_remote_replica_storage_ledger import TRACKED_TABLES

from .test_settings import settings


def quota_settings() -> Settings:
    return settings(
        federation_replica_max_rows_per_guild=10_000,
        federation_replica_max_bytes_per_guild=16 * 1024 * 1024,
        federation_replica_max_rows_per_origin=20_000,
        federation_replica_max_bytes_per_origin=32 * 1024 * 1024,
    )


def test_replica_storage_accepts_exact_high_water_marks() -> None:
    configured = quota_settings()
    usage = ReplicaStorageUsage(
        guild_rows=10_000,
        guild_bytes=16 * 1024 * 1024,
        origin_rows=20_000,
        origin_bytes=32 * 1024 * 1024,
    )

    assert replica_storage_quota_violation(configured, usage) is None


def test_replica_ledger_tracks_history_import_control_rows() -> None:
    tracked = {table: mode for table, _category, mode, _overhead, _minimum in TRACKED_TABLES}

    assert tracked["guild_history_imports"] == "direct"
    assert tracked["guild_history_import_channels"] == "channel"


def test_guild_row_quota_rejection() -> None:
    configured = quota_settings()
    violation = replica_storage_quota_violation(
        configured,
        ReplicaStorageUsage(
            guild_rows=10_001,
            guild_bytes=1,
            origin_rows=10_001,
            origin_bytes=1,
        ),
    )

    assert isinstance(violation, FederationReplicaQuotaExceeded)
    assert (violation.scope, violation.resource, violation.used, violation.limit) == (
        "guild",
        "rows",
        10_001,
        10_000,
    )


def test_replica_storage_bounds_aggregate_origin_state() -> None:
    configured = quota_settings()
    violation = replica_storage_quota_violation(
        configured,
        ReplicaStorageUsage(
            guild_rows=5_000,
            guild_bytes=8 * 1024 * 1024,
            origin_rows=20_001,
            origin_bytes=16 * 1024 * 1024,
        ),
    )

    assert isinstance(violation, FederationReplicaQuotaExceeded)
    assert (violation.scope, violation.resource) == ("origin", "rows")


def remote_guild() -> Guild:
    guild = Guild(
        id=42,
        origin_domain="remote.example",
        name="Remote guild",
        owner_id=99,
        owner_domain="remote.example",
    )
    guild.sync_status = "ready"
    guild.sync_error_code = None
    guild.sync_error = None
    return guild


@pytest.mark.asyncio
async def test_replica_admission_fences_origin_and_reads_trigger_ledger() -> None:
    guild = remote_guild()
    ledger = SimpleNamespace(total_rows=9_000, total_bytes=15 * 1024 * 1024)
    session = cast(
        AsyncSession,
        SimpleNamespace(
            flush=AsyncMock(),
            scalar=AsyncMock(side_effect=[None, ledger]),
            execute=AsyncMock(return_value=SimpleNamespace(one=lambda: (19_000, 31 * 1024 * 1024))),
        ),
    )

    usage = await admit_replica_storage(session, quota_settings(), guild)

    assert usage == ReplicaStorageUsage(
        guild_rows=9_000,
        guild_bytes=15 * 1024 * 1024,
        origin_rows=19_000,
        origin_bytes=31 * 1024 * 1024,
    )
    session.flush.assert_awaited_once()  # type: ignore[attr-defined]
    advisory_sql = str(session.scalar.await_args_list[0].args[0])  # type: ignore[attr-defined]
    ledger_sql = str(session.scalar.await_args_list[1].args[0])  # type: ignore[attr-defined]
    assert "pg_advisory_xact_lock" in advisory_sql
    assert "FOR UPDATE" in ledger_sql


@pytest.mark.asyncio
async def test_replica_admission_rejects_the_atomic_batch_over_high_water() -> None:
    guild = remote_guild()
    ledger = SimpleNamespace(total_rows=10_001, total_bytes=1)
    session = cast(
        AsyncSession,
        SimpleNamespace(
            flush=AsyncMock(),
            scalar=AsyncMock(side_effect=[None, ledger]),
            execute=AsyncMock(return_value=SimpleNamespace(one=lambda: (10_001, 1))),
        ),
    )

    with pytest.raises(FederationReplicaQuotaExceeded) as caught:
        await admit_replica_storage(session, quota_settings(), guild)

    assert (caught.value.scope, caught.value.resource) == ("guild", "rows")


@pytest.mark.asyncio
async def test_quota_pause_state_mutation() -> None:
    guild = remote_guild()
    session = cast(
        AsyncSession,
        SimpleNamespace(scalar=AsyncMock(return_value=guild)),
    )
    violation = FederationReplicaQuotaExceeded("guild", "bytes", 20, 10)

    assert await mark_replica_quota_paused(
        session,
        cast(Any, SimpleNamespace(domain="local.example")),
        guild.id,
        guild.origin_domain,
        violation,
    )
    assert guild.sync_status == "quota_paused"
    assert guild.sync_error_code == REPLICA_QUOTA_ERROR_CODE
    assert "guild bytes high-water mark reached" in (guild.sync_error or "")

    assert await mark_replica_capacity_paused(
        session,
        cast(Any, SimpleNamespace(domain="local.example")),
        guild.id,
        guild.origin_domain,
        error_code="FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED",
        internal_error="remote identity high-water mark 1000/1000",
    )
    assert guild.sync_status == "quota_paused"
    assert guild.sync_error_code == "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED"
    assert guild.sync_error == "remote identity high-water mark 1000/1000"


@pytest.mark.asyncio
async def test_replica_reconciliation_repairs_parent_cascade_accounting(
    postgres_schema, migrate_to
) -> None:
    from sqlalchemy import text

    await migrate_to("head")
    for statement in (
        "INSERT INTO instances "
        "(domain,is_self,current_key_id,encrypted_private_key,private_key_nonce) VALUES "
        "('local.example',true,'main',decode(repeat('ab',32),'hex'),decode(repeat('cd',12),'h"
        "ex')),('remote.example',false,NULL,NULL,NULL)",
        "INSERT INTO users "
        "(id,origin_domain,username,is_local,federation_introduced_by_domain) VALUES "
        "(99,'remote.example','owner',false,'remote.example')",
        "INSERT INTO guilds (id,origin_domain,name,owner_id,owner_domain) VALUES "
        "(42,'remote.example','Remote',99,'remote.example')",
        "INSERT INTO guild_members (guild_id,guild_domain,user_id,user_domain,joined_at) VALUES "
        "(42,'remote.example',99,'remote.example',now())",
        "INSERT INTO channels "
        "(id,origin_domain,guild_id,guild_domain,type,name,created_floor_id) VALUES "
        "(10,'remote.example',42,'remote.example',0,'removed',1),(11,'remote.example',42,'rem"
        "ote.example',0,'retained',1)",
        "INSERT INTO messages "
        "(id,origin_domain,channel_id,channel_domain,author_id,author_domain,content) "
        "VALUES "
        "(100,'remote.example',10,'remote.example',99,'remote.example','removed'),(101,'remot"
        "e.example',11,'remote.example',99,'remote.example','retained')",
    ):
        await postgres_schema.execute(text(statement))
    await postgres_schema.execute(
        text(
            "INSERT INTO reactions (message_id,message_domain,user_id,user_domain,emoji_key) "
            "VALUES (100,'remote.example',99,'remote.example','🔥')"
        )
    )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        await reconcile_replica_storage(session, 42, "remote.example")
        assert (
            await session.scalar(
                text("SELECT message_rows FROM federation_replica_usage WHERE guild_id=42")
            )
            == 2
        )
        await session.execute(
            text("DELETE FROM messages WHERE id=100 AND origin_domain='remote.example'")
        )
        assert list(await session.scalars(text("SELECT id FROM messages"))) == [101]
        # Model stale accounting left by a parent cascade or interrupted repair.
        await session.execute(
            text(
                "UPDATE federation_replica_usage SET "
                "message_rows=999,message_bytes=999999,reaction_rows=999,reaction_bytes=999999 "
                "WHERE guild_id=42"
            )
        )
        await reconcile_replica_storage(session, 42, "remote.example")
        counts = (
            await session.execute(
                text(
                    "SELECT "
                    "message_rows,reaction_rows,member_rows,attachment_rows,projection_rows "
                    "FROM federation_replica_usage WHERE guild_id=42 AND "
                    "guild_domain='remote.example'"
                )
            )
        ).one()
        assert counts == (1, 0, 1, 0, 0)
        assert (
            await session.scalar(
                text("SELECT message_bytes FROM federation_replica_usage WHERE guild_id=42")
            )
            > 0
        )
        before = (
            await session.execute(
                text(
                    "SELECT message_rows,message_bytes,reaction_rows,reaction_bytes,member_rows,"
                    "member_bytes,total_rows,total_bytes FROM federation_replica_usage WHERE "
                    "guild_id=42"
                )
            )
        ).one()
        await reconcile_replica_storage(session, 42, "remote.example")
        after = (
            await session.execute(
                text(
                    "SELECT message_rows,message_bytes,reaction_rows,reaction_bytes,member_rows,"
                    "member_bytes,total_rows,total_bytes FROM federation_replica_usage WHERE "
                    "guild_id=42"
                )
            )
        ).one()
        assert before == after


def test_orphan_remote_identity_candidates_cover_the_fk_graph_and_lock_a_small_batch() -> None:
    statement = orphaned_remote_user_candidates(
        quota_settings(),
        now=datetime(2026, 8, 12, tzinfo=UTC),
        limit=17,
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    referenced_tables = {
        table.name
        for table in User.__table__.metadata.sorted_tables
        if any(
            constraint.referred_table is User.__table__
            for constraint in table.foreign_key_constraints
        )
    }

    assert "users.is_local IS false" in sql
    assert "users.updated_at < '2026-07-13" in sql
    assert "LIMIT 17" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    for table_name in referenced_tables:
        assert f"FROM {table_name}" in sql


def test_orphan_remote_identity_candidates_reject_unsafe_bounds() -> None:
    with pytest.raises(ValueError, match="between 1 and 10000"):
        orphaned_remote_user_candidates(quota_settings(), limit=0)
    with pytest.raises(ValueError, match="timezone-aware"):
        orphaned_remote_user_candidates(
            quota_settings(),
            now=datetime(2026, 8, 12),
        )


async def test_orphan_remote_instance_gc_does_not_treat_cached_peer_keys_as_ownership(
    postgres_schema, migrate_to
) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from app.federation.replica_storage import purge_orphaned_remote_instances

    await migrate_to("head")
    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        session.add_all(
            [
                Instance(domain=domain, updated_at=old)
                for domain in ("keys.example", "owned.example", "empty.example")
            ]
        )
        await session.flush()
        session.add_all(
            [
                PeerKey(domain="keys.example", key_id="main", public_key=b"k" * 32),
                User(
                    id=99,
                    origin_domain="owned.example",
                    username="owner",
                    is_local=False,
                    federation_introduced_by_domain="owned.example",
                ),
            ]
        )
        await session.flush()
        candidates = list(
            await session.scalars(
                orphaned_remote_instance_candidates(quota_settings(), now=now, limit=10)
            )
        )
        assert {row.domain for row in candidates} == {"keys.example", "empty.example"}
        assert (
            await purge_orphaned_remote_instances(session, quota_settings(), now=now, limit=10) == 2
        )
        await session.flush()
        assert set(await session.scalars(select(Instance.domain))) == {"owned.example"}
        assert list(await session.scalars(select(PeerKey.key_id))) == []


@pytest.mark.asyncio
async def test_orphan_remote_identity_gc_deletes_only_the_locked_candidates() -> None:
    candidate = User(
        id=99,
        origin_domain="remote.example",
        is_local=False,
        username="remote_user",
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalars=AsyncMock(return_value=[candidate]),
            delete=AsyncMock(),
        ),
    )

    assert (
        await purge_orphaned_remote_users(
            session,
            quota_settings(),
            now=datetime(2026, 8, 12, tzinfo=UTC),
            limit=1,
        )
        == 1
    )

    session.delete.assert_awaited_once_with(candidate)  # type: ignore[attr-defined]
