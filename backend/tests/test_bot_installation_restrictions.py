from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException

import app.api.applications as applications_api
import app.api.bots as bots_api
from app.api.applications import BotChannelRestrictionsUpdate
from app.chat.schemas import GuildUpdate
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, BotInstallation
from app.db.models import Guild, User


def installation(*, restrictions: list[str], revision: int = 4) -> BotInstallation:
    row = BotInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="guild.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="guild.example",
        granted_scopes=["guilds.read"],
        granted_intents=["guilds"],
        granted_permissions=0,
        channel_restrictions=restrictions,
        e2ee_mode="disabled",
        grant_revision=revision,
        status="active",
    )
    row.created_at = datetime.now(UTC)
    row.installed_at = datetime.now(UTC)
    return row


def application() -> BotApplication:
    return BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=30,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="active",
    )


def bot_user() -> User:
    return User(
        id=10,
        origin_domain="apps.example",
        is_local=False,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
    )


@pytest.mark.asyncio
async def test_channel_restrictions_are_canonical_target_owned_refs() -> None:
    guild = Guild(
        id=70,
        origin_domain="guild.example",
        name="Guild",
        owner_id=80,
        owner_domain="guild.example",
    )
    session = SimpleNamespace(scalars=AsyncMock(return_value=[9, 7]))

    assert await applications_api._canonical_installation_channel_restrictions(
        cast(Any, session),
        guild,
        [EntityRef("9"), EntityRef("7@guild.example")],
    ) == ["7@guild.example", "9@guild.example"]

    for requested, code in (
        ([EntityRef("7@other.example")], "CHANNEL_RESTRICTION_WRONG_AUTHORITY"),
        (
            [EntityRef("7"), EntityRef("7@guild.example")],
            "CHANNEL_RESTRICTION_DUPLICATE",
        ),
    ):
        with pytest.raises(HTTPException) as denied:
            await applications_api._canonical_installation_channel_restrictions(
                cast(Any, session), guild, requested
            )
        assert denied.value.detail == {"code": code}

    session.scalars.return_value = [7]
    with pytest.raises(HTTPException) as missing:
        await applications_api._canonical_installation_channel_restrictions(
            cast(Any, session),
            guild,
            [EntityRef("7"), EntityRef("9")],
        )
    assert missing.value.detail == {"code": "CHANNEL_RESTRICTION_INVALID"}


def restriction_update_context(
    monkeypatch: pytest.MonkeyPatch,
    row: BotInstallation,
    normalized: list[str],
) -> tuple[SimpleNamespace, Mock, AsyncMock, Mock, AsyncMock]:
    query_result = Mock()
    query_result.one_or_none.return_value = (row, application(), bot_user())
    session = SimpleNamespace(
        execute=AsyncMock(return_value=query_result),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        applications_api,
        "get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr(
        applications_api,
        "_canonical_installation_channel_restrictions",
        AsyncMock(return_value=normalized),
    )
    revoke = AsyncMock(return_value=[])
    queue = Mock()
    snapshot = AsyncMock(return_value="apps.example")
    publish = AsyncMock()
    monkeypatch.setattr(applications_api, "revoke_bot_e2ee_access", revoke)
    monkeypatch.setattr(applications_api, "queue_installation_gateway_events", queue)
    monkeypatch.setattr(applications_api, "queue_application_target_snapshot", snapshot)
    monkeypatch.setattr(applications_api, "publish_e2ee_policy_updates", publish)
    monkeypatch.setattr(
        applications_api,
        "wake_application_target_deliveries",
        AsyncMock(),
    )
    return session, query_result, revoke, queue, publish


@pytest.mark.asyncio
async def test_local_restriction_noop_preserves_revision_and_runtime_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = installation(restrictions=["9", "7@guild.example"])
    normalized = ["7@guild.example", "9@guild.example"]
    session, query_result, revoke, queue, publish = restriction_update_context(
        monkeypatch, row, normalized
    )

    result = await applications_api._update_local_bot_channel_restrictions(
        Guild(id=70, origin_domain="guild.example", name="Guild"),
        EntityRef("20@apps.example"),
        [EntityRef(item) for item in normalized],
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result["channel_restrictions"] == normalized
    assert result["grant_revision"] == "4"
    assert row.grant_revision == 4
    revoke.assert_not_awaited()
    queue.assert_not_called()
    publish.assert_not_awaited()
    session.commit.assert_not_awaited()
    assert query_result.one_or_none.called
    query = str(session.execute.await_args.args[0])
    assert "bot_installations.revoked_at IS NULL" in query


@pytest.mark.asyncio
async def test_local_restriction_change_rotates_revision_and_runtime_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = installation(restrictions=["7@guild.example"])
    normalized = ["8@guild.example"]
    session, _query_result, revoke, queue, publish = restriction_update_context(
        monkeypatch, row, normalized
    )

    result = await applications_api._update_local_bot_channel_restrictions(
        Guild(id=70, origin_domain="guild.example", name="Guild"),
        EntityRef("20@apps.example"),
        [EntityRef("8")],
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert row.channel_restrictions == normalized
    assert row.grant_revision == 5
    assert result["channel_restrictions"] == normalized
    assert result["grant_revision"] == "5"
    revoke.assert_awaited_once()
    assert revoke.await_args.kwargs["installation_ids"] == (row.id,)
    queue.assert_called_once_with(cast(Any, session), row, "UPDATE")
    session.commit.assert_awaited_once()
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_restriction_patch_qualifies_and_binds_exact_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "id": "60",
                "status": "active",
                "application_ref": "20@apps.example",
                "guild_ref": "70@guild.example",
                "channel_restrictions": [
                    "7@guild.example",
                    "9@guild.example",
                ],
                "grant_revision": "5",
            },
        )
    )
    monkeypatch.setattr(applications_api, "signed_request", signed)

    result = await applications_api.update_bot_channel_restrictions(
        EntityRef("70@guild.example"),
        EntityRef("20@apps.example"),
        BotChannelRestrictionsUpdate(
            channel_restrictions=[EntityRef("9"), EntityRef("7@guild.example")]
        ),
        cast(Any, SimpleNamespace(user=SimpleNamespace(id=80))),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert result["channel_restrictions"] == [
        "7@guild.example",
        "9@guild.example",
    ]
    assert result["grant_revision"] == "5"
    assert signed.await_args.args[2:5] == (
        "PATCH",
        "guild.example",
        "/_kaede/v1/guilds/70/bot-install",
    )
    assert signed.await_args.kwargs["payload"] == {
        "installer_id": "80",
        "application_ref": "20@apps.example",
        "channel_restrictions": [
            "9@guild.example",
            "7@guild.example",
        ],
    }

    signed.return_value = httpx.Response(
        200,
        json={
            "id": "60",
            "status": "active",
            "application_ref": "20@apps.example",
            "guild_ref": "70@guild.example",
            "channel_restrictions": ["7@other.example", "9@guild.example"],
            "grant_revision": "6",
        },
    )
    with pytest.raises(HTTPException) as substituted:
        await applications_api.update_bot_channel_restrictions(
            EntityRef("70@guild.example"),
            EntityRef("20@apps.example"),
            BotChannelRestrictionsUpdate(
                channel_restrictions=[EntityRef("9"), EntityRef("7@guild.example")]
            ),
            cast(Any, SimpleNamespace(user=SimpleNamespace(id=80))),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
        )
    assert substituted.value.detail == {"code": "REMOTE_BOT_RESTRICTIONS_INVALID"}


@pytest.mark.asyncio
async def test_remote_install_rejects_cross_authority_restriction_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        applications_api,
        "signed_request",
        AsyncMock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "60",
                    "status": "active",
                    "application_ref": "20@apps.example",
                    "guild_ref": "70@guild.example",
                    "channel_restrictions": ["7@other.example"],
                    "grant_revision": "4",
                },
            )
        ),
    )

    with pytest.raises(HTTPException) as substituted:
        await applications_api.install_bot(
            EntityRef("70@guild.example"),
            EntityRef("20@apps.example"),
            "default",
            cast(Any, SimpleNamespace(user=SimpleNamespace(id=80))),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
        )

    assert substituted.value.detail == {"code": "REMOTE_BOT_INSTALL_INVALID"}


@pytest.mark.asyncio
async def test_installation_lists_and_bot_guild_projection_keep_restriction_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = installation(restrictions=["9", "7@guild.example"])
    app = application()
    monkeypatch.setattr(
        applications_api,
        "proxy_remote_application_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        applications_api,
        "managed_application",
        AsyncMock(return_value=(app, SimpleNamespace(), bot_user())),
    )
    session = SimpleNamespace(scalars=AsyncMock(return_value=[row]))

    listed = await applications_api.list_installations(
        EntityRef("20@apps.example"),
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
    )

    assert listed[0]["ref"] == "60@guild.example"
    assert listed[0]["guild_ref"] == "70@guild.example"
    assert listed[0]["channel_restrictions"] == [
        "7@guild.example",
        "9@guild.example",
    ]
    assert listed[0]["grant_revision"] == "4"

    guild_payload = bots_api._bot_guild_payload(
        Guild(id=70, origin_domain="guild.example", name="Guild"), row
    )
    assert guild_payload["installation_id"] == "60"
    assert guild_payload["channel_restrictions"] == [
        "7@guild.example",
        "9@guild.example",
    ]
    assert guild_payload["capability_revision"] == "4"


@pytest.mark.asyncio
async def test_bot_guild_mutations_keep_installation_restriction_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = installation(restrictions=["9", "7@guild.example"])
    guild = Guild(id=70, origin_domain="guild.example", name="Guild")
    install_for_guild = AsyncMock(return_value=(guild, row))
    monkeypatch.setattr(bots_api, "installation_for_guild", install_for_guild)
    monkeypatch.setattr(
        bots_api,
        "update_guild",
        AsyncMock(
            return_value={
                "id": "70",
                "origin_domain": "guild.example",
                "name": "Renamed",
            }
        ),
    )
    monkeypatch.setattr(
        bots_api,
        "delete_guild_asset",
        AsyncMock(
            return_value={
                "id": "70",
                "origin_domain": "guild.example",
                "name": "Renamed",
            }
        ),
    )
    principal = cast(Any, SimpleNamespace(user=bot_user()))
    dependencies = cast(Any, SimpleNamespace())
    settings = cast(Any, SimpleNamespace(domain="guild.example"))

    updated = await bots_api.bot_update_guild(
        EntityRef("70@guild.example"),
        GuildUpdate(name="Renamed"),
        principal,
        dependencies,
        dependencies,
        dependencies,
        settings,
        None,
        None,
    )
    cleared = await bots_api.bot_delete_guild_asset(
        EntityRef("70@guild.example"),
        "icon",
        principal,
        dependencies,
        dependencies,
        settings,
    )

    for rendered in (updated, cleared):
        assert rendered["installation_id"] == "60"
        assert rendered["channel_restrictions"] == [
            "7@guild.example",
            "9@guild.example",
        ]
        assert rendered["capability_revision"] == "4"
    assert [call.args[4] for call in install_for_guild.await_args_list] == [
        "guilds.manage",
        "guilds.assets.manage",
    ]
