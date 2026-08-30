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


def test_stage_capacity_revision_is_the_current_single_head() -> None:
    assert migration.revision == "4ea6c2d8f953"
    assert migration.down_revision == "3d9a5e1c7b42"


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

    assert calls == [
        (
            "drop_constraint",
            (migration.VOICE_USER_LIMIT_CONSTRAINT, "channels"),
            {"type_": "check"},
        ),
        (
            "create_check_constraint",
            (
                migration.VOICE_USER_LIMIT_CONSTRAINT,
                "channels",
                migration.TYPE_AWARE_LIMIT,
            ),
            {},
        ),
    ]


def test_downgrade_clamps_stage_capacity_before_restoring_legacy_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, calls = recording_operations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert [name for name, _args, _kwargs in calls] == [
        "drop_constraint",
        "execute",
        "create_check_constraint",
    ]
    assert calls[1][1] == (
        "UPDATE channels SET user_limit = 99 WHERE type = 13 AND user_limit > 99",
    )
    assert calls[2][1] == (
        migration.VOICE_USER_LIMIT_CONSTRAINT,
        "channels",
        migration.LEGACY_LIMIT,
    )
