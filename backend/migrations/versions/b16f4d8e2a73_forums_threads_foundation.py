"""add forum and thread schema and permissions

Revision ID: b16f4d8e2a73
Revises: a23d8f1c6e40
Create Date: 2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b16f4d8e2a73"
down_revision: str | None = "a23d8f1c6e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


NEW_PERMISSION_MASK = 6_759_101_702_335_743
OLD_PERMISSION_MASK = 3_302_829_321_471
SEND_MESSAGES = 1 << 11
MANAGE_CHANNELS = 1 << 4
MANAGE_MESSAGES = 1 << 13
THREAD_MEMBER_DEFAULTS = (1 << 35) | (1 << 36) | (1 << 38)
PIN_MESSAGES = 1 << 51
BYPASS_SLOWMODE = 1 << 52


def upgrade() -> None:
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        f"(permissions & ~{NEW_PERMISSION_MASK}) = 0",
    )
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        f"((allow | deny) & ~{NEW_PERMISSION_MASK}) = 0",
    )

    # Preserve the behavior of existing roles when the split permissions become
    # enforceable. Discord enabled the three participation permissions for
    # existing members, while PIN_MESSAGES and BYPASS_SLOWMODE were split from
    # the moderator permissions that previously implied those abilities.
    role_backfill_sql = f"""
        UPDATE roles
        SET permissions = permissions
            | CASE
                WHEN (permissions & {SEND_MESSAGES}) <> 0
                THEN {THREAD_MEMBER_DEFAULTS}
                ELSE 0
              END
            | CASE
                WHEN (permissions & {MANAGE_MESSAGES}) <> 0
                THEN {PIN_MESSAGES}
                ELSE 0
              END
            | CASE
                WHEN (permissions & {MANAGE_CHANNELS | MANAGE_MESSAGES}) <> 0
                THEN {BYPASS_SLOWMODE}
                ELSE 0
              END
        """  # noqa: S608 -- interpolated values are module-owned integer masks.
    op.execute(role_backfill_sql)
    overwrite_backfill_sql = f"""
        UPDATE channel_overwrites
        SET allow = allow
                | CASE
                    WHEN (allow & {SEND_MESSAGES}) <> 0
                    THEN {THREAD_MEMBER_DEFAULTS}
                    ELSE 0
                  END
                | CASE
                    WHEN (allow & {MANAGE_MESSAGES}) <> 0
                    THEN {PIN_MESSAGES}
                    ELSE 0
                  END
                | CASE
                    WHEN (allow & {MANAGE_CHANNELS | MANAGE_MESSAGES}) <> 0
                    THEN {BYPASS_SLOWMODE}
                    ELSE 0
                  END,
            deny = deny
                | CASE
                    WHEN (deny & {SEND_MESSAGES}) <> 0
                    THEN {THREAD_MEMBER_DEFAULTS}
                    ELSE 0
                  END
                | CASE
                    WHEN (deny & {MANAGE_MESSAGES}) <> 0
                    THEN {PIN_MESSAGES}
                    ELSE 0
                  END
                | CASE
                    WHEN (allow & {MANAGE_CHANNELS | MANAGE_MESSAGES}) = 0
                         AND (deny & {MANAGE_CHANNELS | MANAGE_MESSAGES}) <> 0
                    THEN {BYPASS_SLOWMODE}
                    ELSE 0
                  END
        """  # noqa: S608 -- interpolated values are module-owned integer masks.
    op.execute(overwrite_backfill_sql)

    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(
        op.f("ck_channels_channel_type"),
        "channels",
        "type IN (0,1,2,4,5,10,11,12,15)",
    )
    op.alter_column(
        "channels",
        "topic",
        existing_type=sa.String(length=1024),
        type_=sa.String(length=4096),
        existing_nullable=True,
    )

    op.add_column(
        "channels", sa.Column("flags", sa.BigInteger(), server_default="0", nullable=False)
    )
    op.add_column("channels", sa.Column("owner_id", sa.BigInteger(), nullable=True))
    op.add_column("channels", sa.Column("owner_domain", sa.String(length=253), nullable=True))
    op.add_column("channels", sa.Column("archived", sa.Boolean(), nullable=True))
    op.add_column("channels", sa.Column("locked", sa.Boolean(), nullable=True))
    op.add_column("channels", sa.Column("invitable", sa.Boolean(), nullable=True))
    op.add_column("channels", sa.Column("auto_archive_duration", sa.Integer(), nullable=True))
    op.add_column(
        "channels", sa.Column("archive_timestamp", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "channels", sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("channels", sa.Column("message_count", sa.Integer(), nullable=True))
    op.add_column("channels", sa.Column("total_message_sent", sa.Integer(), nullable=True))
    op.add_column("channels", sa.Column("member_count", sa.Integer(), nullable=True))
    op.add_column("channels", sa.Column("starter_message_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "channels",
        sa.Column("starter_message_domain", sa.String(length=253), nullable=True),
    )
    op.add_column(
        "channels",
        sa.Column("default_auto_archive_duration", sa.Integer(), nullable=True),
    )
    op.add_column(
        "channels",
        sa.Column("default_thread_rate_limit_per_user", sa.Integer(), nullable=True),
    )
    op.add_column(
        "channels",
        sa.Column(
            "available_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "channels",
        sa.Column(
            "applied_tag_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "channels",
        sa.Column(
            "default_reaction_emoji",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column("channels", sa.Column("default_sort_order", sa.SmallInteger(), nullable=True))
    op.add_column("channels", sa.Column("default_forum_layout", sa.SmallInteger(), nullable=True))
    op.add_column(
        "channels",
        sa.Column("e2ee_required", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("channels", sa.Column("last_thread_id", sa.BigInteger(), nullable=True))
    op.add_column("channels", sa.Column("last_thread_domain", sa.String(length=253), nullable=True))

    channel_checks = (
        (
            "owner_ref_complete",
            "(owner_id IS NULL) = (owner_domain IS NULL)",
        ),
        (
            "starter_message_ref_complete",
            "(starter_message_id IS NULL) = (starter_message_domain IS NULL)",
        ),
        (
            "last_thread_ref_complete",
            "(last_thread_id IS NULL) = (last_thread_domain IS NULL)",
        ),
        ("last_thread_forum_only", "type = 15 OR last_thread_id IS NULL"),
        (
            "thread_requires_unsynced_parent",
            "type NOT IN (10,11,12) OR unavailable OR "
            "(parent_id IS NOT NULL AND NOT permissions_synced)",
        ),
        (
            "thread_metadata_context",
            "(type IN (10,11,12) AND owner_id IS NOT NULL AND archived IS NOT NULL "
            "AND locked IS NOT NULL AND auto_archive_duration IS NOT NULL "
            "AND archive_timestamp IS NOT NULL AND last_activity_at IS NOT NULL "
            "AND message_count IS NOT NULL "
            "AND total_message_sent IS NOT NULL "
            "AND member_count IS NOT NULL) OR "
            "(type NOT IN (10,11,12) AND owner_id IS NULL AND archived IS NULL "
            "AND locked IS NULL AND auto_archive_duration IS NULL "
            "AND archive_timestamp IS NULL AND last_activity_at IS NULL "
            "AND message_count IS NULL "
            "AND total_message_sent IS NULL "
            "AND member_count IS NULL)",
        ),
        (
            "private_thread_invitable_context",
            "(type = 12 AND invitable IS NOT NULL) OR (type <> 12 AND invitable IS NULL)",
        ),
        (
            "auto_archive_duration_value",
            "auto_archive_duration IS NULL OR auto_archive_duration IN (60,1440,4320,10080)",
        ),
        ("nonnegative_message_count", "message_count IS NULL OR message_count >= 0"),
        (
            "nonnegative_total_message_sent",
            "total_message_sent IS NULL OR total_message_sent >= 0",
        ),
        ("nonnegative_member_count", "member_count IS NULL OR member_count >= 0"),
        (
            "forum_metadata_context",
            "(type = 15 AND default_auto_archive_duration IS NOT NULL "
            "AND default_thread_rate_limit_per_user IS NOT NULL "
            "AND default_forum_layout IS NOT NULL) OR "
            "(type <> 15 AND default_reaction_emoji IS NULL AND default_sort_order IS NULL "
            "AND default_forum_layout IS NULL AND available_tags = '[]'::jsonb)",
        ),
        (
            "default_auto_archive_duration_context",
            "type IN (0,5,15) OR default_auto_archive_duration IS NULL",
        ),
        (
            "default_thread_rate_context",
            "type IN (0,15) OR default_thread_rate_limit_per_user IS NULL",
        ),
        (
            "default_auto_archive_duration_value",
            "default_auto_archive_duration IS NULL OR "
            "default_auto_archive_duration IN (60,1440,4320,10080)",
        ),
        (
            "default_thread_rate_limit_range",
            "default_thread_rate_limit_per_user IS NULL OR "
            "default_thread_rate_limit_per_user BETWEEN 0 AND 21600",
        ),
        (
            "available_tags_value",
            "jsonb_typeof(available_tags) = 'array' AND jsonb_array_length(available_tags) <= 20",
        ),
        (
            "applied_tag_ids_value",
            "jsonb_typeof(applied_tag_ids) = 'array' AND jsonb_array_length(applied_tag_ids) <= 5",
        ),
        (
            "applied_tags_thread_only",
            "type IN (10,11,12) OR applied_tag_ids = '[]'::jsonb",
        ),
        (
            "default_reaction_emoji_object",
            "default_reaction_emoji IS NULL OR jsonb_typeof(default_reaction_emoji) = 'object'",
        ),
        (
            "default_sort_order_value",
            "default_sort_order IS NULL OR default_sort_order IN (0,1)",
        ),
        (
            "default_forum_layout_value",
            "default_forum_layout IS NULL OR default_forum_layout IN (0,1,2)",
        ),
        (
            "e2ee_required_context",
            "type IN (10,11,12,15) OR NOT e2ee_required",
        ),
        ("nonnegative_flags", "flags >= 0"),
    )
    for name, condition in channel_checks:
        op.create_check_constraint(op.f(f"ck_channels_{name}"), "channels", condition)

    op.create_foreign_key(
        "fk_channels_last_thread_ref_guild",
        "channels",
        "channels",
        ["last_thread_id", "last_thread_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_index(
        "ix_channels_parent_activity",
        "channels",
        ["parent_id", "parent_domain", "archived", "last_activity_at"],
        unique=False,
        postgresql_where=sa.text("type IN (10,11,12)"),
    )
    op.create_index(
        "ix_channels_parent_archive",
        "channels",
        ["parent_id", "parent_domain", "archived", "archive_timestamp"],
        unique=False,
        postgresql_where=sa.text("type IN (10,11,12)"),
    )
    op.create_index(
        "ix_channels_thread_archive_due",
        "channels",
        ["last_activity_at"],
        unique=False,
        postgresql_where=sa.text("type IN (10,11,12) AND NOT archived"),
    )
    op.create_index(
        "uq_channels_thread_starter_message",
        "channels",
        ["starter_message_id", "starter_message_domain"],
        unique=True,
        postgresql_where=sa.text("type IN (10,11,12) AND starter_message_id IS NOT NULL"),
    )

    op.create_table(
        "thread_members",
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_domain", sa.String(length=253), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column(
            "joined_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("flags", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "notification_level", sa.String(length=16), server_default="inherit", nullable=False
        ),
        sa.CheckConstraint("flags >= 0", name=op.f("ck_thread_members_nonnegative_flags")),
        sa.CheckConstraint(
            "notification_level IN ('inherit','all','mentions','none')",
            name=op.f("ck_thread_members_notification_level_value"),
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "thread_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_thread_members_thread_ref_guild",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_thread_members_guild_member",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "thread_domain",
            "user_id",
            "user_domain",
            name=op.f("pk_thread_members"),
        ),
    )
    op.create_index(
        "ix_thread_members_user",
        "thread_members",
        ["user_id", "user_domain", "thread_id"],
        unique=False,
    )


def downgrade() -> None:
    # Removing these channel types would otherwise orphan messages and media.
    # Require an operator to export/remove forum and thread data deliberately.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM channels WHERE type IN (10,11,12,15)) THEN
                RAISE EXCEPTION 'cannot downgrade while forum or thread channels exist';
            END IF;
            IF EXISTS (SELECT 1 FROM channels WHERE length(topic) > 1024) THEN
                RAISE EXCEPTION 'cannot downgrade while channel topics exceed 1024 characters';
            END IF;
        END
        $$
        """
    )

    # These bits do not exist in the target revision. Strip both compatibility
    # grants and any later explicit assignments after the data-bearing forum
    # and thread downgrade guards above have succeeded.
    op.execute(
        f"UPDATE roles SET permissions = permissions & {OLD_PERMISSION_MASK}"  # noqa: S608
    )
    op.execute(
        f"UPDATE channel_overwrites SET allow = allow & {OLD_PERMISSION_MASK}, "  # noqa: S608
        f"deny = deny & {OLD_PERMISSION_MASK}"
    )

    op.drop_table("thread_members")
    op.drop_index("uq_channels_thread_starter_message", table_name="channels")
    op.drop_index("ix_channels_thread_archive_due", table_name="channels")
    op.drop_index("ix_channels_parent_activity", table_name="channels")
    op.drop_index("ix_channels_parent_archive", table_name="channels")
    op.drop_constraint("fk_channels_last_thread_ref_guild", "channels", type_="foreignkey")

    for name in (
        "nonnegative_flags",
        "e2ee_required_context",
        "default_forum_layout_value",
        "default_sort_order_value",
        "default_reaction_emoji_object",
        "applied_tags_thread_only",
        "applied_tag_ids_value",
        "available_tags_value",
        "default_thread_rate_limit_range",
        "default_thread_rate_context",
        "default_auto_archive_duration_value",
        "default_auto_archive_duration_context",
        "forum_metadata_context",
        "nonnegative_member_count",
        "nonnegative_total_message_sent",
        "nonnegative_message_count",
        "auto_archive_duration_value",
        "private_thread_invitable_context",
        "thread_metadata_context",
        "thread_requires_unsynced_parent",
        "starter_message_ref_complete",
        "last_thread_forum_only",
        "last_thread_ref_complete",
        "owner_ref_complete",
    ):
        op.drop_constraint(op.f(f"ck_channels_{name}"), "channels", type_="check")

    for column_name in (
        "e2ee_required",
        "last_thread_domain",
        "last_thread_id",
        "default_forum_layout",
        "default_sort_order",
        "default_reaction_emoji",
        "applied_tag_ids",
        "available_tags",
        "default_thread_rate_limit_per_user",
        "default_auto_archive_duration",
        "starter_message_domain",
        "starter_message_id",
        "member_count",
        "total_message_sent",
        "message_count",
        "archive_timestamp",
        "last_activity_at",
        "auto_archive_duration",
        "invitable",
        "locked",
        "archived",
        "owner_domain",
        "owner_id",
        "flags",
    ):
        op.drop_column("channels", column_name)

    op.alter_column(
        "channels",
        "topic",
        existing_type=sa.String(length=4096),
        type_=sa.String(length=1024),
        existing_nullable=True,
    )
    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(op.f("ck_channels_channel_type"), "channels", "type IN (0,1,2,4,5)")

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
