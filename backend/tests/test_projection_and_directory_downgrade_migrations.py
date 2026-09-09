from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from migrations.versions import e84f1a2c7d30_application_directory as directory_migration
from migrations.versions import (
    f95b2c3d8e41_developer_team_snapshot_highwaters as projection_migration,
)


@pytest.mark.parametrize(
    ("migration", "guard"),
    (
        (
            directory_migration,
            directory_migration.DIRECTORY_DOWNGRADE_PREFLIGHT_SQL,
        ),
        (
            projection_migration,
            projection_migration.PROJECTION_DOWNGRADE_PREFLIGHT_SQL,
        ),
    ),
)
def test_lossy_downgrades_run_preflight_before_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
    migration: object,
    guard: str,
) -> None:
    order: list[str] = []

    def record(name: str):
        return Mock(side_effect=lambda *args, **kwargs: order.append(name))

    operations = SimpleNamespace(
        execute=record("execute"),
        drop_index=record("drop_index"),
        drop_constraint=record("drop_constraint"),
        drop_table=record("drop_table"),
        drop_column=record("drop_column"),
        f=lambda name: name,
    )
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()  # type: ignore[attr-defined]

    operations.execute.assert_called_once_with(guard)
    assert order[0] == "execute"
    assert any(item.startswith("drop_") for item in order[1:])


async def test_directory_downgrade_guard_covers_every_added_listing_field(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    from app.db.base import Base

    await postgres_schema.execute(
        text("CREATE TABLE bot_applications (id bigint, origin_domain text, status text)")
    )
    await postgres_schema.execute(
        text(
            "CREATE TABLE application_assets (id bigint, application_id bigint, "
            "application_domain text, name text, kind text)"
        )
    )
    await postgres_schema.execute(
        text("INSERT INTO bot_applications VALUES (1,'apps.example','active')")
    )

    def migrate(connection, direction):
        monkeypatch.setattr(
            directory_migration,
            "op",
            Operations(
                MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
            ),
        )
        getattr(directory_migration, direction)()

    await postgres_schema.run_sync(migrate, "upgrade")
    for column, value in (
        ("directory_enabled", "true"),
        ("directory_approved", "true"),
        ("directory_summary", "'Useful app'"),
        ("directory_category", "'utilities'"),
        ("directory_tags", "'[\"tools\"]'::jsonb"),
        ("directory_collections", "'[\"featured\"]'::jsonb"),
        ("directory_media", '\'[{"type":"image","asset_id":"42"}]\'::jsonb'),
        ("directory_external_links", '\'[{"name":"Docs","url":"https://docs.example"}]\'::jsonb'),
        ("directory_supported_locales", "'[\"fr\"]'::jsonb"),
        ("directory_description_localizations", '\'{"fr":"Bonjour"}\'::jsonb'),
        ("banner_hash", "'abc'"),
        ("terms_url", "'https://apps.example/terms'"),
    ):
        savepoint = await postgres_schema.begin_nested()
        try:
            await postgres_schema.execute(
                text(f"UPDATE bot_applications SET {column}={value}")  # noqa: S608 -- fixed fixture literals
            )
            before = (
                await postgres_schema.execute(
                    text("SELECT to_jsonb(bot_applications) FROM bot_applications")
                )
            ).scalar_one()
            with pytest.raises(IntegrityError) as caught:
                async with postgres_schema.begin_nested():
                    await postgres_schema.run_sync(migrate, "downgrade")
            assert caught.value.orig.sqlstate == "23514", column
            assert (
                await postgres_schema.execute(
                    text("SELECT to_jsonb(bot_applications) FROM bot_applications")
                )
            ).scalar_one() == before, column
        finally:
            await savepoint.rollback()
    await postgres_schema.run_sync(migrate, "downgrade")
    assert await postgres_schema.run_sync(
        lambda c: [item["name"] for item in inspect(c).get_columns("bot_applications")]
    ) == ["id", "origin_domain", "status"]


async def test_projection_downgrade_guard_retains_security_highwaters_and_leases(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    from app.db.base import Base

    for statement in (
        "CREATE TABLE bot_user_installations (id bigint, authority_expires_at timestamptz)",
        "CREATE TABLE developer_teams (id bigint, federation_metadata_fingerprint "
        "bytea, federation_applications_fingerprint bytea)",
        "CREATE TABLE developer_team_member_highwaters (team_id bigint, revision bigint)",
        "CREATE INDEX ix_developer_team_member_highwaters_user ON "
        "developer_team_member_highwaters(team_id)",
        "CREATE INDEX ix_bot_user_installations_authority_expiry ON "
        "bot_user_installations(authority_expires_at)",
    ):
        await postgres_schema.execute(text(statement))

    def add_constraint(connection):
        operations = Operations(
            MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
        )
        operations.create_check_constraint(
            operations.f("ck_developer_teams_developer_team_federation_fingerprint_lengths"),
            "developer_teams",
            "true",
        )

    await postgres_schema.run_sync(add_constraint)

    def downgrade(connection):
        monkeypatch.setattr(
            projection_migration,
            "op",
            Operations(
                MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
            ),
        )
        projection_migration.downgrade()

    for table, insert in (
        ("bot_user_installations", "INSERT INTO bot_user_installations VALUES (1,'2030-01-01Z')"),
        (
            "developer_teams",
            "INSERT INTO developer_teams VALUES (1,decode(repeat('ab',32),'hex'),NULL)",
        ),
        (
            "developer_teams",
            "INSERT INTO developer_teams VALUES (1,NULL,decode(repeat('cd',32),'hex'))",
        ),
        (
            "developer_team_member_highwaters",
            "INSERT INTO developer_team_member_highwaters VALUES (1,7)",
        ),
    ):
        savepoint = await postgres_schema.begin_nested()
        try:
            await postgres_schema.execute(text(insert))
            before = (
                await postgres_schema.execute(text(f"SELECT to_jsonb(t) FROM {table} t"))  # noqa: S608 -- fixed fixture identifiers
            ).scalar_one()
            with pytest.raises(IntegrityError) as caught:
                async with postgres_schema.begin_nested():
                    await postgres_schema.run_sync(downgrade)
            assert caught.value.orig.sqlstate == "23514"
            assert (
                await postgres_schema.execute(text(f"SELECT to_jsonb(t) FROM {table} t"))  # noqa: S608 -- fixed fixture identifiers
            ).scalar_one() == before
        finally:
            await savepoint.rollback()
    await postgres_schema.run_sync(downgrade)
    assert (
        await postgres_schema.execute(
            text("SELECT to_regclass('developer_team_member_highwaters')")
        )
    ).scalar_one() is None
