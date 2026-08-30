"""Bind bot installations to their application identity.

Revision ID: d73c8a1f4b20
Revises: fc9a4b7d2e10
Create Date: 2026-08-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d73c8a1f4b20"
down_revision: str | None = "fc9a4b7d2e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "fk_bot_installations_application_ref_bot_applications"
ORPHAN_PREFLIGHT_SQL = """
DO $$
DECLARE
    orphan_count bigint;
    orphan_sample text;
BEGIN
    SELECT count(*), min(
        installation.id::text || ':' || installation.application_id::text || '@' ||
        installation.application_domain
    )
    INTO orphan_count, orphan_sample
    FROM bot_installations AS installation
    LEFT JOIN bot_applications AS application
      ON application.id = installation.application_id
     AND application.origin_domain = installation.application_domain
    WHERE application.id IS NULL;

    IF orphan_count > 0 THEN
        RAISE EXCEPTION
            'bot installation application FK blocked by % orphan(s); sample install:app is %',
            orphan_count, orphan_sample
            USING ERRCODE = '23503',
                  HINT = 'restore the application or remove the orphan before retrying';
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.execute(ORPHAN_PREFLIGHT_SQL)
    op.create_foreign_key(
        CONSTRAINT_NAME,
        "bot_installations",
        "bot_applications",
        ["application_id", "application_domain"],
        ["id", "origin_domain"],
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT_NAME, "bot_installations", type_="foreignkey")
