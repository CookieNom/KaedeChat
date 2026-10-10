"""Private message bookmarks."""

import sqlalchemy as sa
from alembic import op

revision = "b83d2e7f910a"
down_revision = "9e4a1c3d6f82"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "message_bookmarks",
        sa.Column(
            "saved_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
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
            name="fk_message_bookmarks_local_user",
        ),
        sa.CheckConstraint("user_is_local", name="message_bookmarks_user_is_local"),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
        ),
    )


def downgrade():
    op.drop_table("message_bookmarks")
