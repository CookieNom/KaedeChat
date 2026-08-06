"""allow local accounts without email

Revision ID: f6d3a9c2b140
Revises: d20f4a8c6e31
Create Date: 2026-07-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f6d3a9c2b140"
down_revision: str | None = "d20f4a8c6e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        op.f("ck_users_local_auth_fields"),
        "users",
        "NOT is_local OR password_hash IS NOT NULL",
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM users WHERE is_local AND email IS NULL) THEN
                RAISE EXCEPTION
                    'cannot downgrade while local accounts without email exist';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint(op.f("ck_users_local_auth_fields"), "users", type_="check")
    op.create_check_constraint(
        op.f("ck_users_local_auth_fields"),
        "users",
        "NOT is_local OR (password_hash IS NOT NULL AND email IS NOT NULL)",
    )
