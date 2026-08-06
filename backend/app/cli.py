from __future__ import annotations

import asyncio
import base64
import binascii
import os
import re
import secrets
from collections.abc import Mapping
from typing import Annotated
from urllib.parse import urlsplit

import typer

from app.bootstrap import (
    IdentityKeyError,
    bootstrap_instance,
    retire_instance_signing_key,
    rotate_instance_signing_key,
)
from app.core.settings import get_settings
from app.db.session import create_engine_and_sessionmaker

cli = typer.Typer(no_args_is_help=True)

_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_GARAGE_ACCESS_KEY = re.compile(r"^GK[0-9a-fA-F]{32}$")
_DEPLOY_TOKEN = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")


def validate_external_secrets(
    environment: str,
    values: Mapping[str, str],
    *,
    include_voice: bool = True,
) -> None:
    """Reject deploy-service placeholders before a production stack can start."""

    if environment != "production":
        return
    minimum_lengths = {
        "KAEDE_EDGE_SECRET": 32,
        "KAEDE_GATEWAY_SECRET_KEY": 43,
        "KAEDE_PROXY_SECRET": 32,
        "POSTGRES_PASSWORD": 16,
        "DRAGONFLY_PASSWORD": 32,
    }
    if values.get("KAEDE_ADMIN_TOKEN"):
        minimum_lengths["KAEDE_ADMIN_TOKEN"] = 32
    storage_backend = values.get("KAEDE_MEDIA_STORAGE_BACKEND", "garage")
    if storage_backend not in {"garage", "s3"}:
        raise ValueError("KAEDE_MEDIA_STORAGE_BACKEND must be garage or s3")
    if storage_backend == "garage":
        minimum_lengths["GARAGE_ADMIN_TOKEN"] = 32
    if include_voice:
        minimum_lengths.update({"LIVEKIT_API_KEY": 12, "LIVEKIT_API_SECRET": 32})
    for name, minimum in minimum_lengths.items():
        value = values.get(name, "")
        if (
            len(value) < minimum
            or value.lower().startswith(("replace", "change-me"))
            or (
                name
                in {
                    "KAEDE_EDGE_SECRET",
                    "KAEDE_GATEWAY_SECRET_KEY",
                    "KAEDE_PROXY_SECRET",
                    "KAEDE_ADMIN_TOKEN",
                    "DRAGONFLY_PASSWORD",
                    "LIVEKIT_API_KEY",
                    "LIVEKIT_API_SECRET",
                }
                and not _DEPLOY_TOKEN.fullmatch(value)
            )
        ):
            raise ValueError(f"{name} must be replaced with at least {minimum} random characters")
    if (
        values.get("KAEDE_EMAIL_BACKEND") == "smtp"
        and "replace" in values.get("KAEDE_SMTP_URL", "").lower()
    ):
        raise ValueError("KAEDE_SMTP_URL must not contain placeholder credentials")
    if values.get("KAEDE_EMAIL_BACKEND") == "mailtrap_api" and values.get(
        "KAEDE_MAILTRAP_API_TOKEN", ""
    ).lower().startswith(("replace", "change-me")):
        raise ValueError("KAEDE_MAILTRAP_API_TOKEN must not be a placeholder")
    dragonfly_password = values.get("DRAGONFLY_PASSWORD", "")
    dragonfly_url = values.get("KAEDE_DRAGONFLY_URL", "")
    if urlsplit(dragonfly_url).password != dragonfly_password:
        raise ValueError("KAEDE_DRAGONFLY_URL must contain DRAGONFLY_PASSWORD")
    postgres_password = values.get("POSTGRES_PASSWORD", "")
    database_url = values.get("KAEDE_DATABASE_URL", "")
    if urlsplit(database_url).password != postgres_password:
        raise ValueError("KAEDE_DATABASE_URL must contain POSTGRES_PASSWORD")
    if values.get("KAEDE_EDGE_SECRET") == values.get("KAEDE_PROXY_SECRET"):
        raise ValueError("KAEDE_EDGE_SECRET must differ from KAEDE_PROXY_SECRET")
    gateway_key = values.get("KAEDE_GATEWAY_SECRET_KEY", "")
    master_key = values.get("KAEDE_SECRET_KEY", "")
    try:
        decoded_gateway_key = base64.b64decode(
            gateway_key + "=" * (-len(gateway_key) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("KAEDE_GATEWAY_SECRET_KEY must be URL-safe base64") from exc
    if len(decoded_gateway_key) != 32:
        raise ValueError("KAEDE_GATEWAY_SECRET_KEY must decode to exactly 32 bytes")
    try:
        decoded_master_key = base64.b64decode(
            master_key + "=" * (-len(master_key) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("KAEDE_SECRET_KEY must be URL-safe base64") from exc
    if len(decoded_master_key) != 32:
        raise ValueError("KAEDE_SECRET_KEY must decode to exactly 32 bytes")
    if secrets.compare_digest(decoded_gateway_key, decoded_master_key):
        raise ValueError("KAEDE_GATEWAY_SECRET_KEY must differ from KAEDE_SECRET_KEY")
    access_key = values.get("KAEDE_MEDIA_S3_ACCESS_KEY", "")
    secret_key = values.get("KAEDE_MEDIA_S3_SECRET_KEY", "")
    if storage_backend == "garage":
        if not _HEX_64.fullmatch(values.get("GARAGE_RPC_SECRET", "")):
            raise ValueError("GARAGE_RPC_SECRET must be 64 hexadecimal characters")
        if not _GARAGE_ACCESS_KEY.fullmatch(access_key):
            raise ValueError(
                "KAEDE_MEDIA_S3_ACCESS_KEY must be GK followed by 32 hexadecimal characters "
                "when Garage is selected"
            )
        if not _HEX_64.fullmatch(secret_key):
            raise ValueError(
                "KAEDE_MEDIA_S3_SECRET_KEY must be 64 hexadecimal characters "
                "when Garage is selected"
            )
    else:
        for name, value in (
            ("KAEDE_MEDIA_S3_ACCESS_KEY", access_key),
            ("KAEDE_MEDIA_S3_SECRET_KEY", secret_key),
        ):
            if len(value) < 16 or value.lower().startswith(("replace", "change-me")):
                raise ValueError(f"{name} must contain a non-placeholder S3 credential")
    if include_voice:
        if values.get("KAEDE_VOICE_ENABLED", "") != "true":
            raise ValueError("KAEDE_VOICE_ENABLED must be true when the voice profile is enabled")
        for name in ("LIVEKIT_TURN_CERT_PATH", "LIVEKIT_TURN_KEY_PATH"):
            value = values.get(name, "")
            if not value.startswith("/") or "not-used" in value or "replace" in value:
                raise ValueError(f"{name} must be an absolute production certificate path")


@cli.callback()
def root() -> None:
    """Kaede Chat operator commands."""


@cli.command()
def bootstrap() -> None:
    """Create or verify the durable identity of this Kaede instance."""

    async def run() -> None:
        settings = get_settings()
        engine, sessionmaker = create_engine_and_sessionmaker(
            settings.database_url.get_secret_value()
        )
        try:
            async with sessionmaker() as session:
                instance = await bootstrap_instance(session, settings)
                typer.echo(f"instance identity ready: {instance.domain}")
        finally:
            await engine.dispose()

    asyncio.run(run())


@cli.command("rotate-key")
def rotate_key() -> None:
    """Rotate the instance Ed25519 signing key and retain old verification keys."""

    async def run() -> None:
        settings = get_settings()
        engine, sessionmaker = create_engine_and_sessionmaker(
            settings.database_url.get_secret_value()
        )
        try:
            async with sessionmaker() as session:
                instance = await rotate_instance_signing_key(session, settings)
                typer.echo(f"instance signing key rotated: {instance.current_key_id}")
        finally:
            await engine.dispose()

    asyncio.run(run())


@cli.command("retire-key")
def retire_key(
    key_id: Annotated[str, typer.Argument(help="Historical Ed25519 key ID to retire.")],
    force_compromised: Annotated[
        bool,
        typer.Option(
            "--force-compromised",
            help=(
                "DANGER: bypass the safe overlap because this private key is compromised; "
                "queued envelopes signed by it may become unverifiable immediately."
            ),
        ),
    ] = False,
) -> None:
    """Retire a historical signing key after its safe overlap deadline."""

    async def run() -> None:
        settings = get_settings()
        engine, sessionmaker = create_engine_and_sessionmaker(
            settings.database_url.get_secret_value()
        )
        try:
            async with sessionmaker() as session:
                if force_compromised:
                    typer.echo(
                        "WARNING: forcing immediate compromise retirement; queued signed "
                        "envelopes may fail verification.",
                        err=True,
                    )
                try:
                    retired = await retire_instance_signing_key(
                        session,
                        settings,
                        key_id,
                        force_compromised=force_compromised,
                    )
                except IdentityKeyError as exc:
                    raise typer.BadParameter(str(exc)) from exc
                typer.echo(f"instance signing key retired: {retired.key_id}")
        finally:
            await engine.dispose()

    asyncio.run(run())


@cli.command()
def preflight(
    voice: Annotated[
        bool,
        typer.Option("--voice", help="Also validate LiveKit/TURN configuration."),
    ] = False,
) -> None:
    """Validate app and external service configuration without connecting to services."""

    settings = get_settings()
    try:
        validate_external_secrets(settings.environment, os.environ, include_voice=voice)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo("configuration preflight passed")


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
