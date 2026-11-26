"""Instance vendor configuration and account shopping link consent."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "ef3c19bd8560"
down_revision = "d2b67ce43981"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("shopping_links", sa.Boolean()))
    op.create_table(
        "vendor_configurations",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("package", sa.String(48), primary_key=True),
        sa.Column("records", JSONB(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("vendor_configurations")
    op.drop_column("user_settings", "shopping_links")
