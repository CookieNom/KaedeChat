"""index live attachments by message

Revision ID: d20f4a8c6e31
Revises: c91e5b2a7d40
Create Date: 2026-07-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d20f4a8c6e31"
down_revision: str | None = "c91e5b2a7d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_attachments_live_message",
        "attachments",
        ["message_id", "message_domain", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL AND message_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_live_message", table_name="attachments")
