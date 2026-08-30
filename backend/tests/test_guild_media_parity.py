from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from fastapi.routing import APIRoute

from app.api import bots as bot_api
from app.api import media as media_api
from app.api import voice as voice_api
from app.core.types import EntityRef
from app.db.models import Attachment, User
from app.federation.guild_management import GuildManagementResult
from app.media.schemas import AssetCommitRequest, UploadTicketRequest
from app.media.service import FEDERATED_GUILD_UPLOAD_PURPOSES
from app.tasks import federation_deliver, media_local_purge
from app.voice.schemas import VoiceModerationUpdate, VoiceMoveRequest


def test_all_guild_media_surfaces_have_human_and_bot_routes() -> None:
    routes = {
        (method, route.path)
        for route in (*media_api.router.routes, *bot_api.router.routes)
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
    }
    expected = {
        ("DELETE", "/api/v1/users/@me/assets/{kind}"),
        ("POST", "/api/v1/guilds/{guild_id}/assets/{kind}"),
        ("PUT", "/api/v1/guilds/{guild_id}/assets/{kind}"),
        ("DELETE", "/api/v1/guilds/{guild_id}/assets/{kind}"),
        ("POST", "/api/v1/guilds/{guild_id}/roles/{role_id}/icon"),
        ("PUT", "/api/v1/guilds/{guild_id}/roles/{role_id}/icon"),
        ("DELETE", "/api/v1/guilds/{guild_id}/roles/{role_id}/icon"),
        ("POST", "/api/v1/bots/guilds/{guild_ref}/assets/{kind}"),
        ("PUT", "/api/v1/bots/guilds/{guild_ref}/assets/{kind}"),
        ("DELETE", "/api/v1/bots/guilds/{guild_ref}/assets/{kind}"),
        ("POST", "/api/v1/bots/guilds/{guild_ref}/roles/{role_ref}/icon"),
        ("PUT", "/api/v1/bots/guilds/{guild_ref}/roles/{role_ref}/icon"),
        ("DELETE", "/api/v1/bots/guilds/{guild_ref}/roles/{role_ref}/icon"),
    }
    assert expected <= routes


def test_federated_guild_upload_accounting_includes_every_guild_asset_kind() -> None:
    assert {"guild_icon", "guild_banner", "role_icon"} <= set(FEDERATED_GUILD_UPLOAD_PURPOSES)


@pytest.mark.asyncio
async def test_remote_guild_asset_ticket_proxies_before_local_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxied = GuildManagementResult(
        request_id="kagm_" + "a" * 32,
        operation="guild_asset.ticket",
        guild={"id": "10", "domain": "home.example"},
        status_code=201,
        body={"id": "40", "upload_url": "https://media.home.example/upload"},
    )
    proxy = AsyncMock(return_value=(True, proxied))
    monkeypatch.setattr(media_api, "_proxy_guild_management", proxy)
    local = AsyncMock(side_effect=AssertionError("remote assets must not use local_guild"))
    monkeypatch.setattr(media_api, "local_guild", local)
    auth = SimpleNamespace(user=SimpleNamespace(id=8, origin_domain="remote.example"))
    payload = UploadTicketRequest(
        filename="guild.png",
        content_type="image/png",
        size=5,
    )

    rendered = await media_api.create_guild_asset_ticket(
        EntityRef("10@home.example"),
        "icon",
        payload,
        Response(),
        cast(Any, auth),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="remote.example")),
    )

    assert rendered["id"] == "40"
    proxy_call = proxy.await_args
    assert proxy_call is not None
    assert proxy_call.args[4:] == (
        "guild_asset.ticket",
        {"kind": "icon", "data": payload.model_dump(mode="json", exclude_unset=True)},
    )
    local.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_asset_clear_unbinds_and_fans_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=8,
        origin_domain="home.example",
        is_local=True,
        username="lantern",
        password_hash="unused",
        avatar_hash="a" * 64,
        profile_version=4,
        profile_resolved=True,
    )
    attachment = Attachment(
        id=40,
        origin_domain="home.example",
        uploader_id=user.id,
        uploader_domain=user.origin_domain,
        filename="avatar.png",
        content_type="image/png",
        size=5,
        object_key="home.example/40/clean/original",
        scan_status="clean",
        purpose="avatar",
        asset_binding="user:home.example:8:avatar",
    )

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def scalar(self, statement: object) -> User | Attachment:
            self.calls += 1
            return user if self.calls == 1 else attachment

        async def commit(self) -> None:
            return None

    queue_updates = AsyncMock(return_value={"peer.example"})
    publish = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(media_api, "queue_profile_updates", queue_updates)
    monkeypatch.setattr(media_api, "publish_dispatch", publish)
    monkeypatch.setattr(media_api, "enqueue_best_effort", enqueue)

    rendered = await media_api.delete_user_asset(
        "avatar",
        cast(Any, SimpleNamespace(user=user)),
        cast(Any, Session()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert user.avatar_hash is None
    assert user.profile_version == 5
    assert attachment.asset_binding is None
    assert rendered["profile_version"] == "5"
    queue_updates.assert_awaited_once()
    publish_call = publish.await_args
    assert publish_call is not None
    assert publish_call.args[2] == "USER_UPDATE"
    assert [call.args[0] for call in enqueue.await_args_list] == [
        federation_deliver,
        media_local_purge,
    ]


@pytest.mark.asyncio
async def test_bot_guild_asset_ticket_is_charged_to_exact_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(id=7, origin_domain="apps.example")
    principal = cast(Any, SimpleNamespace(user=user))
    guild = SimpleNamespace(id=10, origin_domain="home.example")
    installation = SimpleNamespace(id=30)
    resolve = AsyncMock(return_value=(guild, installation))
    require_scope = AsyncMock()
    issue = AsyncMock(return_value={"id": "40"})
    monkeypatch.setattr(bot_api, "installation_for_guild", resolve)
    monkeypatch.setattr(bot_api, "require_permissions", require_scope)
    monkeypatch.setattr(bot_api, "require_installation_scope", lambda *args: None)
    monkeypatch.setattr(bot_api, "issue_image_asset_ticket", issue)
    payload = UploadTicketRequest(
        filename="guild.png",
        content_type="image/png",
        size=5,
    )

    rendered = await bot_api.bot_create_guild_asset_ticket(
        EntityRef("10@home.example"),
        "banner",
        payload,
        Response(),
        principal,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
    )

    assert rendered == {"id": "40"}
    assert resolve.await_args is not None
    assert resolve.await_args.args[-1] == "guilds.assets.manage"
    assert issue.await_args is not None
    assert issue.await_args.kwargs == {
        "purpose": "guild_banner",
        "bot_installation": installation,
    }


@pytest.mark.asyncio
async def test_bot_role_icon_commit_requires_installation_owned_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = cast(
        Any,
        SimpleNamespace(user=SimpleNamespace(id=7, origin_domain="apps.example")),
    )
    installation = SimpleNamespace(id=30)
    resolve = AsyncMock(return_value=(SimpleNamespace(), installation))
    owned = AsyncMock()
    delegate = AsyncMock(return_value={"id": "20", "icon_hash": "a" * 64})
    monkeypatch.setattr(bot_api, "installation_for_guild", resolve)
    monkeypatch.setattr(bot_api, "require_owned_attachments_for_installation", owned)
    monkeypatch.setattr(bot_api, "commit_role_icon", delegate)

    rendered = await bot_api.bot_commit_role_icon(
        EntityRef("10@home.example"),
        EntityRef("20@home.example"),
        AssetCommitRequest(attachment_id="40"),
        Response(),
        principal,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
    )

    assert rendered["id"] == "20"
    assert resolve.await_args is not None
    assert resolve.await_args.args[-1] == "roles.manage"
    assert owned.await_args is not None
    assert owned.await_args.args[-1] == [40]
    delegate.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_message_edit_rejects_attachment_from_another_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = cast(
        Any,
        SimpleNamespace(user=SimpleNamespace(id=7, origin_domain="apps.example")),
    )
    installation = SimpleNamespace(id=30)
    session = cast(Any, SimpleNamespace())
    settings = cast(Any, SimpleNamespace(domain="home.example"))
    resolve = AsyncMock(return_value=(SimpleNamespace(), installation))
    owned = AsyncMock(
        side_effect=HTTPException(status_code=404, detail={"code": "ATTACHMENT_NOT_FOUND"})
    )
    delegate = AsyncMock()
    monkeypatch.setattr(bot_api, "installation_for_channel", resolve)
    monkeypatch.setattr(bot_api, "require_owned_attachments_for_installation", owned)
    monkeypatch.setattr(bot_api, "edit_message", delegate)
    payload = SimpleNamespace(attachment_ids=["40"])

    with pytest.raises(HTTPException) as denied:
        await bot_api.bot_edit_message(
            EntityRef("10@home.example"),
            EntityRef("20@home.example"),
            cast(Any, payload),
            principal,
            session,
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            settings,
            None,
        )

    assert denied.value.detail == {"code": "ATTACHMENT_NOT_FOUND"}
    owned.assert_awaited_once_with(session, settings, principal, installation, [40])
    delegate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    ("voice_member.update", "voice_member.disconnect", "voice_member.move"),
)
async def test_remote_human_voice_management_routes_to_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    proxy = AsyncMock(return_value=SimpleNamespace(status_code=204, body=None))
    monkeypatch.setattr(voice_api, "proxy_remote_guild_management", proxy)
    auth = cast(
        Any,
        SimpleNamespace(user=SimpleNamespace(id=8, origin_domain="remote.example")),
    )
    settings = cast(Any, SimpleNamespace(domain="remote.example", voice_enabled=True))
    guild_ref = EntityRef("10@home.example")
    user_ref = EntityRef("9")
    session = cast(Any, SimpleNamespace())
    redis = cast(Any, SimpleNamespace())
    snowflake = cast(Any, SimpleNamespace())
    if operation == "voice_member.update":
        response = await voice_api.update_member_voice_moderation(
            guild_ref,
            user_ref,
            VoiceModerationUpdate(server_mute=True),
            auth,
            session,
            redis,
            snowflake,
            settings,
            "reason",
        )
    elif operation == "voice_member.disconnect":
        response = await voice_api.disconnect_member_voice(
            guild_ref,
            user_ref,
            auth,
            session,
            redis,
            snowflake,
            settings,
            "reason",
        )
    else:
        response = await voice_api.move_member_voice(
            guild_ref,
            user_ref,
            VoiceMoveRequest(channel_id=EntityRef("20")),
            auth,
            session,
            redis,
            snowflake,
            settings,
            "reason",
        )

    assert response.status_code == 204
    proxy_call = proxy.await_args
    assert proxy_call is not None
    assert proxy_call.args[4] == operation
    assert proxy_call.args[5]["resource_ref"] == "9@remote.example"
    if operation == "voice_member.move":
        assert proxy_call.args[5]["data"]["channel_id"] == "20@home.example"
