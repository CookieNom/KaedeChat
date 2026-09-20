"""Require mutual consent before encrypting a direct message."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b8d1e6f20394"
down_revision = "a6e2d4f80931"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dm_conversations", sa.Column("e2ee_consent", postgresql.JSONB(), nullable=True))
    op.add_column(
        "e2ee_room_operations", sa.Column("consent_request_id", sa.String(43), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("e2ee_room_operations", "consent_request_id")
    op.drop_column("dm_conversations", "e2ee_consent")
