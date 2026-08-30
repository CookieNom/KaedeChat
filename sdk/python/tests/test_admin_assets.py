from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot import (
    ApplicationAsset,
    ApplicationEmoji,
    Attachment,
    AutoModAction,
    AutoModExecution,
    AutoModRule,
    AutoModTriggerMetadata,
    BulkBanResult,
    Client,
    Emoji,
    EntityRef,
    Guild,
    Invite,
    InviteTargetUsers,
    InviteTargetUsersJobStatus,
    InstanceBan,
    PruneEstimate,
    PruneResult,
    Role,
    Sticker,
    Webhook,
    WorkerState,
)


TARGET = "https://chat.example"
APPLICATION_HOME = "https://apps.example"
GUILD = EntityRef(10, "chat.example")


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def last_await(mock: AsyncMock) -> Any:
    call = mock.await_args
    assert call is not None
    return call


def auto_mod_payload(*, enabled: bool = True) -> dict[str, object]:
    return {
        "id": "20",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "No invites",
        "creator_id": "2",
        "creator_domain": "apps.example",
        "event_type": "message_send",
        "trigger_type": "keyword",
        "trigger_metadata": {
            "keyword_filter": ["discord.gg/*"],
            "regex_patterns": [],
            "presets": [],
            "allow_list": [],
            "mention_total_limit": None,
            "mention_raid_protection_enabled": False,
        },
        "actions": [
            {"type": "block_message", "metadata": {"custom_message": "No ads"}},
            {
                "type": "send_alert_message",
                "metadata": {"channel_id": "30@chat.example"},
            },
        ],
        "enabled": enabled,
        "exempt_roles": ["40@chat.example"],
        "exempt_channels": [],
        "version": 2,
        "created_at": "2026-08-27T00:00:00+00:00",
        "updated_at": "2026-08-27T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_auto_mod_create_and_model_convenience_use_typed_contract() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=auto_mod_payload())  # type: ignore[method-assign]
    metadata = AutoModTriggerMetadata(keyword_filter=["discord.gg/*"])
    actions = [
        AutoModAction.block_message("No ads"),
        AutoModAction.send_alert_message(EntityRef(30, "chat.example")),
    ]

    rule = await bot.create_auto_mod_rule(
        GUILD,
        " No invites ",
        "keyword",
        actions,
        target=TARGET,
        trigger_metadata=metadata,
        exempt_roles=[EntityRef(40, "chat.example")],
        reason="keep chat useful",
    )

    assert isinstance(rule, AutoModRule)
    assert rule.actions[1].channel_ref == EntityRef(30, "chat.example")
    assert rule.exempt_roles == (EntityRef(40, "chat.example"),)
    assert last_await(bot.request).args[:2] == (
        "POST",
        "/api/v1/bots/guilds/10@chat.example/auto-moderation/rules",
    )
    assert last_await(bot.request).kwargs["json"] == {
        "name": "No invites",
        "event_type": "message_send",
        "trigger_type": "keyword",
        "trigger_metadata": metadata.to_dict(),
        "actions": [action.to_dict() for action in actions],
        "enabled": False,
        "exempt_roles": ["40@chat.example"],
        "exempt_channels": [],
    }
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "keep chat useful"
    }

    bot.request.reset_mock()
    bot.request.return_value = auto_mod_payload(enabled=False)
    updated = await rule.edit(enabled=False)
    assert not updated.enabled
    assert last_await(bot.request).args[0] == "PATCH"
    assert last_await(bot.request).kwargs["json"] == {"enabled": False}


@pytest.mark.asyncio
async def test_auto_mod_list_fetch_and_delete_routes() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[[auto_mod_payload()], auto_mod_payload(), None]
    )

    rules = await bot.auto_mod_rules(GUILD, target=TARGET)
    fetched = await bot.fetch_auto_mod_rule(GUILD, 20, target=TARGET)
    await fetched.delete(reason="obsolete")

    assert [rule.ref.id for rule in rules] == [20]
    assert fetched.ref.id == 20
    assert last_await(bot.request).args[:2] == (
        "DELETE",
        "/api/v1/bots/guilds/10@chat.example/auto-moderation/rules/20",
    )
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "obsolete"
    }


@pytest.mark.asyncio
async def test_admin_gateway_events_are_typed() -> None:
    bot = client()
    seen: list[object] = []

    @bot.listen("AUTO_MODERATION_ACTION_EXECUTION")
    async def on_execution(event: object) -> None:
        seen.append(event)

    @bot.listen("GUILD_EMOJI_UPDATE")
    async def on_emoji(event: object) -> None:
        seen.append(event)

    @bot.listen("GUILD_MEMBERS_PRUNED")
    async def on_prune(event: object) -> None:
        seen.append(event)

    await bot.dispatch(
        "AUTO_MODERATION_ACTION_EXECUTION",
        {
            "guild_id": "10",
            "guild_domain": "chat.example",
            "channel_id": "30",
            "channel_domain": "chat.example",
            "rule_id": "20",
            "rule_domain": "chat.example",
            "rule_trigger_type": "keyword",
            "user_id": "50",
            "user_domain": "users.example",
            "action": {"type": "block_message", "metadata": {}},
            "outcome": "blocked",
            "content": "blocked phrase",
            "matched_keyword": "blocked phrase",
            "matched_content": "blocked phrase",
            "alert_system_message_id": None,
            "alert_system_message_domain": None,
            "content_digest": "a" * 64,
        },
        target=TARGET,
    )
    await bot.dispatch("GUILD_EMOJI_UPDATE", emoji_payload(), target=TARGET)
    await bot.dispatch(
        "GUILD_MEMBERS_PRUNED",
        {
            "guild_id": "10",
            "guild_domain": "chat.example",
            "pruned": 1,
            "pruned_user_ids": ["50@users.example"],
            "failed_users": [],
            "days": 7,
        },
        target=TARGET,
    )

    assert isinstance(seen[0], AutoModExecution)
    assert seen[0].actions[0].outcome == "blocked"
    assert seen[0].action.type == "block_message"
    assert seen[0].matched_content == "blocked phrase"
    assert isinstance(seen[1], Emoji)
    assert isinstance(seen[2], PruneResult)


def test_member_profile_automod_execution_has_no_channel() -> None:
    execution = AutoModExecution.from_payload(
        {
            "guild_id": "10",
            "guild_domain": "chat.example",
            "channel_id": None,
            "channel_domain": None,
            "rule_id": "20",
            "rule_domain": "chat.example",
            "rule_trigger_type": "member_profile",
            "user_id": "50",
            "user_domain": "users.example",
            "action": {"type": "block_member_interaction", "metadata": {}},
            "outcome": "blocked",
            "content": "Blocked Profile",
            "matched_keyword": "blocked*",
            "matched_content": "blocked",
            "alert_system_message_id": None,
            "alert_system_message_domain": None,
            "content_digest": "a" * 64,
        }
    )

    assert execution.channel_ref is None
    assert execution.rule_trigger_type == "member_profile"
    assert execution.action.type == "block_member_interaction"


@pytest.mark.parametrize(
    "override",
    [
        {"channel_id": "30", "channel_domain": None},
        {"action": {"type": "timeout", "metadata": {"duration_seconds": True}}},
        {"rule_trigger_type": "not-real"},
    ],
)
def test_automod_execution_rejects_ambiguous_wire_payloads(
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": None,
        "channel_domain": None,
        "rule_id": "20",
        "rule_domain": "chat.example",
        "rule_trigger_type": "member_profile",
        "user_id": "50",
        "user_domain": "users.example",
        "action": {"type": "block_member_interaction", "metadata": {}},
        "outcome": "blocked",
        "content": "Blocked Profile",
        "matched_keyword": "blocked*",
        "matched_content": "blocked",
        "alert_system_message_id": None,
        "alert_system_message_domain": None,
        "content_digest": "a" * 64,
    }
    payload.update(override)

    with pytest.raises(ValueError, match="AutoMod execution"):
        AutoModExecution.from_payload(payload)


@pytest.mark.asyncio
async def test_auto_mod_inputs_reject_invalid_discord_style_combinations() -> None:
    with pytest.raises(ValueError, match="requires only channel_ref"):
        AutoModAction("send_alert_message")
    with pytest.raises(ValueError, match="mention_total_limit"):
        AutoModTriggerMetadata(mention_total_limit=51)
    bot = client()
    with pytest.raises(ValueError, match="keyword rules require"):
        await bot.create_auto_mod_rule(
            GUILD,
            "Broken",
            "keyword",
            [AutoModAction.block_message()],
            target=TARGET,
        ).send(None)

    preset_allow_list = [f"allowed-{index}" for index in range(1_000)]
    preset_metadata = AutoModTriggerMetadata(
        presets=["profanity"],
        allow_list=preset_allow_list,
    )
    assert len(preset_metadata.allow_list) == 1_000

    with pytest.raises(ValueError, match="limited to 100"):
        await bot.create_auto_mod_rule(
            GUILD,
            "Too many keyword exceptions",
            "keyword",
            [AutoModAction.block_message()],
            target=TARGET,
            trigger_metadata=AutoModTriggerMetadata(
                keyword_filter=["blocked"],
                allow_list=preset_allow_list[:101],
            ),
        ).send(None)

    with pytest.raises(ValueError, match="member-profile rule"):
        await bot.create_auto_mod_rule(
            GUILD,
            "Wrong action",
            "keyword",
            [AutoModAction.block_member_interaction()],
            target=TARGET,
            trigger_metadata=AutoModTriggerMetadata(keyword_filter=["blocked"]),
        ).send(None)

    with pytest.raises(ValueError, match="require keyword_filter or regex_patterns"):
        await bot.create_auto_mod_rule(
            GUILD,
            "Empty profile filter",
            "member_profile",
            [AutoModAction.block_member_interaction()],
            target=TARGET,
            event_type="member_update",
        ).send(None)

    with pytest.raises(ValueError, match="duplicate action types"):
        await bot.edit_auto_mod_rule(
            GUILD,
            20,
            target=TARGET,
            actions=[
                AutoModAction.block_message(),
                AutoModAction.block_message("Still blocked"),
            ],
        ).send(None)


@pytest.mark.asyncio
async def test_prune_and_bulk_ban_return_structured_partial_results() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {"pruned": 3, "days": 14},
            {
                "guild_id": "10",
                "guild_domain": "chat.example",
                "pruned": 1,
                "pruned_user_ids": ["50@users.example"],
                "failed_users": [
                    {
                        "user_id": "51@users.example",
                        "code": "ROLE_TOO_HIGH",
                        "message": "That member cannot be managed.",
                    }
                ],
                "days": 14,
            },
            {
                "banned_users": ["60@users.example"],
                "failed_users": ["61@users.example"],
                "failed_user_details": [
                    {
                        "user_id": "61@users.example",
                        "code": "ROLE_TOO_HIGH",
                        "message": "That member cannot be managed.",
                    }
                ],
            },
        ]
    )
    role = EntityRef(40, "chat.example")

    estimate = await bot.estimate_prune(
        GUILD, target=TARGET, days=14, include_roles=[role]
    )
    result = await bot.prune_members(
        GUILD, target=TARGET, days=14, include_roles=[role]
    )
    bans = await bot.bulk_ban_members(
        GUILD,
        [EntityRef(60, "users.example"), EntityRef(61, "users.example")],
        target=TARGET,
        delete_message_seconds=3_600,
        reason="raid",
    )

    assert estimate == PruneEstimate(pruned=3, days=14)
    assert isinstance(result, PruneResult)
    assert result.pruned_users == (EntityRef(50, "users.example"),)
    assert result.failed_users[0].code == "ROLE_TOO_HIGH"
    assert isinstance(bans, BulkBanResult)
    assert bans.banned_users == (EntityRef(60, "users.example"),)
    assert bans.failed_users == (EntityRef(61, "users.example"),)
    assert bans.failed_user_details[0].code == "ROLE_TOO_HIGH"
    assert last_await(bot.request).kwargs["json"] == {
        "user_ids": ["60@users.example", "61@users.example"],
        "delete_message_seconds": 3_600,
        "reason": "raid",
    }


@pytest.mark.asyncio
async def test_instance_bans_are_typed_and_route_to_guild_authority() -> None:
    bot = client()
    expires_at = datetime(2026, 9, 1, tzinfo=UTC)
    payload = {
        "guild_id": "10",
        "guild_domain": "chat.example",
        "instance_domain": "raid.example",
        "reason": "raid",
        "actor_id": "2",
        "actor_domain": "apps.example",
        "created_at": "2026-08-27T00:00:00+00:00",
        "expires_at": expires_at.isoformat(),
    }
    bot.request = AsyncMock(side_effect=[[payload], None, None])  # type: ignore[method-assign]

    bans = await bot.instance_bans(GUILD, after="older.example", limit=5000)
    await bot.ban_instance(
        GUILD,
        "raid.example",
        reason="raid",
        expires_at=expires_at,
    )
    await bans[0].delete(reason="appeal")

    assert len(bans) == 1
    assert isinstance(bans[0], InstanceBan)
    assert bans[0].actor_ref == EntityRef(2, "apps.example")
    assert bans[0].expires_at == expires_at
    calls = bot.request.await_args_list
    assert all(call.kwargs["target"] == TARGET for call in calls)
    assert calls[0].args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/instance-bans",
    )
    assert calls[0].kwargs["params"] == {"limit": 1000, "after": "older.example"}
    assert calls[1].args[:2] == (
        "PUT",
        "/api/v1/bots/guilds/10@chat.example/instance-bans/raid.example",
    )
    assert calls[1].kwargs["json"] == {
        "reason": "raid",
        "expires_at": expires_at.isoformat(),
    }
    assert calls[2].args[:2] == (
        "DELETE",
        "/api/v1/bots/guilds/10@chat.example/instance-bans/raid.example",
    )


def emoji_payload(*, available: bool = True) -> dict[str, object]:
    return {
        "id": "70",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "wave",
        "animated": False,
        "available": available,
        "roles": ["40@chat.example"],
        "media_hash": "a" * 64,
        "creator_id": "2",
        "creator_domain": "apps.example",
        "version": "etag",
    }


def sticker_payload() -> dict[str, object]:
    return {
        "id": "71",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "hello",
        "description": "A greeting",
        "animated": False,
        "available": True,
        "tags": ["wave", "hello"],
        "media_hash": "b" * 64,
        "creator_id": "2",
        "creator_domain": "apps.example",
        "version": "etag",
    }


@pytest.mark.asyncio
async def test_guild_expression_fetch_edit_and_commit_cover_restrictions() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            emoji_payload(),
            sticker_payload(),
            emoji_payload(available=False),
            sticker_payload(),
        ]
    )

    emoji = await bot.fetch_emoji(GUILD, 70, target=TARGET)
    sticker = await bot.fetch_sticker(GUILD, 71, target=TARGET)
    updated = await emoji.edit(
        name="wave2",
        roles=[],
        reason="rename expression",
    )
    await sticker.edit(
        description=None,
        tags=["wave"],
        reason="refresh sticker",
    )

    assert isinstance(emoji, Emoji)
    assert emoji.roles == (EntityRef(40, "chat.example"),)
    assert emoji.creator_ref == EntityRef(2, "apps.example")
    assert isinstance(sticker, Sticker)
    assert sticker.tags == ("wave", "hello")
    assert sticker.creator_ref == EntityRef(2, "apps.example")
    assert not updated.available
    assert last_await(bot.request).kwargs["json"] == {
        "description": None,
        "tags": ["wave"],
    }
    assert bot.request.await_args_list[-2].kwargs["json"] == {
        "name": "wave2",
        "role_ids": [],
    }
    assert bot.request.await_args_list[-2].kwargs["headers"] == {
        "X-Audit-Log-Reason": "rename expression"
    }
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "refresh sticker"
    }

    bot.request.reset_mock()
    bot.request.side_effect = None
    bot.request.return_value = sticker_payload()
    await bot.commit_sticker(
        GUILD,
        EntityRef(99, "chat.example"),
        "Friendly wave",
        target=TARGET,
        description="A greeting",
        tags=["wave", "hello"],
        reason="new sticker",
    )
    assert last_await(bot.request).kwargs["json"]["name"] == "Friendly wave"
    assert last_await(bot.request).kwargs["json"]["tags"] == ["wave", "hello"]
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "new sticker"
    }

    bot.request.reset_mock()
    bot.request.side_effect = None
    bot.request.return_value = emoji_payload()
    await bot.commit_emoji(
        GUILD,
        EntityRef(98, "chat.example"),
        "wave",
        target=TARGET,
        roles=[EntityRef(40, "chat.example")],
        reason="new emoji",
    )
    assert last_await(bot.request).kwargs["json"]["role_ids"] == ["40@chat.example"]
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "new emoji"
    }

    with pytest.raises(ValueError, match="262144"):
        await bot.upload_emoji(
            GUILD,
            b"x" * (256 * 1024 + 1),
            filename="too-large.png",
            content_type="image/png",
            target=TARGET,
        )
    with pytest.raises(ValueError, match="PNG, APNG, or GIF"):
        await bot.upload_sticker(
            GUILD,
            b"image",
            filename="sticker.webp",
            content_type="image/webp",
            target=TARGET,
        )


def application_asset_payload(
    *, kind: str = "icon", name: str = "primary"
) -> dict[str, object]:
    return {
        "id": "80",
        "ref": "80@apps.example",
        "application_ref": "1@apps.example",
        "kind": kind,
        "name": name,
        "media_hash": "c" * 64,
        "content_type": "image/png",
        "width": 512,
        "height": 512,
        "version": 1,
        "created_at": "2026-08-27T00:00:00+00:00",
        "updated_at": "2026-08-27T00:00:00+00:00",
    }


def application_emoji_payload(*, available: bool = True) -> dict[str, object]:
    return {
        "id": "81",
        "ref": "81@apps.example",
        "application_ref": "1@apps.example",
        "name": "party",
        "media_hash": "d" * 64,
        "animated": True,
        "available": available,
        "creator_id": "2",
        "creator_domain": "apps.example",
        "version": 3,
        "created_at": "2026-08-27T00:00:00+00:00",
        "updated_at": "2026-08-27T00:00:00+00:00",
    }


@pytest.mark.parametrize(
    "override",
    [
        {"ref": "82@apps.example"},
        {"kind": 1},
        {"kind": "banner"},
        {"name": None},
        {"name": " "},
        {"media_hash": True},
        {"media_hash": "C" * 64},
        {"content_type": "audio/ogg"},
        {"width": True},
        {"width": None},
        {"version": True},
        {"version": "1"},
        {"version": 0},
        {"created_at": datetime(2026, 8, 27, tzinfo=UTC)},
        {"created_at": "2026-08-27T00:00:00"},
        {"updated_at": "2026-08-26T23:59:59+00:00"},
    ],
)
def test_application_asset_parser_rejects_ambiguous_wire_values(
    override: dict[str, object],
) -> None:
    payload = application_asset_payload()
    payload.update(override)

    with pytest.raises(ValueError):
        ApplicationAsset.from_payload(client(), APPLICATION_HOME, payload)


def test_application_asset_parser_rejects_partial_dimensions() -> None:
    payload = application_asset_payload()
    payload.pop("height")

    with pytest.raises(ValueError, match="dimensions are incomplete"):
        ApplicationAsset.from_payload(client(), APPLICATION_HOME, payload)


@pytest.mark.parametrize(
    "override",
    [
        {"ref": "82@apps.example"},
        {"name": None},
        {"name": "invalid-name"},
        {"media_hash": False},
        {"media_hash": "D" * 64},
        {"animated": 1},
        {"available": "true"},
        {"version": True},
        {"version": "3"},
        {"version": 0},
        {"created_at": datetime(2026, 8, 27, tzinfo=UTC)},
        {"updated_at": "2026-08-26T23:59:59+00:00"},
    ],
)
def test_application_emoji_parser_rejects_ambiguous_wire_values(
    override: dict[str, object],
) -> None:
    payload = application_emoji_payload()
    payload.update(override)

    with pytest.raises(ValueError):
        ApplicationEmoji.from_payload(client(), APPLICATION_HOME, payload)


def test_application_parsers_preserve_explicit_legacy_and_remote_refs() -> None:
    legacy_asset = application_asset_payload()
    for key in ("ref", "width", "height", "version"):
        legacy_asset.pop(key)
    asset = ApplicationAsset.from_payload(client(), APPLICATION_HOME, legacy_asset)
    assert asset.width is None
    assert asset.height is None
    assert asset.version == 1

    legacy_emoji = application_emoji_payload()
    for key in ("ref", "animated", "available", "version"):
        legacy_emoji.pop(key)
    emoji = ApplicationEmoji.from_payload(client(), APPLICATION_HOME, legacy_emoji)
    assert not emoji.animated
    assert emoji.available
    assert emoji.version == 1

    remote_emoji = application_emoji_payload()
    remote_emoji.update(
        {
            "ref": "81@remote-apps.example",
            "application_ref": "1@remote-apps.example",
            "creator_id": "2",
            "creator_domain": "users.example",
        }
    )
    remote = ApplicationEmoji.from_payload(
        client(), "https://remote-apps.example", remote_emoji
    )
    assert remote.ref == EntityRef(81, "remote-apps.example")
    assert remote.creator_ref == EntityRef(2, "users.example")


@pytest.mark.asyncio
async def test_application_assets_and_emojis_cover_bot_crud() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            [application_asset_payload()],
            application_asset_payload(),
            application_asset_payload(kind="other", name="secondary"),
            application_asset_payload(),
            [application_emoji_payload()],
            application_emoji_payload(),
            application_emoji_payload(available=False),
            None,
            None,
        ]
    )

    assets = await bot.application_assets(target=APPLICATION_HOME)
    fetched_asset = await bot.fetch_application_asset(80, target=APPLICATION_HOME)
    updated_asset = await fetched_asset.edit(kind="other", name=" secondary ")
    created_asset = await bot.commit_application_asset(
        EntityRef(90, "apps.example"), "icon", "primary", target=APPLICATION_HOME
    )
    emojis = await bot.application_emojis(target=APPLICATION_HOME)
    fetched = await bot.fetch_application_emoji(81, target=APPLICATION_HOME)
    updated = await fetched.edit(name="party_time")
    await assets[0].delete()
    await updated.delete()

    assert isinstance(assets[0], ApplicationAsset)
    assert updated_asset.kind == "other"
    assert updated_asset.name == "secondary"
    assert isinstance(created_asset, ApplicationAsset)
    assert isinstance(emojis[0], ApplicationEmoji)
    assert fetched.token == "<a:party:81@apps.example>"
    assert updated.name == "party"
    assert bot.request.await_args_list[2].args[:2] == (
        "PATCH",
        "/api/v1/bots/applications/@me/assets/80",
    )
    assert bot.request.await_args_list[2].kwargs["json"] == {
        "kind": "other",
        "name": "secondary",
    }
    assert bot.request.await_args_list[6].kwargs["json"] == {"name": "party_time"}
    assert bot.request.await_args_list[-2].args[:2] == (
        "DELETE",
        "/api/v1/bots/applications/@me/assets/80",
    )
    assert bot.request.await_args_list[-1].args[:2] == (
        "DELETE",
        "/api/v1/bots/applications/@me/emojis/81",
    )


@pytest.mark.asyncio
async def test_application_upload_ticket_is_exposed_without_forcing_upload() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "id": "90",
            "origin_domain": "apps.example",
            "filename": "icon.png",
            "content_type": "image/png",
            "size": 123,
            "scan_status": "pending",
            "purpose": "application_asset",
            "upload_url": "https://objects.example/upload",
        }
    )

    ticket = await bot.create_application_asset_ticket(
        filename="icon.png", content_type="image/png", size=123, target=APPLICATION_HOME
    )

    assert ticket.ref == EntityRef(90, "apps.example")
    assert ticket.purpose == "application_asset"
    assert last_await(bot.request).args[:2] == (
        "POST",
        "/api/v1/bots/applications/@me/assets/tickets",
    )


@pytest.mark.asyncio
async def test_application_emoji_ticket_and_commit_are_typed() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "id": "91",
                "origin_domain": "apps.example",
                "filename": "party.gif",
                "content_type": "image/gif",
                "size": 456,
                "scan_status": "pending",
                "purpose": "application_emoji",
                "upload_url": "https://objects.example/upload",
            },
            application_emoji_payload(),
        ]
    )

    ticket = await bot.create_application_emoji_ticket(
        filename="party.gif",
        content_type="image/gif",
        size=456,
        target=APPLICATION_HOME,
    )
    emoji = await bot.commit_application_emoji(
        ticket.ref, "party", target=APPLICATION_HOME
    )

    assert ticket.purpose == "application_emoji"
    assert isinstance(emoji, ApplicationEmoji)
    assert last_await(bot.request).args[:2] == (
        "POST",
        "/api/v1/bots/applications/@me/emojis",
    )
    assert last_await(bot.request).kwargs["json"] == {
        "attachment_id": "91",
        "name": "party",
    }


@pytest.mark.asyncio
async def test_scheduled_event_invite_target_round_trips() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "code": "abcdefgh",
            "guild": {
                "id": "10",
                "origin_domain": "chat.example",
                "name": "Guild",
            },
            "channel_id": "20",
            "uses": 0,
            "max_uses": 5,
            "temporary": True,
            "reusable": False,
            "target_type": None,
            "target_user_id": None,
            "scheduled_event_id": "30@chat.example",
            "role_ids": ["91@chat.example"],
            "target_user_count": 1,
            "expires_at": None,
            "created_at": "2026-08-27T00:00:00+00:00",
            "revoked_at": None,
        }
    )

    invite = await bot.create_invite(
        GUILD,
        target=TARGET,
        reason="community launch",
        channel_id=20,
        max_uses=5,
        max_age_seconds=None,
        temporary=True,
        unique=True,
        scheduled_event_id=EntityRef(30, "chat.example"),
        role_ids=[EntityRef(91, "chat.example")],
        target_user_ids=[EntityRef(31, "people.example")],
    )

    assert isinstance(invite, Invite)
    assert invite.temporary and invite.unique
    assert invite.scheduled_event_ref == EntityRef(30, "chat.example")
    assert invite.role_refs == (EntityRef(91, "chat.example"),)
    assert invite.target_user_count == 1
    assert last_await(bot.request).kwargs["json"] == {
        "channel_id": "20",
        "max_uses": 5,
        "max_age_seconds": None,
        "temporary": True,
        "unique": True,
        "target_type": None,
        "target_user_id": None,
        "scheduled_event_id": "30@chat.example",
        "role_ids": ["91@chat.example"],
        "target_user_ids": ["31@people.example"],
    }
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "community launch"
    }


@pytest.mark.asyncio
async def test_bot_targeted_invite_lifecycle_is_typed_and_authority_routed() -> None:
    bot = client()
    completed = {
        "status": 2,
        "total_users": 2,
        "processed_users": 2,
        "created_at": "2026-08-28T00:00:00+00:00",
        "completed_at": "2026-08-28T00:00:01+00:00",
        "error_message": None,
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            {
                "target_user_ids": [
                    "31@people.example",
                    "32@people.example",
                ]
            },
            completed,
            completed,
        ]
    )

    targets = await bot.fetch_invite_target_users(
        GUILD,
        "abcdefgh@chat.example",
        target=TARGET,
    )
    assert isinstance(targets, InviteTargetUsers)
    assert targets.users == (
        EntityRef(31, "people.example"),
        EntityRef(32, "people.example"),
    )
    assert bot.request.await_args_list[0].args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/invites/abcdefgh@chat.example/target-users",
    )
    assert bot.request.await_args_list[0].kwargs["target"] == TARGET

    job = await bot.update_invite_target_users(
        GUILD,
        "abcdefgh@chat.example",
        targets.users,
        target=TARGET,
    )
    assert isinstance(job, InviteTargetUsersJobStatus)
    assert job.status == 2 and job.processed_users == 2
    assert bot.request.await_args_list[1].kwargs["json"] == {
        "target_user_ids": ["31@people.example", "32@people.example"]
    }

    status = await bot.fetch_invite_target_users_job_status(
        GUILD,
        "abcdefgh@chat.example",
        target=TARGET,
    )
    assert status == job
    assert (
        bot.request.await_args_list[2]
        .args[1]
        .endswith("/invites/abcdefgh@chat.example/target-users/job-status")
    )


@pytest.mark.asyncio
async def test_channel_invites_use_the_discord_channel_collection() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=[])  # type: ignore[method-assign]

    assert (
        await bot.channel_invites(
            GUILD,
            EntityRef(20, "chat.example"),
            target=TARGET,
        )
        == []
    )
    assert last_await(bot.request).args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/channels/20@chat.example/invites",
    )


@pytest.mark.asyncio
async def test_fetch_invite_is_typed_and_binds_exact_code_authority() -> None:
    bot = client()
    payload = {
        "code": "abcdefgh",
        "guild": {"id": "10", "origin_domain": "chat.example"},
        "channel_id": "20",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    invite = await bot.fetch_invite(
        GUILD,
        "abcdefgh@chat.example",
        target=TARGET,
    )

    assert isinstance(invite, Invite)
    assert invite.code == "abcdefgh"
    assert last_await(bot.request).args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/invites/abcdefgh@chat.example",
    )
    assert last_await(bot.request).kwargs["target"] == TARGET

    bot.request.return_value = payload | {"code": "ijklmnop"}
    with pytest.raises(ValueError, match="requested code"):
        await bot.fetch_invite(GUILD, "abcdefgh", target=TARGET)
    with pytest.raises(ValueError, match="authority"):
        await bot.fetch_invite(GUILD, "abcdefgh@other.example", target=TARGET)


@pytest.mark.asyncio
async def test_revoke_invite_returns_deleted_invite_and_sends_audit_reason() -> None:
    bot = client()
    deleted_payload = {
        "code": "abcdefgh",
        "guild": {"id": "10", "origin_domain": "chat.example"},
        "channel_id": "20",
        "created_at": "2026-08-28T00:00:00+00:00",
        "revoked_at": "2026-08-29T00:00:00+00:00",
    }
    bot.request = AsyncMock(return_value=deleted_payload)  # type: ignore[method-assign]

    deleted = await bot.revoke_invite(
        GUILD,
        "abcdefgh@chat.example",
        target=TARGET,
        reason=" remove stale invite ",
    )

    assert isinstance(deleted, Invite)
    assert deleted.code == "abcdefgh"
    assert deleted.revoked_at == datetime(2026, 8, 29, tzinfo=UTC)
    assert last_await(bot.request).args[:2] == (
        "DELETE",
        "/api/v1/bots/guilds/10@chat.example/invites/abcdefgh@chat.example",
    )
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "remove stale invite"
    }

    bot.request.reset_mock()
    revoked_again = await deleted.revoke(reason="repeat cleanup")
    assert isinstance(revoked_again, Invite)
    assert last_await(bot.request).kwargs["headers"] == {
        "X-Audit-Log-Reason": "repeat cleanup"
    }


@pytest.mark.asyncio
async def test_invite_responses_are_bound_to_the_requested_guild_and_channel() -> None:
    bot = client()
    payload = {
        "code": "abcdefgh",
        "guild": {"id": "11", "origin_domain": "chat.example"},
        "channel_id": "20",
        "created_at": "2026-08-28T00:00:00+00:00",
    }
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="requested resource"):
        await bot.revoke_invite(GUILD, "abcdefgh", target=TARGET)

    payload["guild"] = {"id": "10", "origin_domain": "chat.example"}
    payload["channel_id"] = "21"
    bot.request.return_value = [payload]
    with pytest.raises(ValueError, match="requested resource"):
        await bot.channel_invites(
            GUILD,
            EntityRef(20, "chat.example"),
            target=TARGET,
        )


def test_targeted_invite_sdk_rejects_ambiguous_or_cross_authority_data() -> None:
    with pytest.raises(ValueError, match="targeting projection"):
        Invite.from_payload(
            client(),
            TARGET,
            {
                "code": "abcdefgh",
                "guild": {"id": "10", "origin_domain": "chat.example"},
                "created_at": "2026-08-28T00:00:00+00:00",
                "role_ids": ["91@forged.example"],
                "target_user_count": 1,
            },
        )

    scheduled_event = {
        "id": "31",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "creator_id": "32",
        "creator_domain": "people.example",
        "name": "Town Hall",
        "scheduled_start_time": "2026-08-30T00:00:00+00:00",
        "privacy_level": 2,
        "status": 1,
        "entity_type": 3,
    }
    with pytest.raises(ValueError, match="targeting projection"):
        Invite.from_payload(
            client(),
            TARGET,
            {
                "code": "abcdefgh",
                "guild": {"id": "10", "origin_domain": "chat.example"},
                "created_at": "2026-08-28T00:00:00+00:00",
                "scheduled_event_id": "30@chat.example",
                "guild_scheduled_event": scheduled_event,
            },
        )

    with pytest.raises(ValueError, match="job response"):
        InviteTargetUsersJobStatus.from_payload(
            {
                "status": True,
                "total_users": 1,
                "processed_users": 1,
                "created_at": "2026-08-28T00:00:00+00:00",
                "completed_at": None,
                "error_message": None,
            }
        )


@pytest.mark.asyncio
async def test_invite_sdk_enforces_discord_max_use_limit() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        await client().create_invite(GUILD, target=TARGET, max_uses=101)


def webhook_payload(
    *, channel_id: str = "20", avatar_hash: str | None = None
) -> dict[str, object]:
    return {
        "id": "70",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "channel_id": channel_id,
        "channel_domain": "chat.example",
        "name": "Release relay",
        "avatar_hash": avatar_hash,
        "revoked": False,
    }


@pytest.mark.asyncio
async def test_webhook_get_and_channel_list_are_typed_sdk_operations() -> None:
    bot = client()
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[webhook_payload(), [webhook_payload()]]
    )

    fetched = await bot.fetch_webhook(GUILD, 70, target=TARGET)
    listed = await bot.channel_webhooks(
        GUILD,
        EntityRef(20, "chat.example"),
        target=TARGET,
    )

    assert isinstance(fetched, Webhook)
    assert fetched.ref == EntityRef(70, "chat.example")
    assert [item.ref for item in listed] == [EntityRef(70, "chat.example")]
    assert bot.request.await_args_list[0].args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/webhooks/70",
    )
    assert bot.request.await_args_list[1].args[:2] == (
        "GET",
        "/api/v1/bots/guilds/10@chat.example/channels/20@chat.example/webhooks",
    )


@pytest.mark.asyncio
async def test_webhook_move_and_scanned_avatar_have_full_sdk_helpers() -> None:
    bot = client()
    ticket = {
        "id": "90",
        "origin_domain": "chat.example",
        "filename": "relay.png",
        "content_type": "image/png",
        "size": 4,
        "scan_status": "pending",
        "purpose": "webhook_avatar",
        "upload_url": "https://objects.example/upload",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            webhook_payload(channel_id="21"),
            ticket,
            webhook_payload(channel_id="21", avatar_hash="a" * 64),
            webhook_payload(channel_id="21"),
        ]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    moved = await bot.edit_webhook(
        GUILD,
        70,
        target=TARGET,
        channel=EntityRef(21, "chat.example"),
        reason="move release feed",
    )
    updated = await bot.upload_webhook_avatar(
        GUILD,
        70,
        b"image",
        filename="relay.png",
        content_type="image/png",
        target=TARGET,
        reason="refresh branding",
    )
    cleared = await updated.delete_avatar(reason="retire branding")

    assert isinstance(moved, Webhook)
    assert moved.channel_ref == EntityRef(21, "chat.example")
    assert updated.avatar_hash == "a" * 64
    assert cleared.avatar_hash is None
    assert bot.request.await_args_list[0].kwargs["json"] == {
        "channel_id": "21@chat.example"
    }
    assert bot.request.await_args_list[0].kwargs["headers"] == {
        "X-Audit-Log-Reason": "move release feed"
    }
    assert bot.request.await_args_list[1].args[:2] == (
        "POST",
        "/api/v1/bots/guilds/10@chat.example/webhooks/70/avatar/tickets",
    )
    assert bot.request.await_args_list[2].args[:2] == (
        "PUT",
        "/api/v1/bots/guilds/10@chat.example/webhooks/70/avatar",
    )
    assert bot.request.await_args_list[3].args[:2] == (
        "DELETE",
        "/api/v1/bots/guilds/10@chat.example/webhooks/70/avatar",
    )


@pytest.mark.asyncio
async def test_token_webhook_object_crud_and_avatar_helpers_preserve_token() -> None:
    bot = client()
    ticket = {
        "id": "91",
        "origin_domain": "chat.example",
        "filename": "token-relay.png",
        "content_type": "image/png",
        "size": 5,
        "scan_status": "pending",
        "purpose": "webhook_avatar",
        "upload_url": "https://objects.example/upload-token",
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            webhook_payload(),
            {**webhook_payload(), "name": "Renamed relay"},
            ticket,
            webhook_payload(avatar_hash="b" * 64),
            webhook_payload(),
            None,
        ]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    fetched = await bot.fetch_webhook_with_token(70, "kwh_secret", target=TARGET)
    renamed = await fetched.edit_with_token(name="Renamed relay")
    updated = await renamed.set_avatar_with_token(
        b"image",
        filename="token-relay.png",
        content_type="image/png",
    )
    cleared = await updated.delete_avatar_with_token()
    await cleared.delete_with_token()

    assert fetched.token == "kwh_secret"
    assert renamed.token == "kwh_secret"
    assert updated.token == "kwh_secret"
    assert cleared.token == "kwh_secret"
    assert bot.request.await_args_list[0].args[:2] == (
        "GET",
        "/api/v1/webhooks/70/kwh_secret",
    )
    assert bot.request.await_args_list[1].kwargs["json"] == {"name": "Renamed relay"}
    assert bot.request.await_args_list[2].args[:2] == (
        "POST",
        "/api/v1/webhooks/70/kwh_secret/avatar/tickets",
    )
    assert bot.request.await_args_list[-1].args[:2] == (
        "DELETE",
        "/api/v1/webhooks/70/kwh_secret",
    )


@pytest.mark.asyncio
async def test_guild_asset_upload_commit_and_clear_use_authority_routes() -> None:
    bot = client()
    ticket = {
        "id": "90",
        "origin_domain": "chat.example",
        "filename": "guild.png",
        "content_type": "image/png",
        "size": 5,
        "scan_status": "pending",
        "purpose": "guild_icon",
        "upload_url": "https://objects.example/upload",
    }
    guild = {
        "id": "10",
        "origin_domain": "chat.example",
        "name": "Lantern",
        "owner_id": "2",
        "owner_domain": "chat.example",
        "icon_hash": None,
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ticket, {**ticket, "scan_status": "clean"}, guild]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    uploaded = await bot.upload_guild_asset(
        GUILD,
        "icon",
        b"image",
        filename="guild.png",
        content_type="image/png",
        target="https://edge.example",
    )
    committed = await bot.commit_guild_asset(
        GUILD,
        "icon",
        uploaded.ref,
        target="https://edge.example",
    )
    cleared = await bot.delete_guild_asset(
        GUILD,
        "icon",
        target="https://edge.example",
    )

    assert isinstance(uploaded, Attachment)
    assert isinstance(committed, Attachment)
    assert isinstance(cleared, Guild)
    assert all(call.kwargs["target"] == TARGET for call in bot.request.await_args_list)
    assert [call.args[:2] for call in bot.request.await_args_list] == [
        ("POST", "/api/v1/bots/guilds/10@chat.example/assets/icon"),
        ("PUT", "/api/v1/bots/guilds/10@chat.example/assets/icon"),
        ("DELETE", "/api/v1/bots/guilds/10@chat.example/assets/icon"),
    ]


@pytest.mark.asyncio
async def test_role_icon_upload_commit_and_clear_are_typed() -> None:
    bot = client()
    role_ref = EntityRef(20, "chat.example")
    ticket = {
        "id": "91",
        "origin_domain": "chat.example",
        "filename": "role.png",
        "content_type": "image/png",
        "size": 5,
        "scan_status": "pending",
        "purpose": "role_icon",
        "upload_url": "https://objects.example/upload",
    }
    role = {
        "id": "20",
        "origin_domain": "chat.example",
        "guild_id": "10",
        "guild_domain": "chat.example",
        "name": "Helpers",
        "permissions": "0",
        "position": 1,
        "icon_hash": "a" * 64,
    }
    bot.request = AsyncMock(  # type: ignore[method-assign]
        side_effect=[ticket, role, {**role, "icon_hash": None}]
    )
    bot._put_upload_ticket = AsyncMock()  # type: ignore[method-assign]

    uploaded = await bot.upload_role_icon(
        GUILD,
        role_ref,
        b"image",
        filename="role.png",
        content_type="image/png",
        target=TARGET,
    )
    committed = await bot.commit_role_icon(
        GUILD,
        role_ref,
        uploaded.ref,
        target=TARGET,
    )
    assert isinstance(committed, Role)
    cleared = await committed.delete_icon()

    assert isinstance(cleared, Role)
    assert cleared.icon_hash is None
    assert [call.args[:2] for call in bot.request.await_args_list] == [
        ("POST", "/api/v1/bots/guilds/10@chat.example/roles/20@chat.example/icon"),
        ("PUT", "/api/v1/bots/guilds/10@chat.example/roles/20@chat.example/icon"),
        ("DELETE", "/api/v1/bots/guilds/10@chat.example/roles/20@chat.example/icon"),
    ]


@pytest.mark.asyncio
async def test_webhook_execute_serializes_discord_thread_and_mention_contract() -> None:
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]

    result = await bot.execute_webhook(
        70,
        "kwh_secret",
        "deployed",
        target=TARGET,
        wait=False,
        thread_name="Release notes",
        applied_tag_ids=[4, 5],
        tts=True,
        allowed_mentions={"parse": ["users"], "users": [], "roles": []},
        with_components=True,
    )

    assert result is None
    call = last_await(bot.request)
    assert call.args[:2] == ("POST", "/api/v1/webhooks/70/kwh_secret")
    assert call.kwargs["params"] == {"wait": False, "with_components": True}
    assert call.kwargs["json"]["thread_name"] == "Release notes"
    assert call.kwargs["json"]["applied_tags"] == ["4", "5"]
    assert call.kwargs["json"]["tts"] is True
    assert call.kwargs["json"]["allowed_mentions"]["parse"] == ["users"]


@pytest.mark.asyncio
async def test_slack_and_github_webhook_compatibility_routes_are_exposed() -> None:
    bot = client()
    bot.request = AsyncMock(side_effect=[None, None])  # type: ignore[method-assign]

    slack = await bot.execute_slack_webhook(
        70,
        "kwh_secret",
        {"text": "deployed"},
        target=TARGET,
        wait=False,
        thread_id=EntityRef(22, "chat.example"),
    )
    github = await bot.execute_github_webhook(
        70,
        "kwh_secret",
        "push",
        {"ref": "refs/heads/main"},
        target=TARGET,
        wait=False,
        delivery_id="delivery-1",
    )

    assert slack is None and github is None
    slack_call, github_call = bot.request.await_args_list
    assert slack_call.args[1].endswith("/slack")
    assert slack_call.kwargs["params"]["thread_id"] == "22@chat.example"
    assert github_call.args[1].endswith("/github")
    assert github_call.kwargs["headers"] == {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "delivery-1",
    }


@pytest.mark.asyncio
async def test_webhook_edit_serializes_nullable_fields_flags_and_component_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    bot.request = AsyncMock(return_value={"id": "90"})  # type: ignore[method-assign]

    class Parsed:
        ref = EntityRef(90, "chat.example")
        channel_ref = EntityRef(22, "chat.example")
        webhook_ref = EntityRef(70, "chat.example")

        def bind_webhook_lifecycle(
            self,
            webhook_id: int,
            token: str,
            *,
            thread_id: EntityRef | None,
            e2ee_device_id: str | None,
        ) -> Parsed:
            assert (webhook_id, token, thread_id, e2ee_device_id) == (
                70,
                "kwh_secret",
                EntityRef(22, "chat.example"),
                None,
            )
            return self

    parsed = Parsed()
    monkeypatch.setattr(
        "kaede_bot.client.Message.from_payload",
        lambda *_args, **_kwargs: parsed,
    )

    result = await bot.edit_webhook_message(
        70,
        "kwh_secret",
        EntityRef(90, "chat.example"),
        target=TARGET,
        content=None,
        embeds=None,
        view=None,
        attachment_ids=None,
        flags=4,
        allowed_mentions=None,
        thread_id=EntityRef(22, "chat.example"),
    )

    assert result is parsed
    call = last_await(bot.request)
    assert call.kwargs["params"] == {
        "with_components": True,
        "thread_id": "22@chat.example",
    }
    assert call.kwargs["json"] == {
        "content": None,
        "embeds": None,
        "components": None,
        "attachment_ids": None,
        "flags": 4,
        "allowed_mentions": None,
    }
