from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

migration = importlib.import_module(
    "migrations.versions.1b7e3c9a5d20_stage_scheduled_event_lineage"
)
OperationCall = tuple[str, tuple[Any, ...], dict[str, Any]]


def recording_operations() -> tuple[SimpleNamespace, list[OperationCall]]:
    calls: list[OperationCall] = []

    def operation(name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            calls.append((name, args, kwargs))

        return record

    return (
        SimpleNamespace(
            execute=operation("execute"),
            create_unique_constraint=operation("create_unique_constraint"),
            create_foreign_key=operation("create_foreign_key"),
            drop_constraint=operation("drop_constraint"),
        ),
        calls,
    )


async def test_stage_lineage_migration_enforces_channel_and_event_bindings(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    for sql in (
        "CREATE TABLE channels (id bigint, origin_domain text, guild_id bigint, "
        "guild_domain text, UNIQUE(id,origin_domain,guild_id,guild_domain))",
        "CREATE TABLE guild_scheduled_events (id bigint, origin_domain text, "
        "guild_id bigint, guild_domain text, channel_id bigint, channel_domain "
        "text)",
        "CREATE TABLE stage_instances (id bigint, origin_domain text, guild_id "
        "bigint, guild_domain text, channel_id bigint, channel_domain text, "
        "scheduled_event_id bigint, scheduled_event_domain text)",
        "INSERT INTO channels VALUES (1,'a.example',10,'a.example'), "
        "(2,'a.example',20,'a.example')",
        "INSERT INTO guild_scheduled_events VALUES "
        "(5,'a.example',10,'a.example',1,'a.example'), "
        "(6,'a.example',20,'a.example',2,'a.example')",
        "INSERT INTO stage_instances VALUES "
        "(3,'a.example',10,'a.example',1,'a.example',5,'a.example')",
    ):
        await postgres_schema.execute(text(sql))

    def upgrade(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()

    mutations = ("guild_id=20", "scheduled_event_id=6")
    for mutation in mutations:
        savepoint = await postgres_schema.begin_nested()
        try:
            await postgres_schema.execute(text("UPDATE stage_instances SET " + mutation))  # noqa: S608 -- fixed fixture identifiers
            with pytest.raises(IntegrityError) as caught:
                async with postgres_schema.begin_nested():
                    await postgres_schema.run_sync(upgrade)
            assert caught.value.orig.sqlstate == "23503"
            assert (
                await postgres_schema.run_sync(
                    lambda c: inspect(c).get_foreign_keys("stage_instances")
                )
                == []
            )
            assert (
                await postgres_schema.run_sync(
                    lambda c: inspect(c).get_unique_constraints("guild_scheduled_events")
                )
                == []
            )
        finally:
            await savepoint.rollback()
    await postgres_schema.run_sync(upgrade)
    for mutation in mutations:
        with pytest.raises(IntegrityError):
            async with postgres_schema.begin_nested():
                await postgres_schema.execute(text("UPDATE stage_instances SET " + mutation))  # noqa: S608 -- fixed fixture identifiers
    assert (
        await postgres_schema.execute(
            text("SELECT guild_id,channel_id,scheduled_event_id FROM stage_instances")
        )
    ).all() == [(10, 1, 5)]


def test_downgrade_removes_dependents_before_parent_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    dropped = [call[1][0] for call in calls]
    assert set(dropped) == {
        migration.STAGE_EVENT_LINEAGE_FK,
        migration.STAGE_CHANNEL_LINEAGE_FK,
        migration.EVENT_LINEAGE_UNIQUE,
    }
    for child in (migration.STAGE_EVENT_LINEAGE_FK, migration.STAGE_CHANNEL_LINEAGE_FK):
        assert dropped.index(child) < dropped.index(migration.EVENT_LINEAGE_UNIQUE)
