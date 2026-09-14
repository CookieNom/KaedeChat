"""Channel-defined task fields."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c49a8f1d6e03"
down_revision = "b38f7e0c5d92"
branch_labels = None
depends_on = None


OLD_PURPOSES = "purpose IN ('attachment','avatar','banner','guild_icon','guild_banner','emoji','sticker','webhook_avatar','role_icon','soundboard','scheduled_event_image','application_asset','application_emoji','webhook_attachment')"


def upgrade():
    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint(
        "purpose_value", "attachments", OLD_PURPOSES[:-1] + ", 'tracker_attachment')"
    )
    op.add_column(
        "tracker_boards",
        sa.Column("custom_fields", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "tracker_tasks",
        sa.Column("custom_values", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade():
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM attachments WHERE purpose = 'tracker_attachment')
        OR EXISTS (SELECT 1 FROM tracker_boards WHERE custom_fields <> '[]'::jsonb)
        OR EXISTS (SELECT 1 FROM tracker_tasks WHERE custom_values <> '{}'::jsonb) THEN
            RAISE EXCEPTION 'Remove custom task fields before downgrading';
        END IF;
    END $$""")
    op.drop_column("tracker_tasks", "custom_values")
    op.drop_column("tracker_boards", "custom_fields")

    op.drop_constraint(op.f("ck_attachments_purpose_value"), "attachments", type_="check")
    op.create_check_constraint("purpose_value", "attachments", OLD_PURPOSES)
