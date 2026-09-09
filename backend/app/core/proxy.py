from __future__ import annotations

import hmac
import ipaddress

from starlette.requests import HTTPConnection

from app.core.settings import Settings


def canonical_ip(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def resolve_client_ip(
    *,
    supplied_secret: str | None,
    configured_secret: str | None,
    forwarded_for: str | None,
    direct_host: str | None,
) -> str:
    """Trust exactly one proxy-sanitized address from an authenticated hop."""
    if (
        supplied_secret is not None
        and configured_secret is not None
        and hmac.compare_digest(supplied_secret, configured_secret)
    ):
        forwarded = canonical_ip(forwarded_for)
        if forwarded is not None:
            return forwarded
    return canonical_ip(direct_host) or "unknown"


def connection_client_ip(connection: HTTPConnection, settings: Settings) -> str:
    """Apply the same authenticated-proxy policy to HTTP and WebSocket peers."""
    return resolve_client_ip(
        supplied_secret=connection.headers.get("X-Kaede-Proxy-Secret"),
        configured_secret=(
            settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
        ),
        forwarded_for=connection.headers.get("X-Forwarded-For"),
        direct_host=connection.client.host if connection.client is not None else None,
    )
