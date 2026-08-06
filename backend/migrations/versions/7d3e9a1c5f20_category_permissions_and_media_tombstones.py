"""add category permission sync and durable remote media tombstones

Revision ID: 7d3e9a1c5f20
Revises: 4c2f8a1d9b60
Create Date: 2026-08-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3e9a1c5f20"
down_revision: str | None = "4c2f8a1d9b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_messages_deleted_message_has_no_e2ee"),
        "messages",
        "deleted_at IS NULL OR e2ee IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_messages_plaintext_or_e2ee"), "messages", "content IS NULL OR e2ee IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_messages_e2ee_is_object"),
        "messages",
        "e2ee IS NULL OR jsonb_typeof(e2ee) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        "(permissions & ~1103806065919) = 0",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        "((allow | deny) & ~1103806065919) = 0",
    )
    op.add_column(
        "channels",
        sa.Column(
            "permissions_synced",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    # Existing categorized channels with no independent overwrite set have
    # always behaved visually like inherited children. Preserve that intent.
    op.execute(
        """
        UPDATE channels AS channel
        SET permissions_synced = true
        WHERE channel.parent_id IS NOT NULL
          AND channel.type <> 4
          AND NOT EXISTS (
              SELECT 1
              FROM channel_overwrites AS overwrite
              WHERE overwrite.channel_id = channel.id
                AND overwrite.channel_domain = channel.origin_domain
          )
        """
    )
    op.create_check_constraint(
        op.f("ck_channels_permission_sync_requires_parent"),
        "channels",
        "NOT permissions_synced OR (parent_id IS NOT NULL AND type <> 4)",
    )
    op.create_table(
        "remote_media_tombstones",
        sa.Column("origin_domain", sa.String(length=253), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attachment_id >= 0",
            name=op.f("ck_remote_media_tombstones_nonnegative_attachment_id"),
        ),
        sa.ForeignKeyConstraint(
            ["origin_domain"],
            ["instances.domain"],
            name=op.f("fk_remote_media_tombstones_origin_domain_instances"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "origin_domain",
            "attachment_id",
            name=op.f("pk_remote_media_tombstones"),
        ),
    )
    op.add_column(
        "guild_history_imports",
        sa.Column("remote_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "guild_history_imports", sa.Column("ack_error", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "guild_history_imports", sa.Column("lease_owner", sa.String(length=64), nullable=True)
    )
    op.add_column(
        "guild_history_imports",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column_name, column_type in (
        ("pages_downloaded", sa.Integer()),
        ("bytes_downloaded", sa.BigInteger()),
        ("messages_downloaded", sa.BigInteger()),
        ("reactions_downloaded", sa.BigInteger()),
    ):
        op.add_column(
            "guild_history_imports",
            sa.Column(column_name, column_type, server_default="0", nullable=False),
        )
    op.create_check_constraint(
        op.f("ck_guild_history_imports_nonnegative_budgets"),
        "guild_history_imports",
        "pages_downloaded >= 0 AND bytes_downloaded >= 0 "
        "AND messages_downloaded >= 0 AND reactions_downloaded >= 0",
    )
    op.create_unique_constraint(
        "uq_guild_history_imports_grant_generation",
        "guild_history_imports",
        [
            "guild_id",
            "guild_domain",
            "requester_user_id",
            "requester_user_domain",
            "requester_member_version",
            "permission_generation",
            "history_policy_generation",
        ],
    )
    op.create_table(
        "guild_history_import_channels",
        sa.Column("export_id", sa.BigInteger(), nullable=False),
        sa.Column("export_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("upper_bound_id", sa.BigInteger(), nullable=False),
        sa.Column("next_before_id", sa.BigInteger(), nullable=False),
        sa.Column("complete", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("pages_downloaded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes_downloaded", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("messages_downloaded", sa.BigInteger(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "upper_bound_id >= 0 AND next_before_id >= 0",
            name=op.f("ck_guild_history_import_channels_nonnegative_cursors"),
        ),
        sa.CheckConstraint(
            "pages_downloaded >= 0 AND bytes_downloaded >= 0 AND messages_downloaded >= 0",
            name=op.f("ck_guild_history_import_channels_nonnegative_budgets"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            name=op.f("fk_guild_history_import_channels_channel_id_channel_domain_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["export_id", "export_domain"],
            ["guild_history_imports.export_id", "guild_history_imports.export_domain"],
            name=op.f("fk_guild_history_import_channels_export_id_export_domain_guild_history_imports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "export_id",
            "export_domain",
            "channel_id",
            "channel_domain",
            name=op.f("pk_guild_history_import_channels"),
        ),
    )


def downgrade() -> None:
    op.drop_table("guild_history_import_channels")
    op.drop_constraint(
        "uq_guild_history_imports_grant_generation",
        "guild_history_imports",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_guild_history_imports_nonnegative_budgets"),
        "guild_history_imports",
        type_="check",
    )
    for column_name in (
        "reactions_downloaded",
        "messages_downloaded",
        "bytes_downloaded",
        "pages_downloaded",
        "lease_expires_at",
        "lease_owner",
        "ack_error",
        "remote_acknowledged_at",
    ):
        op.drop_column("guild_history_imports", column_name)
    op.drop_table("remote_media_tombstones")
    op.drop_constraint(op.f("ck_messages_e2ee_is_object"), "messages", type_="check")
    op.drop_constraint(op.f("ck_messages_plaintext_or_e2ee"), "messages", type_="check")
    op.drop_constraint(
        op.f("ck_messages_deleted_message_has_no_e2ee"), "messages", type_="check"
    )
    op.drop_constraint(
        op.f("ck_channels_permission_sync_requires_parent"),
        "channels",
        type_="check",
    )
    op.drop_column("channels", "permissions_synced")
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        type_="check",
    )
