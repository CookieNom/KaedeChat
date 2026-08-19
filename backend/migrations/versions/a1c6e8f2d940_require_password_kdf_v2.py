"""Require password KDF v2 for every local human account.

Revision ID: a1c6e8f2d940
Revises: e7a4c1d9b260
Create Date: 2026-08-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1c6e8f2d940"
down_revision: str | None = "e7a4c1d9b260"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A legacy password hash cannot be converted without learning the user's
    # password. Refuse to make an unsafe or destructive guess: operators must
    # complete password resets before deploying the no-downgrade release.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM users
                WHERE is_local
                  AND account_type = 'human'
                  AND (
                      password_kdf_version IS DISTINCT FROM 2
                      OR password_auth_salt IS NULL
                      OR e2ee_vault_salt IS NULL
                  )
            ) THEN
                RAISE EXCEPTION
                    'cannot require password KDF v2 while legacy local accounts remain; reset those account passwords first'
                    USING ERRCODE = '23514';
            END IF;
        END
        $$;
        """
    )
    op.drop_constraint("password_kdf_fields_complete", "users", type_="check")
    op.create_check_constraint(
        "password_kdf_fields_complete",
        "users",
        "(is_local AND account_type = 'human' AND password_kdf_version = 2 "
        "AND password_auth_salt IS NOT NULL AND e2ee_vault_salt IS NOT NULL) OR "
        "(NOT (is_local AND account_type = 'human') AND password_kdf_version IS NULL "
        "AND password_auth_salt IS NULL AND e2ee_vault_salt IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("password_kdf_fields_complete", "users", type_="check")
    op.create_check_constraint(
        "password_kdf_fields_complete",
        "users",
        "(password_kdf_version IS NULL AND password_auth_salt IS NULL) OR "
        "(password_kdf_version = 2 AND password_auth_salt IS NOT NULL)",
    )
