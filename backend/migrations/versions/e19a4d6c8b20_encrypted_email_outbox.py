"""add encrypted transactional email outbox

Revision ID: e19a4d6c8b20
Revises: c84e2d7a9f31
Create Date: 2026-07-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e19a4d6c8b20"
down_revision: str | None = "c84e2d7a9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older builds stored a pending email-change target in JSONB.  It cannot be
    # safely re-encrypted in a schema migration without embedding application
    # key handling here, so invalidate those short-lived credentials once and
    # require the user to request a new encrypted confirmation.
    op.execute("DELETE FROM one_time_tokens WHERE purpose = 'email_change'")
    op.create_table(
        "email_outbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("one_time_token_id", sa.String(length=64), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(status = 'processing') = (claimed_at IS NOT NULL AND claim_token IS NOT NULL)",
            name=op.f("ck_email_outbox_claim_state"),
        ),
        sa.CheckConstraint(
            "(status IN ('delivered','expired')) = (completed_at IS NOT NULL)",
            name=op.f("ck_email_outbox_completion_state"),
        ),
        sa.CheckConstraint(
            "octet_length(encrypted_payload) BETWEEN 29 AND 1048576",
            name=op.f("ck_email_outbox_encrypted_payload_length"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_email_outbox_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_email_outbox_positive_lifetime"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','retry','delivered','expired')",
            name=op.f("ck_email_outbox_status_value"),
        ),
        sa.ForeignKeyConstraint(
            ["one_time_token_id"],
            ["one_time_tokens.id"],
            name="fk_email_outbox_one_time_token_id_one_time_tokens",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbox"),
        sa.UniqueConstraint(
            "one_time_token_id",
            name="uq_email_outbox_one_time_token_id",
        ),
    )
    op.create_index(
        "ix_email_outbox_due",
        "email_outbox",
        ["next_attempt_at", "created_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending','retry')"),
    )
    op.create_index(
        "ix_email_outbox_stale_claim",
        "email_outbox",
        ["claimed_at"],
        unique=False,
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_email_outbox_terminal_retention",
        "email_outbox",
        ["completed_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('delivered','expired')"),
    )


def downgrade() -> None:
    op.drop_index("ix_email_outbox_terminal_retention", table_name="email_outbox")
    op.drop_index("ix_email_outbox_stale_claim", table_name="email_outbox")
    op.drop_index("ix_email_outbox_due", table_name="email_outbox")
    op.drop_table("email_outbox")
