from __future__ import annotations

import hashlib
import hmac
import json
from html.parser import HTMLParser
from typing import Any, cast
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.api.dependencies import AuthenticatedUser, get_redis, require_user
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings
from app.federation.network import (
    FederationNetworkError,
    PinnedNetworkBackend,
    bounded_http_request,
    public_addresses,
)

router = APIRouter(prefix="/api/v1/link-previews", tags=["link previews"])

MAX_URL_LENGTH = 2048
MAX_HTML_BYTES = 512 * 1024
MAX_MEDIA_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 3
PREVIEW_CACHE_SECONDS = 15 * 60
MEDIA_CAPABILITY_SECONDS = 24 * 60 * 60
MEDIA_PATH_PATTERN = r"^[a-f0-9]{48}$"


class PreviewRequest(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)


class MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title_parts: list[str] = []
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True
            return
        if tag.lower() != "meta":
            return
        values = {name.lower(): value for name, value in attrs if value is not None}
        key = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content", "").strip()
        if key and content and key not in self.metadata:
            self.metadata[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title and len("".join(self.title_parts)) < 512:
            self.title_parts.append(data)


def normalize_preview_url(value: str) -> str:
    value = value.strip()
    if len(value) > MAX_URL_LENGTH:
        raise ValueError("URL is too long")
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only HTTP and HTTPS links can be previewed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credential-bearing links cannot be previewed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Link port is invalid") from exc
    if port not in {None, 80, 443}:
        raise ValueError("Only standard web ports can be previewed")
    hostname = parsed.hostname.rstrip(".").lower()
    if not hostname:
        raise ValueError("Link hostname is invalid")
    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    if port is not None and port != default_port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def preview_metadata(html: str, final_url: str) -> dict[str, str | None]:
    parser = MetadataParser()
    parser.feed(html)
    meta = parser.metadata

    def clean(value: str | None, limit: int) -> str | None:
        if value is None:
            return None
        collapsed = " ".join(value.split())
        return collapsed[:limit] or None

    media = meta.get("og:image") or meta.get("twitter:image")
    video = meta.get("og:video") or meta.get("og:video:url")
    media_url: str | None = None
    media_type: str | None = None
    for candidate, candidate_type in ((media, "image"), (video, "video")):
        if not candidate:
            continue
        try:
            media_url = normalize_preview_url(urljoin(final_url, candidate))
        except ValueError:
            continue
        media_type = candidate_type
        break
    title = clean(meta.get("og:title") or "".join(parser.title_parts), 240)
    description = clean(meta.get("og:description") or meta.get("description"), 500)
    site_name = clean(meta.get("og:site_name"), 100)
    return {
        "title": title,
        "description": description,
        "site_name": site_name,
        "media_source": media_url,
        "media_type": media_type,
    }


async def pinned_client(url: str) -> httpx.AsyncClient:
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise FederationNetworkError("preview link has no hostname")
    addresses = await public_addresses(hostname)
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = cast(Any, transport)._pool
    pool._network_backend = PinnedNetworkBackend(hostname, sorted(addresses)[0])
    return httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=3.0),
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        headers={
            "Accept": "text/html,application/xhtml+xml,image/*,video/*;q=0.8",
            "User-Agent": "Kaede-Link-Preview/1.0",
        },
    )


async def fetch_bounded(
    url: str, *, max_bytes: int, method: str = "GET"
) -> tuple[httpx.Response, str]:
    current = normalize_preview_url(url)
    for redirect in range(MAX_REDIRECTS + 1):
        async with await pinned_client(current) as client:
            response = await bounded_http_request(
                client,
                method,
                current,
                max_response_bytes=max_bytes,
            )
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("Location")
            if not location or redirect == MAX_REDIRECTS:
                raise FederationNetworkError("preview redirect limit exceeded")
            current = normalize_preview_url(urljoin(current, location))
            continue
        if response.status_code < 200 or response.status_code >= 300:
            raise FederationNetworkError("preview destination returned an error")
        return response, current
    raise FederationNetworkError("preview redirect limit exceeded")


def media_token(settings: Settings, url: str) -> str:
    return hmac.new(
        settings.secret_key.get_secret_value().encode(),
        url.encode(),
        hashlib.sha256,
    ).hexdigest()[:48]


async def register_media(redis: Redis, settings: Settings, url: str | None) -> str | None:
    if url is None:
        return None
    token = media_token(settings, url)
    await redis.set(f"link-preview:media:{token}", url, ex=MEDIA_CAPABILITY_SECONDS)
    return f"/api/v1/link-previews/media/{token}"


@router.post("")
async def create_link_preview(
    payload: PreviewRequest,
    response_status: Response,
    auth: AuthenticatedUser = Depends(require_user),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    await enforce_client_rate_limit(
        redis,
        response_status,
        CLIENT_RATE_LIMITS["link_preview"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    try:
        url = normalize_preview_url(payload.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "LINK_PREVIEW_URL_INVALID"}) from exc
    cache_key = f"link-preview:value:{hashlib.sha256(url.encode()).hexdigest()}"
    cached = await redis.get(cache_key)
    if cached:
        decoded: object = json.loads(cast(str, cached))
        if isinstance(decoded, dict):
            cached_value: dict[str, object] = {str(key): item for key, item in decoded.items()}
            source = cached_value.pop("media_source", None)
            cached_value["media_url"] = await register_media(
                redis, settings, source if isinstance(source, str) else None
            )
            return cached_value
        await redis.delete(cache_key)
    fetched: httpx.Response
    final_url: str
    try:
        head, head_url = await fetch_bounded(url, max_bytes=64 * 1024, method="HEAD")
        head_type = head.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if (
            head_type.startswith("image/") and head_type != "image/svg+xml"
        ) or head_type.startswith("video/"):
            fetched, final_url = head, head_url
        else:
            fetched, final_url = await fetch_bounded(url, max_bytes=MAX_HTML_BYTES)
    except (FederationNetworkError, httpx.HTTPError, ValueError):
        try:
            fetched, final_url = await fetch_bounded(url, max_bytes=MAX_MEDIA_BYTES)
        except (FederationNetworkError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                status_code=422, detail={"code": "LINK_PREVIEW_UNAVAILABLE"}
            ) from exc
    content_type = fetched.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    hostname = urlsplit(final_url).hostname or ""
    if (
        content_type.startswith("image/") and content_type != "image/svg+xml"
    ) or content_type.startswith("video/"):
        value: dict[str, object] = {
            "url": final_url,
            "title": None,
            "description": None,
            "site_name": hostname,
            "media_source": final_url,
            "media_type": "image" if content_type.startswith("image/") else "video",
        }
    elif content_type in {"text/html", "application/xhtml+xml"}:
        if len(fetched.content) > MAX_HTML_BYTES:
            raise HTTPException(status_code=422, detail={"code": "LINK_PREVIEW_UNAVAILABLE"})
        charset = fetched.encoding or "utf-8"
        metadata = preview_metadata(fetched.content.decode(charset, errors="replace"), final_url)
        value = {"url": final_url, **metadata}
        if not any(value.get(key) for key in ("title", "description", "media_source")):
            raise HTTPException(status_code=422, detail={"code": "LINK_PREVIEW_UNAVAILABLE"})
        value["site_name"] = value.get("site_name") or hostname
    else:
        raise HTTPException(status_code=422, detail={"code": "LINK_PREVIEW_UNAVAILABLE"})
    await redis.set(cache_key, json.dumps(value), ex=PREVIEW_CACHE_SECONDS)
    source = value.pop("media_source", None)
    value["media_url"] = await register_media(
        redis, settings, source if isinstance(source, str) else None
    )
    return value


@router.get("/media/{token}")
async def link_preview_media(
    response_status: Response,
    token: str = Path(pattern=MEDIA_PATH_PATTERN),
    auth: AuthenticatedUser = Depends(require_user),
    redis: Redis = Depends(get_redis),
) -> Response:
    await enforce_client_rate_limit(
        redis,
        response_status,
        CLIENT_RATE_LIMITS["link_preview_media"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    source = await redis.get(f"link-preview:media:{token}")
    if not source:
        raise HTTPException(status_code=404, detail={"code": "LINK_PREVIEW_MEDIA_EXPIRED"})
    try:
        fetched, _ = await fetch_bounded(source, max_bytes=MAX_MEDIA_BYTES)
    except (FederationNetworkError, httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail={"code": "LINK_PREVIEW_MEDIA_UNAVAILABLE"}
        ) from exc
    content_type = fetched.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type == "image/svg+xml" or not (
        content_type.startswith("image/") or content_type.startswith("video/")
    ):
        raise HTTPException(status_code=415, detail={"code": "LINK_PREVIEW_MEDIA_UNSUPPORTED"})
    return Response(
        content=fetched.content,
        media_type=content_type,
        headers={
            **dict(response_status.headers),
            "Cache-Control": "private, max-age=900",
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": "sandbox; default-src 'none'",
        },
    )
