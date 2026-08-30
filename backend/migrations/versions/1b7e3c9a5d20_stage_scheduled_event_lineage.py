"""Bind Stage instances to their scheduled event's guild and channel.

Revision ID: 1b7e3c9a5d20
Revises: 0a6d2f9c4b81
"""

from collections.abc import Sequence

from alembic import op

revision: str = "1b7e3c9a5d20"
down_revision: str | None = "0a6d2f9c4b81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_LINEAGE_UNIQUE = "uq_guild_scheduled_events_stage_lineage"
STAGE_CHANNEL_LINEAGE_FK = "fk_stage_instances_channel_guild_lineage"
STAGE_EVENT_LINEAGE_FK = "fk_stage_instances_scheduled_event_lineage"

INVALID_CHANNEL_LINEAGE_PREFLIGHT_SQL = """
DO $$
DECLARE
    invalid_stage text;
BEGIN
    SELECT stage.id::text || '@' || stage.origin_domain
    INTO invalid_stage
    FROM stage_instances AS stage
    LEFT JOIN channels AS channel
      ON channel.id = stage.channel_id
     AND channel.origin_domain = stage.channel_domain
     AND channel.guild_id = stage.guild_id
     AND channel.guild_domain = stage.guild_domain
    WHERE channel.id IS NULL
    ORDER BY stage.origin_domain, stage.id
    LIMIT 1;

    IF invalid_stage IS NOT NULL THEN
        RAISE EXCEPTION
            'Stage channel/guild lineage is invalid for %', invalid_stage
            USING ERRCODE = '23503',
                  HINT = 'repair the Stage guild/channel binding before retrying';
    END IF;
END
$$;
"""

INVALID_LINEAGE_PREFLIGHT_SQL = """
DO $$
DECLARE
    invalid_stage text;
BEGIN
    SELECT stage.id::text || '@' || stage.origin_domain
    INTO invalid_stage
    FROM stage_instances AS stage
    LEFT JOIN guild_scheduled_events AS event
      ON event.id = stage.scheduled_event_id
     AND event.origin_domain = stage.scheduled_event_domain
     AND event.guild_id = stage.guild_id
     AND event.guild_domain = stage.guild_domain
     AND event.channel_id = stage.channel_id
     AND event.channel_domain = stage.channel_domain
    WHERE stage.scheduled_event_id IS NOT NULL
      AND event.id IS NULL
    ORDER BY stage.origin_domain, stage.id
    LIMIT 1;

    IF invalid_stage IS NOT NULL THEN
        RAISE EXCEPTION
            'Stage scheduled-event lineage is invalid for %', invalid_stage
            USING ERRCODE = '23503',
                  HINT = 'repair the Stage guild/channel event reference before retrying';
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(INVALID_CHANNEL_LINEAGE_PREFLIGHT_SQL)
    op.execute(INVALID_LINEAGE_PREFLIGHT_SQL)
    op.create_unique_constraint(
        EVENT_LINEAGE_UNIQUE,
        "guild_scheduled_events",
        [
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
    )
    op.create_foreign_key(
        STAGE_CHANNEL_LINEAGE_FK,
        "stage_instances",
        "channels",
        ["channel_id", "channel_domain", "guild_id", "guild_domain"],
        ["id", "origin_domain", "guild_id", "guild_domain"],
        ondelete="CASCADE",
    )
    # Keep the existing two-column ON DELETE SET NULL FK. It clears only the
    # nullable scheduled-event pair; this lineage FK then becomes inapplicable
    # under MATCH SIMPLE without trying to null the Stage's guild or channel.
    op.create_foreign_key(
        STAGE_EVENT_LINEAGE_FK,
        "stage_instances",
        "guild_scheduled_events",
        [
            "scheduled_event_id",
            "scheduled_event_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
        [
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            "channel_id",
            "channel_domain",
        ],
    )


def downgrade() -> None:
    op.drop_constraint(
        STAGE_EVENT_LINEAGE_FK,
        "stage_instances",
        type_="foreignkey",
    )
    op.drop_constraint(
        STAGE_CHANNEL_LINEAGE_FK,
        "stage_instances",
        type_="foreignkey",
    )
    op.drop_constraint(
        EVENT_LINEAGE_UNIQUE,
        "guild_scheduled_events",
        type_="unique",
    )
