"""Permit one-time authoritative resolution of opaque remote profiles.

Revision ID: c31f6a8e2d94
Revises: b72c9e4a1f63
Create Date: 2026-08-12
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c31f6a8e2d94"
down_revision: str | None = "b72c9e4a1f63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_users_immutable_handle ON users")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kaede_reject_handle_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.origin_domain IS DISTINCT FROM OLD.origin_domain
             OR NEW.is_local IS DISTINCT FROM OLD.is_local THEN
            RAISE EXCEPTION 'user identities are immutable' USING ERRCODE = '23514';
          END IF;

          IF NEW.username IS DISTINCT FROM OLD.username
             OR NEW.profile_resolved IS DISTINCT FROM OLD.profile_resolved THEN
            IF NOT (
              NOT OLD.is_local
              AND NOT OLD.profile_resolved
              AND NEW.profile_resolved
              AND left(OLD.username, 8) = 'history_'
              AND NEW.username IS DISTINCT FROM OLD.username
            ) THEN
              RAISE EXCEPTION 'user handles are immutable' USING ERRCODE = '23514';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_users_immutable_handle
        BEFORE UPDATE OF id, origin_domain, is_local, username, profile_resolved ON users
        FOR EACH ROW EXECUTE FUNCTION kaede_reject_handle_change()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_users_immutable_handle ON users")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION kaede_reject_handle_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.username IS DISTINCT FROM OLD.username
             OR NEW.origin_domain IS DISTINCT FROM OLD.origin_domain THEN
            RAISE EXCEPTION 'user handles are immutable' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_users_immutable_handle
        BEFORE UPDATE OF username, origin_domain ON users
        FOR EACH ROW EXECUTE FUNCTION kaede_reject_handle_change()
        """
    )
