"""Link immutable DM segments across a unanimously approved privacy change."""

import sqlalchemy as sa
from alembic import op

revision = "b521afdc9073"
down_revision = "91bc0df6ae32"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channels", sa.Column("dm_previous_id", sa.BigInteger()))
    op.add_column("channels", sa.Column("dm_successor_id", sa.BigInteger()))
    # Serialize racing message admissions against closing the old segment.
    op.execute("""
        CREATE FUNCTION kaede_check_dm_segment_open() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (SELECT dm_successor_id FROM channels WHERE id = NEW.channel_id AND
              origin_domain = NEW.channel_domain AND type = 1 AND encryption_mode = 'e2ee'
              FOR NO KEY UPDATE) IS NOT NULL THEN
            RAISE EXCEPTION 'DM continued in another room' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END $$;
        """)
    op.execute("""CREATE TRIGGER trg_dm_segment_open BEFORE INSERT ON messages
        FOR EACH ROW EXECUTE FUNCTION kaede_check_dm_segment_open()""")
    # A member leaving the current group loses server access to its prior segments.
    op.execute("""
        CREATE FUNCTION kaede_prune_dm_segment_member() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          DELETE FROM dm_participants WHERE conversation_id = (
            SELECT dm_previous_id FROM channels WHERE id = OLD.conversation_id
              AND origin_domain = OLD.conversation_domain
          ) AND conversation_domain = OLD.conversation_domain
            AND user_id = OLD.user_id AND user_domain = OLD.user_domain;
          RETURN OLD;
        END $$;
        """)
    op.execute("""CREATE TRIGGER trg_dm_prune_segment_member AFTER DELETE ON dm_participants
        FOR EACH ROW EXECUTE FUNCTION kaede_prune_dm_segment_member()""")

    for column in ("dm_previous_id", "dm_successor_id"):
        op.create_foreign_key(
            f"fk_channels_{column}",
            "channels",
            "channels",
            [column, "origin_domain"],
            ["id", "origin_domain"],
            deferrable=True,
            initially="DEFERRED",
        )
        op.create_index(
            f"uq_channels_{column}",
            "channels",
            [column, "origin_domain"],
            unique=True,
            postgresql_where=sa.text(f"{column} IS NOT NULL"),
        )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM channels WHERE dm_successor_id IS NOT NULL)")
    ):
        raise RuntimeError("Restore a pre-transition backup before removing DM segment history.")
    op.execute("DROP TRIGGER trg_dm_prune_segment_member ON dm_participants")
    op.execute("DROP FUNCTION kaede_prune_dm_segment_member()")
    op.execute("DROP TRIGGER trg_dm_segment_open ON messages")
    op.execute("DROP FUNCTION kaede_check_dm_segment_open()")
    op.drop_column("channels", "dm_previous_id")
    op.drop_column("channels", "dm_successor_id")
