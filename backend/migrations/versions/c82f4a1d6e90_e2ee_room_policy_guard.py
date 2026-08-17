"""add downgrade-resistant E2EE room policy guard

Revision ID: c82f4a1d6e90
Revises: ab71e2c9d430
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c82f4a1d6e90"
down_revision: str | None = "ab71e2c9d430"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channels",
        sa.Column(
            "encryption_state", sa.String(length=16), nullable=False, server_default="plaintext"
        ),
    )
    op.add_column(
        "channels",
        sa.Column(
            "encryption_policy_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("channels", sa.Column("encryption_protocol", sa.String(length=32)))
    op.add_column("channels", sa.Column("encryption_suite", sa.String(length=96)))
    op.add_column("channels", sa.Column("encryption_group_id", sa.String(length=128)))
    op.add_column("channels", sa.Column("encryption_epoch", sa.BigInteger()))
    op.add_column(
        "channels",
        sa.Column("encryption_activated_at", sa.DateTime(timezone=True)),
    )
    op.execute("UPDATE channels SET encryption_state = 'legacy' WHERE encryption_mode = 'e2ee'")
    op.create_check_constraint(
        "channel_encryption_state_value",
        "channels",
        "encryption_state IN "
        "('plaintext','legacy','proposed','activating','active','rekeying','failed')",
    )
    op.create_check_constraint(
        "channel_encryption_generation_nonnegative",
        "channels",
        "encryption_policy_generation >= 0",
    )
    op.create_check_constraint(
        "channel_encryption_epoch_nonnegative",
        "channels",
        "encryption_epoch IS NULL OR encryption_epoch >= 0",
    )
    op.create_check_constraint(
        "channel_encryption_policy_consistent",
        "channels",
        "(encryption_policy_generation = 0 AND "
        "((encryption_state = 'plaintext' AND encryption_mode = 'plaintext') OR "
        "(encryption_state = 'legacy' AND encryption_mode = 'e2ee')) AND "
        "encryption_protocol IS NULL AND encryption_suite IS NULL AND "
        "encryption_group_id IS NULL AND encryption_epoch IS NULL) OR "
        "(encryption_policy_generation > 0 AND encryption_protocol = 'mls10' AND "
        "encryption_suite = 'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519' AND "
        "encryption_group_id IS NOT NULL AND "
        "((encryption_state IN ('proposed','failed') AND "
        "encryption_mode = 'plaintext' AND encryption_epoch IS NULL) OR "
        "(encryption_state IN ('activating','active','rekeying','failed') AND "
        "encryption_mode = 'e2ee' AND encryption_epoch IS NOT NULL)))",
    )

    op.execute(
        """
        CREATE FUNCTION kaede_enforce_channel_encryption_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.encryption_policy_generation < OLD.encryption_policy_generation THEN
                RAISE EXCEPTION 'channel encryption policy generation regressed'
                    USING ERRCODE = '23514';
            END IF;

            IF OLD.encryption_mode = 'e2ee' AND NEW.encryption_mode <> 'e2ee' THEN
                RAISE EXCEPTION 'encrypted channel cannot be downgraded to plaintext'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.encryption_policy_generation = OLD.encryption_policy_generation
               AND (NEW.encryption_protocol IS DISTINCT FROM OLD.encryption_protocol
                    OR NEW.encryption_suite IS DISTINCT FROM OLD.encryption_suite
                    OR NEW.encryption_group_id IS DISTINCT FROM OLD.encryption_group_id) THEN
                RAISE EXCEPTION 'channel encryption policy was equivocated'
                    USING ERRCODE = '23514';
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_channels_encryption_transition
        BEFORE UPDATE OF encryption_mode, encryption_state,
                         encryption_policy_generation, encryption_protocol,
                         encryption_suite, encryption_group_id, encryption_epoch
        ON channels
        FOR EACH ROW
        EXECUTE FUNCTION kaede_enforce_channel_encryption_transition()
        """
    )

    op.add_column(
        "messages",
        sa.Column(
            "encryption_policy_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column("messages", sa.Column("encryption_epoch", sa.BigInteger()))
    op.create_check_constraint(
        "message_encryption_generation_nonnegative",
        "messages",
        "encryption_policy_generation >= 0",
    )
    op.create_check_constraint(
        "message_encryption_epoch_nonnegative",
        "messages",
        "encryption_epoch IS NULL OR encryption_epoch >= 0",
    )

    # This is deliberately a database admission rule. An application rollback
    # or an overlooked federation path must not be able to downgrade an E2EE
    # room by inserting plaintext. Old rows are untouched, so activation does
    # not make pre-activation history appear encrypted.
    op.execute(
        """
        CREATE FUNCTION kaede_enforce_message_encryption_policy()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            room_mode text;
            room_generation bigint;
            room_epoch bigint;
            body_changed boolean;
        BEGIN
            SELECT encryption_mode, encryption_policy_generation, encryption_epoch
              INTO room_mode, room_generation, room_epoch
              FROM channels
             WHERE id = NEW.channel_id
               AND origin_domain = NEW.channel_domain;

            IF room_mode IS NULL THEN
                RAISE EXCEPTION 'message channel encryption policy is missing'
                    USING ERRCODE = '23514';
            END IF;

            IF NEW.deleted_at IS NULL THEN
                IF room_mode = 'plaintext' AND NEW.e2ee IS NOT NULL THEN
                    RAISE EXCEPTION 'encrypted body is forbidden in a plaintext channel'
                        USING ERRCODE = '23514';
                ELSIF room_mode = 'e2ee' AND (NEW.content IS NOT NULL OR NEW.e2ee IS NULL) THEN
                    RAISE EXCEPTION 'plaintext body is forbidden in an encrypted channel'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            IF TG_OP = 'INSERT' THEN
                body_changed := true;
            ELSE
                body_changed := NEW.content IS DISTINCT FROM OLD.content
                             OR NEW.e2ee IS DISTINCT FROM OLD.e2ee;
            END IF;

            IF NEW.deleted_at IS NULL AND body_changed THEN
                IF NEW.encryption_policy_generation <> room_generation THEN
                    RAISE EXCEPTION 'message encryption policy generation is stale'
                        USING ERRCODE = '23514';
                END IF;
                IF room_mode = 'e2ee' AND NEW.encryption_epoch IS DISTINCT FROM room_epoch THEN
                    RAISE EXCEPTION 'message encryption epoch is stale'
                        USING ERRCODE = '23514';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_messages_encryption_policy
        BEFORE INSERT OR UPDATE OF content, e2ee, deleted_at,
                                   encryption_policy_generation, encryption_epoch,
                                   channel_id, channel_domain
        ON messages
        FOR EACH ROW
        EXECUTE FUNCTION kaede_enforce_message_encryption_policy()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_channels_encryption_transition ON channels")
    op.execute("DROP FUNCTION IF EXISTS kaede_enforce_channel_encryption_transition()")
    op.execute("DROP TRIGGER IF EXISTS trg_messages_encryption_policy ON messages")
    op.execute("DROP FUNCTION IF EXISTS kaede_enforce_message_encryption_policy()")
    op.drop_constraint("message_encryption_epoch_nonnegative", "messages", type_="check")
    op.drop_constraint("message_encryption_generation_nonnegative", "messages", type_="check")
    op.drop_column("messages", "encryption_epoch")
    op.drop_column("messages", "encryption_policy_generation")
    op.drop_constraint("channel_encryption_policy_consistent", "channels", type_="check")
    op.drop_constraint("channel_encryption_epoch_nonnegative", "channels", type_="check")
    op.drop_constraint("channel_encryption_generation_nonnegative", "channels", type_="check")
    op.drop_constraint("channel_encryption_state_value", "channels", type_="check")
    op.drop_column("channels", "encryption_activated_at")
    op.drop_column("channels", "encryption_epoch")
    op.drop_column("channels", "encryption_group_id")
    op.drop_column("channels", "encryption_suite")
    op.drop_column("channels", "encryption_protocol")
    op.drop_column("channels", "encryption_policy_generation")
    op.drop_column("channels", "encryption_state")
