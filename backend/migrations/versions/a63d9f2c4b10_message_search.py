"""Add E2EE-aware durable message search projection.

Revision ID: a63d9f2c4b10
Revises: c31f6a8e2d94
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a63d9f2c4b10"
down_revision: str | None = "c31f6a8e2d94"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column(
            "encryption_mode", sa.String(length=16), nullable=False, server_default="plaintext"
        ),
    )
    op.create_check_constraint(
        "channel_encryption_mode_value",
        "channels",
        "encryption_mode IN ('plaintext','e2ee')",
    )
    op.create_table(
        "search_index_outbox",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(length=253), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("attempts >= 0", name="search_index_outbox_attempts_nonnegative"),
        sa.PrimaryKeyConstraint("message_id", "message_domain"),
    )
    op.create_index(
        "ix_search_index_outbox_due",
        "search_index_outbox",
        ["next_attempt_at", "updated_at"],
    )
    op.create_table(
        "search_index_state",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reset_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("backfill_after_id", sa.BigInteger(), nullable=True),
        sa.Column("backfill_after_domain", sa.String(length=253), nullable=True),
        sa.Column("backfill_completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("id = 1", name="search_index_state_singleton"),
        sa.CheckConstraint(
            "(backfill_after_id IS NULL) = (backfill_after_domain IS NULL)",
            name="search_index_state_cursor_complete",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO search_index_state (id) VALUES (1)")
    op.execute(
        """
        CREATE FUNCTION kaede_enqueue_message_search(p_id bigint, p_domain varchar)
        RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM search_index_state WHERE id = 1 AND enabled) THEN
            INSERT INTO search_index_outbox
              (message_id, message_domain, attempts, next_attempt_at, locked_at,
               last_error_code, updated_at)
            VALUES (p_id, p_domain, 0, now(), NULL, NULL, now())
            ON CONFLICT (message_id, message_domain) DO UPDATE SET
              attempts = 0, next_attempt_at = now(), locked_at = NULL,
              last_error_code = NULL, updated_at = now();
          END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_search_message_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM kaede_enqueue_message_search(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END,
            CASE WHEN TG_OP = 'DELETE' THEN OLD.origin_domain ELSE NEW.origin_domain END
          );
          RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_search_attachment_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP <> 'INSERT' AND OLD.message_id IS NOT NULL THEN
            PERFORM kaede_enqueue_message_search(OLD.message_id, OLD.message_domain);
          END IF;
          IF TG_OP <> 'DELETE' AND NEW.message_id IS NOT NULL THEN
            PERFORM kaede_enqueue_message_search(NEW.message_id, NEW.message_domain);
          END IF;
          RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_search_pin_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          PERFORM kaede_enqueue_message_search(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.message_id ELSE NEW.message_id END,
            CASE WHEN TG_OP = 'DELETE' THEN OLD.message_domain ELSE NEW.message_domain END
          );
          RETURN COALESCE(NEW, OLD);
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_search_channel_encryption_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.encryption_mode IS DISTINCT FROM NEW.encryption_mode THEN
            UPDATE search_index_state SET
              backfill_after_id = NULL,
              backfill_after_domain = NULL,
              backfill_completed = false,
              reset_required = reset_required OR NEW.encryption_mode = 'e2ee',
              updated_at = now()
            WHERE id = 1 AND enabled;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_messages_search AFTER INSERT OR UPDATE OR DELETE ON messages "
        "FOR EACH ROW EXECUTE FUNCTION kaede_search_message_changed()"
    )
    op.execute(
        "CREATE TRIGGER trg_attachments_search AFTER INSERT OR UPDATE OR DELETE ON attachments "
        "FOR EACH ROW EXECUTE FUNCTION kaede_search_attachment_changed()"
    )
    op.execute(
        "CREATE TRIGGER trg_pins_search AFTER INSERT OR UPDATE OR DELETE ON pins "
        "FOR EACH ROW EXECUTE FUNCTION kaede_search_pin_changed()"
    )
    op.execute(
        "CREATE TRIGGER trg_channels_search_encryption AFTER UPDATE OF encryption_mode ON channels "
        "FOR EACH ROW EXECUTE FUNCTION kaede_search_channel_encryption_changed()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_channels_search_encryption ON channels")
    op.execute("DROP TRIGGER IF EXISTS trg_pins_search ON pins")
    op.execute("DROP TRIGGER IF EXISTS trg_attachments_search ON attachments")
    op.execute("DROP TRIGGER IF EXISTS trg_messages_search ON messages")
    op.execute("DROP FUNCTION IF EXISTS kaede_search_pin_changed()")
    op.execute("DROP FUNCTION IF EXISTS kaede_search_channel_encryption_changed()")
    op.execute("DROP FUNCTION IF EXISTS kaede_search_attachment_changed()")
    op.execute("DROP FUNCTION IF EXISTS kaede_search_message_changed()")
    op.execute("DROP FUNCTION IF EXISTS kaede_enqueue_message_search(bigint, varchar)")
    op.drop_index("ix_search_index_outbox_due", table_name="search_index_outbox")
    op.drop_table("search_index_state")
    op.drop_table("search_index_outbox")
    op.drop_constraint("channel_encryption_mode_value", "channels", type_="check")
    op.drop_column("channels", "encryption_mode")
