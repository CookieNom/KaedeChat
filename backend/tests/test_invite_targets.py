from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from pydantic import ValidationError

import app.api.bots as bots_api
import app.api.federation as federation_api
import app.api.invites as invite_api
from app.api.dependencies import AuthenticatedUser
from app.chat.invites import grant_invite_roles, invite_target_payload
from app.chat.schemas import InviteCreate
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.federation.guild_management import GuildManagementResult
from app.federation.schemas import InviteResolveRequest


def target_context() -> tuple[Any, Any, Any, Any]:
    session = SimpleNamespace(get=AsyncMock(), scalar=AsyncMock())
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain="guild.example")
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    return session, redis, settings, guild


def exact_federated_guild_payload() -> dict[str, object]:
    return {
        "id": "10",
        "origin_domain": "guild.example",
        "name": "Guild",
        "description": None,
        "icon_hash": None,
        "banner_hash": None,
        "owner_id": "1",
        "owner_domain": "guild.example",
        "permission_generation": "1",
        "federated_history_policy": "disabled",
        "history_policy_generation": "1",
        "unavailable": False,
        "sync_status": "ready",
        "sync_error_code": None,
        "version": "2026-08-29T00:00:00+00:00",
    }


def exact_federated_invite_payload() -> dict[str, object]:
    return {
        "code": "abcdefgh",
        "guild": exact_federated_guild_payload(),
        "channel_id": "20",
        "target_type": None,
        "target_user_id": None,
        "scheduled_event_id": None,
        "role_ids": [],
        "target_user_count": 0,
        "expires_at": None,
        "uses": 0,
        "max_uses": None,
        "temporary": False,
        "reusable": False,
        "created_at": "2026-08-29T00:00:00+00:00",
        "revoked_at": None,
    }


def test_bare_invite_management_code_uses_explicit_guild_authority() -> None:
    assert invite_api.invite_management_scope(
        "abcdefgh",
        EntityRef("10@guild.example"),
        cast(Any, SimpleNamespace(domain="client.example")),
    ) == ("abcdefgh", "guild.example")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "code": "abcdefgh",
            "guild": {"id": "11", "origin_domain": "guild.example"},
            "channel_id": "20",
        },
        {
            "code": "abcdefgh",
            "guild": {"id": "10", "origin_domain": "guild.example"},
            "channel_id": "21",
        },
        {
            "code": "abcdefgh",
            "guild": {"id": "10", "origin_domain": "guild.example"},
            "channel_id": "20",
            "role_ids": ["91@forged.example"],
        },
        {
            "code": "abcdefgh",
            "guild": {"id": "10", "origin_domain": "guild.example"},
            "channel_id": "20",
            "scheduled_event_id": "30@forged.example",
        },
        {
            "code": "abcdefgh",
            "guild": {"id": "10", "origin_domain": "guild.example"},
            "channel_id": "20",
            "scheduled_event_id": "30@guild.example",
            "guild_scheduled_event": {
                "id": "31",
                "origin_domain": "guild.example",
                "guild_id": "10",
                "guild_domain": "guild.example",
            },
        },
    ],
)
def test_federated_invite_projection_rejects_substituted_resources(
    payload: object,
) -> None:
    with pytest.raises(HTTPException) as raised:
        invite_api.validated_federated_invite_payload(
            payload,
            expected_guild=(10, "guild.example"),
            expected_channel_id=20,
            validate_channel=True,
        )

    assert raised.value.status_code == 502
    assert raised.value.detail == {"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"}


def test_federated_invite_projection_requires_exact_shape_and_order() -> None:
    payload = exact_federated_invite_payload()
    payload["role_ids"] = ["91@guild.example", "92@guild.example"]

    assert (
        invite_api.validated_federated_invite_payload(
            payload,
            expected_guild=(10, "guild.example"),
            expected_channel_id=20,
            validate_channel=True,
        )
        == payload
    )

    for invalid in (
        payload | {"private_note": "must not cross the federation boundary"},
        payload | {"role_ids": ["92@guild.example", "91@guild.example"]},
        payload | {"role_ids": ["91@guild.example", "91@guild.example"]},
        payload | {"guild": exact_federated_guild_payload() | {"version": "1"}},
        payload | {"guild": exact_federated_guild_payload() | {"version": "2026-08-29T00:00:00"}},
    ):
        with pytest.raises(HTTPException) as raised:
            invite_api.validated_federated_invite_payload(
                invalid,
                expected_guild=(10, "guild.example"),
                expected_channel_id=20,
                validate_channel=True,
            )
        assert raised.value.status_code == 502


@pytest.mark.asyncio
async def test_stream_invite_requires_exact_live_guild_occupant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, redis, settings, guild = target_context()
    session.get.return_value = SimpleNamespace()
    occupant = AsyncMock(return_value=SimpleNamespace(can_stream=True))
    monkeypatch.setattr(invite_api, "occupant_in_room", occupant)
    monkeypatch.setattr(invite_api, "screen_share_is_active", AsyncMock(return_value=True))
    payload = InviteCreate.model_validate(
        {
            "channel_id": "20@guild.example",
            "target_type": "stream",
            "target_user_id": "30@people.example",
        }
    )

    user_ref, event_ref = await invite_api.validate_invite_targets(
        cast(Any, session),
        cast(Any, redis),
        cast(Any, settings),
        cast(Any, guild),
        cast(Any, SimpleNamespace(id=20, type=2)),
        payload,
    )

    assert user_ref == (30, "people.example")
    assert event_ref == (None, None)
    assert occupant.await_args.args[1:] == (
        "guild.example",
        "g.10.20",
        "30@people.example",
    )


@pytest.mark.asyncio
async def test_stream_invite_rejects_member_outside_destination_voice_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, redis, settings, guild = target_context()
    session.get.return_value = SimpleNamespace()
    monkeypatch.setattr(invite_api, "occupant_in_room", AsyncMock(return_value=None))
    payload = InviteCreate.model_validate(
        {"target_type": "stream", "target_user_id": "30@people.example"}
    )

    with pytest.raises(HTTPException) as raised:
        await invite_api.validate_invite_targets(
            cast(Any, session),
            cast(Any, redis),
            cast(Any, settings),
            cast(Any, guild),
            cast(Any, SimpleNamespace(id=20, type=2)),
            payload,
        )
    assert raised.value.detail == {"code": "INVITE_TARGET_STREAM_UNAVAILABLE"}


@pytest.mark.asyncio
async def test_stream_invite_requires_an_active_screen_share_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, redis, settings, guild = target_context()
    session.get.return_value = SimpleNamespace()
    monkeypatch.setattr(
        invite_api,
        "occupant_in_room",
        AsyncMock(return_value=SimpleNamespace(can_stream=True)),
    )
    active_share = AsyncMock(return_value=False)
    monkeypatch.setattr(invite_api, "screen_share_is_active", active_share)
    payload = InviteCreate.model_validate(
        {
            "channel_id": "20@guild.example",
            "target_type": "stream",
            "target_user_id": "30@people.example",
        }
    )

    with pytest.raises(HTTPException) as raised:
        await invite_api.validate_invite_targets(
            cast(Any, session),
            cast(Any, redis),
            cast(Any, settings),
            cast(Any, guild),
            cast(Any, SimpleNamespace(id=20, type=2)),
            payload,
        )

    assert raised.value.detail == {"code": "INVITE_TARGET_STREAM_UNAVAILABLE"}
    active_share.assert_awaited_once()


def test_embedded_invite_is_not_advertised_without_an_activity_runtime() -> None:
    with pytest.raises(ValidationError):
        InviteCreate.model_validate(
            {
                "target_type": "embedded_application",
                "target_application_id": "40@apps.example",
            }
        )


@pytest.mark.asyncio
async def test_live_invite_targets_reject_non_voice_destination() -> None:
    session, redis, settings, guild = target_context()
    payload = InviteCreate.model_validate(
        {"target_type": "stream", "target_user_id": "30@people.example"}
    )

    with pytest.raises(HTTPException) as raised:
        await invite_api.validate_invite_targets(
            cast(Any, session),
            cast(Any, redis),
            cast(Any, settings),
            cast(Any, guild),
            cast(Any, SimpleNamespace(id=20, type=0)),
            payload,
        )
    assert raised.value.detail == {"code": "INVITE_TARGET_REQUIRES_VOICE_CHANNEL"}


@pytest.mark.asyncio
async def test_invite_roles_are_idempotent_and_make_membership_permanent() -> None:
    role_one = SimpleNamespace(id=91, origin_domain="guild.example")
    role_two = SimpleNamespace(id=92, origin_domain="guild.example")
    invite = SimpleNamespace(role_ids=["91@guild.example", "92@guild.example"])
    member = SimpleNamespace(
        user_id=30,
        user_domain="people.example",
        temporary=True,
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[role_one, role_two], [91]]),
        execute=AsyncMock(),
    )

    roles, newly_granted = await grant_invite_roles(
        cast(Any, session), cast(Any, guild), cast(Any, member), cast(Any, invite)
    )

    assert roles == [role_one, role_two]
    assert newly_granted == [role_two]
    assert member.temporary is False
    session.execute.assert_awaited_once()


def test_public_invite_target_payload_does_not_expose_user_allowlist() -> None:
    invite = SimpleNamespace(
        target_type=None,
        target_user_id=None,
        target_user_domain=None,
        target_application_id=None,
        target_application_domain=None,
        scheduled_event_id=None,
        scheduled_event_domain=None,
        role_ids=["91@guild.example"],
        target_user_ids=["30@people.example", "31@people.example"],
    )

    payload = invite_target_payload(cast(Any, invite))

    assert payload["role_ids"] == ["91@guild.example"]
    assert payload["target_user_count"] == 2
    assert "target_user_ids" not in payload


def test_audit_only_guild_invite_projection_omits_management_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    invite = SimpleNamespace(
        code="abcdefgh",
        channel_id=20,
        uses=2,
        max_uses=5,
        temporary=True,
        reusable=False,
        target_type=None,
        target_user_id=None,
        target_user_domain=None,
        target_application_id=None,
        target_application_domain=None,
        scheduled_event_id=None,
        scheduled_event_domain=None,
        role_ids=[],
        target_user_ids=[],
        expires_at=now + timedelta(days=1),
        created_at=now,
        revoked_at=None,
    )

    monkeypatch.setattr(invite_api, "guild_payload", lambda _guild: {"id": "10"})
    payload = invite_api.invite_payload(
        cast(Any, invite),
        cast(Any, SimpleNamespace()),
        include_metadata=False,
    )

    assert payload["code"] == "abcdefgh"
    assert payload["expires_at"] == (now + timedelta(days=1)).isoformat()
    assert not {"uses", "max_uses", "temporary", "reusable", "created_at"} & payload.keys()


@pytest.mark.asyncio
async def test_guild_invite_list_requires_manage_guild_and_includes_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=31, origin_domain="people.example")
    session = SimpleNamespace()
    redis = SimpleNamespace()
    permission_check = AsyncMock()
    render = AsyncMock(return_value=[{"code": "abcdefgh"}])
    monkeypatch.setattr(invite_api, "proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr(invite_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(invite_api, "require_permissions", permission_check)
    monkeypatch.setattr(invite_api, "_active_invite_payloads", render)

    result = await invite_api.list_invites(
        EntityRef("10@guild.example"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == [{"code": "abcdefgh"}]
    permission_check.assert_awaited_once_with(
        session,
        redis,
        guild,
        actor,
        Permission.MANAGE_GUILD,
    )
    render.assert_awaited_once_with(
        session,
        guild,
        include_metadata=True,
        viewer=(redis, actor),
    )


@pytest.mark.asyncio
async def test_bot_guild_invite_list_filters_blocked_channel_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(account_type="bot")
    invites = [
        SimpleNamespace(code="allowed1", scheduled_event_id=None),
        SimpleNamespace(code="blocked1", scheduled_event_id=None),
        SimpleNamespace(code="guildall", scheduled_event_id=None),
    ]
    session = SimpleNamespace(scalars=AsyncMock(return_value=invites))

    async def can_access(
        _session: object,
        _redis: object,
        _guild: object,
        _actor: object,
        invite: object,
        *,
        raise_on_denied: bool = True,
    ) -> bool:
        assert not raise_on_denied
        return cast(Any, invite).code != "blocked1"

    monkeypatch.setattr(invite_api, "require_bot_invite_channel_access", can_access)
    monkeypatch.setattr(
        invite_api,
        "scheduled_event_invite_payload",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        invite_api,
        "invite_payload",
        lambda invite, _guild, **_kwargs: {"code": invite.code},
    )

    result = await invite_api._active_invite_payloads(
        cast(Any, session),
        cast(Any, guild),
        viewer=(cast(Any, SimpleNamespace()), cast(Any, actor)),
    )

    assert result == [{"code": "allowed1"}, {"code": "guildall"}]


@pytest.mark.asyncio
async def test_bot_invite_channel_access_checks_nested_scheduled_event_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(account_type="bot")
    invite = SimpleNamespace(
        channel_id=20,
        channel_domain="guild.example",
        scheduled_event_id=30,
        scheduled_event_domain="guild.example",
    )
    event = SimpleNamespace(channel_id=21, channel_domain="guild.example")
    channels = {
        (20, "guild.example"): SimpleNamespace(
            unavailable=False,
            guild_id=10,
            guild_domain="guild.example",
        ),
        (21, "guild.example"): SimpleNamespace(
            unavailable=False,
            guild_id=10,
            guild_domain="guild.example",
        ),
    }
    session = SimpleNamespace(get=AsyncMock(side_effect=lambda _model, ref: channels.get(ref)))
    permission_check = AsyncMock(side_effect=[int(Permission.VIEW_CHANNEL), 0])
    monkeypatch.setattr(
        invite_api,
        "active_scheduled_event_for_invite",
        AsyncMock(return_value=event),
    )
    monkeypatch.setattr(invite_api, "get_permissions", permission_check)

    allowed = await invite_api.require_bot_invite_channel_access(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, guild),
        cast(Any, actor),
        cast(Any, invite),
        raise_on_denied=False,
    )

    assert not allowed
    assert permission_check.await_count == 2


@pytest.mark.asyncio
async def test_bot_channel_less_guild_invite_remains_accessible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(get=AsyncMock())
    monkeypatch.setattr(
        invite_api,
        "active_scheduled_event_for_invite",
        AsyncMock(return_value=None),
    )

    allowed = await invite_api.require_bot_invite_channel_access(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(id=10, origin_domain="guild.example")),
        cast(Any, SimpleNamespace(account_type="bot")),
        cast(
            Any,
            SimpleNamespace(
                channel_id=None,
                channel_domain=None,
                scheduled_event_id=None,
                scheduled_event_domain=None,
            ),
        ),
    )

    assert allowed
    session.get.assert_not_awaited()


@pytest.mark.parametrize("permissions", [Permission(0), Permission.VIEW_AUDIT_LOG])
@pytest.mark.asyncio
async def test_guild_invite_list_rejects_members_without_manage_guild(
    monkeypatch: pytest.MonkeyPatch,
    permissions: Permission,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=31, origin_domain="people.example")
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    render = AsyncMock()
    monkeypatch.setattr(invite_api, "proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr(invite_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        "app.chat.permissions.get_permissions",
        AsyncMock(return_value=int(permissions)),
    )
    monkeypatch.setattr(invite_api, "_active_invite_payloads", render)

    with pytest.raises(HTTPException) as denied:
        await invite_api.list_invites(
            EntityRef("10@guild.example"),
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "MISSING_PERMISSIONS"
    assert denied.value.detail["permissions"] == str(int(Permission.MANAGE_GUILD))
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_revoke_accepts_manage_channels_on_exact_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        guild_id=10,
        guild_domain="guild.example",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=channel))
    permissions = AsyncMock(side_effect=[0, int(Permission.MANAGE_CHANNELS)])
    monkeypatch.setattr(invite_api, "get_permissions", permissions)

    await invite_api.require_invite_revoke_access(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, guild),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(channel_id=20, channel_domain="guild.example")),
    )

    session.get.assert_awaited_once_with(
        invite_api.Channel,
        (20, "guild.example"),
    )
    assert permissions.await_count == 2


@pytest.mark.asyncio
async def test_bot_invite_revoke_checks_channel_before_manage_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = HTTPException(
        status_code=403,
        detail={"code": "BOT_CHANNEL_RESTRICTED"},
    )
    channel_access = AsyncMock(side_effect=denied)
    guild_permissions = AsyncMock(return_value=int(Permission.MANAGE_GUILD))
    monkeypatch.setattr(
        invite_api,
        "require_bot_invite_channel_access",
        channel_access,
    )
    monkeypatch.setattr(invite_api, "get_permissions", guild_permissions)

    with pytest.raises(HTTPException) as raised:
        await invite_api.require_invite_revoke_access(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(id=10, origin_domain="guild.example")),
            cast(Any, SimpleNamespace(account_type="bot")),
            cast(
                Any,
                SimpleNamespace(channel_id=20, channel_domain="guild.example"),
            ),
        )

    assert raised.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    channel_access.assert_awaited_once()
    guild_permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_revoke_returns_deleted_invite_and_records_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = SimpleNamespace(
        code="abcdefgh",
        guild_id=10,
        guild_domain="guild.example",
        revoked_at=None,
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=31, origin_domain="people.example")
    session = SimpleNamespace(scalar=AsyncMock(return_value=invite), commit=AsyncMock())
    rendered = {
        "code": "abcdefgh",
        "revoked_at": "2026-08-29T00:00:00+00:00",
    }
    audit = AsyncMock()
    monkeypatch.setattr(
        invite_api,
        "proxy_remote_guild_management",
        AsyncMock(return_value=None),
    )
    load_guild = AsyncMock(return_value=guild)
    monkeypatch.setattr(invite_api, "local_guild", load_guild)
    monkeypatch.setattr(invite_api, "require_invite_revoke_access", AsyncMock())
    monkeypatch.setattr(
        invite_api,
        "scheduled_event_invite_payload",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(invite_api, "invite_payload", lambda *_args, **_kwargs: rendered)
    monkeypatch.setattr(invite_api, "add_audit_entry", audit)
    monkeypatch.setattr(invite_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(invite_api, "publish_dispatch", AsyncMock())

    result = await invite_api.revoke_invite(
        "abcdefgh",
        EntityRef("10@guild.example"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        " remove stale invite ",
    )

    assert result == rendered
    assert invite.revoked_at is not None
    assert audit.await_args.kwargs["reason"] == "remove stale invite"
    assert load_guild.await_args.args[2] == EntityRef("10@guild.example")
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_invite_revoke_returns_deleted_invite_and_forwards_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    deleted = {"code": "abcdefgh", "revoked_at": "2026-08-29T00:00:00+00:00"}
    revoke = AsyncMock(return_value=deleted)
    monkeypatch.setattr(
        bots_api,
        "bot_invite_management_scope",
        AsyncMock(return_value=(guild, "abcdefgh")),
    )
    monkeypatch.setattr(bots_api, "revoke_invite", revoke)

    result = await bots_api.bot_revoke_invite(
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "bot cleanup",
    )

    assert result == deleted
    assert revoke.await_args.args[-1] == "bot cleanup"


@pytest.mark.asyncio
async def test_bot_invite_create_forwards_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = {"code": "abcdefgh"}
    authorize = AsyncMock()
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(bots_api, "installation_for_guild", authorize)
    monkeypatch.setattr(bots_api, "create_invite", create)

    result = await bots_api.bot_create_invite(
        EntityRef("10@guild.example"),
        InviteCreate(),
        Response(),
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "bot create",
    )

    assert result == created
    assert create.await_args.args[-1] == "bot create"


@pytest.mark.asyncio
async def test_channel_invite_list_uses_exact_channel_permission_and_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=31, origin_domain="people.example")
    auth = SimpleNamespace(user=actor)
    permission_check = AsyncMock()
    render = AsyncMock(return_value=[{"code": "abcdefgh"}])
    monkeypatch.setattr(
        invite_api,
        "_invite_channel_and_guild",
        AsyncMock(return_value=(channel, guild)),
    )
    monkeypatch.setattr(invite_api, "proxy_remote_guild_management", AsyncMock(return_value=None))
    monkeypatch.setattr(invite_api, "require_permissions", permission_check)
    monkeypatch.setattr(invite_api, "_active_invite_payloads", render)

    result = await invite_api.list_channel_invites(
        EntityRef("20@guild.example"),
        cast(Any, auth),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == [{"code": "abcdefgh"}]
    assert permission_check.await_args.kwargs["channel"] is channel
    assert permission_check.await_args.args[4] == Permission.MANAGE_CHANNELS
    assert render.await_args.kwargs["channel"] is channel
    assert render.await_args.kwargs["viewer"][1] is actor


@pytest.mark.asyncio
async def test_channel_invite_list_preserves_three_authority_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=20, origin_domain="guild.example")
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=31, origin_domain="people.example")
    proxy = AsyncMock(
        return_value=GuildManagementResult(
            request_id="kagm_" + "a" * 32,
            operation="invite.list_channel",
            guild={"id": "10", "domain": "guild.example"},
            status_code=200,
            body=[exact_federated_invite_payload()],
        )
    )
    monkeypatch.setattr(
        invite_api,
        "_invite_channel_and_guild",
        AsyncMock(return_value=(channel, guild)),
    )
    monkeypatch.setattr(invite_api, "proxy_remote_guild_management", proxy)

    result = await invite_api.list_channel_invites(
        EntityRef("20@guild.example"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
    )

    assert result[0]["code"] == "abcdefgh"
    assert str(proxy.await_args.args[2]) == "10@guild.example"
    assert proxy.await_args.args[3] is actor
    assert proxy.await_args.args[4:] == (
        "invite.list_channel",
        {"channel_ref": "20@guild.example"},
    )


@pytest.mark.asyncio
async def test_bot_invite_listing_accepts_read_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorize = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    listed = AsyncMock(return_value=[])
    principal = SimpleNamespace(user=SimpleNamespace())
    monkeypatch.setattr(bots_api, "installation_for_guild_any_scope", authorize)
    monkeypatch.setattr(bots_api, "list_invites", listed)

    result = await bots_api.bot_list_invites(
        EntityRef("10@guild.example"),
        cast(Any, principal),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert result == []
    assert authorize.await_args.args[4:] == ("invites.read", "invites.manage")


@pytest.mark.asyncio
async def test_inactive_federated_invite_recovery_binds_exact_viewer_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = SimpleNamespace(
        code="abcdefgh",
        guild_id=10,
        guild_domain="guild.example",
        target_user_ids=[],
        revoked_at=None,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        max_uses=None,
        uses=0,
        scheduled_event_id=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=invite),
        scalar=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await federation_api.federation_invite_resolve(
            InviteResolveRequest(code="abcdefgh", viewer_id="31"),
            cast(Any, SimpleNamespace(origin="people.example", silenced=False)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert raised.value.status_code == 404
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile()
    assert "guild_members.user_id" in str(compiled)
    assert "guild_members.user_domain" in str(compiled)
    assert 31 in compiled.params.values()
    assert "people.example" in compiled.params.values()


@pytest.mark.asyncio
async def test_targeted_invite_preview_is_hidden_from_other_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = SimpleNamespace(
        target_user_ids=["30@people.example"],
        revoked_at=None,
        expires_at=None,
        max_uses=None,
        uses=0,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=invite), scalar=AsyncMock())
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="203.0.113.7"),
    )
    settings = SimpleNamespace(proxy_secret=None, domain="guild.example")
    viewer = AuthenticatedUser(
        user=cast(Any, SimpleNamespace(id=31, origin_domain="people.example")),
        grant=cast(Any, None),
        access_token="token",
        cookie_authenticated=False,
    )
    monkeypatch.setattr(invite_api, "enforce_keyed_rate_limit", AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await invite_api.get_invite(
            "abcdefgh",
            cast(Any, request),
            Response(),
            viewer,
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings),
        )

    assert raised.value.status_code == 404
    session.scalar.assert_not_awaited()


def test_target_user_csv_accepts_federated_refs_and_qualifies_local_ids() -> None:
    assert invite_api.parse_target_users_upload(
        b"user_id\n30\n31@people.example\n",
        "text/csv; charset=utf-8",
        "guild.example",
    ) == ["30@guild.example", "31@people.example"]


def test_target_user_multipart_uses_discord_field_name() -> None:
    boundary = "invite-target-boundary"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="target_users_file"; filename="users.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n"
        "user_id\r\n30\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    assert invite_api.parse_target_users_upload(
        body,
        f"multipart/form-data; boundary={boundary}",
        "guild.example",
    ) == ["30@guild.example"]


def test_target_user_csv_ignores_duplicate_ids_like_discord() -> None:
    assert invite_api.parse_target_users_upload(
        b"user_id\n30\n30@guild.example\n",
        "text/csv",
        "guild.example",
    ) == ["30@guild.example"]


@pytest.mark.asyncio
async def test_inviter_or_audit_permission_can_read_but_not_update_target_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    invite = SimpleNamespace(
        code="abcdefgh",
        revoked_at=None,
        guild_id=10,
        guild_domain="guild.example",
        inviter_id=9,
        inviter_domain="guild.example",
        target_user_ids=["30@people.example"],
        created_at=now,
        updated_at=now,
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    auth = AuthenticatedUser(
        user=cast(Any, SimpleNamespace(id=31, origin_domain="guild.example")),
        grant=cast(Any, None),
        access_token="token",
        cookie_authenticated=False,
    )
    monkeypatch.setattr(
        invite_api,
        "get_permissions",
        AsyncMock(return_value=int(Permission.VIEW_AUDIT_LOG)),
    )
    read_session = SimpleNamespace(scalar=AsyncMock(side_effect=[invite, guild]))

    body = await invite_api.local_get_invite_target_users(
        "abcdefgh",
        None,
        auth,
        cast(Any, read_session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    assert body == {"target_user_ids": ["30@people.example"]}

    update_session = SimpleNamespace(scalar=AsyncMock(side_effect=[invite, guild]))
    with pytest.raises(HTTPException) as raised:
        await invite_api.local_update_invite_target_users(
            "abcdefgh",
            ["32@people.example"],
            None,
            auth,
            cast(Any, update_session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_bot_target_user_access_checks_invite_channel_before_inviter_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invite = SimpleNamespace(
        code="abcdefgh",
        revoked_at=None,
        guild_id=10,
        guild_domain="guild.example",
        inviter_id=31,
        inviter_domain="apps.example",
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(
        id=31,
        origin_domain="apps.example",
        account_type="bot",
    )
    auth = AuthenticatedUser(
        user=cast(Any, actor),
        grant=cast(Any, None),
        access_token="token",
        cookie_authenticated=False,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=invite))
    denied = HTTPException(
        status_code=403,
        detail={"code": "BOT_CHANNEL_RESTRICTED"},
    )
    channel_access = AsyncMock(side_effect=denied)
    permissions = AsyncMock(return_value=int(Permission.MANAGE_GUILD))
    monkeypatch.setattr(invite_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        invite_api,
        "require_bot_invite_channel_access",
        channel_access,
    )
    monkeypatch.setattr(invite_api, "get_permissions", permissions)

    with pytest.raises(HTTPException) as raised:
        await invite_api.local_invite_for_target_users(
            "abcdefgh",
            None,
            auth,
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
            allow_audit_log=True,
        )

    assert raised.value.detail == {"code": "BOT_CHANNEL_RESTRICTED"}
    channel_access.assert_awaited_once()
    permissions.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_user_update_records_the_authority_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    invite = SimpleNamespace(
        code="abcdefgh",
        target_user_ids=["30@people.example"],
        created_at=now,
        updated_at=now,
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    actor = SimpleNamespace(id=9, origin_domain="people.example")
    session = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(
        invite_api,
        "local_invite_for_target_users",
        AsyncMock(return_value=(invite, guild)),
    )
    monkeypatch.setattr(invite_api, "add_audit_entry", audit)
    monkeypatch.setattr(invite_api, "wake_queued_guild_federation", AsyncMock())

    result = await invite_api.local_update_invite_target_users(
        "abcdefgh",
        ["31@people.example"],
        EntityRef("10@guild.example"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "bulk allowlist refresh",
    )

    assert result["total_users"] == 1
    assert audit.await_args.kwargs["reason"] == "bulk allowlist refresh"


@pytest.mark.asyncio
async def test_bot_invite_management_binds_qualified_code_to_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    authorize = AsyncMock(return_value=(guild, SimpleNamespace()))
    monkeypatch.setattr(bots_api, "installation_for_guild", authorize)

    resolved, code = await bots_api.bot_invite_management_scope(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="apps.example")),
        cast(Any, SimpleNamespace()),
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
    )

    assert resolved is guild
    assert code == "abcdefgh"
    assert authorize.await_args.args[-1] == "invites.manage"

    with pytest.raises(HTTPException) as denied:
        await bots_api.bot_invite_management_scope(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="apps.example")),
            cast(Any, SimpleNamespace()),
            EntityRef("10@guild.example"),
            "abcdefgh@forged.example",
        )
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_bot_get_invite_uses_read_scope_and_exact_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    authorize = AsyncMock(return_value=(guild, SimpleNamespace()))
    fetched = {"code": "abcdefgh", "guild": {"id": "10"}}
    fetch = AsyncMock(return_value=fetched)
    monkeypatch.setattr(bots_api, "installation_for_guild_any_scope", authorize)
    monkeypatch.setattr(bots_api, "get_managed_invite", fetch)
    principal = SimpleNamespace(user=SimpleNamespace())
    session = SimpleNamespace()
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain="apps.example")

    result = await bots_api.bot_get_invite(
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
        cast(Any, principal),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, settings),
    )

    assert result == fetched
    assert authorize.await_args.args[4:] == ("invites.read", "invites.manage")
    assert str(fetch.await_args.args[0]) == "10@guild.example"
    assert fetch.await_args.args[1] == "abcdefgh"


@pytest.mark.asyncio
async def test_managed_invite_get_hides_a_bot_blocked_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    invite = SimpleNamespace(code="abcdefgh")
    actor = SimpleNamespace(account_type="bot")
    session = SimpleNamespace(scalar=AsyncMock(return_value=invite))
    channel_access = AsyncMock(return_value=False)
    event_payload = AsyncMock()
    monkeypatch.setattr(
        invite_api,
        "proxy_remote_guild_management",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(invite_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        invite_api,
        "require_bot_invite_channel_access",
        channel_access,
    )
    monkeypatch.setattr(invite_api, "scheduled_event_invite_payload", event_payload)

    with pytest.raises(HTTPException) as denied:
        await invite_api.get_managed_invite(
            EntityRef("10@guild.example"),
            "abcdefgh",
            cast(Any, SimpleNamespace(user=actor)),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "INVITE_NOT_FOUND"}
    assert channel_access.await_args.kwargs == {"raise_on_denied": False}
    event_payload.assert_not_awaited()


def test_bot_target_user_update_has_an_exact_strict_json_contract() -> None:
    parsed = bots_api.BotInviteTargetUsersPut.model_validate(
        {"target_user_ids": ["30@people.example"]}
    )
    assert [str(user_ref) for user_ref in parsed.target_user_ids] == ["30@people.example"]

    with pytest.raises(ValidationError):
        bots_api.BotInviteTargetUsersPut.model_validate({"target_user_ids": [], "ignored": True})


@pytest.mark.asyncio
async def test_bot_target_user_routes_reuse_authoritative_invite_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    scope = AsyncMock(return_value=(guild, "abcdefgh"))
    read = AsyncMock(return_value={"target_user_ids": ["30@people.example"]})
    update = AsyncMock(
        return_value={
            "status": 2,
            "total_users": 1,
            "processed_users": 1,
            "created_at": "2026-08-28T00:00:00+00:00",
            "completed_at": "2026-08-28T00:00:00+00:00",
            "error_message": None,
        }
    )
    status = AsyncMock(return_value=await update())
    monkeypatch.setattr(bots_api, "bot_invite_management_scope", scope)
    monkeypatch.setattr(bots_api, "local_get_invite_target_users", read)
    monkeypatch.setattr(bots_api, "local_update_invite_target_users", update)
    monkeypatch.setattr(
        bots_api,
        "local_get_invite_target_users_job_status",
        status,
    )
    principal = SimpleNamespace(user=SimpleNamespace())
    session = SimpleNamespace()
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain="apps.example")

    body = await bots_api.bot_get_invite_target_users(
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
        cast(Any, principal),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, settings),
    )
    assert body == {"target_user_ids": ["30@people.example"]}

    payload = bots_api.BotInviteTargetUsersPut.model_validate(
        {"target_user_ids": ["30@people.example"]}
    )
    result = await bots_api.bot_update_invite_target_users(
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
        payload,
        cast(Any, principal),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, SimpleNamespace()),
        cast(Any, settings),
        "bot allowlist refresh",
    )
    assert result["status"] == 2
    assert update.await_args.args[1] == ["30@people.example"]
    assert str(update.await_args.args[2]) == "10@guild.example"
    assert update.await_args.args[-1] == "bot allowlist refresh"

    result = await bots_api.bot_get_invite_target_users_job_status(
        EntityRef("10@guild.example"),
        "abcdefgh@guild.example",
        cast(Any, principal),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, settings),
    )
    assert result["processed_users"] == 1
