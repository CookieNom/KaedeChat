from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import CheckConstraint

from app.db import models  # noqa: F401
from app.db.base import Base

migration = importlib.import_module(
    "migrations.versions.4ea6c2d8f953_stage_voice_capacity_constraint"
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
            drop_constraint=operation("drop_constraint"),
            create_check_constraint=operation("create_check_constraint"),
            execute=operation("execute"),
        ),
        calls,
    )


def test_model_constraint_matches_type_specific_discord_limits() -> None:
    constraint = next(
        item
        for item in Base.metadata.tables["channels"].constraints
        if isinstance(item, CheckConstraint) and item.name == "ck_channels_voice_user_limit_range"
    )
    assert str(constraint.sqltext) == migration.TYPE_AWARE_LIMIT


def test_upgrade_replaces_the_legacy_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    drop = (
        "drop_constraint",
        (migration.VOICE_USER_LIMIT_CONSTRAINT, "channels"),
        {"type_": "check"},
    )
    create = (
        "create_check_constraint",
        (migration.VOICE_USER_LIMIT_CONSTRAINT, "channels", migration.TYPE_AWARE_LIMIT),
        {},
    )
    assert calls.index(drop) < calls.index(create)


async def test_downgrade_clamps_stage_capacity_before_restoring_legacy_constraint(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await postgres_schema.execute(
        text(
            "CREATE TABLE channels (id int PRIMARY KEY, type int, user_limit int, "
            "CONSTRAINT ck_channels_voice_user_limit_range CHECK (user_limit IS NULL OR "
            "(type = 2 AND user_limit BETWEEN 0 AND 99) OR (type = 13 AND user_limit "
            "BETWEEN 0 AND 10000)))"
        )
    )
    await postgres_schema.execute(
        text("INSERT INTO channels VALUES (1,13,50),(2,13,10000),(3,2,90),(4,0,NULL)")
    )

    def downgrade(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()

    await postgres_schema.run_sync(downgrade)
    assert (
        await postgres_schema.execute(text("SELECT id,user_limit FROM channels ORDER BY id"))
    ).all() == [(1, 50), (2, 99), (3, 90), (4, None)]
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(text("UPDATE channels SET user_limit=100 WHERE id=2"))
