"""Qualify federated announcement follows by their minting authority.

Revision ID: 0a6d2f9c4b81
Revises: f95b2c3d8e41
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0a6d2f9c4b81"
down_revision: str | None = "f95b2c3d8e41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOLLOW_CROSSPOST_FK = (
    "fk_federated_message_crossposts_follow_id_local_role_federated_channel_follows"
)
FOLLOW_CROSSPOST_AUTHORITY_FK = "fk_federated_message_crossposts_follow_authority_ref"


def upgrade() -> None:
    op.add_column(
        "federated_message_crossposts",
        sa.Column("follow_authority_domain", sa.String(length=253)),
    )
    # The old (follow_id, local_role) primary key makes this join unambiguous.
    # The target authority minted the ID, and that lineage is already protected
    # by target_authority_domain = target_channel_domain.
    op.execute(
        "UPDATE federated_message_crossposts AS crosspost "
        "SET follow_authority_domain = follow.target_authority_domain "
        "FROM federated_channel_follows AS follow "
        "WHERE follow.id = crosspost.follow_id "
        "AND follow.local_role = crosspost.local_role"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM federated_message_crossposts
                WHERE follow_authority_domain IS NULL
            ) THEN
                RAISE EXCEPTION
                    'federated follow authority backfill found an orphan crosspost'
                    USING ERRCODE = '23503',
                          HINT = 'restore the referenced follow before retrying';
            END IF;
        END
        $$;
        """
    )
    op.alter_column(
        "federated_message_crossposts",
        "follow_authority_domain",
        existing_type=sa.String(length=253),
        nullable=False,
    )

    op.drop_constraint(
        op.f(FOLLOW_CROSSPOST_FK),
        "federated_message_crossposts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("pk_federated_message_crossposts"),
        "federated_message_crossposts",
        type_="primary",
    )
    op.drop_constraint(
        op.f("pk_federated_channel_follows"),
        "federated_channel_follows",
        type_="primary",
    )
    op.create_primary_key(
        op.f("pk_federated_channel_follows"),
        "federated_channel_follows",
        ["id", "target_authority_domain", "local_role"],
    )
    op.create_primary_key(
        op.f("pk_federated_message_crossposts"),
        "federated_message_crossposts",
        [
            "source_message_id",
            "source_message_domain",
            "follow_id",
            "follow_authority_domain",
            "local_role",
        ],
    )
    op.create_foreign_key(
        FOLLOW_CROSSPOST_AUTHORITY_FK,
        "federated_message_crossposts",
        "federated_channel_follows",
        ["follow_id", "follow_authority_domain", "local_role"],
        ["id", "target_authority_domain", "local_role"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_federated_message_crossposts_follow",
        "federated_message_crossposts",
        ["follow_id", "follow_authority_domain", "local_role"],
    )


def downgrade() -> None:
    # Removing the authority would merge legal identities from independent
    # target instances. Refuse that lossy downgrade rather than deleting or
    # silently rebinding either follow and its delivery ledger.
    op.execute(
        """
        DO $$
        DECLARE
            collision text;
        BEGIN
            SELECT min(id::text || ':' || local_role)
            INTO collision
            FROM (
                SELECT id, local_role
                FROM federated_channel_follows
                GROUP BY id, local_role
                HAVING count(*) > 1
            ) AS duplicate;
            IF collision IS NOT NULL THEN
                RAISE EXCEPTION
                    'federated follow authority downgrade blocked by collision %', collision
                    USING ERRCODE = '23505',
                          HINT = 'retain this revision while qualified follow identities exist';
            END IF;

            SELECT min(
                source_message_id::text || '@' || source_message_domain || ':' ||
                follow_id::text || ':' || local_role
            )
            INTO collision
            FROM (
                SELECT source_message_id, source_message_domain, follow_id, local_role
                FROM federated_message_crossposts
                GROUP BY source_message_id, source_message_domain, follow_id, local_role
                HAVING count(*) > 1
            ) AS duplicate;
            IF collision IS NOT NULL THEN
                RAISE EXCEPTION
                    'federated crosspost authority downgrade blocked by collision %', collision
                    USING ERRCODE = '23505',
                          HINT = 'retain this revision while qualified crosspost identities exist';
            END IF;
        END
        $$;
        """
    )
    op.drop_index(
        "ix_federated_message_crossposts_follow",
        table_name="federated_message_crossposts",
    )
    op.drop_constraint(
        FOLLOW_CROSSPOST_AUTHORITY_FK,
        "federated_message_crossposts",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("pk_federated_message_crossposts"),
        "federated_message_crossposts",
        type_="primary",
    )
    op.drop_constraint(
        op.f("pk_federated_channel_follows"),
        "federated_channel_follows",
        type_="primary",
    )
    op.create_primary_key(
        op.f("pk_federated_channel_follows"),
        "federated_channel_follows",
        ["id", "local_role"],
    )
    op.create_primary_key(
        op.f("pk_federated_message_crossposts"),
        "federated_message_crossposts",
        ["source_message_id", "source_message_domain", "follow_id", "local_role"],
    )
    op.create_foreign_key(
        op.f(FOLLOW_CROSSPOST_FK),
        "federated_message_crossposts",
        "federated_channel_follows",
        ["follow_id", "local_role"],
        ["id", "local_role"],
        ondelete="CASCADE",
    )
    op.drop_column("federated_message_crossposts", "follow_authority_domain")
