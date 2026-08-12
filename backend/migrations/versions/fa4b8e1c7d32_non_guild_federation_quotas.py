"""bound non-guild federation durable state

Revision ID: fa4b8e1c7d32
Revises: e95c2d8b4f31
Create Date: 2026-08-12 05:30:00.000000
"""

# ruff: noqa: E501 -- keeping the trigger SQL legible is safer than splitting literals.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fa4b8e1c7d32"
down_revision: str | None = "e95c2d8b4f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("federation_introduced_by_domain", sa.String(length=253)),
    )
    op.add_column(
        "users",
        sa.Column("federation_introduced_by_domain", sa.String(length=253)),
    )
    # Historical provenance cannot be reconstructed. Charging each legacy
    # remote namespace/profile to its own origin is deterministic and ensures
    # every retained row participates in the new limits going forward.
    op.execute("UPDATE instances SET federation_introduced_by_domain = domain WHERE NOT is_self")
    op.execute(
        "UPDATE users SET federation_introduced_by_domain = origin_domain WHERE NOT is_local"
    )
    op.create_check_constraint(
        op.f("ck_instances_self_has_no_federation_introducer"),
        "instances",
        "NOT is_self OR federation_introduced_by_domain IS NULL",
    )
    op.create_check_constraint(
        op.f("ck_users_federation_introducer_matches_locality"),
        "users",
        "(is_local AND federation_introduced_by_domain IS NULL) OR "
        "(NOT is_local AND federation_introduced_by_domain IS NOT NULL)",
    )
    op.create_index(
        "ix_instances_federation_introducer",
        "instances",
        ["federation_introduced_by_domain"],
        postgresql_where=sa.text("NOT is_self AND federation_introduced_by_domain IS NOT NULL"),
    )
    op.create_index(
        "ix_users_federation_introducer",
        "users",
        ["federation_introduced_by_domain"],
        postgresql_where=sa.text("NOT is_local"),
    )
    op.create_index(
        "ix_relationships_pending_in_recipient",
        "relationships",
        ["user_id", "user_domain"],
        postgresql_where=sa.text("type = 'pending_in'"),
    )
    op.create_index(
        "ix_relationships_pending_in_origin",
        "relationships",
        ["target_domain"],
        postgresql_where=sa.text("type = 'pending_in'"),
    )
    op.create_index(
        "ix_relationships_pending_in_recipient_origin",
        "relationships",
        ["user_id", "user_domain", "target_domain"],
        postgresql_where=sa.text("type = 'pending_in'"),
    )

    op.create_table(
        "federated_dm_storage_usage",
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_domain", sa.String(length=253), nullable=False),
        sa.Column("authority_domain", sa.String(length=253), nullable=False),
        sa.Column("remote_origin_domain", sa.String(length=253), nullable=False),
        sa.Column("message_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("message_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attachment_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attachment_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("projection_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("projection_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "total_rows",
            sa.BigInteger(),
            sa.Computed("message_rows + attachment_rows + projection_rows", persisted=True),
        ),
        sa.Column(
            "total_bytes",
            sa.BigInteger(),
            sa.Computed("message_bytes + attachment_bytes + projection_bytes", persisted=True),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "message_rows >= 0 AND attachment_rows >= 0 AND projection_rows >= 0",
            name=op.f("ck_federated_dm_storage_usage_nonnegative_rows"),
        ),
        sa.CheckConstraint(
            "message_bytes >= 0 AND attachment_bytes >= 0 AND projection_bytes >= 0",
            name=op.f("ck_federated_dm_storage_usage_nonnegative_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["authority_domain"],
            ["instances.domain"],
            name=op.f("fk_federated_dm_storage_usage_authority_domain_instances"),
        ),
        sa.ForeignKeyConstraint(
            ["remote_origin_domain"],
            ["instances.domain"],
            name=op.f("fk_federated_dm_storage_usage_remote_origin_domain_instances"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            name=op.f(
                "fk_federated_dm_storage_usage_conversation_id_conversation_domain_dm_conversations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "conversation_id",
            "conversation_domain",
            name=op.f("pk_federated_dm_storage_usage"),
        ),
    )
    op.create_index(
        "ix_federated_dm_storage_usage_authority",
        "federated_dm_storage_usage",
        ["authority_domain"],
    )
    op.create_index(
        "ix_federated_dm_storage_usage_remote_origin",
        "federated_dm_storage_usage",
        ["remote_origin_domain"],
    )
    op.create_table(
        "federated_dm_row_charges",
        sa.Column("table_name", sa.String(length=32), nullable=False),
        sa.Column("row_id", sa.BigInteger(), nullable=False),
        sa.Column("row_domain", sa.String(length=253), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_domain", sa.String(length=253), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("charge_bytes", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "table_name IN ('messages','attachments','message_projections')",
            name=op.f("ck_federated_dm_row_charges_table_name"),
        ),
        sa.CheckConstraint(
            "category IN ('message','attachment','projection')",
            name=op.f("ck_federated_dm_row_charges_category"),
        ),
        sa.CheckConstraint(
            "charge_bytes > 0",
            name=op.f("ck_federated_dm_row_charges_positive_charge"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            [
                "federated_dm_storage_usage.conversation_id",
                "federated_dm_storage_usage.conversation_domain",
            ],
            name=op.f(
                "fk_federated_dm_row_charges_conversation_id_conversation_domain_federated_dm_storage_usage"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "table_name",
            "row_id",
            "row_domain",
            name=op.f("pk_federated_dm_row_charges"),
        ),
    )
    op.create_index(
        "ix_federated_dm_row_charges_conversation",
        "federated_dm_row_charges",
        ["conversation_id", "conversation_domain"],
    )

    op.execute(
        """
        CREATE FUNCTION kaede_dm_charge(p_table text, p_row jsonb) RETURNS bigint
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT CASE p_table
                WHEN 'messages' THEN greatest(
                    pg_column_size(p_row - 'edited_at' - 'deleted_at')::bigint + 512,
                    4096
                )
                WHEN 'attachments' THEN greatest(
                    pg_column_size(p_row - 'deleted_at' - 'updated_at')::bigint + 384,
                    4096
                )
                WHEN 'message_projections' THEN greatest(
                    pg_column_size(p_row - 'processed_at')::bigint + 256,
                    2048
                )
                ELSE 4096::bigint
            END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_adjust_dm_usage(
            p_conversation_id bigint,
            p_conversation_domain text,
            p_category text,
            p_row_delta bigint,
            p_byte_delta bigint
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF p_conversation_id IS NULL OR p_conversation_domain IS NULL THEN
                RETURN;
            END IF;
            UPDATE federated_dm_storage_usage
            SET message_rows = message_rows + CASE WHEN p_category = 'message' THEN p_row_delta ELSE 0 END,
                message_bytes = message_bytes + CASE WHEN p_category = 'message' THEN p_byte_delta ELSE 0 END,
                attachment_rows = attachment_rows + CASE WHEN p_category = 'attachment' THEN p_row_delta ELSE 0 END,
                attachment_bytes = attachment_bytes + CASE WHEN p_category = 'attachment' THEN p_byte_delta ELSE 0 END,
                projection_rows = projection_rows + CASE WHEN p_category = 'projection' THEN p_row_delta ELSE 0 END,
                projection_bytes = projection_bytes + CASE WHEN p_category = 'projection' THEN p_byte_delta ELSE 0 END,
                updated_at = now()
            WHERE conversation_id = p_conversation_id
              AND conversation_domain = p_conversation_domain;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_dm_row_conversation(
            p_mode text,
            p_row jsonb,
            OUT conversation_id bigint,
            OUT conversation_domain text
        ) RETURNS record
        LANGUAGE plpgsql
        STABLE
        AS $$
        BEGIN
            conversation_id := NULL;
            conversation_domain := NULL;
            IF p_mode = 'channel' THEN
                SELECT usage.conversation_id, usage.conversation_domain
                INTO conversation_id, conversation_domain
                FROM federated_dm_storage_usage AS usage
                WHERE usage.conversation_id = NULLIF(p_row ->> 'channel_id', '')::bigint
                  AND usage.conversation_domain = NULLIF(p_row ->> 'channel_domain', '');
            ELSIF p_mode = 'message' THEN
                SELECT usage.conversation_id, usage.conversation_domain
                INTO conversation_id, conversation_domain
                FROM messages AS message
                JOIN federated_dm_storage_usage AS usage
                  ON usage.conversation_id = message.channel_id
                 AND usage.conversation_domain = message.channel_domain
                WHERE message.id = NULLIF(p_row ->> 'message_id', '')::bigint
                  AND message.origin_domain = NULLIF(p_row ->> 'message_domain', '');
            ELSE
                RAISE EXCEPTION 'unknown federated DM accounting mode: %', p_mode;
            END IF;
        END;
        $$
        """
    )

    op.execute(
        """
        INSERT INTO federated_dm_storage_usage (
            conversation_id, conversation_domain, authority_domain, remote_origin_domain
        )
        SELECT conversation.id, conversation.origin_domain, conversation.authority_domain,
               (
                   SELECT min(participant.user_domain)
                   FROM dm_participants AS participant
                   WHERE participant.conversation_id = conversation.id
                     AND participant.conversation_domain = conversation.origin_domain
                     AND participant.user_domain <> (
                         SELECT self_instance.domain
                         FROM instances AS self_instance
                         WHERE self_instance.is_self
                     )
               )
        FROM dm_conversations AS conversation
        WHERE EXISTS (
            SELECT 1
            FROM dm_participants AS local_participant
            WHERE local_participant.conversation_id = conversation.id
              AND local_participant.conversation_domain = conversation.origin_domain
              AND local_participant.user_domain = (
                  SELECT self_instance.domain
                  FROM instances AS self_instance
                  WHERE self_instance.is_self
              )
        )
        AND EXISTS (
            SELECT 1
            FROM dm_participants AS first_participant
            JOIN dm_participants AS second_participant
              ON second_participant.conversation_id = first_participant.conversation_id
             AND second_participant.conversation_domain = first_participant.conversation_domain
             AND second_participant.user_domain <> first_participant.user_domain
            WHERE first_participant.conversation_id = conversation.id
              AND first_participant.conversation_domain = conversation.origin_domain
        )
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO federated_dm_row_charges (
            table_name, row_id, row_domain, conversation_id,
            conversation_domain, category, charge_bytes
        )
        SELECT 'messages', message.id, message.origin_domain,
               usage.conversation_id, usage.conversation_domain,
               'message', kaede_dm_charge('messages', to_jsonb(message))
        FROM messages AS message
        JOIN federated_dm_storage_usage AS usage
          ON usage.conversation_id = message.channel_id
         AND usage.conversation_domain = message.channel_domain
        UNION ALL
        SELECT 'attachments', attachment.id, attachment.origin_domain,
               usage.conversation_id, usage.conversation_domain,
               'attachment', kaede_dm_charge('attachments', to_jsonb(attachment))
        FROM attachments AS attachment
        JOIN messages AS message
          ON message.id = attachment.message_id
         AND message.origin_domain = attachment.message_domain
        JOIN federated_dm_storage_usage AS usage
          ON usage.conversation_id = message.channel_id
         AND usage.conversation_domain = message.channel_domain
        UNION ALL
        SELECT 'message_projections', projection.message_id, projection.message_domain,
               usage.conversation_id, usage.conversation_domain,
               'projection', kaede_dm_charge('message_projections', to_jsonb(projection))
        FROM message_projections AS projection
        JOIN federated_dm_storage_usage AS usage
          ON usage.conversation_id = projection.channel_id
         AND usage.conversation_domain = projection.channel_domain
        """
    )
    op.execute(
        """
        UPDATE federated_dm_storage_usage AS usage
        SET message_rows = totals.message_rows,
            message_bytes = totals.message_bytes,
            attachment_rows = totals.attachment_rows,
            attachment_bytes = totals.attachment_bytes,
            projection_rows = totals.projection_rows,
            projection_bytes = totals.projection_bytes,
            updated_at = now()
        FROM (
            SELECT conversation_id, conversation_domain,
                   count(*) FILTER (WHERE category = 'message')::bigint AS message_rows,
                   coalesce(sum(charge_bytes) FILTER (WHERE category = 'message'), 0)::bigint AS message_bytes,
                   count(*) FILTER (WHERE category = 'attachment')::bigint AS attachment_rows,
                   coalesce(sum(charge_bytes) FILTER (WHERE category = 'attachment'), 0)::bigint AS attachment_bytes,
                   count(*) FILTER (WHERE category = 'projection')::bigint AS projection_rows,
                   coalesce(sum(charge_bytes) FILTER (WHERE category = 'projection'), 0)::bigint AS projection_bytes
            FROM federated_dm_row_charges
            GROUP BY conversation_id, conversation_domain
        ) AS totals
        WHERE usage.conversation_id = totals.conversation_id
          AND usage.conversation_domain = totals.conversation_domain
        """
    )

    op.execute(
        """
        CREATE FUNCTION kaede_track_dm_row() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            row_data jsonb;
            row_identifier bigint;
            row_origin text;
            new_conversation_id bigint;
            new_conversation_domain text;
            stored_charge federated_dm_row_charges%ROWTYPE;
            new_charge bigint;
            had_stored boolean := false;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                row_data := to_jsonb(OLD);
            ELSE
                row_data := to_jsonb(NEW);
            END IF;
            row_identifier := NULLIF(
                coalesce(row_data ->> 'id', row_data ->> 'message_id'), ''
            )::bigint;
            row_origin := coalesce(row_data ->> 'origin_domain', row_data ->> 'message_domain');

            SELECT * INTO stored_charge
            FROM federated_dm_row_charges
            WHERE table_name = TG_ARGV[2]
              AND row_id = row_identifier
              AND row_domain = row_origin
            FOR UPDATE;
            had_stored := FOUND;

            IF TG_OP = 'DELETE' THEN
                IF FOUND THEN
                    PERFORM kaede_adjust_dm_usage(
                        stored_charge.conversation_id,
                        stored_charge.conversation_domain,
                        stored_charge.category,
                        -1,
                        -stored_charge.charge_bytes
                    );
                    DELETE FROM federated_dm_row_charges
                    WHERE table_name = TG_ARGV[2]
                      AND row_id = row_identifier
                      AND row_domain = row_origin;
                END IF;
                RETURN OLD;
            END IF;

            SELECT ref.conversation_id, ref.conversation_domain
            INTO new_conversation_id, new_conversation_domain
            FROM kaede_dm_row_conversation(TG_ARGV[1], row_data) AS ref;
            new_charge := kaede_dm_charge(TG_ARGV[2], row_data);

            IF had_stored THEN
                IF (stored_charge.conversation_id, stored_charge.conversation_domain)
                   IS DISTINCT FROM (new_conversation_id, new_conversation_domain) THEN
                    PERFORM kaede_adjust_dm_usage(
                        stored_charge.conversation_id,
                        stored_charge.conversation_domain,
                        stored_charge.category,
                        -1,
                        -stored_charge.charge_bytes
                    );
                    DELETE FROM federated_dm_row_charges
                    WHERE table_name = TG_ARGV[2]
                      AND row_id = row_identifier
                      AND row_domain = row_origin;
                    had_stored := false;
                ELSIF new_charge > stored_charge.charge_bytes THEN
                    UPDATE federated_dm_row_charges
                    SET charge_bytes = new_charge
                    WHERE table_name = TG_ARGV[2]
                      AND row_id = row_identifier
                      AND row_domain = row_origin;
                    PERFORM kaede_adjust_dm_usage(
                        new_conversation_id,
                        new_conversation_domain,
                        TG_ARGV[0],
                        0,
                        new_charge - stored_charge.charge_bytes
                    );
                END IF;
            END IF;

            IF NOT had_stored AND new_conversation_id IS NOT NULL THEN
                INSERT INTO federated_dm_row_charges (
                    table_name, row_id, row_domain, conversation_id,
                    conversation_domain, category, charge_bytes
                ) VALUES (
                    TG_ARGV[2], row_identifier, row_origin, new_conversation_id,
                    new_conversation_domain, TG_ARGV[0], new_charge
                );
                PERFORM kaede_adjust_dm_usage(
                    new_conversation_id,
                    new_conversation_domain,
                    TG_ARGV[0],
                    1,
                    new_charge
                );
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    for table, category, mode in (
        ("messages", "message", "channel"),
        ("attachments", "attachment", "message"),
        ("message_projections", "projection", "channel"),
    ):
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_federated_dm_usage
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION kaede_track_dm_row('{category}', '{mode}', '{table}')
            """
        )


def downgrade() -> None:
    for table in ("message_projections", "attachments", "messages"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_federated_dm_usage ON {table}")
    op.execute("DROP FUNCTION IF EXISTS kaede_track_dm_row()")
    op.execute("DROP FUNCTION IF EXISTS kaede_dm_row_conversation(text, jsonb)")
    op.execute("DROP FUNCTION IF EXISTS kaede_adjust_dm_usage(bigint, text, text, bigint, bigint)")
    op.execute("DROP FUNCTION IF EXISTS kaede_dm_charge(text, jsonb)")
    op.drop_index(
        "ix_federated_dm_row_charges_conversation",
        table_name="federated_dm_row_charges",
    )
    op.drop_table("federated_dm_row_charges")
    op.drop_index(
        "ix_federated_dm_storage_usage_authority",
        table_name="federated_dm_storage_usage",
    )
    op.drop_index(
        "ix_federated_dm_storage_usage_remote_origin",
        table_name="federated_dm_storage_usage",
    )
    op.drop_table("federated_dm_storage_usage")
    op.drop_index("ix_relationships_pending_in_recipient_origin", table_name="relationships")
    op.drop_index("ix_relationships_pending_in_origin", table_name="relationships")
    op.drop_index("ix_relationships_pending_in_recipient", table_name="relationships")
    op.drop_index("ix_users_federation_introducer", table_name="users")
    op.drop_index("ix_instances_federation_introducer", table_name="instances")
    op.drop_constraint(
        op.f("ck_users_federation_introducer_matches_locality"),
        "users",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_instances_self_has_no_federation_introducer"),
        "instances",
        type_="check",
    )
    op.drop_column("users", "federation_introduced_by_domain")
    op.drop_column("instances", "federation_introduced_by_domain")
