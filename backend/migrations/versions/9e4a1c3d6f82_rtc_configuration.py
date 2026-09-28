"""Optional RTC configuration, sticky room placement, and webhook inbox.

Revision ID: 9e4a1c3d6f82
Revises: 8d3f0b2c5e71
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "9e4a1c3d6f82"
down_revision = "8d3f0b2c5e71"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "rtc_configurations",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("configuration", postgresql.JSONB(), nullable=False),
        sa.Column("credentials", sa.LargeBinary()),
    )
    op.create_table(
        "rtc_room_placements",
        sa.Column("finished", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("room", sa.String(100), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("control_url", sa.String(2048), nullable=False),
        sa.Column("connection_url", sa.String(2048), nullable=False),
        sa.Column("credentials", sa.LargeBinary(), nullable=False),
    )
    op.create_table(
        "rtc_webhook_events",
        sa.Column("domain", sa.String(253), primary_key=True),
        sa.Column("event_id", sa.String(128), primary_key=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "retry_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_index(
        "ix_rtc_webhook_pending",
        "rtc_webhook_events",
        ["domain", "retry_at", "accepted_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade():
    op.drop_table("rtc_webhook_events")
    op.drop_table("rtc_room_placements")
    op.drop_table("rtc_configurations")
