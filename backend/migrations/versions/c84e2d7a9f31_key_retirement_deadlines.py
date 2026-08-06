"""add signing-key retirement deadlines

Revision ID: c84e2d7a9f31
Revises: b73f1c9d4a20
Create Date: 2026-07-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84e2d7a9f31"
down_revision: str | None = "b73f1c9d4a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "peer_keys",
        sa.Column("retire_after", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_peer_keys_retirement_after_fetch"),
        "peer_keys",
        "retire_after IS NULL OR retire_after >= fetched_at",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_peer_keys_retirement_after_fetch"),
        "peer_keys",
        type_="check",
    )
    op.drop_column("peer_keys", "retire_after")
