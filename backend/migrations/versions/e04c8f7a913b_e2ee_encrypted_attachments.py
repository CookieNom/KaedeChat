"""add ciphertext-only E2EE attachment policy

Revision ID: e04c8f7a913b
Revises: d93b7e5f201a
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e04c8f7a913b"
down_revision: str | None = "d93b7e5f201a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column(
            "encryption_mode",
            sa.String(length=16),
            nullable=False,
            server_default="plaintext",
        ),
    )
    op.add_column(
        "attachments",
        sa.Column("encryption_protocol", sa.String(length=32)),
    )
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN ('pending','clean','infected','failed','encrypted')",
    )
    op.create_check_constraint(
        "attachment_encryption_mode_value",
        "attachments",
        "encryption_mode IN ('plaintext','e2ee')",
    )
    op.create_check_constraint(
        "attachment_encryption_policy_consistent",
        "attachments",
        "(encryption_mode = 'plaintext' AND encryption_protocol IS NULL AND "
        "scan_status <> 'encrypted') OR "
        "(encryption_mode = 'e2ee' AND encryption_protocol = 'kaede-file-v1' AND "
        "scan_status NOT IN ('clean','infected'))",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE attachments SET scan_status = 'failed' "
        "WHERE encryption_mode = 'e2ee' AND scan_status = 'encrypted'"
    )
    op.drop_constraint(
        "attachment_encryption_policy_consistent",
        "attachments",
        type_="check",
    )
    op.drop_constraint(
        "attachment_encryption_mode_value",
        "attachments",
        type_="check",
    )
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN ('pending','clean','infected','failed')",
    )
    op.drop_column("attachments", "encryption_protocol")
    op.drop_column("attachments", "encryption_mode")
