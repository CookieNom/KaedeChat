"""Add application directory discovery metadata.

Revision ID: e84f1a2c7d30
Revises: d73c8a1f4b20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e84f1a2c7d30"
down_revision: str | None = "d73c8a1f4b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIRECTORY_DOWNGRADE_PREFLIGHT_SQL = """
DO $$
DECLARE
    populated_application text;
BEGIN
    SELECT application.id::text || '@' || application.origin_domain
    INTO populated_application
    FROM bot_applications AS application
    WHERE application.directory_enabled
       OR application.directory_approved
       OR application.directory_summary IS NOT NULL
       OR application.directory_category IS NOT NULL
       OR application.directory_tags <> '[]'::jsonb
       OR application.directory_collections <> '[]'::jsonb
       OR application.directory_media <> '[]'::jsonb
       OR application.directory_external_links <> '[]'::jsonb
       OR application.directory_supported_locales <> '[]'::jsonb
       OR application.directory_description_localizations <> '{}'::jsonb
       OR application.banner_hash IS NOT NULL
       OR application.terms_url IS NOT NULL
    ORDER BY application.origin_domain, application.id
    LIMIT 1;

    IF populated_application IS NOT NULL THEN
        RAISE EXCEPTION
            'application directory downgrade blocked by populated application %',
            populated_application
            USING ERRCODE = '23514',
                  HINT = 'export or deliberately clear directory metadata before retrying';
    END IF;
END
$$;
"""


def upgrade() -> None:
    op.add_column(
        "bot_applications",
        sa.Column("directory_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "bot_applications",
        sa.Column("directory_approved", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("bot_applications", sa.Column("directory_summary", sa.String(200)))
    op.add_column("bot_applications", sa.Column("directory_category", sa.String(32)))
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_collections",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_media",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_external_links",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_supported_locales",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
    )
    op.add_column(
        "bot_applications",
        sa.Column(
            "directory_description_localizations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column("bot_applications", sa.Column("banner_hash", sa.String(128)))
    op.add_column("bot_applications", sa.Column("terms_url", sa.String(2048)))
    op.execute(
        """
        UPDATE bot_applications AS application
        SET directory_media = COALESCE(
            (
                SELECT jsonb_agg(
                    jsonb_build_object('type', 'image', 'asset_id', selected.id::text)
                    ORDER BY selected.name, selected.id
                )
                FROM (
                    SELECT asset.id, asset.name
                    FROM application_assets AS asset
                    WHERE asset.application_id = application.id
                      AND asset.application_domain = application.origin_domain
                      AND asset.kind = 'store'
                    ORDER BY asset.name, asset.id
                    LIMIT 5
                ) AS selected
            ),
            '[]'::jsonb
        )
        """
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_category_value",
        "bot_applications",
        "directory_category IS NULL OR directory_category IN "
        "('entertainment','games','moderation','productivity','social','utilities')",
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_tags_bounded",
        "bot_applications",
        "jsonb_typeof(directory_tags) = 'array' AND jsonb_array_length(directory_tags) <= 5",
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_collections_bounded",
        "bot_applications",
        "jsonb_typeof(directory_collections) = 'array' "
        "AND jsonb_array_length(directory_collections) <= 3 "
        "AND directory_collections <@ "
        '\'["featured","staff-picks","new-and-noteworthy"]\'::jsonb',
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_media_bounded",
        "bot_applications",
        "jsonb_typeof(directory_media) = 'array' AND jsonb_array_length(directory_media) <= 5",
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_external_links_bounded",
        "bot_applications",
        "jsonb_typeof(directory_external_links) = 'array' "
        "AND jsonb_array_length(directory_external_links) <= 5",
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_supported_locales_bounded",
        "bot_applications",
        "jsonb_typeof(directory_supported_locales) = 'array' "
        "AND jsonb_array_length(directory_supported_locales) <= 32",
    )
    op.create_check_constraint(
        "ck_bot_applications_bot_application_directory_localizations_bounded",
        "bot_applications",
        "jsonb_typeof(directory_description_localizations) = 'object' "
        "AND jsonb_array_length(jsonb_path_query_array("
        "directory_description_localizations, '$.keyvalue()')) <= 32",
    )
    op.create_index(
        "ix_bot_applications_directory",
        "bot_applications",
        ["directory_category", "id"],
        postgresql_where=sa.text("directory_enabled AND directory_approved AND status = 'active'"),
    )


def downgrade() -> None:
    # These columns contain operator-authored listing content and product
    # links. Silently dropping them would make a temporary rollback lossy and
    # a later re-upgrade cannot reconstruct their ordering or localization.
    op.execute(DIRECTORY_DOWNGRADE_PREFLIGHT_SQL)
    op.drop_index("ix_bot_applications_directory", table_name="bot_applications")
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_localizations_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_supported_locales_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_external_links_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_media_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_collections_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_tags_bounded",
        "bot_applications",
        type_="check",
    )
    op.drop_constraint(
        "ck_bot_applications_bot_application_directory_category_value",
        "bot_applications",
        type_="check",
    )
    for column in (
        "banner_hash",
        "terms_url",
        "directory_description_localizations",
        "directory_supported_locales",
        "directory_external_links",
        "directory_media",
        "directory_collections",
        "directory_tags",
        "directory_category",
        "directory_summary",
        "directory_approved",
        "directory_enabled",
    ):
        op.drop_column("bot_applications", column)
