"""add per-guild notification settings

Revision ID: 9a4e1c7b2d60
Revises: 7d3e9a1c5f20
Create Date: 2026-08-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4e1c7b2d60"
down_revision: str | None = "7d3e9a1c5f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "guild_notification_settings",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("level", sa.String(length=16), server_default="mentions", nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "level IN ('all','mentions','none')",
            name=op.f("ck_guild_notification_settings_notification_level_value"),
        ),
        sa.CheckConstraint(
            "user_is_local",
            name=op.f("ck_guild_notification_settings_guild_notification_settings_user_is_local"),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name=op.f(
                "fk_guild_notification_settings_guild_id_guild_domain_user_id_user_domain_guild_members"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name="fk_guild_notification_settings_local_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "user_domain",
            "guild_id",
            "guild_domain",
            name=op.f("pk_guild_notification_settings"),
        ),
    )


def downgrade() -> None:
    op.drop_table("guild_notification_settings")
