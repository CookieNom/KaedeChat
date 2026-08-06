from __future__ import annotations

import asyncio
import json
import time
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.federation.link as link
from app.core.settings import Settings


class FakeSocket:
    def __init__(self) -> None:
        self.closed = False
        self.last_request_id: str | None = None
        self.block_next_receive = False
        self.receive_started = asyncio.Event()
        self.release_receive = asyncio.Event()
        self.block_next_close = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.last_request_id = str(payload["id"])

    async def recv(self, *, decode: bool | None = None) -> str:
        del decode
        if self.block_next_receive:
            self.receive_started.set()
            await self.release_receive.wait()
            self.block_next_receive = False
        if self.last_request_id is None:
            raise AssertionError("a result was requested before a batch was sent")
        return json.dumps(
            {"op": "results", "id": self.last_request_id, "results": []},
            separators=(",", ":"),
        )

    async def close(self, *, code: int = 1000) -> None:
        del code
        if self.block_next_close:
            self.block_next_close = False
            self.close_started.set()
            await self.release_close.wait()
        self.closed = True
        self.release_receive.set()


def pooled(socket: FakeSocket) -> link._PooledLink:
    now = time.monotonic()
    return link._PooledLink(socket=cast(Any, socket), opened_at=now, last_used_at=now)


def batch(number: int) -> list[dict[str, object]]:
    return [{"event_id": f"event-{number}"}]


async def send(destination: str, number: int) -> dict[str, object]:
    return await link.send_link_batch(
        cast(AsyncSession, object()),
        cast(Settings, object()),
        destination,
        batch(number),
    )


@pytest.mark.asyncio
async def test_concurrent_opens_reserve_capacity_before_network_await(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link.close_federation_links()
    monkeypatch.setattr(link, "MAX_POOLED_LINKS_PER_LOOP", 2)
    open_started = asyncio.Event()
    release_open = asyncio.Event()
    destinations: list[str] = []

    async def fake_open(
        _session: AsyncSession,
        _settings: Settings,
        destination: str,
    ) -> link._PooledLink:
        destinations.append(destination)
        if len(destinations) == 2:
            open_started.set()
        await release_open.wait()
        return pooled(FakeSocket())

    monkeypatch.setattr(link, "_open_link", fake_open)
    tasks = [
        asyncio.create_task(send("one.example", 1)),
        asyncio.create_task(send("two.example", 2)),
    ]
    try:
        await asyncio.wait_for(open_started.wait(), timeout=1)
        with pytest.raises(link.FederationLinkError, match="pool is saturated"):
            await asyncio.wait_for(send("three.example", 3), timeout=0.2)

        loop_id = id(asyncio.get_running_loop())
        pool = link._loop_pools[loop_id]
        assert len(pool.reservations) == 2
        assert not [key for key in link._links if key[0] == loop_id]

        release_open.set()
        await asyncio.gather(*tasks)
        assert len([key for key in link._links if key[0] == loop_id]) == 2
    finally:
        release_open.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await link.close_federation_links()


@pytest.mark.asyncio
async def test_pool_evicts_an_idle_link_but_never_an_active_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link.close_federation_links()
    monkeypatch.setattr(link, "MAX_POOLED_LINKS_PER_LOOP", 2)
    sockets: dict[str, FakeSocket] = {}

    async def fake_open(
        _session: AsyncSession,
        _settings: Settings,
        destination: str,
    ) -> link._PooledLink:
        socket = FakeSocket()
        sockets[destination] = socket
        return pooled(socket)

    monkeypatch.setattr(link, "_open_link", fake_open)
    active_task: asyncio.Task[dict[str, object]] | None = None
    try:
        await send("active.example", 1)
        await send("idle.example", 2)
        active_socket = sockets["active.example"]
        idle_socket = sockets["idle.example"]
        active_socket.block_next_receive = True
        active_task = asyncio.create_task(send("active.example", 3))
        await asyncio.wait_for(active_socket.receive_started.wait(), timeout=1)

        await send("replacement.example", 4)

        loop_id = id(asyncio.get_running_loop())
        destinations = {key[1] for key in link._links if key[0] == loop_id}
        assert destinations == {"active.example", "replacement.example"}
        assert not active_socket.closed
        assert idle_socket.closed

        active_socket.release_receive.set()
        await active_task
    finally:
        if active_task is not None and not active_task.done():
            active_task.cancel()
            await asyncio.gather(active_task, return_exceptions=True)
        await link.close_federation_links()


@pytest.mark.asyncio
async def test_failed_open_releases_reservation_and_loop_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link.close_federation_links()

    async def fail_open(
        _session: AsyncSession,
        _settings: Settings,
        _destination: str,
    ) -> link._PooledLink:
        raise link.FederationLinkError("expected open failure")

    monkeypatch.setattr(link, "_open_link", fail_open)
    with pytest.raises(link.FederationLinkError, match="expected open failure"):
        await send("failure.example", 1)

    loop_id = id(asyncio.get_running_loop())
    assert not [key for key in link._links if key[0] == loop_id]
    assert not [key for key in link._locks if key[0] == loop_id]
    assert loop_id not in link._loop_pools


@pytest.mark.asyncio
async def test_cancelled_eviction_closes_socket_and_releases_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await link.close_federation_links()
    monkeypatch.setattr(link, "MAX_POOLED_LINKS_PER_LOOP", 1)
    sockets: dict[str, FakeSocket] = {}

    async def fake_open(
        _session: AsyncSession,
        _settings: Settings,
        destination: str,
    ) -> link._PooledLink:
        socket = FakeSocket()
        sockets[destination] = socket
        return pooled(socket)

    monkeypatch.setattr(link, "_open_link", fake_open)
    replacement: asyncio.Task[dict[str, object]] | None = None
    try:
        await send("idle.example", 1)
        idle_socket = sockets["idle.example"]
        idle_socket.block_next_close = True
        replacement = asyncio.create_task(send("replacement.example", 2))
        await asyncio.wait_for(idle_socket.close_started.wait(), timeout=1)
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement

        loop_id = id(asyncio.get_running_loop())
        assert idle_socket.closed
        assert not [key for key in link._links if key[0] == loop_id]
        assert not [key for key in link._locks if key[0] == loop_id]
        assert loop_id not in link._loop_pools
    finally:
        idle = sockets.get("idle.example")
        if idle is not None:
            idle.release_close.set()
        if replacement is not None and not replacement.done():
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
        await link.close_federation_links()
