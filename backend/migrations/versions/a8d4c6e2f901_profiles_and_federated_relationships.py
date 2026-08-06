"""add custom profiles and federated relationship correlation

Revision ID: a8d4c6e2f901
Revises: f6d3a9c2b140
Create Date: 2026-08-04 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8d4c6e2f901"
down_revision: str | None = "f6d3a9c2b140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("custom_status", sa.String(128), nullable=True))
    op.add_column("relationships", sa.Column("request_id", sa.String(64), nullable=True))
    op.create_check_constraint(
        op.f("ck_relationships_relationship_request_id_format"),
        "relationships",
        "request_id IS NULL OR request_id ~ '^kcr_[A-Za-z0-9_-]{16,59}$'",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_relationships_relationship_request_id_format"),
        "relationships",
        type_="check",
    )
    op.drop_column("relationships", "request_id")
    op.drop_column("users", "custom_status")
