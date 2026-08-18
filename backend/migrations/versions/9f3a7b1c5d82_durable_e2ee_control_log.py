"""Retain immutable MLS control records outside deletable message history.

Revision ID: 9f3a7b1c5d82
Revises: 8d2f6a4c91b7
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9f3a7b1c5d82"
down_revision: str | None = "8d2f6a4c91b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "e2ee_control_records",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("author_id", sa.BigInteger(), nullable=False),
        sa.Column("author_domain", sa.String(length=253), nullable=False),
        sa.Column("policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("id >= 0", name="nonnegative_id"),
        sa.CheckConstraint(
            "policy_generation > 0",
            name="positive_policy_generation",
        ),
        sa.CheckConstraint("epoch >= 0", name="nonnegative_epoch"),
        sa.CheckConstraint(
            "operation IN ('welcome','commit')",
            name="operation_value",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(envelope) = 'object' AND envelope->>'operation' = operation",
            name="envelope_matches_operation",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain"),
    )
    op.create_index(
        "ix_e2ee_control_records_channel_order",
        "e2ee_control_records",
        ["channel_id", "channel_domain", "id", "origin_domain"],
    )
    op.execute(
        """
        INSERT INTO e2ee_control_records (
            id, origin_domain, channel_id, channel_domain,
            author_id, author_domain, policy_generation, epoch,
            operation, envelope, created_at
        )
        SELECT
            id, origin_domain, channel_id, channel_domain,
            author_id, author_domain, encryption_policy_generation,
            encryption_epoch, e2ee->>'operation', e2ee, created_at
        FROM messages
        WHERE e2ee IS NOT NULL
          AND e2ee->>'operation' IN ('welcome', 'commit')
          AND encryption_epoch IS NOT NULL
        ON CONFLICT (id, origin_domain) DO NOTHING
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_capture_e2ee_control_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.e2ee IS NOT NULL
             AND NEW.e2ee->>'operation' IN ('welcome', 'commit')
             AND NEW.encryption_epoch IS NOT NULL THEN
            INSERT INTO e2ee_control_records (
              id, origin_domain, channel_id, channel_domain,
              author_id, author_domain, policy_generation, epoch,
              operation, envelope, created_at
            ) VALUES (
              NEW.id, NEW.origin_domain, NEW.channel_id, NEW.channel_domain,
              NEW.author_id, NEW.author_domain, NEW.encryption_policy_generation,
              NEW.encryption_epoch, NEW.e2ee->>'operation', NEW.e2ee, NEW.created_at
            ) ON CONFLICT (id, origin_domain) DO NOTHING;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER capture_e2ee_control_record
        AFTER INSERT OR UPDATE OF e2ee ON messages
        FOR EACH ROW EXECUTE FUNCTION kaede_capture_e2ee_control_record()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS capture_e2ee_control_record ON messages")
    op.execute("DROP FUNCTION IF EXISTS kaede_capture_e2ee_control_record()")
    op.drop_index(
        "ix_e2ee_control_records_channel_order",
        table_name="e2ee_control_records",
    )
    op.drop_table("e2ee_control_records")
