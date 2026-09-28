import base64
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.auth import ROLE_CAPABILITIES, AdminPrincipal
from app.api import rtc
from app.core.settings import Settings
from app.db.models import RTCConfiguration, RTCRoomPlacement, RTCWebhookEvent
from app.voice.livekit import LiveKitControl
from app.voice.rtc import (
    RoutingHints,
    RTCConfig,
    RTCRegion,
    endpoint,
    provider_settings,
    seal,
    select_endpoint,
    unseal,
)


def settings():
    return Settings(
        _env_file=None,
        environment="test",
        domain="chat.example",
        secret_key=base64.urlsafe_b64encode(bytes(range(32))).decode(),
        database_url="postgresql+asyncpg://test:test@localhost/test",
        dragonfly_url="redis://localhost:6379/15",
        voice_api_key="local-key",
        voice_api_secret="s" * 32,
    )


def config():
    return RTCConfig(
        provider="cinnamon",
        api_key="project-key",
        api_secret="p" * 32,
        automatic_url="wss://rtc.example.com",
        regions=[
            RTCRegion(id="west", name="West", url="wss://west.example.com"),
            RTCRegion(id="east", name="East", url="wss://east.example.com", enabled=False),
        ],
    )


def test_defaults_credentials_and_routing():
    assert endpoint("WSS://RTC.EXAMPLE.COM/") == "wss://rtc.example.com"
    default = RTCConfig()
    assert default.provider == "builtin" and default.regions == [] and default.automatic_url == ""
    assert default.api_secret is None and default.api_key == ""
    assert default.default_region is None and default.allow_region_selection
    cfg = config()
    encrypted = seal(settings(), {"api_key": cfg.api_key, "api_secret": "p" * 32})
    assert b"project-key" not in encrypted and b"p" * 32 not in encrypted
    assert unseal(settings(), encrypted)["api_secret"] == "p" * 32
    assert select_endpoint(cfg, None) == cfg.automatic_url
    assert select_endpoint(cfg, RoutingHints(region="west")) == cfg.regions[0].url
    assert (
        provider_settings(settings(), cfg, cfg.automatic_url).voice_livekit_url
        == "https://rtc.example.com"
    )
    with pytest.raises(HTTPException):
        select_endpoint(cfg, RoutingHints(region="east"))
    cfg.allow_region_selection = False
    with pytest.raises(HTTPException):
        select_endpoint(cfg, RoutingHints(region="west"))


@pytest.mark.parametrize(
    "url",
    [
        "http://rtc.example.com",
        "wss://localhost",
        "wss://127.0.0.1",
        "wss://key:secret@rtc.example.com",
        "wss://rtc.example.com?token=x",
        "wss://rtc.example.com/path",
    ],
)
def test_endpoint_rejects_unsafe_inputs(url):
    with pytest.raises(ValueError):
        endpoint(url)


@pytest.mark.parametrize(
    "latency",
    [
        {"west": 0},
        {"west": -1},
        {"west": float("nan")},
        {"west": 60001},
        {"west": True},
        {"west": "35"},
        {"bad region": 3},
    ],
)
def test_latency_validation(latency):
    with pytest.raises(ValidationError):
        RoutingHints(latency=latency)


@pytest.mark.parametrize("role", ["operations", "trust_safety", "bot_reviewer", "auditor"])
async def test_only_instance_administrators_can_read_or_change_rtc(role):
    principal = AdminPrincipal(SimpleNamespace(), frozenset({role}), ROLE_CAPABILITIES[role])
    for operation, kwargs in [
        (rtc.get_rtc, {}),
        (rtc.save_rtc, {"payload": RTCConfig()}),
        (rtc.test_rtc, {"payload": RoutingHints(), "redis": AsyncMock()}),
        (rtc.admin_probes, {"redis": AsyncMock()}),
    ]:
        with pytest.raises(HTTPException) as error:
            await operation(principal=principal, session=AsyncMock(), settings=settings(), **kwargs)
        assert error.value.status_code == 403


async def test_probe_discovery_basic_auth_and_enabled_targets(monkeypatch):
    def respond(request):
        assert request.url == "https://rtc.example.com/v1/rtc/probes"
        assert (
            request.headers["Authorization"]
            == "Basic " + base64.b64encode(b"project-key:" + b"p" * 32).decode()
        )
        return httpx.Response(
            200,
            json={
                "regions": [
                    {"region": "west", "probe_url": "https://node.example.com/rtc-probe"},
                    {"region": "east", "probe_url": "https://east.example.com/rtc-probe"},
                    {"region": "west", "probe_url": "http://node.example.com/rtc-probe"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(rtc.httpx, "AsyncClient", lambda **kwargs: client)
    assert await rtc.discover(config()) == [
        {"region": "west", "probe_url": "https://node.example.com/rtc-probe"}
    ]
    redis = AsyncMock()
    redis.get.return_value = None
    hints = RoutingHints(region="west", latency={"west": 35}, probe_ticket="expired")
    assert (await rtc.fresh_routing(redis, settings(), 1, hints)).latency == {}
    redis.get.return_value = '["west"]'
    hints.latency["east"] = 160
    assert (await rtc.fresh_routing(redis, settings(), 1, hints)).latency == {"west": 35}


@pytest.mark.postgres
async def test_saved_secret_redaction_and_webhook_durable_deduplication(postgres_schema):
    await postgres_schema.run_sync(lambda connection: RTCConfiguration.__table__.create(connection))
    await postgres_schema.run_sync(lambda connection: RTCWebhookEvent.__table__.create(connection))
    await postgres_schema.run_sync(lambda connection: RTCRoomPlacement.__table__.create(connection))
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        principal = AdminPrincipal(SimpleNamespace(id=1), frozenset({"owner"}), frozenset({"*"}))
        saved = await rtc.save_rtc(config(), principal, session, settings())
        assert saved["secret_configured"] and "api_secret" not in saved
        assert saved["webhook_url"] == "https://chat.example/api/v1/voice/cinnamon/webhook"
        cfg = config()
        cfg.api_secret = None
        await rtc.save_rtc(cfg, principal, session, settings())
        assert (
            await rtc.configuration(session, settings())
        ).api_secret.get_secret_value() == "p" * 32
        body = b'{"id":"test-event","event":"room_started","room":{"name":"test-room"}}'

        async def deliver(raw, signature_body=None):
            signed = signature_body if signature_body is not None else raw
            token = jwt.encode(
                {
                    "iss": "project-key",
                    "sha256": base64.b64encode(hashlib.sha256(signed).digest()).decode(),
                },
                "p" * 32,
                algorithm="HS256",
            )

            async def receive():
                return {"type": "http.request", "body": raw, "more_body": False}

            request = Request({"type": "http", "headers": []}, receive)
            return await rtc.cinnamon_webhook(request, token, session, settings())

        assert (await deliver(body)).status_code == 204
        assert (await deliver(body)).status_code == 204
        assert len(list(await session.scalars(select(RTCWebhookEvent)))) == 1
        with pytest.raises(HTTPException) as error:
            await deliver(body + b" ", body)
        assert error.value.status_code == 401
        with pytest.raises(HTTPException) as error:
            await deliver(body.replace(b"room_started", b"room_finished"))
        assert error.value.status_code == 409


async def test_active_room_uses_saved_placement_after_configuration_changes(monkeypatch):
    from app.voice import livekit

    stored = provider_settings(settings(), config(), "wss://west.example.com")
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(provider="cinnamon", finished=False)
    client = SimpleNamespace(
        room=SimpleNamespace(list_rooms=AsyncMock(return_value=SimpleNamespace(rooms=[object()])))
    )

    @asynccontextmanager
    async def database(_settings):
        yield session

    @asynccontextmanager
    async def direct(resolved, latency=None):
        assert resolved is stored
        yield client

    monkeypatch.setattr(livekit, "rtc_session", database)
    monkeypatch.setattr(livekit, "placement_lock", AsyncMock())
    monkeypatch.setattr(livekit, "room_settings", AsyncMock(return_value=stored))
    monkeypatch.setattr(LiveKitControl, "direct_client", staticmethod(direct))
    changed = AsyncMock(side_effect=AssertionError("Active room must not consult new settings"))
    monkeypatch.setattr(livekit, "configuration", changed)
    assert (
        await LiveKitControl(settings()).ensure_room("g.1.2", RoutingHints(region="east")) is stored
    )
    changed.assert_not_called()


@pytest.mark.postgres
async def test_webhook_worker_ignores_old_join_and_retries_failure(postgres_schema, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api import voice
    from app.voice.rtc_webhooks import process_pending_webhooks

    await postgres_schema.run_sync(lambda connection: RTCWebhookEvent.__table__.create(connection))
    factory = async_sessionmaker(
        bind=postgres_schema, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with factory() as session:
        for event_id, kind in [("late-join", "participant_joined"), ("retry", "track_published")]:
            session.add(
                RTCWebhookEvent(
                    domain=settings().domain,
                    event_id=event_id,
                    body=json.dumps(
                        {
                            "id": event_id,
                            "event": kind,
                            "room": {"name": "g.1.2"},
                            "participant": {"identity": "1@chat.example", "sid": "old"},
                        }
                    ),
                    body_hash="0" * 64,
                )
            )
        await session.commit()
    effect = AsyncMock(side_effect=RuntimeError("temporary failure"))
    monkeypatch.setattr(voice, "process_livekit_event", effect)
    monkeypatch.setattr(
        LiveKitControl,
        "list_participants",
        AsyncMock(
            return_value=[
                SimpleNamespace(identity="1@chat.example", sid="new"),
            ]
        ),
    )
    await process_pending_webhooks(factory, AsyncMock(), settings())
    assert effect.await_count == 1  # The stale join never reaches admission or eviction.
    async with factory() as session:
        stale = await session.get(RTCWebhookEvent, (settings().domain, "late-join"))
        retry = await session.get(RTCWebhookEvent, (settings().domain, "retry"))
        assert stale.processed_at is not None
        assert retry.processed_at is None and retry.attempts == 1
        retry.retry_at = retry.accepted_at
        await session.commit()
    effect.side_effect = None
    await process_pending_webhooks(factory, AsyncMock(), settings())
    async with factory() as session:
        assert (
            await session.get(RTCWebhookEvent, (settings().domain, "retry"))
        ).processed_at is not None
    await process_pending_webhooks(factory, AsyncMock(), settings())
    assert effect.await_count == 2  # Completed deliveries do not replay.


async def test_livekit_transport_sends_latency_without_location_and_rejects_redirects():
    from aiohttp import web
    from livekit import api

    from app.voice.livekit import LiveKitError

    requests = []

    async def create(request):
        requests.append(dict(request.headers))
        if len(requests) == 2:
            return web.Response(status=307, headers={"Location": "/unexpected"})
        return web.Response(
            body=api.Room(name="disposable-test").SerializeToString(),
            content_type="application/protobuf",
        )

    server = web.Application()
    server.router.add_post("/twirp/livekit.RoomService/CreateRoom", create)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    resolved = provider_settings(settings(), config(), config().automatic_url)
    resolved = resolved.model_copy(update={"voice_livekit_url": f"http://127.0.0.1:{port}"})
    try:
        async with LiveKitControl.direct_client(resolved, {"west": 35}) as client:
            await client.room.create_room(api.CreateRoomRequest(name="disposable-test"))
            with pytest.raises(LiveKitError):
                await client.room.create_room(api.CreateRoomRequest(name="disposable-test"))
        assert json.loads(requests[0]["X-Cinnamon-Client-Latency"]) == {"west": 35}
        assert not any(k.lower() == "x-cinnamon-client-location" for k in requests[0])
        authorization = requests[0]["Authorization"].removeprefix("Bearer ")
        assert jwt.decode(authorization, "p" * 32, algorithms=["HS256"])["iss"] == "project-key"
    finally:
        await runner.cleanup()


@pytest.mark.postgres
@pytest.mark.parametrize(
    "choice, expected_url, expected_latency",
    [
        (None, "https://rtc.example.com", {"west": 35.0}),
        ("west", "https://west.example.com", None),
    ],
)
async def test_room_creation_persists_route_and_project_credentials(
    postgres_schema,
    monkeypatch,
    choice,
    expected_url,
    expected_latency,
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.voice import livekit
    from app.voice.rtc import room_settings

    await postgres_schema.run_sync(lambda connection: RTCConfiguration.__table__.create(connection))
    await postgres_schema.run_sync(lambda connection: RTCRoomPlacement.__table__.create(connection))
    factory = async_sessionmaker(
        bind=postgres_schema, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )

    @asynccontextmanager
    async def storage(_settings):
        async with factory() as session:
            yield session

    monkeypatch.setattr(livekit, "rtc_session", storage)
    monkeypatch.setattr("app.voice.rtc.rtc_session", storage)
    cfg = config()
    async with factory() as session:
        session.add(
            RTCConfiguration(
                domain=settings().domain,
                configuration=cfg.model_dump(mode="json", exclude={"api_key", "api_secret"}),
                credentials=seal(settings(), {"api_key": cfg.api_key, "api_secret": "p" * 32}),
            )
        )
        await session.commit()
    created = []

    @asynccontextmanager
    async def direct(resolved, latency=None):
        async def create(request):
            created.append((resolved.voice_livekit_url, latency))

        yield SimpleNamespace(
            room=SimpleNamespace(
                list_rooms=AsyncMock(return_value=SimpleNamespace(rooms=[])),
                create_room=create,
            )
        )

    monkeypatch.setattr(LiveKitControl, "direct_client", staticmethod(direct))
    await LiveKitControl(settings()).ensure_room(
        "g.1.2", RoutingHints(region=choice, latency={"west": 35, "east": 160})
    )
    assert created == [(expected_url, expected_latency)]
    saved = await room_settings(settings(), "g.1.2")
    assert saved.voice_livekit_url == expected_url
    assert saved.voice_api_secret.get_secret_value() == "p" * 32
    assert saved._rtc_provider == "cinnamon"


@pytest.mark.postgres
async def test_rtc_migration_is_empty_and_reversible(postgres_schema):
    from importlib import import_module

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text

    migration = import_module("migrations.versions.9e4a1c3d6f82_rtc_configuration")

    def verify(connection):
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.scalar(text("SELECT count(*) FROM rtc_configurations")) == 0
            assert "ix_rtc_webhook_pending" in {
                i["name"] for i in inspect(connection).get_indexes("rtc_webhook_events")
            }
            migration.downgrade()
            assert not inspect(connection).has_table("rtc_configurations")

    await postgres_schema.run_sync(verify)


async def test_client_discovery_contains_no_project_credentials(monkeypatch):
    cfg = config()
    monkeypatch.setattr(rtc, "configuration", AsyncMock(return_value=cfg))
    monkeypatch.setattr(rtc, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(
        rtc,
        "discover",
        AsyncMock(
            return_value=[
                {"region": "west", "probe_url": "https://node.example.com/rtc-probe"},
            ]
        ),
    )
    redis = AsyncMock()
    result = await rtc.routing_payload(
        AsyncMock(), redis, settings(), SimpleNamespace(id=1, origin_domain="chat.example")
    )
    assert result["regions"] == [{"id": "west", "name": "West"}]
    assert "api_key" not in result and "api_secret" not in result
    assert cfg.api_key not in json.dumps(result) and "p" * 32 not in json.dumps(result)
    assert redis.set.await_args.kwargs == {"ex": 60}


async def test_room_listing_continues_after_retired_provider_failure(monkeypatch):
    from app.voice import livekit

    snapshot = SimpleNamespace(room="g.1.2")
    resolved = provider_settings(settings(), config(), config().automatic_url)
    session = AsyncMock()
    session.scalars.return_value = [snapshot]

    @asynccontextmanager
    async def storage(_settings):
        yield session

    @asynccontextmanager
    async def direct(target):
        if target._rtc_provider == "builtin":
            raise RuntimeError("retired endpoint unavailable")
        yield SimpleNamespace(
            room=SimpleNamespace(
                list_rooms=AsyncMock(
                    return_value=SimpleNamespace(
                        rooms=[SimpleNamespace(name="g.1.2"), SimpleNamespace(name="d.9.9")],
                    )
                )
            )
        )

    monkeypatch.setattr(livekit, "rtc_session", storage)
    monkeypatch.setattr(livekit, "placement_settings", lambda *args: resolved)
    monkeypatch.setattr(LiveKitControl, "direct_client", staticmethod(direct))
    assert [r.name for r in await LiveKitControl(settings()).list_rooms()] == ["g.1.2"]


@pytest.mark.parametrize("live", [True, False])
async def test_finish_event_cannot_clear_recreated_room(monkeypatch, live):
    from fastapi import Response
    from livekit import api

    from app.api import voice

    placement = RTCRoomPlacement(
        domain=settings().domain,
        room="g.1.2",
        provider="cinnamon",
        control_url="https://rtc.example.com",
        connection_url="wss://rtc.example.com",
        finished=False,
        credentials=seal(settings(), {"api_key": "project-key", "api_secret": "p" * 32}),
    )
    session = AsyncMock()

    async def get(model, key):
        return placement if model is RTCRoomPlacement else None

    session.get.side_effect = get
    redis = AsyncMock()

    @asynccontextmanager
    async def direct(resolved):
        yield SimpleNamespace(
            room=SimpleNamespace(
                list_rooms=AsyncMock(
                    return_value=SimpleNamespace(
                        rooms=[SimpleNamespace(name="g.1.2")] if live else [],
                    )
                )
            )
        )

    monkeypatch.setattr(LiveKitControl, "direct_client", staticmethod(direct))
    monkeypatch.setattr(voice, "placement_lock", AsyncMock())
    monkeypatch.setattr(voice, "publish_voice_channel_start_time", AsyncMock())
    event = api.WebhookEvent(id="finish", event="room_finished", room=api.Room(name="g.1.2"))
    completed = AsyncMock(return_value=Response(status_code=204))
    await voice.process_livekit_event(event, session, redis, settings(), completed)
    assert placement.finished is (not live)
    assert redis.delete.await_count == (0 if live else 1)
