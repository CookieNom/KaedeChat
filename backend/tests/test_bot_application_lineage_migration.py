from importlib import import_module

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.db import models  # noqa: F401
from app.db.base import Base

migration = import_module("migrations.versions.2c8f4d0b6e31_bot_application_lineage_constraints")


def unique_columns(table_name: str, constraint_name: str) -> tuple[str, ...]:
    constraint = next(
        item
        for item in Base.metadata.tables[table_name].constraints
        if isinstance(item, UniqueConstraint) and item.name == constraint_name
    )
    return tuple(constraint.columns.keys())


def foreign_key(table_name: str, constraint_name: str) -> ForeignKeyConstraint:
    return next(
        item
        for item in Base.metadata.tables[table_name].foreign_key_constraints
        if item.name == constraint_name
    )


@pytest_asyncio.fixture
async def lineage_preflight_schema(postgres_schema):
    from sqlalchemy import text

    # The preflight reads old columns; omit constraints to admit corrupt legacy rows.
    for table in (
        "bot_applications",
        "bot_installations",
        "application_commands",
        "application_command_permissions",
        "bot_workers",
        "bot_tokens",
        "bot_dm_capabilities",
        "bot_interactions",
        "bot_user_installations",
        "bot_dm_grants",
        "bot_e2ee_devices",
        "bot_e2ee_participations",
        "channels",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    for column in ("application_id", "application_domain", "guild_id", "guild_domain"):
        await postgres_schema.execute(
            text(f"ALTER TABLE bot_e2ee_participations DROP COLUMN {column}")
        )
    return postgres_schema


async def test_lineage_preflight_covers_every_replaced_reference(lineage_preflight_schema) -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    connection = lineage_preflight_schema
    cases = [
        (
            "bot_installation.application_bot_user",
            [
                (
                    "bot_applications",
                    {
                        "id": 1,
                        "origin_domain": "a.example",
                        "bot_user_id": 2,
                        "bot_user_domain": "a.example",
                    },
                ),
                (
                    "bot_installations",
                    {
                        "id": 10,
                        "application_id": 1,
                        "application_domain": "a.example",
                        "bot_user_id": 2,
                        "bot_user_domain": "a.example",
                    },
                ),
            ],
            "bot_installations",
            "bot_user_id",
        ),
        (
            "command_permission.command_application",
            [
                (
                    "application_commands",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "application_command_permissions",
                    {
                        "id": 10,
                        "command_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "application_command_permissions",
            "application_id",
        ),
        (
            "bot_token.worker_application",
            [
                ("bot_workers", {"id": 1, "application_id": 2, "application_domain": "a.example"}),
                (
                    "bot_tokens",
                    {
                        "id": 10,
                        "worker_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_tokens",
            "application_id",
        ),
        (
            "bot_token.dm_capability_application",
            [
                (
                    "bot_dm_capabilities",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_tokens",
                    {
                        "id": 10,
                        "dm_capability_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_tokens",
            "application_id",
        ),
        (
            "bot_interaction.installation_application_guild",
            [
                (
                    "bot_installations",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_interactions",
                    {
                        "id": 10,
                        "installation_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_interactions",
            "application_id",
        ),
        (
            "bot_interaction.user_installation_application",
            [
                (
                    "bot_user_installations",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_interactions",
                    {
                        "id": 10,
                        "user_installation_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_interactions",
            "application_id",
        ),
        (
            "bot_interaction.dm_capability_application_channel_user",
            [
                (
                    "bot_dm_capabilities",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_interactions",
                    {
                        "id": 10,
                        "dm_capability_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_interactions",
            "application_id",
        ),
        (
            "bot_interaction.command_application",
            [
                (
                    "application_commands",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_interactions",
                    {
                        "id": 10,
                        "command_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_interactions",
            "application_id",
        ),
        (
            "bot_dm_grant.installation_application",
            [
                (
                    "bot_installations",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_dm_grants",
                    {
                        "id": 10,
                        "installation_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_dm_grants",
            "application_id",
        ),
        (
            "bot_dm_grant.user_installation_application",
            [
                (
                    "bot_user_installations",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_dm_grants",
                    {
                        "id": 10,
                        "user_installation_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_dm_grants",
            "application_id",
        ),
        (
            "bot_dm_grant.dm_capability_application_channel_user",
            [
                (
                    "bot_dm_capabilities",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_dm_grants",
                    {
                        "id": 10,
                        "dm_capability_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_dm_grants",
            "application_id",
        ),
        (
            "bot_e2ee_device.worker_application",
            [
                ("bot_workers", {"id": 1, "application_id": 2, "application_domain": "a.example"}),
                (
                    "bot_e2ee_devices",
                    {
                        "id": 10,
                        "worker_id": 1,
                        "application_id": 2,
                        "application_domain": "a.example",
                    },
                ),
            ],
            "bot_e2ee_devices",
            "application_id",
        ),
        (
            "bot_interaction.channel_guild",
            [
                (
                    "channels",
                    {
                        "id": 1,
                        "origin_domain": "a.example",
                        "guild_id": 2,
                        "guild_domain": "a.example",
                    },
                ),
                (
                    "bot_interactions",
                    {
                        "id": 10,
                        "channel_id": 1,
                        "channel_domain": "a.example",
                        "guild_id": 2,
                        "guild_domain": "a.example",
                    },
                ),
            ],
            "bot_interactions",
            "guild_id",
        ),
        (
            "bot_e2ee_participation.device_installation_application",
            [
                (
                    "bot_e2ee_devices",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_installations",
                    {"id": 3, "application_id": 2, "application_domain": "a.example"},
                ),
                ("bot_e2ee_participations", {"id": 10, "device_id": 1, "installation_id": 3}),
            ],
            "bot_e2ee_devices",
            "application_id",
        ),
        (
            "bot_e2ee_participation.device_grant_application",
            [
                (
                    "bot_e2ee_devices",
                    {"id": 1, "application_id": 2, "application_domain": "a.example"},
                ),
                (
                    "bot_dm_grants",
                    {"id": 3, "application_id": 2, "application_domain": "a.example"},
                ),
                ("bot_e2ee_participations", {"id": 10, "device_id": 1, "dm_grant_id": 3}),
            ],
            "bot_e2ee_devices",
            "application_id",
        ),
        (
            "bot_e2ee_participation.installation_channel_guild",
            [
                (
                    "channels",
                    {
                        "id": 1,
                        "origin_domain": "a.example",
                        "guild_id": 2,
                        "guild_domain": "a.example",
                    },
                ),
                ("bot_installations", {"id": 3, "guild_id": 2, "guild_domain": "a.example"}),
                (
                    "bot_e2ee_participations",
                    {
                        "id": 10,
                        "installation_id": 3,
                        "channel_id": 1,
                        "channel_domain": "a.example",
                    },
                ),
            ],
            "bot_installations",
            "guild_id",
        ),
        (
            "bot_e2ee_participation.grant_channel",
            [
                (
                    "bot_dm_grants",
                    {"id": 3, "conversation_id": 1, "conversation_domain": "a.example"},
                ),
                (
                    "bot_e2ee_participations",
                    {"id": 10, "dm_grant_id": 3, "channel_id": 1, "channel_domain": "a.example"},
                ),
            ],
            "bot_e2ee_participations",
            "channel_id",
        ),
    ]
    for kind, rows, corrupt_table, corrupt_column in cases:
        baseline = await connection.begin_nested()
        for table, values in rows:
            columns = ", ".join(values)
            parameters = ", ".join(":" + column for column in values)
            await connection.execute(
                text(f"INSERT INTO {table} ({columns}) VALUES ({parameters})"),  # noqa: S608
                values,
            )
        await connection.execute(text(migration.LINEAGE_PREFLIGHT_SQL))
        await connection.execute(text(f"UPDATE {corrupt_table} SET {corrupt_column} = 999"))  # noqa: S608
        failed = await connection.begin_nested()
        with pytest.raises(DBAPIError, match=kind.replace(".", r"\.")) as caught:
            await connection.execute(text(migration.LINEAGE_PREFLIGHT_SQL))
        assert caught.value.orig.sqlstate == "23514"
        await failed.rollback()
        await baseline.rollback()


async def test_upgrade_preflights_before_replacing_foreign_keys(lineage_preflight_schema) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    connection = lineage_preflight_schema
    await connection.execute(
        text(
            "INSERT INTO bot_workers (id, application_id, application_domain) VALUES (1, 2, "
            "'a.example')"
        )
    )
    await connection.execute(
        text(
            "INSERT INTO bot_tokens (id, worker_id, application_id, application_domain) "
            "VALUES (10, 1, 3, 'a.example')"
        )
    )

    def upgrade(sync):
        with Operations.context(MigrationContext.configure(sync)):
            migration.upgrade()

    failed = await connection.begin_nested()
    with pytest.raises(DBAPIError, match="bot_token.worker_application"):
        await connection.run_sync(upgrade)
    await failed.rollback()
    assert (
        await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                "current_schema()"
                "AND table_name = 'bot_e2ee_participations' AND column_name = 'application_id'"
            )
        )
        == 0
    )
    assert await connection.scalar(text("SELECT application_id FROM bot_tokens WHERE id = 10")) == 3


async def test_downgrade_restores_the_original_foreign_keys(postgres_schema, migrate_to) -> None:
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import Column, ForeignKeyConstraint, MetaData, Table, UniqueConstraint, inspect
    from sqlalchemy.exc import IntegrityError

    await migrate_to("2c8f4d0b6e31")

    def check_downgrade(sync):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()
        inspector = inspect(sync)
        cases = [
            (
                "bot_installations",
                ("application_id", "application_domain"),
                "bot_applications",
                ("id", "origin_domain"),
                None,
            ),
            (
                "application_command_permissions",
                ("command_id",),
                "application_commands",
                ("id",),
                "CASCADE",
            ),
            ("bot_tokens", ("worker_id",), "bot_workers", ("id",), "CASCADE"),
            ("bot_tokens", ("dm_capability_id",), "bot_dm_capabilities", ("id",), "CASCADE"),
            ("bot_interactions", ("installation_id",), "bot_installations", ("id",), "CASCADE"),
            (
                "bot_interactions",
                ("user_installation_id",),
                "bot_user_installations",
                ("id",),
                "CASCADE",
            ),
            ("bot_interactions", ("dm_capability_id",), "bot_dm_capabilities", ("id",), "CASCADE"),
            ("bot_interactions", ("command_id",), "application_commands", ("id",), "RESTRICT"),
            ("bot_dm_grants", ("installation_id",), "bot_installations", ("id",), "CASCADE"),
            (
                "bot_dm_grants",
                ("user_installation_id",),
                "bot_user_installations",
                ("id",),
                "CASCADE",
            ),
            ("bot_dm_grants", ("dm_capability_id",), "bot_dm_capabilities", ("id",), "CASCADE"),
            ("bot_e2ee_devices", ("worker_id",), "bot_workers", ("id",), "CASCADE"),
            (
                "bot_e2ee_participations",
                ("installation_id",),
                "bot_installations",
                ("id",),
                "CASCADE",
            ),
            ("bot_e2ee_participations", ("dm_grant_id",), "bot_dm_grants", ("id",), "CASCADE"),
            ("bot_e2ee_participations", ("device_id",), "bot_e2ee_devices", ("id",), "CASCADE"),
        ]
        for source, columns, target, remote, ondelete in cases:
            matches = [
                fk
                for fk in inspector.get_foreign_keys(source)
                if tuple(fk["constrained_columns"]) == columns
            ]
            assert len(matches) == 1, (source, columns)
            restored = matches[0]
            assert (
                restored["referred_table"],
                tuple(restored["referred_columns"]),
                restored["options"].get("ondelete"),
            ) == (target, remote, ondelete)
            # Isolate each actual restored definition from unrelated application prerequisites.
            metadata = MetaData()
            parent_types = {
                column["name"]: column["type"] for column in inspector.get_columns(target)
            }
            child_types = {
                column["name"]: column["type"] for column in inspector.get_columns(source)
            }
            parent = Table(
                "fk_parent",
                metadata,
                *[Column(column, parent_types[column]) for column in remote],
                UniqueConstraint(*remote),
            )
            child = Table(
                "fk_child",
                metadata,
                *[Column(column, child_types[column]) for column in columns],
                ForeignKeyConstraint(
                    restored["constrained_columns"],
                    [f"fk_parent.{column}" for column in restored["referred_columns"]],
                    **restored["options"],
                ),
            )
            metadata.create_all(sync)
            values = {column: "home.example" if "domain" in column else 1 for column in remote}
            sync.execute(parent.insert(), values)
            child_values = dict(zip(columns, values.values(), strict=True))
            sync.execute(child.insert(), child_values)
            for column in columns:
                with pytest.raises(IntegrityError) as denied, sync.begin_nested():
                    sync.execute(
                        child.insert(),
                        child_values | {column: "other.example" if "domain" in column else 999},
                    )
                assert denied.value.orig.sqlstate == "23503"
            metadata.drop_all(sync)
        assert not {"application_id", "application_domain", "guild_id", "guild_domain"} & {
            column["name"] for column in inspector.get_columns("bot_e2ee_participations")
        }

    await postgres_schema.run_sync(check_downgrade)


def test_models_bind_every_durable_child_to_its_application_lineage() -> None:
    for name, table_name, columns in migration.PARENT_UNIQUE_CONSTRAINTS:
        assert unique_columns(table_name, name) == columns

    for replacement in migration.FOREIGN_KEY_REPLACEMENTS:
        constraint = foreign_key(replacement.source_table, replacement.new_name)
        assert tuple(constraint.column_keys) == replacement.new_source_columns
        assert tuple(item.target_fullname for item in constraint.elements) == tuple(
            f"{replacement.target_table}.{column}" for column in replacement.new_target_columns
        )
        assert constraint.ondelete == replacement.ondelete
        assert replacement.old_name not in {
            item.name
            for item in Base.metadata.tables[replacement.source_table].foreign_key_constraints
        }

    for name, source, target, local_columns, target_columns in migration.ADDITIONAL_FOREIGN_KEYS:
        constraint = foreign_key(source, name)
        assert tuple(constraint.column_keys) == local_columns
        assert tuple(item.target_fullname for item in constraint.elements) == tuple(
            f"{target}.{column}" for column in target_columns
        )

    participations = Base.metadata.tables["bot_e2ee_participations"]
    assert {
        "application_id",
        "application_domain",
        "guild_id",
        "guild_domain",
    } <= set(participations.c.keys())
    assert participations.c.application_id.nullable is False
    assert participations.c.application_domain.nullable is False
    guild_check = next(
        item
        for item in participations.constraints
        if isinstance(item, CheckConstraint)
        and item.name == migration.PARTICIPATION_GUILD_CHECK_NAME
    )
    assert str(guild_check.sqltext) == migration.PARTICIPATION_GUILD_CHECK
    interactions = Base.metadata.tables["bot_interactions"]
    guild_install_check = next(
        item
        for item in interactions.constraints
        if isinstance(item, CheckConstraint)
        and item.name == migration.INTERACTION_GUILD_INSTALL_CHECK_NAME
    )
    assert str(guild_install_check.sqltext) == migration.INTERACTION_GUILD_INSTALL_CHECK

    # MATCH SIMPLE skips the composite channel/guild fence for private channels;
    # retain the two-column FK so every interaction and participation still
    # requires an existing channel in that branch.
    assert foreign_key("bot_interactions", "fk_bot_interactions_channel_id_channel_domain_channels")
    assert foreign_key(
        "bot_e2ee_participations",
        "fk_bot_e2ee_participations_channel_id_channel_domain_channels",
    )


async def test_user_install_interactions_bind_the_application_but_allow_cross_clickers(
    postgres_schema, migrate_to
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.bot_models import BotApplication, BotInteraction, BotUserInstallation, DeveloperTeam
    from app.db.models import Channel, Guild, Instance, User

    await migrate_to("head")
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        session.add(Instance(domain="apps.example", is_self=False))
        await session.flush()
        session.add_all(
            [
                User(
                    id=identifier,
                    origin_domain="apps.example",
                    username=f"user{identifier}",
                    account_type="bot" if identifier in (10, 13) else "human",
                    is_local=False,
                    federation_introduced_by_domain="apps.example",
                )
                for identifier in (10, 11, 12, 13)
            ]
        )
        session.add(DeveloperTeam(id=30, origin_domain="apps.example", name="Team", personal=False))
        await session.flush()
        session.add_all(
            [
                BotApplication(
                    id=identifier,
                    origin_domain="apps.example",
                    team_id=30,
                    team_domain="apps.example",
                    bot_user_id=bot_id,
                    bot_user_domain="apps.example",
                    name=f"App{identifier}",
                )
                for identifier, bot_id in ((20, 10), (21, 13))
            ]
        )
        session.add(
            Guild(
                id=70,
                origin_domain="apps.example",
                name="Guild",
                owner_id=11,
                owner_domain="apps.example",
            )
        )
        await session.flush()
        session.add(
            Channel(
                id=80,
                origin_domain="apps.example",
                guild_id=70,
                guild_domain="apps.example",
                type=0,
                name="Room",
                created_floor_id=1,
            )
        )
        session.add(
            BotUserInstallation(
                id=50,
                application_id=20,
                application_domain="apps.example",
                user_id=11,
                user_domain="apps.example",
                granted_scopes=["applications.commands", "interactions.respond"],
                granted_intents=[],
                contexts=["guild"],
                grant_revision=1,
                status="active",
            )
        )
        await session.flush()
        fields = dict(
            application_domain="apps.example",
            user_installation_id=50,
            guild_id=70,
            guild_domain="apps.example",
            channel_id=80,
            channel_domain="apps.example",
            user_id=12,
            user_domain="apps.example",
            context="guild",
            integration_type="user_install",
            interaction_type="component",
            invocation_channel_type=0,
            invocation_permissions=0,
            installation_revision=1,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        session.add(BotInteraction(id=100, application_id=20, **fields))
        await session.flush()
        assert (
            await session.execute(
                text("SELECT user_id,user_installation_id FROM bot_interactions WHERE id=100")
            )
        ).one() == (12, 50)
        with pytest.raises(
            IntegrityError, match="fk_bot_interactions_user_installation_application_lineage"
        ):
            async with session.begin_nested():
                session.add(BotInteraction(id=101, application_id=21, **fields))
                await session.flush()
