from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import channels as channels_api
from app.api import federation as federation_api
from app.chat.forwarding import can_forward_between_age_contexts, validate_forward_snapshot
from app.chat.payloads import message_payload, render_poll_payload
from app.chat.schemas import MessageCreate, MessageForwardCreate
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Attachment, Channel, Message, Poll, PollAnswer
from app.federation import events as federation_events
from app.federation.guilds import _validated_message_rich_projection
from app.federation.message_content import (
    validate_replicated_rich_projection,
    validate_webhook_attribution,
)
from app.federation.replication import replicated_message_create_fingerprint
from app.federation.schemas import (
    GuildPollFinalizeProxyRequest,
    GuildPollVoteProxyRequest,
    GuildProxyRequest,
)
from app.federation.security import FederationPrincipal


def remote_actor() -> dict[str, object]:
    return {
        "id": "10",
        "origin_domain": "member.example",
        "username": "member",
        "profile_version": 1,
    }


def proxy_fingerprint_request(**changes: object) -> GuildProxyRequest:
    raw: dict[str, object] = {
        "operation": "message.create",
        "actor": remote_actor(),
        "channel_id": "20",
        "content": "hello <@50@guild.example>",
        "client_nonce": "stable-proxy-replay",
        "allowed_mentions": {"users": ["50@guild.example"]},
        "mention_user_ids": ["50@guild.example"],
        "attachments": [
            {
                "id": "70",
                "origin_domain": "member.example",
                "filename": "hello.txt",
                "content_type": "text/plain",
                "size": 5,
                "encryption_mode": "plaintext",
                "content_sha256": "a" * 64,
            }
        ],
    }
    raw.update(changes)
    return GuildProxyRequest.model_validate(raw)


def test_proxy_request_fingerprint_binds_semantics_not_refreshable_receipts() -> None:
    payload = proxy_fingerprint_request()
    refreshed_profile = proxy_fingerprint_request(
        actor={
            **remote_actor(),
            "username": "renamed",
            "display_name": "Renamed Member",
            "profile_version": 2,
        }
    ).model_copy(
        update={
            "expression_authorizations": {"refreshed": {"signature": "new"}},
            "forward_source_proof": {"signature": "new"},
        }
    )
    refreshed_hash = proxy_fingerprint_request(
        attachments=[
            {
                **payload.attachments[0],
                "content_sha256": "b" * 64,
                "scan_status": "pending",
            }
        ]
    )
    refreshed_processing = proxy_fingerprint_request(
        attachments=[
            {
                **payload.attachments[0],
                "content_type": "application/octet-stream",
                "width": 100,
                "height": 40,
                "blurhash": "processing-result",
                "scan_status": "clean",
                "variants": {"thumbnail_128": {"size": 100}},
            }
        ]
    )

    expected = federation_api.proxy_request_fingerprint(payload, "guild.example")
    assert federation_api.proxy_request_fingerprint(refreshed_profile, "guild.example") == expected
    assert (
        federation_api.proxy_request_fingerprint(refreshed_processing, "guild.example") == expected
    )
    assert federation_api.proxy_request_fingerprint(refreshed_hash, "guild.example") != expected

    assert (
        federation_api.proxy_request_fingerprint(
            proxy_fingerprint_request(content="changed"),
            "guild.example",
        )
        != expected
    )
    assert (
        federation_api.proxy_request_fingerprint(
            proxy_fingerprint_request(allowed_mentions={"parse": ["users"]}),
            "guild.example",
        )
        != expected
    )
    assert (
        federation_api.proxy_request_fingerprint(
            proxy_fingerprint_request(
                attachments=[{**payload.attachments[0], "filename": "changed.txt"}]
            ),
            "guild.example",
        )
        != expected
    )


def test_proxy_request_fingerprint_preserves_policy_presence_and_normalizes_refs() -> None:
    implicit = GuildProxyRequest.model_validate(
        proxy_fingerprint_request().model_dump(mode="json", exclude={"allowed_mentions"})
    )
    explicit_null = GuildProxyRequest.model_validate(
        {
            **implicit.model_dump(mode="json", exclude={"allowed_mentions"}),
            "allowed_mentions": None,
        }
    )
    bare_ref = proxy_fingerprint_request(allowed_mentions={"users": ["50"]})
    qualified_ref = proxy_fingerprint_request(allowed_mentions={"users": ["50@guild.example"]})

    assert "allowed_mentions" not in implicit.model_fields_set
    assert "allowed_mentions" in explicit_null.model_fields_set
    assert federation_api.proxy_request_fingerprint(
        implicit, "guild.example"
    ) != federation_api.proxy_request_fingerprint(explicit_null, "guild.example")
    assert federation_api.proxy_request_fingerprint(
        bare_ref, "guild.example"
    ) == federation_api.proxy_request_fingerprint(qualified_ref, "guild.example")


@pytest.mark.asyncio
async def test_locked_proxy_nonce_replay_uses_stored_immutable_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = proxy_fingerprint_request()
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    receipt = federation_api.proxy_request_fingerprint_receipt(payload, "guild.example")
    message = SimpleNamespace(
        id=80,
        origin_domain="guild.example",
        proxy_request_fingerprint_version=receipt.version,
        proxy_request_fingerprint=receipt.sha256,
        proxy_commit_seq=9,
    )
    event = SimpleNamespace(
        guild_id=60,
        guild_domain="guild.example",
        seq=9,
        envelope={
            "type": "guild.message.committed",
            "context": {
                "guild_id": "60",
                "guild_domain": "guild.example",
                "seq": "9",
            },
            "content": {
                "message": {"id": "80", "origin_domain": "guild.example"},
                "proxy_request_fingerprint": receipt.wire(),
            },
        },
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=message),
        get=AsyncMock(return_value=event),
    )
    lock = AsyncMock()
    monkeypatch.setattr(federation_api, "lock_proxy_nonce", lock)

    replay = await federation_api.locked_proxy_nonce_replay(
        cast(Any, session),
        cast(Any, guild),
        cast(Any, channel),
        cast(Any, actor),
        payload,
    )
    assert replay is not None
    assert replay.message is message
    assert replay.event is event
    lock.assert_awaited_once()

    with pytest.raises(federation_api.ProxyNonceStateConflict):
        await federation_api.locked_proxy_nonce_replay(
            cast(Any, session),
            cast(Any, guild),
            cast(Any, channel),
            cast(Any, actor),
            proxy_fingerprint_request(content="different"),
        )

    message.proxy_request_fingerprint_version = None
    message.proxy_request_fingerprint = None
    assert (
        await federation_api.locked_proxy_nonce_replay(
            cast(Any, session),
            cast(Any, guild),
            cast(Any, channel),
            cast(Any, actor),
            payload,
        )
        is None
    )


@pytest.mark.asyncio
async def test_proxy_nonce_replay_rejects_unknown_durable_fingerprint_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = proxy_fingerprint_request()
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    message = SimpleNamespace(
        id=80,
        origin_domain="guild.example",
        proxy_request_fingerprint_version=2,
        proxy_request_fingerprint="a" * 64,
        proxy_commit_seq=9,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=message),
        get=AsyncMock(),
    )
    monkeypatch.setattr(federation_api, "lock_proxy_nonce", AsyncMock())

    with pytest.raises(federation_api.UnsupportedProxyFingerprintVersion):
        await federation_api.locked_proxy_nonce_replay(
            cast(Any, session),
            cast(Any, guild),
            cast(Any, channel),
            cast(Any, actor),
            payload,
        )
    session.get.assert_not_awaited()

    with pytest.raises(federation_api.UnsupportedProxyFingerprintVersion):
        federation_api.proxy_request_fingerprint(payload, "guild.example", version=2)
    with pytest.raises(federation_api.UnsupportedProxyFingerprintVersion):
        federation_api.proxy_request_fingerprint(payload, "guild.example", version=True)


@pytest.mark.asyncio
async def test_queue_event_explicit_replay_revives_retained_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = SimpleNamespace(
        status="delivered",
        attempts=7,
        next_retry_at=datetime.now(UTC) + timedelta(days=1),
        last_error="old",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, outbox]),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(federation_events, "matching_block", AsyncMock(return_value=None))
    monkeypatch.setattr(federation_events, "ensure_queue_destination", AsyncMock())
    monkeypatch.setattr(federation_events, "record_attachment_recipients", AsyncMock())

    await federation_events.queue_event(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "member.example",
        {
            "event_id": "kcge_replay",
            "origin": "guild.example",
            "type": "guild.message.committed",
            "content": {},
            "context": {},
        },
        requeue_existing=True,
    )

    assert outbox.status == "pending"
    assert outbox.attempts == 0
    assert outbox.last_error is None
    assert outbox.next_retry_at <= datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "retry", "circuit"])
async def test_queue_event_explicit_replay_preserves_active_delivery_backoff(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    retry_at = datetime.now(UTC) + timedelta(days=1)
    outbox = SimpleNamespace(
        status=status,
        attempts=7,
        next_retry_at=retry_at,
        last_error="active",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, outbox]),
        execute=AsyncMock(),
    )
    monkeypatch.setattr(federation_events, "matching_block", AsyncMock(return_value=None))
    monkeypatch.setattr(federation_events, "ensure_queue_destination", AsyncMock())
    monkeypatch.setattr(federation_events, "record_attachment_recipients", AsyncMock())

    await federation_events.queue_event(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "member.example",
        {
            "event_id": "kcge_active_replay",
            "origin": "guild.example",
            "type": "guild.message.committed",
            "content": {},
            "context": {},
        },
        requeue_existing=True,
    )

    assert (outbox.status, outbox.attempts, outbox.next_retry_at, outbox.last_error) == (
        status,
        7,
        retry_at,
        "active",
    )


@pytest.mark.asyncio
async def test_direct_proxy_exact_replay_survives_history_prune_and_mutable_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = proxy_fingerprint_request(attachments=[])
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        guild_id=60,
        unavailable=False,
        type=0,
    )
    receipt = federation_api.proxy_request_fingerprint_receipt(payload, "guild.example")
    committed = {
        "event_id": "kcge_retained",
        "type": "guild.message.committed",
        "context": {
            "guild_id": "60",
            "guild_domain": "guild.example",
            "seq": "9",
        },
        "content": {
            "message": {
                "id": "80",
                "origin_domain": "guild.example",
                "content": payload.content,
            },
            "proxy_request_fingerprint": receipt.wire(),
        },
    }
    message = SimpleNamespace(
        id=80,
        origin_domain="guild.example",
        proxy_request_fingerprint_version=receipt.version,
        proxy_request_fingerprint=receipt.sha256,
        proxy_commit_seq=9,
    )
    event = SimpleNamespace(
        guild_id=60,
        guild_domain="guild.example",
        seq=9,
        envelope=committed,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[channel, event]),
        scalar=AsyncMock(return_value=message),
    )
    mutable_admission = AsyncMock()
    rate_limit = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(federation_api, "lock_terminal_room", AsyncMock())
    monkeypatch.setattr(federation_api, "lock_proxy_nonce", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        mutable_admission,
    )
    monkeypatch.setattr(federation_api, "require_permissions", mutable_admission)

    result = await federation_api.federation_guild_proxy(
        60,
        payload,
        FederationPrincipal(
            origin="member.example",
            key_id="key-1",
            silenced=False,
            source_ip="127.0.0.1",
        ),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == {"message": committed["content"]["message"], "seq": "9", "event": committed}
    rate_limit.assert_awaited_once_with(
        cast(Any, SimpleNamespace()),
        "member.example",
        "guild-message-create",
        capacity=3_000,
        refill_per_minute=3_000,
    )
    mutable_admission.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_queued_proxy_retry_uses_retained_commit_seq_and_detects_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = proxy_fingerprint_request(attachments=[])
    receipt = federation_api.proxy_request_fingerprint_receipt(payload, "guild.example")
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example", guild_id=60)
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    message = SimpleNamespace(
        id=80,
        origin_domain="guild.example",
        proxy_request_fingerprint_version=receipt.version,
        proxy_request_fingerprint=receipt.sha256,
        proxy_commit_seq=9,
    )
    committed = {
        "event_id": "kcge_retained_queue",
        "type": "guild.message.committed",
        "context": {
            "guild_id": "60",
            "guild_domain": "guild.example",
            "seq": "9",
        },
        "content": {
            "message": {"id": "80", "origin_domain": "guild.example"},
            "proxy_request_fingerprint": receipt.wire(),
        },
    }
    event = SimpleNamespace(
        guild_id=60,
        guild_domain="guild.example",
        seq=9,
        envelope=committed,
    )
    envelope = SimpleNamespace(
        origin="member.example",
        actor=SimpleNamespace(id="10", domain="member.example"),
        context={"guild_id": "60", "guild_domain": "guild.example"},
        content=payload.model_dump(mode="json"),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[channel, actor, event]),
        scalar=AsyncMock(return_value=message),
    )
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "lock_proxy_nonce", AsyncMock())
    # Retention replay is an exact primary-key lookup, not a JSON history scan.
    monkeypatch.setattr(
        federation_api,
        "guild_event_for_message",
        AsyncMock(side_effect=AssertionError("legacy scan must not run")),
    )

    normalized, replay = await federation_api.queued_proxy_request_replay(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, envelope),
    )
    assert normalized == payload
    assert replay is not None and replay.event is event

    collision = payload.model_copy(update={"content": "changed"})
    envelope.content = collision.model_dump(mode="json")
    session.get = AsyncMock(side_effect=[channel, actor, event])
    session.scalar = AsyncMock(return_value=message)
    with pytest.raises(federation_api.ProxyNonceStateConflict):
        await federation_api.queued_proxy_request_replay(
            cast(Any, session),
            cast(Any, SimpleNamespace(domain="guild.example")),
            cast(Any, envelope),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_commit_seq", [None, 9])
async def test_queued_legacy_proxy_nonce_without_retained_event_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    legacy_commit_seq: int | None,
) -> None:
    payload = proxy_fingerprint_request(attachments=[])
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example", guild_id=60)
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    message = SimpleNamespace(
        id=80,
        origin_domain="guild.example",
        proxy_request_fingerprint_version=None,
        proxy_request_fingerprint=None,
        proxy_commit_seq=legacy_commit_seq,
    )
    envelope = SimpleNamespace(
        origin="member.example",
        actor=SimpleNamespace(id="10", domain="member.example"),
        context={"guild_id": "60", "guild_domain": "guild.example"},
        content=payload.model_dump(mode="json"),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[channel, actor, None]),
        scalar=AsyncMock(return_value=message),
    )
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "lock_proxy_nonce", AsyncMock())
    monkeypatch.setattr(
        federation_api,
        "guild_event_for_message",
        AsyncMock(return_value=None),
    )

    with pytest.raises(
        federation_api.ProxyNonceStateConflict,
        match="no retained commit event",
    ):
        await federation_api.queued_proxy_request_replay(
            cast(Any, session),
            cast(Any, SimpleNamespace(domain="guild.example")),
            cast(Any, envelope),
        )


def rich_proxy_request() -> dict[str, object]:
    return {
        "operation": "message.create",
        "actor": remote_actor(),
        "channel_id": "20",
        "embeds": [{"title": "Build complete", "description": "All checks passed"}],
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 1,
                        "label": "Details",
                        "custom_id": "build:details",
                    }
                ],
            }
        ],
        "poll": {
            "question": {"text": "Ship it?"},
            "answers": [
                {"poll_media": {"text": "Yes"}},
                {"poll_media": {"text": "No"}},
            ],
            "duration": 24,
            "allow_multiselect": False,
        },
        "application_id": "30@apps.example",
        "interaction_integration_type": "guild_install",
        "interaction_installation_ref": "40@guild.example",
        "interaction_installation_revision": "3",
        "view_persistent": True,
        "client_nonce": "federated-rich-1",
    }


def rendered_poll(
    created_at: datetime, *, finalized_at: datetime | None = None
) -> dict[str, object]:
    return {
        "question": {"text": "Ship it?"},
        "answers": [
            {"answer_id": 1, "poll_media": {"text": "Yes"}},
            {"answer_id": 2, "poll_media": {"text": "No"}},
        ],
        "expiry": (created_at + timedelta(hours=24)).isoformat(),
        "allow_multiselect": False,
        "layout_type": 1,
        "finalized_at": finalized_at.isoformat() if finalized_at is not None else None,
        "results": {
            "is_finalized": finalized_at is not None,
            "answer_counts": [
                {"id": 1, "count": 0, "me_voted": False},
                {"id": 2, "count": 0, "me_voted": False},
            ],
        },
    }


def sticker_item(*, sticker_id: int = 71) -> dict[str, object]:
    return {
        "id": str(sticker_id),
        "origin_domain": "guild.example",
        "name": "party_blob",
        "format_type": 1,
        "media_hash": "a" * 64,
    }


def test_guild_proxy_accepts_complete_rich_message_and_rejects_unsafe_combinations() -> None:
    parsed = GuildProxyRequest.model_validate(rich_proxy_request())

    assert parsed.application_id is not None
    assert parsed.poll is not None
    assert parsed.view_persistent is True
    assert parsed.components[0].components[0].custom_id == "build:details"

    without_application = rich_proxy_request()
    without_application.pop("application_id")
    with pytest.raises(ValidationError, match="application identity"):
        GuildProxyRequest.model_validate(without_application)

    encrypted = rich_proxy_request() | {
        "e2ee": {"version": 1, "ciphertext": "opaque"},
    }
    with pytest.raises(ValidationError, match="rich plaintext"):
        GuildProxyRequest.model_validate(encrypted)

    forwarded = rich_proxy_request() | {"forwarded_message_id": "9@guild.example"}
    with pytest.raises(ValidationError, match="optional text note"):
        GuildProxyRequest.model_validate(forwarded)


def test_replicated_rich_projection_validates_interactions_polls_and_e2ee_fence() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    raw = {
        "content": None,
        "attachments": [],
        "embeds": [{"title": "Build complete"}],
        "components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 1,
                        "label": "Details",
                        "custom_id": "build:details",
                    }
                ],
            }
        ],
        "application_id": "30",
        "application_domain": "apps.example",
        "interaction_integration_type": "guild_install",
        "interaction_installation_ref": "40@guild.example",
        "interaction_installation_revision": "3",
        "view_version": 1,
        "view_persistent": True,
        "poll": rendered_poll(created_at),
    }

    projection = _validated_message_rich_projection(
        raw,
        message_id=100,
        message_origin="guild.example",
        message_created_at=created_at,
        e2ee=None,
        message_type=0,
    )

    assert projection["application_ref"] == (30, "apps.example")
    assert projection["view_version"] == 1
    assert projection["poll"] is not None

    with pytest.raises(ValueError, match="rich plaintext"):
        _validated_message_rich_projection(
            raw,
            message_id=100,
            message_origin="guild.example",
            message_created_at=created_at,
            e2ee={"version": 1, "ciphertext": "opaque"},
            message_type=0,
        )

    invalid_finalization = dict(raw)
    invalid_finalization["poll"] = rendered_poll(
        created_at,
        finalized_at=created_at - timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="finalization"):
        _validated_message_rich_projection(
            invalid_finalization,
            message_id=100,
            message_origin="guild.example",
            message_created_at=created_at,
            e2ee=None,
            message_type=0,
        )


def test_message_replay_fingerprint_binds_rich_and_forward_identity() -> None:
    shared: dict[str, object] = {
        "channel_id": 20,
        "channel_domain": "guild.example",
        "author_id": 10,
        "author_domain": "member.example",
        "content": None,
        "e2ee": None,
        "message_type": 0,
        "flags": 0,
        "client_nonce": "rich-replay",
        "referenced_message_id": None,
        "referenced_message_domain": None,
        "mention_user_refs": [],
        "mention_role_refs": [],
        "mention_everyone": False,
        "created_at": datetime(2026, 8, 27, tzinfo=UTC),
    }
    plain = replicated_message_create_fingerprint(**shared)
    rich = replicated_message_create_fingerprint(
        **shared,
        embeds=[{"title": "Authority-owned"}],
        application_id=30,
        application_domain="apps.example",
        view_version=1,
    )
    forwarded = replicated_message_create_fingerprint(
        **shared,
        forwarded_message_id=9,
        forwarded_message_domain="guild.example",
    )
    sticker = replicated_message_create_fingerprint(
        **shared,
        sticker_items=[sticker_item()],
    )
    webhook = replicated_message_create_fingerprint(
        **shared,
        webhook_id=70,
        webhook_domain="guild.example",
        webhook_name="Release relay",
        webhook_avatar_url="https://cdn.example/avatar.png",
    )

    assert plain != rich
    assert plain != forwarded
    assert rich != forwarded
    assert plain != sticker
    assert plain != webhook


def test_webhook_attribution_is_type_zero_qualified_and_url_bounded() -> None:
    attribution = validate_webhook_attribution(
        {
            "id": "70",
            "origin_domain": "guild.example",
            "ref": "70@guild.example",
            "name": "Release relay",
            "avatar_hash": None,
            "avatar_url": "https://cdn.example/" + "a" * 256,
        },
        message_type=0,
        message_origin="guild.example",
        label="guild message",
    )

    assert attribution is not None
    assert attribution.webhook_ref == (70, "guild.example")
    with pytest.raises(ValueError, match="attribution"):
        validate_webhook_attribution(
            {
                "id": "70",
                "origin_domain": "guild.example",
                "name": "Release relay",
            },
            message_type=2,
            message_origin="guild.example",
            label="guild message",
        )
    with pytest.raises(ValueError, match="authority"):
        validate_webhook_attribution(
            {
                "id": "70",
                "origin_domain": "attacker.example",
                "name": "Release relay",
            },
            message_type=0,
            message_origin="guild.example",
            label="guild message",
        )


def test_webhook_message_payload_keeps_discord_type_and_qualified_identity() -> None:
    message = Message(
        id=100,
        origin_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
        author_id=10,
        author_domain="member.example",
        content="deployed",
        message_type=0,
        webhook_id=70,
        webhook_domain="guild.example",
        webhook_name="Release relay",
        webhook_avatar_hash=None,
        webhook_avatar_url="https://cdn.example/avatar.png",
        embeds=[],
        components=[],
        sticker_items=[],
        flags=0,
        view_version=0,
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
    )

    rendered = message_payload(message)

    assert rendered["message_type"] == 0
    assert rendered["webhook"] == {
        "id": "70",
        "origin_domain": "guild.example",
        "ref": "70@guild.example",
        "name": "Release relay",
        "avatar_hash": None,
        "avatar_url": "https://cdn.example/avatar.png",
    }


def test_federated_dm_rich_projection_preserves_view_poll_and_v2_components() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    projection = validate_replicated_rich_projection(
        {
            "attachments": [],
            "embeds": [{"title": "Remote application"}],
            "components": [{"type": 10, "content": "Application output"}],
            "application_id": "30",
            "application_domain": "apps.example",
            "interaction_integration_type": "dm_capability",
            "interaction_installation_ref": "40@member.example",
            "interaction_installation_revision": "3",
            "view_version": 4,
            "view_persistent": False,
            "view_expires_at": (created_at + timedelta(minutes=15)).isoformat(),
            "poll": rendered_poll(created_at),
        },
        message_id=100,
        message_origin="member.example",
        message_created_at=created_at,
        e2ee=None,
        message_type=0,
        label="DM message",
    )

    assert projection.application_ref == (30, "apps.example")
    assert projection.view_version == 4
    assert projection.view_expires_at == created_at + timedelta(minutes=15)
    assert projection.poll is not None
    assert projection.components == [{"type": 10, "id": 1, "content": "Application output"}]


def test_federated_rich_projection_rejects_dangling_attachment_media() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)

    with pytest.raises(ValueError, match="rich content"):
        validate_replicated_rich_projection(
            {
                "attachments": [],
                "embeds": [{"image": {"url": "attachment://missing.png"}}],
            },
            message_id=100,
            message_origin="member.example",
            message_created_at=created_at,
            e2ee=None,
            message_type=0,
            label="DM message",
        )


def test_sticker_only_messages_are_bounded_and_preserve_immutable_projection() -> None:
    message = MessageCreate(sticker_ids=["71@guild.example"])
    assert [str(item) for item in message.sticker_ids] == ["71@guild.example"]
    with pytest.raises(ValidationError, match="unique"):
        MessageCreate(sticker_ids=["71@guild.example", "71@guild.example"])

    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    projection = validate_replicated_rich_projection(
        {
            "attachments": [],
            "sticker_items": [sticker_item()],
        },
        message_id=100,
        message_origin="member.example",
        message_created_at=created_at,
        e2ee=None,
        message_type=0,
        label="DM message",
    )
    assert projection.sticker_items == [sticker_item()]

    malformed = sticker_item()
    malformed["name"] = "x"
    with pytest.raises(ValueError, match="rich content"):
        validate_replicated_rich_projection(
            {"attachments": [], "sticker_items": [malformed]},
            message_id=100,
            message_origin="member.example",
            message_created_at=created_at,
            e2ee=None,
            message_type=0,
            label="DM message",
        )


def test_forward_snapshot_preserves_stickers_mentions_and_one_nested_snapshot() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC).isoformat()
    inner = {
        "content": "original",
        "message_type": 20,
        "flags": 0,
        "created_at": created_at,
    }
    snapshot = validate_forward_snapshot(
        {
            "content": "forward note",
            "message_type": 19,
            "flags": 0,
            "created_at": created_at,
            "mention_user_refs": [{"id": "9", "origin_domain": "user.example"}],
            "sticker_items": [sticker_item()],
            "message_snapshots": [inner],
        }
    )
    assert snapshot["sticker_items"] == [sticker_item()]
    assert snapshot["mention_user_refs"] == [{"id": "9", "origin_domain": "user.example"}]
    assert snapshot["message_snapshots"][0]["content"] == "original"

    with pytest.raises(ValueError, match="forward snapshot is invalid"):
        validate_forward_snapshot(
            {
                "content": "too deep",
                "message_type": 0,
                "flags": 0,
                "created_at": created_at,
                "message_snapshots": [snapshot],
            }
        )


def test_forward_snapshot_rejects_dangling_attachment_media() -> None:
    with pytest.raises(ValueError, match="forward snapshot"):
        validate_forward_snapshot(
            {
                "embeds": [{"image": {"url": "attachment://missing.png"}}],
                "created_at": datetime(2026, 8, 27, tzinfo=UTC).isoformat(),
            }
        )


def test_federated_forward_requires_signed_age_context() -> None:
    snapshot = {
        "content": "source",
        "message_type": 0,
        "flags": 0,
        "created_at": datetime(2026, 8, 27, tzinfo=UTC).isoformat(),
    }
    request = {
        "operation": "message.create",
        "actor": remote_actor(),
        "channel_id": "20",
        "content": "note",
        "forwarded_message_id": "9@source.example",
        "forwarded_channel_id": "8@source.example",
        "forward_snapshot": snapshot,
        "client_nonce": "forward-age-context",
    }

    with pytest.raises(ValidationError, match="authoritative age context"):
        GuildProxyRequest.model_validate(request)
    with pytest.raises(ValidationError, match="source-authority proof"):
        GuildProxyRequest.model_validate(request | {"forward_source_nsfw": False})
    parsed = GuildProxyRequest.model_validate(
        request
        | {
            "forward_source_nsfw": False,
            "forward_source_proof": {"type": "message.forward.source.authorized"},
        }
    )
    assert parsed.forward_source_nsfw is False
    assert can_forward_between_age_contexts(True, False) is False
    assert can_forward_between_age_contexts(True, True) is True
    assert can_forward_between_age_contexts(None, True) is False


@pytest.mark.asyncio
async def test_destination_authority_rechecks_forward_age_context() -> None:
    destination = Channel(
        id=20,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        type=0,
        nsfw=False,
        created_floor_id=20,
    )
    session = SimpleNamespace(get=AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await federation_api.require_attested_forward_age_context(
            cast(Any, session),
            destination,
            True,
        )
    assert denied.value.status_code == 409
    assert cast(dict[str, object], denied.value.detail)["code"] == (
        "AGE_RESTRICTED_FORWARD_UNSUPPORTED"
    )
    destination.nsfw = True
    await federation_api.require_attested_forward_age_context(
        cast(Any, session),
        destination,
        True,
    )


@pytest.mark.asyncio
async def test_local_forward_admission_rechecks_both_channel_age_contexts() -> None:
    source = Channel(
        id=10,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        type=0,
        nsfw=True,
        created_floor_id=10,
    )
    destination = Channel(
        id=20,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        type=0,
        nsfw=False,
        created_floor_id=20,
    )
    session = SimpleNamespace(get=AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await channels_api.require_forward_age_context(
            cast(Any, session),
            source,
            destination,
        )
    assert cast(dict[str, object], denied.value.detail)["code"] == (
        "AGE_RESTRICTED_FORWARD_UNSUPPORTED"
    )
    destination.nsfw = True
    assert (
        await channels_api.require_forward_age_context(
            cast(Any, session),
            source,
            destination,
        )
        is True
    )


def test_federated_dm_rejects_restricted_or_unattested_forward_snapshots() -> None:
    raw_message = {"message_snapshots": [{"message": {"content": "source"}}]}

    with pytest.raises(ValueError, match="age context is missing"):
        federation_api.validate_dm_forward_age_context({}, raw_message)
    with pytest.raises(ValueError, match="cannot be forwarded to a DM"):
        federation_api.validate_dm_forward_age_context(
            {"forward_source_nsfw": True},
            raw_message,
        )
    federation_api.validate_dm_forward_age_context(
        {"forward_source_nsfw": False},
        raw_message,
    )


def test_forward_payload_is_an_author_free_snapshot_and_batch_is_limited_to_five() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    snapshot = {
        "content": "immutable source",
        "embeds": [],
        "components": [],
        "attachments": [],
        "message_type": 0,
        "flags": 0,
        "created_at": created_at.isoformat(),
    }
    forwarded = Message(
        id=101,
        origin_domain="home.example",
        channel_id=201,
        channel_domain="home.example",
        author_id=301,
        author_domain="home.example",
        content="optional note",
        e2ee=None,
        forwarded_message_id=99,
        forwarded_message_domain="source.example",
        forwarded_channel_id=199,
        forwarded_channel_domain="source.example",
        forward_snapshot=snapshot,
        embeds=[],
        components=[],
        view_version=0,
        created_at=created_at,
    )

    rendered = message_payload(forwarded)

    assert rendered["forwarded_message_id"] is None
    assert rendered["forwarded_message_ref"] is None
    assert rendered["message_reference"] == {"type": 1}
    assert rendered["message_snapshots"] == [{"message": snapshot}]
    assert "author" not in rendered["message_snapshots"][0]["message"]
    parsed = MessageForwardCreate.model_validate(
        {
            "destination_channel_ids": [str(index) for index in range(1, 6)],
            "content": "FYI",
        }
    )
    assert len(parsed.destination_channel_ids) == 5
    with pytest.raises(ValidationError):
        MessageForwardCreate.model_validate(
            {"destination_channel_ids": [str(index) for index in range(1, 7)]}
        )


@pytest.mark.asyncio
async def test_proxy_nonce_replay_binds_normalized_attachment_set() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    message = Message(
        id=40,
        origin_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
        author_id=10,
        author_domain="member.example",
        content="artifact",
        e2ee=None,
        embeds=[],
        components=[],
        view_version=0,
        client_nonce="attachment-replay",
        created_at=created_at,
    )
    attachment = Attachment(
        id=50,
        origin_domain="member.example",
        message_id=message.id,
        message_domain=message.origin_domain,
        uploader_id=10,
        uploader_domain="member.example",
        filename="artifact.txt",
        content_type="text/plain",
        detected_content_type=None,
        size=8,
        object_key="remote/member.example/50/original",
        width=None,
        height=None,
        blurhash=None,
        scan_status="clean",
        encryption_mode="plaintext",
        encryption_protocol=None,
        purpose="attachment",
        variants={},
        content_sha256="a" * 64,
        created_at=created_at,
    )
    attachment_projection = {
        "id": "50",
        "origin_domain": "member.example",
        "filename": "artifact.txt",
        "content_type": "text/plain",
        "size": 8,
        "width": None,
        "height": None,
        "blurhash": None,
        "scan_status": "pending",
        "encryption_mode": "plaintext",
        "encryption_protocol": None,
        "content_sha256": "a" * 64,
        "duration_secs": None,
        "waveform": None,
        "variants": {},
    }
    payload = GuildProxyRequest.model_validate(
        {
            "operation": "message.create",
            "actor": remote_actor(),
            "channel_id": "20",
            "content": "artifact",
            "client_nonce": "attachment-replay",
            "attachments": [attachment_projection],
        }
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[None, None]),
        scalars=AsyncMock(return_value=[attachment]),
    )

    assert await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        payload,
        application_ref=None,
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )

    processing_refresh = payload.model_copy(deep=True)
    processing_refresh.attachments[0].update(
        {
            "content_type": "application/octet-stream",
            "width": 120,
            "height": 60,
            "blurhash": "derived",
            "scan_status": "clean",
            "variants": {"thumbnail_128": {"size": 90}},
        }
    )
    session.get = AsyncMock(side_effect=[None, None])
    assert await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        processing_refresh,
        application_ref=None,
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )

    changed = payload.model_copy(deep=True)
    changed.attachments[0]["filename"] = "different.txt"
    session.get = AsyncMock(side_effect=[None, None])
    assert not await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        changed,
        application_ref=None,
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )

    changed_hash = payload.model_copy(deep=True)
    changed_hash.attachments[0]["content_sha256"] = "b" * 64
    session.get = AsyncMock(side_effect=[None, None])
    assert not await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        changed_hash,
        application_ref=None,
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )


@pytest.mark.asyncio
async def test_remote_poll_remove_is_authoritative_and_not_blocked_by_automod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    message = SimpleNamespace(
        id=40,
        origin_domain="guild.example",
        author_id=actor.id,
        author_domain=actor.origin_domain,
    )
    poll = SimpleNamespace(
        allow_multiselect=False,
        finalized_at=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    payload = GuildPollVoteProxyRequest.model_validate(
        {
            "actor": remote_actor(),
            "channel_id": "20",
            "message_id": "40@guild.example",
            "answer_id": 1,
            "remove": True,
        }
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(answer_id=1)),
        scalar=AsyncMock(return_value=1),
        commit=AsyncMock(),
    )
    interaction_check = AsyncMock()
    queued = AsyncMock()
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    restriction_check = AsyncMock()
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        restriction_check,
    )
    monkeypatch.setattr(
        federation_api,
        "_federation_poll_context",
        AsyncMock(return_value=(guild, channel, message, poll)),
    )
    monkeypatch.setattr(
        federation_api,
        "require_member_interactions_allowed",
        interaction_check,
    )
    monkeypatch.setattr(federation_api, "mark_guild_activity", AsyncMock())
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queued)
    monkeypatch.setattr(federation_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(federation_api, "publish_dispatch", AsyncMock())

    result = await federation_api.federation_guild_poll_vote_proxy(
        guild_id=cast(Any, guild.id),
        payload=payload,
        principal=FederationPrincipal("member.example", "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == {"voted": False}
    restriction_check.assert_not_awaited()
    interaction_check.assert_not_awaited()
    assert queued.await_args.args[4] == "guild.poll.vote.remove"
    assert queued.await_args.args[5]["user"] == {
        "id": "10",
        "origin_domain": "member.example",
    }
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_poll_finalize_applies_member_interaction_gate_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=10, origin_domain="guild.example")
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=channel)
    message = SimpleNamespace(
        id=40,
        origin_domain="guild.example",
        author_id=actor.id,
        author_domain=actor.origin_domain,
    )
    poll = SimpleNamespace(finalized_at=None)
    session = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock())
    denied = HTTPException(status_code=403, detail={"code": "MEMBER_TIMED_OUT"})
    interaction_gate = AsyncMock(side_effect=denied)
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(
        channels_api,
        "_poll_for_mutation",
        AsyncMock(return_value=(access, message, poll)),
    )
    monkeypatch.setattr(
        channels_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )

    with pytest.raises(HTTPException) as caught:
        await channels_api.finalize_poll(
            EntityRef("20@guild.example"),
            EntityRef("40@guild.example"),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert caught.value is denied
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.SEND_POLLS,
    )
    assert poll.finalized_at is None
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_federated_poll_finalize_applies_admission_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=10, origin_domain="member.example")
    guild = SimpleNamespace(id=60, origin_domain="guild.example")
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    message = SimpleNamespace(
        id=40,
        origin_domain="guild.example",
        author_id=actor.id,
        author_domain=actor.origin_domain,
    )
    poll = SimpleNamespace(finalized_at=None)
    payload = GuildPollFinalizeProxyRequest.model_validate(
        {
            "actor": remote_actor(),
            "channel_id": "20",
            "message_id": "40@guild.example",
        }
    )
    restriction_check = AsyncMock()
    denied = HTTPException(status_code=403, detail={"code": "MEMBER_TIMED_OUT"})
    interaction_gate = AsyncMock(side_effect=denied)
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        restriction_check,
    )
    monkeypatch.setattr(
        federation_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )
    monkeypatch.setattr(
        federation_api,
        "_federation_poll_context",
        AsyncMock(return_value=(guild, channel, message, poll)),
    )
    queued = AsyncMock()
    monkeypatch.setattr(federation_api, "queue_guild_mutation", queued)
    session = SimpleNamespace()

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_guild_poll_finalize_proxy(
            cast(Any, guild.id),
            payload,
            FederationPrincipal("member.example", "ed25519:test"),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert caught.value is denied
    restriction_check.assert_awaited_once()
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.SEND_POLLS,
    )
    assert poll.finalized_at is None
    queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_poll_payload_preserves_manual_finalization_time() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    finalized_at = created_at + timedelta(minutes=5)
    message = SimpleNamespace(id=40, origin_domain="guild.example")
    poll = Poll(
        message_id=40,
        message_domain="guild.example",
        question={"text": "Ship it?"},
        allow_multiselect=False,
        layout_type=1,
        expires_at=created_at + timedelta(hours=24),
        finalized_at=finalized_at,
    )
    answers = [
        PollAnswer(
            message_id=40,
            message_domain="guild.example",
            answer_id=1,
            text="Yes",
            emoji=None,
        ),
        PollAnswer(
            message_id=40,
            message_domain="guild.example",
            answer_id=2,
            text="No",
            emoji=None,
        ),
    ]
    session = SimpleNamespace(
        get=AsyncMock(return_value=poll),
        scalars=AsyncMock(return_value=answers),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [])),
    )

    payload = await render_poll_payload(cast(Any, session), cast(Any, message))

    assert payload is not None
    assert payload["finalized_at"] == finalized_at.isoformat()
    assert cast(dict[str, object], payload["results"])["is_finalized"] is True
