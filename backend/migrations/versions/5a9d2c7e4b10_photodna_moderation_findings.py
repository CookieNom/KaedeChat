"""Add PhotoDNA quarantine findings to the instance report queue.

Revision ID: 5a9d2c7e4b10
Revises: e04c8f7a913b
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5a9d2c7e4b10"
down_revision: str | None = "e04c8f7a913b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "abuse_reports",
        sa.Column("source", sa.String(length=24), nullable=False, server_default="user"),
    )
    op.alter_column("abuse_reports", "reporter_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column(
        "abuse_reports", "reporter_domain", existing_type=sa.String(length=253), nullable=True
    )
    op.alter_column(
        "abuse_reports",
        "reporter_is_local",
        existing_type=sa.Boolean(),
        existing_server_default=sa.text("true"),
        server_default=None,
        nullable=True,
    )
    op.drop_constraint("abuse_report_reporter_is_local", "abuse_reports", type_="check")
    op.create_check_constraint(
        "abuse_report_source_value",
        "abuse_reports",
        "source IN ('user','photodna')",
    )
    op.create_check_constraint(
        "abuse_report_source_reporter_policy",
        "abuse_reports",
        "(source = 'user' AND reporter_id IS NOT NULL AND reporter_domain IS NOT NULL "
        "AND reporter_is_local) OR (source = 'photodna' AND reporter_id IS NULL "
        "AND reporter_domain IS NULL AND reporter_is_local IS NULL "
        "AND target_type = 'attachment' AND category = 'illegal_content' "
        "AND encryption_mode = 'plaintext')",
    )
    op.drop_constraint("abuse_report_target_type_value", "abuse_reports", type_="check")
    op.create_check_constraint(
        "abuse_report_target_type_value",
        "abuse_reports",
        "target_type IN "
        "('message','user','bot','application','guild','instance','invite','attachment')",
    )

    op.drop_constraint("attachment_encryption_policy_consistent", "attachments", type_="check")
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN ('pending','clean','infected','failed','encrypted','quarantined')",
    )
    op.create_check_constraint(
        "attachment_encryption_policy_consistent",
        "attachments",
        "(encryption_mode = 'plaintext' AND encryption_protocol IS NULL AND "
        "scan_status <> 'encrypted') OR "
        "(encryption_mode = 'e2ee' AND encryption_protocol = 'kaede-file-v1' AND "
        "scan_status IN ('pending','failed','encrypted'))",
    )
    op.create_index(
        "uq_abuse_reports_photodna_target",
        "abuse_reports",
        ["source", "target_type", "target_ref"],
        unique=True,
        postgresql_where=sa.text("source = 'photodna'"),
    )


def downgrade() -> None:
    op.drop_index("uq_abuse_reports_photodna_target", table_name="abuse_reports")
    op.execute("DELETE FROM abuse_reports WHERE source = 'photodna'")
    op.execute("UPDATE attachments SET scan_status = 'infected' WHERE scan_status = 'quarantined'")
    op.drop_constraint("attachment_encryption_policy_consistent", "attachments", type_="check")
    op.drop_constraint("scan_status", "attachments", type_="check")
    op.create_check_constraint(
        "scan_status",
        "attachments",
        "scan_status IN ('pending','clean','infected','failed','encrypted')",
    )
    op.create_check_constraint(
        "attachment_encryption_policy_consistent",
        "attachments",
        "(encryption_mode = 'plaintext' AND encryption_protocol IS NULL AND "
        "scan_status <> 'encrypted') OR "
        "(encryption_mode = 'e2ee' AND encryption_protocol = 'kaede-file-v1' AND "
        "scan_status NOT IN ('clean','infected'))",
    )

    op.drop_constraint("abuse_report_target_type_value", "abuse_reports", type_="check")
    op.create_check_constraint(
        "abuse_report_target_type_value",
        "abuse_reports",
        "target_type IN ('message','user','bot','application','guild','instance','invite')",
    )
    op.drop_constraint("abuse_report_source_reporter_policy", "abuse_reports", type_="check")
    op.drop_constraint("abuse_report_source_value", "abuse_reports", type_="check")
    op.alter_column(
        "abuse_reports",
        "reporter_is_local",
        existing_type=sa.Boolean(),
        existing_server_default=None,
        server_default=sa.text("true"),
        nullable=False,
    )
    op.alter_column(
        "abuse_reports", "reporter_domain", existing_type=sa.String(length=253), nullable=False
    )
    op.alter_column("abuse_reports", "reporter_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_check_constraint(
        "abuse_report_reporter_is_local",
        "abuse_reports",
        "reporter_is_local",
    )
    op.drop_column("abuse_reports", "source")
