from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.api.bot_gateway as bot_gateway
from app.api.applications import SUPPORTED_INTENTS
from app.api.bot_gateway import (
    GatewayInstallationGrant,
    GatewayProtocolError,
    event_intent,
    event_intents,
    event_permission_options,
    event_scope,
    filtered_event,
)
from app.core.bot_intents import (
    BOT_INTENT_ALIASES,
    BOT_INTENT_NAMES,
    DISCORD_BOT_INTENTS,
    KAEDE_BOT_INTENTS,
)
from app.core.gateway_ops import EVENT_NAMES
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Guild


def principal(*, scopes: set[str], intents: set[str]) -> SimpleNamespace:
    return SimpleNamespace(
        user=SimpleNamespace(id=10, origin_domain="apps.example"),
        application=SimpleNamespace(id=20, origin_domain="apps.example"),
        scopes=frozenset(scopes),
        intents=frozenset(intents),
        worker=SimpleNamespace(id=30),
    )


class GatewaySessionContext:
    def __init__(self, session: object | None = None) -> None:
        self.session = session or SimpleNamespace()

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def command_runtime(
    *,
    scopes: set[str],
    intents: set[str],
    session: object | None = None,
) -> SimpleNamespace:
    guild = Guild(
        id=42,
        origin_domain="guild.example",
        name="Guild",
        owner_id=7,
        owner_domain="guild.example",
    )
    bot = principal(scopes=scopes, intents=intents)
    bot.user.is_local = False
    return SimpleNamespace(
        websocket=SimpleNamespace(send_json=AsyncMock()),
        sessionmaker=lambda: GatewaySessionContext(session),
        redis=SimpleNamespace(),
        guilds=[guild],
        principal=bot,
        authorization_guard=SimpleNamespace(e2ee_device_id="kbe_" + "a" * 43),
        topic_grants={
            "guild:guild.example:42": (
                frozenset(intents),
                frozenset(scopes),
                9,
                frozenset(),
                0,
                (),
            )
        },
    )


@pytest.mark.asyncio
async def test_bot_gateway_presence_update_preserves_documented_activity_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = command_runtime(scopes=set(), intents=set())
    broadcast = AsyncMock(return_value=("online", 11))
    monkeypatch.setattr(bot_gateway, "broadcast_presence_preference", broadcast)

    heartbeat = await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {
            "op": 3,
            "d": {
                "since": 123,
                "activities": [
                    {
                        "name": "Build queue",
                        "state": "Federating",
                        "type": 1,
                        "url": "https://www.twitch.tv/kaede",
                    }
                ],
                "status": "online",
                "afk": False,
            },
        },
        7.0,
    )

    assert heartbeat == 7.0
    assert broadcast.await_args.kwargs == {
        "activities": [
            {
                "name": "Build queue",
                "state": "Federating",
                "type": 1,
                "url": "https://www.twitch.tv/kaede",
            }
        ],
        "since": 123,
        "afk": False,
    }
    assert broadcast.await_args.args[2:] == (
        "online",
        ["guild:guild.example:42"],
    )


@pytest.mark.asyncio
async def test_bot_presence_updates_identity_home_for_signed_federation_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = command_runtime(scopes=set(), intents=set())
    runtime.principal.user.is_local = True
    runtime.guilds = []
    runtime.topic_grants = {}
    broadcast = AsyncMock(return_value=("dnd", 17))
    scheduled = Mock()
    monkeypatch.setattr(bot_gateway, "broadcast_presence_preference", broadcast)
    monkeypatch.setattr("app.gateway.schedule_presence_fanout", scheduled)

    await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {
            "op": 3,
            "d": {
                "since": None,
                "activities": [],
                "status": "dnd",
                "afk": False,
            },
        },
        1.0,
    )

    assert broadcast.await_args.args[2:] == ("dnd", [])
    scheduled.assert_called_once_with(runtime.principal.user, "dnd", 17)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"since": None, "activities": [], "status": "online"},
        {"since": True, "activities": [], "status": "online", "afk": False},
        {
            "since": None,
            "activities": [{"name": "x", "type": True}],
            "status": "online",
            "afk": False,
        },
        {
            "since": None,
            "activities": [{"name": "x", "type": 1, "url": "https://tracker.example/stream"}],
            "status": "online",
            "afk": False,
        },
    ],
)
async def test_bot_gateway_presence_rejects_ambiguous_or_unsupported_payloads(
    data: dict[str, object],
) -> None:
    runtime = command_runtime(scopes=set(), intents=set())

    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {"op": 3, "d": data},
            1.0,
        )

    assert exc.value.code == 4400


@pytest.mark.asyncio
async def test_bot_gateway_member_request_requires_exact_scope_and_presence_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = command_runtime(
        scopes={"members.read"},
        intents={"guild_members", "guild_presences"},
    )
    monkeypatch.setattr(
        "app.api.bots.installation_for_guild",
        AsyncMock(
            return_value=(
                runtime.guilds[0],
                SimpleNamespace(
                    id=9,
                    granted_intents=["guild_members", "guild_presences"],
                ),
            )
        ),
    )
    member = {
        "guild_id": "42",
        "guild_domain": "guild.example",
        "user": {"id": "7", "origin_domain": "member.example"},
        "presence": "idle",
        "presence_details": {
            "status": "idle",
            "activities": [{"name": "Build queue", "type": 0}],
            "client_status": {"web": "idle"},
        },
    }
    payloads = AsyncMock(return_value=[member])
    monkeypatch.setattr("app.gateway.member_payloads", payloads)

    await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {
            "op": 8,
            "d": {
                "guild_id": "42",
                "query": "map",
                "limit": 25,
                "presences": True,
                "nonce": "member-search",
            },
        },
        2.0,
    )

    assert payloads.await_args.kwargs["query_prefix"] is True
    assert payloads.await_args.kwargs["include_presence"] is True
    assert payloads.await_args.kwargs["include_presence_details"] is True
    event = runtime.websocket.send_json.await_args.args[0]
    assert event["t"] == "GUILD_MEMBERS_CHUNK"
    assert event["topic"] == "guild:guild.example:42"
    assert event["d"] == {
        "guild_id": "42",
        "guild_domain": "guild.example",
        "members": [member],
        "chunk_index": 0,
        "chunk_count": 1,
        "not_found": [],
        "presences": [
            {
                "user": {"id": "7", "origin_domain": "member.example"},
                "status": "idle",
                "activities": [{"name": "Build queue", "type": 0}],
                "client_status": {"web": "idle"},
            }
        ],
        "nonce": "member-search",
    }

    denied = command_runtime(scopes={"members.read"}, intents={"guild_members"})
    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            denied,  # type: ignore[arg-type]
            {
                "op": 8,
                "d": {
                    "guild_id": "42",
                    "query": "map",
                    "limit": 25,
                    "presences": True,
                },
            },
            2.0,
        )
    assert exc.value.code == 4403

    stale = command_runtime(
        scopes={"members.read"},
        intents={"guild_members", "guild_presences"},
    )
    stale.principal.intents = frozenset({"guild_members"})
    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            stale,  # type: ignore[arg-type]
            {
                "op": 8,
                "d": {
                    "guild_id": "42",
                    "query": "map",
                    "limit": 25,
                    "presences": True,
                },
            },
            2.0,
        )
    assert exc.value.code == 4403

    monkeypatch.setattr(
        "app.api.bots.installation_for_guild",
        AsyncMock(
            return_value=(
                runtime.guilds[0],
                SimpleNamespace(id=9, granted_intents=["guild_members"]),
            )
        ),
    )
    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {
                "op": 8,
                "d": {
                    "guild_id": "42",
                    "query": "map",
                    "limit": 25,
                    "presences": True,
                },
            },
            2.0,
        )
    assert exc.value.code == 4009


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"guild_id": "42", "query": "", "limit": True},
        {"guild_id": "42", "query": "x", "limit": 0},
        {"guild_id": "42", "query": "x", "limit": 1, "user_ids": ["7"]},
        {"guild_id": "42", "user_ids": "07"},
        {"guild_id": "42", "user_ids": ["7", "7"]},
    ],
)
async def test_bot_gateway_member_request_rejects_ambiguous_inputs(
    data: dict[str, object],
) -> None:
    runtime = command_runtime(scopes={"members.read"}, intents={"guild_members"})
    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {"op": 8, "d": data},
            2.0,
        )
    assert exc.value.code == 4400


@pytest.mark.asyncio
async def test_bot_gateway_voice_join_binds_worker_device_and_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = command_runtime(
        scopes={"voice.connect", "voice.listen", "voice.speak"},
        intents={"guild_voice_states"},
    )
    monkeypatch.setattr(
        "app.api.bots.installation_for_guild",
        AsyncMock(
            return_value=(
                runtime.guilds[0],
                SimpleNamespace(id=9, granted_intents=["guild_voice_states"]),
            )
        ),
    )
    monkeypatch.setattr(
        "app.voice.state.occupant_for_identity",
        AsyncMock(return_value=None),
    )
    token = SimpleNamespace(
        model_dump=lambda **_kwargs: {
            "token": "secret",
            "connection_id": "c" * 43,
            "channel_id": "99",
            "channel_domain": "guild.example",
        }
    )
    mint = AsyncMock(return_value=token)
    monkeypatch.setattr("app.api.bot_voice.bot_channel_voice_token_service", mint)

    await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {
            "op": 4,
            "d": {
                "guild_id": "42",
                "channel_id": "99",
                "self_mute": False,
                "self_deaf": True,
            },
        },
        3.0,
    )

    request = mint.await_args.args[1]
    assert request.sender_device_id == "kbe_" + "a" * 43
    assert request.takeover is True
    assert request.listen is True
    assert request.speak is True
    assert mint.await_args.kwargs == {"self_mute": False, "self_deaf": True}
    assert runtime.websocket.send_json.await_args.args[0] == {
        "op": 0,
        "t": "VOICE_TOKEN",
        "d": {
            "token": "secret",
            "connection_id": "c" * 43,
            "channel_id": "99",
            "channel_domain": "guild.example",
            "guild_id": "42",
            "guild_domain": "guild.example",
        },
        "s": 0,
        "topic": "guild:guild.example:42",
    }

    monkeypatch.setattr(
        "app.api.bots.installation_for_guild",
        AsyncMock(return_value=(runtime.guilds[0], SimpleNamespace(id=9, granted_intents=[]))),
    )
    with pytest.raises(GatewayProtocolError) as exc:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {
                "op": 4,
                "d": {
                    "guild_id": "42",
                    "channel_id": "99",
                    "self_mute": False,
                    "self_deaf": False,
                },
            },
            3.0,
        )
    assert exc.value.code == 4009
    assert mint.await_count == 1


@pytest.mark.asyncio
async def test_bot_gateway_voice_rejects_boolean_ids_and_private_opcode_12() -> None:
    runtime = command_runtime(
        scopes={"voice.connect"},
        intents={"guild_voice_states"},
    )
    with pytest.raises(GatewayProtocolError) as invalid:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {
                "op": 4,
                "d": {
                    "guild_id": True,
                    "channel_id": "99",
                    "self_mute": False,
                    "self_deaf": False,
                },
            },
            3.0,
        )
    assert invalid.value.code == 4400

    with pytest.raises(GatewayProtocolError) as private:
        await bot_gateway.handle_gateway_client_frame(
            runtime,  # type: ignore[arg-type]
            {"op": 12, "d": {"guild_id": "42", "ranges": [[0, 99]]}},
            3.0,
        )
    assert private.value.code == 4400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op", "scopes", "intents", "event_name", "request_data"),
    [
        (
            43,
            {"channels.read"},
            {"guilds"},
            "CHANNEL_INFO",
            {"guild_id": "42", "fields": ["status", "voice_start_time"]},
        ),
        (
            31,
            {"soundboard.read"},
            {"guild_expressions"},
            "GUILD_SOUNDBOARD_SOUNDS_UPDATE",
            {"guild_ids": ["42"]},
        ),
    ],
)
async def test_bot_gateway_requested_resources_recheck_runtime_grants(
    monkeypatch: pytest.MonkeyPatch,
    op: int,
    scopes: set[str],
    intents: set[str],
    event_name: str,
    request_data: dict[str, object],
) -> None:
    local_domain = "local.example"
    monkeypatch.setattr(
        bot_gateway,
        "get_settings",
        lambda: SimpleNamespace(domain=local_domain),
    )
    guild = Guild(
        id=42,
        origin_domain=local_domain,
        name="Guild",
        owner_id=7,
        owner_domain=local_domain,
    )

    class Context:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    socket = SimpleNamespace(send_json=AsyncMock())
    runtime = SimpleNamespace(
        websocket=socket,
        sessionmaker=lambda: Context(),
        redis=SimpleNamespace(),
        guilds=[guild],
        principal=principal(scopes=scopes, intents=intents),
        topic_grants={f"guild:{local_domain}:42": (frozenset(intents), frozenset(scopes), 9)},
    )
    monkeypatch.setattr(
        bot_gateway,
        "visible_guild_channel_info",
        AsyncMock(
            return_value={
                "guild_id": "42",
                "channels": [{"id": "99", "status": None, "voice_start_time": 1}],
            }
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "app.voice.channel_info.visible_guild_channel_info",
        AsyncMock(
            return_value={
                "guild_id": "42",
                "channels": [{"id": "99", "status": None, "voice_start_time": 1}],
            }
        ),
    )
    monkeypatch.setattr(
        "app.api.soundboard.gateway_soundboard_sounds",
        AsyncMock(return_value=[{"sound_id": "5", "name": "Air horn"}]),
    )

    heartbeat = await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {"op": op, "d": request_data},
        123.0,
    )

    assert heartbeat == 123.0
    assert socket.send_json.await_args.args[0]["t"] == event_name


@pytest.mark.asyncio
async def test_bot_gateway_channel_info_proxies_remote_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=42,
        origin_domain="guild.example",
        name="Guild",
        owner_id=7,
        owner_domain="guild.example",
    )
    runtime = command_runtime(
        scopes={"channels.read"},
        intents={"guilds"},
    )
    runtime.guilds = [guild]
    runtime.principal.user.is_local = True
    runtime.principal.user.account_type = "bot"
    runtime.topic_grants = {
        "guild:guild.example:42": (
            frozenset({"guilds"}),
            frozenset({"channels.read"}),
            9,
        )
    }
    monkeypatch.setattr(
        bot_gateway,
        "get_settings",
        lambda: SimpleNamespace(domain="apps.example"),
    )
    proxy = AsyncMock(
        return_value=SimpleNamespace(
            body={
                "guild_id": "42",
                "guild_domain": "guild.example",
                "channels": [
                    {
                        "id": "99",
                        "origin_domain": "guild.example",
                        "guild_id": "42",
                        "guild_domain": "guild.example",
                        "status": "Pairing",
                        "voice_start_time": 1_777_777_777,
                    }
                ],
            }
        )
    )
    local_projection = AsyncMock()
    monkeypatch.setattr(
        "app.federation.guild_management.proxy_remote_guild_management",
        proxy,
    )
    monkeypatch.setattr(
        "app.voice.channel_info.visible_guild_channel_info",
        local_projection,
    )

    await bot_gateway.handle_gateway_client_frame(
        runtime,  # type: ignore[arg-type]
        {
            "op": 43,
            "d": {
                "guild_id": "42",
                "fields": ["status", "voice_start_time"],
            },
        },
        123.0,
    )

    proxy.assert_awaited_once()
    assert proxy.await_args.args[2:] == (
        EntityRef("42@guild.example"),
        runtime.principal.user,
        "voice_channel_info.get",
        {"fields": ["status", "voice_start_time"]},
    )
    local_projection.assert_not_awaited()
    assert runtime.websocket.send_json.await_args.args[0] == {
        "op": bot_gateway.GatewayOp.DISPATCH,
        "t": "CHANNEL_INFO",
        "d": proxy.return_value.body,
        "s": 0,
        "topic": "guild:guild.example:42",
    }


def direct_grant(
    installation_id: int,
    *,
    scopes: set[str] | None = None,
    intents: set[str] | None = None,
    user_installation: bool = False,
    installation_revision: int | None = None,
) -> GatewayInstallationGrant:
    return GatewayInstallationGrant(
        installation_id=installation_id,
        user_installation=user_installation,
        scopes=frozenset(scopes or ()),
        intents=frozenset(intents or ()),
        installation_revision=installation_revision,
    )


def test_intent_registry_is_total_additive_and_preserves_published_aliases() -> None:
    assert len(BOT_INTENT_NAMES) == len(set(BOT_INTENT_NAMES))
    assert frozenset(BOT_INTENT_NAMES) == SUPPORTED_INTENTS
    assert set(DISCORD_BOT_INTENTS).isdisjoint(KAEDE_BOT_INTENTS)
    assert BOT_INTENT_ALIASES == {
        "voice_states": "guild_voice_states",
        "message_reactions": "guild_message_reactions",
        "guild_typing": "guild_message_typing",
    }
    assert set(BOT_INTENT_ALIASES).isdisjoint(DISCORD_BOT_INTENTS)


def test_remaining_message_and_voice_events_are_published_protocol_names() -> None:
    assert {
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
        "CHANNEL_INFO",
        "CHANNEL_PINS_UPDATE",
        "MESSAGE_DELETE_BULK",
        "MESSAGE_REACTION_REMOVE_ALL",
        "MESSAGE_REACTION_REMOVE_EMOJI",
        "VOICE_CHANNEL_EFFECT_SEND",
        "VOICE_CHANNEL_START_TIME_UPDATE",
        "VOICE_CHANNEL_STATUS_UPDATE",
    } <= set(EVENT_NAMES)


def test_discord_canonical_intents_and_kaede_aliases_authorize_same_guild_events() -> None:
    assert event_intent("MESSAGE_REACTION_REMOVE_ALL") == "guild_message_reactions"
    assert event_intents("MESSAGE_REACTION_REMOVE_ALL") == frozenset(
        {"guild_message_reactions", "message_reactions"}
    )
    assert event_intent("TYPING_START") == "guild_message_typing"
    assert event_intents("TYPING_START") == frozenset({"guild_message_typing", "guild_typing"})
    assert event_intent("VOICE_STATE_UPDATE") == "guild_voice_states"
    assert event_intents("VOICE_STATE_UPDATE") == frozenset({"guild_voice_states", "voice_states"})
    assert event_intent("GUILD_SOUNDBOARD_SOUND_CREATE") == "guild_expressions"
    assert event_scope("MESSAGE_DELETE_BULK") == "messages.metadata"


def test_user_topics_use_independent_direct_message_intents() -> None:
    event = {
        "t": "MESSAGE_REACTION_REMOVE_ALL",
        "topic_seq": 4,
        "d": {
            "message_id": "1",
            "message_domain": "apps.example",
            "channel_id": "2",
            "channel_domain": "apps.example",
        },
    }
    allowed = principal(scopes={"reactions.read", "dm.send"}, intents={"direct_message_reactions"})
    direct = (
        direct_grant(
            80,
            scopes={"reactions.read", "dm.send"},
            intents={"direct_message_reactions"},
        ),
    )
    assert (
        filtered_event(
            allowed,
            event,
            {"direct_message_reactions"},
            {"reactions.read"},
            topic="user:apps.example:10",
            installation_grants=direct,
        )
        is not None
    )
    guild_only = principal(
        scopes={"reactions.read", "dm.send"}, intents={"guild_message_reactions"}
    )
    assert (
        filtered_event(
            guild_only,
            event,
            {"guild_message_reactions"},
            {"reactions.read"},
            topic="user:apps.example:10",
            installation_grants=direct,
        )
        is None
    )


@pytest.mark.parametrize(
    ("event_type", "intent", "scope"),
    [
        ("MESSAGE_REACTION_ADD", "direct_message_reactions", "reactions.read"),
        ("MESSAGE_POLL_VOTE_ADD", "direct_message_polls", "polls.read"),
        ("TYPING_START", "direct_message_typing", "channels.read"),
    ],
)
def test_direct_events_require_one_installation_to_grant_intent_and_scope(
    event_type: str,
    intent: str,
    scope: str,
) -> None:
    bot = principal(scopes={scope, "dm.send"}, intents={intent})
    event = {"t": event_type, "topic_seq": 4, "d": {"id": "1"}}
    split_grants = (
        direct_grant(80, intents={intent}, scopes={"dm.send"}),
        direct_grant(81, scopes={scope, "dm.send"}),
    )
    assert (
        filtered_event(
            bot,
            event,
            {intent},
            {scope},
            topic="user:apps.example:10",
            installation_grants=split_grants,
        )
        is None
    )

    exact_grant = (direct_grant(82, intents={intent}, scopes={scope, "dm.send"}),)
    assert (
        filtered_event(
            bot,
            event,
            {intent},
            {scope},
            topic="user:apps.example:10",
            installation_grants=exact_grant,
        )
        is not None
    )


def test_direct_noninteraction_events_require_dm_send_on_the_same_grant() -> None:
    event = {"t": "TYPING_START", "topic_seq": 4, "d": {"channel_id": "1"}}
    intents = {"direct_message_typing"}
    ordinary_scopes = {"channels.read"}
    authorized_principal = principal(scopes={*ordinary_scopes, "dm.send"}, intents=intents)

    assert (
        filtered_event(
            authorized_principal,
            event,
            intents,
            ordinary_scopes,
            topic="user:apps.example:10",
            installation_grants=(direct_grant(80, intents=intents, scopes=ordinary_scopes),),
        )
        is None
    )
    assert (
        filtered_event(
            principal(scopes=ordinary_scopes, intents=intents),
            event,
            intents,
            ordinary_scopes,
            topic="user:apps.example:10",
            installation_grants=(
                direct_grant(80, intents=intents, scopes={*ordinary_scopes, "dm.send"}),
            ),
        )
        is None
    )
    assert (
        filtered_event(
            authorized_principal,
            event,
            intents,
            ordinary_scopes,
            topic="user:apps.example:10",
            installation_grants=(
                direct_grant(80, intents=intents, scopes={*ordinary_scopes, "dm.send"}),
            ),
        )
        is not None
    )


def test_direct_message_rich_fields_cannot_borrow_another_installations_grants() -> None:
    scopes = {"messages.metadata", "messages.content", "attachments.read", "dm.send"}
    intents = {"direct_messages", "message_content"}
    bot = principal(scopes=scopes, intents=intents)
    event = {
        "t": "MESSAGE_CREATE",
        "topic_seq": 5,
        "d": {"content": "secret", "attachments": [{"id": "9"}]},
    }

    # Neither half of the base event grant can authorize it alone.
    split_base = (
        direct_grant(80, intents={"direct_messages"}, scopes={"dm.send"}),
        direct_grant(81, scopes={"messages.metadata", "dm.send"}),
    )
    assert (
        filtered_event(
            bot,
            event,
            intents,
            scopes,
            topic="user:apps.example:10",
            installation_grants=split_base,
        )
        is None
    )

    # A base-authorized installation receives the DM content exception, but
    # cannot borrow Kaede's separate attachment grant from another install.
    split_rich_fields = (
        direct_grant(
            80,
            intents={"direct_messages"},
            scopes={"messages.metadata", "dm.send"},
        ),
        direct_grant(
            81,
            intents={"message_content"},
            scopes={"messages.content", "attachments.read", "dm.send"},
        ),
    )
    redacted = filtered_event(
        bot,
        event,
        intents,
        scopes,
        topic="user:apps.example:10",
        installation_grants=split_rich_fields,
    )
    assert redacted is not None
    assert redacted["d"]["content"] == "secret"
    assert redacted["d"]["attachments"] == []
    assert "content_unavailable" not in redacted["d"]
    assert redacted["d"]["attachments_unavailable"] is True

    exact = (
        direct_grant(
            82,
            intents=intents,
            scopes=scopes,
        ),
    )
    visible = filtered_event(
        bot,
        event,
        intents,
        scopes,
        topic="user:apps.example:10",
        installation_grants=exact,
    )
    assert visible is not None
    assert visible["d"]["content"] == "secret"
    assert visible["d"]["attachments"] == [{"id": "9"}]
    assert visible["d"]["bot_installation_id"] == "82"

    # Even two independently base-authorized installations cannot compose a
    # richer payload. The selected installation owns the whole projection.
    split_complete_fields = (
        direct_grant(
            83,
            intents={"direct_messages", "message_content"},
            scopes={"messages.metadata", "messages.content", "dm.send"},
        ),
        direct_grant(
            84,
            intents={"direct_messages"},
            scopes={"messages.metadata", "attachments.read", "dm.send"},
        ),
    )
    one_projection = filtered_event(
        bot,
        event,
        intents,
        scopes,
        topic="user:apps.example:10",
        installation_grants=split_complete_fields,
    )
    assert one_projection is not None
    assert one_projection["d"]["bot_installation_id"] == "83"
    assert one_projection["d"]["content"] == "secret"
    assert one_projection["d"]["attachments"] == []
    assert one_projection["d"]["attachments_unavailable"] is True


def test_direct_thread_history_cannot_borrow_another_installations_scope() -> None:
    scopes = {
        "channels.read",
        "messages.metadata",
        "messages.content",
        "attachments.read",
        "messages.history",
        "dm.send",
    }
    intents = {"guilds", "message_content"}
    bot = principal(scopes=scopes, intents=intents)
    event = {
        "t": "THREAD_LIST_SYNC",
        "topic_seq": 6,
        "d": {
            "threads": [
                {
                    "id": "8",
                    "starter_message": {
                        "content": "historical secret",
                        "attachments": [{"id": "9"}],
                    },
                }
            ]
        },
    }
    base_scopes = scopes - {"messages.history"}
    split_history = (
        direct_grant(80, intents=intents, scopes=base_scopes),
        direct_grant(81, scopes={"messages.history", "dm.send"}),
    )
    redacted = filtered_event(
        bot,
        event,
        intents,
        scopes,
        topic="user:apps.example:10",
        installation_grants=split_history,
    )
    assert redacted is not None
    starter = redacted["d"]["threads"][0]["starter_message"]
    assert starter["content"] is None
    assert starter["attachments"] == []

    exact = (direct_grant(82, intents=intents, scopes=scopes),)
    visible = filtered_event(
        bot,
        event,
        intents,
        scopes,
        topic="user:apps.example:10",
        installation_grants=exact,
    )
    assert visible is not None
    starter = visible["d"]["threads"][0]["starter_message"]
    assert starter["content"] == "historical secret"
    assert starter["attachments"] == [{"id": "9"}]


def test_thread_member_redaction_does_not_mutate_the_shared_gateway_event() -> None:
    bot = principal(scopes={"channels.read"}, intents={"guilds"})
    own_delta = {
        "user_id": "10",
        "user_domain": "apps.example",
        "member": {"nick": "private"},
        "presence": {"status": "online"},
    }
    event = {
        "t": "THREAD_MEMBERS_UPDATE",
        "topic_seq": 7,
        "d": {
            "added_members": [
                own_delta,
                {
                    "user_id": "11",
                    "user_domain": "apps.example",
                    "member": {"nick": "someone else"},
                },
            ],
            "removed_member_ids": [],
            "removed_member_refs": [],
        },
    }

    rendered = filtered_event(
        bot,
        event,
        {"guilds"},
        {"channels.read"},
        topic="guild:guild.example:70",
    )

    assert rendered is not None
    assert rendered["d"]["added_members"] == [{"user_id": "10", "user_domain": "apps.example"}]
    assert own_delta["member"] == {"nick": "private"}
    assert own_delta["presence"] == {"status": "online"}


def test_user_installed_interactions_require_the_exact_installation() -> None:
    bot = principal(scopes={"applications.commands"}, intents={"interactions"})
    event = {
        "t": "INTERACTION_CREATE",
        "topic_seq": 8,
        "audience_user_refs": ["10@apps.example"],
        "d": {
            "application_ref": "20@apps.example",
            "user_installation_id": "81",
            "installation_revision": "3",
            "bot_user_ref": "10@apps.example",
        },
    }
    assert (
        filtered_event(
            bot,
            event,
            {"interactions"},
            {"applications.commands"},
            topic="user:apps.example:10",
            installation_grants=(
                direct_grant(
                    81,
                    intents={"interactions"},
                    scopes={"applications.commands"},
                    user_installation=True,
                    installation_revision=3,
                ),
            ),
        )
        is not None
    )
    assert (
        filtered_event(
            bot,
            event,
            {"interactions"},
            {"applications.commands"},
            topic="user:apps.example:10",
            installation_grants=(
                direct_grant(
                    81,
                    intents={"interactions"},
                    scopes={"applications.commands"},
                    user_installation=True,
                    installation_revision=2,
                ),
            ),
        )
        is None
    )
    assert (
        filtered_event(
            bot,
            event,
            {"interactions"},
            {"applications.commands"},
            topic="user:apps.example:10",
            installation_grants=(
                direct_grant(
                    82,
                    intents={"interactions"},
                    scopes={"applications.commands"},
                    user_installation=True,
                    installation_revision=3,
                ),
            ),
        )
        is None
    )


def test_sensitive_gateway_events_also_enforce_installed_guild_permissions() -> None:
    audit = principal(scopes={"audit_logs.read"}, intents={"guild_moderation"})
    event = {
        "t": "GUILD_AUDIT_LOG_ENTRY_CREATE",
        "topic_seq": 2,
        "d": {"id": "99"},
    }
    arguments = (
        audit,
        event,
        {"guild_moderation"},
        {"audit_logs.read"},
    )
    assert event_permission_options(event["t"]) == (Permission.VIEW_AUDIT_LOG,)
    assert (
        filtered_event(
            *arguments,
            topic="guild:guild.example:1",
            granted_permissions=0,
        )
        is None
    )
    assert (
        filtered_event(
            *arguments,
            topic="guild:guild.example:1",
            granted_permissions=int(Permission.VIEW_AUDIT_LOG),
        )
        is not None
    )
    assert (
        filtered_event(
            *arguments,
            topic="guild:guild.example:1",
            granted_permissions=int(Permission.ADMINISTRATOR),
        )
        is not None
    )


def test_channel_sensitive_events_do_not_use_a_guild_permission_snapshot() -> None:
    # Invite visibility is checked against the event channel's live
    # overwrites by app.gateway.event_visibility. Discord does not impose a
    # separate MANAGE_WEBHOOKS permission on the sparse webhook update event.
    assert event_permission_options("INVITE_CREATE") == ()
    assert event_permission_options("INVITE_DELETE") == ()
    assert event_permission_options("WEBHOOKS_UPDATE") == ()
