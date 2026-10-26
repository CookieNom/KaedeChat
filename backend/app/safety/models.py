from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.bot_models import TimestampMixin


class ChildSafetyCase(Base, TimestampMixin):
    __tablename__ = "child_safety_cases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    subject_ref: Mapped[str | None] = mapped_column(String(320))
    message_ref: Mapped[str | None] = mapped_column(String(320))
    attachment_ref: Mapped[str | None] = mapped_column(String(320))
    subject_snapshot: Mapped[bytes | None] = mapped_column(LargeBinary)
    details: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    message_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(320))
    review_reason: Mapped[str | None] = mapped_column(String(2000))
    submissions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    holds: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    retention_extensions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    removal_objects: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    removal_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removal_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_submission_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retain_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source IN ('user','photodna')", name="source_value"),
        CheckConstraint("status IN ('pending','confirmed','dismissed')", name="status_value"),
        CheckConstraint(
            "completed_submission_at IS NULL OR (retain_until IS NOT NULL AND "
            "retain_until >= completed_submission_at + interval '1 year')",
            name="submission_minimum_retention",
        ),
        Index("ix_child_safety_cases_queue", "status", "created_at"),
        Index("ix_child_safety_cases_subject", "subject_ref"),
        Index("ix_child_safety_cases_message", "message_ref"),
        Index("ix_child_safety_cases_attachment", "attachment_ref"),
    )


class ChildSafetyReport(Base):
    """No account/report FK: a case must survive deletion of its source records."""

    __tablename__ = "child_safety_reports"

    report_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    case_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("child_safety_cases.id", ondelete="RESTRICT"), nullable=False
    )
    snapshot: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (Index("ix_child_safety_reports_case", "case_id"),)


class ChildSafetyEvidence(Base):
    __tablename__ = "child_safety_evidence"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    case_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("child_safety_cases.id", ondelete="RESTRICT"), nullable=False
    )
    attachment_ref: Mapped[str | None] = mapped_column(String(320))
    provenance: Mapped[str] = mapped_column(String(32), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_blob: Mapped[bytes | None] = mapped_column(LargeBinary)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("size >= 0", name="nonnegative_size"),
        CheckConstraint(
            "provenance IN ('local_upload','federated_origin','reporter_disclosed')",
            name="provenance_value",
        ),
        CheckConstraint("object_key LIKE 'evidence/%'", name="restricted_prefix"),
        CheckConstraint(
            "pending_blob IS NULL OR (stored_at IS NOT NULL AND deleted_at IS NULL)",
            name="buffer_is_preserved",
        ),
        Index("ix_child_safety_evidence_case", "case_id"),
        Index("ix_child_safety_evidence_attachment", "attachment_ref"),
    )


class SafetyClientEvent(Base):
    __tablename__ = "safety_client_events"

    sequence: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(253), nullable=False)
    message_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    attachment_refs: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending_review','removed','hidden','released')", name="state_value"
        ),
        Index("ix_safety_client_events_recipient", "user_id", "user_domain", "sequence"),
    )
