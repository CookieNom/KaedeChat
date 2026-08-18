"""Bind opaque account vault ciphertext to its monotonic revision.

Revision ID: b7f3a0d5e291
Revises: a6e2f9c4d180
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f3a0d5e291"
down_revision: str | None = "a6e2f9c4d180"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Version-one ciphertext did not authenticate its server revision and
    # therefore cannot be safely relabeled as version two. There are no
    # released users; discard these pre-release opaque test vaults instead of
    # pretending they satisfy the stronger format.
    op.execute("DELETE FROM e2ee_account_vaults")
    op.drop_constraint(
        "e2ee_account_vault_format_value",
        "e2ee_account_vaults",
        type_="check",
    )
    op.alter_column(
        "e2ee_account_vaults",
        "format_version",
        existing_type=sa.SmallInteger(),
        server_default="2",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "e2ee_account_vault_format_value",
        "e2ee_account_vaults",
        "format_version = 2",
    )


def downgrade() -> None:
    # Version-two ciphertext likewise cannot be interpreted using v1 AAD.
    op.execute("DELETE FROM e2ee_account_vaults")
    op.drop_constraint(
        "e2ee_account_vault_format_value",
        "e2ee_account_vaults",
        type_="check",
    )
    op.alter_column(
        "e2ee_account_vaults",
        "format_version",
        existing_type=sa.SmallInteger(),
        server_default="1",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "e2ee_account_vault_format_value",
        "e2ee_account_vaults",
        "format_version = 1",
    )
