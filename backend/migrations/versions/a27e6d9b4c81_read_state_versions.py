"""Version manual read-position changes to reject stale acknowledgements."""

import sqlalchemy as sa
from alembic import op

revision = "a27e6d9b4c81"
down_revision = "9e7a1c4b8d20"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "read_states", sa.Column("read_version", sa.Integer(), server_default="0", nullable=False)
    )

    op.create_table(
        "inbox_dismissals",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(253), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "user_domain", "message_id", "message_domain"),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
            name="fk_inbox_dismissals_local_user",
        ),
        sa.CheckConstraint("user_is_local", name="inbox_dismissals_user_is_local"),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("inbox_dismissals")
    op.drop_column("read_states", "read_version")
