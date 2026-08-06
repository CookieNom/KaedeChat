"""Preserve inaccessible replicated channels as lifecycle tombstones.

Revision ID: f50c2d8a1e77
Revises: e34b1a7c9d02
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f50c2d8a1e77"
down_revision: str | None = "e34b1a7c9d02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column(
            "unavailable",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("channels", "unavailable")
