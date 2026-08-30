from __future__ import annotations

from urllib.parse import urljoin, urlsplit


class MediaURLValidationError(ValueError):
    """A server-provided media capability escaped its authenticated authority."""


def _url_origin(value: str, *, label: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise MediaURLValidationError(f"the {label} has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise MediaURLValidationError(f"the {label} is not a safe HTTPS URL")
    return parsed.scheme, parsed.hostname, None if port == 443 else port


def _render_origin(origin: tuple[str, str, int | None]) -> str:
    scheme, host, port = origin
    rendered_host = f"[{host}]" if ":" in host else host
    rendered_port = f":{port}" if port is not None else ""
    return f"{scheme}://{rendered_host}{rendered_port}"


def validate_signed_media_url(location: str, media_origin: str) -> str:
    """Bind a signed media capability to its exact signed HTTPS origin."""

    origin = urlsplit(media_origin)
    expected = _url_origin(media_origin, label="signed media origin")
    canonical_origin = _render_origin(expected)
    if (
        origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or media_origin.rstrip("/") != canonical_origin
        or _url_origin(location, label="media URL") != expected
    ):
        raise MediaURLValidationError(
            "the media URL is outside its authority-signed HTTPS origin"
        )
    return location


def media_url_origin(location: str) -> str:
    """Return the canonical HTTPS origin of a validated media URL."""

    return _render_origin(_url_origin(location, label="media URL"))


def target_authority_domain(target_origin: str) -> str:
    """Return the DNS authority from an already authenticated API target."""

    parsed = urlsplit(target_origin)
    try:
        _target_port = parsed.port
    except ValueError as exc:
        raise MediaURLValidationError("the API target has an invalid port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or parsed.path not in {"", "/"}
    ):
        raise MediaURLValidationError("the API target is not a safe HTTPS origin")
    return parsed.hostname


def validate_authority_media_url(location: str, authority_domain: str) -> str:
    """Bind a server-provided media capability to ``media.<authority>``.

    Query strings and paths are intentionally preserved because object-storage
    signatures live there. Non-default ports are limited to ``.localhost``
    development authorities, matching Kaede's established local media setup.
    """

    parsed = urlsplit(location)
    try:
        port = parsed.port
    except ValueError as exc:
        raise MediaURLValidationError("the media URL has an invalid port") from exc
    authority = authority_domain.lower()
    development_port = authority.endswith(".localhost")
    if (
        parsed.scheme != "https"
        or parsed.hostname != f"media.{authority}"
        or (port not in {None, 443} and not development_port)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise MediaURLValidationError(
            "the media URL is not on the target authority's safe HTTPS media host"
        )
    return location


def validate_target_media_url(location: str, target_origin: str) -> str:
    """Validate a media capability against an authenticated API target."""

    return validate_authority_media_url(
        location,
        target_authority_domain(target_origin),
    )


def resolve_target_media_location(
    location: str,
    *,
    target_origin: str,
    base_url: str | None = None,
) -> str:
    """Resolve and validate a media ``Location`` without trusting its host."""

    resolved = urljoin(base_url or target_origin, location)
    return validate_target_media_url(resolved, target_origin)
