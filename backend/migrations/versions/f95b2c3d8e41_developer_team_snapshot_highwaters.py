"""Add projection high-waters and foreign-install authority leases.

Revision ID: f95b2c3d8e41
Revises: e84f1a2c7d30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f95b2c3d8e41"
down_revision: str | None = "e84f1a2c7d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECTION_DOWNGRADE_PREFLIGHT_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM bot_user_installations
        WHERE authority_expires_at IS NOT NULL
    ) OR EXISTS (
        SELECT 1
        FROM developer_teams
        WHERE federation_metadata_fingerprint IS NOT NULL
           OR federation_applications_fingerprint IS NOT NULL
    ) OR EXISTS (
        SELECT 1 FROM developer_team_member_highwaters
    ) THEN
        RAISE EXCEPTION
            'projection high-water downgrade blocked by retained federation authority state'
            USING ERRCODE = '23514',
                  HINT = 'retain this revision or deliberately clear the projection state first';
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.add_column(
        "bot_user_installations",
        sa.Column("authority_expires_at", sa.DateTime(timezone=True)),
    )
    # Existing mirrors must fail closed immediately after upgrade while
    # authority-owned rows retain their historical NULL/source semantics.
    op.execute(
        """
        UPDATE bot_user_installations AS installation
        SET authority_expires_at = CURRENT_TIMESTAMP
        FROM users AS installing_user
        WHERE installing_user.id = installation.user_id
          AND installing_user.origin_domain = installation.user_domain
          AND installing_user.is_local IS FALSE
        """
    )
    op.create_index(
        "ix_bot_user_installations_authority_expiry",
        "bot_user_installations",
        ["authority_expires_at"],
        postgresql_where=sa.text("status = 'active' AND authority_expires_at IS NOT NULL"),
    )
    op.add_column(
        "developer_teams",
        sa.Column("federation_metadata_fingerprint", sa.LargeBinary(length=32)),
    )
    op.add_column(
        "developer_teams",
        sa.Column("federation_applications_fingerprint", sa.LargeBinary(length=32)),
    )
    op.create_check_constraint(
        op.f("ck_developer_teams_developer_team_federation_fingerprint_lengths"),
        "developer_teams",
        "(federation_metadata_fingerprint IS NULL "
        "OR octet_length(federation_metadata_fingerprint) = 32) "
        "AND (federation_applications_fingerprint IS NULL "
        "OR octet_length(federation_applications_fingerprint) = 32)",
    )
    op.create_table(
        "developer_team_member_highwaters",
        sa.Column("team_id", sa.BigInteger(), nullable=False),
        sa.Column("team_domain", sa.String(length=253), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(length=253), nullable=False),
        sa.Column("user_is_local", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_fingerprint", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "user_is_local",
            name=op.f(
                "ck_developer_team_member_highwaters_developer_team_member_highwater_user_is_local"
            ),
        ),
        sa.CheckConstraint(
            "revision >= 1 AND octet_length(snapshot_fingerprint) = 32",
            name=op.f("ck_developer_team_member_highwaters_developer_team_member_highwater_values"),
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "team_domain"],
            ["developer_teams.id", "developer_teams.origin_domain"],
            ondelete="CASCADE",
            name=op.f("fk_developer_team_member_highwaters_team_id_developer_teams"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
            name=op.f("fk_developer_team_member_highwaters_user_id_users"),
        ),
        sa.PrimaryKeyConstraint(
            "team_id",
            "team_domain",
            "user_id",
            "user_domain",
            name=op.f("pk_developer_team_member_highwaters"),
        ),
    )
    op.create_index(
        "ix_developer_team_member_highwaters_user",
        "developer_team_member_highwaters",
        ["user_id", "user_domain"],
    )


def downgrade() -> None:
    # Removing a foreign-install lease re-enables an otherwise expired mirror,
    # while removing a fingerprint/high-water permits stale snapshot replay.
    # Both are security state, not disposable cache rows.
    op.execute(PROJECTION_DOWNGRADE_PREFLIGHT_SQL)
    op.drop_index(
        "ix_developer_team_member_highwaters_user",
        table_name="developer_team_member_highwaters",
    )
    op.drop_table("developer_team_member_highwaters")
    op.drop_constraint(
        op.f("ck_developer_teams_developer_team_federation_fingerprint_lengths"),
        "developer_teams",
        type_="check",
    )
    op.drop_column("developer_teams", "federation_applications_fingerprint")
    op.drop_column("developer_teams", "federation_metadata_fingerprint")
    op.drop_index(
        "ix_bot_user_installations_authority_expiry",
        table_name="bot_user_installations",
    )
    op.drop_column("bot_user_installations", "authority_expires_at")
