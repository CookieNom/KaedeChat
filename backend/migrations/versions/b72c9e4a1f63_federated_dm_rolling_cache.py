"""add bounded rolling history metadata for federated DM replicas

Revision ID: b72c9e4a1f63
Revises: fa4b8e1c7d32
Create Date: 2026-08-12 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b72c9e4a1f63"
down_revision: str | None = "fa4b8e1c7d32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dm_conversations",
        sa.Column(
            "history_truncated",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "dm_conversations",
        sa.Column("history_truncated_before_id", sa.BigInteger()),
    )
    op.add_column(
        "dm_conversations",
        sa.Column("history_truncated_before_domain", sa.String(length=253)),
    )
    op.add_column(
        "dm_conversations",
        sa.Column("history_cache_start_id", sa.BigInteger()),
    )
    op.add_column(
        "dm_conversations",
        sa.Column("history_cache_start_domain", sa.String(length=253)),
    )
    op.create_check_constraint(
        op.f("ck_dm_conversations_history_truncated_before_ref_complete"),
        "dm_conversations",
        "(history_truncated_before_id IS NULL) = (history_truncated_before_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_dm_conversations_history_cache_start_ref_complete"),
        "dm_conversations",
        "(history_cache_start_id IS NULL) = (history_cache_start_domain IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_dm_conversations_history_boundary_requires_truncation"),
        "dm_conversations",
        "history_truncated OR history_truncated_before_id IS NULL",
    )

    # These references are ordering/display watermarks. Keeping the composite
    # values after a replica-cache eviction preserves unread ordering and lets
    # clients render an unavailable reply target without retaining its body.
    op.drop_constraint("fk_messages_reply_ref_channel", "messages", type_="foreignkey")
    op.drop_constraint("fk_read_states_last_message_ref_channel", "read_states", type_="foreignkey")

    # Preserve the old FK invariant everywhere except the one intentional
    # rolling-replica case. An opaque ref must point into the signed, evicted
    # prefix of a non-authoritative truncated DM and to a participant origin.
    op.execute(
        """
        CREATE FUNCTION kaede_dm_ref_may_be_opaque(
            p_channel_id bigint,
            p_channel_domain text,
            p_message_id bigint,
            p_message_domain text
        ) RETURNS boolean
        LANGUAGE sql
        STABLE
        AS $$
            SELECT EXISTS (
                SELECT 1
                FROM dm_conversations AS conversation
                WHERE conversation.id = p_channel_id
                  AND conversation.origin_domain = p_channel_domain
                  AND conversation.history_truncated
                  AND conversation.history_truncated_before_id IS NOT NULL
                  AND conversation.authority_domain <> (
                      SELECT self_instance.domain
                      FROM instances AS self_instance
                      WHERE self_instance.is_self
                  )
                  AND p_message_domain <> (
                      SELECT self_instance.domain
                      FROM instances AS self_instance
                      WHERE self_instance.is_self
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM instances AS authority
                      WHERE authority.domain = conversation.authority_domain
                        AND authority.capabilities @> '["dm-history-page/1"]'::jsonb
                  )
                  AND (p_message_id, p_message_domain) <= (
                      conversation.history_truncated_before_id,
                      conversation.history_truncated_before_domain
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM dm_participants AS participant
                      WHERE participant.conversation_id = conversation.id
                        AND participant.conversation_domain = conversation.origin_domain
                        AND participant.user_domain = p_message_domain
                  )
            )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_validate_message_reply_reference() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.referenced_message_id IS NOT NULL
               AND (
                   NEW.referenced_message_id,
                   NEW.referenced_message_domain
               ) >= (NEW.id, NEW.origin_domain) THEN
                RAISE EXCEPTION 'reply target must precede the message'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF NEW.referenced_message_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM messages AS target
                   WHERE target.id = NEW.referenced_message_id
                     AND target.origin_domain = NEW.referenced_message_domain
                     AND target.channel_id = NEW.channel_id
                     AND target.channel_domain = NEW.channel_domain
               )
               AND NOT kaede_dm_ref_may_be_opaque(
                   NEW.channel_id,
                   NEW.channel_domain,
                   NEW.referenced_message_id,
                   NEW.referenced_message_domain
               ) THEN
                RAISE EXCEPTION 'reply target does not exist in this channel'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_messages_reply_reference
        BEFORE INSERT OR UPDATE OF referenced_message_id,
            referenced_message_domain, channel_id, channel_domain
        ON messages
        FOR EACH ROW EXECUTE FUNCTION kaede_validate_message_reply_reference()
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_validate_read_state_reference() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF NEW.last_message_id IS NOT NULL
               AND NOT EXISTS (
                   SELECT 1 FROM messages AS target
                   WHERE target.id = NEW.last_message_id
                     AND target.origin_domain = NEW.last_message_domain
                     AND target.channel_id = NEW.channel_id
                     AND target.channel_domain = NEW.channel_domain
               )
               AND NOT kaede_dm_ref_may_be_opaque(
                   NEW.channel_id,
                   NEW.channel_domain,
                   NEW.last_message_id,
                   NEW.last_message_domain
               ) THEN
                RAISE EXCEPTION 'read cursor does not exist in this channel'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_read_states_last_message_reference
        BEFORE INSERT OR UPDATE OF last_message_id,
            last_message_domain, channel_id, channel_domain
        ON read_states
        FOR EACH ROW EXECUTE FUNCTION kaede_validate_read_state_reference()
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_validate_dm_history_boundary() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM messages AS reply
                WHERE reply.channel_id = NEW.id
                  AND reply.channel_domain = NEW.origin_domain
                  AND reply.referenced_message_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM messages AS target
                      WHERE target.id = reply.referenced_message_id
                        AND target.origin_domain = reply.referenced_message_domain
                        AND target.channel_id = reply.channel_id
                        AND target.channel_domain = reply.channel_domain
                  )
                  AND NOT kaede_dm_ref_may_be_opaque(
                      NEW.id,
                      NEW.origin_domain,
                      reply.referenced_message_id,
                      reply.referenced_message_domain
                  )
            ) OR EXISTS (
                SELECT 1
                FROM read_states AS state
                WHERE state.channel_id = NEW.id
                  AND state.channel_domain = NEW.origin_domain
                  AND state.last_message_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM messages AS target
                      WHERE target.id = state.last_message_id
                        AND target.origin_domain = state.last_message_domain
                        AND target.channel_id = state.channel_id
                        AND target.channel_domain = state.channel_domain
                  )
                  AND NOT kaede_dm_ref_may_be_opaque(
                      NEW.id,
                      NEW.origin_domain,
                      state.last_message_id,
                      state.last_message_domain
                  )
            ) THEN
                RAISE EXCEPTION 'DM history boundary would orphan a reference'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_dm_conversations_history_boundary
        AFTER UPDATE OF history_truncated, history_truncated_before_id,
            history_truncated_before_domain, authority_domain
        ON dm_conversations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION kaede_validate_dm_history_boundary()
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_validate_deleted_message_reference() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (
                EXISTS (
                    SELECT 1 FROM messages AS reply
                    WHERE reply.referenced_message_id = OLD.id
                      AND reply.referenced_message_domain = OLD.origin_domain
                      AND reply.channel_id = OLD.channel_id
                      AND reply.channel_domain = OLD.channel_domain
                ) OR EXISTS (
                    SELECT 1 FROM read_states AS state
                    WHERE state.last_message_id = OLD.id
                      AND state.last_message_domain = OLD.origin_domain
                      AND state.channel_id = OLD.channel_id
                      AND state.channel_domain = OLD.channel_domain
                )
            ) AND NOT kaede_dm_ref_may_be_opaque(
                OLD.channel_id,
                OLD.channel_domain,
                OLD.id,
                OLD.origin_domain
            ) THEN
                RAISE EXCEPTION 'message is still referenced in this channel'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            RETURN OLD;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER trg_messages_delete_reference
        AFTER DELETE ON messages
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION kaede_validate_deleted_message_reference()
        """
    )


def downgrade() -> None:
    # Rolling replicas may now retain opaque watermarks/reply references after
    # the target body is evicted. A downgrade cannot restore those rows, so
    # clear only dangling references before restoring the legacy FKs.
    op.execute("DROP TRIGGER trg_messages_delete_reference ON messages")
    op.execute("DROP FUNCTION kaede_validate_deleted_message_reference()")
    op.execute("DROP TRIGGER trg_dm_conversations_history_boundary ON dm_conversations")
    op.execute("DROP FUNCTION kaede_validate_dm_history_boundary()")
    op.execute("DROP TRIGGER trg_read_states_last_message_reference ON read_states")
    op.execute("DROP FUNCTION kaede_validate_read_state_reference()")
    op.execute("DROP TRIGGER trg_messages_reply_reference ON messages")
    op.execute("DROP FUNCTION kaede_validate_message_reply_reference()")
    op.execute("DROP FUNCTION kaede_dm_ref_may_be_opaque(bigint, text, bigint, text)")
    op.execute(
        """
        UPDATE read_states AS state
        SET last_message_id = NULL, last_message_domain = NULL
        WHERE state.last_message_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages AS message
              WHERE message.id = state.last_message_id
                AND message.origin_domain = state.last_message_domain
                AND message.channel_id = state.channel_id
                AND message.channel_domain = state.channel_domain
          )
        """
    )
    op.execute(
        """
        UPDATE messages AS reply
        SET referenced_message_id = NULL, referenced_message_domain = NULL
        WHERE reply.referenced_message_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM messages AS target
              WHERE target.id = reply.referenced_message_id
                AND target.origin_domain = reply.referenced_message_domain
                AND target.channel_id = reply.channel_id
                AND target.channel_domain = reply.channel_domain
          )
        """
    )
    op.create_foreign_key(
        "fk_read_states_last_message_ref_channel",
        "read_states",
        "messages",
        ["last_message_id", "last_message_domain", "channel_id", "channel_domain"],
        ["id", "origin_domain", "channel_id", "channel_domain"],
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
    op.drop_constraint(
        op.f("ck_dm_conversations_history_boundary_requires_truncation"),
        "dm_conversations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_dm_conversations_history_cache_start_ref_complete"),
        "dm_conversations",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_dm_conversations_history_truncated_before_ref_complete"),
        "dm_conversations",
        type_="check",
    )
    op.drop_column("dm_conversations", "history_cache_start_domain")
    op.drop_column("dm_conversations", "history_cache_start_id")
    op.drop_column("dm_conversations", "history_truncated_before_domain")
    op.drop_column("dm_conversations", "history_truncated_before_id")
    op.drop_column("dm_conversations", "history_truncated")
