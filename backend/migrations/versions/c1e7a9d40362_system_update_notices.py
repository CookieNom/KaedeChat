"""Local-only system accounts and durable update notification receipts."""

from alembic import op
import sqlalchemy as sa

revision = "c1e7a9d40362"
down_revision = "b8d1e6f20394"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(op.f("ck_users_account_type_value"), "users", type_="check")
    op.create_check_constraint(
        "account_type_value", "users", "account_type IN ('human','bot','system')"
    )
    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        "local_auth_fields",
        "users",
        "deleted_at IS NOT NULL OR NOT is_local OR account_type IN ('bot','system') OR password_hash IS NOT NULL",
    )
    op.create_check_constraint("system_is_local", "users", "account_type != 'system' OR is_local")
    op.create_index(
        "uq_users_system_origin",
        "users",
        ["origin_domain"],
        unique=True,
        postgresql_where=sa.text("account_type = 'system'"),
    )
    op.create_table(
        "system_update_notices",
        sa.Column("revision", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_domain", sa.String(253), primary_key=True),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"], ["users.id", "users.origin_domain"], ondelete="CASCADE"
        ),
    )


def downgrade():
    # Downgrading requires explicitly removing the system conversations/account first.
    op.drop_table("system_update_notices")
    op.drop_index("uq_users_system_origin", table_name="users")
    op.drop_constraint(op.f("ck_users_system_is_local"), "users", type_="check")
    op.drop_constraint(op.f("ck_users_account_type_value"), "users", type_="check")
    op.create_check_constraint("account_type_value", "users", "account_type IN ('human','bot')")
    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        "local_auth_fields",
        "users",
        "deleted_at IS NOT NULL OR NOT is_local OR account_type = 'bot' OR password_hash IS NOT NULL",
    )
