"""add encrypted mobile push-device registrations

Revision ID: e82f1b6a4c90
Revises: d74e2a9c5b31
Create Date: 2026-08-10 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e82f1b6a4c90"
down_revision: str | None = "d74e2a9c5b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "push_devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("device_name", sa.String(length=100), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "user_is_local", name=op.f("ck_push_devices_push_devices_user_is_local")
        ),
        sa.CheckConstraint(
            "platform IN ('android','ios')", name=op.f("ck_push_devices_platform_value")
        ),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_push_devices_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name=op.f("fk_push_devices_local_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_push_devices")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_push_devices_token_hash")),
    )
    op.create_index(
        "ix_push_devices_user", "push_devices", ["user_id", "user_domain"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_push_devices_user", table_name="push_devices")
    op.drop_table("push_devices")
