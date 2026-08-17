"""Allow explicitly disclosed E2EE report evidence.

Revision ID: ab71e2c9d430
Revises: f03b6d9e2a71
"""

from __future__ import annotations

from alembic import op

revision: str = "ab71e2c9d430"
down_revision: str | None = "f03b6d9e2a71"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_constraint(
        "abuse_report_encryption_mode_value",
        "abuse_reports",
        type_="check",
    )
    op.create_check_constraint(
        "abuse_report_encryption_mode_value",
        "abuse_reports",
        "encryption_mode IN ('plaintext','e2ee_metadata','e2ee_user_disclosed')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE abuse_reports SET encryption_mode = 'e2ee_metadata' "
        "WHERE encryption_mode = 'e2ee_user_disclosed'"
    )
    op.drop_constraint(
        "abuse_report_encryption_mode_value",
        "abuse_reports",
        type_="check",
    )
    op.create_check_constraint(
        "abuse_report_encryption_mode_value",
        "abuse_reports",
        "encryption_mode IN ('plaintext','e2ee_metadata')",
    )
