"""Preserve restricted child-safety cases independently of accounts and messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "ac4e7f9b2d60"
down_revision = "9a3c6d8e2f41"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_attachments_object_key", "attachments", ["object_key"])
    op.create_index(
        "ix_attachments_variants_object_keys",
        "attachments",
        ["variants"],
        postgresql_using="gin",
        postgresql_ops={"variants": "jsonb_path_ops"},
    )
    op.create_table(
        "child_safety_cases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("subject_ref", sa.String(320)),
        sa.Column("message_ref", sa.String(320)),
        sa.Column("attachment_ref", sa.String(320)),
        sa.Column("subject_snapshot", sa.LargeBinary()),
        sa.Column("details", sa.LargeBinary(), nullable=False),
        sa.Column("message_snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("reviewed_by", sa.String(320)),
        sa.Column("review_reason", sa.String(2000)),
        sa.Column("submissions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("holds", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("retention_extensions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("removal_objects", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("removal_checked_at", sa.DateTime(timezone=True)),
        sa.Column("removal_completed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_submission_at", sa.DateTime(timezone=True)),
        sa.Column("retain_until", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("disposed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("source IN ('user','photodna')", name="source_value"),
        sa.CheckConstraint("status IN ('pending','confirmed','dismissed')", name="status_value"),
        sa.CheckConstraint(
            "completed_submission_at IS NULL OR (retain_until IS NOT NULL AND "
            "retain_until >= completed_submission_at + interval '1 year')",
            name="submission_minimum_retention",
        ),
    )
    for name, columns in (
        ("queue", ["status", "created_at"]),
        ("subject", ["subject_ref"]),
        ("message", ["message_ref"]),
        ("attachment", ["attachment_ref"]),
    ):
        op.create_index(f"ix_child_safety_cases_{name}", "child_safety_cases", columns)
    op.create_table(
        "child_safety_reports",
        sa.Column("report_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "case_id",
            sa.BigInteger(),
            sa.ForeignKey("child_safety_cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("snapshot", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_child_safety_reports_case", "child_safety_reports", ["case_id"])
    op.create_table(
        "child_safety_evidence",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "case_id",
            sa.BigInteger(),
            sa.ForeignKey("child_safety_cases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("attachment_ref", sa.String(320)),
        sa.Column("provenance", sa.String(32), nullable=False),
        sa.Column("object_key", sa.String(512), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True)),
        sa.Column("pending_blob", sa.LargeBinary()),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("size >= 0", name="nonnegative_size"),
        sa.CheckConstraint(
            "provenance IN ('local_upload','federated_origin','reporter_disclosed')",
            name="provenance_value",
        ),
        sa.CheckConstraint("object_key LIKE 'evidence/%'", name="restricted_prefix"),
        sa.CheckConstraint(
            "pending_blob IS NULL OR (stored_at IS NOT NULL AND deleted_at IS NULL)",
            name="buffer_is_preserved",
        ),
    )
    op.create_index("ix_child_safety_evidence_case", "child_safety_evidence", ["case_id"])
    op.create_index(
        "ix_child_safety_evidence_attachment", "child_safety_evidence", ["attachment_ref"]
    )
    op.create_table(
        "safety_client_events",
        sa.Column("sequence", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_domain", sa.String(253), nullable=False),
        sa.Column("message_refs", postgresql.JSONB(), nullable=False),
        sa.Column("attachment_refs", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.CheckConstraint(
            "state IN ('pending_review','removed','hidden','released')", name="state_value"
        ),
    )
    op.create_index(
        "ix_safety_client_events_recipient",
        "safety_client_events",
        ["user_id", "user_domain", "sequence"],
    )
    op.add_column("messages", sa.Column("safety_state", sa.String(24)))
    op.add_column("messages", sa.Column("safety_reason", sa.String(500)))
    op.add_column("messages", sa.Column("safety_updated_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "safety_state_value",
        "messages",
        "safety_state IS NULL OR safety_state IN ('pending_review','removed','hidden')",
    )
    op.drop_constraint(
        op.f("ck_abuse_reports_abuse_report_category_value"), "abuse_reports", type_="check"
    )
    op.create_check_constraint(
        "abuse_report_category_value",
        "abuse_reports",
        "category IN ('spam','harassment','hate','sexual_content','violence','self_harm',"
        "'impersonation','privacy','malware','illegal_content','csam','other')",
    )
    op.execute("""
        CREATE FUNCTION protect_child_safety_message() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.safety_state IN ('pending_review', 'removed') THEN
                    RAISE EXCEPTION 'SAFETY_MESSAGE_RETAINED' USING ERRCODE = '23514';
                END IF;
                RETURN OLD;
            END IF;
            IF NEW.safety_state IN ('pending_review', 'removed') AND NEW.deleted_at IS NOT NULL THEN
                RAISE EXCEPTION 'SAFETY_MESSAGE_RETAINED' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF OLD.safety_state = 'removed' AND NEW.safety_state IS DISTINCT FROM 'removed' THEN
                    RAISE EXCEPTION 'SAFETY_MESSAGE_RETAINED' USING ERRCODE = '23514';
                END IF;
                IF OLD.safety_state IN ('pending_review', 'removed')
                    AND NEW.safety_state IS NOT DISTINCT FROM OLD.safety_state
                    AND (NEW.content IS DISTINCT FROM OLD.content
                        OR NEW.e2ee IS DISTINCT FROM OLD.e2ee)
                THEN
                    RAISE EXCEPTION 'SAFETY_MESSAGE_RETAINED' USING ERRCODE = '23514';
                END IF;
            END IF;
            RETURN NEW;
        END
        $$;
    """)
    op.execute("""
        CREATE TRIGGER protect_child_safety_message
        BEFORE INSERT OR DELETE OR UPDATE ON messages
        FOR EACH ROW EXECUTE FUNCTION protect_child_safety_message();
    """)


def downgrade() -> None:
    # A downgrade cannot silently discard preserved material or its obligations.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM child_safety_cases WHERE disposed_at IS NULL) THEN
                RAISE EXCEPTION 'Dispose retained child-safety cases before downgrading';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER protect_child_safety_message ON messages")
    op.execute("DROP FUNCTION protect_child_safety_message()")
    op.drop_index("ix_attachments_object_key", table_name="attachments")
    op.drop_index("ix_attachments_variants_object_keys", table_name="attachments")
    op.drop_constraint(op.f("ck_messages_safety_state_value"), "messages", type_="check")
    op.drop_column("messages", "safety_reason")
    op.drop_column("messages", "safety_updated_at")
    op.drop_column("messages", "safety_state")
    op.execute("UPDATE abuse_reports SET category = 'illegal_content' WHERE category = 'csam'")
    op.drop_constraint(
        op.f("ck_abuse_reports_abuse_report_category_value"), "abuse_reports", type_="check"
    )
    op.create_check_constraint(
        "abuse_report_category_value",
        "abuse_reports",
        "category IN ('spam','harassment','hate','sexual_content','violence','self_harm',"
        "'impersonation','privacy','malware','illegal_content','other')",
    )
    op.drop_table("safety_client_events")
    op.drop_table("child_safety_evidence")
    op.drop_table("child_safety_reports")
    op.drop_table("child_safety_cases")
