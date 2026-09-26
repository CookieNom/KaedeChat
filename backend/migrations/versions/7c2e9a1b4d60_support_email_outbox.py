"""Allow support email without an account token.

Revision ID: 7c2e9a1b4d60
Revises: c1e7a9d40362
"""

import sqlalchemy as sa
from alembic import op

revision = "7c2e9a1b4d60"
down_revision = "c1e7a9d40362"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("email_outbox", "one_time_token_id", existing_type=sa.String(64), nullable=True)


def downgrade() -> None:
    # A downgrade must not silently discard undelivered support requests.
    op.execute(
        "DELETE FROM email_outbox WHERE one_time_token_id IS NULL "
        "AND status IN ('delivered', 'expired')"
    )
    op.alter_column(
        "email_outbox", "one_time_token_id", existing_type=sa.String(64), nullable=False
    )
