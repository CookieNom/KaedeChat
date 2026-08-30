from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.permissions import ALL_PERMISSIONS
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
    federation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    federation_metadata_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    federation_applications_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name="team_name_length"),
        CheckConstraint(
            "federation_revision >= 1",
            name="developer_team_federation_revision_positive",
        ),
        CheckConstraint(
            "(federation_metadata_fingerprint IS NULL "
            "OR octet_length(federation_metadata_fingerprint) = 32) "
            "AND (federation_applications_fingerprint IS NULL "
            "OR octet_length(federation_applications_fingerprint) = 32)",
            name="developer_team_federation_fingerprint_lengths",
        ),
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
        CheckConstraint(
            "role IN ('owner','administrator','developer','security','analyst','support')",
            name="developer_team_member_role_value",
        ),
        Index("ix_developer_team_members_user", "user_id", "user_domain"),
    )


class DeveloperTeamMemberHighwater(Base, TimestampMixin):
    """Last authority snapshot accepted for one local developer-team member."""

    __tablename__ = "developer_team_member_highwaters"
    team_id: Mapped[int] = mapped_column(BigInteger)
    team_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_is_local: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    snapshot_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
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
        CheckConstraint(
            "user_is_local",
            name="developer_team_member_highwater_user_is_local",
        ),
        CheckConstraint(
            "revision >= 1 AND octet_length(snapshot_fingerprint) = 32",
            name="developer_team_member_highwater_values",
        ),
        Index("ix_developer_team_member_highwaters_user", "user_id", "user_domain"),
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
    directory_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    directory_approved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=false()
    )
    directory_summary: Mapped[str | None] = mapped_column(String(200))
    directory_category: Mapped[str | None] = mapped_column(String(32))
    directory_tags: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    directory_collections: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    directory_media: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    directory_external_links: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    directory_supported_locales: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    directory_description_localizations: Mapped[dict[str, str]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    icon_hash: Mapped[str | None] = mapped_column(String(128))
    banner_hash: Mapped[str | None] = mapped_column(String(128))
    support_url: Mapped[str | None] = mapped_column(String(2048))
    privacy_url: Mapped[str | None] = mapped_column(String(2048))
    terms_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="draft")
    custody_mode: Mapped[str] = mapped_column(String(16), nullable=False, server_default="managed")
    target_policy: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    default_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    default_intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    default_permissions: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    supported_install_types: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["guild_install"], server_default='["guild_install"]'
    )
    user_install_scopes: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: ["applications.commands", "interactions.respond"],
        server_default='["applications.commands","interactions.respond"]',
    )
    user_install_contexts: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: ["guild", "bot_dm", "private_channel"],
        server_default='["guild","bot_dm","private_channel"]',
    )
    e2ee_modes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
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
        UniqueConstraint(
            "id",
            "origin_domain",
            "bot_user_id",
            "bot_user_domain",
            name="uq_bot_applications_id_origin_bot_user",
        ),
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
            "directory_category IS NULL OR directory_category IN "
            "('entertainment','games','moderation','productivity','social','utilities')",
            name="bot_application_directory_category_value",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_tags) = 'array' AND jsonb_array_length(directory_tags) <= 5",
            name="bot_application_directory_tags_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_collections) = 'array' "
            "AND jsonb_array_length(directory_collections) <= 3 "
            "AND directory_collections <@ "
            '\'["featured","staff-picks","new-and-noteworthy"]\'::jsonb',
            name="bot_application_directory_collections_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_media) = 'array' AND jsonb_array_length(directory_media) <= 5",
            name="bot_application_directory_media_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_external_links) = 'array' "
            "AND jsonb_array_length(directory_external_links) <= 5",
            name="bot_application_directory_external_links_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_supported_locales) = 'array' "
            "AND jsonb_array_length(directory_supported_locales) <= 32",
            name="bot_application_directory_supported_locales_bounded",
        ),
        CheckConstraint(
            "jsonb_typeof(directory_description_localizations) = 'object' "
            "AND jsonb_array_length(jsonb_path_query_array("
            "directory_description_localizations, '$.keyvalue()')) <= 32",
            name="bot_application_directory_localizations_bounded",
        ),
        CheckConstraint(
            "default_permissions >= 0 AND manifest_generation >= 1 "
            "AND command_generation >= 1 AND revocation_generation >= 1 "
            f"AND (default_permissions & ~{ALL_PERMISSIONS}) = 0",
            name="bot_application_positive_values",
        ),
        CheckConstraint(
            "jsonb_typeof(supported_install_types) = 'array' "
            "AND jsonb_array_length(supported_install_types) BETWEEN 1 AND 2",
            name="bot_application_install_types_are_bounded_array",
        ),
        CheckConstraint(
            "jsonb_typeof(user_install_scopes) = 'array' "
            "AND jsonb_array_length(user_install_scopes) BETWEEN 2 AND 4",
            name="bot_application_user_install_scopes_are_bounded_array",
        ),
        CheckConstraint(
            "jsonb_typeof(user_install_contexts) = 'array' "
            "AND jsonb_array_length(user_install_contexts) BETWEEN 1 AND 3",
            name="bot_application_user_install_contexts_are_bounded_array",
        ),
        CheckConstraint(
            "jsonb_typeof(e2ee_modes) = 'array' "
            "AND e2ee_modes <@ '[\"participant\"]'::jsonb "
            "AND jsonb_array_length(e2ee_modes) <= 1",
            name="bot_application_e2ee_modes_value",
        ),
        Index("ix_bot_applications_team", "team_id", "team_domain"),
        Index(
            "ix_bot_applications_directory",
            "directory_category",
            "id",
            postgresql_where=text("directory_enabled AND directory_approved AND status = 'active'"),
        ),
    )


class ApplicationAsset(Base, TimestampMixin):
    __tablename__ = "application_assets"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('icon','cover','store','achievement','activity','other')",
            name="application_asset_kind_value",
        ),
        CheckConstraint("media_hash ~ '^[0-9a-f]{64}$'", name="application_asset_hash_format"),
        CheckConstraint(
            "(width IS NULL) = (height IS NULL) AND (width IS NULL OR (width > 0 AND height > 0))",
            name="application_asset_dimensions",
        ),
        CheckConstraint("version >= 1", name="application_asset_version_positive"),
        UniqueConstraint(
            "application_id",
            "application_domain",
            "kind",
            "name",
            name="uq_application_asset_name",
        ),
    )


class ApplicationEmoji(Base, TimestampMixin):
    __tablename__ = "application_emojis"
    id: Mapped[int] = mapped_column(BigInteger)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    name_casefold: Mapped[str] = mapped_column(String(32), nullable=False)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str | None] = mapped_column(String(512))
    animated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    creator_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    __table_args__ = (
        PrimaryKeyConstraint("id", "application_domain"),
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["creator_id", "creator_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("media_hash ~ '^[0-9a-f]{64}$'", name="application_emoji_hash_format"),
        CheckConstraint("version >= 1", name="application_emoji_version_positive"),
        UniqueConstraint(
            "application_id",
            "application_domain",
            "name_casefold",
            name="uq_application_emoji_name",
        ),
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
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
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

    @property
    def authority_id(self) -> int:
        return self.source_id if self.source_id is not None else self.id

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
        CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name="bot_worker_source_ref_complete",
        ),
        CheckConstraint(
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
            name="bot_worker_source_authority",
        ),
        UniqueConstraint("source_id", "source_domain", name="uq_bot_worker_source"),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_bot_workers_id_application",
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
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
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
    e2ee_mode: Mapped[str] = mapped_column(String(24), nullable=False, server_default="disabled")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=true())

    @property
    def authority_id(self) -> int:
        return self.source_id if self.source_id is not None else self.id

    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("slug ~ '^[a-z0-9][a-z0-9_-]{1,63}$'", name="bot_template_slug_format"),
        CheckConstraint(
            f"permissions >= 0 AND generation >= 1 AND (permissions & ~{ALL_PERMISSIONS}) = 0",
            name="bot_template_positive_values",
        ),
        CheckConstraint(
            "e2ee_mode IN ('disabled','participant')",
            name="bot_template_e2ee_mode_value",
        ),
        CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name="bot_template_source_ref_complete",
        ),
        CheckConstraint(
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
            name="bot_template_source_authority",
        ),
        UniqueConstraint("source_id", "source_domain", name="uq_bot_template_source"),
        UniqueConstraint(
            "application_id", "application_domain", "slug", name="uq_bot_template_slug"
        ),
    )


class ApplicationCommand(Base, TimestampMixin):
    __tablename__ = "application_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="chat_input")
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    contexts: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["guild"], server_default='["guild"]'
    )
    integration_types: Mapped[list[str]] = mapped_column(
        JSONB, default=lambda: ["guild_install"], server_default='["guild_install"]'
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")

    @property
    def authority_id(self) -> int:
        return self.source_id if self.source_id is not None else self.id

    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(guild_id IS NULL) = (guild_domain IS NULL)", name="command_guild_ref_complete"
        ),
        CheckConstraint(
            "char_length(name) BETWEEN 1 AND 32",
            name="application_command_name_format",
        ),
        CheckConstraint(
            "type IN ('chat_input','user','message')", name="application_command_type_value"
        ),
        CheckConstraint(
            "state IN ('pending','active','superseded','failed')",
            name="application_command_state_value",
        ),
        CheckConstraint("jsonb_typeof(contexts) = 'array'", name="command_contexts_are_array"),
        CheckConstraint(
            "jsonb_typeof(integration_types) = 'array'",
            name="command_integration_types_are_array",
        ),
        CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name="command_source_ref_complete",
        ),
        CheckConstraint(
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
            name="command_source_authority",
        ),
        UniqueConstraint("source_id", "source_domain", name="uq_application_command_source"),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_application_commands_id_application",
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


class ApplicationCommandPermission(Base, TimestampMixin):
    """Guild-authoritative application or command permission overwrite."""

    __tablename__ = "application_command_permissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    # Application-wide configuration is represented by a null command ID.
    # Command rows use the local surrogate key even when the command authority
    # lives on another instance.
    command_id: Mapped[int | None] = mapped_column(BigInteger)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    permission: Mapped[bool] = mapped_column(Boolean, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["command_id", "application_id", "application_domain"],
            [
                "application_commands.id",
                "application_commands.application_id",
                "application_commands.application_domain",
            ],
            name="fk_application_command_permissions_command_application_lineage",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "target_type IN ('role','user','channel')",
            name="application_command_permission_target_type_value",
        ),
        CheckConstraint(
            "target_id >= 0",
            name="application_command_permission_target_id_nonnegative",
        ),
        Index(
            "uq_application_command_permission_application_target",
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
            "target_id",
            "target_domain",
            "target_type",
            unique=True,
            postgresql_where=text("command_id IS NULL"),
        ),
        Index(
            "uq_application_command_permission_command_target",
            "command_id",
            "target_id",
            "target_domain",
            "target_type",
            unique=True,
            postgresql_where=text("command_id IS NOT NULL"),
        ),
        Index(
            "ix_application_command_permissions_guild_application",
            "guild_id",
            "guild_domain",
            "application_id",
            "application_domain",
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
            ["application_id", "application_domain", "bot_user_id", "bot_user_domain"],
            [
                "bot_applications.id",
                "bot_applications.origin_domain",
                "bot_applications.bot_user_id",
                "bot_applications.bot_user_domain",
            ],
            name="fk_bot_installations_application_bot_user_lineage",
        ),
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
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0 "
            f"AND (granted_permissions & ~{ALL_PERMISSIONS}) = 0",
            name="bot_installation_positive_values",
        ),
        CheckConstraint(
            "e2ee_mode IN ('disabled','participant')",
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
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_bot_installations_id_application",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            "guild_id",
            "guild_domain",
            name="uq_bot_installations_id_application_guild",
        ),
        Index("ix_bot_installations_guild", "guild_id", "guild_domain", "status"),
    )


class BotUserInstallation(Base, TimestampMixin):
    """A user-owned installation; it does not make the bot a DM participant."""

    __tablename__ = "bot_user_installations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Stable identity minted by the installing user's home. Mirrors retain a
    # local surrogate ``id`` for existing foreign keys, but never derive it
    # from a truncated hash of this authority-owned reference.
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    granted_intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    contexts: Mapped[list[str]] = mapped_column(
        JSONB,
        default=lambda: ["bot_dm", "private_channel"],
        server_default='["bot_dm","private_channel"]',
    )
    grant_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    # User-installed applications can return files from private interaction
    # responses without borrowing a human user's storage ledger.
    media_bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    media_pending_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    authority_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("jsonb_typeof(contexts) = 'array'", name="user_install_contexts_are_array"),
        CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name="user_install_source_ref_complete",
        ),
        CheckConstraint(
            "grant_revision >= 1 AND media_bytes_used >= 0 AND media_pending_bytes >= 0",
            name="user_install_revision_positive",
        ),
        CheckConstraint(
            "status IN ('active','suspended','revoked')", name="user_install_status_value"
        ),
        UniqueConstraint(
            "application_id",
            "application_domain",
            "user_id",
            "user_domain",
            name="uq_bot_user_installation_app_user",
        ),
        UniqueConstraint(
            "source_id",
            "source_domain",
            "application_id",
            "application_domain",
            "user_id",
            "user_domain",
            name="uq_bot_user_installation_source_app_user",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_bot_user_installations_id_application",
        ),
        Index("ix_bot_user_installations_user", "user_id", "user_domain", "status"),
        Index(
            "ix_bot_user_installations_authority_expiry",
            "authority_expires_at",
            postgresql_where=text("status = 'active' AND authority_expires_at IS NOT NULL"),
        ),
    )


class BotApplicationTarget(Base, TimestampMixin):
    """Authority-signed aggregate of an application's presence on one instance.

    The target instance owns ``generation`` and the two installation counts.
    Application homes retain the same row as a monotonic projection, allowing
    workers to discover runtime targets without exposing guild or user rosters.
    Zero-count rows at generation 1+ are deliberate tombstones that fence
    delayed snapshots. Generation 0 is the sole "counts not discovered yet"
    sentinel used when A first learns only a runtime destination.
    """

    __tablename__ = "bot_application_targets"
    application_id: Mapped[int] = mapped_column(BigInteger)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    guild_installations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    user_installations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    runtime_manifest_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    runtime_revocation_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    runtime_access_revocation_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    runtime_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    runtime_target_allowed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=true()
    )
    runtime_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    __table_args__ = (
        PrimaryKeyConstraint("application_id", "application_domain", "target_domain"),
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["target_domain"], ["instances.domain"]),
        CheckConstraint(
            "generation >= 0 AND guild_installations >= 0 AND user_installations >= 0 "
            "AND runtime_manifest_generation >= 0 AND runtime_revocation_generation >= 0 "
            "AND runtime_access_revocation_generation >= 0 "
            "AND (runtime_fingerprint IS NULL OR octet_length(runtime_fingerprint) = 32)",
            name="bot_application_target_nonnegative_values",
        ),
        CheckConstraint(
            "runtime_status IN "
            "('draft','active','review_required','suspended','deleting','deleted')",
            name="bot_application_target_runtime_status_value",
        ),
        Index(
            "ix_bot_application_targets_active",
            "application_id",
            "application_domain",
            "target_domain",
            postgresql_where=text("guild_installations > 0 OR user_installations > 0"),
        ),
    )


class BotApplicationRuntimeHighwater(Base, TimestampMixin):
    """A-signed runtime state retained before a full app mirror exists."""

    __tablename__ = "bot_application_runtime_highwaters"
    application_id: Mapped[int] = mapped_column(BigInteger)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    bot_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    manifest_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    revocation_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    access_revocation_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    target_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    runtime_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("application_id", "application_domain", "target_domain"),
        CheckConstraint(
            "manifest_generation >= 1 AND revocation_generation >= 1 "
            "AND access_revocation_generation >= 0 "
            "AND octet_length(runtime_fingerprint) = 32",
            name="bot_application_runtime_highwater_values",
        ),
        CheckConstraint(
            "status IN ('draft','active','review_required','suspended','deleting','deleted')",
            name="bot_application_runtime_highwater_status_value",
        ),
        Index(
            "ix_bot_application_runtime_highwaters_expiry",
            "application_domain",
            "expires_at",
        ),
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
    dm_capability_id: Mapped[int | None] = mapped_column(BigInteger)
    dm_capability_revision: Mapped[int | None] = mapped_column(BigInteger)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["worker_id", "application_id", "application_domain"],
            [
                "bot_workers.id",
                "bot_workers.application_id",
                "bot_workers.application_domain",
            ],
            name="fk_bot_tokens_worker_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["dm_capability_id", "application_id", "application_domain"],
            [
                "bot_dm_capabilities.id",
                "bot_dm_capabilities.application_id",
                "bot_dm_capabilities.application_domain",
            ],
            name="fk_bot_tokens_dm_capability_application_lineage",
            ondelete="CASCADE",
        ),
        CheckConstraint("octet_length(token_hash) = 32", name="bot_token_hash_length"),
        CheckConstraint("expires_at > issued_at", name="bot_token_positive_lifetime"),
        CheckConstraint(
            "(dm_capability_id IS NULL) = (dm_capability_revision IS NULL) "
            "AND (dm_capability_revision IS NULL OR dm_capability_revision >= 1)",
            name="bot_token_dm_capability_binding",
        ),
        Index("ix_bot_tokens_worker_expiry", "worker_id", "expires_at"),
        Index("ix_bot_tokens_dm_capability", "dm_capability_id", "expires_at"),
    )


class BotInteraction(Base, TimestampMixin):
    __tablename__ = "bot_interactions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    user_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    # A DM capability is an independent, authority-signed installation
    # projection.  Keep its local surrogate and exact revision on the
    # interaction instead of trying to recover it later from the channel or a
    # generic application token.
    dm_capability_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    interaction_type: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="command"
    )
    context: Mapped[str] = mapped_column(String(24), nullable=False, server_default="guild")
    integration_type: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="guild_install"
    )
    # Guild responses from a user-installed application use the invoking
    # member's authority captured at invocation time.  A missing snapshot is
    # fail-closed for public user-install responses.
    invocation_permissions: Mapped[int | None] = mapped_column(BigInteger)
    invocation_channel_type: Mapped[int | None] = mapped_column(Integer)
    installation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command_id: Mapped[int | None] = mapped_column(BigInteger)
    command_name: Mapped[str | None] = mapped_column(String(32))
    command_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="chat_input"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    encrypted_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    custom_id: Mapped[str | None] = mapped_column(String(100))
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    dispatch_fingerprint: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    response_grant_id: Mapped[str | None] = mapped_column(String(64))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    callback_type: Mapped[int | None] = mapped_column(Integer)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    autocomplete_generation: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_message_id: Mapped[int | None] = mapped_column(BigInteger)
    response_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "installation_id",
                "application_id",
                "application_domain",
                "guild_id",
                "guild_domain",
            ],
            [
                "bot_installations.id",
                "bot_installations.application_id",
                "bot_installations.application_domain",
                "bot_installations.guild_id",
                "bot_installations.guild_domain",
            ],
            name="fk_bot_interactions_installation_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_installation_id", "application_id", "application_domain"],
            [
                "bot_user_installations.id",
                "bot_user_installations.application_id",
                "bot_user_installations.application_domain",
            ],
            name="fk_bot_interactions_user_installation_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "dm_capability_id",
                "application_id",
                "application_domain",
                "channel_id",
                "channel_domain",
                "user_id",
                "user_domain",
            ],
            [
                "bot_dm_capabilities.id",
                "bot_dm_capabilities.application_id",
                "bot_dm_capabilities.application_domain",
                "bot_dm_capabilities.conversation_id",
                "bot_dm_capabilities.conversation_domain",
                "bot_dm_capabilities.target_user_id",
                "bot_dm_capabilities.target_user_domain",
            ],
            name="fk_bot_interactions_dm_capability_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["command_id", "application_id", "application_domain"],
            [
                "application_commands.id",
                "application_commands.application_id",
                "application_commands.application_domain",
            ],
            name="fk_bot_interactions_command_application_lineage",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_bot_interactions_channel_guild_lineage",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "command_type IN ('chat_input','user','message')",
            name="bot_interaction_command_type_value",
        ),
        CheckConstraint(
            "interaction_type IN ('command','component','modal_submit','autocomplete')",
            name="bot_interaction_type_value",
        ),
        CheckConstraint(
            "context IN ('guild','bot_dm','private_channel')",
            name="bot_interaction_context_value",
        ),
        CheckConstraint(
            "integration_type IN ('guild_install','user_install','dm_capability')",
            name="bot_interaction_integration_type_value",
        ),
        CheckConstraint(
            "invocation_permissions IS NULL OR (invocation_permissions >= 0 "
            f"AND (invocation_permissions & ~{ALL_PERMISSIONS}) = 0)",
            name="bot_interaction_invocation_permissions_nonnegative",
        ),
        CheckConstraint(
            "(guild_id IS NULL) = (invocation_channel_type IS NULL) AND "
            "(invocation_channel_type IS NULL OR invocation_channel_type BETWEEN 0 AND 18)",
            name="bot_interaction_invocation_channel_type_context",
        ),
        CheckConstraint("installation_revision >= 1", name="bot_interaction_revision_positive"),
        CheckConstraint(
            "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int "
            "+ (dm_capability_id IS NOT NULL)::int = 1",
            name="bot_interaction_one_installation",
        ),
        CheckConstraint(
            "(guild_id IS NULL) = (guild_domain IS NULL)",
            name="bot_interaction_guild_ref_complete",
        ),
        CheckConstraint(
            "(context = 'guild') = (guild_id IS NOT NULL)",
            name="bot_interaction_context_matches_guild",
        ),
        CheckConstraint(
            "installation_id IS NULL OR guild_id IS NOT NULL",
            name="bot_interaction_guild_install_context",
        ),
        CheckConstraint(
            "(integration_type = 'guild_install') = (installation_id IS NOT NULL)",
            name="bot_interaction_integration_matches_installation",
        ),
        CheckConstraint(
            "(integration_type = 'user_install') = (user_installation_id IS NOT NULL)",
            name="bot_interaction_integration_matches_user_installation",
        ),
        CheckConstraint(
            "(integration_type = 'dm_capability') = (dm_capability_id IS NOT NULL)",
            name="bot_interaction_integration_matches_dm_capability",
        ),
        CheckConstraint(
            "dm_capability_id IS NULL OR context = 'bot_dm'",
            name="bot_interaction_dm_capability_context",
        ),
        CheckConstraint(
            "(interaction_type IN ('command','autocomplete')) = (command_name IS NOT NULL)",
            name="bot_interaction_command_name_context",
        ),
        CheckConstraint(
            "(interaction_type IN ('command','autocomplete')) = (command_id IS NOT NULL)",
            name="bot_interaction_command_id_context",
        ),
        CheckConstraint(
            "(message_id IS NULL) = (message_domain IS NULL)",
            name="bot_interaction_message_ref_complete",
        ),
        CheckConstraint(
            "token_hash IS NULL OR octet_length(token_hash) = 32",
            name="bot_interaction_token_hash_length",
        ),
        CheckConstraint(
            "dispatch_fingerprint IS NULL OR octet_length(dispatch_fingerprint) = 32",
            name="bot_interaction_dispatch_fingerprint_length",
        ),
        CheckConstraint(
            "response_grant_id IS NULL OR char_length(response_grant_id) BETWEEN 32 AND 64",
            name="bot_interaction_response_grant_length",
        ),
        CheckConstraint(
            "(response_grant_id IS NULL) = (request_fingerprint IS NULL)",
            name="bot_interaction_remote_request_binding",
        ),
        CheckConstraint(
            "request_fingerprint IS NULL OR char_length(request_fingerprint) = 64",
            name="bot_interaction_request_fingerprint_length",
        ),
        CheckConstraint(
            "status IN ('pending','deferred','responded','expired','failed')",
            name="bot_interaction_status_value",
        ),
        CheckConstraint(
            "encrypted_payload IS NULL OR (payload - ARRAY["
            "'_interaction_event_snapshot','_interaction_installation_lineage',"
            "'target_ref','response_id','view_version','triggering_interaction_id',"
            "'source_component','source_modal']::text[]) = '{}'::jsonb",
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
        Index(
            "uq_bot_interactions_response_grant",
            "response_grant_id",
            unique=True,
            postgresql_where=text("response_grant_id IS NOT NULL"),
        ),
        Index("ix_bot_interactions_expiry", "expires_at", "status"),
    )


class BotInteractionResponse(Base, TimestampMixin):
    """Initial/original and follow-up responses, including isolated ephemeral data."""

    __tablename__ = "bot_interaction_responses"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    interaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    response_type: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    ephemeral: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Monotonic authority-owned revision for the private response projection.
    # Federation delivery may retry or reorder CREATE/UPDATE/DELETE envelopes;
    # the invoking user's home applies only the greatest exact revision.
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    __table_args__ = (
        ForeignKeyConstraint(["interaction_id"], ["bot_interactions.id"], ondelete="CASCADE"),
        CheckConstraint("sequence >= 0", name="interaction_response_sequence_nonnegative"),
        CheckConstraint(
            "response_type IN (1,4,5,6,7,8,9,10)",
            name="interaction_response_type_value",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="response_payload_is_object"),
        CheckConstraint("revision >= 1", name="response_revision_positive"),
        CheckConstraint(
            "(message_id IS NULL) = (message_domain IS NULL)",
            name="interaction_response_message_ref_complete",
        ),
        CheckConstraint(
            "NOT ephemeral OR message_id IS NULL",
            name="ephemeral_response_is_not_channel_message",
        ),
        UniqueConstraint("interaction_id", "sequence", name="uq_interaction_response_sequence"),
        Index("ix_interaction_responses_interaction", "interaction_id", "sequence"),
    )


class FederatedInteractionResponseLocator(Base, TimestampMixin):
    """Non-secret routing/tombstone state retained at an invoker's home.

    Private response bodies remain in the signed federation event/in-memory
    gateway projection.  This row retains only enough authority and revision
    state to reject colliding snowflakes, stale replays, and route later user
    actions to the interaction authority.
    """

    __tablename__ = "federated_interaction_response_locators"
    response_id: Mapped[int] = mapped_column(BigInteger)
    response_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    interaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    interaction_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    response_type: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("response_id", "response_domain"),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("response_domain = interaction_domain", name="response_authority_matches"),
        CheckConstraint("sequence >= 0", name="response_locator_sequence_nonnegative"),
        CheckConstraint("revision >= 1", name="response_locator_revision_positive"),
        CheckConstraint(
            "char_length(event_fingerprint) = 64",
            name="response_locator_event_fingerprint_length",
        ),
        Index(
            "ix_interaction_response_locators_invoker",
            "user_id",
            "user_domain",
            "interaction_id",
            "interaction_domain",
        ),
    )


class FederatedInteractionAdmissionGrant(Base, TimestampMixin):
    """A one-interaction A->C nonce authorizing private response projection."""

    __tablename__ = "federated_interaction_admission_grants"
    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    authority_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    interaction_id: Mapped[int | None] = mapped_column(BigInteger)
    interaction_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "char_length(grant_id) BETWEEN 32 AND 64",
            name="interaction_admission_grant_id_length",
        ),
        CheckConstraint(
            "authority_domain = channel_domain",
            name="interaction_admission_grant_channel_authority",
        ),
        CheckConstraint(
            "(interaction_id IS NULL) = (interaction_domain IS NULL)",
            name="interaction_admission_grant_interaction_ref_complete",
        ),
        UniqueConstraint(
            "interaction_id",
            "interaction_domain",
            name="uq_interaction_admission_grant_interaction_ref",
        ),
        Index(
            "ix_interaction_admission_grants_expiry",
            "expires_at",
        ),
    )


class InteractionDispatchOutbox(Base):
    """At-least-once private Gateway projection without duplicating its body."""

    __tablename__ = "interaction_dispatch_outbox"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    interaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    interaction_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    response_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    response_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation: Mapped[str] = mapped_column(String(8), nullable=False)
    event_origin_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    event_id: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        ForeignKeyConstraint(
            ["event_origin_domain", "event_id"],
            ["federation_events.origin_domain", "federation_events.event_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "response_id",
            "response_domain",
            "revision",
            name="uq_interaction_dispatch_response_revision",
        ),
        CheckConstraint("revision >= 1", name="interaction_dispatch_revision_positive"),
        CheckConstraint("attempts >= 0", name="interaction_dispatch_attempts_nonnegative"),
        CheckConstraint(
            "operation IN ('CREATE','UPDATE','DELETE')",
            name="interaction_dispatch_operation_value",
        ),
        CheckConstraint(
            "(event_origin_domain IS NULL) = (event_id IS NULL)",
            name="interaction_dispatch_event_ref_complete",
        ),
        Index("ix_interaction_dispatch_due", "next_attempt_at", "id"),
    )


class InteractionCreateDispatchOutbox(Base):
    """Sealed, short-lived recovery for one committed INTERACTION_CREATE."""

    __tablename__ = "interaction_create_dispatch_outbox"
    interaction_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topic: Mapped[str] = mapped_column(String(768), nullable=False)
    audience_user_ref: Mapped[str] = mapped_column(String(320), nullable=False)
    event_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    event_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(["interaction_id"], ["bot_interactions.id"], ondelete="CASCADE"),
        CheckConstraint(
            "octet_length(event_ciphertext) BETWEEN 29 AND 1048605",
            name="interaction_create_dispatch_ciphertext_length",
        ),
        CheckConstraint(
            "char_length(audience_user_ref) BETWEEN 3 AND 320",
            name="interaction_create_dispatch_audience_length",
        ),
        CheckConstraint(
            "octet_length(event_fingerprint) = 32",
            name="interaction_create_dispatch_fingerprint_length",
        ),
        CheckConstraint(
            "attempts >= 0",
            name="interaction_create_dispatch_attempts_nonnegative",
        ),
        Index(
            "ix_interaction_create_dispatch_due",
            "next_attempt_at",
            "interaction_id",
        ),
    )


class FederatedInteractionAttachmentGrant(Base, TimestampMixin):
    """One bounded A->C invocation-media disclosure and binding capability."""

    __tablename__ = "federated_interaction_attachment_grants"
    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    attachment_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attachment_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    destination_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    interaction_id: Mapped[int | None] = mapped_column(BigInteger)
    interaction_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    metadata_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    admission_grant_id: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["attachment_id", "attachment_domain"],
            ["attachments.id", "attachments.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "(interaction_id IS NULL) = (interaction_domain IS NULL)",
            name="interaction_attachment_grant_interaction_ref_complete",
        ),
        CheckConstraint(
            "char_length(metadata_fingerprint) = 64",
            name="interaction_attachment_grant_fingerprint_length",
        ),
        CheckConstraint(
            "admission_grant_id IS NULL OR char_length(admission_grant_id) BETWEEN 32 AND 64",
            name="interaction_attachment_admission_grant_length",
        ),
        UniqueConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
            name="uq_interaction_attachment_grant_destination",
        ),
        Index(
            "ix_interaction_attachment_grants_expiry",
            "expires_at",
            "destination_domain",
        ),
    )


class BotInteractionPoll(Base):
    """A normalized poll attached to an isolated interaction response."""

    __tablename__ = "bot_interaction_polls"
    response_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    question: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allow_multiselect: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
    layout_type: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(["response_id"], ["bot_interaction_responses.id"], ondelete="CASCADE"),
        CheckConstraint("jsonb_typeof(question) = 'object'", name="question_is_object"),
        CheckConstraint("layout_type = 1", name="layout_type_value"),
        CheckConstraint("expires_at > created_at", name="positive_duration"),
    )


class BotInteractionPollAnswer(Base):
    __tablename__ = "bot_interaction_poll_answers"
    response_id: Mapped[int] = mapped_column(BigInteger)
    answer_id: Mapped[int] = mapped_column(SmallInteger)
    text: Mapped[str | None] = mapped_column(String(55))
    emoji: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    __table_args__ = (
        PrimaryKeyConstraint("response_id", "answer_id"),
        ForeignKeyConstraint(
            ["response_id"], ["bot_interaction_polls.response_id"], ondelete="CASCADE"
        ),
        CheckConstraint("answer_id BETWEEN 1 AND 10", name="answer_id_range"),
        CheckConstraint("text IS NOT NULL OR emoji IS NOT NULL", name="answer_has_body"),
        CheckConstraint("emoji IS NULL OR jsonb_typeof(emoji) = 'object'", name="emoji_is_object"),
    )


class BotInteractionPollVote(Base):
    __tablename__ = "bot_interaction_poll_votes"
    response_id: Mapped[int] = mapped_column(BigInteger)
    answer_id: Mapped[int] = mapped_column(SmallInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        PrimaryKeyConstraint("response_id", "answer_id", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["response_id", "answer_id"],
            ["bot_interaction_poll_answers.response_id", "bot_interaction_poll_answers.answer_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        Index("ix_bot_interaction_poll_votes_voter", "user_id", "user_domain", "response_id"),
    )


class BotDMCapability(Base, TimestampMixin):
    """One install-authority proof bound to a single direct conversation.

    Guild installation snowflakes are only unique at their authority. This row
    keeps the exact composite source identity and a local surrogate ``id`` for
    FKs/quota accounting. It deliberately is not a ``BotInstallation`` replica:
    a DM authority has no right to fabricate guild membership or roles.
    """

    __tablename__ = "bot_dm_capabilities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_installation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    bot_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bot_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    installing_user_id: Mapped[int | None] = mapped_column(BigInteger)
    installing_user_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    target_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    pair_key: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    conversation_id: Mapped[int | None] = mapped_column(BigInteger)
    conversation_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    granted_intents: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    channel_restrictions: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    e2ee_mode: Mapped[str] = mapped_column(String(24), nullable=False, server_default="disabled")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    admission_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    target_access_revocation_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    proof_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    proof: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    media_bytes_used: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    media_pending_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["bot_user_id", "bot_user_domain"],
            ["users.id", "users.origin_domain"],
        ),
        ForeignKeyConstraint(
            ["installing_user_id", "installing_user_domain"],
            ["users.id", "users.origin_domain"],
        ),
        ForeignKeyConstraint(
            ["target_user_id", "target_user_domain"],
            ["users.id", "users.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["source_installation_domain"], ["instances.domain"]),
        ForeignKeyConstraint(["authority_domain"], ["instances.domain"]),
        CheckConstraint(
            "(conversation_id IS NULL) = (conversation_domain IS NULL)",
            name="bot_dm_capability_conversation_ref_complete",
        ),
        CheckConstraint(
            "conversation_domain IS NULL OR conversation_domain = authority_domain",
            name="bot_dm_capability_conversation_authority",
        ),
        CheckConstraint(
            "(source_kind = 'guild' AND guild_id IS NOT NULL AND guild_domain IS NOT NULL "
            "AND installing_user_id IS NULL AND installing_user_domain IS NULL) OR "
            "(source_kind = 'user' AND guild_id IS NULL AND guild_domain IS NULL "
            "AND installing_user_id = target_user_id "
            "AND installing_user_domain = target_user_domain)",
            name="bot_dm_capability_source_context",
        ),
        CheckConstraint(
            "grant_id ~ '^kbdg_[A-Za-z0-9_-]{43}$'",
            name="bot_dm_capability_grant_id_format",
        ),
        CheckConstraint(
            "pair_key ~ '^[0-9a-f]{64}$'",
            name="bot_dm_capability_pair_key_format",
        ),
        CheckConstraint(
            "jsonb_typeof(granted_scopes) = 'array' "
            "AND jsonb_typeof(granted_intents) = 'array' "
            "AND jsonb_typeof(channel_restrictions) = 'array' "
            "AND jsonb_typeof(proof) = 'object'",
            name="bot_dm_capability_json_shapes",
        ),
        CheckConstraint(
            "e2ee_mode IN ('disabled','participant')",
            name="bot_dm_capability_e2ee_mode_value",
        ),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="bot_dm_capability_status_value",
        ),
        CheckConstraint(
            "revision >= 1 AND admission_revision >= 1 AND admission_revision <= revision "
            "AND target_access_revocation_generation >= 0 "
            "AND media_bytes_used >= 0 AND media_pending_bytes >= 0 "
            "AND octet_length(proof_fingerprint) = 32 AND expires_at > created_at",
            name="bot_dm_capability_positive_values",
        ),
        UniqueConstraint("grant_id", name="uq_bot_dm_capability_grant_id"),
        UniqueConstraint(
            "source_kind",
            "source_installation_id",
            "source_installation_domain",
            "pair_key",
            "authority_domain",
            name="uq_bot_dm_capability_source_pair_authority",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_bot_dm_capabilities_id_application",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            "target_user_id",
            "target_user_domain",
            name="uq_bot_dm_capabilities_id_app_conversation_target",
        ),
        Index(
            "ix_bot_dm_capabilities_application_active",
            "application_id",
            "application_domain",
            "bot_user_id",
            "bot_user_domain",
            "status",
        ),
        Index(
            "ix_bot_dm_capabilities_conversation_active",
            "conversation_id",
            "conversation_domain",
            "status",
        ),
    )


class BotDMCapabilityHighwater(Base, TimestampMixin):
    """Minimal B-signed grant ledger retained before identities materialize."""

    __tablename__ = "bot_dm_capability_highwaters"
    grant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    installation_authority_domain: Mapped[str] = mapped_column(
        String(DOMAIN_LENGTH), nullable=False
    )
    identity_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorization_fingerprint: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        CheckConstraint(
            "grant_id ~ '^kbdg_[A-Za-z0-9_-]{43}$'",
            name="bot_dm_capability_highwater_grant_id_format",
        ),
        CheckConstraint("revision >= 1", name="bot_dm_capability_highwater_revision_positive"),
        CheckConstraint(
            "status IN ('active','suspended','revoked')",
            name="bot_dm_capability_highwater_status_value",
        ),
        CheckConstraint(
            "octet_length(identity_fingerprint) = 32 "
            "AND octet_length(authorization_fingerprint) = 32",
            name="bot_dm_capability_highwater_fingerprints",
        ),
        Index(
            "ix_bot_dm_capability_highwaters_authority_expiry",
            "installation_authority_domain",
            "expires_at",
        ),
    )


class BotDMGrant(Base, TimestampMixin):
    __tablename__ = "bot_dm_grants"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    conversation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    user_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    dm_capability_id: Mapped[int | None] = mapped_column(BigInteger)
    granted_by_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    granted_by_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    consent_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    consent_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    history_floor_message_id: Mapped[int | None] = mapped_column(BigInteger)
    history_floor_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["installation_id", "application_id", "application_domain"],
            [
                "bot_installations.id",
                "bot_installations.application_id",
                "bot_installations.application_domain",
            ],
            name="fk_bot_dm_grants_installation_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_installation_id", "application_id", "application_domain"],
            [
                "bot_user_installations.id",
                "bot_user_installations.application_id",
                "bot_user_installations.application_domain",
            ],
            name="fk_bot_dm_grants_user_installation_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "dm_capability_id",
                "application_id",
                "application_domain",
                "conversation_id",
                "conversation_domain",
                "granted_by_id",
                "granted_by_domain",
            ],
            [
                "bot_dm_capabilities.id",
                "bot_dm_capabilities.application_id",
                "bot_dm_capabilities.application_domain",
                "bot_dm_capabilities.conversation_id",
                "bot_dm_capabilities.conversation_domain",
                "bot_dm_capabilities.target_user_id",
                "bot_dm_capabilities.target_user_domain",
            ],
            name="fk_bot_dm_grants_dm_capability_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["granted_by_id", "granted_by_domain"], ["users.id", "users.origin_domain"]
        ),
        CheckConstraint(
            "(installation_id IS NOT NULL)::int + (user_installation_id IS NOT NULL)::int + "
            "(dm_capability_id IS NOT NULL)::int = 1",
            name="bot_dm_grant_one_installation",
        ),
        CheckConstraint(
            "consent_state IN ('pending','active','revoked')", name="bot_dm_consent_state_value"
        ),
        CheckConstraint("jsonb_typeof(scopes) = 'array'", name="bot_dm_scopes_are_array"),
        CheckConstraint("consent_generation >= 1", name="bot_dm_consent_generation_positive"),
        CheckConstraint(
            "(history_floor_message_id IS NULL) = (history_floor_message_domain IS NULL)",
            name="bot_dm_history_floor_complete",
        ),
        UniqueConstraint(
            "conversation_id",
            "conversation_domain",
            "application_id",
            "application_domain",
            name="uq_bot_dm_grant_application_conversation",
        ),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            "conversation_id",
            "conversation_domain",
            name="uq_bot_dm_grants_id_application_conversation",
        ),
    )


class BotDMGrantConsent(Base):
    """One participant's consent to a bot joining a private MLS room."""

    __tablename__ = "bot_dm_grant_consents"
    grant_id: Mapped[int] = mapped_column(BigInteger)
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    consent_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("grant_id", "user_id", "user_domain"),
        ForeignKeyConstraint(["grant_id"], ["bot_dm_grants.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("consent_generation >= 1", name="bot_dm_consent_generation_positive"),
        CheckConstraint("status IN ('active','revoked')", name="bot_dm_consent_status_value"),
        Index("ix_bot_dm_grant_consents_user", "user_id", "user_domain", "status"),
    )


class BotE2EEDevice(Base, TimestampMixin):
    __tablename__ = "bot_e2ee_devices"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    source_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    protocol_id: Mapped[str] = mapped_column(String(64), nullable=False)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    worker_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    identity_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    credential: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    trust_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "application_domain"],
            ["bot_applications.id", "bot_applications.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["worker_id", "application_id", "application_domain"],
            [
                "bot_workers.id",
                "bot_workers.application_id",
                "bot_workers.application_domain",
            ],
            name="fk_bot_e2ee_devices_worker_application_lineage",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(source_id IS NULL) = (source_domain IS NULL)",
            name="bot_e2ee_device_source_ref_complete",
        ),
        CheckConstraint(
            "source_id IS NULL OR (source_id > 0 AND source_domain = application_domain)",
            name="bot_e2ee_device_source_authority",
        ),
        CheckConstraint(
            "protocol_id ~ '^kbe_[A-Za-z0-9_-]{43}$'",
            name="bot_e2ee_device_protocol_id_format",
        ),
        CheckConstraint("octet_length(identity_key) = 32", name="bot_e2ee_identity_key_length"),
        CheckConstraint(
            "octet_length(credential) BETWEEN 1 AND 16384",
            name="bot_e2ee_credential_length",
        ),
        CheckConstraint("jsonb_typeof(capabilities) = 'array'", name="bot_e2ee_capabilities_array"),
        CheckConstraint("generation >= 1", name="bot_e2ee_device_generation_positive"),
        CheckConstraint(
            "trust_state IN ('pending','trusted','rejected','revoked')",
            name="bot_e2ee_trust_state_value",
        ),
        Index(
            "uq_bot_e2ee_worker_active_device",
            "application_id",
            "application_domain",
            "worker_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        UniqueConstraint("source_id", "source_domain", name="uq_bot_e2ee_device_source"),
        UniqueConstraint("protocol_id", name="uq_bot_e2ee_device_protocol_id"),
        UniqueConstraint(
            "id",
            "application_id",
            "application_domain",
            name="uq_bot_e2ee_devices_id_application",
        ),
    )


class BotE2EEKeyPackage(Base):
    __tablename__ = "bot_e2ee_key_packages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cipher_suite: Mapped[str] = mapped_column(String(96), nullable=False)
    package: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    package_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_for_ref: Mapped[str | None] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        ForeignKeyConstraint(["device_id"], ["bot_e2ee_devices.id"], ondelete="CASCADE"),
        CheckConstraint(
            "octet_length(package) BETWEEN 1 AND 32768",
            name="bot_e2ee_key_package_length",
        ),
        CheckConstraint(
            "octet_length(package_hash) = 32",
            name="bot_e2ee_key_package_hash_length",
        ),
        CheckConstraint("expires_at > created_at", name="bot_e2ee_key_package_expiry"),
        CheckConstraint(
            "(claimed_at IS NULL) = (claimed_for_ref IS NULL)",
            name="bot_e2ee_key_package_claim_complete",
        ),
        Index(
            "ix_bot_e2ee_key_packages_available",
            "device_id",
            "expires_at",
            postgresql_where=text("claimed_at IS NULL"),
        ),
        UniqueConstraint("device_id", "package_hash", name="uq_bot_e2ee_key_package_digest"),
    )


class BotE2EEParticipation(Base, TimestampMixin):
    __tablename__ = "bot_e2ee_participations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    application_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    application_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    dm_grant_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    device_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consenting_actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consenting_actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    consent_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    joined_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    history_floor_message_id: Mapped[int | None] = mapped_column(BigInteger)
    history_floor_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "installation_id",
                "application_id",
                "application_domain",
                "guild_id",
                "guild_domain",
            ],
            [
                "bot_installations.id",
                "bot_installations.application_id",
                "bot_installations.application_domain",
                "bot_installations.guild_id",
                "bot_installations.guild_domain",
            ],
            name="fk_bot_e2ee_participations_installation_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "dm_grant_id",
                "application_id",
                "application_domain",
                "channel_id",
                "channel_domain",
            ],
            [
                "bot_dm_grants.id",
                "bot_dm_grants.application_id",
                "bot_dm_grants.application_domain",
                "bot_dm_grants.conversation_id",
                "bot_dm_grants.conversation_domain",
            ],
            name="fk_bot_e2ee_participations_dm_grant_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_bot_e2ee_participations_channel_guild_lineage",
        ),
        CheckConstraint(
            "(installation_id IS NOT NULL)::int + (dm_grant_id IS NOT NULL)::int = 1",
            name="bot_e2ee_participation_one_consent",
        ),
        CheckConstraint(
            "(installation_id IS NULL) = (guild_id IS NULL) "
            "AND (guild_id IS NULL) = (guild_domain IS NULL)",
            name="bot_e2ee_participation_guild_lineage",
        ),
        ForeignKeyConstraint(
            ["device_id", "application_id", "application_domain"],
            [
                "bot_e2ee_devices.id",
                "bot_e2ee_devices.application_id",
                "bot_e2ee_devices.application_domain",
            ],
            name="fk_bot_e2ee_participations_device_application_lineage",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["consenting_actor_id", "consenting_actor_domain"],
            ["users.id", "users.origin_domain"],
        ),
        CheckConstraint("consent_generation >= 1", name="bot_e2ee_consent_generation_positive"),
        CheckConstraint("joined_epoch >= 0", name="bot_e2ee_joined_epoch_nonnegative"),
        CheckConstraint(
            "(history_floor_message_id IS NULL) = (history_floor_message_domain IS NULL)",
            name="bot_e2ee_history_floor_complete",
        ),
        CheckConstraint(
            "status IN ('pending','active','revoked')",
            name="bot_e2ee_participation_status_value",
        ),
        UniqueConstraint(
            "installation_id",
            "channel_id",
            "channel_domain",
            "device_id",
            name="uq_bot_e2ee_installation_channel_device",
        ),
        UniqueConstraint(
            "dm_grant_id",
            "channel_id",
            "channel_domain",
            "device_id",
            name="uq_bot_e2ee_dm_grant_channel_device",
        ),
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
            "AND reporter_is_local IS NOT NULL) OR (source = 'photodna' AND reporter_id IS NULL "
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
            "uq_abuse_reports_federated_source_ref",
            text("(evidence ->> 'source_report_ref')"),
            unique=True,
            postgresql_where=text("source = 'user' AND reporter_is_local = false"),
        ),
        Index(
            "uq_abuse_reports_photodna_target",
            "source",
            "target_type",
            "target_ref",
            unique=True,
            postgresql_where=text("source = 'photodna'"),
        ),
    )
