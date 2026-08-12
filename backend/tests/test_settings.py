import base64
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.settings import AUXILIARY_KAEDE_ENV, Settings

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()
VALID_FCM_ACCOUNT = base64.b64encode(
    json.dumps(
        {
            "type": "service_account",
            "project_id": "kaede-mobile",
            "client_email": "firebase@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    ).encode()
).decode()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_settings_normalize_domain_and_hide_secrets() -> None:
    configured = settings(domain="Chat.Example.COM.")
    assert configured.domain == "chat.example.com"
    assert VALID_KEY not in repr(configured)


def test_secret_must_be_32_bytes() -> None:
    with pytest.raises(ValidationError):
        settings(secret_key=base64.urlsafe_b64encode(b"short").decode())


def test_secret_must_use_the_url_safe_base64_alphabet() -> None:
    standard_alphabet = base64.b64encode(b"\xfb" * 32).decode()
    assert "+" in standard_alphabet or "/" in standard_alphabet
    with pytest.raises(ValidationError, match="URL-safe"):
        settings(secret_key=standard_alphabet)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("database_url", "postgresql://test:test@postgres/test"),
        ("database_url", "postgresql+asyncpg://test:test@postgres/one/two"),
        ("database_url", "postgresql+asyncpg://test:test@bad host/test"),
        ("dragonfly_url", "http://dragonfly:6379/0"),
        ("dragonfly_url", "redis://dragonfly:bad/0"),
        ("app_url", "https://chat.example.com/app"),
        ("app_url", "https://user@chat.example.com"),
        ("smtp_url", "smtp://mail.example.com/path"),
    ],
)
def test_service_urls_are_strict(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        settings(**{field: value})


def test_conditional_email_and_refresh_settings() -> None:
    with pytest.raises(ValidationError, match="smtp_url"):
        settings(email_backend="smtp")
    with pytest.raises(ValidationError, match="mailtrap_api_token"):
        settings(email_backend="mailtrap_api")
    with pytest.raises(ValidationError, match="cannot exceed"):
        settings(refresh_sliding_days=91, refresh_absolute_days=90)
    with pytest.raises(ValidationError):
        settings(federation_event_retention_days=6)
    with pytest.raises(ValidationError):
        settings(federation_history_export_ttl_minutes=9)
    with pytest.raises(ValidationError):
        settings(federation_history_page_messages=501)
    with pytest.raises(ValidationError):
        settings(federation_history_max_messages=99)
    with pytest.raises(ValidationError, match="events_total"):
        settings(
            federation_inbox_max_events_per_origin=2_000,
            federation_inbox_max_events_total=1_000,
        )
    with pytest.raises(ValidationError, match="bytes_total"):
        settings(
            federation_inbox_max_bytes_per_origin=2 * 1024 * 1024,
            federation_inbox_max_bytes_total=1024 * 1024,
        )
    with pytest.raises(ValidationError):
        settings(federation_max_remote_instances=99)
    with pytest.raises(ValidationError):
        settings(federation_peer_key_history_limit=127)
    with pytest.raises(ValidationError, match="replica_max_rows_per_origin"):
        settings(
            federation_replica_max_rows_per_guild=20_000,
            federation_replica_max_rows_per_origin=10_000,
        )
    with pytest.raises(ValidationError, match="replica_max_bytes_per_origin"):
        settings(
            federation_replica_max_bytes_per_guild=32 * 1024 * 1024,
            federation_replica_max_bytes_per_origin=16 * 1024 * 1024,
        )
    with pytest.raises(ValidationError, match="replica_cache_messages"):
        settings(
            federation_dm_replica_cache_messages_per_conversation=1_001,
            federation_dm_max_messages_per_conversation=1_000,
        )
    with pytest.raises(ValidationError, match="replica_cache_bytes"):
        settings(
            federation_dm_replica_cache_bytes_per_conversation=2 * 1024 * 1024,
            federation_dm_max_bytes_per_conversation=1024 * 1024,
        )
    with pytest.raises(ValidationError, match="history_page_bytes"):
        settings(
            federation_history_page_bytes=2 * 1024 * 1024,
            federation_history_max_bytes=1024 * 1024,
        )
    with pytest.raises(ValidationError):
        settings(federation_remote_identity_retention_days=6)
    with pytest.raises(ValidationError, match="remote_media_inflight_bytes_per_origin"):
        settings(
            media_max_attachment_bytes=2 * 1024 * 1024,
            federation_remote_media_inflight_bytes_per_origin=1024 * 1024,
        )
    with pytest.raises(ValidationError, match="remote_media_inflight_bytes_total"):
        settings(
            media_max_attachment_bytes=1024 * 1024,
            federation_remote_media_inflight_bytes_per_origin=2 * 1024 * 1024,
            federation_remote_media_inflight_bytes_total=1024 * 1024,
        )
    with pytest.raises(ValidationError):
        settings(audit_retention_days=89)


def test_federation_storage_defaults_have_realistic_import_headroom() -> None:
    configured = settings()

    assert configured.federation_inbox_max_events_per_origin == 5_000_000
    assert configured.federation_inbox_max_bytes_per_origin == 16 * 1024**3
    assert configured.federation_inbox_max_events_total == 50_000_000
    assert configured.federation_inbox_max_bytes_total == 160 * 1024**3
    assert configured.federation_replica_max_rows_per_guild == 20_000_000
    assert configured.federation_replica_max_bytes_per_guild == 64 * 1024**3
    assert configured.federation_replica_max_rows_per_origin == 100_000_000
    assert configured.federation_replica_max_bytes_per_origin == 320 * 1024**3
    assert configured.federation_history_max_messages == 2_000_000
    assert configured.federation_history_max_bytes == 32 * 1024**3
    assert configured.federation_dm_replica_cache_messages_per_conversation == 250_000
    assert configured.federation_dm_replica_cache_bytes_per_conversation == 2 * 1024**3
    assert configured.media_remote_cache_bytes == 100 * 1024**3
    assert (
        configured.federation_dm_replica_cache_messages_per_conversation
        < configured.federation_dm_max_messages_per_conversation
    )
    assert (
        configured.federation_dm_replica_cache_bytes_per_conversation
        < configured.federation_dm_max_bytes_per_conversation
    )


def test_blank_optional_secrets_are_treated_as_unset() -> None:
    configured = settings(
        proxy_secret="",
        admin_token=" ",
        smtp_url="",
        mailtrap_api_token="",
        federation_ca_file="",
        klipy_api_key="",
        turnstile_site_key="",
        turnstile_secret="",
        push_fcm_service_account_b64="",
    )
    assert configured.proxy_secret is None
    assert configured.admin_token is None
    assert configured.smtp_url is None
    assert configured.mailtrap_api_token is None
    assert configured.federation_ca_file is None
    assert configured.klipy_api_key is None
    assert configured.turnstile_secret is None
    assert configured.push_fcm_service_account_b64 is None


def test_optional_interaction_services_require_credentials_and_hide_them() -> None:
    with pytest.raises(ValidationError, match="klipy_api_key"):
        settings(service_role="api", klipy_enabled=True)
    with pytest.raises(ValidationError, match="TURNSTILE_SECRET"):
        settings(
            service_role="api",
            turnstile_enabled=True,
            turnstile_site_key="0x4AAAAAAExampleSiteKey",
        )
    configured = settings(
        service_role="api",
        klipy_enabled=True,
        klipy_api_key="klipy_example_key",
        turnstile_enabled=True,
        turnstile_site_key="0x4AAAAAAExampleSiteKey",
        turnstile_secret="0x4AAAAAAExampleSecret",
    )
    assert "klipy_example_key" not in repr(configured)
    assert "0x4AAAAAAExampleSecret" not in repr(configured)


def test_mobile_push_service_account_is_validated_and_secret_safe() -> None:
    with pytest.raises(ValidationError, match="push_fcm_service_account_b64"):
        settings(service_role="worker", push_enabled=True)
    with pytest.raises(ValidationError, match="base64-encoded"):
        settings(push_fcm_service_account_b64="not-base64")
    malformed = base64.b64encode(json.dumps({"type": "authorized_user"}).encode()).decode()
    with pytest.raises(ValidationError, match="missing required fields"):
        settings(push_fcm_service_account_b64=malformed)
    alternate_endpoint = base64.b64encode(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "kaede-mobile",
                "client_email": "firebase@example.iam.gserviceaccount.com",
                "private_key": "test",
                "token_uri": "https://oauth.example.test/token",
            }
        ).encode()
    ).decode()
    with pytest.raises(ValidationError, match="Google's OAuth token endpoint"):
        settings(push_fcm_service_account_b64=alternate_endpoint)
    configured = settings(
        service_role="worker",
        push_enabled=True,
        push_fcm_service_account_b64=VALID_FCM_ACCOUNT,
    )
    assert VALID_FCM_ACCOUNT not in repr(configured)
    gateway = settings(service_role="gateway", push_enabled=True)
    assert gateway.push_enabled is True


def test_media_configuration_is_bounded_and_secret_safe() -> None:
    configured = settings()
    assert "GK00000000000000000000000000000000" not in repr(configured)
    assert configured.media_s3_create_buckets is True
    with pytest.raises(ValidationError, match="inflight"):
        settings(media_max_attachment_bytes=100, media_inflight_quota_bytes=99)
    with pytest.raises(ValidationError, match="bucket"):
        settings(media_derived_bucket="Bad_Bucket")
    with pytest.raises(ValidationError, match="without dots"):
        settings(media_s3_addressing_style="virtual", media_derived_bucket="derived.assets")


@pytest.mark.parametrize(
    "override",
    [
        {
            "federation_history_max_active_exports_per_origin": 11,
            "federation_history_max_active_exports_total": 10,
        },
        {
            "federation_history_max_active_channel_grants_per_origin": 101,
            "federation_history_max_active_channel_grants_total": 100,
        },
        {
            "federation_history_max_active_exports_per_origin": 11,
            "federation_history_max_active_channel_grants_per_origin": 10,
        },
    ],
)
def test_history_export_capacity_configuration_preserves_aggregate_bounds(
    override: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="federation_history_max_active"):
        settings(**override)


def test_external_s3_defaults_to_non_mutating_bucket_verification() -> None:
    configured = settings(media_storage_backend="s3")
    assert configured.media_s3_create_buckets is False


def production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "chat.example.com",
        "environment": "production",
        "app_url": "https://chat.example.com",
        "email_backend": "smtp",
        "smtp_url": "smtps://mail.example.com:465",
        "email_from_address": "no-reply@example.com",
        "proxy_secret": "p" * 32,
        "admin_token": None,
    }
    values.update(overrides)
    return settings(**values)


def test_valid_production_settings() -> None:
    configured = production_settings()
    assert configured.domain == "chat.example.com"


def test_production_can_disable_email_delivery() -> None:
    configured = production_settings(
        email_backend="disabled",
        smtp_url=None,
        email_from_address="no-reply@localhost",
    )
    assert configured.email_backend == "disabled"


def test_valid_external_s3_production_settings() -> None:
    configured = production_settings(
        media_storage_backend="s3",
        media_s3_endpoint="https://s3.us-west-004.backblazeb2.com",
        media_public_base_url="https://s3.us-west-004.backblazeb2.com",
        media_s3_region="us-west-004",
        media_s3_create_buckets=False,
        media_s3_secret_key="external/secret+key=" + "z" * 16,
    )
    assert configured.media_s3_region == "us-west-004"
    assert configured.media_s3_create_buckets is False


def test_external_s3_production_requires_explicit_https_endpoints() -> None:
    with pytest.raises(ValidationError, match="media_public_base_url"):
        production_settings(media_storage_backend="s3", media_public_base_url=None)
    with pytest.raises(ValidationError, match="HTTPS"):
        production_settings(
            media_storage_backend="s3",
            media_s3_endpoint="http://s3.example.com",
            media_public_base_url="https://s3.example.com",
        )


def test_gateway_production_settings_do_not_require_email_credentials() -> None:
    configured = production_settings(
        service_role="gateway",
        email_backend="console",
        smtp_url=None,
        email_from_address="unused@localhost",
    )
    assert configured.service_role == "gateway"


@pytest.mark.parametrize(
    "override",
    [
        {"domain": "chat.localhost"},
        {"app_url": "http://chat.example.com"},
        {"app_url": "https://web.example.com"},
        {"email_backend": "console"},
        {"proxy_secret": "short"},
        {"federation_peer_overrides": {"peer.example.com": "http://127.0.0.1"}},
        {"federation_ca_file": "/var/empty/test-ca.crt"},
        {"admin_token": "short"},
        {"email_from_address": "root@localhost"},
    ],
)
def test_production_rejects_unsafe_configuration(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        production_settings(**override)


def test_domain_allows_one_root_dot_but_not_multiple() -> None:
    assert settings(domain="Chat.Example.com.").domain == "chat.example.com"
    with pytest.raises(ValidationError):
        settings(domain="chat.example.com..")


def test_unknown_kaede_environment_setting_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAEDE_DATABSE_URL", "typo")
    with pytest.raises(ValueError, match="KAEDE_DATABSE_URL"):
        settings()


def test_deployment_only_voice_switch_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAEDE_VOICE_ENABLED", "false")
    assert settings().environment == "test"


def test_committed_environment_templates_use_known_kaede_settings() -> None:
    repository = Path(__file__).resolve().parents[2]
    if not (repository / ".env.example").is_file():
        repository = Path("/repository")
    assert (repository / ".env.example").is_file()
    templates = (
        repository / ".env.example",
        repository / ".env.s3.example",
        repository / "deploy" / "reference.env.example",
        repository / "deploy" / ".env.alpha",
        repository / "deploy" / ".env.beta",
        repository / "deploy" / ".env.schema",
    )
    known = {f"KAEDE_{name.upper()}" for name in Settings.model_fields} | AUXILIARY_KAEDE_ENV
    unknown: dict[str, list[str]] = {}
    for template in templates:
        names = set(
            re.findall(
                r"^(KAEDE_[A-Z0-9_]+)=",
                template.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        )
        if unexpected := sorted(names - known):
            unknown[str(template.relative_to(repository))] = unexpected
    assert unknown == {}
