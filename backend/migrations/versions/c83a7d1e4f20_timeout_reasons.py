"""store member timeout reasons

Revision ID: c83a7d1e4f20
Revises: b61f3d8e2a40
Create Date: 2026-08-07 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c83a7d1e4f20"
down_revision: str | None = "b61f3d8e2a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "guild_members",
        sa.Column("timeout_reason", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("guild_members", "timeout_reason")
