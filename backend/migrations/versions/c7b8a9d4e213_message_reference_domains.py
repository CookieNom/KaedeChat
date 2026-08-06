"""Make persisted message cursors domain-qualified.

Revision ID: c7b8a9d4e213
Revises: a1f04f6ac911
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7b8a9d4e213"
down_revision: str | None = "a1f04f6ac911"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("last_message_domain", sa.String(253), nullable=True))
    op.add_column(
        "read_states", sa.Column("last_message_domain", sa.String(253), nullable=True)
    )

    # Older code rarely persisted Channel.last_message_id, but when it did the
    # channel origin is the only authoritative domain available for guild
    # channels. For DMs, choose the matching message origin deterministically.
    op.execute(
        """
        UPDATE channels AS channel
        SET last_message_domain = (
            SELECT message.origin_domain
            FROM messages AS message
            WHERE message.id = channel.last_message_id
              AND message.channel_id = channel.id
              AND message.channel_domain = channel.origin_domain
            ORDER BY message.origin_domain
            LIMIT 1
        )
        WHERE channel.last_message_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE read_states AS state
        SET last_message_domain = (
            SELECT message.origin_domain
            FROM messages AS message
            WHERE message.id = state.last_message_id
              AND message.channel_id = state.channel_id
              AND message.channel_domain = state.channel_domain
            ORDER BY message.origin_domain
            LIMIT 1
        )
        WHERE state.last_message_id IS NOT NULL
        """
    )
    # Orphaned legacy numeric cursors cannot be resolved safely; clearing them
    # is preferable to binding them to the wrong federated message.
    op.execute(
        "UPDATE channels SET last_message_id = NULL "
        "WHERE last_message_id IS NOT NULL AND last_message_domain IS NULL"
    )
    op.execute(
        "UPDATE read_states SET last_message_id = NULL "
        "WHERE last_message_id IS NOT NULL AND last_message_domain IS NULL"
    )
    op.create_check_constraint(
        op.f("ck_channels_last_message_ref_complete"),
        "channels",
        "(last_message_id IS NULL) = (last_message_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_read_states_last_message_ref_complete"),
        "read_states",
        "(last_message_id IS NULL) = (last_message_domain IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_read_states_last_message_ref_complete"), "read_states", type_="check"
    )
    op.drop_constraint(
        op.f("ck_channels_last_message_ref_complete"), "channels", type_="check"
    )
    op.drop_column("read_states", "last_message_domain")
    op.drop_column("channels", "last_message_domain")
