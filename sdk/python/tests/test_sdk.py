import asyncio
from collections.abc import Callable
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Literal
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kaede_bot import (
    ALL_PERMISSIONS,
    ActionRow,
    ApplicationCommandPermissions,
    BOT_INTENT_ALIASES,
    BOT_INTENT_NAMES,
    Button,
    ChannelPositionUpdate,
    Embed,
    EncryptedVoiceTransport,
    EVENT_NAMES,
    PERMISSION_SCHEMA,
    Poll,
    PollAnswer,
    PollMedia,
    Permission,
    View,
)
from kaede_bot.client import Client, canonical_target_origin
from kaede_bot.applications import ApplicationAsset
from kaede_bot.e2ee import encrypted_forward_snapshot_digest
from kaede_bot.errors import ApiError, Forbidden
from kaede_bot.intents import Intents
from kaede_bot.models import (
    Call,
    Channel,
    ChannelDeleteEvent,
    DMOpenRejectedEvent,
    Emoji,
    EmojisUpdateEvent,
    Guild,
    Interaction,
    Member,
    Message,
    MessageBulkDeleteEvent,
    PollVoteEvent,
    PresenceEvent,
    RawEvent,
    ReactionEvent,
    ReactionClearEvent,
    ScheduledEvent,
    Sticker,
    StickerDeleteEvent,
    StickersUpdateEvent,
    ThreadListSyncEvent,
    ThreadMembersUpdateEvent,
    TrackerBoardUpdateEvent,
    TrackerLane,
    TrackerLaneDeleteEvent,
    TrackerTask,
    TrackerTaskDeleteEvent,
    VoiceStateEvent,
    Webhook,
)
from kaede_bot.refs import EntityRef, User
from kaede_bot.soundboard import SoundboardSound
from kaede_bot.state import WorkerState
import kaede_bot.client as client_module
import kaede_bot.state as state_module


def test_entity_ref_and_human_handle_are_distinct() -> None:
    ref = EntityRef.parse("123@chat.example")
    user = User(ref, "alice", "Alice")
    assert str(ref) == "123@chat.example"
    assert user.handle == "alice@chat.example"
    assert user.mention == "<@123@chat.example>"


def test_top_level_exports_encrypted_voice_transport() -> None:
    assert EncryptedVoiceTransport.__name__ == "EncryptedVoiceTransport"


def test_generated_protocol_constants_cover_permissions_intents_and_events() -> None:
    assert PERMISSION_SCHEMA == "kaede-permissions-v1"
    assert Permission.CREATE_INSTANT_INVITE is Permission.CREATE_INVITE
    assert Permission.MANAGE_GUILD_EXPRESSIONS is Permission.MANAGE_EMOJIS
    assert ALL_PERMISSIONS == 576456216817434111
    assert Permission.PRIORITY_SPEAKER == 1 << 8
    assert BOT_INTENT_ALIASES["voice_states"] == "guild_voice_states"
    assert {
        "direct_messages",
        "direct_message_reactions",
        "direct_message_typing",
        "guild_message_reactions",
        "guild_message_typing",
        "guild_voice_states",
        "interactions",
        "guild_tasks",
    } <= set(BOT_INTENT_NAMES)
    assert {
        "MESSAGE_DELETE_BULK",
        "MESSAGE_REACTION_REMOVE_ALL",
        "MESSAGE_REACTION_REMOVE_EMOJI",
    } <= set(EVENT_NAMES)


@pytest.mark.parametrize(
    "value",
    [
        "alice@chat.example",
        "0@chat.example",
        "01@chat.example",
        "9223372036854775808@chat.example",
        "1@Chat.example",
        "1@chat.example.",
        "1@chat.example@evil.example",
        "1@chat.example:443",
        "1@localhost",
        "-1@chat.example",
    ],
)
def test_entity_ref_rejects_usernames_and_noncanonical_domains(value: str) -> None:
    with pytest.raises(ValueError):
        EntityRef.parse(value)


@pytest.mark.parametrize(
    ("identifier", "domain"),
    [
        (True, "chat.example"),
        ("1", "chat.example"),
        (0, "chat.example"),
        (9_223_372_036_854_775_808, "chat.example"),
        (1, "Chat.example"),
        (1, "chat.example/path"),
    ],
)
def test_entity_ref_constructor_cannot_bypass_wire_validation(
    identifier: object,
    domain: str,
) -> None:
    with pytest.raises(ValueError):
        EntityRef(identifier, domain)  # type: ignore[arg-type]


def test_entity_ref_from_wire_preserves_exact_canonical_evidence() -> None:
    assert EntityRef.from_wire("9223372036854775807", "chat.example") == EntityRef(
        9223372036854775807, "chat.example"
    )


@pytest.mark.parametrize(
    ("identifier", "domain"),
    [
        ("0", "chat.example"),
        ("01", "chat.example"),
        ("9223372036854775808", "chat.example"),
        (1, "chat.example"),
        (True, "chat.example"),
        ("1", "Chat.example"),
        ("1", "chat.example."),
        ("1", "localhost"),
        ("1", None),
    ],
)
def test_entity_ref_from_wire_rejects_noncanonical_ids_and_domains(
    identifier: object, domain: object
) -> None:
    with pytest.raises(ValueError):
        EntityRef.from_wire(identifier, domain)


@pytest.mark.parametrize(
    "topic",
    [
        "guild:chat.example:0",
        "guild:chat.example:01",
        "guild:chat.example:9223372036854775808",
        "guild:Chat.example:1",
        "guild:localhost:1",
    ],
)
def test_gateway_topic_rejects_noncanonical_guild_authorities(topic: str) -> None:
    assert client_module._guild_ref_from_topic(topic) is None


def test_federated_bot_profile_derives_the_legacy_bot_flag_from_account_type() -> None:
    user = User.from_payload(
        {
            "id": "7",
            "origin_domain": "apps.example",
            "username": "release_bot",
            "account_type": "bot",
            "profile_version": 2,
            "e2ee_device_generation": "3",
        }
    )

    assert user.account_type == "bot"
    assert user.bot is True
    assert user.profile_version == 2
    assert user.e2ee_device_generation == 3


@pytest.mark.parametrize(
    "payload",
    [
        {"account_type": "bot", "bot": False},
        {"account_type": "human", "bot": True},
        {"account_type": "service"},
        {"bot": "false"},
        {"profile_resolved": "false"},
        {"profile_version": "01"},
        {"e2ee_device_generation": -1},
        {"id": "07"},
    ],
)
def test_user_payload_rejects_ambiguous_identity_discriminators(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        User.from_payload(
            {
                "id": "7",
                "origin_domain": "users.example",
                "username": "alice",
                **payload,
            }
        )


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
        "https://localhost",
        "https://bad_domain.example",
        "https://\N{SNOWMAN}.example",
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
async def test_fetch_guild_binds_installation_channel_restriction_lineage() -> None:
    bot = client()
    payload = {
        "id": "2",
        "origin_domain": "guild.example",
        "name": "Guild",
        "installation_id": "77",
        "capability_revision": "4",
        "granted_scopes": ["guilds.read"],
        "granted_intents": ["guilds"],
        "channel_restrictions": [
            "20@guild.example",
            "21@guild.example",
        ],
    }
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    guild = await bot.fetch_guild(
        EntityRef(2, "guild.example"), target="https://guild.example"
    )

    assert guild.installation_id == 77
    assert guild.installation_revision == 4
    assert guild.channel_restrictions == (
        EntityRef(20, "guild.example"),
        EntityRef(21, "guild.example"),
    )

    for substitution in (
        {"channel_restrictions": None},
        {"channel_restrictions": ["20"]},
        {"channel_restrictions": ["20@other.example"]},
        {"channel_restrictions": ["20@guild.example", "20@guild.example"]},
        {"channel_restrictions": ["20@guild.example", 21]},
        {"capability_revision": None},
        {"capability_revision": "0"},
        {"capability_revision": "04"},
        {"installation_id": "0"},
        {"id": "3"},
    ):
        bot.request = AsyncMock(  # type: ignore[method-assign]
            return_value=payload | substitution
        )
        with pytest.raises(ValueError):
            await bot.fetch_guild(
                EntityRef(2, "guild.example"), target="https://guild.example"
            )


@pytest.mark.asyncio
async def test_guild_list_and_mutation_responses_keep_exact_installation_lineage() -> (
    None
):
    bot = client()
    guild_ref = EntityRef(2, "guild.example")
    payload = {
        "id": "2",
        "origin_domain": "guild.example",
        "name": "Guild",
        "installation_id": "77",
        "capability_revision": "4",
        "granted_scopes": ["guilds.read", "guilds.manage"],
        "granted_intents": ["guilds"],
        "channel_restrictions": ["20@guild.example"],
    }

    bot.request = AsyncMock(return_value=[payload])  # type: ignore[method-assign]
    listed = await bot.fetch_guilds(target="https://guild.example")
    assert listed[0].ref == guild_ref
    assert listed[0].channel_restrictions == (EntityRef(20, "guild.example"),)

    substituted = payload | {
        "id": "3",
        "origin_domain": "other.example",
        "channel_restrictions": ["20@other.example"],
    }
    bot.request = AsyncMock(return_value=[substituted])  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="changed its target authority"):
        await bot.fetch_guilds(target="https://guild.example")

    bot.request = AsyncMock(return_value=payload | {"name": "Renamed"})  # type: ignore[method-assign]
    edited = await bot.edit_guild(
        guild_ref,
        target="https://guild.example",
        version="v4",
        name="Renamed",
    )
    assert edited.ref == guild_ref
    assert edited.channel_restrictions == (EntityRef(20, "guild.example"),)

    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    cleared = await bot.delete_guild_asset(
        guild_ref,
        "icon",
        target="https://guild.example",
    )
    assert cleared.ref == guild_ref
    assert cleared.channel_restrictions == (EntityRef(20, "guild.example"),)

    bot.request = AsyncMock(return_value=payload | {"id": "3"})  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="changed the requested guild"):
        await bot.edit_guild(
            guild_ref,
            target="https://guild.example",
            version="v4",
            description="substituted",
        )
    with pytest.raises(ValueError, match="changed the requested guild"):
        await bot.delete_guild_asset(
            guild_ref,
            "banner",
            target="https://guild.example",
        )


@pytest.mark.parametrize(
    "decode",
    [
        pytest.param(
            lambda bot: User.from_payload(
                {
                    "id": "01",
                    "origin_domain": "users.example",
                    "username": "alice",
                }
            ),
            id="user",
        ),
        pytest.param(
            lambda bot: Guild.from_payload(
                bot,
                "https://guild.example",
                {"id": "01", "origin_domain": "guild.example", "name": "Guild"},
            ),
            id="guild",
        ),
        pytest.param(
            lambda bot: Channel.from_payload(
                bot,
                "https://guild.example",
                {"id": "01", "origin_domain": "guild.example", "type": 0},
            ),
            id="channel",
        ),
        pytest.param(
            lambda bot: Message.from_payload(
                bot,
                "https://chat.example",
                {
                    "id": "01",
                    "origin_domain": "chat.example",
                    "channel_id": "5",
                    "channel_domain": "chat.example",
                    "attachments": [],
                },
            ),
            id="message",
        ),
        pytest.param(
            lambda bot: ApplicationAsset.from_payload(
                bot,
                "https://apps.example",
                {
                    "id": "01",
                    "application_ref": "1@apps.example",
                    "kind": "icon",
                    "name": "icon",
                    "media_hash": "hash",
                    "content_type": "image/png",
                    "version": 1,
                    "created_at": "2026-08-29T00:00:00+00:00",
                    "updated_at": "2026-08-29T00:00:00+00:00",
                },
            ),
            id="application",
        ),
        pytest.param(
            lambda bot: Interaction.from_payload(
                bot,
                "https://chat.example",
                {
                    "id": "01",
                    "application_ref": "1@apps.example",
                    "guild_ref": "2@chat.example",
                    "channel_ref": "5@chat.example",
                    "user": {
                        "id": "3",
                        "origin_domain": "users.example",
                        "username": "alice",
                    },
                    "command": {"name": "ping"},
                    "options": {},
                },
            ),
            id="interaction",
        ),
        pytest.param(
            lambda bot: ScheduledEvent.from_payload(
                bot,
                "https://guild.example",
                {
                    "id": "01",
                    "origin_domain": "guild.example",
                    "guild_id": "2",
                    "guild_domain": "guild.example",
                    "creator_id": "3",
                    "creator_domain": "users.example",
                    "name": "Town hall",
                    "scheduled_start_time": "2026-08-30T00:00:00+00:00",
                    "status": 1,
                    "entity_type": 3,
                },
            ),
            id="scheduled-event",
        ),
        pytest.param(
            lambda bot: bot._event_model(
                "MESSAGE_DELETE",
                {
                    "id": "01",
                    "origin_domain": "chat.example",
                    "channel_id": "5",
                    "channel_domain": "chat.example",
                },
                target="https://chat.example",
                topic=None,
                sequence=1,
            ),
            id="gateway-event",
        ),
    ],
)
def test_wire_model_decoders_reject_leading_zero_entity_ids(
    decode: Callable[[Client], object],
) -> None:
    with pytest.raises(ValueError):
        decode(client())


def test_follower_webhook_parses_type_source_and_never_invents_a_token() -> None:
    item = Webhook.from_payload(
        client(),
        "https://chat.example",
        {
            "id": "71",
            "origin_domain": "chat.example",
            "type": 2,
            "application_id": None,
            "application_domain": None,
            "guild_id": "11",
            "guild_domain": "chat.example",
            "channel_id": "13",
            "channel_domain": "chat.example",
            "name": "Upstream",
            "avatar_hash": None,
            "revoked": False,
            "source_guild": {
                "id": "19",
                "origin_domain": "source.example",
                "name": "Upstream",
                "icon_hash": None,
            },
            "source_channel": {
                "id": "17",
                "origin_domain": "source.example",
                "name": "releases",
            },
        },
    )

    assert item.type == 2
    assert item.token is None
    assert item.execution_url is None
    assert item.source_guild is not None
    assert item.source_guild.ref == EntityRef(19, "source.example")
    assert item.source_channel is not None
    assert item.source_channel.ref == EntityRef(17, "source.example")


def test_token_webhook_parses_one_time_execution_url() -> None:
    item = Webhook.from_payload(
        client(),
        "https://chat.example",
        {
            "id": "72",
            "guild_id": "11",
            "guild_domain": "chat.example",
            "channel_id": "13",
            "channel_domain": "chat.example",
            "name": "Builds",
            "avatar_hash": None,
            "revoked": False,
            "token": "kwh_secret",
            "execution_url": "https://chat.example/api/v1/webhooks/72/kwh_secret",
        },
    )

    assert item.token == "kwh_secret"
    assert item.execution_url == "https://chat.example/api/v1/webhooks/72/kwh_secret"


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
            "message_id": "5",
            "message_domain": "guild.example",
            "channel_id": "6",
            "channel_domain": "guild.example",
            "user_id": "7",
            "user_domain": "users.example",
            "reaction": "👋",
            "emoji": {
                "id": None,
                "origin_domain": None,
                "name": "👋",
                "animated": False,
            },
            "guild_id": "8",
            "guild_domain": "guild.example",
            "message_author_id": "9",
            "message_author_domain": "author.example",
            "burst": False,
            "burst_colors": [],
            "type": 0,
        },
        target="https://guild.example",
    )

    assert len(seen) == 1
    assert seen[0].message_ref == EntityRef(5, "guild.example")
    assert seen[0].user_ref == EntityRef(7, "users.example")
    assert seen[0].guild_ref == EntityRef(8, "guild.example")
    assert seen[0].message_author_ref == EntityRef(9, "author.example")
    assert seen[0].emoji_details is not None
    assert seen[0].emoji_details.token == "👋"
    assert seen[0].reaction_type == 0


@pytest.mark.asyncio
async def test_reaction_event_rebuilds_qualified_custom_token() -> None:
    bot = client()
    seen: list[ReactionEvent] = []

    @bot.event
    async def on_message_reaction_add(event: ReactionEvent) -> None:
        seen.append(event)

    await bot.dispatch(
        "MESSAGE_REACTION_ADD",
        {
            "message_id": "5",
            "message_domain": "guild.example",
            "channel_id": "6",
            "channel_domain": "guild.example",
            "user_id": "7",
            "user_domain": "users.example",
            "emoji": {
                "id": "10",
                "origin_domain": "emoji.example",
                "name": "wave",
                "animated": True,
            },
        },
        target="https://guild.example",
    )

    assert seen[0].emoji == "<a:wave:10@emoji.example>"
    assert seen[0].emoji_details is not None
    assert seen[0].emoji_details.ref == EntityRef(10, "emoji.example")


@pytest.mark.asyncio
async def test_aggregate_message_events_are_typed_and_keep_composite_refs() -> None:
    bot = client()
    bulk: list[MessageBulkDeleteEvent] = []
    clears: list[ReactionClearEvent] = []
    reaction_emoji = "👋"

    @bot.event
    async def on_message_delete_bulk(event: MessageBulkDeleteEvent) -> None:
        bulk.append(event)

    @bot.event
    async def on_message_reaction_remove_emoji(event: ReactionClearEvent) -> None:
        clears.append(event)

    await bot.dispatch(
        "MESSAGE_DELETE_BULK",
        {
            "ids": [
                {"id": "5", "origin_domain": "guild.example"},
                {"id": "6", "origin_domain": "remote.example"},
            ],
            "channel_id": "7",
            "channel_domain": "guild.example",
            "guild_id": "8",
            "guild_domain": "guild.example",
        },
        target="https://guild.example",
        topic="guild:guild.example:8",
    )
    await bot.dispatch(
        "MESSAGE_REACTION_REMOVE_EMOJI",
        {
            "message_id": "5",
            "message_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "guild_id": "8",
            "guild_domain": "guild.example",
            "reaction": reaction_emoji,
            "emoji": {
                "id": None,
                "origin_domain": None,
                "name": reaction_emoji,
                "animated": False,
            },
        },
        target="https://guild.example",
        topic="guild:guild.example:8",
    )

    assert bulk[0].message_refs == (
        EntityRef(5, "guild.example"),
        EntityRef(6, "remote.example"),
    )
    assert bulk[0].guild_ref == EntityRef(8, "guild.example")
    assert clears[0].message_ref == EntityRef(5, "guild.example")
    assert clears[0].emoji == reaction_emoji
    assert clears[0].emoji_details is not None
    assert clears[0].emoji_details.name == reaction_emoji


@pytest.mark.asyncio
async def test_poll_vote_events_are_typed_and_keep_federated_user_identity() -> None:
    bot = client()
    seen: list[PollVoteEvent] = []

    @bot.event
    async def on_message_poll_vote_add(event: PollVoteEvent) -> None:
        seen.append(event)

    await bot.dispatch(
        "MESSAGE_POLL_VOTE_ADD",
        {
            "message_id": "5",
            "message_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "guild_id": "8",
            "guild_domain": "guild.example",
            "user_id": "9",
            "user_domain": "member.example",
            "answer_id": 2,
        },
        target="https://guild.example",
        topic="guild:guild.example:8",
    )

    assert seen == [
        PollVoteEvent(
            target="https://guild.example",
            message_ref=EntityRef(5, "guild.example"),
            channel_ref=EntityRef(7, "guild.example"),
            user_ref=EntityRef(9, "member.example"),
            answer_id=2,
            added=True,
            guild_ref=EntityRef(8, "guild.example"),
        )
    ]


@pytest.mark.asyncio
async def test_soundboard_gateway_mutations_dispatch_typed_resources() -> None:
    bot = client()
    seen: list[SoundboardSound] = []

    @bot.event
    async def on_guild_soundboard_sound_create(event: SoundboardSound) -> None:
        seen.append(event)

    await bot.dispatch(
        "GUILD_SOUNDBOARD_SOUND_CREATE",
        {
            "id": "4",
            "origin_domain": "guild.example",
            "guild_id": "8",
            "guild_domain": "guild.example",
            "name": "airhorn",
            "media_hash": "00" * 32,
            "content_type": "audio/ogg",
            "duration_ms": 900,
            "volume": 0.8,
        },
        target="https://guild.example",
        topic="guild:guild.example:8",
    )

    assert seen[0].ref == EntityRef(4, "guild.example")
    assert seen[0].guild_ref == EntityRef(8, "guild.example")


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
        installation_id=None,
        dm_capability_id=None,
    )


@pytest.mark.asyncio
async def test_message_reaction_management_helpers_keep_exact_refs() -> None:
    bot = client()
    bot.remove_user_reaction = AsyncMock()  # type: ignore[method-assign]
    bot.clear_reactions = AsyncMock()  # type: ignore[method-assign]
    bot.clear_reaction = AsyncMock()  # type: ignore[method-assign]
    bot.reaction_users = AsyncMock(return_value=([], 0, None))  # type: ignore[method-assign]
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
    user = EntityRef(12, "users.example")
    cursor = EntityRef(11, "users.example")
    reaction_emoji = "👋"

    await message.remove_user_reaction(user, reaction_emoji)
    await message.clear_reactions()
    await message.clear_reaction(reaction_emoji)
    assert await message.reaction_users(reaction_emoji, after=cursor, limit=25) == (
        [],
        0,
        None,
    )

    bot.remove_user_reaction.assert_awaited_once_with(
        message.channel_ref,
        message.ref,
        user,
        reaction_emoji,
        target=message.target,
        installation_id=None,
        dm_capability_id=None,
    )
    bot.clear_reactions.assert_awaited_once_with(
        message.channel_ref,
        message.ref,
        target=message.target,
        installation_id=None,
        dm_capability_id=None,
    )
    bot.clear_reaction.assert_awaited_once_with(
        message.channel_ref,
        message.ref,
        reaction_emoji,
        target=message.target,
        installation_id=None,
        dm_capability_id=None,
    )
    bot.reaction_users.assert_awaited_once_with(
        message.channel_ref,
        message.ref,
        reaction_emoji,
        target=message.target,
        after=cursor,
        limit=25,
        installation_id=None,
        dm_capability_id=None,
    )


@pytest.mark.asyncio
async def test_reaction_requests_use_discord_compatible_encoded_routes() -> None:
    bot = client()
    bot.request = AsyncMock(return_value={"items": [], "total": 0})  # type: ignore[method-assign]
    bot._runtime_grant_headers = AsyncMock(return_value={})  # type: ignore[method-assign]
    channel = EntityRef(8, "guild.example")
    message = EntityRef(9, "guild.example")
    emoji = "👋"
    encoded = "%F0%9F%91%8B"

    await bot.add_reaction(channel, message, emoji)
    await bot.remove_reaction(channel, message, emoji)
    await bot.clear_reaction(channel, message, emoji)
    await bot.reaction_users(channel, message, emoji)

    calls = bot.request.await_args_list
    assert calls[0].args[:2] == (
        "PUT",
        f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{encoded}/@me",
    )
    assert calls[1].args[:2] == (
        "DELETE",
        f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{encoded}/@me",
    )
    assert calls[2].args[:2] == (
        "DELETE",
        f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{encoded}",
    )
    assert calls[3].args[:2] == (
        "GET",
        f"/api/v1/bots/channels/{channel}/messages/{message}/reactions/{encoded}",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("callback_type", [True, 1, 10, 12])
async def test_sdk_rejects_callbacks_without_a_supported_runtime(
    callback_type: object,
) -> None:
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="unsupported interaction callback type"):
        await bot.interaction_callback(10, callback_type)  # type: ignore[arg-type]

    bot.request.assert_not_awaited()


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
    assert "guild_tasks" not in Intents.default().names()
    assert "guild_tasks" in Intents.all().names()
    additive = {
        "guild_moderation",
        "guild_expressions",
        "guild_integrations",
        "guild_webhooks",
        "guild_invites",
        "guild_scheduled_events",
        "guild_message_polls",
        "direct_message_polls",
        "auto_moderation_configuration",
        "auto_moderation_execution",
    }
    assert additive.isdisjoint(Intents.default().names())
    assert additive <= set(Intents.all().names())
    assert {"direct_messages", "direct_message_reactions"} <= set(
        Intents.default().names()
    )
    assert {
        "guild_voice_states",
        "guild_message_reactions",
        "guild_message_typing",
        "direct_message_typing",
    } <= set(Intents.all().names())


@pytest.mark.asyncio
async def test_unknown_additive_gateway_events_remain_raw_and_dispatchable() -> None:
    bot = client()
    seen: list[RawEvent] = []

    @bot.listen("FUTURE_ADDITIVE_EVENT")
    async def on_future(event: RawEvent) -> None:
        seen.append(event)

    await bot.dispatch(
        "FUTURE_ADDITIVE_EVENT",
        {"future": True},
        target="https://guild.example",
        topic="guild:guild.example:1",
        sequence=9,
    )

    assert len(seen) == 1
    assert seen[0].type == "FUTURE_ADDITIVE_EVENT"
    assert seen[0].data == {"future": True}


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


@pytest.mark.asyncio
async def test_channel_reorder_serializes_only_supplied_partial_fields() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await bot.reorder_channels(
        EntityRef(2, "guild.example"),
        (
            ChannelPositionUpdate(
                EntityRef(3, "guild.example"),
                position=4,
            ),
            ChannelPositionUpdate(
                EntityRef(4, "guild.example"),
                parent_id=None,
            ),
            ChannelPositionUpdate(
                EntityRef(5, "guild.example"),
                position=None,
                lock_permissions=None,
                flags=None,
            ),
            ChannelPositionUpdate(
                EntityRef(6, "guild.example"),
                flags=16,
            ),
        ),
        target="https://guild.example",
    )

    bot.request.assert_awaited_once_with(
        "PATCH",
        "/api/v1/bots/guilds/2@guild.example/channels",
        target="https://guild.example",
        json={
            "channels": [
                {"id": "3", "position": 4},
                {"id": "4", "parent_id": None},
                {
                    "id": "5",
                    "position": None,
                    "lock_permissions": None,
                    "flags": None,
                },
                {"id": "6", "flags": 16},
            ]
        },
        headers=None,
    )

    with pytest.raises(ValueError, match="at least one"):
        await bot.reorder_channels(
            EntityRef(2, "guild.example"),
            (ChannelPositionUpdate(EntityRef(3, "guild.example")),),
        )

    with pytest.raises(ValueError, match="flags must be 0, 16, or null"):
        await bot.reorder_channels(
            EntityRef(2, "guild.example"),
            (ChannelPositionUpdate(EntityRef(3, "guild.example"), flags=8),),
        )


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
                "sticker_items": [
                    {
                        "id": "8",
                        "origin_domain": "guild.example",
                        "name": "party_blob",
                        "format_type": 2,
                        "media_hash": "abc",
                    }
                ],
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
        dm_capability_id=None,
    )
    bot.send_message.assert_awaited_once_with(
        EntityRef(3, "guild.example"),
        stickers=[sticker],
        target="https://guild.example",
        installation_id=77,
        dm_capability_id=None,
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


def test_federated_poll_finalization_message_update_is_typed_and_complete() -> None:
    bot = client()
    finalized_at = "2026-08-29T12:00:00+00:00"

    event = bot._event_model(  # noqa: SLF001 - exact Gateway wire contract
        "MESSAGE_UPDATE",
        {
            "id": "81",
            "origin_domain": "guild.example",
            "channel_id": "80",
            "channel_domain": "guild.example",
            "content": "Choose one",
            "created_at": "2026-08-29T11:00:00+00:00",
            "attachments": [],
            "poll": {
                "question": {"text": "Ship it?"},
                "answers": [
                    {"answer_id": 1, "poll_media": {"text": "Yes"}},
                    {"answer_id": 2, "poll_media": {"text": "No"}},
                ],
                "expiry": finalized_at,
                "allow_multiselect": False,
                "layout_type": 1,
                "finalized_at": finalized_at,
                "results": {
                    "is_finalized": True,
                    "answer_counts": [
                        {"id": 1, "count": 2, "me_voted": True},
                        {"id": 2, "count": 1, "me_voted": False},
                    ],
                },
            },
        },
        target="https://guild.example",
        topic="guild:guild.example:2",
        sequence=1,
    )

    assert isinstance(event, Message)
    assert event.poll is not None
    assert event.poll["finalized_at"] == finalized_at
    assert event.poll["results"]["is_finalized"] is True


def test_expression_collection_gateway_events_are_typed() -> None:
    bot = client()
    emoji_update = bot._event_model(
        "GUILD_EMOJIS_UPDATE",
        {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "emojis": [
                {
                    "id": "7",
                    "origin_domain": "guild.example",
                    "guild_id": "2",
                    "guild_domain": "guild.example",
                    "name": "party",
                }
            ],
        },
        target="https://guild.example",
        topic="guild:guild.example:2",
        sequence=3,
    )
    sticker_update = bot._event_model(
        "GUILD_STICKERS_UPDATE",
        {
            "guild_id": "2",
            "guild_domain": "guild.example",
            "stickers": [
                {
                    "id": "8",
                    "origin_domain": "guild.example",
                    "guild_id": "2",
                    "guild_domain": "guild.example",
                    "name": "wave",
                }
            ],
        },
        target="https://guild.example",
        topic="guild:guild.example:2",
        sequence=4,
    )

    assert isinstance(emoji_update, EmojisUpdateEvent)
    assert emoji_update.guild_ref == EntityRef(2, "guild.example")
    assert [item.name for item in emoji_update.emojis] == ["party"]
    assert isinstance(sticker_update, StickersUpdateEvent)
    assert sticker_update.guild_ref == EntityRef(2, "guild.example")
    assert [item.name for item in sticker_update.stickers] == ["wave"]


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


def test_command_decorator_preserves_discord_registration_metadata() -> None:
    bot = client()

    @bot.command(
        name="weather",
        description="Forecast",
        name_localizations={"fr": "météo"},
        description_localizations={"fr": "Prévisions"},
        default_member_permissions=["SEND_MESSAGES"],
        nsfw=True,
        contexts=["guild", "private_channel"],
        integration_types=["guild_install", "user_install"],
    )
    async def weather() -> None:
        return None

    assert bot._commands == [  # noqa: SLF001 - registration payload contract
        {
            "name": "weather",
            "type": "chat_input",
            "description": "Forecast",
            "name_localizations": {"fr": "météo"},
            "description_localizations": {"fr": "Prévisions"},
            "default_member_permissions": ["SEND_MESSAGES"],
            "nsfw": True,
            "contexts": ["guild", "private_channel"],
            "integration_types": ["guild_install", "user_install"],
            "options": [],
        }
    ]


def test_command_decorator_uses_discord_context_defaults_without_replacing_empty_values() -> (
    None
):
    bot = client()

    @bot.command(name="weather", description="Forecast")
    async def weather() -> None:
        return None

    @bot.command(
        name="empty",
        description="Rejected by registration",
        contexts=[],
        integration_types=[],
        options=[],
    )
    async def empty() -> None:
        return None

    assert bot._commands[0]["contexts"] == [  # noqa: SLF001 - registration contract
        "guild",
        "bot_dm",
        "private_channel",
    ]
    assert bot._commands[0]["integration_types"] == ["guild_install"]  # noqa: SLF001
    assert bot._commands[1]["contexts"] == []  # noqa: SLF001
    assert bot._commands[1]["integration_types"] == []  # noqa: SLF001


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
    bot = Client(worker_state=state)

    @bot.command(
        name="weather",
        description="Forecast",
        contexts=["guild", "private_channel"],
        integration_types=["guild_install", "user_install"],
    )
    async def weather() -> None:
        return None

    await bot.sync_commands(
        application_home="https://apps.example",
        control_token="secret",
    )
    await bot.sync_commands(
        application_home="https://apps.example",
        control_token="secret",
        guild=EntityRef(7, "guild.example"),
    )

    assert len(client_options) == 3
    assert all(
        options["base_url"] == "https://apps.example" for options in client_options
    )
    assert all(options["follow_redirects"] is False for options in client_options)
    assert all(options["trust_env"] is False for options in client_options)
    assert [method for method, _, _ in requests] == ["POST", "PUT", "PUT"]
    assert requests[-1][1].endswith("/guilds/7@guild.example/commands")
    guild_command = requests[-1][2]["json"]["commands"][0]
    assert guild_command["contexts"] == ["guild"]
    assert guild_command["integration_types"] == ["guild_install"]


def test_command_permission_event_parses_qualified_command_identity() -> None:
    bot = client()
    command_event = bot._event_model(  # noqa: SLF001 - gateway projection contract
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
        {
            "application_ref": "1@apps.example",
            "guild_ref": "7@guild.example",
            "command_ref": "9@apps.example",
            "permissions": [],
        },
        target="https://guild.example",
        topic="guild:guild.example:7",
        sequence=1,
    )
    assert command_event.command_ref == EntityRef(9, "apps.example")

    application_event = bot._event_model(  # noqa: SLF001 - gateway projection contract
        "APPLICATION_COMMAND_PERMISSIONS_UPDATE",
        {
            "application_ref": "1@apps.example",
            "guild_ref": "7@guild.example",
            "command_ref": None,
            "permissions": [],
        },
        target="https://guild.example",
        topic="guild:guild.example:7",
        sequence=2,
    )
    assert application_event.command_ref is None


def command_permission_payload(*, command: bool) -> dict[str, object]:
    command_ref = "9@apps.example" if command else None
    return {
        "id": command_ref or "1@apps.example",
        "application_id": "1",
        "application_domain": "apps.example",
        "application_ref": "1@apps.example",
        "application_name": "Weather Bot",
        "guild_id": "7",
        "guild_domain": "guild.example",
        "guild_ref": "7@guild.example",
        "command": (
            {
                "id": "9",
                "origin_domain": "apps.example",
                "ref": "9@apps.example",
                "name": "weather",
                "type": "chat_input",
                "guild_ref": None,
            }
            if command
            else None
        ),
        "command_ref": command_ref,
        "synced": command,
        "permissions": [
            {
                "id": "7@guild.example",
                "type": "role",
                "permission": True,
            }
        ],
    }


@pytest.mark.asyncio
async def test_command_permission_reads_bind_bot_application_guild_and_command() -> (
    None
):
    bot = client()
    guild_ref = EntityRef(7, "guild.example")
    command_ref = EntityRef(9, "apps.example")
    application_scope = command_permission_payload(command=False)
    command_scope = command_permission_payload(command=True)
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[application_scope], command_scope]
    )

    scopes = await bot.command_permissions(guild_ref)
    scope = await bot.command_permission(guild_ref, command_ref)

    assert scopes[0].application_ref == bot.worker_state.application_ref
    assert scopes[0].guild_ref == guild_ref
    assert scopes[0].command_ref is None
    assert scope.command_ref == command_ref
    assert scope.permissions[0].target_ref == guild_ref
    assert bot.request.await_args_list[0].args[1] == (
        "/api/v1/bots/applications/@me/guilds/7@guild.example/commands/permissions"
    )
    assert bot.request.await_args_list[1].args[1] == (
        "/api/v1/bots/applications/@me/guilds/7@guild.example/commands/"
        "9@apps.example/permissions"
    )
    assert all(
        call.kwargs["target"] == "https://guild.example"
        for call in bot.request.await_args_list
    )

    for substitution in (
        {"application_ref": "2@apps.example"},
        {"guild_ref": "7@other.example"},
        {"id": "10@apps.example"},
        {"command_ref": "10@apps.example"},
    ):
        with pytest.raises(ValueError):
            ApplicationCommandPermissions.from_payload(
                command_scope | substitution,
                target="https://guild.example",
                expected_application_ref=bot.worker_state.application_ref,
                expected_guild_ref=guild_ref,
            )

    with pytest.raises(ValueError, match="authority"):
        await bot.command_permission(guild_ref, EntityRef(9, "other.example"))


@pytest.mark.asyncio
@pytest.mark.parametrize("installation_type", ["guild", "user"])
async def test_open_dm_binds_subsequent_channel_writes_to_the_exact_installation(
    installation_type: Literal["guild", "user"],
) -> None:
    bot = client()
    capability_id = "kbdg_" + "a" * 43
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "5",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "recipients": [
                {
                    "id": "12",
                    "origin_domain": "chat.example",
                    "username": "alice",
                    "bot": False,
                    "account_type": "human",
                }
            ],
            "bot_dm_capability_id": capability_id,
            "bot_dm_capability_revision": "1",
            "bot_dm_capability_expires_at": "2099-01-01T00:00:00+00:00",
            "bot_dm_capability_lineage_ref": "99@chat.example",
            "bot_installation_ref": "77@install.example",
            "bot_installation_type": installation_type,
            "authority_origin": "https://chat.example",
        }
    )

    channel = await bot.open_dm(
        "alice@chat.example",
        installation_ref=EntityRef(77, "install.example"),
        installation_type=installation_type,
    )

    assert isinstance(channel, Channel)
    assert channel.bot_installation_id is None
    assert channel.dm_capability_id == capability_id
    assert channel.dm_capability_revision == 1
    assert channel.installation_ref == EntityRef(77, "install.example")
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["headers"] == {
        "X-Kaede-Bot-Installation": "77@install.example",
        "X-Kaede-Bot-Installation-Type": installation_type,
    }
    assert bot.request.await_args.kwargs["target"] == "https://apps.example"
    assert await bot._dm_capability_headers_for_path(  # noqa: SLF001
        "/api/v1/bots/channels/5@chat.example/messages"
    ) == {
        "X-Kaede-Bot-Source-Installation": "77@install.example",
        "X-Kaede-Bot-Installation-Type": installation_type,
        "X-Kaede-Bot-DM-Capability": capability_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_installation_ref", "response_installation_type"),
    [
        ("78@install.example", "guild"),
        ("77@install.example", "user"),
    ],
)
async def test_open_dm_rejects_substituted_source_installation(
    response_installation_ref: str,
    response_installation_type: Literal["guild", "user"],
) -> None:
    bot = client()
    capability_id = "kbdg_" + "a" * 43
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "5",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "recipients": [
                {
                    "id": "12",
                    "origin_domain": "chat.example",
                    "username": "alice",
                    "bot": False,
                    "account_type": "human",
                }
            ],
            "bot_dm_capability_id": capability_id,
            "bot_dm_capability_revision": "1",
            "bot_dm_capability_expires_at": "2099-01-01T00:00:00+00:00",
            "bot_dm_capability_lineage_ref": "99@chat.example",
            "bot_installation_ref": response_installation_ref,
            "bot_installation_type": response_installation_type,
            "authority_origin": "https://chat.example",
        }
    )

    with pytest.raises(ApiError, match="requested installation lineage"):
        await bot.open_dm(
            "alice@chat.example",
            installation_ref=EntityRef(77, "install.example"),
            installation_type="guild",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"type": 3},
        {"type": True},
        {"guild_ref": "7@chat.example"},
        {"guild_id": "7", "guild_domain": "chat.example"},
        {"conversation_type": "group"},
        {"recipients": []},
        {
            "recipients": [
                {
                    "id": "12",
                    "origin_domain": "chat.example",
                    "username": "alice",
                    "bot": False,
                    "account_type": "human",
                },
                "invalid",
            ]
        },
        {
            "recipients": [
                {
                    "id": "13",
                    "origin_domain": "chat.example",
                    "username": "bob",
                    "bot": False,
                    "account_type": "human",
                }
            ]
        },
    ],
)
async def test_open_dm_binds_exact_direct_channel_and_recipient(
    substitution: dict[str, object],
) -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "5",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "recipients": [
                {
                    "id": "12",
                    "origin_domain": "chat.example",
                    "username": "alice",
                    "bot": False,
                    "account_type": "human",
                }
            ],
            "bot_dm_capability_id": "kbdg_" + "a" * 43,
            "bot_dm_capability_revision": "1",
            "bot_dm_capability_expires_at": "2099-01-01T00:00:00+00:00",
            "bot_dm_capability_lineage_ref": "99@chat.example",
            "bot_installation_ref": "77@install.example",
            "bot_installation_type": "guild",
            "authority_origin": "https://chat.example",
        }
        | substitution
    )

    with pytest.raises(ApiError, match="requested DM recipient"):
        await bot.open_dm(
            " @ALICE@CHAT.EXAMPLE. ",
            installation_ref=EntityRef(77, "install.example"),
        )
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["json"] == {"handle": "alice@chat.example"}


@pytest.mark.asyncio
async def test_open_dm_rejects_queued_results_with_an_expiring_proof() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "status": "queued",
            "operation_id": "event-id",
            "pair_key": "a" * 64,
            "bot_installation_id": "77",
        }
    )

    with pytest.raises(ApiError, match="must complete"):
        await bot.open_dm(
            "alice@chat.example",
            installation_ref=EntityRef(77, "guild.example"),
        )


@pytest.mark.asyncio
async def test_dm_capability_refresh_is_singleflight_and_rejects_channel_swap() -> None:
    bot = client()
    channel_ref = EntityRef(5, "chat.example")
    current = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "a" * 43,
        revision=1,
        expires_at=client_module.time.time() + 5,
        target="https://chat.example",
        lineage_ref=EntityRef(99, "chat.example"),
    )
    refreshed = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=current.installation_ref,
        installation_type=current.installation_type,
        grant_id=current.grant_id,
        revision=current.revision + 1,
        expires_at=client_module.time.time() + 600,
        target=current.target,
        lineage_ref=current.lineage_ref,
    )
    key = (channel_ref, current.grant_id)
    bot._dm_capabilities[key] = current  # noqa: SLF001
    bot._dm_default_capabilities[channel_ref] = current.grant_id  # noqa: SLF001
    refresh_response: dict[str, Any] = {
        "grant_id": refreshed.grant_id,
        "revision": str(refreshed.revision),
        "expires_at": "2099-01-01T00:00:00+00:00",
        "installation_ref": str(refreshed.installation_ref),
        "installation_type": refreshed.installation_type,
        "authority_origin": refreshed.target,
        "channel_ref": str(channel_ref),
        "lineage_ref": "99@chat.example",
        "channel": {
            "id": str(channel_ref.id),
            "origin_domain": channel_ref.domain,
            "type": 1,
            "bot_dm_capability_id": refreshed.grant_id,
            "bot_dm_capability_revision": str(refreshed.revision),
            "bot_installation_ref": str(refreshed.installation_ref),
            "bot_installation_type": refreshed.installation_type,
        },
    }
    request_refresh = AsyncMock(return_value=refresh_response)
    bot.request = request_refresh  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        bot._dm_capability_headers_for_path(  # noqa: SLF001
            "/api/v1/bots/channels/5@chat.example/messages"
        ),
        bot._dm_capability_headers_for_path(  # noqa: SLF001
            "/api/v1/bots/channels/5@chat.example/messages"
        ),
    )

    assert first == second
    assert first["X-Kaede-Bot-DM-Capability"] == refreshed.grant_id
    request_refresh.assert_awaited_once()
    accepted = bot._dm_capabilities[key]  # noqa: SLF001
    assert accepted.revision == refreshed.revision

    request_refresh.reset_mock()
    request_refresh.return_value = refresh_response | {
        "revision": str(current.revision),
        "channel": refresh_response["channel"]
        | {"bot_dm_capability_revision": str(current.revision)},
    }
    with pytest.raises(ApiError, match="immutable conversation lineage"):
        await bot._refresh_dm_capability(key, force=True)  # noqa: SLF001
    assert bot._dm_capabilities[key] is accepted  # noqa: SLF001

    bot._dm_capabilities[key] = current  # noqa: SLF001
    request_refresh.reset_mock()
    request_refresh.return_value = refresh_response | {
        "channel_ref": "6@chat.example",
    }
    with pytest.raises(ApiError, match="immutable conversation lineage"):
        await bot._refresh_dm_capability(key, force=True)  # noqa: SLF001
    assert bot._dm_capabilities[key] is current  # noqa: SLF001

    for substitution in (
        {"installation_ref": "78@guild.example"},
        {"installation_type": "user"},
        {"lineage_ref": "100@chat.example"},
        {"authority_origin": "http://chat.example"},
    ):
        request_refresh.return_value = refresh_response | substitution
        with pytest.raises(ApiError, match="DM capability"):
            await bot._refresh_dm_capability(key, force=True)  # noqa: SLF001
        assert bot._dm_capabilities[key] is current  # noqa: SLF001


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "substitution",
    [
        {"bot_dm_capability_revision": "5"},
        {"bot_installation_ref": "78@guild.example"},
        {"bot_installation_type": "user"},
    ],
)
async def test_call_response_cannot_substitute_current_capability_lineage(
    substitution: dict[str, str],
) -> None:
    bot = client()
    channel_ref = EntityRef(5, "chat.example")
    context = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "a" * 43,
        revision=4,
        expires_at=client_module.time.time() + 600,
        target="https://chat.example",
        lineage_ref=EntityRef(99, "chat.example"),
    )
    bot._dm_capabilities[(channel_ref, context.grant_id)] = context  # noqa: SLF001
    bot._dm_default_capabilities[channel_ref] = context.grant_id  # noqa: SLF001
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "90",
            "channel_id": "5",
            "channel_domain": "chat.example",
            "authority_domain": "chat.example",
            "room": "d.5.90",
            "state": "ringing",
            "caller": "8@apps.example",
            "participants": ["8@apps.example", "9@users.example"],
            "created_at": 1,
            "bot_dm_capability_id": context.grant_id,
            "bot_dm_capability_revision": str(context.revision),
            "bot_installation_ref": str(context.installation_ref),
            "bot_installation_type": context.installation_type,
        }
        | substitution
    )

    with pytest.raises(ValueError, match="DM capability lineage"):
        await bot.start_call(channel_ref)


def test_call_gateway_event_binds_registered_capability_lineage() -> None:
    bot = client()
    channel_ref = EntityRef(5, "chat.example")
    context = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "a" * 43,
        revision=4,
        expires_at=client_module.time.time() + 600,
        target="https://chat.example",
        lineage_ref=EntityRef(99, "chat.example"),
    )
    bot._dm_capabilities[(channel_ref, context.grant_id)] = context  # noqa: SLF001
    payload = {
        "id": "90",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "authority_domain": "chat.example",
        "room": "d.5.90",
        "state": "ringing",
        "caller": "8@apps.example",
        "participants": ["8@apps.example", "9@users.example"],
        "created_at": 1,
        "bot_dm_capability_id": context.grant_id,
        "bot_dm_capability_revision": str(context.revision),
        "bot_installation_ref": str(context.installation_ref),
        "bot_installation_type": context.installation_type,
    }

    exact = bot._event_model(  # noqa: SLF001
        "CALL_CREATE",
        payload,
        target="https://chat.example",
        topic="user:apps.example:8",
        sequence=1,
    )
    assert isinstance(exact, Call)

    for substitution in (
        {"bot_dm_capability_id": "kbdg_" + "z" * 43},
        {"bot_dm_capability_revision": "5"},
        {"bot_installation_ref": "78@guild.example"},
        {"bot_installation_type": "user"},
    ):
        rejected = bot._event_model(  # noqa: SLF001
            "CALL_CREATE",
            payload | substitution,
            target="https://chat.example",
            topic="user:apps.example:8",
            sequence=1,
        )
        assert isinstance(rejected, RawEvent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "dropped"),
    [
        (401, "BOT_TOKEN_INVALID", False),
        (403, "BOT_DM_GRANT_REQUIRED", False),
        (404, "BOT_DM_GRANT_NOT_FOUND", False),
        (409, "BOT_DM_GRANT_CONFLICT", False),
        (502, "BOT_DM_AUTHORITY_INVALID", False),
        (403, "BOT_DM_GRANT_FENCED", True),
    ],
)
async def test_dm_reconciliation_drops_only_explicit_authority_fence(
    status: int,
    code: str,
    dropped: bool,
) -> None:
    bot = client()
    channel_ref = EntityRef(5, "chat.example")
    context = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "a" * 43,
        revision=1,
        expires_at=client_module.time.time() + 600,
        target="https://chat.example",
    )
    key = (channel_ref, context.grant_id)
    bot._dm_capabilities[key] = context  # noqa: SLF001
    bot._dm_default_capabilities[channel_ref] = context.grant_id  # noqa: SLF001
    bot._capability_targets[context.target].add(key)  # noqa: SLF001
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=ApiError(status, code, "refresh rejected")
    )

    await bot._reconcile_dm_capabilities_for_target(context.target)  # noqa: SLF001

    assert (key not in bot._dm_capabilities) is dropped  # noqa: SLF001


@pytest.mark.asyncio
async def test_open_dm_dynamically_connects_capability_authority_when_running() -> None:
    bot = client()
    bot._started = True  # noqa: SLF001 - exercise live target lifecycle
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "5",
            "origin_domain": "chat.example",
            "type": 1,
            "conversation_type": "direct",
            "recipients": [
                {
                    "id": "12",
                    "origin_domain": "chat.example",
                    "username": "alice",
                    "bot": False,
                    "account_type": "human",
                }
            ],
            "bot_dm_capability_id": "kbdg_" + "a" * 43,
            "bot_dm_capability_revision": "1",
            "bot_dm_capability_expires_at": "2099-01-01T00:00:00+00:00",
            "bot_dm_capability_lineage_ref": "99@chat.example",
            "bot_installation_ref": "77@guild.example",
            "bot_installation_type": "guild",
            "authority_origin": "https://chat.example",
        }
    )
    ensure_gateway = Mock()
    bot._ensure_dm_gateway_task = ensure_gateway  # type: ignore[method-assign]  # noqa: SLF001
    refresh_loop = AsyncMock()
    bot._dm_capability_refresh_loop = refresh_loop  # type: ignore[method-assign]  # noqa: SLF001

    channel = await bot.open_dm(
        "alice@chat.example",
        installation_ref=EntityRef(77, "guild.example"),
    )
    await asyncio.sleep(0)

    ensure_gateway.assert_called_once_with((channel.ref, "kbdg_" + "a" * 43))
    refresh_loop.assert_awaited_once_with((channel.ref, "kbdg_" + "a" * 43))
    await bot.close()


@pytest.mark.asyncio
async def test_dm_capability_refresh_preserves_authority_error_and_expires_closed() -> (
    None
):
    bot = client()
    channel_ref = EntityRef(5, "chat.example")
    context = client_module._DMCapabilityContext(  # noqa: SLF001
        installation_ref=EntityRef(77, "guild.example"),
        installation_type="guild",
        grant_id="kbdg_" + "a" * 43,
        revision=1,
        expires_at=client_module.time.time() + 0.05,
        target="https://chat.example",
    )
    key = (channel_ref, context.grant_id)
    bot._dm_capabilities[key] = context  # noqa: SLF001
    bot._dm_default_capabilities[channel_ref] = context.grant_id  # noqa: SLF001
    unavailable = ApiError(
        503,
        "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE",
        "installation authority unavailable",
    )
    bot.request = AsyncMock(side_effect=unavailable)  # type: ignore[method-assign]
    with pytest.raises(ApiError) as refresh_error:
        await bot._dm_capability_headers_for_path(  # noqa: SLF001
            "/api/v1/bots/channels/5@chat.example/messages"
        )
    assert refresh_error.value.code == "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"

    bot._started = True  # noqa: SLF001
    bot.dispatch = AsyncMock()  # type: ignore[method-assign]
    remove_target = AsyncMock()
    bot._remove_discovered_target = remove_target  # type: ignore[method-assign]  # noqa: SLF001
    bot._capability_targets[context.target].add(key)  # noqa: SLF001
    await asyncio.wait_for(bot._dm_capability_refresh_loop(key), timeout=0.5)  # noqa: SLF001

    assert key not in bot._dm_capabilities  # noqa: SLF001
    remove_target.assert_awaited_once_with(context.target)


def test_dm_open_rejection_event_remains_parseable_for_legacy_outbox_work() -> None:
    bot = client()
    event = bot._event_model(  # noqa: SLF001 - public Gateway wire contract
        "DM_OPEN_REJECTED",
        {
            "pair_key": "a" * 64,
            "code": "DM_OPEN_REJECTED",
            "authority_domain": "chat.example",
        },
        target="https://apps.example",
        topic="user:apps.example:10",
        sequence=3,
    )
    assert event == DMOpenRejectedEvent(
        target="https://apps.example",
        pair_key="a" * 64,
        code="DM_OPEN_REJECTED",
        authority_domain="chat.example",
    )


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
async def test_ordinary_messages_serialize_allowed_mentions_for_send_reply_and_edit() -> (
    None
):
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "author_id": "20",
        "author_domain": "users.example",
        "author": {
            "id": "20",
            "origin_domain": "users.example",
            "username": "alice",
            "display_name": "Alice",
        },
        "content": "hello",
        "created_at": "2026-08-18T00:00:00+00:00",
        "attachments": [],
        "bot_installation_id": "77",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            payload,
            {**payload, "id": "10", "content": "reply"},
            {**payload, "content": "edited"},
        ]
    )
    policy = {
        "parse": [],
        "users": ["20@users.example"],
        "roles": [],
        "replied_user": False,
    }

    message = await bot.send_message(
        EntityRef(5, "chat.example"),
        "hello",
        target="https://chat.example",
        installation_id=77,
        allowed_mentions=policy,
    )
    await message.reply("reply", mention_author=True)
    await message.edit(
        "edited",
        allowed_mentions={
            "parse": [],
            "users": [],
            "roles": [],
            "replied_user": False,
        },
    )

    create_body = bot.request.await_args_list[0].kwargs["json"]
    reply_body = bot.request.await_args_list[1].kwargs["json"]
    edit_body = bot.request.await_args_list[2].kwargs["json"]
    assert create_body["allowed_mentions"] == policy
    assert reply_body["allowed_mentions"] == {
        "parse": ["everyone", "roles", "users"],
        "users": [],
        "roles": [],
        "replied_user": True,
    }
    assert edit_body["allowed_mentions"] == {
        "parse": [],
        "users": [],
        "roles": [],
        "replied_user": False,
    }


@pytest.mark.asyncio
async def test_message_reply_mentions_require_a_bound_reply_and_no_external_envelope() -> (
    None
):
    bot = client()
    bot.request = AsyncMock()  # type: ignore[method-assign]
    channel = EntityRef(5, "chat.example")
    author = EntityRef(20, "users.example")

    with pytest.raises(ValueError, match="requires reply_to"):
        await bot.send_message(channel, replied_user_ref=author)
    with pytest.raises(ValueError, match="already binds"):
        await bot.send_message(
            channel,
            reply_to=EntityRef(9, "chat.example"),
            e2ee={"ciphertext": "opaque"},
            allowed_mentions={"parse": []},
        )
    bot.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_authority_forward_acquires_and_submits_exact_source_proof() -> (
    None
):
    bot = client()
    source = Message.from_payload(
        bot,
        "https://source.example",
        {
            "id": "90",
            "origin_domain": "source.example",
            "channel_id": "9",
            "channel_domain": "source.example",
            "author_id": "1",
            "author_domain": "apps.example",
            "content": "source",
            "created_at": "2026-08-28T00:00:00+00:00",
            "attachments": [],
            "bot_installation_id": "77",
        },
    )
    authorization = {
        "type": "message.forward.source.authorized",
        "origin": "source.example",
    }
    destination_payload = {
        "id": "100",
        "origin_domain": "destination.example",
        "channel_id": "10",
        "channel_domain": "destination.example",
        "content": None,
        "created_at": "2026-08-28T00:01:00+00:00",
        "attachments": [],
        "message_snapshots": [{"message": {"content": "source"}}],
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"authorization": authorization}, destination_payload]
    )

    message = await bot.send_message(
        EntityRef(10, "destination.example"),
        target="https://destination.example",
        installation_id=88,
        forward=source,
        client_nonce="forward-sdk-1",
    )

    proof_call, create_call = bot.request.await_args_list
    assert proof_call.args == (
        "POST",
        "/api/v1/bots/channels/9@source.example/messages/90@source.example/forward-authorize",
    )
    assert proof_call.kwargs["target"] == "https://source.example"
    assert proof_call.kwargs["headers"] == {"X-Kaede-Bot-Installation": "77"}
    assert proof_call.kwargs["json"] == {
        "destination_channel_id": "10@destination.example",
        "destination_encryption_mode": "plaintext",
        "client_nonce": "forward-sdk-1",
    }
    assert create_call.kwargs["target"] == "https://destination.example"
    assert create_call.kwargs["headers"] == {"X-Kaede-Bot-Installation": "88"}
    assert create_call.kwargs["json"]["forwarded_message_id"] == "90@source.example"
    assert create_call.kwargs["json"]["forward_source_proof"] == authorization
    assert create_call.kwargs["json"]["client_nonce"] == "forward-sdk-1"
    assert message.forward_snapshot == {"content": "source"}


@pytest.mark.asyncio
async def test_encrypted_source_forward_to_plaintext_submits_committed_disclosure() -> (
    None
):
    bot = client()
    source_snapshot = {
        "content": "decrypted source",
        "embeds": [],
        "components": [],
        "attachments": [],
        "mention_user_refs": [],
        "sticker_items": [],
        "message_snapshots": [],
        "message_type": 0,
        "flags": 0,
        "created_at": "2026-08-28T00:00:00+00:00",
        "edited_at": None,
    }
    source = Message.from_payload(
        bot,
        "https://source.example",
        {
            "id": "90",
            "origin_domain": "source.example",
            "channel_id": "9",
            "channel_domain": "source.example",
            "author_id": "1",
            "author_domain": "apps.example",
            "content": "decrypted source",
            "e2ee": {
                "forward_projection_version": 2,
                "forward_projection_digest": encrypted_forward_snapshot_digest(
                    source_snapshot
                ),
            },
            "created_at": source_snapshot["created_at"],
            "attachments": [],
            "bot_installation_id": "77",
        },
    )
    authorization = {"type": "message.forward.source.authorized"}
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"authorization": authorization},
            {
                "id": "100",
                "origin_domain": "destination.example",
                "channel_id": "10",
                "channel_domain": "destination.example",
                "content": None,
                "created_at": "2026-08-28T00:01:00+00:00",
                "attachments": [],
                "message_snapshots": [{"message": source_snapshot}],
            },
        ]
    )

    with pytest.raises(ValueError, match="explicit disclosure consent"):
        await bot.send_message(
            EntityRef(10, "destination.example"),
            target="https://destination.example",
            installation_id=88,
            forward=source,
            client_nonce="forward-disclose-refused",
        )
    bot.request.assert_not_awaited()

    await bot.send_message(
        EntityRef(10, "destination.example"),
        target="https://destination.example",
        installation_id=88,
        forward=source,
        allow_plaintext_forward_disclosure=True,
        client_nonce="forward-disclose-1",
    )

    create_body = bot.request.await_args_list[1].kwargs["json"]
    assert create_body["forward_snapshot"] == source_snapshot
    assert create_body["attachment_ids"] == []
    assert create_body["forward_source_proof"] == authorization


@pytest.mark.asyncio
async def test_dm_message_resources_keep_one_installation_for_every_operation() -> None:
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": "hello",
        "created_at": "2026-08-18T00:00:00+00:00",
        "bot_installation_id": "77",
        "attachments": [
            {
                "id": "70",
                "origin_domain": "chat.example",
                "filename": "notes.txt",
                "content_type": "text/plain",
                "size": 5,
            }
        ],
    }
    message = Message.from_payload(bot, "https://chat.example", payload)
    assert message.attachments[0].installation_id == 77

    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]
    edited = await message.edit("updated")
    await message.delete()
    await message.add_reaction("👍")
    await message.remove_reaction("👍")
    await message.remove_user_reaction(EntityRef(12, "chat.example"), "👍")
    await message.clear_reactions()
    await message.clear_reaction("👍")
    await message.reaction_users("👍")
    with pytest.raises(Forbidden) as vote_error:
        await message.vote(1)
    with pytest.raises(Forbidden) as remove_vote_error:
        await message.remove_vote(1)
    ended = await message.end_poll()
    await message.pin()
    await message.unpin()

    assert edited.bot_installation_id == 77
    assert ended.bot_installation_id == 77
    assert vote_error.value.code == "BOT_POLL_VOTE_UNSUPPORTED"
    assert remove_vote_error.value.code == "BOT_POLL_VOTE_UNSUPPORTED"
    assert len(bot.request.await_args_list) == 11
    assert all(
        call.kwargs["headers"] == {"X-Kaede-Bot-Installation": "77"}
        for call in bot.request.await_args_list
    )


@pytest.mark.asyncio
async def test_current_pin_resource_is_typed_paginated_and_authority_routed() -> None:
    bot = client()
    channel = EntityRef(5, "chat.example")

    def pinned(message_id: int, timestamp: str) -> dict[str, object]:
        return {
            "pinned_at": timestamp,
            "message": {
                "id": str(message_id),
                "origin_domain": "chat.example",
                "channel_id": "5",
                "channel_domain": "chat.example",
                "content": "saved",
                "created_at": "2026-08-18T00:00:00+00:00",
                "attachments": [],
            },
        }

    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "items": [pinned(9, "2026-08-28T02:00:00+00:00")],
                "has_more": True,
            },
            {
                "items": [pinned(8, "2026-08-28T01:00:00+00:00")],
                "has_more": False,
            },
        ]
    )

    messages = await bot.pins(channel, installation_id=77)

    assert [message.ref.id for message in messages] == [9, 8]
    assert all(message.pinned_at is not None for message in messages)
    assert bot.request.await_args_list[0].args[:2] == (
        "GET",
        "/api/v1/bots/channels/5@chat.example/messages/pins",
    )
    assert bot.request.await_args_list[0].kwargs["params"] == {"limit": 50}
    assert bot.request.await_args_list[1].kwargs["params"] == {
        "limit": 50,
        "before": "2026-08-28T02:00:00+00:00",
    }
    assert all(
        call.kwargs["target"] == "https://chat.example"
        for call in bot.request.await_args_list
    )


@pytest.mark.asyncio
async def test_pin_resource_rejects_nonadvancing_pages_and_uses_modern_paths() -> None:
    bot = client()
    channel = EntityRef(5, "chat.example")
    message = EntityRef(9, "chat.example")
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"items": [], "has_more": True},
            None,
            None,
        ]
    )

    with pytest.raises(ValueError, match="did not advance"):
        await bot.pins(channel)
    await bot.pin_message(channel, message, reason="keep this")
    await bot.unpin_message(channel, message, reason="cleanup")

    assert bot.request.await_args_list[1].args[:2] == (
        "PUT",
        "/api/v1/bots/channels/5@chat.example/messages/pins/9@chat.example",
    )
    assert bot.request.await_args_list[2].args[:2] == (
        "DELETE",
        "/api/v1/bots/channels/5@chat.example/messages/pins/9@chat.example",
    )
    assert (
        bot.request.await_args_list[1].kwargs["headers"]["X-Audit-Log-Reason"]
        == "keep this"
    )
    assert (
        bot.request.await_args_list[2].kwargs["headers"]["X-Audit-Log-Reason"]
        == "cleanup"
    )


@pytest.mark.asyncio
async def test_voice_message_send_is_typed_and_rejects_mixed_bodies() -> None:
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": None,
        "created_at": "2026-08-18T00:00:00+00:00",
        "attachments": [],
        "flags": 1 << 13,
    }
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    message = await bot.send_message(
        EntityRef(5, "chat.example"),
        target="https://chat.example",
        attachment_ids=[70],
        voice_message=True,
    )

    assert message.is_voice_message is True
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["json"] == {
        "content": None,
        "attachment_ids": [70],
        "sticker_ids": [],
        "mention_user_ids": [],
        "embeds": [],
        "voice_message": True,
        "flags": 0,
    }
    with pytest.raises(ValueError, match="exactly one audio attachment"):
        await bot.send_message(
            EntityRef(5, "chat.example"),
            target="https://chat.example",
            voice_message=True,
        )
    with pytest.raises(ValueError, match="cannot include content"):
        await bot.send_message(
            EntityRef(5, "chat.example"),
            "caption",
            target="https://chat.example",
            attachment_ids=[70],
            voice_message=True,
        )


@pytest.mark.asyncio
async def test_message_view_timeout_disables_remote_components() -> None:
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": None,
        "created_at": "2026-08-18T00:00:00+00:00",
        "attachments": [],
        "components": [],
        "application_id": "1",
        "application_domain": "apps.example",
        "view_version": 1,
    }
    bot.request = AsyncMock(side_effect=[payload, payload])  # type: ignore[method-assign]
    view = View(
        [ActionRow([Button(label="Approve", custom_id="approve")])],
        timeout=0.01,
    )

    message = await bot.send_message(
        EntityRef(5, "chat.example"),
        view=view,
        target="https://chat.example",
    )

    assert await view.wait() is True
    assert message.interaction_id is None
    timeout_edit = bot.request.await_args_list[1]
    assert timeout_edit.args == (
        "PATCH",
        "/api/v1/bots/channels/5@chat.example/messages/9@chat.example",
    )
    assert timeout_edit.kwargs["json"]["view_version"] == 1
    assert (
        timeout_edit.kwargs["json"]["components"][0]["components"][0]["disabled"]
        is True
    )


def test_public_interaction_message_preserves_response_identity() -> None:
    bot = client()
    message = Message.from_payload(
        bot,
        "https://chat.example",
        {
            "id": "9",
            "origin_domain": "chat.example",
            "channel_id": "5",
            "channel_domain": "chat.example",
            "content": "done",
            "attachments": [],
            "interaction_id": "70",
            "response_id": "81",
        },
    )

    assert message.interaction_id == 70
    assert message.interaction_response_id == 81


@pytest.mark.asyncio
async def test_public_followup_timeout_uses_response_not_message_id() -> None:
    bot = client()
    payload = {
        "id": "9",
        "origin_domain": "chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "content": "choose",
        "attachments": [],
        "interaction_id": "70",
        "response_id": "81",
        "view_version": 2,
    }
    bot.request = AsyncMock(side_effect=[payload, payload])  # type: ignore[method-assign]
    view = View(
        [ActionRow([Button(label="Choose", custom_id="choose")])],
        timeout=0.01,
    )

    message = await bot.create_interaction_followup(
        70,
        "choose",
        target="https://chat.example",
        view=view,
    )

    assert isinstance(message, Message)
    assert await view.wait() is True
    timeout_edit = bot.request.await_args_list[1]
    assert timeout_edit.args == (
        "PATCH",
        "/api/v1/bots/interactions/70/followups/81",
    )
    assert timeout_edit.kwargs["json"]["view_version"] == 2


@pytest.mark.asyncio
async def test_download_attachment_stops_after_the_first_byte_over_limit(
    monkeypatch,
) -> None:
    bot = client()
    bot._redirect_location = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "https://media.chat.example/object",
            "https://media.chat.example",
        )
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
            assert (method, url) == ("GET", "https://media.chat.example/object")
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


def test_message_retains_public_interaction_metadata() -> None:
    bot = client()
    metadata = {
        "id": "29",
        "origin_domain": "guild.example",
        "interaction_ref": "29@guild.example",
        "type": "command",
        "command_name": "ship",
        "command_type": "chat_input",
        "user": {
            "id": "4",
            "origin_domain": "users.example",
            "username": "human",
            "display_name": None,
            "avatar_hash": None,
            "bot": False,
        },
        "user_ref": "4@users.example",
        "application_ref": "5@apps.example",
        "integration_type": "guild_install",
        "authorizing_integration_owners": {"guild_install": "2@guild.example"},
    }
    message = Message.from_payload(
        bot,
        "https://guild.example",
        {
            "id": "30",
            "origin_domain": "guild.example",
            "channel_id": "20",
            "channel_domain": "guild.example",
            "content": "release notes",
            "message_type": 20,
            "application_id": "5",
            "application_domain": "apps.example",
            "interaction_metadata": metadata,
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
        },
    )

    assert message.interaction_metadata == metadata
    assert message.interaction_metadata is not metadata


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
async def test_encrypted_forum_shell_and_claim_use_two_phase_bot_routes() -> None:
    bot = client()
    envelope = {
        "version": 2,
        "operation": "create",
        "rich_payload_digest": "opaque",
        "sender_device_id": "kbe_" + "a" * 43,
    }
    shell = thread_payload(
        id="30",
        e2ee_required=True,
        encryption_mode="plaintext",
        encryption_state="plaintext",
        starter_message=None,
        message=None,
        starter_reservation={"client_nonce": "claim-1", "claimed": False},
    )
    claimed = {
        "id": "30",
        "origin_domain": "guild.example",
        "channel_id": "30",
        "channel_domain": "guild.example",
        "author_id": "4",
        "author_domain": "apps.example",
        "content": None,
        "e2ee": envelope,
        "client_nonce": "claim-1",
        "created_at": "2026-08-24T12:00:00+00:00",
        "attachments": [],
    }
    bot.request = AsyncMock(side_effect=[shell, claimed])  # type: ignore[method-assign]

    thread = await bot.start_thread(
        EntityRef(20, "guild.example"),
        "private post",
        starter_reservation_nonce="claim-1",
    )
    assert thread.starter_message is None
    assert thread.starter_reservation == {
        "client_nonce": "claim-1",
        "claimed": False,
    }
    create_call = bot.request.await_args_list[0]
    assert create_call.args == (
        "POST",
        "/api/v1/bots/channels/20@guild.example/threads",
    )
    assert create_call.kwargs["target"] == "https://guild.example"
    assert create_call.kwargs["json"] == {
        "name": "private post",
        "starter_reservation_nonce": "claim-1",
    }

    message = await bot.claim_encrypted_forum_starter(
        thread.ref,
        "claim-1",
        e2ee=envelope,
    )
    assert message.ref == EntityRef(30, "guild.example")
    claim_call = bot.request.await_args_list[1]
    assert claim_call.args == (
        "POST",
        "/api/v1/bots/channels/30@guild.example/starter",
    )
    assert claim_call.kwargs["target"] == "https://guild.example"
    assert claim_call.kwargs["json"]["client_nonce"] == "claim-1"
    assert claim_call.kwargs["json"]["e2ee"] == envelope
    assert claim_call.kwargs["json"]["content"] is None

    with pytest.raises(ValueError, match="cannot include a starter"):
        await bot.start_thread(
            EntityRef(20, "guild.example"),
            "unsafe",
            content="plaintext",
            starter_reservation_nonce="claim-2",
        )


@pytest.mark.asyncio
async def test_forum_post_supports_rich_starter_and_registers_view() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]
    view = View(timeout=60)
    view.add_row(ActionRow([Button(label="Approve", custom_id="approve")]))
    poll = Poll(
        question=PollMedia(text="Ship it?"),
        answers=[
            PollAnswer(PollMedia(text="Yes")),
            PollAnswer(PollMedia(text="No")),
        ],
        duration=24,
    )
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

    thread = await forum.create_post(
        "release notes",
        embeds=[Embed(title="Version 2")],
        view=view,
        poll=poll,
        reply_to=EntityRef(31, "guild.example"),
        mention_user_ids=[EntityRef(41, "guild.example")],
        applied_tag_ids=[7],
    )

    assert bot.request.await_args is not None
    starter = bot.request.await_args.kwargs["json"]["message"]
    assert starter["embeds"] == [{"title": "Version 2"}]
    assert starter["components"][0]["components"][0]["custom_id"] == "approve"
    assert starter["view_timeout_seconds"] == 60
    assert starter["poll"]["question"] == {"text": "Ship it?"}
    assert starter["referenced_message_id"] == "31@guild.example"
    assert starter["mention_user_ids"] == ["41@guild.example"]
    assert thread.starter_message_ref in bot._views


@pytest.mark.asyncio
async def test_forum_post_accepts_rich_only_and_rejects_mixed_forward() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]
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

    await forum.create_post("rich", embeds=[Embed(description="body")])
    await forum.create_post("voice", attachment_ids=[44], voice_message=True)
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["json"]["message"] == {
        "attachment_ids": ["44"],
        "voice_message": True,
    }
    with pytest.raises(ValueError, match="forwarded thread starter"):
        await forum.create_post(
            "forward",
            "copied body",
            forward=EntityRef(32, "guild.example"),
        )


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
    bot.request = AsyncMock(return_value=thread_payload())  # type: ignore[method-assign]
    thread = Channel.from_payload(
        bot,
        "https://guild.example",
        thread_payload(),
    )

    deleted = await thread.delete()

    assert deleted.ref == thread.ref
    bot.request.assert_awaited_once_with(
        "DELETE",
        "/api/v1/bots/channels/30@guild.example",
        target="https://guild.example",
        headers=None,
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
        headers=None,
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
        headers={},
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
        headers={},
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
        headers={},
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


def test_direct_thread_sync_binds_exact_capability_to_nested_content() -> None:
    bot = client()
    grant_id = "kbdg_" + "g" * 43
    direct_thread = thread_payload(
        origin_domain="chat.example",
        guild_id=None,
        guild_domain=None,
        parent_id="20",
        parent_domain="chat.example",
        type=12,
        starter_message={
            "id": "31",
            "origin_domain": "chat.example",
            "channel_id": "30",
            "channel_domain": "chat.example",
            "content": "encrypted starter",
            "created_at": "2026-08-24T12:00:00+00:00",
            "attachments": [],
        },
    )

    event = bot._event_model(  # noqa: SLF001 - exact Gateway wire contract
        "THREAD_LIST_SYNC",
        {
            "channel_ids": ["20@chat.example"],
            "threads": [direct_thread],
            "members": [],
            "bot_dm_capability_id": grant_id,
            "bot_dm_capability_revision": "4",
            "installation_ref": "77@guild.example",
            "installation_type": "guild",
        },
        target="https://chat.example",
        topic="user:apps.example:8",
        sequence=1,
    )

    assert isinstance(event, ThreadListSyncEvent)
    assert event.guild_ref is None
    assert event.channel_refs == (EntityRef(20, "chat.example"),)
    thread = event.threads[0]
    assert thread.dm_capability_id == grant_id
    assert thread.dm_capability_revision == 4
    assert thread.installation_ref == EntityRef(77, "guild.example")
    assert thread.starter_message is not None
    assert thread.starter_message.dm_capability_id == grant_id
    assert thread.starter_message.dm_capability_revision == 4


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


def tracker_task_payload() -> dict[str, object]:
    return {
        "id": "301",
        "origin_domain": "guild.example",
        "channel_id": "20",
        "channel_domain": "guild.example",
        "lane_id": "201",
        "lane_domain": "guild.example",
        "number": "7",
        "key": "OPS-7",
        "title": "Ship the release",
        "description": "Run the production checklist",
        "priority": "high",
        "position": 0,
        "due_at": "2026-08-27T12:00:00+00:00",
        "completed_at": None,
        "creator": {
            "id": "5",
            "origin_domain": "users.example",
            "username": "alice",
            "display_name": "Alice",
        },
        "assignee": None,
        "version": "task-v1",
    }


def tracker_lane_payload() -> dict[str, object]:
    return {
        "id": "201",
        "origin_domain": "guild.example",
        "channel_id": "20",
        "channel_domain": "guild.example",
        "name": "Planned",
        "color": 0xF59E0B,
        "kind": "planned",
        "completed": False,
        "position": 0,
        "task_count": 1,
        "version": "lane-v1",
    }


def tracker_board_payload() -> dict[str, object]:
    return {
        "channel_id": "20",
        "channel_domain": "guild.example",
        "key_prefix": "OPS",
        "next_task_number": "8",
        "permissions": str((1 << 58) - 1),
        "version": "board-v1",
        "lanes": [tracker_lane_payload()],
        "tasks": [tracker_task_payload()],
    }


@pytest.mark.asyncio
async def test_tracker_bot_crud_uses_composite_refs_and_versions() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_board_payload()
    )

    board = await bot.fetch_tracker(
        EntityRef(20, "guild.example"), target="https://guild.example"
    )

    assert board.key_prefix == "OPS"
    assert board.next_task_number == 8
    assert board.lanes[0].task_count == 1
    assert board.tasks[0].key == "OPS-7"
    assert board.tasks[0].creator.handle == "alice@users.example"

    bot.request = AsyncMock(return_value=tracker_task_payload())  # type: ignore[method-assign]
    due_at = datetime.fromisoformat("2026-08-27T12:00:00+00:00")
    task = await board.create_task(
        EntityRef(201, "guild.example"),
        "Ship the release",
        description="Run the production checklist",
        priority="high",
        due_at=due_at,
        client_nonce="deploy-7",
    )

    assert task.ref == EntityRef(301, "guild.example")
    assert bot.request.await_args is not None
    assert bot.request.await_args.args[:2] == (
        "POST",
        "/api/v1/bots/channels/20@guild.example/tracker/tasks",
    )
    assert bot.request.await_args.kwargs["json"] == {
        "lane_id": "201@guild.example",
        "title": "Ship the release",
        "description": "Run the production checklist",
        "priority": "high",
        "due_at": "2026-08-27T12:00:00+00:00",
        "assignee_id": None,
        "client_nonce": "deploy-7",
    }

    bot.request = AsyncMock(return_value=tracker_task_payload())  # type: ignore[method-assign]
    await task.edit(description=None, assignee=None)
    bot.request.assert_awaited_once_with(
        "PATCH",
        "/api/v1/bots/channels/20@guild.example/tracker/tasks/301@guild.example",
        target="https://guild.example",
        json={"description": None, "assignee_id": None},
        headers={"If-Match": "task-v1"},
    )


@pytest.mark.asyncio
async def test_qualified_tracker_ref_selects_its_authority_with_multiple_targets() -> (
    None
):
    bot = client()
    bot._targets.update(  # noqa: SLF001 - exercise target selection without network setup
        {
            "https://guild.example": AsyncMock(),
            "https://other.example": AsyncMock(),
        }
    )
    bot.request = AsyncMock(return_value=tracker_board_payload())  # type: ignore[method-assign]

    await bot.fetch_tracker(EntityRef(20, "guild.example"))

    bot.request.assert_awaited_once_with(
        "GET",
        "/api/v1/bots/channels/20@guild.example/tracker",
        target="https://guild.example",
    )


@pytest.mark.asyncio
async def test_tracker_board_and_lane_convenience_requests_are_complete() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_board_payload()
    )
    board = await bot.fetch_tracker(
        EntityRef(20, "guild.example"), target="https://guild.example"
    )

    updated_board_payload = {
        **tracker_board_payload(),
        "key_prefix": "REL",
        "version": "board-v2",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=updated_board_payload
    )
    updated_board = await board.edit(key_prefix="REL")

    assert updated_board.key_prefix == "REL"
    bot.request.assert_awaited_once_with(
        "PATCH",
        "/api/v1/bots/channels/20@guild.example/tracker",
        target="https://guild.example",
        json={"key_prefix": "REL"},
        headers={"If-Match": "board-v1"},
    )

    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_lane_payload()
    )
    lane = await board.create_lane(
        "Verification",
        color=0x22C55E,
        kind="custom",
        completed=True,
        position=2,
    )

    assert lane.ref == EntityRef(201, "guild.example")
    bot.request.assert_awaited_once_with(
        "POST",
        "/api/v1/bots/channels/20@guild.example/tracker/lanes",
        target="https://guild.example",
        json={
            "name": "Verification",
            "color": 0x22C55E,
            "kind": "custom",
            "completed": True,
            "position": 2,
        },
    )

    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_lane_payload()
    )
    await lane.edit(
        name="Ready",
        color=0x3B82F6,
        kind="planned",
        completed=False,
    )
    bot.request.assert_awaited_once_with(
        "PATCH",
        ("/api/v1/bots/channels/20@guild.example/tracker/lanes/201@guild.example"),
        target="https://guild.example",
        json={
            "name": "Ready",
            "color": 0x3B82F6,
            "kind": "planned",
            "completed": False,
        },
        headers={"If-Match": "lane-v1"},
    )

    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_lane_payload()
    )
    await lane.move(4)
    bot.request.assert_awaited_once_with(
        "POST",
        ("/api/v1/bots/channels/20@guild.example/tracker/lanes/201@guild.example/move"),
        target="https://guild.example",
        json={"position": 4},
        headers={"If-Match": "lane-v1"},
    )

    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await lane.delete()
    bot.request.assert_awaited_once_with(
        "DELETE",
        ("/api/v1/bots/channels/20@guild.example/tracker/lanes/201@guild.example"),
        target="https://guild.example",
        headers={"If-Match": "lane-v1"},
    )


@pytest.mark.asyncio
async def test_tracker_task_move_and_delete_convenience_requests_are_complete() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value=tracker_board_payload()
    )
    board = await bot.fetch_tracker(
        EntityRef(20, "guild.example"), target="https://guild.example"
    )
    task = board.tasks[0]
    destination = EntityRef(202, "guild.example")

    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={**tracker_task_payload(), "lane_id": "202"}
    )
    await task.move(destination, 3)
    bot.request.assert_awaited_once_with(
        "POST",
        ("/api/v1/bots/channels/20@guild.example/tracker/tasks/301@guild.example/move"),
        target="https://guild.example",
        json={"lane_id": "202@guild.example", "position": 3},
        headers={"If-Match": "task-v1"},
    )

    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    await task.delete()
    bot.request.assert_awaited_once_with(
        "DELETE",
        ("/api/v1/bots/channels/20@guild.example/tracker/tasks/301@guild.example"),
        target="https://guild.example",
        headers={"If-Match": "task-v1"},
    )


@pytest.mark.asyncio
async def test_tracker_channel_creation_sends_the_optional_key_prefix() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 17,
            "name": "Release plan",
        }
    )

    channel = await bot.create_channel(
        EntityRef(2, "guild.example"),
        "Release plan",
        type=17,
        tracker_key_prefix="REL",
        target="https://guild.example",
    )

    assert channel.is_tracker
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["json"]["tracker_key_prefix"] == "REL"


@pytest.mark.asyncio
async def test_guild_tracker_channel_creation_forwards_the_optional_key_prefix() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "20",
            "origin_domain": "guild.example",
            "guild_id": "2",
            "guild_domain": "guild.example",
            "type": 17,
            "name": "Release plan",
        }
    )
    guild = Guild(
        client=bot,
        target="https://guild.example",
        ref=EntityRef(2, "guild.example"),
        name="Guild",
    )

    channel = await guild.create_channel(
        "Release plan", type=17, tracker_key_prefix="REL"
    )

    assert channel.is_tracker
    assert bot.request.await_args is not None
    assert bot.request.await_args.kwargs["json"]["tracker_key_prefix"] == "REL"


@pytest.mark.asyncio
async def test_tracker_gateway_events_are_typed() -> None:
    bot = client()
    seen: list[object] = []

    for event_name in (
        "TRACKER_BOARD_UPDATE",
        "TRACKER_LANE_CREATE",
        "TRACKER_LANE_DELETE",
        "TRACKER_TASK_UPDATE",
        "TRACKER_TASK_DELETE",
    ):
        bot.listen(event_name)(lambda event: _record_event(seen, event))

    await bot.dispatch(
        "TRACKER_BOARD_UPDATE",
        {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "key_prefix": "OPS",
            "next_task_number": "8",
            "version": "board-v2",
            "full_refresh": True,
            "reason": "lane_completion_updated",
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "TRACKER_LANE_CREATE",
        {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "lane": tracker_lane_payload(),
            "board_version": "board-v3",
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "TRACKER_LANE_DELETE",
        {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "lane_id": "201",
            "lane_domain": "guild.example",
            "board_version": "board-v4",
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "TRACKER_TASK_UPDATE",
        {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "task": tracker_task_payload(),
            "board_version": "board-v5",
        },
        target="https://guild.example",
    )
    await bot.dispatch(
        "TRACKER_TASK_DELETE",
        {
            "channel_id": "20",
            "channel_domain": "guild.example",
            "task_id": "301",
            "task_domain": "guild.example",
            "board_version": "board-v6",
        },
        target="https://guild.example",
    )

    assert isinstance(seen[0], TrackerBoardUpdateEvent)
    assert seen[0].version == "board-v2"
    assert seen[0].full_refresh is True
    assert seen[0].reason == "lane_completion_updated"
    assert isinstance(seen[1], TrackerLane)
    assert seen[1].board_version == "board-v3"
    assert isinstance(seen[2], TrackerLaneDeleteEvent)
    assert seen[2].board_version == "board-v4"
    assert isinstance(seen[3], TrackerTask)
    assert seen[3].board_version == "board-v5"
    assert isinstance(seen[4], TrackerTaskDeleteEvent)
    assert seen[4].board_version == "board-v6"


async def _record_event(seen: list[object], event: object) -> None:
    seen.append(event)
