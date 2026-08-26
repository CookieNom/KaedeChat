"""account bounded remote tracker replicas

Revision ID: fb7c3e9a1d42
Revises: f92a6c1d4b70
Create Date: 2026-08-26 00:00:00.000000
"""

# ruff: noqa: E501, S608 -- migration interpolations are closed static SQL fragments.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "fb7c3e9a1d42"
down_revision: str | None = "f92a6c1d4b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TRACKER_TABLES: tuple[tuple[str, int, int], ...] = (
    ("tracker_boards", 320, 2048),
    ("tracker_lanes", 320, 1024),
    # Descriptions are charged at their actual UTF-8 database footprint; the
    # fixed minimum covers tuple/index/user-reference overhead.
    ("tracker_tasks", 384, 2048),
)


def _replace_adjust_function(*, include_tracker: bool) -> None:
    tracker_rows = (
        ",\n                tracker_rows = greatest(0, tracker_rows + "
        "CASE WHEN p_category = 'tracker' THEN p_row_delta ELSE 0 END)"
        if include_tracker
        else ""
    )
    tracker_bytes = (
        ",\n                tracker_bytes = greatest(0, tracker_bytes + "
        "CASE WHEN p_category = 'tracker' THEN p_byte_delta ELSE 0 END)"
        if include_tracker
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION kaede_adjust_replica_usage(
            p_guild_id bigint,
            p_guild_domain text,
            p_category text,
            p_row_delta bigint,
            p_byte_delta bigint
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF p_guild_id IS NULL OR p_guild_domain IS NULL THEN
                RETURN;
            END IF;
            INSERT INTO federation_replica_usage (guild_id, guild_domain)
            VALUES (p_guild_id, p_guild_domain)
            ON CONFLICT (guild_id, guild_domain) DO NOTHING;
            UPDATE federation_replica_usage
            SET message_rows = greatest(0, message_rows + CASE WHEN p_category = 'message' THEN p_row_delta ELSE 0 END),
                message_bytes = greatest(0, message_bytes + CASE WHEN p_category = 'message' THEN p_byte_delta ELSE 0 END),
                reaction_rows = greatest(0, reaction_rows + CASE WHEN p_category = 'reaction' THEN p_row_delta ELSE 0 END),
                reaction_bytes = greatest(0, reaction_bytes + CASE WHEN p_category = 'reaction' THEN p_byte_delta ELSE 0 END),
                member_rows = greatest(0, member_rows + CASE WHEN p_category = 'member' THEN p_row_delta ELSE 0 END),
                member_bytes = greatest(0, member_bytes + CASE WHEN p_category = 'member' THEN p_byte_delta ELSE 0 END),
                attachment_rows = greatest(0, attachment_rows + CASE WHEN p_category = 'attachment' THEN p_row_delta ELSE 0 END),
                attachment_bytes = greatest(0, attachment_bytes + CASE WHEN p_category = 'attachment' THEN p_byte_delta ELSE 0 END),
                projection_rows = greatest(0, projection_rows + CASE WHEN p_category = 'projection' THEN p_row_delta ELSE 0 END),
                projection_bytes = greatest(0, projection_bytes + CASE WHEN p_category = 'projection' THEN p_byte_delta ELSE 0 END),
                structural_rows = greatest(0, structural_rows + CASE WHEN p_category = 'structural' THEN p_row_delta ELSE 0 END),
                structural_bytes = greatest(0, structural_bytes + CASE WHEN p_category = 'structural' THEN p_byte_delta ELSE 0 END){tracker_rows}{tracker_bytes},
                updated_at = now()
            WHERE guild_id = p_guild_id AND guild_domain = p_guild_domain;
        END;
        $$
        """
    )


def _create_total_columns(*, include_tracker: bool) -> None:
    row_tail = " + tracker_rows" if include_tracker else ""
    byte_tail = " + tracker_bytes" if include_tracker else ""
    op.add_column(
        "federation_replica_usage",
        sa.Column(
            "total_rows",
            sa.BigInteger(),
            sa.Computed(
                "message_rows + reaction_rows + member_rows + attachment_rows + "
                f"projection_rows + structural_rows{row_tail}",
                persisted=True,
            ),
        ),
    )
    op.add_column(
        "federation_replica_usage",
        sa.Column(
            "total_bytes",
            sa.BigInteger(),
            sa.Computed(
                "message_bytes + reaction_bytes + member_bytes + attachment_bytes + "
                f"projection_bytes + structural_bytes{byte_tail}",
                persisted=True,
            ),
        ),
    )


def upgrade() -> None:
    op.add_column(
        "federation_replica_usage",
        sa.Column("tracker_rows", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "federation_replica_usage",
        sa.Column("tracker_bytes", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.drop_column("federation_replica_usage", "total_rows")
    op.drop_column("federation_replica_usage", "total_bytes")
    _create_total_columns(include_tracker=True)
    op.drop_constraint(
        op.f("ck_federation_replica_usage_nonnegative_rows"),
        "federation_replica_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_federation_replica_usage_nonnegative_bytes"),
        "federation_replica_usage",
        type_="check",
    )
    op.create_check_constraint(
        op.f("ck_federation_replica_usage_nonnegative_rows"),
        "federation_replica_usage",
        "message_rows >= 0 AND reaction_rows >= 0 AND member_rows >= 0 "
        "AND attachment_rows >= 0 AND projection_rows >= 0 AND structural_rows >= 0 "
        "AND tracker_rows >= 0",
    )
    op.create_check_constraint(
        op.f("ck_federation_replica_usage_nonnegative_bytes"),
        "federation_replica_usage",
        "message_bytes >= 0 AND reaction_bytes >= 0 AND member_bytes >= 0 "
        "AND attachment_bytes >= 0 AND projection_bytes >= 0 "
        "AND structural_bytes >= 0 AND tracker_bytes >= 0",
    )
    _replace_adjust_function(include_tracker=True)
    for table, overhead, minimum_charge in TRACKER_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_replica_usage
            BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION kaede_track_replica_row(
                'tracker', 'direct', '{overhead}', '{minimum_charge}'
            )
            """
        )
    op.execute(
        """
        CREATE FUNCTION kaede_reconcile_tracker_replica_usage(
            p_guild_id bigint,
            p_guild_domain text
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_rows bigint;
            v_bytes bigint;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM guilds AS guild
                JOIN instances AS peer ON peer.domain = guild.origin_domain
                WHERE guild.id = p_guild_id
                  AND guild.origin_domain = p_guild_domain
                  AND NOT peer.is_self
            ) THEN
                RETURN;
            END IF;
            WITH accounted(bytes) AS (
                SELECT greatest(pg_column_size(to_jsonb(board))::bigint + 320, 2048)
                FROM tracker_boards AS board
                WHERE board.guild_id = p_guild_id AND board.guild_domain = p_guild_domain
                UNION ALL
                SELECT greatest(pg_column_size(to_jsonb(lane))::bigint + 320, 1024)
                FROM tracker_lanes AS lane
                WHERE lane.guild_id = p_guild_id AND lane.guild_domain = p_guild_domain
                UNION ALL
                SELECT greatest(pg_column_size(to_jsonb(task))::bigint + 384, 2048)
                FROM tracker_tasks AS task
                WHERE task.guild_id = p_guild_id AND task.guild_domain = p_guild_domain
            )
            SELECT count(*), coalesce(sum(bytes), 0)
            INTO v_rows, v_bytes
            FROM accounted;
            INSERT INTO federation_replica_usage (
                guild_id, guild_domain, tracker_rows, tracker_bytes
            ) VALUES (
                p_guild_id, p_guild_domain, v_rows, v_bytes
            )
            ON CONFLICT (guild_id, guild_domain) DO UPDATE SET
                tracker_rows = excluded.tracker_rows,
                tracker_bytes = excluded.tracker_bytes,
                updated_at = now();
        END;
        $$
        """
    )
    op.execute(
        """
        SELECT kaede_reconcile_tracker_replica_usage(guild.id, guild.origin_domain)
        FROM guilds AS guild
        JOIN instances AS peer ON peer.domain = guild.origin_domain
        WHERE NOT peer.is_self
        """
    )


def downgrade() -> None:
    for table, _overhead, _minimum_charge in reversed(TRACKER_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_replica_usage ON {table}")
    op.execute("DROP FUNCTION IF EXISTS kaede_reconcile_tracker_replica_usage(bigint, text)")
    _replace_adjust_function(include_tracker=False)
    op.drop_column("federation_replica_usage", "total_rows")
    op.drop_column("federation_replica_usage", "total_bytes")
    op.drop_constraint(
        op.f("ck_federation_replica_usage_nonnegative_rows"),
        "federation_replica_usage",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_federation_replica_usage_nonnegative_bytes"),
        "federation_replica_usage",
        type_="check",
    )
    op.drop_column("federation_replica_usage", "tracker_bytes")
    op.drop_column("federation_replica_usage", "tracker_rows")
    _create_total_columns(include_tracker=False)
    op.create_check_constraint(
        op.f("ck_federation_replica_usage_nonnegative_rows"),
        "federation_replica_usage",
        "message_rows >= 0 AND reaction_rows >= 0 AND member_rows >= 0 "
        "AND attachment_rows >= 0 AND projection_rows >= 0 AND structural_rows >= 0",
    )
    op.create_check_constraint(
        op.f("ck_federation_replica_usage_nonnegative_bytes"),
        "federation_replica_usage",
        "message_bytes >= 0 AND reaction_bytes >= 0 AND member_bytes >= 0 "
        "AND attachment_bytes >= 0 AND projection_bytes >= 0 "
        "AND structural_bytes >= 0",
    )
