"""replay initial federated group DM snapshots

Revision ID: f03b6d9e2a71
Revises: e71c4a9d2b60
Create Date: 2026-08-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f03b6d9e2a71"
down_revision: str | None = "e71c4a9d2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rejected applications keep only the inbox idempotency claim because the
    # event and its attempted projection are rolled back to a savepoint. The
    # previous repair joined through federation_events and therefore missed
    # these claims. Match the two exact group-state validation errors instead.
    op.execute(
        """
        CREATE TEMPORARY TABLE kaede_replayable_group_dm_rejections
        ON COMMIT DROP AS
        SELECT inbox.origin_domain,
               inbox.event_id,
               coalesce(event.envelope_bytes, 0)::bigint AS envelope_bytes
        FROM federation_inbox AS inbox
        LEFT JOIN federation_events AS event
          ON event.origin_domain = inbox.origin_domain
         AND event.event_id = inbox.event_id
        WHERE inbox.status = 'rejected'
          AND inbox.result_code = 'KAED_FED_EVENT_REJECTED'
          AND inbox.error IN (
              'federated direct conversation must have exactly one remote origin',
              'group DM notice does not match the membership transition'
          )
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
            FROM kaede_replayable_group_dm_rejections
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
            FROM kaede_replayable_group_dm_rejections
        ) AS rejected
        WHERE self_instance.is_self
        """
    )
    op.execute(
        """
        DELETE FROM federation_inbox AS inbox
        USING kaede_replayable_group_dm_rejections AS rejected
        WHERE inbox.origin_domain = rejected.origin_domain
          AND inbox.event_id = rejected.event_id
        """
    )
    op.execute(
        """
        DELETE FROM federation_events AS event
        USING kaede_replayable_group_dm_rejections AS rejected
        WHERE event.origin_domain = rejected.origin_domain
          AND event.event_id = rejected.event_id
        """
    )
    # Apply this migration to recipients before authorities during a rolling
    # deployment. Once both sides have the first-snapshot fix, make every
    # affected signed state immediately eligible for ordered redelivery.
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
          AND outbox.status IN ('failed', 'retry', 'circuit')
          AND outbox.last_error = 'KAED_FED_EVENT_REJECTED'
        """
    )


def downgrade() -> None:
    # Rejected idempotency claims and retry scheduling are operational repair
    # data and cannot be reconstructed safely after replay.
    pass
