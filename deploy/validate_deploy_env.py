"""Validate the operator environment at the deployment boundary.

The application settings validator sees the environment passed to a process.  A
Compose ``--env-file`` is otherwise only an interpolation source, which can hide
misspelled variables from that validator.  Production preflight loads the same
file through ``env_file`` and invokes this small, dependency-free guard before
the application preflight.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FCM_AUTH_ENDPOINT = "https://oauth2.googleapis.com/token"
PLACEHOLDER_MARKERS = (
    "change-me",
    "changeme",
    "not-configured",
    "not-used",
    "replace-",
    "replace_",
)
SENSITIVE_NAMES = {
    "DRAGONFLY_PASSWORD",
    "GARAGE_ADMIN_TOKEN",
    "GARAGE_RPC_SECRET",
    "GRAFANA_ADMIN_PASSWORD",
    "KAEDE_ADMIN_TOKEN",
    "KAEDE_DATABASE_URL",
    "KAEDE_DRAGONFLY_URL",
    "KAEDE_EDGE_SECRET",
    "KAEDE_GATEWAY_SECRET_KEY",
    "KAEDE_MAILTRAP_API_TOKEN",
    "KAEDE_KLIPY_API_KEY",
    "KAEDE_MEDIA_S3_ACCESS_KEY",
    "KAEDE_MEDIA_S3_SECRET_KEY",
    "KAEDE_MEDIA_S3_SESSION_TOKEN",
    "KAEDE_PROXY_SECRET",
    "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64",
    "KAEDE_SECRET_KEY",
    "KAEDE_SMTP_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "POSTGRES_PASSWORD",
    "TURNSTILE_SECRET",
}
VOICE_PORT_DEFAULTS = {
    "LIVEKIT_CONTROL_PORT": 7880,
    "LIVEKIT_RTC_TCP_PORT": 7881,
    "LIVEKIT_RTC_UDP_PORT": 7882,
    "LIVEKIT_TURN_TLS_PORT": 5349,
    "KAEDE_TURN_UDP_PORT": 13478,
}
AUTO_UPDATE_DURATION_RE = re.compile(r"^[1-9][0-9]*(?:s|m|min|h|d|w)$")
AUTO_UPDATE_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
AUTO_UPDATE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
AUTO_UPDATE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
MEDIA_MAX_ATTACHMENT_BYTES_DEFAULT = 15 * 1024**2
MEDIA_INFLIGHT_QUOTA_BYTES_DEFAULT = 500 * 1024**2
REMOTE_MEDIA_CACHE_BYTES_DEFAULT = 100 * 1024**3
FEDERATION_INTEGER_DEFAULTS = {
    "KAEDE_FEDERATION_CLOCK_SKEW_SECONDS": 300,
    "KAEDE_FEDERATION_EVENT_RETENTION_DAYS": 30,
    "KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN": 5_000_000,
    "KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN": 16 * 1024**3,
    "KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL": 50_000_000,
    "KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL": 160 * 1024**3,
    "KAEDE_FEDERATION_MAX_REMOTE_INSTANCES": 10_000,
    "KAEDE_FEDERATION_PEER_KEY_HISTORY_LIMIT": 512,
    "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT": 1_000,
    "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT_ORIGIN": 100,
    "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_ORIGIN": 10_000,
    "KAEDE_FEDERATION_REMOTE_USERS_PER_INTRODUCER": 100_000,
    "KAEDE_FEDERATION_THIRD_PARTY_INSTANCES_PER_INTRODUCER": 1_000,
    "KAEDE_FEDERATION_REMOTE_MEDIA_TOMBSTONES_PER_ORIGIN": 100_000,
    "KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_AUTHORITY": 1_000_000,
    "KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_REMOTE_ORIGIN": 100_000,
    "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_CONVERSATION": 5_000_000,
    "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_AUTHORITY": 50_000_000,
    "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_REMOTE_ORIGIN": 10_000_000,
    "KAEDE_FEDERATION_DM_MAX_BYTES_PER_CONVERSATION": 32 * 1024**3,
    "KAEDE_FEDERATION_DM_MAX_BYTES_PER_AUTHORITY": 320 * 1024**3,
    "KAEDE_FEDERATION_DM_MAX_BYTES_PER_REMOTE_ORIGIN": 64 * 1024**3,
    "KAEDE_FEDERATION_DM_REPLICA_CACHE_MESSAGES_PER_CONVERSATION": 250_000,
    "KAEDE_FEDERATION_DM_REPLICA_CACHE_BYTES_PER_CONVERSATION": 2 * 1024**3,
    "KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_GUILD": 20_000_000,
    "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD": 64 * 1024**3,
    "KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_ORIGIN": 100_000_000,
    "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN": 320 * 1024**3,
    "KAEDE_FEDERATION_REMOTE_IDENTITY_RETENTION_DAYS": 30,
    "KAEDE_FEDERATION_REMOTE_IDENTITY_GC_BATCH_SIZE": 5_000,
    "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN": 256 * 1024**2,
    "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_TOTAL": 512 * 1024**2,
    "KAEDE_FEDERATION_HISTORY_EXPORT_TTL_MINUTES": 1_440,
    "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_PER_ORIGIN": 1_000,
    "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_TOTAL": 10_000,
    "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_PER_ORIGIN": 100_000,
    "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_TOTAL": 1_000_000,
    "KAEDE_FEDERATION_HISTORY_PAGE_MESSAGES": 100,
    "KAEDE_FEDERATION_HISTORY_PAGE_BYTES": 512 * 1024,
    "KAEDE_FEDERATION_HISTORY_MAX_MESSAGES": 2_000_000,
    "KAEDE_FEDERATION_HISTORY_MAX_BYTES": 32 * 1024**3,
    "KAEDE_FEDERATION_HISTORY_MAX_PAGES": 250_000,
    "KAEDE_FEDERATION_HISTORY_MAX_REACTIONS": 10_000_000,
    "KAEDE_FEDERATION_HISTORY_MAX_DURATION_SECONDS": 7_200,
    "KAEDE_FEDERATION_HISTORY_MERGE_CHUNK_SIZE": 500,
}


class DeploymentConfigurationError(ValueError):
    """The selected deployment environment is unsafe or ambiguous."""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DeploymentConfigurationError(
            f"operator environment file does not exist or is not a regular file: {path}. "
            "Run `make setup` to create it, or pass --file with the correct path"
        )
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise DeploymentConfigurationError(
            f"operator environment file is not valid UTF-8: {path}. Save it as UTF-8 "
            "text, then rerun validation"
        ) from error
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise DeploymentConfigurationError(
            f"cannot read operator environment file {path}: {reason}. Check file "
            "ownership and permissions, then rerun validation"
        ) from error
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not KEY_RE.fullmatch(key):
            raise DeploymentConfigurationError(
                f"{path}:{line_number}: expected one KEY=value assignment with a shell-style "
                "variable name, for example KAEDE_DOMAIN=chat.example.net"
            )
        if key in values:
            raise DeploymentConfigurationError(
                f"{path}:{line_number}: duplicate setting {key}; keep exactly one assignment"
            )
        values[key] = _unquote(value.strip())
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def _deployment_port(values: dict[str, str], name: str, default: int) -> int:
    raw_value = values.get(name, str(default)).strip()
    if not raw_value.isdecimal():
        raise DeploymentConfigurationError(
            f"{name} must be an integer from 1024 to 65535; received {raw_value!r}"
        )
    port = int(raw_value)
    if not 1024 <= port <= 65535:
        raise DeploymentConfigurationError(
            f"{name} must be an integer from 1024 to 65535; received {port}"
        )
    return port


def _validate_voice_ports(values: dict[str, str]) -> None:
    ports = {
        name: _deployment_port(values, name, default)
        for name, default in VOICE_PORT_DEFAULTS.items()
    }
    duplicates: dict[int, list[str]] = {}
    for name, port in ports.items():
        duplicates.setdefault(port, []).append(name)
    collision = next((names for names in duplicates.values() if len(names) > 1), None)
    if collision:
        raise DeploymentConfigurationError(
            "LiveKit host ports must be distinct; conflicting settings: "
            + ", ".join(collision)
        )

    for name, default in (
        ("KAEDE_CADDY_HOST_PORT", 18081),
        ("KAEDE_API_HOST_PORT", 18082),
    ):
        host_port = _deployment_port(values, name, default)
        if host_port in duplicates:
            raise DeploymentConfigurationError(
                f"{name} conflicts with {duplicates[host_port][0]} on port {host_port}"
            )

    livekit_url = values.get(
        "KAEDE_VOICE_LIVEKIT_URL",
        f"http://host.docker.internal:{ports['LIVEKIT_CONTROL_PORT']}",
    ).strip()
    parsed = urlsplit(livekit_url)
    try:
        url_port = parsed.port
    except ValueError as error:
        raise DeploymentConfigurationError(
            "KAEDE_VOICE_LIVEKIT_URL has an invalid port"
        ) from error
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"host.docker.internal", "127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise DeploymentConfigurationError(
            "KAEDE_VOICE_LIVEKIT_URL must be a local HTTP control-plane URL"
        )
    if url_port != ports["LIVEKIT_CONTROL_PORT"]:
        raise DeploymentConfigurationError(
            "KAEDE_VOICE_LIVEKIT_URL port must match LIVEKIT_CONTROL_PORT; "
            f"URL uses {url_port!r}, but LIVEKIT_CONTROL_PORT is "
            f"{ports['LIVEKIT_CONTROL_PORT']}"
        )


def _validate_fcm_service_account(encoded: str) -> None:
    try:
        decoded = base64.b64decode(encoded, validate=True)
        if not decoded or len(decoded) > 64 * 1024:
            raise ValueError
        document = json.loads(decoded)
    except (
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValueError,
    ) as error:
        raise DeploymentConfigurationError(
            "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64 must be base64-encoded Firebase "
            "service-account JSON no larger than 64 KiB"
        ) from error
    required = ("project_id", "client_email", "private_key", "token_uri")
    if (
        not isinstance(document, dict)
        or document.get("type") != "service_account"
        or any(
            not isinstance(document.get(name), str) or not document[name].strip()
            for name in required
        )
        or document["token_uri"] != FCM_AUTH_ENDPOINT
    ):
        raise DeploymentConfigurationError(
            "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64 is not a valid Firebase service account. "
            "Use a service-account JSON file (not google-services.json) with type, "
            "project_id, client_email, private_key, and Google's OAuth token_uri; rerun "
            "`make setup` to import it safely"
        )


def _validate_auto_update(values: dict[str, str]) -> None:
    enabled = values.get("AUTO_UPDATE_ENABLED", "false").strip().lower()
    if enabled not in {"true", "false"}:
        raise DeploymentConfigurationError("AUTO_UPDATE_ENABLED must be true or false")
    remote = values.get("AUTO_UPDATE_REMOTE", "origin").strip()
    if not AUTO_UPDATE_REMOTE_RE.fullmatch(remote):
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_REMOTE must be a Git remote name containing only letters, "
            "digits, dot, underscore, or dash. Do not put a remote URL or "
            "credentials in this setting"
        )
    branch = values.get("AUTO_UPDATE_BRANCH", "main").strip()
    if (
        not AUTO_UPDATE_BRANCH_RE.fullmatch(branch)
        or branch.endswith("/")
        or ".." in branch
    ):
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_BRANCH must be a branch name containing only letters, digits, "
            "dot, underscore, dash, or slash, without '..' or a trailing slash"
        )
    interval = values.get("AUTO_UPDATE_INTERVAL", "6h").strip()
    if interval not in {"6h", "12h", "1d", "1w"}:
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_INTERVAL must be 6h, 12h, 1d, or 1w"
        )
    jitter = values.get("AUTO_UPDATE_JITTER", "30m").strip()
    if not AUTO_UPDATE_DURATION_RE.fullmatch(jitter):
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_JITTER must be a positive systemd duration such as 30m"
        )
    timeout = values.get("AUTO_UPDATE_WAIT_TIMEOUT_SECONDS", "300").strip()
    if not timeout.isdecimal() or not 60 <= int(timeout) <= 3600:
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_WAIT_TIMEOUT_SECONDS must be an integer from 60 through 3600"
        )
    backup_hook = values.get("AUTO_UPDATE_BACKUP_HOOK", "").strip()
    if backup_hook and (
        not AUTO_UPDATE_PATH_RE.fullmatch(backup_hook) or ".." in backup_hook
    ):
        raise DeploymentConfigurationError(
            "AUTO_UPDATE_BACKUP_HOOK must be an absolute path containing only letters, "
            "digits, dot, underscore, dash, and slash, without '..'"
        )


def _validate_federation_budgets(values: dict[str, str]) -> None:
    configured: dict[str, int] = {}
    for name, default in FEDERATION_INTEGER_DEFAULTS.items():
        raw_value = values.get(name, str(default)).strip()
        if not re.fullmatch(r"[0-9]+", raw_value) or int(raw_value) <= 0:
            raise DeploymentConfigurationError(
                f"{name} must be a positive integer number of rows, bytes, or time units"
            )
        configured[name] = int(raw_value)

    remote_cache_bytes = values.get(
        "KAEDE_MEDIA_REMOTE_CACHE_BYTES", str(REMOTE_MEDIA_CACHE_BYTES_DEFAULT)
    ).strip()
    if not re.fullmatch(r"[0-9]+", remote_cache_bytes) or int(remote_cache_bytes) <= 0:
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_REMOTE_CACHE_BYTES must be a positive integer number of bytes"
        )
    maximum_attachment_bytes = values.get(
        "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES", str(MEDIA_MAX_ATTACHMENT_BYTES_DEFAULT)
    ).strip()
    if (
        not re.fullmatch(r"[0-9]+", maximum_attachment_bytes)
        or int(maximum_attachment_bytes) <= 0
    ):
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES must be a positive integer number of bytes"
        )
    media_inflight_quota_bytes = values.get(
        "KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES", str(MEDIA_INFLIGHT_QUOTA_BYTES_DEFAULT)
    ).strip()
    if (
        not re.fullmatch(r"[0-9]+", media_inflight_quota_bytes)
        or int(media_inflight_quota_bytes) <= 0
    ):
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES must be a positive integer number of bytes"
        )
    if int(maximum_attachment_bytes) > int(media_inflight_quota_bytes):
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES cannot exceed "
            "KAEDE_MEDIA_INFLIGHT_QUOTA_BYTES"
        )
    if (
        int(maximum_attachment_bytes)
        > configured["KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN"]
    ):
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_MAX_ATTACHMENT_BYTES cannot exceed "
            "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN"
        )

    history_enabled = values.get("KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED", "true")
    if history_enabled.strip().lower() not in {"true", "false"}:
        raise DeploymentConfigurationError(
            "KAEDE_FEDERATION_HISTORY_IMPORT_ENABLED must be true or false"
        )

    non_strict_bounds = (
        (
            "KAEDE_FEDERATION_INBOX_MAX_EVENTS_PER_ORIGIN",
            "KAEDE_FEDERATION_INBOX_MAX_EVENTS_TOTAL",
        ),
        (
            "KAEDE_FEDERATION_INBOX_MAX_BYTES_PER_ORIGIN",
            "KAEDE_FEDERATION_INBOX_MAX_BYTES_TOTAL",
        ),
        (
            "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT_ORIGIN",
            "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT",
        ),
        (
            "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_RECIPIENT_ORIGIN",
            "KAEDE_FEDERATION_PENDING_RELATIONSHIPS_PER_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_CONVERSATION",
            "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_REMOTE_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_DM_MAX_BYTES_PER_CONVERSATION",
            "KAEDE_FEDERATION_DM_MAX_BYTES_PER_REMOTE_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_DM_REPLICA_CACHE_MESSAGES_PER_CONVERSATION",
            "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_CONVERSATION",
        ),
        (
            "KAEDE_FEDERATION_DM_REPLICA_CACHE_BYTES_PER_CONVERSATION",
            "KAEDE_FEDERATION_DM_MAX_BYTES_PER_CONVERSATION",
        ),
        (
            "KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_GUILD",
            "KAEDE_FEDERATION_REPLICA_MAX_ROWS_PER_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_GUILD",
            "KAEDE_FEDERATION_REPLICA_MAX_BYTES_PER_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_PER_ORIGIN",
            "KAEDE_FEDERATION_REMOTE_MEDIA_INFLIGHT_BYTES_TOTAL",
        ),
        (
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_PER_ORIGIN",
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_TOTAL",
        ),
        (
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_PER_ORIGIN",
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_TOTAL",
        ),
        (
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_PER_ORIGIN",
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_PER_ORIGIN",
        ),
        (
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_EXPORTS_TOTAL",
            "KAEDE_FEDERATION_HISTORY_MAX_ACTIVE_CHANNEL_GRANTS_TOTAL",
        ),
        ("KAEDE_FEDERATION_HISTORY_PAGE_BYTES", "KAEDE_FEDERATION_HISTORY_MAX_BYTES"),
    )
    for lower_name, upper_name in non_strict_bounds:
        if configured[lower_name] > configured[upper_name]:
            raise DeploymentConfigurationError(
                f"{lower_name} cannot exceed {upper_name}"
            )

    strict_bounds = (
        (
            "KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_REMOTE_ORIGIN",
            "KAEDE_FEDERATION_DM_MAX_CONVERSATIONS_PER_AUTHORITY",
        ),
        (
            "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_REMOTE_ORIGIN",
            "KAEDE_FEDERATION_DM_MAX_MESSAGES_PER_AUTHORITY",
        ),
        (
            "KAEDE_FEDERATION_DM_MAX_BYTES_PER_REMOTE_ORIGIN",
            "KAEDE_FEDERATION_DM_MAX_BYTES_PER_AUTHORITY",
        ),
    )
    for lower_name, upper_name in strict_bounds:
        if configured[lower_name] >= configured[upper_name]:
            raise DeploymentConfigurationError(
                f"{lower_name} must be below {upper_name}"
            )


def validate_values(values: dict[str, str], *, observability: bool) -> None:
    _validate_auto_update(values)
    _validate_federation_budgets(values)
    environment = values.get("KAEDE_ENVIRONMENT", "production").strip().lower()
    allow_nonproduction = (
        values.get("ALLOW_NONPRODUCTION_DEPLOYMENT", "").lower() == "true"
    )
    if environment != "production":
        if not allow_nonproduction:
            raise DeploymentConfigurationError(
                "the production Compose topology requires KAEDE_ENVIRONMENT=production; "
                "use deploy/compose.dev.yml or the isolated validation targets"
            )
        return

    for name in sorted(SENSITIVE_NAMES):
        value = values.get(name, "").strip()
        if value and _is_placeholder(value):
            raise DeploymentConfigurationError(
                f"{name} still contains a documented placeholder"
            )

    if (
        values.get("KAEDE_KLIPY_ENABLED", "false").strip().lower() == "true"
        and not values.get("KAEDE_KLIPY_API_KEY", "").strip()
    ):
        raise DeploymentConfigurationError(
            "KAEDE_KLIPY_API_KEY is required when KLIPY is enabled"
        )
    if values.get("KAEDE_TURNSTILE_ENABLED", "false").strip().lower() == "true":
        if (
            not values.get("KAEDE_TURNSTILE_SITE_KEY", "").strip()
            or not values.get("TURNSTILE_SECRET", "").strip()
        ):
            raise DeploymentConfigurationError(
                "KAEDE_TURNSTILE_SITE_KEY and TURNSTILE_SECRET are required when Turnstile is enabled"
            )
    if values.get("KAEDE_PUSH_ENABLED", "false").strip().lower() == "true":
        push_credential = values.get("KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64", "").strip()
        if not push_credential:
            raise DeploymentConfigurationError(
                "KAEDE_PUSH_FCM_SERVICE_ACCOUNT_B64 is required when mobile push is enabled"
            )
        _validate_fcm_service_account(push_credential)

    if values.get("KAEDE_MEDIA_SCAN_ENABLED", "true").strip().lower() != "true":
        raise DeploymentConfigurationError(
            "KAEDE_MEDIA_SCAN_ENABLED must be true in production. Set it to true and "
            "rerun `make env-check`"
        )

    if values.get("KAEDE_VOICE_ENABLED", "false").strip().lower() == "true":
        _validate_voice_ports(values)

    domain = values.get("KAEDE_DOMAIN", "").strip().lower().removesuffix(".")
    if domain.endswith(".example.com"):
        raise DeploymentConfigurationError(
            "KAEDE_DOMAIN must not use the example.com template"
        )

    smtp_url = values.get("KAEDE_SMTP_URL", "").strip()
    if values.get("KAEDE_EMAIL_BACKEND", "console") == "smtp" and smtp_url:
        parsed = urlsplit(smtp_url)
        if (parsed.hostname or "").endswith(".example.com"):
            raise DeploymentConfigurationError(
                "KAEDE_SMTP_URL must not use the example.com template"
            )

    if observability:
        password = values.get("GRAFANA_ADMIN_PASSWORD", "")
        if len(password) < 20 or _is_placeholder(password):
            raise DeploymentConfigurationError(
                "GRAFANA_ADMIN_PASSWORD must contain at least 20 non-placeholder characters"
            )


def validate_file_permissions(path: Path, values: dict[str, str]) -> None:
    if values.get("KAEDE_ENVIRONMENT", "production").strip().lower() != "production":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise DeploymentConfigurationError(
            "operator environment contains secrets and must not be group/world "
            f"accessible (mode is {mode:04o}); run `chmod 600 {path}`"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate Kaede Chat deployment settings before starting services."
    )
    parser.add_argument("--file", type=Path, help="path to the operator .env file")
    parser.add_argument(
        "--file-only",
        action="store_true",
        help="validate only --file instead of the current process environment",
    )
    parser.add_argument(
        "--observability",
        action="store_true",
        help="also enforce observability-profile settings",
    )
    arguments = parser.parse_args()

    if arguments.file is not None:
        values = read_env_file(arguments.file)
        validate_file_permissions(arguments.file, values)
        if arguments.file_only:
            validate_values(values, observability=arguments.observability)
            print("operator environment file validation passed")
            return
    elif arguments.file_only:
        raise DeploymentConfigurationError(
            "--file-only requires --file PATH; for example, --file .env --file-only"
        )

    validate_values(dict(os.environ), observability=arguments.observability)
    print("deployment environment validation passed")


if __name__ == "__main__":
    try:
        main()
    except DeploymentConfigurationError as error:
        raise SystemExit(f"configuration error: {error}") from error
