"""Bind mobile push registrations to the session that enrolled them."""

import sqlalchemy as sa
from alembic import op

revision = "c89fa1e42bd7"
down_revision = "b521afdc9073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing installations rebind during their next authenticated registration.
    op.add_column("push_devices", sa.Column("session_id", sa.String(64)))


def downgrade() -> None:
    op.drop_column("push_devices", "session_id")
