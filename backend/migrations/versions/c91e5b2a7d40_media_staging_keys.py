"""track client-writable media staging objects

Revision ID: c91e5b2a7d40
Revises: b46d9a1f2c73
Create Date: 2026-07-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c91e5b2a7d40"
down_revision: str | None = "b46d9a1f2c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("staging_object_key", sa.String(length=512), nullable=True),
    )
    # Only unfinished rows can still have a live browser PUT credential during
    # an online upgrade. Existing clean rows predate immutable promotion and
    # should be deployed after quiescing for one upload-ticket lifetime.
    op.execute(
        """
        UPDATE attachments
        SET staging_object_key = object_key
        WHERE scan_status IN ('pending', 'failed') AND deleted_at IS NULL
        """
    )
    op.create_index(
        "ix_attachments_staging_gc",
        "attachments",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("staging_object_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_staging_gc", table_name="attachments")
    op.drop_column("attachments", "staging_object_key")
