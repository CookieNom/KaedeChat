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

import anyio
import httpcore
import httpx
from anyio import WouldBlock
from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.json_limits import strict_json_loads
from app.core.settings import DOMAIN_RE, Settings
from app.db.models import Instance, PeerKey
from app.federation.schemas import KEY_ID_RE


class FederationNetworkError(RuntimeError):
    pass


class FederationInstanceQuotaExceeded(FederationNetworkError):
    """The bounded remote-instance metadata cache cannot admit another peer."""

    code = "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED"
    federation_code = "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED"

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            f"remote instance cache has reached its configured limit ({used} >= {limit})"
        )

    def detail(self, *, federation: bool = False) -> dict[str, object]:
        # The exact population is operator data, not federation protocol data.
        return {"code": self.federation_code if federation else self.code}


PEER_KEY_REFRESH_INTERVAL = timedelta(hours=1)
# A forged unknown-key flood must not turn remote discovery into an unbounded
# queue of database transactions. Fresh cached reads bypass this limiter; only
# requests that genuinely need network discovery consume a slot.
PEER_DISCOVERY_LIMITER = anyio.CapacityLimiter(4)
# Snapshot synchronization can explicitly authorize an aggregate page budget
# up to 64 MiB. Ordinary signed requests remain transport-capped at 4 MiB (or
# a smaller route-specific limit) before reaching this decoder.
MAX_FEDERATION_JSON_RESPONSE_BYTES = 64 * 1024 * 1024


def decode_federation_response_json(
    response: httpx.Response,
    *,
    max_response_bytes: int = MAX_FEDERATION_JSON_RESPONSE_BYTES,
) -> object:
    """Strictly decode one bounded JSON document returned by a federation peer.

    Federation responses are untrusted protocol input.  ``httpx.Response.json``
    accepts duplicate object keys, non-finite numbers, floating-point values,
    and integers that cannot be represented consistently by all supported
    clients.  Decode through the same strict tree validator used for signed
    federation requests so response processing is deterministic as well.
    """

    content = response.content
    if len(content) > max_response_bytes:
        raise FederationNetworkError("peer JSON response exceeded the size limit")
    try:
        return strict_json_loads(content, label="federation peer response")
    except ValueError as exc:
        raise FederationNetworkError("peer returned invalid federation JSON") from exc


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


def peer_key_history_exceeds_limit(
    existing_count: int,
    cached_key_ids: set[str],
    advertised_key_ids: set[str],
    limit: int,
) -> bool:
    """Return whether accepting newly advertised immutable key IDs exceeds a cap."""

    return existing_count + len(advertised_key_ids - cached_key_ids) > limit


async def ensure_remote_instance_record(
    session: AsyncSession,
    settings: Settings,
    domain: str,
    *,
    display_name: str | None = None,
    software_version: str | None = None,
) -> Instance:
    """Create one bounded, non-authoritative peer metadata row if needed.

    A row created here is not a trust decision: authenticated traffic still
    requires a verified discovery key. It exists so opaque composite user
    references can be retained without allowing one guild authority to assert
    another user's mutable profile.
    """

    domain = normalize_domain(domain)
    if domain == settings.domain:
        raise FederationNetworkError("the local instance cannot be cached as a remote peer")
    instance = await session.get(Instance, domain)
    if instance is not None:
        if instance.is_self:
            raise FederationNetworkError("remote instance identity conflicts with this server")
        return instance
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(func.hashtextextended("kaede-peer-instance-admission", 0))
        )
    )
    instance = await session.get(Instance, domain, populate_existing=True)
    if instance is not None:
        if instance.is_self:
            raise FederationNetworkError("remote instance identity conflicts with this server")
        return instance
    remote_instances = int(
        await session.scalar(
            select(func.count()).select_from(Instance).where(Instance.is_self.is_(False))
        )
        or 0
    )
    if remote_instances >= settings.federation_max_remote_instances:
        raise FederationInstanceQuotaExceeded(
            remote_instances,
            settings.federation_max_remote_instances,
        )
    instance = Instance(
        domain=domain,
        is_self=False,
        display_name=(display_name or domain)[:100],
        software_version=(software_version or "unknown")[:40],
    )
    session.add(instance)
    await session.flush()
    return instance


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

    request_headers = dict(kwargs.pop("headers", {}) or {})
    request_headers.setdefault("Accept-Encoding", "identity")
    configured_timeout = client.timeout.read
    wall_timeout = (
        min(float(configured_timeout), 30.0)
        if isinstance(configured_timeout, (int, float)) and configured_timeout > 0
        else 30.0
    )
    try:
        with anyio.fail_after(wall_timeout):
            async with client.stream(method, url, headers=request_headers, **kwargs) as response:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    # Reject before iterating: httpx transparently decompresses
                    # while streaming, and one compressed chunk can otherwise
                    # expand before the decoded-byte counter gets to reject it.
                    raise FederationNetworkError("peer returned an encoded response")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if not content_length.isascii() or not content_length.isdecimal():
                            raise ValueError
                        declared_length = int(content_length)
                    except ValueError:
                        raise FederationNetworkError(
                            "peer sent an invalid content length"
                        ) from None
                    if not 0 <= declared_length <= max_response_bytes:
                        raise FederationNetworkError("peer response exceeded the size limit")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > max_response_bytes:
                        raise FederationNetworkError("peer response exceeded the size limit")
                    chunks.append(chunk)
                # Do not retain transport framing on the detached response.
                detached_headers = [
                    (name, value)
                    for name, value in response.headers.multi_items()
                    if name.lower() not in {"content-length", "transfer-encoding"}
                ]
                return httpx.Response(
                    response.status_code,
                    headers=detached_headers,
                    content=b"".join(chunks),
                    request=response.request,
                    extensions=response.extensions,
                )
    except TimeoutError as exc:
        raise FederationNetworkError("peer response exceeded its deadline") from exc


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

    try:
        PEER_DISCOVERY_LIMITER.acquire_nowait()
    except WouldBlock as exc:
        raise FederationNetworkError("peer discovery is busy; retry shortly") from exc
    try:
        return await _refresh_peer(session, settings, domain, force=force)
    finally:
        PEER_DISCOVERY_LIMITER.release()


async def _refresh_peer(
    session: AsyncSession,
    settings: Settings,
    domain: str,
    *,
    force: bool,
) -> Instance:

    # Fetch and apply a stale peer trust document in one serialized critical
    # section. Fresh reads stay lock-free, while stale concurrent refreshes
    # recheck after the winner commits so an older network response cannot
    # overwrite a newer one.
    refresh_lock = await session.scalar(
        select(func.pg_try_advisory_xact_lock(func.hashtextextended(f"kaede-peer:{domain}", 0)))
    )
    if refresh_lock is not True:
        raise FederationNetworkError("another discovery refresh for this peer is in progress")
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
        with anyio.fail_after(12):
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
                try:
                    discovery_payload = decode_federation_response_json(discovery)
                except FederationNetworkError as exc:
                    raise FederationNetworkError("peer discovery failed") from exc
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
                try:
                    keys_payload = decode_federation_response_json(keys_response)
                except FederationNetworkError as exc:
                    raise FederationNetworkError("peer discovery failed") from exc
    except FederationNetworkError:
        raise
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise FederationNetworkError("peer discovery failed") from exc
    except TimeoutError as exc:
        raise FederationNetworkError("peer discovery exceeded its deadline") from exc
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
    now = datetime.now(UTC)
    existing_keys = list(
        await session.scalars(
            select(PeerKey)
            .where(
                PeerKey.domain == domain,
                or_(
                    PeerKey.expired_at.is_(None),
                    PeerKey.key_id.in_(tuple(verify_keys)),
                ),
            )
            .with_for_update()
        )
    )
    existing_by_id = {peer_key.key_id: peer_key for peer_key in existing_keys}
    existing_key_count = int(
        await session.scalar(
            select(func.count()).select_from(PeerKey).where(PeerKey.domain == domain)
        )
        or 0
    )
    if peer_key_history_exceeds_limit(
        existing_key_count,
        set(existing_by_id),
        set(verify_keys),
        settings.federation_peer_key_history_limit,
    ):
        raise FederationNetworkError("peer key history has reached its configured limit")
    for key_id, public_key in verify_keys.items():
        existing = existing_by_id.get(key_id)
        if existing is not None and not secrets.compare_digest(existing.public_key, public_key):
            # Key IDs are immutable trust anchors. Rotation must allocate a new ID.
            raise FederationNetworkError("peer reused a key ID with different material")
    if (
        instance is not None
        and "request-nonce/1" in instance.capabilities
        and "request-nonce/1" not in capabilities
    ):
        raise FederationNetworkError("peer attempted to remove a pinned security capability")

    # Every rejection check above is side-effect free. Keep the remaining trust
    # update in a savepoint as defense in depth: callers may deliberately catch
    # a discovery failure and commit unrelated delivery state, which must never
    # persist a partially applied key rotation or capability downgrade.
    async with session.begin_nested():
        if instance is None:
            instance = await ensure_remote_instance_record(
                session,
                settings,
                domain,
                display_name=str(keys_payload.get("display_name") or domain),
                software_version=str(keys_payload.get("software_version") or "unknown"),
            )
        else:
            instance.display_name = str(keys_payload.get("display_name") or domain)[:100]
            instance.software_version = str(keys_payload.get("software_version") or "unknown")[:40]
        retire_omitted_peer_keys(existing_keys, set(verify_keys), now)
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
