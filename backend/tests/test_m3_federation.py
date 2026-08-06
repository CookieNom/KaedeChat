import base64
import gzip
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.core.federation import (
    SECURITY_CRITICAL_GUILD_EVENTS,
    block_covers_domain,
    federation_policy_holds_event,
    sign_envelope,
    verify_envelope,
)
from app.core.gateway_ops import EVENT_NAMES
from app.core.settings import Settings
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.models import Channel
from app.federation.client import silence_blocks_path
from app.federation.delivery import (
    BACKOFF_SECONDS,
    expired_guild_context,
    lock_outbox_destinations,
    publish_dm_delivery_update,
    retry_delay,
)
from app.federation.events import ensure_queue_destination
from app.federation.guilds import (
    GUILD_MUTATION_EVENT_TYPES,
    fetch_guild_snapshot,
    guild_event_channel_ref,
    guild_event_requires_snapshot,
    guild_history_requires_snapshot,
    guild_snapshot_payload,
    guild_snapshot_rate_scope,
    mark_guild_replica_stale,
    synchronize_guild,
    tombstone_omitted_replicated_channel,
    validate_guild_snapshot,
)
from app.federation.link import websocket_url
from app.federation.network import (
    FederationNetworkError,
    bounded_http_request,
    normalize_domain,
    peer_base_url,
    peer_key_needs_refresh,
    public_address,
    retire_omitted_peer_keys,
)
from app.federation.replication import resolve_delegated_profile, validate_snowflake_timestamp
from app.federation.schemas import DMOpenFederationRequest, EventEnvelope, RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    admit_unknown_key_refresh,
    bounded_request_body,
    enforce_federation_route_rate_limit,
    enforce_federation_source_rate_limit,
    enforce_origin_event_rate_limit,
    event_timestamp_allowed,
    federation_event_policy_code,
    lock_block_policy_shared,
    require_guild_federation_access,
    validated_event_envelope,
)
from app.federation.users import refresh_remote_user, resolve_handle

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": VALID_KEY,
        "database_url": "postgresql+asyncpg://test:test@postgres/test",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
        "federation_peer_overrides": {
            "beta.localhost": "http://beta-api:8000",
        },
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_federated_snowflake_timestamp_must_match_signed_creation_time() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    timestamp = int(created_at.timestamp() * 1000) - EPOCH_MS
    identifier = timestamp << (WORKER_BITS + SEQUENCE_BITS)
    event_timestamp_ms = int(created_at.timestamp() * 1000)

    validate_snowflake_timestamp(
        identifier,
        created_at,
        "message",
        event_timestamp_ms=event_timestamp_ms,
    )
    with pytest.raises(ValueError, match="does not match"):
        validate_snowflake_timestamp(
            identifier,
            created_at + timedelta(minutes=2),
            "message",
            event_timestamp_ms=event_timestamp_ms,
        )
    with pytest.raises(ValueError, match="signed event timestamp"):
        validate_snowflake_timestamp(
            identifier,
            created_at,
            "message",
            event_timestamp_ms=event_timestamp_ms + 120_000,
        )
    with pytest.raises(ValueError, match="timezone"):
        validate_snowflake_timestamp(
            identifier,
            created_at.replace(tzinfo=None),
            "message",
            event_timestamp_ms=event_timestamp_ms,
        )


def test_peer_key_cache_refreshes_and_retires_omitted_keys() -> None:
    now = datetime(2026, 7, 18, tzinfo=UTC)
    current = cast(
        Any,
        SimpleNamespace(
            key_id="ed25519:current",
            fetched_at=now - timedelta(minutes=30),
            expired_at=None,
        ),
    )
    omitted = cast(
        Any,
        SimpleNamespace(
            key_id="ed25519:omitted",
            fetched_at=now - timedelta(minutes=30),
            expired_at=None,
        ),
    )
    stale = cast(
        Any,
        SimpleNamespace(
            key_id="ed25519:stale",
            fetched_at=now - timedelta(hours=2),
            expired_at=None,
        ),
    )

    assert not peer_key_needs_refresh(current, now)
    assert peer_key_needs_refresh(stale, now)
    retire_omitted_peer_keys([current, omitted], {current.key_id}, now)
    assert current.expired_at is None
    assert omitted.expired_at == now


def test_omitted_replicated_channel_becomes_an_inaccessible_tombstone() -> None:
    channel = Channel(
        id=42,
        origin_domain="beta.localhost",
        guild_id=7,
        guild_domain="beta.localhost",
        type=0,
        name="private-room",
        topic="not retained after permission loss",
        position=3,
        parent_id=8,
        parent_domain="beta.localhost",
        rate_limit_per_user=10,
        created_floor_id=42,
    )

    tombstone_omitted_replicated_channel(channel)

    assert channel.unavailable
    assert (channel.guild_id, channel.guild_domain) == (7, "beta.localhost")
    assert channel.name is None
    assert channel.topic is None
    assert (channel.parent_id, channel.parent_domain) == (None, None)


def test_durable_event_timestamp_is_bounded_by_skew_and_retention() -> None:
    now_ms = 1_800_000_000_000
    assert event_timestamp_allowed(
        now_ms - 7 * 86_400_000,
        now_ms=now_ms,
        future_skew_seconds=300,
        retention_days=7,
    )
    assert not event_timestamp_allowed(
        now_ms - 7 * 86_400_000 - 1,
        now_ms=now_ms,
        future_skew_seconds=300,
        retention_days=7,
    )
    assert not event_timestamp_allowed(
        now_ms + 300_001,
        now_ms=now_ms,
        future_skew_seconds=300,
        retention_days=7,
    )


def test_only_local_guild_events_create_expiry_resync_markers() -> None:
    local_event = cast(
        Any,
        SimpleNamespace(
            event_type="guild.message.create",
            envelope={"context": {"guild_id": "42", "guild_domain": "alpha.localhost"}},
        ),
    )
    assert expired_guild_context(local_event, "alpha.localhost") == (42, "alpha.localhost")
    local_event.envelope["context"]["guild_domain"] = "beta.localhost"
    assert expired_guild_context(local_event, "alpha.localhost") is None
    local_event.event_type = "dm.message.create"
    assert expired_guild_context(local_event, "alpha.localhost") is None


def test_complete_guild_mutation_registry_and_snapshot_fences() -> None:
    assert {
        "guild.update",
        "guild.channel.create",
        "guild.channel.update",
        "guild.channel.delete",
        "guild.role.create",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.ban.add",
        "guild.ban.remove",
        "guild.message.update",
        "guild.message.delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.pin.add",
        "guild.pin.remove",
    } == GUILD_MUTATION_EVENT_TYPES
    event = {
        "context": {
            "channel_id": "42",
            "channel_domain": "alpha.localhost",
            "snapshot_required": True,
        }
    }
    assert guild_event_requires_snapshot(event)
    assert guild_event_channel_ref(event) == (42, "alpha.localhost")


def test_snapshot_rate_scope_is_bound_to_the_structural_revision() -> None:
    assert guild_snapshot_rate_scope(42, 7, paginated=False) == "guild-snapshot-start:42:7"
    assert guild_snapshot_rate_scope(42, 8, paginated=False) == "guild-snapshot-start:42:8"
    assert guild_snapshot_rate_scope(42, 8, paginated=True) == "guild-snapshot-page:42:8"


@pytest.mark.asyncio
async def test_failed_resync_remains_durably_discoverable() -> None:
    guild = SimpleNamespace(last_event_seq=6, sync_status="ready")
    session = SimpleNamespace(scalar=AsyncMock(return_value=guild))

    marked = await mark_guild_replica_stale(
        cast(Any, session),
        settings(domain="beta.localhost"),
        42,
        "alpha.localhost",
        7,
    )

    assert marked
    assert guild.sync_status == "stale"

    guild.sync_status = "ready"
    guild.last_event_seq = 7
    assert not await mark_guild_replica_stale(
        cast(Any, session),
        settings(domain="beta.localhost"),
        42,
        "alpha.localhost",
        7,
    )
    assert guild.sync_status == "ready"


def test_hot_link_url_preserves_authority_and_forces_websocket_scheme() -> None:
    assert websocket_url("https://beta.example", "/_kaede/v1/link") == (
        "wss://beta.example/_kaede/v1/link"
    )
    assert websocket_url("http://beta-api:8000", "/_kaede/v1/link") == (
        "ws://beta-api:8000/_kaede/v1/link"
    )


def test_missing_or_rolled_back_guild_history_requires_an_explicit_snapshot() -> None:
    assert guild_history_requires_snapshot(
        after_seq=3,
        latest_seq=8,
        first_retained_seq=6,
    )
    assert guild_history_requires_snapshot(
        after_seq=9,
        latest_seq=8,
        first_retained_seq=None,
    )
    assert not guild_history_requires_snapshot(
        after_seq=3,
        latest_seq=8,
        first_retained_seq=4,
    )
    assert not guild_history_requires_snapshot(
        after_seq=8,
        latest_seq=8,
        first_retained_seq=None,
    )


def test_event_envelope_signature_excludes_only_signatures() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    envelope = {
        "event_id": "kcfe_0123456789abcdef",
        "origin": "alpha.localhost",
        "type": "dm.message.create",
        "ts": 42,
        "actor": {"id": "123", "domain": "alpha.localhost"},
        "context": {},
        "content": {"text": "lantern"},
    }
    signature = base64.b64decode(sign_envelope(envelope, private))
    envelope["signatures"] = {"alpha.localhost": {"ed25519:test": "ignored"}}
    assert verify_envelope(envelope, signature, private.public_key())
    envelope["content"] = {"text": "tampered"}
    assert not verify_envelope(envelope, signature, private.public_key())


def test_event_validation_preserves_signed_extension_members() -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw: dict[str, Any] = {
        "event_id": "kcfe_0123456789abcdef",
        "origin": "alpha.localhost",
        "type": "dm.message.create",
        "ts": 42,
        "actor": {"id": "123", "domain": "alpha.localhost", "extension": "signed"},
        "context": {},
        "content": {"text": "lantern"},
        "extension": {"must": "remain signed"},
    }
    encoded = sign_envelope(raw, private)
    raw["signatures"] = {"alpha.localhost": {"ed25519:test": encoded}}
    parsed = EventEnvelope.model_validate(raw)
    rendered = parsed.model_dump(mode="json")
    assert rendered["extension"] == {"must": "remain signed"}
    assert rendered["actor"]["extension"] == "signed"
    assert verify_envelope(rendered, base64.b64decode(encoded), private.public_key())
    rendered["extension"] = {"must": "not be malleable"}
    assert not verify_envelope(rendered, base64.b64decode(encoded), private.public_key())


@pytest.mark.asyncio
async def test_gap_fill_event_verification_rejects_expired_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    raw: dict[str, Any] = {
        "event_id": "kcfe_0123456789abcdef",
        "origin": "alpha.localhost",
        "type": "guild.message.create",
        "ts": now_ms,
        "actor": {"id": "123", "domain": "alpha.localhost"},
        "context": {"guild_id": "456", "guild_domain": "alpha.localhost", "seq": "1"},
        "content": {},
    }
    raw["signatures"] = {"alpha.localhost": {"ed25519:test": sign_envelope(raw, private)}}

    class FakeSession:
        async def get(self, _model: object, _identity: object) -> object:
            return SimpleNamespace(
                public_key=private.public_key().public_bytes_raw(), expired_at=None
            )

    parsed = await validated_event_envelope(
        cast(Any, FakeSession()), settings(), "alpha.localhost", raw
    )
    assert parsed.event_id == raw["event_id"]

    class ExpiredSession:
        async def get(self, _model: object, _identity: object) -> object:
            return SimpleNamespace(public_key=private.public_key().public_bytes_raw(), expired_at=1)

    refresh = AsyncMock()
    monkeypatch.setattr("app.federation.security.ensure_peer", refresh)
    with pytest.raises(ValueError, match="signature"):
        await validated_event_envelope(
            cast(Any, ExpiredSession()), settings(), "alpha.localhost", raw
        )
    refresh.assert_awaited_once()

    raw["ts"] = now_ms + 301_000
    raw["signatures"] = {"alpha.localhost": {"ed25519:test": sign_envelope(raw, private)}}
    with pytest.raises(ValueError, match="timestamp"):
        await validated_event_envelope(cast(Any, FakeSession()), settings(), "alpha.localhost", raw)
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_envelope_verification_refreshes_once_for_a_rotated_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    raw: dict[str, Any] = {
        "event_id": "kcfe_0123456789abcdef",
        "origin": "beta.localhost",
        "type": "guild.message.create",
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": "123", "domain": "beta.localhost"},
        "context": {"guild_id": "456", "guild_domain": "beta.localhost", "seq": "1"},
        "content": {},
    }
    raw["signatures"] = {"beta.localhost": {"ed25519:rotated": sign_envelope(raw, private)}}

    class RotatingSession:
        refreshed = False

        async def get(self, _model: object, _identity: object) -> object | None:
            if not self.refreshed:
                return None
            return SimpleNamespace(
                public_key=private.public_key().public_bytes_raw(),
                expired_at=None,
            )

    session = RotatingSession()

    async def refresh_peer(
        _session: object, _settings: Settings, domain: str, *, force: bool = False
    ) -> object:
        assert domain == "beta.localhost"
        assert force
        session.refreshed = True
        return object()

    refresh = AsyncMock(side_effect=refresh_peer)
    monkeypatch.setattr("app.federation.security.ensure_peer", refresh)

    envelope = await validated_event_envelope(cast(Any, session), settings(), "beta.localhost", raw)

    assert envelope.event_id == raw["event_id"]
    refresh.assert_awaited_once()


def test_block_policy_holds_durable_traffic_and_protects_reconciliation() -> None:
    assert federation_policy_holds_event("suspend", "dm.message.create")
    assert federation_policy_holds_event("suspend", "guild.message.create")
    assert not federation_policy_holds_event("silence", "dm.message.create")
    assert federation_policy_holds_event("silence", "guild.message.create")
    assert {
        "guild.access.revoked",
        "guild.resync.required",
        "relationship.remove",
    } == SECURITY_CRITICAL_GUILD_EVENTS


def test_block_subdomain_coverage_has_a_label_boundary() -> None:
    assert block_covers_domain("example.com", True, "chat.example.com")
    assert block_covers_domain("example.com", False, "example.com")
    assert not block_covers_domain("example.com", False, "chat.example.com")
    assert not block_covers_domain("example.com", True, "notexample.com")


@pytest.mark.asyncio
async def test_expiration_takes_destination_advisories_in_global_order() -> None:
    statements: list[str] = []

    class FakeSession:
        async def scalar(self, statement: object) -> object:
            compiled = cast(Any, statement).compile(compile_kwargs={"literal_binds": True})
            statements.append(str(compiled))
            return None

    await lock_outbox_destinations(
        cast(Any, FakeSession()),
        ["zeta.localhost", "alpha.localhost", "zeta.localhost", "middle.localhost"],
    )

    assert len(statements) == 3
    assert "kaede-outbox:alpha.localhost" in statements[0]
    assert "kaede-outbox:middle.localhost" in statements[1]
    assert "kaede-outbox:zeta.localhost" in statements[2]


def test_silenced_principal_cannot_pull_or_proxy_guild_state() -> None:
    require_guild_federation_access(FederationPrincipal("beta.localhost", "ed25519:test"))
    with pytest.raises(HTTPException) as raised:
        require_guild_federation_access(
            FederationPrincipal("beta.localhost", "ed25519:test", silenced=True)
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}


@pytest.mark.asyncio
async def test_federation_policy_fence_uses_the_shared_global_lock() -> None:
    statements: list[str] = []

    class FakeSession:
        async def scalar(self, statement: object) -> object:
            compiled = cast(Any, statement).compile(compile_kwargs={"literal_binds": True})
            statements.append(str(compiled))
            return None

    await lock_block_policy_shared(cast(Any, FakeSession()))

    assert len(statements) == 1
    assert "pg_advisory_xact_lock_shared" in statements[0]
    assert "kaede-instance-blocks" in statements[0]


def test_local_silence_blocks_symmetric_guild_federation_surfaces() -> None:
    assert silence_blocks_path("/_kaede/v1/users/lookup")
    assert silence_blocks_path("/_kaede/v1/invites/resolve")
    assert silence_blocks_path("/_kaede/v1/guilds/123/snapshot")
    assert silence_blocks_path("/_kaede/v1/guilds/123/events")
    assert silence_blocks_path("/_kaede/v1/guilds/123/proxy")
    assert silence_blocks_path("/_kaede/v1/guilds/123/join")
    assert not silence_blocks_path("/_kaede/v1/inbox")
    assert not silence_blocks_path("/_kaede/v1/dms/open")


@pytest.mark.asyncio
async def test_mixed_inbox_rechecks_silence_for_each_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return None

    policies = AsyncMock(
        side_effect=[
            SimpleNamespace(level="silence"),
            SimpleNamespace(level="silence"),
            SimpleNamespace(level="suspend"),
        ]
    )
    monkeypatch.setattr("app.federation.security.matching_block", policies)
    session = cast(Any, FakeSession())

    assert (
        await federation_event_policy_code(session, "beta.localhost", "guild.message.create")
        == "KAED_FED_INSTANCE_SILENCED"
    )
    assert (
        await federation_event_policy_code(session, "beta.localhost", "dm.message.create") is None
    )
    assert (
        await federation_event_policy_code(session, "beta.localhost", "dm.message.create")
        == "KAED_FED_INSTANCE_SUSPENDED"
    )
    assert policies.await_count == 3


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "100.64.0.1",
        "100.127.255.254",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "fec0::1",
        "fe80::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "::ffff:100.64.0.1",
        "not-an-address",
    ],
)
def test_ssrf_guard_rejects_non_public_addresses(address: str) -> None:
    assert not public_address(address)


@pytest.mark.parametrize(
    "address",
    ["8.8.8.8", "2001:4860:4860::8888", "::ffff:8.8.8.8"],
)
def test_ssrf_guard_accepts_globally_routable_addresses(address: str) -> None:
    assert public_address(address)


@pytest.mark.asyncio
async def test_peer_response_is_bounded_while_streaming() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x" * 17))
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FederationNetworkError, match="size limit"):
            await bounded_http_request(
                client,
                "GET",
                "https://beta.example/federation",
                max_response_bytes=16,
            )


@pytest.mark.asyncio
async def test_bounded_peer_response_is_not_decoded_twice() -> None:
    encoded = gzip.compress(b'{"snapshot":"ok"}')
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=encoded)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        response = await bounded_http_request(
            client,
            "GET",
            "https://beta.example/snapshot",
            max_response_bytes=1024,
        )
    assert response.json() == {"snapshot": "ok"}
    assert "Content-Encoding" not in response.headers


@pytest.mark.asyncio
async def test_cached_federation_request_body_still_enforces_size_limit() -> None:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    request._body = b"x" * (1024 * 1024 + 1)

    with pytest.raises(HTTPException) as raised:
        await bounded_request_body(request)
    assert raised.value.status_code == 413


@pytest.mark.asyncio
async def test_unknown_key_refresh_consumes_both_unauthenticated_quotas() -> None:
    class FakeRedis:
        async def eval(self, _script: str, _keys: int, *_args: object) -> list[int]:
            return [1, 1]

    redis = FakeRedis()
    assert await admit_unknown_key_refresh(cast(Any, redis), "192.0.2.10", "beta.example")
    assert await admit_unknown_key_refresh(cast(Any, redis), "192.0.2.10", "beta.example")

    class LimitedRedis(FakeRedis):
        async def eval(self, _script: str, _keys: int, *_args: object) -> list[int]:
            return [0, 0]

    with pytest.raises(HTTPException) as raised:
        await admit_unknown_key_refresh(cast(Any, LimitedRedis()), "192.0.2.10", "beta.example")
    assert raised.value.status_code == 429


@pytest.mark.asyncio
async def test_federation_source_limit_runs_before_authenticated_origin_work() -> None:
    class LimitedRedis:
        async def eval(self, _script: str, _keys: int, *_args: object) -> list[int]:
            return [0, 0]

    with pytest.raises(HTTPException) as raised:
        await enforce_federation_source_rate_limit(cast(Any, LimitedRedis()), "192.0.2.10")
    assert raised.value.status_code == 429


@pytest.mark.asyncio
async def test_invite_and_join_routes_have_independent_admission_buckets() -> None:
    seen: list[str] = []

    class FakeRedis:
        async def eval(self, _script: str, _keys: int, key: str, *_args: object) -> list[int]:
            seen.append(key)
            return [1, 0]

    redis = cast(Any, FakeRedis())
    await enforce_federation_route_rate_limit(
        redis, "beta.localhost", "invite-resolve", capacity=30, refill_per_minute=30
    )
    await enforce_federation_route_rate_limit(
        redis, "beta.localhost", "guild-join", capacity=10, refill_per_minute=10
    )
    assert seen == [
        "federation:route:invite-resolve:beta.localhost",
        "federation:route:guild-join:beta.localhost",
    ]


@pytest.mark.asyncio
async def test_federation_event_budget_charges_the_entire_batch() -> None:
    seen: list[object] = []

    class FakeRedis:
        async def eval(self, _script: str, _keys: int, *args: object) -> list[int]:
            seen.extend(args)
            return [1, 175]

    await enforce_origin_event_rate_limit(cast(Any, FakeRedis()), "beta.localhost", 25)
    assert seen[0] == "federation:event-rate:beta.localhost"
    assert seen[-1] == "25"


@pytest.mark.asyncio
async def test_known_offline_destination_queues_without_key_refresh() -> None:
    known = SimpleNamespace(domain="beta.localhost")

    class FakeSession:
        async def get(self, _model: object, key: object) -> object:
            assert key == "beta.localhost"
            return known

    resolved = await ensure_queue_destination(
        cast(Any, FakeSession()),
        settings(),
        "beta.localhost",
        discover_destination=True,
    )
    assert resolved is known


@pytest.mark.asyncio
async def test_blocked_unknown_destination_uses_placeholder_without_discovery() -> None:
    placeholder = SimpleNamespace(domain="blocked.localhost", current_key_id=None)

    class FakeSession:
        lookups = 0

        async def get(self, _model: object, key: object, **_kwargs: object) -> object | None:
            assert key == "blocked.localhost"
            self.lookups += 1
            return None if self.lookups == 1 else placeholder

        async def execute(self, _statement: object) -> object:
            return object()

    resolved = await ensure_queue_destination(
        cast(Any, FakeSession()),
        settings(),
        "blocked.localhost",
        discover_destination=True,
        create_offline_placeholder=True,
    )

    assert resolved is placeholder


@pytest.mark.asyncio
async def test_remote_user_lookup_uses_fresh_cache_without_network() -> None:
    cached = SimpleNamespace(
        id=123,
        origin_domain="beta.localhost",
        username="bob",
        is_local=False,
        updated_at=datetime.now(UTC) - timedelta(seconds=30),
    )

    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return cached

    class NoRedis:
        async def exists(self, _key: str) -> int:
            raise AssertionError("fresh cached lookups must not touch Redis")

    resolved = await resolve_handle(
        cast(Any, FakeSession()),
        settings(),
        cast(Any, NoRedis()),
        "alpha.localhost:1",
        "bob@beta.localhost",
    )
    assert resolved is cached


@pytest.mark.asyncio
async def test_stale_remote_user_returns_immediately_and_coalesces_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = SimpleNamespace(
        id=123,
        origin_domain="beta.localhost",
        username="bob",
        is_local=False,
        updated_at=datetime.now(UTC) - timedelta(minutes=6),
    )
    queued = AsyncMock(return_value=True)
    monkeypatch.setattr("app.federation.users.enqueue_best_effort", queued)

    class FakeSession:
        async def scalar(self, _statement: object) -> object:
            return cached

    class FakeRedis:
        async def set(self, key: str, value: str, **kwargs: object) -> bool:
            assert key == "federation:user-lookup:refresh:beta.localhost:bob"
            assert value == "1"
            assert kwargs == {"ex": 30, "nx": True}
            return True

    resolved = await resolve_handle(
        cast(Any, FakeSession()),
        settings(),
        cast(Any, FakeRedis()),
        "alpha.localhost:1",
        "bob@beta.localhost",
    )

    assert resolved is cached
    queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_profile_refresh_obeys_target_domain_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock(side_effect=AssertionError("rate-limited refresh reached the network"))
    monkeypatch.setattr("app.federation.users.signed_request", request)

    class LimitedRedis:
        async def eval(self, _script: str, keys: int, *args: object) -> int:
            assert keys == 1
            assert ":target:beta.localhost:" in str(args[0])
            return 121

    result = await refresh_remote_user(
        cast(Any, object()), settings(), cast(Any, LimitedRedis()), "bob", "beta.localhost"
    )
    assert result is None
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_user_lookup_limits_outbound_amplification() -> None:
    class FakeSession:
        async def scalar(self, _statement: object) -> None:
            return None

    class LimitedRedis:
        async def exists(self, _key: str) -> int:
            return 0

        async def eval(self, _script: str, _keys: int, *_args: object) -> list[int]:
            return [31, 1]

    with pytest.raises(HTTPException) as raised:
        await resolve_handle(
            cast(Any, FakeSession()),
            settings(),
            cast(Any, LimitedRedis()),
            "alpha.localhost:1",
            "bob@beta.localhost",
        )
    assert raised.value.status_code == 429


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", ([31, 1], [1, 121]))
async def test_remote_user_lookup_has_independent_requester_and_target_limits(
    counts: list[int],
) -> None:
    seen: dict[str, object] = {}

    class FakeSession:
        async def scalar(self, _statement: object) -> None:
            return None

    class LimitedRedis:
        async def exists(self, _key: str) -> int:
            return 0

        async def eval(self, _script: str, keys: int, *args: object) -> list[int]:
            seen["keys"] = keys
            seen["args"] = args
            return counts

    with pytest.raises(HTTPException) as raised:
        await resolve_handle(
            cast(Any, FakeSession()),
            settings(),
            cast(Any, LimitedRedis()),
            "alpha.localhost:1",
            "bob@beta.localhost",
        )
    assert raised.value.status_code == 429
    assert seen["keys"] == 2
    rate_keys = cast(tuple[object, ...], seen["args"])
    assert ":requester:alpha.localhost:1:" in str(rate_keys[0])
    assert ":target:beta.localhost:" in str(rate_keys[1])


@pytest.mark.asyncio
async def test_guild_home_cannot_mutate_a_cached_third_party_profile() -> None:
    cached = SimpleNamespace(
        id=77,
        origin_domain="gamma.localhost",
        username="carol",
        display_name="Authoritative Carol",
        avatar_hash="authoritative-avatar",
        is_local=False,
    )

    class FakeSession:
        async def get(self, _model: object, identity: object) -> object:
            assert identity == (77, "gamma.localhost")
            return cached

    delegated = RemoteUserProfile(
        id="77",
        origin_domain="gamma.localhost",
        username="carol",
        display_name="Poisoned by Beta",
        avatar_hash="poisoned-avatar",
    )
    resolved = await resolve_delegated_profile(
        cast(Any, FakeSession()),
        settings(),
        delegated,
        authority_origin="beta.localhost",
    )

    assert resolved is cached
    assert cached.display_name == "Authoritative Carol"
    assert cached.avatar_hash == "authoritative-avatar"


@pytest.mark.asyncio
async def test_guild_home_cannot_create_an_unknown_third_party_profile() -> None:
    class FakeSession:
        async def get(self, _model: object, _identity: object) -> None:
            return None

    delegated = RemoteUserProfile(
        id="77",
        origin_domain="gamma.localhost",
        username="carol",
    )
    with pytest.raises(ValueError, match="authoritative origin first"):
        await resolve_delegated_profile(
            cast(Any, FakeSession()),
            settings(),
            delegated,
            authority_origin="beta.localhost",
        )


@pytest.mark.asyncio
async def test_guild_snapshot_pagination_uses_a_stable_membership_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watermark = "2026-07-18T12:00:00+00:00"
    structural = {
        "snapshot_seq": "9",
        "member_snapshot_at": watermark,
        "guild": {"id": "10", "origin_domain": "beta.localhost"},
        "roles": [],
        "channels": [],
        "overwrites": [],
    }
    pages = [
        {
            **structural,
            "members": [
                {
                    "user": {
                        "id": "11",
                        "origin_domain": "beta.localhost",
                        "username": "owner",
                    }
                }
            ],
            "member_roles": [],
            "next_member_cursor": {
                "user_domain": "beta.localhost",
                "user_id": "11",
            },
        },
        {
            **structural,
            "members": [
                {
                    "user": {
                        "id": "12",
                        "origin_domain": "gamma.localhost",
                        "username": "carol",
                    }
                }
            ],
            "member_roles": [],
            "next_member_cursor": None,
        },
    ]
    queries: list[dict[str, str]] = []

    async def fake_signed_request(
        _session: object,
        _settings: object,
        _method: str,
        _origin: str,
        _path: str,
        *,
        query: dict[str, str],
    ) -> httpx.Response:
        queries.append(query)
        return httpx.Response(200, json=pages[len(queries) - 1])

    monkeypatch.setattr("app.federation.guilds.signed_request", fake_signed_request)
    snapshot = await fetch_guild_snapshot(cast(Any, object()), settings(), "beta.localhost", 10)

    assert [item["user"]["id"] for item in snapshot["members"]] == ["11", "12"]
    assert queries == [
        {},
        {
            "member_after_domain": "beta.localhost",
            "member_after_id": "11",
            "member_snapshot_at": watermark,
            "member_snapshot_seq": "9",
        },
    ]


@pytest.mark.asyncio
async def test_successful_gap_fill_restores_a_policy_staled_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="beta.localhost",
        last_event_seq=5,
        sync_status="stale",
        unavailable=True,
    )

    async def fake_signed_request(*_args: object, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"events": [], "latest_seq": "5"})

    monkeypatch.setattr("app.federation.guilds.signed_request", fake_signed_request)

    assert await synchronize_guild(cast(Any, object()), settings(), cast(Any, guild)) == []
    assert guild.sync_status == "ready"
    assert not guild.unavailable


@pytest.mark.asyncio
async def test_peer_override_is_development_only() -> None:
    assert await peer_base_url(settings(), "BETA.LOCALHOST.") == "http://beta-api:8000"
    with pytest.raises(ValidationError, match="peer_overrides"):
        settings(
            environment="production",
            domain="alpha.example",
            app_url="https://alpha.example",
            email_backend="smtp",
            smtp_url="smtps://mail.alpha.example",
            proxy_secret="x" * 32,
            federation_peer_overrides={"beta.example": "https://internal.invalid"},
        )


def test_outbox_backoff_uses_normative_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.federation.delivery._JITTER.uniform", lambda _low, _high: 1.0)
    assert [retry_delay(index + 1).total_seconds() for index in range(8)] == [
        5,
        30,
        120,
        600,
        1800,
        3600,
        3600,
        3600,
    ]
    assert BACKOFF_SECONDS[-1] == 3600


@pytest.mark.asyncio
async def test_dm_delivery_update_uses_composite_message_identity() -> None:
    published: dict[str, Any] = {}

    class FakeRedis:
        async def eval(self, _script: str, _keys: int, *args: object) -> list[object]:
            event = json.loads(str(args[-1]))
            published.update(event)
            return [1, json.dumps(event)]

    event = SimpleNamespace(
        event_type="dm.message.create",
        envelope={
            "actor": {"id": "123", "domain": "alpha.localhost"},
            "content": {
                "message": {
                    "id": "456",
                    "origin_domain": "alpha.localhost",
                    "channel_id": "789",
                    "channel_domain": "alpha.localhost",
                }
            },
        },
    )
    await publish_dm_delivery_update(
        cast(Any, FakeRedis()),
        settings(),
        cast(Any, event),
        "beta.localhost",
        "delivered",
        None,
    )
    assert published["t"] == "MESSAGE_DELIVERY_UPDATE"
    assert published["d"]["message_id"] == "456"
    assert published["d"]["message_domain"] == "alpha.localhost"
    assert "MESSAGE_DELIVERY_UPDATE" in EVENT_NAMES
    assert "DM_OPEN_REJECTED" in EVENT_NAMES


def test_dm_open_requires_two_distinct_federated_identities() -> None:
    participant = {
        "id": "123",
        "origin_domain": normalize_domain("alpha.localhost"),
        "username": "alice",
    }
    with pytest.raises(ValidationError):
        DMOpenFederationRequest(participants=[participant, participant])


def test_federation_profiles_enforce_database_ids_and_domain_syntax() -> None:
    with pytest.raises(ValidationError):
        RemoteUserProfile(id=str(1 << 63), origin_domain="alpha.localhost", username="alice")
    with pytest.raises(ValidationError):
        RemoteUserProfile(id=1, origin_domain="alpha.localhost", username="alice")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RemoteUserProfile(id="01", origin_domain="alpha.localhost", username="alice")
    with pytest.raises((ValidationError, FederationNetworkError)):
        RemoteUserProfile(id="1", origin_domain="bad/.localhost", username="alice")
    with pytest.raises(FederationNetworkError):
        normalize_domain("bad/.localhost")


def test_guild_snapshot_rejects_cross_origin_entity_injection() -> None:
    snapshot: dict[str, Any] = {
        "snapshot_seq": "0",
        "guild": {
            "id": "10",
            "origin_domain": "beta.localhost",
            "name": "Paper Lantern",
            "owner_id": "11",
            "owner_domain": "beta.localhost",
            "permission_generation": "1",
        },
        "roles": [
            {
                "id": "10",
                "origin_domain": "beta.localhost",
                "name": "@everyone",
                "color": 0,
                "permissions": "0",
                "position": 0,
                "hoist": False,
                "mentionable": False,
            }
        ],
        "channels": [
            {
                "id": "12",
                "origin_domain": "beta.localhost",
                "type": 0,
                "name": "general",
                "topic": None,
                "position": 0,
                "parent_id": None,
                "parent_domain": None,
                "rate_limit_per_user": 0,
                "created_floor_id": "12",
            }
        ],
        "members": [
            {
                "user": {
                    "id": "11",
                    "origin_domain": "beta.localhost",
                    "username": "owner",
                },
                "nickname": None,
                "joined_at": "2026-07-17T00:00:00+00:00",
                "timeout_until": None,
                "voice_flags": 0,
                "member_version": "1",
            }
        ],
        "member_roles": [],
        "overwrites": [],
    }
    validate_guild_snapshot(snapshot, expected_origin="beta.localhost", expected_guild_id=10)
    snapshot["channels"][0]["origin_domain"] = "evil.example"
    with pytest.raises(ValueError, match="channel identity"):
        validate_guild_snapshot(snapshot, expected_origin="beta.localhost", expected_guild_id=10)


def test_guild_snapshot_flattens_child_of_hidden_category() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="beta.localhost",
        next_event_seq=2,
        name="Paper Lantern",
        description=None,
        icon_hash=None,
        banner_hash=None,
        owner_id=11,
        owner_domain="beta.localhost",
        permission_generation=1,
        federated_history_policy="disabled",
        history_policy_generation=1,
    )
    visible_child = SimpleNamespace(
        id=12,
        origin_domain="beta.localhost",
        type=0,
        name="visible-child",
        topic=None,
        position=0,
        parent_id=13,
        parent_domain="beta.localhost",
        rate_limit_per_user=0,
        federated_history_policy="inherit",
        created_floor_id=12,
        permissions_synced=False,
    )

    snapshot_at = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    payload = guild_snapshot_payload(
        cast(Any, guild),
        [],
        [cast(Any, visible_child)],
        [],
        [],
        [],
        member_snapshot_at=snapshot_at,
        next_member_cursor=("gamma.localhost", 77),
    )

    assert payload["channels"][0]["parent_id"] is None
    assert payload["channels"][0]["parent_domain"] is None
    assert payload["member_snapshot_at"] == snapshot_at.isoformat()
    assert payload["next_member_cursor"] == {
        "user_domain": "gamma.localhost",
        "user_id": "77",
    }
