from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.push import rotated_token_fields
from app.push.presentation import push_presentation
from app.push.schemas import (
    PushDeviceCreate,
    PushNotificationRedeem,
    PushNotificationResponse,
)
from app.push.service import fcm_sync_payload
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
