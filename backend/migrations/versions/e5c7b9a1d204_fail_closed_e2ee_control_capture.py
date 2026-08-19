"""Fail closed until signed E2EE control metadata is reconciled.

Revision ID: e5c7b9a1d204
Revises: a1c6e8f2d940
Create Date: 2026-08-18
"""

# ruff: noqa: S608 -- the interpolated mode is checked against a closed literal set.

from collections.abc import Sequence

from alembic import op

revision: str = "e5c7b9a1d204"
down_revision: str | None = "a1c6e8f2d940"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_control_capture_function(*, welcome_mode: str, commit_mode: str) -> None:
    if welcome_mode not in {"audit", "join"} or commit_mode not in {"audit", "process"}:
        raise ValueError("invalid control capture mode")
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION kaede_capture_e2ee_control_record() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.e2ee IS NOT NULL
             AND NEW.e2ee->>'operation' IN ('welcome', 'commit')
             AND NEW.encryption_epoch IS NOT NULL THEN
            INSERT INTO e2ee_control_records (
              id, origin_domain, channel_id, channel_domain,
              author_id, author_domain, policy_generation, epoch,
              operation, apply_mode, envelope, created_at
            ) VALUES (
              NEW.id, NEW.origin_domain, NEW.channel_id, NEW.channel_domain,
              NEW.author_id, NEW.author_domain, NEW.encryption_policy_generation,
              NEW.encryption_epoch, NEW.e2ee->>'operation',
              CASE WHEN NEW.e2ee->>'operation' = 'welcome'
                THEN '{welcome_mode}' ELSE '{commit_mode}' END,
              NEW.e2ee, NEW.created_at
            ) ON CONFLICT (id, origin_domain) DO NOTHING;
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )


def upgrade() -> None:
    # Rows captured before signed operation metadata was reconciled must never
    # tell a client to apply a control. Complete rows retain the explicit mode
    # selected by the signed event metadata.
    op.drop_constraint(
        op.f("ck_e2ee_control_records_apply_mode_value"),
        "e2ee_control_records",
        type_="check",
    )
    op.create_check_constraint(
        "apply_mode_value",
        "e2ee_control_records",
        "(operation = 'welcome' AND apply_mode IN ('join','audit')) OR "
        "(operation = 'commit' AND apply_mode IN ('process','audit'))",
    )
    op.execute(
        "UPDATE e2ee_control_records SET apply_mode = 'audit' WHERE room_operation_id IS NULL"
    )
    _create_control_capture_function(welcome_mode="audit", commit_mode="audit")


def downgrade() -> None:
    op.execute(
        """
        UPDATE e2ee_control_records
        SET apply_mode = CASE WHEN operation = 'welcome' THEN 'join' ELSE 'process' END
        WHERE room_operation_id IS NULL
        """
    )
    op.drop_constraint(
        op.f("ck_e2ee_control_records_apply_mode_value"),
        "e2ee_control_records",
        type_="check",
    )
    op.create_check_constraint(
        "apply_mode_value",
        "e2ee_control_records",
        "(operation = 'welcome' AND apply_mode = 'join') OR "
        "(operation = 'commit' AND apply_mode IN ('process','audit'))",
    )
    _create_control_capture_function(welcome_mode="join", commit_mode="process")
