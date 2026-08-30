from __future__ import annotations

from app.core.settings import DOMAIN_RE


def normalize_custom_expression_domain(value: str) -> str:
    """Canonicalize the authority domain embedded in an expression token."""

    domain = value.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise ValueError("invalid federation domain")
    return domain
