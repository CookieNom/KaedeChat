"""mobile activity push kinds

Revision ID: a23d8f1c6e40
Revises: f8d2a6c4e190
Create Date: 2026-08-23
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a23d8f1c6e40"
down_revision: str | None = "f8d2a6c4e190"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_push_wake_outbox_kind_value", "push_wake_outbox", type_="check")
    op.create_check_constraint(
        "ck_push_wake_outbox_kind_value",
        "push_wake_outbox",
        "kind IN ('direct_message','mention','guild_message','call','moderation','relationship')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM push_wake_outbox WHERE kind IN ('call','moderation','relationship')")
    op.drop_constraint("ck_push_wake_outbox_kind_value", "push_wake_outbox", type_="check")
    op.create_check_constraint(
        "ck_push_wake_outbox_kind_value",
        "push_wake_outbox",
        "kind IN ('direct_message','mention','guild_message')",
    )
