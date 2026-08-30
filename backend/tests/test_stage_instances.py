from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint

from app.api.stage_instances import (
    StageInstanceCreate,
    StageInstancePatch,
    bot_get_stage_voice_state,
    bot_update_current_stage_voice_state,
    bot_update_stage_voice_state,
    get_local_stage_voice_state,
    stage_voice_state_payload,
    update_local_stage_voice_state,
)
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import GuildScheduledEvent, StageInstance
from app.federation.guild_management import (
    BOT_GUILD_MANAGEMENT_CONTRACTS,
    GuildManagementRequest,
)
from app.voice.permissions import (
    STAGE_INSTANCE_MODERATOR_PERMISSIONS,
    STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
    STAGE_VOICE_STATE_READ_PERMISSIONS,
)
from app.voice.schemas import (
    CurrentUserVoiceStateUpdate,
    UserVoiceStateUpdate,
    VoiceStateFederationRequest,
)
from app.voice.service import (
    authoritative_guild_token,
    is_stage_moderator,
    update_authoritative_occupant_grant,
    voice_activity_allowed,
    voice_speaking_allowed,
)
from app.voice.state import Occupant, federation_occupant_state, public_occupant_state


def settings() -> Settings:
    return Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
        voice_enabled=True,
        voice_api_key="LKtestkey",
        voice_api_secret="livekit-test-secret-000000000000000000000000000000000000000",
        voice_public_url="wss://alpha.localhost/livekit",
    )


def occupant(**overrides: object) -> Occupant:
    values: dict[str, object] = {
        "identity": "78@alpha.localhost",
        "user_id": "78",
        "user_domain": "alpha.localhost",
        "room": "g.12.34",
        "guild_id": "12",
        "channel_id": "34",
        "joined_at": 1,
        "connection_id": "c" * 43,
        "client_kind": "web",
        "suppressed": True,
        "can_speak": False,
        "can_stream": False,
        "participant_metadata": {
            "generation": 4,
            "connection_id": "c" * 43,
            "client_kind": "web",
            "user_id": "78",
            "user_domain": "alpha.localhost",
            "guild_id": "12",
            "channel_id": "34",
            "channel_domain": "alpha.localhost",
            "e2ee": False,
            "server_mute": False,
            "server_deaf": False,
            "suppressed": True,
            "request_to_speak_timestamp": None,
            "can_speak": False,
            "can_stream": False,
            "can_listen": True,
            "allow_speak": True,
            "allow_stream": True,
            "allow_listen": True,
            "can_use_vad": True,
        },
    }
    values.update(overrides)
    return Occupant(**values)  # type: ignore[arg-type]


def test_stage_models_match_discord_writable_contract() -> None:
    created = StageInstanceCreate(
        channel_id=EntityRef("34@alpha.localhost"),
        topic="  Release room  ",
        send_start_notification=True,
    )
    assert created.topic == "Release room"
    assert created.privacy_level == 2
    assert created.send_start_notification is True
    with pytest.raises(ValidationError):
        StageInstanceCreate.model_validate(
            {"channel_id": "34@alpha.localhost", "topic": "x", "privacy_level": 1}
        )
    with pytest.raises(ValidationError, match="must be an integer"):
        StageInstanceCreate.model_validate(
            {"channel_id": "34@alpha.localhost", "topic": "x", "privacy_level": True}
        )
    with pytest.raises(ValidationError, match="must be an integer"):
        StageInstancePatch.model_validate({"privacy_level": True})

    request = CurrentUserVoiceStateUpdate(
        request_to_speak_timestamp=(datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    )
    assert request.channel_id is None
    assert request.request_to_speak_timestamp is not None
    assert CurrentUserVoiceStateUpdate(request_to_speak_timestamp=None).model_fields_set == {
        "request_to_speak_timestamp"
    }
    with pytest.raises(ValidationError):
        CurrentUserVoiceStateUpdate(channel_id=EntityRef("34@alpha.localhost"))
    with pytest.raises(ValidationError):
        CurrentUserVoiceStateUpdate(suppress=None)
    assert UserVoiceStateUpdate(suppress=False).channel_id is None


def test_stage_schema_binds_channel_guild_and_scheduled_event_lineage() -> None:
    event_unique = next(
        constraint
        for constraint in GuildScheduledEvent.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_guild_scheduled_events_stage_lineage"
    )
    assert [column.name for column in event_unique.columns] == [
        "id",
        "origin_domain",
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
    ]

    foreign_keys = {
        constraint.name: constraint
        for constraint in StageInstance.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }
    channel_lineage = foreign_keys["fk_stage_instances_channel_guild_lineage"]
    assert [column.name for column in channel_lineage.columns] == [
        "channel_id",
        "channel_domain",
        "guild_id",
        "guild_domain",
    ]
    assert [element.target_fullname for element in channel_lineage.elements] == [
        "channels.id",
        "channels.origin_domain",
        "channels.guild_id",
        "channels.guild_domain",
    ]
    assert channel_lineage.ondelete == "CASCADE"

    event_lineage = foreign_keys["fk_stage_instances_scheduled_event_lineage"]
    assert [column.name for column in event_lineage.columns] == [
        "scheduled_event_id",
        "scheduled_event_domain",
        "guild_id",
        "guild_domain",
        "channel_id",
        "channel_domain",
    ]
    assert [element.target_fullname for element in event_lineage.elements] == [
        "guild_scheduled_events.id",
        "guild_scheduled_events.origin_domain",
        "guild_scheduled_events.guild_id",
        "guild_scheduled_events.guild_domain",
        "guild_scheduled_events.channel_id",
        "guild_scheduled_events.channel_domain",
    ]
    assert event_lineage.ondelete is None
    assert any(
        constraint.ondelete == "SET NULL"
        and [column.name for column in constraint.columns]
        == ["scheduled_event_id", "scheduled_event_domain"]
        for constraint in foreign_keys.values()
    )
    assert any(
        "scheduled_event_id IS NULL" in str(constraint.sqltext)
        for constraint in StageInstance.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    )


def test_stage_moderator_requires_all_three_discord_permissions() -> None:
    assert is_stage_moderator(STAGE_INSTANCE_MODERATOR_PERMISSIONS)
    assert not is_stage_moderator(Permission.MANAGE_CHANNELS | Permission.MUTE_MEMBERS)


def test_stage_speaker_state_replaces_voice_only_speak_and_vad_permissions() -> None:
    assert not voice_speaking_allowed(2, Permission(0))
    assert not voice_activity_allowed(2, Permission(0))
    assert voice_speaking_allowed(13, Permission(0))
    assert voice_activity_allowed(13, Permission(0))


def test_public_voice_state_excludes_control_plane_capabilities() -> None:
    current = occupant()
    public = public_occupant_state(current)
    assert public["suppressed"] is True
    assert "connection_id" not in public
    assert "allow_speak" not in public
    assert "participant_metadata" not in public
    rendered = stage_voice_state_payload(current, "alpha.localhost")
    assert rendered["session_id"] != current.connection_id
    assert rendered["guild_domain"] == "alpha.localhost"

    with pytest.raises(ValidationError):
        VoiceStateFederationRequest.model_validate(
            {
                "guild_id": "12",
                "room": "g.12.34",
                "generated_at": 1,
                "snapshot_version": 1,
                "participants": [public],
            }
        )
    private = federation_occupant_state(current)
    projected = VoiceStateFederationRequest.model_validate(
        {
            "guild_id": "12",
            "room": "g.12.34",
            "generated_at": 1,
            "snapshot_version": 1,
            "participants": [private],
        }
    )
    assert projected.participants[0].generation == 4
    assert projected.participants[0].connection_id == current.connection_id


def test_stage_voice_operations_are_closed_federation_protocol_values() -> None:
    base = {
        "guild": {"id": "12", "domain": "alpha.localhost"},
        "actor": {"id": "78", "domain": "beta.localhost"},
        "requesting_instance": "beta.localhost",
        "request_id": "kagm_" + "a" * 32,
        "issued_at": 10,
        "deadline": 20,
    }
    for operation in (
        "stage_instance.create",
        "stage_voice_state.get",
        "stage_voice_state.self",
        "stage_voice_state.user",
    ):
        assert (
            GuildManagementRequest.model_validate(
                {**base, "operation": operation, "payload": {}}
            ).operation
            == operation
        )


def test_stage_bot_contracts_share_the_local_permission_masks() -> None:
    assert BOT_GUILD_MANAGEMENT_CONTRACTS["stage_voice_state.get"].permission_options == (
        Permission(0),
    )
    assert BOT_GUILD_MANAGEMENT_CONTRACTS["stage_voice_state.self"].permission_options == (
        Permission(0),
    )
    assert BOT_GUILD_MANAGEMENT_CONTRACTS["stage_voice_state.user"].permission_options == (
        STAGE_VOICE_STATE_MODERATOR_PERMISSIONS,
    )
    assert BOT_GUILD_MANAGEMENT_CONTRACTS["stage_instance.create"].permission_options == (
        STAGE_INSTANCE_MODERATOR_PERMISSIONS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_ref", "granted", "allowed"),
    [
        ("78@alpha.localhost", Permission(0), True),
        ("90@people.example", Permission(0), False),
        ("90@people.example", Permission.CONNECT, True),
    ],
)
async def test_direct_bot_stage_reads_apply_the_dynamic_installation_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    target_ref: str,
    granted: Permission,
    allowed: bool,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="bot")
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    installation = SimpleNamespace(granted_permissions=int(granted))
    monkeypatch.setattr(
        "app.api.stage_instances.installation_for_guild",
        AsyncMock(return_value=(guild, installation)),
    )
    proxy = AsyncMock(return_value=SimpleNamespace(body={"user_id": target_ref.split("@")[0]}))
    monkeypatch.setattr("app.api.stage_instances.proxy_stage_voice_state", proxy)

    call = bot_get_stage_voice_state(
        EntityRef("12@alpha.localhost"),
        EntityRef(target_ref),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        settings(),
    )
    if not allowed:
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 403
        proxy.assert_not_awaited()
    else:
        rendered = await call
        assert rendered["user_id"] == target_ref.split("@")[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "granted", "allowed"),
    [
        (CurrentUserVoiceStateUpdate(suppress=True), Permission(0), True),
        (CurrentUserVoiceStateUpdate(suppress=False), Permission(0), False),
        (CurrentUserVoiceStateUpdate(suppress=False), Permission.MUTE_MEMBERS, True),
        (
            CurrentUserVoiceStateUpdate(request_to_speak_timestamp="2026-08-29T14:00:00+00:00"),
            Permission.REQUEST_TO_SPEAK,
            True,
        ),
        (
            CurrentUserVoiceStateUpdate(
                suppress=False,
                request_to_speak_timestamp="2026-08-29T14:00:00+00:00",
            ),
            Permission.MUTE_MEMBERS,
            False,
        ),
    ],
)
async def test_direct_bot_stage_self_updates_apply_the_dynamic_installation_ceiling(
    monkeypatch: pytest.MonkeyPatch,
    payload: CurrentUserVoiceStateUpdate,
    granted: Permission,
    allowed: bool,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="bot")
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    installation = SimpleNamespace(granted_permissions=int(granted))
    monkeypatch.setattr(
        "app.api.stage_instances.installation_for_guild",
        AsyncMock(return_value=(guild, installation)),
    )
    proxy = AsyncMock(return_value=SimpleNamespace(body={}))
    monkeypatch.setattr("app.api.stage_instances.proxy_stage_voice_state", proxy)

    call = bot_update_current_stage_voice_state(
        EntityRef("12@alpha.localhost"),
        payload,
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        settings(),
    )
    if not allowed:
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 403
        proxy.assert_not_awaited()
    else:
        response = await call
        assert response.status_code == 204


@pytest.mark.asyncio
async def test_direct_bot_stage_other_update_requires_mute_installation_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="bot")
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    installation = SimpleNamespace(granted_permissions=0)
    monkeypatch.setattr(
        "app.api.stage_instances.installation_for_guild",
        AsyncMock(return_value=(guild, installation)),
    )
    proxy = AsyncMock(return_value=SimpleNamespace(body={}))
    monkeypatch.setattr("app.api.stage_instances.proxy_stage_voice_state", proxy)

    with pytest.raises(HTTPException) as caught:
        await bot_update_stage_voice_state(
            EntityRef("12@alpha.localhost"),
            EntityRef("90@people.example"),
            UserVoiceStateUpdate(suppress=True),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, AsyncMock()),
            cast(Any, AsyncMock()),
            cast(Any, AsyncMock()),
            settings(),
        )

    assert caught.value.status_code == 403
    proxy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("account_type", "target_ref", "required"),
    [
        ("human", "78@alpha.localhost", None),
        ("human", "90@people.example", STAGE_VOICE_STATE_READ_PERMISSIONS),
        ("bot", "78@alpha.localhost", None),
        ("bot", "90@people.example", STAGE_VOICE_STATE_READ_PERMISSIONS),
    ],
)
async def test_stage_voice_read_distinguishes_self_from_other_for_humans_and_bots(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
    target_ref: str,
    required: Permission | None,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type=account_type)
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="alpha.localhost", type=13)
    target_id, target_domain = target_ref.split("@", 1)
    target = SimpleNamespace(
        id=int(target_id),
        origin_domain=target_domain,
        account_type="human",
    )
    current = replace(
        occupant(),
        identity=target_ref,
        user_id=target_id,
        user_domain=target_domain,
    )
    require = AsyncMock(return_value=required or Permission(0))
    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor", AsyncMock(return_value=guild)
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, target)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", require)
    channel_grant = AsyncMock()
    monkeypatch.setattr("app.api.stage_instances.require_bot_channel_grant", channel_grant)

    rendered = await get_local_stage_voice_state(
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        settings(),
        EntityRef("12@alpha.localhost"),
        cast(Any, actor),
        EntityRef(target_ref),
    )

    assert rendered["user_id"] == target_id
    if required is None:
        require.assert_not_awaited()
    else:
        assert require.await_args.args[4] == required
    channel_grant.assert_awaited_once()


@pytest.mark.asyncio
async def test_stage_join_defaults_a_non_moderator_to_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        profile_resolved=True,
        display_name=None,
        username="audience",
        account_type="human",
    )
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        type=13,
        encryption_mode="plaintext",
        encryption_state="disabled",
        encryption_policy_generation=0,
        encryption_epoch=None,
        bitrate=64_000,
        user_limit=0,
        rtc_region=None,
        video_quality_mode=1,
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(voice_flags=0)
    control = SimpleNamespace(ensure_room=AsyncMock(), remove_participant=AsyncMock())
    captured: dict[str, object] = {}

    def mint(*_args: object, **kwargs: object) -> tuple[str, datetime]:
        captured.update(kwargs)
        return "x" * 32, datetime.now(UTC) + timedelta(minutes=1)

    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.require_permissions",
        AsyncMock(return_value=Permission.CONNECT | Permission.SPEAK | Permission.STREAM),
    )
    monkeypatch.setattr("app.voice.service.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.claim_voice_connection",
        AsyncMock(return_value=(True, 4, "", "")),
    )
    monkeypatch.setattr("app.voice.service.mint_join_token", mint)

    grant = await authoritative_guild_token(
        session,
        AsyncMock(),
        settings(),
        channel=channel,
        guild=guild,
        actor=actor,
        connection_id="c" * 43,
    )

    assert grant.can_speak is False
    assert grant.can_stream is False
    assert grant.can_priority_speak is False
    assert captured["can_publish_data"] is False
    assert cast(dict[str, object], captured["metadata"])["suppressed"] is True


@pytest.mark.asyncio
async def test_stage_moderator_can_publish_without_voice_only_speak_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        profile_resolved=True,
        display_name=None,
        username="moderator",
        account_type="human",
    )
    channel = SimpleNamespace(
        id=34,
        origin_domain="alpha.localhost",
        type=13,
        encryption_mode="plaintext",
        encryption_state="disabled",
        encryption_policy_generation=0,
        encryption_epoch=None,
        bitrate=64_000,
        user_limit=0,
        rtc_region=None,
        video_quality_mode=1,
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(voice_flags=0)
    control = SimpleNamespace(ensure_room=AsyncMock(), remove_participant=AsyncMock())
    captured: dict[str, object] = {}

    def mint(*_args: object, **kwargs: object) -> tuple[str, datetime]:
        captured.update(kwargs)
        return "x" * 32, datetime.now(UTC) + timedelta(minutes=1)

    permissions = Permission.CONNECT | Permission.STREAM | STAGE_INSTANCE_MODERATOR_PERMISSIONS
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.require_permissions", AsyncMock(return_value=permissions)
    )
    monkeypatch.setattr("app.voice.service.require_e2ee_voice_device", AsyncMock())
    monkeypatch.setattr(
        "app.voice.service.claim_voice_connection",
        AsyncMock(return_value=(True, 4, "", "")),
    )
    monkeypatch.setattr("app.voice.service.mint_join_token", mint)

    grant = await authoritative_guild_token(
        session,
        AsyncMock(),
        settings(),
        channel=channel,
        guild=guild,
        actor=actor,
        connection_id="c" * 43,
    )

    assert grant.can_speak is True
    assert grant.can_stream is True
    assert grant.can_priority_speak is False
    assert captured["can_publish_data"] is False
    metadata = cast(dict[str, object], captured["metadata"])
    assert metadata["suppressed"] is False
    assert metadata["can_use_vad"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["human", "bot"])
@pytest.mark.parametrize(
    ("suppress", "required"),
    [
        (True, None),
        (False, STAGE_VOICE_STATE_MODERATOR_PERMISSIONS),
    ],
)
async def test_stage_self_suppress_has_no_baseline_and_unsuppress_requires_mute(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
    suppress: bool,
    required: Permission | None,
) -> None:
    actor = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type=account_type,
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="alpha.localhost", type=13)
    current = occupant()
    session = AsyncMock()
    session.scalar.return_value = None
    require = AsyncMock(return_value=required or Permission(0))
    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor", AsyncMock(return_value=guild)
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, actor)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", require)
    channel_grant = AsyncMock()
    monkeypatch.setattr("app.api.stage_instances.require_bot_channel_grant", channel_grant)
    monkeypatch.setattr(
        "app.api.stage_instances.get_permissions", AsyncMock(return_value=Permission(0))
    )
    monkeypatch.setattr(
        "app.api.stage_instances.update_authoritative_occupant_grant",
        AsyncMock(return_value=replace(current, suppressed=suppress)),
    )
    monkeypatch.setattr("app.api.stage_instances.publish_ephemeral", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.enqueue_best_effort", AsyncMock())

    await update_local_stage_voice_state(
        cast(Any, session),
        cast(Any, AsyncMock()),
        settings(),
        EntityRef("12@alpha.localhost"),
        cast(Any, actor),
        EntityRef("78@alpha.localhost"),
        CurrentUserVoiceStateUpdate(suppress=suppress),
        current_user=True,
    )

    if required is None:
        require.assert_not_awaited()
    else:
        assert [call.args[4] for call in require.await_args_list] == [required]
    channel_grant.assert_awaited_once()


@pytest.mark.asyncio
async def test_federated_bot_stage_self_update_preserves_channel_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="bot.example", account_type="bot")
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="alpha.localhost", type=13)
    current = replace(
        occupant(),
        identity="78@bot.example",
        user_domain="bot.example",
    )
    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor", AsyncMock(return_value=guild)
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, actor)),
    )
    channel_grant = AsyncMock(
        side_effect=HTTPException(
            status_code=403,
            detail={"code": "BOT_CHANNEL_RESTRICTED"},
        )
    )
    monkeypatch.setattr("app.api.stage_instances.require_bot_channel_grant", channel_grant)
    require = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr("app.api.stage_instances.require_permissions", require)
    monkeypatch.setattr("app.api.stage_instances.update_authoritative_occupant_grant", update)

    with pytest.raises(HTTPException) as caught:
        await update_local_stage_voice_state(
            cast(Any, AsyncMock()),
            cast(Any, AsyncMock()),
            settings(),
            EntityRef("12@alpha.localhost"),
            cast(Any, actor),
            EntityRef("78@bot.example"),
            CurrentUserVoiceStateUpdate(suppress=True),
            current_user=True,
        )

    assert caught.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    channel_grant.assert_awaited_once()
    require.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_to_speak_preserves_audience_and_fans_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=78, origin_domain="alpha.localhost", account_type="human")
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="alpha.localhost", type=13)
    current = occupant()
    requested = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    captured: dict[str, object] = {}

    async def update_grant(*_args: object, **kwargs: object) -> Occupant:
        captured.update(kwargs)
        return replace(
            current,
            request_to_speak_timestamp=cast(str, kwargs["request_to_speak_timestamp"]),
        )

    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor", AsyncMock(return_value=guild)
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, actor)),
    )
    require = AsyncMock(return_value=Permission.REQUEST_TO_SPEAK)
    monkeypatch.setattr("app.api.stage_instances.require_permissions", require)
    channel_grant = AsyncMock()
    monkeypatch.setattr("app.api.stage_instances.require_bot_channel_grant", channel_grant)
    monkeypatch.setattr(
        "app.api.stage_instances.get_permissions",
        AsyncMock(return_value=Permission.SPEAK | Permission.STREAM),
    )
    monkeypatch.setattr("app.api.stage_instances.update_authoritative_occupant_grant", update_grant)
    monkeypatch.setattr("app.api.stage_instances.publish_ephemeral", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.enqueue_best_effort", AsyncMock())

    rendered = await update_local_stage_voice_state(
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        settings(),
        EntityRef("12@alpha.localhost"),
        cast(Any, actor),
        EntityRef("78@alpha.localhost"),
        CurrentUserVoiceStateUpdate(request_to_speak_timestamp=requested),
        current_user=True,
    )

    assert captured["suppressed"] is True
    assert captured["can_speak"] is False
    assert captured["request_to_speak_timestamp"] == requested
    assert rendered["request_to_speak_timestamp"] == requested
    assert require.await_args.kwargs["channel"] is channel
    assert [call.args[4] for call in require.await_args_list] == [Permission.REQUEST_TO_SPEAK]


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["human", "bot"])
async def test_stage_voice_moderation_uses_mute_members(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
) -> None:
    moderator = SimpleNamespace(
        id=78,
        origin_domain="alpha.localhost",
        account_type=account_type,
    )
    target = SimpleNamespace(
        id=90,
        origin_domain="people.example",
        account_type="human",
    )
    guild = SimpleNamespace(id=12, origin_domain="alpha.localhost")
    channel = SimpleNamespace(id=34, origin_domain="alpha.localhost", type=13)
    current = replace(
        occupant(),
        identity="90@people.example",
        user_id="90",
        user_domain="people.example",
    )
    require = AsyncMock(return_value=STAGE_VOICE_STATE_MODERATOR_PERMISSIONS)
    monkeypatch.setattr(
        "app.api.stage_instances.stage_guild_for_actor", AsyncMock(return_value=guild)
    )
    monkeypatch.setattr(
        "app.api.stage_instances.connected_stage_occupant",
        AsyncMock(return_value=(current, channel, target)),
    )
    monkeypatch.setattr("app.api.stage_instances.require_permissions", require)
    channel_grant = AsyncMock()
    monkeypatch.setattr("app.api.stage_instances.require_bot_channel_grant", channel_grant)
    monkeypatch.setattr("app.api.stage_instances.require_can_manage_member", AsyncMock())
    monkeypatch.setattr(
        "app.api.stage_instances.get_permissions", AsyncMock(return_value=Permission(0))
    )
    monkeypatch.setattr(
        "app.api.stage_instances.update_authoritative_occupant_grant",
        AsyncMock(return_value=current),
    )
    monkeypatch.setattr("app.api.stage_instances.publish_ephemeral", AsyncMock())
    monkeypatch.setattr("app.api.stage_instances.enqueue_best_effort", AsyncMock())

    await update_local_stage_voice_state(
        cast(Any, AsyncMock()),
        cast(Any, AsyncMock()),
        settings(),
        EntityRef("12@alpha.localhost"),
        cast(Any, moderator),
        EntityRef("90@people.example"),
        UserVoiceStateUpdate(suppress=True),
        current_user=False,
    )

    assert require.await_args.args[4] == STAGE_VOICE_STATE_MODERATOR_PERMISSIONS
    channel_grant.assert_awaited_once()


@pytest.mark.asyncio
async def test_grant_rotation_updates_live_participant_and_supersedes_old_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = occupant()
    control = SimpleNamespace(update_participant=AsyncMock())
    monkeypatch.setattr("app.voice.service.LiveKitControl", lambda _settings: control)
    monkeypatch.setattr(
        "app.voice.service.claim_voice_grant_transition", AsyncMock(return_value=True)
    )
    monkeypatch.setattr("app.voice.service.current_generation", AsyncMock(return_value=4))
    rotate = AsyncMock(return_value=5)
    monkeypatch.setattr("app.voice.service.rotate_occupant_grant", rotate)
    release = AsyncMock(return_value=True)
    monkeypatch.setattr("app.voice.service.release_voice_grant_transition", release)

    updated = await update_authoritative_occupant_grant(
        cast(Any, AsyncMock()),
        settings(),
        current,
        can_speak=True,
        can_stream=True,
        suppressed=False,
        request_to_speak_timestamp=None,
        update_request_timestamp=True,
    )

    assert updated.can_speak is True
    assert updated.suppressed is False
    assert updated.participant_metadata["generation"] == 5
    metadata = control.update_participant.await_args.kwargs["metadata"]
    assert metadata["generation"] == 5
    rotate.assert_awaited_once()
    release.assert_awaited_once()
