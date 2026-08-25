"""add temporary instance account suspensions

Revision ID: f27a6c9e4b10
Revises: c18f4a7d2e90
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f27a6c9e4b10"
down_revision: str | None = "c18f4a7d2e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("suspended_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_users_suspended_until"),
        "users",
        ["suspended_until"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_users_suspended_until"), table_name="users")
    op.drop_column("users", "suspended_until")
