"""Allow users to withhold their language from bot interactions."""

from alembic import op
import sqlalchemy as sa

revision = "b38f7e0c5d92"
down_revision = "a27e6d9b4c81"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("share_locale_with_bots", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "share_locale_with_bots")
