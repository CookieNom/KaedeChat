"""Add iOS VoIP relay routes.

Revision ID: 6b1f4d8a2c90
Revises: 4ea6c2d8f953
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6b1f4d8a2c90"
down_revision: str | None = "4ea6c2d8f953"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_relay_subscriptions",
        sa.Column("provider", sa.String(16), server_default="fcm", nullable=False),
    )
    op.create_check_constraint(
        "ck_push_relay_subscriptions_provider_value",
        "push_relay_subscriptions",
        "provider IN ('fcm','apns_voip')",
    )
    op.create_check_constraint(
        "ck_push_relay_subscriptions_provider_platform",
        "push_relay_subscriptions",
        "provider = 'fcm' OR platform = 'ios'",
    )
    op.add_column("push_devices", sa.Column("relay_voip_subscription_id", sa.String(64)))
    op.add_column("push_devices", sa.Column("relay_voip_route_id", sa.String(64)))
    op.add_column("push_devices", sa.Column("relay_voip_wake_secret_encrypted", sa.LargeBinary()))
    op.create_unique_constraint(
        "uq_push_devices_relay_voip_subscription_id",
        "push_devices",
        ["relay_voip_subscription_id"],
    )
    op.drop_constraint("ck_push_devices_transport_fields", "push_devices", type_="check")
    op.create_check_constraint(
        "ck_push_devices_transport_fields",
        "push_devices",
        "(transport = 'direct_fcm' AND token_hash IS NOT NULL "
        "AND token_encrypted IS NOT NULL AND relay_origin IS NULL "
        "AND relay_subscription_id IS NULL AND relay_route_id IS NULL "
        "AND relay_wake_secret_encrypted IS NULL "
        "AND relay_voip_subscription_id IS NULL AND relay_voip_route_id IS NULL "
        "AND relay_voip_wake_secret_encrypted IS NULL) OR "
        "(transport = 'relay' AND token_hash IS NULL AND token_encrypted IS NULL "
        "AND relay_origin IS NOT NULL AND relay_subscription_id IS NOT NULL "
        "AND relay_route_id IS NOT NULL AND relay_wake_secret_encrypted IS NOT NULL "
        "AND ((relay_voip_subscription_id IS NULL AND relay_voip_route_id IS NULL "
        "AND relay_voip_wake_secret_encrypted IS NULL) OR "
        "(platform = 'ios' AND relay_voip_subscription_id IS NOT NULL "
        "AND relay_voip_route_id IS NOT NULL "
        "AND relay_voip_wake_secret_encrypted IS NOT NULL)))",
    )


def downgrade() -> None:
    op.drop_constraint("ck_push_devices_transport_fields", "push_devices", type_="check")
    op.create_check_constraint(
        "ck_push_devices_transport_fields",
        "push_devices",
        "(transport = 'direct_fcm' AND token_hash IS NOT NULL "
        "AND token_encrypted IS NOT NULL AND relay_origin IS NULL "
        "AND relay_subscription_id IS NULL AND relay_route_id IS NULL "
        "AND relay_wake_secret_encrypted IS NULL) OR "
        "(transport = 'relay' AND token_hash IS NULL AND token_encrypted IS NULL "
        "AND relay_origin IS NOT NULL AND relay_subscription_id IS NOT NULL "
        "AND relay_route_id IS NOT NULL AND relay_wake_secret_encrypted IS NOT NULL)",
    )
    op.drop_constraint("uq_push_devices_relay_voip_subscription_id", "push_devices", type_="unique")
    op.drop_column("push_devices", "relay_voip_wake_secret_encrypted")
    op.drop_column("push_devices", "relay_voip_route_id")
    op.drop_column("push_devices", "relay_voip_subscription_id")
    op.drop_constraint(
        "ck_push_relay_subscriptions_provider_platform",
        "push_relay_subscriptions",
        type_="check",
    )
    op.drop_constraint(
        "ck_push_relay_subscriptions_provider_value",
        "push_relay_subscriptions",
        type_="check",
    )
    op.drop_column("push_relay_subscriptions", "provider")
