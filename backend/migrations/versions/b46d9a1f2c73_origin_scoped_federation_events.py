"""scope federation event identities by origin

Revision ID: b46d9a1f2c73
Revises: a35c8d2e7f41
Create Date: 2026-07-20 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b46d9a1f2c73"
down_revision: str | None = "a35c8d2e7f41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "federation_outbox",
        sa.Column("event_origin_domain", sa.String(length=253), nullable=True),
    )
    op.execute(
        """
        UPDATE federation_outbox AS outbox
        SET event_origin_domain = event.origin_domain
        FROM federation_events AS event
        WHERE event.event_id = outbox.event_id
        """
    )
    op.alter_column("federation_outbox", "event_origin_domain", nullable=False)

    op.drop_constraint(
        "fk_federation_outbox_event_id_federation_events",
        "federation_outbox",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_federation_outbox_destination_event_id",
        "federation_outbox",
        type_="unique",
    )
    op.drop_constraint("pk_federation_events", "federation_events", type_="primary")
    op.create_primary_key(
        "pk_federation_events",
        "federation_events",
        ["origin_domain", "event_id"],
    )
    op.create_foreign_key(
        "fk_federation_outbox_event_ref",
        "federation_outbox",
        "federation_events",
        ["event_origin_domain", "event_id"],
        ["origin_domain", "event_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_federation_outbox_destination_event_ref",
        "federation_outbox",
        ["destination", "event_origin_domain", "event_id"],
    )

    op.drop_constraint("uq_guild_events_event_id", "guild_events", type_="unique")
    op.create_unique_constraint(
        "uq_guild_events_guild_domain_event_id",
        "guild_events",
        ["guild_domain", "event_id"],
    )


def downgrade() -> None:
    # The legacy schema cannot represent valid cross-origin ID collisions. Fail
    # before changing constraints instead of partially downgrading or silently
    # deleting authenticated federation history.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM federation_events
                GROUP BY event_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: federation event IDs collide across origins'
                    USING HINT = 'retain this revision or archive one origin before retrying';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM guild_events
                GROUP BY event_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade: guild event IDs collide across guild domains'
                    USING HINT = 'retain this revision or archive one guild domain before retrying';
            END IF;
        END $$
        """
    )
    op.drop_constraint(
        "uq_guild_events_guild_domain_event_id", "guild_events", type_="unique"
    )
    op.create_unique_constraint("uq_guild_events_event_id", "guild_events", ["event_id"])

    op.drop_constraint(
        "uq_federation_outbox_destination_event_ref",
        "federation_outbox",
        type_="unique",
    )
    op.drop_constraint(
        "fk_federation_outbox_event_ref", "federation_outbox", type_="foreignkey"
    )
    op.drop_constraint("pk_federation_events", "federation_events", type_="primary")
    op.create_primary_key("pk_federation_events", "federation_events", ["event_id"])
    op.create_foreign_key(
        "fk_federation_outbox_event_id_federation_events",
        "federation_outbox",
        "federation_events",
        ["event_id"],
        ["event_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_federation_outbox_destination_event_id",
        "federation_outbox",
        ["destination", "event_id"],
    )
    op.drop_column("federation_outbox", "event_origin_domain")
