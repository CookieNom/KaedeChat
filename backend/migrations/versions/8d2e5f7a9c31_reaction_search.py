"""Index message reactions for search.

Revision ID: 8d2e5f7a9c31
Revises: b83d2e7f910a
"""

from alembic import op

revision: str = "8d2e5f7a9c31"
down_revision: str | None = "b83d2e7f910a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The attachment trigger handles both old and new message references.
    op.execute(
        "CREATE TRIGGER trg_reactions_search AFTER INSERT OR UPDATE OR DELETE ON reactions "
        "FOR EACH ROW EXECUTE FUNCTION kaede_search_attachment_changed()"
    )
    op.execute(
        "UPDATE search_index_state SET backfill_after_id = NULL, "
        "backfill_after_domain = NULL, backfill_completed = false, updated_at = now() "
        "WHERE id = 1"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_reactions_search ON reactions")
