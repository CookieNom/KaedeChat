"""add durable guild moderation sanctions

Revision ID: b61f3d8e2a40
Revises: 9a4e1c7b2d60
Create Date: 2026-08-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b61f3d8e2a40"
down_revision: str | None = "9a4e1c7b2d60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "guild_members",
        sa.Column(
            "timeout_indefinite", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("bans", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        op.f("ck_bans_expiry_after_creation"),
        "bans",
        "expires_at IS NULL OR expires_at > created_at",
    )
    op.create_index(op.f("ix_bans_expiry"), "bans", ["expires_at"], unique=False)
    op.create_table(
        "guild_instance_bans",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("instance_domain", sa.String(length=253), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_domain", sa.String(length=253), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at",
            name=op.f("ck_guild_instance_bans_expiry_after_creation"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "actor_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_guild_instance_bans_actor_id_actor_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            name=op.f("fk_guild_instance_bans_guild_id_guild_domain_guilds"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instance_domain"],
            ["instances.domain"],
            name=op.f("fk_guild_instance_bans_instance_domain_instances"),
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "guild_domain",
            "instance_domain",
            name=op.f("pk_guild_instance_bans"),
        ),
    )
    op.create_index(
        op.f("ix_guild_instance_bans_expiry"),
        "guild_instance_bans",
        ["expires_at"],
        unique=False,
    )
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        "(permissions & ~3302829321471) = 0",
    )
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        "((allow | deny) & ~3302829321471) = 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        "((allow | deny) & ~1103806065919) = 0",
    )
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        "(permissions & ~1103806065919) = 0",
    )
    op.drop_index(
        op.f("ix_guild_instance_bans_expiry"), table_name="guild_instance_bans"
    )
    op.drop_table("guild_instance_bans")
    op.drop_index(op.f("ix_bans_expiry"), table_name="bans")
    op.drop_constraint(op.f("ck_bans_expiry_after_creation"), "bans", type_="check")
    op.drop_column("bans", "expires_at")
    op.drop_column("guild_members", "timeout_indefinite")
