"""Validate the operator environment at the deployment boundary.

The application settings validator sees the environment passed to a process.  A
Compose ``--env-file`` is otherwise only an interpolation source, which can hide
misspelled variables from that validator.  Production preflight loads the same
file through ``env_file`` and invokes this small, dependency-free guard before
the application preflight.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit


KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
    "KAEDE_MEDIA_S3_ACCESS_KEY",
    "KAEDE_MEDIA_S3_SECRET_KEY",
    "KAEDE_MEDIA_S3_SESSION_TOKEN",
    "KAEDE_PROXY_SECRET",
    "KAEDE_SECRET_KEY",
    "KAEDE_SMTP_URL",
    "LIVEKIT_API_KEY",
    "LIVEKIT_API_SECRET",
    "POSTGRES_PASSWORD",
}


class DeploymentConfigurationError(ValueError):
    """The selected deployment environment is unsafe or ambiguous."""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DeploymentConfigurationError(f"operator environment is not a file: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not KEY_RE.fullmatch(key):
            raise DeploymentConfigurationError(
                f"{path}:{line_number}: expected one KEY=value assignment"
            )
        if key in values:
            raise DeploymentConfigurationError(f"{path}:{line_number}: duplicate setting {key}")
        values[key] = _unquote(value.strip())
    return values


def _is_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def validate_values(values: dict[str, str], *, observability: bool) -> None:
    environment = values.get("KAEDE_ENVIRONMENT", "production").strip().lower()
    allow_nonproduction = values.get("ALLOW_NONPRODUCTION_DEPLOYMENT", "").lower() == "true"
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
            raise DeploymentConfigurationError(f"{name} still contains a documented placeholder")

    if values.get("KAEDE_MEDIA_SCAN_ENABLED", "true").strip().lower() != "true":
        raise DeploymentConfigurationError("KAEDE_MEDIA_SCAN_ENABLED must be true in production")

    domain = values.get("KAEDE_DOMAIN", "").strip().lower().removesuffix(".")
    if domain.endswith(".example.com"):
        raise DeploymentConfigurationError("KAEDE_DOMAIN must not use the example.com template")

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
            f"operator environment must not be group/world accessible (mode is {mode:04o})"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path)
    parser.add_argument("--file-only", action="store_true")
    parser.add_argument("--observability", action="store_true")
    arguments = parser.parse_args()

    if arguments.file is not None:
        values = read_env_file(arguments.file)
        validate_file_permissions(arguments.file, values)
        if arguments.file_only:
            validate_values(values, observability=arguments.observability)
            print("operator environment file validation passed")
            return
    elif arguments.file_only:
        raise DeploymentConfigurationError("--file-only requires --file")

    validate_values(dict(os.environ), observability=arguments.observability)
    print("deployment environment validation passed")


if __name__ == "__main__":
    try:
        main()
    except DeploymentConfigurationError as error:
        raise SystemExit(f"configuration error: {error}") from error
