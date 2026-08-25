from datetime import datetime
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kaede_bot.client import Client, canonical_target_origin
from kaede_bot.errors import ApiError
from kaede_bot.intents import Intents
from kaede_bot.models import (
    Channel,
    ChannelDeleteEvent,
    Emoji,
    Guild,
    Interaction,
    Member,
    Message,
    PresenceEvent,
    ReactionEvent,
    Sticker,
    StickerDeleteEvent,
    ThreadListSyncEvent,
    ThreadMembersUpdateEvent,
    VoiceStateEvent,
)
from kaede_bot.refs import EntityRef, User
from kaede_bot.state import WorkerState
import kaede_bot.client as client_module
import kaede_bot.state as state_module


def test_entity_ref_and_human_handle_are_distinct() -> None:
    ref = EntityRef.parse("123@chat.example")
    user = User(ref, "alice", "Alice")
    assert str(ref) == "123@chat.example"
    assert user.handle == "alice@chat.example"
    assert user.mention == "<@123@chat.example>"


@pytest.mark.parametrize(
    "value",
    ["alice@chat.example", "1@Chat.example", "1@chat.example.", "-1@chat.example"],
)
def test_entity_ref_rejects_usernames_and_noncanonical_domains(value: str) -> None:
    with pytest.raises(ValueError):
        EntityRef.parse(value)


def test_worker_state_round_trip_uses_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state = WorkerState(
        EntityRef(1, "apps.example"), 2, Ed25519PrivateKey.generate(), "production"
    )
    state.save(root)
    assert root.stat().st_mode & 0o077 == 0
    assert (root / "worker.json").stat().st_mode & 0o077 == 0
    loaded = WorkerState.load(root)
    assert loaded.application_ref == state.application_ref
    assert loaded.worker_id == state.worker_id
    assert loaded.public_key == state.public_key


def test_worker_state_refuses_shared_directory(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    state = WorkerState(
        EntityRef(1, "apps.example"), 2, Ed25519PrivateKey.generate(), "production"
    )
    with pytest.raises(PermissionError):
        state.save(root)


def test_bot_target_origins_are_canonical_and_safe() -> None:
    assert canonical_target_origin("https://CHAT.Example/") == "https://chat.example"
    assert canonical_target_origin("https://chat.example:443") == "https://chat.example"
    assert (
        canonical_target_origin("https://chat.example:8443")
        == "https://chat.example:8443"
    )
    for value in (
        "http://chat.example",
        "https://user@chat.example",
        "https://chat.example/api",
        "https://chat.example?target=other",
        "https://chat.example#fragment",
        "https://chat.example.",
        "https://chat.example:invalid",
    ):
        with pytest.raises(ValueError, match="canonical HTTPS origins"):
            canonical_target_origin(value)


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


@pytest.mark.asyncio
async def test_reaction_event_alias_is_typed_and_dispatched() -> None:
    bot = client()
    seen: list[ReactionEvent] = []

    @bot.event
    async def on_reaction_add(event: ReactionEvent) -> None:
        seen.append(event)

    await bot.dispatch(
        "MESSAGE_REACTION_ADD",
        {
            "id": "5",
            "origin_domain": "guild.example",
            "channel_id": "6",
            "channel_domain": "guild.example",
            "user_id": "7",
            "user_domain": "users.example",
            "reaction": "wave",
        },
        target="https://guild.example",
    )

    assert len(seen) == 1
    assert seen[0].message_ref == EntityRef(5, "guild.example")
    assert seen[0].user_ref == EntityRef(7, "users.example")


@pytest.mark.asyncio
async def test_message_convenience_methods_keep_event_target() -> None:
    bot = client()
    bot.send_message = AsyncMock()  # type: ignore[method-assign]
    message = Message.from_payload(
        bot,
        "https://two.example",
        {
            "id": "9",
            "origin_domain": "two.example",
            "channel_id": "8",
            "channel_domain": "two.example",
            "content": "hello",
            "created_at": "2026-08-18T00:00:00+00:00",
            "attachments": [],
        },
    )

    await message.reply("world")

    bot.send_message.assert_awaited_once_with(
        EntityRef(8, "two.example"),
        "world",
        target="https://two.example",
        reply_to=EntityRef(9, "two.example"),
    )


@pytest.mark.asyncio
async def test_interaction_response_keeps_event_target() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "12",
            "origin_domain": "two.example",
            "channel_id": "11",
            "channel_domain": "two.example",
            "content": "done",
            "created_at": "2026-08-18T00:00:00+00:00",
            "attachments": [],
        }
    )
    interaction = Interaction.from_payload(
        bot,
        "https://two.example",
        {
            "id": "10",
            "application_ref": "1@apps.example",
            "guild_ref": "2@two.example",
            "channel_ref": "11@two.example",
            "user": {
                "id": "3",
                "origin_domain": "two.example",
                "username": "alice",
            },
            "command": {"name": "ping"},
            "options": {},
        },
    )

    response = await interaction.respond("done")

    assert response.target == "https://two.example"
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["target"] == "https://two.example"


def test_gateway_cursors_are_private_and_survive_restart(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state = WorkerState(
        EntityRef(1, "apps.example"), 2, Ed25519PrivateKey.generate(), "production"
    )
    state.save(root)
    state.save_cursors({"https://one.example": {"guild:one.example:3": 44}})

    loaded = WorkerState.load(root)

    assert (root / "gateway-cursors.json").stat().st_mode & 0o077 == 0
    assert loaded.load_cursors() == {"https://one.example": {"guild:one.example:3": 44}}


def test_intents_include_independent_typing_subscription() -> None:
    assert "guild_typing" not in Intents.default().names()
    assert "guild_typing" in Intents.all().names()


@pytest.mark.asyncio
async def test_channel_update_uses_composite_refs_and_optimistic_version() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "3",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 0,
            "name": "renamed",
            "topic": None,
            "version": "2026-08-18T01:02:03+00:00",
        }
    )

    channel = await bot.edit_channel(
        EntityRef(2, "guild.example"),
        EntityRef(3, "guild.example"),
        target="https://guild.example",
        version="2026-08-18T00:00:00+00:00",
        name="renamed",
        topic=None,
    )

    assert channel.version == "2026-08-18T01:02:03+00:00"
    assert bot.request.await_args is not None
    assert bot.request.await_args.args == (
        "PATCH",
        "/api/v1/bots/guilds/2@guild.example/channels/3@guild.example",
    )
    assert bot.request.await_args.kwargs["headers"] == {
        "If-Match": "2026-08-18T00:00:00+00:00"
    }
    assert bot.request.await_args.kwargs["json"] == {
        "name": "renamed",
        "topic": None,
    }


def test_member_hydrates_presence_roles_and_voice_authority_state() -> None:
    bot = client()
    member = Member.from_payload(
        bot,
        "https://guild.example",
        {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "user": {
                "id": "4",
                "origin_domain": "users.example",
                "username": "alice",
            },
            "nickname": "Al",
            "joined_at": "2026-08-18T00:00:00+00:00",
            "member_version": "7",
            "role_ids": ["8", "9"],
            "presence": "idle",
            "voice_flags": 3,
        },
    )

    assert member.presence == "idle"
    assert member.role_ids == (8, 9)
    assert member.voice_flags == 3
    assert member.member_version == 7


@pytest.mark.asyncio
async def test_delete_and_voice_gateway_events_are_typed() -> None:
    bot = client()
    deleted: list[ChannelDeleteEvent] = []
    voice: list[VoiceStateEvent] = []

    @bot.listen("CHANNEL_DELETE")
    async def deleted_listener(event: ChannelDeleteEvent) -> None:
        deleted.append(event)

    @bot.listen("VOICE_STATE_UPDATE")
    async def voice_listener(event: VoiceStateEvent) -> None:
        voice.append(event)

    await bot.dispatch(
        "CHANNEL_DELETE",
        {
            "id": "3",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "VOICE_STATE_UPDATE",
        {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "channel_id": "5",
            "channel_domain": "guild.example",
            "user_id": "4",
            "user_domain": "users.example",
            "connected": True,
            "self_mute": False,
        },
        target="https://guild.example",
    )

    assert deleted[0].guild_ref == EntityRef(2, "guild.example")
    assert voice[0].channel_ref == EntityRef(5, "guild.example")
    assert voice[0].user_ref == EntityRef(4, "users.example")
    assert voice[0].connected is True


@pytest.mark.asyncio
async def test_sparse_presence_and_voice_events_inherit_guild_topic_context() -> None:
    bot = client()
    presences: list[PresenceEvent] = []
    voice: list[VoiceStateEvent] = []

    @bot.listen("PRESENCE_UPDATE")
    async def presence_listener(event: PresenceEvent) -> None:
        presences.append(event)

    @bot.listen("VOICE_STATE_UPDATE")
    async def voice_listener(event: VoiceStateEvent) -> None:
        voice.append(event)

    topic = "guild:guild.example:2"
    await bot.dispatch(
        "PRESENCE_UPDATE",
        {"user_id": "4", "user_domain": "users.example", "status": "online"},
        target="https://guild.example",
        topic=topic,
    )
    await bot.dispatch(
        "VOICE_STATE_UPDATE",
        {"channel_id": "5", "user_id": "4", "user_domain": "users.example"},
        target="https://guild.example",
        topic=topic,
    )

    assert presences[0].guild_ref == EntityRef(2, "guild.example")
    assert voice[0].guild_ref == EntityRef(2, "guild.example")
    assert voice[0].channel_ref == EntityRef(5, "guild.example")


def test_emoji_token_is_canonical_and_federation_qualified() -> None:
    bot = client()
    emoji = Emoji.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "7",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "name": "dance",
            "animated": True,
        },
    )
    assert emoji.token == "<a:dance:7@guild.example>"


@pytest.mark.asyncio
async def test_sticker_is_typed_discoverable_and_sendable() -> None:
    bot = client()
    payload = {
        "id": "8",
        "origin_domain": "guild.example",
        "guild_id": "2",
        "guild_domain": "guild.example",
        "name": "party_blob",
        "description": "Celebrating",
        "animated": True,
        "media_hash": "abc",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[payload], {**payload, "id": "9", "content": None}]
    )

    stickers = await bot.stickers(
        EntityRef(2, "guild.example"), target="https://guild.example"
    )
    sticker = stickers[0]
    assert isinstance(sticker, Sticker)
    assert sticker.token == "<sticker:party_blob:8@guild.example>"
    assert sticker.media_url.endswith("/media/stickers/8/thumbnail_512")

    bot.send_message = AsyncMock(
        return_value=Message.from_payload(  # type: ignore[method-assign]
            bot,
            "https://guild.example",
            {
                "id": "9",
                "origin_domain": "guild.example",
                "channel_id": "3",
                "channel_domain": "guild.example",
                "content": sticker.token,
                "created_at": "2026-08-24T00:00:00+00:00",
                "attachments": [],
            },
        )
    )
    await bot.send_sticker(
        EntityRef(3, "guild.example"),
        sticker,
        target="https://guild.example",
        installation_id=77,
    )
    bot.send_message.assert_awaited_once_with(
        EntityRef(3, "guild.example"),
        sticker.token,
        target="https://guild.example",
        installation_id=77,
    )


def test_sticker_gateway_events_are_typed() -> None:
    bot = client()
    created = bot._event_model(
        "GUILD_STICKER_CREATE",
        {
            "id": "8",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "name": "wave",
        },
        target="https://guild.example",
        topic="guild:guild.example:2",
        sequence=1,
    )
    deleted = bot._event_model(
        "GUILD_STICKER_DELETE",
        {
            "id": "8",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
        },
        target="https://guild.example",
        topic="guild:guild.example:2",
        sequence=2,
    )
    assert isinstance(created, Sticker)
    assert isinstance(deleted, StickerDeleteEvent)


@pytest.mark.asyncio
async def test_upload_rejects_non_https_presigned_urls_before_sending_bytes() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "7",
            "origin_domain": "guild.example",
            "filename": "hello.txt",
            "content_type": "text/plain",
            "size": 5,
            "scan_status": "pending",
            "upload_url": "http://storage.example/upload",
        }
    )

    with pytest.raises(ApiError, match="safe HTTPS"):
        await bot.upload_attachment(
            EntityRef(3, "guild.example"),
            b"hello",
            filename="hello.txt",
            content_type="text/plain",
            target="https://guild.example",
        )


def test_channel_model_keeps_version_for_safe_convenience_updates() -> None:
    bot = client()
    channel = Channel.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "3",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 0,
            "version": "2026-08-18T00:00:00+00:00",
        },
    )
    assert channel.version == "2026-08-18T00:00:00+00:00"


@pytest.mark.asyncio
async def test_control_tokens_are_never_sent_to_non_https_application_homes(
    monkeypatch, tmp_path: Path
) -> None:
    def unexpected_client(**_: object) -> object:
        raise AssertionError("an HTTP client must not be created for an unsafe origin")

    monkeypatch.setattr(state_module.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="canonical HTTPS origins"):
        await WorkerState.enroll(
            application_home="http://apps.example",
            application_ref="1@apps.example",
            control_token="secret",
            directory=tmp_path / "worker",
            scopes=[],
            intents=[],
        )

    bot = client()
    monkeypatch.setattr(client_module.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ValueError, match="canonical HTTPS origins"):
        await bot.sync_commands(
            application_home="https://apps.example/control",
            control_token="secret",
        )
    with pytest.raises(ValueError, match="authoritative application_ref domain"):
        await bot.sync_commands(
            application_home="https://attacker.example",
            control_token="secret",
        )


@pytest.mark.asyncio
async def test_control_clients_bind_directly_to_the_authoritative_origin(
    monkeypatch, tmp_path: Path
) -> None:
    client_options: list[dict[str, object]] = []
    requests: list[tuple[str, str, dict[str, object]]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"id": "2"}

    class ControlClient:
        def __init__(self, **kwargs: object) -> None:
            client_options.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, path: str, **kwargs: object) -> Response:
            requests.append(("POST", path, kwargs))
            return Response()

        async def put(self, path: str, **kwargs: object) -> Response:
            requests.append(("PUT", path, kwargs))
            return Response()

    monkeypatch.setattr(state_module.httpx, "AsyncClient", ControlClient)
    state = await WorkerState.enroll(
        application_home="https://APPS.EXAMPLE:443/",
        application_ref="1@apps.example",
        control_token="secret",
        directory=tmp_path / "worker",
        scopes=[],
        intents=[],
    )
    await Client(worker_state=state).sync_commands(
        application_home="https://apps.example",
        control_token="secret",
    )

    assert len(client_options) == 2
    assert all(
        options["base_url"] == "https://apps.example" for options in client_options
    )
    assert all(options["follow_redirects"] is False for options in client_options)
    assert all(options["trust_env"] is False for options in client_options)
    assert [method for method, _, _ in requests] == ["POST", "PUT"]


@pytest.mark.asyncio
async def test_open_dm_binds_subsequent_channel_writes_to_the_exact_installation() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "5",
            "origin_domain": "chat.example",
            "type": 1,
            "bot_installation_id": "77",
        }
    )

    channel = await bot.open_dm(
        "alice@chat.example",
        installation_id=77,
        target="https://chat.example",
    )

    assert channel.bot_installation_id == 77
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["headers"] == {
        "X-Kaede-Bot-Installation": "77"
    }

    bot.send_message = AsyncMock()  # type: ignore[method-assign]
    await channel.send("hello")
    assert bot.send_message.await_args is not None
    assert bot.send_message.await_args.kwargs["installation_id"] == 77


@pytest.mark.asyncio
async def test_dm_message_replies_keep_the_exact_installation_binding() -> None:
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": "hello",
        "created_at": "2026-08-18T00:00:00+00:00",
        "attachments": [],
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[payload, {**payload, "id": "10"}]
    )

    message = await bot.send_message(
        EntityRef(5, "chat.example"),
        "hello",
        target="https://chat.example",
        installation_id=77,
    )
    await message.reply("again")

    assert message.bot_installation_id == 77
    reply = bot.request.await_args_list[1]
    assert reply.kwargs["headers"] == {"X-Kaede-Bot-Installation": "77"}


@pytest.mark.asyncio
async def test_download_attachment_stops_after_the_first_byte_over_limit(
    monkeypatch,
) -> None:
    bot = client()
    bot._redirect_location = AsyncMock(  # type: ignore[method-assign]
        return_value="https://media.example/object"
    )

    class Response:
        is_redirect = False
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.chunks_requested = 0

        def raise_for_status(self) -> None:
            return None

        async def aiter_bytes(self):
            for chunk in (b"123", b"456", b"this must never be requested"):
                self.chunks_requested += 1
                yield chunk

    response = Response()

    class StreamContext:
        async def __aenter__(self) -> Response:
            return response

        async def __aexit__(self, *_: object) -> None:
            return None

    class HttpClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def stream(self, method: str, url: str) -> StreamContext:
            assert (method, url) == ("GET", "https://media.example/object")
            return StreamContext()

    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **_: HttpClient(),
    )

    with pytest.raises(ValueError, match="exceeds max_bytes"):
        await bot.download_attachment(
            EntityRef(9, "chat.example"),
            target="https://chat.example",
            max_bytes=5,
        )
    assert response.chunks_requested == 2


def thread_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "30",
        "origin_domain": "guild.example",
        "guild_id": "2",
        "guild_domain": "guild.example",
        "parent_id": "20",
        "parent_domain": "guild.example",
        "owner_id": "4",
        "owner_domain": "users.example",
        "type": 11,
        "name": "release notes",
        "created_at": "2026-08-24T11:59:59+00:00",
        "last_message_id": "31",
        "last_message_domain": "guild.example",
        "archived": False,
        "locked": False,
        "invitable": None,
        "auto_archive_duration": 1440,
        "archive_timestamp": "2026-08-24T12:00:00+00:00",
        # The starter is intentionally excluded from both Discord thread counters.
        "message_count": 0,
        "total_message_sent": 0,
        "member_count": 2,
        "applied_tag_ids": [7],
        "starter_message": {
            "id": "31",
            "origin_domain": "guild.example",
            "channel_id": "30",
            "channel_domain": "guild.example",
            "content": "Ship it",
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
        },
    }
    payload.update(overrides)
    return payload


def test_forum_and_thread_channel_payloads_are_typed() -> None:
    bot = client()
    forum = Channel.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 15,
            "name": "support",
            "available_tags": [
                {
                    "id": "7",
                    "name": "Resolved",
                    "moderated": True,
                    "emoji_name": "✅",
                }
            ],
            "default_auto_archive_duration": 1440,
            "default_thread_rate_limit_per_user": 10,
            "default_sort_order": 0,
            "default_forum_layout": 2,
            "e2ee_required": True,
        },
    )
    thread = Channel.from_payload(
        bot,
        "https://guild.example",
        thread_payload(newly_created=True),
    )

    assert forum.is_forum is True
    assert forum.available_tags[0].name == "Resolved"
    assert forum.e2ee_required is True
    assert thread.is_thread is True
    assert thread.thread_metadata is not None
    assert thread.thread_metadata.auto_archive_duration == 1440
    assert thread.thread_metadata.invitable is None
    assert thread.message_count == 0
    assert thread.total_message_sent == 0
    assert thread.owner_ref == EntityRef(4, "users.example")
    assert thread.created_at == datetime.fromisoformat("2026-08-24T11:59:59+00:00")
    assert thread.last_message_ref == EntityRef(31, "guild.example")
    assert thread.newly_created is True
    assert thread.starter_message_ref == EntityRef(31, "guild.example")
    assert thread.starter_message is not None
    assert thread.starter_message.content == "Ship it"


def test_parent_thread_projection_is_typed_on_messages() -> None:
    bot = client()
    message = Message.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "30",
            "origin_domain": "guild.example",
            "channel_id": "20",
            "channel_domain": "guild.example",
            "author_id": "4",
            "author_domain": "users.example",
            "content": "release notes",
            "message_type": 18,
            "flags": 1 << 5,
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
            "thread": thread_payload(starter_message=None),
        },
    )

    assert message.message_type == 18
    assert message.thread is not None
    assert message.thread.ref == EntityRef(30, "guild.example")


def test_type_21_starter_exposes_its_resolved_parent_message() -> None:
    bot = client()
    source = {
        "id": "31",
        "origin_domain": "guild.example",
        "channel_id": "20",
        "channel_domain": "guild.example",
        "content": "Ship it",
        "created_at": "2026-08-24T12:00:00+00:00",
        "attachments": [],
    }
    starter = Message.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "31",
            "origin_domain": "guild.example",
            "channel_id": "30",
            "channel_domain": "guild.example",
            "message_type": 21,
            "content": None,
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
            "message_reference": {
                "message_id": "31",
                "message_domain": "guild.example",
                "channel_id": "20",
                "channel_domain": "guild.example",
            },
            "referenced_message": source,
        },
    )

    assert starter.content is None
    assert starter.referenced_message_ref == EntityRef(31, "guild.example")
    assert starter.referenced_message is not None
    assert starter.referenced_message.content == "Ship it"


def test_redacted_thread_starter_and_null_nonapplicable_defaults_are_safe() -> None:
    bot = client()
    thread = Channel.from_payload(
        bot,
        "https://guild.example",
        thread_payload(
            flags="0",
            message_count=None,
            total_message_sent=None,
            member_count=None,
            default_auto_archive_duration=None,
            default_thread_rate_limit_per_user=None,
            default_forum_layout=None,
            starter_message={
                "id": "31",
                "origin_domain": "guild.example",
                "channel_id": "30",
                "channel_domain": "guild.example",
                "content": None,
                "attachments": [],
                "content_unavailable": True,
            },
        ),
    )

    assert thread.message_count == 0
    assert thread.default_auto_archive_duration is None
    assert thread.starter_message is not None
    assert thread.starter_message.content_unavailable is True
    assert thread.starter_message.created_at is None


def test_forum_create_accepts_discord_message_response_alias() -> None:
    bot = client()
    starter = thread_payload()["starter_message"]
    thread = Channel.from_payload(
        bot,
        "https://guild.example",
        thread_payload(starter_message=None, message=starter),
    )

    assert thread.starter_message is not None
    assert thread.starter_message.content == "Ship it"


@pytest.mark.asyncio
async def test_start_thread_uses_atomic_nested_starter_payload() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]

    thread = await bot.start_thread(
        EntityRef(20, "guild.example"),
        "release notes",
        target="https://guild.example",
        type=11,
        content="Ship it",
        attachment_ids=[90],
        applied_tag_ids=[7],
        auto_archive_duration=1440,
        client_nonce="nonce-1",
    )

    assert thread.ref == EntityRef(30, "guild.example")
    assert bot.request.await_args is not None
    assert bot.request.await_args.args == (
        "POST",
        "/api/v1/bots/channels/20@guild.example/threads",
    )
    assert bot.request.await_args.kwargs["json"] == {
        "name": "release notes",
        "type": 11,
        "auto_archive_duration": 1440,
        "applied_tag_ids": ["7"],
        "message": {
            "content": "Ship it",
            "attachment_ids": ["90"],
            "client_nonce": "nonce-1",
        },
    }

    forum = Channel.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 15,
            "name": "support",
        },
    )
    with pytest.raises(ValueError, match="2000"):
        await forum.create_post("Too long", "x" * 2001)
    with pytest.raises(ValueError, match="content or an attachment"):
        await forum.create_post("Empty")


@pytest.mark.asyncio
async def test_message_start_thread_uses_message_scoped_route() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]
    message = Message.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "31",
            "origin_domain": "guild.example",
            "channel_id": "20",
            "channel_domain": "guild.example",
            "content": "Ship it",
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
        },
    )

    await message.start_thread("release notes", auto_archive_duration=60)

    assert bot.request.await_args is not None
    assert bot.request.await_args.args == (
        "POST",
        "/api/v1/bots/channels/20@guild.example/messages/31@guild.example/threads",
    )
    assert bot.request.await_args.kwargs["json"] == {
        "name": "release notes",
        "auto_archive_duration": 60,
    }


@pytest.mark.asyncio
async def test_thread_delete_uses_thread_scoped_route() -> None:
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]
    thread = Channel.from_payload(
        bot,
        "https://guild.example",
        thread_payload(),
    )

    await thread.delete()

    bot.request.assert_awaited_once_with(
        "DELETE",
        "/api/v1/bots/channels/30@guild.example",
        target="https://guild.example",
    )


@pytest.mark.asyncio
async def test_thread_edit_serializes_applied_tags_as_wire_snowflakes() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]
    thread = Channel.from_payload(bot, "https://guild.example", thread_payload())

    await thread.edit_thread(applied_tag_ids=[7, 8])

    bot.request.assert_awaited_once_with(
        "PATCH",
        "/api/v1/bots/channels/30@guild.example",
        target="https://guild.example",
        json={"applied_tag_ids": ["7", "8"]},
    )


@pytest.mark.asyncio
async def test_thread_membership_exposes_kaede_notification_preferences() -> None:
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]
    thread = Channel.from_payload(bot, "https://guild.example", thread_payload())

    await thread.join(notification_level="all")

    bot.request.assert_awaited_once_with(
        "PUT",
        "/api/v1/bots/channels/30@guild.example/thread-members/@me",
        target="https://guild.example",
        json={"flags": 0, "notification_level": "all"},
    )
    with pytest.raises(ValueError, match="notification level"):
        await thread.join(notification_level="loudest")


@pytest.mark.asyncio
async def test_adding_another_thread_member_does_not_set_their_preferences() -> None:
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]
    thread = Channel.from_payload(bot, "https://guild.example", thread_payload())

    await thread.add_member(EntityRef(9, "users.example"))

    bot.request.assert_awaited_once_with(
        "PUT",
        "/api/v1/bots/channels/30@guild.example/thread-members/9@users.example",
        target="https://guild.example",
    )


@pytest.mark.asyncio
async def test_thread_member_fetch_and_listing_are_paginated_and_typed() -> None:
    bot = client()
    payload = {
        "id": "30",
        "thread_domain": "guild.example",
        "guild_id": "2",
        "guild_domain": "guild.example",
        "user_id": "4",
        "user_domain": "users.example",
        "join_timestamp": "2026-08-24T12:00:01+00:00",
        "flags": 0,
        "member": {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "user": {
                "id": "4",
                "origin_domain": "users.example",
                "username": "alice",
                "display_name": "Alice",
            },
            "nickname": None,
            "joined_at": "2026-08-24T11:00:00+00:00",
            "role_ids": [],
        },
        "presence": None,
    }
    bot.request = AsyncMock(side_effect=[[payload], payload])  # type: ignore[method-assign]
    thread = Channel.from_payload(bot, "https://guild.example", thread_payload())

    members = await bot.thread_members(
        thread.ref,
        target=thread.target,
        after=EntityRef(3, "users.example"),
        limit=500,
        with_member=True,
    )
    fetched = await thread.fetch_member(EntityRef(4, "users.example"), with_member=True)

    assert members[0].member is not None
    assert members[0].member.name == "Alice"
    assert fetched.user_ref == EntityRef(4, "users.example")
    assert bot.request.await_args_list[0].kwargs["params"] == {
        "limit": 100,
        "with_member": "true",
        "after": "3@users.example",
    }
    assert bot.request.await_args_list[1].args == (
        "GET",
        "/api/v1/bots/channels/30@guild.example/thread-members/4@users.example",
    )
    assert bot.request.await_args_list[1].kwargs["params"] == {"with_member": "true"}


@pytest.mark.asyncio
async def test_thread_listing_and_membership_use_discord_shaped_envelope() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "threads": [thread_payload()],
            "members": [
                {
                    "id": "30",
                    "thread_domain": "guild.example",
                    "user_id": "4",
                    "user_domain": "users.example",
                    "join_timestamp": "2026-08-24T12:00:01+00:00",
                    "flags": 0,
                }
            ],
            "has_more": True,
            "next_cursor": "opaque.cursor",
        }
    )

    page = await bot.fetch_threads(
        EntityRef(20, "guild.example"),
        target="https://guild.example",
        archived=True,
        cursor="opaque.previous",
        limit=500,
        tag_ids=[7, 8],
        query="release",
        sort_order=1,
    )

    assert page.has_more is True
    assert page.next_cursor == "opaque.cursor"
    assert page.threads[0].ref == EntityRef(30, "guild.example")
    assert page.members[0].user_ref == EntityRef(4, "users.example")
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["params"] == {
        "archived": "true",
        "cursor": "opaque.previous",
        "limit": 100,
        "tag_id": ["7", "8"],
        "query": "release",
        "sort_order": 1,
    }

    with pytest.raises(ValueError, match="before or cursor"):
        await bot.fetch_threads(
            EntityRef(20, "guild.example"),
            before=datetime.fromisoformat("2026-08-24T12:00:00+00:00"),
            cursor="opaque.previous",
        )

    await bot.fetch_threads(
        EntityRef(20, "guild.example"),
        target="https://guild.example",
        include_archived=True,
        sort_order=0,
    )
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["params"] == {
        "include_archived": "true",
        "limit": 50,
        "sort_order": 0,
    }

    with pytest.raises(ValueError, match="include_archived"):
        await bot.fetch_threads(
            EntityRef(20, "guild.example"),
            archived=True,
            include_archived=True,
        )


@pytest.mark.asyncio
async def test_guild_active_threads_keeps_the_guild_target() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={"threads": [thread_payload()], "members": [], "has_more": False}
    )
    guild = Guild.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "2",
            "origin_domain": "guild.example",
            "name": "Guild",
        },
    )

    page = await guild.active_threads()

    assert page.threads[0].ref == EntityRef(30, "guild.example")
    bot.request.assert_awaited_once_with(
        "GET",
        "/api/v1/bots/guilds/2@guild.example/threads/active",
        target="https://guild.example",
    )


@pytest.mark.asyncio
async def test_thread_gateway_sync_and_member_delta_are_typed() -> None:
    bot = client()
    sync: list[ThreadListSyncEvent] = []
    deltas: list[ThreadMembersUpdateEvent] = []

    @bot.listen("THREAD_LIST_SYNC")
    async def sync_listener(event: ThreadListSyncEvent) -> None:
        sync.append(event)

    @bot.listen("THREAD_MEMBERS_UPDATE")
    async def delta_listener(event: ThreadMembersUpdateEvent) -> None:
        deltas.append(event)

    member = {
        "id": "30",
        "thread_domain": "guild.example",
        "guild_id": "2",
        "guild_domain": "guild.example",
        "user_id": "4",
        "user_domain": "users.example",
        "join_timestamp": "2026-08-24T12:00:01+00:00",
        "flags": 0,
    }
    await bot.dispatch(
        "THREAD_LIST_SYNC",
        {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "channel_ids": ["20"],
            "threads": [thread_payload()],
            "members": [member],
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "THREAD_MEMBERS_UPDATE",
        {
            "id": "30",
            "thread_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "member_count": 2,
            "added_members": [member],
            "removed_member_ids": ["5"],
            "removed_member_refs": [{"id": "5", "origin_domain": "users.example"}],
        },
        target="https://guild.example",
    )

    assert sync[0].channel_refs == (EntityRef(20, "guild.example"),)
    assert sync[0].threads[0].name == "release notes"
    assert deltas[0].member_count == 2
    assert deltas[0].removed_member_refs == (EntityRef(5, "users.example"),)


@pytest.mark.asyncio
async def test_forum_channel_create_serializes_nested_wire_snowflakes() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 15,
            "name": "support",
        }
    )

    available_tags = [{"id": 7, "name": "Resolved", "moderated": True, "emoji_id": 91}]
    default_reaction = {"emoji_id": 92}

    await bot.create_channel(
        EntityRef(2, "guild.example"),
        "support",
        target="https://guild.example",
        type=15,
        topic="Read before posting",
        available_tags=available_tags,
        default_reaction_emoji=default_reaction,
        default_sort_order=0,
        default_forum_layout=1,
        e2ee_required=True,
    )

    assert bot.request.await_args is not None
    body = bot.request.await_args.kwargs["json"]
    assert body["type"] == 15
    assert body["available_tags"] == [
        {"id": "7", "name": "Resolved", "moderated": True, "emoji_id": "91"}
    ]
    assert body["default_reaction_emoji"] == {"emoji_id": "92"}
    assert body["e2ee_required"] is True
    assert available_tags[0]["id"] == 7
    assert available_tags[0]["emoji_id"] == 91
    assert default_reaction["emoji_id"] == 92


@pytest.mark.asyncio
async def test_forum_channel_edit_serializes_nested_wire_snowflakes() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 15,
            "name": "support",
        }
    )

    await bot.edit_channel(
        EntityRef(2, "guild.example"),
        EntityRef(20, "guild.example"),
        target="https://guild.example",
        version="channel-v1",
        available_tags=[
            {"id": 7, "name": "Resolved", "moderated": True, "emoji_id": 91}
        ],
        default_reaction_emoji={"emoji_id": 92},
    )

    bot.request.assert_awaited_once_with(
        "PATCH",
        "/api/v1/bots/guilds/2@guild.example/channels/20@guild.example",
        target="https://guild.example",
        json={
            "available_tags": [
                {
                    "id": "7",
                    "name": "Resolved",
                    "moderated": True,
                    "emoji_id": "91",
                }
            ],
            "default_reaction_emoji": {"emoji_id": "92"},
        },
        headers={"If-Match": "channel-v1"},
    )
