"""Mark installation-owned roles as managed.

Revision ID: 9e7a1c4b8d20
Revises: 6b1f4d8a2c90
"""

from alembic import op
import sqlalchemy as sa

revision = "9e7a1c4b8d20"
down_revision = "6b1f4d8a2c90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("roles", sa.Column("managed", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.execute("""
        UPDATE roles AS role SET managed = true
        FROM bot_installations AS installation
        WHERE role.id = installation.role_id
          AND role.origin_domain = installation.role_domain
          AND installation.revoked_at IS NULL
          AND installation.status != 'revoked'
    """)


def downgrade() -> None:
    op.drop_column("roles", "managed")
