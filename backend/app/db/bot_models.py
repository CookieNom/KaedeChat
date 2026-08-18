from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DOMAIN_LENGTH = 253


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InstanceAdminGrant(Base, TimestampMixin):
    __tablename__ = "instance_admin_grants"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    granted_by_id: Mapped[int | None] = mapped_column(BigInteger)
    granted_by_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
        ),
        CheckConstraint("user_is_local", name="admin_grant_user_is_local"),
        CheckConstraint(
            "role IN ('owner','administrator','trust_safety','bot_reviewer','operations','auditor')",  # noqa: E501
            name="admin_grant_role_value",
        ),
        CheckConstraint("generation >= 1", name="admin_grant_generation_positive"),
        UniqueConstraint("user_id", "user_domain", "role", name="uq_admin_grant_user_role"),
        Index("ix_instance_admin_grants_active", "user_id", "user_domain", "revoked_at"),
    )


class InstanceAuditEvent(Base):
    __tablename__ = "instance_audit_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('user','admin','cli','service','system')", name="actor_kind_value"
        ),
        Index("ix_instance_audit_events_created", "created_at", "id"),
        Index("ix_instance_audit_events_target", "target_type", "target_ref"),
    )


class DeveloperTeam(Base, TimestampMixin):
    __tablename__ = "developer_teams"
    id: Mapped[int] = mapped_column(BigInteger)
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    personal: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name="team_name_length"),
    )


class DeveloperTeamMember(Base, TimestampMixin):
    __tablename__ = "developer_team_members"
    team_id: Mapped[int] = mapped_column(BigInteger)
    team_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    role: Mapped[str] = mapped_column(String(24), nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("team_id", "team_domain", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["team_id", "team_domain"],
            ["developer_teams.id", "developer_teams.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "user_domain", "user_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
        ),
        CheckConstraint("user_is_local", name="developer_team_member_user_is_local"),
        CheckConstraint(
            "role IN ('owner','administrator','developer','security','analyst','support')",
            name="developer_team_member_role_value",
        ),
        Index("ix_developer_team_members_user", "user_id", "user_domain"),
    )


class BotApplication(Base, TimestampMixin):
    __tablename__ = "bot_applications"
    id: Mapped[int] = mapped_column(BigInteger)
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    team_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    team_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    bot_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    icon_hash: Mapped[str | None] = mapped_column(String(128))
    support_url: Mapped[str | None] = mapped_column(String(2048))
    privacy_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="draft")
    custody_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="managed")
    target_policy: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    default_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    default_intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    default_permissions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    e2ee_modes: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["interaction_only"], server_default='["interaction_only"]'
    )
    manifest_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    command_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    revocation_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        ForeignKeyConstraint(
            ["team_id", "team_domain"], ["developer_teams.id", "developer_teams.origin_domain"]
        ),
        ForeignKeyConstraint(
            ["bot_user_id", "bot_user_domain"], ["users.id", "users.origin_domain"]
        ),
        UniqueConstraint("bot_user_id", "bot_user_domain", name="uq_bot_application_user"),
        CheckConstraint(
            "status IN ('draft','active','review_required','suspended','deleting','deleted')",
            name="bot_application_status_value",
        ),
        CheckConstraint("custody_mode IN ('managed','external')", name="custody_mode_value"),
        CheckConstraint(
            "target_policy IN ('open','allowlist','blocklist','local_only')",
            name="bot_target_policy_value",
        ),
        CheckConstraint(
            "default_permissions >= 0 AND manifest_generation >= 1 "
            "AND command_generation >= 1 AND revocation_generation >= 1",
            name="bot_application_positive_values",
        ),
        Index("ix_bot_applications_team", "team_id", "team_domain"),
    )


class BotCredential(Base, TimestampMixin):
    __tablename__ = "bot_credentials"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    token_hint: Mapped[str] = mapped_column(String(20), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("octet_length(token_hash) = 32", name="bot_credential_hash_length"),
        Index("ix_bot_credentials_application", "application_id", "application_domain"),
    )


class BotWorker(Base, TimestampMixin):
    __tablename__ = "bot_workers"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    target_domains: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    session_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("octet_length(public_key) = 32", name="bot_worker_public_key_length"),
        CheckConstraint(
            "generation >= 1 AND session_limit BETWEEN 1 AND 16", name="bot_worker_limits"
        ),
        UniqueConstraint("application_id", "application_domain", "name", name="uq_bot_worker_name"),
    )


class BotInstanceRule(Base, TimestampMixin):
    __tablename__ = "bot_instance_rules"
    application_id: Mapped[int] = mapped_column(BigInteger)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("application_id", "application_domain", "target_domain"),
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "effect IN (\x27allow\x27,\x27deny\x27)", name="bot_instance_rule_effect_value"
        ),
    )


class BotInstallTemplate(Base, TimestampMixin):
    __tablename__ = "bot_install_templates"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    permissions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    contexts: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["guild"], server_default='["guild"]'
    )
    e2ee_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="interaction_only"
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{1,63}$'", name="bot_template_slug_format"),
        CheckConstraint(
            "permissions >= 0 AND generation >= 1", name="bot_template_positive_values"
        ),
        CheckConstraint(
            "e2ee_mode IN ('disabled','interaction_only','participant')",
            name="bot_template_e2ee_mode_value",
        ),
        UniqueConstraint(
            "application_id", "application_domain", "slug", name="uq_bot_template_slug"
        ),
    )


class ApplicationCommand(Base, TimestampMixin):
    __tablename__ = "application_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="chat_input")
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(guild_id IS NULL) = (guild_domain IS NULL)", name="command_guild_ref_complete"
        ),
        CheckConstraint("name ~ '^[a-z0-9_-]{1,32}$'", name="application_command_name_format"),
        CheckConstraint(
            "type IN ('chat_input','user','message')", name="application_command_type_value"
        ),
        CheckConstraint(
            "state IN ('pending','active','superseded','failed')",
            name="application_command_state_value",
        ),
        UniqueConstraint(
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
            "type",
            "name",
            name="uq_application_command_scope_name",
        ),
    )


class BotInstallation(Base, TimestampMixin):
    __tablename__ = "bot_installations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    bot_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    role_id: Mapped[int | None] = mapped_column(BigInteger)
    role_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    installer_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    installer_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    granted_intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    granted_permissions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    channel_restrictions: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    e2ee_mode: Mapped[str] = mapped_column(String(24), nullable=False, server_default="disabled")
    grant_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    media_bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    media_pending_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["bot_user_id", "bot_user_domain"], ["users.id", "users.origin_domain"]
        ),
        ForeignKeyConstraint(
            ["role_id", "role_domain"],
            ["roles.id", "roles.origin_domain"],
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["installer_id", "installer_domain"], ["users.id", "users.origin_domain"]
        ),
        CheckConstraint(
            "(role_id IS NULL) = (role_domain IS NULL)",
            name="bot_installation_role_ref_complete",
        ),
        CheckConstraint(
            "granted_permissions >= 0 AND grant_revision >= 1 "
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0",
            name="bot_installation_positive_values",
        ),
        CheckConstraint(
            "e2ee_mode IN ('disabled','interaction_only','participant')",
            name="bot_installation_e2ee_mode_value",
        ),
        CheckConstraint(
            "status IN ('active','suspended','revoked')", name="bot_installation_status_value"
        ),
        UniqueConstraint(
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
            name="uq_bot_installation_app_guild",
        ),
        Index("ix_bot_installations_guild", "guild_id", "guild_domain", "status"),
    )


class BotToken(Base):
    __tablename__ = "bot_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dpop_thumbprint: Mapped[str | None] = mapped_column(String(128))
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["worker_id"], ["bot_workers.id"], ondelete="CASCADE"),
        CheckConstraint("octet_length(token_hash) = 32", name="bot_token_hash_length"),
        CheckConstraint("expires_at > issued_at", name="bot_token_positive_lifetime"),
        Index("ix_bot_tokens_worker_expiry", "worker_id", "expires_at"),
    )


class BotInteraction(Base, TimestampMixin):
    __tablename__ = "bot_interactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    command_name: Mapped[str] = mapped_column(String(32), nullable=False)
    command_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="chat_input"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    encrypted_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_message_id: Mapped[int | None] = mapped_column(BigInteger)
    response_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        ForeignKeyConstraint(["installation_id"], ["bot_installations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "command_type IN ('chat_input','user','message')",
            name="bot_interaction_command_type_value",
        ),
        CheckConstraint(
            "status IN ('pending','deferred','responded','expired','failed')",
            name="bot_interaction_status_value",
        ),
        CheckConstraint(
            "encrypted_payload IS NULL OR payload = '{}'::jsonb",
            name="bot_interaction_payload_mode",
        ),
        CheckConstraint(
            "(response_message_id IS NULL) = (response_message_domain IS NULL)",
            name="bot_interaction_response_ref_complete",
        ),
        Index(
            "ix_bot_interactions_application_status",
            "application_id",
            "application_domain",
            "status",
            "id",
        ),
        Index("ix_bot_interactions_expiry", "expires_at", "status"),
    )


class AbuseReport(Base, TimestampMixin):
    __tablename__ = "abuse_reports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source: Mapped[str] = mapped_column(String(24), nullable=False, server_default="user")
    reporter_id: Mapped[int | None] = mapped_column(BigInteger)
    reporter_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    reporter_is_local: Mapped[bool | None] = mapped_column(Boolean)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(320), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    message_ref: Mapped[str | None] = mapped_column(String(320))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    encryption_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="plaintext"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="submitted")
    assigned_admin_id: Mapped[int | None] = mapped_column(BigInteger)
    assigned_admin_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    resolution: Mapped[str | None] = mapped_column(String(2000))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["reporter_id", "reporter_domain", "reporter_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
        ),
        CheckConstraint("source IN ('user','photodna')", name="abuse_report_source_value"),
        CheckConstraint(
            "(source = 'user' AND reporter_id IS NOT NULL AND reporter_domain IS NOT NULL "
            "AND reporter_is_local) OR (source = 'photodna' AND reporter_id IS NULL "
            "AND reporter_domain IS NULL AND reporter_is_local IS NULL "
            "AND target_type = 'attachment' AND category = 'illegal_content' "
            "AND encryption_mode = 'plaintext')",
            name="abuse_report_source_reporter_policy",
        ),
        CheckConstraint(
            "target_type IN "
            "('message','user','bot','application','guild','instance','invite','attachment')",
            name="abuse_report_target_type_value",
        ),
        CheckConstraint(
            "category IN ('spam','harassment','hate','sexual_content','violence','self_harm','impersonation','privacy','malware','illegal_content','other')",  # noqa: E501
            name="abuse_report_category_value",
        ),
        CheckConstraint(
            "encryption_mode IN ('plaintext','e2ee_metadata','e2ee_user_disclosed')",
            name="abuse_report_encryption_mode_value",
        ),
        CheckConstraint(
            "status IN ('submitted','triaged','in_review','awaiting_remote','needs_information','action_taken','closed_no_action','duplicate','reopened')",  # noqa: E501
            name="abuse_report_status_value",
        ),
        Index("ix_abuse_reports_queue", "status", "created_at"),
        Index(
            "uq_abuse_reports_photodna_target",
            "source",
            "target_type",
            "target_ref",
            unique=True,
            postgresql_where=text("source = 'photodna'"),
        ),
    )
