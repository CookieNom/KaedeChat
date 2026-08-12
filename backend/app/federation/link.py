from __future__ import annotations

import asyncio
import json
import secrets
import ssl
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from anyio import WouldBlock
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import ClientConnection, connect
from websockets.typing import Subprotocol

from app.core.federation import canonical_request_target
from app.core.json_limits import strict_json_loads
from app.core.settings import Settings
from app.federation.client import OUTBOUND_FEDERATION_LIMITER, federation_signing_headers
from app.federation.network import (
    ensure_peer,
    normalize_domain,
    peer_base_url,
    public_addresses,
)

FEDERATION_LINK_SUBPROTOCOL = "kaede-fed.1"
MAX_LINK_FRAME_BYTES = 1024 * 1024
MAX_LINK_AGE_SECONDS = 55 * 60
MAX_POOLED_LINKS_PER_LOOP = 64


class FederationLinkError(RuntimeError):
    pass


@dataclass(slots=True)
class _PooledLink:
    socket: ClientConnection
    opened_at: float
    last_used_at: float


@dataclass(slots=True)
class _LinkLock:
    lock: asyncio.Lock
    users: int = 0


@dataclass(slots=True)
class _LoopPool:
    admission: asyncio.Lock
    reservations: set[tuple[int, str]]


_links: dict[tuple[int, str], _PooledLink] = {}
_locks: dict[tuple[int, str], _LinkLock] = {}
_active_links: set[tuple[int, str]] = set()
_loop_pools: dict[int, _LoopPool] = {}


def _loop_pool(loop_id: int) -> _LoopPool:
    return _loop_pools.setdefault(loop_id, _LoopPool(asyncio.Lock(), set()))


def _drop_empty_loop_pool(loop_id: int, pool: _LoopPool) -> None:
    if _loop_pools.get(loop_id) is not pool or pool.reservations:
        return
    if any(key[0] == loop_id for key in _links) or any(key[0] == loop_id for key in _locks):
        return
    _loop_pools.pop(loop_id, None)


async def _reserve_link_slot(
    loop_id: int,
    loop_key: tuple[int, str],
    *,
    replace: _PooledLink | None = None,
) -> tuple[_LoopPool, _PooledLink | None]:
    """Atomically reserve one bounded pool slot before opening a socket."""

    pool = _loop_pool(loop_id)
    async with pool.admission:
        if replace is not None:
            if _links.get(loop_key) is not replace:
                raise FederationLinkError("federation hot-link replacement lost its pool entry")
            _links.pop(loop_key)
            pool.reservations.add(loop_key)
            return pool, replace

        if loop_key in pool.reservations or loop_key in _links:
            raise FederationLinkError("federation hot-link already owns a pool slot")

        usage = len(pool.reservations) + sum(key[0] == loop_id for key in _links)
        evicted: _PooledLink | None = None
        if usage >= MAX_POOLED_LINKS_PER_LOOP:
            idle = [
                (key, value)
                for key, value in _links.items()
                if key[0] == loop_id
                and key not in _active_links
                and (_locks.get(key) is None or _locks[key].users == 0)
            ]
            if not idle:
                raise FederationLinkError("federation hot-link pool is saturated")
            evict_key, evicted = min(idle, key=lambda item: item[1].last_used_at)
            _links.pop(evict_key)
            stale_lock = _locks.get(evict_key)
            if stale_lock is None or stale_lock.users == 0:
                _locks.pop(evict_key, None)

        pool.reservations.add(loop_key)
        return pool, evicted


async def _install_reserved_link(
    pool: _LoopPool,
    loop_key: tuple[int, str],
    pooled: _PooledLink,
) -> None:
    async with pool.admission:
        if loop_key not in pool.reservations:
            raise FederationLinkError("federation hot-link reservation was lost")
        pool.reservations.remove(loop_key)
        _links[loop_key] = pooled


async def _release_link_reservation(
    loop_id: int,
    pool: _LoopPool,
    loop_key: tuple[int, str],
) -> None:
    async with pool.admission:
        pool.reservations.discard(loop_key)
    _drop_empty_loop_pool(loop_id, pool)


async def _remove_pooled_link(
    loop_id: int,
    loop_key: tuple[int, str],
    pooled: _PooledLink,
) -> None:
    pool = _loop_pools.get(loop_id)
    if pool is None:
        if _links.get(loop_key) is pooled:
            _links.pop(loop_key)
        return
    async with pool.admission:
        if _links.get(loop_key) is pooled:
            _links.pop(loop_key)


def websocket_url(base: str, path: str) -> str:
    parsed = urlsplit(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


async def _open_link(
    session: AsyncSession,
    settings: Settings,
    destination: str,
) -> _PooledLink:
    await ensure_peer(session, settings, destination)
    target = canonical_request_target("/_kaede/v1/link")
    headers = await federation_signing_headers(
        session,
        settings,
        "GET",
        destination,
        target,
        b"",
    )
    base = await peer_base_url(settings, destination)
    uri = websocket_url(base, "/_kaede/v1/link")
    connection_options: dict[str, Any] = {}
    if destination not in settings.federation_peer_overrides:
        address = sorted(await public_addresses(destination))[0]
        connection_options = {
            "host": address,
            "port": 443,
            "server_hostname": destination,
        }
    elif settings.federation_ca_file is not None and uri.startswith("wss://"):
        connection_options["ssl"] = ssl.create_default_context(cafile=settings.federation_ca_file)
    try:
        socket = await connect(
            uri,
            subprotocols=[Subprotocol(FEDERATION_LINK_SUBPROTOCOL)],
            additional_headers=headers,
            proxy=None,
            compression=None,
            open_timeout=10,
            close_timeout=3,
            ping_interval=20,
            ping_timeout=20,
            max_size=MAX_LINK_FRAME_BYTES,
            max_queue=4,
            **connection_options,
        )
        async with asyncio.timeout(5):
            hello = await socket.recv(decode=True)
        try:
            hello_payload = strict_json_loads(hello) if isinstance(hello, str) else None
        except ValueError:
            hello_payload = None
        if not isinstance(hello_payload, dict) or hello_payload.get("op") != "hello":
            await socket.close(code=1002)
            raise FederationLinkError("peer returned an invalid hot-link greeting")
        if socket.subprotocol != FEDERATION_LINK_SUBPROTOCOL:
            await socket.close(code=1002)
            raise FederationLinkError("peer did not negotiate kaede-fed/1")
        now = time.monotonic()
        return _PooledLink(socket=socket, opened_at=now, last_used_at=now)
    except FederationLinkError:
        raise
    except Exception as exc:
        raise FederationLinkError("federation hot link is unavailable") from exc


async def send_link_batch(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    events: list[dict[str, object]],
) -> dict[str, object]:
    # Share the same small admission pool as signed HTTP. Delivery callers hold
    # SQL rows and transaction locks, so waiting here behind slow peer sockets
    # would otherwise turn the hot-link optimization into DB-pool starvation.
    try:
        OUTBOUND_FEDERATION_LIMITER.acquire_nowait()
    except WouldBlock as exc:
        raise FederationLinkError("outbound federation is busy; retry shortly") from exc
    try:
        return await _send_link_batch_admitted(session, settings, destination, events)
    finally:
        OUTBOUND_FEDERATION_LIMITER.release()


async def _send_link_batch_admitted(
    session: AsyncSession,
    settings: Settings,
    destination: str,
    events: list[dict[str, object]],
) -> dict[str, object]:
    destination = normalize_domain(destination)
    if not 1 <= len(events) <= 100:
        raise FederationLinkError("hot-link event batch is outside protocol bounds")
    loop_id = id(asyncio.get_running_loop())
    loop_key = (loop_id, destination)
    lock_entry = _locks.setdefault(loop_key, _LinkLock(asyncio.Lock()))
    lock_entry.users += 1
    pooled: _PooledLink | None = None
    try:
        async with lock_entry.lock:
            _active_links.add(loop_key)
            pooled = _links.get(loop_key)
            expired = (
                pooled is not None and time.monotonic() - pooled.opened_at >= MAX_LINK_AGE_SECONDS
            )
            if pooled is None or expired:
                pool, evicted = await _reserve_link_slot(
                    loop_id,
                    loop_key,
                    replace=pooled if expired else None,
                )
                pooled = None
                opened: _PooledLink | None = None
                try:
                    if evicted is not None:
                        with suppress(Exception):
                            await evicted.socket.close(code=1000)
                    opened = await _open_link(session, settings, destination)
                    await _install_reserved_link(pool, loop_key, opened)
                except BaseException:
                    await asyncio.shield(_release_link_reservation(loop_id, pool, loop_key))
                    if evicted is not None:
                        with suppress(Exception):
                            await asyncio.shield(evicted.socket.close(code=1011))
                    if opened is not None:
                        with suppress(Exception):
                            await asyncio.shield(opened.socket.close(code=1011))
                    raise
                pooled = opened
            request_id = secrets.token_urlsafe(18)
            await pooled.socket.send(
                json.dumps(
                    {"op": "events", "id": request_id, "events": events},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            async with asyncio.timeout(15):
                raw = await pooled.socket.recv(decode=True)
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_LINK_FRAME_BYTES:
                raise FederationLinkError("peer returned an invalid hot-link frame")
            payload = strict_json_loads(raw)
            if (
                not isinstance(payload, dict)
                or payload.get("op") != "results"
                or payload.get("id") != request_id
                or not isinstance(payload.get("results"), list)
            ):
                raise FederationLinkError("peer returned mismatched hot-link results")
            pooled.last_used_at = time.monotonic()
            return payload
    except FederationLinkError:
        if pooled is not None:
            await _remove_pooled_link(loop_id, loop_key, pooled)
            with suppress(Exception):
                await pooled.socket.close(code=1011)
        raise
    except asyncio.CancelledError:
        if pooled is not None:
            await _remove_pooled_link(loop_id, loop_key, pooled)
            with suppress(Exception):
                await pooled.socket.close(code=1011)
        raise
    except Exception as exc:
        if pooled is not None:
            await _remove_pooled_link(loop_id, loop_key, pooled)
            with suppress(Exception):
                await pooled.socket.close(code=1011)
        raise FederationLinkError("federation hot-link exchange failed") from exc
    finally:
        _active_links.discard(loop_key)
        lock_entry.users -= 1
        if lock_entry.users == 0 and loop_key not in _links:
            _locks.pop(loop_key, None)
        loop_pool = _loop_pools.get(loop_id)
        if loop_pool is not None:
            _drop_empty_loop_pool(loop_id, loop_pool)


async def close_federation_links() -> None:
    links = list(_links.values())
    pools = list(_loop_pools.values())
    for pool in pools:
        async with pool.admission:
            pool.reservations.clear()
    _links.clear()
    _locks.clear()
    _active_links.clear()
    _loop_pools.clear()
    for pooled in links:
        with suppress(Exception):
            await pooled.socket.close(code=1001)
