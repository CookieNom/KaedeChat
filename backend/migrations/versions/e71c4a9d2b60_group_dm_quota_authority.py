"""scope federated group DM quotas to their authority

Revision ID: e71c4a9d2b60
Revises: d15a6c8e2f40
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e71c4a9d2b60"
down_revision: str | None = "d15a6c8e2f40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Direct conversations have one remote origin. Groups can span many homes,
    # so their authoritative home is the only stable quota scope shared by all
    # replicas. This also repairs groups created before multi-origin admission
    # was supported.
    op.execute(
        """
        UPDATE federated_dm_storage_usage AS usage
        SET remote_origin_domain = conversation.authority_domain,
            updated_at = now()
        FROM dm_conversations AS conversation
        WHERE conversation.id = usage.conversation_id
          AND conversation.origin_domain = usage.conversation_domain
          AND conversation.type = 'group'
        """
    )
    # The first group-DM release incorrectly applied the direct-message
    # two-home invariant before recording a replica. Remove only those exact
    # rejected claims so the sender can safely replay the same signed event.
    # Keep the O(1) inbox ledgers exact while doing so.
    op.execute(
        """
        CREATE TEMPORARY TABLE kaede_legacy_group_dm_rejections
        ON COMMIT DROP AS
        SELECT inbox.origin_domain, inbox.event_id, event.envelope_bytes
        FROM federation_inbox AS inbox
        JOIN federation_events AS event
          ON event.origin_domain = inbox.origin_domain
         AND event.event_id = inbox.event_id
        WHERE inbox.status = 'rejected'
          AND inbox.error = 'federated direct conversation must have exactly one remote origin'
          AND event.event_type = 'dm.group.state'
        """
    )
    op.execute(
        """
        UPDATE instances AS peer
        SET federation_inbox_events = greatest(
                0,
                peer.federation_inbox_events - rejected.event_count
            ),
            federation_inbox_event_bytes = greatest(
                0,
                peer.federation_inbox_event_bytes - rejected.event_bytes
            )
        FROM (
            SELECT origin_domain,
                   count(*)::bigint AS event_count,
                   coalesce(sum(envelope_bytes), 0)::bigint AS event_bytes
            FROM kaede_legacy_group_dm_rejections
            GROUP BY origin_domain
        ) AS rejected
        WHERE peer.domain = rejected.origin_domain
          AND NOT peer.is_self
        """
    )
    op.execute(
        """
        UPDATE instances AS self_instance
        SET federation_inbox_events = greatest(
                0,
                self_instance.federation_inbox_events - rejected.event_count
            ),
            federation_inbox_event_bytes = greatest(
                0,
                self_instance.federation_inbox_event_bytes - rejected.event_bytes
            )
        FROM (
            SELECT count(*)::bigint AS event_count,
                   coalesce(sum(envelope_bytes), 0)::bigint AS event_bytes
            FROM kaede_legacy_group_dm_rejections
        ) AS rejected
        WHERE self_instance.is_self
        """
    )
    op.execute(
        """
        DELETE FROM federation_inbox AS inbox
        USING kaede_legacy_group_dm_rejections AS rejected
        WHERE inbox.origin_domain = rejected.origin_domain
          AND inbox.event_id = rejected.event_id
        """
    )
    op.execute(
        """
        DELETE FROM federation_events AS event
        USING kaede_legacy_group_dm_rejections AS rejected
        WHERE event.origin_domain = rejected.origin_domain
          AND event.event_id = rejected.event_id
        """
    )
    # On an authority, make the corresponding signed states eligible for the
    # normal ordered outbox drain. Delivery keeps them retryable during a
    # rolling deployment until the recipient has this migration as well.
    op.execute(
        """
        UPDATE federation_outbox AS outbox
        SET status = 'retry',
            attempts = 0,
            next_retry_at = now(),
            last_error = NULL
        FROM federation_events AS event
        WHERE event.origin_domain = outbox.event_origin_domain
          AND event.event_id = outbox.event_id
          AND event.event_type = 'dm.group.state'
          AND outbox.status = 'failed'
          AND outbox.last_error = 'KAED_FED_EVENT_REJECTED'
        """
    )


def downgrade() -> None:
    # Restore the legacy deterministic attribution used by the original quota
    # backfill. Multi-home groups are unsupported by the downgraded code.
    op.execute(
        """
        UPDATE federated_dm_storage_usage AS usage
        SET remote_origin_domain = legacy.remote_origin_domain,
            updated_at = now()
        FROM (
            SELECT conversation.id AS conversation_id,
                   conversation.origin_domain AS conversation_domain,
                   min(participant.user_domain) FILTER (
                       WHERE participant.user_domain <> self_instance.domain
                   ) AS remote_origin_domain
            FROM dm_conversations AS conversation
            JOIN dm_participants AS participant
              ON participant.conversation_id = conversation.id
             AND participant.conversation_domain = conversation.origin_domain
            CROSS JOIN instances AS self_instance
            WHERE conversation.type = 'group'
              AND self_instance.is_self
            GROUP BY conversation.id, conversation.origin_domain
        ) AS legacy
        WHERE usage.conversation_id = legacy.conversation_id
          AND usage.conversation_domain = legacy.conversation_domain
          AND legacy.remote_origin_domain IS NOT NULL
        """
    )
