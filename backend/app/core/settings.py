from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import os
import re
from functools import lru_cache
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
URLSAFE_BASE64_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")
FCM_AUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
HOST_RE = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$",
    re.IGNORECASE,
)
AUXILIARY_KAEDE_ENV = {
    "KAEDE_API_HOST_PORT",
    "KAEDE_CADDY_HOST_PORT",
    "KAEDE_DEV_HTTP_PORT",
    "KAEDE_DEV_HTTPS_PORT",
    "KAEDE_EDGE_SECRET",
    "KAEDE_GATEWAY_SECRET_KEY",
    "KAEDE_GENERATED_OUTPUT",
    "KAEDE_GRAFANA_HOST_PORT",
    "KAEDE_TURN_UDP_PORT",
    "KAEDE_VOICE_ENABLED",
}
HEADER_SECRET_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")


def valid_url_host(host: str | None) -> bool:
    if host is None:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return HOST_RE.fullmatch(host) is not None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAEDE_",
        env_file=".env",
        extra="forbid",
        case_sensitive=False,
    )

    def __init__(self, **values: Any) -> None:
        known = {f"KAEDE_{name.upper()}" for name in type(self).model_fields}
        unknown = sorted(
            name
            for name in os.environ
            if name.upper().startswith("KAEDE_") and name.upper() not in known | AUXILIARY_KAEDE_ENV
        )
        if unknown:
            raise ValueError(f"unknown Kaede environment settings: {', '.join(unknown)}")
        super().__init__(**values)

    # Instance and API
    domain: str
    environment: Literal["development", "test", "production"] = "production"
    service_role: Literal[
        "full", "api", "gateway", "worker", "scheduler", "migration", "preflight"
    ] = "full"
    secret_key: SecretStr
    api_workers: int = Field(default=4, ge=1, le=64)
    gateway_workers: int = Field(default=1, ge=1, le=64)
    gateway_identify_rate_per_second: int = Field(default=100, ge=1, le=10_000)
    gateway_identify_burst: int = Field(default=200, ge=1, le=20_000)
    gateway_identify_ip_rate_per_second: int = Field(default=5, ge=1, le=1000)
    gateway_identify_ip_burst: int = Field(default=10, ge=1, le=2000)
    gateway_warmup_max_rows: int = Field(default=50_000, ge=100, le=1_000_000)
    gateway_warmup_timeout_seconds: int = Field(default=60, ge=5, le=300)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    proxy_secret: SecretStr | None = None
    # New encrypted-room activation is a release gate. Disabling it never
    # downgrades or interrupts rooms that are already encrypted.
    e2ee_activation_enabled: bool = False

    # Persistence and queues
    database_url: SecretStr
    dragonfly_url: SecretStr

    # Message search. Meilisearch is an internal, rebuildable projection; SQL
    # and the federation protocol remain authoritative.
    search_enabled: bool = False
    search_url: str = "http://meilisearch:7700"
    search_master_key: SecretStr | None = None
    search_index_prefix: str = "kaede"
    search_request_timeout_seconds: int = Field(default=5, ge=1, le=30)
    search_batch_size: int = Field(default=250, ge=10, le=2_000)
    search_federation_timeout_seconds: int = Field(default=8, ge=2, le=30)

    # Federation
    federation_mode: Literal["open", "allowlist"] = "open"
    federation_clock_skew_seconds: int = Field(default=300, ge=30, le=900)
    federation_event_retention_days: int = Field(default=30, ge=7)
    federation_inbox_max_events_per_origin: int = Field(
        default=5_000_000, ge=1_000, le=1_000_000_000
    )
    federation_inbox_max_bytes_per_origin: int = Field(
        default=16 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    federation_inbox_max_events_total: int = Field(default=50_000_000, ge=1_000, le=1_000_000_000)
    federation_inbox_max_bytes_total: int = Field(
        default=160 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    # Discovery documents are fetched before a peer is trusted. Keep both the
    # number of cached peer identities and each peer's rotated key history
    # bounded so a collection of otherwise valid domains cannot grow these
    # tables forever.
    federation_max_remote_instances: int = Field(default=10_000, ge=100, le=1_000_000)
    federation_peer_key_history_limit: int = Field(default=512, ge=128, le=10_000)
    # Non-guild durable state is deliberately independent from the inbox and
    # remote-guild ledgers.  An authenticated peer must not be able to turn a
    # stream of valid relationship/DM/identity events into unbounded SQL state.
    federation_pending_relationships_per_recipient: int = Field(default=1_000, ge=10, le=100_000)
    federation_pending_relationships_per_recipient_origin: int = Field(
        default=100, ge=1, le=100_000
    )
    federation_pending_relationships_per_origin: int = Field(default=10_000, ge=10, le=1_000_000)
    federation_remote_users_per_introducer: int = Field(default=100_000, ge=100, le=10_000_000)
    federation_third_party_instances_per_introducer: int = Field(default=1_000, ge=10, le=100_000)
    federation_remote_media_tombstones_per_origin: int = Field(
        default=100_000, ge=1_000, le=10_000_000
    )
    federation_dm_max_conversations_per_authority: int = Field(
        default=1_000_000, ge=100, le=10_000_000
    )
    federation_dm_max_conversations_per_remote_origin: int = Field(
        default=100_000, ge=10, le=10_000_000
    )
    federation_dm_max_messages_per_conversation: int = Field(
        default=5_000_000, ge=100, le=100_000_000
    )
    federation_dm_max_messages_per_authority: int = Field(
        default=50_000_000, ge=100, le=1_000_000_000
    )
    federation_dm_max_messages_per_remote_origin: int = Field(
        default=10_000_000, ge=100, le=1_000_000_000
    )
    federation_dm_max_bytes_per_conversation: int = Field(
        default=32 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    federation_dm_max_bytes_per_authority: int = Field(
        default=320 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=10 * 1024 * 1024 * 1024 * 1024,
    )
    federation_dm_max_bytes_per_remote_origin: int = Field(
        default=64 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=10 * 1024 * 1024 * 1024 * 1024,
    )
    # A non-authoritative DM replica prunes oldest messages back to these
    # rolling-cache targets before the hard safety ceilings above are reached.
    # Authorities never prune message history to satisfy these cache targets.
    federation_dm_replica_cache_messages_per_conversation: int = Field(
        default=250_000, ge=100, le=100_000_000
    )
    federation_dm_replica_cache_bytes_per_conversation: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    federation_remote_media_inflight_bytes_per_origin: int = Field(
        default=256 * 1024 * 1024,
        ge=1024 * 1024,
        le=16 * 1024 * 1024 * 1024,
    )
    federation_remote_media_inflight_bytes_total: int = Field(
        default=512 * 1024 * 1024,
        ge=1024 * 1024,
        le=64 * 1024 * 1024 * 1024,
    )
    # Durable SQL state copied from remote guilds. These are high-water marks,
    # not history page budgets: live events and resumable imports share them.
    federation_replica_max_rows_per_guild: int = Field(
        default=20_000_000, ge=10_000, le=1_000_000_000
    )
    federation_replica_max_bytes_per_guild: int = Field(
        default=64 * 1024 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=10 * 1024 * 1024 * 1024 * 1024,
    )
    federation_replica_max_rows_per_origin: int = Field(
        default=100_000_000, ge=10_000, le=1_000_000_000
    )
    federation_replica_max_bytes_per_origin: int = Field(
        default=320 * 1024 * 1024 * 1024,
        ge=16 * 1024 * 1024,
        le=10 * 1024 * 1024 * 1024 * 1024,
    )
    # A remote profile can outlive the last replicated guild/DM row that
    # referenced it.  Keep a generous grace period for caches and delayed
    # events, then let the bounded federation retention job remove only
    # identities with no remaining durable references.
    federation_remote_identity_retention_days: int = Field(default=30, ge=7, le=3650)
    federation_remote_identity_gc_batch_size: int = Field(default=5_000, ge=100, le=50_000)
    federation_history_import_enabled: bool = True
    federation_history_export_ttl_minutes: int = Field(default=24 * 60, ge=10, le=7 * 24 * 60)
    federation_history_max_active_exports_per_origin: int = Field(default=1_000, ge=1, le=1_000_000)
    federation_history_max_active_exports_total: int = Field(default=10_000, ge=1, le=10_000_000)
    federation_history_max_active_channel_grants_per_origin: int = Field(
        default=100_000, ge=1, le=100_000_000
    )
    federation_history_max_active_channel_grants_total: int = Field(
        default=1_000_000, ge=1, le=1_000_000_000
    )
    federation_history_page_messages: int = Field(default=100, ge=10, le=500)
    federation_history_page_bytes: int = Field(default=512 * 1024, ge=64 * 1024, le=8 * 1024 * 1024)
    federation_history_max_messages: int = Field(default=2_000_000, ge=100, le=10_000_000)
    federation_history_max_bytes: int = Field(
        default=32 * 1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=100 * 1024 * 1024 * 1024,
    )
    federation_history_max_pages: int = Field(default=250_000, ge=10, le=1_000_000)
    federation_history_max_reactions: int = Field(default=10_000_000, ge=100, le=100_000_000)
    federation_history_max_duration_seconds: int = Field(default=7200, ge=60, le=24 * 3600)
    federation_history_merge_chunk_size: int = Field(default=500, ge=50, le=5_000)
    federation_peer_overrides: dict[str, str] = Field(default_factory=dict)
    federation_ca_file: str | None = None
    admin_token: SecretStr | None = None

    # Media
    media_max_attachment_bytes: int = Field(default=15 * 1024 * 1024, ge=1)
    media_user_quota_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=1)
    media_inflight_limit: int = Field(default=10, ge=1, le=100)
    media_inflight_quota_bytes: int = Field(default=500 * 1024 * 1024, ge=1)
    media_upload_ttl_seconds: int = Field(default=15 * 60, ge=60, le=3600)
    media_scan_enabled: bool = True
    media_storage_backend: Literal["garage", "s3"] = "garage"
    media_s3_endpoint: str = "http://garage:3900"
    media_public_base_url: str | None = None
    media_s3_region: str = "kaede"
    media_s3_addressing_style: Literal["path", "virtual"] = "path"
    media_s3_create_buckets: bool | None = None
    media_s3_init_timeout_seconds: int = Field(default=120, ge=1, le=900)
    media_s3_access_key: SecretStr | None = None
    media_s3_secret_key: SecretStr | None = None
    media_s3_session_token: SecretStr | None = None
    media_attachments_bucket: str = "kaede-attachments"
    media_derived_bucket: str = "kaede-derived"
    media_remote_cache_bucket: str = "kaede-remote-cache"
    media_clamav_host: str = "clamav"
    media_clamav_port: int = Field(default=3310, ge=1, le=65535)
    media_remote_cache_bytes: int = Field(default=100 * 1024 * 1024 * 1024, ge=1)
    media_remote_cache_ttl_days: int = Field(default=30, ge=1, le=365)
    media_retention_days: int | None = Field(default=None, ge=1)
    media_emoji_limit: int = Field(default=100, ge=1, le=1000)
    media_max_emoji_bytes: int = Field(default=512 * 1024, ge=1024, le=15 * 1024 * 1024)

    # Voice, video, and calls
    voice_enabled: bool = False
    voice_public_url: str | None = None
    voice_livekit_url: str = "http://127.0.0.1:7880"
    voice_api_key: SecretStr | None = None
    voice_api_secret: SecretStr | None = None
    voice_token_ttl_seconds: int = Field(default=15 * 60, ge=60, le=15 * 60)
    voice_occupancy_stale_seconds: int = Field(default=75, ge=60, le=300)
    voice_call_ttl_seconds: int = Field(default=24 * 60 * 60, ge=300, le=7 * 24 * 60 * 60)

    # Email
    email_backend: Literal["smtp", "mailtrap_api", "console", "disabled"] = "console"
    email_from_address: str = "no-reply@localhost"
    app_url: str = "http://localhost:5173"
    smtp_url: SecretStr | None = None
    mailtrap_api_token: SecretStr | None = None

    # Authentication
    access_token_ttl_seconds: int = Field(default=15 * 60, ge=60)
    refresh_sliding_days: int = Field(default=30, ge=1)
    refresh_absolute_days: int = Field(default=90, ge=1)
    verification_ttl_hours: int = Field(default=48, ge=1)
    password_reset_ttl_minutes: int = Field(default=30, ge=5)

    # Optional external interaction services
    klipy_enabled: bool = False
    klipy_api_key: SecretStr | None = None
    turnstile_enabled: bool = False
    turnstile_site_key: str | None = None
    # Cloudflare names this credential TURNSTILE_SECRET. Accept the field name
    # as well so tests and programmatic configuration remain straightforward.
    turnstile_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("TURNSTILE_SECRET", "turnstile_secret"),
    )

    # Mobile push. Relay delivery is the normal official-app path; direct FCM
    # remains available for separately signed community builds. A relay service
    # is enabled only on the operator that owns the corresponding mobile app.
    push_relay_enabled: bool = True
    push_relay_url: str = "https://push.kaede.chat"
    push_relay_origin: str = "kaede.chat"
    push_relay_app_id: str = "chat.kaede.mobile"
    push_relay_service_enabled: bool = False
    push_relay_fcm_service_account_b64: SecretStr | None = None
    push_enabled: bool = False
    push_fcm_service_account_b64: SecretStr | None = None

    # Observability and retention
    audit_retention_days: int = Field(default=90, ge=90)
    metrics_enabled: bool = True

    @field_validator(
        "proxy_secret",
        "admin_token",
        "smtp_url",
        "mailtrap_api_token",
        "federation_ca_file",
        "media_public_base_url",
        "media_s3_access_key",
        "media_s3_secret_key",
        "media_s3_session_token",
        "media_s3_create_buckets",
        "media_retention_days",
        "voice_public_url",
        "voice_api_key",
        "voice_api_secret",
        "klipy_api_key",
        "turnstile_site_key",
        "turnstile_secret",
        "push_relay_fcm_service_account_b64",
        "push_fcm_service_account_b64",
        "search_master_key",
        mode="before",
    )
    @classmethod
    def blank_optional_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, SecretStr) and not value.get_secret_value().strip():
            return None
        return value

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, value: str) -> str:
        if value.endswith(".."):
            raise ValueError("must contain at most one trailing root label")
        domain = value.removesuffix(".").lower()
        if not DOMAIN_RE.fullmatch(domain):
            raise ValueError("must be a lower-case fully-qualified domain")
        return domain

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not URLSAFE_BASE64_RE.fullmatch(raw):
            raise ValueError("must be URL-safe base64")
        try:
            decoded = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("must be URL-safe base64") from exc
        if len(decoded) != 32:
            raise ValueError("must decode to exactly 32 bytes")
        return value

    @field_validator("proxy_secret", "admin_token")
    @classmethod
    def validate_header_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not HEADER_SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError("must contain only URL-safe token characters")
        return value

    @field_validator("klipy_api_key")
    @classmethod
    def validate_klipy_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{8,256}", value.get_secret_value()
        ):
            raise ValueError("must be an 8-256 character KLIPY API key")
        return value

    @field_validator("turnstile_site_key")
    @classmethod
    def validate_turnstile_site_key(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", value):
            raise ValueError("must be a valid Cloudflare Turnstile site key")
        return value

    @field_validator("turnstile_secret")
    @classmethod
    def validate_turnstile_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{8,256}", value.get_secret_value()
        ):
            raise ValueError("must be a valid Cloudflare Turnstile secret")
        return value

    @field_validator("push_relay_origin")
    @classmethod
    def validate_push_relay_origin(cls, value: str) -> str:
        return cls.validate_domain(value)

    @field_validator("push_relay_url")
    @classmethod
    def validate_push_relay_url(cls, value: str) -> str:
        parsed = urlsplit(value.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("must be an absolute HTTP(S) URL")
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("must be an origin URL without credentials, path, query, or fragment")
        return value.rstrip("/")

    @field_validator("push_relay_app_id")
    @classmethod
    def validate_push_relay_app_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,159}", value):
            raise ValueError("must be a valid mobile application identifier")
        return value

    @field_validator("push_relay_fcm_service_account_b64", "push_fcm_service_account_b64")
    @classmethod
    def validate_push_service_account(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value().strip()
        try:
            decoded = base64.b64decode(raw, validate=True)
            if len(decoded) > 64 * 1024:
                raise ValueError("Firebase service account is too large")
            document = json.loads(decoded)
        except (ValueError, binascii.Error, json.JSONDecodeError) as exc:
            raise ValueError("must be base64-encoded Firebase service-account JSON") from exc
        required = {"project_id", "client_email", "private_key", "token_uri"}
        if (
            not isinstance(document, dict)
            or document.get("type") != "service_account"
            or any(
                not isinstance(document.get(name), str) or not document[name].strip()
                for name in required
            )
        ):
            raise ValueError("Firebase service account is missing required fields")
        if document["token_uri"] != FCM_AUTH_ENDPOINT:
            raise ValueError("Firebase token_uri must be Google's OAuth token endpoint")
        return value

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme != "postgresql+asyncpg"
            or not valid_url_host(parsed.hostname)
            or not parsed.path.strip("/")
            or "/" in parsed.path.strip("/")
            or parsed.fragment
        ):
            raise ValueError("must be a postgresql+asyncpg URL with a host and database name")
        return value

    @field_validator("dragonfly_url")
    @classmethod
    def validate_dragonfly_url(cls, value: SecretStr) -> SecretStr:
        parsed = urlsplit(value.get_secret_value())
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme not in {"redis", "rediss"}
            or not valid_url_host(parsed.hostname)
            or parsed.fragment
        ):
            raise ValueError("must be a redis:// or rediss:// URL with a host")
        return value

    @field_validator("app_url")
    @classmethod
    def validate_app_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not valid_url_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "must be an absolute HTTP(S) origin without credentials, query, fragment, or path"
            )
        return value.rstrip("/")

    @field_validator("media_s3_endpoint", "media_public_base_url")
    @classmethod
    def validate_media_origin(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not valid_url_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("must be an absolute HTTP(S) origin without credentials or a path")
        return value.rstrip("/")

    @field_validator("voice_public_url")
    @classmethod
    def validate_voice_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme not in {"ws", "wss"}
            or not valid_url_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/", "/livekit"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "must be an absolute WebSocket URL with no path or the /livekit proxy path"
            )
        return value.rstrip("/")

    @field_validator("voice_livekit_url")
    @classmethod
    def validate_voice_livekit_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("contains an invalid port") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not valid_url_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("must be an absolute HTTP(S) origin without credentials or a path")
        return value.rstrip("/")

    @field_validator("voice_api_key")
    @classmethod
    def validate_voice_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(
            r"[A-Za-z0-9_-]{3,128}", value.get_secret_value()
        ):
            raise ValueError("must be a 3-128 character LiveKit API key")
        return value

    @field_validator("voice_api_secret")
    @classmethod
    def validate_voice_api_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            raw = value.get_secret_value()
            if not 32 <= len(raw) <= 256 or any(ord(item) < 33 or ord(item) > 126 for item in raw):
                raise ValueError("must be a 32-256 character printable LiveKit API secret")
        return value

    @field_validator(
        "media_attachments_bucket", "media_derived_bucket", "media_remote_cache_bucket"
    )
    @classmethod
    def validate_bucket_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value):
            raise ValueError("must be a valid lower-case S3 bucket name")
        return value

    @field_validator("media_s3_access_key")
    @classmethod
    def validate_media_access_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not 16 <= len(raw) <= 128 or not re.fullmatch(r"[A-Za-z0-9._~-]+", raw):
            raise ValueError("must be a 16-128 character URL-safe object-store access key")
        return value

    @field_validator("media_s3_secret_key")
    @classmethod
    def validate_media_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not 16 <= len(raw) <= 128 or any(
            ord(character) < 33 or ord(character) > 126 for character in raw
        ):
            raise ValueError("must be a 16-128 character printable object-store secret key")
        return value

    @field_validator("media_s3_session_token")
    @classmethod
    def validate_media_session_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw = value.get_secret_value()
        if not 16 <= len(raw) <= 4096 or any(
            ord(character) < 33 or ord(character) > 126 for character in raw
        ):
            raise ValueError("must be a printable 16-4096 character S3 session token")
        return value

    @field_validator("media_s3_region")
    @classmethod
    def validate_media_region(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", value):
            raise ValueError("must be a valid SigV4 region identifier")
        return value

    @field_validator("smtp_url")
    @classmethod
    def validate_smtp_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            parsed = urlsplit(value.get_secret_value())
            try:
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("contains an invalid port") from exc
            if (
                parsed.scheme not in {"smtp", "smtps"}
                or not valid_url_host(parsed.hostname)
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("must be an smtp:// or smtps:// URL with a host")
        return value

    @field_validator("email_from_address")
    @classmethod
    def validate_from_address(cls, value: str) -> str:
        local, separator, domain = value.rpartition("@")
        if (
            not separator
            or not local
            or not domain
            or any(character.isspace() for character in value)
        ):
            raise ValueError("must be a plain email address")
        return f"{local}@{domain.lower()}"

    @model_validator(mode="after")
    def validate_environment_configuration(self) -> Self:
        if self.gateway_identify_burst < self.gateway_identify_rate_per_second:
            raise ValueError("gateway_identify_burst cannot be below its per-second rate")
        if self.gateway_identify_ip_burst < self.gateway_identify_ip_rate_per_second:
            raise ValueError("gateway_identify_ip_burst cannot be below its per-second rate")
        if self.refresh_sliding_days > self.refresh_absolute_days:
            raise ValueError("refresh_sliding_days cannot exceed refresh_absolute_days")
        if self.federation_inbox_max_events_total < self.federation_inbox_max_events_per_origin:
            raise ValueError(
                "federation_inbox_max_events_total cannot be below the per-origin limit"
            )
        if self.federation_inbox_max_bytes_total < self.federation_inbox_max_bytes_per_origin:
            raise ValueError(
                "federation_inbox_max_bytes_total cannot be below the per-origin limit"
            )
        if self.federation_remote_media_inflight_bytes_per_origin < self.media_max_attachment_bytes:
            raise ValueError(
                "federation_remote_media_inflight_bytes_per_origin cannot be below one "
                "maximum attachment"
            )
        if (
            self.federation_remote_media_inflight_bytes_total
            < self.federation_remote_media_inflight_bytes_per_origin
        ):
            raise ValueError(
                "federation_remote_media_inflight_bytes_total cannot be below the per-origin limit"
            )
        if self.federation_replica_max_rows_per_origin < self.federation_replica_max_rows_per_guild:
            raise ValueError(
                "federation_replica_max_rows_per_origin cannot be below the per-guild limit"
            )
        if (
            self.federation_replica_max_bytes_per_origin
            < self.federation_replica_max_bytes_per_guild
        ):
            raise ValueError(
                "federation_replica_max_bytes_per_origin cannot be below the per-guild limit"
            )
        if (
            self.federation_history_max_active_exports_total
            < self.federation_history_max_active_exports_per_origin
        ):
            raise ValueError(
                "federation_history_max_active_exports_total cannot be below the per-origin limit"
            )
        if (
            self.federation_history_max_active_channel_grants_total
            < self.federation_history_max_active_channel_grants_per_origin
        ):
            raise ValueError(
                "federation_history_max_active_channel_grants_total cannot be below the "
                "per-origin limit"
            )
        if (
            self.federation_history_max_active_channel_grants_per_origin
            < self.federation_history_max_active_exports_per_origin
        ):
            raise ValueError(
                "federation_history_max_active_channel_grants_per_origin cannot be below "
                "the active-export limit"
            )
        if (
            self.federation_history_max_active_channel_grants_total
            < self.federation_history_max_active_exports_total
        ):
            raise ValueError(
                "federation_history_max_active_channel_grants_total cannot be below the "
                "active-export limit"
            )
        if (
            self.federation_dm_max_messages_per_authority
            < self.federation_dm_max_messages_per_conversation
        ):
            raise ValueError(
                "federation_dm_max_messages_per_authority cannot be below the "
                "per-conversation limit"
            )
        if (
            self.federation_dm_max_bytes_per_authority
            < self.federation_dm_max_bytes_per_conversation
        ):
            raise ValueError(
                "federation_dm_max_bytes_per_authority cannot be below the per-conversation limit"
            )
        if (
            self.federation_pending_relationships_per_recipient_origin
            > self.federation_pending_relationships_per_recipient
        ):
            raise ValueError(
                "federation_pending_relationships_per_recipient_origin cannot exceed "
                "the per-recipient limit"
            )
        if (
            self.federation_pending_relationships_per_recipient_origin
            > self.federation_pending_relationships_per_origin
        ):
            raise ValueError(
                "federation_pending_relationships_per_recipient_origin cannot exceed "
                "the per-origin limit"
            )
        if (
            self.federation_dm_max_conversations_per_remote_origin
            >= self.federation_dm_max_conversations_per_authority
        ):
            raise ValueError(
                "federation_dm_max_conversations_per_remote_origin must be below the "
                "aggregate authority limit"
            )
        if (
            self.federation_dm_max_messages_per_remote_origin
            < self.federation_dm_max_messages_per_conversation
        ):
            raise ValueError(
                "federation_dm_max_messages_per_remote_origin cannot be below the "
                "per-conversation limit"
            )
        if (
            self.federation_dm_max_messages_per_remote_origin
            >= self.federation_dm_max_messages_per_authority
        ):
            raise ValueError(
                "federation_dm_max_messages_per_remote_origin must be below the "
                "aggregate authority limit"
            )
        if (
            self.federation_dm_max_bytes_per_remote_origin
            < self.federation_dm_max_bytes_per_conversation
        ):
            raise ValueError(
                "federation_dm_max_bytes_per_remote_origin cannot be below the "
                "per-conversation limit"
            )
        if (
            self.federation_dm_max_bytes_per_remote_origin
            >= self.federation_dm_max_bytes_per_authority
        ):
            raise ValueError(
                "federation_dm_max_bytes_per_remote_origin must be below the "
                "aggregate authority limit"
            )
        if (
            self.federation_dm_replica_cache_messages_per_conversation
            > self.federation_dm_max_messages_per_conversation
        ):
            raise ValueError(
                "federation_dm_replica_cache_messages_per_conversation cannot exceed "
                "the hard per-conversation limit"
            )
        if (
            self.federation_dm_replica_cache_bytes_per_conversation
            > self.federation_dm_max_bytes_per_conversation
        ):
            raise ValueError(
                "federation_dm_replica_cache_bytes_per_conversation cannot exceed "
                "the hard per-conversation limit"
            )
        if self.federation_history_page_bytes > self.federation_history_max_bytes:
            raise ValueError(
                "federation_history_page_bytes cannot exceed federation_history_max_bytes"
            )
        if self.media_inflight_quota_bytes < self.media_max_attachment_bytes:
            raise ValueError("media_inflight_quota_bytes cannot be below one maximum attachment")
        if self.search_enabled:
            if (
                self.search_master_key is None
                or len(self.search_master_key.get_secret_value()) < 32
            ):
                raise ValueError(
                    "search_master_key must contain at least 32 characters when search is enabled"
                )
            search_endpoint = urlsplit(self.search_url)
            if (
                search_endpoint.scheme not in {"http", "https"}
                or not search_endpoint.hostname
                or search_endpoint.username is not None
                or search_endpoint.password is not None
                or search_endpoint.path not in {"", "/"}
                or search_endpoint.query
                or search_endpoint.fragment
            ):
                raise ValueError(
                    "search_url must be an HTTP(S) origin without a path, credentials, or query"
                )
        if self.media_s3_create_buckets is None:
            self.media_s3_create_buckets = self.media_storage_backend == "garage"
        if self.media_s3_addressing_style == "virtual" and any(
            "." in bucket
            for bucket in (
                self.media_attachments_bucket,
                self.media_derived_bucket,
                self.media_remote_cache_bucket,
            )
        ):
            raise ValueError("virtual-hosted S3 addressing requires bucket names without dots")
        if self.media_s3_addressing_style == "virtual":
            for endpoint in (self.media_s3_endpoint, self.media_public_base_url):
                if endpoint is None:
                    continue
                host = urlsplit(endpoint).hostname
                try:
                    ipaddress.ip_address(host or "")
                except ValueError:
                    continue
                raise ValueError("virtual-hosted S3 addressing requires DNS endpoint hosts")
        media_runtime = self.service_role in {"full", "api", "worker", "preflight"}
        if (
            media_runtime
            and (self.media_s3_access_key is None or self.media_s3_secret_key is None)
            and self.environment == "production"
        ):
            raise ValueError("media S3 credentials are required in production")
        if (
            self.media_public_base_url is None
            and self.environment == "production"
            and self.media_storage_backend == "s3"
        ):
            raise ValueError("media_public_base_url is required for external S3 storage")
        if self.media_public_base_url is None:
            scheme = "https" if self.environment == "production" else "http"
            self.media_public_base_url = f"{scheme}://media.{self.domain}"
        if self.voice_public_url is None:
            scheme = "wss" if self.environment == "production" else "ws"
            self.voice_public_url = f"{scheme}://{self.domain}/livekit"
        voice_runtime = self.service_role in {"full", "api", "worker", "preflight"}
        if (
            self.voice_enabled
            and voice_runtime
            and (self.voice_api_key is None or self.voice_api_secret is None)
        ):
            raise ValueError(
                "voice_api_key and voice_api_secret are required when voice is enabled"
            )
        email_runtime = self.service_role in {"full", "worker", "preflight"}
        if email_runtime and self.email_backend == "smtp" and self.smtp_url is None:
            raise ValueError("smtp_url is required for the smtp email backend")
        if (
            email_runtime
            and self.email_backend == "mailtrap_api"
            and (self.mailtrap_api_token is None or not self.mailtrap_api_token.get_secret_value())
        ):
            raise ValueError("mailtrap_api_token is required for the mailtrap_api email backend")
        interaction_runtime = self.service_role in {"full", "api", "preflight"}
        if interaction_runtime and self.klipy_enabled and self.klipy_api_key is None:
            raise ValueError("klipy_api_key is required when KLIPY GIF support is enabled")
        if (
            interaction_runtime
            and self.turnstile_enabled
            and (self.turnstile_site_key is None or self.turnstile_secret is None)
        ):
            raise ValueError(
                "turnstile_site_key and TURNSTILE_SECRET are required when Turnstile is enabled"
            )
        push_runtime = self.service_role in {"full", "worker", "preflight"}
        if push_runtime and self.push_enabled and self.push_fcm_service_account_b64 is None:
            raise ValueError("push_fcm_service_account_b64 is required when mobile push is enabled")
        relay_push_runtime = self.service_role in {"full", "worker"}
        if (
            relay_push_runtime
            and self.push_relay_service_enabled
            and self.push_relay_fcm_service_account_b64 is None
        ):
            raise ValueError(
                "push_relay_fcm_service_account_b64 is required when the push relay "
                "service is enabled"
            )
        if self.push_relay_service_enabled and self.push_relay_origin != self.domain:
            raise ValueError(
                "push_relay_origin must match domain when the push relay service is enabled"
            )
        if (
            self.environment == "production"
            and self.push_relay_enabled
            and urlsplit(self.push_relay_url).scheme != "https"
        ):
            raise ValueError("push_relay_url must use HTTPS in production")
        if self.environment == "production":
            if self.domain.endswith(".localhost"):
                raise ValueError("a .localhost domain cannot be used in production")
            if urlsplit(self.app_url).scheme != "https":
                raise ValueError("app_url must use HTTPS in production")
            if urlsplit(self.app_url).hostname != self.domain:
                raise ValueError("app_url host must match domain in production")
            if email_runtime and self.email_backend == "console":
                raise ValueError("the console email backend cannot be used in production")
            if not self.media_scan_enabled:
                raise ValueError("media malware scanning cannot be disabled in production")
            if self.proxy_secret is None or len(self.proxy_secret.get_secret_value()) < 32:
                raise ValueError("proxy_secret must contain at least 32 characters in production")
            if self.proxy_secret.get_secret_value().lower().startswith(("replace", "change-me")):
                raise ValueError("proxy_secret must not be a placeholder in production")
            if self.federation_peer_overrides:
                raise ValueError("federation_peer_overrides cannot be used in production")
            if self.federation_ca_file is not None:
                raise ValueError("federation_ca_file cannot be used in production")
            if self.media_storage_backend == "garage":
                if urlsplit(self.media_s3_endpoint).scheme != "http":
                    raise ValueError(
                        "the internal Garage S3 endpoint must use HTTP inside the data network"
                    )
                if urlsplit(self.media_public_base_url).hostname != f"media.{self.domain}":
                    raise ValueError("Garage media_public_base_url host must be media.<domain>")
            elif urlsplit(self.media_s3_endpoint).scheme != "https":
                raise ValueError("an external S3 endpoint must use HTTPS in production")
            if urlsplit(self.media_public_base_url).scheme != "https":
                raise ValueError("media_public_base_url must use HTTPS in production")
            if self.voice_enabled and urlsplit(self.voice_public_url).scheme != "wss":
                raise ValueError("voice_public_url must use WSS in production")
            if self.voice_enabled and urlsplit(self.voice_public_url).hostname != self.domain:
                raise ValueError("voice_public_url host must match domain in production")
            if self.voice_enabled and urlsplit(self.voice_livekit_url).hostname not in {
                "127.0.0.1",
                "localhost",
                "host.docker.internal",
            }:
                raise ValueError("voice_livekit_url must target the local LiveKit control plane")
            if self.admin_token is not None and len(self.admin_token.get_secret_value()) < 32:
                raise ValueError("admin_token must contain at least 32 characters in production")
            if (
                self.admin_token is not None
                and self.admin_token.get_secret_value().lower().startswith(("replace", "change-me"))
            ):
                raise ValueError("admin_token must not be a placeholder in production")
            if (
                email_runtime
                and self.email_backend != "disabled"
                and "." not in self.email_from_address.rpartition("@")[2]
            ):
                raise ValueError("email_from_address must use a qualified domain in production")
        return self

    @property
    def secret_key_bytes(self) -> bytes:
        raw = self.secret_key.get_secret_value()
        return base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
