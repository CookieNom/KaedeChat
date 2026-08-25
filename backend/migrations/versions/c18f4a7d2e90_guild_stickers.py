"""add guild stickers and sticker media transforms

Revision ID: c18f4a7d2e90
Revises: b16f4d8e2a73
Create Date: 2026-08-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c18f4a7d2e90"
down_revision: str | None = "b16f4d8e2a73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "attachments", sa.Column("media_transform", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_attachments_media_transform_object"),
        "attachments",
        "media_transform IS NULL OR jsonb_typeof(media_transform) = 'object'",
    )
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner',"
        "'emoji','sticker','webhook_avatar')",
    )
    op.create_table(
        "stickers",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("description", sa.String(length=100), nullable=True),
        sa.Column("media_hash", sa.String(length=64), nullable=True),
        sa.Column("animated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(length=253), nullable=False),
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(length=253), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "origin_domain = guild_domain", name=op.f("ck_stickers_origin_matches_guild")
        ),
        sa.CheckConstraint(
            "media_hash IS NULL OR media_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_stickers_media_hash_format"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"], ["users.id", "users.origin_domain"]
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain"),
        sa.UniqueConstraint("guild_id", "guild_domain", "name", name="uq_stickers_guild_name"),
    )


def downgrade() -> None:
    op.drop_table("stickers")
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner',"
        "'emoji','webhook_avatar')",
    )
    op.drop_constraint(op.f("ck_attachments_media_transform_object"), "attachments", type_="check")
    op.drop_column("attachments", "media_transform")
