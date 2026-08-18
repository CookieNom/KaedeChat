"""Add client-derived passwords and password-encrypted E2EE account vaults.

Revision ID: 7c4e91a8d2f6
Revises: 5a9d2c7e4b10
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c4e91a8d2f6"
down_revision: str | None = "5a9d2c7e4b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_kdf_version", sa.SmallInteger(), nullable=True))
    op.add_column(
        "users",
        sa.Column("password_auth_salt", sa.LargeBinary(length=16), nullable=True),
    )
    op.add_column("users", sa.Column("e2ee_vault_salt", sa.LargeBinary(length=16), nullable=True))
    op.create_check_constraint(
        "password_kdf_version_value",
        "users",
        "password_kdf_version IS NULL OR password_kdf_version = 2",
    )
    op.create_check_constraint(
        "password_auth_salt_length",
        "users",
        "password_auth_salt IS NULL OR octet_length(password_auth_salt) = 16",
    )
    op.create_check_constraint(
        "e2ee_vault_salt_length",
        "users",
        "e2ee_vault_salt IS NULL OR octet_length(e2ee_vault_salt) = 16",
    )
    op.create_check_constraint(
        "password_kdf_fields_complete",
        "users",
        "(password_kdf_version IS NULL AND password_auth_salt IS NULL) OR "
        "(password_kdf_version = 2 AND password_auth_salt IS NOT NULL)",
    )

    op.create_table(
        "e2ee_account_vaults",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("format_version", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("user_is_local", name="e2ee_account_vaults_user_is_local"),
        sa.CheckConstraint("revision > 0", name="e2ee_account_vault_revision_positive"),
        sa.CheckConstraint("format_version = 2", name="e2ee_account_vault_format_value"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="e2ee_account_vault_nonce_length"),
        sa.CheckConstraint(
            "octet_length(ciphertext) BETWEEN 17 AND 33554448",
            name="e2ee_account_vault_ciphertext_length",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name="fk_e2ee_account_vaults_local_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "user_domain"),
    )


def downgrade() -> None:
    op.drop_table("e2ee_account_vaults")
    op.drop_constraint("password_kdf_fields_complete", "users", type_="check")
    op.drop_constraint("e2ee_vault_salt_length", "users", type_="check")
    op.drop_constraint("password_auth_salt_length", "users", type_="check")
    op.drop_constraint("password_kdf_version_value", "users", type_="check")
    op.drop_column("users", "e2ee_vault_salt")
    op.drop_column("users", "password_auth_salt")
    op.drop_column("users", "password_kdf_version")
