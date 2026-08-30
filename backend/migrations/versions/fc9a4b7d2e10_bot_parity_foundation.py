"""add bot parity platform primitives

Revision ID: fc9a4b7d2e10
Revises: fb7c3e9a1d42
Create Date: 2026-08-26 00:00:00.000000

This revision is deliberately additive.  In particular, it does not reuse or
renumber any published permission bit and it keeps ephemeral interaction data
outside the partitioned channel-message history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "fc9a4b7d2e10"
down_revision: str | None = "fb7c3e9a1d42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DOMAIN_LENGTH = 253
USE_EXTERNAL_EMOJIS = 1 << 18
USE_EXTERNAL_STICKERS = 1 << 58
NEW_PERMISSION_MASK = ((1 << 59) - 1) & ~(1 << 19)
OLD_PERMISSION_MASK = 285982278599306495
BOT_PERMISSION_SPLIT_BACKFILLS = (
    "UPDATE bot_applications SET default_permissions = default_permissions | :stickers "
    "WHERE (default_permissions & :emojis) <> 0",
    "UPDATE bot_install_templates SET permissions = permissions | :stickers "
    "WHERE (permissions & :emojis) <> 0",
    "UPDATE bot_installations SET granted_permissions = granted_permissions | :stickers "
    "WHERE (granted_permissions & :emojis) <> 0",
)
BOT_PERMISSION_DOWNGRADE_CLEANUPS = (
    "UPDATE bot_applications SET default_permissions = default_permissions & :retained",
    "UPDATE bot_install_templates SET permissions = permissions & :retained",
    "UPDATE bot_installations SET granted_permissions = granted_permissions & :retained",
)


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb")


def _backfill_proxy_commit_sequences() -> None:
    """Retain discoverable legacy proxy commits without inventing a digest.

    Historical authority events do not contain every request-only field needed
    to reconstruct the new fingerprint safely.  Their exact sequence can still
    be recovered, which lets retention preserve the signed event and leaves the
    existing full projection comparison available for legacy retries.
    """

    op.execute(
        "WITH proxy_receipts AS ("
        "SELECT event.guild_domain AS message_domain, "
        "event.envelope #>> '{content,message,id}' AS message_id, "
        "MIN(event.seq) AS commit_seq "
        "FROM guild_events AS event "
        "WHERE event.envelope->>'type' = 'guild.message.committed' "
        "AND event.envelope #>> '{content,message,id}' IS NOT NULL "
        "GROUP BY event.guild_domain, event.envelope #>> '{content,message,id}'"
        ") "
        "UPDATE messages AS message SET proxy_commit_seq = receipt.commit_seq "
        "FROM proxy_receipts AS receipt "
        "WHERE message.client_nonce IS NOT NULL "
        "AND message.proxy_commit_seq IS NULL "
        "AND message.origin_domain = receipt.message_domain "
        "AND message.id::text = receipt.message_id"
    )


def _replace_permission_mask(mask: int) -> None:
    # The migration validation path upgrades the full chain in one PostgreSQL
    # transaction. Earlier revisions can leave deferred FK trigger events on
    # channel_overwrites; force those already-valid events to run before this
    # table is altered.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.drop_constraint(op.f("ck_roles_known_permission_mask"), "roles", type_="check")
    op.drop_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_roles_known_permission_mask"),
        "roles",
        f"(permissions & ~{mask}) = 0",
    )
    op.create_check_constraint(
        op.f("ck_channel_overwrites_known_permission_masks"),
        "channel_overwrites",
        f"((allow | deny) & ~{mask}) = 0",
    )
    op.execute("SET CONSTRAINTS ALL DEFERRED")


def _harden_bot_permission_masks() -> None:
    constraints = (
        (
            "bot_applications",
            "bot_application_positive_values",
            "default_permissions >= 0 AND manifest_generation >= 1 "
            "AND command_generation >= 1 AND revocation_generation >= 1 "
            f"AND (default_permissions & ~{NEW_PERMISSION_MASK}) = 0",
        ),
        (
            "bot_install_templates",
            "bot_template_positive_values",
            f"permissions >= 0 AND generation >= 1 AND (permissions & ~{NEW_PERMISSION_MASK}) = 0",
        ),
        (
            "bot_installations",
            "bot_installation_positive_values",
            "granted_permissions >= 0 AND grant_revision >= 1 "
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0 "
            f"AND (granted_permissions & ~{NEW_PERMISSION_MASK}) = 0",
        ),
    )
    for table, name, expression in constraints:
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expression)


def _restore_bot_permission_masks() -> None:
    constraints = (
        (
            "bot_applications",
            "bot_application_positive_values",
            "default_permissions >= 0 AND manifest_generation >= 1 "
            "AND command_generation >= 1 AND revocation_generation >= 1",
        ),
        (
            "bot_install_templates",
            "bot_template_positive_values",
            "permissions >= 0 AND generation >= 1",
        ),
        (
            "bot_installations",
            "bot_installation_positive_values",
            "granted_permissions >= 0 AND grant_revision >= 1 "
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0",
        ),
    )
    for table, name, expression in constraints:
        op.drop_constraint(op.f(f"ck_{table}_{name}"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_{name}"), table, expression)


def _backfill_external_sticker_permission() -> None:
    """Keep existing emoji grants behaviorally stable after splitting stickers."""

    op.execute(
        sa.text(
            "UPDATE roles SET permissions = permissions | :stickers "
            "WHERE (permissions & :emojis) <> 0"
        ).bindparams(stickers=USE_EXTERNAL_STICKERS, emojis=USE_EXTERNAL_EMOJIS)
    )
    op.execute(
        sa.text(
            "UPDATE channel_overwrites "
            "SET allow = allow | CASE WHEN (allow & :emojis) <> 0 THEN :stickers ELSE 0 END, "
            "deny = deny | CASE WHEN (deny & :emojis) <> 0 THEN :stickers ELSE 0 END "
            "WHERE ((allow | deny) & :emojis) <> 0"
        ).bindparams(stickers=USE_EXTERNAL_STICKERS, emojis=USE_EXTERNAL_EMOJIS)
    )
    for statement in BOT_PERMISSION_SPLIT_BACKFILLS:
        op.execute(
            sa.text(statement).bindparams(
                stickers=USE_EXTERNAL_STICKERS, emojis=USE_EXTERNAL_EMOJIS
            )
        )


def _remove_new_permissions() -> None:
    """Make stored masks valid before restoring the pre-foundation constraint."""

    op.execute(
        sa.text("UPDATE roles SET permissions = permissions & :retained").bindparams(
            retained=OLD_PERMISSION_MASK
        )
    )
    op.execute(
        sa.text(
            "UPDATE channel_overwrites SET allow = allow & :retained, deny = deny & :retained"
        ).bindparams(retained=OLD_PERMISSION_MASK)
    )
    for statement in BOT_PERMISSION_DOWNGRADE_CLEANUPS:
        op.execute(sa.text(statement).bindparams(retained=OLD_PERMISSION_MASK))


def _remove_interaction_only_e2ee_mode() -> None:
    """Remove the advertised mode that had no privacy-preserving runtime."""

    op.execute(
        "UPDATE bot_applications "
        "SET e2ee_modes = e2ee_modes - 'interaction_only' "
        "WHERE e2ee_modes ? 'interaction_only'"
    )
    op.alter_column(
        "bot_applications",
        "e2ee_modes",
        existing_type=postgresql.JSONB(),
        server_default=_json_default("[]"),
        existing_nullable=False,
    )
    op.create_check_constraint(
        op.f("ck_bot_applications_bot_application_e2ee_modes_value"),
        "bot_applications",
        "jsonb_typeof(e2ee_modes) = 'array' "
        "AND e2ee_modes <@ '[\"participant\"]'::jsonb "
        "AND jsonb_array_length(e2ee_modes) <= 1",
    )
    for table, constraint, disable_interaction_only in (
        (
            "bot_install_templates",
            "bot_template_e2ee_mode_value",
            "UPDATE bot_install_templates SET e2ee_mode = 'disabled' "
            "WHERE e2ee_mode = 'interaction_only'",
        ),
        (
            "bot_installations",
            "bot_installation_e2ee_mode_value",
            "UPDATE bot_installations SET e2ee_mode = 'disabled' "
            "WHERE e2ee_mode = 'interaction_only'",
        ),
    ):
        op.execute(disable_interaction_only)
        op.drop_constraint(op.f(f"ck_{table}_{constraint}"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_{constraint}"),
            table,
            "e2ee_mode IN ('disabled','participant')",
        )
    op.alter_column(
        "bot_install_templates",
        "e2ee_mode",
        existing_type=sa.String(24),
        server_default="disabled",
        existing_nullable=False,
    )


def _restore_interaction_only_e2ee_schema() -> None:
    """Restore the older schema shape for a downgrade without re-enabling rows."""

    op.drop_constraint(
        op.f("ck_bot_applications_bot_application_e2ee_modes_value"),
        "bot_applications",
        type_="check",
    )
    op.alter_column(
        "bot_applications",
        "e2ee_modes",
        existing_type=postgresql.JSONB(),
        server_default=_json_default('["interaction_only"]'),
        existing_nullable=False,
    )
    for table, constraint in (
        ("bot_install_templates", "bot_template_e2ee_mode_value"),
        ("bot_installations", "bot_installation_e2ee_mode_value"),
    ):
        op.drop_constraint(op.f(f"ck_{table}_{constraint}"), table, type_="check")
        op.create_check_constraint(
            op.f(f"ck_{table}_{constraint}"),
            table,
            "e2ee_mode IN ('disabled','interaction_only','participant')",
        )
    op.alter_column(
        "bot_install_templates",
        "e2ee_mode",
        existing_type=sa.String(24),
        server_default="interaction_only",
        existing_nullable=False,
    )


def _add_federated_bot_child_source_refs() -> None:
    """Keep authority snowflakes distinct from collision-safe local surrogates."""

    legacy_backfills = {
        "bot_workers": sa.text(
            "UPDATE bot_workers AS child "
            "SET source_id = child.id, source_domain = child.application_domain "
            "WHERE EXISTS (SELECT 1 FROM instances AS self_instance "
            "WHERE self_instance.is_self "
            "AND self_instance.domain <> child.application_domain)"
        ),
        "bot_install_templates": sa.text(
            "UPDATE bot_install_templates AS child "
            "SET source_id = child.id, source_domain = child.application_domain "
            "WHERE EXISTS (SELECT 1 FROM instances AS self_instance "
            "WHERE self_instance.is_self "
            "AND self_instance.domain <> child.application_domain)"
        ),
        "application_commands": sa.text(
            "UPDATE application_commands AS child "
            "SET source_id = child.id, source_domain = child.application_domain "
            "WHERE EXISTS (SELECT 1 FROM instances AS self_instance "
            "WHERE self_instance.is_self "
            "AND self_instance.domain <> child.application_domain)"
        ),
    }
    definitions = (
        (
            "bot_workers",
            "bot_worker_source_ref_complete",
            "bot_worker_source_authority",
            "uq_bot_worker_source",
        ),
        (
            "bot_install_templates",
            "bot_template_source_ref_complete",
            "bot_template_source_authority",
            "uq_bot_template_source",
        ),
        (
            "application_commands",
            "command_source_ref_complete",
            "command_source_authority",
            "uq_application_command_source",
        ),
    )
    for table, check_name, authority_check_name, unique_name in definitions:
        op.add_column(table, sa.Column("source_id", sa.BigInteger()))
        op.add_column(table, sa.Column("source_domain", sa.String(DOMAIN_LENGTH)))
        # Existing remote mirrors used the authority snowflake directly. Keep
        # that identity as provenance; subsequent refreshes can move safely to
        # locally minted surrogate primary keys without losing the mapping.
        op.execute(legacy_backfills[table])
        op.create_check_constraint(
            op.f(f"ck_{table}_{check_name}"),
            table,
            "(source_id IS NULL) = (source_domain IS NULL)",
        )
        op.create_check_constraint(
            op.f(f"ck_{table}_{authority_check_name}"),
            table,
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
        )
        op.create_unique_constraint(unique_name, table, ["source_id", "source_domain"])


def _remove_federated_bot_child_source_refs() -> None:
    definitions = (
        (
            "application_commands",
            "command_source_ref_complete",
            "command_source_authority",
            "uq_application_command_source",
        ),
        (
            "bot_install_templates",
            "bot_template_source_ref_complete",
            "bot_template_source_authority",
            "uq_bot_template_source",
        ),
        (
            "bot_workers",
            "bot_worker_source_ref_complete",
            "bot_worker_source_authority",
            "uq_bot_worker_source",
        ),
    )
    for table, check_name, authority_check_name, unique_name in definitions:
        op.drop_constraint(unique_name, table, type_="unique")
        op.drop_constraint(op.f(f"ck_{table}_{authority_check_name}"), table, type_="check")
        op.drop_constraint(op.f(f"ck_{table}_{check_name}"), table, type_="check")
        op.drop_column(table, "source_domain")
        op.drop_column(table, "source_id")


def _add_foundation_columns() -> None:
    op.add_column(
        "users",
        sa.Column(
            "age_assurance_state",
            sa.String(16),
            server_default="unknown",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_users_age_assurance_state_value"),
        "users",
        "age_assurance_state IN ('unknown','adult','minor')",
    )
    op.create_check_constraint(
        op.f("ck_users_age_assurance_local_human_only"),
        "users",
        "age_assurance_state = 'unknown' OR (is_local AND account_type = 'human')",
    )
    op.add_column(
        "user_settings",
        sa.Column(
            "age_restricted_dm_commands_enabled",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )

    guild_columns = (
        sa.Column("verification_level", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "default_message_notifications", sa.SmallInteger(), server_default="0", nullable=False
        ),
        sa.Column("explicit_content_filter", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("preferred_locale", sa.String(35), server_default="en-US", nullable=False),
        sa.Column("afk_channel_id", sa.BigInteger()),
        sa.Column("afk_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("afk_timeout", sa.Integer(), server_default="300", nullable=False),
        sa.Column("system_channel_id", sa.BigInteger()),
        sa.Column("system_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("system_channel_flags", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("rules_channel_id", sa.BigInteger()),
        sa.Column("rules_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("public_updates_channel_id", sa.BigInteger()),
        sa.Column("public_updates_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("safety_alerts_channel_id", sa.BigInteger()),
        sa.Column("safety_alerts_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("community_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("invites_disabled_until", sa.DateTime(timezone=True)),
        sa.Column("dms_disabled_until", sa.DateTime(timezone=True)),
    )
    for column in guild_columns:
        op.add_column("guilds", column)
    op.create_check_constraint(
        op.f("ck_guilds_verification_level_range"),
        "guilds",
        "verification_level BETWEEN 0 AND 4",
    )
    op.create_check_constraint(
        op.f("ck_guilds_default_message_notifications_range"),
        "guilds",
        "default_message_notifications BETWEEN 0 AND 1",
    )
    op.create_check_constraint(
        op.f("ck_guilds_explicit_content_filter_range"),
        "guilds",
        "explicit_content_filter BETWEEN 0 AND 2",
    )
    op.create_check_constraint(
        op.f("ck_guilds_afk_timeout_value"),
        "guilds",
        "afk_timeout IN (60,300,900,1800,3600)",
    )
    op.create_check_constraint(
        op.f("ck_guilds_system_channel_flags_nonnegative"),
        "guilds",
        "system_channel_flags >= 0",
    )
    op.create_check_constraint(
        op.f("ck_guilds_settings_channel_refs_complete"),
        "guilds",
        "(afk_channel_id IS NULL) = (afk_channel_domain IS NULL) AND "
        "(system_channel_id IS NULL) = (system_channel_domain IS NULL) AND "
        "(rules_channel_id IS NULL) = (rules_channel_domain IS NULL) AND "
        "(public_updates_channel_id IS NULL) = (public_updates_channel_domain IS NULL) AND "
        "(safety_alerts_channel_id IS NULL) = (safety_alerts_channel_domain IS NULL)",
    )

    op.add_column(
        "guild_members",
        sa.Column("temporary", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("guild_members", sa.Column("last_guild_activity_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_guild_members_prune_activity",
        "guild_members",
        ["guild_id", "guild_domain", "last_guild_activity_at"],
    )

    for column in (
        sa.Column("nsfw", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("bitrate", sa.Integer()),
        sa.Column("user_limit", sa.Integer()),
        sa.Column("rtc_region", sa.String(64)),
        sa.Column("video_quality_mode", sa.SmallInteger()),
        sa.Column("voice_status", sa.String(500)),
    ):
        op.add_column("channels", column)
    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(
        op.f("ck_channels_channel_type"),
        "channels",
        "type IN (0,1,2,4,5,10,11,12,13,15,17)",
    )
    op.execute(
        "UPDATE channels SET bitrate = 64000, user_limit = 0, video_quality_mode = 1 "
        "WHERE type IN (2,13)"
    )
    op.create_check_constraint(
        op.f("ck_channels_voice_metadata_context"),
        "channels",
        "(type IN (2,13) AND bitrate IS NOT NULL AND user_limit IS NOT NULL "
        "AND video_quality_mode IS NOT NULL) OR "
        "(type NOT IN (2,13) AND bitrate IS NULL AND user_limit IS NULL AND rtc_region IS NULL "
        "AND video_quality_mode IS NULL AND voice_status IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_channels_voice_bitrate_range"),
        "channels",
        "bitrate IS NULL OR bitrate BETWEEN 8000 AND 384000",
    )
    op.create_check_constraint(
        op.f("ck_channels_voice_user_limit_range"),
        "channels",
        "user_limit IS NULL OR user_limit BETWEEN 0 AND 99",
    )
    op.create_check_constraint(
        op.f("ck_channels_video_quality_mode_value"),
        "channels",
        "video_quality_mode IS NULL OR video_quality_mode IN (1,2)",
    )

    for column in (
        sa.Column("embeds", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False),
        sa.Column(
            "components", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False
        ),
        sa.Column(
            "sticker_items", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False
        ),
        sa.Column(
            "mention_role_refs",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column("mention_everyone", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("application_id", sa.BigInteger()),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("interaction_metadata", postgresql.JSONB()),
        sa.Column("view_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tts", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("webhook_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("webhook_avatar_url", sa.String(2048)),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("forwarded_message_id", sa.BigInteger()),
        sa.Column("forwarded_message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("forwarded_channel_id", sa.BigInteger()),
        sa.Column("forwarded_channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("forward_snapshot", postgresql.JSONB()),
        sa.Column("poll_result", postgresql.JSONB()),
        sa.Column("message_reference", postgresql.JSONB()),
        sa.Column("proxy_request_fingerprint_version", sa.Integer()),
        sa.Column("proxy_request_fingerprint", sa.String(64)),
        sa.Column("proxy_commit_seq", sa.BigInteger()),
    ):
        op.add_column("messages", column)
    _backfill_proxy_commit_sequences()
    op.execute("UPDATE messages SET published_at = created_at WHERE (flags & 1) = 1")
    op.execute("UPDATE messages SET webhook_domain = origin_domain WHERE webhook_id IS NOT NULL")
    op.execute(
        "UPDATE messages AS message SET message_reference = "
        "jsonb_strip_nulls(jsonb_build_object("
        "'type', 0, "
        "'message_id', message.referenced_message_id::text, "
        "'message_domain', message.referenced_message_domain, "
        "'channel_id', message.channel_id::text, "
        "'channel_domain', message.channel_domain, "
        "'guild_id', channel.guild_id::text, "
        "'guild_domain', channel.guild_domain)) "
        "FROM channels AS channel "
        "WHERE message.message_type = 6 "
        "AND message.referenced_message_id IS NOT NULL "
        "AND (channel.id, channel.origin_domain) = "
        "(message.channel_id, message.channel_domain)"
    )
    # Older builds reserved type 12 for an Activity placeholder that never had
    # a runtime. Preserve its body as an ordinary message instead of letting a
    # reference-less row masquerade as Discord's CHANNEL_FOLLOW_ADD.
    op.execute(
        "UPDATE messages SET message_type = 0 WHERE message_type = 12 AND message_reference IS NULL"
    )
    op.drop_constraint("fk_messages_webhook_id_webhooks", "messages", type_="foreignkey")
    op.create_check_constraint(
        op.f("ck_messages_webhook_ref_complete"),
        "messages",
        "(webhook_id IS NULL) = (webhook_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_proxy_request_fingerprint_complete"),
        "messages",
        "(proxy_request_fingerprint_version IS NULL) = (proxy_request_fingerprint IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_proxy_request_fingerprint_version_positive"),
        "messages",
        "proxy_request_fingerprint_version IS NULL OR proxy_request_fingerprint_version >= 1",
    )
    op.create_check_constraint(
        op.f("ck_messages_proxy_request_fingerprint_format"),
        "messages",
        "proxy_request_fingerprint IS NULL OR proxy_request_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.create_check_constraint(
        op.f("ck_messages_proxy_commit_seq_positive"),
        "messages",
        "proxy_commit_seq IS NULL OR proxy_commit_seq >= 1",
    )
    op.create_check_constraint(
        op.f("ck_messages_proxy_request_fingerprint_has_nonce_receipt"),
        "messages",
        "proxy_request_fingerprint IS NULL OR "
        "(client_nonce IS NOT NULL AND proxy_commit_seq IS NOT NULL)",
    )
    op.create_index(
        "ix_messages_proxy_commit_receipt",
        "messages",
        ["proxy_commit_seq", "channel_id", "channel_domain"],
        postgresql_where=sa.text("proxy_commit_seq IS NOT NULL"),
    )
    op.create_check_constraint(
        op.f("ck_messages_embeds_are_array"), "messages", "jsonb_typeof(embeds) = 'array'"
    )
    op.create_check_constraint(
        op.f("ck_messages_components_are_array"),
        "messages",
        "jsonb_typeof(components) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_messages_sticker_items_are_bounded_array"),
        "messages",
        "jsonb_typeof(sticker_items) = 'array' AND jsonb_array_length(sticker_items) <= 3",
    )
    op.create_check_constraint(
        op.f("ck_messages_role_mentions_are_array"),
        "messages",
        "jsonb_typeof(mention_role_refs) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_messages_application_ref_complete"),
        "messages",
        "(application_id IS NULL) = (application_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_interaction_metadata_is_object"),
        "messages",
        "interaction_metadata IS NULL OR jsonb_typeof(interaction_metadata) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_messages_forwarded_message_ref_complete"),
        "messages",
        "(forwarded_message_id IS NULL) = (forwarded_message_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_forwarded_channel_ref_complete"),
        "messages",
        "(forwarded_channel_id IS NULL) = (forwarded_channel_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_forward_snapshot_is_object"),
        "messages",
        "forward_snapshot IS NULL OR jsonb_typeof(forward_snapshot) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_messages_forward_snapshot_has_source"),
        "messages",
        "forward_snapshot IS NULL OR "
        "(forwarded_message_id IS NOT NULL AND forwarded_channel_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_poll_result_is_object"),
        "messages",
        "poll_result IS NULL OR jsonb_typeof(poll_result) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_messages_poll_result_matches_message_type"),
        "messages",
        "(message_type = 46) = (poll_result IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_messages_poll_result_has_reference"),
        "messages",
        "message_type <> 46 OR referenced_message_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_messages_nonnegative_view_version"), "messages", "view_version >= 0"
    )
    op.create_check_constraint(
        op.f("ck_messages_message_reference_is_object"),
        "messages",
        "message_reference IS NULL OR jsonb_typeof(message_reference) = 'object'",
    )
    op.create_check_constraint(
        op.f("ck_messages_channel_follow_has_reference"),
        "messages",
        "message_type <> 12 OR message_reference IS NOT NULL",
    )

    invite_columns = (
        sa.Column("temporary", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("reusable", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("target_type", sa.String(32)),
        sa.Column("target_user_id", sa.BigInteger()),
        sa.Column("target_user_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("scheduled_event_id", sa.BigInteger()),
        sa.Column("scheduled_event_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column(
            "role_ids",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column(
            "target_user_ids",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
    )
    for column in invite_columns:
        op.add_column("invites", column)
    # Discord caps a finite invite at 100 uses. Preserve any pre-existing
    # legacy rows above that limit while enforcing the cap for every new or
    # updated row; operators can validate after those old invites expire.
    op.drop_constraint(op.f("ck_invites_positive_max_uses"), "invites", type_="check")
    op.create_check_constraint(
        op.f("ck_invites_positive_max_uses"),
        "invites",
        "max_uses IS NULL OR max_uses BETWEEN 1 AND 100",
        postgresql_not_valid=True,
    )
    op.create_check_constraint(
        op.f("ck_invites_target_type_value"),
        "invites",
        "target_type IS NULL OR target_type = 'stream'",
    )
    op.create_check_constraint(
        op.f("ck_invites_target_refs_complete"),
        "invites",
        "(target_user_id IS NULL) = (target_user_domain IS NULL) AND "
        "(scheduled_event_id IS NULL) = (scheduled_event_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_invites_target_type_matches_ref"),
        "invites",
        "(target_type = 'stream' AND target_user_id IS NOT NULL) OR "
        "(target_type IS NULL AND target_user_id IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_invites_role_ids_are_bounded_array"),
        "invites",
        "jsonb_typeof(role_ids) = 'array' AND jsonb_array_length(role_ids) <= 100",
    )
    op.create_check_constraint(
        op.f("ck_invites_target_user_ids_are_bounded_array"),
        "invites",
        "jsonb_typeof(target_user_ids) = 'array' AND jsonb_array_length(target_user_ids) <= 1000",
    )

    op.add_column(
        "emojis", sa.Column("available", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.add_column(
        "stickers",
        sa.Column("tags", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False),
    )
    op.add_column(
        "stickers", sa.Column("available", sa.Boolean(), server_default=sa.true(), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_stickers_tags_are_bounded_array"),
        "stickers",
        "jsonb_typeof(tags) = 'array' AND jsonb_array_length(tags) <= 10",
    )

    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner','emoji',"
        "'sticker','webhook_avatar','role_icon','soundboard','application_asset',"
        "'application_emoji','webhook_attachment','scheduled_event_image')",
    )

    op.add_column(
        "webhooks", sa.Column("type", sa.SmallInteger(), server_default="1", nullable=False)
    )
    op.add_column("webhooks", sa.Column("application_id", sa.BigInteger()))
    op.add_column("webhooks", sa.Column("application_domain", sa.String(DOMAIN_LENGTH)))
    # Existing hashes cannot be reversed. Legacy rows remain NULL and require
    # one explicit manager rotation; every new/rotated token is encrypted.
    op.add_column("webhooks", sa.Column("token_ciphertext", sa.LargeBinary()))
    op.create_foreign_key(
        op.f("fk_webhooks_application_id_application_domain_bot_applications"),
        "webhooks",
        "bot_applications",
        ["application_id", "application_domain"],
        ["id", "origin_domain"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(op.f("ck_webhooks_type_value"), "webhooks", "type IN (1,2,3)")
    op.create_check_constraint(
        op.f("ck_webhooks_application_matches_type"),
        "webhooks",
        "(type = 3) = (application_id IS NOT NULL) AND "
        "(application_id IS NULL) = (application_domain IS NULL)",
    )

    op.add_column(
        "application_commands",
        sa.Column(
            "contexts",
            postgresql.JSONB(),
            server_default=_json_default('["guild"]'),
            nullable=False,
        ),
    )
    op.add_column(
        "application_commands",
        sa.Column(
            "integration_types",
            postgresql.JSONB(),
            server_default=_json_default('["guild_install"]'),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_application_commands_command_contexts_are_array"),
        "application_commands",
        "jsonb_typeof(contexts) = 'array'",
    )
    op.create_check_constraint(
        op.f("ck_application_commands_command_integration_types_are_array"),
        "application_commands",
        "jsonb_typeof(integration_types) = 'array'",
    )
    op.drop_constraint(
        op.f("ck_application_commands_application_command_name_format"),
        "application_commands",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_application_commands_application_command_name_format"),
        "application_commands",
        "char_length(name) BETWEEN 1 AND 32",
    )


def _create_content_tables() -> None:
    op.create_table(
        "guild_scheduled_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger()),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("scheduled_start_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_end_time", sa.DateTime(timezone=True)),
        sa.Column("privacy_level", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("status", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("entity_type", sa.SmallInteger(), nullable=False),
        sa.Column("entity_id", sa.BigInteger()),
        sa.Column("entity_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("entity_metadata", postgresql.JSONB()),
        sa.Column("recurrence_rule", postgresql.JSONB()),
        sa.Column("image_hash", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_guild_scheduled_events")),
        sa.UniqueConstraint(
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            name="uq_guild_scheduled_events_ref_guild",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_guild_scheduled_events_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            ondelete="CASCADE",
            name="fk_guild_scheduled_events_channel_ref",
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_guild_scheduled_events_creator_id_creator_domain_users"),
        ),
        sa.CheckConstraint(
            "origin_domain = guild_domain",
            name=op.f("ck_guild_scheduled_events_origin_matches_guild"),
        ),
        sa.CheckConstraint(
            "(channel_id IS NULL) = (channel_domain IS NULL)",
            name=op.f("ck_guild_scheduled_events_channel_ref_complete"),
        ),
        sa.CheckConstraint(
            "(entity_id IS NULL) = (entity_domain IS NULL)",
            name=op.f("ck_guild_scheduled_events_entity_ref_complete"),
        ),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100",
            name=op.f("ck_guild_scheduled_events_name_length"),
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) BETWEEN 1 AND 1000",
            name=op.f("ck_guild_scheduled_events_description_length"),
        ),
        sa.CheckConstraint(
            "privacy_level = 2",
            name=op.f("ck_guild_scheduled_events_privacy_level_value"),
        ),
        sa.CheckConstraint(
            "status IN (1,2,3,4)",
            name=op.f("ck_guild_scheduled_events_status_value"),
        ),
        sa.CheckConstraint(
            "entity_type IN (1,2,3)",
            name=op.f("ck_guild_scheduled_events_entity_type_value"),
        ),
        sa.CheckConstraint(
            "scheduled_end_time IS NULL OR scheduled_end_time > scheduled_start_time",
            name=op.f("ck_guild_scheduled_events_positive_duration"),
        ),
        sa.CheckConstraint(
            "entity_metadata IS NULL OR jsonb_typeof(entity_metadata) = 'object'",
            name=op.f("ck_guild_scheduled_events_entity_metadata_object"),
        ),
        sa.CheckConstraint(
            "recurrence_rule IS NULL OR jsonb_typeof(recurrence_rule) = 'object'",
            name=op.f("ck_guild_scheduled_events_recurrence_rule_object"),
        ),
        sa.CheckConstraint(
            "(entity_type IN (1,2) AND channel_id IS NOT NULL "
            "AND entity_metadata IS NULL) OR "
            "(entity_type = 3 AND channel_id IS NULL "
            "AND scheduled_end_time IS NOT NULL "
            "AND entity_metadata IS NOT NULL "
            "AND jsonb_typeof(entity_metadata->'location') = 'string' "
            "AND char_length(entity_metadata->>'location') BETWEEN 1 AND 100)",
            name=op.f("ck_guild_scheduled_events_entity_fields_match_type"),
        ),
        sa.CheckConstraint(
            "image_hash IS NULL OR char_length(image_hash) = 64",
            name=op.f("ck_guild_scheduled_events_image_hash_length"),
        ),
    )
    op.create_index(
        "ix_guild_scheduled_events_guild_status_start",
        "guild_scheduled_events",
        ["guild_id", "guild_domain", "status", "scheduled_start_time"],
    )
    op.create_table(
        "stage_instances",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_type", sa.SmallInteger(), server_default="13", nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("topic", sa.String(120), nullable=False),
        sa.Column("privacy_level", sa.SmallInteger(), server_default="2", nullable=False),
        sa.Column("discoverable_disabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("scheduled_event_id", sa.BigInteger()),
        sa.Column("scheduled_event_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("empty_since", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_stage_instances")),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "channel_type"],
            ["channels.id", "channels.origin_domain", "channels.type"],
            ondelete="CASCADE",
            name=op.f("fk_stage_instances_channel_id_channel_domain_channel_type_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_stage_instances_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_stage_instances_creator_id_creator_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["scheduled_event_id", "scheduled_event_domain"],
            ["guild_scheduled_events.id", "guild_scheduled_events.origin_domain"],
            ondelete="SET NULL",
            name=op.f(
                "fk_stage_instances_scheduled_event_id_scheduled_event_domain_"
                "guild_scheduled_events"
            ),
        ),
        sa.CheckConstraint(
            "origin_domain = guild_domain",
            name=op.f("ck_stage_instances_origin_matches_guild"),
        ),
        sa.CheckConstraint(
            "channel_domain = guild_domain",
            name=op.f("ck_stage_instances_channel_origin_matches_guild"),
        ),
        sa.CheckConstraint(
            "creator_id >= 0",
            name=op.f("ck_stage_instances_creator_id_nonnegative"),
        ),
        sa.CheckConstraint("channel_type = 13", name=op.f("ck_stage_instances_channel_type")),
        sa.CheckConstraint(
            "char_length(topic) BETWEEN 1 AND 120",
            name=op.f("ck_stage_instances_topic_length"),
        ),
        sa.CheckConstraint(
            "privacy_level = 2",
            name=op.f("ck_stage_instances_privacy_level_value"),
        ),
        sa.CheckConstraint(
            "(scheduled_event_id IS NULL) = (scheduled_event_domain IS NULL)",
            name=op.f("ck_stage_instances_scheduled_event_ref_complete"),
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_domain",
            name="uq_stage_instances_channel",
        ),
        sa.UniqueConstraint(
            "scheduled_event_id",
            "scheduled_event_domain",
            name="uq_stage_instances_scheduled_event",
        ),
    )
    op.create_table(
        "guild_scheduled_event_subscriptions",
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("event_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "event_id",
            "event_domain",
            "user_id",
            "user_domain",
            name=op.f("pk_guild_scheduled_event_subscriptions"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "event_domain", "guild_id", "guild_domain"],
            [
                "guild_scheduled_events.id",
                "guild_scheduled_events.origin_domain",
                "guild_scheduled_events.guild_id",
                "guild_scheduled_events.guild_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_guild_scheduled_event_subscriptions_event_id_event_domain_guild_id_"
                "guild_domain_guild_scheduled_events"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_guild_scheduled_event_subscriptions_guild_id_guild_domain_user_id_"
                "user_domain_guild_members"
            ),
        ),
        sa.CheckConstraint(
            "event_domain = guild_domain",
            name=op.f("ck_guild_scheduled_event_subscriptions_event_origin_matches_guild"),
        ),
    )
    op.create_index(
        "ix_guild_scheduled_event_subscriptions_user",
        "guild_scheduled_event_subscriptions",
        ["user_id", "user_domain", "created_at"],
    )
    op.create_foreign_key(
        op.f(
            "fk_invites_scheduled_event_id_scheduled_event_domain_guild_id_guild_domain_"
            "guild_scheduled_events"
        ),
        "invites",
        "guild_scheduled_events",
        ["scheduled_event_id", "scheduled_event_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
    )

    op.create_table(
        "polls",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("question", postgresql.JSONB(), nullable=False),
        sa.Column("allow_multiselect", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("layout_type", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("message_id", "message_domain", name=op.f("pk_polls")),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_polls_message_id_message_domain_messages"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(question) = 'object'", name=op.f("ck_polls_question_is_object")
        ),
        sa.CheckConstraint("layout_type = 1", name=op.f("ck_polls_layout_type_value")),
        sa.CheckConstraint("expires_at > created_at", name=op.f("ck_polls_positive_duration")),
    )
    op.create_table(
        "poll_answers",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("answer_id", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.String(55)),
        sa.Column("emoji", postgresql.JSONB()),
        sa.PrimaryKeyConstraint(
            "message_id", "message_domain", "answer_id", name=op.f("pk_poll_answers")
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["polls.message_id", "polls.message_domain"],
            ondelete="CASCADE",
            name=op.f("fk_poll_answers_message_id_message_domain_polls"),
        ),
        sa.CheckConstraint(
            "answer_id BETWEEN 1 AND 10", name=op.f("ck_poll_answers_answer_id_range")
        ),
        sa.CheckConstraint(
            "text IS NOT NULL OR emoji IS NOT NULL",
            name=op.f("ck_poll_answers_answer_has_text_or_emoji"),
        ),
        sa.CheckConstraint(
            "emoji IS NULL OR jsonb_typeof(emoji) = 'object'",
            name=op.f("ck_poll_answers_emoji_is_object"),
        ),
    )
    op.create_table(
        "poll_votes",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("answer_id", sa.SmallInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "message_id",
            "message_domain",
            "answer_id",
            "user_id",
            "user_domain",
            name=op.f("pk_poll_votes"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain", "answer_id"],
            ["poll_answers.message_id", "poll_answers.message_domain", "poll_answers.answer_id"],
            ondelete="CASCADE",
            name=op.f("fk_poll_votes_message_id_message_domain_answer_id_poll_answers"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_poll_votes_user_id_user_domain_users"),
        ),
    )
    op.create_index("ix_poll_votes_voter", "poll_votes", ["user_id", "user_domain", "message_id"])
    op.create_table(
        "message_views",
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("integration_type", sa.String(24), nullable=False),
        sa.Column("installation_id", sa.BigInteger(), nullable=False),
        sa.Column("installation_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("installation_revision", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("persistent", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("message_id", "message_domain", name=op.f("pk_message_views")),
        sa.ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_message_views_message_id_message_domain_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_message_views_application_id_application_domain_bot_applications"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_message_views_positive_version")),
        sa.CheckConstraint(
            "integration_type IN ('guild_install','user_install','dm_capability')",
            name=op.f("ck_message_views_message_view_integration_type_value"),
        ),
        sa.CheckConstraint(
            "installation_revision >= 1",
            name=op.f("ck_message_views_message_view_installation_revision_positive"),
        ),
        sa.CheckConstraint(
            "persistent OR expires_at IS NOT NULL",
            name=op.f("ck_message_views_transient_view_has_expiry"),
        ),
    )
    op.create_table(
        "channel_follows",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("source_channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("target_channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("avatar_hash", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_follows")),
        sa.ForeignKeyConstraint(
            ["source_channel_id", "source_channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_channel_follows_source_channel_id_source_channel_domain_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["target_channel_id", "target_channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_channel_follows_target_channel_id_target_channel_domain_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_channel_follows_creator_id_creator_domain_users"),
        ),
        sa.UniqueConstraint(
            "source_channel_id",
            "source_channel_domain",
            "target_channel_id",
            "target_channel_domain",
            name="uq_channel_follow_pair",
        ),
        sa.CheckConstraint(
            "(source_channel_id, source_channel_domain) <> "
            "(target_channel_id, target_channel_domain)",
            name=op.f("ck_channel_follows_source_and_target_differ"),
        ),
        sa.CheckConstraint(
            "name IS NULL OR (name = btrim(name) AND length(name) BETWEEN 1 AND 80)",
            name=op.f("ck_channel_follows_name_format"),
        ),
        sa.CheckConstraint(
            "avatar_hash IS NULL OR avatar_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_channel_follows_avatar_hash_format"),
        ),
    )
    op.create_table(
        "message_crossposts",
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("follow_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_message_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "source_message_id",
            "source_message_domain",
            "follow_id",
            name=op.f("pk_message_crossposts"),
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id", "source_message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_message_crossposts_source_message_id_source_message_domain_messages"),
        ),
        sa.ForeignKeyConstraint(
            ["follow_id"],
            ["channel_follows.id"],
            ondelete="CASCADE",
            name=op.f("fk_message_crossposts_follow_id_channel_follows"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_message_id", "destination_message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_message_crossposts_destination_message_id_destination_message_domain_messages"
            ),
        ),
        sa.UniqueConstraint(
            "destination_message_id",
            "destination_message_domain",
            name="uq_message_crosspost_destination",
        ),
    )
    op.create_table(
        "federated_channel_follows",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("local_role", sa.String(8), nullable=False),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("source_channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("target_channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("source_authority_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_authority_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("lifecycle_state", sa.String(16), server_default="active", nullable=False),
        sa.Column("authorization_id", sa.String(48), nullable=False),
        sa.Column("authorization_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notice_message_id", sa.BigInteger(), nullable=True),
        sa.Column("notice_message_domain", sa.String(DOMAIN_LENGTH), nullable=True),
        sa.Column("name", sa.String(80), nullable=True),
        sa.Column("avatar_hash", sa.String(64), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("authority_receipt", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "local_role", name=op.f("pk_federated_channel_follows")),
        sa.UniqueConstraint(
            "source_channel_id",
            "source_channel_domain",
            "target_channel_id",
            "target_channel_domain",
            "local_role",
            name="uq_federated_channel_follow_pair_role",
        ),
        sa.CheckConstraint(
            "local_role IN ('source','target')",
            name=op.f("ck_federated_channel_follows_local_role_value"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_federated_channel_follows_positive_generation"),
        ),
        sa.CheckConstraint(
            "lifecycle_state IN ('pending','accepted','active','revoked')",
            name=op.f("ck_federated_channel_follows_lifecycle_state_value"),
        ),
        sa.CheckConstraint(
            "active = (lifecycle_state = 'active')",
            name=op.f("ck_federated_channel_follows_active_matches_lifecycle_state"),
        ),
        sa.CheckConstraint(
            "authorization_id ~ '^kafi_[A-Za-z0-9_-]{43}$'",
            name=op.f("ck_federated_channel_follows_authorization_id_format"),
        ),
        sa.CheckConstraint(
            "(lifecycle_state = 'revoked') = (revoked_at IS NOT NULL)",
            name=op.f("ck_federated_channel_follows_revoked_state_has_timestamp"),
        ),
        sa.CheckConstraint(
            "lifecycle_state <> 'active' OR (activated_at IS NOT NULL AND revoked_at IS NULL)",
            name=op.f("ck_federated_channel_follows_active_state_has_timestamp"),
        ),
        sa.CheckConstraint(
            "lifecycle_state NOT IN ('pending','accepted') OR "
            "(activated_at IS NULL AND revoked_at IS NULL)",
            name=op.f("ck_federated_channel_follows_incomplete_state_timestamps"),
        ),
        sa.CheckConstraint(
            "(notice_message_id IS NULL) = (notice_message_domain IS NULL)",
            name=op.f("ck_federated_channel_follows_notice_message_ref_complete"),
        ),
        sa.CheckConstraint(
            "notice_message_domain IS NULL OR notice_message_domain = target_channel_domain",
            name=op.f("ck_federated_channel_follows_notice_matches_target"),
        ),
        sa.CheckConstraint(
            "name IS NULL OR (name = btrim(name) AND length(name) BETWEEN 1 AND 80)",
            name=op.f("ck_federated_channel_follows_name_format"),
        ),
        sa.CheckConstraint(
            "avatar_hash IS NULL OR avatar_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_federated_channel_follows_avatar_hash_format"),
        ),
        sa.CheckConstraint(
            "source_authority_domain = source_channel_domain",
            name=op.f("ck_federated_channel_follows_source_authority_matches_channel"),
        ),
        sa.CheckConstraint(
            "target_authority_domain = target_channel_domain",
            name=op.f("ck_federated_channel_follows_target_authority_matches_channel"),
        ),
        sa.CheckConstraint(
            "(source_channel_id, source_channel_domain) <> "
            "(target_channel_id, target_channel_domain)",
            name=op.f("ck_federated_channel_follows_source_and_target_differ"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(authority_receipt) = 'object'",
            name=op.f("ck_federated_channel_follows_authority_receipt_is_object"),
        ),
    )
    op.create_index(
        "ix_federated_channel_follows_source",
        "federated_channel_follows",
        ["source_channel_id", "source_channel_domain", "active"],
    )
    op.create_index(
        "ix_federated_channel_follows_target",
        "federated_channel_follows",
        ["target_channel_id", "target_channel_domain", "active"],
    )
    op.create_table(
        "federated_message_crossposts",
        sa.Column("source_message_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("follow_id", sa.BigInteger(), nullable=False),
        sa.Column("local_role", sa.String(8), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("destination_message_id", sa.BigInteger(), nullable=True),
        sa.Column("destination_message_domain", sa.String(DOMAIN_LENGTH), nullable=True),
        sa.Column("delivery_status", sa.String(16), server_default="delivered", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_retry_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("source_projection", postgresql.JSONB(), nullable=True),
        sa.Column("source_author_profile", postgresql.JSONB(), nullable=True),
        sa.Column(
            "published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "source_message_id",
            "source_message_domain",
            "follow_id",
            "local_role",
            name=op.f("pk_federated_message_crossposts"),
        ),
        sa.ForeignKeyConstraint(
            ["follow_id", "local_role"],
            ["federated_channel_follows.id", "federated_channel_follows.local_role"],
            ondelete="CASCADE",
            name=op.f(
                "fk_federated_message_crossposts_follow_id_local_role_federated_channel_follows"
            ),
        ),
        sa.CheckConstraint(
            "local_role IN ('source','target')",
            name=op.f("ck_federated_message_crossposts_local_role_value"),
        ),
        sa.CheckConstraint(
            "generation >= 1 AND attempts >= 0",
            name=op.f("ck_federated_message_crossposts_positive_delivery_values"),
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending','retry','delivered','terminal')",
            name=op.f("ck_federated_message_crossposts_delivery_status_value"),
        ),
        sa.CheckConstraint(
            "(destination_message_id IS NULL) = (destination_message_domain IS NULL)",
            name=op.f("ck_federated_message_crossposts_destination_ref_complete"),
        ),
        sa.CheckConstraint(
            "delivery_status <> 'delivered' OR destination_message_id IS NOT NULL",
            name=op.f("ck_federated_message_crossposts_completed_delivery_has_destination"),
        ),
        sa.CheckConstraint(
            "(local_role = 'source' AND jsonb_typeof(source_projection) = 'object' "
            "AND jsonb_typeof(source_author_profile) = 'object') OR "
            "(local_role = 'target' AND source_projection IS NULL "
            "AND source_author_profile IS NULL)",
            name=op.f("ck_federated_message_crossposts_source_job_projection_role"),
        ),
        sa.CheckConstraint(
            "local_role <> 'target' OR (delivery_status = 'delivered' AND attempts = 1)",
            name=op.f("ck_federated_message_crossposts_target_receipt_is_delivered"),
        ),
    )
    op.create_index(
        "ix_federated_message_crossposts_destination",
        "federated_message_crossposts",
        ["destination_message_id", "destination_message_domain"],
    )
    op.create_index(
        "uq_federated_message_crosspost_target_destination",
        "federated_message_crossposts",
        ["destination_message_id", "destination_message_domain", "local_role"],
        unique=True,
        postgresql_where=sa.text("local_role = 'target'"),
    )
    op.create_index(
        "ix_federated_message_crossposts_retry",
        "federated_message_crossposts",
        ["local_role", "delivery_status", "next_retry_at"],
    )


def _create_expression_and_automod_tables() -> None:
    op.create_unique_constraint(
        "uq_emojis_ref_guild",
        "emojis",
        ["id", "origin_domain", "guild_id", "guild_domain"],
    )
    op.create_table(
        "emoji_role_restrictions",
        sa.Column("emoji_id", sa.BigInteger(), nullable=False),
        sa.Column("emoji_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("role_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.PrimaryKeyConstraint(
            "emoji_id",
            "emoji_domain",
            "role_id",
            "role_domain",
            name=op.f("pk_emoji_role_restrictions"),
        ),
        sa.ForeignKeyConstraint(
            ["emoji_id", "emoji_domain", "guild_id", "guild_domain"],
            ["emojis.id", "emojis.origin_domain", "emojis.guild_id", "emojis.guild_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_emoji_role_restrictions_emoji_id_emoji_domain_guild_id_guild_domain_emojis"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["role_id", "role_domain", "guild_id", "guild_domain"],
            ["roles.id", "roles.origin_domain", "roles.guild_id", "roles.guild_domain"],
            ondelete="CASCADE",
            name=op.f("fk_emoji_role_restrictions_role_id_role_domain_guild_id_guild_domain_roles"),
        ),
    )
    op.create_table(
        "soundboard_sounds",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("media_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512)),
        sa.Column("content_type", sa.String(64), nullable=False),
        sa.Column("volume", sa.Float(), server_default="1", nullable=False),
        sa.Column("emoji_id", sa.BigInteger()),
        sa.Column("emoji_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("emoji_name", sa.String(64)),
        sa.Column("available", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.BigInteger(), nullable=False),
        sa.Column("created_by_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_soundboard_sounds")),
        sa.UniqueConstraint(
            "id", "origin_domain", "guild_id", "guild_domain", name="uq_soundboard_ref_guild"
        ),
        sa.UniqueConstraint("guild_id", "guild_domain", "name", name="uq_soundboard_guild_name"),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_soundboard_sounds_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id", "created_by_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_soundboard_sounds_created_by_id_created_by_domain_users"),
        ),
        sa.CheckConstraint(
            "origin_domain = guild_domain", name=op.f("ck_soundboard_sounds_origin_matches_guild")
        ),
        sa.CheckConstraint(
            "media_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_soundboard_sounds_media_hash_format")
        ),
        sa.CheckConstraint(
            "volume BETWEEN 0 AND 1", name=op.f("ck_soundboard_sounds_volume_range")
        ),
        sa.CheckConstraint(
            "duration_ms BETWEEN 1 AND 5200", name=op.f("ck_soundboard_sounds_duration_range")
        ),
        sa.CheckConstraint(
            "content_type IN ('audio/mpeg','audio/ogg')",
            name=op.f("ck_soundboard_sounds_content_type_value"),
        ),
        sa.CheckConstraint(
            "(emoji_id IS NULL) = (emoji_domain IS NULL)",
            name=op.f("ck_soundboard_sounds_emoji_ref_complete"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_soundboard_sounds_positive_version")),
    )

    op.create_table(
        "auto_mod_rules",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("origin_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("trigger_type", sa.String(32), nullable=False),
        sa.Column(
            "trigger_metadata",
            postgresql.JSONB(),
            server_default=_json_default("{}"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "origin_domain", name=op.f("pk_auto_mod_rules")),
        sa.UniqueConstraint(
            "id", "origin_domain", "guild_id", "guild_domain", name="uq_auto_mod_rule_ref_guild"
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_auto_mod_rules_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_auto_mod_rules_creator_id_creator_domain_users"),
        ),
        sa.CheckConstraint(
            "origin_domain = guild_domain", name=op.f("ck_auto_mod_rules_origin_matches_guild")
        ),
        sa.CheckConstraint(
            "event_type IN ('message_send','member_update')",
            name=op.f("ck_auto_mod_rules_event_type_value"),
        ),
        sa.CheckConstraint(
            "trigger_type IN ('keyword','spam','keyword_preset','mention_spam','member_profile')",
            name=op.f("ck_auto_mod_rules_trigger_type_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(trigger_metadata) = 'object'",
            name=op.f("ck_auto_mod_rules_trigger_metadata_is_object"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_auto_mod_rules_positive_version")),
    )
    op.create_index(
        "ix_auto_mod_rules_guild", "auto_mod_rules", ["guild_id", "guild_domain", "enabled"]
    )
    op.create_table(
        "auto_mod_actions",
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=_json_default("{}"), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "rule_id", "rule_domain", "position", name=op.f("pk_auto_mod_actions")
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "rule_domain"],
            ["auto_mod_rules.id", "auto_mod_rules.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_auto_mod_actions_rule_id_rule_domain_auto_mod_rules"),
        ),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 9", name=op.f("ck_auto_mod_actions_position_range")
        ),
        sa.CheckConstraint(
            "action_type IN "
            "('block_message','send_alert_message','timeout','block_member_interaction')",
            name=op.f("ck_auto_mod_actions_action_type_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(metadata) = 'object'", name=op.f("ck_auto_mod_actions_metadata_is_object")
        ),
    )
    op.create_table(
        "auto_mod_rule_exempt_roles",
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("role_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.PrimaryKeyConstraint(
            "rule_id",
            "rule_domain",
            "role_id",
            "role_domain",
            name=op.f("pk_auto_mod_rule_exempt_roles"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "rule_domain", "guild_id", "guild_domain"],
            [
                "auto_mod_rules.id",
                "auto_mod_rules.origin_domain",
                "auto_mod_rules.guild_id",
                "auto_mod_rules.guild_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_rule_exempt_roles_rule_id_rule_domain_guild_id_guild_domain_auto_mod_rules"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["role_id", "role_domain", "guild_id", "guild_domain"],
            ["roles.id", "roles.origin_domain", "roles.guild_id", "roles.guild_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_rule_exempt_roles_role_id_role_domain_guild_id_guild_domain_roles"
            ),
        ),
    )
    op.create_table(
        "auto_mod_rule_exempt_channels",
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.PrimaryKeyConstraint(
            "rule_id",
            "rule_domain",
            "channel_id",
            "channel_domain",
            name=op.f("pk_auto_mod_rule_exempt_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "rule_domain", "guild_id", "guild_domain"],
            [
                "auto_mod_rules.id",
                "auto_mod_rules.origin_domain",
                "auto_mod_rules.guild_id",
                "auto_mod_rules.guild_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_rule_exempt_channels_rule_id_rule_domain_guild_id_guild_domain_auto_mod_rules"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            ["channels.id", "channels.origin_domain", "channels.guild_id", "channels.guild_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_rule_exempt_channels_channel_id_channel_domain_guild_id_guild_domain_channels"
            ),
        ),
    )
    op.create_table(
        "auto_mod_executions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("action_type", sa.String(32), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger()),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("matched_content_digest", sa.String(64)),
        sa.Column(
            "evidence", postgresql.JSONB(), server_default=_json_default("{}"), nullable=False
        ),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auto_mod_executions")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_auto_mod_executions_idempotency_key")),
        sa.ForeignKeyConstraint(
            ["rule_id", "rule_domain", "guild_id", "guild_domain"],
            [
                "auto_mod_rules.id",
                "auto_mod_rules.origin_domain",
                "auto_mod_rules.guild_id",
                "auto_mod_rules.guild_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_executions_rule_id_rule_domain_guild_id_guild_domain_auto_mod_rules"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id", "target_user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_auto_mod_executions_target_user_id_target_user_domain_users"),
        ),
        sa.CheckConstraint(
            "(channel_id IS NULL) = (channel_domain IS NULL) AND "
            "(message_id IS NULL) = (message_domain IS NULL)",
            name=op.f("ck_auto_mod_executions_optional_refs_complete"),
        ),
        sa.CheckConstraint(
            "matched_content_digest IS NULL OR matched_content_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auto_mod_executions_matched_digest_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name=op.f("ck_auto_mod_executions_evidence_is_object"),
        ),
        sa.CheckConstraint(
            "outcome IN ('blocked','alerted','timed_out','skipped','failed')",
            name=op.f("ck_auto_mod_executions_outcome_value"),
        ),
    )
    op.create_index(
        "ix_auto_mod_executions_guild", "auto_mod_executions", ["guild_id", "guild_domain", "id"]
    )
    op.create_index("ix_auto_mod_executions_retention", "auto_mod_executions", ["created_at"])
    op.create_table(
        "auto_mod_member_blocks",
        sa.Column("rule_id", sa.BigInteger(), nullable=False),
        sa.Column("rule_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("profile_digest", sa.String(64), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(), server_default=_json_default("{}"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "rule_id",
            "rule_domain",
            "guild_id",
            "guild_domain",
            "user_id",
            "user_domain",
            name=op.f("pk_auto_mod_member_blocks"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_id", "rule_domain", "guild_id", "guild_domain"],
            [
                "auto_mod_rules.id",
                "auto_mod_rules.origin_domain",
                "auto_mod_rules.guild_id",
                "auto_mod_rules.guild_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_member_blocks_rule_id_rule_domain_guild_id_guild_domain_auto_mod_rules"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_auto_mod_member_blocks_guild_id_guild_domain_user_id_user_domain_guild_members"
            ),
        ),
        sa.CheckConstraint(
            "profile_digest ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_auto_mod_member_blocks_profile_digest_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence) = 'object'",
            name=op.f("ck_auto_mod_member_blocks_evidence_is_object"),
        ),
    )
    op.create_index(
        "ix_auto_mod_member_blocks_member",
        "auto_mod_member_blocks",
        ["guild_id", "guild_domain", "user_id", "user_domain"],
    )


def _create_application_tables() -> None:
    op.add_column(
        "bot_applications",
        sa.Column(
            "supported_install_types",
            postgresql.JSONB(),
            server_default=_json_default('["guild_install"]'),
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "user_install_scopes",
            postgresql.JSONB(),
            server_default=_json_default('["applications.commands","interactions.respond"]'),
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "user_install_contexts",
            postgresql.JSONB(),
            server_default=_json_default('["guild","bot_dm","private_channel"]'),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_bot_applications_bot_application_install_types_are_bounded_array"),
        "bot_applications",
        "jsonb_typeof(supported_install_types) = 'array' "
        "AND jsonb_array_length(supported_install_types) BETWEEN 1 AND 2",
    )
    op.create_check_constraint(
        op.f("ck_bot_applications_bot_application_user_install_scopes_are_bounded_array"),
        "bot_applications",
        "jsonb_typeof(user_install_scopes) = 'array' "
        "AND jsonb_array_length(user_install_scopes) BETWEEN 2 AND 4",
    )
    op.create_check_constraint(
        op.f("ck_bot_applications_bot_application_user_install_contexts_are_bounded_array"),
        "bot_applications",
        "jsonb_typeof(user_install_contexts) = 'array' "
        "AND jsonb_array_length(user_install_contexts) BETWEEN 1 AND 3",
    )
    op.create_table(
        "application_command_permissions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("command_id", sa.BigInteger()),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("target_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("permission", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_command_permissions")),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_application_command_permissions_application_id_application_domain_"
                "bot_applications"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_application_command_permissions_guild_id_guild_domain_guilds"),
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["application_commands.id"],
            ondelete="CASCADE",
            name=op.f("fk_application_command_permissions_command_id_application_commands"),
        ),
        sa.CheckConstraint(
            "target_type IN ('role','user','channel')",
            name=op.f(
                "ck_application_command_permissions_"
                "application_command_permission_target_type_value"
            ),
        ),
        sa.CheckConstraint(
            "target_id >= 0",
            name=op.f(
                "ck_application_command_permissions_"
                "application_command_permission_target_id_nonnegative"
            ),
        ),
    )
    op.create_index(
        "uq_application_command_permission_application_target",
        "application_command_permissions",
        [
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
            "target_id",
            "target_domain",
            "target_type",
        ],
        unique=True,
        postgresql_where=sa.text("command_id IS NULL"),
    )
    op.create_index(
        "uq_application_command_permission_command_target",
        "application_command_permissions",
        ["command_id", "target_id", "target_domain", "target_type"],
        unique=True,
        postgresql_where=sa.text("command_id IS NOT NULL"),
    )
    op.create_index(
        "ix_application_command_permissions_guild_application",
        "application_command_permissions",
        ["guild_id", "guild_domain", "application_id", "application_domain"],
    )
    op.create_table(
        "application_assets",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("media_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512)),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("width", sa.Integer()),
        sa.Column("height", sa.Integer()),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_assets")),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_application_assets_application_id_application_domain_bot_applications"),
        ),
        sa.UniqueConstraint(
            "application_id", "application_domain", "kind", "name", name="uq_application_asset_name"
        ),
        sa.CheckConstraint(
            "kind IN ('icon','cover','store','achievement','activity','other')",
            name=op.f("ck_application_assets_application_asset_kind_value"),
        ),
        sa.CheckConstraint(
            "media_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_application_assets_application_asset_hash_format"),
        ),
        sa.CheckConstraint(
            "(width IS NULL) = (height IS NULL) AND (width IS NULL OR (width > 0 AND height > 0))",
            name=op.f("ck_application_assets_application_asset_dimensions"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_application_assets_application_asset_version_positive")
        ),
    )
    op.create_table(
        "application_emojis",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("name_casefold", sa.String(32), nullable=False),
        sa.Column("media_hash", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(512)),
        sa.Column("animated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("available", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("creator_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", "application_domain", name=op.f("pk_application_emojis")),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_application_emojis_application_id_application_domain_bot_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_application_emojis_creator_id_creator_domain_users"),
        ),
        sa.UniqueConstraint(
            "application_id",
            "application_domain",
            "name_casefold",
            name="uq_application_emoji_name",
        ),
        sa.CheckConstraint(
            "media_hash ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_application_emojis_application_emoji_hash_format"),
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_application_emojis_application_emoji_version_positive")
        ),
    )
    op.create_table(
        "bot_user_installations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger()),
        sa.Column("source_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "granted_scopes", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False
        ),
        sa.Column(
            "granted_intents",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column(
            "contexts",
            postgresql.JSONB(),
            server_default=_json_default('["bot_dm","private_channel"]'),
            nullable=False,
        ),
        sa.Column("grant_revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("media_bytes_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("media_pending_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_user_installations")),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_bot_user_installations_application_id_application_domain_bot_applications"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_user_installations_user_id_user_domain_users"),
        ),
        sa.UniqueConstraint(
            "application_id",
            "application_domain",
            "user_id",
            "user_domain",
            name="uq_bot_user_installation_app_user",
        ),
        sa.UniqueConstraint(
            "source_id",
            "source_domain",
            "application_id",
            "application_domain",
            "user_id",
            "user_domain",
            name="uq_bot_user_installation_source_app_user",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(contexts) = 'array'",
            name=op.f("ck_bot_user_installations_user_install_contexts_are_array"),
        ),
        sa.CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name=op.f("ck_bot_user_installations_user_install_source_ref_complete"),
        ),
        sa.CheckConstraint(
            "grant_revision >= 1 AND media_bytes_used >= 0 AND media_pending_bytes >= 0",
            name=op.f("ck_bot_user_installations_user_install_revision_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name=op.f("ck_bot_user_installations_user_install_status_value"),
        ),
    )
    op.create_index(
        "ix_bot_user_installations_user",
        "bot_user_installations",
        ["user_id", "user_domain", "status"],
    )
    op.create_table(
        "bot_application_targets",
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("guild_installations", sa.Integer(), server_default="0", nullable=False),
        sa.Column("user_installations", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "runtime_manifest_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "runtime_revocation_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "runtime_access_revocation_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("runtime_status", sa.String(length=24), server_default="active", nullable=False),
        sa.Column(
            "runtime_target_allowed",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("runtime_fingerprint", sa.LargeBinary(length=32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "application_id",
            "application_domain",
            "target_domain",
            name=op.f("pk_bot_application_targets"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_bot_application_targets_application_id_application_domain_bot_applications"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["target_domain"],
            ["instances.domain"],
            name=op.f("fk_bot_application_targets_target_domain_instances"),
        ),
        sa.CheckConstraint(
            "generation >= 0 AND guild_installations >= 0 AND user_installations >= 0 "
            "AND runtime_manifest_generation >= 0 AND runtime_revocation_generation >= 0 "
            "AND runtime_access_revocation_generation >= 0 "
            "AND (runtime_fingerprint IS NULL OR octet_length(runtime_fingerprint) = 32)",
            name=op.f("ck_bot_application_targets_bot_application_target_nonnegative_values"),
        ),
        sa.CheckConstraint(
            "runtime_status IN "
            "('draft','active','review_required','suspended','deleting','deleted')",
            name=op.f("ck_bot_application_targets_bot_application_target_runtime_status_value"),
        ),
    )
    op.create_index(
        "ix_bot_application_targets_active",
        "bot_application_targets",
        ["application_id", "application_domain", "target_domain"],
        postgresql_where=sa.text("guild_installations > 0 OR user_installations > 0"),
    )
    op.create_table(
        "bot_application_runtime_highwaters",
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("target_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("bot_user_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("manifest_generation", sa.BigInteger(), nullable=False),
        sa.Column("revocation_generation", sa.BigInteger(), nullable=False),
        sa.Column("access_revocation_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("target_allowed", sa.Boolean(), nullable=False),
        sa.Column("runtime_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "application_id",
            "application_domain",
            "target_domain",
            name=op.f("pk_bot_application_runtime_highwaters"),
        ),
        sa.CheckConstraint(
            "manifest_generation >= 1 AND revocation_generation >= 1 "
            "AND access_revocation_generation >= 0 "
            "AND octet_length(runtime_fingerprint) = 32",
            name=op.f(
                "ck_bot_application_runtime_highwaters_bot_application_runtime_highwater_values"
            ),
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','review_required','suspended','deleting','deleted')",
            name=op.f(
                "ck_bot_application_runtime_highwaters_bot_application_runtime_highwater_status_value"
            ),
        ),
    )
    op.create_index(
        "ix_bot_application_runtime_highwaters_expiry",
        "bot_application_runtime_highwaters",
        ["application_domain", "expires_at"],
    )
    op.execute(
        "INSERT INTO bot_application_targets "
        "(application_id, application_domain, target_domain, generation, "
        "guild_installations, user_installations) "
        "SELECT app.id, app.origin_domain, self_instance.domain, 1, "
        "(SELECT count(*) FROM bot_installations AS install "
        " WHERE install.application_id = app.id "
        " AND install.application_domain = app.origin_domain "
        " AND install.status = 'active' AND install.revoked_at IS NULL "
        " AND EXISTS (SELECT 1 FROM guild_members AS member "
        "  WHERE member.guild_id = install.guild_id "
        "  AND member.guild_domain = install.guild_domain "
        "  AND member.user_id = install.bot_user_id "
        "  AND member.user_domain = install.bot_user_domain)), "
        "(SELECT count(*) FROM bot_user_installations AS user_install "
        " WHERE user_install.application_id = app.id "
        " AND user_install.application_domain = app.origin_domain "
        " AND user_install.status = 'active' AND user_install.revoked_at IS NULL) "
        "FROM bot_applications AS app "
        "CROSS JOIN instances AS self_instance "
        "WHERE self_instance.is_self "
        "AND (EXISTS (SELECT 1 FROM bot_installations AS install "
        " WHERE install.application_id = app.id "
        " AND install.application_domain = app.origin_domain "
        " AND install.status = 'active' AND install.revoked_at IS NULL "
        " AND EXISTS (SELECT 1 FROM guild_members AS member "
        "  WHERE member.guild_id = install.guild_id "
        "  AND member.guild_domain = install.guild_domain "
        "  AND member.user_id = install.bot_user_id "
        "  AND member.user_domain = install.bot_user_domain)) "
        "OR EXISTS (SELECT 1 FROM bot_user_installations AS user_install "
        " WHERE user_install.application_id = app.id "
        " AND user_install.application_domain = app.origin_domain "
        " AND user_install.status = 'active' AND user_install.revoked_at IS NULL))"
    )
    op.add_column("attachments", sa.Column("bot_user_installation_id", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_attachments_bot_user_installation_id_bot_user_installations"),
        "attachments",
        "bot_user_installations",
        ["bot_user_installation_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        "NOT (bot_installation_id IS NOT NULL AND bot_user_installation_id IS NOT NULL)",
    )
    op.create_index(
        "ix_attachments_bot_user_installation_usage",
        "attachments",
        ["bot_user_installation_id"],
    )
    op.add_column("attachments", sa.Column("duration_secs", sa.Float()))
    op.add_column("attachments", sa.Column("waveform", sa.String(344)))
    op.add_column("attachments", sa.Column("upload_channel_id", sa.BigInteger()))
    op.add_column("attachments", sa.Column("upload_channel_domain", sa.String(DOMAIN_LENGTH)))
    op.add_column("attachments", sa.Column("source_attachment_id", sa.BigInteger()))
    op.add_column("attachments", sa.Column("source_attachment_domain", sa.String(DOMAIN_LENGTH)))
    op.create_check_constraint(
        op.f("ck_attachments_upload_channel_ref_complete"),
        "attachments",
        "(upload_channel_id IS NULL) = (upload_channel_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_attachments_source_attachment_ref_complete"),
        "attachments",
        "(source_attachment_id IS NULL) = (source_attachment_domain IS NULL)",
    )
    op.create_unique_constraint(
        "uq_attachments_message_source_attachment",
        "attachments",
        [
            "message_id",
            "message_domain",
            "source_attachment_id",
            "source_attachment_domain",
        ],
    )
    op.create_check_constraint(
        op.f("ck_attachments_voice_metadata_complete"),
        "attachments",
        "(duration_secs IS NULL) = (waveform IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_attachments_voice_metadata_valid"),
        "attachments",
        "duration_secs IS NULL OR (duration_secs > 0 AND duration_secs <= 1200 "
        "AND length(waveform) BETWEEN 4 AND 344 AND content_type LIKE 'audio/%' "
        "AND encryption_mode = 'plaintext')",
    )

    op.alter_column(
        "bot_interactions", "installation_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.alter_column("bot_interactions", "guild_id", existing_type=sa.BigInteger(), nullable=True)
    op.alter_column(
        "bot_interactions", "guild_domain", existing_type=sa.String(DOMAIN_LENGTH), nullable=True
    )
    op.alter_column("bot_interactions", "command_name", existing_type=sa.String(32), nullable=True)
    op.drop_constraint(
        op.f("ck_bot_interactions_bot_interaction_payload_mode"),
        "bot_interactions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_bot_interactions_bot_interaction_payload_mode"),
        "bot_interactions",
        "encrypted_payload IS NULL OR (payload - ARRAY["
        "'_interaction_event_snapshot','_interaction_installation_lineage',"
        "'target_ref','response_id','view_version','triggering_interaction_id',"
        "'source_component','source_modal']::text[]) = '{}'::jsonb",
    )
    for column in (
        sa.Column("user_installation_id", sa.BigInteger()),
        sa.Column("dm_capability_id", sa.BigInteger()),
        sa.Column("interaction_type", sa.String(24), server_default="command", nullable=False),
        sa.Column("context", sa.String(24), server_default="guild", nullable=False),
        sa.Column(
            "integration_type", sa.String(24), server_default="guild_install", nullable=False
        ),
        sa.Column("invocation_permissions", sa.BigInteger()),
        sa.Column("invocation_channel_type", sa.Integer()),
        sa.Column("installation_revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("command_id", sa.BigInteger()),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("custom_id", sa.String(100)),
        sa.Column("token_hash", sa.LargeBinary(32)),
        sa.Column("dispatch_fingerprint", sa.LargeBinary(32)),
        sa.Column("response_grant_id", sa.String(64)),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("callback_type", sa.Integer()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("autocomplete_generation", sa.BigInteger()),
    ):
        op.add_column("bot_interactions", column)
    # Preserve the exact authority lineage for interactions that were still
    # retained when this migration started. A permanent default of revision 1
    # would silently weaken callbacks after an installation had already been
    # revised.
    op.execute(
        "UPDATE bot_interactions AS interaction "
        "SET installation_revision = installation.grant_revision "
        "FROM bot_installations AS installation "
        "WHERE interaction.installation_id = installation.id"
    )
    op.alter_column(
        "bot_interactions",
        "installation_revision",
        existing_type=sa.BigInteger(),
        server_default=None,
        existing_nullable=False,
    )
    op.execute(
        "UPDATE bot_interactions AS interaction "
        "SET invocation_channel_type = channel.type "
        "FROM channels AS channel "
        "WHERE interaction.guild_id IS NOT NULL "
        "AND interaction.channel_id = channel.id "
        "AND interaction.channel_domain = channel.origin_domain"
    )
    # Legacy rows predate the structural command FK. Prefer the exact guild
    # command over its global fallback, then the newest retained generation.
    # Commands are soft-versioned, so superseded definitions remain available
    # for this one-time lineage backfill.
    op.execute(
        "UPDATE bot_interactions AS interaction "
        "SET command_id = ("
        "SELECT command.id FROM application_commands AS command "
        "WHERE command.application_id = interaction.application_id "
        "AND command.application_domain = interaction.application_domain "
        "AND command.name = interaction.command_name "
        "AND command.type = interaction.command_type "
        "AND ((command.guild_id = interaction.guild_id "
        "AND command.guild_domain = interaction.guild_domain) "
        "OR command.guild_id IS NULL) "
        "ORDER BY (command.guild_id IS NOT NULL) DESC, command.generation DESC, command.id DESC "
        "LIMIT 1)"
    )
    op.create_foreign_key(
        op.f("fk_bot_interactions_user_installation_id_bot_user_installations"),
        "bot_interactions",
        "bot_user_installations",
        ["user_installation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_bot_interactions_command_id_application_commands"),
        "bot_interactions",
        "application_commands",
        ["command_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    interaction_checks = {
        "bot_interaction_type_value": (
            "interaction_type IN ('command','component','modal_submit','autocomplete')"
        ),
        "bot_interaction_context_value": "context IN ('guild','bot_dm','private_channel')",
        "bot_interaction_integration_type_value": (
            "integration_type IN ('guild_install','user_install','dm_capability')"
        ),
        "bot_interaction_invocation_permissions_nonnegative": (
            "invocation_permissions IS NULL OR (invocation_permissions >= 0 "
            f"AND (invocation_permissions & ~{NEW_PERMISSION_MASK}) = 0)"
        ),
        "bot_interaction_invocation_channel_type_context": (
            "(guild_id IS NULL) = (invocation_channel_type IS NULL) AND "
            "(invocation_channel_type IS NULL OR invocation_channel_type BETWEEN 0 AND 18)"
        ),
        "bot_interaction_revision_positive": "installation_revision >= 1",
        "bot_interaction_one_installation": (
            "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int "
            "+ (dm_capability_id IS NOT NULL)::int = 1"
        ),
        "bot_interaction_guild_ref_complete": "(guild_id IS NULL) = (guild_domain IS NULL)",
        "bot_interaction_context_matches_guild": "(context = 'guild') = (guild_id IS NOT NULL)",
        "bot_interaction_integration_matches_installation": (
            "(integration_type = 'guild_install') = (installation_id IS NOT NULL)"
        ),
        "bot_interaction_integration_matches_user_installation": (
            "(integration_type = 'user_install') = (user_installation_id IS NOT NULL)"
        ),
        "bot_interaction_integration_matches_dm_capability": (
            "(integration_type = 'dm_capability') = (dm_capability_id IS NOT NULL)"
        ),
        "bot_interaction_dm_capability_context": ("dm_capability_id IS NULL OR context = 'bot_dm'"),
        "bot_interaction_command_name_context": (
            "(interaction_type IN ('command','autocomplete')) = (command_name IS NOT NULL)"
        ),
        "bot_interaction_command_id_context": (
            "(interaction_type IN ('command','autocomplete')) = (command_id IS NOT NULL)"
        ),
        "bot_interaction_message_ref_complete": "(message_id IS NULL) = (message_domain IS NULL)",
        "bot_interaction_token_hash_length": "token_hash IS NULL OR octet_length(token_hash) = 32",
        "bot_interaction_dispatch_fingerprint_length": (
            "dispatch_fingerprint IS NULL OR octet_length(dispatch_fingerprint) = 32"
        ),
        "bot_interaction_response_grant_length": (
            "response_grant_id IS NULL OR char_length(response_grant_id) BETWEEN 32 AND 64"
        ),
        "bot_interaction_remote_request_binding": (
            "(response_grant_id IS NULL) = (request_fingerprint IS NULL)"
        ),
        "bot_interaction_request_fingerprint_length": (
            "request_fingerprint IS NULL OR char_length(request_fingerprint) = 64"
        ),
    }
    for name, expression in interaction_checks.items():
        op.create_check_constraint(
            op.f(f"ck_bot_interactions_{name}"), "bot_interactions", expression
        )
    op.create_index(
        "uq_bot_interactions_response_grant",
        "bot_interactions",
        ["response_grant_id"],
        unique=True,
        postgresql_where=sa.text("response_grant_id IS NOT NULL"),
    )

    op.create_table(
        "bot_interaction_responses",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("response_type", sa.Integer(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=_json_default("{}"), nullable=False
        ),
        sa.Column("ephemeral", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_interaction_responses")),
        sa.ForeignKeyConstraint(
            ["interaction_id"],
            ["bot_interactions.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interaction_responses_interaction_id_bot_interactions"),
        ),
        sa.UniqueConstraint("interaction_id", "sequence", name="uq_interaction_response_sequence"),
        sa.CheckConstraint(
            "sequence >= 0",
            name=op.f("ck_bot_interaction_responses_interaction_response_sequence_nonnegative"),
        ),
        sa.CheckConstraint(
            "response_type IN (1,4,5,6,7,8,9,10)",
            name=op.f("ck_bot_interaction_responses_interaction_response_type_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name=op.f("ck_bot_interaction_responses_response_payload_is_object"),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_bot_interaction_responses_response_revision_positive"),
        ),
        sa.CheckConstraint(
            "(message_id IS NULL) = (message_domain IS NULL)",
            name=op.f("ck_bot_interaction_responses_interaction_response_message_ref_complete"),
        ),
        sa.CheckConstraint(
            "NOT ephemeral OR message_id IS NULL",
            name=op.f("ck_bot_interaction_responses_ephemeral_response_is_not_channel_message"),
        ),
    )
    op.create_index(
        "ix_interaction_responses_interaction",
        "bot_interaction_responses",
        ["interaction_id", "sequence"],
    )
    op.create_table(
        "federated_interaction_response_locators",
        sa.Column("response_id", sa.BigInteger(), nullable=False),
        sa.Column("response_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("interaction_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("response_type", sa.Integer(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("event_fingerprint", sa.String(64), nullable=False),
        sa.Column("deleted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "response_id",
            "response_domain",
            name=op.f("pk_federated_interaction_response_locators"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_federated_interaction_response_locators_user_id_user_domain_users"),
        ),
        sa.CheckConstraint(
            "response_domain = interaction_domain",
            name=op.f("ck_federated_interaction_response_locators_response_authority_matches"),
        ),
        sa.CheckConstraint(
            "sequence >= 0",
            name=op.f(
                "ck_federated_interaction_response_locators_response_locator_sequence_nonnegative"
            ),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f(
                "ck_federated_interaction_response_locators_response_locator_revision_positive"
            ),
        ),
        sa.CheckConstraint(
            "char_length(event_fingerprint) = 64",
            name=op.f(
                "ck_federated_interaction_response_locators_"
                "response_locator_event_fingerprint_length"
            ),
        ),
    )
    op.create_index(
        "ix_interaction_response_locators_invoker",
        "federated_interaction_response_locators",
        ["user_id", "user_domain", "interaction_id", "interaction_domain"],
    )
    op.create_table(
        "federated_interaction_admission_grants",
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("authority_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("interaction_id", sa.BigInteger()),
        sa.Column("interaction_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_federated_interaction_admission_grants")),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_federated_interaction_admission_grants_user_id_user_domain_users"),
        ),
        sa.UniqueConstraint(
            "interaction_id",
            "interaction_domain",
            name="uq_interaction_admission_grant_interaction_ref",
        ),
        sa.CheckConstraint(
            "char_length(grant_id) BETWEEN 32 AND 64",
            name=op.f(
                "ck_federated_interaction_admission_grants_interaction_admission_grant_id_length"
            ),
        ),
        sa.CheckConstraint(
            "authority_domain = channel_domain",
            name=op.f(
                "ck_federated_interaction_admission_grants_"
                "interaction_admission_grant_channel_authority"
            ),
        ),
        sa.CheckConstraint(
            "(interaction_id IS NULL) = (interaction_domain IS NULL)",
            name=op.f(
                "ck_federated_interaction_admission_grants_"
                "interaction_admission_grant_interaction_ref_complete"
            ),
        ),
    )
    op.create_index(
        "ix_interaction_admission_grants_expiry",
        "federated_interaction_admission_grants",
        ["expires_at"],
    )
    op.create_table(
        "interaction_dispatch_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("interaction_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("response_id", sa.BigInteger(), nullable=False),
        sa.Column("response_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(8), nullable=False),
        sa.Column("event_origin_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("event_id", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_interaction_dispatch_outbox")),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_interaction_dispatch_outbox_user_id_user_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["event_origin_domain", "event_id"],
            ["federation_events.origin_domain", "federation_events.event_id"],
            name=op.f("fk_interaction_dispatch_outbox_event_ref_federation_events"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "response_id",
            "response_domain",
            "revision",
            name="uq_interaction_dispatch_response_revision",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f("ck_interaction_dispatch_outbox_interaction_dispatch_revision_positive"),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f("ck_interaction_dispatch_outbox_interaction_dispatch_attempts_nonnegative"),
        ),
        sa.CheckConstraint(
            "operation IN ('CREATE','UPDATE','DELETE')",
            name=op.f("ck_interaction_dispatch_outbox_interaction_dispatch_operation_value"),
        ),
        sa.CheckConstraint(
            "(event_origin_domain IS NULL) = (event_id IS NULL)",
            name=op.f("ck_interaction_dispatch_outbox_interaction_dispatch_event_ref_complete"),
        ),
    )
    op.create_index(
        "ix_interaction_dispatch_due",
        "interaction_dispatch_outbox",
        ["next_attempt_at", "id"],
    )
    op.create_table(
        "interaction_create_dispatch_outbox",
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("topic", sa.String(768), nullable=False),
        sa.Column("audience_user_ref", sa.String(320), nullable=False),
        sa.Column("event_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("event_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "interaction_id", name=op.f("pk_interaction_create_dispatch_outbox")
        ),
        sa.ForeignKeyConstraint(
            ["interaction_id"],
            ["bot_interactions.id"],
            name=op.f("fk_interaction_create_dispatch_outbox_interaction_id_bot_interactions"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "octet_length(event_ciphertext) BETWEEN 29 AND 1048605",
            name=op.f(
                "ck_interaction_create_dispatch_outbox_"
                "interaction_create_dispatch_ciphertext_length"
            ),
        ),
        sa.CheckConstraint(
            "char_length(audience_user_ref) BETWEEN 3 AND 320",
            name=op.f(
                "ck_interaction_create_dispatch_outbox_interaction_create_dispatch_audience_length"
            ),
        ),
        sa.CheckConstraint(
            "octet_length(event_fingerprint) = 32",
            name=op.f(
                "ck_interaction_create_dispatch_outbox_"
                "interaction_create_dispatch_fingerprint_length"
            ),
        ),
        sa.CheckConstraint(
            "attempts >= 0",
            name=op.f(
                "ck_interaction_create_dispatch_outbox_"
                "interaction_create_dispatch_attempts_nonnegative"
            ),
        ),
    )
    op.create_index(
        "ix_interaction_create_dispatch_due",
        "interaction_create_dispatch_outbox",
        ["next_attempt_at", "interaction_id"],
    )
    op.create_table(
        "bot_interaction_polls",
        sa.Column("response_id", sa.BigInteger(), nullable=False),
        sa.Column("question", postgresql.JSONB(), nullable=False),
        sa.Column("allow_multiselect", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("layout_type", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("response_id", name=op.f("pk_bot_interaction_polls")),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["bot_interaction_responses.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interaction_polls_response_id_bot_interaction_responses"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(question) = 'object'",
            name=op.f("ck_bot_interaction_polls_question_is_object"),
        ),
        sa.CheckConstraint(
            "layout_type = 1",
            name=op.f("ck_bot_interaction_polls_layout_type_value"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_bot_interaction_polls_positive_duration"),
        ),
    )
    op.create_table(
        "bot_interaction_poll_answers",
        sa.Column("response_id", sa.BigInteger(), nullable=False),
        sa.Column("answer_id", sa.SmallInteger(), nullable=False),
        sa.Column("text", sa.String(55)),
        sa.Column("emoji", postgresql.JSONB(none_as_null=True)),
        sa.PrimaryKeyConstraint(
            "response_id", "answer_id", name=op.f("pk_bot_interaction_poll_answers")
        ),
        sa.ForeignKeyConstraint(
            ["response_id"],
            ["bot_interaction_polls.response_id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_interaction_poll_answers_response_id_bot_interaction_polls"),
        ),
        sa.CheckConstraint(
            "answer_id BETWEEN 1 AND 10",
            name=op.f("ck_bot_interaction_poll_answers_answer_id_range"),
        ),
        sa.CheckConstraint(
            "text IS NOT NULL OR emoji IS NOT NULL",
            name=op.f("ck_bot_interaction_poll_answers_answer_has_body"),
        ),
        sa.CheckConstraint(
            "emoji IS NULL OR jsonb_typeof(emoji) = 'object'",
            name=op.f("ck_bot_interaction_poll_answers_emoji_is_object"),
        ),
    )
    op.create_table(
        "bot_interaction_poll_votes",
        sa.Column("response_id", sa.BigInteger(), nullable=False),
        sa.Column("answer_id", sa.SmallInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "response_id",
            "answer_id",
            "user_id",
            "user_domain",
            name=op.f("pk_bot_interaction_poll_votes"),
        ),
        sa.ForeignKeyConstraint(
            ["response_id", "answer_id"],
            [
                "bot_interaction_poll_answers.response_id",
                "bot_interaction_poll_answers.answer_id",
            ],
            ondelete="CASCADE",
            name=op.f(
                "fk_bot_interaction_poll_votes_response_id_answer_id_bot_interaction_poll_answers"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_interaction_poll_votes_user_id_user_domain_users"),
        ),
    )
    op.create_index(
        "ix_bot_interaction_poll_votes_voter",
        "bot_interaction_poll_votes",
        ["user_id", "user_domain", "response_id"],
    )
    op.add_column("attachments", sa.Column("interaction_id", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_attachments_interaction_id_bot_interactions"),
        "attachments",
        "bot_interactions",
        ["interaction_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column("attachments", sa.Column("interaction_response_id", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_attachments_interaction_response_id_bot_interaction_responses"),
        "attachments",
        "bot_interaction_responses",
        ["interaction_response_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_attachments_message_or_interaction_owner_exclusive"),
        "attachments",
        "(message_id IS NOT NULL)::int + (interaction_id IS NOT NULL)::int + "
        "(interaction_response_id IS NOT NULL)::int <= 1",
    )
    op.create_check_constraint(
        op.f("ck_attachments_interaction_attachment_policy"),
        "attachments",
        "interaction_id IS NULL OR report_id IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_attachments_interaction_response_attachment_policy"),
        "attachments",
        "interaction_response_id IS NULL OR report_id IS NULL",
    )
    op.create_index(
        "ix_attachments_interaction",
        "attachments",
        ["interaction_id"],
    )
    op.create_index(
        "ix_attachments_interaction_response",
        "attachments",
        ["interaction_response_id"],
    )
    op.create_table(
        "federated_interaction_attachment_grants",
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("destination_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("interaction_id", sa.BigInteger()),
        sa.Column("interaction_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("metadata_fingerprint", sa.String(64), nullable=False),
        sa.Column("admission_grant_id", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "grant_id",
            name=op.f("pk_federated_interaction_attachment_grants"),
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id", "attachment_domain"],
            ["attachments.id", "attachments.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_federated_interaction_attachment_grants_attachment_id_attachment_domain_attachments"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_federated_interaction_attachment_grants_user_id_user_domain_users"),
        ),
        sa.UniqueConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
            name="uq_interaction_attachment_grant_destination",
        ),
        sa.CheckConstraint(
            "(interaction_id IS NULL) = (interaction_domain IS NULL)",
            name=op.f(
                "ck_federated_interaction_attachment_grants_"
                "interaction_attachment_grant_interaction_ref_complete"
            ),
        ),
        sa.CheckConstraint(
            "char_length(metadata_fingerprint) = 64",
            name=op.f(
                "ck_federated_interaction_attachment_grants_"
                "interaction_attachment_grant_fingerprint_length"
            ),
        ),
        sa.CheckConstraint(
            "admission_grant_id IS NULL OR char_length(admission_grant_id) BETWEEN 32 AND 64",
            name=op.f(
                "ck_federated_interaction_attachment_grants_"
                "interaction_attachment_admission_grant_length"
            ),
        ),
    )
    op.create_index(
        "ix_interaction_attachment_grants_expiry",
        "federated_interaction_attachment_grants",
        ["expires_at", "destination_domain"],
    )

    op.create_table(
        "bot_dm_grants",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("installation_id", sa.BigInteger()),
        sa.Column("user_installation_id", sa.BigInteger()),
        sa.Column("granted_by_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_by_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("consent_state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("scopes", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False),
        sa.Column("consent_generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("history_floor_message_id", sa.BigInteger()),
        sa.Column("history_floor_message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_dm_grants")),
        sa.ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_grants_conversation_id_conversation_domain_dm_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_grants_application_id_application_domain_bot_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["bot_installations.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_grants_installation_id_bot_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["user_installation_id"],
            ["bot_user_installations.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_grants_user_installation_id_bot_user_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id", "granted_by_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_dm_grants_granted_by_id_granted_by_domain_users"),
        ),
        sa.UniqueConstraint(
            "conversation_id",
            "conversation_domain",
            "application_id",
            "application_domain",
            name="uq_bot_dm_grant_application_conversation",
        ),
        sa.CheckConstraint(
            "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int = 1",
            name=op.f("ck_bot_dm_grants_bot_dm_grant_one_installation"),
        ),
        sa.CheckConstraint(
            "consent_state IN ('pending','active','revoked')",
            name=op.f("ck_bot_dm_grants_bot_dm_consent_state_value"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(scopes) = 'array'", name=op.f("ck_bot_dm_grants_bot_dm_scopes_are_array")
        ),
        sa.CheckConstraint(
            "consent_generation >= 1",
            name=op.f("ck_bot_dm_grants_bot_dm_consent_generation_positive"),
        ),
        sa.CheckConstraint(
            "(history_floor_message_id IS NULL) = (history_floor_message_domain IS NULL)",
            name=op.f("ck_bot_dm_grants_bot_dm_history_floor_complete"),
        ),
    )

    op.create_table(
        "bot_dm_grant_consents",
        sa.Column("grant_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("consent_generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "consented_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint(
            "grant_id",
            "user_id",
            "user_domain",
            name=op.f("pk_bot_dm_grant_consents"),
        ),
        sa.ForeignKeyConstraint(
            ["grant_id"],
            ["bot_dm_grants.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_grant_consents_grant_id_bot_dm_grants"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_dm_grant_consents_user_id_user_domain_users"),
        ),
        sa.CheckConstraint(
            "consent_generation >= 1",
            name=op.f("ck_bot_dm_grant_consents_bot_dm_consent_generation_positive"),
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked')",
            name=op.f("ck_bot_dm_grant_consents_bot_dm_consent_status_value"),
        ),
    )
    op.create_index(
        "ix_bot_dm_grant_consents_user",
        "bot_dm_grant_consents",
        ["user_id", "user_domain", "status"],
    )

    op.create_table(
        "bot_e2ee_devices",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.BigInteger()),
        sa.Column("source_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("protocol_id", sa.String(64), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("worker_id", sa.BigInteger(), nullable=False),
        sa.Column("identity_key", sa.LargeBinary(), nullable=False),
        sa.Column("credential", sa.LargeBinary(), nullable=False),
        sa.Column(
            "capabilities", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False
        ),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("trust_state", sa.String(16), server_default="pending", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_e2ee_devices")),
        sa.ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_devices_application_id_application_domain_bot_applications"),
        ),
        sa.ForeignKeyConstraint(
            ["worker_id"],
            ["bot_workers.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_devices_worker_id_bot_workers"),
        ),
        sa.UniqueConstraint("source_id", "source_domain", name="uq_bot_e2ee_device_source"),
        sa.UniqueConstraint("protocol_id", name="uq_bot_e2ee_device_protocol_id"),
        sa.CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_device_source_ref_complete"),
        ),
        sa.CheckConstraint(
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_device_source_authority"),
        ),
        sa.CheckConstraint(
            "protocol_id ~ '^kbe_[A-Za-z0-9_-]{43}$'",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_device_protocol_id_format"),
        ),
        sa.CheckConstraint(
            "octet_length(identity_key) = 32",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_identity_key_length"),
        ),
        sa.CheckConstraint(
            "octet_length(credential) BETWEEN 1 AND 16384",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_credential_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_capabilities_array"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_device_generation_positive"),
        ),
        sa.CheckConstraint(
            "trust_state IN ('pending','trusted','rejected','revoked')",
            name=op.f("ck_bot_e2ee_devices_bot_e2ee_trust_state_value"),
        ),
    )
    op.create_index(
        "uq_bot_e2ee_worker_active_device",
        "bot_e2ee_devices",
        ["application_id", "application_domain", "worker_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "bot_e2ee_key_packages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("cipher_suite", sa.String(96), nullable=False),
        sa.Column("package", sa.LargeBinary(), nullable=False),
        sa.Column("package_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_for_ref", sa.String(320)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_e2ee_key_packages")),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["bot_e2ee_devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_key_packages_device_id_bot_e2ee_devices"),
        ),
        sa.UniqueConstraint("device_id", "package_hash", name="uq_bot_e2ee_key_package_digest"),
        sa.CheckConstraint(
            "octet_length(package) BETWEEN 1 AND 32768",
            name=op.f("ck_bot_e2ee_key_packages_bot_e2ee_key_package_length"),
        ),
        sa.CheckConstraint(
            "octet_length(package_hash) = 32",
            name=op.f("ck_bot_e2ee_key_packages_bot_e2ee_key_package_hash_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_bot_e2ee_key_packages_bot_e2ee_key_package_expiry"),
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claimed_for_ref IS NULL)",
            name=op.f("ck_bot_e2ee_key_packages_bot_e2ee_key_package_claim_complete"),
        ),
    )
    op.create_index(
        "ix_bot_e2ee_key_packages_available",
        "bot_e2ee_key_packages",
        ["device_id", "expires_at"],
        postgresql_where=sa.text("claimed_at IS NULL"),
    )
    op.create_table(
        "bot_e2ee_participations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("installation_id", sa.BigInteger()),
        sa.Column("dm_grant_id", sa.BigInteger()),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("consenting_actor_id", sa.BigInteger(), nullable=False),
        sa.Column("consenting_actor_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("consent_generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("joined_epoch", sa.BigInteger(), nullable=False),
        sa.Column("history_floor_message_id", sa.BigInteger()),
        sa.Column("history_floor_message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_e2ee_participations")),
        sa.ForeignKeyConstraint(
            ["installation_id"],
            ["bot_installations.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_participations_installation_id_bot_installations"),
        ),
        sa.ForeignKeyConstraint(
            ["dm_grant_id"],
            ["bot_dm_grants.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_participations_dm_grant_id_bot_dm_grants"),
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_participations_channel_id_channel_domain_channels"),
        ),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["bot_e2ee_devices.id"],
            ondelete="CASCADE",
            name=op.f("fk_bot_e2ee_participations_device_id_bot_e2ee_devices"),
        ),
        sa.ForeignKeyConstraint(
            ["consenting_actor_id", "consenting_actor_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f(
                "fk_bot_e2ee_participations_consenting_actor_id_consenting_actor_domain_users"
            ),
        ),
        sa.UniqueConstraint(
            "installation_id",
            "channel_id",
            "channel_domain",
            "device_id",
            name="uq_bot_e2ee_installation_channel_device",
        ),
        sa.UniqueConstraint(
            "dm_grant_id",
            "channel_id",
            "channel_domain",
            "device_id",
            name="uq_bot_e2ee_dm_grant_channel_device",
        ),
        sa.CheckConstraint(
            "(installation_id IS NOT NULL)::int + (dm_grant_id IS NOT NULL)::int = 1",
            name=op.f("ck_bot_e2ee_participations_bot_e2ee_participation_one_consent"),
        ),
        sa.CheckConstraint(
            "consent_generation >= 1",
            name=op.f("ck_bot_e2ee_participations_bot_e2ee_consent_generation_positive"),
        ),
        sa.CheckConstraint(
            "joined_epoch >= 0",
            name=op.f("ck_bot_e2ee_participations_bot_e2ee_joined_epoch_nonnegative"),
        ),
        sa.CheckConstraint(
            "(history_floor_message_id IS NULL) = (history_floor_message_domain IS NULL)",
            name=op.f("ck_bot_e2ee_participations_bot_e2ee_history_floor_complete"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','active','revoked')",
            name=op.f("ck_bot_e2ee_participations_bot_e2ee_participation_status_value"),
        ),
    )


def _create_guild_setting_foreign_keys() -> None:
    for prefix in (
        "afk",
        "system",
        "rules",
        "public_updates",
        "safety_alerts",
    ):
        op.create_foreign_key(
            f"fk_guilds_{prefix}_channel_ref",
            "guilds",
            "channels",
            [f"{prefix}_channel_id", f"{prefix}_channel_domain", "id", "origin_domain"],
            ["id", "origin_domain", "guild_id", "guild_domain"],
            deferrable=True,
            initially="DEFERRED",
        )


def _create_bot_dm_capability_table() -> None:
    op.create_table(
        "bot_dm_capabilities",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_installation_id", sa.BigInteger(), nullable=False),
        sa.Column("source_installation_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("application_id", sa.BigInteger(), nullable=False),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("bot_user_id", sa.BigInteger(), nullable=False),
        sa.Column("bot_user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("guild_id", sa.BigInteger()),
        sa.Column("guild_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("installing_user_id", sa.BigInteger()),
        sa.Column("installing_user_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("target_user_id", sa.BigInteger(), nullable=False),
        sa.Column("target_user_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("pair_key", sa.String(64), nullable=False),
        sa.Column("authority_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("conversation_id", sa.BigInteger()),
        sa.Column("conversation_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column(
            "granted_scopes", postgresql.JSONB(), server_default=_json_default("[]"), nullable=False
        ),
        sa.Column(
            "granted_intents",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column(
            "channel_restrictions",
            postgresql.JSONB(),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column("e2ee_mode", sa.String(24), server_default="disabled", nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "target_access_revocation_generation",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("admission_revision", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("proof_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("proof", postgresql.JSONB(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("media_bytes_used", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("media_pending_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bot_dm_capabilities")),
        sa.ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
            name=op.f(
                "fk_bot_dm_capabilities_conversation_id_conversation_domain_dm_conversations"
            ),
        ),
        sa.ForeignKeyConstraint(
            ["bot_user_id", "bot_user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_dm_capabilities_bot_user_id_bot_user_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["installing_user_id", "installing_user_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_bot_dm_capabilities_installing_user_id_installing_user_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["target_user_id", "target_user_domain"],
            ["users.id", "users.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_bot_dm_capabilities_target_user_id_target_user_domain_users"),
        ),
        sa.ForeignKeyConstraint(
            ["source_installation_domain"],
            ["instances.domain"],
            name=op.f("fk_bot_dm_capabilities_source_installation_domain_instances"),
        ),
        sa.ForeignKeyConstraint(
            ["authority_domain"],
            ["instances.domain"],
            name=op.f("fk_bot_dm_capabilities_authority_domain_instances"),
        ),
        sa.CheckConstraint(
            "(conversation_id IS NULL) = (conversation_domain IS NULL)",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_conversation_ref_complete"),
        ),
        sa.CheckConstraint(
            "conversation_domain IS NULL OR conversation_domain = authority_domain",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_conversation_authority"),
        ),
        sa.CheckConstraint(
            "(source_kind = 'guild' AND guild_id IS NOT NULL AND guild_domain IS NOT NULL "
            "AND installing_user_id IS NULL AND installing_user_domain IS NULL) OR "
            "(source_kind = 'user' AND guild_id IS NULL AND guild_domain IS NULL "
            "AND installing_user_id = target_user_id "
            "AND installing_user_domain = target_user_domain)",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_source_context"),
        ),
        sa.CheckConstraint(
            "grant_id ~ '^kbdg_[A-Za-z0-9_-]{43}$'",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_grant_id_format"),
        ),
        sa.CheckConstraint(
            "pair_key ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_pair_key_format"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(granted_scopes) = 'array' "
            "AND jsonb_typeof(granted_intents) = 'array' "
            "AND jsonb_typeof(channel_restrictions) = 'array' "
            "AND jsonb_typeof(proof) = 'object'",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_json_shapes"),
        ),
        sa.CheckConstraint(
            "e2ee_mode IN ('disabled','participant')",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_e2ee_mode_value"),
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_status_value"),
        ),
        sa.CheckConstraint(
            "revision >= 1 AND admission_revision >= 1 AND admission_revision <= revision "
            "AND target_access_revocation_generation >= 0 "
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0 "
            "AND octet_length(proof_fingerprint) = 32 AND expires_at > created_at",
            name=op.f("ck_bot_dm_capabilities_bot_dm_capability_positive_values"),
        ),
        sa.UniqueConstraint("grant_id", name="uq_bot_dm_capability_grant_id"),
        sa.UniqueConstraint(
            "source_kind",
            "source_installation_id",
            "source_installation_domain",
            "pair_key",
            "authority_domain",
            name="uq_bot_dm_capability_source_pair_authority",
        ),
    )
    op.create_index(
        "ix_bot_dm_capabilities_application_active",
        "bot_dm_capabilities",
        ["application_id", "application_domain", "bot_user_id", "bot_user_domain", "status"],
    )
    op.create_index(
        "ix_bot_dm_capabilities_conversation_active",
        "bot_dm_capabilities",
        ["conversation_id", "conversation_domain", "status"],
    )
    op.create_table(
        "bot_dm_capability_highwaters",
        sa.Column("grant_id", sa.String(64), nullable=False),
        sa.Column("installation_authority_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("identity_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("authorization_fingerprint", sa.LargeBinary(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("grant_id", name=op.f("pk_bot_dm_capability_highwaters")),
        sa.CheckConstraint(
            "grant_id ~ '^kbdg_[A-Za-z0-9_-]{43}$'",
            name=op.f(
                "ck_bot_dm_capability_highwaters_bot_dm_capability_highwater_grant_id_format"
            ),
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name=op.f(
                "ck_bot_dm_capability_highwaters_bot_dm_capability_highwater_revision_positive"
            ),
        ),
        sa.CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name=op.f("ck_bot_dm_capability_highwaters_bot_dm_capability_highwater_status_value"),
        ),
        sa.CheckConstraint(
            "octet_length(identity_fingerprint) = 32 "
            "AND octet_length(authorization_fingerprint) = 32",
            name=op.f("ck_bot_dm_capability_highwaters_bot_dm_capability_highwater_fingerprints"),
        ),
    )
    op.create_index(
        "ix_bot_dm_capability_highwaters_authority_expiry",
        "bot_dm_capability_highwaters",
        ["installation_authority_domain", "expires_at"],
    )


def _wire_bot_dm_capability_references() -> None:
    op.create_foreign_key(
        op.f("fk_bot_interactions_dm_capability_id_bot_dm_capabilities"),
        "bot_interactions",
        "bot_dm_capabilities",
        ["dm_capability_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.add_column("bot_dm_grants", sa.Column("dm_capability_id", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_bot_dm_grants_dm_capability_id_bot_dm_capabilities"),
        "bot_dm_grants",
        "bot_dm_capabilities",
        ["dm_capability_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        op.f("ck_bot_dm_grants_bot_dm_grant_one_installation"),
        "bot_dm_grants",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_bot_dm_grants_bot_dm_grant_one_installation"),
        "bot_dm_grants",
        "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int + "
        "(dm_capability_id IS NOT NULL)::int = 1",
    )
    op.add_column("attachments", sa.Column("bot_dm_capability_id", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_attachments_bot_dm_capability_id_bot_dm_capabilities"),
        "attachments",
        "bot_dm_capabilities",
        ["bot_dm_capability_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        "(bot_installation_id IS NOT NULL)::int + "
        "(bot_user_installation_id IS NOT NULL)::int + "
        "(bot_dm_capability_id IS NOT NULL)::int <= 1",
    )
    op.create_index(
        "ix_attachments_bot_dm_capability_usage",
        "attachments",
        ["bot_dm_capability_id"],
    )


def _bind_bot_tokens_to_dm_capabilities() -> None:
    op.add_column("bot_tokens", sa.Column("dm_capability_id", sa.BigInteger()))
    op.add_column("bot_tokens", sa.Column("dm_capability_revision", sa.BigInteger()))
    op.create_foreign_key(
        op.f("fk_bot_tokens_dm_capability_id_bot_dm_capabilities"),
        "bot_tokens",
        "bot_dm_capabilities",
        ["dm_capability_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        op.f("ck_bot_tokens_bot_token_dm_capability_binding"),
        "bot_tokens",
        "(dm_capability_id IS NULL) = (dm_capability_revision IS NULL) "
        "AND (dm_capability_revision IS NULL OR dm_capability_revision >= 1)",
    )
    op.create_index(
        "ix_bot_tokens_dm_capability",
        "bot_tokens",
        ["dm_capability_id", "expires_at"],
    )


def _create_encrypted_forum_starter_reservations() -> None:
    op.create_table(
        "encrypted_forum_starter_reservations",
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=False),
        sa.Column("parent_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("claimant_kind", sa.String(16), nullable=False),
        sa.Column("claimant_id", sa.BigInteger(), nullable=False),
        sa.Column("claimant_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("worker_id", sa.BigInteger()),
        sa.Column("claimant_device_id", sa.String(48)),
        sa.Column("application_id", sa.BigInteger()),
        sa.Column("application_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("installation_type", sa.String(24)),
        sa.Column("installation_id", sa.BigInteger()),
        sa.Column("installation_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("installation_revision", sa.BigInteger()),
        sa.Column("webhook_id", sa.BigInteger()),
        sa.Column("webhook_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("client_nonce", sa.String(64), nullable=False),
        sa.Column("reservation_key", sa.LargeBinary(32), nullable=False),
        sa.Column("request_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("claim_request_hash", sa.LargeBinary(32)),
        sa.Column("claimed_message_id", sa.BigInteger()),
        sa.Column("claimed_message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint(
            "thread_id",
            "thread_domain",
            name=op.f("pk_encrypted_forum_starter_reservations"),
        ),
        sa.UniqueConstraint(
            "reservation_key",
            name="uq_encrypted_forum_starter_reservation_key",
        ),
        sa.ForeignKeyConstraint(
            ["thread_id", "thread_domain"],
            ["channels.id", "channels.origin_domain"],
            name=op.f("fk_encrypted_forum_starter_reservations_thread_ref_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id", "parent_domain"],
            ["channels.id", "channels.origin_domain"],
            name=op.f("fk_encrypted_forum_starter_reservations_parent_ref_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claimed_message_id", "claimed_message_domain"],
            ["messages.id", "messages.origin_domain"],
            name=op.f("fk_encrypted_forum_starter_reservations_claimed_message_ref_messages"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "claimant_kind IN ('human','bot','webhook')",
            name=op.f("ck_encrypted_forum_starter_reservations_claimant_kind_value"),
        ),
        sa.CheckConstraint(
            "(application_id IS NULL) = (application_domain IS NULL)",
            name=op.f("ck_encrypted_forum_starter_reservations_application_ref_complete"),
        ),
        sa.CheckConstraint(
            "(installation_type IS NULL) = (installation_id IS NULL) "
            "AND (installation_id IS NULL) = (installation_domain IS NULL) "
            "AND (installation_id IS NULL) = (installation_revision IS NULL)",
            name=op.f("ck_encrypted_forum_starter_reservations_installation_lineage_complete"),
        ),
        sa.CheckConstraint(
            "installation_type IS NULL OR installation_type IN "
            "('guild_install','user_install','dm_capability','webhook')",
            name=op.f("ck_encrypted_forum_starter_reservations_installation_type_value"),
        ),
        sa.CheckConstraint(
            "installation_revision IS NULL OR installation_revision >= 1",
            name=op.f("ck_encrypted_forum_starter_reservations_installation_revision_positive"),
        ),
        sa.CheckConstraint(
            "(webhook_id IS NULL) = (webhook_domain IS NULL)",
            name=op.f("ck_encrypted_forum_starter_reservations_webhook_ref_complete"),
        ),
        sa.CheckConstraint(
            "(claimant_kind = 'human' AND worker_id IS NULL "
            "AND claimant_device_id IS NULL AND application_id IS NULL "
            "AND installation_id IS NULL AND webhook_id IS NULL) OR "
            "(claimant_kind = 'bot' AND application_id IS NOT NULL "
            "AND installation_id IS NOT NULL AND webhook_id IS NULL "
            "AND worker_id IS NOT NULL AND claimant_device_id IS NOT NULL) OR "
            "(claimant_kind = 'webhook' AND webhook_id IS NOT NULL)",
            name=op.f("ck_encrypted_forum_starter_reservations_claimant_lineage"),
        ),
        sa.CheckConstraint(
            "claimant_device_id IS NULL OR claimant_device_id ~ '^(kbe|kwe)_[A-Za-z0-9_-]{43}$'",
            name=op.f("ck_encrypted_forum_starter_reservations_claimant_device_id_format"),
        ),
        sa.CheckConstraint(
            "(claimed_message_id IS NULL) = (claimed_message_domain IS NULL) "
            "AND (claimed_at IS NULL) = (claimed_message_id IS NULL) "
            "AND (claim_request_hash IS NULL) = (claimed_message_id IS NULL) "
            "AND (claimed_message_id IS NULL OR "
            "(claimed_message_id = thread_id AND claimed_message_domain = thread_domain))",
            name=op.f("ck_encrypted_forum_starter_reservations_claim_state"),
        ),
        sa.CheckConstraint(
            "char_length(client_nonce) BETWEEN 1 AND 64 AND client_nonce ~ '^[A-Za-z0-9._:-]+$'",
            name=op.f("ck_encrypted_forum_starter_reservations_client_nonce_format"),
        ),
        sa.CheckConstraint(
            "octet_length(reservation_key) = 32 AND octet_length(request_hash) = 32 "
            "AND (claim_request_hash IS NULL OR octet_length(claim_request_hash) = 32)",
            name=op.f("ck_encrypted_forum_starter_reservations_digest_lengths"),
        ),
    )
    op.create_index(
        "ix_encrypted_forum_starter_reservations_parent",
        "encrypted_forum_starter_reservations",
        ["parent_id", "parent_domain"],
    )


def _create_webhook_e2ee_tables() -> None:
    op.create_unique_constraint(
        "uq_webhooks_ref_domain",
        "webhooks",
        ["id", "guild_domain"],
    )
    op.create_table(
        "webhook_e2ee_devices",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("protocol_id", sa.String(64), nullable=False),
        sa.Column("identity_key", sa.LargeBinary(32), nullable=False),
        sa.Column("credential", sa.LargeBinary(), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=_json_default("[]"),
            nullable=False,
        ),
        sa.Column("generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("trust_state", sa.String(16), server_default="trusted", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_e2ee_devices")),
        sa.ForeignKeyConstraint(
            ["webhook_id", "webhook_domain"],
            ["webhooks.id", "webhooks.guild_domain"],
            name=op.f("fk_webhook_e2ee_devices_webhook_ref_webhooks"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("protocol_id", name="uq_webhook_e2ee_devices_protocol_id"),
        sa.UniqueConstraint(
            "id", "webhook_id", "webhook_domain", name="uq_webhook_e2ee_devices_lineage"
        ),
        sa.CheckConstraint(
            "protocol_id ~ '^kwe_[A-Za-z0-9_-]{43}$'",
            name=op.f("ck_webhook_e2ee_devices_protocol_id_format"),
        ),
        sa.CheckConstraint(
            "octet_length(identity_key) = 32",
            name=op.f("ck_webhook_e2ee_devices_identity_key_length"),
        ),
        sa.CheckConstraint(
            "octet_length(credential) BETWEEN 1 AND 16384",
            name=op.f("ck_webhook_e2ee_devices_credential_length"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name=op.f("ck_webhook_e2ee_devices_capabilities_array"),
        ),
        sa.CheckConstraint(
            "generation >= 1",
            name=op.f("ck_webhook_e2ee_devices_generation_positive"),
        ),
        sa.CheckConstraint(
            "trust_state IN ('trusted','revoked')",
            name=op.f("ck_webhook_e2ee_devices_trust_state_value"),
        ),
    )
    op.create_index(
        "uq_webhook_e2ee_devices_active_webhook",
        "webhook_e2ee_devices",
        ["webhook_id", "webhook_domain"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "webhook_e2ee_key_packages",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("cipher_suite", sa.String(96), nullable=False),
        sa.Column("package", sa.LargeBinary(), nullable=False),
        sa.Column("package_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_operation_id", sa.String(64)),
        sa.Column("claimed_operation_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_e2ee_key_packages")),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["webhook_e2ee_devices.id"],
            name=op.f("fk_webhook_e2ee_key_packages_device_id_webhook_e2ee_devices"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "device_id", "package_hash", name="uq_webhook_e2ee_key_packages_digest"
        ),
        sa.CheckConstraint(
            "octet_length(package) BETWEEN 1 AND 32768",
            name=op.f("ck_webhook_e2ee_key_packages_package_length"),
        ),
        sa.CheckConstraint(
            "octet_length(package_hash) = 32",
            name=op.f("ck_webhook_e2ee_key_packages_package_hash_length"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name=op.f("ck_webhook_e2ee_key_packages_expiry_after_creation"),
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claimed_operation_id IS NULL) "
            "AND (claimed_operation_id IS NULL) = (claimed_operation_domain IS NULL)",
            name=op.f("ck_webhook_e2ee_key_packages_claim_complete"),
        ),
    )
    op.create_index(
        "ix_webhook_e2ee_key_packages_available",
        "webhook_e2ee_key_packages",
        ["device_id", "expires_at"],
        postgresql_where=sa.text("claimed_at IS NULL"),
    )
    op.create_table(
        "webhook_e2ee_participations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_id", sa.BigInteger(), nullable=False),
        sa.Column("webhook_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("device_id", sa.BigInteger(), nullable=False),
        sa.Column("consenting_actor_id", sa.BigInteger(), nullable=False),
        sa.Column("consenting_actor_domain", sa.String(DOMAIN_LENGTH), nullable=False),
        sa.Column("consent_generation", sa.BigInteger(), server_default="1", nullable=False),
        sa.Column("joined_epoch", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("history_floor_message_id", sa.BigInteger()),
        sa.Column("history_floor_message_domain", sa.String(DOMAIN_LENGTH)),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_e2ee_participations")),
        sa.ForeignKeyConstraint(
            ["webhook_id", "webhook_domain"],
            ["webhooks.id", "webhooks.guild_domain"],
            name=op.f("fk_webhook_e2ee_participations_webhook_ref_webhooks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "webhook_id", "webhook_domain"],
            [
                "webhook_e2ee_devices.id",
                "webhook_e2ee_devices.webhook_id",
                "webhook_e2ee_devices.webhook_domain",
            ],
            name=op.f("fk_webhook_e2ee_participations_device_lineage_webhook_e2ee_devices"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            name=op.f("fk_webhook_e2ee_participations_channel_ref_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["consenting_actor_id", "consenting_actor_domain"],
            ["users.id", "users.origin_domain"],
            name=op.f("fk_webhook_e2ee_participations_consenting_actor_ref_users"),
        ),
        sa.UniqueConstraint(
            "webhook_id",
            "webhook_domain",
            "channel_id",
            "channel_domain",
            "device_id",
            name="uq_webhook_e2ee_participations_device_channel",
        ),
        sa.CheckConstraint(
            "consent_generation >= 1",
            name=op.f("ck_webhook_e2ee_participations_consent_generation_positive"),
        ),
        sa.CheckConstraint(
            "joined_epoch >= 0",
            name=op.f("ck_webhook_e2ee_participations_joined_epoch_nonnegative"),
        ),
        sa.CheckConstraint(
            "(history_floor_message_id IS NULL) = (history_floor_message_domain IS NULL)",
            name=op.f("ck_webhook_e2ee_participations_history_floor_complete"),
        ),
        sa.CheckConstraint(
            "status IN ('pending','active','revoked')",
            name=op.f("ck_webhook_e2ee_participations_status_value"),
        ),
    )
    op.create_index(
        "ix_webhook_e2ee_participations_channel",
        "webhook_e2ee_participations",
        ["channel_id", "channel_domain", "status"],
    )


def _guard_feature_data_downgrade() -> None:
    """Refuse a downgrade that would silently discard foundation feature data."""

    op.execute(
        """
        DO $$
        DECLARE
            feature_table text;
            has_rows boolean;
        BEGIN
            FOREACH feature_table IN ARRAY ARRAY[
                'guild_scheduled_events',
                'stage_instances',
                'guild_scheduled_event_subscriptions',
                'polls',
                'poll_answers',
                'poll_votes',
                'message_views',
                'channel_follows',
                'message_crossposts',
                'federated_channel_follows',
                'federated_message_crossposts',
                'emoji_role_restrictions',
                'soundboard_sounds',
                'auto_mod_rules',
                'auto_mod_actions',
                'auto_mod_rule_exempt_roles',
                'auto_mod_rule_exempt_channels',
                'auto_mod_executions',
                'auto_mod_member_blocks',
                'application_command_permissions',
                'application_assets',
                'application_emojis',
                'bot_user_installations',
                'bot_application_runtime_highwaters',
                'bot_interaction_responses',
                'federated_interaction_response_locators',
                'federated_interaction_admission_grants',
                'interaction_dispatch_outbox',
                'interaction_create_dispatch_outbox',
                'bot_interaction_polls',
                'bot_interaction_poll_answers',
                'bot_interaction_poll_votes',
                'federated_interaction_attachment_grants',
                'bot_dm_grants',
                'bot_dm_grant_consents',
                'bot_e2ee_devices',
                'bot_e2ee_key_packages',
                'bot_e2ee_participations',
                'bot_dm_capabilities',
                'bot_dm_capability_highwaters',
                'encrypted_forum_starter_reservations',
                'webhook_e2ee_devices',
                'webhook_e2ee_key_packages',
                'webhook_e2ee_participations'
            ] LOOP
                EXECUTE format('SELECT EXISTS (SELECT 1 FROM %I)', feature_table)
                    INTO has_rows;
                IF has_rows THEN
                    RAISE EXCEPTION
                        'cannot downgrade fc9a4b7d2e10 while % contains feature data',
                        feature_table
                        USING ERRCODE = '23514',
                              HINT = 'export or deliberately remove the feature data first';
                END IF;
            END LOOP;

            IF EXISTS (
                SELECT 1 FROM developer_team_members WHERE NOT user_is_local
            ) OR EXISTS (
                SELECT 1 FROM developer_teams WHERE federation_revision <> 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while federated developer-team state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM users WHERE age_assurance_state <> 'unknown'
            ) OR EXISTS (
                SELECT 1 FROM user_settings WHERE age_restricted_dm_commands_enabled
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while age-assurance settings exist'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM guilds
                WHERE verification_level <> 0
                   OR default_message_notifications <> 0
                   OR explicit_content_filter <> 0
                   OR preferred_locale <> 'en-US'
                   OR afk_channel_id IS NOT NULL
                   OR afk_timeout <> 300
                   OR system_channel_id IS NOT NULL
                   OR system_channel_flags <> 0
                   OR rules_channel_id IS NOT NULL
                   OR public_updates_channel_id IS NOT NULL
                   OR safety_alerts_channel_id IS NOT NULL
                   OR community_enabled
                   OR invites_disabled_until IS NOT NULL
                   OR dms_disabled_until IS NOT NULL
            ) OR EXISTS (
                SELECT 1
                FROM guild_members
                WHERE temporary OR last_guild_activity_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while guild safety or lifecycle state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM channels
                WHERE type = 13
                   OR nsfw
                   OR voice_status IS NOT NULL
                   OR (
                        type = 2 AND (
                            bitrate IS DISTINCT FROM 64000
                            OR user_limit IS DISTINCT FROM 0
                            OR rtc_region IS NOT NULL
                            OR video_quality_mode IS DISTINCT FROM 1
                        )
                   )
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while Stage or new channel settings exist'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM messages
                WHERE embeds <> '[]'::jsonb
                   OR components <> '[]'::jsonb
                   OR sticker_items <> '[]'::jsonb
                   OR mention_role_refs <> '[]'::jsonb
                   OR mention_everyone
                   OR application_id IS NOT NULL
                   OR interaction_metadata IS NOT NULL
                   OR view_version <> 0
                   OR tts
                   OR webhook_avatar_url IS NOT NULL
                   OR (webhook_domain IS NOT NULL AND webhook_domain <> origin_domain)
                   OR (published_at IS NOT NULL AND published_at IS DISTINCT FROM created_at)
                   OR forwarded_message_id IS NOT NULL
                   OR forwarded_channel_id IS NOT NULL
                   OR forward_snapshot IS NOT NULL
                   OR poll_result IS NOT NULL
                   OR (message_type = 12 AND message_reference IS NOT NULL)
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while rich message data exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM invites
                WHERE temporary
                   OR reusable
                   OR target_type IS NOT NULL
                   OR target_user_id IS NOT NULL
                   OR scheduled_event_id IS NOT NULL
                   OR role_ids <> '[]'::jsonb
                   OR target_user_ids <> '[]'::jsonb
            ) OR EXISTS (
                SELECT 1 FROM emojis WHERE NOT available
            ) OR EXISTS (
                SELECT 1 FROM stickers WHERE tags <> '[]'::jsonb OR NOT available
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while invite or expression state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM attachments
                WHERE purpose IN (
                    'soundboard',
                    'application_asset',
                    'application_emoji',
                    'webhook_attachment',
                    'scheduled_event_image'
                )
                   OR bot_user_installation_id IS NOT NULL
                   OR bot_dm_capability_id IS NOT NULL
                   OR duration_secs IS NOT NULL
                   OR upload_channel_id IS NOT NULL
                   OR source_attachment_id IS NOT NULL
                   OR interaction_id IS NOT NULL
                   OR interaction_response_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while new attachment metadata exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM webhooks
                WHERE type <> 1
                   OR application_id IS NOT NULL
                   OR token_ciphertext IS NOT NULL
            ) OR EXISTS (
                SELECT 1
                FROM application_commands
                WHERE contexts <> '["guild"]'::jsonb
                   OR integration_types <> '["guild_install"]'::jsonb
            ) OR EXISTS (
                SELECT 1
                FROM bot_applications
                WHERE supported_install_types <> '["guild_install"]'::jsonb
                   OR user_install_scopes
                      <> '["applications.commands","interactions.respond"]'::jsonb
                   OR user_install_contexts <> '["guild","bot_dm","private_channel"]'::jsonb
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while app or webhook feature state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM bot_interactions
                WHERE user_installation_id IS NOT NULL
                   OR dm_capability_id IS NOT NULL
                   OR interaction_type <> 'command'
                   OR context <> 'guild'
                   OR integration_type <> 'guild_install'
                   OR invocation_permissions IS NOT NULL
                   OR message_id IS NOT NULL
                   OR custom_id IS NOT NULL
                   OR token_hash IS NOT NULL
                   OR dispatch_fingerprint IS NOT NULL
                   OR response_grant_id IS NOT NULL
                   OR request_fingerprint IS NOT NULL
                   OR callback_type IS NOT NULL
                   OR acknowledged_at IS NOT NULL
                   OR autocomplete_generation IS NOT NULL
                   OR command_name IS NULL
                   OR guild_id IS NULL
                   OR installation_id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while new interaction state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM bot_workers WHERE source_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM bot_install_templates WHERE source_id IS NOT NULL
            ) OR EXISTS (
                SELECT 1 FROM application_commands WHERE source_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while federated bot child projections exist'
                    USING ERRCODE = '23514',
                          HINT = 'retain this revision while authority-qualified child IDs exist';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM bot_application_targets
                WHERE generation <> 1
                   OR runtime_manifest_generation <> 0
                   OR runtime_revocation_generation <> 0
                   OR runtime_access_revocation_generation <> 0
                   OR runtime_status <> 'active'
                   OR NOT runtime_target_allowed
                   OR runtime_fingerprint IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while bot target replay or runtime state exists'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM messages
                WHERE proxy_request_fingerprint IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while durable proxy request bindings exist'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1 FROM roles
                WHERE (permissions & 2248097551880960) <> 0
                   OR ((permissions & 288230376151711744) <> 0
                       AND (permissions & 262144) = 0)
            ) OR EXISTS (
                SELECT 1 FROM channel_overwrites
                WHERE (allow & 2248097551880960) <> 0
                   OR (deny & 2248097551880960) <> 0
                   OR ((allow & 288230376151711744) <> 0 AND (allow & 262144) = 0)
                   OR ((deny & 288230376151711744) <> 0 AND (deny & 262144) = 0)
            ) OR EXISTS (
                SELECT 1 FROM bot_applications
                WHERE (default_permissions & 2248097551880960) <> 0
                   OR ((default_permissions & 288230376151711744) <> 0
                       AND (default_permissions & 262144) = 0)
            ) OR EXISTS (
                SELECT 1 FROM bot_install_templates
                WHERE (permissions & 2248097551880960) <> 0
                   OR ((permissions & 288230376151711744) <> 0
                       AND (permissions & 262144) = 0)
            ) OR EXISTS (
                SELECT 1 FROM bot_installations
                WHERE (granted_permissions & 2248097551880960) <> 0
                   OR ((granted_permissions & 288230376151711744) <> 0
                       AND (granted_permissions & 262144) = 0)
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade fc9a4b7d2e10 while new permission grants exist'
                    USING ERRCODE = '23514';
            END IF;
        END
        $$
        """
    )


def upgrade() -> None:
    op.drop_constraint(
        op.f("ck_developer_team_members_developer_team_member_user_is_local"),
        "developer_team_members",
        type_="check",
    )
    op.add_column(
        "developer_teams",
        sa.Column("federation_revision", sa.BigInteger(), server_default="1", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_developer_teams_developer_team_federation_revision_positive"),
        "developer_teams",
        "federation_revision >= 1",
    )
    _replace_permission_mask(NEW_PERMISSION_MASK)
    _harden_bot_permission_masks()
    _backfill_external_sticker_permission()
    _remove_interaction_only_e2ee_mode()
    _add_foundation_columns()
    _add_federated_bot_child_source_refs()
    _create_content_tables()
    _create_expression_and_automod_tables()
    _create_bot_dm_capability_table()
    _bind_bot_tokens_to_dm_capabilities()
    _create_application_tables()
    _wire_bot_dm_capability_references()
    _create_guild_setting_foreign_keys()
    _create_encrypted_forum_starter_reservations()
    _create_webhook_e2ee_tables()


def downgrade() -> None:
    _guard_feature_data_downgrade()
    op.drop_constraint(
        op.f("ck_bot_interactions_bot_interaction_payload_mode"),
        "bot_interactions",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_bot_interactions_bot_interaction_payload_mode"),
        "bot_interactions",
        "encrypted_payload IS NULL OR payload = '{}'::jsonb",
    )
    _restore_bot_permission_masks()
    _restore_interaction_only_e2ee_schema()
    # A pre-foundation server could only retain local developer-team members.
    # Remove federated projections before restoring that historical invariant.
    op.execute("DELETE FROM developer_team_members WHERE NOT user_is_local")
    op.create_check_constraint(
        op.f("ck_developer_team_members_developer_team_member_user_is_local"),
        "developer_team_members",
        "user_is_local",
    )
    op.drop_constraint(
        op.f("ck_developer_teams_developer_team_federation_revision_positive"),
        "developer_teams",
        type_="check",
    )
    op.drop_column("developer_teams", "federation_revision")
    op.drop_index(
        "ix_encrypted_forum_starter_reservations_parent",
        table_name="encrypted_forum_starter_reservations",
    )
    op.drop_table("encrypted_forum_starter_reservations")
    op.drop_index(
        "ix_webhook_e2ee_participations_channel",
        table_name="webhook_e2ee_participations",
    )
    op.drop_table("webhook_e2ee_participations")
    op.drop_index(
        "ix_webhook_e2ee_key_packages_available",
        table_name="webhook_e2ee_key_packages",
    )
    op.drop_table("webhook_e2ee_key_packages")
    op.drop_index(
        "uq_webhook_e2ee_devices_active_webhook",
        table_name="webhook_e2ee_devices",
    )
    op.drop_table("webhook_e2ee_devices")
    op.drop_constraint("uq_webhooks_ref_domain", "webhooks", type_="unique")
    for prefix in reversed(("afk", "system", "rules", "public_updates", "safety_alerts")):
        op.drop_constraint(f"fk_guilds_{prefix}_channel_ref", "guilds", type_="foreignkey")

    op.drop_constraint("uq_attachments_message_source_attachment", "attachments", type_="unique")
    op.drop_constraint(
        op.f("ck_attachments_source_attachment_ref_complete"),
        "attachments",
        type_="check",
    )
    op.drop_column("attachments", "source_attachment_domain")
    op.drop_column("attachments", "source_attachment_id")
    op.drop_constraint(
        op.f("ck_attachments_upload_channel_ref_complete"), "attachments", type_="check"
    )
    op.drop_column("attachments", "upload_channel_domain")
    op.drop_column("attachments", "upload_channel_id")
    op.drop_constraint(op.f("ck_attachments_voice_metadata_valid"), "attachments", type_="check")
    op.drop_constraint(op.f("ck_attachments_voice_metadata_complete"), "attachments", type_="check")
    op.drop_column("attachments", "waveform")
    op.drop_column("attachments", "duration_secs")

    op.drop_index("ix_attachments_bot_dm_capability_usage", table_name="attachments")
    op.drop_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        "NOT (bot_installation_id IS NOT NULL AND bot_user_installation_id IS NOT NULL)",
    )
    op.drop_constraint(
        op.f("fk_attachments_bot_dm_capability_id_bot_dm_capabilities"),
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "bot_dm_capability_id")
    op.drop_constraint(
        op.f("ck_bot_dm_grants_bot_dm_grant_one_installation"),
        "bot_dm_grants",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_bot_dm_grants_bot_dm_grant_one_installation"),
        "bot_dm_grants",
        "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int = 1",
    )
    op.drop_constraint(
        op.f("fk_bot_dm_grants_dm_capability_id_bot_dm_capabilities"),
        "bot_dm_grants",
        type_="foreignkey",
    )
    op.drop_column("bot_dm_grants", "dm_capability_id")

    op.drop_index("ix_bot_tokens_dm_capability", table_name="bot_tokens")
    op.drop_constraint(
        op.f("ck_bot_tokens_bot_token_dm_capability_binding"),
        "bot_tokens",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_bot_tokens_dm_capability_id_bot_dm_capabilities"),
        "bot_tokens",
        type_="foreignkey",
    )
    op.drop_column("bot_tokens", "dm_capability_revision")
    op.drop_column("bot_tokens", "dm_capability_id")

    op.drop_table("federated_interaction_attachment_grants")
    op.drop_index("ix_attachments_interaction_response", table_name="attachments")
    op.drop_constraint(
        op.f("ck_attachments_interaction_response_attachment_policy"),
        "attachments",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_attachments_message_or_interaction_owner_exclusive"),
        "attachments",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_attachments_interaction_response_id_bot_interaction_responses"),
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "interaction_response_id")
    op.drop_index("ix_attachments_interaction", table_name="attachments")
    op.drop_constraint(
        op.f("ck_attachments_interaction_attachment_policy"),
        "attachments",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_attachments_interaction_id_bot_interactions"),
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "interaction_id")
    op.drop_index("ix_attachments_bot_user_installation_usage", table_name="attachments")
    op.drop_constraint(
        op.f("ck_attachments_bot_usage_owner_exclusive"),
        "attachments",
        type_="check",
    )
    op.drop_constraint(
        op.f("fk_attachments_bot_user_installation_id_bot_user_installations"),
        "attachments",
        type_="foreignkey",
    )
    op.drop_column("attachments", "bot_user_installation_id")

    op.drop_constraint(
        op.f(
            "fk_invites_scheduled_event_id_scheduled_event_domain_guild_id_guild_domain_"
            "guild_scheduled_events"
        ),
        "invites",
        type_="foreignkey",
    )
    op.drop_table("guild_scheduled_event_subscriptions")
    op.drop_table("stage_instances")
    op.drop_table("guild_scheduled_events")

    for table in (
        "bot_e2ee_participations",
        "bot_e2ee_key_packages",
        "bot_e2ee_devices",
        "bot_dm_grant_consents",
        "bot_dm_grants",
        "bot_interaction_poll_votes",
        "bot_interaction_poll_answers",
        "bot_interaction_polls",
        "interaction_create_dispatch_outbox",
        "interaction_dispatch_outbox",
        "federated_interaction_admission_grants",
        "federated_interaction_response_locators",
        "bot_interaction_responses",
    ):
        op.drop_table(table)

    op.drop_table("bot_dm_capability_highwaters")
    op.drop_constraint(
        op.f("fk_bot_interactions_dm_capability_id_bot_dm_capabilities"),
        "bot_interactions",
        type_="foreignkey",
    )
    op.drop_table("bot_dm_capabilities")

    interaction_checks = (
        "bot_interaction_request_fingerprint_length",
        "bot_interaction_remote_request_binding",
        "bot_interaction_response_grant_length",
        "bot_interaction_dispatch_fingerprint_length",
        "bot_interaction_token_hash_length",
        "bot_interaction_message_ref_complete",
        "bot_interaction_command_id_context",
        "bot_interaction_command_name_context",
        "bot_interaction_dm_capability_context",
        "bot_interaction_integration_matches_dm_capability",
        "bot_interaction_integration_matches_user_installation",
        "bot_interaction_integration_matches_installation",
        "bot_interaction_context_matches_guild",
        "bot_interaction_guild_ref_complete",
        "bot_interaction_one_installation",
        "bot_interaction_invocation_channel_type_context",
        "bot_interaction_invocation_permissions_nonnegative",
        "bot_interaction_revision_positive",
        "bot_interaction_integration_type_value",
        "bot_interaction_context_value",
        "bot_interaction_type_value",
    )
    for name in interaction_checks:
        op.drop_constraint(op.f(f"ck_bot_interactions_{name}"), "bot_interactions", type_="check")
    op.drop_index("uq_bot_interactions_response_grant", table_name="bot_interactions")
    op.drop_constraint(
        op.f("fk_bot_interactions_user_installation_id_bot_user_installations"),
        "bot_interactions",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_bot_interactions_command_id_application_commands"),
        "bot_interactions",
        type_="foreignkey",
    )
    for column in (
        "autocomplete_generation",
        "acknowledged_at",
        "callback_type",
        "token_hash",
        "dispatch_fingerprint",
        "response_grant_id",
        "request_fingerprint",
        "custom_id",
        "message_domain",
        "message_id",
        "invocation_channel_type",
        "invocation_permissions",
        "command_id",
        "installation_revision",
        "integration_type",
        "context",
        "interaction_type",
        "dm_capability_id",
        "user_installation_id",
    ):
        op.drop_column("bot_interactions", column)
    op.alter_column("bot_interactions", "command_name", existing_type=sa.String(32), nullable=False)
    op.alter_column(
        "bot_interactions", "guild_domain", existing_type=sa.String(DOMAIN_LENGTH), nullable=False
    )
    op.alter_column("bot_interactions", "guild_id", existing_type=sa.BigInteger(), nullable=False)
    op.alter_column(
        "bot_interactions", "installation_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.drop_table("bot_application_runtime_highwaters")
    op.drop_table("bot_application_targets")
    op.drop_table("bot_user_installations")
    op.drop_table("application_emojis")
    op.drop_table("application_assets")
    op.drop_table("application_command_permissions")
    for name in (
        "user_install_contexts_are_bounded_array",
        "user_install_scopes_are_bounded_array",
        "install_types_are_bounded_array",
    ):
        op.drop_constraint(
            op.f(f"ck_bot_applications_bot_application_{name}"),
            "bot_applications",
            type_="check",
        )
    op.drop_column("bot_applications", "user_install_contexts")
    op.drop_column("bot_applications", "user_install_scopes")
    op.drop_column("bot_applications", "supported_install_types")

    for table in (
        "auto_mod_member_blocks",
        "auto_mod_executions",
        "auto_mod_rule_exempt_channels",
        "auto_mod_rule_exempt_roles",
        "auto_mod_actions",
        "auto_mod_rules",
        "soundboard_sounds",
        "emoji_role_restrictions",
        "federated_message_crossposts",
        "federated_channel_follows",
        "message_crossposts",
        "channel_follows",
        "message_views",
        "poll_votes",
        "poll_answers",
        "polls",
    ):
        op.drop_table(table)

    op.drop_constraint("uq_emojis_ref_guild", "emojis", type_="unique")

    op.drop_constraint(
        op.f("ck_application_commands_command_integration_types_are_array"),
        "application_commands",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_application_commands_application_command_name_format"),
        "application_commands",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_application_commands_application_command_name_format"),
        "application_commands",
        "name ~ '^[a-z0-9_-]{1,32}$'",
    )
    op.drop_constraint(
        op.f("ck_application_commands_command_contexts_are_array"),
        "application_commands",
        type_="check",
    )
    op.drop_column("application_commands", "integration_types")
    op.drop_column("application_commands", "contexts")

    op.drop_constraint(op.f("ck_webhooks_application_matches_type"), "webhooks", type_="check")
    op.drop_constraint(op.f("ck_webhooks_type_value"), "webhooks", type_="check")
    op.drop_constraint(
        op.f("fk_webhooks_application_id_application_domain_bot_applications"),
        "webhooks",
        type_="foreignkey",
    )
    op.drop_column("webhooks", "application_domain")
    op.drop_column("webhooks", "application_id")
    op.drop_column("webhooks", "token_ciphertext")
    op.drop_column("webhooks", "type")

    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        op.f("ck_attachments_purpose_value"),
        "attachments",
        "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner','emoji',"
        "'sticker','webhook_avatar','role_icon')",
    )
    op.drop_constraint(op.f("ck_stickers_tags_are_bounded_array"), "stickers", type_="check")
    op.drop_column("stickers", "available")
    op.drop_column("stickers", "tags")
    op.drop_column("emojis", "available")

    for name in (
        "target_user_ids_are_bounded_array",
        "role_ids_are_bounded_array",
        "target_type_matches_ref",
        "target_refs_complete",
        "target_type_value",
    ):
        op.drop_constraint(op.f(f"ck_invites_{name}"), "invites", type_="check")
    op.drop_constraint(op.f("ck_invites_positive_max_uses"), "invites", type_="check")
    op.create_check_constraint(
        op.f("ck_invites_positive_max_uses"),
        "invites",
        "max_uses IS NULL OR max_uses > 0",
    )
    for column in (
        "target_user_ids",
        "role_ids",
        "scheduled_event_domain",
        "scheduled_event_id",
        "target_user_domain",
        "target_user_id",
        "target_type",
        "reusable",
        "temporary",
    ):
        op.drop_column("invites", column)

    for name in (
        "webhook_ref_complete",
        "proxy_request_fingerprint_complete",
        "proxy_request_fingerprint_version_positive",
        "proxy_request_fingerprint_format",
        "proxy_commit_seq_positive",
        "proxy_request_fingerprint_has_nonce_receipt",
        "nonnegative_view_version",
        "forward_snapshot_has_source",
        "forward_snapshot_is_object",
        "poll_result_has_reference",
        "poll_result_matches_message_type",
        "poll_result_is_object",
        "forwarded_channel_ref_complete",
        "forwarded_message_ref_complete",
        "interaction_metadata_is_object",
        "application_ref_complete",
        "role_mentions_are_array",
        "components_are_array",
        "sticker_items_are_bounded_array",
        "embeds_are_array",
        "message_reference_is_object",
        "channel_follow_has_reference",
    ):
        op.drop_constraint(op.f(f"ck_messages_{name}"), "messages", type_="check")
    op.execute(
        "UPDATE messages SET webhook_id = NULL WHERE webhook_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM webhooks WHERE webhooks.id = messages.webhook_id)"
    )
    op.drop_index("ix_messages_proxy_commit_receipt", table_name="messages")
    for column in (
        "proxy_commit_seq",
        "proxy_request_fingerprint",
        "proxy_request_fingerprint_version",
        "poll_result",
        "message_reference",
        "forward_snapshot",
        "forwarded_channel_domain",
        "forwarded_channel_id",
        "forwarded_message_domain",
        "forwarded_message_id",
        "tts",
        "published_at",
        "webhook_avatar_url",
        "webhook_domain",
        "view_version",
        "interaction_metadata",
        "mention_everyone",
        "mention_role_refs",
        "application_domain",
        "application_id",
        "components",
        "sticker_items",
        "embeds",
    ):
        op.drop_column("messages", column)
    op.create_foreign_key(
        "fk_messages_webhook_id_webhooks",
        "messages",
        "webhooks",
        ["webhook_id"],
        ["id"],
        ondelete="SET NULL",
    )

    for name in (
        "video_quality_mode_value",
        "voice_user_limit_range",
        "voice_bitrate_range",
        "voice_metadata_context",
    ):
        op.drop_constraint(op.f(f"ck_channels_{name}"), "channels", type_="check")
    op.execute("DELETE FROM channels WHERE type = 13")
    op.drop_constraint(op.f("ck_channels_channel_type"), "channels", type_="check")
    op.create_check_constraint(
        op.f("ck_channels_channel_type"),
        "channels",
        "type IN (0,1,2,4,5,10,11,12,15,17)",
    )
    for column in (
        "voice_status",
        "video_quality_mode",
        "rtc_region",
        "user_limit",
        "bitrate",
        "nsfw",
    ):
        op.drop_column("channels", column)
    op.drop_index("ix_guild_members_prune_activity", table_name="guild_members")
    op.drop_column("guild_members", "last_guild_activity_at")
    op.drop_column("guild_members", "temporary")

    for name in (
        "settings_channel_refs_complete",
        "system_channel_flags_nonnegative",
        "afk_timeout_value",
        "explicit_content_filter_range",
        "default_message_notifications_range",
        "verification_level_range",
    ):
        op.drop_constraint(op.f(f"ck_guilds_{name}"), "guilds", type_="check")
    for column in (
        "dms_disabled_until",
        "invites_disabled_until",
        "community_enabled",
        "safety_alerts_channel_domain",
        "safety_alerts_channel_id",
        "public_updates_channel_domain",
        "public_updates_channel_id",
        "rules_channel_domain",
        "rules_channel_id",
        "system_channel_flags",
        "system_channel_domain",
        "system_channel_id",
        "afk_timeout",
        "afk_channel_domain",
        "afk_channel_id",
        "preferred_locale",
        "explicit_content_filter",
        "default_message_notifications",
        "verification_level",
    ):
        op.drop_column("guilds", column)
    op.drop_column("user_settings", "age_restricted_dm_commands_enabled")
    op.drop_constraint(
        op.f("ck_users_age_assurance_local_human_only"),
        "users",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_users_age_assurance_state_value"),
        "users",
        type_="check",
    )
    op.drop_column("users", "age_assurance_state")
    _remove_federated_bot_child_source_refs()
    _remove_new_permissions()
    _replace_permission_mask(OLD_PERMISSION_MASK)
