"""add permission-bound federated history sync

Revision ID: 4c2f8a1d9b60
Revises: a8d4c6e2f901
Create Date: 2026-08-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "4c2f8a1d9b60"
down_revision: str | None = "a8d4c6e2f901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_instances_capabilities_are_array"),
        "instances",
        "jsonb_typeof(capabilities) = 'array'",
    )
    op.add_column(
        "users",
        sa.Column("profile_resolved", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_users_unresolved_history_handle"),
        "users",
        "is_local OR profile_resolved OR username LIKE 'history_%'",
    )
    op.add_column(
        "guilds",
        sa.Column(
            "federated_history_policy",
            sa.String(length=16),
            server_default="disabled",
            nullable=False,
        ),
    )
    op.add_column(
        "guilds",
        sa.Column(
            "history_policy_generation",
            sa.BigInteger(),
            server_default="1",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_guilds_federated_history_policy_value"),
        "guilds",
        "federated_history_policy IN ('disabled','full_retained')",
    )
    op.create_check_constraint(
        op.f("ck_guilds_positive_history_policy_generation"),
        "guilds",
        "history_policy_generation >= 1",
    )
    op.add_column(
        "channels",
        sa.Column(
            "federated_history_policy",
            sa.String(length=16),
            server_default="inherit",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_channels_federated_history_policy_value"),
        "channels",
        "federated_history_policy IN ('inherit','disabled','full_retained')",
    )

    op.create_table(
        "guild_history_exports",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("requester_origin", sa.String(length=253), nullable=False),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("requester_user_domain", sa.String(length=253), nullable=False),
        sa.Column("requester_member_version", sa.BigInteger(), nullable=False),
        sa.Column("baseline_seq", sa.BigInteger(), nullable=False),
        sa.Column("permission_generation", sa.BigInteger(), nullable=False),
        sa.Column("history_policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("baseline_seq >= 0", name=op.f("ck_guild_history_exports_nonnegative_baseline_seq")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_guild_history_exports_positive_expiry")),
        sa.CheckConstraint("history_policy_generation >= 1", name=op.f("ck_guild_history_exports_positive_history_policy_generation")),
        sa.CheckConstraint("permission_generation >= 1", name=op.f("ck_guild_history_exports_positive_permission_generation")),
        sa.CheckConstraint("requester_origin = requester_user_domain", name=op.f("ck_guild_history_exports_requester_origin_match")),
        sa.CheckConstraint("requester_member_version >= 1", name=op.f("ck_guild_history_exports_positive_member_version")),
        sa.CheckConstraint("status IN ('active','completed','revoked','expired','failed')", name=op.f("ck_guild_history_exports_status_value")),
        sa.ForeignKeyConstraint(["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], name=op.f("fk_guild_history_exports_guild_id_guild_domain_guilds"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requester_origin"], ["instances.domain"], name=op.f("fk_guild_history_exports_requester_origin_instances"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requester_user_id", "requester_user_domain"], ["users.id", "users.origin_domain"], name=op.f("fk_guild_history_exports_requester_user_id_requester_user_domain_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_guild_history_exports")),
    )
    op.create_index(
        "ix_guild_history_exports_active",
        "guild_history_exports",
        ["guild_id", "guild_domain", "requester_origin", "expires_at"],
        unique=False,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "guild_history_export_channels",
        sa.Column("export_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("upper_bound_id", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("upper_bound_id >= 0", name=op.f("ck_guild_history_export_channels_nonnegative_upper_bound")),
        sa.ForeignKeyConstraint(["channel_id", "channel_domain"], ["channels.id", "channels.origin_domain"], name=op.f("fk_guild_history_export_channels_channel_id_channel_domain_channels"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["export_id"], ["guild_history_exports.id"], name=op.f("fk_guild_history_export_channels_export_id_guild_history_exports"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("export_id", "channel_id", "channel_domain", name=op.f("pk_guild_history_export_channels")),
    )
    op.create_table(
        "guild_history_imports",
        sa.Column("export_id", sa.BigInteger(), nullable=False),
        sa.Column("export_domain", sa.String(length=253), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("requester_user_domain", sa.String(length=253), nullable=False),
        sa.Column("requester_member_version", sa.BigInteger(), nullable=False),
        sa.Column("baseline_seq", sa.BigInteger(), nullable=False),
        sa.Column("permission_generation", sa.BigInteger(), nullable=False),
        sa.Column("history_policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("baseline_seq >= 0", name=op.f("ck_guild_history_imports_nonnegative_baseline_seq")),
        sa.CheckConstraint("export_domain = guild_domain", name=op.f("ck_guild_history_imports_export_is_guild_home")),
        sa.CheckConstraint("requester_member_version >= 1", name=op.f("ck_guild_history_imports_positive_member_version")),
        sa.CheckConstraint("permission_generation >= 1", name=op.f("ck_guild_history_imports_positive_permission_generation")),
        sa.CheckConstraint("history_policy_generation >= 1", name=op.f("ck_guild_history_imports_positive_history_policy_generation")),
        sa.CheckConstraint("status IN ('pending','downloading','reconciling','completed','revoked','failed')", name=op.f("ck_guild_history_imports_status_value")),
        sa.ForeignKeyConstraint(["export_domain"], ["instances.domain"], name=op.f("fk_guild_history_imports_export_domain_instances"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], name=op.f("fk_guild_history_imports_guild_id_guild_domain_guilds"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requester_user_id", "requester_user_domain"], ["users.id", "users.origin_domain"], name=op.f("fk_guild_history_imports_requester_user_id_requester_user_domain_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("export_id", "export_domain", name=op.f("pk_guild_history_imports")),
    )
    op.create_index("ix_guild_history_imports_pending", "guild_history_imports", ["status", "updated_at"], unique=False)
    op.create_table(
        "guild_history_staged_messages",
        sa.Column("export_id", sa.BigInteger(), nullable=False),
        sa.Column("export_domain", sa.String(length=253), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name=op.f("ck_guild_history_staged_messages_payload_is_object")),
        sa.ForeignKeyConstraint(["channel_id", "channel_domain"], ["channels.id", "channels.origin_domain"], name=op.f("fk_guild_history_staged_messages_channel_id_channel_domain_channels"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["export_id", "export_domain"], ["guild_history_imports.export_id", "guild_history_imports.export_domain"], name=op.f("fk_guild_history_staged_messages_export_id_export_domain_guild_history_imports"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("export_id", "export_domain", "message_id", "message_domain", name=op.f("pk_guild_history_staged_messages")),
    )
    op.create_index("ix_guild_history_staged_messages_channel", "guild_history_staged_messages", ["export_id", "export_domain", "channel_id", "message_id"], unique=False)
    op.create_table(
        "federated_history_messages",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(length=253), nullable=False),
        sa.Column("export_id", sa.BigInteger(), nullable=False),
        sa.Column("export_domain", sa.String(length=253), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["export_id", "export_domain"], ["guild_history_imports.export_id", "guild_history_imports.export_domain"], name=op.f("fk_federated_history_messages_export_id_export_domain_guild_history_imports"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["message_id", "message_domain"], ["messages.id", "messages.origin_domain"], name=op.f("fk_federated_history_messages_message_id_message_domain_messages"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id", "message_domain", name=op.f("pk_federated_history_messages")),
    )
    op.create_index("ix_federated_history_messages_export", "federated_history_messages", ["export_id", "export_domain"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_federated_history_messages_export", table_name="federated_history_messages")
    op.drop_table("federated_history_messages")
    op.drop_index("ix_guild_history_staged_messages_channel", table_name="guild_history_staged_messages")
    op.drop_table("guild_history_staged_messages")
    op.drop_index("ix_guild_history_imports_pending", table_name="guild_history_imports")
    op.drop_table("guild_history_imports")
    op.drop_table("guild_history_export_channels")
    op.drop_index("ix_guild_history_exports_active", table_name="guild_history_exports")
    op.drop_table("guild_history_exports")
    op.drop_constraint(op.f("ck_channels_federated_history_policy_value"), "channels", type_="check")
    op.drop_column("channels", "federated_history_policy")
    op.drop_constraint(op.f("ck_guilds_positive_history_policy_generation"), "guilds", type_="check")
    op.drop_constraint(op.f("ck_guilds_federated_history_policy_value"), "guilds", type_="check")
    op.drop_column("guilds", "history_policy_generation")
    op.drop_column("guilds", "federated_history_policy")
    op.drop_constraint(op.f("ck_instances_capabilities_are_array"), "instances", type_="check")
    op.drop_column("instances", "capabilities")
    op.drop_constraint(op.f("ck_users_unresolved_history_handle"), "users", type_="check")
    op.drop_column("users", "profile_resolved")
