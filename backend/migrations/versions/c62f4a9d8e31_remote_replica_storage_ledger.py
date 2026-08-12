"""bound durable remote guild replica storage

Revision ID: c62f4a9d8e31
Revises: a47d8c2e6f19
Create Date: 2026-08-11 03:00:00.000000
"""

# ruff: noqa: E501 -- keeping the migration SQL legible is safer than splitting literals.

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c62f4a9d8e31"
down_revision: str | None = "a47d8c2e6f19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TRACKED_TABLES: tuple[tuple[str, str, str, int, int], ...] = (
    ("messages", "message", "channel", 512, 4096),
    ("reactions", "reaction", "message", 320, 1024),
    # One remote membership normally materializes a remote User profile too.
    # Charge enough fixed space for that shared profile and its indexes; this
    # intentionally over-counts identities shared by several guilds rather
    # than leaving a peer-controlled companion table outside the byte bound.
    ("guild_members", "member", "direct", 4096, 4096),
    ("attachments", "attachment", "message", 384, 4096),
    ("message_projections", "projection", "channel", 256, 2048),
    ("roles", "structural", "direct", 320, 1024),
    ("member_roles", "structural", "direct", 320, 1024),
    ("channels", "structural", "direct", 320, 4096),
    ("channel_overwrites", "structural", "direct", 320, 1536),
    ("pins", "structural", "channel", 320, 1536),
    ("bans", "structural", "direct", 320, 4096),
    ("guild_instance_bans", "structural", "direct", 320, 4096),
    ("emojis", "structural", "direct", 320, 4096),
    ("guild_history_imports", "structural", "direct", 512, 4096),
    ("guild_history_import_channels", "structural", "channel", 320, 2048),
    ("guild_history_staged_messages", "structural", "channel", 320, 4096),
    ("federated_history_messages", "structural", "message", 320, 1024),
)


def upgrade() -> None:
    op.add_column(
        "guilds",
        sa.Column("snapshot_generation", sa.BigInteger(), server_default="1", nullable=False),
    )
    op.add_column("guilds", sa.Column("sync_error_code", sa.String(length=64)))
    op.add_column("guilds", sa.Column("sync_error", sa.String(length=500)))
    op.drop_constraint(op.f("ck_guilds_sync_status"), "guilds", type_="check")
    op.create_check_constraint(
        op.f("ck_guilds_sync_status"),
        "guilds",
        "sync_status IN ('ready','syncing','stale','failed','quota_paused')",
    )
    op.create_check_constraint(
        op.f("ck_guilds_positive_snapshot_generation"),
        "guilds",
        "snapshot_generation >= 1",
    )
    op.create_check_constraint(
        op.f("ck_guilds_sync_error_requires_failure"),
        "guilds",
        "sync_status IN ('failed','quota_paused') OR sync_error_code IS NULL",
    )

    op.create_table(
        "federation_replica_usage",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("guild_domain", sa.String(length=253), nullable=False),
        sa.Column("message_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("message_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reaction_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reaction_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("member_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("member_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attachment_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("attachment_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("projection_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("projection_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("structural_rows", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("structural_bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column(
            "total_rows",
            sa.BigInteger(),
            sa.Computed(
                "message_rows + reaction_rows + member_rows + attachment_rows + "
                "projection_rows + structural_rows",
                persisted=True,
            ),
        ),
        sa.Column(
            "total_bytes",
            sa.BigInteger(),
            sa.Computed(
                "message_bytes + reaction_bytes + member_bytes + attachment_bytes + "
                "projection_bytes + structural_bytes",
                persisted=True,
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "message_rows >= 0 AND reaction_rows >= 0 AND member_rows >= 0 "
            "AND attachment_rows >= 0 AND projection_rows >= 0 AND structural_rows >= 0",
            name=op.f("ck_federation_replica_usage_nonnegative_rows"),
        ),
        sa.CheckConstraint(
            "message_bytes >= 0 AND reaction_bytes >= 0 AND member_bytes >= 0 "
            "AND attachment_bytes >= 0 AND projection_bytes >= 0 "
            "AND structural_bytes >= 0",
            name=op.f("ck_federation_replica_usage_nonnegative_bytes"),
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            name=op.f("fk_federation_replica_usage_guild_id_guild_domain_guilds"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id", "guild_domain", name=op.f("pk_federation_replica_usage")
        ),
    )
    op.create_index(
        op.f("ix_federation_replica_usage_origin"),
        "federation_replica_usage",
        ["guild_domain"],
    )

    op.execute(
        """
        CREATE FUNCTION kaede_replica_charge_data(
            p_table text,
            p_row jsonb
        ) RETURNS jsonb
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        AS $$
            SELECT CASE p_table
                WHEN 'message_projections' THEN p_row - 'processed_at'
                WHEN 'channels' THEN
                    p_row - 'last_message_id' - 'last_message_domain' - 'updated_at'
                WHEN 'guilds' THEN
                    p_row - 'sync_status' - 'sync_error_code' - 'sync_error'
                          - 'unavailable' - 'last_event_seq' - 'next_event_seq' - 'updated_at'
                ELSE p_row
            END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_adjust_replica_usage(
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
                structural_bytes = greatest(0, structural_bytes + CASE WHEN p_category = 'structural' THEN p_byte_delta ELSE 0 END),
                updated_at = now()
            WHERE guild_id = p_guild_id AND guild_domain = p_guild_domain;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_replica_row_guild(
            p_mode text,
            p_row jsonb,
            OUT guild_id bigint,
            OUT guild_domain text
        ) RETURNS record
        LANGUAGE plpgsql
        STABLE
        AS $$
        BEGIN
            guild_id := NULL;
            guild_domain := NULL;
            IF p_mode = 'direct' THEN
                SELECT guild.id, guild.origin_domain
                INTO guild_id, guild_domain
                FROM guilds AS guild
                JOIN instances AS peer ON peer.domain = guild.origin_domain
                WHERE guild.id = NULLIF(p_row ->> 'guild_id', '')::bigint
                  AND guild.origin_domain = NULLIF(p_row ->> 'guild_domain', '')
                  AND NOT peer.is_self;
            ELSIF p_mode = 'channel' THEN
                SELECT channel.guild_id, channel.guild_domain
                INTO guild_id, guild_domain
                FROM channels AS channel
                JOIN guilds AS guild
                  ON guild.id = channel.guild_id
                 AND guild.origin_domain = channel.guild_domain
                JOIN instances AS peer ON peer.domain = guild.origin_domain
                WHERE channel.id = NULLIF(p_row ->> 'channel_id', '')::bigint
                  AND channel.origin_domain = NULLIF(p_row ->> 'channel_domain', '')
                  AND NOT peer.is_self;
            ELSIF p_mode = 'message' THEN
                SELECT channel.guild_id, channel.guild_domain
                INTO guild_id, guild_domain
                FROM messages AS message
                JOIN channels AS channel
                  ON channel.id = message.channel_id
                 AND channel.origin_domain = message.channel_domain
                JOIN guilds AS guild
                  ON guild.id = channel.guild_id
                 AND guild.origin_domain = channel.guild_domain
                JOIN instances AS peer ON peer.domain = guild.origin_domain
                WHERE message.id = NULLIF(p_row ->> 'message_id', '')::bigint
                  AND message.origin_domain = NULLIF(p_row ->> 'message_domain', '')
                  AND NOT peer.is_self;
            ELSE
                RAISE EXCEPTION 'unknown federation replica accounting mode: %', p_mode;
            END IF;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_track_replica_row() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_data jsonb;
            new_data jsonb;
            old_guild_id bigint;
            old_guild_domain text;
            new_guild_id bigint;
            new_guild_domain text;
            old_size bigint;
            new_size bigint;
            overhead bigint := TG_ARGV[2]::bigint;
            minimum_charge bigint := TG_ARGV[3]::bigint;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                old_data := to_jsonb(OLD);
                SELECT ref.guild_id, ref.guild_domain
                INTO old_guild_id, old_guild_domain
                FROM kaede_replica_row_guild(TG_ARGV[1], old_data) AS ref;
                old_size := greatest(
                    pg_column_size(
                        kaede_replica_charge_data(TG_TABLE_NAME, old_data)
                    )::bigint + overhead,
                    minimum_charge
                );
            END IF;
            IF TG_OP <> 'DELETE' THEN
                new_data := to_jsonb(NEW);
                SELECT ref.guild_id, ref.guild_domain
                INTO new_guild_id, new_guild_domain
                FROM kaede_replica_row_guild(TG_ARGV[1], new_data) AS ref;
                new_size := greatest(
                    pg_column_size(
                        kaede_replica_charge_data(TG_TABLE_NAME, new_data)
                    )::bigint + overhead,
                    minimum_charge
                );
            END IF;
            IF TG_OP = 'UPDATE'
               AND (old_guild_id, old_guild_domain) = (new_guild_id, new_guild_domain) THEN
                PERFORM kaede_adjust_replica_usage(
                    new_guild_id,
                    new_guild_domain,
                    TG_ARGV[0],
                    0,
                    new_size - old_size
                );
            ELSE
                IF TG_OP <> 'INSERT' THEN
                    PERFORM kaede_adjust_replica_usage(
                        old_guild_id,
                        old_guild_domain,
                        TG_ARGV[0],
                        -1,
                        -old_size
                    );
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    PERFORM kaede_adjust_replica_usage(
                        new_guild_id,
                        new_guild_domain,
                        TG_ARGV[0],
                        1,
                        new_size
                    );
                END IF;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION kaede_track_replica_guild() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_size bigint;
            new_size bigint;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF EXISTS (
                    SELECT 1 FROM instances AS peer
                    WHERE peer.domain = NEW.origin_domain AND NOT peer.is_self
                ) THEN
                    PERFORM kaede_adjust_replica_usage(
                        NEW.id, NEW.origin_domain, 'structural', 1,
                        greatest(
                            pg_column_size(
                                kaede_replica_charge_data('guilds', to_jsonb(NEW))
                            )::bigint + 320,
                            8192
                        )
                    );
                END IF;
            ELSIF TG_OP = 'UPDATE' THEN
                IF EXISTS (
                    SELECT 1 FROM instances AS peer
                    WHERE peer.domain = NEW.origin_domain AND NOT peer.is_self
                ) THEN
                    old_size := greatest(
                        pg_column_size(
                            kaede_replica_charge_data('guilds', to_jsonb(OLD))
                        )::bigint + 320,
                        8192
                    );
                    new_size := greatest(
                        pg_column_size(
                            kaede_replica_charge_data('guilds', to_jsonb(NEW))
                        )::bigint + 320,
                        8192
                    );
                    PERFORM kaede_adjust_replica_usage(
                        NEW.id, NEW.origin_domain, 'structural', 0, new_size - old_size
                    );
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_guilds_replica_usage
        AFTER INSERT OR UPDATE ON guilds
        FOR EACH ROW EXECUTE FUNCTION kaede_track_replica_guild()
        """
    )
    for table, category, mode, overhead, minimum_charge in TRACKED_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_replica_usage
            BEFORE INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION kaede_track_replica_row(
                '{category}', '{mode}', '{overhead}', '{minimum_charge}'
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION kaede_reconcile_replica_usage(
            p_guild_id bigint,
            p_guild_domain text
        ) RETURNS void
        LANGUAGE plpgsql
        AS $$
        DECLARE
            v_message_rows bigint;
            v_message_bytes bigint;
            v_reaction_rows bigint;
            v_reaction_bytes bigint;
            v_member_rows bigint;
            v_member_bytes bigint;
            v_attachment_rows bigint;
            v_attachment_bytes bigint;
            v_projection_rows bigint;
            v_projection_bytes bigint;
            v_structural_rows bigint;
            v_structural_bytes bigint;
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM guilds AS guild
                JOIN instances AS peer ON peer.domain = guild.origin_domain
                WHERE guild.id = p_guild_id
                  AND guild.origin_domain = p_guild_domain
                  AND NOT peer.is_self
            ) THEN
                DELETE FROM federation_replica_usage
                WHERE guild_id = p_guild_id AND guild_domain = p_guild_domain;
                RETURN;
            END IF;
            WITH accounted(category, bytes) AS (
                SELECT 'message', greatest(pg_column_size(to_jsonb(message))::bigint + 512, 4096)
                FROM messages AS message
                JOIN channels AS channel ON channel.id = message.channel_id AND channel.origin_domain = message.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'reaction', greatest(pg_column_size(to_jsonb(reaction))::bigint + 320, 1024)
                FROM reactions AS reaction
                JOIN messages AS message ON message.id = reaction.message_id AND message.origin_domain = reaction.message_domain
                JOIN channels AS channel ON channel.id = message.channel_id AND channel.origin_domain = message.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'member', greatest(pg_column_size(to_jsonb(member))::bigint + 4096, 4096)
                FROM guild_members AS member
                WHERE member.guild_id = p_guild_id AND member.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'attachment', greatest(pg_column_size(to_jsonb(attachment))::bigint + 384, 4096)
                FROM attachments AS attachment
                JOIN messages AS message ON message.id = attachment.message_id AND message.origin_domain = attachment.message_domain
                JOIN channels AS channel ON channel.id = message.channel_id AND channel.origin_domain = message.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'projection', greatest(pg_column_size(kaede_replica_charge_data('message_projections', to_jsonb(projection)))::bigint + 256, 2048)
                FROM message_projections AS projection
                JOIN channels AS channel ON channel.id = projection.channel_id AND channel.origin_domain = projection.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(kaede_replica_charge_data('guilds', to_jsonb(guild)))::bigint + 320, 8192)
                FROM guilds AS guild WHERE guild.id = p_guild_id AND guild.origin_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(kaede_replica_charge_data('channels', to_jsonb(channel)))::bigint + 320, 4096)
                FROM channels AS channel WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(role))::bigint + 320, 1024)
                FROM roles AS role WHERE role.guild_id = p_guild_id AND role.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(member_role))::bigint + 320, 1024)
                FROM member_roles AS member_role WHERE member_role.guild_id = p_guild_id AND member_role.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(overwrite))::bigint + 320, 1536)
                FROM channel_overwrites AS overwrite WHERE overwrite.guild_id = p_guild_id AND overwrite.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(pin))::bigint + 320, 1536)
                FROM pins AS pin
                JOIN channels AS channel ON channel.id = pin.channel_id AND channel.origin_domain = pin.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(ban))::bigint + 320, 4096)
                FROM bans AS ban WHERE ban.guild_id = p_guild_id AND ban.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(instance_ban))::bigint + 320, 4096)
                FROM guild_instance_bans AS instance_ban WHERE instance_ban.guild_id = p_guild_id AND instance_ban.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(emoji))::bigint + 320, 4096)
                FROM emojis AS emoji WHERE emoji.guild_id = p_guild_id AND emoji.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(history_import))::bigint + 512, 4096)
                FROM guild_history_imports AS history_import
                WHERE history_import.guild_id = p_guild_id AND history_import.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(import_channel))::bigint + 320, 2048)
                FROM guild_history_import_channels AS import_channel
                JOIN channels AS channel ON channel.id = import_channel.channel_id AND channel.origin_domain = import_channel.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(staged))::bigint + 320, 4096)
                FROM guild_history_staged_messages AS staged
                JOIN channels AS channel ON channel.id = staged.channel_id AND channel.origin_domain = staged.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
                UNION ALL
                SELECT 'structural', greatest(pg_column_size(to_jsonb(provenance))::bigint + 320, 1024)
                FROM federated_history_messages AS provenance
                JOIN messages AS message ON message.id = provenance.message_id AND message.origin_domain = provenance.message_domain
                JOIN channels AS channel ON channel.id = message.channel_id AND channel.origin_domain = message.channel_domain
                WHERE channel.guild_id = p_guild_id AND channel.guild_domain = p_guild_domain
            )
            SELECT count(*) FILTER (WHERE category = 'message'), coalesce(sum(bytes) FILTER (WHERE category = 'message'), 0),
                   count(*) FILTER (WHERE category = 'reaction'), coalesce(sum(bytes) FILTER (WHERE category = 'reaction'), 0),
                   count(*) FILTER (WHERE category = 'member'), coalesce(sum(bytes) FILTER (WHERE category = 'member'), 0),
                   count(*) FILTER (WHERE category = 'attachment'), coalesce(sum(bytes) FILTER (WHERE category = 'attachment'), 0),
                   count(*) FILTER (WHERE category = 'projection'), coalesce(sum(bytes) FILTER (WHERE category = 'projection'), 0),
                   count(*) FILTER (WHERE category = 'structural'), coalesce(sum(bytes) FILTER (WHERE category = 'structural'), 0)
            INTO v_message_rows, v_message_bytes, v_reaction_rows, v_reaction_bytes,
                 v_member_rows, v_member_bytes, v_attachment_rows, v_attachment_bytes,
                 v_projection_rows, v_projection_bytes, v_structural_rows, v_structural_bytes
            FROM accounted;
            INSERT INTO federation_replica_usage (
                guild_id, guild_domain, message_rows, message_bytes, reaction_rows,
                reaction_bytes, member_rows, member_bytes, attachment_rows,
                attachment_bytes, projection_rows, projection_bytes, structural_rows,
                structural_bytes
            ) VALUES (
                p_guild_id, p_guild_domain, v_message_rows, v_message_bytes,
                v_reaction_rows, v_reaction_bytes, v_member_rows, v_member_bytes,
                v_attachment_rows, v_attachment_bytes, v_projection_rows,
                v_projection_bytes, v_structural_rows, v_structural_bytes
            )
            ON CONFLICT (guild_id, guild_domain) DO UPDATE SET
                message_rows = excluded.message_rows,
                message_bytes = excluded.message_bytes,
                reaction_rows = excluded.reaction_rows,
                reaction_bytes = excluded.reaction_bytes,
                member_rows = excluded.member_rows,
                member_bytes = excluded.member_bytes,
                attachment_rows = excluded.attachment_rows,
                attachment_bytes = excluded.attachment_bytes,
                projection_rows = excluded.projection_rows,
                projection_bytes = excluded.projection_bytes,
                structural_rows = excluded.structural_rows,
                structural_bytes = excluded.structural_bytes,
                updated_at = now();
        END;
        $$
        """
    )
    op.execute(
        """
        SELECT kaede_reconcile_replica_usage(guild.id, guild.origin_domain)
        FROM guilds AS guild
        JOIN instances AS peer ON peer.domain = guild.origin_domain
        WHERE NOT peer.is_self
        """
    )


def downgrade() -> None:
    for table, _category, _mode, _overhead, _minimum in reversed(TRACKED_TABLES):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_replica_usage ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_guilds_replica_usage ON guilds")
    op.execute("DROP FUNCTION IF EXISTS kaede_reconcile_replica_usage(bigint, text)")
    op.execute("DROP FUNCTION IF EXISTS kaede_track_replica_guild()")
    op.execute("DROP FUNCTION IF EXISTS kaede_track_replica_row()")
    op.execute("DROP FUNCTION IF EXISTS kaede_replica_row_guild(text, jsonb)")
    op.execute(
        "DROP FUNCTION IF EXISTS kaede_adjust_replica_usage(bigint, text, text, bigint, bigint)"
    )
    op.execute("DROP FUNCTION IF EXISTS kaede_replica_charge_data(text, jsonb)")
    op.drop_index(
        op.f("ix_federation_replica_usage_origin"),
        table_name="federation_replica_usage",
    )
    op.drop_table("federation_replica_usage")
    op.drop_constraint(op.f("ck_guilds_sync_error_requires_failure"), "guilds", type_="check")
    op.drop_constraint(op.f("ck_guilds_positive_snapshot_generation"), "guilds", type_="check")
    op.drop_constraint(op.f("ck_guilds_sync_status"), "guilds", type_="check")
    op.create_check_constraint(
        op.f("ck_guilds_sync_status"),
        "guilds",
        "sync_status IN ('ready','syncing','stale','failed')",
    )
    op.drop_column("guilds", "sync_error")
    op.drop_column("guilds", "sync_error_code")
    op.drop_column("guilds", "snapshot_generation")
