from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.db import models  # noqa: F401
from app.db.base import Base
from scripts import verify_migration_guards

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


def test_lineage_migration_precedes_the_current_single_head() -> None:
    assert migration.revision == "2c8f4d0b6e31"
    assert migration.down_revision == "1b7e3c9a5d20"
    assert verify_migration_guards.CURRENT_HEAD_REVISION == "4ea6c2d8f953"


def test_lineage_preflight_covers_every_replaced_reference() -> None:
    sql = migration.LINEAGE_PREFLIGHT_SQL
    expected_kinds = {
        "bot_installation.application_bot_user",
        "command_permission.command_application",
        "bot_token.worker_application",
        "bot_token.dm_capability_application",
        "bot_interaction.installation_application_guild",
        "bot_interaction.channel_guild",
        "bot_interaction.user_installation_application",
        "bot_interaction.dm_capability_application_channel_user",
        "bot_interaction.command_application",
        "bot_dm_grant.installation_application",
        "bot_dm_grant.user_installation_application",
        "bot_dm_grant.dm_capability_application_channel_user",
        "bot_e2ee_device.worker_application",
        "bot_e2ee_participation.device_installation_application",
        "bot_e2ee_participation.installation_channel_guild",
        "bot_e2ee_participation.device_grant_application",
        "bot_e2ee_participation.grant_channel",
    }

    assert all(kind in sql for kind in expected_kinds)
    assert sql.count("IS DISTINCT FROM") == len(expected_kinds)
    assert "ERRCODE = '23514'" in sql
    assert "repair the cross-application child reference" in sql


def test_upgrade_preflights_before_replacing_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            f=lambda name: name,
            execute=lambda sql: calls.append(("execute", sql)),
            add_column=lambda table, column: calls.append(
                ("add_column", table, column.name, column.nullable)
            ),
            alter_column=lambda table, column, **kwargs: calls.append(
                ("alter_column", table, column, kwargs)
            ),
            create_check_constraint=lambda name, table, condition: calls.append(
                ("create_check", name, table, condition)
            ),
            create_unique_constraint=lambda name, table, columns: calls.append(
                ("create_unique", name, table, tuple(columns))
            ),
            drop_constraint=lambda name, table, **kwargs: calls.append(
                ("drop", name, table, kwargs)
            ),
            create_foreign_key=lambda name, source, target, local, remote, **kwargs: calls.append(
                ("create_fk", name, source, target, tuple(local), tuple(remote), kwargs)
            ),
        ),
    )

    migration.upgrade()

    assert calls[0] == ("execute", migration.LINEAGE_PREFLIGHT_SQL)
    backfill_index = calls.index(("execute", migration.PARTICIPATION_BACKFILL_SQL))
    assert [call[2] for call in calls[1:backfill_index]] == [
        "application_id",
        "application_domain",
        "guild_id",
        "guild_domain",
    ]
    assert all(call[0] == "add_column" for call in calls[1:backfill_index])
    assert [call[2] for call in calls[backfill_index + 1 : backfill_index + 3]] == [
        "application_id",
        "application_domain",
    ]
    assert calls[backfill_index + 3] == (
        "create_check",
        migration.PARTICIPATION_GUILD_CHECK_NAME,
        "bot_e2ee_participations",
        migration.PARTICIPATION_GUILD_CHECK,
    )
    assert calls[backfill_index + 4] == (
        "create_check",
        migration.INTERACTION_GUILD_INSTALL_CHECK_NAME,
        "bot_interactions",
        migration.INTERACTION_GUILD_INSTALL_CHECK,
    )
    first_unique = backfill_index + 5
    first_fk = first_unique + len(migration.PARENT_UNIQUE_CONSTRAINTS)
    assert all(call[0] == "create_unique" for call in calls[first_unique:first_fk])
    replacements = migration.FOREIGN_KEY_REPLACEMENTS
    replacement_end = first_fk + len(replacements) * 2
    for replacement, drop_call, create_call in zip(
        replacements,
        calls[first_fk:replacement_end:2],
        calls[first_fk + 1 : replacement_end : 2],
        strict=True,
    ):
        assert drop_call == (
            "drop",
            replacement.old_name,
            replacement.source_table,
            {"type_": "foreignkey"},
        )
        assert create_call[0:4] == (
            "create_fk",
            replacement.new_name,
            replacement.source_table,
            replacement.target_table,
        )
    assert [call[1] for call in calls[replacement_end:]] == [
        name for name, _source, _target, _local, _remote in migration.ADDITIONAL_FOREIGN_KEYS
    ]


def test_downgrade_restores_the_original_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            f=lambda name: name,
            drop_constraint=lambda name, table, **kwargs: calls.append(
                ("drop", name, table, kwargs)
            ),
            create_foreign_key=lambda name, source, target, local, remote, **kwargs: calls.append(
                ("create_fk", name, source, target, tuple(local), tuple(remote), kwargs)
            ),
            drop_column=lambda table, column: calls.append(("drop_column", table, column)),
        ),
    )

    migration.downgrade()

    additional_count = len(migration.ADDITIONAL_FOREIGN_KEYS)
    assert [call[1] for call in calls[:additional_count]] == [
        name
        for name, _source, _target, _local, _remote in reversed(migration.ADDITIONAL_FOREIGN_KEYS)
    ]
    replacements = tuple(reversed(migration.FOREIGN_KEY_REPLACEMENTS))
    fk_start = additional_count
    fk_end = fk_start + len(replacements) * 2
    fk_calls = calls[fk_start:fk_end]
    for replacement, drop_call, create_call in zip(
        replacements,
        fk_calls[::2],
        fk_calls[1::2],
        strict=True,
    ):
        assert drop_call[1:3] == (replacement.new_name, replacement.source_table)
        assert create_call[1:6] == (
            replacement.old_name,
            replacement.source_table,
            replacement.target_table,
            replacement.old_source_columns,
            replacement.old_target_columns,
        )
    assert calls[fk_end] == (
        "drop",
        migration.INTERACTION_GUILD_INSTALL_CHECK_NAME,
        "bot_interactions",
        {"type_": "check"},
    )
    assert calls[fk_end + 1] == (
        "drop",
        migration.PARTICIPATION_GUILD_CHECK_NAME,
        "bot_e2ee_participations",
        {"type_": "check"},
    )
    assert [call[2] for call in calls[fk_end + 2 : fk_end + 6]] == [
        "guild_domain",
        "guild_id",
        "application_domain",
        "application_id",
    ]
    assert [call[1] for call in calls[fk_end + 6 :]] == [
        name for name, _table, _columns in reversed(migration.PARENT_UNIQUE_CONSTRAINTS)
    ]


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


def test_user_install_interactions_bind_the_application_but_allow_cross_clickers() -> None:
    lineage = foreign_key(
        "bot_interactions",
        "fk_bot_interactions_user_installation_application_lineage",
    )
    assert tuple(lineage.column_keys) == (
        "user_installation_id",
        "application_id",
        "application_domain",
    )
    assert tuple(item.target_fullname for item in lineage.elements) == (
        "bot_user_installations.id",
        "bot_user_installations.application_id",
        "bot_user_installations.application_domain",
    )
    assert tuple(
        foreign_key(
            "bot_interactions",
            "fk_bot_interactions_user_id_user_domain_users",
        ).column_keys
    ) == ("user_id", "user_domain")

    sql = migration.LINEAGE_PREFLIGHT_SQL
    start = sql.index("'bot_interaction.user_installation_application'")
    segment = sql[start : sql.index("UNION ALL", start)]
    assert "interaction.application_id" in segment
    assert "installation.application_id" in segment
    assert "interaction.user_id" not in segment
    assert "installation.user_id" not in segment
