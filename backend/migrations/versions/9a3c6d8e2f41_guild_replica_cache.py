"""Keep local guild cache and legacy import cursors across eviction."""

import sqlalchemy as sa
from alembic import op

revision = "9a3c6d8e2f41"
down_revision = "8d2e5f7a9c31"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("guilds", sa.Column("replica_evicted_through_id", sa.BigInteger()))
    op.add_column(
        "guild_history_import_channels",
        sa.Column("next_after_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute("""
        UPDATE guild_history_import_channels AS channel SET next_after_id = COALESCE((
            SELECT max(staged.message_id) FROM guild_history_staged_messages AS staged
            WHERE staged.export_id = channel.export_id
              AND staged.export_domain = channel.export_domain
              AND staged.channel_id = channel.channel_id
              AND staged.channel_domain = channel.channel_domain
        ), 0)
    """)


def downgrade() -> None:
    op.drop_column("guild_history_import_channels", "next_after_id")
    op.drop_column("guilds", "replica_evicted_through_id")
