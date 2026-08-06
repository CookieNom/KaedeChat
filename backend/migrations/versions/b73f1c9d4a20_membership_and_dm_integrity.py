"""Enforce overwrite targets, owner membership, and DM channel pairing.

Revision ID: b73f1c9d4a20
Revises: a62d77c48b10
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b73f1c9d4a20"
down_revision: str | None = "a62d77c48b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Bind every overwrite to its channel's guild. Invalid legacy targets are
    # authorization no-ops at best and privilege surprises after an ID is
    # recreated at worst, so remove them before installing the target FKs.
    op.add_column("channel_overwrites", sa.Column("guild_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "channel_overwrites",
        sa.Column("guild_domain", sa.String(length=253), nullable=True),
    )
    op.execute(
        """
        UPDATE channel_overwrites AS overwrite
        SET guild_id = channel.guild_id,
            guild_domain = channel.guild_domain
        FROM channels AS channel
        WHERE channel.id = overwrite.channel_id
          AND channel.origin_domain = overwrite.channel_domain
        """
    )
    op.execute(
        "DELETE FROM channel_overwrites WHERE guild_id IS NULL OR guild_domain IS NULL"
    )
    op.alter_column("channel_overwrites", "guild_id", nullable=False)
    op.alter_column("channel_overwrites", "guild_domain", nullable=False)

    op.add_column(
        "channel_overwrites",
        sa.Column(
            "role_target_id",
            sa.BigInteger(),
            sa.Computed("CASE WHEN target_type = 'role' THEN target_id ELSE NULL END", persisted=True),
            nullable=True,
        ),
    )
    op.add_column(
        "channel_overwrites",
        sa.Column(
            "role_target_domain",
            sa.String(length=253),
            sa.Computed(
                "CASE WHEN target_type = 'role' THEN target_domain ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "channel_overwrites",
        sa.Column(
            "member_target_id",
            sa.BigInteger(),
            sa.Computed(
                "CASE WHEN target_type = 'member' THEN target_id ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "channel_overwrites",
        sa.Column(
            "member_target_domain",
            sa.String(length=253),
            sa.Computed(
                "CASE WHEN target_type = 'member' THEN target_domain ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.execute(
        """
        DELETE FROM channel_overwrites AS overwrite
        WHERE (overwrite.target_type = 'role' AND NOT EXISTS (
            SELECT 1
            FROM roles AS role
            WHERE role.id = overwrite.target_id
              AND role.origin_domain = overwrite.target_domain
              AND role.guild_id = overwrite.guild_id
              AND role.guild_domain = overwrite.guild_domain
        )) OR (overwrite.target_type = 'member' AND NOT EXISTS (
            SELECT 1
            FROM guild_members AS member
            WHERE member.guild_id = overwrite.guild_id
              AND member.guild_domain = overwrite.guild_domain
              AND member.user_id = overwrite.target_id
              AND member.user_domain = overwrite.target_domain
        ))
        """
    )
    op.drop_constraint(
        "fk_channel_overwrites_channel_id_channel_domain_channels",
        "channel_overwrites",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_channel_overwrites_channel_ref_guild",
        "channel_overwrites",
        "channels",
        ["channel_id", "channel_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_channel_overwrites_role_target",
        "channel_overwrites",
        "roles",
        ["role_target_id", "role_target_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_channel_overwrites_member_target",
        "channel_overwrites",
        "guild_members",
        ["guild_id", "guild_domain", "member_target_id", "member_target_domain"],
        ["guild_id", "guild_domain", "user_id", "user_domain"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )

    # The owner identity is authoritative, so repair old rows by restoring the
    # corresponding membership before making that invariant mandatory.
    op.execute(
        """
        INSERT INTO guild_members (
            guild_id,
            guild_domain,
            user_id,
            user_domain,
            joined_at
        )
        SELECT guild.id,
               guild.origin_domain,
               guild.owner_id,
               guild.owner_domain,
               guild.created_at
        FROM guilds AS guild
        ON CONFLICT (guild_id, guild_domain, user_id, user_domain) DO NOTHING
        """
    )
    op.create_foreign_key(
        "fk_guilds_owner_membership",
        "guilds",
        "guild_members",
        ["id", "origin_domain", "owner_id", "owner_domain"],
        ["guild_id", "guild_domain", "user_id", "user_domain"],
        deferrable=True,
        initially="DEFERRED",
    )

    # A DM channel and its conversation row are a single lifecycle object. Fail
    # loudly instead of deleting any legacy orphan that may still own messages.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM channels AS channel
            WHERE channel.type = 1
              AND NOT EXISTS (
                SELECT 1
                FROM dm_conversations AS conversation
                WHERE conversation.id = channel.id
                  AND conversation.origin_domain = channel.origin_domain
              )
          ) THEN
            RAISE EXCEPTION 'cannot enforce DM integrity: orphan type-1 channel exists'
              USING ERRCODE = '23514';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM dm_conversations AS conversation
            JOIN channels AS channel
              ON channel.id = conversation.id
             AND channel.origin_domain = conversation.origin_domain
            WHERE channel.type <> 1 OR channel.guild_id IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'cannot enforce DM integrity: conversation channel is not type 1'
              USING ERRCODE = '23514';
          END IF;
        END
        $$
        """
    )
    op.create_unique_constraint(
        "uq_channels_ref_type", "channels", ["id", "origin_domain", "type"]
    )
    op.add_column(
        "dm_conversations",
        sa.Column("channel_type", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        "ck_dm_conversations_channel_type", "dm_conversations", "channel_type = 1"
    )
    op.drop_constraint(
        "fk_dm_conversations_channel_identity", "dm_conversations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_dm_conversations_channel_identity",
        "dm_conversations",
        "channels",
        ["id", "origin_domain", "channel_type"],
        ["id", "origin_domain", "type"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.add_column(
        "channels",
        sa.Column(
            "dm_conversation_id",
            sa.BigInteger(),
            sa.Computed("CASE WHEN type = 1 THEN id ELSE NULL END", persisted=True),
            nullable=True,
        ),
    )
    op.add_column(
        "channels",
        sa.Column(
            "dm_conversation_domain",
            sa.String(length=253),
            sa.Computed(
                "CASE WHEN type = 1 THEN origin_domain ELSE NULL END", persisted=True
            ),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_channels_dm_conversation_identity",
        "channels",
        "dm_conversations",
        ["dm_conversation_id", "dm_conversation_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_channels_dm_conversation_identity", "channels", type_="foreignkey"
    )
    op.drop_column("channels", "dm_conversation_domain")
    op.drop_column("channels", "dm_conversation_id")
    op.drop_constraint(
        "fk_dm_conversations_channel_identity", "dm_conversations", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_dm_conversations_channel_identity",
        "dm_conversations",
        "channels",
        ["id", "origin_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    op.drop_constraint(
        "ck_dm_conversations_channel_type", "dm_conversations", type_="check"
    )
    op.drop_column("dm_conversations", "channel_type")
    op.drop_constraint("uq_channels_ref_type", "channels", type_="unique")

    op.drop_constraint("fk_guilds_owner_membership", "guilds", type_="foreignkey")

    op.drop_constraint(
        "fk_channel_overwrites_member_target", "channel_overwrites", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_channel_overwrites_role_target", "channel_overwrites", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_channel_overwrites_channel_ref_guild",
        "channel_overwrites",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_channel_overwrites_channel_id_channel_domain_channels",
        "channel_overwrites",
        "channels",
        ["channel_id", "channel_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )
    op.drop_column("channel_overwrites", "member_target_domain")
    op.drop_column("channel_overwrites", "member_target_id")
    op.drop_column("channel_overwrites", "role_target_domain")
    op.drop_column("channel_overwrites", "role_target_id")
    op.drop_column("channel_overwrites", "guild_domain")
    op.drop_column("channel_overwrites", "guild_id")
