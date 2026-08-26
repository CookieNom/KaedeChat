from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Text,
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
USERNAME_LENGTH = 32


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FederatedIdMixin:
    id: Mapped[int] = mapped_column(BigInteger)
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))


class Instance(Base, TimestampMixin):
    __tablename__ = "instances"
    domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), primary_key=True)
    is_self: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    display_name: Mapped[str | None] = mapped_column(String(100))
    software_version: Mapped[str | None] = mapped_column(String(40))
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    federation_mode: Mapped[str] = mapped_column(String(16), server_default="open")
    current_key_id: Mapped[str | None] = mapped_column(String(64))
    encrypted_private_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    private_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The peer whose signed state first required this cached namespace.  This
    # is accounting provenance, not delegated trust. Operator-created rows may
    # remain NULL until federation actually introduces an identity from them.
    federation_introduced_by_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    # Remote rows hold per-origin quota usage; the singleton self row holds the
    # exact instance-wide total and is the admission serialization point.
    federation_inbox_events: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    federation_inbox_event_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        UniqueConstraint("domain", "is_self", name="uq_instances_domain_is_self"),
        Index(
            "uq_instances_single_self",
            "is_self",
            unique=True,
            postgresql_where=text("is_self"),
        ),
        CheckConstraint(
            "NOT is_self OR (current_key_id IS NOT NULL AND encrypted_private_key IS NOT NULL "
            "AND private_key_nonce IS NOT NULL)",
            name="self_has_private_key",
        ),
        CheckConstraint("federation_mode IN ('open','allowlist')", name="federation_mode_value"),
        CheckConstraint("jsonb_typeof(capabilities) = 'array'", name="capabilities_are_array"),
        CheckConstraint(
            "is_self OR (encrypted_private_key IS NULL AND private_key_nonce IS NULL)",
            name="remote_has_no_private_key",
        ),
        CheckConstraint(
            "federation_inbox_events >= 0",
            name="nonnegative_federation_inbox_events",
        ),
        CheckConstraint(
            "federation_inbox_event_bytes >= 0",
            name="nonnegative_federation_inbox_event_bytes",
        ),
        CheckConstraint(
            "NOT is_self OR federation_introduced_by_domain IS NULL",
            name="self_has_no_federation_introducer",
        ),
        Index(
            "ix_instances_federation_introducer",
            "federation_introduced_by_domain",
            postgresql_where=text("NOT is_self AND federation_introduced_by_domain IS NOT NULL"),
        ),
    )


class PeerKey(Base):
    __tablename__ = "peer_keys"
    domain: Mapped[str] = mapped_column(
        String(DOMAIN_LENGTH), ForeignKey("instances.domain", ondelete="CASCADE")
    )
    key_id: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Local historical keys receive a retirement deadline when they stop being
    # current.  Remote keys may leave this unset because their authoritative
    # peer controls omission/retirement through discovery.
    retire_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("domain", "key_id"),
        CheckConstraint("octet_length(public_key) = 32", name="public_key_length"),
        CheckConstraint(
            "expired_at IS NULL OR expired_at >= fetched_at", name="expiry_after_fetch"
        ),
        CheckConstraint(
            "retire_after IS NULL OR retire_after >= fetched_at",
            name="retirement_after_fetch",
        ),
    )


class User(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "users"
    is_local: Mapped[bool] = mapped_column(Boolean, nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="human", server_default="human"
    )
    username: Mapped[str] = mapped_column(String(USERNAME_LENGTH), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    avatar_hash: Mapped[str | None] = mapped_column(String(128))
    banner_hash: Mapped[str | None] = mapped_column(String(128))
    bio: Mapped[str | None] = mapped_column(String(500))
    custom_status: Mapped[str | None] = mapped_column(String(128))
    password_hash: Mapped[str | None] = mapped_column(Text)
    password_kdf_version: Mapped[int | None] = mapped_column(SmallInteger)
    password_auth_salt: Mapped[bytes | None] = mapped_column(LargeBinary(16))
    e2ee_vault_salt: Mapped[bytes | None] = mapped_column(LargeBinary(16))
    email: Mapped[str | None] = mapped_column(String(320))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suspended_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_version: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    e2ee_device_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="0", nullable=False
    )
    # A reset fences portable-identity enrollment and replacement-vault writes
    # to the authenticated session that requested it until both artifacts are
    # durable. Only a digest of the one-time recovery bearer is retained; the
    # cleartext authorization is returned once to the client.
    e2ee_recovery_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    e2ee_recovery_session_id: Mapped[str | None] = mapped_column(String(64))
    e2ee_recovery_generation: Mapped[int | None] = mapped_column(BigInteger)
    e2ee_recovery_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_resolved: Mapped[bool] = mapped_column(Boolean, server_default=true(), nullable=False)
    # Charge a remote identity to the authenticated peer that introduced it
    # until the User row is physically garbage-collected. For a profile learned
    # from its own home this is simply ``origin_domain``.
    federation_introduced_by_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))

    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        UniqueConstraint("id", "origin_domain", "is_local", name="uq_users_ref_locality"),
        ForeignKeyConstraint(
            ["origin_domain", "is_local"],
            ["instances.domain", "instances.is_self"],
            name="fk_users_origin_locality_instances",
        ),
        CheckConstraint("username ~ '^[a-z0-9_.]{2,32}$'", name="username_format"),
        CheckConstraint("account_type IN ('human','bot')", name="account_type_value"),
        CheckConstraint(
            "NOT is_local OR account_type = 'bot' OR password_hash IS NOT NULL",
            name="local_auth_fields",
        ),
        CheckConstraint(
            "account_type = 'human' OR (password_hash IS NULL AND email IS NULL "
            "AND email_verified_at IS NULL AND totp_secret_encrypted IS NULL)",
            name="bot_has_no_human_auth_fields",
        ),
        CheckConstraint(
            "is_local OR (password_hash IS NULL AND email IS NULL "
            "AND email_verified_at IS NULL AND totp_secret_encrypted IS NULL)",
            name="remote_has_no_auth_fields",
        ),
        CheckConstraint("id >= 0", name="nonnegative_id"),
        CheckConstraint("profile_version >= 1", name="positive_profile_version"),
        CheckConstraint("e2ee_device_generation >= 0", name="e2ee_device_generation_nonnegative"),
        CheckConstraint(
            "e2ee_recovery_token_hash IS NULL OR octet_length(e2ee_recovery_token_hash) = 32",
            name="e2ee_recovery_token_hash_length",
        ),
        CheckConstraint(
            "e2ee_recovery_generation IS NULL OR e2ee_recovery_generation > 0",
            name="e2ee_recovery_generation_positive",
        ),
        CheckConstraint(
            "(e2ee_recovery_token_hash IS NULL AND "
            "e2ee_recovery_session_id IS NULL AND "
            "e2ee_recovery_generation IS NULL AND "
            "e2ee_recovery_expires_at IS NULL) OR "
            "(e2ee_recovery_token_hash IS NOT NULL AND "
            "e2ee_recovery_session_id IS NOT NULL AND "
            "e2ee_recovery_generation IS NOT NULL AND "
            "e2ee_recovery_expires_at IS NOT NULL)",
            name="e2ee_recovery_authorization_complete",
        ),
        CheckConstraint(
            "password_kdf_version IS NULL OR password_kdf_version = 2",
            name="password_kdf_version_value",
        ),
        CheckConstraint(
            "password_auth_salt IS NULL OR octet_length(password_auth_salt) = 16",
            name="password_auth_salt_length",
        ),
        CheckConstraint(
            "e2ee_vault_salt IS NULL OR octet_length(e2ee_vault_salt) = 16",
            name="e2ee_vault_salt_length",
        ),
        CheckConstraint(
            "(is_local AND account_type = 'human' AND password_kdf_version = 2 "
            "AND password_auth_salt IS NOT NULL AND e2ee_vault_salt IS NOT NULL) OR "
            "(NOT (is_local AND account_type = 'human') AND password_kdf_version IS NULL "
            "AND password_auth_salt IS NULL AND e2ee_vault_salt IS NULL)",
            name="password_kdf_fields_complete",
        ),
        CheckConstraint(
            "is_local OR profile_resolved OR username LIKE 'history_%'",
            name="unresolved_history_handle",
        ),
        CheckConstraint(
            "(is_local AND federation_introduced_by_domain IS NULL) OR "
            "(NOT is_local AND federation_introduced_by_domain IS NOT NULL)",
            name="federation_introducer_matches_locality",
        ),
        Index("uq_users_username_origin", func.lower(username), "origin_domain", unique=True),
        Index(
            "uq_users_local_email",
            func.lower(email),
            unique=True,
            postgresql_where=text("is_local AND email IS NOT NULL"),
        ),
        Index(
            "ix_users_unverified_created",
            "created_at",
            postgresql_where=text("is_local AND email_verified_at IS NULL"),
        ),
        Index(
            "ix_users_federation_introducer",
            "federation_introduced_by_domain",
            postgresql_where=text("NOT is_local"),
        ),
    )


class InstanceUserRestriction(Base, TimestampMixin):
    """Moderation state this instance owns for one remote human identity."""

    __tablename__ = "instance_user_restrictions"
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    restriction_type: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(500))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain"),
        # Deliberately no FK for the target: a remote profile row may be
        # garbage-collected after its final local membership is removed, but
        # the instance ban must survive a later reintroduction of that ref.
        ForeignKeyConstraint(
            ["actor_id", "actor_domain"],
            ["users.id", "users.origin_domain"],
        ),
        CheckConstraint(
            "(restriction_type = 'banned' AND expires_at IS NULL) OR "
            "(restriction_type = 'suspended' AND expires_at IS NOT NULL)",
            name="type_expiry_consistent",
        ),
        Index("ix_instance_user_restrictions_expiry", "expires_at"),
    )


class LocalUserMixin:
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_is_local: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )

    @staticmethod
    def locality_constraints(prefix: str) -> tuple[ForeignKeyConstraint, CheckConstraint]:
        return (
            ForeignKeyConstraint(
                ["user_id", "user_domain", "user_is_local"],
                ["users.id", "users.origin_domain", "users.is_local"],
                name=f"fk_{prefix}_local_user",
                ondelete="CASCADE",
            ),
            CheckConstraint("user_is_local", name=f"{prefix}_user_is_local"),
        )


class UserSettings(Base, LocalUserMixin, TimestampMixin):
    __tablename__ = "user_settings"
    locale: Mapped[str] = mapped_column(String(16), server_default="en-US")
    theme: Mapped[str] = mapped_column(String(16), server_default="system")
    dm_privacy: Mapped[str] = mapped_column(String(16), server_default="shared_guild")
    notification_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
    guild_navigation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=lambda: {"items": []}, server_default='{"items": []}'
    )
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain"),
        *LocalUserMixin.locality_constraints("user_settings"),
        CheckConstraint(
            "dm_privacy IN ('everyone','shared_guild','friends')", name="dm_privacy_value"
        ),
    )


class PushDevice(Base, LocalUserMixin, TimestampMixin):
    __tablename__ = "push_devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    transport: Mapped[str] = mapped_column(
        String(16), nullable=False, default="relay", server_default="relay"
    )
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), unique=True)
    token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    relay_origin: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    relay_subscription_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    relay_route_id: Mapped[str | None] = mapped_column(String(64))
    relay_wake_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    device_name: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        *LocalUserMixin.locality_constraints("push_devices"),
        CheckConstraint("platform IN ('android','ios')", name="platform_value"),
        CheckConstraint("transport IN ('relay','direct_fcm')", name="transport_value"),
        CheckConstraint(
            "token_hash IS NULL OR octet_length(token_hash) = 32", name="token_hash_length"
        ),
        CheckConstraint(
            "(transport = 'direct_fcm' AND token_hash IS NOT NULL AND token_encrypted IS NOT NULL "
            "AND relay_origin IS NULL AND relay_subscription_id IS NULL "
            "AND relay_route_id IS NULL AND relay_wake_secret_encrypted IS NULL) OR "
            "(transport = 'relay' AND token_hash IS NULL AND token_encrypted IS NULL "
            "AND relay_origin IS NOT NULL AND relay_subscription_id IS NOT NULL "
            "AND relay_route_id IS NOT NULL AND relay_wake_secret_encrypted IS NOT NULL)",
            name="transport_fields",
        ),
        Index("ix_push_devices_user", "user_id", "user_domain"),
    )


class PushWakeOutbox(Base):
    """A content-free wake waiting to be accepted durably by a relay."""

    __tablename__ = "push_wake_outbox"
    request_id: Mapped[str] = mapped_column(String(43), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("push_devices.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    event_token: Mapped[str | None] = mapped_column(String(43))
    delivery_id: Mapped[str] = mapped_column(String(43), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        UniqueConstraint(
            "device_id", "message_id", "message_domain", "kind", name="uq_push_wake_event"
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "kind IN ('direct_message','mention','guild_message',"
            "'call','moderation','relationship')",
            name="kind_value",
        ),
        Index("ix_push_wake_outbox_due", "next_attempt_at", "expires_at"),
    )


class PushRelaySubscription(Base):
    """Official/custom relay routing state with no Kaede account identifier."""

    __tablename__ = "push_relay_subscriptions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    grant_id: Mapped[str] = mapped_column(String(43), nullable=False, unique=True)
    home_origin: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    app_id: Mapped[str] = mapped_column(String(160), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    route_id: Mapped[str] = mapped_column(String(43), nullable=False)
    provider_token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    provider_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    management_secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("platform IN ('android','ios')", name="platform_value"),
        CheckConstraint("octet_length(provider_token_hash) = 32", name="token_hash_length"),
        CheckConstraint(
            "octet_length(management_secret_hash) = 32", name="management_secret_hash_length"
        ),
        Index("ix_push_relay_subscriptions_home", "home_origin", "enabled"),
        Index("ix_push_relay_subscriptions_token_hash", "provider_token_hash"),
    )


class PushRelayDelivery(Base):
    """Relay-side durable provider queue and idempotency outcome."""

    __tablename__ = "push_relay_deliveries"
    home_origin: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    request_id: Mapped[str] = mapped_column(String(43))
    subscription_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("push_relay_subscriptions.id", ondelete="CASCADE")
    )
    route_id: Mapped[str] = mapped_column(String(43), nullable=False)
    event_token: Mapped[str] = mapped_column(String(43), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(43), nullable=False)
    wake_mac: Mapped[str] = mapped_column(String(43), nullable=False)
    priority: Mapped[str] = mapped_column(String(12), nullable=False, server_default="normal")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        PrimaryKeyConstraint("home_origin", "request_id"),
        CheckConstraint("priority IN ('normal','urgent')", name="priority_value"),
        CheckConstraint("state IN ('pending','delivered','expired','invalid')", name="state_value"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        Index("ix_push_relay_deliveries_due", "state", "next_attempt_at", "expires_at"),
    )


class Relationship(Base, LocalUserMixin, TimestampMixin):
    __tablename__ = "relationships"
    target_id: Mapped[int] = mapped_column(BigInteger)
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain", "target_id", "target_domain"),
        *LocalUserMixin.locality_constraints("relationships"),
        ForeignKeyConstraint(
            ["target_id", "target_domain"], ["users.id", "users.origin_domain"], ondelete="CASCADE"
        ),
        CheckConstraint(
            "type IN ('friend','pending_in','pending_out','blocked')", name="relationship_type"
        ),
        CheckConstraint(
            "request_id IS NULL OR request_id ~ '^kcr_[A-Za-z0-9_-]{16,59}$'",
            name="relationship_request_id_format",
        ),
        CheckConstraint(
            "(user_id, user_domain) <> (target_id, target_domain)", name="not_self_relationship"
        ),
        Index(
            "ix_relationships_pending_in_recipient",
            "user_id",
            "user_domain",
            postgresql_where=text("type = 'pending_in'"),
        ),
        Index(
            "ix_relationships_pending_in_origin",
            "target_domain",
            postgresql_where=text("type = 'pending_in'"),
        ),
        Index(
            "ix_relationships_pending_in_recipient_origin",
            "user_id",
            "user_domain",
            "target_domain",
            postgresql_where=text("type = 'pending_in'"),
        ),
    )


class Session(Base, LocalUserMixin):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    refresh_token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    previous_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    device_name: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        *LocalUserMixin.locality_constraints("sessions"),
        CheckConstraint("expires_at <= absolute_expires_at", name="expiry_order"),
        Index("ix_sessions_refresh_token_hash", "refresh_token_hash"),
        Index(
            "ix_sessions_previous_token_hash",
            "previous_token_hash",
            postgresql_where=text("previous_token_hash IS NOT NULL"),
        ),
    )


class E2EEAccountVault(Base, LocalUserMixin):
    """Password-encrypted portable MLS state; the server never receives its key."""

    __tablename__ = "e2ee_account_vaults"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), primary_key=True)
    user_is_local: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    format_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="2")
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        *LocalUserMixin.locality_constraints("e2ee_account_vaults"),
        CheckConstraint("revision > 0", name="e2ee_account_vault_revision_positive"),
        CheckConstraint("format_version = 2", name="e2ee_account_vault_format_value"),
        CheckConstraint("octet_length(nonce) = 12", name="e2ee_account_vault_nonce_length"),
        CheckConstraint(
            "octet_length(ciphertext) BETWEEN 17 AND 33554448",
            name="e2ee_account_vault_ciphertext_length",
        ),
    )


class E2EEAccountVaultDigest(Base, LocalUserMixin):
    """Append-only compact commitment history for the opaque account vault."""

    __tablename__ = "e2ee_account_vault_digests"
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_is_local: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true(), nullable=False
    )
    digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        *LocalUserMixin.locality_constraints("e2ee_account_vault_digests"),
        CheckConstraint("revision > 0", name="e2ee_account_vault_digest_revision_positive"),
        CheckConstraint("octet_length(digest) = 32", name="e2ee_account_vault_digest_length"),
    )


class E2EEDevice(Base, LocalUserMixin):
    """A separately revocable cryptographic endpoint, not an auth session."""

    __tablename__ = "e2ee_devices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    identity_key: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    credential: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    capabilities: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    trust_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unverified", server_default="unverified"
    )
    registered_session_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="SET NULL")
    )
    device_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        *LocalUserMixin.locality_constraints("e2ee_devices"),
        UniqueConstraint(
            "user_id",
            "user_domain",
            "identity_key",
            name="uq_e2ee_devices_user_identity_key",
        ),
        UniqueConstraint(
            "user_id",
            "user_domain",
            "device_generation",
            name="uq_e2ee_devices_user_generation",
        ),
        UniqueConstraint(
            "id",
            "user_id",
            "user_domain",
            name="uq_e2ee_devices_ref_user",
        ),
        CheckConstraint("octet_length(identity_key) = 32", name="e2ee_identity_key_length"),
        CheckConstraint(
            "octet_length(credential) BETWEEN 1 AND 16384",
            name="e2ee_device_credential_length",
        ),
        CheckConstraint(
            "platform IN ('web','desktop','android','ios')",
            name="e2ee_device_platform_value",
        ),
        CheckConstraint(
            "trust_state IN ('unverified','verified','blocked')",
            name="e2ee_device_trust_state_value",
        ),
        CheckConstraint("device_generation > 0", name="e2ee_device_generation_positive"),
        CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name="e2ee_device_capabilities_array",
        ),
        Index("ix_e2ee_devices_user_active", "user_id", "user_domain", "revoked_at"),
    )


class E2EEKeyPackage(Base, LocalUserMixin):
    """A bounded, one-use MLS KeyPackage published for one local device."""

    __tablename__ = "e2ee_key_packages"
    id: Mapped[str] = mapped_column(String(43), primary_key=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cipher_suite: Mapped[str] = mapped_column(String(96), nullable=False)
    package_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by_id: Mapped[int | None] = mapped_column(BigInteger)
    claimed_by_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    claimed_operation_id: Mapped[str | None] = mapped_column(String(47))
    claimed_operation_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        *LocalUserMixin.locality_constraints("e2ee_key_packages"),
        ForeignKeyConstraint(
            ["device_id", "user_id", "user_domain"],
            ["e2ee_devices.id", "e2ee_devices.user_id", "e2ee_devices.user_domain"],
            name="fk_e2ee_key_packages_device_user",
            ondelete="CASCADE",
        ),
        UniqueConstraint("device_id", "id", name="uq_e2ee_key_package_device"),
        CheckConstraint(
            "octet_length(package_data) BETWEEN 1 AND 32768",
            name="e2ee_key_package_data_length",
        ),
        CheckConstraint(
            "(claimed_by_id IS NULL) = (claimed_by_domain IS NULL)",
            name="e2ee_key_package_claim_complete",
        ),
        CheckConstraint(
            "(claimed_at IS NULL) = (claimed_by_id IS NULL)",
            name="e2ee_key_package_claim_state",
        ),
        CheckConstraint(
            "(claimed_operation_id IS NULL) = (claimed_operation_domain IS NULL)",
            name="e2ee_key_package_operation_complete",
        ),
        CheckConstraint(
            "claimed_at IS NULL OR claimed_operation_id IS NOT NULL",
            name="e2ee_key_package_claim_has_operation",
        ),
        CheckConstraint(
            "claimed_operation_id IS NULL OR claimed_operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="e2ee_key_package_operation_id_format",
        ),
        CheckConstraint("expires_at > created_at", name="e2ee_key_package_positive_lifetime"),
        Index(
            "ix_e2ee_key_packages_available",
            "user_id",
            "user_domain",
            "device_id",
            "expires_at",
            postgresql_where=text("claimed_at IS NULL"),
        ),
        Index(
            "ix_e2ee_key_packages_operation",
            "claimed_operation_id",
            "claimed_operation_domain",
            postgresql_where=text("claimed_operation_id IS NOT NULL"),
        ),
    )


class E2EERoomOperation(Base, TimestampMixin):
    """Durable, idempotent MLS activation/rekey operation at room authority."""

    __tablename__ = "e2ee_room_operations"
    id: Mapped[str] = mapped_column(String(47), primary_key=True)
    authority_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    actor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    sender_device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="claiming", server_default="claiming"
    )
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    base_policy_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    group_id: Mapped[str] = mapped_column(String(43), nullable=False)
    participant_refs: Mapped[list[dict[str, str]]] = mapped_column(JSONB, nullable=False)
    key_packages: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    prepared_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    activation_request_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    prepared_vault_revision: Mapped[int | None] = mapped_column(BigInteger)
    prepared_vault_digest: Mapped[bytes | None] = mapped_column(LargeBinary(32))
    committed_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "kind IN ('activate','rekey')",
            name="e2ee_room_operation_kind_value",
        ),
        CheckConstraint(
            "id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="e2ee_room_operation_id_format",
        ),
        CheckConstraint(
            "sender_device_id ~ '^ked_[A-Za-z0-9_-]{43}$'",
            name="e2ee_room_operation_sender_device_id_format",
        ),
        CheckConstraint(
            "status IN ('claiming','prepared','committed','failed')",
            name="e2ee_room_operation_status_value",
        ),
        CheckConstraint(
            "octet_length(request_digest) = 32",
            name="e2ee_room_operation_request_digest_length",
        ),
        CheckConstraint(
            "activation_request_digest IS NULL OR octet_length(activation_request_digest) = 32",
            name="e2ee_room_operation_activation_digest_length",
        ),
        CheckConstraint(
            "prepared_vault_digest IS NULL OR octet_length(prepared_vault_digest) = 32",
            name="e2ee_room_operation_vault_digest_length",
        ),
        CheckConstraint(
            "base_policy_generation >= 0 AND policy_generation > base_policy_generation",
            name="e2ee_room_operation_generation_order",
        ),
        CheckConstraint(
            "char_length(group_id) = 43",
            name="e2ee_room_operation_group_id_length",
        ),
        CheckConstraint(
            "jsonb_typeof(participant_refs) = 'array' AND jsonb_typeof(key_packages) = 'array'",
            name="e2ee_room_operation_collections",
        ),
        CheckConstraint(
            "prepared_response IS NULL OR jsonb_typeof(prepared_response) = 'object'",
            name="e2ee_room_operation_prepared_response_object",
        ),
        CheckConstraint(
            "committed_response IS NULL OR jsonb_typeof(committed_response) = 'object'",
            name="e2ee_room_operation_committed_response_object",
        ),
        CheckConstraint(
            "(status <> 'prepared' OR prepared_response IS NOT NULL) AND "
            "(status <> 'committed' OR (prepared_response IS NOT NULL AND "
            "activation_request_digest IS NOT NULL AND prepared_vault_revision IS NOT NULL AND "
            "prepared_vault_revision > 0 AND prepared_vault_digest IS NOT NULL AND "
            "committed_response IS NOT NULL AND committed_at IS NOT NULL))",
            name="e2ee_room_operation_status_consistent",
        ),
        Index(
            "uq_e2ee_room_operations_active_channel",
            "channel_id",
            "channel_domain",
            unique=True,
            postgresql_where=text("status IN ('claiming','prepared')"),
        ),
        Index(
            "ix_e2ee_room_operations_actor",
            "actor_id",
            "actor_domain",
            "created_at",
        ),
    )


class E2EEPackageClaimBatch(Base):
    """Exact idempotent package-claim response retained on each target home."""

    __tablename__ = "e2ee_package_claim_batches"
    operation_id: Mapped[str] = mapped_column(String(47))
    operation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_id: Mapped[int] = mapped_column(BigInteger)
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_is_local: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    claimant_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claimant_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    excluded_device_id: Mapped[str | None] = mapped_column(String(64))
    max_devices: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    request_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "operation_id",
            "operation_domain",
            "target_id",
            "target_domain",
        ),
        ForeignKeyConstraint(
            ["target_id", "target_domain", "target_is_local"],
            ["users.id", "users.origin_domain", "users.is_local"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "target_is_local",
            name="e2ee_package_claim_batch_target_is_local",
        ),
        CheckConstraint(
            "operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="e2ee_package_claim_batch_operation_id_format",
        ),
        CheckConstraint(
            "octet_length(request_digest) = 32",
            name="e2ee_package_claim_batch_request_digest_length",
        ),
        CheckConstraint(
            "max_devices BETWEEN 1 AND 48",
            name="e2ee_package_claim_batch_max_devices",
        ),
        CheckConstraint(
            "jsonb_typeof(response) = 'object'",
            name="e2ee_package_claim_batch_response_object",
        ),
        Index(
            "ix_e2ee_package_claim_batches_channel",
            "channel_id",
            "channel_domain",
            "created_at",
        ),
    )


class OneTimeToken(Base, LocalUserMixin):
    __tablename__ = "one_time_tokens"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        *LocalUserMixin.locality_constraints("one_time_tokens"),
        CheckConstraint("expires_at > created_at", name="positive_lifetime"),
        Index("ix_one_time_tokens_purpose_expires", "purpose", "expires_at"),
    )


class EmailOutbox(Base):
    """Encrypted, durable delivery intent for a one-time-token email.

    Recipient addresses and message bodies deliberately live only inside the
    authenticated ciphertext.  The remaining columns are operational metadata
    that workers can use without exposing account PII or bearer credentials.
    """

    __tablename__ = "email_outbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    one_time_token_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("one_time_tokens.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_token: Mapped[str | None] = mapped_column(String(64))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','processing','retry','delivered','expired')",
            name="status_value",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        CheckConstraint(
            "octet_length(encrypted_payload) BETWEEN 29 AND 1048576",
            name="encrypted_payload_length",
        ),
        CheckConstraint("expires_at > created_at", name="positive_lifetime"),
        CheckConstraint(
            "(status = 'processing') = (claimed_at IS NOT NULL AND claim_token IS NOT NULL)",
            name="claim_state",
        ),
        CheckConstraint(
            "(status IN ('delivered','expired')) = (completed_at IS NOT NULL)",
            name="completion_state",
        ),
        Index(
            "ix_email_outbox_due",
            "next_attempt_at",
            "created_at",
            postgresql_where=text("status IN ('pending','retry')"),
        ),
        Index(
            "ix_email_outbox_stale_claim",
            "claimed_at",
            postgresql_where=text("status = 'processing'"),
        ),
        Index(
            "ix_email_outbox_terminal_retention",
            "completed_at",
            postgresql_where=text("status IN ('delivered','expired')"),
        ),
    )


class RecoveryCode(Base, LocalUserMixin):
    __tablename__ = "recovery_codes"
    code_hash: Mapped[bytes] = mapped_column(LargeBinary)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain", "code_hash"),
        *LocalUserMixin.locality_constraints("recovery_codes"),
    )


class AuthEvent(Base, LocalUserMixin):
    __tablename__ = "auth_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(500))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (*LocalUserMixin.locality_constraints("auth_events"),)


class Guild(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "guilds"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    icon_hash: Mapped[str | None] = mapped_column(String(128))
    banner_hash: Mapped[str | None] = mapped_column(String(128))
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    owner_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    next_event_seq: Mapped[int] = mapped_column(BigInteger, server_default="1", nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    sync_status: Mapped[str] = mapped_column(String(16), server_default="ready", nullable=False)
    permission_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="1", nullable=False
    )
    federated_history_policy: Mapped[str] = mapped_column(
        String(16), server_default="disabled", nullable=False
    )
    history_policy_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="1", nullable=False
    )
    snapshot_generation: Mapped[int] = mapped_column(BigInteger, server_default="1", nullable=False)
    sync_error_code: Mapped[str | None] = mapped_column(String(64))
    sync_error: Mapped[str | None] = mapped_column(String(500))
    unavailable: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        ForeignKeyConstraint(["owner_id", "owner_domain"], ["users.id", "users.origin_domain"]),
        ForeignKeyConstraint(
            ["id", "origin_domain", "owner_id", "owner_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_guilds_owner_membership",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint(
            "sync_status IN ('ready','syncing','stale','failed','quota_paused')",
            name="sync_status",
        ),
        CheckConstraint(
            "next_event_seq >= 1 AND last_event_seq >= 0 AND last_event_seq < next_event_seq",
            name="event_sequence_order",
        ),
        CheckConstraint("permission_generation >= 1", name="positive_permission_generation"),
        CheckConstraint(
            "federated_history_policy IN ('disabled','full_retained')",
            name="federated_history_policy_value",
        ),
        CheckConstraint(
            "history_policy_generation >= 1", name="positive_history_policy_generation"
        ),
        CheckConstraint("snapshot_generation >= 1", name="positive_snapshot_generation"),
        CheckConstraint(
            "sync_status IN ('failed','quota_paused') OR sync_error_code IS NULL",
            name="sync_error_requires_failure",
        ),
    )


class FederationReplicaUsage(Base, TimestampMixin):
    """Trigger-maintained database footprint for one remote guild replica."""

    __tablename__ = "federation_replica_usage"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    message_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    message_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    reaction_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    reaction_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    member_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    member_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    attachment_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    attachment_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    projection_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    projection_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    structural_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    structural_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    tracker_rows: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    tracker_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    total_rows: Mapped[int] = mapped_column(
        BigInteger,
        Computed(
            "message_rows + reaction_rows + member_rows + attachment_rows + "
            "projection_rows + structural_rows + tracker_rows",
            persisted=True,
        ),
    )
    total_bytes: Mapped[int] = mapped_column(
        BigInteger,
        Computed(
            "message_bytes + reaction_bytes + member_bytes + attachment_bytes + "
            "projection_bytes + structural_bytes + tracker_bytes",
            persisted=True,
        ),
    )
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "message_rows >= 0 AND reaction_rows >= 0 AND member_rows >= 0 "
            "AND attachment_rows >= 0 AND projection_rows >= 0 AND structural_rows >= 0 "
            "AND tracker_rows >= 0",
            name="nonnegative_rows",
        ),
        CheckConstraint(
            "message_bytes >= 0 AND reaction_bytes >= 0 AND member_bytes >= 0 "
            "AND attachment_bytes >= 0 AND projection_bytes >= 0 "
            "AND structural_bytes >= 0 AND tracker_bytes >= 0",
            name="nonnegative_bytes",
        ),
        Index("ix_federation_replica_usage_origin", "guild_domain"),
    )


class GuildEvent(Base):
    __tablename__ = "guild_events"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    seq: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[str] = mapped_column(String(64))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain", "seq"),
        UniqueConstraint("guild_domain", "event_id", name="uq_guild_events_guild_domain_event_id"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        CheckConstraint("seq >= 1", name="positive_sequence"),
        Index("ix_guild_events_created_at", "created_at"),
    )


class GuildHistoryExport(Base):
    """Authority-side, short-lived permission-bound history grant."""

    __tablename__ = "guild_history_exports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    requester_origin: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    requester_user_id: Mapped[int] = mapped_column(BigInteger)
    requester_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    requester_member_version: Mapped[int] = mapped_column(BigInteger)
    baseline_seq: Mapped[int] = mapped_column(BigInteger)
    permission_generation: Mapped[int] = mapped_column(BigInteger)
    history_policy_generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default="active", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["requester_origin"], ["instances.domain"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["requester_user_id", "requester_user_domain"],
            ["users.id", "users.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("requester_origin = requester_user_domain", name="requester_origin_match"),
        CheckConstraint("baseline_seq >= 0", name="nonnegative_baseline_seq"),
        CheckConstraint("requester_member_version >= 1", name="positive_member_version"),
        CheckConstraint("permission_generation >= 1", name="positive_permission_generation"),
        CheckConstraint(
            "history_policy_generation >= 1", name="positive_history_policy_generation"
        ),
        CheckConstraint(
            "status IN ('active','completed','revoked','expired','failed')",
            name="status_value",
        ),
        CheckConstraint("expires_at > created_at", name="positive_expiry"),
        Index(
            "ix_guild_history_exports_active",
            "guild_id",
            "guild_domain",
            "requester_origin",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )


class GuildHistoryExportChannel(Base):
    __tablename__ = "guild_history_export_channels"
    export_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    upper_bound_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("export_id", "channel_id", "channel_domain"),
        ForeignKeyConstraint(["export_id"], ["guild_history_exports.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("upper_bound_id >= 0", name="nonnegative_upper_bound"),
    )


class GuildMember(Base, TimestampMixin):
    __tablename__ = "guild_members"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    nickname: Mapped[str | None] = mapped_column(String(100))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeout_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timeout_indefinite: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    timeout_reason: Mapped[str | None] = mapped_column(String(512))
    voice_flags: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    member_version: Mapped[int] = mapped_column(BigInteger, server_default="1", nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("voice_flags >= 0", name="nonnegative_voice_flags"),
        CheckConstraint("member_version >= 1", name="positive_member_version"),
        Index("ix_guild_members_user", "user_id", "user_domain"),
    )


class RemoteGuildMembershipIntent(Base, LocalUserMixin, TimestampMixin):
    """Local authority over rejoining a guild hosted by another instance.

    This row deliberately has no foreign key to ``guilds``.  A replica can be
    purged after its final local member leaves. Missing intent is fail-closed
    for a new membership, while short-lived rows correlate explicit joins and
    guard delayed snapshots until bounded retention removes them.
    """

    __tablename__ = "remote_guild_membership_intents"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    state: Mapped[str] = mapped_column(
        String(16), default="departed", server_default="departed", nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain", "user_id", "user_domain"),
        *LocalUserMixin.locality_constraints("remote_guild_membership_intents"),
        CheckConstraint("guild_id >= 0", name="nonnegative_guild_id"),
        CheckConstraint("guild_domain <> user_domain", name="guild_is_remote_from_local_user"),
        CheckConstraint("state IN ('departed','joining')", name="state_value"),
        Index(
            "ix_remote_guild_membership_intents_user",
            "user_id",
            "user_domain",
        ),
    )


class GuildNotificationSetting(Base, LocalUserMixin, TimestampMixin):
    __tablename__ = "guild_notification_settings"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    level: Mapped[str] = mapped_column(
        String(16), default="mentions", server_default="mentions", nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain", "guild_id", "guild_domain"),
        *LocalUserMixin.locality_constraints("guild_notification_settings"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint("level IN ('all','mentions','none')", name="notification_level_value"),
    )


class Role(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "roles"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon_hash: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    permissions: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    hoist: Mapped[bool] = mapped_column(Boolean, server_default=false())
    mentionable: Mapped[bool] = mapped_column(Boolean, server_default=false())
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        UniqueConstraint(
            "id", "origin_domain", "guild_id", "guild_domain", name="uq_roles_ref_guild"
        ),
        CheckConstraint("origin_domain = guild_domain", name="origin_matches_guild"),
        CheckConstraint("position >= 0", name="nonnegative_position"),
        CheckConstraint("permissions >= 0", name="nonnegative_permissions"),
        CheckConstraint("(permissions & ~285982278599306495) = 0", name="known_permission_mask"),
        CheckConstraint("color BETWEEN 0 AND 16777215", name="color_range"),
        CheckConstraint(
            "icon_hash IS NULL OR char_length(icon_hash) = 64", name="icon_hash_length"
        ),
        Index("ix_roles_guild_position", "guild_id", "guild_domain", "position"),
    )


class MemberRole(Base):
    __tablename__ = "member_roles"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    role_id: Mapped[int] = mapped_column(BigInteger)
    role_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        PrimaryKeyConstraint(
            "guild_id", "guild_domain", "user_id", "user_domain", "role_id", "role_domain"
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["role_id", "role_domain", "guild_id", "guild_domain"],
            ["roles.id", "roles.origin_domain", "roles.guild_id", "roles.guild_domain"],
            ondelete="CASCADE",
        ),
    )


class Channel(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "channels"
    guild_id: Mapped[int | None] = mapped_column(BigInteger)
    guild_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    type: Mapped[int] = mapped_column(Integer, nullable=False)
    unavailable: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    name: Mapped[str | None] = mapped_column(String(100))
    # Forum guidelines may use Discord's 4096-character limit. API schemas
    # retain the 1024-character limit for channel types that do not support it.
    topic: Mapped[str | None] = mapped_column(String(4096))
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    parent_id: Mapped[int | None] = mapped_column(BigInteger)
    parent_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    permissions_synced: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), nullable=False
    )
    rate_limit_per_user: Mapped[int] = mapped_column(Integer, server_default="0")
    flags: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    archived: Mapped[bool | None] = mapped_column(Boolean)
    locked: Mapped[bool | None] = mapped_column(Boolean)
    invitable: Mapped[bool | None] = mapped_column(Boolean)
    auto_archive_duration: Mapped[int | None] = mapped_column(Integer)
    archive_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int | None] = mapped_column(Integer)
    total_message_sent: Mapped[int | None] = mapped_column(Integer)
    member_count: Mapped[int | None] = mapped_column(Integer)
    starter_message_id: Mapped[int | None] = mapped_column(BigInteger)
    starter_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    default_auto_archive_duration: Mapped[int | None] = mapped_column(Integer)
    default_thread_rate_limit_per_user: Mapped[int | None] = mapped_column(Integer)
    available_tags: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    applied_tag_ids: Mapped[list[str]] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    # PostgreSQL JSONB distinguishes SQL NULL from the JSON literal `null`.
    # Forum channels without a default reaction need SQL NULL so the database
    # object-shape constraint treats the optional value as absent.
    default_reaction_emoji: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    default_sort_order: Mapped[int | None] = mapped_column(SmallInteger)
    default_forum_layout: Mapped[int | None] = mapped_column(SmallInteger)
    e2ee_required: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    federated_history_policy: Mapped[str] = mapped_column(
        String(16), server_default="inherit", nullable=False
    )
    # Search is a channel-level policy, not something inferred from whichever
    # messages a replica currently has.  Future E2EE setup flips this to
    # ``e2ee`` before encrypted messages are accepted, which guarantees that
    # plaintext never enters an external search index for that channel.
    encryption_mode: Mapped[str] = mapped_column(
        String(16), server_default="plaintext", nullable=False
    )
    encryption_state: Mapped[str] = mapped_column(
        String(16), server_default="plaintext", nullable=False
    )
    encryption_policy_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="0", nullable=False
    )
    encryption_protocol: Mapped[str | None] = mapped_column(String(32))
    encryption_suite: Mapped[str | None] = mapped_column(String(96))
    encryption_group_id: Mapped[str | None] = mapped_column(String(128))
    encryption_epoch: Mapped[int | None] = mapped_column(BigInteger)
    encryption_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    last_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    last_thread_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    created_floor_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    dm_conversation_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("CASE WHEN type = 1 THEN id ELSE NULL END", persisted=True),
    )
    dm_conversation_domain: Mapped[str | None] = mapped_column(
        String(DOMAIN_LENGTH),
        Computed("CASE WHEN type = 1 THEN origin_domain ELSE NULL END", persisted=True),
    )
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        UniqueConstraint(
            "id",
            "origin_domain",
            "guild_id",
            "guild_domain",
            name="uq_channels_ref_guild",
        ),
        UniqueConstraint("id", "origin_domain", "type", name="uq_channels_ref_type"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["parent_id", "parent_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["parent_id", "parent_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_channels_parent_ref_guild",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["last_message_id", "last_message_domain", "id", "origin_domain"],
            [
                "messages.id",
                "messages.origin_domain",
                "messages.channel_id",
                "messages.channel_domain",
            ],
            name="fk_channels_last_message_ref_channel",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["last_thread_id", "last_thread_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_channels_last_thread_ref_guild",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["dm_conversation_id", "dm_conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            name="fk_channels_dm_conversation_identity",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("type IN (0,1,2,4,5,10,11,12,15,17)", name="channel_type"),
        CheckConstraint(
            "encryption_mode IN ('plaintext','e2ee')",
            name="channel_encryption_mode_value",
        ),
        CheckConstraint(
            "encryption_state IN "
            "('plaintext','legacy','proposed','activating','active','rekeying','failed')",
            name="channel_encryption_state_value",
        ),
        CheckConstraint(
            "encryption_policy_generation >= 0",
            name="channel_encryption_generation_nonnegative",
        ),
        CheckConstraint(
            "encryption_epoch IS NULL OR encryption_epoch >= 0",
            name="channel_encryption_epoch_nonnegative",
        ),
        CheckConstraint(
            "(encryption_policy_generation = 0 AND "
            "((encryption_state = 'plaintext' AND encryption_mode = 'plaintext') OR "
            "(encryption_state = 'legacy' AND encryption_mode = 'e2ee')) AND "
            "encryption_protocol IS NULL AND encryption_suite IS NULL AND "
            "encryption_group_id IS NULL AND encryption_epoch IS NULL) OR "
            "(encryption_policy_generation > 0 AND encryption_protocol = 'mls10' AND "
            "encryption_suite = "
            "'MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519' AND "
            "encryption_group_id IS NOT NULL AND "
            "((encryption_state IN ('proposed','failed') AND "
            "encryption_mode = 'plaintext' AND encryption_epoch IS NULL) OR "
            "(encryption_state IN ('activating','active','rekeying','failed') AND "
            "encryption_mode = 'e2ee' AND encryption_epoch IS NOT NULL)))",
            name="channel_encryption_policy_consistent",
        ),
        CheckConstraint("(guild_id IS NULL) = (guild_domain IS NULL)", name="guild_ref_complete"),
        CheckConstraint(
            "(parent_id IS NULL) = (parent_domain IS NULL)", name="parent_ref_complete"
        ),
        CheckConstraint("(owner_id IS NULL) = (owner_domain IS NULL)", name="owner_ref_complete"),
        CheckConstraint(
            "(starter_message_id IS NULL) = (starter_message_domain IS NULL)",
            name="starter_message_ref_complete",
        ),
        CheckConstraint("parent_id IS NULL OR guild_id IS NOT NULL", name="parent_requires_guild"),
        CheckConstraint("(type = 1) = (guild_id IS NULL)", name="dm_type_matches_guild"),
        CheckConstraint(
            "guild_id IS NULL OR origin_domain = guild_domain", name="origin_matches_guild"
        ),
        CheckConstraint(
            "parent_id IS NULL OR (parent_id, parent_domain) <> (id, origin_domain)",
            name="parent_not_self",
        ),
        CheckConstraint(
            "NOT permissions_synced OR (parent_id IS NOT NULL AND type <> 4)",
            name="permission_sync_requires_parent",
        ),
        CheckConstraint(
            "type NOT IN (10,11,12) OR unavailable OR "
            "(parent_id IS NOT NULL AND NOT permissions_synced)",
            name="thread_requires_unsynced_parent",
        ),
        CheckConstraint(
            "(type IN (10,11,12) AND owner_id IS NOT NULL AND archived IS NOT NULL "
            "AND locked IS NOT NULL AND auto_archive_duration IS NOT NULL "
            "AND archive_timestamp IS NOT NULL AND last_activity_at IS NOT NULL "
            "AND message_count IS NOT NULL "
            "AND total_message_sent IS NOT NULL "
            "AND member_count IS NOT NULL) OR "
            "(type NOT IN (10,11,12) AND owner_id IS NULL AND archived IS NULL "
            "AND locked IS NULL AND auto_archive_duration IS NULL "
            "AND archive_timestamp IS NULL AND last_activity_at IS NULL "
            "AND message_count IS NULL "
            "AND total_message_sent IS NULL "
            "AND member_count IS NULL)",
            name="thread_metadata_context",
        ),
        CheckConstraint(
            "(type = 12 AND invitable IS NOT NULL) OR (type <> 12 AND invitable IS NULL)",
            name="private_thread_invitable_context",
        ),
        CheckConstraint(
            "auto_archive_duration IS NULL OR auto_archive_duration IN (60,1440,4320,10080)",
            name="auto_archive_duration_value",
        ),
        CheckConstraint(
            "message_count IS NULL OR message_count >= 0",
            name="nonnegative_message_count",
        ),
        CheckConstraint(
            "total_message_sent IS NULL OR total_message_sent >= 0",
            name="nonnegative_total_message_sent",
        ),
        CheckConstraint(
            "member_count IS NULL OR member_count >= 0",
            name="nonnegative_member_count",
        ),
        CheckConstraint(
            "(type = 15 AND default_auto_archive_duration IS NOT NULL "
            "AND default_thread_rate_limit_per_user IS NOT NULL "
            "AND default_forum_layout IS NOT NULL) OR "
            "(type <> 15 AND default_reaction_emoji IS NULL AND default_sort_order IS NULL "
            "AND default_forum_layout IS NULL AND available_tags = '[]'::jsonb)",
            name="forum_metadata_context",
        ),
        CheckConstraint(
            "type IN (0,5,15) OR default_auto_archive_duration IS NULL",
            name="default_auto_archive_duration_context",
        ),
        CheckConstraint(
            "type IN (0,15) OR default_thread_rate_limit_per_user IS NULL",
            name="default_thread_rate_context",
        ),
        CheckConstraint(
            "default_auto_archive_duration IS NULL OR "
            "default_auto_archive_duration IN (60,1440,4320,10080)",
            name="default_auto_archive_duration_value",
        ),
        CheckConstraint(
            "default_thread_rate_limit_per_user IS NULL OR "
            "default_thread_rate_limit_per_user BETWEEN 0 AND 21600",
            name="default_thread_rate_limit_range",
        ),
        CheckConstraint(
            "jsonb_typeof(available_tags) = 'array' AND jsonb_array_length(available_tags) <= 20",
            name="available_tags_value",
        ),
        CheckConstraint(
            "jsonb_typeof(applied_tag_ids) = 'array' AND jsonb_array_length(applied_tag_ids) <= 5",
            name="applied_tag_ids_value",
        ),
        CheckConstraint(
            "type IN (10,11,12) OR applied_tag_ids = '[]'::jsonb",
            name="applied_tags_thread_only",
        ),
        CheckConstraint(
            "default_reaction_emoji IS NULL OR jsonb_typeof(default_reaction_emoji) = 'object'",
            name="default_reaction_emoji_object",
        ),
        CheckConstraint(
            "default_sort_order IS NULL OR default_sort_order IN (0,1)",
            name="default_sort_order_value",
        ),
        CheckConstraint(
            "default_forum_layout IS NULL OR default_forum_layout IN (0,1,2)",
            name="default_forum_layout_value",
        ),
        CheckConstraint(
            "type IN (10,11,12,15) OR NOT e2ee_required",
            name="e2ee_required_context",
        ),
        CheckConstraint("position >= 0", name="nonnegative_position"),
        CheckConstraint("flags >= 0", name="nonnegative_flags"),
        CheckConstraint("rate_limit_per_user BETWEEN 0 AND 21600", name="rate_limit_range"),
        CheckConstraint(
            "federated_history_policy IN ('inherit','disabled','full_retained')",
            name="federated_history_policy_value",
        ),
        CheckConstraint("created_floor_id >= 0", name="nonnegative_created_floor"),
        CheckConstraint(
            "(last_message_id IS NULL) = (last_message_domain IS NULL)",
            name="last_message_ref_complete",
        ),
        CheckConstraint(
            "(last_thread_id IS NULL) = (last_thread_domain IS NULL)",
            name="last_thread_ref_complete",
        ),
        CheckConstraint(
            "type = 15 OR last_thread_id IS NULL",
            name="last_thread_forum_only",
        ),
        Index("ix_channels_guild_position", "guild_id", "guild_domain", "position"),
        Index(
            "ix_channels_parent_activity",
            "parent_id",
            "parent_domain",
            "archived",
            "last_activity_at",
            postgresql_where=text("type IN (10,11,12)"),
        ),
        Index(
            "ix_channels_thread_archive_due",
            "last_activity_at",
            postgresql_where=text("type IN (10,11,12) AND NOT archived"),
        ),
        Index(
            "ix_channels_parent_archive",
            "parent_id",
            "parent_domain",
            "archived",
            "archive_timestamp",
            postgresql_where=text("type IN (10,11,12)"),
        ),
        Index(
            "uq_channels_thread_starter_message",
            "starter_message_id",
            "starter_message_domain",
            unique=True,
            postgresql_where=text("type IN (10,11,12) AND starter_message_id IS NOT NULL"),
        ),
    )


class TrackerBoard(Base, TimestampMixin):
    __tablename__ = "tracker_boards"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_type: Mapped[int] = mapped_column(Integer, server_default="17", nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    key_prefix: Mapped[str] = mapped_column(String(10), nullable=False)
    next_task_number: Mapped[int] = mapped_column(BigInteger, server_default="1", nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("channel_id", "channel_domain"),
        UniqueConstraint(
            "channel_id",
            "channel_domain",
            "guild_id",
            "guild_domain",
            name="uq_tracker_boards_ref_guild",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_tracker_boards_channel_ref_guild",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "channel_type"],
            ["channels.id", "channels.origin_domain", "channels.type"],
            name="fk_tracker_boards_channel_ref_type",
            ondelete="CASCADE",
        ),
        CheckConstraint("channel_type = 17", name="channel_type"),
        CheckConstraint("channel_domain = guild_domain", name="origin_matches_guild"),
        CheckConstraint("key_prefix ~ '^[A-Z][A-Z0-9]{1,9}$'", name="key_prefix_format"),
        CheckConstraint("next_task_number >= 1", name="positive_next_task_number"),
    )


class TrackerDispatchOutbox(Base):
    """Committed tracker changes awaiting projection into the gateway stream."""

    __tablename__ = "tracker_dispatch_outbox"
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_dispatch_outbox_board_ref_guild",
            ondelete="CASCADE",
        ),
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint(
            "event_type IN ('TRACKER_BOARD_UPDATE','TRACKER_LANE_CREATE',"
            "'TRACKER_LANE_UPDATE','TRACKER_LANE_DELETE','TRACKER_TASK_CREATE',"
            "'TRACKER_TASK_UPDATE','TRACKER_TASK_DELETE')",
            name="event_type_value",
        ),
        Index("ix_tracker_dispatch_outbox_due", "next_attempt_at", "id"),
    )


class TrackerLane(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "tracker_lanes"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    color: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    kind: Mapped[str] = mapped_column(String(16), server_default="custom", nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        UniqueConstraint(
            "id",
            "origin_domain",
            "channel_id",
            "channel_domain",
            name="uq_tracker_lanes_ref_channel",
        ),
        UniqueConstraint(
            "channel_id",
            "channel_domain",
            "position",
            name="uq_tracker_lanes_channel_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_lanes_board_ref_guild",
            ondelete="CASCADE",
        ),
        CheckConstraint("origin_domain = channel_domain", name="origin_matches_channel"),
        CheckConstraint("channel_domain = guild_domain", name="channel_matches_guild"),
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("color BETWEEN 0 AND 16777215", name="color_range"),
        CheckConstraint(
            "kind IN ('backlog','planned','in_progress','completed','custom')",
            name="kind_value",
        ),
        CheckConstraint("position BETWEEN 0 AND 49", name="position_range"),
        Index("ix_tracker_lanes_channel_position", "channel_id", "channel_domain", "position"),
    )


class TrackerTask(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "tracker_tasks"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    lane_id: Mapped[int] = mapped_column(BigInteger)
    lane_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(8), server_default="none", nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    creator_id: Mapped[int] = mapped_column(BigInteger)
    creator_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    assignee_id: Mapped[int | None] = mapped_column(BigInteger)
    assignee_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    client_nonce: Mapped[str | None] = mapped_column(String(64))
    client_request_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        UniqueConstraint(
            "channel_id",
            "channel_domain",
            "number",
            name="uq_tracker_tasks_channel_number",
        ),
        UniqueConstraint(
            "channel_id",
            "channel_domain",
            "creator_id",
            "creator_domain",
            "client_nonce",
            name="uq_tracker_tasks_creator_nonce",
        ),
        UniqueConstraint(
            "channel_id",
            "channel_domain",
            "lane_id",
            "lane_domain",
            "position",
            name="uq_tracker_tasks_lane_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "tracker_boards.channel_id",
                "tracker_boards.channel_domain",
                "tracker_boards.guild_id",
                "tracker_boards.guild_domain",
            ],
            name="fk_tracker_tasks_board_ref_guild",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["lane_id", "lane_domain", "channel_id", "channel_domain"],
            [
                "tracker_lanes.id",
                "tracker_lanes.origin_domain",
                "tracker_lanes.channel_id",
                "tracker_lanes.channel_domain",
            ],
            name="fk_tracker_tasks_lane_ref_channel",
        ),
        ForeignKeyConstraint(
            ["creator_id", "creator_domain"],
            ["users.id", "users.origin_domain"],
            name="fk_tracker_tasks_creator",
        ),
        ForeignKeyConstraint(
            ["assignee_id", "assignee_domain"],
            ["users.id", "users.origin_domain"],
            name="fk_tracker_tasks_assignee",
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain", "assignee_id", "assignee_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_tracker_tasks_assignee_membership",
            # PostgreSQL 15+ can null only the optional identity columns while
            # retaining the task's required guild identity. Service cleanup
            # updates resource versions/events; this FK is the final invariant
            # for bulk, cascade, and future membership-removal paths.
            ondelete="SET NULL (assignee_id, assignee_domain)",
        ),
        CheckConstraint("origin_domain = channel_domain", name="origin_matches_channel"),
        CheckConstraint("channel_domain = guild_domain", name="channel_matches_guild"),
        CheckConstraint("lane_domain = channel_domain", name="lane_matches_channel"),
        CheckConstraint("number >= 1", name="positive_number"),
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 200", name="title_length"),
        CheckConstraint(
            "description IS NULL OR char_length(description) <= 10000",
            name="description_length",
        ),
        CheckConstraint(
            "priority IN ('none','low','medium','high','urgent')", name="priority_value"
        ),
        CheckConstraint("position BETWEEN 0 AND 4999", name="position_range"),
        CheckConstraint(
            "(assignee_id IS NULL) = (assignee_domain IS NULL)",
            name="assignee_ref_complete",
        ),
        CheckConstraint(
            "(client_nonce IS NULL) = (client_request_hash IS NULL)",
            name="client_idempotency_complete",
        ),
        CheckConstraint(
            "client_nonce IS NULL OR (char_length(client_nonce) BETWEEN 1 AND 64 "
            "AND client_nonce ~ '^[A-Za-z0-9._:-]+$')",
            name="client_nonce_format",
        ),
        CheckConstraint(
            "client_request_hash IS NULL OR char_length(client_request_hash) = 64",
            name="client_request_hash_length",
        ),
        Index(
            "ix_tracker_tasks_channel_lane_position",
            "channel_id",
            "channel_domain",
            "lane_id",
            "position",
        ),
        Index(
            "ix_tracker_tasks_channel_assignee",
            "channel_id",
            "channel_domain",
            "assignee_id",
            "assignee_domain",
        ),
        Index(
            "ix_tracker_tasks_channel_due",
            "channel_id",
            "channel_domain",
            "due_at",
            postgresql_where=text("due_at IS NOT NULL"),
        ),
    )


class ThreadMember(Base):
    __tablename__ = "thread_members"
    thread_id: Mapped[int] = mapped_column(BigInteger)
    thread_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    flags: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    notification_level: Mapped[str] = mapped_column(
        String(16), server_default="inherit", nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("thread_id", "thread_domain", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["thread_id", "thread_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_thread_members_thread_ref_guild",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain", "user_id", "user_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_thread_members_guild_member",
            ondelete="CASCADE",
        ),
        CheckConstraint("flags >= 0", name="nonnegative_flags"),
        CheckConstraint(
            "notification_level IN ('inherit','all','mentions','none')",
            name="notification_level_value",
        ),
        Index("ix_thread_members_user", "user_id", "user_domain", "thread_id"),
    )


class ChannelOverwrite(Base):
    __tablename__ = "channel_overwrites"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_id: Mapped[int] = mapped_column(BigInteger)
    target_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    target_type: Mapped[str] = mapped_column(String(8))
    role_target_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("CASE WHEN target_type = 'role' THEN target_id ELSE NULL END", persisted=True),
    )
    role_target_domain: Mapped[str | None] = mapped_column(
        String(DOMAIN_LENGTH),
        Computed(
            "CASE WHEN target_type = 'role' THEN target_domain ELSE NULL END",
            persisted=True,
        ),
    )
    member_target_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("CASE WHEN target_type = 'member' THEN target_id ELSE NULL END", persisted=True),
    )
    member_target_domain: Mapped[str | None] = mapped_column(
        String(DOMAIN_LENGTH),
        Computed(
            "CASE WHEN target_type = 'member' THEN target_domain ELSE NULL END",
            persisted=True,
        ),
    )
    allow: Mapped[int] = mapped_column(BigInteger, server_default="0")
    deny: Mapped[int] = mapped_column(BigInteger, server_default="0")
    __table_args__ = (
        PrimaryKeyConstraint(
            "channel_id", "channel_domain", "target_id", "target_domain", "target_type"
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_channel_overwrites_channel_ref_guild",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["role_target_id", "role_target_domain", "guild_id", "guild_domain"],
            ["roles.id", "roles.origin_domain", "roles.guild_id", "roles.guild_domain"],
            name="fk_channel_overwrites_role_target",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain", "member_target_id", "member_target_domain"],
            [
                "guild_members.guild_id",
                "guild_members.guild_domain",
                "guild_members.user_id",
                "guild_members.user_domain",
            ],
            name="fk_channel_overwrites_member_target",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        CheckConstraint("target_type IN ('role','member')", name="target_type"),
        CheckConstraint("allow >= 0 AND deny >= 0", name="nonnegative_masks"),
        CheckConstraint("(allow & deny) = 0", name="disjoint_masks"),
        CheckConstraint(
            "((allow | deny) & ~285982278599306495) = 0",
            name="known_permission_masks",
        ),
    )


class Message(Base, FederatedIdMixin):
    __tablename__ = "messages"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    content: Mapped[str | None] = mapped_column(Text)
    # SQL NULL means a plaintext message.  JSONB's default encodes Python None
    # as the JSON literal `null`, which is neither absent nor a valid envelope
    # and violates the object-only database invariant.
    e2ee: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    # Immutable policy metadata records the security boundary in force when
    # this body was admitted. It lets pre-activation history coexist with an
    # encrypted room without treating that history as retroactively protected.
    encryption_policy_generation: Mapped[int] = mapped_column(
        BigInteger, server_default="0", nullable=False
    )
    encryption_epoch: Mapped[int | None] = mapped_column(BigInteger)
    message_type: Mapped[int] = mapped_column(Integer, server_default="0")
    flags: Mapped[int] = mapped_column(Integer, server_default="0")
    client_nonce: Mapped[str | None] = mapped_column(String(64))
    referenced_message_id: Mapped[int | None] = mapped_column(BigInteger)
    referenced_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    mention_user_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    webhook_id: Mapped[int | None] = mapped_column(BigInteger)
    webhook_name: Mapped[str | None] = mapped_column(String(80))
    webhook_avatar_hash: Mapped[str | None] = mapped_column(String(128))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        UniqueConstraint(
            "id",
            "origin_domain",
            "channel_id",
            "channel_domain",
            name="uq_messages_ref_channel",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"], ["channels.id", "channels.origin_domain"]
        ),
        ForeignKeyConstraint(["author_id", "author_domain"], ["users.id", "users.origin_domain"]),
        ForeignKeyConstraint(["webhook_id"], ["webhooks.id"], ondelete="SET NULL"),
        # Reply references deliberately remain opaque after an older message
        # is evicted from a non-authoritative DM cache. Mutation paths validate
        # that a newly supplied reference belongs to this channel.
        CheckConstraint("id >= 0", name="nonnegative_id"),
        CheckConstraint(
            "(referenced_message_id IS NULL) = (referenced_message_domain IS NULL)",
            name="referenced_message_ref_complete",
        ),
        CheckConstraint("flags >= 0", name="nonnegative_flags"),
        CheckConstraint("content IS NULL OR char_length(content) <= 4000", name="content_length"),
        CheckConstraint(
            "deleted_at IS NULL OR content IS NULL", name="deleted_message_has_no_content"
        ),
        CheckConstraint("deleted_at IS NULL OR e2ee IS NULL", name="deleted_message_has_no_e2ee"),
        CheckConstraint("content IS NULL OR e2ee IS NULL", name="plaintext_or_e2ee"),
        CheckConstraint("e2ee IS NULL OR jsonb_typeof(e2ee) = 'object'", name="e2ee_is_object"),
        CheckConstraint(
            "encryption_policy_generation >= 0",
            name="message_encryption_generation_nonnegative",
        ),
        CheckConstraint(
            "encryption_epoch IS NULL OR encryption_epoch >= 0",
            name="message_encryption_epoch_nonnegative",
        ),
        CheckConstraint("jsonb_typeof(mention_user_refs) = 'array'", name="mentions_are_array"),
        Index("ix_messages_channel_id_desc", "channel_id", "channel_domain", "id"),
        Index("ix_messages_author_id_desc", "author_id", "author_domain", "id"),
        Index("ix_messages_id_brin", "id", postgresql_using="brin"),
        # PostgreSQL cannot enforce a cross-partition unique client nonce unless the
        # timestamp-bearing partition key is included. Federation inbox/outbox
        # idempotency is authoritative; this index keeps nonce reconciliation cheap.
        Index(
            "ix_messages_author_nonce",
            "author_id",
            "author_domain",
            "client_nonce",
            postgresql_where=text("client_nonce IS NOT NULL"),
        ),
        {"postgresql_partition_by": "RANGE (id)"},
    )


class E2EEControlRecord(Base, FederatedIdMixin):
    """Immutable sparse MLS controls retained independently of chat history."""

    __tablename__ = "e2ee_control_records"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    author_id: Mapped[int] = mapped_column(BigInteger)
    author_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    policy_generation: Mapped[int] = mapped_column(BigInteger)
    epoch: Mapped[int] = mapped_column(BigInteger)
    operation: Mapped[str] = mapped_column(String(16))
    apply_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    room_operation_id: Mapped[str | None] = mapped_column(String(47))
    room_operation_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("id >= 0", name="nonnegative_id"),
        CheckConstraint("policy_generation > 0", name="positive_policy_generation"),
        CheckConstraint("epoch >= 0", name="nonnegative_epoch"),
        CheckConstraint("operation IN ('welcome','commit')", name="operation_value"),
        CheckConstraint(
            "(operation = 'welcome' AND apply_mode IN ('join','audit')) OR "
            "(operation = 'commit' AND apply_mode IN ('process','audit'))",
            name="apply_mode_value",
        ),
        CheckConstraint(
            "(room_operation_id IS NULL) = (room_operation_domain IS NULL)",
            name="room_operation_ref_complete",
        ),
        CheckConstraint(
            "room_operation_id IS NULL OR room_operation_id ~ '^keo_[A-Za-z0-9_-]{43}$'",
            name="room_operation_id_format",
        ),
        CheckConstraint(
            "jsonb_typeof(envelope) = 'object' AND envelope->>'operation' = operation",
            name="envelope_matches_operation",
        ),
        Index(
            "ix_e2ee_control_records_channel_order",
            "channel_id",
            "channel_domain",
            "id",
            "origin_domain",
        ),
        UniqueConstraint(
            "room_operation_id",
            "room_operation_domain",
            "operation",
            name="uq_e2ee_control_records_room_operation_kind",
        ),
    )


class MessageProjection(Base):
    """Durable, idempotent post-commit cursor and mention projection work."""

    __tablename__ = "message_projections"
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    mention_user_refs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default="[]"
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "message_domain"),
        ForeignKeyConstraint(
            ["message_id", "message_domain", "channel_id", "channel_domain"],
            [
                "messages.id",
                "messages.origin_domain",
                "messages.channel_id",
                "messages.channel_domain",
            ],
            name="fk_message_projections_message_ref_channel",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("jsonb_typeof(mention_user_refs) = 'array'", name="mentions_are_array"),
        Index(
            "ix_message_projections_pending",
            "created_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )


class SearchIndexOutbox(Base):
    """Durable desired-state queue for the rebuildable message search index."""

    __tablename__ = "search_index_outbox"
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "message_domain"),
        CheckConstraint("attempts >= 0", name="search_index_outbox_attempts_nonnegative"),
        Index("ix_search_index_outbox_due", "next_attempt_at", "updated_at"),
    )


class SearchIndexState(Base):
    """Singleton cursor for resumable online search backfills."""

    __tablename__ = "search_index_state"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    reset_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    backfill_after_id: Mapped[int | None] = mapped_column(BigInteger)
    backfill_after_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    backfill_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint("id = 1", name="search_index_state_singleton"),
        CheckConstraint(
            "(backfill_after_id IS NULL) = (backfill_after_domain IS NULL)",
            name="search_index_state_cursor_complete",
        ),
    )


class GuildHistoryImport(Base):
    """Replica-side durable state for a resumable historical import."""

    __tablename__ = "guild_history_imports"
    export_id: Mapped[int] = mapped_column(BigInteger)
    export_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    requester_user_id: Mapped[int] = mapped_column(BigInteger)
    requester_user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    requester_member_version: Mapped[int] = mapped_column(BigInteger)
    baseline_seq: Mapped[int] = mapped_column(BigInteger)
    permission_generation: Mapped[int] = mapped_column(BigInteger)
    history_policy_generation: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default="pending", nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    remote_acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ack_error: Mapped[str | None] = mapped_column(String(500))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pages_downloaded: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    messages_downloaded: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    reactions_downloaded: Mapped[int] = mapped_column(
        BigInteger, server_default="0", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("export_id", "export_domain"),
        ForeignKeyConstraint(["export_domain"], ["instances.domain"], ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["requester_user_id", "requester_user_domain"],
            ["users.id", "users.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("export_domain = guild_domain", name="export_is_guild_home"),
        CheckConstraint("baseline_seq >= 0", name="nonnegative_baseline_seq"),
        CheckConstraint("requester_member_version >= 1", name="positive_member_version"),
        CheckConstraint("permission_generation >= 1", name="positive_permission_generation"),
        CheckConstraint(
            "history_policy_generation >= 1", name="positive_history_policy_generation"
        ),
        CheckConstraint(
            "status IN ('pending','downloading','reconciling','completed','revoked','failed')",
            name="status_value",
        ),
        CheckConstraint(
            "pages_downloaded >= 0 AND bytes_downloaded >= 0 "
            "AND messages_downloaded >= 0 AND reactions_downloaded >= 0",
            name="nonnegative_budgets",
        ),
        UniqueConstraint(
            "guild_id",
            "guild_domain",
            "requester_user_id",
            "requester_user_domain",
            "requester_member_version",
            "permission_generation",
            "history_policy_generation",
            name="uq_guild_history_imports_grant_generation",
        ),
        Index("ix_guild_history_imports_pending", "status", "updated_at"),
    )


class GuildHistoryImportChannel(Base):
    """Durable recent-first cursor for one channel in an import."""

    __tablename__ = "guild_history_import_channels"
    export_id: Mapped[int] = mapped_column(BigInteger)
    export_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    upper_bound_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_before_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    complete: Mapped[bool] = mapped_column(Boolean, server_default=false(), nullable=False)
    pages_downloaded: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    bytes_downloaded: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    messages_downloaded: Mapped[int] = mapped_column(BigInteger, server_default="0", nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("export_id", "export_domain", "channel_id", "channel_domain"),
        ForeignKeyConstraint(
            ["export_id", "export_domain"],
            ["guild_history_imports.export_id", "guild_history_imports.export_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("upper_bound_id >= 0 AND next_before_id >= 0", name="nonnegative_cursors"),
        CheckConstraint(
            "pages_downloaded >= 0 AND bytes_downloaded >= 0 AND messages_downloaded >= 0",
            name="nonnegative_budgets",
        ),
    )


class GuildHistoryStagedMessage(Base):
    __tablename__ = "guild_history_staged_messages"
    export_id: Mapped[int] = mapped_column(BigInteger)
    export_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("export_id", "export_domain", "message_id", "message_domain"),
        ForeignKeyConstraint(
            ["export_id", "export_domain"],
            ["guild_history_imports.export_id", "guild_history_imports.export_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        Index(
            "ix_guild_history_staged_messages_channel",
            "export_id",
            "export_domain",
            "channel_id",
            "message_id",
        ),
    )


class FederatedHistoryMessage(Base):
    """A local message whose initial copy arrived through a history export."""

    __tablename__ = "federated_history_messages"
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    export_id: Mapped[int] = mapped_column(BigInteger)
    export_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "message_domain"),
        ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["export_id", "export_domain"],
            ["guild_history_imports.export_id", "guild_history_imports.export_domain"],
            ondelete="RESTRICT",
        ),
        Index("ix_federated_history_messages_export", "export_id", "export_domain"),
    )


class Attachment(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "attachments"
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    report_id: Mapped[int | None] = mapped_column(BigInteger)
    uploader_id: Mapped[int] = mapped_column(BigInteger)
    uploader_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    bot_installation_id: Mapped[int | None] = mapped_column(BigInteger)
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    object_key: Mapped[str] = mapped_column(String(512))
    staging_object_key: Mapped[str | None] = mapped_column(String(512))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    blurhash: Mapped[str | None] = mapped_column(String(128))
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    perceptual_hash: Mapped[str | None] = mapped_column(String(64))
    detected_content_type: Mapped[str | None] = mapped_column(String(255))
    variants: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    # Immutable, server-validated presentation transform requested before the
    # upload is finalized. Public sticker derivatives are generated from this
    # recipe after malware and PhotoDNA checks pass.
    media_transform: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    purpose: Mapped[str] = mapped_column(String(24), server_default="attachment")
    asset_binding: Mapped[str | None] = mapped_column(String(600))
    scan_status: Mapped[str] = mapped_column(String(16), server_default="pending")
    encryption_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="plaintext"
    )
    encryption_protocol: Mapped[str | None] = mapped_column(String(32))
    upload_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["report_id"], ["abuse_reports.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["uploader_id", "uploader_domain"],
            ["users.id", "users.origin_domain"],
        ),
        ForeignKeyConstraint(
            ["bot_installation_id"],
            ["bot_installations.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(message_id IS NULL) = (message_domain IS NULL)",
            name="message_ref_complete",
        ),
        CheckConstraint(
            "report_id IS NULL OR "
            "(message_id IS NULL AND message_domain IS NULL AND encryption_mode = 'plaintext')",
            name="report_evidence_is_unbound_plaintext",
        ),
        CheckConstraint("size >= 0", name="nonnegative_size"),
        CheckConstraint(
            "(width IS NULL OR width > 0) AND (height IS NULL OR height > 0)",
            name="positive_dimensions",
        ),
        CheckConstraint(
            "scan_status IN "
            "('pending','clean','infected','failed','encrypted','quarantined','rejected')",
            name="scan_status",
        ),
        CheckConstraint(
            "encryption_mode IN ('plaintext','e2ee')",
            name="attachment_encryption_mode_value",
        ),
        CheckConstraint(
            "(encryption_mode = 'plaintext' AND encryption_protocol IS NULL AND "
            "scan_status <> 'encrypted') OR "
            "(encryption_mode = 'e2ee' AND encryption_protocol = 'kaede-file-v1' AND "
            "scan_status IN ('pending','failed','encrypted'))",
            name="attachment_encryption_policy_consistent",
        ),
        CheckConstraint(
            "purpose IN ('attachment','avatar','banner','guild_icon',"
            "'guild_banner','emoji','sticker','webhook_avatar','role_icon')",
            name="purpose_value",
        ),
        CheckConstraint(
            "media_transform IS NULL OR jsonb_typeof(media_transform) = 'object'",
            name="media_transform_object",
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR char_length(content_sha256) = 64",
            name="sha256_length",
        ),
        CheckConstraint("jsonb_typeof(variants) = 'object'", name="variants_object"),
        UniqueConstraint("asset_binding", name="uq_attachments_asset_binding"),
        Index("ix_attachments_report_id", "report_id"),
        Index(
            "ix_attachments_pending_gc",
            "upload_expires_at",
            postgresql_where=text("finalized_at IS NULL AND deleted_at IS NULL"),
        ),
        Index(
            "ix_attachments_staging_gc",
            "updated_at",
            postgresql_where=text("staging_object_key IS NOT NULL"),
        ),
        Index("ix_attachments_uploader_usage", "uploader_id", "uploader_domain"),
        Index("ix_attachments_bot_installation_usage", "bot_installation_id"),
        Index(
            "ix_attachments_live_message",
            "message_id",
            "message_domain",
            "id",
            postgresql_where=text("deleted_at IS NULL AND message_id IS NOT NULL"),
        ),
        Index(
            "ix_attachments_public_asset_hash",
            "content_sha256",
            postgresql_where=text("purpose <> 'attachment' AND scan_status = 'clean'"),
        ),
        Index(
            "ix_attachments_content_digest",
            "origin_domain",
            "content_sha256",
            # Status values are bound parameters in the hot capability and
            # binding probes. Key only on non-null digests so PostgreSQL can
            # use this index even after asyncpg selects a generic plan.
            postgresql_where=text("content_sha256 IS NOT NULL"),
        ),
    )


class Reaction(Base):
    __tablename__ = "reactions"
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    emoji_key: Mapped[str] = mapped_column(String(320))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "message_domain", "user_id", "user_domain", "emoji_key"),
        ForeignKeyConstraint(
            ["message_id", "message_domain"],
            ["messages.id", "messages.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("char_length(emoji_key) > 0", name="nonempty_emoji"),
    )


class Pin(Base):
    __tablename__ = "pins"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    pinned_by_id: Mapped[int] = mapped_column(BigInteger)
    pinned_by_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    pinned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("channel_id", "channel_domain", "message_id", "message_domain"),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["message_id", "message_domain", "channel_id", "channel_domain"],
            [
                "messages.id",
                "messages.origin_domain",
                "messages.channel_id",
                "messages.channel_domain",
            ],
            name="fk_pins_message_ref_channel",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["pinned_by_id", "pinned_by_domain"], ["users.id", "users.origin_domain"]
        ),
    )


class DMConversation(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "dm_conversations"
    pair_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(16), server_default="direct")
    authority_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    channel_type: Mapped[int] = mapped_column(Integer, server_default="1", nullable=False)
    history_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    history_truncated_before_id: Mapped[int | None] = mapped_column(BigInteger)
    history_truncated_before_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    history_cache_start_id: Mapped[int | None] = mapped_column(BigInteger)
    history_cache_start_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(["authority_domain"], ["instances.domain"]),
        ForeignKeyConstraint(
            ["owner_id", "owner_domain"],
            ["users.id", "users.origin_domain"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["id", "origin_domain", "channel_type"],
            ["channels.id", "channels.origin_domain", "channels.type"],
            name="fk_dm_conversations_channel_identity",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        CheckConstraint("type IN ('direct','group')", name="conversation_type"),
        CheckConstraint("channel_type = 1", name="channel_type"),
        CheckConstraint("origin_domain = authority_domain", name="origin_is_authority"),
        CheckConstraint("pair_key ~ '^[0-9a-f]{64}$'", name="pair_key_format"),
        CheckConstraint(
            "(type = 'direct' AND owner_id IS NULL AND owner_domain IS NULL) OR "
            "(type = 'group' AND owner_id IS NOT NULL AND owner_domain IS NOT NULL)",
            name="owner_matches_type",
        ),
        CheckConstraint(
            "(history_truncated_before_id IS NULL) = (history_truncated_before_domain IS NULL)",
            name="history_truncated_before_ref_complete",
        ),
        CheckConstraint(
            "(history_cache_start_id IS NULL) = (history_cache_start_domain IS NULL)",
            name="history_cache_start_ref_complete",
        ),
        CheckConstraint(
            "history_truncated OR history_truncated_before_id IS NULL",
            name="history_boundary_requires_truncation",
        ),
    )


class DMParticipant(Base):
    __tablename__ = "dm_participants"
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    conversation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("conversation_id", "conversation_domain", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
    )


class FederatedDMStorageUsage(Base, TimestampMixin):
    """Trigger-maintained high-water accounting for a cross-instance DM.

    Logical message deletion and metadata clearing do not release capacity.
    Charges leave this row only when the underlying SQL rows are physically
    deleted (or moved to another conversation), preventing delete/rewrite loops
    from evading retained-state admission.
    """

    __tablename__ = "federated_dm_storage_usage"
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    conversation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    authority_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    remote_origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), nullable=False)
    message_rows: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    message_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    attachment_rows: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    attachment_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    projection_rows: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    projection_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    total_rows: Mapped[int] = mapped_column(
        BigInteger,
        Computed("message_rows + attachment_rows + projection_rows", persisted=True),
    )
    total_bytes: Mapped[int] = mapped_column(
        BigInteger,
        Computed("message_bytes + attachment_bytes + projection_bytes", persisted=True),
    )
    __table_args__ = (
        PrimaryKeyConstraint("conversation_id", "conversation_domain"),
        ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            ["dm_conversations.id", "dm_conversations.origin_domain"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["authority_domain"], ["instances.domain"]),
        ForeignKeyConstraint(["remote_origin_domain"], ["instances.domain"]),
        CheckConstraint(
            "message_rows >= 0 AND attachment_rows >= 0 AND projection_rows >= 0",
            name="nonnegative_rows",
        ),
        CheckConstraint(
            "message_bytes >= 0 AND attachment_bytes >= 0 AND projection_bytes >= 0",
            name="nonnegative_bytes",
        ),
        Index("ix_federated_dm_storage_usage_authority", "authority_domain"),
        Index("ix_federated_dm_storage_usage_remote_origin", "remote_origin_domain"),
    )


class FederatedDMRowCharge(Base):
    """Per-row retained charge used to release high-water usage exactly."""

    __tablename__ = "federated_dm_row_charges"
    table_name: Mapped[str] = mapped_column(String(32))
    row_id: Mapped[int] = mapped_column(BigInteger)
    row_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    conversation_id: Mapped[int] = mapped_column(BigInteger)
    conversation_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    charge_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (
        PrimaryKeyConstraint("table_name", "row_id", "row_domain"),
        ForeignKeyConstraint(
            ["conversation_id", "conversation_domain"],
            [
                "federated_dm_storage_usage.conversation_id",
                "federated_dm_storage_usage.conversation_domain",
            ],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "table_name IN ('messages','attachments','message_projections')",
            name="table_name",
        ),
        CheckConstraint(
            "category IN ('message','attachment','projection')",
            name="category",
        ),
        CheckConstraint("charge_bytes > 0", name="positive_charge"),
        Index(
            "ix_federated_dm_row_charges_conversation",
            "conversation_id",
            "conversation_domain",
        ),
    )


class ReadState(Base, LocalUserMixin):
    __tablename__ = "read_states"
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    last_message_id: Mapped[int | None] = mapped_column(BigInteger)
    last_message_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    mention_count: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain", "channel_id", "channel_domain"),
        *LocalUserMixin.locality_constraints("read_states"),
        CheckConstraint(
            "(last_message_id IS NULL) = (last_message_domain IS NULL)",
            name="last_message_ref_complete",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="CASCADE",
        ),
        # A read cursor is an ordering watermark, not ownership of the message
        # row. Keeping the opaque composite reference preserves unread state
        # when an older non-authoritative DM cache entry is evicted.
        CheckConstraint("mention_count >= 0", name="nonnegative_mentions"),
    )


class Invite(Base, TimestampMixin):
    __tablename__ = "invites"
    code: Mapped[str] = mapped_column(String(8), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    inviter_id: Mapped[int] = mapped_column(BigInteger)
    inviter_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    uses: Mapped[int] = mapped_column(Integer, server_default="0")
    max_uses: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["inviter_id", "inviter_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("char_length(code) = 8", name="code_length"),
        CheckConstraint(
            "(channel_id IS NULL) = (channel_domain IS NULL)", name="channel_ref_complete"
        ),
        CheckConstraint("uses >= 0", name="nonnegative_uses"),
        CheckConstraint("max_uses IS NULL OR max_uses > 0", name="positive_max_uses"),
        CheckConstraint("max_uses IS NULL OR uses <= max_uses", name="uses_within_limit"),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain"],
            ["channels.id", "channels.origin_domain"],
            ondelete="SET NULL",
        ),
        ForeignKeyConstraint(
            ["channel_id", "channel_domain", "guild_id", "guild_domain"],
            [
                "channels.id",
                "channels.origin_domain",
                "channels.guild_id",
                "channels.guild_domain",
            ],
            name="fk_invites_channel_ref_guild",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class Ban(Base):
    __tablename__ = "bans"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    user_id: Mapped[int] = mapped_column(BigInteger)
    user_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    reason: Mapped[str | None] = mapped_column(String(512))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain", "user_id", "user_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["user_id", "user_domain"], ["users.id", "users.origin_domain"]),
        ForeignKeyConstraint(["actor_id", "actor_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="expiry_after_creation"
        ),
        Index("ix_bans_expiry", "expires_at"),
    )


class GuildInstanceBan(Base):
    __tablename__ = "guild_instance_bans"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    instance_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    reason: Mapped[str | None] = mapped_column(String(512))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("guild_id", "guild_domain", "instance_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["instance_domain"], ["instances.domain"]),
        ForeignKeyConstraint(["actor_id", "actor_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint(
            "expires_at IS NULL OR expires_at > created_at", name="expiry_after_creation"
        ),
        Index("ix_guild_instance_bans_expiry", "expires_at"),
    )


class AuditLogEntry(Base):
    __tablename__ = "audit_log_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    action_type: Mapped[int] = mapped_column(Integer)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(512))
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["actor_id", "actor_domain"], ["users.id", "users.origin_domain"]),
        Index("ix_audit_log_guild_id_desc", "guild_id", "guild_domain", "id"),
    )


class Emoji(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "emojis"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    # Content-addressed public asset identity. Remote guild replicas keep this
    # value without copying the object into their own storage.
    media_hash: Mapped[str | None] = mapped_column(String(64))
    animated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    creator_id: Mapped[int] = mapped_column(BigInteger)
    creator_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["creator_id", "creator_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("origin_domain = guild_domain", name="origin_matches_guild"),
        UniqueConstraint("guild_id", "guild_domain", "name", name="uq_emojis_guild_name"),
        CheckConstraint(
            "media_hash IS NULL OR media_hash ~ '^[0-9a-f]{64}$'",
            name="media_hash_format",
        ),
    )


class Sticker(Base, FederatedIdMixin, TimestampMixin):
    __tablename__ = "stickers"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(String(100))
    media_hash: Mapped[str | None] = mapped_column(String(64))
    animated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    creator_id: Mapped[int] = mapped_column(BigInteger)
    creator_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    __table_args__ = (
        PrimaryKeyConstraint("id", "origin_domain"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"], ["guilds.id", "guilds.origin_domain"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(["creator_id", "creator_domain"], ["users.id", "users.origin_domain"]),
        CheckConstraint("origin_domain = guild_domain", name="origin_matches_guild"),
        UniqueConstraint("guild_id", "guild_domain", "name", name="uq_stickers_guild_name"),
        CheckConstraint(
            "media_hash IS NULL OR media_hash ~ '^[0-9a-f]{64}$'",
            name="media_hash_format",
        ),
    )


class Webhook(Base, TimestampMixin):
    __tablename__ = "webhooks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    channel_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    name: Mapped[str] = mapped_column(String(80))
    avatar_hash: Mapped[str | None] = mapped_column(String(128))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    creator_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_webhooks_token_hash"),
        ForeignKeyConstraint(
            ["guild_id", "guild_domain"],
            ["guilds.id", "guilds.origin_domain"],
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
            name="fk_webhooks_channel_ref_guild",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(["creator_id", "creator_domain"], ["users.id", "users.origin_domain"]),
    )


class FederationEvent(Base):
    __tablename__ = "federation_events"
    event_id: Mapped[str] = mapped_column(String(64))
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_type: Mapped[str] = mapped_column(String(100))
    envelope: Mapped[dict[str, Any]] = mapped_column(JSONB)
    envelope_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("origin_domain", "event_id"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="positive_retention"),
        CheckConstraint("envelope_bytes >= 0", name="nonnegative_envelope_bytes"),
    )


class FederationOutbox(Base):
    __tablename__ = "federation_outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    destination: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "destination",
            "event_origin_domain",
            "event_id",
            name="uq_federation_outbox_destination_event_ref",
        ),
        ForeignKeyConstraint(["destination"], ["instances.domain"]),
        ForeignKeyConstraint(
            ["event_origin_domain", "event_id"],
            ["federation_events.origin_domain", "federation_events.event_id"],
            name="fk_federation_outbox_event_ref",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('pending','retry','circuit','delivered','failed','expired')",
            name="status_value",
        ),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        Index("ix_federation_outbox_delivery", "destination", "status", "next_retry_at"),
    )


class FederationInbox(Base):
    __tablename__ = "federation_inbox"
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_id: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), server_default="received")
    result_code: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        PrimaryKeyConstraint("origin_domain", "event_id"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        CheckConstraint("status IN ('received','processed','rejected')", name="status_value"),
    )


class AttachmentFederationRecipient(Base):
    """Durable record of an instance that received or fetched local media.

    Access revocation may remove the membership/participant rows used for live
    fanout. This ledger intentionally survives those removals so a later
    terminal media verdict can still invalidate every prior replica/cache.
    """

    __tablename__ = "attachment_federation_recipients"
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    attachment_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    destination_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
        ),
        ForeignKeyConstraint(
            ["attachment_id", "attachment_domain"],
            ["attachments.id", "attachments.origin_domain"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "destination_domain <> attachment_domain",
            name="remote_destination",
        ),
        Index("ix_attachment_federation_recipients_destination", "destination_domain"),
    )


class MediaTombstoneSource(Base):
    """Local signing state retained independently of attachment/message rows."""

    __tablename__ = "media_tombstone_sources"
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    attachment_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    signer_id: Mapped[int] = mapped_column(BigInteger)
    signer_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_id: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint("attachment_id", "attachment_domain"),
        CheckConstraint("attachment_id >= 0", name="nonnegative_attachment_id"),
        CheckConstraint("signer_id >= 0", name="nonnegative_signer_id"),
        # Generation zero is retained only for authenticated media.delete
        # envelopes emitted before signed generations were introduced. Local
        # rollover rewrites those proofs as generation one.
        CheckConstraint("generation >= 0", name="nonnegative_generation"),
        Index("ix_media_tombstone_sources_key", "key_id"),
    )


class MediaTombstoneDestination(Base):
    """Replica route retained after attachment and membership rows disappear."""

    __tablename__ = "media_tombstone_destinations"
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    attachment_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    destination_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    room_kind: Mapped[str | None] = mapped_column(String(16))
    room_id: Mapped[int | None] = mapped_column(BigInteger)
    room_domain: Mapped[str | None] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "attachment_id",
            "attachment_domain",
            "destination_domain",
        ),
        ForeignKeyConstraint(["destination_domain"], ["instances.domain"]),
        CheckConstraint("attachment_id >= 0", name="nonnegative_attachment_id"),
        CheckConstraint(
            "destination_domain <> attachment_domain",
            name="remote_destination",
        ),
        CheckConstraint(
            "(room_kind IS NULL AND room_id IS NULL AND room_domain IS NULL) OR "
            "(room_kind IN ('guild','group_dm') AND room_id IS NOT NULL "
            "AND room_id >= 0 AND room_domain IS NOT NULL)",
            name="room_ref_complete",
        ),
        Index("ix_media_tombstone_destinations_destination", "destination_domain"),
        Index(
            "ix_media_tombstone_destinations_room",
            "room_kind",
            "room_id",
            "room_domain",
        ),
    )


class RoomFederationRecipient(Base):
    """Historical instance access retained until authoritative room deletion."""

    __tablename__ = "room_federation_recipients"
    room_kind: Mapped[str] = mapped_column(String(16))
    room_id: Mapped[int] = mapped_column(BigInteger)
    room_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    destination_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "room_kind",
            "room_id",
            "room_domain",
            "destination_domain",
        ),
        ForeignKeyConstraint(["destination_domain"], ["instances.domain"]),
        CheckConstraint("room_kind IN ('guild','group_dm')", name="room_kind"),
        CheckConstraint("room_id >= 0", name="nonnegative_room_id"),
        CheckConstraint("destination_domain <> room_domain", name="remote_destination"),
        Index("ix_room_federation_recipients_destination", "destination_domain"),
    )


class TerminalRoomDeletion(Base):
    """One durable terminal-room proof and acknowledgement per destination."""

    __tablename__ = "terminal_room_deletions"
    room_kind: Mapped[str] = mapped_column(String(16))
    room_id: Mapped[int] = mapped_column(BigInteger)
    room_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    destination_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    event_type: Mapped[str] = mapped_column(String(64))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    event_id: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "room_kind",
            "room_id",
            "room_domain",
            "destination_domain",
        ),
        ForeignKeyConstraint(["destination_domain"], ["instances.domain"]),
        CheckConstraint("room_kind IN ('guild','group_dm')", name="room_kind"),
        CheckConstraint("room_id >= 0", name="nonnegative_room_id"),
        CheckConstraint("actor_id >= 0", name="nonnegative_actor_id"),
        CheckConstraint("generation > 0", name="positive_generation"),
        CheckConstraint(
            "(room_kind = 'guild' AND event_type = 'guild.instance_access.revoked') OR "
            "(room_kind = 'group_dm' AND event_type = 'dm.group.state')",
            name="event_matches_room_kind",
        ),
        Index(
            "ix_terminal_room_deletions_pending_key",
            "room_domain",
            "key_id",
            postgresql_where=text("acknowledged_at IS NULL"),
        ),
        Index("ix_terminal_room_deletions_event", "room_domain", "event_id"),
    )


class GuildMediaDeletionRequest(Base):
    """Durable guild-authority request addressed to a remote media origin."""

    __tablename__ = "guild_media_deletion_requests"
    guild_id: Mapped[int] = mapped_column(BigInteger)
    guild_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    attachment_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    actor_id: Mapped[int] = mapped_column(BigInteger)
    actor_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(64))
    generation: Mapped[int] = mapped_column(BigInteger)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        PrimaryKeyConstraint(
            "guild_id",
            "guild_domain",
            "attachment_id",
            "attachment_domain",
        ),
        ForeignKeyConstraint(["attachment_domain"], ["instances.domain"]),
        CheckConstraint("guild_id >= 0", name="nonnegative_guild_id"),
        CheckConstraint("attachment_id >= 0", name="nonnegative_attachment_id"),
        CheckConstraint("message_id >= 0", name="nonnegative_message_id"),
        CheckConstraint("actor_id >= 0", name="nonnegative_actor_id"),
        CheckConstraint("generation > 0", name="positive_generation"),
        CheckConstraint("guild_domain = message_domain", name="message_at_guild_home"),
        CheckConstraint("attachment_domain <> guild_domain", name="remote_attachment_origin"),
        Index(
            "ix_guild_media_deletion_requests_pending_key",
            "guild_domain",
            "key_id",
            postgresql_where=text("acknowledged_at IS NULL"),
        ),
        Index(
            "ix_guild_media_deletion_requests_event",
            "guild_domain",
            "event_id",
        ),
    )


class RemoteMediaCache(Base):
    __tablename__ = "remote_media_cache"
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    variant: Mapped[str] = mapped_column(String(32))
    object_key: Mapped[str] = mapped_column(String(512))
    size: Mapped[int] = mapped_column(BigInteger)
    content_type: Mapped[str] = mapped_column(
        String(255), server_default="application/octet-stream"
    )
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(16), server_default="pending")
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        PrimaryKeyConstraint("origin_domain", "attachment_id", "variant"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"]),
        CheckConstraint("size >= 0", name="nonnegative_size"),
        CheckConstraint(
            "scan_status IN ('pending','clean','infected','failed')", name="scan_status"
        ),
        CheckConstraint(
            "content_sha256 IS NULL OR char_length(content_sha256) = 64",
            name="sha256_length",
        ),
        Index("ix_remote_media_cache_eviction", "last_accessed_at"),
    )


class RemoteMediaOrphan(Base):
    """A remote-cache object that is not safe to forget until deletion succeeds."""

    __tablename__ = "remote_media_orphans"
    object_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0", nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    next_retry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        CheckConstraint("size >= 0", name="nonnegative_size"),
        CheckConstraint("attempts >= 0", name="nonnegative_attempts"),
        Index("ix_remote_media_orphans_retry", "next_retry_at"),
    )


class RemoteMediaTombstone(Base):
    """Durable authority proof that remote attachment bytes must not be served."""

    __tablename__ = "remote_media_tombstones"
    origin_domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH))
    attachment_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[str] = mapped_column(String(64))
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now() + interval '30 days'"),
        nullable=False,
    )
    __table_args__ = (
        PrimaryKeyConstraint("origin_domain", "attachment_id"),
        ForeignKeyConstraint(["origin_domain"], ["instances.domain"], ondelete="CASCADE"),
        CheckConstraint("attachment_id >= 0", name="nonnegative_attachment_id"),
        Index("ix_remote_media_tombstones_expiry", "expires_at"),
    )


class UserStorageUsage(Base, LocalUserMixin):
    __tablename__ = "user_storage_usage"
    bytes_used: Mapped[int] = mapped_column(BigInteger, server_default="0")
    pending_bytes: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "user_domain"),
        *LocalUserMixin.locality_constraints("user_storage_usage"),
        CheckConstraint("bytes_used >= 0 AND pending_bytes >= 0", name="nonnegative_usage"),
    )


class InstanceBlock(Base):
    __tablename__ = "instance_blocks"
    domain: Mapped[str] = mapped_column(String(DOMAIN_LENGTH), primary_key=True)
    level: Mapped[str] = mapped_column(String(16))
    include_subdomains: Mapped[bool] = mapped_column(Boolean, server_default=true())
    reason: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint("level IN ('silence','suspend')", name="block_level"),)


# Keep extension models registered when callers import this canonical module.
from app.db import bot_models as bot_models  # noqa: E402, F401

ALL_MODEL_TABLES = tuple(Base.metadata.tables)
