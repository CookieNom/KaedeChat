from __future__ import annotations

import hmac
import ipaddress


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
