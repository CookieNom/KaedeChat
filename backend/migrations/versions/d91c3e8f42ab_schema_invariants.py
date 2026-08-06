"""Harden cross-entity and lifecycle invariants.

Revision ID: d91c3e8f42ab
Revises: c7b8a9d4e213
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d91c3e8f42ab"
down_revision: str | None = "c7b8a9d4e213"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CHECKS: tuple[tuple[str, str, str], ...] = (
    ("instances", "ck_instances_federation_mode_value", "federation_mode IN ('open','allowlist')"),
    (
        "instances",
        "ck_instances_remote_has_no_private_key",
        "is_self OR (encrypted_private_key IS NULL AND private_key_nonce IS NULL)",
    ),
    ("peer_keys", "ck_peer_keys_public_key_length", "octet_length(public_key) = 32"),
    (
        "peer_keys",
        "ck_peer_keys_expiry_after_fetch",
        "expired_at IS NULL OR expired_at >= fetched_at",
    ),
    ("users", "ck_users_nonnegative_id", "id >= 0"),
    ("users", "ck_users_positive_profile_version", "profile_version >= 1"),
    (
        "users",
        "ck_users_remote_has_no_auth_fields",
        "is_local OR (password_hash IS NULL AND email IS NULL "
        "AND email_verified_at IS NULL AND totp_secret_encrypted IS NULL)",
    ),
    ("sessions", "ck_sessions_expiry_order", "expires_at <= absolute_expires_at"),
    ("one_time_tokens", "ck_one_time_tokens_positive_lifetime", "expires_at > created_at"),
    (
        "guilds",
        "ck_guilds_event_sequence_order",
        "next_event_seq >= 1 AND last_event_seq >= 0 AND last_event_seq < next_event_seq",
    ),
    (
        "guilds",
        "ck_guilds_positive_permission_generation",
        "permission_generation >= 1",
    ),
    ("guild_events", "ck_guild_events_positive_sequence", "seq >= 1"),
    ("guild_members", "ck_guild_members_nonnegative_voice_flags", "voice_flags >= 0"),
    (
        "guild_members",
        "ck_guild_members_positive_member_version",
        "member_version >= 1",
    ),
    ("roles", "ck_roles_origin_matches_guild", "origin_domain = guild_domain"),
    ("roles", "ck_roles_nonnegative_position", "position >= 0"),
    ("roles", "ck_roles_nonnegative_permissions", "permissions >= 0"),
    ("roles", "ck_roles_color_range", "color BETWEEN 0 AND 16777215"),
    (
        "channels",
        "ck_channels_parent_ref_complete",
        "(parent_id IS NULL) = (parent_domain IS NULL)",
    ),
    ("channels", "ck_channels_dm_type_matches_guild", "(type = 1) = (guild_id IS NULL)"),
    (
        "channels",
        "ck_channels_origin_matches_guild",
        "guild_id IS NULL OR origin_domain = guild_domain",
    ),
    (
        "channels",
        "ck_channels_parent_not_self",
        "parent_id IS NULL OR (parent_id, parent_domain) <> (id, origin_domain)",
    ),
    ("channels", "ck_channels_nonnegative_position", "position >= 0"),
    (
        "channels",
        "ck_channels_rate_limit_range",
        "rate_limit_per_user BETWEEN 0 AND 21600",
    ),
    ("channels", "ck_channels_nonnegative_created_floor", "created_floor_id >= 0"),
    (
        "channel_overwrites",
        "ck_channel_overwrites_nonnegative_masks",
        "allow >= 0 AND deny >= 0",
    ),
    ("channel_overwrites", "ck_channel_overwrites_disjoint_masks", "(allow & deny) = 0"),
    ("messages", "ck_messages_nonnegative_id", "id >= 0"),
    (
        "messages",
        "ck_messages_referenced_message_ref_complete",
        "(referenced_message_id IS NULL) = (referenced_message_domain IS NULL)",
    ),
    ("messages", "ck_messages_nonnegative_flags", "flags >= 0"),
    (
        "messages",
        "ck_messages_content_length",
        "content IS NULL OR char_length(content) <= 4000",
    ),
    (
        "messages",
        "ck_messages_deleted_message_has_no_content",
        "deleted_at IS NULL OR content IS NULL",
    ),
    (
        "messages",
        "ck_messages_mentions_are_array",
        "jsonb_typeof(mention_user_refs) = 'array'",
    ),
    (
        "attachments",
        "ck_attachments_message_ref_complete",
        "(message_id IS NULL) = (message_domain IS NULL)",
    ),
    ("attachments", "ck_attachments_nonnegative_size", "size >= 0"),
    (
        "attachments",
        "ck_attachments_positive_dimensions",
        "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0)",
    ),
    ("reactions", "ck_reactions_nonempty_emoji", "char_length(emoji_key) > 0"),
    (
        "dm_conversations",
        "ck_dm_conversations_origin_is_authority",
        "origin_domain = authority_domain",
    ),
    (
        "dm_conversations",
        "ck_dm_conversations_pair_key_format",
        "pair_key ~ '^[0-9a-f]{64}$'",
    ),
    ("read_states", "ck_read_states_nonnegative_mentions", "mention_count >= 0"),
    (
        "invites",
        "ck_invites_channel_ref_complete",
        "(channel_id IS NULL) = (channel_domain IS NULL)",
    ),
    ("invites", "ck_invites_nonnegative_uses", "uses >= 0"),
    ("invites", "ck_invites_positive_max_uses", "max_uses IS NULL OR max_uses > 0"),
    (
        "invites",
        "ck_invites_uses_within_limit",
        "max_uses IS NULL OR uses <= max_uses",
    ),
    ("emojis", "ck_emojis_origin_matches_guild", "origin_domain = guild_domain"),
    (
        "federation_events",
        "ck_federation_events_positive_retention",
        "expires_at IS NULL OR expires_at > created_at",
    ),
    (
        "federation_outbox",
        "ck_federation_outbox_status_value",
        "status IN ('pending','retry','circuit','delivered','failed','expired')",
    ),
    ("federation_outbox", "ck_federation_outbox_nonnegative_attempts", "attempts >= 0"),
    (
        "federation_inbox",
        "ck_federation_inbox_status_value",
        "status IN ('received','processed','rejected')",
    ),
    ("remote_media_cache", "ck_remote_media_cache_nonnegative_size", "size >= 0"),
    (
        "remote_media_cache",
        "ck_remote_media_cache_scan_status",
        "scan_status IN ('pending','clean','infected','failed')",
    ),
)


def upgrade() -> None:
    for table, name, condition in CHECKS:
        op.create_check_constraint(op.f(name), table, condition)

    op.create_unique_constraint(
        "uq_roles_ref_guild", "roles", ["id", "origin_domain", "guild_id", "guild_domain"]
    )
    op.drop_constraint(
        "fk_member_roles_role_id_role_domain_roles", "member_roles", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_member_roles_role_id_role_domain_guild_id_guild_domain_roles",
        "member_roles",
        "roles",
        ["role_id", "role_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_invites_channel_id_channel_domain_channels",
        "invites",
        "channels",
        ["channel_id", "channel_domain"],
        ["id", "origin_domain"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_bans_actor_id_actor_domain_users",
        "bans",
        "users",
        ["actor_id", "actor_domain"],
        ["id", "origin_domain"],
    )
    op.create_foreign_key(
        "fk_audit_log_entries_actor_id_actor_domain_users",
        "audit_log_entries",
        "users",
        ["actor_id", "actor_domain"],
        ["id", "origin_domain"],
    )
    op.create_foreign_key(
        "fk_emojis_creator_id_creator_domain_users",
        "emojis",
        "users",
        ["creator_id", "creator_domain"],
        ["id", "origin_domain"],
    )
    op.create_foreign_key(
        "fk_webhooks_guild_id_guild_domain_guilds",
        "webhooks",
        "guilds",
        ["guild_id", "guild_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_webhooks_creator_id_creator_domain_users",
        "webhooks",
        "users",
        ["creator_id", "creator_domain"],
        ["id", "origin_domain"],
    )
    op.create_unique_constraint("uq_webhooks_token_hash", "webhooks", ["token_hash"])
    op.create_foreign_key(
        "fk_federation_outbox_destination_instances",
        "federation_outbox",
        "instances",
        ["destination"],
        ["domain"],
    )
    op.create_foreign_key(
        "fk_federation_inbox_origin_domain_instances",
        "federation_inbox",
        "instances",
        ["origin_domain"],
        ["domain"],
    )
    op.create_foreign_key(
        "fk_remote_media_cache_origin_domain_instances",
        "remote_media_cache",
        "instances",
        ["origin_domain"],
        ["domain"],
    )

    op.execute(
        """
        CREATE FUNCTION kaede_reject_handle_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.username IS DISTINCT FROM OLD.username
             OR NEW.origin_domain IS DISTINCT FROM OLD.origin_domain THEN
            RAISE EXCEPTION 'user handles are immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_users_immutable_handle
        BEFORE UPDATE OF username, origin_domain ON users
        FOR EACH ROW EXECUTE FUNCTION kaede_reject_handle_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_users_immutable_handle ON users")
    op.execute("DROP FUNCTION kaede_reject_handle_change()")

    op.drop_constraint(
        "fk_remote_media_cache_origin_domain_instances",
        "remote_media_cache",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_federation_inbox_origin_domain_instances", "federation_inbox", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_federation_outbox_destination_instances",
        "federation_outbox",
        type_="foreignkey",
    )
    op.drop_constraint("uq_webhooks_token_hash", "webhooks", type_="unique")
    op.drop_constraint(
        "fk_webhooks_creator_id_creator_domain_users", "webhooks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_webhooks_guild_id_guild_domain_guilds", "webhooks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_emojis_creator_id_creator_domain_users", "emojis", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_audit_log_entries_actor_id_actor_domain_users",
        "audit_log_entries",
        type_="foreignkey",
    )
    op.drop_constraint("fk_bans_actor_id_actor_domain_users", "bans", type_="foreignkey")
    op.drop_constraint(
        "fk_invites_channel_id_channel_domain_channels", "invites", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_member_roles_role_id_role_domain_guild_id_guild_domain_roles",
        "member_roles",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_member_roles_role_id_role_domain_roles",
        "member_roles",
        "roles",
        ["role_id", "role_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_roles_ref_guild", "roles", type_="unique")

    for table, name, _ in reversed(CHECKS):
        op.drop_constraint(op.f(name), table, type_="check")
