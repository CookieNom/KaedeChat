"""add synchronized guild navigation layout

Revision ID: f91a2c7d5e40
Revises: e82f1b6a4c90
Create Date: 2026-08-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f91a2c7d5e40"
down_revision: str | None = "e82f1b6a4c90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "guild_navigation",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{\"items\": []}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "guild_navigation")
