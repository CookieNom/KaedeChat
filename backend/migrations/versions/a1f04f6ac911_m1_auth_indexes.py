"""M1 identity lookup and retention indexes.

Revision ID: a1f04f6ac911
Revises: e66205549616
Create Date: 2026-07-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f04f6ac911"
down_revision: str | None = "e66205549616"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_users_unverified_created",
        "users",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("is_local AND email_verified_at IS NULL"),
    )
    op.create_index(
        "ix_sessions_refresh_token_hash",
        "sessions",
        ["refresh_token_hash"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_previous_token_hash",
        "sessions",
        ["previous_token_hash"],
        unique=False,
        postgresql_where=sa.text("previous_token_hash IS NOT NULL"),
    )
    op.create_index(
        "ix_one_time_tokens_purpose_expires",
        "one_time_tokens",
        ["purpose", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_one_time_tokens_purpose_expires", table_name="one_time_tokens")
    op.drop_index(
        "ix_sessions_previous_token_hash",
        table_name="sessions",
        postgresql_where=sa.text("previous_token_hash IS NOT NULL"),
    )
    op.drop_index("ix_sessions_refresh_token_hash", table_name="sessions")
    op.drop_index(
        "ix_users_unverified_created",
        table_name="users",
        postgresql_where=sa.text("is_local AND email_verified_at IS NULL"),
    )
