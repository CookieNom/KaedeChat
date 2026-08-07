from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from redis.asyncio import Redis

from app.api.dependencies import AuthenticatedUser, get_redis, require_user
from app.core.rate_limits import CLIENT_RATE_LIMITS, enforce_client_rate_limit
from app.core.settings import Settings, get_settings

router = APIRouter(prefix="/api/v1/gifs", tags=["gifs"])
KLIPY_API_ROOT = "https://api.klipy.com/api/v1"
KLIPY_MEDIA_HOST = "media.klipy.com"
MAX_UPSTREAM_BYTES = 2 * 1024 * 1024


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _media_variant(item: dict[str, Any], size: str) -> dict[str, Any] | None:
    containers = [item.get("file"), item.get("files")]
    for container in containers:
        if not isinstance(container, dict):
            continue
        candidate = container.get(size)
        if isinstance(candidate, dict):
            for format_name in ("webp", "gif"):
                value = candidate.get(format_name)
                if isinstance(value, dict):
                    return value
    return None


def _safe_media_url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return None
    if (
        url.scheme != "https"
        or url.host != KLIPY_MEDIA_HOST
        or url.userinfo
        or url.port not in {None, 443}
    ):
        return None
    return str(url)


def parse_klipy_items(payload: object) -> tuple[list[dict[str, object]], bool]:
    if not isinstance(payload, dict) or payload.get("result") is not True:
        raise ValueError("KLIPY returned an invalid response")
    envelope = payload.get("data")
    if not isinstance(envelope, dict):
        raise ValueError("KLIPY returned an invalid response")
    raw_items = envelope.get("data")
    if not isinstance(raw_items, list):
        raise ValueError("KLIPY returned an invalid response")
    parsed: list[dict[str, object]] = []
    for raw in raw_items[:50]:
        if not isinstance(raw, dict):
            continue
        full = next(
            (
                variant
                for size in ("md", "hd", "sm", "xs")
                if (variant := _media_variant(raw, size)) is not None
            ),
            None,
        )
        preview = next(
            (
                variant
                for size in ("sm", "xs", "md", "hd")
                if (variant := _media_variant(raw, size)) is not None
            ),
            None,
        )
        if full is None or preview is None:
            continue
        url = _safe_media_url(full.get("url"))
        preview_url = _safe_media_url(preview.get("url"))
        if url is None or preview_url is None:
            continue
        identifier = raw.get("id")
        if not isinstance(identifier, (str, int)):
            continue
        title = raw.get("title")
        parsed.append(
            {
                "id": str(identifier)[:256],
                "title": title[:200] if isinstance(title, str) else "GIF",
                "url": url,
                "preview_url": preview_url,
                "width": _positive_int(full.get("width")),
                "height": _positive_int(full.get("height")),
            }
        )
    return parsed, envelope.get("has_next") is True


def _customer_id(settings: Settings, auth: AuthenticatedUser) -> str:
    identity = f"{auth.user.origin_domain}:{auth.user.id}".encode()
    key = settings.secret_key.get_secret_value().encode()
    return hmac.new(key, identity, hashlib.sha256).hexdigest()[:32]


async def _fetch_klipy(
    settings: Settings,
    auth: AuthenticatedUser,
    *,
    query: str | None,
    page: int,
    limit: int,
) -> tuple[list[dict[str, object]], bool]:
    api_key = settings.klipy_api_key
    if api_key is None:
        raise HTTPException(status_code=404, detail={"code": "GIF_PICKER_DISABLED"})
    endpoint = "search" if query else "trending"
    params: dict[str, str | int] = {
        "page": page,
        "per_page": limit,
        "customer_id": _customer_id(settings, auth),
        "locale": "en_US",
    }
    if query:
        params["q"] = query
    url = f"{KLIPY_API_ROOT}/{api_key.get_secret_value()}/gifs/{endpoint}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(url, params=params)
        if response.status_code != 200:
            raise HTTPException(status_code=503, detail={"code": "GIF_PROVIDER_UNAVAILABLE"})
        if len(response.content) > MAX_UPSTREAM_BYTES:
            raise HTTPException(status_code=502, detail={"code": "GIF_PROVIDER_INVALID"})
        return parse_klipy_items(response.json())
    except HTTPException:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=503, detail={"code": "GIF_PROVIDER_UNAVAILABLE"}) from exc


@router.get("")
async def list_gifs(
    response: Response,
    query: str | None = Query(default=None, min_length=1, max_length=100),
    page: int = Query(default=1, ge=1, le=100),
    limit: int = Query(default=24, ge=1, le=50),
    auth: AuthenticatedUser = Depends(require_user),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.klipy_enabled:
        raise HTTPException(status_code=404, detail={"code": "GIF_PICKER_DISABLED"})
    await enforce_client_rate_limit(
        redis,
        response,
        CLIENT_RATE_LIMITS["gif_search"],
        user_id=auth.user.id,
        user_domain=auth.user.origin_domain,
    )
    items, has_next = await _fetch_klipy(
        settings, auth, query=query.strip() if query else None, page=page, limit=limit
    )
    return {"items": items, "page": page, "next_page": page + 1 if has_next else None}
