"""Fence bot child rows to one application lineage.

Revision ID: 2c8f4d0b6e31
Revises: 1b7e3c9a5d20
Create Date: 2026-08-29
"""

from collections.abc import Sequence
from dataclasses import dataclass

import sqlalchemy as sa
from alembic import op

revision: str = "2c8f4d0b6e31"
down_revision: str | None = "1b7e3c9a5d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_LENGTH = 253


@dataclass(frozen=True)
class ForeignKeyReplacement:
    old_name: str
    new_name: str
    source_table: str
    target_table: str
    old_source_columns: tuple[str, ...]
    old_target_columns: tuple[str, ...]
    new_source_columns: tuple[str, ...]
    new_target_columns: tuple[str, ...]
    ondelete: str | None = None


PARENT_UNIQUE_CONSTRAINTS = (
    (
        "uq_bot_applications_id_origin_bot_user",
        "bot_applications",
        ("id", "origin_domain", "bot_user_id", "bot_user_domain"),
    ),
    (
        "uq_bot_workers_id_application",
        "bot_workers",
        ("id", "application_id", "application_domain"),
    ),
    (
        "uq_application_commands_id_application",
        "application_commands",
        ("id", "application_id", "application_domain"),
    ),
    (
        "uq_bot_installations_id_application",
        "bot_installations",
        ("id", "application_id", "application_domain"),
    ),
    (
        "uq_bot_installations_id_application_guild",
        "bot_installations",
        ("id", "application_id", "application_domain", "guild_id", "guild_domain"),
    ),
    (
        "uq_bot_user_installations_id_application",
        "bot_user_installations",
        ("id", "application_id", "application_domain"),
    ),
    (
        "uq_bot_dm_capabilities_id_application",
        "bot_dm_capabilities",
        ("id", "application_id", "application_domain"),
    ),
    (
        "uq_bot_dm_capabilities_id_app_conversation_target",
        "bot_dm_capabilities",
        (
            "id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            "target_user_id",
            "target_user_domain",
        ),
    ),
    (
        "uq_bot_dm_grants_id_application_conversation",
        "bot_dm_grants",
        ("id", "application_id", "application_domain", "conversation_id", "conversation_domain"),
    ),
    (
        "uq_bot_e2ee_devices_id_application",
        "bot_e2ee_devices",
        ("id", "application_id", "application_domain"),
    ),
)

FOREIGN_KEY_REPLACEMENTS = (
    ForeignKeyReplacement(
        "fk_bot_installations_application_ref_bot_applications",
        "fk_bot_installations_application_bot_user_lineage",
        "bot_installations",
        "bot_applications",
        ("application_id", "application_domain"),
        ("id", "origin_domain"),
        ("application_id", "application_domain", "bot_user_id", "bot_user_domain"),
        ("id", "origin_domain", "bot_user_id", "bot_user_domain"),
    ),
    ForeignKeyReplacement(
        "fk_application_command_permissions_command_id_application_commands",
        "fk_application_command_permissions_command_application_lineage",
        "application_command_permissions",
        "application_commands",
        ("command_id",),
        ("id",),
        ("command_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_tokens_worker_id_bot_workers",
        "fk_bot_tokens_worker_application_lineage",
        "bot_tokens",
        "bot_workers",
        ("worker_id",),
        ("id",),
        ("worker_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_tokens_dm_capability_id_bot_dm_capabilities",
        "fk_bot_tokens_dm_capability_application_lineage",
        "bot_tokens",
        "bot_dm_capabilities",
        ("dm_capability_id",),
        ("id",),
        ("dm_capability_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_interactions_installation_id_bot_installations",
        "fk_bot_interactions_installation_application_lineage",
        "bot_interactions",
        "bot_installations",
        ("installation_id",),
        ("id",),
        (
            "installation_id",
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
        ),
        ("id", "application_id", "application_domain", "guild_id", "guild_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_interactions_user_installation_id_bot_user_installations",
        "fk_bot_interactions_user_installation_application_lineage",
        "bot_interactions",
        "bot_user_installations",
        ("user_installation_id",),
        ("id",),
        ("user_installation_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_interactions_dm_capability_id_bot_dm_capabilities",
        "fk_bot_interactions_dm_capability_lineage",
        "bot_interactions",
        "bot_dm_capabilities",
        ("dm_capability_id",),
        ("id",),
        (
            "dm_capability_id",
            "application_id",
            "application_domain",
            "channel_id",
            "channel_domain",
            "user_id",
            "user_domain",
        ),
        (
            "id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            "target_user_id",
            "target_user_domain",
        ),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_interactions_command_id_application_commands",
        "fk_bot_interactions_command_application_lineage",
        "bot_interactions",
        "application_commands",
        ("command_id",),
        ("id",),
        ("command_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "RESTRICT",
    ),
    ForeignKeyReplacement(
        "fk_bot_dm_grants_installation_id_bot_installations",
        "fk_bot_dm_grants_installation_application_lineage",
        "bot_dm_grants",
        "bot_installations",
        ("installation_id",),
        ("id",),
        ("installation_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_dm_grants_user_installation_id_bot_user_installations",
        "fk_bot_dm_grants_user_installation_application_lineage",
        "bot_dm_grants",
        "bot_user_installations",
        ("user_installation_id",),
        ("id",),
        ("user_installation_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_dm_grants_dm_capability_id_bot_dm_capabilities",
        "fk_bot_dm_grants_dm_capability_lineage",
        "bot_dm_grants",
        "bot_dm_capabilities",
        ("dm_capability_id",),
        ("id",),
        (
            "dm_capability_id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            "granted_by_id",
            "granted_by_domain",
        ),
        (
            "id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            "target_user_id",
            "target_user_domain",
        ),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_e2ee_devices_worker_id_bot_workers",
        "fk_bot_e2ee_devices_worker_application_lineage",
        "bot_e2ee_devices",
        "bot_workers",
        ("worker_id",),
        ("id",),
        ("worker_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_e2ee_participations_installation_id_bot_installations",
        "fk_bot_e2ee_participations_installation_lineage",
        "bot_e2ee_participations",
        "bot_installations",
        ("installation_id",),
        ("id",),
        (
            "installation_id",
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
        ),
        ("id", "application_id", "application_domain", "guild_id", "guild_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_e2ee_participations_dm_grant_id_bot_dm_grants",
        "fk_bot_e2ee_participations_dm_grant_lineage",
        "bot_e2ee_participations",
        "bot_dm_grants",
        ("dm_grant_id",),
        ("id",),
        (
            "dm_grant_id",
            "application_id",
            "application_domain",
            "channel_id",
            "channel_domain",
        ),
        ("id", "application_id", "application_domain", "conversation_id", "conversation_domain"),
        "CASCADE",
    ),
    ForeignKeyReplacement(
        "fk_bot_e2ee_participations_device_id_bot_e2ee_devices",
        "fk_bot_e2ee_participations_device_application_lineage",
        "bot_e2ee_participations",
        "bot_e2ee_devices",
        ("device_id",),
        ("id",),
        ("device_id", "application_id", "application_domain"),
        ("id", "application_id", "application_domain"),
        "CASCADE",
    ),
)

LINEAGE_PREFLIGHT_SQL = """
DO $$
DECLARE
    mismatch_kind text;
    mismatch_row text;
BEGIN
    SELECT candidate.kind, candidate.row_ref
    INTO mismatch_kind, mismatch_row
    FROM (
        SELECT 'bot_installation.application_bot_user' AS kind, installation.id::text AS row_ref
        FROM bot_installations AS installation
        JOIN bot_applications AS application
          ON application.id = installation.application_id
         AND application.origin_domain = installation.application_domain
        WHERE (installation.bot_user_id, installation.bot_user_domain)
              IS DISTINCT FROM (application.bot_user_id, application.bot_user_domain)

        UNION ALL
        SELECT 'command_permission.command_application', permission.id::text
        FROM application_command_permissions AS permission
        JOIN application_commands AS command ON command.id = permission.command_id
        WHERE (permission.application_id, permission.application_domain)
              IS DISTINCT FROM (command.application_id, command.application_domain)

        UNION ALL
        SELECT 'bot_token.worker_application', token.id::text
        FROM bot_tokens AS token
        JOIN bot_workers AS worker ON worker.id = token.worker_id
        WHERE (token.application_id, token.application_domain)
              IS DISTINCT FROM (worker.application_id, worker.application_domain)

        UNION ALL
        SELECT 'bot_token.dm_capability_application', token.id::text
        FROM bot_tokens AS token
        JOIN bot_dm_capabilities AS capability ON capability.id = token.dm_capability_id
        WHERE (token.application_id, token.application_domain)
              IS DISTINCT FROM (capability.application_id, capability.application_domain)

        UNION ALL
        SELECT 'bot_interaction.installation_application_guild', interaction.id::text
        FROM bot_interactions AS interaction
        JOIN bot_installations AS installation ON installation.id = interaction.installation_id
        WHERE (
            interaction.application_id,
            interaction.application_domain,
            interaction.guild_id,
            interaction.guild_domain
        ) IS DISTINCT FROM (
            installation.application_id,
            installation.application_domain,
            installation.guild_id,
            installation.guild_domain
        )

        UNION ALL
        SELECT 'bot_interaction.channel_guild', interaction.id::text
        FROM bot_interactions AS interaction
        JOIN channels AS channel
          ON channel.id = interaction.channel_id
         AND channel.origin_domain = interaction.channel_domain
        WHERE interaction.guild_id IS NOT NULL
          AND (interaction.guild_id, interaction.guild_domain)
              IS DISTINCT FROM (channel.guild_id, channel.guild_domain)

        UNION ALL
        SELECT 'bot_interaction.user_installation_application', interaction.id::text
        FROM bot_interactions AS interaction
        JOIN bot_user_installations AS installation
          ON installation.id = interaction.user_installation_id
        WHERE (interaction.application_id, interaction.application_domain)
              IS DISTINCT FROM (installation.application_id, installation.application_domain)

        UNION ALL
        SELECT 'bot_interaction.dm_capability_application_channel_user', interaction.id::text
        FROM bot_interactions AS interaction
        JOIN bot_dm_capabilities AS capability ON capability.id = interaction.dm_capability_id
        WHERE (
            interaction.application_id,
            interaction.application_domain,
            interaction.channel_id,
            interaction.channel_domain,
            interaction.user_id,
            interaction.user_domain
        ) IS DISTINCT FROM (
            capability.application_id,
            capability.application_domain,
            capability.conversation_id,
            capability.conversation_domain,
            capability.target_user_id,
            capability.target_user_domain
        )

        UNION ALL
        SELECT 'bot_interaction.command_application', interaction.id::text
        FROM bot_interactions AS interaction
        JOIN application_commands AS command ON command.id = interaction.command_id
        WHERE (interaction.application_id, interaction.application_domain)
              IS DISTINCT FROM (command.application_id, command.application_domain)

        UNION ALL
        SELECT 'bot_dm_grant.installation_application', grant_row.id::text
        FROM bot_dm_grants AS grant_row
        JOIN bot_installations AS installation ON installation.id = grant_row.installation_id
        WHERE (grant_row.application_id, grant_row.application_domain)
              IS DISTINCT FROM (installation.application_id, installation.application_domain)

        UNION ALL
        SELECT 'bot_dm_grant.user_installation_application', grant_row.id::text
        FROM bot_dm_grants AS grant_row
        JOIN bot_user_installations AS installation
          ON installation.id = grant_row.user_installation_id
        WHERE (grant_row.application_id, grant_row.application_domain)
              IS DISTINCT FROM (installation.application_id, installation.application_domain)

        UNION ALL
        SELECT 'bot_dm_grant.dm_capability_application_channel_user', grant_row.id::text
        FROM bot_dm_grants AS grant_row
        JOIN bot_dm_capabilities AS capability ON capability.id = grant_row.dm_capability_id
        WHERE (
            grant_row.application_id,
            grant_row.application_domain,
            grant_row.conversation_id,
            grant_row.conversation_domain,
            grant_row.granted_by_id,
            grant_row.granted_by_domain
        ) IS DISTINCT FROM (
            capability.application_id,
            capability.application_domain,
            capability.conversation_id,
            capability.conversation_domain,
            capability.target_user_id,
            capability.target_user_domain
        )

        UNION ALL
        SELECT 'bot_e2ee_device.worker_application', device.id::text
        FROM bot_e2ee_devices AS device
        JOIN bot_workers AS worker ON worker.id = device.worker_id
        WHERE (device.application_id, device.application_domain)
              IS DISTINCT FROM (worker.application_id, worker.application_domain)

        UNION ALL
        SELECT 'bot_e2ee_participation.device_installation_application', participation.id::text
        FROM bot_e2ee_participations AS participation
        JOIN bot_e2ee_devices AS device ON device.id = participation.device_id
        JOIN bot_installations AS installation
          ON installation.id = participation.installation_id
        WHERE (device.application_id, device.application_domain)
              IS DISTINCT FROM (installation.application_id, installation.application_domain)

        UNION ALL
        SELECT 'bot_e2ee_participation.installation_channel_guild', participation.id::text
        FROM bot_e2ee_participations AS participation
        JOIN bot_installations AS installation
          ON installation.id = participation.installation_id
        JOIN channels AS channel
          ON channel.id = participation.channel_id
         AND channel.origin_domain = participation.channel_domain
        WHERE (channel.guild_id, channel.guild_domain)
              IS DISTINCT FROM (installation.guild_id, installation.guild_domain)

        UNION ALL
        SELECT 'bot_e2ee_participation.device_grant_application', participation.id::text
        FROM bot_e2ee_participations AS participation
        JOIN bot_e2ee_devices AS device ON device.id = participation.device_id
        JOIN bot_dm_grants AS grant_row ON grant_row.id = participation.dm_grant_id
        WHERE (device.application_id, device.application_domain)
              IS DISTINCT FROM (grant_row.application_id, grant_row.application_domain)

        UNION ALL
        SELECT 'bot_e2ee_participation.grant_channel', participation.id::text
        FROM bot_e2ee_participations AS participation
        JOIN bot_dm_grants AS grant_row ON grant_row.id = participation.dm_grant_id
        WHERE (participation.channel_id, participation.channel_domain)
              IS DISTINCT FROM (grant_row.conversation_id, grant_row.conversation_domain)
    ) AS candidate
    ORDER BY candidate.kind, candidate.row_ref
    LIMIT 1;

    IF mismatch_kind IS NOT NULL THEN
        RAISE EXCEPTION
            'bot application lineage constraint blocked by % mismatch at row %',
            mismatch_kind, mismatch_row
            USING ERRCODE = '23514',
                  HINT = 'repair the cross-application child reference before retrying';
    END IF;
END
$$;
"""

PARTICIPATION_BACKFILL_SQL = """
UPDATE bot_e2ee_participations AS participation
SET application_id = device.application_id,
    application_domain = device.application_domain,
    guild_id = channel.guild_id,
    guild_domain = channel.guild_domain
FROM bot_e2ee_devices AS device, channels AS channel
WHERE device.id = participation.device_id
  AND channel.id = participation.channel_id
  AND channel.origin_domain = participation.channel_domain;
"""

PARTICIPATION_GUILD_CHECK = (
    "(installation_id IS NULL) = (guild_id IS NULL) AND (guild_id IS NULL) = (guild_domain IS NULL)"
)
PARTICIPATION_GUILD_CHECK_NAME = "ck_bot_e2ee_participations_bot_e2ee_participation_guild_lineage"
INTERACTION_GUILD_INSTALL_CHECK = "installation_id IS NULL OR guild_id IS NOT NULL"
INTERACTION_GUILD_INSTALL_CHECK_NAME = "ck_bot_interactions_bot_interaction_guild_install_context"

ADDITIONAL_FOREIGN_KEYS = (
    (
        "fk_bot_interactions_channel_guild_lineage",
        "bot_interactions",
        "channels",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
        ("id", "origin_domain", "guild_id", "guild_domain"),
    ),
    (
        "fk_bot_e2ee_participations_channel_guild_lineage",
        "bot_e2ee_participations",
        "channels",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
        ("id", "origin_domain", "guild_id", "guild_domain"),
    ),
)


def _create_foreign_key(replacement: ForeignKeyReplacement) -> None:
    op.create_foreign_key(
        replacement.new_name,
        replacement.source_table,
        replacement.target_table,
        list(replacement.new_source_columns),
        list(replacement.new_target_columns),
        ondelete=replacement.ondelete,
    )


def _restore_foreign_key(replacement: ForeignKeyReplacement) -> None:
    op.create_foreign_key(
        op.f(replacement.old_name),
        replacement.source_table,
        replacement.target_table,
        list(replacement.old_source_columns),
        list(replacement.old_target_columns),
        ondelete=replacement.ondelete,
    )


def upgrade() -> None:
    op.execute(LINEAGE_PREFLIGHT_SQL)
    op.add_column("bot_e2ee_participations", sa.Column("application_id", sa.BigInteger()))
    op.add_column(
        "bot_e2ee_participations", sa.Column("application_domain", sa.String(DOMAIN_LENGTH))
    )
    op.add_column("bot_e2ee_participations", sa.Column("guild_id", sa.BigInteger()))
    op.add_column("bot_e2ee_participations", sa.Column("guild_domain", sa.String(DOMAIN_LENGTH)))
    op.execute(PARTICIPATION_BACKFILL_SQL)
    op.alter_column(
        "bot_e2ee_participations",
        "application_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "bot_e2ee_participations",
        "application_domain",
        existing_type=sa.String(DOMAIN_LENGTH),
        nullable=False,
    )
    op.create_check_constraint(
        op.f(PARTICIPATION_GUILD_CHECK_NAME),
        "bot_e2ee_participations",
        PARTICIPATION_GUILD_CHECK,
    )
    op.create_check_constraint(
        op.f(INTERACTION_GUILD_INSTALL_CHECK_NAME),
        "bot_interactions",
        INTERACTION_GUILD_INSTALL_CHECK,
    )
    for name, table, columns in PARENT_UNIQUE_CONSTRAINTS:
        op.create_unique_constraint(name, table, list(columns))
    for replacement in FOREIGN_KEY_REPLACEMENTS:
        op.drop_constraint(op.f(replacement.old_name), replacement.source_table, type_="foreignkey")
        _create_foreign_key(replacement)
    for name, source, target, local_columns, target_columns in ADDITIONAL_FOREIGN_KEYS:
        op.create_foreign_key(
            name,
            source,
            target,
            list(local_columns),
            list(target_columns),
        )


def downgrade() -> None:
    for name, source, _target, _local_columns, _target_columns in reversed(ADDITIONAL_FOREIGN_KEYS):
        op.drop_constraint(name, source, type_="foreignkey")
    for replacement in reversed(FOREIGN_KEY_REPLACEMENTS):
        op.drop_constraint(replacement.new_name, replacement.source_table, type_="foreignkey")
        _restore_foreign_key(replacement)
    op.drop_constraint(
        op.f(INTERACTION_GUILD_INSTALL_CHECK_NAME),
        "bot_interactions",
        type_="check",
    )
    op.drop_constraint(
        op.f(PARTICIPATION_GUILD_CHECK_NAME),
        "bot_e2ee_participations",
        type_="check",
    )
    op.drop_column("bot_e2ee_participations", "guild_domain")
    op.drop_column("bot_e2ee_participations", "guild_id")
    op.drop_column("bot_e2ee_participations", "application_domain")
    op.drop_column("bot_e2ee_participations", "application_id")
    for name, table, _columns in reversed(PARENT_UNIQUE_CONSTRAINTS):
        op.drop_constraint(name, table, type_="unique")
