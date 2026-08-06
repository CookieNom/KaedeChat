"""Persist stable federation inbox rejection results.

Revision ID: a62d77c48b10
Revises: f50c2d8a1e77
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a62d77c48b10"
down_revision: str | None = "f50c2d8a1e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "federation_inbox",
        sa.Column("result_code", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("federation_inbox", "result_code")
