from __future__ import annotations

import importlib

import pytest

migration = importlib.import_module(
    "migrations.versions.0a6d2f9c4b81_federated_follow_authority_identity"
)


async def upgrade_follow_schema(connection, monkeypatch):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    for sql in (
        "CREATE TABLE federated_channel_follows (id bigint, "
        "target_authority_domain text, local_role text, CONSTRAINT "
        "pk_federated_channel_follows PRIMARY KEY(id,local_role))",
        "CREATE TABLE federated_message_crossposts (source_message_id bigint, "
        "source_message_domain text, follow_id bigint, local_role text, "
        "CONSTRAINT pk_federated_message_crossposts PRIMARY "
        "KEY(source_message_id,source_message_domain,follow_id,local_role))",
        "INSERT INTO federated_channel_follows VALUES "
        "(1,'a.example','source'),(1,'b.example','target')",
        "INSERT INTO federated_message_crossposts VALUES "
        "(2,'source.example',1,'source'),(2,'source.example',1,'target')",
    ):
        await connection.execute(text(sql))

    def upgrade(sync_connection):
        operations = Operations(MigrationContext.configure(sync_connection))
        operations.create_foreign_key(
            operations.f(
                "fk_federated_message_crossposts_follow_id_local_role_federated_channel_follows"
            ),
            "federated_message_crossposts",
            "federated_channel_follows",
            ["follow_id", "local_role"],
            ["id", "local_role"],
            ondelete="CASCADE",
        )
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

    await connection.run_sync(upgrade)


async def test_upgrade_backfills_before_replacing_qualified_identity(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await upgrade_follow_schema(postgres_schema, monkeypatch)
    assert (
        await postgres_schema.execute(
            text(
                "SELECT follow_id,follow_authority_domain,local_role FROM "
                "federated_message_crossposts ORDER BY local_role"
            )
        )
    ).all() == [(1, "a.example", "source"), (1, "b.example", "target")]
    await postgres_schema.execute(
        text("INSERT INTO federated_channel_follows VALUES (1,'b.example','source')")
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federated_message_crossposts VALUES "
            "(2,'source.example',1,'source','b.example')"
        )
    )
    with pytest.raises(IntegrityError):
        async with postgres_schema.begin_nested():
            await postgres_schema.execute(
                text(
                    "INSERT INTO federated_message_crossposts VALUES "
                    "(3,'source.example',1,'source','foreign.example')"
                )
            )
    await postgres_schema.execute(
        text("DELETE FROM federated_channel_follows WHERE target_authority_domain='a.example'")
    )
    assert (
        await postgres_schema.execute(
            text(
                "SELECT follow_authority_domain,local_role FROM "
                "federated_message_crossposts ORDER BY local_role"
            )
        )
    ).all() == [("b.example", "source"), ("b.example", "target")]


async def test_downgrade_refuses_lossy_authority_collapse_first(
    postgres_schema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await upgrade_follow_schema(postgres_schema, monkeypatch)
    await postgres_schema.execute(
        text("INSERT INTO federated_channel_follows VALUES (1,'b.example','source')")
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federated_message_crossposts VALUES "
            "(2,'source.example',1,'source','b.example')"
        )
    )
    with pytest.raises(IntegrityError) as caught:
        async with postgres_schema.begin_nested():
            await postgres_schema.run_sync(lambda _: migration.downgrade())
    assert caught.value.orig.sqlstate == "23505"
    assert (
        await postgres_schema.execute(
            text(
                "SELECT follow_authority_domain,local_role FROM "
                "federated_message_crossposts ORDER BY "
                "local_role,follow_authority_domain"
            )
        )
    ).all() == [("a.example", "source"), ("b.example", "source"), ("b.example", "target")]
    assert (
        await postgres_schema.execute(text("SELECT count(*) FROM federated_channel_follows"))
    ).scalar_one() == 3
