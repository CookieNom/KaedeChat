"""bind disclosed attachment evidence to abuse reports

Revision ID: f31d8a2c6e40
Revises: f27a6c9e4b10
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f31d8a2c6e40"
down_revision: str | None = "f27a6c9e4b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("report_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_attachments_report_id_abuse_reports"),
        "attachments",
        "abuse_reports",
        ["report_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        op.f("ix_attachments_report_id"),
        "attachments",
        ["report_id"],
        unique=False,
    )
    op.create_check_constraint(
        op.f("ck_attachments_report_evidence_is_unbound_plaintext"),
        "attachments",
        "report_id IS NULL OR "
        "(message_id IS NULL AND message_domain IS NULL AND encryption_mode = 'plaintext')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_attachments_report_evidence_is_unbound_plaintext"),
        "attachments",
        type_="check",
    )
    op.drop_index(op.f("ix_attachments_report_id"), table_name="attachments")
    op.drop_constraint(
        op.f("fk_attachments_report_id_abuse_reports"),
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "report_id")
