import base64
import gzip
import hashlib
import json
import socket
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request
from starlette.websockets import WebSocket

import app.api.federation as federation_api
from app.api.federation import (
    _guild_snapshot_cursor_changed,
    federation_user_profile_by_ref,
    visible_guild_channels_for_origin,
    well_known,
)
from app.core.federation import (
    SECURITY_CRITICAL_GUILD_EVENTS,
    SigningInput,
    block_covers_domain,
    content_sha256,
    federation_policy_holds_event,
    sign_envelope,
    sign_request,
    verify_envelope,
)
from app.core.gateway_ops import EVENT_NAMES
from app.core.settings import Settings
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.models import Channel, FederationEvent, FederationOutbox, Instance, PeerKey, User
from app.federation.client import (
    OUTBOUND_FEDERATION_LIMITER,
    OUTBOUND_FEDERATION_REQUEST_LIMITER,
    _prepare_signed_request,
    signed_request,
    silence_blocks_path,
)
from app.federation.delivery import (
    BACKOFF_SECONDS,
    drain_destination,
    expired_guild_context,
    group_state_rejection_is_upgrade_retryable,
    lock_outbox_destinations,
    publish_dm_delivery_update,
    retry_delay,
)
from app.federation.events import ensure_queue_destination
from app.federation.guilds import (
    GUILD_MUTATION_EVENT_TYPES,
    _event_ref,
    apply_guild_mutation_event,
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
    PEER_DISCOVERY_LIMITER,
    FederationInstanceQuotaExceeded,
    FederationNetworkError,
    bounded_http_request,
    decode_federation_response_json,
    ensure_peer,
    ensure_remote_instance_record,
    normalize_domain,
    peer_base_url,
    peer_key_history_exceeds_limit,
    peer_key_needs_refresh,
    public_address,
    public_addresses,
    retire_omitted_peer_keys,
)
from app.federation.replication import (
    insert_unresolved_remote_user,
    remote_media_dimensions,
    resolve_delegated_profile,
    sanitized_remote_blurhash,
    sanitized_remote_variants,
    unresolved_remote_username,
    upsert_remote_user,
    validate_snowflake_timestamp,
)
from app.federation.schemas import DMOpenFederationRequest, EventEnvelope, RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    admit_unknown_key_refresh,
    authenticate_federation,
    authenticate_federation_websocket,
    bounded_request_body,
    consume_request_nonce,
    enforce_federation_route_rate_limit,
    enforce_federation_source_rate_limit,
    enforce_origin_event_rate_limit,
    event_timestamp_allowed,
    federation_event_policy_code,
    federation_request_nonce,
    lock_block_policy_shared,
    refresh_event_signing_keys,
    require_guild_federation_access,
    require_pinned_request_nonce,
    validated_event_envelope,
)
from app.federation.users import refresh_remote_user, resolve_handle

VALID_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode()


@pytest.mark.asyncio
async def test_search_capability_is_advertised_only_when_enabled() -> None:
    disabled = await well_known(settings())
    enabled = await well_known(settings(search_enabled=True, search_master_key="s" * 32))
    assert "message-search/1" not in disabled["capabilities"]
    assert "message-search/1" in enabled["capabilities"]


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


@pytest.mark.asyncio
async def test_rejected_peer_refresh_does_not_mutate_cached_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    old_material = bytes(range(32))
    new_material = bytes(reversed(range(32)))
    instance = Instance(
        domain="beta.localhost",
        is_self=False,
        display_name="Trusted beta",
        software_version="1.0",
        capabilities=["request-nonce/1"],
        current_key_id="ed25519:old",
        last_seen_at=now,
    )
    old_key = PeerKey(
        domain="beta.localhost",
        key_id="ed25519:old",
        public_key=old_material,
        fetched_at=now - timedelta(days=2),
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=instance),
        scalar=AsyncMock(side_effect=[old_key, True, old_key, 1]),
        scalars=AsyncMock(return_value=[old_key]),
        add=AsyncMock(),
        flush=AsyncMock(),
    )
    client = httpx.AsyncClient()
    monkeypatch.setattr(
        "app.federation.network.peer_http_client",
        AsyncMock(return_value=("http://beta-api:8000", client)),
    )
    responses = [
        httpx.Response(
            200,
            request=httpx.Request("GET", "http://beta-api:8000/.well-known/kaede/server"),
            json={"server": "beta.localhost", "versions": ["1"], "capabilities": []},
        ),
        httpx.Response(
            200,
            request=httpx.Request("GET", "http://beta-api:8000/_kaede/v1/keys"),
            json={
                "server_name": "beta.localhost",
                "current_key_id": "ed25519:new",
                "verify_keys": {"ed25519:new": base64.b64encode(new_material).decode("ascii")},
                "old_verify_keys": {},
                "display_name": "Attacker controlled",
                "software_version": "9.9",
            },
        ),
    ]
    monkeypatch.setattr(
        "app.federation.network.bounded_http_request",
        AsyncMock(side_effect=responses),
    )

    with pytest.raises(FederationNetworkError, match="pinned security capability"):
        await ensure_peer(cast(Any, session), settings(), "beta.localhost", force=True)

    assert instance.display_name == "Trusted beta"
    assert instance.software_version == "1.0"
    assert instance.capabilities == ["request-nonce/1"]
    assert instance.current_key_id == "ed25519:old"
    assert old_key.public_key == old_material
    assert old_key.expired_at is None
    session.add.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbound_federation_capacity_fails_before_touching_database() -> None:
    borrowers = [object() for _ in range(OUTBOUND_FEDERATION_REQUEST_LIMITER.total_tokens)]
    for borrower in borrowers:
        OUTBOUND_FEDERATION_REQUEST_LIMITER.acquire_on_behalf_of_nowait(borrower)
    session = SimpleNamespace(get=AsyncMock(), scalar=AsyncMock())
    try:
        with pytest.raises(FederationNetworkError, match="busy"):
            await signed_request(
                cast(Any, session),
                settings(),
                "GET",
                "beta.localhost",
                "/_kaede/v1/test",
            )
    finally:
        for borrower in borrowers:
            OUTBOUND_FEDERATION_REQUEST_LIMITER.release_on_behalf_of(borrower)

    session.get.assert_not_awaited()
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_stream_capacity_does_not_block_interactive_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrowers = [object() for _ in range(OUTBOUND_FEDERATION_LIMITER.total_tokens)]
    for borrower in borrowers:
        OUTBOUND_FEDERATION_LIMITER.acquire_on_behalf_of_nowait(borrower)
    client = httpx.AsyncClient()
    monkeypatch.setattr(
        "app.federation.client._prepare_signed_request",
        AsyncMock(
            return_value=(
                "https://beta.localhost",
                client,
                "/_kaede/v1/test",
                b"",
                {},
            )
        ),
    )
    request = AsyncMock(return_value=httpx.Response(204))
    monkeypatch.setattr("app.federation.client.bounded_http_request", request)
    try:
        response = await signed_request(
            cast(Any, SimpleNamespace()),
            settings(),
            "POST",
            "beta.localhost",
            "/_kaede/v1/test",
        )
    finally:
        for borrower in borrowers:
            OUTBOUND_FEDERATION_LIMITER.release_on_behalf_of(borrower)

    assert response.status_code == 204
    request.assert_awaited_once()


@pytest.mark.asyncio
async def test_interactive_request_does_not_take_background_drain_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(scalar=AsyncMock())
    monkeypatch.setattr("app.federation.client.lock_block_policy_shared", AsyncMock())
    monkeypatch.setattr("app.federation.client.matching_block", AsyncMock(return_value=None))
    monkeypatch.setattr("app.federation.client.ensure_peer", AsyncMock())
    monkeypatch.setattr(
        "app.federation.client.federation_signing_headers",
        AsyncMock(return_value={"Authorization": "test"}),
    )
    client = httpx.AsyncClient()
    monkeypatch.setattr(
        "app.federation.client.peer_http_client",
        AsyncMock(return_value=("https://beta.localhost", client)),
    )

    _, prepared_client, target, _, _ = await _prepare_signed_request(
        cast(Any, session),
        settings(),
        "POST",
        "beta.localhost",
        "/_kaede/v1/guilds/1/proxy-reaction",
        payload={"emoji": "🔥"},
        query=None,
        request_timeout=10,
        hop=1,
    )

    assert target == "/_kaede/v1/guilds/1/proxy-reaction"
    session.scalar.assert_not_awaited()
    await prepared_client.aclose()


@pytest.mark.asyncio
async def test_signed_request_maps_transport_failure_to_federation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("GET", "https://beta.localhost/_kaede/v1/test")
    client = httpx.AsyncClient()
    monkeypatch.setattr(
        "app.federation.client._prepare_signed_request",
        AsyncMock(
            return_value=(
                "https://beta.localhost",
                client,
                "/_kaede/v1/test",
                b"",
                {},
            )
        ),
    )
    monkeypatch.setattr(
        "app.federation.client.bounded_http_request",
        AsyncMock(side_effect=httpx.ReadError("peer reset the stream", request=request)),
    )

    with pytest.raises(FederationNetworkError, match="request failed"):
        await signed_request(
            cast(Any, SimpleNamespace()),
            settings(),
            "GET",
            "beta.localhost",
            "/_kaede/v1/test",
        )


@pytest.mark.asyncio
async def test_duplicate_destination_drain_returns_without_querying_rows() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.scalars = AsyncMock()

        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _statement: object) -> bool | None:
            self.scalar_calls += 1
            # Shared policy lock, then fail-fast destination drain lock.
            return None if self.scalar_calls == 1 else False

    session = FakeSession()

    assert (
        await drain_destination(
            lambda: session,  # type: ignore[arg-type]
            settings(),
            "beta.localhost",
        )
        == 0
    )
    assert session.scalar_calls == 2
    session.scalars.assert_not_awaited()


def test_group_state_generic_rejection_retries_only_during_upgrade_window() -> None:
    now = datetime.now(UTC)
    event = FederationEvent(
        event_id="kcfe_group",
        origin_domain="alpha.localhost",
        event_type="dm.group.state",
        envelope={},
    )
    row = FederationOutbox(
        destination="beta.localhost",
        event_origin_domain="alpha.localhost",
        event_id=event.event_id,
        created_at=now - timedelta(hours=1),
    )

    assert group_state_rejection_is_upgrade_retryable(event, row, "KAED_FED_EVENT_REJECTED", now)
    assert not group_state_rejection_is_upgrade_retryable(
        event, row, "KAED_FED_BAD_EVENT_SIGNATURE", now
    )
    row.created_at = now - timedelta(hours=25)
    assert not group_state_rejection_is_upgrade_retryable(
        event, row, "KAED_FED_EVENT_REJECTED", now
    )


@pytest.mark.asyncio
async def test_peer_discovery_capacity_fails_before_refresh_lock_or_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    borrowers = [object() for _ in range(4)]
    for borrower in borrowers:
        PEER_DISCOVERY_LIMITER.acquire_on_behalf_of_nowait(borrower)
    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=None),
    )
    peer_client = AsyncMock()
    monkeypatch.setattr("app.federation.network.peer_http_client", peer_client)
    try:
        with pytest.raises(FederationNetworkError, match="discovery is busy"):
            await ensure_peer(cast(Any, session), settings(), "beta.localhost", force=True)
    finally:
        for borrower in borrowers:
            PEER_DISCOVERY_LIMITER.release_on_behalf_of(borrower)

    # One lock-free cache lookup is allowed; no advisory refresh lock or peer
    # request may be attempted while the global discovery budget is exhausted.
    assert session.scalar.await_count == 1
    peer_client.assert_not_awaited()


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


def test_peer_key_history_cap_counts_only_new_immutable_ids() -> None:
    assert not peer_key_history_exceeds_limit(
        512,
        {"ed25519:known"},
        {"ed25519:known"},
        512,
    )
    assert peer_key_history_exceeds_limit(
        512,
        {"ed25519:known"},
        {"ed25519:known", "ed25519:new"},
        512,
    )


@pytest.mark.asyncio
async def test_remote_instance_admission_cap_is_global_and_bounded() -> None:
    session = cast(
        Any,
        SimpleNamespace(
            get=AsyncMock(side_effect=[None, None]),
            scalar=AsyncMock(side_effect=[None, 100]),
        ),
    )
    with pytest.raises(FederationInstanceQuotaExceeded, match="configured limit") as rejected:
        await ensure_remote_instance_record(
            session,
            settings(federation_max_remote_instances=100),
            "gamma.localhost",
        )
    assert rejected.value.detail() == {"code": "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED"}
    assert rejected.value.detail(federation=True) == {
        "code": "KAED_FED_INSTANCE_STORAGE_QUOTA_EXCEEDED"
    }


@pytest.mark.asyncio
async def test_delegated_third_party_profile_becomes_an_opaque_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = User(
        id=77,
        origin_domain="gamma.localhost",
        is_local=False,
        username=unresolved_remote_username(77, "gamma.localhost"),
        profile_resolved=False,
    )
    session = cast(
        Any,
        SimpleNamespace(
            get=AsyncMock(side_effect=[None, placeholder]),
            execute=AsyncMock(),
        ),
    )
    admit_identity = AsyncMock(return_value=(None, "beta.localhost"))
    monkeypatch.setattr(
        "app.federation.replication.admit_remote_user_identity",
        admit_identity,
    )
    claimed = RemoteUserProfile(
        id="77",
        origin_domain="gamma.localhost",
        username="spoofed_name",
        profile_version=99,
    )

    resolved = await resolve_delegated_profile(
        session,
        settings(),
        claimed,
        authority_origin="beta.localhost",
    )

    assert resolved is placeholder
    assert resolved.username.startswith("history_")
    assert resolved.username != claimed.username
    assert not resolved.profile_resolved
    admit_identity.assert_awaited_once_with(
        session,
        settings(),
        77,
        "gamma.localhost",
        introduced_by_domain="beta.localhost",
    )


@pytest.mark.asyncio
async def test_opaque_identity_retries_a_preexisting_predictable_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = 77
    origin = "gamma.localhost"
    legacy_alias = "history_" + hashlib.sha256(f"{user_id}@{origin}".encode()).hexdigest()[:20]
    aliases = iter([legacy_alias.removeprefix("history_"), "f" * 24])
    monkeypatch.setattr(
        "app.federation.replication.secrets.token_hex",
        lambda _length: next(aliases),
    )

    class FakeSession:
        def __init__(self) -> None:
            self.row: User | None = None
            self.statements: list[object] = []

        async def get(self, _model: object, identity: object) -> User | None:
            assert identity == (user_id, origin)
            return self.row

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            params = cast(Any, statement).compile().params
            # A resolved authoritative user on another composite ID already
            # owns the alias generated by the legacy deterministic scheme.
            if params["username"] != legacy_alias:
                self.row = User(
                    id=user_id,
                    origin_domain=origin,
                    is_local=False,
                    username=params["username"],
                    profile_resolved=False,
                    federation_introduced_by_domain="beta.localhost",
                )
            return object()

    session = FakeSession()
    placeholder = await insert_unresolved_remote_user(
        cast(Any, session),
        user_id=user_id,
        origin_domain=origin,
        introduced_by_domain="beta.localhost",
    )

    assert placeholder.username == f"history_{'f' * 24}"
    assert len(placeholder.username) == 32
    assert len(session.statements) == 2
    for statement in session.statements:
        sql = str(cast(Any, statement).compile())
        assert "ON CONFLICT DO NOTHING" in sql
        assert "ON CONFLICT (id, origin_domain)" not in sql


@pytest.mark.asyncio
async def test_authoritative_profile_handle_conflict_keeps_placeholder_nonfatally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = User(
        id=77,
        origin_domain="gamma.localhost",
        is_local=False,
        username="history_deadbeef",
        profile_resolved=False,
        federation_introduced_by_domain="beta.localhost",
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=88))
    monkeypatch.setattr(
        "app.federation.replication.admit_remote_user_identity",
        AsyncMock(return_value=(placeholder, "gamma.localhost")),
    )

    result = await upsert_remote_user(
        cast(Any, session),
        settings(),
        RemoteUserProfile(
            id="77",
            origin_domain="gamma.localhost",
            username="already_owned",
            display_name="Untrusted collision",
            profile_version=2,
        ),
    )

    assert result is placeholder
    assert result.username == "history_deadbeef"
    assert result.display_name is None
    assert not result.profile_resolved
    conflict_query = session.scalar.await_args.args[0]
    assert "lower(users.username)" in str(conflict_query)


@pytest.mark.asyncio
async def test_new_authoritative_profile_handle_conflict_falls_back_to_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = User(
        id=77,
        origin_domain="gamma.localhost",
        is_local=False,
        username="history_deadbeef",
        profile_resolved=False,
        federation_introduced_by_domain="gamma.localhost",
    )
    session = SimpleNamespace(
        execute=AsyncMock(),
        get=AsyncMock(return_value=None),
        scalar=AsyncMock(return_value=88),
    )
    monkeypatch.setattr(
        "app.federation.replication.admit_remote_user_identity",
        AsyncMock(return_value=(None, "gamma.localhost")),
    )
    insert_placeholder = AsyncMock(return_value=placeholder)
    monkeypatch.setattr(
        "app.federation.replication.insert_unresolved_remote_user",
        insert_placeholder,
    )

    result = await upsert_remote_user(
        cast(Any, session),
        settings(),
        RemoteUserProfile(
            id="77",
            origin_domain="gamma.localhost",
            username="already_owned",
            profile_version=2,
        ),
    )

    assert result is placeholder
    assert not result.profile_resolved
    insert_placeholder.assert_awaited_once_with(
        session,
        user_id=77,
        origin_domain="gamma.localhost",
        introduced_by_domain="gamma.localhost",
    )


@pytest.mark.asyncio
async def test_delegated_profile_cannot_spoof_a_resolved_third_party_user() -> None:
    resolved_user = User(
        id=77,
        origin_domain="gamma.localhost",
        is_local=False,
        username="real_name",
        profile_resolved=True,
    )
    session = cast(Any, SimpleNamespace(get=AsyncMock(return_value=resolved_user)))
    claimed = RemoteUserProfile(
        id="77",
        origin_domain="gamma.localhost",
        username="spoofed_name",
        profile_version=100,
    )

    with pytest.raises(ValueError, match="authoritative identity"):
        await resolve_delegated_profile(
            session,
            settings(),
            claimed,
            authority_origin="beta.localhost",
        )


@pytest.mark.parametrize(
    "metadata",
    [
        {"width": True, "height": 10},
        {"width": -1, "height": 10},
        {"width": 2**31, "height": 1},
        {"width": 20_000, "height": 20_000},
        {"width": 10, "height": None},
    ],
)
def test_remote_attachment_dimensions_are_bounded(metadata: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="dimensions"):
        remote_media_dimensions(cast(Any, metadata))


def test_remote_attachment_variants_are_fixed_and_sanitized() -> None:
    variants = sanitized_remote_variants(
        {
            "thumbnail_128": {
                "object_key": "must-not-cross-the-trust-boundary",
                "content_type": "image/webp",
                "size": 1234,
                "width": 128,
                "height": 64,
                "processing_version": 2,
            },
            "future_unrecognized_variant": {"arbitrary": "data"},
        },
        max_bytes=2048,
    )
    assert set(variants) == {"thumbnail_128"}
    assert "object_key" not in variants["thumbnail_128"]
    with pytest.raises(ValueError, match="variant size"):
        sanitized_remote_variants(
            {
                "poster": {
                    "content_type": "image/webp",
                    "size": 2049,
                    "width": 10,
                    "height": 10,
                }
            },
            max_bytes=2048,
        )


def test_remote_attachment_blurhash_is_bounded() -> None:
    assert sanitized_remote_blurhash("abcdef") == "abcdef"
    with pytest.raises(ValueError, match="blurhash"):
        sanitized_remote_blurhash("x" * 129)
    with pytest.raises(ValueError, match="blurhash"):
        sanitized_remote_blurhash("abc\ndef")


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
        "guild.emoji.create",
        "guild.emoji.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
        "guild.member.update",
        "guild.member.remove",
        "guild.members.origin.remove",
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


def test_legacy_guild_update_reference_inherits_only_a_missing_authoritative_domain() -> None:
    assert _event_ref(
        {"id": "42"},
        "guild",
        default_origin_domain="alpha.localhost",
    ) == (42, "alpha.localhost")
    assert _event_ref(
        {"id": "42", "origin_domain": "alpha.localhost"},
        "guild",
        default_origin_domain="alpha.localhost",
    ) == (42, "alpha.localhost")

    with pytest.raises(FederationNetworkError, match="invalid federation domain"):
        _event_ref(
            {"id": "42", "origin_domain": "bad/domain"},
            "guild",
            default_origin_domain="alpha.localhost",
        )
    with pytest.raises(FederationNetworkError, match="invalid federation domain"):
        _event_ref(
            {"id": "42", "origin_domain": None},
            "guild",
            default_origin_domain="alpha.localhost",
        )


@pytest.mark.asyncio
async def test_legacy_asset_update_applies_and_unblocks_the_guild_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    purge_history = AsyncMock(return_value=0)
    monkeypatch.setattr(
        "app.federation.history.purge_ineligible_federated_history",
        purge_history,
    )
    guild = SimpleNamespace(
        id=42,
        origin_domain="alpha.localhost",
        last_event_seq=26,
        next_event_seq=27,
        sync_status="stale",
        permission_generation=0,
        name="Example guild",
        description=None,
        icon_hash=None,
        banner_hash=None,
        federated_history_policy="disabled",
        history_policy_generation=0,
    )
    actor = SimpleNamespace(id=7, origin_domain="alpha.localhost")
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        get=AsyncMock(return_value=actor),
    )
    event = {
        "type": "guild.update",
        "context": {
            "guild_id": "42",
            "guild_domain": "alpha.localhost",
            "seq": "27",
        },
        "actor": {"id": "7", "domain": "alpha.localhost"},
        "content": {
            "guild": {
                "id": "42",
                "icon_hash": "a" * 64,
            }
        },
    }

    dispatch = await apply_guild_mutation_event(
        cast(Any, session),
        settings(domain="beta.localhost"),
        cast(Any, guild),
        event,
    )

    assert dispatch == (
        "GUILD_UPDATE",
        {
            "guild_id": "42",
            "guild_domain": "alpha.localhost",
            "id": "42",
            "icon_hash": "a" * 64,
        },
    )
    assert guild.icon_hash == "a" * 64
    assert guild.last_event_seq == 27
    assert guild.next_event_seq == 28
    assert guild.snapshot_generation == 2
    assert guild.sync_status == "ready"
    purge_history.assert_awaited_once()


def test_snapshot_rate_scope_is_bound_to_the_structural_revision() -> None:
    assert guild_snapshot_rate_scope(42, 7, paginated=False) == "guild-snapshot-start:42:7"
    assert guild_snapshot_rate_scope(42, 8, paginated=False) == "guild-snapshot-start:42:8"
    assert guild_snapshot_rate_scope(42, 8, paginated=True) == "guild-snapshot-page:42:8"


def test_snapshot_cursor_survives_only_message_sequence_advances() -> None:
    assert not _guild_snapshot_cursor_changed(
        current_seq=12,
        current_generation=4,
        requested_seq=9,
        requested_generation=4,
    )
    assert _guild_snapshot_cursor_changed(
        current_seq=12,
        current_generation=5,
        requested_seq=9,
        requested_generation=4,
    )
    assert _guild_snapshot_cursor_changed(
        current_seq=12,
        current_generation=4,
        requested_seq=9,
        requested_generation=None,
    )


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
        "guild.instance_access.revoked",
        "guild.leave.request",
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
async def test_peer_dns_failure_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = SimpleNamespace(getaddrinfo=AsyncMock(side_effect=socket.gaierror()))
    monkeypatch.setattr("app.federation.network.asyncio.get_running_loop", lambda: loop)

    with pytest.raises(FederationNetworkError, match="DNS resolution"):
        await public_addresses("missing.example")


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
async def test_bounded_peer_response_rejects_compression_before_decoding() -> None:
    encoded = gzip.compress(b'{"snapshot":"ok"}')

    def compressed_response(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=encoded)

    transport = httpx.MockTransport(compressed_response)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(FederationNetworkError, match="encoded response"):
            await bounded_http_request(
                client,
                "GET",
                "https://beta.example/snapshot",
                max_response_bytes=1024,
            )


def test_federation_response_json_uses_strict_protocol_decoding() -> None:
    response = httpx.Response(200, content=b'{"events":[],"cursor":"7"}')

    assert decode_federation_response_json(response) == {"events": [], "cursor": "7"}


@pytest.mark.parametrize(
    "content",
    [
        b'{"event_id":"1","event_id":"2"}',
        b'{"value":NaN}',
        b'{"value":1.5}',
        b'{"value":9007199254740992}',
    ],
)
def test_federation_response_json_rejects_ambiguous_peer_payloads(content: bytes) -> None:
    response = httpx.Response(200, content=content)

    with pytest.raises(FederationNetworkError, match="invalid federation JSON"):
        decode_federation_response_json(response)


def test_federation_response_json_enforces_its_byte_bound() -> None:
    response = httpx.Response(200, content=b"{}")

    with pytest.raises(FederationNetworkError, match="size limit"):
        decode_federation_response_json(response, max_response_bytes=1)


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
async def test_v2_request_nonce_is_validated_pinned_and_single_use() -> None:
    nonce = "abcdefghijklmnopqrstuv"
    assert federation_request_nonce({"X-Kaede-Version": "2", "X-Kaede-Nonce": nonce}) == nonce
    with pytest.raises(HTTPException) as malformed:
        federation_request_nonce({"X-Kaede-Version": "2", "X-Kaede-Nonce": "short"})
    assert malformed.value.status_code == 400

    peer = cast(Any, SimpleNamespace(capabilities=["request-nonce/1"]))
    with pytest.raises(HTTPException) as downgraded:
        require_pinned_request_nonce(peer, None)
    assert cast(dict[str, object], downgraded.value.detail)["code"] == ("KAED_FED_NONCE_REQUIRED")

    class FakeRedis:
        accepted = True

        async def set(self, *_args: object, **_kwargs: object) -> bool:
            return self.accepted

    redis = FakeRedis()
    configured = settings(federation_clock_skew_seconds=300)
    await consume_request_nonce(cast(Any, redis), configured, "beta.localhost", nonce)
    redis.accepted = False
    with pytest.raises(HTTPException) as replayed:
        await consume_request_nonce(cast(Any, redis), configured, "beta.localhost", nonce)
    assert replayed.value.status_code == 409
    assert cast(dict[str, object], replayed.value.detail)["code"] == ("KAED_FED_REPLAYED_REQUEST")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["http", "websocket"])
async def test_authenticated_origin_is_limited_before_nonce_allocation(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
) -> None:
    configured = settings()
    origin = "beta.localhost"
    key_id = "ed25519:transport"
    nonce = "abcdefghijklmnopqrstuv"
    timestamp = int(time.time())
    path = "/_kaede/v1/link" if transport == "websocket" else "/_kaede/v1/test"
    private_key = Ed25519PrivateKey.generate()
    signing_input = SigningInput(
        method="GET",
        request_target=path,
        origin=origin,
        destination=configured.domain,
        timestamp=timestamp,
        content_hash=content_sha256(b""),
        nonce=nonce,
    )
    signature = base64.b64encode(sign_request(signing_input, private_key)).decode("ascii")
    headers = {
        "Authorization": f'Kaede origin="{origin}",key="{key_id}",sig="{signature}"',
        "X-Kaede-Version": "2",
        "X-Kaede-Nonce": nonce,
        "X-Kaede-Timestamp": str(timestamp),
    }
    instance = SimpleNamespace(
        capabilities=["request-nonce/1"],
        federation_mode="open",
        last_seen_at=None,
    )
    peer_key = SimpleNamespace(
        public_key=private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        ),
        fetched_at=datetime.now(UTC),
        expired_at=None,
    )

    class FakeSession:
        async def get(self, model: object, _key: object, **_kwargs: object) -> object | None:
            if model is Instance:
                return instance
            if model is PeerKey:
                return peer_key
            return None

        async def commit(self) -> None:
            return None

    async def no_op(*_args: object, **_kwargs: object) -> None:
        return None

    async def no_block(*_args: object, **_kwargs: object) -> None:
        return None

    order: list[str] = []

    async def record_origin_limit(*_args: object, **_kwargs: object) -> None:
        order.append("origin-limit")

    async def record_nonce(*_args: object, **_kwargs: object) -> None:
        order.append("nonce")

    monkeypatch.setattr("app.federation.security.lock_block_policy_shared", no_op)
    monkeypatch.setattr("app.federation.security.matching_block", no_block)
    monkeypatch.setattr("app.federation.security.enforce_federation_source_rate_limit", no_op)
    monkeypatch.setattr("app.federation.security.enforce_origin_rate_limit", record_origin_limit)
    monkeypatch.setattr("app.federation.security.consume_request_nonce", record_nonce)
    monkeypatch.setattr("app.federation.security.federation_client_ip", lambda *_args: "192.0.2.1")
    monkeypatch.setattr(
        "app.federation.security.federation_websocket_client_ip",
        lambda *_args: "192.0.2.1",
    )

    raw_headers = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    session = cast(Any, FakeSession())
    redis = cast(Any, object())
    if transport == "http":
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "https",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": raw_headers,
                "client": ("192.0.2.1", 443),
                "server": (configured.domain, 443),
            }
        )
        request._body = b""
        await authenticate_federation(request, session, redis, configured)
    else:

        async def receive() -> dict[str, object]:
            return {"type": "websocket.disconnect"}

        async def send(_message: dict[str, object]) -> None:
            return None

        websocket = WebSocket(
            {
                "type": "websocket",
                "http_version": "1.1",
                "scheme": "wss",
                "path": path,
                "raw_path": path.encode(),
                "query_string": b"",
                "headers": raw_headers,
                "client": ("192.0.2.1", 443),
                "server": (configured.domain, 443),
                "subprotocols": [],
            },
            receive,
            send,
        )
        await authenticate_federation_websocket(websocket, session, redis, configured)

    assert order == ["origin-limit", "nonce"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"op":"ping"}', "{" + '"padding":"x",' * 1000])
async def test_hot_link_rate_limit_closes_before_parsing_any_frame(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    class FakeSession:
        async def __aenter__(self) -> object:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class FakeRedis:
        async def eval(self, script: str, _keys: int, *_args: object) -> int:
            assert script == federation_api.LINK_ADMIT_LUA
            return 1

        async def zrem(self, *_args: object) -> None:
            return None

    class FakeWebSocket:
        def __init__(self) -> None:
            self.headers = {"Sec-WebSocket-Protocol": federation_api.FEDERATION_LINK_SUBPROTOCOL}
            self.app = SimpleNamespace(
                state=SimpleNamespace(
                    sessionmaker=lambda: FakeSession(),
                    redis=FakeRedis(),
                    snowflake=object(),
                )
            )
            self.closed: list[int] = []

        async def accept(self, **_kwargs: object) -> None:
            return None

        async def send_json(self, _payload: object) -> None:
            return None

        async def receive_text(self) -> str:
            return raw

        async def close(self, *, code: int) -> None:
            self.closed.append(code)

    parse = AsyncMock()
    monkeypatch.setattr(
        federation_api,
        "authenticate_federation_websocket",
        AsyncMock(return_value=FederationPrincipal("beta.localhost", "key")),
    )
    monkeypatch.setattr(federation_api, "get_settings", lambda: settings())
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_link_frame_rate_limit",
        AsyncMock(side_effect=HTTPException(status_code=429)),
    )
    monkeypatch.setattr(federation_api, "strict_json_loads", parse)
    monkeypatch.setattr(federation_api, "heartbeat_federation_link", AsyncMock())
    websocket = FakeWebSocket()

    await federation_api.federation_link(cast(Any, websocket))

    assert websocket.closed == [4429]
    parse.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_event_key_refresh_is_bounded_and_releases_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refreshed = AsyncMock()
    monkeypatch.setattr("app.federation.security.ensure_peer", refreshed)
    released: list[tuple[object, ...]] = []

    class FakeRedis:
        async def exists(self, _key: str) -> int:
            return 0

        async def set(self, _key: str, _value: str, **kwargs: object) -> bool:
            assert kwargs == {"ex": 30, "nx": True}
            return True

        async def eval(self, _script: str, _keys: int, *args: object) -> list[int]:
            if len(args) == 5:
                return [1, 1]
            released.append(args)
            return [1]

    principal = FederationPrincipal(
        "beta.localhost",
        "ed25519:transport",
        source_ip="192.0.2.10",
    )
    session = cast(Any, object())
    configured = settings()
    assert await refresh_event_signing_keys(
        session,
        cast(Any, FakeRedis()),
        configured,
        principal,
        "ed25519:rotated",
    )
    refreshed.assert_awaited_once_with(session, configured, "beta.localhost", force=True)
    assert released


@pytest.mark.asyncio
async def test_durable_event_key_refresh_does_not_bypass_missing_source_identity() -> None:
    principal = FederationPrincipal("beta.localhost", "ed25519:transport")
    assert not await refresh_event_signing_keys(
        cast(Any, object()),
        cast(Any, object()),
        settings(),
        principal,
        "ed25519:rotated",
    )


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
    placeholder = SimpleNamespace(domain="blocked.localhost", current_key_id=None, is_self=False)

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
        profile_resolved=True,
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
async def test_guild_home_creates_only_an_opaque_unknown_third_party_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opaque = SimpleNamespace(
        id=77,
        origin_domain="gamma.localhost",
        username=unresolved_remote_username(77, "gamma.localhost"),
        display_name=None,
        avatar_hash=None,
        is_local=False,
        profile_resolved=False,
    )

    class FakeSession:
        user_lookups = 0

        async def get(self, model: object, identity: object) -> object | None:
            assert model is User
            assert identity == (77, "gamma.localhost")
            self.user_lookups += 1
            return None if self.user_lookups == 1 else opaque

        async def execute(self, _statement: object) -> object:
            return object()

    admit_identity = AsyncMock(return_value=(None, "beta.localhost"))
    monkeypatch.setattr(
        "app.federation.replication.admit_remote_user_identity",
        admit_identity,
    )

    delegated = RemoteUserProfile(
        id="77",
        origin_domain="gamma.localhost",
        username="carol",
    )
    resolved = await resolve_delegated_profile(
        cast(Any, FakeSession()),
        settings(),
        delegated,
        authority_origin="beta.localhost",
    )

    assert resolved is opaque
    assert resolved.username != delegated.username
    assert resolved.display_name is None
    assert resolved.avatar_hash is None
    admit_identity.assert_awaited_once()


@pytest.mark.asyncio
async def test_guild_snapshot_pagination_uses_a_stable_membership_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watermark = "2026-07-18T12:00:00+00:00"
    structural = {
        "snapshot_seq": "9",
        "snapshot_generation": "4",
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
        **_kwargs: object,
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
            "member_snapshot_generation": "4",
        },
    ]


@pytest.mark.asyncio
async def test_snapshot_pages_2001_members_across_intervening_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message may advance event sequence without invalidating member paging."""

    watermark = "2026-07-18T12:00:00+00:00"
    profiles = [
        {
            "user": {
                "id": str(10_000 + index),
                "origin_domain": "beta.localhost",
                "username": f"member_{index}",
            }
        }
        for index in range(2_001)
    ]
    structural = {
        # The authority can now have later message events, but every page keeps
        # the original baseline sequence and structural generation.
        "snapshot_seq": "9",
        "snapshot_generation": "4",
        "member_snapshot_at": watermark,
        "guild": {"id": "10", "origin_domain": "beta.localhost"},
        "roles": [],
        "channels": [],
        "overwrites": [],
    }
    pages = []
    for start, stop in ((0, 1000), (1000, 2000), (2000, 2001)):
        page_members = profiles[start:stop]
        pages.append(
            {
                **structural,
                "members": page_members,
                "member_roles": [],
                "next_member_cursor": (
                    {
                        "user_domain": "beta.localhost",
                        "user_id": page_members[-1]["user"]["id"],
                    }
                    if stop < len(profiles)
                    else None
                ),
            }
        )
    queries: list[dict[str, str]] = []

    async def fake_signed_request(
        *_args: object,
        query: dict[str, str],
        **_kwargs: object,
    ) -> httpx.Response:
        queries.append(query)
        return httpx.Response(200, json=pages[len(queries) - 1])

    monkeypatch.setattr("app.federation.guilds.signed_request", fake_signed_request)
    snapshot = await fetch_guild_snapshot(cast(Any, object()), settings(), "beta.localhost", 10)

    assert len(snapshot["members"]) == 2_001
    assert len(queries) == 3
    assert queries[1]["member_snapshot_seq"] == "9"
    assert queries[1]["member_snapshot_generation"] == "4"
    assert queries[2]["member_snapshot_generation"] == "4"


@pytest.mark.asyncio
async def test_snapshot_pagination_rejects_structural_generation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watermark = "2026-07-18T12:00:00+00:00"
    structural = {
        "snapshot_seq": "9",
        "snapshot_generation": "4",
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
            "snapshot_generation": "5",
            "members": [],
            "member_roles": [],
            "next_member_cursor": None,
        },
    ]
    calls = 0

    async def fake_signed_request(*_args: object, **_kwargs: object) -> httpx.Response:
        nonlocal calls
        response = httpx.Response(200, json=pages[calls])
        calls += 1
        return response

    monkeypatch.setattr("app.federation.guilds.signed_request", fake_signed_request)
    with pytest.raises(RuntimeError, match="structural generation changed"):
        await fetch_guild_snapshot(cast(Any, object()), settings(), "beta.localhost", 10)


@pytest.mark.asyncio
async def test_snapshot_visibility_for_2001_members_has_bounded_query_count() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        owner_id=1,
        owner_domain="beta.localhost",
    )
    role = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        permissions=int(1 << 10),  # VIEW_CHANNEL
    )
    channel = SimpleNamespace(
        id=20,
        origin_domain="alpha.localhost",
        type=0,
        parent_id=None,
        parent_domain=None,
        permissions_synced=False,
    )
    members = [
        SimpleNamespace(
            user_id=index + 1,
            user_domain="beta.localhost",
            timeout_indefinite=False,
            timeout_until=None,
        )
        for index in range(2_001)
    ]

    class BulkSession:
        def __init__(self) -> None:
            self.calls = 0
            self.rows = [members, [], []]

        async def scalars(self, _statement: object) -> list[object]:
            rows = self.rows[self.calls]
            self.calls += 1
            return rows

    session = BulkSession()
    visible = await visible_guild_channels_for_origin(
        cast(Any, session),
        cast(Any, guild),
        "beta.localhost",
        loaded_roles=[cast(Any, role)],
        loaded_channels=[cast(Any, channel)],
    )

    assert visible == [channel]
    assert session.calls == 3


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
    monkeypatch.setattr(
        "app.federation.guilds.admit_replica_storage",
        AsyncMock(),
    )

    assert await synchronize_guild(cast(Any, object()), settings(), cast(Any, guild)) == []
    assert guild.sync_status == "ready"
    assert not guild.unavailable


@pytest.mark.asyncio
async def test_semantic_poison_gap_page_is_quarantined_and_snapshot_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="beta.localhost",
        last_event_seq=5,
        sync_status="stale",
        sync_error_code=None,
        sync_error=None,
        unavailable=False,
    )
    session = SimpleNamespace(
        rollback=AsyncMock(),
        get=AsyncMock(return_value=guild),
        commit=AsyncMock(),
    )
    response = httpx.Response(
        200,
        json={"events": [{}] * 1_001, "latest_seq": "1006"},
    )
    fetch_snapshot = AsyncMock(return_value={"snapshot_seq": "1006"})
    apply_snapshot = AsyncMock()
    monkeypatch.setattr(
        "app.federation.guilds.signed_request",
        AsyncMock(return_value=response),
    )
    monkeypatch.setattr("app.federation.guilds.fetch_guild_snapshot", fetch_snapshot)
    monkeypatch.setattr("app.federation.guilds.apply_guild_snapshot", apply_snapshot)

    assert await synchronize_guild(cast(Any, session), settings(), cast(Any, guild)) == []

    session.rollback.assert_awaited_once()
    session.commit.assert_awaited_once()
    fetch_snapshot.assert_awaited_once()
    apply_snapshot.assert_awaited_once()
    assert guild.sync_status == "failed"
    assert guild.unavailable is True


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


@pytest.mark.asyncio
async def test_exact_profile_endpoint_signs_the_requested_local_composite_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings(domain="alpha.localhost")
    user = User(
        id=42,
        origin_domain="alpha.localhost",
        username="alice",
        is_local=True,
        profile_resolved=True,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=user))
    redis = SimpleNamespace()
    rate_limit = AsyncMock()
    envelope = AsyncMock(return_value={"origin": "alpha.localhost", "type": "user.profile"})
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(federation_api, "build_envelope", envelope)

    result = await federation_user_profile_by_ref(
        user_id=42,
        user_domain="alpha.localhost",
        principal=FederationPrincipal("beta.localhost", "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, redis),
        settings=configured,
    )

    assert result["type"] == "user.profile"
    content = envelope.await_args.args[4]
    assert content["subject"] == {"id": "42", "origin_domain": "alpha.localhost"}
    assert content["profile"]["id"] == "42"
    assert content["profile"]["origin_domain"] == "alpha.localhost"
    rate_limit.assert_awaited_once()


@pytest.mark.asyncio
async def test_exact_profile_endpoint_does_not_answer_for_another_home() -> None:
    with pytest.raises(HTTPException) as error:
        await federation_user_profile_by_ref(
            user_id=42,
            user_domain="remote.example",
            principal=FederationPrincipal("beta.localhost", "ed25519:test"),
            session=cast(Any, SimpleNamespace()),
            redis=cast(Any, SimpleNamespace()),
            settings=settings(domain="alpha.localhost"),
        )
    assert error.value.status_code == 404


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
                "member_version": "1",
            }
        ],
        "member_roles": [],
        "overwrites": [],
        "emojis": [
            {
                "id": "13",
                "origin_domain": "beta.localhost",
                "guild_id": "10",
                "guild_domain": "beta.localhost",
                "name": "lantern",
                "animated": False,
                "media_hash": "a" * 64,
            }
        ],
    }
    validate_guild_snapshot(snapshot, expected_origin="beta.localhost", expected_guild_id=10)
    with pytest.raises(ValueError, match="joining local member"):
        validate_guild_snapshot(
            snapshot,
            expected_origin="beta.localhost",
            expected_guild_id=10,
            required_member=(42, "alpha.localhost"),
        )
    snapshot["members"].append(
        {
            "user": {
                "id": "42",
                "origin_domain": "alpha.localhost",
                "username": "alice",
            },
            "nickname": None,
            "joined_at": "2026-07-17T00:00:00+00:00",
            "timeout_until": None,
            "voice_flags": 0,
            "member_version": "1",
        }
    )
    validate_guild_snapshot(
        snapshot,
        expected_origin="beta.localhost",
        expected_guild_id=10,
        required_member=(42, "alpha.localhost"),
    )
    snapshot["channels"][0]["origin_domain"] = "evil.example"
    with pytest.raises(ValueError, match="channel identity"):
        validate_guild_snapshot(snapshot, expected_origin="beta.localhost", expected_guild_id=10)


def test_guild_snapshot_rejects_invalid_custom_emoji_identity() -> None:
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
        "roles": [],
        "channels": [],
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
        "emojis": [
            {
                "id": "13",
                "origin_domain": "evil.example",
                "guild_id": "10",
                "guild_domain": "beta.localhost",
                "name": "lantern",
                "animated": False,
                "media_hash": "a" * 64,
            }
        ],
    }
    with pytest.raises(ValueError, match="emoji identity"):
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


def test_guild_snapshot_does_not_export_private_moderation_state() -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        next_event_seq=2,
        name="Private moderation",
        description=None,
        icon_hash=None,
        banner_hash=None,
        owner_id=11,
        owner_domain="alpha.localhost",
        permission_generation=1,
        federated_history_policy="disabled",
        history_policy_generation=1,
    )
    member = SimpleNamespace(
        user_id=11,
        user_domain="alpha.localhost",
        nickname=None,
        joined_at=datetime(2026, 7, 18, tzinfo=UTC),
        timeout_until=datetime(2026, 7, 19, tzinfo=UTC),
        timeout_indefinite=False,
        timeout_reason="private moderator note",
        voice_flags=3,
        member_version=2,
    )
    user = SimpleNamespace(
        id=11,
        origin_domain="alpha.localhost",
        username="alice",
        display_name=None,
        avatar_hash=None,
    )

    payload = guild_snapshot_payload(
        cast(Any, guild),
        [],
        [],
        [(cast(Any, member), cast(Any, user))],
        [],
        [],
        member_snapshot_at=datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )

    projected_member = payload["members"][0]
    assert "timeout_reason" not in projected_member
    assert "voice_flags" not in projected_member
