"""add E2EE devices and one-use MLS key packages

Revision ID: d93b7e5f201a
Revises: c82f4a1d6e90
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d93b7e5f201a"
down_revision: str | None = "c82f4a1d6e90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "e2ee_device_generation",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "e2ee_device_generation_nonnegative",
        "users",
        "e2ee_device_generation >= 0",
    )
    op.create_table(
        "e2ee_devices",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("identity_key", sa.LargeBinary(length=32), nullable=False),
        sa.Column("credential", sa.LargeBinary(), nullable=False),
        sa.Column("device_name", sa.String(length=100), nullable=False),
        sa.Column("platform", sa.String(length=16), nullable=False),
        sa.Column(
            "capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("trust_state", sa.String(length=16), nullable=False, server_default="unverified"),
        sa.Column(
            "registered_session_id",
            sa.String(length=64),
            sa.ForeignKey("sessions.id", ondelete="SET NULL"),
        ),
        sa.Column("device_generation", sa.BigInteger(), nullable=False),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name="fk_e2ee_devices_local_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "user_id",
            "user_domain",
            "identity_key",
            name="uq_e2ee_devices_user_identity_key",
        ),
        sa.UniqueConstraint(
            "user_id",
            "user_domain",
            "device_generation",
            name="uq_e2ee_devices_user_generation",
        ),
        sa.UniqueConstraint(
            "id",
            "user_id",
            "user_domain",
            name="uq_e2ee_devices_ref_user",
        ),
        sa.CheckConstraint("user_is_local", name="e2ee_devices_user_is_local"),
        sa.CheckConstraint("octet_length(identity_key) = 32", name="e2ee_identity_key_length"),
        sa.CheckConstraint(
            "octet_length(credential) BETWEEN 1 AND 16384",
            name="e2ee_device_credential_length",
        ),
        sa.CheckConstraint(
            "platform IN ('web','desktop','android','ios')",
            name="e2ee_device_platform_value",
        ),
        sa.CheckConstraint(
            "trust_state IN ('unverified','verified','blocked')",
            name="e2ee_device_trust_state_value",
        ),
        sa.CheckConstraint("device_generation > 0", name="e2ee_device_generation_positive"),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name="e2ee_device_capabilities_array",
        ),
    )
    op.create_index(
        "ix_e2ee_devices_user_active",
        "e2ee_devices",
        ["user_id", "user_domain", "revoked_at"],
    )
    op.create_table(
        "e2ee_key_packages",
        sa.Column("id", sa.String(length=43), primary_key=True),
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cipher_suite", sa.String(length=96), nullable=False),
        sa.Column("package_data", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("claimed_by_id", sa.BigInteger()),
        sa.Column("claimed_by_domain", sa.String(length=253)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["device_id", "user_id", "user_domain"],
            ["e2ee_devices.id", "e2ee_devices.user_id", "e2ee_devices.user_domain"],
            name="fk_e2ee_key_packages_device_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            name="fk_e2ee_key_packages_local_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("device_id", "id", name="uq_e2ee_key_package_device"),
        sa.CheckConstraint("user_is_local", name="e2ee_key_packages_user_is_local"),
        sa.CheckConstraint(
            "octet_length(package_data) BETWEEN 1 AND 32768",
            name="e2ee_key_package_data_length",
        ),
        sa.CheckConstraint(
            "(claimed_by_id IS NULL) = (claimed_by_domain IS NULL)",
            name="e2ee_key_package_claim_complete",
        ),
        sa.CheckConstraint(
            "(claimed_at IS NULL) = (claimed_by_id IS NULL)",
            name="e2ee_key_package_claim_state",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="e2ee_key_package_positive_lifetime",
        ),
    )
    op.create_index(
        "ix_e2ee_key_packages_available",
        "e2ee_key_packages",
        ["user_id", "user_domain", "device_id", "expires_at"],
        postgresql_where=sa.text("claimed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_e2ee_key_packages_available", table_name="e2ee_key_packages")
    op.drop_table("e2ee_key_packages")
    op.drop_index("ix_e2ee_devices_user_active", table_name="e2ee_devices")
    op.drop_table("e2ee_devices")
    op.drop_constraint("e2ee_device_generation_nonnegative", "users", type_="check")
    op.drop_column("users", "e2ee_device_generation")
