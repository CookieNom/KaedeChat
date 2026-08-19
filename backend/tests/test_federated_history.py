import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.chat.guild_revision import federation_channel_state
from app.chat.schemas import ChannelUpdate, GuildUpdate
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.db.models import Channel, Guild
from app.federation import history as history_module
from app.federation.history import (
    HISTORY_CAPACITY_CODE,
    HISTORY_EVENT_TYPES,
    HISTORY_LIMIT_REACHED_CODE,
    HISTORY_TEMPORARILY_UNAVAILABLE_CODE,
    FederatedHistoryLimitExceeded,
    HistoryDeltaAdvanced,
    _ensure_history_identity,
    _merge_history_import_batch,
    _reconcile_history_delta,
    _validate_history_message,
    cleanup_history_transfers,
    effective_history_policy,
    ensure_history_export_capacity,
    history_response_error,
    lock_history_export_capacity,
    unresolved_history_username,
)


def snowflake_at(value: datetime, sequence: int = 0) -> int:
    timestamp = int(value.timestamp() * 1000) - EPOCH_MS
    return (timestamp << (WORKER_BITS + SEQUENCE_BITS)) | sequence


def history_settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "domain": "replica.example",
        "federation_clock_skew_seconds": 300,
        "federation_history_page_bytes": 512 * 1024,
        "federation_history_max_pages": 100,
        "federation_history_max_messages": 1_000,
        "federation_history_max_bytes": 8 * 1024 * 1024,
        "federation_history_max_reactions": 1_000,
        "federation_history_merge_chunk_size": 100,
        "federation_history_max_active_exports_per_origin": 1_000,
        "federation_history_max_active_exports_total": 10_000,
        "federation_history_max_active_channel_grants_per_origin": 100_000,
        "federation_history_max_active_channel_grants_total": 1_000_000,
        "media_max_attachment_bytes": 15 * 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_history_cleanup_reports_expired_and_abandoned_rows_without_committing() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(rowcount=3),
        SimpleNamespace(rowcount=4),
        SimpleNamespace(rowcount=2),
    ]

    assert await cleanup_history_transfers(
        session,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    ) == {
        "history_exports": 3,
        "history_imports": 2,
        "history_staged_messages": 4,
    }
    assert session.execute.await_count == 3
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_export_capacity_uses_global_then_origin_locks() -> None:
    session = AsyncMock()
    session.scalar.side_effect = [None, None]

    await lock_history_export_capacity(session, "remote.example")

    statements = [
        str(
            call.args[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for call in session.scalar.await_args_list
    ]
    assert "kaede-history-export-global" in statements[0]
    assert "kaede-history-export-origin:remote.example" in statements[1]


@pytest.mark.asyncio
async def test_history_export_capacity_bounds_per_origin_channel_multiplication() -> None:
    session = AsyncMock()
    # total exports, origin exports, total channel grants, origin channel grants
    session.scalar.side_effect = [10, 2, 150, 95]
    configured = history_settings(
        federation_history_max_active_exports_per_origin=10,
        federation_history_max_active_exports_total=100,
        federation_history_max_active_channel_grants_per_origin=100,
        federation_history_max_active_channel_grants_total=1_000,
    )

    with pytest.raises(HTTPException) as raised:
        await ensure_history_export_capacity(
            session,
            configured,  # type: ignore[arg-type]
            "remote.example",
            6,
            datetime.now(UTC),
        )

    assert raised.value.status_code == 429
    assert raised.value.detail == {
        "code": "KAED_FED_HISTORY_CAPACITY",
        "retry_after_ms": 60_000,
    }


def guild(policy: str = "disabled") -> Guild:
    return Guild(
        id=10,
        origin_domain="home.example",
        name="History guild",
        owner_id=20,
        owner_domain="home.example",
        federated_history_policy=policy,
        history_policy_generation=1,
    )


def channel(policy: str = "inherit") -> Channel:
    return Channel(
        id=30,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=0,
        name="general",
        created_floor_id=30,
        federated_history_policy=policy,
    )


def test_history_policy_defaults_to_disabled_and_supports_channel_overrides() -> None:
    parent = guild()
    child = channel()
    assert effective_history_policy(parent, child) == "disabled"
    parent.federated_history_policy = "full_retained"
    assert effective_history_policy(parent, child) == "full_retained"
    child.federated_history_policy = "disabled"
    assert effective_history_policy(parent, child) == "disabled"


def test_new_channel_federation_state_materializes_the_pre_flush_policy_default() -> None:
    new_channel = Channel(
        id=31,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=0,
        name="new-channel",
        position=1,
        rate_limit_per_user=0,
        created_floor_id=31,
    )

    assert federation_channel_state(new_channel)["federated_history_policy"] == "inherit"


def test_history_policy_schemas_reject_unknown_and_explicit_null_values() -> None:
    assert GuildUpdate(federated_history_policy="full_retained").federated_history_policy == (
        "full_retained"
    )
    assert ChannelUpdate(federated_history_policy="inherit").federated_history_policy == "inherit"
    with pytest.raises(ValidationError):
        GuildUpdate(federated_history_policy=None)
    with pytest.raises(ValidationError):
        ChannelUpdate(federated_history_policy="public")  # type: ignore[arg-type]


def test_history_placeholder_handles_are_random_bounded_and_do_not_expose_the_id() -> None:
    first = unresolved_history_username(1234, "remote.example")
    second = unresolved_history_username(1234, "remote.example")
    assert first != second
    assert "1234" not in first
    assert first.startswith("history_")
    assert len(first) == 32
    assert first.removeprefix("history_").isalnum()


def test_history_delta_registry_contains_only_mutations_needed_for_reconciliation() -> None:
    assert {
        "guild.message.update",
        "guild.message.delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.pin.add",
        "guild.pin.remove",
    } == HISTORY_EVENT_TYPES


def test_history_message_validation_binds_author_channel_and_range() -> None:
    created_at = datetime.now(UTC)
    message_snowflake = snowflake_at(created_at, 10)
    raw = {
        "id": str(message_snowflake),
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": "retained message",
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": created_at.isoformat(),
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }
    message_id, validated = _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=message_snowflake,
    )
    assert message_id == message_snowflake
    assert validated["content"] == "retained message"

    with pytest.raises(ValueError, match="outside its granted range"):
        _validate_history_message(
            {**raw, "id": str(message_snowflake + 1)},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_snowflake,
        )
    with pytest.raises(ValueError, match="profile does not match"):
        _validate_history_message(
            {**raw, "author_id": "21"},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_snowflake,
        )

    with pytest.raises(ValueError, match="snowflake timestamp does not match"):
        _validate_history_message(
            {**raw, "created_at": datetime(2026, 1, 1, tzinfo=UTC).isoformat()},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_snowflake,
        )


def test_encrypted_history_operation_matches_the_message_projection() -> None:
    created_at = datetime.now(UTC)
    message_id = snowflake_at(created_at, 12)
    raw = {
        "id": str(message_id),
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": None,
        "e2ee": {
            "version": 2,
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "policy_generation": "1",
            "epoch": "1",
            "sender_device_id": "ked_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "operation": "create",
            "ciphertext": "AQ",
        },
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": created_at.isoformat(),
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }
    _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=message_id,
    )

    message_ref = f"{message_id}@home.example"
    edited = {
        **raw,
        "edited_at": (created_at + timedelta(microseconds=1)).isoformat(),
        "e2ee": {**raw["e2ee"], "operation": "edit", "target_message": message_ref},
    }
    _validate_history_message(
        edited,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=message_id,
    )

    invalid = [
        {**raw, "e2ee": {**raw["e2ee"], "target_message": None}},
        {**raw, "e2ee": {**raw["e2ee"], "operation": "welcome"}},
        {**edited, "e2ee": raw["e2ee"]},
        {
            **edited,
            "e2ee": {
                **edited["e2ee"],
                "target_message": f"{message_id + 1}@home.example",
            },
        },
    ]
    for candidate in invalid:
        with pytest.raises(ValueError, match="MLS (create|edit)"):
            _validate_history_message(
                candidate,
                guild_origin="home.example",
                channel_id=30,
                after=0,
                upper_bound=message_id,
            )


def test_history_attachment_metadata_uses_bounded_remote_media_shape() -> None:
    created_at = datetime.now(UTC)
    message_id = snowflake_at(created_at, 10)
    attachment_id = snowflake_at(created_at, 11)
    attachment = {
        "id": str(attachment_id),
        "origin_domain": "home.example",
        "filename": "image.png",
        "content_type": "image/png",
        "size": 1024,
        "width": 20,
        "height": 10,
        "blurhash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        "variants": {
            "thumbnail_128": {
                "content_type": "image/webp",
                "size": 512,
                "width": 20,
                "height": 10,
            },
            "unregistered": {"arbitrary": "metadata"},
        },
    }
    raw = {
        "id": str(message_id),
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": "image",
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [attachment],
        "reactions": [],
        "pin": None,
        "created_at": created_at.isoformat(),
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }

    _parsed, validated = _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=message_id,
    )
    assert set(validated["attachments"][0]["variants"]) == {"thumbnail_128"}

    with pytest.raises(ValueError, match="dimensions"):
        _validate_history_message(
            {**raw, "attachments": [{**attachment, "width": True}]},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_id,
        )
    with pytest.raises(ValueError, match="dimensions"):
        _validate_history_message(
            {**raw, "attachments": [{**attachment, "width": 100_000, "height": 100_000}]},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_id,
        )
    with pytest.raises(ValueError, match="blurhash"):
        _validate_history_message(
            {**raw, "attachments": [{**attachment, "blurhash": "x" * 129}]},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=message_id,
        )


def test_recent_first_history_validation_requires_strict_descending_pages() -> None:
    created_at = datetime.now(UTC)
    newest_id = snowflake_at(created_at, 100)
    older_id = snowflake_at(created_at, 50)
    upper_bound = snowflake_at(created_at, 200)
    raw = {
        "id": str(newest_id),
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": "newest retained message",
        "e2ee": None,
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": created_at.isoformat(),
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }
    message_id, _validated = _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=upper_bound,
        before=upper_bound,
    )
    assert message_id == newest_id
    next_id, _validated = _validate_history_message(
        {**raw, "id": str(older_id)},
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=upper_bound,
        before=upper_bound,
        previous_id=newest_id,
    )
    assert next_id == older_id
    with pytest.raises(ValueError, match="outside its granted range"):
        _validate_history_message(
            {**raw, "id": str(newest_id + 1)},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=upper_bound,
            before=upper_bound,
            previous_id=newest_id,
        )


@pytest.mark.asyncio
async def test_history_identity_never_materializes_an_unknown_third_party() -> None:
    session = AsyncMock()
    session.get.return_value = None

    with pytest.raises(ValueError, match="unresolved third-party identity"):
        await _ensure_history_identity(
            session,
            history_settings(),  # type: ignore[arg-type]
            42,
            "third.example",
            authority_origin="guild-home.example",
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_identity_does_not_fallback_after_profile_resolution_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.get.return_value = None
    resolver = AsyncMock(side_effect=ValueError("profile is not authoritative"))
    monkeypatch.setattr(history_module, "resolve_delegated_profile", resolver)
    profile = history_module.RemoteUserProfile.model_validate(
        {
            "id": "42",
            "origin_domain": "third.example",
            "username": "member",
            "profile_version": 1,
        }
    )

    with pytest.raises(ValueError, match="not authoritative"):
        await _ensure_history_identity(
            session,
            history_settings(),  # type: ignore[arg-type]
            42,
            "third.example",
            profile=profile,
            authority_origin="guild-home.example",
        )

    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_delta_rejects_an_expired_deadline_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock()
    monkeypatch.setattr(history_module, "signed_request", request)
    history_import = SimpleNamespace(
        export_id=7,
        export_domain="home.example",
        pages_downloaded=0,
        messages_downloaded=0,
        bytes_downloaded=0,
        reactions_downloaded=0,
    )

    session = AsyncMock()
    session.scalar.return_value = 0
    with pytest.raises(FederatedHistoryLimitExceeded) as raised:
        await _reconcile_history_delta(
            session,
            history_settings(),  # type: ignore[arg-type]
            SimpleNamespace(id=1, origin_domain="home.example"),  # type: ignore[arg-type]
            history_import,  # type: ignore[arg-type]
            10,
            deadline=time.monotonic() - 1,
        )
    assert raised.value.code == HISTORY_LIMIT_REACHED_CODE
    assert raised.value.resource == "duration"
    assert not raised.value.retryable

    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_delta_rejects_nonadvancing_cursor_and_budget_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "events": [],
                "cursor_seq": "10",
                "latest_seq": "11",
                "complete": False,
            },
        )
    )
    monkeypatch.setattr(history_module, "signed_request", request)
    monkeypatch.setattr(
        history_module,
        "_lock_live_history_import",
        AsyncMock(side_effect=lambda _session, _settings, guild, item: (guild, item)),
    )
    session = AsyncMock()
    session.scalar.return_value = 0
    history_import = SimpleNamespace(
        export_id=7,
        export_domain="home.example",
        pages_downloaded=0,
        messages_downloaded=0,
        bytes_downloaded=0,
        reactions_downloaded=0,
    )
    with pytest.raises(ValueError, match="did not advance"):
        await _reconcile_history_delta(
            session,
            history_settings(),  # type: ignore[arg-type]
            SimpleNamespace(id=1, origin_domain="home.example"),  # type: ignore[arg-type]
            history_import,  # type: ignore[arg-type]
            10,
            deadline=time.monotonic() + 30,
        )

    request.return_value = httpx.Response(
        200,
        json={
            "events": [{"type": "guild.message.update"}],
            "cursor_seq": "11",
            "latest_seq": "11",
            "complete": True,
        },
    )
    history_import.messages_downloaded = 1
    with pytest.raises(FederatedHistoryLimitExceeded) as raised:
        await _reconcile_history_delta(
            session,
            history_settings(federation_history_max_messages=1),  # type: ignore[arg-type]
            SimpleNamespace(id=1, origin_domain="home.example"),  # type: ignore[arg-type]
            history_import,  # type: ignore[arg-type]
            10,
            deadline=time.monotonic() + 30,
        )
    assert raised.value.resource == "messages"


@pytest.mark.asyncio
async def test_history_delta_request_cap_is_persistent_across_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock()
    monkeypatch.setattr(history_module, "signed_request", request)
    session = AsyncMock()
    # No channel-page work means all persisted pages were delta requests.
    session.scalar.return_value = 0
    history_import = SimpleNamespace(
        export_id=7,
        export_domain="home.example",
        pages_downloaded=history_module.HISTORY_DELTA_MAX_REQUESTS,
        messages_downloaded=0,
        bytes_downloaded=0,
        reactions_downloaded=0,
    )

    with pytest.raises(FederatedHistoryLimitExceeded) as raised:
        await _reconcile_history_delta(
            session,
            history_settings(
                federation_history_max_pages=history_module.HISTORY_DELTA_MAX_REQUESTS + 100
            ),  # type: ignore[arg-type]
            SimpleNamespace(id=1, origin_domain="home.example"),  # type: ignore[arg-type]
            history_import,  # type: ignore[arg-type]
            10,
            deadline=time.monotonic() + 30,
        )
    assert raised.value.resource == "delta_requests"

    request.assert_not_awaited()


def test_history_response_error_preserves_safe_capacity_retry_after() -> None:
    response = httpx.Response(
        429,
        headers={"Retry-After": "60"},
        json={
            "detail": {
                "code": "KAED_FED_HISTORY_CAPACITY",
                "retry_after_ms": 75_000,
                "message": "untrusted authority diagnostic",
            }
        },
    )

    error = history_response_error(response)

    assert error.code == HISTORY_CAPACITY_CODE
    assert error.retryable
    assert error.retry_after_ms == 75_000
    assert error.dispatch_payload() == {
        "code": HISTORY_CAPACITY_CODE,
        "retryable": True,
        "retry_after_ms": 75_000,
    }
    assert "untrusted" not in str(error)


def test_history_response_error_classifies_transient_and_terminal_responses() -> None:
    transient = history_response_error(
        httpx.Response(503, headers={"Retry-After": "2"}, content=b"busy")
    )
    terminal = history_response_error(httpx.Response(403, content=b"private details"))

    assert transient.code == HISTORY_TEMPORARILY_UNAVAILABLE_CODE
    assert transient.retryable
    assert transient.retry_after_ms == 2_000
    assert not terminal.retryable
    assert "private details" not in str(terminal)


@pytest.mark.asyncio
async def test_history_merge_batch_fences_live_events_after_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AsyncMock()
    session.scalar.return_value = SimpleNamespace(last_event_seq=12)
    monkeypatch.setattr(
        history_module,
        "_lock_live_history_import",
        AsyncMock(side_effect=lambda _session, _settings, guild, item: (guild, item)),
    )

    with pytest.raises(HistoryDeltaAdvanced) as raised:
        await _merge_history_import_batch(
            session,
            history_settings(),  # type: ignore[arg-type]
            SimpleNamespace(id=1, origin_domain="home.example"),  # type: ignore[arg-type]
            SimpleNamespace(export_id=7, export_domain="home.example"),  # type: ignore[arg-type]
            reconciled_seq=11,
            tombstone_delivery_wakes=set(),
        )

    assert raised.value.required_seq == 12
