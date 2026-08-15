"""push relay transport

Revision ID: c94d7e2a61f0
Revises: b84e2f6a19d7
Create Date: 2026-08-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c94d7e2a61f0"
down_revision: str | None = "b84e2f6a19d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "push_devices",
        sa.Column("transport", sa.String(length=16), server_default="direct_fcm", nullable=False),
    )
    op.add_column("push_devices", sa.Column("relay_origin", sa.String(length=253)))
    op.add_column("push_devices", sa.Column("relay_subscription_id", sa.String(length=64)))
    op.add_column("push_devices", sa.Column("relay_route_id", sa.String(length=64)))
    op.add_column("push_devices", sa.Column("relay_wake_secret_encrypted", sa.LargeBinary()))
    op.alter_column("push_devices", "token_hash", existing_type=sa.LargeBinary(32), nullable=True)
    op.alter_column(
        "push_devices", "token_encrypted", existing_type=sa.LargeBinary(), nullable=True
    )
    op.create_unique_constraint(
        "uq_push_devices_relay_subscription_id",
        "push_devices",
        ["relay_subscription_id"],
    )
    op.create_check_constraint(
        "ck_push_devices_transport_value",
        "push_devices",
        "transport IN ('relay','direct_fcm')",
    )
    op.create_check_constraint(
        "ck_push_devices_transport_fields",
        "push_devices",
        "(transport = 'direct_fcm' AND token_hash IS NOT NULL AND token_encrypted IS NOT NULL "
        "AND relay_origin IS NULL AND relay_subscription_id IS NULL "
        "AND relay_route_id IS NULL AND relay_wake_secret_encrypted IS NULL) OR "
        "(transport = 'relay' AND token_hash IS NULL AND token_encrypted IS NULL "
        "AND relay_origin IS NOT NULL AND relay_subscription_id IS NOT NULL "
        "AND relay_route_id IS NOT NULL AND relay_wake_secret_encrypted IS NOT NULL)",
    )
    op.alter_column("push_devices", "transport", server_default="relay")

    op.create_table(
        "push_wake_outbox",
        sa.Column("request_id", sa.String(length=43), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(length=36),
            sa.ForeignKey("push_devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("message_domain", sa.String(length=253), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("event_token", sa.String(length=43)),
        sa.Column("delivery_id", sa.String(length=43), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "device_id", "message_id", "message_domain", "kind", name="uq_push_wake_event"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_push_wake_outbox_nonnegative_attempts"),
        sa.CheckConstraint(
            "kind IN ('direct_message','mention','guild_message')",
            name="ck_push_wake_outbox_kind_value",
        ),
    )
    op.create_index(
        "ix_push_wake_outbox_due", "push_wake_outbox", ["next_attempt_at", "expires_at"]
    )

    op.create_table(
        "push_relay_subscriptions",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("grant_id", sa.String(length=43), nullable=False, unique=True),
        sa.Column("home_origin", sa.String(length=253), nullable=False),
        sa.Column("app_id", sa.String(length=160), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column("route_id", sa.String(length=43), nullable=False),
        sa.Column("provider_token_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("provider_token_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("management_secret_hash", sa.LargeBinary(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "platform IN ('android','ios')",
            name="ck_push_relay_subscriptions_platform_value",
        ),
        sa.CheckConstraint(
            "octet_length(provider_token_hash) = 32",
            name="ck_push_relay_subscriptions_token_hash_length",
        ),
        sa.CheckConstraint(
            "octet_length(management_secret_hash) = 32",
            name="ck_push_relay_subscriptions_management_secret_hash_length",
        ),
    )
    op.create_index(
        "ix_push_relay_subscriptions_home",
        "push_relay_subscriptions",
        ["home_origin", "enabled"],
    )
    op.create_index(
        "ix_push_relay_subscriptions_token_hash",
        "push_relay_subscriptions",
        ["provider_token_hash"],
    )

    op.create_table(
        "push_relay_deliveries",
        sa.Column("home_origin", sa.String(length=253), nullable=False),
        sa.Column("request_id", sa.String(length=43), nullable=False),
        sa.Column(
            "subscription_id",
            sa.String(length=64),
            sa.ForeignKey("push_relay_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("route_id", sa.String(length=43), nullable=False),
        sa.Column("event_token", sa.String(length=43), nullable=False),
        sa.Column("delivery_id", sa.String(length=43), nullable=False),
        sa.Column("wake_mac", sa.String(length=43), nullable=False),
        sa.Column("priority", sa.String(length=12), server_default="normal", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_error", sa.String(length=200)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("home_origin", "request_id"),
        sa.CheckConstraint(
            "priority IN ('normal','urgent')",
            name="ck_push_relay_deliveries_priority_value",
        ),
        sa.CheckConstraint(
            "state IN ('pending','delivered','expired','invalid')",
            name="ck_push_relay_deliveries_state_value",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_push_relay_deliveries_nonnegative_attempts"),
    )
    op.create_index(
        "ix_push_relay_deliveries_due",
        "push_relay_deliveries",
        ["state", "next_attempt_at", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("push_relay_deliveries")
    op.drop_table("push_relay_subscriptions")
    op.drop_table("push_wake_outbox")
    op.drop_constraint("ck_push_devices_transport_fields", "push_devices", type_="check")
    op.drop_constraint("ck_push_devices_transport_value", "push_devices", type_="check")
    op.drop_constraint("uq_push_devices_relay_subscription_id", "push_devices", type_="unique")
    # The previous schema cannot represent relay-only registrations. Downgrade
    # removes those replaceable device bindings before restoring NOT NULL.
    op.execute("DELETE FROM push_devices WHERE transport = 'relay'")
    op.drop_column("push_devices", "relay_wake_secret_encrypted")
    op.drop_column("push_devices", "relay_route_id")
    op.drop_column("push_devices", "relay_subscription_id")
    op.drop_column("push_devices", "relay_origin")
    op.drop_column("push_devices", "transport")
    op.alter_column(
        "push_devices", "token_encrypted", existing_type=sa.LargeBinary(), nullable=False
    )
    op.alter_column("push_devices", "token_hash", existing_type=sa.LargeBinary(32), nullable=False)
