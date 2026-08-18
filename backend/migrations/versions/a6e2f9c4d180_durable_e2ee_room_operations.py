"""Persist idempotent two-phase E2EE room operations and package claims.

Revision ID: a6e2f9c4d180
Revises: 9f3a7b1c5d82
Create Date: 2026-08-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a6e2f9c4d180"
down_revision: str | None = "9f3a7b1c5d82"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_control_capture_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION kaede_capture_e2ee_control_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.e2ee IS NOT NULL
             AND NEW.e2ee->>'operation' IN ('welcome', 'commit')
             AND NEW.encryption_epoch IS NOT NULL THEN
            INSERT INTO e2ee_control_records (
              id, origin_domain, channel_id, channel_domain,
              author_id, author_domain, policy_generation, epoch,
              operation, apply_mode, envelope, created_at
            ) VALUES (
              NEW.id, NEW.origin_domain, NEW.channel_id, NEW.channel_domain,
              NEW.author_id, NEW.author_domain, NEW.encryption_policy_generation,
              NEW.encryption_epoch, NEW.e2ee->>'operation',
              CASE WHEN NEW.e2ee->>'operation' = 'welcome' THEN 'join' ELSE 'process' END,
              NEW.e2ee, NEW.created_at
            ) ON CONFLICT (id, origin_domain) DO NOTHING;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER capture_e2ee_control_record
        AFTER INSERT OR UPDATE OF e2ee ON messages
        FOR EACH ROW EXECUTE FUNCTION kaede_capture_e2ee_control_record()
        """
    )


def _create_legacy_control_capture_trigger() -> None:
    op.execute(
        """
        CREATE FUNCTION kaede_capture_e2ee_control_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.e2ee IS NOT NULL
             AND NEW.e2ee->>'operation' IN ('welcome', 'commit')
             AND NEW.encryption_epoch IS NOT NULL THEN
            INSERT INTO e2ee_control_records (
              id, origin_domain, channel_id, channel_domain,
              author_id, author_domain, policy_generation, epoch,
              operation, envelope, created_at
            ) VALUES (
              NEW.id, NEW.origin_domain, NEW.channel_id, NEW.channel_domain,
              NEW.author_id, NEW.author_domain, NEW.encryption_policy_generation,
              NEW.encryption_epoch, NEW.e2ee->>'operation', NEW.e2ee, NEW.created_at
            ) ON CONFLICT (id, origin_domain) DO NOTHING;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER capture_e2ee_control_record
        AFTER INSERT OR UPDATE OF e2ee ON messages
        FOR EACH ROW EXECUTE FUNCTION kaede_capture_e2ee_control_record()
        """
    )


def upgrade() -> None:
    op.add_column(
        "e2ee_key_packages",
        sa.Column("claimed_operation_id", sa.String(length=47), nullable=True),
    )
    op.add_column(
        "e2ee_key_packages",
        sa.Column("claimed_operation_domain", sa.String(length=253), nullable=True),
    )
    # Consumed packages are already unusable. Dropping only those historical
    # rows avoids inventing an operation identity during the protocol cutover.
    op.execute("DELETE FROM e2ee_key_packages WHERE claimed_at IS NOT NULL")
    op.create_check_constraint(
        "e2ee_key_package_operation_complete",
        "e2ee_key_packages",
        "(claimed_operation_id IS NULL) = (claimed_operation_domain IS NULL)",
    )
    op.create_check_constraint(
        "e2ee_key_package_claim_has_operation",
        "e2ee_key_packages",
        "claimed_at IS NULL OR claimed_operation_id IS NOT NULL",
    )
    op.create_check_constraint(
        "e2ee_key_package_operation_id_format",
        "e2ee_key_packages",
        "claimed_operation_id IS NULL OR claimed_operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
    )
    op.create_index(
        "ix_e2ee_key_packages_operation",
        "e2ee_key_packages",
        ["claimed_operation_id", "claimed_operation_domain"],
        postgresql_where=sa.text("claimed_operation_id IS NOT NULL"),
    )

    op.create_table(
        "e2ee_room_operations",
        sa.Column("id", sa.String(length=47), nullable=False),
        sa.Column("authority_domain", sa.String(length=253), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_domain", sa.String(length=253), nullable=False),
        sa.Column("sender_device_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            server_default="claiming",
            nullable=False,
        ),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("base_policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("policy_generation", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.String(length=43), nullable=False),
        sa.Column("participant_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "key_packages",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("prepared_response", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("activation_request_digest", sa.LargeBinary(length=32)),
        sa.Column("prepared_vault_revision", sa.BigInteger()),
        sa.Column("prepared_vault_digest", sa.LargeBinary(length=32)),
        sa.Column("committed_response", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('activate','rekey')",
            name="e2ee_room_operation_kind_value",
        ),
        sa.CheckConstraint(
            "id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="e2ee_room_operation_id_format",
        ),
        sa.CheckConstraint(
            "sender_device_id ~ '^ked_[A-Za-z0-9_-]{43}$'",
            name="e2ee_room_operation_sender_device_id_format",
        ),
        sa.CheckConstraint(
            "status IN ('claiming','prepared','committed','failed')",
            name="e2ee_room_operation_status_value",
        ),
        sa.CheckConstraint(
            "octet_length(request_digest) = 32",
            name="e2ee_room_operation_request_digest_length",
        ),
        sa.CheckConstraint(
            "activation_request_digest IS NULL OR octet_length(activation_request_digest) = 32",
            name="e2ee_room_operation_activation_digest_length",
        ),
        sa.CheckConstraint(
            "prepared_vault_digest IS NULL OR octet_length(prepared_vault_digest) = 32",
            name="e2ee_room_operation_vault_digest_length",
        ),
        sa.CheckConstraint(
            "base_policy_generation >= 0 AND policy_generation > base_policy_generation",
            name="e2ee_room_operation_generation_order",
        ),
        sa.CheckConstraint(
            "char_length(group_id) = 43",
            name="e2ee_room_operation_group_id_length",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(participant_refs) = 'array' AND jsonb_typeof(key_packages) = 'array'",
            name="e2ee_room_operation_collections",
        ),
        sa.CheckConstraint(
            "prepared_response IS NULL OR jsonb_typeof(prepared_response) = 'object'",
            name="e2ee_room_operation_prepared_response_object",
        ),
        sa.CheckConstraint(
            "committed_response IS NULL OR jsonb_typeof(committed_response) = 'object'",
            name="e2ee_room_operation_committed_response_object",
        ),
        sa.CheckConstraint(
            "(status <> 'prepared' OR prepared_response IS NOT NULL) AND "
            "(status <> 'committed' OR (prepared_response IS NOT NULL AND "
            "activation_request_digest IS NOT NULL AND prepared_vault_revision IS NOT NULL AND "
            "prepared_vault_revision > 0 AND prepared_vault_digest IS NOT NULL AND "
            "committed_response IS NOT NULL AND committed_at IS NOT NULL))",
            name="e2ee_room_operation_status_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_e2ee_room_operations_active_channel",
        "e2ee_room_operations",
        ["channel_id", "channel_domain"],
        unique=True,
        postgresql_where=sa.text("status IN ('claiming','prepared')"),
    )
    op.create_index(
        "ix_e2ee_room_operations_actor",
        "e2ee_room_operations",
        ["actor_id", "actor_domain", "created_at"],
    )

    op.create_table(
        "e2ee_package_claim_batches",
        sa.Column("operation_id", sa.String(length=47), nullable=False),
        sa.Column("operation_domain", sa.String(length=253), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("target_domain", sa.String(length=253), nullable=False),
        sa.Column("target_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("channel_domain", sa.String(length=253), nullable=False),
        sa.Column("claimant_id", sa.BigInteger(), nullable=False),
        sa.Column("claimant_domain", sa.String(length=253), nullable=False),
        sa.Column("excluded_device_id", sa.String(length=64)),
        sa.Column("max_devices", sa.SmallInteger(), nullable=False),
        sa.Column("request_digest", sa.LargeBinary(length=32), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "target_is_local",
            name="e2ee_package_claim_batch_target_is_local",
        ),
        sa.CheckConstraint(
            "operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="e2ee_package_claim_batch_operation_id_format",
        ),
        sa.CheckConstraint(
            "octet_length(request_digest) = 32",
            name="e2ee_package_claim_batch_request_digest_length",
        ),
        sa.CheckConstraint(
            "max_devices BETWEEN 1 AND 48",
            name="e2ee_package_claim_batch_max_devices",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(response) = 'object'",
            name="e2ee_package_claim_batch_response_object",
        ),
        sa.ForeignKeyConstraint(
            ["target_id", "target_domain", "target_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "operation_id",
            "operation_domain",
            "target_id",
            "target_domain",
        ),
    )
    op.create_index(
        "ix_e2ee_package_claim_batches_channel",
        "e2ee_package_claim_batches",
        ["channel_id", "channel_domain", "created_at"],
    )

    op.execute("DROP TRIGGER IF EXISTS capture_e2ee_control_record ON messages")
    op.execute("DROP FUNCTION IF EXISTS kaede_capture_e2ee_control_record()")
    op.add_column(
        "e2ee_control_records",
        sa.Column("apply_mode", sa.String(length=16), server_default="process", nullable=False),
    )
    op.add_column(
        "e2ee_control_records",
        sa.Column("room_operation_id", sa.String(length=47)),
    )
    op.add_column(
        "e2ee_control_records",
        sa.Column("room_operation_domain", sa.String(length=253)),
    )
    op.execute("UPDATE e2ee_control_records SET apply_mode = 'join' WHERE operation = 'welcome'")
    # The default exists only so the new non-null column can be introduced over
    # existing rows. Future writers must choose join/process/audit explicitly.
    op.alter_column("e2ee_control_records", "apply_mode", server_default=None)
    op.create_check_constraint(
        "apply_mode_value",
        "e2ee_control_records",
        "(operation = 'welcome' AND apply_mode = 'join') OR "
        "(operation = 'commit' AND apply_mode IN ('process','audit'))",
    )
    op.create_check_constraint(
        "room_operation_ref_complete",
        "e2ee_control_records",
        "(room_operation_id IS NULL) = (room_operation_domain IS NULL)",
    )
    op.create_check_constraint(
        "room_operation_id_format",
        "e2ee_control_records",
        "room_operation_id IS NULL OR room_operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
    )
    op.create_unique_constraint(
        "uq_e2ee_control_records_room_operation_kind",
        "e2ee_control_records",
        ["room_operation_id", "room_operation_domain", "operation"],
    )
    _create_control_capture_trigger()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS capture_e2ee_control_record ON messages")
    op.execute("DROP FUNCTION IF EXISTS kaede_capture_e2ee_control_record()")
    op.drop_constraint(
        "uq_e2ee_control_records_room_operation_kind",
        "e2ee_control_records",
        type_="unique",
    )
    op.drop_constraint(
        op.f("ck_e2ee_control_records_room_operation_id_format"),
        "e2ee_control_records",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_e2ee_control_records_room_operation_ref_complete"),
        "e2ee_control_records",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_e2ee_control_records_apply_mode_value"),
        "e2ee_control_records",
        type_="check",
    )
    op.drop_column("e2ee_control_records", "room_operation_domain")
    op.drop_column("e2ee_control_records", "room_operation_id")
    op.drop_column("e2ee_control_records", "apply_mode")
    _create_legacy_control_capture_trigger()

    op.drop_index(
        "ix_e2ee_package_claim_batches_channel",
        table_name="e2ee_package_claim_batches",
    )
    op.drop_table("e2ee_package_claim_batches")
    op.drop_index("ix_e2ee_room_operations_actor", table_name="e2ee_room_operations")
    op.drop_index(
        "uq_e2ee_room_operations_active_channel",
        table_name="e2ee_room_operations",
    )
    op.drop_table("e2ee_room_operations")

    op.drop_index("ix_e2ee_key_packages_operation", table_name="e2ee_key_packages")
    op.drop_constraint(
        op.f("ck_e2ee_key_packages_e2ee_key_package_operation_id_format"),
        "e2ee_key_packages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_e2ee_key_packages_e2ee_key_package_claim_has_operation"),
        "e2ee_key_packages",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_e2ee_key_packages_e2ee_key_package_operation_complete"),
        "e2ee_key_packages",
        type_="check",
    )
    op.drop_column("e2ee_key_packages", "claimed_operation_domain")
    op.drop_column("e2ee_key_packages", "claimed_operation_id")
