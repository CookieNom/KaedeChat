"""Retain compact append-only account-vault digest history.

Revision ID: c8a4e1d7f290
Revises: b7f3a0d5e291
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8a4e1d7f290"
down_revision: str | None = "b7f3a0d5e291"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "e2ee_account_vault_digests",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("user_is_local", name="e2ee_account_vault_digests_user_is_local"),
        sa.CheckConstraint("revision > 0", name="e2ee_account_vault_digest_revision_positive"),
        sa.CheckConstraint("octet_length(digest) = 32", name="e2ee_account_vault_digest_length"),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name="fk_e2ee_account_vault_digests_local_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "user_domain", "revision"),
    )


def downgrade() -> None:
    op.drop_table("e2ee_account_vault_digests")
