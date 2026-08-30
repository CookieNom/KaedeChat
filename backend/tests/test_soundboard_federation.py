from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Response
from pydantic import ValidationError

import app.api.soundboard as soundboard_api
from app.api.soundboard import (
    FederatedSoundboardSound,
    SoundboardFederationCapability,
    SoundboardFederationPage,
    SoundboardFederationPlay,
    SoundboardFederationRef,
    SoundboardFederationRequest,
    SoundboardSourceCapability,
    SoundboardSourceCapabilityRequest,
)
from app.core.federation import FEDERATION_CAPABILITIES
from app.db.models import Guild, SoundboardSound, User
from app.federation.client import silence_blocks_path
from app.federation.security import FederationPrincipal
from app.voice.schemas import SoundboardPlayRequest
from app.voice.state import Occupant


def ref(entity_id: int, domain: str = "authority.example") -> SoundboardFederationRef:
    return SoundboardFederationRef(id=str(entity_id), domain=domain)


def request_payload(
    operation: str = "list",
    *,
    caller_kind: str = "human",
) -> SoundboardFederationRequest:
    now = int(time.time())
    return SoundboardFederationRequest.model_validate(
        {
            "guild": ref(10),
            "caller": {
                "kind": caller_kind,
                "user": ref(20, "member.example"),
                "application": ref(30, "member.example") if caller_kind == "bot" else None,
            },
            "requesting_instance": "member.example",
            "request_id": "kasb_" + "a" * 32,
            "issued_at": now,
            "deadline": now + 10,
            "operation": operation,
            "sound": ref(40) if operation in {"get", "play"} else None,
            "channel": ref(50) if operation == "play" else None,
            "sound_version": "1" if operation == "play" else None,
            "volume": 0.75 if operation == "play" else None,
            "actor_intent": {"proof": "test"} if operation == "play" else None,
        }
    )


@pytest.mark.parametrize("field", ["issued_at", "deadline"])
def test_soundboard_federation_timestamps_reject_boolean_coercion(field: str) -> None:
    payload = request_payload().model_dump(mode="json")
    payload[field] = True
    with pytest.raises(ValidationError, match=f"{field} must be an integer"):
        SoundboardFederationRequest.model_validate(payload)


def sound_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "40",
        "origin_domain": "authority.example",
        "guild_id": "10",
        "guild_domain": "authority.example",
        "name": "Air horn",
        "media_hash": "a" * 64,
        "content_type": "audio/ogg",
        "volume": 0.8,
        "emoji_id": None,
        "emoji_domain": None,
        "emoji_name": "📣",
        "available": True,
        "duration_ms": 1_500,
        "created_by_id": "20",
        "created_by_domain": "member.example",
        "version": "1",
    }
    payload.update(updates)
    return payload


def default_catalog() -> list[dict[str, object]]:
    digest = "d" * 64
    return [
        {
            "sound_id": "41",
            "name": "Quack",
            "media_hash": digest,
            "content_type": "audio/ogg",
            "download_url": f"https://media.voice.example/defaults/{digest}.ogg",
            "volume": 0.8,
            "emoji_name": "🦆",
            "duration_ms": 900,
        }
    ]


def play_payload() -> SoundboardFederationPlay:
    request = request_payload("play")
    return SoundboardFederationPlay(
        request=request,
        capability=SoundboardFederationCapability(
            sound=FederatedSoundboardSound.model_validate(sound_payload()),
            download_url="https://media.authority.example/sound?signature=secret",
            media_authority="authority.example",
            media_origin="https://media.authority.example",
            effective_volume=0.6,
            expires_in=60,
        ),
        guild=request.guild,
        channel=ref(50),
        user=request.caller.user,
        delivery_id="kase_" + "b" * 32,
    )


def test_federation_request_is_strict_and_operation_shaped() -> None:
    request_payload("list")
    request_payload("get")
    request_payload("play", caller_kind="bot")

    malformed = request_payload("list").model_dump(mode="json")
    malformed["sound"] = ref(40).model_dump(mode="json")
    with pytest.raises(ValidationError, match="do not match its operation"):
        SoundboardFederationRequest.model_validate(malformed)

    bot_without_application = request_payload("list").model_dump(mode="json")
    bot_without_application["caller"]["kind"] = "bot"
    with pytest.raises(ValidationError, match="application reference"):
        SoundboardFederationRequest.model_validate(bot_without_application)

    excessive_deadline = request_payload("list").model_dump(mode="json")
    excessive_deadline["deadline"] = excessive_deadline["issued_at"] + 11
    with pytest.raises(ValidationError, match="deadline is invalid"):
        SoundboardFederationRequest.model_validate(excessive_deadline)


def test_default_soundboard_catalog_is_immutable_and_guildless() -> None:
    settings = SimpleNamespace(
        domain="voice.example",
        environment="production",
        soundboard_default_sounds=default_catalog(),
    )
    [sound] = soundboard_api._default_soundboard_catalog(settings)
    payload = soundboard_api._default_sound_payload(settings, sound)
    validated = FederatedSoundboardSound.model_validate(payload)
    assert validated.guild_id is None
    assert validated.created_by_id is None
    assert validated.origin_domain == "voice.example"

    settings.soundboard_default_sounds[0]["download_url"] = (
        "https://media.voice.example/defaults/mutable.ogg"
    )
    with pytest.raises(HTTPException) as raised:
        soundboard_api._default_soundboard_catalog(settings)
    assert raised.value.detail == {"code": "DEFAULT_SOUNDBOARD_CATALOG_INVALID"}


@pytest.mark.asyncio
async def test_soundboard_creator_is_only_returned_to_expression_managers() -> None:
    sound = SoundboardSound(
        id=40,
        origin_domain="authority.example",
        guild_id=10,
        guild_domain="authority.example",
        name="Air horn",
        media_hash="a" * 64,
        object_key="soundboard/air-horn.ogg",
        content_type="audio/ogg",
        volume=0.8,
        available=True,
        duration_ms=1_500,
        created_by_id=20,
        created_by_domain="member.example",
        version=1,
    )
    creator = User(
        id=20,
        origin_domain="member.example",
        username="creator",
        account_type="human",
        is_local=False,
        profile_version=7,
        e2ee_device_generation=9,
        profile_resolved=True,
        federation_introduced_by_domain="member.example",
    )
    session = SimpleNamespace(scalars=AsyncMock(return_value=[creator]))

    [hidden] = await soundboard_api._render_guild_sounds(
        session,
        [sound],
        include_creators=False,
    )
    assert hidden["created_by_id"] is None
    assert hidden["created_by_domain"] is None
    assert "user" not in hidden
    session.scalars.assert_not_awaited()

    [visible] = await soundboard_api._render_guild_sounds(
        session,
        [sound],
        include_creators=True,
    )
    assert visible["created_by_id"] == "20"
    assert visible["user"]["id"] == "20"
    assert visible["user"]["origin_domain"] == "member.example"
    assert visible["user"]["profile_version"] == "7"
    assert visible["user"]["e2ee_device_generation"] == "9"

    [federated] = await soundboard_api._render_guild_sounds(
        session,
        [sound],
        include_creators=True,
        federated=True,
    )
    request = request_payload("list")
    page = SoundboardFederationPage.model_validate({"request": request, "sounds": [federated]})
    assert page.sounds[0].user is not None
    assert page.sounds[0].user.profile_version == 7
    assert page.sounds[0].user.e2ee_device_generation == 9
    assert type(federated["user"]["profile_version"]) is int
    assert type(federated["user"]["e2ee_device_generation"]) is int
    [client_projection] = soundboard_api._validate_federation_page(
        page,
        request,
    )
    assert client_projection["user"]["profile_version"] == "7"
    assert client_projection["user"]["e2ee_device_generation"] == "9"
    assert client_projection["user"]["handle"] == "creator@member.example"
    assert client_projection["user"]["profile_resolved"] is True
    assert client_projection["user"]["bot"] is False
    assert not soundboard_api._can_view_soundboard_creators(0)
    assert soundboard_api._can_view_soundboard_creators(
        int(soundboard_api.Permission.CREATE_GUILD_EXPRESSIONS)
    )


def test_query_response_rejects_cross_guild_substitution_and_duplicates() -> None:
    request = request_payload("list")
    page = SoundboardFederationPage(
        request=request,
        sounds=[FederatedSoundboardSound.model_validate(sound_payload())],
    )
    assert soundboard_api._validate_federation_page(page, request)[0]["id"] == "40"

    substituted = page.model_copy(
        update={"sounds": [FederatedSoundboardSound.model_validate(sound_payload(guild_id="11"))]}
    )
    with pytest.raises(ValueError, match="another guild"):
        soundboard_api._validate_federation_page(substituted, request)

    duplicate = page.model_copy(update={"sounds": [page.sounds[0], page.sounds[0]]})
    with pytest.raises(ValueError, match="duplicate"):
        soundboard_api._validate_federation_page(duplicate, request)


def test_play_response_is_exactly_bound_and_rejects_unsafe_media() -> None:
    play = play_payload()
    settings = SimpleNamespace(environment="production")
    capability = soundboard_api._validate_federation_play(play, play.request, settings)
    assert capability["sound"]["id"] == "40"

    substituted = play.model_copy(update={"channel": ref(51)})
    with pytest.raises(ValueError, match="substituted room"):
        soundboard_api._validate_federation_play(substituted, play.request, settings)

    unsafe_capability = play.capability.model_copy(
        update={"download_url": "http://media.authority.example/sound"}
    )
    unsafe = play.model_copy(update={"capability": unsafe_capability})
    with pytest.raises(ValueError, match="unsafe media capability"):
        soundboard_api._validate_federation_play(unsafe, play.request, settings)

    external_storage = play.model_copy(
        update={
            "capability": play.capability.model_copy(
                update={
                    "download_url": (
                        "https://kaede-sounds.s3.us-west-004.backblazeb2.com/sound?signature=secret"
                    ),
                    "media_origin": ("https://kaede-sounds.s3.us-west-004.backblazeb2.com"),
                }
            )
        }
    )
    accepted = soundboard_api._validate_federation_play(
        external_storage,
        play.request,
        settings,
    )
    assert accepted["media_origin"] == ("https://kaede-sounds.s3.us-west-004.backblazeb2.com")

    for hostile_url in (
        "https://127.0.0.1/latest/meta-data",
        "https://metadata.internal/sound",
        "https://media.authority.example.attacker.test/sound",
        "https://media.authority.example:8443/sound",
    ):
        hostile = play.model_copy(
            update={"capability": play.capability.model_copy(update={"download_url": hostile_url})}
        )
        with pytest.raises(ValueError, match="unsafe media capability"):
            soundboard_api._validate_federation_play(hostile, play.request, settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["list", "play"])
async def test_soundboard_response_requires_current_remote_owner_signer(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    request = request_payload(operation)
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Transferred guild",
        owner_id=99,
        owner_domain="owner.example",
    )
    envelope = SimpleNamespace(
        type=(
            soundboard_api.SOUNDBOARD_FEDERATION_PLAY_EVENT
            if operation == "play"
            else soundboard_api.SOUNDBOARD_FEDERATION_QUERY_EVENT
        ),
        actor=SimpleNamespace(id="98", domain="stale-owner.example"),
        context={
            "guild_id": request.guild.id,
            "guild_domain": request.guild.domain,
            **(
                {
                    "channel_id": request.channel.id,
                    "channel_domain": request.channel.domain,
                }
                if request.channel is not None
                else {}
            ),
        },
        ts=int(time.time() * 1_000),
        content={"bound": True},
    )
    validated = AsyncMock(return_value=envelope)
    monkeypatch.setattr(soundboard_api, "validated_event_envelope", validated)
    session = SimpleNamespace(get=AsyncMock(return_value=guild))
    settings = SimpleNamespace(federation_clock_skew_seconds=60)

    with pytest.raises(ValueError, match="current guild owner"):
        await soundboard_api._validated_soundboard_envelope(
            session,
            settings,
            request,
            {},
            event_type=envelope.type,
        )

    envelope.actor = SimpleNamespace(id="99", domain="owner.example")
    content = await soundboard_api._validated_soundboard_envelope(
        session,
        settings,
        request,
        {},
        event_type=envelope.type,
    )
    assert content == {"bound": True}
    assert validated.await_args.kwargs["allow_authority_attested_actor"] is True


@pytest.mark.asyncio
async def test_soundboard_query_is_signed_by_remote_owner_after_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = request_payload("list")
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Transferred guild",
        owner_id=99,
        owner_domain="owner.example",
    )
    owner = User(
        id=99,
        origin_domain="owner.example",
        username="remote-owner",
        is_local=False,
    )
    actor = User(id=20, origin_domain="member.example", username="member", is_local=False)
    monkeypatch.setattr(
        soundboard_api,
        "_authorize_federated_soundboard_request",
        AsyncMock(return_value=(guild, actor, None)),
    )
    monkeypatch.setattr(soundboard_api, "get_permissions", AsyncMock(return_value=0))
    monkeypatch.setattr(soundboard_api, "_list_guild_sounds", AsyncMock(return_value=[]))
    monkeypatch.setattr(soundboard_api, "guild_authority_owner", AsyncMock(return_value=owner))
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(soundboard_api, "build_guild_authority_envelope", signed)
    monkeypatch.setattr(soundboard_api, "enforce_federation_route_rate_limit", AsyncMock())

    result = await soundboard_api.federation_soundboard_query(
        10,
        request,
        FederationPrincipal(origin="member.example", key_id="key"),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="authority.example"),
    )

    assert result == {"signed": True}
    assert signed.await_args.args[2] is guild
    assert signed.await_args.args[4] is owner


@pytest.mark.asyncio
async def test_soundboard_play_is_signed_by_remote_owner_after_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = request_payload("play")
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Transferred guild",
        owner_id=99,
        owner_domain="owner.example",
    )
    owner = User(
        id=99,
        origin_domain="owner.example",
        username="remote-owner",
        is_local=False,
    )
    monkeypatch.setattr(soundboard_api, "room_occupants", AsyncMock(return_value=[]))
    monkeypatch.setattr(soundboard_api, "guild_authority_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(
        soundboard_api,
        "_deliver_soundboard_effect_to_local_occupants",
        AsyncMock(return_value=True),
    )
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(soundboard_api, "build_guild_authority_envelope", signed)

    _play, envelope = await soundboard_api._authoritative_soundboard_play_envelope(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(
            domain="authority.example",
            environment="production",
        ),
        guild,
        request,
        play_payload().capability.model_dump(mode="json"),
        BackgroundTasks(),
        SimpleNamespace(),
    )

    assert envelope == {"signed": True}
    assert signed.await_args.args[2] is guild
    assert signed.await_args.args[4] is owner


@pytest.mark.asyncio
async def test_third_instance_source_capability_is_exactly_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    request = SoundboardSourceCapabilityRequest(
        source_guild=ref(70, "source.example"),
        target_guild=ref(10, "voice.example"),
        target_channel=ref(50, "voice.example"),
        sound=ref(40, "source.example"),
        sound_version="1",
        caller={"kind": "human", "user": ref(20, "member.example")},
        requesting_instance="voice.example",
        request_id="kasc_" + "c" * 32,
        issued_at=now,
        deadline=now + 10,
        volume=0.5,
        actor_intent={"proof": "test"},
    )
    capability = SoundboardFederationCapability(
        sound=FederatedSoundboardSound.model_validate(
            sound_payload(
                origin_domain="source.example",
                guild_id="70",
                guild_domain="source.example",
            )
        ),
        download_url="https://media.source.example/sounds/40?signature=secret",
        media_authority="source.example",
        media_origin="https://media.source.example",
        effective_volume=0.4,
        expires_in=60,
    )
    proof = SoundboardSourceCapability(request=request, capability=capability)
    envelope = SimpleNamespace(
        type=soundboard_api.SOUNDBOARD_SOURCE_CAPABILITY_EVENT,
        context={
            "guild_id": "70",
            "guild_domain": "source.example",
            "target_guild_id": "10",
            "target_guild_domain": "voice.example",
            "target_channel_id": "50",
            "target_channel_domain": "voice.example",
        },
        ts=now * 1_000,
        content=proof.model_dump(mode="json"),
    )
    monkeypatch.setattr(
        soundboard_api,
        "validated_event_envelope",
        AsyncMock(return_value=envelope),
    )
    settings = SimpleNamespace(
        environment="production",
        federation_clock_skew_seconds=60,
    )
    validated = await soundboard_api._validated_source_capability_envelope(
        SimpleNamespace(),
        settings,
        request,
        {},
    )
    assert validated.media_authority == "source.example"

    substituted = proof.model_copy(
        update={
            "capability": capability.model_copy(
                update={
                    "download_url": "https://media.voice.example/sounds/40",
                }
            )
        }
    )
    envelope.content = substituted.model_dump(mode="json")
    with pytest.raises(ValueError, match="unsafe media capability"):
        await soundboard_api._validated_source_capability_envelope(
            SimpleNamespace(),
            settings,
            request,
            {},
        )


@pytest.mark.asyncio
async def test_source_validates_receiver_bound_intent_not_relay_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = int(time.time())
    request = SoundboardSourceCapabilityRequest(
        source_guild=ref(70, "source.example"),
        target_guild=ref(10, "voice.example"),
        target_channel=ref(50, "voice.example"),
        sound=ref(40, "source.example"),
        sound_version="4",
        caller={"kind": "human", "user": ref(20, "member.example")},
        requesting_instance="voice.example",
        request_id="kasc_" + "d" * 32,
        issued_at=now,
        deadline=now + 10,
        volume=0.5,
        actor_intent={"signed": "by-member-home"},
    )
    validate = AsyncMock(side_effect=RuntimeError("stop after actor proof"))
    monkeypatch.setattr(soundboard_api, "_validate_soundboard_actor_intent", validate)
    monkeypatch.setattr(
        soundboard_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="stop after actor proof"):
        await soundboard_api.federation_soundboard_source_capability(
            70,
            request,
            FederationPrincipal(origin="voice.example", key_id="ed25519:test"),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(
                domain="source.example",
                federation_clock_skew_seconds=60,
            ),
        )

    assert validate.await_args.kwargs["audience"] == "source.example"
    assert validate.await_args.kwargs["runtime_target_domain"] == "voice.example"
    assert validate.await_args.kwargs["source_guild"] == (70, "source.example")
    assert validate.await_args.kwargs["target_channel"] == (50, "voice.example")
    assert validate.await_args.kwargs["sound"] == (40, "source.example")


@pytest.mark.asyncio
async def test_target_authority_fetches_remote_source_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=20,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    guild = Guild(
        id=10,
        origin_domain="voice.example",
        name="Voice guild",
        owner_id=99,
        owner_domain="voice.example",
    )
    channel = SimpleNamespace(id=50, origin_domain="voice.example")
    caller = soundboard_api._federation_caller(actor)
    permission_mask = (
        soundboard_api.Permission.VIEW_CHANNEL
        | soundboard_api.Permission.CONNECT
        | soundboard_api.Permission.SPEAK
        | soundboard_api.Permission.USE_SOUNDBOARD
        | soundboard_api.Permission.USE_EXTERNAL_SOUNDS
    )
    monkeypatch.setattr(
        soundboard_api,
        "get_permissions",
        AsyncMock(return_value=permission_mask),
    )
    monkeypatch.setattr(
        soundboard_api,
        "room_occupants",
        AsyncMock(
            return_value=[
                Occupant(
                    identity="20@member.example",
                    user_id="20",
                    user_domain="member.example",
                    room="g.10.50",
                    guild_id="10",
                    channel_id="50",
                    joined_at=1,
                    can_speak=True,
                    allow_speak=True,
                )
            ]
        ),
    )
    monkeypatch.setattr(soundboard_api, "enforce_keyed_rate_limit", AsyncMock())
    capability = {
        "sound": sound_payload(
            origin_domain="source.example",
            guild_id="70",
            guild_domain="source.example",
        ),
        "download_url": "https://media.source.example/sounds/40?signature=secret",
        "media_authority": "source.example",
        "media_origin": "https://media.source.example",
        "effective_volume": 0.4,
        "expires_in": 60,
    }
    fetch = AsyncMock(return_value=capability)
    monkeypatch.setattr(soundboard_api, "_request_remote_sound_capability", fetch)
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    result = await soundboard_api._soundboard_play_capability(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="voice.example"),
        channel,
        guild,
        actor,
        SoundboardPlayRequest(
            sound_id=soundboard_api.EntityRef("40@source.example"),
            source_guild_id=soundboard_api.EntityRef("70@source.example"),
            actor_intent={"proof": "test"},
        ),
        Response(),
        caller=caller,
    )
    assert result["media_authority"] == "source.example"
    source_request = fetch.await_args.args[2]
    assert source_request.source_guild == ref(70, "source.example")
    assert source_request.target_guild == ref(10, "voice.example")
    assert source_request.caller.user == ref(20, "member.example")


@pytest.mark.asyncio
async def test_authority_rechecks_bot_install_scope_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = request_payload("play", caller_kind="bot")
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Guild",
        owner_id=99,
        owner_domain="authority.example",
    )
    bot = User(
        id=20,
        origin_domain="member.example",
        username="bot",
        account_type="bot",
        is_local=False,
    )
    installation = SimpleNamespace(
        granted_scopes=["soundboard.read"],
        grant_revision=3,
    )

    class Session:
        async def get(self, model: object, key: object) -> object | None:
            del model
            return guild if key == (10, "authority.example") else bot

        scalar = AsyncMock(return_value=installation)

    redis = SimpleNamespace(set=AsyncMock(return_value=True))
    settings = SimpleNamespace(
        domain="authority.example",
        federation_clock_skew_seconds=60,
    )
    principal = FederationPrincipal(origin="member.example", key_id="key")
    monkeypatch.setattr(soundboard_api, "usable_guild_installation", lambda: True)
    monkeypatch.setattr(
        soundboard_api,
        "_validate_soundboard_actor_intent",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as raised:
        await soundboard_api._authorize_federated_soundboard_request(
            Session(),
            redis,
            settings,
            principal,
            10,
            payload,
            scopes={"play": "soundboard.use"},
        )
    assert raised.value.detail == {"code": "BOT_SCOPE_REQUIRED", "scope": "soundboard.use"}

    installation.granted_scopes.append("soundboard.use")
    payload = payload.model_copy(update={"request_id": "kasb_" + "c" * 32})
    (
        authorized_guild,
        actor,
        authorized_installation,
    ) = await soundboard_api._authorize_federated_soundboard_request(
        Session(),
        redis,
        settings,
        principal,
        10,
        payload,
        scopes={"play": "soundboard.use"},
    )
    assert authorized_guild is guild
    assert actor is bot
    assert authorized_installation is installation

    downgraded = request_payload("play").model_copy(update={"request_id": "kasb_" + "d" * 32})
    with pytest.raises(HTTPException) as raised:
        await soundboard_api._authorize_federated_soundboard_request(
            Session(),
            redis,
            settings,
            principal,
            10,
            downgraded,
            scopes={"play": "soundboard.use"},
        )
    assert raised.value.detail == {"code": "KAED_FED_SOUNDBOARD_CALLER_KIND_MISMATCH"}


@pytest.mark.asyncio
async def test_remote_human_query_uses_authority_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Guild",
        owner_id=99,
        owner_domain="authority.example",
    )
    actor = User(
        id=20,
        origin_domain="member.example",
        username="member",
        is_local=True,
    )
    request = request_payload("list")
    page = SoundboardFederationPage(
        request=request,
        sounds=[FederatedSoundboardSound.model_validate(sound_payload())],
    )

    class Session:
        async def get(self, model: object, key: object) -> object | None:
            del model
            return guild if key == (10, "authority.example") else SimpleNamespace()

    monkeypatch.setattr(soundboard_api, "_new_federation_request", lambda *a, **k: request)
    proxy = AsyncMock(return_value=page)
    monkeypatch.setattr(soundboard_api, "_request_remote_soundboard", proxy)
    result = await soundboard_api.list_human_soundboard_sounds(
        soundboard_api.EntityRef("10@authority.example"),
        SimpleNamespace(user=actor),
        Session(),
        SimpleNamespace(),
        SimpleNamespace(domain="member.example"),
    )
    assert result["items"][0]["id"] == "40"
    proxy.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_bot_play_uses_authority_and_local_occupant_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Guild",
        owner_id=99,
        owner_domain="authority.example",
    )
    actor = User(
        id=20,
        origin_domain="member.example",
        username="bot",
        account_type="bot",
        is_local=True,
    )
    channel = SimpleNamespace(
        id=50,
        origin_domain="authority.example",
        guild_id=10,
        guild_domain="authority.example",
        type=2,
    )
    installation = soundboard_api.BotInstallation(grant_revision=3)
    request = request_payload("play", caller_kind="bot")
    play = play_payload().model_copy(update={"request": request, "user": request.caller.user})
    monkeypatch.setattr(
        soundboard_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel, installation)),
    )
    monkeypatch.setattr(soundboard_api, "_new_federation_request", lambda *a, **k: request)
    monkeypatch.setattr(soundboard_api, "_request_remote_soundboard", AsyncMock(return_value=play))
    monkeypatch.setattr(
        soundboard_api,
        "_validate_soundboard_actor_intent",
        AsyncMock(),
    )
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(
        soundboard_api,
        "_deliver_soundboard_effect_to_local_occupants",
        deliver,
    )

    class Session:
        get = AsyncMock(return_value=guild)

    principal = SimpleNamespace(
        user=actor,
        application=SimpleNamespace(id=30, origin_domain="member.example"),
    )
    result = await soundboard_api.authorize_soundboard_play(
        soundboard_api.EntityRef("50@authority.example"),
        SoundboardPlayRequest(
            sound_id=soundboard_api.EntityRef("40@authority.example"),
            actor_intent={"proof": "test"},
        ),
        Response(),
        BackgroundTasks(),
        SimpleNamespace(),
        principal,
        Session(),
        SimpleNamespace(),
        SimpleNamespace(domain="member.example", environment="production"),
    )
    assert result.status_code == 204
    deliver.assert_awaited_once()


@pytest.mark.asyncio
async def test_effect_delivery_is_occupant_only_and_replay_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    play = play_payload()
    occupants = [
        Occupant(
            identity="21@member.example",
            user_id="21",
            user_domain="member.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=1,
        ),
        Occupant(
            identity="22@other.example",
            user_id="22",
            user_domain="other.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=2,
        ),
        Occupant(
            identity="23@apps.example",
            user_id="23",
            user_domain="apps.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=3,
            client_kind="bot",
        ),
    ]
    monkeypatch.setattr(soundboard_api, "room_occupants", AsyncMock(return_value=occupants))
    published = AsyncMock()
    monkeypatch.setattr(soundboard_api, "publish_dispatch", published)
    redis = SimpleNamespace(set=AsyncMock(side_effect=[True, False]))
    settings = SimpleNamespace(domain="member.example")

    assert await soundboard_api._deliver_soundboard_effect_to_local_occupants(
        redis,
        settings,
        play,
        authority_domain="authority.example",
        consume_replay=True,
    )
    assert published.await_count == 2
    assert [call.args[1:3] for call in published.await_args_list] == [
        ("user:member.example:21", "VOICE_CHANNEL_EFFECT_SEND"),
        ("user:apps.example:23", "VOICE_CHANNEL_EFFECT_SEND"),
    ]

    assert await soundboard_api._deliver_soundboard_effect_to_local_occupants(
        redis,
        settings,
        play,
        authority_domain="authority.example",
        consume_replay=True,
    )
    assert published.await_count == 2


def test_effect_routes_only_federate_remote_human_domains() -> None:
    occupants = [
        Occupant(
            identity="21@member.example",
            user_id="21",
            user_domain="member.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=1,
        ),
        Occupant(
            identity="22@other.example",
            user_id="22",
            user_domain="other.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=2,
        ),
        Occupant(
            identity="23@apps.example",
            user_id="23",
            user_domain="apps.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=3,
            client_kind="bot",
        ),
        Occupant(
            identity="24@other.example",
            user_id="24",
            user_domain="other.example",
            room="g.10.50",
            guild_id="10",
            channel_id="50",
            joined_at=4,
            client_kind="bot",
        ),
    ]

    local_users, destinations = soundboard_api._soundboard_effect_routes(
        occupants,
        "member.example",
    )

    assert local_users == [
        (21, "member.example"),
        (23, "apps.example"),
        (24, "other.example"),
    ]
    assert destinations == ["other.example"]


@pytest.mark.parametrize("operation", ["update", "delete"])
@pytest.mark.parametrize("creator_id", [20, 21], ids=["creator", "noncreator"])
@pytest.mark.asyncio
async def test_soundboard_mutations_delegate_creator_aware_permission(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    creator_id: int,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="authority.example",
        name="Guild",
        owner_id=99,
        owner_domain="authority.example",
    )
    actor = User(
        id=20,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    sound = SimpleNamespace(created_by_id=creator_id, created_by_domain="member.example")
    monkeypatch.setattr(soundboard_api, "_sound_for_guild", AsyncMock(return_value=sound))

    class PermissionChecked(Exception):
        pass

    permission = AsyncMock(side_effect=PermissionChecked)
    monkeypatch.setattr(soundboard_api, "require_can_manage_expression", permission)
    with pytest.raises(PermissionChecked):
        if operation == "update":
            await soundboard_api._update_soundboard_sound(
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(domain="authority.example"),
                guild,
                actor,
                SimpleNamespace(),
                SimpleNamespace(),
                reason=None,
            )
        else:
            await soundboard_api._delete_soundboard_sound(
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(),
                SimpleNamespace(domain="authority.example"),
                guild,
                actor,
                SimpleNamespace(),
                reason=None,
            )
    assert permission.await_args.kwargs == {
        "creator_id": creator_id,
        "creator_domain": "member.example",
    }


def test_federation_routes_are_registered() -> None:
    methods = {
        (route.path, method)
        for route in soundboard_api.federation_router.routes
        for method in (route.methods or set())
    }
    assert ("/_kaede/v1/guilds/{guild_id}/soundboard/query", "POST") in methods
    assert ("/_kaede/v1/guilds/{guild_id}/soundboard/play", "POST") in methods
    assert ("/_kaede/v1/voice/soundboard-effect", "POST") in methods
    assert "guild-soundboard/1" in FEDERATION_CAPABILITIES
    assert silence_blocks_path("/_kaede/v1/guilds/10/soundboard/query")
    assert silence_blocks_path("/_kaede/v1/guilds/10/soundboard/play")
    assert silence_blocks_path("/_kaede/v1/voice/soundboard-effect")
