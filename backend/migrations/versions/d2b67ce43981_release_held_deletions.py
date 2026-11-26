"""Return previously policy-held removal controls to the delivery queue."""

from alembic import op

revision = "d2b67ce43981"
down_revision = "c89fa1e42bd7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The drainer validates the full envelope before dispatch under a block.
    op.execute("""
        UPDATE federation_outbox AS o
        SET status = 'pending', attempts = 0, next_retry_at = now(), last_error = NULL
        FROM federation_events AS e
        WHERE (e.origin_domain, e.event_id) = (o.event_origin_domain, o.event_id)
          AND o.status = 'circuit' AND o.last_error LIKE 'held by local federation block:%'
          AND (e.event_type IN ('media.delete', 'guild.media.delete.request')
               OR (e.event_type IN ('guild.instance_access.revoked', 'dm.group.state')
                   AND e.envelope->'content' ? '_terminal_generation'))
    """)


def downgrade() -> None:
    pass
