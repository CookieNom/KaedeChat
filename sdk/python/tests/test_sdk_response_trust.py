from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import (
    AutoModRule,
    BulkBanResult,
    Client,
    EntityRef,
    PruneEstimate,
    PruneResult,
    VoiceStateEvent,
    WorkerState,
)


TARGET = "https://chat.example"
CHANNEL = EntityRef(10, "chat.example")
MESSAGE = EntityRef(20, "chat.example")


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "trust",
        )
    )


def private_response(
    *,
    response_id: str = "30",
    interaction_id: str = "90",
    sequence: object = 0,
    revision: object = "1",
    channel_id: str = "10",
    channel_domain: str = "chat.example",
    **overrides: object,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": response_id,
        "interaction_id": interaction_id,
        "response_id": response_id,
        "response_ref": f"{response_id}@chat.example",
        "sequence": sequence,
        "revision": revision,
        "ephemeral": True,
        "response_type": 4,
        "channel_id": channel_id,
        "channel_domain": channel_domain,
        "application_ref": "1@apps.example",
        "content": "private",
    }
    payload.update(overrides)
    return payload


def callback_wrapper(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "interaction": {
            "id": "90",
            "type": 2,
            "response_message_id": message["id"],
            "response_message_loading": False,
            "response_message_ephemeral": True,
        },
        "resource": {"type": 4, "message": message},
    }


def remember_interaction(bot: Client) -> None:
    bot._remember_interaction_lifecycle_grant(  # noqa: SLF001
        {
            "id": "90",
            "interaction_ref": "90@chat.example",
            "channel_ref": "10@chat.example",
            "token": "t" * 43,
            "expires_at": "2099-08-29T00:00:00+00:00",
            "integration_type": "guild_install",
            "installation_revision": "1",
            "installation_id": "44",
        },
        target=TARGET,
    )


@pytest.mark.asyncio
async def test_private_interaction_response_happy_path_tracks_revision() -> None:
    bot = client()
    remember_interaction(bot)
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            callback_wrapper(private_response()),
            private_response(),
            private_response(revision="2", content="edited"),
        ]
    )

    created = await bot.interaction_callback(90, 4, target=TARGET)
    fetched = await bot.fetch_original_interaction_response(90, target=TARGET)
    edited = await bot.edit_original_interaction_response(
        90,
        target=TARGET,
        content="edited",
    )

    assert created["response_ref"] == "30@chat.example"  # type: ignore[index]
    assert fetched["sequence"] == 0  # type: ignore[index]
    assert edited["revision"] == "2"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value | {"response_id": "31"},
        lambda value: value | {"response_ref": "30@evil.example"},
        lambda value: value | {"interaction_id": "91"},
        lambda value: value | {"application_ref": "2@apps.example"},
        lambda value: value | {"channel_domain": "evil.example"},
        lambda value: value | {"channel_ref": "11@chat.example"},
        lambda value: value | {"sequence": True},
        lambda value: value | {"revision": "01"},
        lambda value: {key: item for key, item in value.items() if key != "revision"},
        lambda value: value | {"application_id": "1"},
    ],
)
async def test_private_interaction_response_rejects_substitution_and_alias_conflict(
    mutate: Any,
) -> None:
    bot = client()
    remember_interaction(bot)
    bot.request = AsyncMock(return_value=mutate(private_response()))  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        await bot.fetch_original_interaction_response(90, target=TARGET)


@pytest.mark.asyncio
async def test_private_interaction_response_rejects_revision_rollback_or_stall() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            private_response(revision="2"),
            private_response(revision="1"),
            private_response(revision="2"),
        ]
    )

    await bot.fetch_original_interaction_response(90, target=TARGET)
    with pytest.raises(ValueError, match="revision"):
        await bot.fetch_original_interaction_response(90, target=TARGET)
    with pytest.raises(ValueError, match="revision"):
        await bot.edit_original_interaction_response(
            90,
            target=TARGET,
            content="stale",
        )


@pytest.mark.asyncio
async def test_interaction_reference_only_and_non_message_callback_variants_survive() -> (
    None
):
    bot = client()
    autocomplete = {
        "interaction": {
            "id": "90",
            "type": 4,
            "response_message_loading": False,
            "response_message_ephemeral": False,
        },
        "resource": {"type": 8},
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[{"id": "30", "ephemeral": True}, autocomplete]
    )

    legacy = await bot.fetch_original_interaction_response(90, target=TARGET)
    returned = await bot.interaction_callback(90, 8, target=TARGET)

    assert legacy == {"id": "30", "ephemeral": True}
    assert returned == autocomplete


@pytest.mark.asyncio
async def test_type_seven_source_projection_does_not_relabel_original_response() -> (
    None
):
    bot = client()
    source = private_response(revision="2")
    update_wrapper = callback_wrapper(source)
    update_wrapper["resource"]["type"] = 7
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            update_wrapper,
            private_response(response_id="31", revision="1"),
        ]
    )

    updated = await bot.interaction_callback(90, 7, target=TARGET)
    original = await bot.fetch_original_interaction_response(90, target=TARGET)

    assert updated["response_ref"] == "30@chat.example"  # type: ignore[index]
    assert original["response_ref"] == "31@chat.example"  # type: ignore[index]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["interaction"].pop("response_message_ephemeral"),
        lambda value: value["interaction"].update(response_message_id="31"),
        lambda value: value["interaction"].update(response_message_ephemeral=False),
        lambda value: value["resource"].update(type=7),
    ],
)
async def test_callback_wrapper_rejects_incomplete_or_conflicting_identity(
    mutate: Any,
) -> None:
    bot = client()
    raw = callback_wrapper(private_response())
    mutate(raw)
    bot.request = AsyncMock(return_value=raw)  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        await bot.interaction_callback(90, 4, target=TARGET)


def user_payload(user_id: int, domain: str = "users.example") -> dict[str, object]:
    return {"id": str(user_id), "origin_domain": domain, "username": f"u{user_id}"}


@pytest.mark.asyncio
async def test_poll_voter_pages_are_strictly_ordered_and_advance() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "users": [user_payload(1), user_payload(2)],
                "next_after": "2@users.example",
            },
            {"users": [user_payload(3)], "next_after": None},
        ]
    )

    first, cursor = await bot.poll_voters(CHANNEL, MESSAGE, 1, target=TARGET, limit=2)
    assert cursor == EntityRef(2, "users.example")
    second, final = await bot.poll_voters(
        CHANNEL,
        MESSAGE,
        1,
        target=TARGET,
        after=cursor,
        limit=2,
    )

    assert [user.ref.id for user in first + second] == [1, 2, 3]
    assert final is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        {
            "users": [user_payload(1), user_payload(1)],
            "next_after": "1@users.example",
        },
        {
            "users": [user_payload(2), user_payload(1)],
            "next_after": "1@users.example",
        },
        {
            "users": [user_payload(1), user_payload(2)],
            "next_after": "1@users.example",
        },
        {
            "users": [user_payload(1)],
            "next_after": "1@users.example",
        },
        {
            "users": [user_payload(1), user_payload(2)],
            "next_after": "2@users.example",
            "message_ref": "20@chat.example",
        },
    ],
)
async def test_poll_voter_page_rejects_duplicate_reordering_cursor_and_aliases(
    page: dict[str, object],
) -> None:
    bot = client()
    bot.request = AsyncMock(return_value=page)  # type: ignore[method-assign]

    with pytest.raises(ValueError):
        await bot.poll_voters(CHANNEL, MESSAGE, 1, target=TARGET, limit=2)


@pytest.mark.asyncio
async def test_poll_voter_cursor_cannot_cross_poll_scope_or_loop() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"users": [user_payload(1)], "next_after": "1@users.example"},
            {"users": [user_payload(1)], "next_after": None},
        ]
    )
    _, cursor = await bot.poll_voters(CHANNEL, MESSAGE, 1, target=TARGET, limit=1)
    assert cursor is not None

    with pytest.raises(ValueError, match="different poll"):
        await bot.poll_voters(
            CHANNEL,
            EntityRef(21, "chat.example"),
            1,
            target=TARGET,
            after=cursor,
            limit=1,
        )
    with pytest.raises(ValueError, match="advance"):
        await bot.poll_voters(
            CHANNEL,
            MESSAGE,
            1,
            target=TARGET,
            after=cursor,
            limit=1,
        )


@pytest.mark.parametrize(
    "override",
    [
        {"connected": "false"},
        {"self_mute": 0},
        {"self_deaf": None},
        {"server_mute": "false"},
        {"server_deaf": 1},
        {"heartbeat": 0},
        {"server_mute": True, "mute": False},
        {"server_deaf": False, "deaf": True},
    ],
)
def test_gateway_voice_state_rejects_boolean_coercion_and_alias_conflict(
    override: dict[str, object],
) -> None:
    bot = client()
    payload: dict[str, Any] = {
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": "20",
        "channel_domain": "chat.example",
        "user_id": "30",
        "user_domain": "users.example",
    }
    payload.update(override)

    with pytest.raises(ValueError, match="boolean"):
        bot._event_model(  # noqa: SLF001
            "VOICE_STATE_UPDATE",
            payload,
            target=TARGET,
            topic="guild:chat.example:10",
            sequence=1,
        )


def test_gateway_voice_state_accepts_consistent_discord_aliases() -> None:
    event = client()._event_model(  # noqa: SLF001
        "VOICE_STATE_UPDATE",
        {
            "guild_id": "10",
            "guild_domain": "chat.example",
            "channel_id": "20",
            "channel_domain": "chat.example",
            "user_id": "30",
            "user_domain": "users.example",
            "connected": True,
            "server_mute": False,
            "mute": False,
            "deaf": True,
            "heartbeat": False,
        },
        target=TARGET,
        topic="guild:chat.example:10",
        sequence=1,
    )

    assert isinstance(event, VoiceStateEvent)
    assert event.server_mute is False
    assert event.server_deaf is True


def auto_mod_payload() -> dict[str, Any]:
    return {
        "id": "40",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "Block spam",
        "creator_id": "2",
        "creator_domain": "apps.example",
        "event_type": "message_send",
        "trigger_type": "keyword",
        "trigger_metadata": {
            "keyword_filter": ["blocked"],
            "regex_patterns": [],
            "presets": [],
            "allow_list": [],
            "mention_total_limit": None,
            "mention_raid_protection_enabled": False,
        },
        "actions": [{"type": "block_message", "metadata": {}}],
        "enabled": True,
        "exempt_roles": [],
        "exempt_channels": [],
        "version": 1,
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "path,value",
    [
        (("enabled",), "false"),
        (("trigger_metadata", "keyword_filter"), "blocked"),
        (("trigger_metadata", "presets"), ["not-real"]),
        (("trigger_metadata", "mention_total_limit"), True),
        (("actions",), [7]),
        (("actions",), [{"type": "block_message", "metadata": []}]),
        (
            ("actions",),
            [
                {"type": "block_message", "metadata": {}},
                {"type": "block_message", "metadata": {}},
            ],
        ),
        (("exempt_roles",), ["41@chat.example", "41@chat.example"]),
        (("version",), "1"),
        (("created_at",), "2026-08-29T00:00:00"),
        (("updated_at",), "2026-08-28T00:00:00+00:00"),
    ],
)
def test_automod_rule_response_rejects_lossy_or_filtered_values(
    path: tuple[str, ...], value: object
) -> None:
    payload = auto_mod_payload()
    parent: dict[str, Any] = payload
    for key in path[:-1]:
        nested = parent[key]
        assert isinstance(nested, dict)
        parent = nested
    parent[path[-1]] = value

    with pytest.raises((TypeError, ValueError)):
        AutoModRule.from_payload(client(), TARGET, payload)


def prune_payload(**overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "guild_id": "10",
        "guild_domain": "chat.example",
        "pruned": 1,
        "pruned_user_ids": ["50@users.example"],
        "failed_users": [
            {
                "user_id": "51@users.example",
                "code": "ROLE_TOO_HIGH",
                "message": "Cannot manage user.",
            }
        ],
        "days": 14,
    }
    payload.update(overrides)
    return payload


def test_moderation_parsers_reject_duplicates_overlap_and_detail_reordering() -> None:
    with pytest.raises(ValueError):
        PruneEstimate.from_payload({"pruned": True, "days": 14})
    with pytest.raises(ValueError):
        PruneResult.from_payload(
            prune_payload(pruned_user_ids=["50@users.example", "50@users.example"])
        )
    with pytest.raises(ValueError):
        PruneResult.from_payload(
            prune_payload(
                failed_users=[
                    {
                        "user_id": "50@users.example",
                        "code": "FAILED",
                        "message": "Failed.",
                    }
                ]
            )
        )
    with pytest.raises(ValueError):
        BulkBanResult.from_payload(
            {
                "banned_users": ["60@users.example"],
                "failed_users": ["61@users.example", "62@users.example"],
                "failed_user_details": [
                    {
                        "user_id": "62@users.example",
                        "code": "FAILED",
                        "message": "Failed.",
                    },
                    {
                        "user_id": "61@users.example",
                        "code": "FAILED",
                        "message": "Failed.",
                    },
                ],
            }
        )


@pytest.mark.asyncio
async def test_moderation_callers_bind_request_scope_and_partition() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"pruned": 3, "days": 13},
            prune_payload(guild_id="11"),
            {
                "banned_users": ["60@users.example"],
                "failed_users": [],
                "failed_user_details": [],
            },
        ]
    )

    with pytest.raises(ValueError, match="day window"):
        await bot.estimate_prune(CHANNEL, target=TARGET, days=14)
    with pytest.raises(ValueError, match="scope"):
        await bot.prune_members(CHANNEL, target=TARGET, days=14)
    with pytest.raises(ValueError, match="partition"):
        await bot.bulk_ban_members(
            CHANNEL,
            [EntityRef(60, "users.example"), EntityRef(61, "users.example")],
            target=TARGET,
        )


def test_prune_gateway_event_binds_subscribed_guild() -> None:
    with pytest.raises(ValueError, match="subscribed guild"):
        client()._event_model(  # noqa: SLF001
            "GUILD_MEMBERS_PRUNED",
            prune_payload(guild_id="11"),
            target=TARGET,
            topic="guild:chat.example:10",
            sequence=1,
        )


def test_automod_happy_path_preserves_federated_creator() -> None:
    rule = AutoModRule.from_payload(client(), TARGET, deepcopy(auto_mod_payload()))

    assert rule.guild_ref == CHANNEL
    assert rule.creator_ref == EntityRef(2, "apps.example")
