"""Fence E2EE identity recovery with a one-time session authorization.

Revision ID: d4b6e8f1a203
Revises: c8a4e1d7f290
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b6e8f1a203"
down_revision: str | None = "c8a4e1d7f290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("e2ee_recovery_token_hash", sa.LargeBinary(length=32), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("e2ee_recovery_session_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("e2ee_recovery_generation", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("e2ee_recovery_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "e2ee_recovery_token_hash_length",
        "users",
        "e2ee_recovery_token_hash IS NULL OR octet_length(e2ee_recovery_token_hash) = 32",
    )
    op.create_check_constraint(
        "e2ee_recovery_generation_positive",
        "users",
        "e2ee_recovery_generation IS NULL OR e2ee_recovery_generation > 0",
    )
    op.create_check_constraint(
        "e2ee_recovery_authorization_complete",
        "users",
        "(e2ee_recovery_token_hash IS NULL AND "
        "e2ee_recovery_session_id IS NULL AND "
        "e2ee_recovery_generation IS NULL AND "
        "e2ee_recovery_expires_at IS NULL) OR "
        "(e2ee_recovery_token_hash IS NOT NULL AND "
        "e2ee_recovery_session_id IS NOT NULL AND "
        "e2ee_recovery_generation IS NOT NULL AND "
        "e2ee_recovery_expires_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "e2ee_recovery_authorization_complete",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "e2ee_recovery_generation_positive",
        "users",
        type_="check",
    )
    op.drop_constraint(
        "e2ee_recovery_token_hash_length",
        "users",
        type_="check",
    )
    op.drop_column("users", "e2ee_recovery_expires_at")
    op.drop_column("users", "e2ee_recovery_generation")
    op.drop_column("users", "e2ee_recovery_session_id")
    op.drop_column("users", "e2ee_recovery_token_hash")
