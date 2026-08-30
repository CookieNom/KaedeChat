from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api import applications, developer_management_federation
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, DeveloperTeam, DeveloperTeamMember
from app.db.models import User
from app.federation.developer_management import (
    DEVELOPER_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS,
    DeveloperManagementOperation,
    DeveloperManagementRequest,
    authorize_developer_management_request,
    new_developer_management_request,
    remote_management_developer_team,
    request_developer_management,
)
from app.federation.security import FederationPrincipal


def team(*, domain: str = "apps.example") -> DeveloperTeam:
    return DeveloperTeam(
        id=20,
        origin_domain=domain,
        name="Remote team",
        personal=False,
    )


def actor(*, domain: str = "users.example", local: bool = True) -> User:
    return User(
        id=40,
        origin_domain=domain,
        is_local=local,
        account_type="human",
        username="alice",
        password_hash="hash" if local else None,
    )


def request(
    operation: str = "member.list",
    *,
    payload: dict[str, object] | None = None,
    request_id: str = "kdtm_" + "A" * 32,
) -> DeveloperManagementRequest:
    return DeveloperManagementRequest.model_validate(
        {
            "team": {"id": "20", "domain": "apps.example"},
            "actor": {"id": "40", "domain": "users.example"},
            "requesting_instance": "users.example",
            "request_id": request_id,
            "issued_at": 100,
            "deadline": 115,
            "operation": operation,
            "payload": payload or {},
        }
    )


def projected_member(*, local: bool) -> DeveloperTeamMember:
    return DeveloperTeamMember(
        team_id=20,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=local,
        role="developer",
    )


def test_developer_management_request_is_closed_and_deadline_bounded() -> None:
    with pytest.raises(ValidationError):
        DeveloperManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "operation": "arbitrary.http.proxy"}
        )
    with pytest.raises(ValidationError):
        DeveloperManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "deadline": 116}
        )
    with pytest.raises(ValidationError):
        DeveloperManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "unexpected": True}
        )
    with pytest.raises(ValidationError):
        DeveloperManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "deadline": True}
        )


def test_new_request_binds_team_actor_and_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.federation.developer_management.time.time", lambda: 1_000)
    created = new_developer_management_request(
        SimpleNamespace(domain="users.example"),
        team(),
        actor(),
        "member.update",
        {"user_ref": "50@apps.example", "data": {"role": "analyst"}},
    )
    assert created.team.model_dump() == {"id": "20", "domain": "apps.example"}
    assert created.actor.model_dump() == {"id": "40", "domain": "users.example"}
    assert created.requesting_instance == "users.example"
    assert created.deadline == 1_015


@pytest.mark.asyncio
async def test_home_requires_local_human_and_projected_membership() -> None:
    current_team = team()
    current_actor = actor()
    session = SimpleNamespace(get=AsyncMock(side_effect=[current_team, None]))
    with pytest.raises(HTTPException) as denied:
        await remote_management_developer_team(
            session,
            SimpleNamespace(domain="users.example"),
            EntityRef("20@apps.example"),
            current_actor,
        )
    assert denied.value.detail == {"code": "DEVELOPER_TEAM_NOT_FOUND"}

    session.get.side_effect = [current_team, projected_member(local=True)]
    assert (
        await remote_management_developer_team(
            session,
            SimpleNamespace(domain="users.example"),
            EntityRef("20@apps.example"),
            current_actor,
        )
        is current_team
    )

    with pytest.raises(HTTPException):
        await remote_management_developer_team(
            SimpleNamespace(get=AsyncMock()),
            SimpleNamespace(domain="users.example"),
            EntityRef("20@apps.example"),
            actor(local=False),
        )


@pytest.mark.asyncio
async def test_authority_rechecks_remote_human_and_authoritative_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.federation.developer_management.time.time", lambda: 105)
    current_team = team()
    remote_actor = actor(local=False)
    member = projected_member(local=False)
    session = SimpleNamespace(get=AsyncMock(side_effect=[current_team, remote_actor, member]))
    redis = SimpleNamespace(set=AsyncMock(return_value=True))
    resolved = await authorize_developer_management_request(
        session,
        redis,
        SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30),
        FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
        20,
        request(),
    )
    assert resolved == (current_team, remote_actor)
    redis.set.assert_awaited_once_with(
        "federation:developer-management:users.example:" + "kdtm_" + "A" * 32,
        "1",
        ex=40,
        nx=True,
    )

    session.get.side_effect = [current_team, remote_actor, None]
    with pytest.raises(HTTPException) as revoked:
        await authorize_developer_management_request(
            session,
            SimpleNamespace(set=AsyncMock(return_value=True)),
            SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30),
            FederationPrincipal(origin="users.example", key_id="key-1"),
            20,
            request(request_id="kdtm_" + "B" * 32),
        )
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "admission_required"),
    [
        ("member.add", {}, True),
        ("application.create", {}, True),
        ("member.list", {}, False),
        ("member.remove", {"user_ref": "40@users.example"}, False),
        ("member.remove", {"user_ref": "50@users.example"}, True),
    ],
)
async def test_developer_authority_applies_semantic_remote_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    operation: DeveloperManagementOperation,
    payload: dict[str, object],
    admission_required: bool,
) -> None:
    current_team = team()
    remote_actor = actor(local=False)
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[current_team, remote_actor, projected_member(local=False)]),
    )
    denied = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    admission = AsyncMock(side_effect=denied)
    monkeypatch.setattr(
        "app.federation.developer_management.require_remote_user_creation_allowed",
        admission,
    )
    monkeypatch.setattr(
        "app.federation.developer_management.consume_management_request_once",
        AsyncMock(),
    )

    if admission_required:
        with pytest.raises(HTTPException) as caught:
            await authorize_developer_management_request(
                session,
                SimpleNamespace(),
                SimpleNamespace(domain="apps.example"),
                FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
                20,
                request(operation, payload=payload),
            )
        assert caught.value is denied
        admission.assert_awaited_once_with(session, remote_actor)
    else:
        resolved = await authorize_developer_management_request(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
            FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
            20,
            request(operation, payload=payload),
        )
        assert resolved == (current_team, remote_actor)
        admission.assert_not_awaited()


def test_developer_management_admission_exemptions_cover_reads_and_removals() -> None:
    assert {
        "member.list",
    } == DEVELOPER_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS


@pytest.mark.asyncio
async def test_authority_rejects_caller_mismatch_expiry_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.federation.developer_management.time.time", lambda: 105)
    settings = SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30)
    session = SimpleNamespace(get=AsyncMock())
    with pytest.raises(HTTPException) as mismatch:
        await authorize_developer_management_request(
            session,
            SimpleNamespace(set=AsyncMock(return_value=True)),
            settings,
            FederationPrincipal(origin="other.example", key_id="key-1"),
            20,
            request(),
        )
    assert mismatch.value.detail["code"] == "KAED_FED_DEVELOPER_MANAGEMENT_CALLER_MISMATCH"

    with pytest.raises(HTTPException) as expired:
        await authorize_developer_management_request(
            session,
            SimpleNamespace(set=AsyncMock(return_value=True)),
            settings,
            FederationPrincipal(origin="users.example", key_id="key-1"),
            20,
            request().model_copy(update={"deadline": 105}),
        )
    assert expired.value.detail["code"] == "KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_EXPIRED"

    with pytest.raises(HTTPException) as replayed:
        await authorize_developer_management_request(
            session,
            SimpleNamespace(set=AsyncMock(return_value=False)),
            settings,
            FederationPrincipal(origin="users.example", key_id="key-1"),
            20,
            request(),
        )
    assert replayed.value.detail["code"] == "KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_REPLAYED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed_field",
    ["request_id", "team", "operation", "status_code"],
)
async def test_response_is_strictly_bound_to_request(
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    response = {
        "request_id": "kdtm_" + "A" * 32,
        "team": {"id": "20", "domain": "apps.example"},
        "operation": "member.list",
        "status_code": 200,
        "body": [],
    }
    replacements: dict[str, object] = {
        "request_id": "kdtm_" + "B" * 32,
        "team": {"id": "21", "domain": "apps.example"},
        "operation": "member.update",
        "status_code": 201,
    }
    response[changed_field] = replacements[changed_field]
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(response).encode(),
        headers={},
    )
    signed = AsyncMock(return_value=upstream)
    monkeypatch.setattr("app.federation.developer_management.signed_request", signed)
    with pytest.raises(HTTPException) as invalid:
        await request_developer_management(
            SimpleNamespace(),
            SimpleNamespace(),
            request(),
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_DEVELOPER_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (200, {}),
        (200, None),
        (True, []),
    ],
)
async def test_response_rejects_malformed_success_contracts(
    monkeypatch: pytest.MonkeyPatch,
    status_code: object,
    body: object,
) -> None:
    response = {
        "request_id": "kdtm_" + "A" * 32,
        "team": {"id": "20", "domain": "apps.example"},
        "operation": "member.list",
        "status_code": status_code,
        "body": body,
    }
    monkeypatch.setattr(
        "app.federation.developer_management.signed_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                content=json.dumps(response).encode(),
                headers={},
            )
        ),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_developer_management(
            SimpleNamespace(),
            SimpleNamespace(),
            request(),
        )

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_DEVELOPER_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "status_code", "body"),
    [
        (
            "member.update",
            {"user_ref": "50@users.example", "data": {"role": "analyst"}},
            200,
            {
                "user": {
                    "id": "51",
                    "origin_domain": "users.example",
                    "ref": "51@users.example",
                },
                "role": "analyst",
            },
        ),
        (
            "application.create",
            {"data": {"name": "Weather", "team_ref": "20@apps.example"}},
            201,
            {
                "id": "70",
                "origin_domain": "apps.example",
                "ref": "70@apps.example",
                "team_ref": "21@apps.example",
                "bot_user": {
                    "id": "71",
                    "origin_domain": "apps.example",
                    "ref": "71@apps.example",
                },
            },
        ),
        (
            "application.create",
            {"data": {"name": "Weather", "team_ref": "20@apps.example"}},
            201,
            {
                "id": "70",
                "origin_domain": "apps.example",
                "ref": "70@apps.example",
                "team_ref": "20@apps.example",
                "bot_user": {
                    "id": "71",
                    "origin_domain": "bots.example",
                    "ref": "71@bots.example",
                },
            },
        ),
        (
            "application.create",
            {"data": {"name": "Weather", "team_ref": "20@apps.example"}},
            201,
            {
                "id": "70",
                "origin_domain": "apps.example",
                "ref": "70@apps.example",
                "team_ref": "20@apps.example",
                "bot_user": {
                    "id": "71",
                    "origin_domain": "apps.example",
                    "ref": "71@apps.example",
                    "bot": False,
                },
            },
        ),
    ],
)
async def test_response_rejects_swapped_body_identities(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    payload: dict[str, object],
    status_code: int,
    body: dict[str, object],
) -> None:
    current = request(operation, payload=payload)
    response = {
        "request_id": current.request_id,
        "team": current.team.model_dump(mode="json"),
        "operation": current.operation,
        "status_code": status_code,
        "body": body,
    }
    monkeypatch.setattr(
        "app.federation.developer_management.signed_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                content=json.dumps(response).encode(),
                headers={},
            )
        ),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_developer_management(SimpleNamespace(), SimpleNamespace(), current)

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_DEVELOPER_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_request_uses_signed_team_authority_route(monkeypatch: pytest.MonkeyPatch) -> None:
    response = {
        "request_id": "kdtm_" + "A" * 32,
        "team": {"id": "20", "domain": "apps.example"},
        "operation": "member.list",
        "status_code": 200,
        "body": [],
    }
    signed = AsyncMock(
        return_value=SimpleNamespace(
            status_code=200,
            content=json.dumps(response).encode(),
            headers={},
        )
    )
    monkeypatch.setattr("app.federation.developer_management.signed_request", signed)
    result = await request_developer_management(
        SimpleNamespace(),
        SimpleNamespace(),
        request(),
    )
    assert result.body == []
    assert signed.await_args.args[2:5] == (
        "POST",
        "apps.example",
        "/_kaede/v1/developer-teams/20/management",
    )
    assert signed.await_args.kwargs["request_timeout"] == 15


@pytest.mark.asyncio
async def test_member_dispatch_reuses_public_endpoint_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_members = AsyncMock(return_value=[{"role": "developer"}])
    add_member = AsyncMock(return_value={"role": "analyst"})
    update_member = AsyncMock(return_value={"role": "security"})
    remove_member = AsyncMock()
    monkeypatch.setattr(applications, "list_developer_team_members", list_members)
    monkeypatch.setattr(applications, "add_developer_team_member", add_member)
    monkeypatch.setattr(applications, "patch_developer_team_member", update_member)
    monkeypatch.setattr(applications, "remove_developer_team_member", remove_member)
    current_team = team()
    current_actor = actor(local=False)
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="apps.example")
    resolve_profile = AsyncMock()
    monkeypatch.setattr(
        developer_management_federation,
        "resolve_delegated_profile",
        resolve_profile,
    )

    listed = await developer_management_federation._dispatch_member_management(
        request(), current_team, current_actor, session, settings
    )
    added = await developer_management_federation._dispatch_member_management(
        request(
            "member.add",
            payload={
                "data": {"user_ref": "50@users.example", "role": "analyst"},
                "target": {
                    "id": "50",
                    "origin_domain": "users.example",
                    "username": "bob",
                },
            },
        ),
        current_team,
        current_actor,
        session,
        settings,
    )
    updated = await developer_management_federation._dispatch_member_management(
        request(
            "member.update",
            payload={"user_ref": "50@apps.example", "data": {"role": "security"}},
        ),
        current_team,
        current_actor,
        session,
        settings,
    )
    removed = await developer_management_federation._dispatch_member_management(
        request("member.remove", payload={"user_ref": "50@apps.example"}),
        current_team,
        current_actor,
        session,
        settings,
    )

    assert [listed.status_code, added.status_code, updated.status_code, removed.status_code] == [
        200,
        201,
        200,
        204,
    ]
    list_members.assert_awaited_once()
    add_member.assert_awaited_once()
    resolve_profile.assert_awaited_once()
    update_member.assert_awaited_once()
    remove_member.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_create_is_bound_to_authoritative_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_application = AsyncMock(return_value={"ref": "70@apps.example"})
    monkeypatch.setattr(applications, "create_application", create_application)
    current_team = team()
    result = await developer_management_federation._dispatch_application_create(
        request("application.create", payload={"data": {"name": "Weather"}}),
        current_team,
        actor(local=False),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="apps.example"),
    )
    assert result.status_code == 201
    submitted = create_application.await_args.args[0]
    assert submitted.team_ref.resolve("apps.example") == (20, "apps.example")

    with pytest.raises(HTTPException) as mismatch:
        await developer_management_federation._dispatch_application_create(
            request(
                "application.create",
                payload={
                    "data": {
                        "name": "Weather",
                        "team_ref": "21@apps.example",
                    }
                },
                request_id="kdtm_" + "B" * 32,
            ),
            current_team,
            actor(local=False),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
        )
    assert mismatch.value.detail == {"code": "KAED_FED_DEVELOPER_MANAGEMENT_TEAM_MISMATCH"}
    create_application.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_member_add_carries_a_qualified_selected_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = User(
        id=50,
        origin_domain="users.example",
        is_local=True,
        account_type="human",
        username="bob",
        password_hash="hash",
        profile_version=2,
        e2ee_device_generation=1,
    )
    proxy = AsyncMock(return_value=SimpleNamespace(body={"role": "analyst"}))
    monkeypatch.setattr(applications, "proxy_remote_developer_management", proxy)
    result = await applications.add_developer_team_member(
        EntityRef("20@apps.example"),
        applications.DeveloperTeamMemberPut(
            user_ref=EntityRef("50@users.example"),
            role="analyst",
        ),
        developer_management_federation._auth(actor()),
        SimpleNamespace(get=AsyncMock(return_value=target)),
        SimpleNamespace(domain="users.example"),
    )
    assert result == {"role": "analyst"}
    forwarded = proxy.await_args.args[5]
    assert forwarded["data"]["user_ref"] == "50@users.example"
    assert forwarded["target"]["id"] == "50"
    assert forwarded["target"]["origin_domain"] == "users.example"


@pytest.mark.asyncio
async def test_remote_team_application_create_proxies_before_local_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(return_value=SimpleNamespace(body={"ref": "70@apps.example"}))
    monkeypatch.setattr(applications, "proxy_remote_developer_management", proxy)
    monkeypatch.setattr(applications, "enforce_keyed_rate_limit", AsyncMock())
    snowflake = SimpleNamespace(mint=AsyncMock())
    result = await applications.create_application(
        applications.ApplicationCreate(
            name="Weather",
            team_ref=EntityRef("20@apps.example"),
        ),
        Response(),
        developer_management_federation._auth(actor()),
        SimpleNamespace(),
        SimpleNamespace(),
        snowflake,
        SimpleNamespace(domain="users.example"),
    )
    assert result == {"ref": "70@apps.example"}
    assert proxy.await_args.args[5]["data"]["team_ref"] == "20@apps.example"
    snowflake.mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_authority_service_accepts_a_revalidated_remote_team_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_team = team()
    member = projected_member(local=False)
    proxy = AsyncMock(return_value=None)
    commit = AsyncMock()
    monkeypatch.setattr(applications, "proxy_remote_developer_management", proxy)
    monkeypatch.setattr(applications, "enforce_keyed_rate_limit", AsyncMock())
    monkeypatch.setattr(
        applications,
        "managed_team",
        AsyncMock(return_value=(current_team, member)),
    )
    monkeypatch.setattr(applications, "commit_developer_team_mutation", commit)
    monkeypatch.setattr(
        applications,
        "application_payload",
        Mock(return_value={"ref": "70@apps.example"}),
    )
    persisted: list[object] = []
    ordering: list[str] = []

    def add(value: object) -> None:
        persisted.append(value)
        ordering.append(f"add:{type(value).__name__}")

    async def flush() -> None:
        ordering.append("flush")

    session = SimpleNamespace(
        add=Mock(side_effect=add),
        flush=AsyncMock(side_effect=flush),
        scalar=AsyncMock(side_effect=[current_team, 0]),
    )
    result = await applications.create_application(
        applications.ApplicationCreate(
            name="Weather",
            team_ref=EntityRef("20@apps.example"),
        ),
        Response(),
        developer_management_federation._auth(actor(local=False)),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(side_effect=[70, 71])),
        SimpleNamespace(domain="apps.example"),
    )
    assert result["ref"] == "70@apps.example"
    assert [type(value) for value in persisted] == [User, BotApplication]
    assert ordering == ["add:User", "flush", "add:BotApplication"]
    session.flush.assert_awaited_once_with()
    assert session.flush.await_count == 1
    assert session.add.call_count == 2
    commit.assert_awaited_once_with(session, SimpleNamespace(domain="apps.example"), current_team)


@pytest.mark.asyncio
async def test_team_application_capacity_matches_discord_limit() -> None:
    current_team = team()
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[current_team, applications.DEVELOPER_TEAM_APPLICATION_LIMIT])
    )

    with pytest.raises(HTTPException) as caught:
        await applications.require_team_application_capacity(session, current_team)

    assert caught.value.status_code == 409
    assert caught.value.detail == {
        "code": "DEVELOPER_TEAM_APPLICATION_LIMIT_REACHED",
        "limit": 75,
    }


@pytest.mark.asyncio
async def test_authority_converts_nested_payload_errors_to_public_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        developer_management_federation,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        developer_management_federation,
        "authorize_developer_management_request",
        AsyncMock(return_value=(team(), actor(local=False))),
    )
    malformed = request(
        "member.add",
        payload={"data": {"role": "developer"}},
    )
    with pytest.raises(RequestValidationError):
        await developer_management_federation.developer_management_authority(
            20,
            malformed,
            FederationPrincipal(origin="users.example", key_id="key-1"),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
        )
