"""add custom icons to guild roles

Revision ID: d12f8a4c7e31
Revises: c4e8a1f6d290
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d12f8a4c7e31"
down_revision: str | None = "c4e8a1f6d290"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner',"
        "'emoji','sticker','webhook_avatar','role_icon')",
    )
    op.add_column("roles", sa.Column("icon_hash", sa.String(length=64), nullable=True))
    op.create_check_constraint(
        op.f("ck_roles_icon_hash_length"),
        "roles",
        "icon_hash IS NULL OR char_length(icon_hash) = 64",
    )


def downgrade() -> None:
    op.execute("DELETE FROM attachments WHERE purpose = 'role_icon'")
    op.drop_constraint(op.f("ck_roles_icon_hash_length"), "roles", type_="check")
    op.drop_column("roles", "icon_hash")
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner',"
        "'emoji','sticker','webhook_avatar')",
    )
