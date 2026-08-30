from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any, cast, get_args
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError

from app.api import bots, guild_lifecycle, guilds, invites, management, moderation
from app.api import guild_management_federation as management_api
from app.chat.schemas import (
    GuildOwnershipTransfer,
    InviteCreate,
    MemberUpdate,
    OverwritePut,
    RoleUpdate,
)
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Guild, Invite, User
from app.federation import guild_management as guild_management_rpc
from app.federation.guild_management import (
    BOT_GUILD_MANAGEMENT_CONTRACTS,
    BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS,
    GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS,
    GuildManagementOperation,
    GuildManagementRequest,
    GuildManagementResult,
    authorize_guild_management_request,
    proxy_remote_guild_management_body,
    request_guild_management,
)
from app.federation.security import FederationPrincipal
from app.voice.channel_info import channel_info_item


def management_request(operation: str, payload: dict[str, object]) -> GuildManagementRequest:
    return GuildManagementRequest.model_validate(
        {
            "guild": {"id": "10", "domain": "home.example"},
            "actor": {"id": "8", "domain": "remote.example"},
            "requesting_instance": "remote.example",
            "request_id": "kagm_" + "a" * 32,
            "issued_at": 10,
            "deadline": 20,
            "operation": operation,
            "payload": payload,
        }
    )


def management_result(
    status_code: int,
    body: object = None,
    *,
    operation: GuildManagementOperation = "guild.update",
) -> GuildManagementResult:
    return GuildManagementResult(
        request_id="kagm_" + "a" * 32,
        operation=operation,
        guild={"id": "10", "domain": "home.example"},
        status_code=status_code,
        body=body,
    )


def federated_invite_payload(
    code: str = "Abcd1234",
    *,
    guild_id: int = 10,
    channel_id: int | None = 20,
) -> dict[str, object]:
    return {
        "code": code,
        "guild": {
            "id": str(guild_id),
            "origin_domain": "home.example",
            "name": "Guild",
            "description": None,
            "icon_hash": None,
            "banner_hash": None,
            "owner_id": "1",
            "owner_domain": "home.example",
            "permission_generation": "1",
            "federated_history_policy": "disabled",
            "history_policy_generation": "1",
            "unavailable": False,
            "sync_status": "ready",
            "sync_error_code": None,
            "version": "2026-08-29T00:00:00+00:00",
        },
        "channel_id": str(channel_id) if channel_id is not None else None,
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


def human_actor() -> User:
    return User(
        id=8,
        origin_domain="remote.example",
        username="remote-human",
        is_local=False,
        account_type="human",
    )


@pytest.fixture
def allow_remote_management_mutation(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    admission = AsyncMock()
    monkeypatch.setattr(
        guild_management_rpc,
        "require_remote_user_creation_allowed",
        admission,
    )
    return admission


def local_actor() -> User:
    return User(
        id=8,
        origin_domain="client.example",
        username="local-human",
        is_local=True,
        account_type="human",
    )


def test_closed_management_protocol_has_an_authority_dispatcher() -> None:
    operations = set(get_args(GuildManagementOperation))
    special_operations = {"voice_message.capability"}

    assert operations
    assert all(
        management_api._management_dispatcher(operation) is not None
        for operation in operations - special_operations
    )
    assert management_api._management_dispatcher("voice_message.capability") is None
    assert management_api._management_dispatcher("not-a-real-operation") is None
    assert (
        management_api._management_dispatcher("emoji.get")
        is management_api._dispatch_expression_metadata
    )
    assert (
        management_api._management_dispatcher("emoji.create")
        is management_api._dispatch_expression_media
    )
    assert (
        management_api._management_dispatcher("bot_e2ee.grant") is management_api._dispatch_bot_e2ee
    )


def test_management_response_contracts_cover_every_operation_once() -> None:
    operations = set(get_args(GuildManagementOperation))

    assert set(guild_management_rpc._GUILD_MANAGEMENT_RESULT_CONTRACT) == operations
    assert set(guild_management_rpc._GUILD_MANAGEMENT_IDENTITY_CONTRACT) == operations
    assert len(guild_management_rpc._GUILD_MANAGEMENT_IDENTITY_CONTRACT) == len(operations)


def test_authority_result_centrally_echoes_request_lineage() -> None:
    request = management_request("moderation.prune.estimate", {"days": 7})

    result = management_api._result(request, 200, {"pruned": 0, "days": 7})

    assert result.request_id == request.request_id
    assert result.operation == request.operation
    assert result.guild == request.guild


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "envelope_change",
    [
        {"request_id": "kagm_" + "b" * 32},
        {"operation": "guild.owner.transfer"},
        {"guild": {"id": "11", "domain": "home.example"}},
    ],
)
async def test_signed_management_response_rejects_substituted_envelope_lineage(
    monkeypatch: pytest.MonkeyPatch,
    envelope_change: dict[str, object],
) -> None:
    current = management_request("guild.update", {"data": {"name": "Renamed"}})
    response_body: dict[str, object] = {
        "request_id": current.request_id,
        "operation": current.operation,
        "guild": current.guild.model_dump(mode="json"),
        "status_code": 200,
        "body": {"id": "10", "origin_domain": "home.example"},
    }
    response_body.update(envelope_change)
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(response_body).encode(),
        headers={},
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "signed_request",
        AsyncMock(return_value=upstream),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_guild_management(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            current,
        )

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("management_call", "body"),
    [
        (
            management_request(
                "channel.update",
                {"channel_ref": "20@home.example", "data": {"name": "renamed"}},
            ),
            {
                "id": "21",
                "origin_domain": "home.example",
                "guild_id": "10",
                "guild_domain": "home.example",
            },
        ),
        (
            management_request(
                "scheduled_event.update",
                {"resource_ref": "30@home.example", "data": {"name": "Town hall"}},
            ),
            {
                "id": "30",
                "origin_domain": "home.example",
                "guild_id": "11",
                "guild_domain": "home.example",
            },
        ),
        (
            management_request(
                "webhook.update",
                {"resource_id": 40, "data": {"name": "deploys"}},
            ),
            {
                "id": "41",
                "origin_domain": "home.example",
                "guild_id": "10",
                "guild_domain": "home.example",
            },
        ),
        (
            management_request(
                "tracker.lane.update",
                {
                    "channel_ref": "20@home.example",
                    "resource_ref": "50@home.example",
                    "data": {"name": "Ready"},
                },
            ),
            {
                "id": "50",
                "origin_domain": "home.example",
                "channel_id": "21",
                "channel_domain": "home.example",
            },
        ),
        (
            management_request(
                "stage_instance.update",
                {"channel_id": "20@home.example", "data": {"topic": "Town hall"}},
            ),
            {
                "id": "60",
                "origin_domain": "home.example",
                "guild_id": "10",
                "guild_domain": "home.example",
                "channel_id": "21",
                "channel_domain": "home.example",
            },
        ),
        (
            management_request(
                "role_icon.delete",
                {"resource_ref": "30@home.example"},
            ),
            {
                "id": "31",
                "origin_domain": "home.example",
                "guild_id": "10",
                "guild_domain": "home.example",
            },
        ),
        (
            management_request(
                "bot_e2ee.grant",
                {
                    "channel_ref": "20@home.example",
                    "application_ref": "70@apps.example",
                },
            ),
            {
                "channel_ref": "20@home.example",
                "application_ref": "71@apps.example",
            },
        ),
    ],
)
def test_management_response_rejects_substituted_resource_or_guild_body(
    management_call: GuildManagementRequest,
    body: dict[str, object],
) -> None:
    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            management_call,
            GuildManagementResult(
                request_id=management_call.request_id,
                operation=management_call.operation,
                guild=management_call.guild,
                status_code=200,
                body=body,
            ),
        )

    assert invalid.value.status_code == 502
    assert invalid.value.detail == {"code": "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"}


def test_management_response_allows_foreign_nested_actor_identity() -> None:
    request = management_request(
        "scheduled_event.update",
        {"resource_ref": "30@home.example", "data": {"name": "Town hall"}},
    )
    body = {
        "id": "30",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "creator": {"id": "9", "origin_domain": "people.example"},
    }

    result = guild_management_rpc.validate_guild_management_result(
        request,
        GuildManagementResult(
            request_id=request.request_id,
            operation=request.operation,
            guild=request.guild,
            status_code=200,
            body=body,
        ),
    )

    assert result.body == body


@pytest.mark.parametrize(
    ("change",),
    [
        ({"channel_domain": "other.example"},),
        ({"channel_domain": None},),
        ({"identity": "9@remote.example"},),
        ({"room": "g.10.21"},),
    ],
)
def test_stage_voice_response_binds_exact_user_channel_and_room(
    change: dict[str, object],
) -> None:
    request = management_request(
        "stage_voice_state.get",
        {"user_ref": "8@remote.example"},
    )
    body: dict[str, object] = {
        "guild_id": "10",
        "guild_domain": "home.example",
        "user_id": "8",
        "user_domain": "remote.example",
        "identity": "8@remote.example",
        "channel_id": "20",
        "channel_domain": "home.example",
        "room": "g.10.20",
    }
    assert (
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, body, operation="stage_voice_state.get"),
        ).body
        == body
    )

    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(
                200,
                body | change,
                operation="stage_voice_state.get",
            ),
        )
    assert invalid.value.status_code == 502


def test_stage_voice_response_matches_the_explicit_requested_channel() -> None:
    request = management_request(
        "stage_voice_state.user",
        {
            "user_ref": "9@people.example",
            "data": {"channel_id": "20", "suppress": True},
        },
    )
    body = {
        "guild_id": "10",
        "guild_domain": "home.example",
        "user_id": "9",
        "user_domain": "people.example",
        "identity": "9@people.example",
        "channel_id": "20",
        "channel_domain": "home.example",
        "room": "g.10.20",
    }
    guild_management_rpc.validate_guild_management_result(
        request,
        management_result(200, body, operation="stage_voice_state.user"),
    )

    with pytest.raises(HTTPException):
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(
                200,
                body | {"channel_id": "21", "room": "g.10.21"},
                operation="stage_voice_state.user",
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_ref", "qualified_target"),
    [
        ("8", "8@remote.example"),
        ("8@remote.example", "8@remote.example"),
        ("9@people.example", "9@people.example"),
    ],
)
async def test_federated_stage_voice_read_dispatch_preserves_self_or_other_target(
    monkeypatch: pytest.MonkeyPatch,
    target_ref: str,
    qualified_target: str,
) -> None:
    rendered = {
        "guild_id": "10",
        "guild_domain": "home.example",
        "user_id": qualified_target.split("@", 1)[0],
        "user_domain": qualified_target.split("@", 1)[1],
        "identity": qualified_target,
        "channel_id": "20",
        "channel_domain": "home.example",
        "room": "g.10.20",
    }
    get_voice_state = AsyncMock(return_value=rendered)
    monkeypatch.setattr(
        "app.api.stage_instances.get_local_stage_voice_state",
        get_voice_state,
    )
    actor = SimpleNamespace(id=8, origin_domain="remote.example")
    request = management_request("stage_voice_state.get", {"user_ref": target_ref})

    result = await management_api._dispatch_stage_instances(
        request,
        actor,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert result.body == rendered
    assert str(get_voice_state.await_args.args[5]) == qualified_target
    assert get_voice_state.await_args.args[4] is actor


def channel_info_body() -> dict[str, object]:
    return {
        "guild_id": "10",
        "guild_domain": "home.example",
        "channels": [
            {
                "id": "20",
                "origin_domain": "home.example",
                "guild_id": "10",
                "guild_domain": "home.example",
                "status": "Pairing",
            }
        ],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.pop("guild_id"),
        lambda item: item.update(origin_domain="other.example"),
        lambda item: item.update(voice_start_time=123),
        lambda item: item.pop("status"),
    ],
)
def test_channel_info_response_binds_each_unique_channel_and_exact_fields(
    mutate: Any,
) -> None:
    request = management_request("voice_channel_info.get", {"fields": ["status"]})
    valid = channel_info_body()
    assert (
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, valid, operation="voice_channel_info.get"),
        ).body
        == valid
    )

    invalid_body = channel_info_body()
    mutate(cast(list[dict[str, object]], invalid_body["channels"])[0])
    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, invalid_body, operation="voice_channel_info.get"),
        )
    assert invalid.value.status_code == 502

    duplicate = channel_info_body()
    channels = cast(list[dict[str, object]], duplicate["channels"])
    channels.append(dict(channels[0]))
    with pytest.raises(HTTPException):
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, duplicate, operation="voice_channel_info.get"),
        )


def test_bulk_ban_response_is_an_exact_partition_of_requested_qualified_users() -> None:
    request = management_request(
        "moderation.bulk_ban",
        {
            "data": {
                "user_ids": ["20@remote.example", "21@people.example"],
                "delete_message_seconds": 0,
            }
        },
    )
    valid = {
        "banned_users": ["20@remote.example"],
        "failed_users": ["21@people.example"],
        "failed_user_details": [
            {
                "user_id": "21@people.example",
                "code": "MISSING_PERMISSIONS",
                "message": "The user could not be banned.",
            }
        ],
    }
    assert (
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, valid, operation="moderation.bulk_ban"),
        ).body
        == valid
    )

    invalid_bodies = [
        valid | {"banned_users": ["20@remote.example", "22@remote.example"]},
        valid | {"failed_users": []},
        valid | {"banned_users": ["20@remote.example", "21@people.example"]},
        valid | {"banned_users": ["20@remote.example", "20@remote.example"]},
        valid
        | {
            "failed_user_details": [
                {
                    "user_id": "20@remote.example",
                    "code": "MISSING_PERMISSIONS",
                    "message": "The user could not be banned.",
                }
            ]
        },
        valid | {"failed_users": ["21"]},
        valid | {"unexpected": True},
    ]
    for body in invalid_bodies:
        with pytest.raises(HTTPException) as invalid:
            guild_management_rpc.validate_guild_management_result(
                request,
                management_result(200, body, operation="moderation.bulk_ban"),
            )
        assert invalid.value.status_code == 502


@pytest.mark.asyncio
async def test_bulk_ban_authority_resolves_bare_users_against_requesting_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import bulk_moderation

    bulk_ban = AsyncMock(
        return_value={
            "banned_users": ["20@remote.example"],
            "failed_users": [],
            "failed_user_details": [],
        }
    )
    monkeypatch.setattr(bulk_moderation, "bulk_ban_members", bulk_ban)
    request = management_request(
        "moderation.bulk_ban",
        {"data": {"user_ids": ["20"], "delete_message_seconds": 0}},
    )
    await management_api._dispatch_bulk_moderation(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )
    forwarded = bulk_ban.await_args.args[1]
    assert [str(item) for item in forwarded.user_ids] == ["20@remote.example"]

    duplicate = management_request(
        "moderation.bulk_ban",
        {
            "data": {
                "user_ids": ["20", "20@remote.example"],
                "delete_message_seconds": 0,
            }
        },
    )
    with pytest.raises(HTTPException) as rejected:
        await management_api._dispatch_bulk_moderation(
            duplicate,
            human_actor(),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
        )
    assert rejected.value.detail["code"] == "BULK_BAN_USER_DUPLICATE"
    assert bulk_ban.await_count == 1


def test_role_reorder_response_matches_requested_roles_in_exact_order() -> None:
    request = management_request(
        "role.reorder",
        {
            "data": {
                "roles": [
                    {"id": "30", "position": 1, "version": "v1"},
                    {"id": "31", "position": 2, "version": "v2"},
                ]
            }
        },
    )
    roles = [
        {
            "id": "30",
            "origin_domain": "home.example",
            "guild_id": "10",
            "guild_domain": "home.example",
        },
        {
            "id": "31",
            "origin_domain": "home.example",
            "guild_id": "10",
            "guild_domain": "home.example",
        },
    ]
    guild_management_rpc.validate_guild_management_result(
        request,
        management_result(200, roles, operation="role.reorder"),
    )
    for invalid_roles in (
        list(reversed(roles)),
        [roles[0], dict(roles[0])],
        [roles[0]],
        [roles[0], roles[1] | {"origin_domain": "other.example"}],
    ):
        with pytest.raises(HTTPException):
            guild_management_rpc.validate_guild_management_result(
                request,
                management_result(200, invalid_roles, operation="role.reorder"),
            )


def overwrite_body() -> dict[str, object]:
    return {
        "guild_id": "10",
        "guild_domain": "home.example",
        "channel_id": "20",
        "channel_domain": "home.example",
        "overwrites": [
            {
                "target_id": "10",
                "target_domain": "home.example",
                "target_type": "role",
                "allow": "0",
                "deny": "1024",
            },
            {
                "target_id": "9",
                "target_domain": "people.example",
                "target_type": "member",
                "allow": "1024",
                "deny": "0",
            },
        ],
    }


def test_channel_overwrite_response_binds_channel_and_unique_targets() -> None:
    request = management_request(
        "channel.overwrite.list",
        {"channel_ref": "20@home.example"},
    )
    valid = overwrite_body()
    guild_management_rpc.validate_guild_management_result(
        request,
        management_result(200, valid, operation="channel.overwrite.list"),
    )
    overwrites = cast(list[dict[str, object]], valid["overwrites"])
    invalid_bodies = [
        valid | {"channel_id": "21"},
        valid | {"overwrites": [overwrites[0], dict(overwrites[0])]},
        valid
        | {
            "overwrites": [
                overwrites[0] | {"target_domain": "other.example"},
            ]
        },
        valid | {"overwrites": [overwrites[0] | {"allow": "01"}]},
        valid | {"extra": True},
    ]
    for body in invalid_bodies:
        with pytest.raises(HTTPException):
            guild_management_rpc.validate_guild_management_result(
                request,
                management_result(200, body, operation="channel.overwrite.list"),
            )


@pytest.mark.asyncio
async def test_channel_overwrite_authority_wraps_and_public_proxy_unwraps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    public = cast(list[dict[str, str]], overwrite_body()["overwrites"])
    public_list_overwrites = guilds.list_overwrites
    list_service = AsyncMock(return_value=public)
    monkeypatch.setattr(guilds, "list_overwrites", list_service)
    request = management_request(
        "channel.overwrite.list",
        {"channel_ref": "20@home.example"},
    )
    result = await management_api._dispatch_channel_core(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )
    assert result.body == overwrite_body()
    guild_management_rpc.validate_guild_management_result(request, result)

    monkeypatch.setattr(guilds, "list_overwrites", public_list_overwrites)
    monkeypatch.setattr(
        guilds,
        "proxy_remote_guild_management",
        AsyncMock(return_value=result),
    )
    unwrapped = await guilds.list_overwrites(
        EntityRef("10@home.example"),
        EntityRef("20@home.example"),
        cast(Any, SimpleNamespace(user=human_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
    )
    assert unwrapped == public


@pytest.mark.asyncio
async def test_channel_info_item_and_authority_dispatch_emit_full_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="home.example", unavailable=False)
    channel = SimpleNamespace(id=20, origin_domain="home.example", type=2)
    redis = SimpleNamespace(get=AsyncMock(side_effect=[b"Pairing", b"123"]))
    rendered = await channel_info_item(
        cast(Any, redis),
        cast(Any, guild),
        cast(Any, channel),
        ("status", "voice_start_time"),
    )
    assert rendered == {
        "id": "20",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "status": "Pairing",
        "voice_start_time": 123,
    }

    visible = AsyncMock(
        return_value={
            "guild_id": "10",
            "channels": [rendered],
        }
    )
    monkeypatch.setattr("app.voice.channel_info.visible_guild_channel_info", visible)
    request = management_request(
        "voice_channel_info.get",
        {"fields": ["status", "voice_start_time"]},
    )
    result = await management_api._dispatch_voice_channel_info(
        request,
        human_actor(),
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=guild))),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert result.status_code == 200
    assert result.body == {
        "guild_id": "10",
        "guild_domain": "home.example",
        "channels": [rendered],
    }
    guild_management_rpc.validate_guild_management_result(request, result)
    assert visible.await_args.args[-1] == ["status", "voice_start_time"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status_code", "body"),
    [
        ("automod.list", 200, {}),
        ("guild.update", 200, None),
        ("guild.update", 201, {}),
        ("guild.update", True, {}),
    ],
)
async def test_signed_management_response_enforces_operation_status_and_shape(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status_code: object,
    body: object,
) -> None:
    current = management_request(operation, {})
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(
            {
                "request_id": current.request_id,
                "operation": current.operation,
                "guild": current.guild.model_dump(mode="json"),
                "status_code": status_code,
                "body": body,
            }
        ).encode(),
        headers={},
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "signed_request",
        AsyncMock(return_value=upstream),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_guild_management(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            current,
        )

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_GUILD_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_body_only_management_proxy_reuses_the_operation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        guild_management_rpc,
        "proxy_remote_guild_management",
        AsyncMock(return_value=management_result(200, {})),
    )

    with pytest.raises(HTTPException) as invalid:
        await proxy_remote_guild_management_body(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            EntityRef("10@home.example"),
            local_actor(),
            "automod.list",
        )

    assert invalid.value.status_code == 502


@pytest.mark.parametrize(
    ("route", "local_authority_call"),
    [
        (management.update_guild, "local_guild("),
        (management.update_channel, "local_guild("),
        (management.reorder_channels, "local_guild("),
        (management.delete_channel, "local_guild("),
        (management.reorder_roles, "local_guild("),
        (management.update_role, "local_guild("),
        (management.delete_role, "local_guild("),
        (management.assign_role, "local_guild("),
        (management.replace_member_roles, "local_guild("),
        (management.remove_role, "local_guild("),
        (guilds.create_channel, "local_guild("),
        (guilds.create_role, "local_guild("),
        (guilds.put_overwrite, "local_guild("),
        (guilds.list_overwrites, "local_guild("),
        (guilds.delete_overwrite, "local_guild("),
        (guilds.sync_channel_permissions, "local_guild("),
        (moderation.update_member, "local_guild("),
        (moderation.kick_member, "kick_member_service("),
        (moderation.ban_member, "stage_ban_member("),
        (moderation.remove_ban, "local_guild("),
        (moderation.list_bans, "local_guild("),
        (moderation.list_instance_bans, "local_guild("),
        (moderation.ban_instance, "local_guild("),
        (moderation.remove_instance_ban, "local_guild("),
        (guild_lifecycle.transfer_guild_ownership, "_locked_guild("),
        (guild_lifecycle.delete_guild, "_locked_guild("),
        (invites.create_invite, "local_guild("),
        (invites.list_invites, "local_guild("),
        (invites.revoke_invite, "session.scalar("),
    ],
)
def test_remote_proxy_admission_precedes_local_authority_checks(
    route: object,
    local_authority_call: str,
) -> None:
    source = inspect.getsource(route)

    assert source.index("proxy_remote_guild_management(") < source.index(local_authority_call)


def test_signed_management_resource_ids_and_timing_reject_json_booleans() -> None:
    with pytest.raises(ValidationError):
        management_api._Resource.model_validate({"resource_id": True})
    with pytest.raises(ValidationError):
        management_api._ResourceMutation.model_validate({"resource_id": False, "data": {}})
    with pytest.raises(ValidationError):
        GuildManagementRequest.model_validate(
            {
                **management_request("automod.list", {}).model_dump(mode="json"),
                "issued_at": True,
            }
        )


@pytest.mark.asyncio
async def test_authority_returns_422_for_malformed_nested_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        management_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        management_api,
        "authorize_guild_management_request",
        AsyncMock(return_value=(SimpleNamespace(), human_actor())),
    )
    api = FastAPI()
    api.include_router(management_api.router)
    api.dependency_overrides[management_api.authenticate_federation] = lambda: FederationPrincipal(
        origin="remote.example", key_id="main"
    )
    api.dependency_overrides[management_api.get_session] = lambda: SimpleNamespace()
    api.dependency_overrides[management_api.get_redis] = lambda: SimpleNamespace()
    api.dependency_overrides[management_api.get_snowflake] = lambda: SimpleNamespace()
    api.dependency_overrides[management_api.get_settings] = lambda: SimpleNamespace(
        domain="home.example"
    )
    malformed = management_request("automod.get", {"resource_id": True})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/_kaede/v1/guilds/10/management",
            json=malformed.model_dump(mode="json"),
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_signed_management_rejects_forged_actor_binding_before_replay_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = management_request("guild.update", {"data": {"name": "Renamed"}})
    forged = request.model_copy(
        update={"actor": request.actor.model_copy(update={"domain": "evil.example"})}
    )
    consumed = AsyncMock()
    monkeypatch.setattr(guild_management_rpc, "consume_management_request_once", consumed)

    with pytest.raises(HTTPException) as caught:
        await authorize_guild_management_request(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            forged,
        )

    assert caught.value.status_code == 403
    consumed.assert_not_awaited()


@pytest.mark.asyncio
async def test_signed_management_replay_fails_before_authority_state_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replayed = AsyncMock(
        side_effect=HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_GUILD_MANAGEMENT_REQUEST_REPLAYED"},
        )
    )
    monkeypatch.setattr(guild_management_rpc, "consume_management_request_once", replayed)
    session = SimpleNamespace(get=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            management_request("guild.update", {"data": {"name": "Renamed"}}),
        )

    assert caught.value.detail == {"code": "KAED_FED_GUILD_MANAGEMENT_REQUEST_REPLAYED"}
    session.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "account_type", "admission_required"),
    [
        ("guild.update", "human", True),
        ("voice_status.update", "bot", True),
        ("member.ban.list", "human", False),
        ("guild.delete", "human", True),
        ("member.kick", "human", True),
        ("scheduled_event.unsubscribe", "human", False),
        ("webhook.e2ee.revoke", "human", False),
        ("bot_e2ee.revoke", "human", False),
    ],
)
async def test_signed_management_applies_semantic_remote_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    account_type: str,
    admission_required: bool,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="remote-actor",
        is_local=False,
        account_type=account_type,
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, SimpleNamespace()]),
        scalars=AsyncMock(),
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "consume_management_request_once",
        AsyncMock(),
    )
    denied = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    admission = AsyncMock(side_effect=denied)
    monkeypatch.setattr(
        guild_management_rpc,
        "require_remote_user_creation_allowed",
        admission,
    )

    if admission_required:
        with pytest.raises(HTTPException) as caught:
            await authorize_guild_management_request(
                cast(Any, session),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace(domain="home.example")),
                FederationPrincipal(origin="remote.example", key_id="main"),
                10,
                management_request(operation, {}),
            )
        assert caught.value is denied
        admission.assert_awaited_once_with(session, actor)
        session.scalars.assert_not_awaited()
    else:
        resolved = await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            management_request(operation, {}),
        )
        assert resolved == (guild, actor)
        admission.assert_not_awaited()


def test_management_admission_exemptions_are_closed_and_representative() -> None:
    operations = set(get_args(GuildManagementOperation))

    assert operations >= GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS
    assert {
        "member.ban.list",
        "scheduled_event.unsubscribe",
        "webhook.e2ee.revoke",
        "bot_e2ee.revoke",
    } <= (GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS)
    assert (
        not {
            "guild.update",
            "guild.delete",
            "member.kick",
            "voice_member.disconnect",
            "bot_e2ee.grant",
        }
        & GUILD_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "granted_scopes", "granted_permissions"),
    [
        (
            "voice_status.update",
            ["channels.manage"],
            Permission.SET_VOICE_CHANNEL_STATUS,
        ),
        ("webhook.update", ["webhooks.manage"], Permission.MANAGE_WEBHOOKS),
        ("invite.get", ["invites.read"], Permission(0)),
        (
            "webhook.avatar.ticket",
            ["webhooks.manage", "attachments.write"],
            Permission.MANAGE_WEBHOOKS,
        ),
        ("automod.create", ["automod.rules.manage"], Permission.MANAGE_GUILD),
        ("scheduled_event.create", ["events.manage"], Permission.CREATE_EVENTS),
        (
            "tracker.task.create",
            ["tasks.write"],
            Permission.VIEW_CHANNEL | Permission.CREATE_TRACKER_TASKS,
        ),
    ],
)
async def test_remote_bot_management_revalidates_exact_active_installation(
    monkeypatch: pytest.MonkeyPatch,
    allow_remote_management_mutation: AsyncMock,
    operation: str,
    granted_scopes: list[str],
    granted_permissions: Permission,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="status-bot",
        is_local=False,
        account_type="bot",
    )
    member = SimpleNamespace()
    installation = SimpleNamespace(
        granted_scopes=granted_scopes,
        granted_permissions=int(granted_permissions),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, member]),
        scalars=AsyncMock(return_value=[installation]),
    )
    consumed = AsyncMock()
    monkeypatch.setattr(guild_management_rpc, "consume_management_request_once", consumed)

    resolved_guild, resolved_actor = await authorize_guild_management_request(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
        FederationPrincipal(origin="remote.example", key_id="main"),
        10,
        management_request(operation, {"resource_id": 20, "data": {}}),
    )

    assert resolved_guild is guild
    assert resolved_actor is actor
    session.scalars.assert_awaited_once()
    statement = session.scalars.await_args.args[0]
    compiled = statement.compile()
    sql = str(compiled)
    assert "bot_installations.status" in sql
    assert "bot_installations.revoked_at IS NULL" in sql
    assert "bot_installations.bot_user_id" in sql
    assert "bot_installations.bot_user_domain" in sql
    assert "bot_applications.bot_user_id" in sql
    assert "bot_application_targets.runtime_fingerprint IS NOT NULL" in sql
    assert "bot_application_targets.runtime_target_allowed IS true" in sql
    assert "active" in compiled.params.values()
    assert 8 in compiled.params.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "permissions", "expected_code"),
    [
        ("stage_voice_state.get", {"user_ref": "8@remote.example"}, Permission(0), None),
        (
            "stage_voice_state.get",
            {"user_ref": "9@people.example"},
            Permission(0),
            "MISSING_PERMISSIONS",
        ),
        (
            "stage_voice_state.get",
            {"user_ref": "9@people.example"},
            Permission.CONNECT,
            None,
        ),
        (
            "stage_voice_state.self",
            {"data": {"suppress": True}},
            Permission(0),
            None,
        ),
        (
            "stage_voice_state.self",
            {"data": {"suppress": False}},
            Permission(0),
            "MISSING_PERMISSIONS",
        ),
        (
            "stage_voice_state.self",
            {"data": {"suppress": False}},
            Permission.MUTE_MEMBERS,
            None,
        ),
        (
            "stage_voice_state.self",
            {"data": {"request_to_speak_timestamp": None}},
            Permission(0),
            None,
        ),
        (
            "stage_voice_state.self",
            {"data": {"request_to_speak_timestamp": "2026-08-29T14:00:00+00:00"}},
            Permission(0),
            "MISSING_PERMISSIONS",
        ),
        (
            "stage_voice_state.self",
            {"data": {"request_to_speak_timestamp": "2026-08-29T14:00:00+00:00"}},
            Permission.REQUEST_TO_SPEAK,
            None,
        ),
        (
            "stage_voice_state.self",
            {
                "data": {
                    "suppress": False,
                    "request_to_speak_timestamp": "2026-08-29T14:00:00+00:00",
                }
            },
            Permission.MUTE_MEMBERS,
            "MISSING_PERMISSIONS",
        ),
    ],
)
async def test_signed_stage_voice_bot_grants_follow_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
    allow_remote_management_mutation: AsyncMock,
    operation: str,
    payload: dict[str, object],
    permissions: Permission,
    expected_code: str | None,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="stage-bot",
        is_local=False,
        account_type="bot",
    )
    scope = "voice.states.read" if operation.endswith(".get") else "voice.connect"
    installation = SimpleNamespace(
        granted_scopes=[scope],
        granted_permissions=int(permissions),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, SimpleNamespace()]),
        scalars=AsyncMock(return_value=[installation]),
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "consume_management_request_once",
        AsyncMock(),
    )
    request = management_request(operation, payload)

    if expected_code is None:
        resolved = await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            request,
        )
        assert resolved == (guild, actor)
    else:
        with pytest.raises(HTTPException) as caught:
            await authorize_guild_management_request(
                cast(Any, session),
                cast(Any, SimpleNamespace()),
                cast(Any, SimpleNamespace(domain="home.example")),
                FederationPrincipal(origin="remote.example", key_id="main"),
                10,
                request,
            )
        assert caught.value.status_code == 403
        assert caught.value.detail["code"] == expected_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"data": {"suppress": "false"}},
        {"data": {"suppress": 0}},
        {"data": {"request_to_speak_timestamp": False}},
        {"data": {"request_to_speak_timestamp": "2026-08-29T14:00:00"}},
        {"data": {"suppress": True}, "unexpected": True},
    ],
)
async def test_signed_stage_voice_bot_grant_decision_rejects_ambiguous_payloads(
    monkeypatch: pytest.MonkeyPatch,
    allow_remote_management_mutation: AsyncMock,
    payload: dict[str, object],
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="stage-bot",
        is_local=False,
        account_type="bot",
    )
    installation = SimpleNamespace(
        granted_scopes=["voice.connect"],
        granted_permissions=int(Permission.ADMINISTRATOR),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, SimpleNamespace()]),
        scalars=AsyncMock(return_value=[installation]),
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "consume_management_request_once",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as caught:
        await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            management_request("stage_voice_state.self", payload),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {"code": "KAED_FED_BAD_REQUEST"}


@pytest.mark.asyncio
async def test_remote_bot_management_rejects_ambiguous_active_installations(
    monkeypatch: pytest.MonkeyPatch,
    allow_remote_management_mutation: AsyncMock,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="federated-bot",
        is_local=False,
        account_type="bot",
    )
    installation = SimpleNamespace(
        granted_scopes=["events.manage"],
        granted_permissions=int(Permission.CREATE_EVENTS),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, SimpleNamespace()]),
        scalars=AsyncMock(return_value=[installation, installation]),
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "consume_management_request_once",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as caught:
        await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            management_request("scheduled_event.create", {}),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "BOT_INSTALLATION_NOT_FOUND"}


def test_bot_management_contract_covers_every_operation_exactly_once() -> None:
    operations = set(get_args(GuildManagementOperation))

    assert not (set(BOT_GUILD_MANAGEMENT_CONTRACTS) & BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS)
    assert (
        set(BOT_GUILD_MANAGEMENT_CONTRACTS) | BOT_GUILD_MANAGEMENT_HUMAN_ONLY_OPERATIONS
    ) == operations
    assert all(
        contract.scope_options and contract.permission_options
        for contract in BOT_GUILD_MANAGEMENT_CONTRACTS.values()
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "scopes", "permissions", "code"),
    [
        ("scheduled_event.create", [], Permission.CREATE_EVENTS, "BOT_SCOPE_REQUIRED"),
        ("scheduled_event.create", ["events.manage"], Permission(0), "MISSING_PERMISSIONS"),
        ("guild.delete", ["guilds.manage"], Permission.ADMINISTRATOR, "BOT_OPERATION_UNSUPPORTED"),
    ],
)
async def test_remote_bot_management_fails_closed_on_missing_installation_grants(
    monkeypatch: pytest.MonkeyPatch,
    allow_remote_management_mutation: AsyncMock,
    operation: str,
    scopes: list[str],
    permissions: Permission,
    code: str,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Guild",
        owner_id=7,
        owner_domain="home.example",
    )
    actor = User(
        id=8,
        origin_domain="remote.example",
        username="federated-bot",
        is_local=False,
        account_type="bot",
    )
    installation = SimpleNamespace(
        granted_scopes=scopes,
        granted_permissions=int(permissions),
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[guild, actor, SimpleNamespace()]),
        scalars=AsyncMock(return_value=[installation]),
    )
    monkeypatch.setattr(
        guild_management_rpc,
        "consume_management_request_once",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as caught:
        await authorize_guild_management_request(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
            FederationPrincipal(origin="remote.example", key_id="main"),
            10,
            management_request(operation, {}),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == code
    if code == "BOT_OPERATION_UNSUPPORTED":
        session.scalars.assert_not_awaited()


@pytest.mark.parametrize(
    "value",
    [
        "short",
        "Abcd_234",
        "Abcd1234@Home.example",
        "Abcd1234@home.example.",
        "Abcd1234@home.example@evil.example",
    ],
)
def test_invite_management_code_rejects_malformed_or_noncanonical_routes(value: str) -> None:
    with pytest.raises(HTTPException) as caught:
        invites.parse_invite_management_code(value)

    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_remote_invite_revoke_requires_matching_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock()
    monkeypatch.setattr(invites, "proxy_remote_guild_management", proxy)

    with pytest.raises(HTTPException) as caught:
        await invites.revoke_invite(
            "Abcd1234@home.example",
            EntityRef("10@other.example"),
            cast(Any, SimpleNamespace(user=local_actor())),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="client.example")),
        )

    assert caught.value.detail == {"code": "INVITE_AUTHORITY_MISMATCH"}
    proxy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["Abcd1234", "Abcd1234@home.example"])
async def test_remote_invite_revoke_strips_route_but_keeps_guild_binding(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    deleted = federated_invite_payload() | {"revoked_at": "2026-08-29T00:00:00+00:00"}
    proxy = AsyncMock(return_value=management_result(200, deleted))
    monkeypatch.setattr(invites, "proxy_remote_guild_management", proxy)

    response = await invites.revoke_invite(
        code,
        EntityRef("10@home.example"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        " remove stale invite ",
    )

    assert response == deleted
    assert proxy.await_args.args[2] == EntityRef("10@home.example")
    assert proxy.await_args.args[4:] == (
        "invite.revoke",
        {"code": "Abcd1234", "reason": "remove stale invite"},
    )


@pytest.mark.asyncio
async def test_invite_revoke_rejects_cross_guild_confused_deputy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(
        scalar=AsyncMock(
            return_value=Invite(
                code="Abcd1234",
                guild_id=11,
                guild_domain="home.example",
            )
        )
    )
    monkeypatch.setattr(invites, "proxy_remote_guild_management", AsyncMock(return_value=None))

    with pytest.raises(HTTPException) as caught:
        await invites.revoke_invite(
            "Abcd1234",
            EntityRef("10@home.example"),
            cast(Any, SimpleNamespace(user=local_actor())),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="home.example")),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "INVITE_NOT_FOUND"}


@pytest.mark.asyncio
async def test_remote_guild_ownership_proxy_qualifies_the_target_and_preserves_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(
        return_value=management_result(
            200,
            {"id": "10", "owner_id": "9", "owner_domain": "other.example"},
        )
    )
    monkeypatch.setattr(guild_lifecycle, "proxy_remote_guild_management", proxy)
    monkeypatch.setattr(
        guild_lifecycle,
        "_locked_guild",
        AsyncMock(side_effect=AssertionError("remote ownership must not use local state")),
    )

    body = await guild_lifecycle.transfer_guild_ownership(
        EntityRef("10@home.example"),
        GuildOwnershipTransfer(owner_id="9@other.example"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "version",
        "ownership handoff",
    )

    assert body["owner_domain"] == "other.example"
    assert proxy.await_args.args[4] == "guild.owner.transfer"
    assert proxy.await_args.args[5] == {
        "data": {"owner_id": "9@other.example"},
        "if_match": "version",
        "reason": "ownership handoff",
    }


@pytest.mark.asyncio
async def test_remote_channel_overwrite_proxy_qualifies_channel_and_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=management_result(200, {"status": "updated"}))
    monkeypatch.setattr(guilds, "proxy_remote_guild_management", proxy)

    body = await guilds.put_overwrite(
        EntityRef("10@home.example"),
        EntityRef("20"),
        OverwritePut(target_id="9", target_type="member", allow="0", deny="0"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "parity",
    )

    assert body == {"status": "updated"}
    assert proxy.await_args.args[4] == "channel.overwrite.put"
    assert proxy.await_args.args[5] == {
        "channel_ref": "20@home.example",
        "data": {
            "target_id": "9@client.example",
            "target_type": "member",
            "allow": "0",
            "deny": "0",
        },
        "reason": "parity",
    }


@pytest.mark.asyncio
async def test_remote_role_proxy_qualifies_authority_owned_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=management_result(200, {"id": "30", "name": "Member"}))
    monkeypatch.setattr(management, "proxy_remote_guild_management", proxy)

    body = await management.update_role(
        EntityRef("10@home.example"),
        EntityRef("30"),
        RoleUpdate(name="Member"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "version",
        "role cleanup",
    )

    assert body["id"] == "30"
    assert proxy.await_args.args[4] == "role.update"
    assert proxy.await_args.args[5]["resource_ref"] == "30@home.example"
    assert proxy.await_args.args[5]["reason"] == "role cleanup"


@pytest.mark.asyncio
async def test_remote_channel_delete_returns_the_authority_channel_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = {
        "id": "20",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "type": 0,
        "name": "obsolete",
    }
    proxy = AsyncMock(return_value=management_result(200, deleted))
    monkeypatch.setattr(management, "proxy_remote_guild_management", proxy)

    body = await management.delete_channel(
        EntityRef("10@home.example"),
        EntityRef("20"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "remove obsolete channel",
    )

    assert body == deleted
    assert proxy.await_args.args[4] == "channel.delete"
    assert proxy.await_args.args[5] == {
        "channel_ref": "20@home.example",
        "reason": "remove obsolete channel",
    }


@pytest.mark.asyncio
async def test_bot_channel_delete_forwards_reason_and_deleted_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = {
        "id": "20",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "type": 0,
        "name": "obsolete",
    }
    delete = AsyncMock(return_value=deleted)
    monkeypatch.setattr(bots, "installation_for_guild", AsyncMock())
    monkeypatch.setattr(bots, "delete_guild_channel", delete)

    body = await bots.bot_delete_channel(
        EntityRef("10@home.example"),
        EntityRef("20@home.example"),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
        "bot cleanup",
    )

    assert body == deleted
    assert delete.await_args.args[-1] == "bot cleanup"


@pytest.mark.asyncio
async def test_remote_member_proxy_qualifies_the_caller_home_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=management_result(200, {"user": {"id": "9"}}))
    monkeypatch.setattr(moderation, "proxy_remote_guild_management", proxy)

    body = await moderation.update_member(
        EntityRef("10@home.example"),
        EntityRef("9"),
        MemberUpdate(nickname="Remote nickname"),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "parity",
    )

    assert body == {"user": {"id": "9"}}
    assert proxy.await_args.args[4] == "member.update"
    assert proxy.await_args.args[5]["user_ref"] == "9@client.example"


@pytest.mark.asyncio
async def test_remote_instance_ban_list_preserves_authority_only_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=management_result(200, [{"domain": "blocked.example"}]))
    monkeypatch.setattr(moderation, "proxy_remote_guild_management", proxy)

    body = await moderation.list_instance_bans(
        EntityRef("10@home.example"),
        25,
        "alpha.example",
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
    )

    assert body == [{"domain": "blocked.example"}]
    assert proxy.await_args.args[4] == "instance_ban.list"
    assert proxy.await_args.args[5] == {"limit": 25, "after": "alpha.example"}


@pytest.mark.asyncio
async def test_remote_invite_create_qualifies_channel_and_requires_create_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = federated_invite_payload()
    proxy = AsyncMock(return_value=management_result(201, created))
    monkeypatch.setattr(invites, "proxy_remote_guild_management", proxy)
    monkeypatch.setattr(invites, "enforce_client_rate_limit", AsyncMock())

    body = await invites.create_invite(
        EntityRef("10@home.example"),
        InviteCreate(channel_id="20@home.example"),
        Response(),
        cast(Any, SimpleNamespace(user=local_actor())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="client.example")),
        "create for incident response",
    )

    assert body == created
    assert proxy.await_args.args[4] == "invite.create"
    assert proxy.await_args.args[5]["data"]["channel_id"] == "20@home.example"
    assert proxy.await_args.args[5]["reason"] == "create for incident response"

    proxy.return_value = management_result(200, created)
    with pytest.raises(HTTPException) as caught:
        await invites.create_invite(
            EntityRef("10@home.example"),
            InviteCreate(channel_id="20@home.example"),
            Response(),
            cast(Any, SimpleNamespace(user=local_actor())),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="client.example")),
        )
    assert caught.value.status_code == 502


@pytest.mark.asyncio
async def test_scheduled_event_subscription_dispatch_uses_the_selected_authority_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscribe = AsyncMock()
    unsubscribe = AsyncMock()
    monkeypatch.setattr("app.api.scheduled_events.subscribe_scheduled_event", subscribe)
    monkeypatch.setattr("app.api.scheduled_events.unsubscribe_scheduled_event", unsubscribe)
    request = management_request(
        "scheduled_event.subscribe",
        {"resource_ref": "42@home.example"},
    )
    actor = SimpleNamespace(id=8, origin_domain="remote.example")

    status_code, body = await management_api._dispatch_scheduled_event_subscription(
        request,
        actor,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (status_code, body) == (204, None)
    subscribe.assert_awaited_once()
    unsubscribe.assert_not_awaited()
    assert str(subscribe.await_args.args[0]) == "10"
    assert str(subscribe.await_args.args[1]) == "42@home.example"
    assert subscribe.await_args.args[2].user is actor


@pytest.mark.asyncio
async def test_guild_lifecycle_authority_dispatch_reuses_the_local_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    update = AsyncMock(return_value={"id": "10", "name": "Renamed"})
    monkeypatch.setattr(management, "update_guild", update)
    request = management_request(
        "guild.update",
        {
            "data": {"name": "Renamed"},
            "if_match": "version",
            "reason": "federated rename",
        },
    )

    result = await management_api._dispatch_guild_core(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, {"id": "10", "name": "Renamed"})
    update.assert_awaited_once()
    assert update.await_args.args[-2:] == ("version", "federated rename")


@pytest.mark.asyncio
async def test_channel_overwrite_authority_dispatch_reuses_the_local_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put = AsyncMock(return_value={"status": "updated"})
    monkeypatch.setattr(guilds, "put_overwrite", put)
    request = management_request(
        "channel.overwrite.put",
        {
            "channel_ref": "20@home.example",
            "data": {
                "target_id": "8@remote.example",
                "target_type": "member",
                "allow": "0",
                "deny": "0",
            },
            "reason": "parity",
        },
    )

    result = await management_api._dispatch_channel_core(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, {"status": "updated"})
    put.assert_awaited_once()
    assert str(put.await_args.args[1]) == "20@home.example"
    assert str(put.await_args.args[2].target_id) == "8@remote.example"


@pytest.mark.asyncio
async def test_channel_reorder_authority_dispatch_preserves_partial_patch_and_204(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reorder = AsyncMock(return_value=Response(status_code=204))
    monkeypatch.setattr(management, "reorder_channels", reorder)
    request = management_request(
        "channel.reorder",
        {
            "data": {"channels": [{"id": "20", "position": 1}]},
            "reason": "federated reorder",
        },
    )

    result = await management_api._dispatch_channel_core(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (204, None)
    forwarded = reorder.await_args.args[1]
    assert forwarded.model_dump(mode="json", exclude_unset=True) == {
        "channels": [{"id": "20", "position": 1}]
    }
    assert reorder.await_args.args[-1] == "federated reorder"


@pytest.mark.asyncio
async def test_channel_delete_authority_dispatch_returns_deleted_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = {
        "id": "20",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "type": 0,
        "name": "obsolete",
    }
    delete = AsyncMock(return_value=deleted)
    monkeypatch.setattr(management, "delete_channel", delete)
    request = management_request(
        "channel.delete",
        {
            "channel_ref": "20@home.example",
            "reason": "federated cleanup",
        },
    )

    result = await management_api._dispatch_channel_core(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, deleted)
    assert delete.await_args.args[-1] == "federated cleanup"


@pytest.mark.asyncio
async def test_role_authority_dispatch_preserves_create_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create = AsyncMock(return_value={"id": "30", "name": "Member"})
    monkeypatch.setattr(guilds, "create_role", create)
    request = management_request(
        "role.create",
        {"data": {"name": "Member", "permissions": "0"}},
    )

    result = await management_api._dispatch_roles(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (
        201,
        {"id": "30", "name": "Member"},
    )
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_member_ban_authority_dispatch_preserves_private_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_service = AsyncMock(return_value=[{"user": {"id": "9"}}])
    monkeypatch.setattr(moderation, "list_bans", list_service)
    request = management_request(
        "member.ban.list",
        {"limit": 25, "after": "9@remote.example"},
    )

    result = await management_api._dispatch_members(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, [{"user": {"id": "9"}}])
    list_service.assert_awaited_once()
    assert list_service.await_args.args[1] == 25
    assert str(list_service.await_args.args[2]) == "9@remote.example"


@pytest.mark.asyncio
async def test_instance_ban_authority_dispatch_preserves_private_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_service = AsyncMock(return_value=[{"domain": "blocked.example"}])
    monkeypatch.setattr(moderation, "list_instance_bans", list_service)
    request = management_request(
        "instance_ban.list",
        {"limit": 25, "after": "alpha.example"},
    )

    result = await management_api._dispatch_instance_bans(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, [{"domain": "blocked.example"}])
    list_service.assert_awaited_once()
    assert list_service.await_args.args[2] == "alpha.example"


@pytest.mark.asyncio
async def test_invite_revoke_authority_binds_the_requested_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted = {"code": "Abcd1234", "revoked_at": "2026-08-29T00:00:00+00:00"}
    revoke = AsyncMock(return_value=deleted)
    monkeypatch.setattr(invites, "revoke_invite", revoke)
    request = management_request(
        "invite.revoke",
        {"code": "Abcd1234", "reason": "federated cleanup"},
    )

    result = await management_api._dispatch_invites(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, deleted)
    revoke.assert_awaited_once()
    assert revoke.await_args.args[0] == "Abcd1234"
    assert str(revoke.await_args.args[1]) == "10"
    assert revoke.await_args.args[-1] == "federated cleanup"


@pytest.mark.asyncio
async def test_invite_get_dispatches_at_the_bound_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetched = federated_invite_payload()
    get_invite = AsyncMock(return_value=fetched)
    monkeypatch.setattr(invites, "get_managed_invite", get_invite)
    request = management_request("invite.get", {"code": "Abcd1234"})

    result = await management_api._dispatch_invites(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, fetched)
    get_invite.assert_awaited_once()
    assert str(get_invite.await_args.args[0]) == "10"
    assert get_invite.await_args.args[1] == "Abcd1234"


def test_invite_get_management_result_binds_requested_code() -> None:
    request = management_request("invite.get", {"code": "Abcd1234"})

    assert (
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(
                200,
                federated_invite_payload(),
                operation="invite.get",
            ),
        ).body
        == federated_invite_payload()
    )
    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(
                200,
                federated_invite_payload(code="Badc5678"),
                operation="invite.get",
            ),
        )
    assert invalid.value.status_code == 502


@pytest.mark.parametrize(
    ("channel_id", "channel_domain"),
    [("20", None), (None, "home.example"), ("20", "other.example")],
)
def test_invite_nested_event_response_keeps_channel_under_guild_authority(
    channel_id: str | None,
    channel_domain: str | None,
) -> None:
    request = management_request("invite.get", {"code": "Abcd1234"})
    body = federated_invite_payload() | {
        "scheduled_event_id": "30@home.example",
        "guild_scheduled_event": {
            "id": "30",
            "origin_domain": "home.example",
            "guild_id": "10",
            "guild_domain": "home.example",
            "channel_id": channel_id,
            "channel_domain": channel_domain,
            "entity_id": None,
            "entity_domain": None,
        },
    }

    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, body, operation="invite.get"),
        )

    assert invalid.value.status_code == 502


@pytest.mark.parametrize(
    ("field", "domain_field"),
    [("channel_id", "channel_domain"), ("entity_id", "entity_domain")],
)
def test_scheduled_event_response_rejects_foreign_nested_resource_authority(
    field: str,
    domain_field: str,
) -> None:
    request = management_request(
        "scheduled_event.get",
        {"resource_ref": "30@home.example"},
    )
    body: dict[str, object] = {
        "id": "30",
        "origin_domain": "home.example",
        "guild_id": "10",
        "guild_domain": "home.example",
        "channel_id": None,
        "channel_domain": None,
        "entity_id": None,
        "entity_domain": None,
    }
    body[field] = "20"
    body[domain_field] = "other.example"

    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, body, operation="scheduled_event.get"),
        )

    assert invalid.value.status_code == 502


def test_invite_revoke_management_result_requires_deleted_invite_body() -> None:
    deleted = federated_invite_payload()
    request = management_request("invite.revoke", {"code": "Abcd1234"})

    assert (
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(200, deleted, operation="invite.revoke"),
        ).body
        == deleted
    )
    with pytest.raises(HTTPException) as invalid:
        guild_management_rpc.validate_guild_management_result(
            request,
            management_result(204, operation="invite.revoke"),
        )
    assert invalid.value.status_code == 502


@pytest.mark.asyncio
async def test_invite_create_authority_forwards_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = federated_invite_payload()
    create = AsyncMock(return_value=created)
    monkeypatch.setattr(invites, "create_invite", create)
    request = management_request(
        "invite.create",
        {"data": {"channel_id": "20@home.example"}, "reason": "federated create"},
    )

    result = await management_api._dispatch_invites(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (201, created)
    assert create.await_args.args[-1] == "federated create"


@pytest.mark.asyncio
async def test_channel_invite_list_dispatch_preserves_qualified_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listed = AsyncMock(return_value=[{"code": "Abcd1234"}])
    monkeypatch.setattr(invites, "list_channel_invites", listed)
    request = management_request(
        "invite.list_channel",
        {"channel_ref": "20@home.example"},
    )

    result = await management_api._dispatch_invites(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, [{"code": "Abcd1234"}])
    listed.assert_awaited_once()
    assert str(listed.await_args.args[0]) == "20@home.example"


@pytest.mark.asyncio
async def test_targeted_invite_update_dispatches_at_guild_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = {
        "status": 2,
        "total_users": 1,
        "processed_users": 1,
        "created_at": "2026-08-28T00:00:00+00:00",
        "completed_at": "2026-08-28T00:00:00+00:00",
        "error_message": None,
    }
    update = AsyncMock(return_value=job)
    monkeypatch.setattr(invites, "local_update_invite_target_users", update)
    request = management_request(
        "invite.target_users.update",
        {
            "code": "Abcd1234",
            "target_user_ids": ["8@remote.example"],
            "reason": "federated allowlist refresh",
        },
    )

    result = await management_api._dispatch_invites(
        request,
        human_actor(),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert (result.status_code, result.body) == (200, job)
    update.assert_awaited_once()
    assert update.await_args.args[0] == "Abcd1234"
    assert update.await_args.args[1] == ["8@remote.example"]
    assert str(update.await_args.args[2]) == "10"
    assert update.await_args.args[-1] == "federated allowlist refresh"
