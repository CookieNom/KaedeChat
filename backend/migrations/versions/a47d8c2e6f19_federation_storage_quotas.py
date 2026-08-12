"""bound retained federation inbox storage

Revision ID: a47d8c2e6f19
Revises: f91a2c7d5e40
Create Date: 2026-08-11 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a47d8c2e6f19"
down_revision: str | None = "f91a2c7d5e40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instances",
        sa.Column("federation_inbox_events", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "instances",
        sa.Column(
            "federation_inbox_event_bytes", sa.BigInteger(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "federation_events",
        sa.Column("envelope_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.execute("UPDATE federation_events SET envelope_bytes = octet_length(envelope::text)")
    op.execute(
        "UPDATE instances AS peer SET "
        "federation_inbox_events = ("
        "SELECT count(*) FROM federation_inbox AS inbox "
        "WHERE inbox.origin_domain = peer.domain), "
        "federation_inbox_event_bytes = ("
        "SELECT coalesce(sum(event.envelope_bytes), 0) "
        "FROM federation_events AS event WHERE event.origin_domain = peer.domain) "
        "WHERE NOT peer.is_self"
    )
    # Reuse the singleton self-instance row as the exact global ledger. This
    # keeps admission O(1) and gives all origins one row-lock serialization
    # point without introducing a second singleton table.
    op.execute(
        "UPDATE instances AS self_instance SET "
        "federation_inbox_events = (SELECT count(*) FROM federation_inbox), "
        "federation_inbox_event_bytes = ("
        "SELECT coalesce(sum(event.envelope_bytes), 0) FROM federation_events AS event "
        "JOIN instances AS origin ON origin.domain = event.origin_domain "
        "WHERE NOT origin.is_self) "
        "WHERE self_instance.is_self"
    )
    op.create_check_constraint(
        op.f("ck_instances_nonnegative_federation_inbox_events"),
        "instances",
        "federation_inbox_events >= 0",
    )
    op.create_check_constraint(
        op.f("ck_instances_nonnegative_federation_inbox_event_bytes"),
        "instances",
        "federation_inbox_event_bytes >= 0",
    )
    op.create_check_constraint(
        op.f("ck_federation_events_nonnegative_envelope_bytes"),
        "federation_events",
        "envelope_bytes >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_federation_events_nonnegative_envelope_bytes"),
        "federation_events",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_instances_nonnegative_federation_inbox_event_bytes"),
        "instances",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_instances_nonnegative_federation_inbox_events"),
        "instances",
        type_="check",
    )
    op.drop_column("federation_events", "envelope_bytes")
    op.drop_column("instances", "federation_inbox_event_bytes")
    op.drop_column("instances", "federation_inbox_events")
