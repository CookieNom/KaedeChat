"""add durable message projections

Revision ID: a35c8d2e7f41
Revises: f24b7e9a3c10
Create Date: 2026-07-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a35c8d2e7f41"
down_revision: str | None = "f24b7e9a3c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_projections",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(253), nullable=False),
        sa.Column(
            "mention_user_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(mention_user_refs) = 'array'",
            name=op.f("ck_message_projections_mentions_are_array"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            name=op.f("fk_message_projections_channel_id_channel_domain_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain", "channel_id", "channel_domain"],
            [
                "messages.id",
                "messages.origin_domain",
                "messages.channel_id",
                "messages.channel_domain",
            ],
            name="fk_message_projections_message_ref_channel",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "message_id", "message_domain", name=op.f("pk_message_projections")
        ),
    )
    op.create_index(
        "ix_message_projections_pending",
        "message_projections",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_message_projections_pending", table_name="message_projections")
    op.drop_table("message_projections")
