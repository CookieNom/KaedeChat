"""Permanent account tombstones and durable content deletion requests."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a6e2d4f80931"
down_revision = "f71d9a3b2c80"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("content_deletion", postgresql.JSONB(none_as_null=True)))

    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        "local_auth_fields",
        "users",
        "deleted_at IS NOT NULL OR NOT is_local OR account_type = 'bot' OR password_hash IS NOT NULL",
    )
    op.drop_constraint(op.f("ck_users_password_kdf_fields_complete"), "users", type_="check")
    op.create_check_constraint(
        "password_kdf_fields_complete",
        "users",
        "(deleted_at IS NOT NULL AND password_kdf_version IS NULL AND password_auth_salt IS NULL AND e2ee_vault_salt IS NULL) OR "
        "(is_local AND account_type = 'human' AND password_kdf_version = 2 AND password_auth_salt IS NOT NULL AND e2ee_vault_salt IS NOT NULL) OR "
        "(NOT (is_local AND account_type = 'human') AND password_kdf_version IS NULL AND password_auth_salt IS NULL AND e2ee_vault_salt IS NULL)",
    )


def downgrade():
    # Deletion is intentionally irreversible. Refuse to weaken identity
    # reservations by downgrading a database containing account tombstones.
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM users WHERE deleted_at IS NOT NULL)"))
        .scalar()
    ):
        raise RuntimeError("Cannot downgrade after an account has been deleted")
    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        "local_auth_fields",
        "users",
        "NOT is_local OR account_type = 'bot' OR password_hash IS NOT NULL",
    )
    op.drop_constraint(op.f("ck_users_password_kdf_fields_complete"), "users", type_="check")
    op.create_check_constraint(
        "password_kdf_fields_complete",
        "users",
        "(is_local AND account_type = 'human' AND password_kdf_version = 2 AND password_auth_salt IS NOT NULL AND e2ee_vault_salt IS NOT NULL) OR "
        "(NOT (is_local AND account_type = 'human') AND password_kdf_version IS NULL AND password_auth_salt IS NULL AND e2ee_vault_salt IS NULL)",
    )

    op.drop_column("users", "content_deletion")
    op.drop_column("users", "deleted_at")
