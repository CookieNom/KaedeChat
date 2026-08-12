"""preserve explicit departures from remote guilds

Revision ID: e95c2d8b4f31
Revises: d84e1b7a3c20
Create Date: 2026-08-12 03:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e95c2d8b4f31"
down_revision: str | None = "d84e1b7a3c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_guild_membership_intents",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="departed", nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "guild_domain <> user_domain",
            name=op.f("ck_remote_guild_membership_intents_guild_is_remote_from_local_user"),
        ),
        sa.CheckConstraint(
            "guild_id >= 0",
            name=op.f("ck_remote_guild_membership_intents_nonnegative_guild_id"),
        ),
        sa.CheckConstraint(
            "state IN ('departed','joining')",
            name=op.f("ck_remote_guild_membership_intents_state_value"),
        ),
        sa.CheckConstraint(
            "user_is_local",
            name=op.f(
                "ck_remote_guild_membership_intents_remote_guild_membership_intents_user_is_local"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name=op.f("fk_remote_guild_membership_intents_local_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "guild_domain",
            "user_id",
            "user_domain",
            name=op.f("pk_remote_guild_membership_intents"),
        ),
    )
    op.create_index(
        op.f("ix_remote_guild_membership_intents_user"),
        "remote_guild_membership_intents",
        ["user_id", "user_domain"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_remote_guild_membership_intents_user"),
        table_name="remote_guild_membership_intents",
    )
    op.drop_table("remote_guild_membership_intents")
