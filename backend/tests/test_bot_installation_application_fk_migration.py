import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from migrations.versions import d73c8a1f4b20_bot_installation_application_fk as migration


async def test_upgrade_fails_closed_on_orphans_before_creating_the_constraint(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await postgres_schema.execute(
        text(
            "CREATE TABLE bot_applications (id bigint, origin_domain text, PRIMARY KEY "
            "(id, origin_domain))"
        )
    )
    await postgres_schema.execute(
        text(
            "CREATE TABLE bot_installations (id bigint PRIMARY KEY, application_id "
            "bigint, application_domain text, CONSTRAINT positive_id CHECK(id>0))"
        )
    )
    await postgres_schema.execute(text("INSERT INTO bot_applications VALUES (10,'owned.example')"))
    await postgres_schema.execute(
        text("INSERT INTO bot_installations VALUES (1,10,'foreign.example')")
    )

    def upgrade(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()

    with pytest.raises(IntegrityError) as caught:
        async with postgres_schema.begin_nested():
            await postgres_schema.run_sync(upgrade)
    assert caught.value.orig.sqlstate == "23503"
    assert (
        await postgres_schema.run_sync(lambda c: inspect(c).get_foreign_keys("bot_installations"))
        == []
    )
    assert (
        await postgres_schema.execute(text("SELECT application_domain FROM bot_installations"))
    ).scalar_one() == "foreign.example"
    await postgres_schema.execute(
        text("UPDATE bot_installations SET application_domain='owned.example'")
    )
    await postgres_schema.run_sync(upgrade)
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(
                text("INSERT INTO bot_installations VALUES (2,10,'foreign.example')")
            )


async def test_downgrade_removes_only_the_application_constraint(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await postgres_schema.execute(
        text(
            "CREATE TABLE bot_applications (id bigint, origin_domain text, PRIMARY KEY "
            "(id, origin_domain))"
        )
    )
    await postgres_schema.execute(
        text(
            "CREATE TABLE bot_installations (id bigint PRIMARY KEY, application_id "
            "bigint, application_domain text, CONSTRAINT positive_id CHECK(id>0))"
        )
    )

    def migrate(connection):
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.downgrade()

    await postgres_schema.run_sync(migrate)
    assert (
        await postgres_schema.run_sync(lambda c: inspect(c).get_foreign_keys("bot_installations"))
        == []
    )
    await postgres_schema.execute(
        text("INSERT INTO bot_installations VALUES (1,10,'foreign.example')")
    )
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(
                text("INSERT INTO bot_installations VALUES (-1,10,'foreign.example')")
            )
    assert (
        await postgres_schema.execute(text("SELECT id FROM bot_installations"))
    ).scalars().all() == [1]
