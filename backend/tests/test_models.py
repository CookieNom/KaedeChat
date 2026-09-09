import pytest
from sqlalchemy import CheckConstraint, Computed, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from app.core.permissions import ALL_PERMISSIONS
from app.db import models  # noqa: F401
from app.db.base import Base


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
    assert {"to", "recipient", "subject", "text", "html", "token"}.isdisjoint(outbox.columns.keys())
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
    partition = messages.dialect_options["postgresql"]["partition_by"]
    assert "".join(partition.lower().split()) == "range(id)"
    indexes = {
        (
            tuple(column.name for column in index.columns),
            index.dialect_options["postgresql"]["using"] or "btree",
        )
        for index in messages.indexes
    }
    assert {
        (("channel_id", "channel_domain", "id"), "btree"),
        (("author_id", "author_domain", "id"), "btree"),
        (("id",), "brin"),
    } <= indexes


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


async def test_role_and_overwrite_masks_exclude_reserved_permission_bits(
    postgres_schema, migrate_to
) -> None:
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

    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    await migrate_to("head")
    for table, columns in (
        ("roles", ("permissions",)),
        ("channel_overwrites", ("allow", "deny")),
        ("bot_applications", ("default_permissions",)),
        ("bot_install_templates", ("permissions",)),
        ("bot_installations", ("granted_permissions",)),
        ("bot_interactions", ("invocation_permissions",)),
    ):
        # Copy the executed checks; unrelated nullable fields isolate mask validation.
        await postgres_schema.execute(
            text(f"CREATE TABLE mask_probe (LIKE {table} INCLUDING CONSTRAINTS)")
        )
        for column in Base.metadata.tables[table].columns:
            await postgres_schema.execute(
                text(f'ALTER TABLE mask_probe ALTER COLUMN "{column.name}" DROP NOT NULL')
            )
        mask_check = {
            "roles": "ck_roles_known_permission_mask",
            "channel_overwrites": "ck_channel_overwrites_known_permission_masks",
            "bot_applications": "ck_bot_applications_bot_application_positive_values",
            "bot_install_templates": "ck_bot_install_templates_bot_template_positive_values",
            "bot_installations": "ck_bot_installations_bot_installation_positive_values",
            "bot_interactions": (
                "ck_bot_interactions_bot_interaction_invocation_permissions_nonnegative"
            ),
        }[table]
        checks = await postgres_schema.scalars(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='mask_probe'::regclass AND contype='c'"
            )
        )
        checks = list(checks)
        assert sum(check.startswith(mask_check[:50]) for check in checks) == 1
        for check in checks:
            if not check.startswith(mask_check[:50]):
                await postgres_schema.execute(
                    text(f'ALTER TABLE mask_probe DROP CONSTRAINT "{check}"')
                )
        if table == "channel_overwrites":
            await postgres_schema.execute(
                text('ALTER TABLE mask_probe ALTER COLUMN "allow" SET DEFAULT 0')
            )
            await postgres_schema.execute(
                text('ALTER TABLE mask_probe ALTER COLUMN "deny" SET DEFAULT 0')
            )
        for column in columns:
            insert = text(f'INSERT INTO mask_probe ("{column}") VALUES (:mask)')  # noqa: S608 -- fixed columns
            await postgres_schema.execute(insert, {"mask": ALL_PERMISSIONS})
            # Historical migrations forbid bit 19 and bits beyond their 59-bit range.
            for bit in (19, 59):
                with pytest.raises(IntegrityError) as rejected:
                    async with postgres_schema.begin_nested():
                        await postgres_schema.execute(insert, {"mask": 1 << bit})
                assert rejected.value.orig.sqlstate == "23514"
        await postgres_schema.execute(text("DROP TABLE mask_probe"))


@pytest.mark.parametrize("migration_case", ["foundation", "lease"])
async def test_message_parity_columns_and_authoritative_install_source_are_registered(
    postgres_schema, migrate_to, migration_case
) -> None:
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
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    def apply(sync, module, direction):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            getattr(module, direction)()

    if migration_case == "lease":
        for statement in (
            "CREATE TABLE users (id bigint,origin_domain varchar(253),is_local boolean,UNIQUE"
            "(id,origin_domain,is_local))",
            "CREATE TABLE developer_teams (id bigint,origin_domain varchar(253),PRIMARY KEY(i"
            "d,origin_domain))",
            "CREATE TABLE bot_user_installations (id bigint,user_id bigint,user_domain varcha"
            "r(253),status varchar(16),source_id bigint,source_domain varchar(253))",
            "INSERT INTO users VALUES (1,'local.example',true),(1,'remote.example',false)",
            "INSERT INTO bot_user_installations VALUES (50,1,'local.example','active',NULL,NU"
            "LL),(51,1,'remote.example','active',777,'remote.example')",
        ):
            await postgres_schema.execute(text(statement))
        module = import_module(
            "migrations.versions.f95b2c3d8e41_developer_team_snapshot_highwaters"
        )
        await postgres_schema.run_sync(lambda sync: apply(sync, module, "upgrade"))
        now = await postgres_schema.scalar(text("SELECT CURRENT_TIMESTAMP"))
        assert (
            await postgres_schema.execute(
                text(
                    "SELECT id,authority_expires_at,source_id,source_domain FROM bot_user_ins"
                    "tallations ORDER BY id"
                )
            )
        ).all() == [(50, None, None, None), (51, now, 777, "remote.example")]
        indexes = await postgres_schema.run_sync(
            lambda sync: inspect(sync).get_indexes("bot_user_installations")
        )
        assert "ix_bot_user_installations_authority_expiry" in {index["name"] for index in indexes}
        return
    await migrate_to("fb7c3e9a1d42")
    for statement in (
        "INSERT INTO instances (domain,is_self,current_key_id,encrypted_private_key,private_k"
        "ey_nonce) VALUES ('test.example',true,'main',decode(repeat('ab',32),'hex'),decode(re"
        "peat('cd',12),'hex'))",
        "INSERT INTO users (id,origin_domain,username,is_local,password_hash,password_kdf_ver"
        "sion,password_auth_salt,e2ee_vault_salt) VALUES (7,'test.example','owner',true,'hash"
        "',2,decode(repeat('ab',16),'hex'),decode(repeat('cd',16),'hex'))",
        "INSERT INTO guilds (id,origin_domain,name,owner_id,owner_domain) VALUES (2,'test.exa"
        "mple','Guild',7,'test.example')",
        "INSERT INTO guild_members (guild_id,guild_domain,user_id,user_domain,joined_at) VALU"
        "ES (2,'test.example',7,'test.example',now())",
        "INSERT INTO channels (id,origin_domain,guild_id,guild_domain,type,name,created_floor"
        "_id) VALUES (1,'test.example',2,'test.example',0,'Room',1)",
        "INSERT INTO messages (id,origin_domain,channel_id,channel_domain,author_id,author_do"
        "main,content) VALUES (10,'test.example',1,'test.example',7,'test.example','original'"
        ")",
        "INSERT INTO messages (id,origin_domain,channel_id,channel_domain,author_id,author_do"
        "main,content,message_type,referenced_message_id,referenced_message_domain,client_non"
        "ce) VALUES (11,'test.example',1,'test.example',7,'test.example','pin notice',6,10,'t"
        "est.example',NULL),(12,'test.example',1,'test.example',7,'test.example','legacy acti"
        "vity',12,NULL,NULL,NULL),(13,'test.example',1,'test.example',7,'test.example','proxi"
        "ed',0,NULL,NULL,'receipt'),(14,'test.example',1,'test.example',7,'test.example','no "
        "nonce',0,NULL,NULL,NULL)",
    ):
        await postgres_schema.execute(text(statement))
    import json

    for seq, message_id in ((7, 13), (5, 13), (9, 14)):
        await postgres_schema.execute(
            text(
                "INSERT INTO guild_events (guild_id,guild_domain,seq,event_id,envelope) VALUE"
                "S (2,'test.example',:seq,:event_id,CAST(:envelope AS jsonb))"
            ),
            dict(
                seq=seq,
                event_id=f"event{seq}",
                envelope=json.dumps(
                    {
                        "type": "guild.message.committed",
                        "content": {"message": {"id": str(message_id)}},
                    }
                ),
            ),
        )
    module = import_module("migrations.versions.fc9a4b7d2e10_bot_parity_foundation")
    await postgres_schema.run_sync(lambda sync: apply(sync, module, "upgrade"))
    assert await postgres_schema.scalar(
        text("SELECT message_reference FROM messages WHERE id=11")
    ) == {
        "type": 0,
        "message_id": "10",
        "message_domain": "test.example",
        "channel_id": "1",
        "channel_domain": "test.example",
        "guild_id": "2",
        "guild_domain": "test.example",
    }
    assert (
        await postgres_schema.execute(text("SELECT message_type,content FROM messages WHERE id=12"))
    ).one() == (0, "legacy activity")
    assert (
        await postgres_schema.execute(
            text(
                "SELECT id,proxy_commit_seq,proxy_request_fingerprint FROM messages WHERE id "
                "IN (13,14) ORDER BY id"
            )
        )
    ).all() == [(13, 5, None), (14, None, None)]
    columns = await postgres_schema.run_sync(lambda sync: inspect(sync).get_columns("attachments"))
    assert {"upload_channel_id", "upload_channel_domain", "duration_secs", "waveform"} <= {
        column["name"] for column in columns
    }
    await postgres_schema.run_sync(lambda sync: apply(sync, module, "downgrade"))
    assert (
        await postgres_schema.execute(text("SELECT id,content FROM messages ORDER BY id"))
    ).all() == [
        (10, "original"),
        (11, "pin notice"),
        (12, "legacy activity"),
        (13, "proxied"),
        (14, "no nonce"),
    ]
    columns = await postgres_schema.run_sync(lambda sync: inspect(sync).get_columns("messages"))
    assert not {"message_reference", "proxy_commit_seq", "proxy_request_fingerprint"} & {
        column["name"] for column in columns
    }


@pytest.mark.asyncio
async def test_bot_dm_runtime_lineage_and_terminal_highwaters_are_registered(migrate_to) -> None:
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

    from datetime import UTC, datetime

    from sqlalchemy import insert, inspect, select, update
    from sqlalchemy.exc import IntegrityError

    connection = await migrate_to("fc9a4b7d2e10")
    columns = await connection.run_sync(
        lambda sync: {item["name"] for item in inspect(sync).get_columns("bot_dm_capabilities")}
    )
    assert "target_access_revocation_generation" in columns
    assert "access_revocation_generation" not in columns
    indexes = await connection.run_sync(
        lambda sync: {
            item["name"] for item in inspect(sync).get_indexes("bot_dm_capability_highwaters")
        }
    )
    assert "ix_bot_dm_capability_highwaters_authority_expiry" in indexes
    grant = "kbdg_" + "a" * 43
    await connection.execute(
        insert(capability_highwaters).values(
            grant_id=grant,
            installation_authority_domain="apps.example",
            identity_fingerprint=b"i" * 32,
            revision=1,
            authorization_fingerprint=b"a" * 32,
            status="revoked",
            expires_at=datetime.now(UTC),
        )
    )
    await connection.execute(
        insert(runtime_highwaters).values(
            application_id=1,
            application_domain="apps.example",
            target_domain="target.example",
            bot_user_id=2,
            bot_user_domain="apps.example",
            manifest_generation=1,
            revocation_generation=1,
            access_revocation_generation=0,
            status="deleted",
            target_allowed=False,
            runtime_fingerprint=b"r" * 32,
            expires_at=datetime.now(UTC),
        )
    )
    for table, column in (
        (capability_highwaters, "identity_fingerprint"),
        (capability_highwaters, "authorization_fingerprint"),
        (runtime_highwaters, "runtime_fingerprint"),
    ):
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(update(table).values({column: b"short"}))
    assert await connection.scalar(select(capability_highwaters.c.status)) == "revoked"
    assert await connection.scalar(select(runtime_highwaters.c.status)) == "deleted"


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


@pytest.mark.asyncio
async def test_bot_parity_migration_drops_attachment_checks_before_their_columns(
    migrate_to,
) -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    connection = await migrate_to("fc9a4b7d2e10")
    migration = importlib.import_module("migrations.versions.fc9a4b7d2e10_bot_parity_foundation")

    def downgrade(sync):
        before = {column["name"] for column in inspect(sync).get_columns("attachments")}
        assert {"upload_channel_domain", "upload_channel_id", "duration_secs", "waveform"} <= before
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()
        after = {column["name"] for column in inspect(sync).get_columns("attachments")}
        assert (
            not {"upload_channel_domain", "upload_channel_id", "duration_secs", "waveform"} & after
        )
        assert "id" in after

    await connection.run_sync(downgrade)


async def test_bot_parity_downgrade_refuses_to_discard_feature_data(migrate_to) -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import IntegrityError

    from app.db.base import Base

    connection = await migrate_to("fc9a4b7d2e10")
    migration = importlib.import_module("migrations.versions.fc9a4b7d2e10_bot_parity_foundation")

    def downgrade(sync):
        sync.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()

    for statement in (
        "INSERT INTO instances (domain, is_self, current_key_id, encrypted_private_key, "
        "private_key_nonce) VALUES ('test.example', true, 'ed25519:test', "
        "decode(repeat('ab', 32), 'hex'), decode(repeat('cd', 12), 'hex'))",
        "INSERT INTO users (id, origin_domain, username, is_local, password_hash, "
        "password_kdf_version, password_auth_salt, e2ee_vault_salt) VALUES "
        "(7, 'test.example', 'owner', true, 'hash', 2, "
        "decode(repeat('ab', 16), 'hex'), decode(repeat('cd', 16), 'hex'))",
        "INSERT INTO guilds (id, origin_domain, name, owner_id, owner_domain) VALUES (2, "
        "'test.example', 'Guild', 7, 'test.example')",
        "INSERT INTO guild_members (guild_id, guild_domain, user_id, user_domain, "
        "joined_at) VALUES"
        "(2, 'test.example', 7, 'test.example', now())",
        "INSERT INTO channels (id, origin_domain, guild_id, guild_domain, type, name, "
        "created_floor_id) VALUES (1, 'test.example', 2, 'test.example', 0, 'Room', 1)",
        "INSERT INTO messages (id, origin_domain, channel_id, channel_domain, author_id, "
        "author_domain, content) VALUES (10, 'test.example', 1, 'test.example', 7, "
        "'test.example', 'original')",
    ):
        await connection.execute(text(statement))

    # Each populated feature independently blocks the real downgrade before DDL.
    for mutation in (
        "UPDATE guilds SET community_enabled = true WHERE id = 2",
        "UPDATE channels SET type = 13, bitrate = 64000, user_limit = 0, video_quality_mode "
        "= 1 WHERE id = 1",
        "UPDATE messages SET tts = true WHERE id = 10",
        "UPDATE users SET age_assurance_state = 'adult' WHERE id = 7",
    ):
        savepoint = await connection.begin_nested()
        try:
            await connection.execute(text(mutation))
            with pytest.raises(IntegrityError, match="cannot downgrade fc9a4b7d2e10"):
                async with connection.begin_nested():
                    await connection.run_sync(downgrade)
            assert "sticker_items" in await connection.run_sync(
                lambda sync: {column["name"] for column in inspect(sync).get_columns("messages")}
            )
            assert (
                await connection.scalar(text("SELECT content FROM messages WHERE id = 10"))
                == "original"
            )
        finally:
            await savepoint.rollback()
    await connection.run_sync(downgrade)
    assert "sticker_items" not in await connection.run_sync(
        lambda sync: {column["name"] for column in inspect(sync).get_columns("messages")}
    )
    assert await connection.scalar(text("SELECT content FROM messages WHERE id = 10")) == "original"


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
    # Executed reply/read/delete trigger behavior and downgrade preservation are
    # covered by test_rolling_cache_migration_preserves_reference_integrity_outside_opaque_prefix.
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


@pytest.mark.asyncio
async def test_tracker_dispatch_outbox_downgrade_refuses_to_discard_pending_events(
    migrate_to,
) -> None:
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    connection = await migrate_to("f92a6c1d4b70")
    migration = importlib.import_module("migrations.versions.f92a6c1d4b70_tracker_dispatch_outbox")
    for statement in (
        "INSERT INTO instances (domain) VALUES ('test.example')",
        "INSERT INTO users (id, origin_domain, username, is_local, "
        "federation_introduced_by_domain) VALUES "
        "(7, 'test.example', 'owner', false, 'test.example')",
        "INSERT INTO guilds (id, origin_domain, name, owner_id, owner_domain) VALUES (2, "
        "'test.example', 'Guild', 7, 'test.example')",
        "INSERT INTO channels (id, origin_domain, guild_id, guild_domain, type, name, "
        "created_floor_id) VALUES (1, 'test.example', 2, 'test.example', 17, 'Tracker', 1)",
        "INSERT INTO tracker_boards (channel_id, channel_domain, guild_id, guild_domain, "
        "key_prefix) VALUES (1, 'test.example', 2, 'test.example', 'TASK')",
    ):
        await connection.execute(text(statement))
    await connection.execute(
        text(
            "INSERT INTO tracker_dispatch_outbox (channel_id, channel_domain, guild_id, "
            "guild_domain, event_type, payload) VALUES (1, 'test.example', 2, "
            "'test.example', 'TRACKER_BOARD_UPDATE', '{}')"
        )
    )

    def downgrade(sync):
        with Operations.context(
            MigrationContext.configure(sync, opts={"target_metadata": Base.metadata})
        ):
            migration.downgrade()

    with pytest.raises(DBAPIError, match="drain tracker dispatch outbox"):
        async with connection.begin_nested():
            await connection.run_sync(downgrade)
    assert await connection.scalar(text("SELECT count(*) FROM tracker_dispatch_outbox")) == 1
    await connection.execute(text("DELETE FROM tracker_dispatch_outbox"))
    await connection.run_sync(downgrade)
    assert await connection.scalar(text("SELECT to_regclass('tracker_dispatch_outbox')")) is None


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
