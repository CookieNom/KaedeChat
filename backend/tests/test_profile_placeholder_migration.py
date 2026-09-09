from __future__ import annotations

import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


@pytest.mark.asyncio
async def test_profile_resolution_trigger_allows_only_one_safe_remote_transition(
    postgres_schema,
) -> None:
    connection = postgres_schema
    migration = importlib.import_module(
        "migrations.versions.c31f6a8e2d94_resolve_opaque_profile_handles"
    )
    await connection.execute(
        text(
            "CREATE TABLE users (id bigint PRIMARY KEY, origin_domain text, is_local "
            "boolean, username text, profile_resolved boolean)"
        )
    )

    def upgrade(sync):
        with Operations.context(MigrationContext.configure(sync)):
            migration.upgrade()

    await connection.run_sync(upgrade)
    await connection.execute(
        text(
            "INSERT INTO users VALUES (1, 'remote.example', false, 'history_opaque', "
            "false), (2, 'local.example', true, 'history_local', false), (3, "
            "'remote.example', false, 'ordinary', false)"
        )
    )
    # Every identity and resolution prerequisite is isolated from a valid opaque row.
    for change in (
        "id = 4",
        "origin_domain = 'other.example'",
        "is_local = true",
        "username = 'resolved'",
        "profile_resolved = true",
    ):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text(f"UPDATE users SET {change} WHERE id = 1"))  # noqa: S608 -- fixed mutation table
    for user_id in (2, 3):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "UPDATE users SET username = 'resolved', profile_resolved = true "
                        "WHERE id = :id"
                    ),
                    {"id": user_id},
                )
    await connection.execute(
        text("UPDATE users SET username = 'resolved', profile_resolved = true WHERE id = 1")
    )
    assert (
        await connection.execute(text("SELECT username, profile_resolved FROM users WHERE id = 1"))
    ).one() == ("resolved", True)
    for change in ("username = 'renamed'", "profile_resolved = false"):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(text(f"UPDATE users SET {change} WHERE id = 1"))  # noqa: S608 -- fixed mutation table
