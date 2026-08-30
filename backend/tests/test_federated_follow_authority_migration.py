from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest

migration = importlib.import_module(
    "migrations.versions.0a6d2f9c4b81_federated_follow_authority_identity"
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
            f=lambda name: name,
            add_column=operation("add_column"),
            execute=operation("execute"),
            alter_column=operation("alter_column"),
            drop_constraint=operation("drop_constraint"),
            create_primary_key=operation("create_primary_key"),
            create_foreign_key=operation("create_foreign_key"),
            create_index=operation("create_index"),
            drop_index=operation("drop_index"),
            drop_column=operation("drop_column"),
        ),
        calls,
    )


def test_upgrade_backfills_before_replacing_qualified_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    names = [name for name, _args, _kwargs in calls]
    assert names[:4] == ["add_column", "execute", "execute", "alter_column"]
    update_sql = str(calls[1][1][0])
    guard_sql = str(calls[2][1][0])
    assert "SET follow_authority_domain = follow.target_authority_domain" in update_sql
    assert "follow_authority_domain IS NULL" in guard_sql
    assert "ERRCODE = '23503'" in guard_sql

    primary_keys = [call for call in calls if call[0] == "create_primary_key"]
    assert primary_keys[0][1][2] == ["id", "target_authority_domain", "local_role"]
    assert primary_keys[1][1][2] == [
        "source_message_id",
        "source_message_domain",
        "follow_id",
        "follow_authority_domain",
        "local_role",
    ]
    foreign_key = next(call for call in calls if call[0] == "create_foreign_key")
    assert foreign_key[1][3:] == (
        ["follow_id", "follow_authority_domain", "local_role"],
        ["id", "target_authority_domain", "local_role"],
    )
    assert foreign_key[2] == {"ondelete": "CASCADE"}


def test_downgrade_refuses_lossy_authority_collapse_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert calls[0][0] == "execute"
    guard_sql = str(calls[0][1][0])
    assert "HAVING count(*) > 1" in guard_sql
    assert "ERRCODE = '23505'" in guard_sql
    assert "qualified follow identities exist" in guard_sql
    assert [name for name, _args, _kwargs in calls][-1] == "drop_column"
