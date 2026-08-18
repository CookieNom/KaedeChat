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
    Interaction,
    Member,
    Message,
    PresenceEvent,
    ReactionEvent,
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
