from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import Client, EntityRef, StageInstance, StageVoiceState, WorkerState

TARGET = "https://apps.example"
GUILD = EntityRef(10, "chat.example")
CHANNEL = EntityRef(30, "chat.example")
USER = EntityRef(40, "people.example")
BOT_USER = EntityRef(70, "apps.example")
EVENT = EntityRef(50, "chat.example")


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def stage_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "60",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "topic": "Town hall",
        "privacy_level": 2,
        "discoverable_disabled": True,
        "guild_scheduled_event_id": "50",
        "guild_scheduled_event_domain": "chat.example",
    }
    payload.update(overrides)
    return payload


def voice_state_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": "30",
        "channel_domain": "chat.example",
        "user_id": "40",
        "user_domain": "people.example",
        "session_id": "a" * 64,
        "suppress": True,
        "self_mute": False,
        "self_deaf": False,
        "server_mute": False,
        "server_deaf": False,
        "request_to_speak_timestamp": "2026-08-28T12:00:00+00:00",
        "can_speak": False,
        "can_stream": False,
        "joined_at": 1_777_000_000,
    }
    payload.update(overrides)
    return payload


def last_await(mock: AsyncMock) -> Any:
    call = mock.await_args
    assert call is not None
    return call


@pytest.mark.asyncio
async def test_stage_instance_lifecycle_uses_bot_routes_and_typed_model() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[stage_payload(), stage_payload(), stage_payload(topic="Q&A"), None]
    )

    created = await bot.create_stage_instance(
        CHANNEL,
        "  Town hall  ",
        target=TARGET,
        send_start_notification=True,
        scheduled_event=EVENT,
        reason="open stage",
    )
    fetched = await bot.fetch_stage_instance(CHANNEL, target=TARGET)
    edited = await fetched.edit(topic=" Q&A ", reason="next segment")
    await edited.delete(reason="finished")

    assert isinstance(created, StageInstance)
    assert created.ref == EntityRef(60, "chat.example")
    assert created.guild_ref == GUILD
    assert created.channel_ref == CHANNEL
    assert created.scheduled_event_ref == EVENT
    assert bot.request.await_args_list[0].args[:2] == (
        "POST",
        "/api/v1/bots/stage-instances",
    )
    assert bot.request.await_args_list[0].kwargs["json"] == {
        "channel_id": "30@chat.example",
        "topic": "Town hall",
        "privacy_level": 2,
        "send_start_notification": True,
        "guild_scheduled_event_id": "50@chat.example",
    }
    assert bot.request.await_args_list[2].args[:2] == (
        "PATCH",
        "/api/v1/bots/stage-instances/30@chat.example",
    )
    assert bot.request.await_args_list[2].kwargs["json"] == {"topic": "Q&A"}
    assert bot.request.await_args_list[3].args[:2] == (
        "DELETE",
        "/api/v1/bots/stage-instances/30@chat.example",
    )
    with pytest.raises(ValueError, match="privacy"):
        StageInstance.from_payload(bot, TARGET, stage_payload(privacy_level=True))
    with pytest.raises(ValueError, match="privacy"):
        await bot.create_stage_instance(
            CHANNEL,
            "Invalid",
            privacy_level=True,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_stage_voice_state_get_self_request_and_moderation_routes() -> None:
    bot = client()
    state_payload = voice_state_payload()
    current_payload = voice_state_payload(
        user_id=str(BOT_USER.id),
        user_domain=BOT_USER.domain,
    )
    bot._bot_user_refs["https://chat.example"] = BOT_USER  # noqa: SLF001
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[current_payload, state_payload, None, None, None, None]
    )
    requested_at = datetime(2026, 8, 28, 12, 30, tzinfo=UTC)

    current = await bot.fetch_current_stage_voice_state(GUILD, target=TARGET)
    member = await bot.fetch_stage_voice_state(GUILD, USER, target=TARGET)
    await bot.request_to_speak(
        GUILD,
        target=TARGET,
        channel=CHANNEL,
        requested_at=requested_at,
    )
    await bot.clear_request_to_speak(GUILD, target=TARGET, channel=CHANNEL)
    await bot.promote_stage_speaker(GUILD, USER, channel=CHANNEL, target=TARGET)
    await bot.move_stage_user_to_audience(GUILD, USER, channel=CHANNEL, target=TARGET)

    assert isinstance(current, StageVoiceState)
    assert current.user_ref == BOT_USER
    assert current.suppress
    assert current.request_to_speak_at == datetime(2026, 8, 28, 12, tzinfo=UTC)
    assert member.channel_ref == CHANNEL
    assert bot.request.await_args_list[0].args[1].endswith("/voice-states/@me")
    assert (
        bot.request.await_args_list[1]
        .args[1]
        .endswith("/voice-states/40@people.example")
    )
    assert bot.request.await_args_list[2].kwargs["json"] == {
        "channel_id": "30@chat.example",
        "request_to_speak_timestamp": requested_at.isoformat(),
    }
    assert bot.request.await_args_list[3].kwargs["json"] == {
        "channel_id": "30@chat.example",
        "request_to_speak_timestamp": None,
    }
    assert bot.request.await_args_list[4].kwargs["json"]["suppress"] is False
    assert bot.request.await_args_list[5].kwargs["json"]["suppress"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"guild_id": "11"},
        {"guild_domain": "elsewhere.example"},
        {"channel_domain": "elsewhere.example"},
        {"user_id": "41"},
        {"user_domain": "elsewhere.example"},
    ],
)
async def test_stage_voice_state_rejects_response_lineage_substitution(
    overrides: dict[str, object],
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=voice_state_payload(**overrides)
    )

    with pytest.raises(ValueError, match="requested lineage"):
        await bot.fetch_stage_voice_state(GUILD, USER, target=TARGET)


@pytest.mark.asyncio
async def test_current_stage_voice_state_requires_authenticated_bot_lineage() -> None:
    bot = client()
    payload = voice_state_payload(
        user_id=str(BOT_USER.id),
        user_domain=BOT_USER.domain,
    )
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="authenticated bot lineage"):
        await bot.fetch_current_stage_voice_state(GUILD, target=TARGET)

    bot._bot_user_refs["https://chat.example"] = BOT_USER  # noqa: SLF001
    payload["user_id"] = "71"
    with pytest.raises(ValueError, match="requested lineage"):
        await bot.fetch_current_stage_voice_state(GUILD, target=TARGET)


@pytest.mark.asyncio
async def test_stage_sdk_rejects_ambiguous_or_invalid_updates() -> None:
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="1 and 120"):
        await bot.create_stage_instance(CHANNEL, "   ", target=TARGET)
    with pytest.raises(ValueError, match="at least one"):
        await bot.edit_stage_instance(CHANNEL, target=TARGET)
    with pytest.raises(ValueError, match="requires a timezone"):
        await bot.update_current_stage_voice_state(
            GUILD,
            target=TARGET,
            request_to_speak_at=datetime(2026, 8, 28, 12, 30),
        )
    with pytest.raises(ValueError, match="suppress or request"):
        await bot.update_current_stage_voice_state(
            GUILD,
            target=TARGET,
            channel=CHANNEL,
        )
    bot.request.assert_not_awaited()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (stage_payload(topic=7), "Stage topic"),
        (stage_payload(discoverable_disabled="false"), "discoverable_disabled"),
        (stage_payload(id=True), "wire entity IDs"),
    ],
)
def test_stage_instance_payload_rejects_type_coercion(
    payload: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        StageInstance.from_payload(client(), TARGET, payload)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"suppress": "false"}, "suppress"),
        ({"self_mute": 0}, "self_mute"),
        ({"self_deaf": None}, "self_deaf"),
        ({"server_mute": "false"}, "server_mute"),
        ({"server_deaf": 0}, "server_deaf"),
        ({"can_speak": "false"}, "can_speak"),
        ({"can_stream": 1}, "can_stream"),
        ({"joined_at": "1777000000"}, "joined_at"),
        ({"session_id": 7}, "session ID"),
        ({"user_id": True}, "wire entity IDs"),
        ({"suppress": True, "suppressed": False}, "suppress"),
    ],
)
def test_stage_voice_payload_rejects_type_or_alias_coercion(
    overrides: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        StageVoiceState.from_payload(voice_state_payload(**overrides))


@pytest.mark.asyncio
async def test_stage_gateway_mutations_dispatch_typed_instances() -> None:
    bot = client()
    seen: list[StageInstance] = []

    @bot.event
    async def on_stage_instance_update(event: StageInstance) -> None:
        seen.append(event)

    await bot.dispatch(
        "STAGE_INSTANCE_UPDATE",
        stage_payload(),
        target="https://chat.example",
        topic=f"guild:{GUILD.domain}:{GUILD.id}",
    )

    assert len(seen) == 1
    assert seen[0].channel_ref == CHANNEL
    assert seen[0].guild_ref == GUILD
