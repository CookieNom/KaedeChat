"""Server rules, onboarding, and private member choices."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f71d9a3b2c80"
down_revision = "c49a8f1d6e03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "guilds", sa.Column("onboarding", postgresql.JSONB(), nullable=False, server_default="{}")
    )
    op.add_column(
        "guild_members",
        sa.Column("onboarding_state", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade():
    op.drop_column("guild_members", "onboarding_state")
    op.drop_column("guilds", "onboarding")
