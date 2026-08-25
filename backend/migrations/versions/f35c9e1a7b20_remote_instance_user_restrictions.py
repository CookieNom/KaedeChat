"""add instance-owned restrictions for remote users

Revision ID: f35c9e1a7b20
Revises: f31d8a2c6e40
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f35c9e1a7b20"
down_revision: str | None = "f31d8a2c6e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instance_user_restrictions",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("restriction_type", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_domain", sa.String(length=253), nullable=False),
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
            "(restriction_type = 'banned' AND expires_at IS NULL) OR "
            "(restriction_type = 'suspended' AND expires_at IS NOT NULL)",
            name=op.f("ck_instance_user_restrictions_type_expiry_consistent"),
        ),
        sa.ForeignKeyConstraint(
            ["actor_id", "actor_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_instance_user_restrictions_actor_id_actor_domain_users"),
        ),
        sa.PrimaryKeyConstraint(
            "user_id",
            "user_domain",
            name=op.f("pk_instance_user_restrictions"),
        ),
    )
    op.create_index(
        op.f("ix_instance_user_restrictions_expires_at"),
        "instance_user_restrictions",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_instance_user_restrictions_expires_at"),
        table_name="instance_user_restrictions",
    )
    op.drop_table("instance_user_restrictions")
