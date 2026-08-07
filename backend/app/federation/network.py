from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import secrets
import socket
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

import httpcore
import httpx
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import DOMAIN_RE, Settings
from app.db.models import Instance, PeerKey
from app.federation.schemas import KEY_ID_RE


class FederationNetworkError(RuntimeError):
    pass


PEER_KEY_REFRESH_INTERVAL = timedelta(hours=1)


def peer_key_needs_refresh(peer_key: PeerKey, now: datetime) -> bool:
    """Return whether a cached peer key is too old to remain trusted as-is."""

    return peer_key.expired_at is not None or peer_key.fetched_at < (
        now - PEER_KEY_REFRESH_INTERVAL
    )


def retire_omitted_peer_keys(
    peer_keys: list[PeerKey], advertised_key_ids: set[str], now: datetime
) -> None:
    """Expire keys a peer has deliberately removed from its signed key set."""

    for peer_key in peer_keys:
        if peer_key.expired_at is None and peer_key.key_id not in advertised_key_ids:
            peer_key.expired_at = now


async def bounded_http_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_response_bytes: int,
    **kwargs: Any,
) -> httpx.Response:
    """Issue an HTTP request without allowing an unbounded buffered response.

    Federation peers are untrusted.  ``httpx.AsyncClient.request`` buffers the
    complete body before a caller can inspect its size, so enforce the limit
    while streaming and return a detached in-memory response for existing JSON
    callers.
    """

    async with client.stream(method, url, **kwargs) as response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if not content_length.isascii() or not content_length.isdecimal():
                    raise ValueError
                declared_length = int(content_length)
            except ValueError:
                raise FederationNetworkError("peer sent an invalid content length") from None
            if not 0 <= declared_length <= max_response_bytes:
                raise FederationNetworkError("peer response exceeded the size limit")
        chunks: list[bytes] = []
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > max_response_bytes:
                raise FederationNetworkError("peer response exceeded the size limit")
            chunks.append(chunk)
        # ``aiter_bytes`` returns decoded representation bytes. Do not retain
        # framing or content-coding headers on the detached response or httpx
        # will try to decode the in-memory body a second time.
        detached_headers = [
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
        ]
        return httpx.Response(
            response.status_code,
            headers=detached_headers,
            content=b"".join(chunks),
            request=response.request,
            extensions=response.extensions,
        )


def normalize_domain(value: str) -> str:
    domain = value.rstrip(".").lower()
    if not DOMAIN_RE.fullmatch(domain):
        raise FederationNetworkError("invalid federation domain")
    return domain


def public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    # ``IPv6Address.is_global`` has varied across Python releases for mapped
    # addresses, and the IPv6 wrapper is itself classified as reserved even
    # when the embedded IPv4 address is public.  Apply the policy to the
    # effective destination so mapped loopback, RFC1918, link-local, and shared
    # (CGNAT) addresses cannot bypass the SSRF guard while mapped public
    # addresses remain usable on dual-stack hosts.
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id is not None:
            return False
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped

    return bool(
        ip.is_global
        and not ip.is_private
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and not getattr(ip, "is_site_local", False)
    )


class PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, hostname: str, address: str) -> None:
        self.hostname = hostname
        self.address = address
        self.backend = httpcore.AnyIOBackend()

    async def connect_tcp(  # noqa: ASYNC109 -- httpcore interface name
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        target = self.address if host == self.hostname else host
        return await self.backend.connect_tcp(target, port, timeout, local_address, socket_options)

    async def connect_unix_socket(  # noqa: ASYNC109 -- httpcore interface name
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        return await self.backend.connect_unix_socket(path, timeout, socket_options)

    async def sleep(self, seconds: float) -> None:
        await self.backend.sleep(seconds)


async def public_addresses(domain: str) -> set[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await loop.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise FederationNetworkError("peer DNS resolution failed") from exc
    addresses = {record[4][0] for record in records}
    if not addresses or any(not public_address(address) for address in addresses):
        raise FederationNetworkError("peer resolved to a prohibited address")
    return addresses


async def peer_base_url(settings: Settings, domain: str) -> str:
    domain = normalize_domain(domain)
    override = settings.federation_peer_overrides.get(domain)
    if override is not None:
        if settings.environment == "production":
            raise FederationNetworkError("peer overrides are forbidden in production")
        parsed = urlsplit(override)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise FederationNetworkError("invalid development peer override")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FederationNetworkError("unsafe development peer override")
        return override.rstrip("/")
    if domain.endswith(".localhost"):
        raise FederationNetworkError("localhost federation requires an explicit peer override")
    await public_addresses(domain)
    return f"https://{domain}"


async def peer_http_client(
    settings: Settings, domain: str, *, request_timeout: float
) -> tuple[str, httpx.AsyncClient]:
    normalized = normalize_domain(domain)
    base = await peer_base_url(settings, normalized)
    if normalized in settings.federation_peer_overrides:
        return base, httpx.AsyncClient(
            timeout=request_timeout,
            follow_redirects=False,
            trust_env=False,
            verify=settings.federation_ca_file or True,
        )
    addresses = await public_addresses(normalized)
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = cast(Any, transport)._pool
    pool._network_backend = PinnedNetworkBackend(normalized, sorted(addresses)[0])
    return base, httpx.AsyncClient(
        timeout=request_timeout,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


async def ensure_peer(
    session: AsyncSession, settings: Settings, domain: str, *, force: bool = False
) -> Instance:
    domain = normalize_domain(domain)
    instance = await session.get(Instance, domain)
    now = datetime.now(UTC)
    known_key = await session.scalar(
        select(PeerKey)
        .where(PeerKey.domain == domain, PeerKey.expired_at.is_(None))
        .order_by(PeerKey.fetched_at.desc())
        .limit(1)
    )
    if (
        instance is not None
        and known_key is not None
        and not peer_key_needs_refresh(known_key, now)
        and not force
    ):
        return instance

    # Fetch and apply a stale peer trust document in one serialized critical
    # section. Fresh reads stay lock-free, while stale concurrent refreshes
    # recheck after the winner commits so an older network response cannot
    # overwrite a newer one.
    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"kaede-peer:{domain}", 0)))
    )
    instance = await session.get(Instance, domain, populate_existing=True)
    known_key = await session.scalar(
        select(PeerKey)
        .where(PeerKey.domain == domain, PeerKey.expired_at.is_(None))
        .order_by(PeerKey.fetched_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )
    now = datetime.now(UTC)
    if (
        instance is not None
        and known_key is not None
        and not peer_key_needs_refresh(known_key, now)
        and not force
    ):
        return instance
    try:
        base, client = await peer_http_client(settings, domain, request_timeout=5)
        async with client:
            discovery = await bounded_http_request(
                client,
                "GET",
                f"{base}/.well-known/kaede/server",
                max_response_bytes=64 * 1024,
            )
            if discovery.status_code != 200:
                raise FederationNetworkError("peer discovery failed")
            discovery_payload = discovery.json()
            if not isinstance(discovery_payload, dict):
                raise FederationNetworkError("peer discovery payload is invalid")
            versions = discovery_payload.get("versions")
            if not isinstance(versions, list) or "1" not in versions:
                raise FederationNetworkError("peer does not support kaede-fed/1")
            raw_capabilities = discovery_payload.get("capabilities", [])
            if (
                not isinstance(raw_capabilities, list)
                or len(raw_capabilities) > 64
                or any(
                    not isinstance(item, str) or not 1 <= len(item) <= 64
                    for item in raw_capabilities
                )
            ):
                raise FederationNetworkError("peer capability list is invalid")
            capabilities = sorted(set(raw_capabilities))
            server = normalize_domain(str(discovery_payload.get("server", "")))
            if server != domain:
                raise FederationNetworkError("delegated federation hosts are not enabled in M3")
            keys_response = await bounded_http_request(
                client,
                "GET",
                f"{base}/_kaede/v1/keys",
                max_response_bytes=256 * 1024,
            )
            if keys_response.status_code != 200:
                raise FederationNetworkError("peer key fetch failed")
            keys_payload = keys_response.json()
    except FederationNetworkError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise FederationNetworkError("peer discovery failed") from exc
    if not isinstance(keys_payload, dict):
        raise FederationNetworkError("peer key payload is invalid")
    advertised_server = keys_payload.get("server_name")
    if advertised_server is not None and normalize_domain(str(advertised_server)) != domain:
        raise FederationNetworkError("peer key response has the wrong server name")

    decoded_maps: list[dict[str, bytes]] = []
    for map_name in ("old_verify_keys", "verify_keys"):
        raw_keys = keys_payload.get(map_name, {})
        if not isinstance(raw_keys, dict) or len(raw_keys) > 64:
            raise FederationNetworkError("peer published an invalid key map")
        decoded: dict[str, bytes] = {}
        for raw_key_id, encoded in raw_keys.items():
            key_id = str(raw_key_id)
            if not KEY_ID_RE.fullmatch(key_id) or not isinstance(encoded, str):
                raise FederationNetworkError("peer published an invalid key")
            try:
                public_key = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise FederationNetworkError("peer published an invalid key") from exc
            if len(public_key) != 32:
                raise FederationNetworkError("peer published an invalid key")
            decoded[key_id] = public_key
        decoded_maps.append(decoded)
    old_keys, current_keys = decoded_maps
    for key_id in old_keys.keys() & current_keys.keys():
        if not secrets.compare_digest(old_keys[key_id], current_keys[key_id]):
            raise FederationNetworkError("peer reused a key ID with different material")
    verify_keys = {**old_keys, **current_keys}
    current_key_id = str(keys_payload.get("current_key_id") or "")
    if not verify_keys or not KEY_ID_RE.fullmatch(current_key_id):
        raise FederationNetworkError("peer published no current verification key")
    if current_key_id not in current_keys:
        raise FederationNetworkError("peer current key is not published as current")

    instance = await session.get(Instance, domain, populate_existing=True)
    if instance is None:
        instance = Instance(
            domain=domain,
            is_self=False,
            display_name=str(keys_payload.get("display_name") or domain)[:100],
            software_version=str(keys_payload.get("software_version") or "unknown")[:40],
        )
        session.add(instance)
        await session.flush()
    now = datetime.now(UTC)
    existing_keys = list(
        await session.scalars(select(PeerKey).where(PeerKey.domain == domain).with_for_update())
    )
    retire_omitted_peer_keys(existing_keys, set(verify_keys), now)
    existing_by_id = {peer_key.key_id: peer_key for peer_key in existing_keys}
    for key_id, public_key in verify_keys.items():
        existing = existing_by_id.get(key_id)
        if existing is None:
            session.add(
                PeerKey(
                    domain=domain,
                    key_id=key_id,
                    public_key=public_key,
                    fetched_at=now,
                )
            )
        elif not secrets.compare_digest(existing.public_key, public_key):
            # Key IDs are immutable trust anchors. Rotation must allocate a new ID.
            raise FederationNetworkError("peer reused a key ID with different material")
        else:
            existing.fetched_at = now
            existing.expired_at = None
    instance.current_key_id = current_key_id
    instance.capabilities = capabilities
    instance.last_seen_at = now
    await session.flush()
    return instance


def federation_http_error(code: str, status_code: int = 400) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": code})
