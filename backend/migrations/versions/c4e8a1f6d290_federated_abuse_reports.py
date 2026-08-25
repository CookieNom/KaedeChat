"""route remote-member reports to the moderation authority

Revision ID: c4e8a1f6d290
Revises: f35c9e1a7b20
Create Date: 2026-08-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1f6d290"
down_revision: str | None = "f35c9e1a7b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("abuse_report_source_reporter_policy", "abuse_reports", type_="check")
    op.create_check_constraint(
        "abuse_report_source_reporter_policy",
        "abuse_reports",
        "(source = 'user' AND reporter_id IS NOT NULL AND reporter_domain IS NOT NULL "
        "AND reporter_is_local IS NOT NULL) OR "
        "(source = 'photodna' AND reporter_id IS NULL AND reporter_domain IS NULL "
        "AND reporter_is_local IS NULL AND target_type = 'attachment' "
        "AND category = 'illegal_content' AND encryption_mode = 'plaintext')",
    )
    op.create_index(
        "uq_abuse_reports_federated_source_ref",
        "abuse_reports",
        [sa.text("(evidence ->> 'source_report_ref')")],
        unique=True,
        postgresql_where=sa.text("source = 'user' AND reporter_is_local = false"),
    )


def downgrade() -> None:
    op.drop_index("uq_abuse_reports_federated_source_ref", table_name="abuse_reports")
    op.execute(
        "DELETE FROM attachments WHERE report_id IN "
        "(SELECT id FROM abuse_reports WHERE source = 'user' AND reporter_is_local = false)"
    )
    op.execute("DELETE FROM abuse_reports WHERE source = 'user' AND reporter_is_local = false")
    op.drop_constraint("abuse_report_source_reporter_policy", "abuse_reports", type_="check")
    op.create_check_constraint(
        "abuse_report_source_reporter_policy",
        "abuse_reports",
        "(source = 'user' AND reporter_id IS NOT NULL AND reporter_domain IS NOT NULL "
        "AND reporter_is_local) OR "
        "(source = 'photodna' AND reporter_id IS NULL AND reporter_domain IS NULL "
        "AND reporter_is_local IS NULL AND target_type = 'attachment' "
        "AND category = 'illegal_content' AND encryption_mode = 'plaintext')",
    )
