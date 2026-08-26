"""add durable tracker gateway dispatch outbox

Revision ID: f92a6c1d4b70
Revises: e84c2d7f91a3
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f92a6c1d4b70"
down_revision: str | None = "e84c2d7f91a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tracker_dispatch_outbox",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_tracker_dispatch_outbox_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "event_type IN ('TRACKER_BOARD_UPDATE','TRACKER_LANE_CREATE',"
            "'TRACKER_LANE_UPDATE','TRACKER_LANE_DELETE','TRACKER_TASK_CREATE',"
            "'TRACKER_TASK_UPDATE','TRACKER_TASK_DELETE')",
            name=op.f("ck_tracker_dispatch_outbox_event_type_value"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_dispatch_outbox_board_ref_guild",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracker_dispatch_outbox")),
    )
    op.create_index(
        "ix_tracker_dispatch_outbox_due",
        "tracker_dispatch_outbox",
        ["next_attempt_at", "id"],
    )


def downgrade() -> None:
    # A pending row represents a committed mutation that has not yet entered
    # the resumable gateway stream. Dropping it would violate the documented
    # at-least-once contract, so operators must restore delivery and drain it.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM tracker_dispatch_outbox) THEN
            RAISE EXCEPTION 'drain tracker dispatch outbox before downgrading f92a6c1d4b70';
          END IF;
        END $$
        """
    )
    op.drop_index("ix_tracker_dispatch_outbox_due", table_name="tracker_dispatch_outbox")
    op.drop_table("tracker_dispatch_outbox")
