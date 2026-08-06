import base64

import pytest

from app.cli import validate_external_secrets


def production_values() -> dict[str, str]:
    return {
        "KAEDE_EDGE_SECRET": "edge_" + "a" * 32,
        "KAEDE_SECRET_KEY": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "KAEDE_GATEWAY_SECRET_KEY": base64.urlsafe_b64encode(bytes(range(32, 64)))
        .decode()
        .rstrip("="),
        "KAEDE_PROXY_SECRET": "proxy_" + "z" * 32,
        "POSTGRES_PASSWORD": "p" * 32,
        "KAEDE_DATABASE_URL": f"postgresql+asyncpg://kaede:{'p' * 32}@postgres:5432/kaede",
        "DRAGONFLY_PASSWORD": "r" * 32,
        "KAEDE_DRAGONFLY_URL": f"redis://:{'r' * 32}@dragonfly:6379/0",
        "GARAGE_RPC_SECRET": "a" * 64,
        "GARAGE_ADMIN_TOKEN": "b" * 32,
        "KAEDE_MEDIA_STORAGE_BACKEND": "garage",
        "KAEDE_MEDIA_S3_ACCESS_KEY": f"GK{'c' * 32}",
        "KAEDE_MEDIA_S3_SECRET_KEY": "d" * 64,
        "LIVEKIT_API_KEY": "e" * 16,
        "LIVEKIT_API_SECRET": "f" * 32,
        "KAEDE_VOICE_ENABLED": "true",
        "LIVEKIT_TURN_CERT_PATH": "/etc/letsencrypt/live/chat/fullchain.pem",
        "LIVEKIT_TURN_KEY_PATH": "/etc/letsencrypt/live/chat/privkey.pem",
    }


def test_external_secret_preflight_accepts_generated_values() -> None:
    validate_external_secrets("production", production_values())


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("POSTGRES_PASSWORD", "replace-me"),
        ("KAEDE_EDGE_SECRET", "contains a space despite being very long"),
        ("KAEDE_GATEWAY_SECRET_KEY", "replace-me"),
        ("DRAGONFLY_PASSWORD", "replace-me"),
        ("GARAGE_RPC_SECRET", "not-hex"),
        ("KAEDE_MEDIA_S3_ACCESS_KEY", "GKshort"),
        ("KAEDE_MEDIA_S3_SECRET_KEY", "short"),
        ("LIVEKIT_API_SECRET", "replace-me"),
        ("LIVEKIT_API_KEY", "bad:key-with-invalid-yaml-syntax"),
        ("LIVEKIT_TURN_CERT_PATH", "relative.pem"),
    ],
)
def test_external_secret_preflight_rejects_placeholders(name: str, value: str) -> None:
    values = production_values()
    values[name] = value
    with pytest.raises(ValueError, match=name):
        validate_external_secrets("production", values)


def test_external_secret_preflight_is_not_applied_to_test_environment() -> None:
    validate_external_secrets("test", {})


def test_core_preflight_does_not_require_disabled_voice_secrets() -> None:
    values = production_values()
    values["KAEDE_VOICE_ENABLED"] = "false"
    for name in (
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
        "LIVEKIT_TURN_CERT_PATH",
        "LIVEKIT_TURN_KEY_PATH",
    ):
        values.pop(name)
    validate_external_secrets("production", values, include_voice=False)


def test_external_s3_preflight_does_not_require_garage_secrets() -> None:
    values = production_values()
    values["KAEDE_MEDIA_STORAGE_BACKEND"] = "s3"
    values["KAEDE_MEDIA_S3_ACCESS_KEY"] = "AKIA" + "a" * 16
    values["KAEDE_MEDIA_S3_SECRET_KEY"] = "external/secret+key=" + "z" * 16
    for name in ("GARAGE_RPC_SECRET", "GARAGE_ADMIN_TOKEN"):
        values.pop(name)
    validate_external_secrets("production", values, include_voice=False)


@pytest.mark.parametrize("enabled", [None, "", "false", "TRUE", "1", "yes"])
def test_voice_preflight_requires_explicit_true(enabled: str | None) -> None:
    values = production_values()
    if enabled is None:
        values.pop("KAEDE_VOICE_ENABLED")
    else:
        values["KAEDE_VOICE_ENABLED"] = enabled
    with pytest.raises(ValueError, match="KAEDE_VOICE_ENABLED"):
        validate_external_secrets("production", values, include_voice=True)


@pytest.mark.parametrize(
    ("url_name", "password_name"),
    [
        ("KAEDE_DATABASE_URL", "POSTGRES_PASSWORD"),
        ("KAEDE_DRAGONFLY_URL", "DRAGONFLY_PASSWORD"),
    ],
)
def test_external_secret_preflight_requires_matching_service_urls(
    url_name: str, password_name: str
) -> None:
    values = production_values()
    values[password_name] = "x" * 32
    with pytest.raises(ValueError, match=url_name):
        validate_external_secrets("production", values)


def test_external_secret_preflight_separates_edge_and_proxy_trust() -> None:
    values = production_values()
    values["KAEDE_PROXY_SECRET"] = values["KAEDE_EDGE_SECRET"]
    with pytest.raises(ValueError, match="must differ"):
        validate_external_secrets("production", values)


def test_external_secret_preflight_separates_gateway_and_master_keys() -> None:
    values = production_values()
    values["KAEDE_GATEWAY_SECRET_KEY"] = values["KAEDE_SECRET_KEY"]
    with pytest.raises(ValueError, match="must differ"):
        validate_external_secrets("production", values)


def test_external_secret_preflight_compares_decoded_key_material() -> None:
    values = production_values()
    values["KAEDE_GATEWAY_SECRET_KEY"] = values["KAEDE_SECRET_KEY"]
    values["KAEDE_SECRET_KEY"] += "="
    with pytest.raises(ValueError, match="must differ"):
        validate_external_secrets("production", values)
