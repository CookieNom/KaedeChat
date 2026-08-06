"""Bind persisted references to their owning channel or guild.

Revision ID: e34b1a7c9d02
Revises: d91c3e8f42ab
Create Date: 2026-07-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e34b1a7c9d02"
down_revision: str | None = "d91c3e8f42ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # These redundant supersets of the primary keys are the PostgreSQL FK
    # targets that let child rows prove ownership without triggers. The message
    # target includes the RANGE partition key, so PostgreSQL can enforce it on
    # the partitioned table and all present/future partitions.
    op.create_unique_constraint(
        "uq_channels_ref_guild",
        "channels",
        ["id", "origin_domain", "guild_id", "guild_domain"],
    )
    op.create_unique_constraint(
        "uq_messages_ref_channel",
        "messages",
        ["id", "origin_domain", "channel_id", "channel_domain"],
    )

    op.create_check_constraint(
        "ck_channels_parent_requires_guild",
        "channels",
        "parent_id IS NULL OR guild_id IS NOT NULL",
    )
    op.create_foreign_key(
        "fk_channels_parent_ref_guild",
        "channels",
        "channels",
        ["parent_id", "parent_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_messages_reply_ref_channel",
        "messages",
        "messages",
        [
            "referenced_message_id",
            "referenced_message_domain",
            "channel_id",
            "channel_domain",
        ],
        ["id", "origin_domain", "channel_id", "channel_domain"],
    )
    op.create_foreign_key(
        "fk_channels_last_message_ref_channel",
        "channels",
        "messages",
        ["last_message_id", "last_message_domain", "id", "origin_domain"],
        ["id", "origin_domain", "channel_id", "channel_domain"],
    )
    op.create_foreign_key(
        "fk_read_states_last_message_ref_channel",
        "read_states",
        "messages",
        ["last_message_id", "last_message_domain", "channel_id", "channel_domain"],
        ["id", "origin_domain", "channel_id", "channel_domain"],
    )
    op.create_foreign_key(
        "fk_invites_channel_ref_guild",
        "invites",
        "channels",
        ["channel_id", "channel_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        deferrable=True,
        initially="DEFERRED",
    )

    op.drop_constraint(
        "fk_pins_message_id_message_domain_messages", "pins", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_pins_message_ref_channel",
        "pins",
        "messages",
        ["message_id", "message_domain", "channel_id", "channel_domain"],
        ["id", "origin_domain", "channel_id", "channel_domain"],
        ondelete="CASCADE",
    )

    op.drop_constraint(
        "fk_webhooks_channel_id_channel_domain_channels", "webhooks", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_webhooks_channel_ref_guild",
        "webhooks",
        "channels",
        ["channel_id", "channel_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
    )

    # Conversation creation inserts the identity row before its channel. An
    # initially-deferred FK preserves that concurrency-safe insert order while
    # requiring both records to exist by commit.
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


def downgrade() -> None:
    op.drop_constraint(
        "fk_dm_conversations_channel_identity", "dm_conversations", type_="foreignkey"
    )

    op.drop_constraint("fk_webhooks_channel_ref_guild", "webhooks", type_="foreignkey")
    op.create_foreign_key(
        "fk_webhooks_channel_id_channel_domain_channels",
        "webhooks",
        "channels",
        ["channel_id", "channel_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_pins_message_ref_channel", "pins", type_="foreignkey")
    op.create_foreign_key(
        "fk_pins_message_id_message_domain_messages",
        "pins",
        "messages",
        ["message_id", "message_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )

    op.drop_constraint("fk_invites_channel_ref_guild", "invites", type_="foreignkey")
    op.drop_constraint(
        "fk_read_states_last_message_ref_channel", "read_states", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_channels_last_message_ref_channel", "channels", type_="foreignkey"
    )
    op.drop_constraint("fk_messages_reply_ref_channel", "messages", type_="foreignkey")
    op.drop_constraint("fk_channels_parent_ref_guild", "channels", type_="foreignkey")
    op.drop_constraint("ck_channels_parent_requires_guild", "channels", type_="check")
    op.drop_constraint("uq_messages_ref_channel", "messages", type_="unique")
    op.drop_constraint("uq_channels_ref_guild", "channels", type_="unique")
