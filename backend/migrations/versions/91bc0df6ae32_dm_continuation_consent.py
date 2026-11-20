"""Record unanimous consent to continue a DM with server-readable messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "91bc0df6ae32"
down_revision = "ac4e7f9b2d60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dm_conversations", sa.Column("plaintext_continuation_request", postgresql.JSONB())
    )


def downgrade() -> None:
    op.drop_column("dm_conversations", "plaintext_continuation_request")
