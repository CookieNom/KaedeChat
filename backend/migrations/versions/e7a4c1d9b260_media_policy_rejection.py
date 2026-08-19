"""Add terminal media policy rejection state.

Revision ID: e7a4c1d9b260
Revises: d4b6e8f1a203
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e7a4c1d9b260"
down_revision: str | None = "d4b6e8f1a203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN "
        "('pending','clean','infected','failed','encrypted','quarantined','rejected')",
    )


def downgrade() -> None:
    op.execute("UPDATE attachments SET scan_status = 'infected' WHERE scan_status = 'rejected'")
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN ('pending','clean','infected','failed','encrypted','quarantined')",
    )
