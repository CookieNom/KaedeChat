"""bot interaction delivery and instance policy

Revision ID: b84e2f6a19d7
Revises: 94397280832f
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b84e2f6a19d7"
down_revision: str | None = "94397280832f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bot_installations", sa.Column("role_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "bot_installations",
        sa.Column("role_domain", sa.String(length=253), nullable=True),
    )
    op.create_check_constraint(
        "bot_installation_role_ref_complete",
        "bot_installations",
        "(role_id IS NULL) = (role_domain IS NULL)",
    )
    op.create_foreign_key(
        op.f("fk_bot_installations_role_id_role_domain_roles"),
        "bot_installations",
        "roles",
        ["role_id", "role_domain"],
        ["id", "origin_domain"],
        ondelete="SET NULL",
    )
    op.create_table(
        "bot_instance_rules",
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(length=253), nullable=False),
        sa.Column("target_domain", sa.String(length=253), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "effect IN ('allow','deny')",
            name=op.f("ck_bot_instance_rules_bot_instance_rule_effect_value"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_instance_rules_application_id_application_domain_bot_applications"),
        ),
        sa.PrimaryKeyConstraint(
            "application_id",
            "application_domain",
            "target_domain",
            name=op.f("pk_bot_instance_rules"),
        ),
    )
    op.create_table(
        "bot_interactions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(length=253), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("command_name", sa.String(length=32), nullable=False),
        sa.Column(
            "command_type", sa.String(length=16), server_default="chat_input", nullable=False
        ),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("encrypted_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_message_id", sa.BigInteger(), nullable=True),
        sa.Column("response_message_domain", sa.String(length=253), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "command_type IN ('chat_input','user','message')",
            name=op.f("ck_bot_interactions_bot_interaction_command_type_value"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','deferred','responded','expired','failed')",
            name=op.f("ck_bot_interactions_bot_interaction_status_value"),
        ),
        sa.CheckConstraint(
            "encrypted_payload IS NULL OR payload = '{}'::jsonb",
            name=op.f("ck_bot_interactions_bot_interaction_payload_mode"),
        ),
        sa.CheckConstraint(
            "(response_message_id IS NULL) = (response_message_domain IS NULL)",
            name=op.f("ck_bot_interactions_bot_interaction_response_ref_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["bot_installations.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interactions_installation_id_bot_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interactions_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interactions_channel_id_channel_domain_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_interactions_user_id_user_domain_users"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_interactions")),
    )
    op.create_index(
        "ix_bot_interactions_application_status",
        "bot_interactions",
        ["application_id", "application_domain", "status", "id"],
        unique=False,
    )
    op.create_index(
        "ix_bot_interactions_expiry", "bot_interactions", ["expires_at", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_bot_interactions_expiry", table_name="bot_interactions")
    op.drop_index("ix_bot_interactions_application_status", table_name="bot_interactions")
    op.drop_table("bot_interactions")
    op.drop_table("bot_instance_rules")
    op.drop_constraint(
        op.f("fk_bot_installations_role_id_role_domain_roles"),
        "bot_installations",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("ck_bot_installations_bot_installation_role_ref_complete"),
        "bot_installations",
        type_="check",
    )
    op.drop_column("bot_installations", "role_domain")
    op.drop_column("bot_installations", "role_id")
