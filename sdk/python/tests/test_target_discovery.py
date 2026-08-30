from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import kaede_bot.client as client_module
from kaede_bot.client import Client
from kaede_bot.errors import ApiError
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def target_client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(20, "apps.example"),
            40,
            Ed25519PrivateKey.generate(),
            "production",
        )
    )


@pytest.mark.asyncio
async def test_start_failure_closes_partial_runtime_state() -> None:
    bot = target_client()

    class RuntimeClient:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    runtime_client = RuntimeClient()
    gateway_task: asyncio.Task[None] | None = None

    async def add_target(target: str) -> str:
        bot._targets[target] = runtime_client  # type: ignore[assignment]  # noqa: SLF001
        return target

    async def idle_gateway() -> None:
        await asyncio.Event().wait()

    def ensure_gateway(target: str) -> asyncio.Task[None]:
        nonlocal gateway_task
        gateway_task = asyncio.create_task(idle_gateway())
        bot._gateway_tasks[target] = gateway_task  # noqa: SLF001
        return gateway_task

    bot.fetch_bot_identity = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(scopes=frozenset())
    )
    bot._clear_dm_capability_state = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    bot.add_target = add_target  # type: ignore[assignment]
    bot._ensure_gateway_task = ensure_gateway  # type: ignore[method-assign]  # noqa: SLF001
    bot.discover_targets = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("discovery failed")
    )

    with pytest.raises(RuntimeError, match="discovery failed"):
        await bot.start("https://guilds.example")

    assert runtime_client.closed
    assert gateway_task is not None
    assert gateway_task.cancelled()
    assert not bot._started  # noqa: SLF001
    assert not bot._starting  # noqa: SLF001
    assert bot._targets == {}  # noqa: SLF001
    assert bot._gateway_tasks == {}  # noqa: SLF001


@pytest.mark.asyncio
async def test_start_rejects_a_concurrent_start_before_network_io() -> None:
    bot = target_client()
    bot._starting = True  # noqa: SLF001
    bot.fetch_bot_identity = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="already starting or running"):
        await bot.start()

    bot.fetch_bot_identity.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_cancellation_closes_started_gateway_tasks() -> None:
    bot = target_client()
    discovery_started = asyncio.Event()
    gateway_task: asyncio.Task[None] | None = None

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def discover_targets(**_: object) -> tuple[list[str], int]:
        discovery_started.set()
        await wait_forever()
        raise AssertionError("unreachable")

    def ensure_gateway(target: str) -> asyncio.Task[None]:
        nonlocal gateway_task
        gateway_task = asyncio.create_task(wait_forever())
        bot._gateway_tasks[target] = gateway_task  # noqa: SLF001
        return gateway_task

    bot.fetch_bot_identity = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(scopes=frozenset())
    )
    bot._clear_dm_capability_state = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    bot.add_target = AsyncMock(  # type: ignore[method-assign]
        return_value="https://guilds.example"
    )
    bot._ensure_gateway_task = ensure_gateway  # type: ignore[method-assign]  # noqa: SLF001
    bot.discover_targets = discover_targets  # type: ignore[method-assign]

    start_task = asyncio.create_task(bot.start("https://guilds.example"))
    await discovery_started.wait()
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert gateway_task is not None
    assert gateway_task.cancelled()
    assert not bot._started  # noqa: SLF001
    assert not bot._starting  # noqa: SLF001
    assert bot._gateway_tasks == {}  # noqa: SLF001


class DiscoveryResponse:
    status_code = 200
    is_success = True
    headers: dict[str, str] = {}
    text = ""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


@pytest.mark.asyncio
async def test_worker_discovers_authority_bound_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, Any]]] = []
    response = DiscoveryResponse(
        {
            "application_ref": "20@apps.example",
            "targets": [
                {
                    "domain": "guilds.example",
                    "origin": "https://guilds.example",
                    "generation": "3",
                    "install_types": ["guild_install"],
                }
            ],
            "poll_after_seconds": 45,
        }
    )

    class HttpClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["base_url"] == "https://apps.example"
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self) -> HttpClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, path: str, *, json: dict[str, Any]) -> DiscoveryResponse:
            requests.append((path, json))
            return response

    monkeypatch.setattr(client_module.httpx, "AsyncClient", HttpClient)
    bot = target_client()

    origins, poll_after = await bot.discover_targets()

    assert origins == ["https://guilds.example"]
    assert poll_after == 45
    assert requests[0][0] == "/api/v1/bot-workers/targets"
    assertion = requests[0][1]
    assert assertion["application_ref"] == "20@apps.example"
    assert assertion["worker_id"] == 40
    assert assertion["audience"] == ("https://apps.example/api/v1/bot-workers/targets")


@pytest.mark.asyncio
async def test_worker_rejects_discovery_origin_outside_signed_target_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = DiscoveryResponse(
        {
            "application_ref": "20@apps.example",
            "targets": [
                {
                    "domain": "guilds.example",
                    "origin": "https://attacker.example",
                    "generation": "3",
                    "install_types": ["guild_install"],
                }
            ],
            "poll_after_seconds": 30,
        }
    )

    class HttpClient:
        def __init__(self, **_: object) -> None:
            return None

        async def __aenter__(self) -> HttpClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, *_: object, **__: object) -> DiscoveryResponse:
            return response

    monkeypatch.setattr(client_module.httpx, "AsyncClient", HttpClient)

    with pytest.raises(ApiError, match="signed authority"):
        await target_client().discover_targets()


@pytest.mark.asyncio
async def test_discovery_removes_revoked_auto_target_but_keeps_explicit_target() -> (
    None
):
    bot = target_client()
    bot.add_target = AsyncMock(side_effect=lambda target: target)  # type: ignore[method-assign]
    bot._explicit_targets = {"https://one.example"}  # noqa: SLF001
    bot._discovered_targets = {  # noqa: SLF001
        "https://one.example",
        "https://two.example",
    }
    remove = AsyncMock()
    bot._remove_discovered_target = remove  # type: ignore[method-assign]  # noqa: SLF001
    bot._ensure_gateway_task = lambda _: AsyncMock()  # type: ignore[assignment]  # noqa: SLF001

    await bot._apply_discovered_targets(["https://one.example"])  # noqa: SLF001

    remove.assert_awaited_once_with("https://two.example")


@pytest.mark.asyncio
async def test_application_media_uses_home_token_without_a_runtime_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class HttpClient:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["base_url"] == "https://apps.example"

        async def post(self, path: str, *, json: dict[str, Any]) -> DiscoveryResponse:
            calls.append(("POST", path))
            assert json["audience"] == (
                "https://apps.example/api/v1/bot-workers/home-token"
            )
            return DiscoveryResponse(
                {
                    "access_token": "kb1_at_home",
                    "expires_in": 480,
                    "dpop_thumbprint": "thumbprint",
                }
            )

        async def request(
            self,
            method: str,
            path: str,
            **kwargs: object,
        ) -> DiscoveryResponse:
            calls.append((method, path))
            headers = kwargs["headers"]
            assert isinstance(headers, dict)
            assert headers["Authorization"] == "Bot kb1_at_home"
            return DiscoveryResponse({"items": []})

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(client_module.httpx, "AsyncClient", HttpClient)
    bot = target_client()

    result = await bot.request("GET", "/api/v1/bots/applications/@me/assets")
    await bot.close()

    assert result == {"items": []}
    assert calls == [
        ("POST", "/api/v1/bot-workers/home-token"),
        ("GET", "/api/v1/bots/applications/@me/assets"),
    ]
