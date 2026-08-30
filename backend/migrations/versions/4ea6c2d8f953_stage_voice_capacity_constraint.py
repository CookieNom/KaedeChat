"""Make voice-channel capacity bounds type-aware.

Revision ID: 4ea6c2d8f953
Revises: 3d9a5e1c7b42
Create Date: 2026-08-29
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "4ea6c2d8f953"
down_revision: str | None = "3d9a5e1c7b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VOICE_USER_LIMIT_CONSTRAINT = "ck_channels_voice_user_limit_range"
TYPE_AWARE_LIMIT = (
    "user_limit IS NULL OR "
    "(type = 2 AND user_limit BETWEEN 0 AND 99) OR "
    "(type = 13 AND user_limit BETWEEN 0 AND 10000)"
)
LEGACY_LIMIT = "user_limit IS NULL OR user_limit BETWEEN 0 AND 99"


def upgrade() -> None:
    constraint_name = op.f(VOICE_USER_LIMIT_CONSTRAINT)
    op.drop_constraint(constraint_name, "channels", type_="check")
    op.create_check_constraint(
        constraint_name,
        "channels",
        TYPE_AWARE_LIMIT,
    )


def downgrade() -> None:
    constraint_name = op.f(VOICE_USER_LIMIT_CONSTRAINT)
    op.drop_constraint(constraint_name, "channels", type_="check")
    # Older releases cannot represent Stage capacities above 99. Keep the
    # downgrade executable by retaining the largest value that schema accepts.
    op.execute("UPDATE channels SET user_limit = 99 WHERE type = 13 AND user_limit > 99")
    op.create_check_constraint(
        constraint_name,
        "channels",
        LEGACY_LIMIT,
    )
