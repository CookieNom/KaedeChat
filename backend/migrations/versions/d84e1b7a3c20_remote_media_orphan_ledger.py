"""track remote media objects until physical deletion

Revision ID: d84e1b7a3c20
Revises: c62f4a9d8e31
Create Date: 2026-08-12 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d84e1b7a3c20"
down_revision: str | None = "c62f4a9d8e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "remote_media_orphans",
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.String(length=500)),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_remote_media_orphans_nonnegative_attempts"),
        ),
        sa.CheckConstraint(
            "size >= 0",
            name=op.f("ck_remote_media_orphans_nonnegative_size"),
        ),
        sa.PrimaryKeyConstraint("object_key", name=op.f("pk_remote_media_orphans")),
    )
    op.create_index(
        op.f("ix_remote_media_orphans_retry"),
        "remote_media_orphans",
        ["next_retry_at"],
    )
    op.add_column(
        "remote_media_tombstones",
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now() + interval '30 days'"),
            nullable=False,
        ),
    )
    op.create_index(
        op.f("ix_remote_media_tombstones_expiry"),
        "remote_media_tombstones",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_remote_media_tombstones_expiry"),
        table_name="remote_media_tombstones",
    )
    op.drop_column("remote_media_tombstones", "expires_at")
    op.drop_index(
        op.f("ix_remote_media_orphans_retry"),
        table_name="remote_media_orphans",
    )
    op.drop_table("remote_media_orphans")
