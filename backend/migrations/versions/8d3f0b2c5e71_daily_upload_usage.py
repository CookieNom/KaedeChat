"""Track daily upload reservations per uploader.

Revision ID: 8d3f0b2c5e71
Revises: 7c2e9a1b4d60
"""

import sqlalchemy as sa
from alembic import op

revision = "8d3f0b2c5e71"
down_revision = "7c2e9a1b4d60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_daily_upload_usage",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(253), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("bytes_used", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "user_domain"),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"], ["users.id", "users.origin_domain"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("bytes_used >= 0", name="nonnegative_usage"),
    )


def downgrade() -> None:
    op.drop_table("user_daily_upload_usage")
