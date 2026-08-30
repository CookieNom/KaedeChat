from pathlib import Path

from sqlalchemy import CheckConstraint, Computed, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app.core.permissions import ALL_PERMISSIONS
from app.db import models  # noqa: F401
from app.db.base import Base


def test_complete_v1_schema_is_registered() -> None:
    required = {
        "instances",
        "peer_keys",
        "users",
        "user_settings",
        "push_devices",
        "push_wake_outbox",
        "push_relay_subscriptions",
        "push_relay_deliveries",
        "relationships",
        "sessions",
        "one_time_tokens",
        "email_outbox",
        "recovery_codes",
        "auth_events",
        "guilds",
        "federation_replica_usage",
        "guild_events",
        "guild_history_exports",
        "guild_history_export_channels",
        "guild_members",
        "remote_guild_membership_intents",
        "guild_notification_settings",
        "guild_instance_bans",
        "roles",
        "member_roles",
        "channels",
        "tracker_boards",
        "tracker_dispatch_outbox",
        "tracker_lanes",
        "tracker_tasks",
        "thread_members",
        "channel_overwrites",
        "messages",
        "message_projections",
        "search_index_outbox",
        "search_index_state",
        "guild_history_imports",
        "guild_history_import_channels",
        "guild_history_staged_messages",
        "federated_history_messages",
        "attachments",
        "attachment_federation_recipients",
        "media_tombstone_sources",
        "media_tombstone_destinations",
        "room_federation_recipients",
        "terminal_room_deletions",
        "guild_media_deletion_requests",
        "reactions",
        "pins",
        "dm_conversations",
        "dm_participants",
        "federated_dm_storage_usage",
        "federated_dm_row_charges",
        "read_states",
        "invites",
        "bans",
        "audit_log_entries",
        "emojis",
        "stickers",
        "webhooks",
        "federation_events",
        "federation_outbox",
        "federation_inbox",
        "remote_media_cache",
        "remote_media_orphans",
        "remote_media_tombstones",
        "user_storage_usage",
        "instance_blocks",
        "instance_user_restrictions",
        "instance_admin_grants",
        "instance_audit_events",
        "developer_teams",
        "developer_team_members",
        "developer_team_member_highwaters",
        "bot_applications",
        "bot_application_targets",
        "bot_application_runtime_highwaters",
        "bot_credentials",
        "bot_workers",
        "bot_instance_rules",
        "bot_install_templates",
        "application_commands",
        "application_command_permissions",
        "application_assets",
        "application_emojis",
        "auto_mod_actions",
        "auto_mod_executions",
        "auto_mod_member_blocks",
        "auto_mod_rule_exempt_channels",
        "auto_mod_rule_exempt_roles",
        "auto_mod_rules",
        "bot_dm_grants",
        "bot_dm_grant_consents",
        "bot_dm_capabilities",
        "bot_dm_capability_highwaters",
        "bot_e2ee_devices",
        "bot_e2ee_key_packages",
        "bot_e2ee_participations",
        "bot_installations",
        "bot_interaction_poll_answers",
        "bot_interaction_poll_votes",
        "bot_interaction_polls",
        "bot_interaction_responses",
        "federated_interaction_admission_grants",
        "federated_interaction_attachment_grants",
        "federated_interaction_response_locators",
        "interaction_create_dispatch_outbox",
        "interaction_dispatch_outbox",
        "bot_tokens",
        "bot_interactions",
        "bot_user_installations",
        "channel_follows",
        "emoji_role_restrictions",
        "federated_channel_follows",
        "federated_message_crossposts",
        "guild_scheduled_event_subscriptions",
        "guild_scheduled_events",
        "stage_instances",
        "message_crossposts",
        "message_views",
        "poll_answers",
        "poll_votes",
        "polls",
        "soundboard_sounds",
        "abuse_reports",
        "e2ee_account_vaults",
        "e2ee_account_vault_digests",
        "e2ee_control_records",
        "e2ee_devices",
        "e2ee_key_packages",
        "e2ee_package_claim_batches",
        "e2ee_room_operations",
        "encrypted_forum_starter_reservations",
        "webhook_e2ee_devices",
        "webhook_e2ee_key_packages",
        "webhook_e2ee_participations",
    }
    assert required == set(Base.metadata.tables)


def test_community_invite_columns_belong_to_invites_not_stage_instances() -> None:
    invites = Base.metadata.tables["invites"]
    invite_columns = invites.c
    stage_columns = Base.metadata.tables["stage_instances"].c

    assert {"role_ids", "target_user_ids"} <= set(invite_columns.keys())
    assert "target_application_id" not in invite_columns
    assert "target_application_domain" not in invite_columns
    assert "role_ids" not in stage_columns
    assert "target_user_ids" not in stage_columns
    max_uses = next(
        constraint
        for constraint in invites.constraints
        if constraint.name == "ck_invites_positive_max_uses"
    )
    assert isinstance(max_uses, CheckConstraint)
    assert "BETWEEN 1 AND 100" in str(max_uses.sqltext)


def test_push_devices_are_local_encrypted_registrations() -> None:
    devices = Base.metadata.tables["push_devices"]
    assert tuple(devices.primary_key.columns.keys()) == ("id",)
    assert devices.c.token_hash.unique is True
    assert devices.c.token_encrypted.nullable is True
    assert devices.c.transport.server_default is not None
    assert "ck_push_devices_push_devices_user_is_local" in constraint_names("push_devices")
    assert "ck_push_devices_platform_value" in constraint_names("push_devices")
    assert "ck_push_devices_token_hash_length" in constraint_names("push_devices")
    assert "ck_push_devices_transport_fields" in constraint_names("push_devices")
    local_user = foreign_key_for_columns(
        "push_devices", ("user_id", "user_domain", "user_is_local")
    )
    assert tuple(element.target_fullname for element in local_user.elements) == (
        "users.id",
        "users.origin_domain",
        "users.is_local",
    )
    assert local_user.ondelete == "CASCADE"


def test_user_settings_store_synchronized_guild_navigation() -> None:
    settings = Base.metadata.tables["user_settings"]
    assert settings.c.guild_navigation.nullable is False
    assert "JSONB" in str(settings.c.guild_navigation.type)


def test_guild_notification_settings_are_membership_scoped() -> None:
    table = Base.metadata.tables["guild_notification_settings"]
    assert tuple(table.primary_key.columns.keys()) == (
        "user_id",
        "user_domain",
        "guild_id",
        "guild_domain",
    )
    membership = foreign_key_for_columns(
        "guild_notification_settings",
        ("guild_id", "guild_domain", "user_id", "user_domain"),
    )
    assert tuple(element.target_fullname for element in membership.elements) == (
        "guild_members.guild_id",
        "guild_members.guild_domain",
        "guild_members.user_id",
        "guild_members.user_domain",
    )
    assert membership.ondelete == "CASCADE"
    assert "ck_guild_notification_settings_notification_level_value" in constraint_names(
        "guild_notification_settings"
    )


def test_remote_guild_membership_intents_survive_replica_purge() -> None:
    table = Base.metadata.tables["remote_guild_membership_intents"]
    assert tuple(table.primary_key.columns.keys()) == (
        "guild_id",
        "guild_domain",
        "user_id",
        "user_domain",
    )
    assert not any(
        element.target_fullname.startswith("guilds.")
        for constraint in table.foreign_key_constraints
        for element in constraint.elements
    )
    local_user = foreign_key_for_columns(
        "remote_guild_membership_intents",
        ("user_id", "user_domain", "user_is_local"),
    )
    assert local_user.ondelete == "CASCADE"
    assert {
        "ck_remote_guild_membership_intents_guild_is_remote_from_local_user",
        "ck_remote_guild_membership_intents_state_value",
        "ck_remote_guild_membership_intents_remote_guild_membership_intents_user_is_local",
    } <= constraint_names("remote_guild_membership_intents")


def test_remote_guild_replica_usage_is_durable_and_cascade_scoped() -> None:
    guilds = Base.metadata.tables["guilds"]
    usage = Base.metadata.tables["federation_replica_usage"]

    assert guilds.c.snapshot_generation.nullable is False
    assert guilds.c.sync_error_code.type.length == 64
    assert {
        "ck_guilds_positive_snapshot_generation",
        "ck_guilds_sync_error_requires_failure",
    } <= constraint_names("guilds")
    assert tuple(usage.primary_key.columns.keys()) == ("guild_id", "guild_domain")
    guild_fk = foreign_key_for_columns("federation_replica_usage", ("guild_id", "guild_domain"))
    assert guild_fk.ondelete == "CASCADE"
    assert isinstance(usage.c.total_rows.computed, Computed)
    assert isinstance(usage.c.total_bytes.computed, Computed)
    assert usage.c.tracker_rows.nullable is False
    assert usage.c.tracker_bytes.nullable is False
    assert "tracker_rows" in str(usage.c.total_rows.computed.sqltext)
    assert "tracker_bytes" in str(usage.c.total_bytes.computed.sqltext)
    assert {
        "ck_federation_replica_usage_nonnegative_rows",
        "ck_federation_replica_usage_nonnegative_bytes",
    } <= constraint_names("federation_replica_usage")


def test_guild_sanctions_have_expiry_and_instance_scope() -> None:
    members = Base.metadata.tables["guild_members"]
    bans = Base.metadata.tables["bans"]
    instance_bans = Base.metadata.tables["guild_instance_bans"]

    assert members.c.timeout_indefinite.nullable is False
    assert members.c.timeout_reason.type.length == 512
    assert bans.c.expires_at.nullable is True
    assert "ck_bans_expiry_after_creation" in constraint_names("bans")
    assert "ix_bans_expiry" in {index.name for index in bans.indexes}
    assert tuple(instance_bans.primary_key.columns.keys()) == (
        "guild_id",
        "guild_domain",
        "instance_domain",
    )
    instance_fk = foreign_key_for_columns("guild_instance_bans", ("instance_domain",))
    assert tuple(element.target_fullname for element in instance_fk.elements) == (
        "instances.domain",
    )
    guild_fk = foreign_key_for_columns("guild_instance_bans", ("guild_id", "guild_domain"))
    assert guild_fk.ondelete == "CASCADE"
    assert "ck_guild_instance_bans_expiry_after_creation" in constraint_names("guild_instance_bans")
    assert "ix_guild_instance_bans_expiry" in {index.name for index in instance_bans.indexes}


def test_email_outbox_contains_only_encrypted_delivery_content() -> None:
    outbox = Base.metadata.tables["email_outbox"]
    assert {"to", "recipient", "subject", "text", "html", "token"}.isdisjoint(outbox.columns)
    assert {
        "ck_email_outbox_status_value",
        "ck_email_outbox_claim_state",
        "ck_email_outbox_completion_state",
        "ck_email_outbox_encrypted_payload_length",
    } <= constraint_names("email_outbox")
    assert {
        "ix_email_outbox_due",
        "ix_email_outbox_stale_claim",
        "ix_email_outbox_terminal_retention",
    } <= {index.name for index in outbox.indexes}
    token_fk = foreign_key_for_columns("email_outbox", ("one_time_token_id",))
    assert tuple(element.target_fullname for element in token_fk.elements) == (
        "one_time_tokens.id",
    )
    assert token_fk.ondelete == "CASCADE"


def test_messages_are_partitioned_and_have_workhorse_indexes() -> None:
    messages = Base.metadata.tables["messages"]
    assert messages.dialect_options["postgresql"]["partition_by"] == "RANGE (id)"
    names = {index.name for index in messages.indexes}
    expected = {
        "ix_messages_channel_id_desc",
        "ix_messages_author_id_desc",
        "ix_messages_id_brin",
    }
    assert expected <= names


def test_message_projection_work_is_durable_and_channel_bound() -> None:
    projections = Base.metadata.tables["message_projections"]
    assert tuple(projections.primary_key.columns.keys()) == ("message_id", "message_domain")
    message_fk = foreign_key_for_columns(
        "message_projections",
        ("message_id", "message_domain", "channel_id", "channel_domain"),
    )
    assert tuple(element.target_fullname for element in message_fk.elements) == (
        "messages.id",
        "messages.origin_domain",
        "messages.channel_id",
        "messages.channel_domain",
    )
    assert "ix_message_projections_pending" in {index.name for index in projections.indexes}


def test_federated_history_grants_and_import_provenance_are_bound() -> None:
    exports = Base.metadata.tables["guild_history_exports"]
    imports = Base.metadata.tables["guild_history_imports"]
    staged = Base.metadata.tables["guild_history_staged_messages"]
    provenance = Base.metadata.tables["federated_history_messages"]

    assert tuple(exports.primary_key.columns.keys()) == ("id",)
    assert tuple(imports.primary_key.columns.keys()) == ("export_id", "export_domain")
    assert tuple(staged.primary_key.columns.keys()) == (
        "export_id",
        "export_domain",
        "message_id",
        "message_domain",
    )
    assert tuple(provenance.primary_key.columns.keys()) == ("message_id", "message_domain")
    assert has_foreign_key(
        "guild_history_staged_messages",
        ("export_id", "export_domain"),
        ("guild_history_imports.export_id", "guild_history_imports.export_domain"),
    )
    assert has_foreign_key(
        "federated_history_messages",
        ("message_id", "message_domain"),
        ("messages.id", "messages.origin_domain"),
    )
    assert "ck_guilds_federated_history_policy_value" in constraint_names("guilds")
    assert "ck_channels_federated_history_policy_value" in constraint_names("channels")


def test_federation_events_and_outbox_references_are_origin_scoped() -> None:
    events = Base.metadata.tables["federation_events"]
    instances = Base.metadata.tables["instances"]
    outbox = Base.metadata.tables["federation_outbox"]
    assert tuple(events.primary_key.columns.keys()) == ("origin_domain", "event_id")
    event_fk = foreign_key_for_columns(outbox.name, ("event_origin_domain", "event_id"))
    assert tuple(element.target_fullname for element in event_fk.elements) == (
        "federation_events.origin_domain",
        "federation_events.event_id",
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("destination", "event_origin_domain", "event_id")
        for constraint in outbox.constraints
    )
    assert events.c.envelope_bytes.nullable is False
    assert instances.c.federation_inbox_events.nullable is False
    assert instances.c.federation_inbox_event_bytes.nullable is False
    assert {
        "ck_federation_events_nonnegative_envelope_bytes",
    } <= constraint_names("federation_events")
    assert {
        "ck_instances_nonnegative_federation_inbox_events",
        "ck_instances_nonnegative_federation_inbox_event_bytes",
    } <= constraint_names("instances")


def test_media_staging_objects_have_a_recoverable_cleanup_cursor() -> None:
    attachments = Base.metadata.tables["attachments"]
    assert "staging_object_key" in attachments.c
    indexes = {index.name: index for index in attachments.indexes}
    assert "ix_attachments_staging_gc" in indexes
    message_index = indexes["ix_attachments_live_message"]
    assert tuple(message_index.columns.keys()) == ("message_id", "message_domain", "id")
    assert str(message_index.dialect_options["postgresql"]["where"]) == (
        "deleted_at IS NULL AND message_id IS NOT NULL"
    )


def test_local_user_tables_have_database_constraints() -> None:
    users = Base.metadata.tables["users"]
    assert {
        "e2ee_recovery_token_hash",
        "e2ee_recovery_session_id",
        "e2ee_recovery_generation",
        "e2ee_recovery_expires_at",
    } <= set(users.c.keys())
    assert {
        "ck_users_e2ee_recovery_token_hash_length",
        "ck_users_e2ee_recovery_generation_positive",
        "ck_users_e2ee_recovery_authorization_complete",
        "ck_users_password_kdf_fields_complete",
    } <= constraint_names("users")
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name is not None
        and constraint.name.endswith("password_kdf_fields_complete")
        and "is_local AND account_type = 'human' AND password_kdf_version = 2"
        in str(constraint.sqltext)
        and "e2ee_vault_salt IS NOT NULL" in str(constraint.sqltext)
        for constraint in users.constraints
    )
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name is not None
        and constraint.name.endswith("local_auth_fields")
        and str(constraint.sqltext)
        == "NOT is_local OR account_type = 'bot' OR password_hash IS NOT NULL"
        for constraint in users.constraints
    )
    for table_name in {
        "user_settings",
        "relationships",
        "sessions",
        "one_time_tokens",
        "recovery_codes",
        "auth_events",
        "read_states",
        "user_storage_usage",
    }:
        table = Base.metadata.tables[table_name]
        assert any(
            isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "user_is_local"
            for constraint in table.constraints
        ), table_name
        assert any(
            tuple(constraint.column_keys) == ("user_id", "user_domain", "user_is_local")
            and tuple(element.target_fullname for element in constraint.elements)
            == ("users.id", "users.origin_domain", "users.is_local")
            for constraint in table.foreign_key_constraints
        ), table_name


def test_profiles_and_relationship_requests_have_bounded_state() -> None:
    users = Base.metadata.tables["users"]
    relationships = Base.metadata.tables["relationships"]
    assert users.c.custom_status.type.length == 128
    assert relationships.c.request_id.type.length == 64
    assert "ck_relationships_relationship_request_id_format" in constraint_names("relationships")


def test_developer_team_members_accept_federated_user_identities() -> None:
    members = Base.metadata.tables["developer_team_members"]
    assert not any(
        isinstance(constraint, CheckConstraint) and str(constraint.sqltext) == "user_is_local"
        for constraint in members.constraints
    )
    user = foreign_key_for_columns(
        "developer_team_members",
        ("user_id", "user_domain", "user_is_local"),
    )
    assert tuple(element.target_fullname for element in user.elements) == (
        "users.id",
        "users.origin_domain",
        "users.is_local",
    )


def test_federated_entity_identities_use_composite_primary_keys() -> None:
    for table_name in {
        "users",
        "guilds",
        "roles",
        "channels",
        "messages",
        "attachments",
        "dm_conversations",
        "emojis",
    }:
        table = Base.metadata.tables[table_name]
        assert tuple(table.primary_key.columns.keys()) == ("id", "origin_domain"), table_name


def constraint_names(table_name: str) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name is not None
    }


def has_foreign_key(
    table_name: str, local_columns: tuple[str, ...], targets: tuple[str, ...]
) -> bool:
    return any(
        tuple(constraint.column_keys) == local_columns
        and tuple(element.target_fullname for element in constraint.elements) == targets
        for constraint in Base.metadata.tables[table_name].foreign_key_constraints
    )


def foreign_key_for_columns(
    table_name: str, local_columns: tuple[str, ...]
) -> ForeignKeyConstraint:
    return next(
        constraint
        for constraint in Base.metadata.tables[table_name].foreign_key_constraints
        if tuple(constraint.column_keys) == local_columns
    )


def test_composite_references_are_complete_and_guild_scoped() -> None:
    assert {
        "ck_channels_parent_ref_complete",
        "ck_channels_last_message_ref_complete",
    } <= constraint_names("channels")
    assert "ck_messages_referenced_message_ref_complete" in constraint_names("messages")
    assert "ck_attachments_message_ref_complete" in constraint_names("attachments")
    assert "ck_invites_channel_ref_complete" in constraint_names("invites")
    assert "ck_read_states_last_message_ref_complete" in constraint_names("read_states")

    roles = Base.metadata.tables["roles"]
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("id", "origin_domain", "guild_id", "guild_domain")
        for constraint in roles.constraints
    )
    assert has_foreign_key(
        "member_roles",
        ("role_id", "role_domain", "guild_id", "guild_domain"),
        ("roles.id", "roles.origin_domain", "roles.guild_id", "roles.guild_domain"),
    )


def test_bot_installation_application_reference_binds_the_exact_bot_identity() -> None:
    constraint = foreign_key_for_columns(
        "bot_installations",
        ("application_id", "application_domain", "bot_user_id", "bot_user_domain"),
    )

    assert tuple(element.target_fullname for element in constraint.elements) == (
        "bot_applications.id",
        "bot_applications.origin_domain",
        "bot_applications.bot_user_id",
        "bot_applications.bot_user_domain",
    )
    assert constraint.name == "fk_bot_installations_application_bot_user_lineage"
    # Default NO ACTION prevents hard deletion while installations remain. An
    # application's soft-delete status is not part of the key, so retention is valid.
    assert constraint.ondelete is None


def test_role_and_overwrite_masks_exclude_reserved_permission_bits() -> None:
    for table_name, constraint_name in (
        ("roles", "ck_roles_known_permission_mask"),
        ("channel_overwrites", "ck_channel_overwrites_known_permission_masks"),
    ):
        constraint = next(
            item
            for item in Base.metadata.tables[table_name].constraints
            if item.name == constraint_name
        )
        assert isinstance(constraint, CheckConstraint)
        assert f"~{ALL_PERMISSIONS}" in str(constraint.sqltext)
    # Reserved bits 9, 37, and 42 must not become accidental grants.
    assert ALL_PERMISSIONS == 576456216817434111
    migration = (
        Path(__file__).parents[1] / "migrations/versions/fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    assert "NEW_PERMISSION_MASK = ((1 << 59) - 1) & ~(1 << 19)" in migration
    for table_name, constraint_name, column_name in (
        (
            "bot_applications",
            "ck_bot_applications_bot_application_positive_values",
            "default_permissions",
        ),
        (
            "bot_install_templates",
            "ck_bot_install_templates_bot_template_positive_values",
            "permissions",
        ),
        (
            "bot_installations",
            "ck_bot_installations_bot_installation_positive_values",
            "granted_permissions",
        ),
        (
            "bot_interactions",
            "ck_bot_interactions_bot_interaction_invocation_permissions_nonnegative",
            "invocation_permissions",
        ),
    ):
        constraint = next(
            item
            for item in Base.metadata.tables[table_name].constraints
            if item.name == constraint_name
        )
        assert isinstance(constraint, CheckConstraint)
        sql = str(constraint.sqltext)
        assert column_name in sql
        assert f"~{ALL_PERMISSIONS}" in sql


def test_message_parity_columns_and_authoritative_install_source_are_registered() -> None:
    messages = Base.metadata.tables["messages"]
    attachments = Base.metadata.tables["attachments"]
    installations = Base.metadata.tables["bot_user_installations"]

    assert {
        "sticker_items",
        "tts",
        "webhook_avatar_url",
        "message_reference",
        "proxy_request_fingerprint_version",
        "proxy_request_fingerprint",
        "proxy_commit_seq",
    } <= set(messages.c.keys())
    assert {
        "duration_secs",
        "waveform",
        "upload_channel_id",
        "upload_channel_domain",
    } <= set(attachments.c.keys())
    assert {
        "ck_messages_sticker_items_are_bounded_array",
        "ck_messages_message_reference_is_object",
        "ck_messages_channel_follow_has_reference",
        "ck_messages_proxy_request_fingerprint_complete",
        "ck_messages_proxy_request_fingerprint_version_positive",
        "ck_messages_proxy_request_fingerprint_format",
        "ck_messages_proxy_commit_seq_positive",
        "ck_messages_proxy_request_fingerprint_has_nonce_receipt",
    } <= constraint_names("messages")
    assert "ix_messages_proxy_commit_receipt" in {index.name for index in messages.indexes}
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    assert 'sa.Column("message_reference", postgresql.JSONB())' in migration
    assert 'sa.Column("proxy_request_fingerprint_version", sa.Integer())' in migration
    assert 'sa.Column("proxy_request_fingerprint", sa.String(64))' in migration
    assert 'sa.Column("proxy_commit_seq", sa.BigInteger())' in migration
    assert "WITH proxy_receipts AS" in migration
    assert "MIN(event.seq) AS commit_seq" in migration
    assert "message.client_nonce IS NOT NULL" in migration
    assert "message.message_type = 6" in migration
    assert "'guild_id', channel.guild_id::text" in migration
    assert "message_type <> 12 OR message_reference IS NOT NULL" in migration
    assert "UPDATE messages SET message_type = 0" in migration
    assert "WHERE message_type = 12 AND message_reference IS NULL" in migration
    assert migration.rindex('"channel_follow_has_reference"') < migration.index(
        'op.drop_column("messages", column)'
    )
    assert {
        "ck_attachments_upload_channel_ref_complete",
        "ck_attachments_voice_metadata_complete",
        "ck_attachments_voice_metadata_valid",
    } <= constraint_names("attachments")

    assert installations.c.source_id.nullable is True
    assert installations.c.source_domain.nullable is True
    assert installations.c.authority_expires_at.nullable is True
    assert "ix_bot_user_installations_authority_expiry" in {
        index.name for index in installations.indexes
    }
    assert "ck_bot_user_installations_user_install_source_ref_complete" in constraint_names(
        "bot_user_installations"
    )
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys())
        == (
            "source_id",
            "source_domain",
            "application_id",
            "application_domain",
            "user_id",
            "user_domain",
        )
        for constraint in installations.constraints
    )
    lease_migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "f95b2c3d8e41_developer_team_snapshot_highwaters.py"
    ).read_text()
    assert 'sa.Column("authority_expires_at", sa.DateTime(timezone=True))' in lease_migration
    assert "installing_user.is_local IS FALSE" in lease_migration
    assert '"ix_bot_user_installations_authority_expiry"' in lease_migration


def test_bot_dm_runtime_lineage_and_terminal_highwaters_are_registered() -> None:
    capabilities = Base.metadata.tables["bot_dm_capabilities"]
    capability_highwaters = Base.metadata.tables["bot_dm_capability_highwaters"]
    runtime_highwaters = Base.metadata.tables["bot_application_runtime_highwaters"]

    assert "access_revocation_generation" not in capabilities.c
    assert capabilities.c.target_access_revocation_generation.nullable is False
    assert capabilities.c.proof_fingerprint.type.length == 32
    assert "JSONB" in str(capabilities.c.proof.type)
    assert {
        "ck_bot_dm_capabilities_bot_dm_capability_positive_values",
        "ck_bot_dm_capabilities_bot_dm_capability_status_value",
    } <= constraint_names("bot_dm_capabilities")

    assert tuple(capability_highwaters.primary_key.columns.keys()) == ("grant_id",)
    assert {
        "installation_authority_domain",
        "identity_fingerprint",
        "revision",
        "authorization_fingerprint",
        "status",
        "expires_at",
    } <= set(capability_highwaters.c.keys())
    assert capability_highwaters.c.identity_fingerprint.type.length == 32
    assert capability_highwaters.c.authorization_fingerprint.type.length == 32
    assert capability_highwaters.c.expires_at.nullable is False
    assert "ix_bot_dm_capability_highwaters_authority_expiry" in {
        index.name for index in capability_highwaters.indexes
    }

    assert tuple(runtime_highwaters.primary_key.columns.keys()) == (
        "application_id",
        "application_domain",
        "target_domain",
    )
    assert {
        "manifest_generation",
        "revocation_generation",
        "access_revocation_generation",
        "runtime_fingerprint",
        "status",
        "target_allowed",
    } <= set(runtime_highwaters.c.keys())
    assert runtime_highwaters.c.runtime_fingerprint.type.length == 32

    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    assert '"target_access_revocation_generation"' in migration
    assert '"bot_dm_capability_highwaters"' in migration
    assert '"ix_bot_dm_capability_highwaters_authority_expiry"' in migration
    assert '"bot_application_runtime_highwaters"' in migration
    assert "octet_length(authorization_fingerprint) = 32" in migration


def test_federated_application_children_separate_source_and_local_ids() -> None:
    expected = {
        "bot_workers": "ck_bot_workers_bot_worker_source_ref_complete",
        "bot_install_templates": "ck_bot_install_templates_bot_template_source_ref_complete",
        "application_commands": "ck_application_commands_command_source_ref_complete",
    }
    for table_name, check_name in expected.items():
        table = Base.metadata.tables[table_name]
        assert table.c.id.primary_key
        assert table.c.source_id.nullable
        assert table.c.source_domain.nullable
        assert check_name in constraint_names(table_name)
        assert any(
            isinstance(constraint, UniqueConstraint)
            and tuple(constraint.columns.keys()) == ("source_id", "source_domain")
            for constraint in table.constraints
        )


def test_bot_parity_migration_drops_attachment_checks_before_their_columns() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    upload_check = 'op.f("ck_attachments_upload_channel_ref_complete")'
    voice_check = 'op.f("ck_attachments_voice_metadata_complete")'
    upload_column = 'op.drop_column("attachments", "upload_channel_domain")'
    voice_column = 'op.drop_column("attachments", "waveform")'

    assert migration.rindex(upload_check) < migration.index(upload_column)
    assert migration.rindex(voice_check) < migration.index(voice_column)


def test_bot_parity_downgrade_refuses_to_discard_feature_data() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    guard_start = migration.index("def _guard_feature_data_downgrade()")
    upgrade_start = migration.index("def upgrade()", guard_start)
    guard = migration[guard_start:upgrade_start]
    downgrade_start = migration.index("def downgrade()")

    assert "_guard_feature_data_downgrade()" in migration[downgrade_start:]
    assert migration.index("_guard_feature_data_downgrade()", downgrade_start) < migration.index(
        "_restore_bot_permission_masks()", downgrade_start
    )
    for protected_state in (
        "guild_scheduled_events",
        "stage_instances",
        "soundboard_sounds",
        "auto_mod_rules",
        "bot_user_installations",
        "bot_interaction_responses",
        "bot_e2ee_participations",
        "webhook_e2ee_participations",
        "federated bot child projections exist",
        "bot target replay or runtime state exists",
        "proxy_request_fingerprint IS NOT NULL",
        "type = 13",
        "forward_snapshot IS NOT NULL",
        "new permission grants exist",
    ):
        assert protected_state in guard
    assert "export or deliberately remove the feature data first" in guard


def test_channel_and_message_references_cannot_cross_owners() -> None:
    channels = Base.metadata.tables["channels"]
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("id", "origin_domain", "guild_id", "guild_domain")
        for constraint in channels.constraints
    )
    assert "ck_channels_parent_requires_guild" in constraint_names("channels")
    assert has_foreign_key(
        "channels",
        ("parent_id", "parent_domain", "guild_id", "guild_domain"),
        (
            "channels.id",
            "channels.origin_domain",
            "channels.guild_id",
            "channels.guild_domain",
        ),
    )
    parent_fk = foreign_key_for_columns(
        "channels", ("parent_id", "parent_domain", "guild_id", "guild_domain")
    )
    assert parent_fk.deferrable is True
    assert parent_fk.initially == "DEFERRED"

    messages = Base.metadata.tables["messages"]
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys())
        == ("id", "origin_domain", "channel_id", "channel_domain")
        for constraint in messages.constraints
    )
    message_target = (
        "messages.id",
        "messages.origin_domain",
        "messages.channel_id",
        "messages.channel_domain",
    )
    # Reply and read cursors may remain as opaque composite references after a
    # capability-gated rolling DM cache evicts the target.  The migration
    # replaces these two global FKs with constraint triggers that retain the
    # same-channel invariant everywhere else.
    assert not has_foreign_key(
        "messages",
        (
            "referenced_message_id",
            "referenced_message_domain",
            "channel_id",
            "channel_domain",
        ),
        message_target,
    )
    assert not has_foreign_key(
        "read_states",
        ("last_message_id", "last_message_domain", "channel_id", "channel_domain"),
        message_target,
    )
    migration = (
        Path(__file__).parents[1] / "migrations/versions/b72c9e4a1f63_federated_dm_rolling_cache.py"
    ).read_text()
    assert "CREATE TRIGGER trg_messages_reply_reference" in migration
    assert "CREATE TRIGGER trg_read_states_last_message_reference" in migration
    assert "CREATE CONSTRAINT TRIGGER trg_messages_delete_reference" in migration

    assert has_foreign_key(
        "channels",
        ("last_message_id", "last_message_domain", "id", "origin_domain"),
        message_target,
    )
    assert has_foreign_key(
        "pins",
        ("message_id", "message_domain", "channel_id", "channel_domain"),
        message_target,
    )


def test_forum_and_thread_channel_metadata_is_bounded_and_contextual() -> None:
    channels = Base.metadata.tables["channels"]
    assert channels.c.topic.type.length == 4096
    assert channels.c.flags.nullable is False
    assert channels.c.e2ee_required.nullable is False
    assert channels.c.available_tags.nullable is False
    assert channels.c.applied_tag_ids.nullable is False
    assert channels.c.default_reaction_emoji.type.none_as_null is True
    reaction_bind = channels.c.default_reaction_emoji.type.bind_processor(postgresql.dialect())
    assert reaction_bind is not None
    assert reaction_bind(None) is None
    assert has_foreign_key(
        "channels",
        ("last_thread_id", "last_thread_domain", "guild_id", "guild_domain"),
        (
            "channels.id",
            "channels.origin_domain",
            "channels.guild_id",
            "channels.guild_domain",
        ),
    )
    assert {
        "ck_channels_thread_requires_unsynced_parent",
        "ck_channels_thread_metadata_context",
        "ck_channels_private_thread_invitable_context",
        "ck_channels_auto_archive_duration_value",
        "ck_channels_forum_metadata_context",
        "ck_channels_available_tags_value",
        "ck_channels_applied_tag_ids_value",
        "ck_channels_applied_tags_thread_only",
        "ck_channels_default_reaction_emoji_object",
        "ck_channels_default_sort_order_value",
        "ck_channels_default_forum_layout_value",
        "ck_channels_e2ee_required_context",
    } <= constraint_names("channels")

    channel_type = next(
        constraint
        for constraint in channels.constraints
        if constraint.name == "ck_channels_channel_type"
    )
    assert str(channel_type.sqltext) == "type IN (0,1,2,4,5,10,11,12,13,15,17)"
    indexes = {index.name: index for index in channels.indexes}
    assert "ix_channels_parent_activity" in indexes
    assert "ix_channels_thread_archive_due" in indexes
    starter_index = indexes["uq_channels_thread_starter_message"]
    assert starter_index.unique is True
    assert str(starter_index.dialect_options["postgresql"]["where"]) == (
        "type IN (10,11,12) AND starter_message_id IS NOT NULL"
    )


def test_thread_starter_and_membership_references_are_owner_scoped() -> None:
    assert {
        "ck_channels_owner_ref_complete",
        "ck_channels_starter_message_ref_complete",
    } <= constraint_names("channels")
    # Replicated structural snapshots may retain a starter identity without
    # retaining historical messages, so this is deliberately a checked,
    # unique logical reference rather than a physical message FK.
    assert not any(
        tuple(column.name for column in constraint.columns)
        == ("starter_message_id", "starter_message_domain")
        for constraint in Base.metadata.tables["channels"].foreign_key_constraints
    )
    assert not any(
        tuple(column.name for column in constraint.columns) == ("owner_id", "owner_domain")
        for constraint in Base.metadata.tables["channels"].foreign_key_constraints
    )

    members = Base.metadata.tables["thread_members"]
    assert tuple(members.primary_key.columns.keys()) == (
        "thread_id",
        "thread_domain",
        "user_id",
        "user_domain",
    )
    thread = foreign_key_for_columns(
        "thread_members", ("thread_id", "thread_domain", "guild_id", "guild_domain")
    )
    assert tuple(element.target_fullname for element in thread.elements) == (
        "channels.id",
        "channels.origin_domain",
        "channels.guild_id",
        "channels.guild_domain",
    )
    membership = foreign_key_for_columns(
        "thread_members", ("guild_id", "guild_domain", "user_id", "user_domain")
    )
    assert tuple(element.target_fullname for element in membership.elements) == (
        "guild_members.guild_id",
        "guild_members.guild_domain",
        "guild_members.user_id",
        "guild_members.user_domain",
    )
    assert thread.ondelete == membership.ondelete == "CASCADE"
    assert "ck_thread_members_notification_level_value" in constraint_names("thread_members")


def test_invites_webhooks_and_dm_conversations_bind_to_their_channel_identity() -> None:
    channel_guild_target = (
        "channels.id",
        "channels.origin_domain",
        "channels.guild_id",
        "channels.guild_domain",
    )
    assert has_foreign_key(
        "invites",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
        channel_guild_target,
    )
    invite_channel_fk = foreign_key_for_columns(
        "invites", ("channel_id", "channel_domain", "guild_id", "guild_domain")
    )
    assert invite_channel_fk.deferrable is True
    assert invite_channel_fk.initially == "DEFERRED"
    assert has_foreign_key(
        "webhooks",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
        channel_guild_target,
    )

    identity_fk = foreign_key_for_columns(
        "dm_conversations", ("id", "origin_domain", "channel_type")
    )
    assert tuple(element.target_fullname for element in identity_fk.elements) == (
        "channels.id",
        "channels.origin_domain",
        "channels.type",
    )
    assert identity_fk.deferrable is True
    assert identity_fk.initially == "DEFERRED"
    assert identity_fk.ondelete == "CASCADE"


def test_guild_owner_must_be_a_member_of_the_same_guild() -> None:
    owner_membership = foreign_key_for_columns(
        "guilds", ("id", "origin_domain", "owner_id", "owner_domain")
    )
    assert tuple(element.target_fullname for element in owner_membership.elements) == (
        "guild_members.guild_id",
        "guild_members.guild_domain",
        "guild_members.user_id",
        "guild_members.user_domain",
    )
    assert owner_membership.deferrable is True
    assert owner_membership.initially == "DEFERRED"


def test_channel_overwrites_bind_to_existing_targets_in_the_same_guild() -> None:
    assert has_foreign_key(
        "channel_overwrites",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
        (
            "channels.id",
            "channels.origin_domain",
            "channels.guild_id",
            "channels.guild_domain",
        ),
    )
    role_target = foreign_key_for_columns(
        "channel_overwrites",
        ("role_target_id", "role_target_domain", "guild_id", "guild_domain"),
    )
    assert tuple(element.target_fullname for element in role_target.elements) == (
        "roles.id",
        "roles.origin_domain",
        "roles.guild_id",
        "roles.guild_domain",
    )
    member_target = foreign_key_for_columns(
        "channel_overwrites",
        ("guild_id", "guild_domain", "member_target_id", "member_target_domain"),
    )
    assert tuple(element.target_fullname for element in member_target.elements) == (
        "guild_members.guild_id",
        "guild_members.guild_domain",
        "guild_members.user_id",
        "guild_members.user_domain",
    )
    assert role_target.ondelete == member_target.ondelete == "CASCADE"
    overwrites = Base.metadata.tables["channel_overwrites"]
    assert isinstance(overwrites.c.role_target_id.computed, Computed)
    assert isinstance(overwrites.c.member_target_id.computed, Computed)


def test_type_one_channels_and_dm_conversations_have_an_inverse_identity_fk() -> None:
    channels = Base.metadata.tables["channels"]
    assert any(
        isinstance(constraint, UniqueConstraint)
        and tuple(constraint.columns.keys()) == ("id", "origin_domain", "type")
        for constraint in channels.constraints
    )
    inverse = foreign_key_for_columns("channels", ("dm_conversation_id", "dm_conversation_domain"))
    assert tuple(element.target_fullname for element in inverse.elements) == (
        "dm_conversations.id",
        "dm_conversations.origin_domain",
    )
    assert inverse.ondelete == "CASCADE"
    assert inverse.deferrable is True
    assert inverse.initially == "DEFERRED"
    assert isinstance(channels.c.dm_conversation_id.computed, Computed)


def test_tracker_storage_is_channel_scoped_bounded_and_cascade_safe() -> None:
    boards = Base.metadata.tables["tracker_boards"]
    dispatch_outbox = Base.metadata.tables["tracker_dispatch_outbox"]
    lanes = Base.metadata.tables["tracker_lanes"]
    tasks = Base.metadata.tables["tracker_tasks"]

    assert tuple(boards.primary_key.columns.keys()) == ("channel_id", "channel_domain")
    board_type = foreign_key_for_columns(
        "tracker_boards", ("channel_id", "channel_domain", "channel_type")
    )
    assert tuple(element.target_fullname for element in board_type.elements) == (
        "channels.id",
        "channels.origin_domain",
        "channels.type",
    )
    assert board_type.ondelete == "CASCADE"
    outbox_board = foreign_key_for_columns(
        "tracker_dispatch_outbox",
        ("channel_id", "channel_domain", "guild_id", "guild_domain"),
    )
    assert outbox_board.ondelete == "CASCADE"
    assert {
        "ck_tracker_dispatch_outbox_attempts_nonnegative",
        "ck_tracker_dispatch_outbox_event_type_value",
    } <= constraint_names("tracker_dispatch_outbox")
    assert "ix_tracker_dispatch_outbox_due" in {index.name for index in dispatch_outbox.indexes}

    lane_board = foreign_key_for_columns(
        "tracker_lanes", ("channel_id", "channel_domain", "guild_id", "guild_domain")
    )
    task_board = foreign_key_for_columns(
        "tracker_tasks", ("channel_id", "channel_domain", "guild_id", "guild_domain")
    )
    assert lane_board.ondelete == task_board.ondelete == "CASCADE"
    task_lane = foreign_key_for_columns(
        "tracker_tasks", ("lane_id", "lane_domain", "channel_id", "channel_domain")
    )
    assert tuple(element.target_fullname for element in task_lane.elements) == (
        "tracker_lanes.id",
        "tracker_lanes.origin_domain",
        "tracker_lanes.channel_id",
        "tracker_lanes.channel_domain",
    )
    task_assignee_membership = foreign_key_for_columns(
        "tracker_tasks", ("guild_id", "guild_domain", "assignee_id", "assignee_domain")
    )
    assert tuple(element.target_fullname for element in task_assignee_membership.elements) == (
        "guild_members.guild_id",
        "guild_members.guild_domain",
        "guild_members.user_id",
        "guild_members.user_domain",
    )
    assert task_assignee_membership.ondelete == "SET NULL (assignee_id, assignee_domain)"

    lane_order = next(
        constraint
        for constraint in lanes.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_tracker_lanes_channel_position"
    )
    task_order = next(
        constraint
        for constraint in tasks.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_tracker_tasks_lane_position"
    )
    assert lane_order.deferrable is task_order.deferrable is True
    assert lane_order.initially == task_order.initially == "DEFERRED"
    assert {
        "ck_tracker_boards_channel_type",
        "ck_tracker_boards_key_prefix_format",
    } <= constraint_names("tracker_boards")
    assert "ck_tracker_lanes_position_range" in constraint_names("tracker_lanes")
    assert {
        "ck_tracker_tasks_position_range",
        "ck_tracker_tasks_client_idempotency_complete",
        "ck_tracker_tasks_assignee_ref_complete",
    } <= constraint_names("tracker_tasks")
    assert {
        "ix_tracker_tasks_channel_assignee",
        "ix_tracker_tasks_channel_due",
    } <= {index.name for index in tasks.indexes}


def test_tracker_dispatch_outbox_downgrade_refuses_to_discard_pending_events() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "f92a6c1d4b70_tracker_dispatch_outbox.py"
    ).read_text()
    guard = "IF EXISTS (SELECT 1 FROM tracker_dispatch_outbox)"
    assert guard in migration
    assert migration.index(guard) < migration.index('op.drop_table("tracker_dispatch_outbox")')


def test_security_sensitive_actor_and_origin_foreign_keys_exist() -> None:
    expected = (
        (
            "invites",
            ("channel_id", "channel_domain"),
            ("channels.id", "channels.origin_domain"),
        ),
        ("bans", ("actor_id", "actor_domain"), ("users.id", "users.origin_domain")),
        (
            "audit_log_entries",
            ("actor_id", "actor_domain"),
            ("users.id", "users.origin_domain"),
        ),
        (
            "emojis",
            ("creator_id", "creator_domain"),
            ("users.id", "users.origin_domain"),
        ),
        (
            "webhooks",
            ("guild_id", "guild_domain"),
            ("guilds.id", "guilds.origin_domain"),
        ),
        (
            "webhooks",
            ("creator_id", "creator_domain"),
            ("users.id", "users.origin_domain"),
        ),
        (
            "federation_outbox",
            ("destination",),
            ("instances.domain",),
        ),
        (
            "federation_inbox",
            ("origin_domain",),
            ("instances.domain",),
        ),
    )
    for table, local_columns, targets in expected:
        assert has_foreign_key(table, local_columns, targets), (table, local_columns)


def test_lifecycle_and_range_constraints_are_registered() -> None:
    required = {
        "instances": {
            "ck_instances_federation_mode_value",
            "ck_instances_capabilities_are_array",
        },
        "peer_keys": {
            "ck_peer_keys_expiry_after_fetch",
            "ck_peer_keys_retirement_after_fetch",
        },
        "sessions": {"ck_sessions_expiry_order"},
        "guilds": {"ck_guilds_event_sequence_order"},
        "channels": {"ck_channels_rate_limit_range", "ck_channels_dm_type_matches_guild"},
        "messages": {"ck_messages_content_length", "ck_messages_deleted_message_has_no_content"},
        "federation_outbox": {
            "ck_federation_outbox_status_value",
            "ck_federation_outbox_nonnegative_attempts",
        },
        "federation_inbox": {"ck_federation_inbox_status_value"},
        "remote_media_cache": {"ck_remote_media_cache_scan_status"},
    }
    for table, names in required.items():
        assert names <= constraint_names(table), table
