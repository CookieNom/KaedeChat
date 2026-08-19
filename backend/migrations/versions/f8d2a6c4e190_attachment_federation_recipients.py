"""retain prior media recipient instances for terminal tombstones

Revision ID: f8d2a6c4e190
Revises: e5c7b9a1d204
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f8d2a6c4e190"
down_revision: str | None = "e5c7b9a1d204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_retained_delivery_routes() -> None:
    """Recover only recipient routes with authoritative pre-upgrade evidence."""

    # Retained outbox rows are exact evidence that this instance queued the
    # corresponding signed metadata to a destination.  Materialize their room
    # references once so attachment and terminal-room ledgers cannot disagree.
    # Every bigint cast is guarded against malformed or overflowing JSON.
    op.execute(
        """
        CREATE TEMPORARY TABLE f8_retained_event_routes ON COMMIT DROP AS
        WITH raw_routes AS (
            SELECT
                outbox.destination AS destination_domain,
                outbox.created_at AS disclosed_at,
                event.event_type,
                event.envelope,
                CASE
                    WHEN NOT (
                        event.event_type = 'guild.instance_access.revoked'
                        AND event.envelope #>> '{content,reason}' = 'guild_deleted'
                    )
                    AND jsonb_typeof(event.envelope #> '{context,guild_id}')
                        IN ('string', 'number')
                    AND jsonb_typeof(event.envelope #> '{context,guild_domain}') = 'string'
                    THEN 'guild'
                    WHEN NOT (
                        event.event_type = 'dm.group.state'
                        AND event.envelope #> '{content,conversation,deleted}' = 'true'::jsonb
                    )
                    AND event.event_type IN (
                        'dm.group.call.create',
                        'dm.group.message.proposed',
                        'dm.group.message.committed'
                    )
                    AND jsonb_typeof(event.envelope #> '{context,conversation_id}')
                        IN ('string', 'number')
                    AND jsonb_typeof(event.envelope #> '{context,conversation_domain}')
                        = 'string'
                    THEN 'group_dm'
                    WHEN NOT (
                        event.event_type = 'dm.group.state'
                        AND event.envelope #> '{content,conversation,deleted}' = 'true'::jsonb
                    )
                    AND event.event_type = 'dm.group.state'
                    AND jsonb_typeof(event.envelope #> '{content,conversation,id}')
                        IN ('string', 'number')
                    AND jsonb_typeof(
                        event.envelope #> '{content,conversation,origin_domain}'
                    ) = 'string'
                    THEN 'group_dm'
                END AS raw_room_kind,
                CASE
                    WHEN jsonb_typeof(event.envelope #> '{context,guild_id}')
                        IN ('string', 'number')
                    AND jsonb_typeof(event.envelope #> '{context,guild_domain}') = 'string'
                    THEN event.envelope #>> '{context,guild_id}'
                    WHEN event.event_type IN (
                        'dm.group.call.create',
                        'dm.group.message.proposed',
                        'dm.group.message.committed'
                    )
                    THEN event.envelope #>> '{context,conversation_id}'
                    WHEN event.event_type = 'dm.group.state'
                    THEN event.envelope #>> '{content,conversation,id}'
                END AS raw_room_id,
                CASE
                    WHEN jsonb_typeof(event.envelope #> '{context,guild_id}')
                        IN ('string', 'number')
                    AND jsonb_typeof(event.envelope #> '{context,guild_domain}') = 'string'
                    THEN event.envelope #>> '{context,guild_domain}'
                    WHEN event.event_type IN (
                        'dm.group.call.create',
                        'dm.group.message.proposed',
                        'dm.group.message.committed'
                    )
                    THEN event.envelope #>> '{context,conversation_domain}'
                    WHEN event.event_type = 'dm.group.state'
                    THEN event.envelope #>> '{content,conversation,origin_domain}'
                END AS raw_room_domain
            FROM federation_outbox AS outbox
            JOIN federation_events AS event
              ON event.origin_domain = outbox.event_origin_domain
             AND event.event_id = outbox.event_id
            WHERE jsonb_typeof(event.envelope) = 'object'
              AND jsonb_typeof(event.envelope -> 'event_id') = 'string'
              AND jsonb_typeof(event.envelope -> 'origin') = 'string'
              AND jsonb_typeof(event.envelope -> 'type') = 'string'
              AND event.envelope ->> 'event_id' = event.event_id
              AND event.envelope ->> 'origin' = event.origin_domain
              AND event.envelope ->> 'type' = event.event_type
        )
        SELECT
            destination_domain,
            disclosed_at,
            event_type,
            envelope,
            CASE
                WHEN raw_room_kind IS NOT NULL
                 AND raw_room_id ~ '^(0|[1-9][0-9]{0,18})$'
                 AND char_length(raw_room_domain) BETWEEN 1 AND 253
                THEN CASE
                    WHEN raw_room_id::numeric <= 9223372036854775807
                    THEN raw_room_kind
                END
            END::varchar(16) AS room_kind,
            CASE
                WHEN raw_room_kind IS NOT NULL
                 AND raw_room_id ~ '^(0|[1-9][0-9]{0,18})$'
                 AND char_length(raw_room_domain) BETWEEN 1 AND 253
                THEN CASE
                    WHEN raw_room_id::numeric <= 9223372036854775807
                    THEN raw_room_id::bigint
                END
            END AS room_id,
            CASE
                WHEN raw_room_kind IS NOT NULL
                 AND raw_room_id ~ '^(0|[1-9][0-9]{0,18})$'
                 AND char_length(raw_room_domain) BETWEEN 1 AND 253
                THEN CASE
                    WHEN raw_room_id::numeric <= 9223372036854775807
                    THEN raw_room_domain
                END
            END::varchar(253) AS room_domain
        FROM raw_routes
        """
    )

    # A retained room-bearing event proves that this destination had room
    # metadata queued. Terminal events themselves are excluded above because
    # they consume, rather than extend, the historical access ledger.
    op.execute(
        """
        INSERT INTO room_federation_recipients (
            room_kind,
            room_id,
            room_domain,
            destination_domain,
            created_at
        )
        SELECT
            room_kind,
            room_id,
            room_domain,
            destination_domain,
            min(disclosed_at)
        FROM f8_retained_event_routes
        WHERE room_kind IS NOT NULL
          AND room_id IS NOT NULL
          AND room_domain IS NOT NULL
          AND destination_domain <> room_domain
        GROUP BY room_kind, room_id, room_domain, destination_domain
        ON CONFLICT DO NOTHING
        """
    )

    # Current remote membership is exact evidence of room access even when the
    # ordinary event/outbox retention window has elapsed. Limit this recovery
    # to rooms for which this database is the signing authority.
    op.execute(
        """
        INSERT INTO room_federation_recipients (
            room_kind,
            room_id,
            room_domain,
            destination_domain,
            created_at
        )
        SELECT
            'guild',
            guild.id,
            guild.origin_domain,
            member.user_domain,
            min(member.created_at)
        FROM guilds AS guild
        JOIN instances AS self_instance
          ON self_instance.domain = guild.origin_domain
         AND self_instance.is_self
        JOIN guild_members AS member
          ON member.guild_id = guild.id
         AND member.guild_domain = guild.origin_domain
        WHERE NOT guild.unavailable
          AND member.user_domain <> guild.origin_domain
        GROUP BY guild.id, guild.origin_domain, member.user_domain
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO room_federation_recipients (
            room_kind,
            room_id,
            room_domain,
            destination_domain,
            created_at
        )
        SELECT
            'group_dm',
            conversation.id,
            conversation.origin_domain,
            participant.user_domain,
            min(participant.joined_at)
        FROM dm_conversations AS conversation
        JOIN channels AS channel
          ON channel.id = conversation.id
         AND channel.origin_domain = conversation.origin_domain
        JOIN instances AS self_instance
          ON self_instance.domain = conversation.authority_domain
         AND self_instance.is_self
        JOIN dm_participants AS participant
          ON participant.conversation_id = conversation.id
         AND participant.conversation_domain = conversation.origin_domain
        WHERE conversation.type = 'group'
          AND NOT channel.unavailable
          AND participant.user_domain <> conversation.origin_domain
        GROUP BY
            conversation.id,
            conversation.origin_domain,
            participant.user_domain
        ON CONFLICT DO NOTHING
        """
    )

    # Match message_attachment_refs(): authoritative message payloads nest the
    # list below content.message, while proxy proposals use content.attachments.
    # Invalid JSON members become NULL and are discarded before any bigint cast.
    op.execute(
        """
        CREATE TEMPORARY TABLE f8_retained_attachment_routes ON COMMIT DROP AS
        WITH disclosed AS (
            SELECT
                route.destination_domain,
                route.disclosed_at,
                route.room_kind,
                route.room_id,
                route.room_domain,
                attachment.payload
            FROM f8_retained_event_routes AS route
            CROSS JOIN LATERAL (
                SELECT nested.value AS payload
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(
                            route.envelope #> '{content,message,attachments}'
                        ) = 'array'
                        THEN route.envelope #> '{content,message,attachments}'
                        ELSE '[]'::jsonb
                    END
                ) AS nested(value)
                UNION ALL
                SELECT direct.value AS payload
                FROM jsonb_array_elements(
                    CASE
                        WHEN jsonb_typeof(route.envelope #> '{content,attachments}') = 'array'
                        THEN route.envelope #> '{content,attachments}'
                        ELSE '[]'::jsonb
                    END
                ) AS direct(value)
            ) AS attachment
        ), parsed AS (
            SELECT
                destination_domain,
                disclosed_at,
                room_kind,
                room_id,
                room_domain,
                CASE
                    WHEN jsonb_typeof(payload) = 'object'
                     AND jsonb_typeof(payload -> 'id') IN ('string', 'number')
                     AND payload ->> 'id' ~ '^(0|[1-9][0-9]{0,18})$'
                    THEN CASE
                        WHEN (payload ->> 'id')::numeric <= 9223372036854775807
                        THEN (payload ->> 'id')::bigint
                    END
                END AS attachment_id,
                CASE
                    WHEN jsonb_typeof(payload) = 'object'
                     AND jsonb_typeof(payload -> 'origin_domain') = 'string'
                     AND char_length(payload ->> 'origin_domain') BETWEEN 1 AND 253
                    THEN payload ->> 'origin_domain'
                END::varchar(253) AS attachment_domain
            FROM disclosed
        )
        SELECT
            attachment_id,
            attachment_domain,
            destination_domain,
            room_kind,
            room_id,
            room_domain,
            disclosed_at
        FROM parsed
        WHERE attachment_id IS NOT NULL
          AND attachment_domain IS NOT NULL
          AND destination_domain <> attachment_domain
        """
    )

    # This FK-backed ledger contains extant attachments only, matching the
    # runtime path. The independent destination ledger also retains exact raw
    # history references whose attachment row has already been evicted.
    op.execute(
        """
        INSERT INTO attachment_federation_recipients (
            attachment_id,
            attachment_domain,
            destination_domain,
            created_at
        )
        SELECT
            attachment.id,
            attachment.origin_domain,
            route.destination_domain,
            min(route.disclosed_at)
        FROM f8_retained_attachment_routes AS route
        JOIN attachments AS attachment
          ON attachment.id = route.attachment_id
         AND attachment.origin_domain = route.attachment_domain
        GROUP BY
            attachment.id,
            attachment.origin_domain,
            route.destination_domain
        ON CONFLICT DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO media_tombstone_destinations (
            attachment_id,
            attachment_domain,
            destination_domain,
            room_kind,
            room_id,
            room_domain,
            created_at
        )
        SELECT DISTINCT ON (
            attachment_id,
            attachment_domain,
            destination_domain
        )
            attachment_id,
            attachment_domain,
            destination_domain,
            room_kind,
            room_id,
            room_domain,
            disclosed_at
        FROM f8_retained_attachment_routes
        ORDER BY
            attachment_id,
            attachment_domain,
            destination_domain,
            (room_kind IS NOT NULL) DESC,
            disclosed_at,
            room_kind,
            room_domain,
            room_id
        ON CONFLICT DO NOTHING
        """
    )
    op.execute("DROP TABLE f8_retained_attachment_routes")
    op.execute("DROP TABLE f8_retained_event_routes")


def _backfill_legacy_media_delete_sources() -> None:
    """Retain unambiguous local or durably accepted remote E0 proofs."""

    # Pre-generation media.delete envelopes are still valid signed terminal
    # truth. They intentionally map to generation zero. A local source can be
    # advanced and re-signed as generation one; an accepted remote source must
    # retain the exact origin signature for downstream relay. A remote event is
    # eligible only when its matching inbox claim was durably processed, which
    # proves that the normal signature and protocol validation completed. Do
    # not repair malformed envelopes or infer an actor/signing key.
    op.execute(
        """
        CREATE TEMPORARY TABLE f8_retained_media_delete_events ON COMMIT DROP AS
        WITH raw_events AS (
            SELECT
                event.origin_domain AS attachment_domain,
                event.event_id,
                event.created_at,
                event.envelope,
                event.envelope #>> '{content,attachment_id}' AS raw_attachment_id,
                event.envelope #>> '{actor,id}' AS raw_signer_id,
                event.envelope #>> '{actor,domain}' AS signer_domain,
                event.envelope ->> 'ts' AS raw_event_ts,
                CASE
                    WHEN NOT (event.envelope #> '{content}' ? 'generation') THEN 0
                    WHEN jsonb_typeof(event.envelope #> '{content,generation}') = 'string'
                     AND event.envelope #>> '{content,generation}'
                         ~ '^[1-9][0-9]{0,18}$'
                    THEN CASE
                        WHEN (event.envelope #>> '{content,generation}')::numeric
                            <= 9223372036854775807
                        THEN (event.envelope #>> '{content,generation}')::bigint
                    END
                END AS generation,
                CASE
                    WHEN jsonb_typeof(
                        event.envelope -> 'signatures' -> event.origin_domain
                    ) = 'object'
                    THEN event.envelope -> 'signatures' -> event.origin_domain
                    ELSE '{}'::jsonb
                END AS origin_signatures,
                CASE
                    WHEN jsonb_typeof(event.envelope -> 'signatures') = 'object'
                    THEN event.envelope -> 'signatures'
                    ELSE '{}'::jsonb
                END AS all_signatures
            FROM federation_events AS event
            JOIN instances AS origin_instance
              ON origin_instance.domain = event.origin_domain
            WHERE event.event_type = 'media.delete'
              AND (
                    origin_instance.is_self
                    OR EXISTS (
                        SELECT 1
                        FROM federation_inbox AS accepted_inbox
                        WHERE accepted_inbox.origin_domain = event.origin_domain
                          AND accepted_inbox.event_id = event.event_id
                          AND accepted_inbox.status = 'processed'
                          AND accepted_inbox.processed_at IS NOT NULL
                          AND accepted_inbox.result_code IS NULL
                    )
              )
              AND jsonb_typeof(event.envelope) = 'object'
              AND jsonb_typeof(event.envelope -> 'event_id') = 'string'
              AND jsonb_typeof(event.envelope -> 'origin') = 'string'
              AND jsonb_typeof(event.envelope -> 'type') = 'string'
              AND jsonb_typeof(event.envelope -> 'ts') = 'number'
              AND jsonb_typeof(event.envelope -> 'actor') = 'object'
              AND jsonb_typeof(event.envelope #> '{actor,id}') = 'string'
              AND jsonb_typeof(event.envelope #> '{actor,domain}') = 'string'
              AND jsonb_typeof(event.envelope -> 'content') = 'object'
              AND jsonb_typeof(event.envelope #> '{content,attachment_id}') = 'string'
              AND jsonb_typeof(event.envelope #> '{content,origin_domain}') = 'string'
              AND event.envelope ->> 'event_id' = event.event_id
              AND event.envelope ->> 'origin' = event.origin_domain
              AND event.envelope ->> 'type' = event.event_type
              AND event.envelope #>> '{content,origin_domain}' = event.origin_domain
        ), signed_events AS (
            SELECT raw_event.*, signature.key_id
            FROM raw_events AS raw_event
            CROSS JOIN LATERAL jsonb_object_keys(
                raw_event.origin_signatures
            ) AS signature(key_id)
            WHERE (
                SELECT count(*)
                FROM jsonb_object_keys(raw_event.all_signatures)
            ) = 1
              AND (
                SELECT count(*)
                FROM jsonb_object_keys(raw_event.origin_signatures)
              ) = 1
              AND jsonb_typeof(raw_event.origin_signatures -> signature.key_id) = 'string'
              AND char_length(raw_event.origin_signatures ->> signature.key_id)
                  BETWEEN 1 AND 128
              AND signature.key_id ~ '^[A-Za-z0-9._:-]{1,64}$'
        ), parsed_events AS (
            SELECT
                CASE
                    WHEN raw_attachment_id ~ '^(0|[1-9][0-9]{0,18})$'
                    THEN CASE
                        WHEN raw_attachment_id::numeric <= 9223372036854775807
                        THEN raw_attachment_id::bigint
                    END
                END AS attachment_id,
                attachment_domain,
                CASE
                    WHEN raw_signer_id ~ '^(0|[1-9][0-9]{0,18})$'
                    THEN CASE
                        WHEN raw_signer_id::numeric <= 9223372036854775807
                        THEN raw_signer_id::bigint
                    END
                END AS signer_id,
                signer_domain,
                event_id,
                key_id,
                generation,
                CASE
                    WHEN raw_event_ts ~ '^(0|[1-9][0-9]{0,18})$'
                    THEN CASE
                        WHEN raw_event_ts::numeric <= 9223372036854775807
                        THEN raw_event_ts::bigint
                    END
                END AS event_ts,
                created_at
            FROM signed_events
        )
        SELECT
            attachment_id,
            attachment_domain,
            signer_id,
            signer_domain,
            event_id,
            key_id,
            generation,
            event_ts,
            created_at
        FROM parsed_events
        WHERE attachment_id IS NOT NULL
          AND signer_id IS NOT NULL
          AND signer_domain = attachment_domain
          AND generation IS NOT NULL
          AND event_ts IS NOT NULL
          AND event_id ~ '^kcfe_[A-Za-z0-9_-]{16,59}$'
        """
    )

    op.execute(
        """
        INSERT INTO media_tombstone_sources (
            attachment_id,
            attachment_domain,
            signer_id,
            signer_domain,
            event_id,
            key_id,
            generation,
            created_at,
            updated_at
        )
        SELECT DISTINCT ON (attachment_id, attachment_domain)
            attachment_id,
            attachment_domain,
            signer_id,
            signer_domain,
            event_id,
            key_id,
            generation,
            created_at,
            created_at
        FROM f8_retained_media_delete_events
        ORDER BY
            attachment_id,
            attachment_domain,
            generation DESC,
            event_ts DESC,
            event_id DESC
        ON CONFLICT DO NOTHING
        """
    )

    # Every destination of any retained generation is historical invalidation
    # state, including a route for which the attachment row is already gone.
    op.execute(
        """
        INSERT INTO media_tombstone_destinations (
            attachment_id,
            attachment_domain,
            destination_domain,
            created_at
        )
        SELECT
            retained.attachment_id,
            retained.attachment_domain,
            outbox.destination,
            min(outbox.created_at)
        FROM f8_retained_media_delete_events AS retained
        JOIN federation_outbox AS outbox
          ON outbox.event_origin_domain = retained.attachment_domain
         AND outbox.event_id = retained.event_id
        WHERE outbox.destination <> retained.attachment_domain
        GROUP BY
            retained.attachment_id,
            retained.attachment_domain,
            outbox.destination
        ON CONFLICT DO NOTHING
        """
    )

    # A relay cannot re-sign a remote proof. Keep the exact accepted origin
    # envelope indefinitely and enqueue it to every retained downstream route,
    # including B -> D metadata routes whose original A -> B delivery was
    # already acknowledged before this schema existed. Existing delivered
    # rows remain delivered; every other retained attempt is made immediately
    # retryable. The outbox identity column deliberately uses its sequence.
    op.execute(
        """
        UPDATE federation_events AS event
        SET expires_at = NULL
        FROM media_tombstone_sources AS source
        JOIN instances AS origin_instance
          ON origin_instance.domain = source.attachment_domain
         AND NOT origin_instance.is_self
        WHERE event.origin_domain = source.attachment_domain
          AND event.event_id = source.event_id
        """
    )
    op.execute(
        """
        INSERT INTO federation_outbox (
            destination,
            event_origin_domain,
            event_id,
            status,
            attempts,
            next_retry_at,
            last_error
        )
        SELECT
            destination.destination_domain,
            source.attachment_domain,
            source.event_id,
            'pending',
            0,
            now(),
            NULL
        FROM media_tombstone_sources AS source
        JOIN instances AS origin_instance
          ON origin_instance.domain = source.attachment_domain
         AND NOT origin_instance.is_self
        JOIN media_tombstone_destinations AS destination
          ON destination.attachment_id = source.attachment_id
         AND destination.attachment_domain = source.attachment_domain
        WHERE destination.destination_domain <> source.attachment_domain
        ON CONFLICT (destination, event_origin_domain, event_id) DO UPDATE
        SET status = 'pending',
            attempts = 0,
            next_retry_at = now(),
            last_error = NULL
        WHERE federation_outbox.status <> 'delivered'
        """
    )
    op.execute("DROP TABLE f8_retained_media_delete_events")


def upgrade() -> None:
    op.drop_index("ix_attachments_pending_gc", table_name="attachments")
    op.create_index(
        "ix_attachments_pending_gc",
        "attachments",
        ["upload_expires_at"],
        postgresql_where=sa.text("finalized_at IS NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_attachments_content_digest",
        "attachments",
        ["origin_domain", "content_sha256"],
        postgresql_where=sa.text("content_sha256 IS NOT NULL"),
    )
    op.create_table(
        "attachment_federation_recipients",
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_domain", sa.String(length=253), nullable=False),
        sa.Column("destination_domain", sa.String(length=253), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "destination_domain <> attachment_domain",
            name=op.f("ck_attachment_federation_recipients_remote_destination"),
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id", "attachment_domain"],
            ["attachments.id", "attachments.origin_domain"],
            name=op.f("fk_attachment_federation_recipients_attachment_ref_attachments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
            name=op.f("pk_attachment_federation_recipients"),
        ),
    )
    op.create_index(
        op.f("ix_attachment_federation_recipients_destination"),
        "attachment_federation_recipients",
        ["destination_domain"],
    )
    op.create_table(
        "media_tombstone_sources",
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_domain", sa.String(length=253), nullable=False),
        sa.Column("signer_id", sa.BigInteger(), nullable=False),
        sa.Column("signer_domain", sa.String(length=253), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attachment_id >= 0",
            name=op.f("ck_media_tombstone_sources_nonnegative_attachment_id"),
        ),
        sa.CheckConstraint(
            "signer_id >= 0",
            name=op.f("ck_media_tombstone_sources_nonnegative_signer_id"),
        ),
        sa.CheckConstraint(
            "generation >= 0",
            name=op.f("ck_media_tombstone_sources_nonnegative_generation"),
        ),
        sa.PrimaryKeyConstraint(
            "attachment_id",
            "attachment_domain",
            name=op.f("pk_media_tombstone_sources"),
        ),
    )
    op.create_index(
        op.f("ix_media_tombstone_sources_key"),
        "media_tombstone_sources",
        ["key_id"],
    )
    op.create_table(
        "media_tombstone_destinations",
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_domain", sa.String(length=253), nullable=False),
        sa.Column("destination_domain", sa.String(length=253), nullable=False),
        sa.Column("room_kind", sa.String(length=16), nullable=True),
        sa.Column("room_id", sa.BigInteger(), nullable=True),
        sa.Column("room_domain", sa.String(length=253), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attachment_id >= 0",
            name=op.f("ck_media_tombstone_destinations_nonnegative_attachment_id"),
        ),
        sa.CheckConstraint(
            "destination_domain <> attachment_domain",
            name=op.f("ck_media_tombstone_destinations_remote_destination"),
        ),
        sa.CheckConstraint(
            "(room_kind IS NULL AND room_id IS NULL AND room_domain IS NULL) OR "
            "(room_kind IN ('guild','group_dm') AND room_id IS NOT NULL "
            "AND room_id >= 0 AND room_domain IS NOT NULL)",
            name=op.f("ck_media_tombstone_destinations_room_ref_complete"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_domain"],
            ["instances.domain"],
            name=op.f("fk_media_tombstone_destinations_destination_domain_instances"),
        ),
        sa.PrimaryKeyConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
            name=op.f("pk_media_tombstone_destinations"),
        ),
    )
    op.create_index(
        op.f("ix_media_tombstone_destinations_destination"),
        "media_tombstone_destinations",
        ["destination_domain"],
    )
    op.create_index(
        op.f("ix_media_tombstone_destinations_room"),
        "media_tombstone_destinations",
        ["room_kind", "room_id", "room_domain"],
    )
    op.create_table(
        "room_federation_recipients",
        sa.Column("room_kind", sa.String(length=16), nullable=False),
        sa.Column("room_id", sa.BigInteger(), nullable=False),
        sa.Column("room_domain", sa.String(length=253), nullable=False),
        sa.Column("destination_domain", sa.String(length=253), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "room_kind IN ('guild','group_dm')",
            name=op.f("ck_room_federation_recipients_room_kind"),
        ),
        sa.CheckConstraint(
            "room_id >= 0",
            name=op.f("ck_room_federation_recipients_nonnegative_room_id"),
        ),
        sa.CheckConstraint(
            "destination_domain <> room_domain",
            name=op.f("ck_room_federation_recipients_remote_destination"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_domain"],
            ["instances.domain"],
            name=op.f("fk_room_federation_recipients_destination_domain_instances"),
        ),
        sa.PrimaryKeyConstraint(
            "room_kind",
            "room_id",
            "room_domain",
            "destination_domain",
            name=op.f("pk_room_federation_recipients"),
        ),
    )
    op.create_index(
        op.f("ix_room_federation_recipients_destination"),
        "room_federation_recipients",
        ["destination_domain"],
    )
    op.create_table(
        "terminal_room_deletions",
        sa.Column("room_kind", sa.String(length=16), nullable=False),
        sa.Column("room_id", sa.BigInteger(), nullable=False),
        sa.Column("room_domain", sa.String(length=253), nullable=False),
        sa.Column("destination_domain", sa.String(length=253), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_domain", sa.String(length=253), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "room_kind IN ('guild','group_dm')",
            name=op.f("ck_terminal_room_deletions_room_kind"),
        ),
        sa.CheckConstraint(
            "room_id >= 0",
            name=op.f("ck_terminal_room_deletions_nonnegative_room_id"),
        ),
        sa.CheckConstraint(
            "actor_id >= 0",
            name=op.f("ck_terminal_room_deletions_nonnegative_actor_id"),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_terminal_room_deletions_positive_generation"),
        ),
        sa.CheckConstraint(
            "(room_kind = 'guild' AND event_type = 'guild.instance_access.revoked') OR "
            "(room_kind = 'group_dm' AND event_type = 'dm.group.state')",
            name=op.f("ck_terminal_room_deletions_event_matches_room_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_domain"],
            ["instances.domain"],
            name=op.f("fk_terminal_room_deletions_destination_domain_instances"),
        ),
        sa.PrimaryKeyConstraint(
            "room_kind",
            "room_id",
            "room_domain",
            "destination_domain",
            name=op.f("pk_terminal_room_deletions"),
        ),
    )
    op.create_index(
        op.f("ix_terminal_room_deletions_pending_key"),
        "terminal_room_deletions",
        ["room_domain", "key_id"],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.create_index(
        op.f("ix_terminal_room_deletions_event"),
        "terminal_room_deletions",
        ["room_domain", "event_id"],
    )
    op.create_table(
        "guild_media_deletion_requests",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_domain", sa.String(length=253), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(length=253), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_domain", sa.String(length=253), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "guild_id >= 0",
            name=op.f("ck_guild_media_deletion_requests_nonnegative_guild_id"),
        ),
        sa.CheckConstraint(
            "attachment_id >= 0",
            name=op.f("ck_guild_media_deletion_requests_nonnegative_attachment_id"),
        ),
        sa.CheckConstraint(
            "message_id >= 0",
            name=op.f("ck_guild_media_deletion_requests_nonnegative_message_id"),
        ),
        sa.CheckConstraint(
            "actor_id >= 0",
            name=op.f("ck_guild_media_deletion_requests_nonnegative_actor_id"),
        ),
        sa.CheckConstraint(
            "generation > 0",
            name=op.f("ck_guild_media_deletion_requests_positive_generation"),
        ),
        sa.CheckConstraint(
            "guild_domain = message_domain",
            name=op.f("ck_guild_media_deletion_requests_message_at_guild_home"),
        ),
        sa.CheckConstraint(
            "attachment_domain <> guild_domain",
            name=op.f("ck_guild_media_deletion_requests_remote_attachment_origin"),
        ),
        sa.ForeignKeyConstraint(
            ["attachment_domain"],
            ["instances.domain"],
            name=op.f("fk_guild_media_deletion_requests_attachment_domain_instances"),
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "guild_domain",
            "attachment_id",
            "attachment_domain",
            name=op.f("pk_guild_media_deletion_requests"),
        ),
    )
    op.create_index(
        op.f("ix_guild_media_deletion_requests_pending_key"),
        "guild_media_deletion_requests",
        ["guild_domain", "key_id"],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    op.create_index(
        op.f("ix_guild_media_deletion_requests_event"),
        "guild_media_deletion_requests",
        ["guild_domain", "event_id"],
    )
    _backfill_retained_delivery_routes()
    _backfill_legacy_media_delete_sources()


def downgrade() -> None:
    # Deliberately retain origin-signed remote media.delete envelopes and relay
    # outboxes repaired by upgrade. There is no safe way to distinguish a row
    # inserted here from a concurrent legitimate queue write, and deleting
    # either would resurrect terminal media after a schema rollback.
    op.drop_index("ix_attachments_content_digest", table_name="attachments")
    op.drop_index("ix_attachments_pending_gc", table_name="attachments")
    op.create_index(
        "ix_attachments_pending_gc",
        "attachments",
        ["upload_expires_at"],
        postgresql_where=sa.text("finalized_at IS NULL"),
    )
    op.drop_index(
        op.f("ix_guild_media_deletion_requests_event"),
        table_name="guild_media_deletion_requests",
    )
    op.drop_index(
        op.f("ix_guild_media_deletion_requests_pending_key"),
        table_name="guild_media_deletion_requests",
    )
    op.drop_table("guild_media_deletion_requests")
    op.drop_index(
        op.f("ix_terminal_room_deletions_event"),
        table_name="terminal_room_deletions",
    )
    op.drop_index(
        op.f("ix_terminal_room_deletions_pending_key"),
        table_name="terminal_room_deletions",
    )
    op.drop_table("terminal_room_deletions")
    op.drop_index(
        op.f("ix_room_federation_recipients_destination"),
        table_name="room_federation_recipients",
    )
    op.drop_table("room_federation_recipients")
    op.drop_index(
        op.f("ix_media_tombstone_destinations_room"),
        table_name="media_tombstone_destinations",
    )
    op.drop_index(
        op.f("ix_media_tombstone_destinations_destination"),
        table_name="media_tombstone_destinations",
    )
    op.drop_table("media_tombstone_destinations")
    op.drop_index(
        op.f("ix_media_tombstone_sources_key"),
        table_name="media_tombstone_sources",
    )
    op.drop_table("media_tombstone_sources")
    op.drop_index(
        op.f("ix_attachment_federation_recipients_destination"),
        table_name="attachment_federation_recipients",
    )
    op.drop_table("attachment_federation_recipients")
