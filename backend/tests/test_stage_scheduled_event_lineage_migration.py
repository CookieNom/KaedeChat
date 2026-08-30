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


def test_upgrade_fails_closed_before_adding_both_lineage_fks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert [name for name, _args, _kwargs in calls[:3]] == [
        "execute",
        "execute",
        "create_unique_constraint",
    ]
    channel_guard = str(calls[0][1][0])
    event_guard = str(calls[1][1][0])
    assert "channel.guild_id = stage.guild_id" in channel_guard
    assert "channel.guild_domain = stage.guild_domain" in channel_guard
    assert "event.guild_id = stage.guild_id" in event_guard
    assert "event.channel_id = stage.channel_id" in event_guard
    assert "ERRCODE = '23503'" in channel_guard
    assert "ERRCODE = '23503'" in event_guard

    unique = calls[2]
    assert unique[1] == (
        migration.EVENT_LINEAGE_UNIQUE,
        "guild_scheduled_events",
        [
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
    )
    foreign_keys = [call for call in calls if call[0] == "create_foreign_key"]
    assert foreign_keys[0][1][0] == migration.STAGE_CHANNEL_LINEAGE_FK
    assert foreign_keys[0][1][3:] == (
        ["channel_id", "channel_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
    )
    assert foreign_keys[0][2] == {"ondelete": "CASCADE"}
    assert foreign_keys[1][1][0] == migration.STAGE_EVENT_LINEAGE_FK
    assert foreign_keys[1][1][3:] == (
        [
            "scheduled_event_id",
            "scheduled_event_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
        [
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
    )
    assert foreign_keys[1][2] == {}


def test_downgrade_removes_dependents_before_parent_unique(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert [call[1][0] for call in calls] == [
        migration.STAGE_EVENT_LINEAGE_FK,
        migration.STAGE_CHANNEL_LINEAGE_FK,
        migration.EVENT_LINEAGE_UNIQUE,
    ]
