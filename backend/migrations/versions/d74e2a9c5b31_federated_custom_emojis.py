"""add content identity for federated custom emoji

Revision ID: d74e2a9c5b31
Revises: c83a7d1e4f20
Create Date: 2026-08-07 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d74e2a9c5b31"
down_revision: str | None = "c83a7d1e4f20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "reactions",
        "emoji_key",
        existing_type=sa.String(length=255),
        type_=sa.String(length=320),
        existing_nullable=False,
    )
    op.add_column("emojis", sa.Column("media_hash", sa.String(length=64), nullable=True))
    op.execute(
        """
        UPDATE emojis AS emoji
           SET media_hash = attachment.content_sha256
          FROM attachments AS attachment
         WHERE attachment.asset_binding =
               ('emoji:' || emoji.origin_domain || ':' || emoji.id::text)
           AND attachment.content_sha256 IS NOT NULL
        """
    )
    op.create_check_constraint(
        op.f("ck_emojis_media_hash_format"),
        "emojis",
        "media_hash IS NULL OR media_hash ~ '^[0-9a-f]{64}$'",
    )
    # The initial schema used an unnamed convention-generated constraint.
    op.drop_constraint(op.f("uq_emojis_guild_id_guild_domain_name"), "emojis", type_="unique")
    op.create_unique_constraint(
        "uq_emojis_guild_name", "emojis", ["guild_id", "guild_domain", "name"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_emojis_guild_name", "emojis", type_="unique")
    op.create_unique_constraint(
        op.f("uq_emojis_guild_id_guild_domain_name"),
        "emojis",
        ["guild_id", "guild_domain", "name"],
    )
    op.drop_constraint(op.f("ck_emojis_media_hash_format"), "emojis", type_="check")
    op.drop_column("emojis", "media_hash")
    op.alter_column(
        "reactions",
        "emoji_key",
        existing_type=sa.String(length=320),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
