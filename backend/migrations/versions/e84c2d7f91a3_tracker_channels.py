"""add normalized task tracker channels

Revision ID: e84c2d7f91a3
Revises: d12f8a4c7e31
Create Date: 2026-08-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e84c2d7f91a3"
down_revision: str | None = "d12f8a4c7e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_PERMISSION_MASK = 6_759_101_702_335_743
TRACKER_PERMISSION_MASK = 285_982_278_599_306_495
SEND_MESSAGES = 1 << 11
TRACKER_MEMBER_DEFAULTS = (1 << 53) | (1 << 54)


def upgrade() -> None:
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        f"(permissions & ~{TRACKER_PERMISSION_MASK}) = 0",
    )
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        f"((allow | deny) & ~{TRACKER_PERMISSION_MASK}) = 0",
    )

    # Existing guilds should get the same useful baseline as newly created
    # guilds. A role or category that could create messages previously can
    # create and maintain its own tracker tasks; explicit message denies retain
    # their read-only meaning for newly created, permission-synced trackers.
    op.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions | :tracker_defaults "
            "WHERE (permissions & :send_messages) <> 0"
        ).bindparams(
            tracker_defaults=TRACKER_MEMBER_DEFAULTS,
            send_messages=SEND_MESSAGES,
        )
    )
    op.execute(
        sa.text(
            "UPDATE channel_overwrites SET "
            "allow = allow | CASE WHEN (allow & :send_messages) <> 0 "
            "THEN :tracker_defaults ELSE 0 END, "
            "deny = deny | CASE WHEN (deny & :send_messages) <> 0 "
            "THEN :tracker_defaults ELSE 0 END"
        ).bindparams(
            tracker_defaults=TRACKER_MEMBER_DEFAULTS,
            send_messages=SEND_MESSAGES,
        )
    )

    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(
        op.f("ck_channels_channel_type"),
        "channels",
        "type IN (0,1,2,4,5,10,11,12,15,17)",
    )

    op.create_table(
        "tracker_boards",
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_type", sa.Integer(), server_default="17", nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("key_prefix", sa.String(length=10), nullable=False),
        sa.Column("next_task_number", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "channel_domain = guild_domain",
            name=op.f("ck_tracker_boards_origin_matches_guild"),
        ),
        sa.CheckConstraint(
            "channel_type = 17",
            name=op.f("ck_tracker_boards_channel_type"),
        ),
        sa.CheckConstraint(
            "key_prefix ~ '^[A-Z][A-Z0-9]{1,9}$'",
            name=op.f("ck_tracker_boards_key_prefix_format"),
        ),
        sa.CheckConstraint(
            "next_task_number >= 1",
            name=op.f("ck_tracker_boards_positive_next_task_number"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_tracker_boards_channel_ref_guild",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "channel_type"],
            ["channels.id", "channels.origin_domain", "channels.type"],
            name="fk_tracker_boards_channel_ref_type",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "channel_id",
            "channel_domain",
            name=op.f("pk_tracker_boards"),
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            "guild_id",
            "guild_domain",
            name="uq_tracker_boards_ref_guild",
        ),
    )

    op.create_table(
        "tracker_lanes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("color", sa.Integer(), server_default="0", nullable=False),
        sa.Column("kind", sa.String(length=16), server_default="custom", nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin_domain = channel_domain",
            name=op.f("ck_tracker_lanes_origin_matches_channel"),
        ),
        sa.CheckConstraint(
            "channel_domain = guild_domain",
            name=op.f("ck_tracker_lanes_channel_matches_guild"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 100",
            name=op.f("ck_tracker_lanes_name_length"),
        ),
        sa.CheckConstraint(
            "color BETWEEN 0 AND 16777215",
            name=op.f("ck_tracker_lanes_color_range"),
        ),
        sa.CheckConstraint(
            "kind IN ('backlog','planned','in_progress','completed','custom')",
            name=op.f("ck_tracker_lanes_kind_value"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 49",
            name=op.f("ck_tracker_lanes_position_range"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_lanes_board_ref_guild",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_tracker_lanes")),
        sa.UniqueConstraint(
            "id",
            "origin_domain",
            "channel_id",
            "channel_domain",
            name="uq_tracker_lanes_ref_channel",
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            "position",
            name="uq_tracker_lanes_channel_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index(
        "ix_tracker_lanes_channel_position",
        "tracker_lanes",
        ["channel_id", "channel_domain", "position"],
        unique=False,
    )

    op.create_table(
        "tracker_tasks",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("lane_id", sa.BigInteger(), nullable=False),
        sa.Column("lane_domain", sa.String(length=253), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=8), server_default="none", nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(length=253), nullable=False),
        sa.Column("assignee_id", sa.BigInteger(), nullable=True),
        sa.Column("assignee_domain", sa.String(length=253), nullable=True),
        sa.Column("client_nonce", sa.String(length=64), nullable=True),
        sa.Column("client_request_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "origin_domain = channel_domain",
            name=op.f("ck_tracker_tasks_origin_matches_channel"),
        ),
        sa.CheckConstraint(
            "channel_domain = guild_domain",
            name=op.f("ck_tracker_tasks_channel_matches_guild"),
        ),
        sa.CheckConstraint(
            "lane_domain = channel_domain",
            name=op.f("ck_tracker_tasks_lane_matches_channel"),
        ),
        sa.CheckConstraint("number >= 1", name=op.f("ck_tracker_tasks_positive_number")),
        sa.CheckConstraint(
            "char_length(btrim(title)) BETWEEN 1 AND 200",
            name=op.f("ck_tracker_tasks_title_length"),
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 10000",
            name=op.f("ck_tracker_tasks_description_length"),
        ),
        sa.CheckConstraint(
            "priority IN ('none','low','medium','high','urgent')",
            name=op.f("ck_tracker_tasks_priority_value"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 4999",
            name=op.f("ck_tracker_tasks_position_range"),
        ),
        sa.CheckConstraint(
            "(assignee_id IS NULL) = (assignee_domain IS NULL)",
            name=op.f("ck_tracker_tasks_assignee_ref_complete"),
        ),
        sa.CheckConstraint(
            "(client_nonce IS NULL) = (client_request_hash IS NULL)",
            name=op.f("ck_tracker_tasks_client_idempotency_complete"),
        ),
        sa.CheckConstraint(
            "client_nonce IS NULL OR (char_length(client_nonce) BETWEEN 1 AND 64 "
            "AND client_nonce ~ '^[A-Za-z0-9._:-]+$')",
            name=op.f("ck_tracker_tasks_client_nonce_format"),
        ),
        sa.CheckConstraint(
            "client_request_hash IS NULL OR char_length(client_request_hash) = 64",
            name=op.f("ck_tracker_tasks_client_request_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_tasks_board_ref_guild",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lane_id", "lane_domain", "channel_id", "channel_domain"],
            [
                "tracker_lanes.id",
                "tracker_lanes.origin_domain",
                "tracker_lanes.channel_id",
                "tracker_lanes.channel_domain",
            ],
            name="fk_tracker_tasks_lane_ref_channel",
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name="fk_tracker_tasks_creator",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id", "assignee_domain"],
            ["users.id", "users.origin_domain"],
            name="fk_tracker_tasks_assignee",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain", "assignee_id", "assignee_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_tracker_tasks_assignee_membership",
            ondelete="SET NULL (assignee_id, assignee_domain)",
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_tracker_tasks")),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            "number",
            name="uq_tracker_tasks_channel_number",
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            "creator_id",
            "creator_domain",
            "client_nonce",
            name="uq_tracker_tasks_creator_nonce",
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            "lane_id",
            "lane_domain",
            "position",
            name="uq_tracker_tasks_lane_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.create_index(
        "ix_tracker_tasks_channel_lane_position",
        "tracker_tasks",
        ["channel_id", "channel_domain", "lane_id", "position"],
        unique=False,
    )
    op.create_index(
        "ix_tracker_tasks_channel_assignee",
        "tracker_tasks",
        ["channel_id", "channel_domain", "assignee_id", "assignee_domain"],
        unique=False,
    )
    op.create_index(
        "ix_tracker_tasks_channel_due",
        "tracker_tasks",
        ["channel_id", "channel_domain", "due_at"],
        unique=False,
        postgresql_where=sa.text("due_at IS NOT NULL"),
    )


def downgrade() -> None:
    # Refuse to silently destroy tracker content. Operators must explicitly
    # remove tracker channels before downgrading to a schema that cannot store them.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM channels WHERE type = 17) THEN
            RAISE EXCEPTION 'remove tracker channels before downgrading e84c2d7f91a3';
          END IF;
        END $$
        """
    )
    op.drop_index("ix_tracker_tasks_channel_due", table_name="tracker_tasks")
    op.drop_index("ix_tracker_tasks_channel_assignee", table_name="tracker_tasks")
    op.drop_index("ix_tracker_tasks_channel_lane_position", table_name="tracker_tasks")
    op.drop_table("tracker_tasks")
    op.drop_index("ix_tracker_lanes_channel_position", table_name="tracker_lanes")
    op.drop_table("tracker_lanes")
    op.drop_table("tracker_boards")

    # Strip bits that do not exist at the target revision before restoring its
    # permission-mask constraints.
    op.execute(
        sa.text("UPDATE roles SET permissions = permissions & :old_mask").bindparams(
            old_mask=OLD_PERMISSION_MASK
        )
    )
    op.execute(
        sa.text(
            "UPDATE channel_overwrites SET allow = allow & :old_mask, deny = deny & :old_mask"
        ).bindparams(old_mask=OLD_PERMISSION_MASK)
    )
    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(
        op.f("ck_channels_channel_type"),
        "channels",
        "type IN (0,1,2,4,5,10,11,12,15)",
    )
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        f"((allow | deny) & ~{OLD_PERMISSION_MASK}) = 0",
    )
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        f"(permissions & ~{OLD_PERMISSION_MASK}) = 0",
    )
