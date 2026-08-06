"""add M4 media lifecycle metadata

Revision ID: f24b7e9a3c10
Revises: e19a4d6c8b20
Create Date: 2026-07-19 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f24b7e9a3c10"
down_revision: str | None = "e19a4d6c8b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("content_sha256", sa.String(64), nullable=True))
    op.add_column("attachments", sa.Column("perceptual_hash", sa.String(64), nullable=True))
    op.add_column(
        "attachments", sa.Column("detected_content_type", sa.String(255), nullable=True)
    )
    op.add_column(
        "attachments",
        sa.Column(
            "variants",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "attachments",
        sa.Column("purpose", sa.String(24), server_default="attachment", nullable=False),
    )
    op.add_column("attachments", sa.Column("asset_binding", sa.String(600), nullable=True))
    op.create_unique_constraint("uq_attachments_asset_binding", "attachments", ["asset_binding"])
    op.add_column(
        "attachments", sa.Column("upload_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "attachments", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner','emoji','webhook_avatar')",
    )
    op.create_check_constraint(
        op.f("ck_attachments_sha256_length"),
        "attachments",
        "content_sha256 IS NULL OR char_length(content_sha256) = 64",
    )
    op.create_check_constraint(
        op.f("ck_attachments_variants_object"),
        "attachments",
        "jsonb_typeof(variants) = 'object'",
    )
    op.create_index(
        "ix_attachments_pending_gc",
        "attachments",
        ["upload_expires_at"],
        unique=False,
        postgresql_where=sa.text("finalized_at IS NULL"),
    )
    op.create_index(
        "ix_attachments_uploader_usage",
        "attachments",
        ["uploader_id", "uploader_domain"],
        unique=False,
    )
    op.create_index(
        "ix_attachments_public_asset_hash",
        "attachments",
        ["content_sha256"],
        unique=False,
        postgresql_where=sa.text("purpose <> 'attachment' AND scan_status = 'clean'"),
    )

    op.add_column(
        "remote_media_cache",
        sa.Column(
            "content_type",
            sa.String(255),
            server_default="application/octet-stream",
            nullable=False,
        ),
    )
    op.add_column(
        "remote_media_cache", sa.Column("content_sha256", sa.String(64), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_remote_media_cache_sha256_length"),
        "remote_media_cache",
        "content_sha256 IS NULL OR char_length(content_sha256) = 64",
    )

    op.add_column("messages", sa.Column("webhook_id", sa.BigInteger(), nullable=True))
    op.add_column("messages", sa.Column("webhook_name", sa.String(80), nullable=True))
    op.add_column("messages", sa.Column("webhook_avatar_hash", sa.String(128), nullable=True))
    op.create_foreign_key(
        "fk_messages_webhook_id_webhooks",
        "messages",
        "webhooks",
        ["webhook_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_messages_webhook_id_webhooks", "messages", type_="foreignkey")
    op.drop_column("messages", "webhook_avatar_hash")
    op.drop_column("messages", "webhook_name")
    op.drop_column("messages", "webhook_id")
    op.drop_constraint(
        op.f("ck_remote_media_cache_sha256_length"),
        "remote_media_cache",
        type_="check",
    )
    op.drop_column("remote_media_cache", "content_sha256")
    op.drop_column("remote_media_cache", "content_type")
    op.drop_index("ix_attachments_public_asset_hash", table_name="attachments")
    op.drop_index("ix_attachments_uploader_usage", table_name="attachments")
    op.drop_index("ix_attachments_pending_gc", table_name="attachments")
    op.drop_constraint(op.f("ck_attachments_variants_object"), "attachments", type_="check")
    op.drop_constraint(op.f("ck_attachments_sha256_length"), "attachments", type_="check")
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.drop_constraint("uq_attachments_asset_binding", "attachments", type_="unique")
    op.drop_column("attachments", "deleted_at")
    op.drop_column("attachments", "upload_expires_at")
    op.drop_column("attachments", "purpose")
    op.drop_column("attachments", "asset_binding")
    op.drop_column("attachments", "variants")
    op.drop_column("attachments", "detected_content_type")
    op.drop_column("attachments", "perceptual_hash")
    op.drop_column("attachments", "content_sha256")
