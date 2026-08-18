"""Add per-installation bot media accounting.

Revision ID: 8d2f6a4c91b7
Revises: 7c4e91a8d2f6
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d2f6a4c91b7"
down_revision: str | None = "7c4e91a8d2f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bot_installations",
        sa.Column("media_bytes_used", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "bot_installations",
        sa.Column("media_pending_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.drop_constraint("bot_installation_positive_values", "bot_installations", type_="check")
    op.create_check_constraint(
        "bot_installation_positive_values",
        "bot_installations",
        "granted_permissions >= 0 AND grant_revision >= 1 "
        "AND media_bytes_used >= 0 AND media_pending_bytes >= 0",
    )
    op.add_column(
        "attachments",
        sa.Column("bot_installation_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_attachments_bot_installation_id_bot_installations",
        "attachments",
        "bot_installations",
        ["bot_installation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_attachments_bot_installation_usage",
        "attachments",
        ["bot_installation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_attachments_bot_installation_usage", table_name="attachments")
    op.drop_constraint(
        "fk_attachments_bot_installation_id_bot_installations",
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "bot_installation_id")
    op.drop_constraint("bot_installation_positive_values", "bot_installations", type_="check")
    op.create_check_constraint(
        "bot_installation_positive_values",
        "bot_installations",
        "granted_permissions >= 0 AND grant_revision >= 1",
    )
    op.drop_column("bot_installations", "media_pending_bytes")
    op.drop_column("bot_installations", "media_bytes_used")
