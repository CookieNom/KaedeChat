from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.push import (
    _push_relay_rate_limit,
    relay_enrollment_key,
    rotated_token_fields,
)
from app.chat.payloads import public_user_display_name
from app.db.models import User
from app.push.presentation import notification_previews_enabled, push_presentation
from app.push.relay import stable_wake_identifier, wake_mac
from app.push.schemas import (
    PushDeviceCreate,
    PushNotificationRedeem,
    PushNotificationResponse,
    PushRelaySubscriptionCreate,
    PushRelayWakeCreate,
)
from app.push.service import fcm_relay_payload, fcm_sync_payload
from app.push.sync import (
    PushSyncEvent,
    claim_push_sync,
    issue_push_sync,
    load_push_sync,
    push_sync_key,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def set(self, key: str, value: str, **_: object) -> bool:
        if key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True


def test_public_user_display_name_hides_unresolved_history_handle() -> None:
    unresolved = User(
        id=42,
        origin_domain="remote.example",
        username="history_deadbeef",
        is_local=False,
        profile_resolved=False,
    )
    resolved = User(
        id=43,
        origin_domain="remote.example",
        username="maple",
        display_name="Maple",
        is_local=False,
        profile_resolved=True,
    )

    assert public_user_display_name(unresolved) == "Remote user · remote.example"
    assert public_user_display_name(resolved) == "Maple"


def test_push_registration_accepts_native_platforms() -> None:
    android = PushDeviceCreate(
        installation_id=uuid4(), platform="android", token="a" * 64, device_name="Pixel"
    )
    ios = PushDeviceCreate(installation_id=uuid4(), platform="ios", token="b" * 64)
    assert android.platform == "android"
    assert android.device_name == "Pixel"
    assert ios.platform == "ios"


@pytest.mark.parametrize(
    "token",
    [
        "short",
        " " + "a" * 24,
        "a" * 20 + "\n",
        "a" * 10 + " " + "b" * 10,
    ],
)
def test_push_registration_rejects_malformed_tokens(token: str) -> None:
    with pytest.raises(ValidationError):
        PushDeviceCreate(installation_id=uuid4(), platform="android", token=token)


def test_push_registration_rejects_unknown_platforms() -> None:
    with pytest.raises(ValidationError):
        PushDeviceCreate(  # type: ignore[arg-type]
            installation_id=uuid4(), platform="windows", token="a" * 64
        )


def test_push_registration_requires_valid_installation_id() -> None:
    with pytest.raises(ValidationError):
        PushDeviceCreate(
            installation_id="not-a-uuid",  # type: ignore[arg-type]
            platform="android",
            token="a" * 64,
        )


def test_provider_token_rotation_updates_digest_and_ciphertext_together() -> None:
    now = datetime.now(UTC)
    body = PushDeviceCreate(
        installation_id=uuid4(),
        platform="ios",
        token="a" * 64,
        device_name="iPhone",
    )
    fields = rotated_token_fields(
        body,
        digest=b"d" * 32,
        encrypted=b"ciphertext",
        now=now,
    )

    assert fields["token_hash"] == b"d" * 32
    assert fields["token_encrypted"] == b"ciphertext"
    assert fields["platform"] == "ios"
    assert fields["last_seen_at"] is now


def test_push_presentation_hides_message_content_by_default() -> None:
    assert push_presentation(
        show_preview=False,
        is_dm=True,
        is_mention=False,
        title="Alice",
        body="private message text",
    ) == ("Kaede Chat", "New direct message")
    assert push_presentation(
        show_preview=False,
        is_dm=False,
        is_mention=True,
        title="Alice in Staff",
        body="private message text",
    ) == ("Kaede Chat", "You were mentioned")


def test_push_presentation_allows_opted_in_previews() -> None:
    assert push_presentation(
        show_preview=True,
        is_dm=False,
        is_mention=False,
        title="Alice in Lounge",
        body="Hello",
    ) == ("Alice in Lounge", "Hello")


def test_notification_previews_default_on_and_preserve_explicit_opt_out() -> None:
    assert notification_previews_enabled({}) is True
    assert notification_previews_enabled({"show_notification_previews": True}) is True
    assert notification_previews_enabled({"show_notification_previews": False}) is False


@pytest.mark.parametrize("platform", ["android", "ios"])
def test_fcm_payload_is_an_opaque_content_free_wake(platform: str) -> None:
    payload = fcm_sync_payload("provider-token", "x" * 43, platform)
    message = payload["message"]

    assert message["data"] == {"sync_version": "1", "event_token": "x" * 43}
    assert "notification" not in message
    rendered = str(payload)
    for private_value in (
        "private message text",
        "Alice",
        "channel_ref",
        "message_ref",
        "direct_message",
        "guild_message",
        "mention",
    ):
        assert private_value not in rendered


def test_fcm_payload_rejects_unknown_platform() -> None:
    with pytest.raises(ValueError, match="unsupported push platform"):
        fcm_sync_payload("provider-token", "x" * 43, "windows")


def test_relay_payload_is_content_free_and_mac_bound() -> None:
    secret = "A" * 43
    fields = {
        "route_id": "r" * 43,
        "event_token": "e" * 43,
        "delivery_id": "d" * 43,
        "expires_at": 2_000_000_000,
    }
    mac = wake_mac(secret, **fields)
    payload = fcm_relay_payload(
        "provider-token",
        **fields,
        wake_mac=mac,
        platform="android",
    )
    assert payload["message"]["data"] == {
        "sync_version": "2",
        "route_id": fields["route_id"],
        "event_token": fields["event_token"],
        "delivery_id": fields["delivery_id"],
        "expires_at": str(fields["expires_at"]),
        "wake_mac": mac,
    }
    rendered = str(payload)
    for private_value in ("user_id", "message_ref", "channel_ref", "sender", "content"):
        assert private_value not in rendered


def test_relay_wake_schema_and_idempotency_identifiers_are_strict() -> None:
    settings = SimpleNamespace(secret_key_bytes=b"s" * 32)
    request_id = stable_wake_identifier(
        settings,  # type: ignore[arg-type]
        purpose="request",
        device_id="8a73864e-f353-4880-991e-fcbf1c916dbf",
        message_id=42,
        message_domain="remote.example",
        kind="mention",
    )
    assert len(request_id) == 43
    assert request_id == stable_wake_identifier(
        settings,  # type: ignore[arg-type]
        purpose="request",
        device_id="8a73864e-f353-4880-991e-fcbf1c916dbf",
        message_id=42,
        message_domain="remote.example",
        kind="mention",
    )
    wake = PushRelayWakeCreate(
        version=2,
        request_id=request_id,
        subscription_id="kps_" + "s" * 40,
        route_id="r" * 43,
        event_token="e" * 43,
        delivery_id="d" * 43,
        expires_at=2_000_000_000,
        wake_mac="m" * 43,
    )
    assert wake.version == 2
    with pytest.raises(ValidationError):
        PushRelayWakeCreate(**{**wake.model_dump(), "request_id": "not opaque"})


def test_relay_subscription_rejects_malformed_device_secrets() -> None:
    with pytest.raises(ValidationError):
        PushRelaySubscriptionCreate(grant={}, provider_token="p" * 32, management_secret="short")


def test_relay_pending_enrollment_keys_do_not_expose_device_routes() -> None:
    route = "r" * 43
    key = relay_enrollment_key(route)
    assert route not in key
    assert key == relay_enrollment_key(route)


@pytest.mark.asyncio
async def test_push_relay_rate_limit_is_scoped_hashed_and_retryable() -> None:
    redis = MemoryRedis()
    scope = "registration-origin:private-home.example"

    await _push_relay_rate_limit(redis, scope, limit=2)  # type: ignore[arg-type]
    await _push_relay_rate_limit(redis, scope, limit=2)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as raised:
        await _push_relay_rate_limit(redis, scope, limit=2)  # type: ignore[arg-type]

    error = raised.value
    assert getattr(error, "status_code", None) == 429
    assert getattr(error, "headers", None) == {"Retry-After": "60"}
    assert len(redis.counters) == 1
    key = next(iter(redis.counters))
    assert scope not in key
    assert redis.expiries[key] == 60


@pytest.mark.parametrize(
    "event_token",
    ["short", "a" * 42, "a" * 44, "a" * 42 + "/", "42@chat.example"],
)
def test_fcm_payload_rejects_non_opaque_event_tokens(event_token: str) -> None:
    with pytest.raises(ValueError, match="invalid push sync token"):
        fcm_sync_payload("provider-token", event_token, "android")


def test_push_redemption_requires_a_fixed_urlsafe_token() -> None:
    valid = PushNotificationRedeem(installation_id=uuid4(), event_token="a" * 43)
    assert valid.event_token == "a" * 43
    for token in ("short", "a" * 42, "a" * 44, "a" * 42 + "/"):
        with pytest.raises(ValidationError):
            PushNotificationRedeem(installation_id=uuid4(), event_token=token)


def test_redeemed_notification_supports_private_sender_presentation() -> None:
    response = PushNotificationResponse(
        kind="direct_message",
        title="Turtle",
        body="Hello",
        channel_ref="42@chat.example",
        message_ref="73@remote.example",
        sender_name="Turtle",
        sender_ref="9@remote.example",
        sender_avatar_hash="a" * 64,
        sent_at="2026-08-11T11:42:00+00:00",
    )

    assert response.sender_name == "Turtle"
    assert response.sender_avatar_hash == "a" * 64
    with pytest.raises(ValidationError):
        PushNotificationResponse(
            kind="direct_message",
            title="Turtle",
            body="Hello",
            channel_ref="42@chat.example",
            message_ref="73@remote.example",
            sender_name="Turtle",
            sender_ref="9@remote.example",
            sender_avatar_hash="../avatar.png",
            sent_at="2026-08-11T11:42:00+00:00",
        )


@pytest.mark.asyncio
async def test_push_sync_tokens_are_hashed_bound_and_single_use() -> None:
    redis = MemoryRedis()
    event = PushSyncEvent(
        device_id=str(uuid4()),
        user_id=123,
        user_domain="kaede.chat",
        message_id=456,
        message_domain="remote.example",
        kind="mention",
    )
    token = await issue_push_sync(redis, event)  # type: ignore[arg-type]

    assert len(token) == 43
    assert token not in push_sync_key(token)
    loaded = await load_push_sync(redis, token)  # type: ignore[arg-type]
    assert loaded is not None
    encoded, restored = loaded
    assert restored == event
    assert await claim_push_sync(redis, token, encoded)  # type: ignore[arg-type]
    assert await load_push_sync(redis, token) is None  # type: ignore[arg-type]
    assert not await claim_push_sync(redis, token, encoded)  # type: ignore[arg-type]
