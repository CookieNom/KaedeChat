"""group direct messages

Revision ID: d15a6c8e2f40
Revises: c94d7e2a61f0
Create Date: 2026-08-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d15a6c8e2f40"
down_revision: str | None = "c94d7e2a61f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("dm_conversations", sa.Column("owner_id", sa.BigInteger()))
    op.add_column("dm_conversations", sa.Column("owner_domain", sa.String(length=253)))
    op.add_column(
        "dm_conversations",
        sa.Column("state_version", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_dm_conversations_owner_id_owner_domain_users"),
        "dm_conversations",
        "users",
        ["owner_id", "owner_domain"],
        ["id", "origin_domain"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_dm_conversations_owner_matches_type"),
        "dm_conversations",
        "(type = 'direct' AND owner_id IS NULL AND owner_domain IS NULL) OR "
        "(type = 'group' AND owner_id IS NOT NULL AND owner_domain IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_dm_conversations_owner_matches_type"), "dm_conversations", type_="check"
    )
    op.drop_constraint(
        op.f("fk_dm_conversations_owner_id_owner_domain_users"),
        "dm_conversations",
        type_="foreignkey",
    )
    op.drop_column("dm_conversations", "owner_domain")
    op.drop_column("dm_conversations", "owner_id")
    op.drop_column("dm_conversations", "state_version")
