import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

import app.api.bots as bots_api
import app.api.expressions as expressions_api
import app.api.interactions as interactions_api
import app.api.soundboard as soundboard_api
import app.api.voice as voice_api
import app.chat.audit as audit_api
import app.chat.expression_events as expression_events
import app.chat.permissions as permissions_api
import app.chat.postcommit as postcommit
import app.voice.status as voice_status_api
from app.api.channels import (
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
    message_create_permissions,
    require_voice_message_attachments,
    require_voice_message_guild_capacity,
    validate_merged_message_edit,
)
from app.api.federation import ProxyMentionProjection, proxy_message_matches_request
from app.bots.installations import queue_installation_gateway_events
from app.chat.schemas import MessageCreate, MessageEdit
from app.chat.voice_messages import VoiceMessageCapability, guild_voice_message_capability
from app.core.channel_types import is_message_capable_channel_type
from app.core.permission_contract import required_permissions
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation, BotInteraction, BotUserInstallation
from app.db.models import Attachment, Guild, User
from app.federation.schemas import GuildProxyRequest, RemoteUserProfile
from app.media.schemas import UploadTicketRequest
from app.voice.schemas import SoundboardPlayRequest, VoiceChannelStatusUpdate


def guild_and_actor() -> tuple[Guild, User]:
    guild = Guild(
        id=70,
        origin_domain="chat.example",
        name="Guild",
        owner_id=80,
        owner_domain="chat.example",
    )
    actor = User(
        id=80,
        origin_domain="chat.example",
        username="member",
        is_local=True,
    )
    return guild, actor


@pytest.mark.asyncio
async def test_expression_authorizers_separate_read_create_and_manage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor = guild_and_actor()
    auth = SimpleNamespace(user=actor)
    human_permissions = AsyncMock(return_value=int(Permission.VIEW_CHANNEL))
    session = SimpleNamespace(get=AsyncMock(return_value=object()))
    monkeypatch.setattr(expressions_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(expressions_api, "require_permissions", human_permissions)

    await expressions_api._authorize_human(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        EntityRef("70"),
        auth,
        manage=False,
    )
    human_permissions.assert_not_awaited()
    session.get.assert_awaited_once()

    await expressions_api._authorize_human(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        EntityRef("70"),
        auth,
        manage=True,
    )
    assert human_permissions.await_args.args[-1] == Permission.MANAGE_EMOJIS

    sound_permissions = AsyncMock(return_value=int(Permission.CREATE_GUILD_EXPRESSIONS))
    monkeypatch.setattr(soundboard_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(soundboard_api, "require_permissions", sound_permissions)
    await soundboard_api._creatable_human_guild(
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        EntityRef("70"),
        auth,
    )
    assert sound_permissions.await_args.args[-1] == Permission.CREATE_GUILD_EXPRESSIONS


@pytest.mark.asyncio
async def test_expression_edits_use_creator_aware_discord_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor = guild_and_actor()
    required = AsyncMock(return_value=int(Permission.CREATE_GUILD_EXPRESSIONS))
    monkeypatch.setattr(permissions_api, "require_permissions", required)

    await permissions_api.require_can_manage_expression(
        SimpleNamespace(),
        SimpleNamespace(),
        guild,
        actor,
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
    )
    assert required.await_args.args[-1] == Permission.CREATE_GUILD_EXPRESSIONS

    await permissions_api.require_can_manage_expression(
        SimpleNamespace(),
        SimpleNamespace(),
        guild,
        actor,
        creator_id=actor.id + 1,
        creator_domain=actor.origin_domain,
    )
    assert required.await_args.args[-1] == Permission.MANAGE_GUILD_EXPRESSIONS


@pytest.mark.asyncio
async def test_bot_expression_creation_rejects_manage_without_create_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor = guild_and_actor()
    installation = SimpleNamespace()
    authorize = AsyncMock(return_value=(guild, installation))
    monkeypatch.setattr(bots_api, "installation_for_guild_any_scope", authorize)
    monkeypatch.setattr(bots_api, "require_installation_scope", Mock())
    monkeypatch.setattr(
        bots_api,
        "get_permissions",
        AsyncMock(return_value=int(Permission.MANAGE_EMOJIS)),
    )

    with pytest.raises(HTTPException) as raised:
        await bots_api.bot_create_emoji_ticket(
            EntityRef("70"),
            UploadTicketRequest(filename="wave.png", content_type="image/png", size=128),
            Response(),
            SimpleNamespace(user=actor),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example", media_max_emoji_bytes=256 * 1024),
        )

    assert raised.value.status_code == 403
    assert authorize.await_args.args[-2:] == ("expressions.manage", "emojis.manage")


@pytest.mark.asyncio
async def test_external_sound_requires_external_sound_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor = guild_and_actor()
    sound = SimpleNamespace(
        id=91,
        origin_domain="chat.example",
        guild_id=71,
        guild_domain="chat.example",
        version=1,
    )
    base = Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.USE_SOUNDBOARD
    monkeypatch.setattr(soundboard_api, "get_permissions", AsyncMock(return_value=int(base)))
    session = SimpleNamespace(scalar=AsyncMock(return_value=sound))

    with pytest.raises(HTTPException) as raised:
        await soundboard_api._soundboard_play_capability(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
            SimpleNamespace(id=72),
            guild,
            actor,
            SoundboardPlayRequest(sound_id=EntityRef("91")),
            Response(),
            caller=soundboard_api._federation_caller(actor),
        )

    assert raised.value.status_code == 403
    assert int(raised.value.detail["permissions"]) & Permission.USE_EXTERNAL_SOUNDS


def test_user_installed_guild_application_without_external_apps_is_ephemeral() -> None:
    installation = BotUserInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        user_id=80,
        user_domain="chat.example",
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["guild"],
        grant_revision=1,
        status="active",
    )
    interaction = BotInteraction(
        id=70,
        application_id=20,
        application_domain="apps.example",
        user_installation_id=60,
        guild_id=1,
        guild_domain="chat.example",
        channel_id=72,
        channel_domain="chat.example",
        user_id=80,
        user_domain="chat.example",
        invocation_permissions=int(Permission.SEND_MESSAGES),
        invocation_channel_type=0,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )

    assert interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_permissions |= int(Permission.USE_EXTERNAL_APPS)
    assert interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_permissions |= int(Permission.USE_APPLICATION_COMMANDS)
    assert not interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_permissions &= ~int(Permission.SEND_MESSAGES)
    assert interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_channel_type = 11
    interaction.invocation_permissions |= int(Permission.SEND_MESSAGES_IN_THREADS)
    assert not interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_permissions &= ~int(Permission.USE_APPLICATION_COMMANDS)
    assert interactions_api.user_install_response_forced_ephemeral(interaction, installation)
    interaction.invocation_permissions = int(Permission.ADMINISTRATOR)
    assert not interactions_api.user_install_response_forced_ephemeral(interaction, installation)


@pytest.mark.asyncio
async def test_voice_status_and_voice_message_permissions_are_operation_specific(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    required = AsyncMock(return_value=0)
    monkeypatch.setattr(voice_status_api, "require_permissions", required)
    monkeypatch.setattr(voice_status_api, "queue_guild_mutation", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(voice_status_api, "add_audit_entry", audit)
    monkeypatch.setattr(voice_status_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(voice_status_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(
        voice_status_api,
        "voice_user_room",
        AsyncMock(side_effect=["g.70.72", None]),
    )
    guild = SimpleNamespace(id=70, origin_domain="chat.example")
    channel = SimpleNamespace(id=72, origin_domain="chat.example", type=2)
    actor = SimpleNamespace(id=80, origin_domain="users.example")
    session = SimpleNamespace(commit=AsyncMock())
    values: dict[str, str] = {}

    async def get_value(key: str) -> str | None:
        return values.get(key)

    async def set_value(key: str, value: str) -> None:
        values[key] = value

    async def delete_value(key: str) -> int:
        return int(values.pop(key, None) is not None)

    redis = SimpleNamespace(
        get=get_value,
        set=set_value,
        delete=delete_value,
    )
    settings = SimpleNamespace(domain="chat.example")

    await voice_status_api.set_voice_channel_status(
        session, redis, SimpleNamespace(), settings, guild, channel, actor, "  Pairing  "
    )
    assert required.await_args.args[-1] == Permission.SET_VOICE_CHANNEL_STATUS
    assert values["voice:channel-status:chat.example:70:72"] == "Pairing"
    await voice_status_api.set_voice_channel_status(
        session, redis, SimpleNamespace(), settings, guild, channel, actor, None
    )
    assert required.await_args.args[-1] == (
        Permission.SET_VOICE_CHANNEL_STATUS | Permission.MANAGE_CHANNELS
    )
    assert [call.args[4] for call in audit.await_args_list] == [192, 193]
    assert "voice:channel-status:chat.example:70:72" not in values
    assert VoiceChannelStatusUpdate(status="  🎉 Party  ").status == "🎉 Party"
    with pytest.raises(ValidationError):
        VoiceChannelStatusUpdate.model_validate({"status": True})

    voice_message = MessageCreate(voice_message=True, attachment_ids=["91"])
    required = message_create_permissions(voice_message, guild_channel=True)
    assert required & Permission.SEND_VOICE_MESSAGES
    assert required & Permission.ATTACH_FILES
    assert not message_create_permissions(voice_message, guild_channel=False) & (
        Permission.SEND_VOICE_MESSAGES
    )
    assert MESSAGE_FLAG_IS_VOICE_MESSAGE == 1 << 13

    tts_message = MessageCreate(content="announcement", tts=True)
    assert message_create_permissions(tts_message, guild_channel=True) & (
        Permission.SEND_TTS_MESSAGES
    )
    assert not message_create_permissions(tts_message, guild_channel=False) & (
        Permission.SEND_TTS_MESSAGES
    )


@pytest.mark.asyncio
async def test_voice_status_restores_ephemeral_state_when_durable_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_status_api, "require_permissions", AsyncMock())
    monkeypatch.setattr(voice_status_api, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(voice_status_api, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(
        voice_status_api,
        "voice_user_room",
        AsyncMock(return_value="g.70.72"),
    )
    guild = SimpleNamespace(id=70, origin_domain="chat.example")
    channel = SimpleNamespace(id=72, origin_domain="chat.example", type=2)
    actor = SimpleNamespace(id=80, origin_domain="users.example")
    status_key = "voice:channel-status:chat.example:70:72"
    values = {
        status_key: "Previous",
    }

    async def get_value(key: str) -> str | None:
        return values.get(key)

    async def set_value(key: str, value: str) -> None:
        values[key] = value

    async def delete_value(key: str) -> int:
        return int(values.pop(key, None) is not None)

    session = SimpleNamespace(
        commit=AsyncMock(side_effect=RuntimeError("database unavailable")),
        rollback=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await voice_status_api.set_voice_channel_status(
            session,
            SimpleNamespace(get=get_value, set=set_value, delete=delete_value),
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
            guild,
            channel,
            actor,
            "Replacement",
        )

    assert values[status_key] == "Previous"
    session.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_voice_status_actor_is_bound_to_scoped_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=70, origin_domain="chat.example")
    channel = SimpleNamespace(id=72, origin_domain="chat.example", type=2)
    actor = SimpleNamespace(id=80, origin_domain="apps.example")
    proxy = AsyncMock()
    monkeypatch.setattr(
        voice_api,
        "load_voice_channel",
        AsyncMock(return_value=(channel, guild)),
    )
    monkeypatch.setattr(voice_api, "proxy_remote_guild_management", proxy)

    with pytest.raises(HTTPException) as caught:
        await voice_api.update_voice_channel_status_for_actor(
            EntityRef("72@chat.example"),
            VoiceChannelStatusUpdate(status="Live"),
            actor,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
            expected_guild_ref=EntityRef("71@chat.example"),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "CHANNEL_NOT_FOUND"}
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_status_http_surfaces_return_discord_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=80, origin_domain="users.example")
    status_update = VoiceChannelStatusUpdate(status="Live")
    update = AsyncMock(return_value={"status": "Live"})
    monkeypatch.setattr(voice_api, "update_voice_channel_status_for_actor", update)

    human_response = await voice_api.update_voice_channel_status(
        EntityRef("72@chat.example"),
        status_update,
        SimpleNamespace(user=actor),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        "event night",
    )
    assert human_response.status_code == 204
    assert human_response.body == b""

    monkeypatch.setattr(
        bots_api,
        "installation_for_guild",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(bots_api, "_require_bot_requested_channel", AsyncMock())
    bot_response = await bots_api.bot_update_voice_channel_status(
        EntityRef("70@chat.example"),
        EntityRef("72@chat.example"),
        status_update,
        SimpleNamespace(user=actor),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        "event night",
    )
    assert bot_response.status_code == 204
    assert bot_response.body == b""
    assert update.await_count == 2
    assert update.await_args.kwargs["expected_guild_ref"] == EntityRef("70@chat.example")


def test_voice_and_stage_channels_are_message_capable_but_forums_use_starters() -> None:
    for channel_type in (0, 2, 5, 10, 11, 12, 13):
        assert is_message_capable_channel_type(channel_type, guild_channel=True)
    assert not is_message_capable_channel_type(15, guild_channel=True)
    for channel_type in (1, 3):
        assert is_message_capable_channel_type(channel_type, guild_channel=False)


def test_voice_message_shape_requires_one_plaintext_audio_attachment() -> None:
    with pytest.raises(ValidationError):
        VoiceMessageCapability.model_validate({"available": 1})
    with pytest.raises(ValueError, match="voice message requires exactly one"):
        MessageCreate(content="not a voice note", voice_message=True, attachment_ids=["91"])
    with pytest.raises(ValueError, match="voice message requires exactly one"):
        MessageCreate(voice_message=True, attachment_ids=["91", "92"])

    require_voice_message_attachments(
        True,
        [
            Attachment(
                content_type="audio/ogg",
                detected_content_type="audio/ogg",
                encryption_mode="plaintext",
                duration_secs=1.5,
                waveform="AQ==",
            )
        ],
    )
    with pytest.raises(HTTPException) as raised:
        require_voice_message_attachments(
            True,
            [{"content_type": "image/png", "encryption_mode": "plaintext"}],
        )
    assert raised.value.detail["code"] == "VOICE_MESSAGE_ATTACHMENT_INVALID"


@pytest.mark.asyncio
async def test_voice_messages_have_no_guild_member_count_cap() -> None:
    guild, _ = guild_and_actor()
    large_guild = SimpleNamespace(scalar=AsyncMock(return_value=100_000))
    capability = await guild_voice_message_capability(large_guild, guild)
    assert capability.model_dump() == {"available": True}
    await require_voice_message_guild_capacity(
        large_guild,
        guild,
        voice_message=True,
    )
    large_guild.scalar.assert_not_awaited()
    dm = SimpleNamespace(scalar=AsyncMock())
    await require_voice_message_guild_capacity(dm, None, voice_message=True)
    dm.scalar.assert_not_awaited()


def test_voice_upload_metadata_enforces_discord_wire_bounds() -> None:
    accepted = UploadTicketRequest(
        filename="note.ogg",
        content_type="audio/ogg",
        size=128,
        duration_secs=1_200,
        waveform="AQ==",
    )
    assert accepted.duration_secs == 1_200

    with pytest.raises(ValueError):
        UploadTicketRequest(
            filename="note.ogg",
            content_type="audio/ogg",
            size=128,
            duration_secs=1_200.01,
            waveform="AQ==",
        )
    with pytest.raises(ValueError, match="1 to 256"):
        UploadTicketRequest(
            filename="note.ogg",
            content_type="audio/ogg",
            size=128,
            duration_secs=1,
            waveform=base64.b64encode(b"\x01" * 257).decode(),
        )
    with pytest.raises(ValueError, match="plaintext audio"):
        UploadTicketRequest(
            filename="note.png",
            content_type="image/png",
            size=128,
            duration_secs=1,
            waveform="AQ==",
        )


def test_message_edit_allows_explicit_content_clear_when_stored_body_remains() -> None:
    edit = MessageEdit.model_validate({"content": None})
    assert edit.content is None
    assert edit.model_fields_set == {"content"}

    for remaining in (
        {"embeds": [{"title": "retained"}]},
        {"components": [{"type": 1, "components": []}]},
        {"sticker_items": [{"id": "1"}]},
        {"forward_snapshot": {"content": "retained"}},
    ):
        assert (
            validate_merged_message_edit(
                content=None,
                e2ee=None,
                embeds=remaining.get("embeds", []),
                components=remaining.get("components", []),
                attachment_count=0,
                sticker_items=remaining.get("sticker_items", []),
                forward_snapshot=remaining.get("forward_snapshot"),
                current_flags=0,
                requested_flags=None,
            )
            is False
        )

    with pytest.raises(HTTPException) as raised:
        validate_merged_message_edit(
            content=None,
            e2ee=None,
            embeds=[],
            components=[],
            attachment_count=0,
            sticker_items=[],
            forward_snapshot=None,
            current_flags=0,
            requested_flags=None,
        )
    assert raised.value.detail == {"code": "MESSAGE_BODY_REQUIRED"}


@pytest.mark.asyncio
async def test_federated_voice_message_shape_and_nonce_replay_preserve_flag() -> None:
    payload = GuildProxyRequest(
        operation="message.create",
        actor=RemoteUserProfile(
            id="80",
            origin_domain="remote.example",
            username="member",
        ),
        channel_id="72",
        voice_message=True,
        client_nonce="voice-note-1",
        attachments=[
            {
                "id": "91",
                "origin_domain": "remote.example",
                "content_type": "audio/ogg",
                "encryption_mode": "plaintext",
                "duration_secs": 1.5,
                "waveform": "AQ==",
            }
        ],
    )
    message = SimpleNamespace(
        flags=0,
        content=None,
        e2ee=None,
        embeds=[],
        components=[],
    )
    assert not await proxy_message_matches_request(
        SimpleNamespace(),
        message,
        payload,
        application_ref=None,
        forwarded_message=None,
        mentions=ProxyMentionProjection((), (), (), frozenset(), False),
    )


@pytest.mark.asyncio
async def test_postcommit_dispatch_publishes_after_commit_and_drops_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    publish = AsyncMock(return_value={})
    monkeypatch.setattr(postcommit, "publish_dispatch", publish)
    session = AsyncSession()
    queue_postcommit = postcommit.queue_postcommit_dispatch
    queue_postcommit(session, "guild:chat.example:70", "AFTER_COMMIT", {"ok": True})
    assert await postcommit.publish_committed_dispatches(session, SimpleNamespace()) == 0
    await session.commit()
    assert await postcommit.publish_committed_dispatches(session, SimpleNamespace()) == 1
    assert publish.await_args.args[2] == "AFTER_COMMIT"
    await session.close()

    rolled_back = AsyncSession()
    queue_postcommit(rolled_back, "guild:chat.example:70", "ROLLED_BACK", {"ok": False})
    await rolled_back.rollback()
    assert await postcommit.publish_committed_dispatches(rolled_back, SimpleNamespace()) == 0
    assert publish.await_count == 1
    await rolled_back.close()


@pytest.mark.asyncio
async def test_audit_entry_queues_redacted_gateway_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor = guild_and_actor()
    session = SimpleNamespace(add=Mock())
    queued = Mock()
    monkeypatch.setattr(audit_api, "queue_postcommit_dispatch", queued)
    entry = await audit_api.add_audit_entry(
        session,
        SimpleNamespace(mint=AsyncMock(return_value=501)),
        guild,
        actor,
        11,
        target_type="channel",
        target_ref={"id": "72", "token": "do-not-publish"},
        reason="  routine cleanup  ",
        changes=[{"key": "webhook_token", "new_value": "do-not-publish"}],
    )

    session.add.assert_called_once_with(entry)
    assert entry.reason == "routine cleanup"
    assert queued.call_args.args[2] == "GUILD_AUDIT_LOG_ENTRY_CREATE"
    payload = queued.call_args.args[3]
    assert payload["target_ref"]["token"] == "[redacted]"
    assert payload["changes"][0]["new_value"] == "[redacted]"


@pytest.mark.asyncio
async def test_audit_entry_rejects_an_oversized_reason_before_minting() -> None:
    guild, actor = guild_and_actor()
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=501))
    session = SimpleNamespace(add=Mock())

    with pytest.raises(HTTPException) as denied:
        await audit_api.add_audit_entry(
            session,
            snowflake,
            guild,
            actor,
            11,
            reason="x" * 513,
        )

    assert denied.value.detail["code"] == "AUDIT_REASON_TOO_LONG"
    snowflake.mint.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_installation_events_dispatch_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = BotInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="chat.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="chat.example",
        granted_scopes=["applications.commands"],
        granted_intents=["interactions"],
        granted_permissions=int(Permission.USE_APPLICATION_COMMANDS),
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=2,
        status="active",
        role_id=90,
        role_domain="chat.example",
    )
    publish = AsyncMock(return_value={})
    monkeypatch.setattr(postcommit, "publish_dispatch", publish)
    session = AsyncSession()
    queue_installation_gateway_events(session, installation, "UPDATE")
    assert publish.await_count == 0
    await session.commit()
    assert await postcommit.publish_committed_dispatches(session, SimpleNamespace()) == 4

    event_types = [call.args[2] for call in publish.await_args_list]
    assert event_types == [
        "INTEGRATION_UPDATE",
        "BOT_INSTALLATION_UPDATE",
        "GUILD_INTEGRATIONS_UPDATE",
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
    ]
    assert publish.await_args_list[-1].kwargs["audience_user_refs"] == ("10@apps.example",)
    await session.close()


class StartTimeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: object) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self.values:
                removed += 1
                del self.values[key]
        return removed


@pytest.mark.asyncio
async def test_voice_channel_start_time_dispatch_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = StartTimeRedis()
    publish = AsyncMock(return_value={})
    monkeypatch.setattr(voice_api, "publish_dispatch", publish)
    monkeypatch.setattr(
        "app.chat.guild_revision.guild_authority_owner",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr("app.chat.guild_revision.queue_guild_mutation", AsyncMock())
    monkeypatch.setattr("app.chat.guild_revision.wake_queued_guild_federation", AsyncMock())
    settings = SimpleNamespace(domain="chat.example")
    guild = SimpleNamespace(id=70, origin_domain="chat.example")
    channel = SimpleNamespace(
        id=72,
        origin_domain="chat.example",
        guild_id=70,
        guild_domain="chat.example",
        type=2,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda _model, key: channel if key[0] == 72 else guild),
        commit=AsyncMock(),
    )

    assert await voice_api.publish_voice_channel_start_time(
        redis,
        settings,
        session,
        guild_id=70,
        channel_id=72,
        room="g.70.72",
        started_at=1_777_777_777,
    )
    assert not await voice_api.publish_voice_channel_start_time(
        redis,
        settings,
        session,
        guild_id=70,
        channel_id=72,
        room="g.70.72",
        started_at=1_777_777_778,
    )
    assert await voice_api.publish_voice_channel_start_time(
        redis,
        settings,
        session,
        guild_id=70,
        channel_id=72,
        room="g.70.72",
        started_at=None,
    )
    assert not await voice_api.publish_voice_channel_start_time(
        redis,
        settings,
        session,
        guild_id=70,
        channel_id=72,
        room="g.70.72",
        started_at=None,
    )
    assert [call.args[2] for call in publish.await_args_list] == [
        "VOICE_CHANNEL_START_TIME_UPDATE",
        "VOICE_CHANNEL_START_TIME_UPDATE",
    ]
    assert publish.await_args_list[-1].args[3]["voice_start_time"] is None


@pytest.mark.asyncio
async def test_voice_channel_start_time_restores_redis_when_commit_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = StartTimeRedis()
    monkeypatch.setattr(
        "app.chat.guild_revision.guild_authority_owner",
        AsyncMock(return_value=SimpleNamespace()),
    )
    monkeypatch.setattr("app.chat.guild_revision.queue_guild_mutation", AsyncMock())
    guild = SimpleNamespace(id=70, origin_domain="chat.example")
    channel = SimpleNamespace(
        id=72,
        origin_domain="chat.example",
        guild_id=70,
        guild_domain="chat.example",
        type=2,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda _model, key: channel if key[0] == 72 else guild),
        commit=AsyncMock(side_effect=RuntimeError("database unavailable")),
        rollback=AsyncMock(),
    )
    key = "voice:v2:start-time:chat.example:g.70.72"

    with pytest.raises(RuntimeError, match="database unavailable"):
        await voice_api.publish_voice_channel_start_time(
            redis,
            SimpleNamespace(domain="chat.example"),
            session,
            guild_id=70,
            channel_id=72,
            room="g.70.72",
            started_at=1_777_777_777,
        )
    assert key not in redis.values

    redis.values[key] = "1777777000"
    session.rollback.reset_mock()
    with pytest.raises(RuntimeError, match="database unavailable"):
        await voice_api.publish_voice_channel_start_time(
            redis,
            SimpleNamespace(domain="chat.example"),
            session,
            guild_id=70,
            channel_id=72,
            room="g.70.72",
            started_at=None,
        )
    assert redis.values[key] == "1777777000"
    assert session.rollback.await_count == 1


def test_expression_permission_contract_uses_distinct_operations() -> None:
    assert required_permissions("guild.expression.read") == Permission(0)
    assert required_permissions("guild.expression.create") == Permission.CREATE_GUILD_EXPRESSIONS
    assert required_permissions("guild.expression.manage") == Permission.MANAGE_EMOJIS


@pytest.mark.asyncio
async def test_expression_collection_events_publish_complete_typed_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _ = guild_and_actor()
    emoji = SimpleNamespace(
        id=1,
        origin_domain="chat.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="party",
        animated=False,
        available=True,
        media_hash="a" * 64,
        creator_id=80,
        creator_domain="chat.example",
        updated_at=None,
    )
    sticker = SimpleNamespace(
        id=2,
        origin_domain="chat.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="wave",
        description=None,
        animated=False,
        available=True,
        tags=["wave"],
        media_hash="b" * 64,
        creator_id=80,
        creator_domain="chat.example",
        updated_at=None,
    )
    sound = SimpleNamespace(
        id=3,
        origin_domain="chat.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="chime",
        media_hash="c" * 64,
        content_type="audio/ogg",
        volume=0.8,
        emoji_id=None,
        emoji_domain=None,
        emoji_name="🔔",
        available=True,
        duration_ms=900,
        created_by_id=80,
        created_by_domain="chat.example",
        version=1,
    )

    class RoleRows:
        def tuples(self) -> list[tuple[int, str, int, str]]:
            return [(1, "chat.example", 9, "chat.example")]

    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[emoji], [sticker], [sound]]),
        execute=AsyncMock(return_value=RoleRows()),
    )
    published = AsyncMock(return_value={})
    monkeypatch.setattr(expression_events, "publish_dispatch", published)

    await expression_events.publish_guild_emojis_update(
        session,
        SimpleNamespace(),
        guild,  # type: ignore[arg-type]
    )
    await expression_events.publish_guild_stickers_update(
        session,
        SimpleNamespace(),
        guild,  # type: ignore[arg-type]
    )
    await expression_events.publish_guild_soundboard_sounds_update(
        session,
        SimpleNamespace(),
        guild,  # type: ignore[arg-type]
    )

    assert [call.args[2] for call in published.await_args_list] == [
        "GUILD_EMOJIS_UPDATE",
        "GUILD_STICKERS_UPDATE",
        "GUILD_SOUNDBOARD_SOUNDS_UPDATE",
    ]
    assert published.await_args_list[0].args[3]["emojis"][0]["roles"] == ["9@chat.example"]
    assert published.await_args_list[1].args[3]["stickers"][0]["name"] == "wave"
    assert published.await_args_list[2].args[3]["soundboard_sounds"][0]["name"] == "chime"
