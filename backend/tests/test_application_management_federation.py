from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.application_directory import DIRECTORY_READINESS_KEYS
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, DeveloperTeamMember
from app.db.models import Attachment, User
from app.federation.application_management import (
    APPLICATION_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS,
    ApplicationManagementOperation,
    ApplicationManagementRequest,
    authorize_application_management_request,
    new_application_management_request,
    remote_management_application,
    request_application_management,
)
from app.federation.security import FederationPrincipal
from app.media import service as media_service


def application(*, domain: str = "apps.example") -> BotApplication:
    return BotApplication(
        id=10,
        origin_domain=domain,
        team_id=20,
        team_domain=domain,
        bot_user_id=30,
        bot_user_domain=domain,
        name="Weather",
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


def request() -> ApplicationManagementRequest:
    return ApplicationManagementRequest(
        application={"id": "10", "domain": "apps.example"},
        team={"id": "20", "domain": "apps.example"},
        bot_user={"id": "30", "domain": "apps.example"},
        actor={"id": "40", "domain": "users.example"},
        requesting_instance="users.example",
        request_id="kaam_" + "A" * 32,
        issued_at=100,
        deadline=115,
        operation="application.get",
        payload={},
    )


def preview_body(*, application_ref: str = "10@apps.example") -> dict[str, object]:
    missing = list(DIRECTORY_READINESS_KEYS)
    return {
        "application_ref": application_ref,
        "application": {
            "id": "10",
            "ref": "10@apps.example",
            "origin_domain": "apps.example",
            "name": "Weather",
            "summary": None,
            "category": None,
            "tags": [],
            "collections": [],
            "icon_hash": None,
            "banner_hash": None,
            "verified": False,
            "install_template": None,
            "user_install_supported": False,
            "description": None,
            "support_url": None,
            "privacy_policy_url": None,
            "terms_url": None,
            "media": [],
            "external_links": [],
            "supported_locales": [],
            "description_localizations": {},
            "popular_commands": [],
            "similar_apps": [],
        },
        "readiness": {
            "status": "incomplete",
            "ready": False,
            "preview_available": True,
            "missing": missing,
            "items": [{"key": key, "ready": False} for key in DIRECTORY_READINESS_KEYS],
        },
    }


def test_application_management_request_is_typed_and_bounded() -> None:
    with pytest.raises(ValidationError):
        ApplicationManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "operation": "arbitrary.http.proxy"}
        )
    with pytest.raises(ValidationError):
        ApplicationManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "deadline": 116}
        )
    with pytest.raises(ValidationError):
        ApplicationManagementRequest.model_validate(
            {**request().model_dump(mode="json"), "issued_at": True}
        )


def test_application_management_resource_ids_reject_json_booleans() -> None:
    from app.api.application_management_federation import _Resource, _ResourceData

    with pytest.raises(ValidationError):
        _Resource.model_validate({"resource_id": True})
    with pytest.raises(ValidationError):
        _ResourceData.model_validate({"resource_id": False, "data": {}})


def test_new_application_management_request_binds_actor_and_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.federation.application_management.time.time", lambda: 1_000)
    created = new_application_management_request(
        SimpleNamespace(domain="users.example"),
        application(),
        actor(),
        "worker.list",
    )
    assert created.application.model_dump() == {"id": "10", "domain": "apps.example"}
    assert created.team.model_dump() == {"id": "20", "domain": "apps.example"}
    assert created.bot_user.model_dump() == {"id": "30", "domain": "apps.example"}
    assert created.actor.model_dump() == {"id": "40", "domain": "users.example"}
    assert created.requesting_instance == "users.example"
    assert created.deadline == 1_015


@pytest.mark.asyncio
async def test_remote_management_requires_a_projected_team_membership() -> None:
    app = application()
    user = actor()
    session = SimpleNamespace(get=AsyncMock(side_effect=[app, None]))
    with pytest.raises(HTTPException) as denied:
        await remote_management_application(
            session,
            SimpleNamespace(domain="users.example"),
            EntityRef("10@apps.example"),
            user,
        )
    assert denied.value.status_code == 404
    assert denied.value.detail["code"] == "APPLICATION_NOT_FOUND"

    member = DeveloperTeamMember(
        team_id=20,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=True,
        role="developer",
    )
    session.get.side_effect = [app, member]
    assert (
        await remote_management_application(
            session,
            SimpleNamespace(domain="users.example"),
            EntityRef("10@apps.example"),
            user,
        )
        is app
    )


@pytest.mark.asyncio
async def test_authority_rechecks_remote_actor_and_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.federation.application_management.time.time", lambda: 105)
    app = application()
    remote_actor = actor(local=False)
    member = DeveloperTeamMember(
        team_id=20,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=False,
        role="developer",
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[app, remote_actor, member]))
    redis = SimpleNamespace(set=AsyncMock(return_value=True))
    resolved_app, resolved_actor = await authorize_application_management_request(
        session,
        redis,
        SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30),
        FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
        10,
        request(),
    )
    assert (resolved_app, resolved_actor) == (app, remote_actor)
    redis.set.assert_awaited_once()

    session.get.side_effect = [app, remote_actor, None]
    redis.set.return_value = True
    with pytest.raises(HTTPException) as revoked:
        await authorize_application_management_request(
            session,
            redis,
            SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30),
            FederationPrincipal(origin="users.example", key_id="key-1"),
            10,
            request().model_copy(update={"request_id": "kaam_" + "B" * 32}),
        )
    assert revoked.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "admission_required"),
    [
        ("application.update", True),
        ("credential.create", True),
        ("asset.delete", True),
        ("application.get", False),
        ("credential.revoke", False),
        ("worker.revoke", False),
    ],
)
async def test_application_authority_applies_semantic_remote_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    operation: ApplicationManagementOperation,
    admission_required: bool,
) -> None:
    current_application = application()
    remote_actor = actor(local=False)
    member = DeveloperTeamMember(
        team_id=20,
        team_domain="apps.example",
        user_id=40,
        user_domain="users.example",
        user_is_local=False,
        role="developer",
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[current_application, remote_actor, member]),
    )
    denied = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    admission = AsyncMock(side_effect=denied)
    monkeypatch.setattr(
        "app.federation.application_management.require_remote_user_creation_allowed",
        admission,
    )
    monkeypatch.setattr(
        "app.federation.application_management.consume_management_request_once",
        AsyncMock(),
    )
    management_request = request().model_copy(update={"operation": operation})

    if admission_required:
        with pytest.raises(HTTPException) as caught:
            await authorize_application_management_request(
                session,
                SimpleNamespace(),
                SimpleNamespace(domain="apps.example"),
                FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
                10,
                management_request,
            )
        assert caught.value is denied
        admission.assert_awaited_once_with(session, remote_actor)
    else:
        resolved = await authorize_application_management_request(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
            FederationPrincipal(origin="users.example", key_id="key-1", silenced=True),
            10,
            management_request,
        )
        assert resolved == (current_application, remote_actor)
        admission.assert_not_awaited()


def test_application_management_admission_exemptions_cover_reads_and_removals() -> None:
    assert {
        "application.get",
        "credential.list",
        "credential.revoke",
        "worker.revoke",
    } <= APPLICATION_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS
    assert not {"application.update", "credential.create", "asset.update", "asset.delete"} & (
        APPLICATION_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS
    )


@pytest.mark.asyncio
async def test_authority_rejects_caller_mismatch_and_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.federation.application_management.time.time", lambda: 105)
    settings = SimpleNamespace(domain="apps.example", federation_clock_skew_seconds=30)
    session = SimpleNamespace(get=AsyncMock())
    redis = SimpleNamespace(set=AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as mismatch:
        await authorize_application_management_request(
            session,
            redis,
            settings,
            FederationPrincipal(origin="other.example", key_id="key-1"),
            10,
            request(),
        )
    assert mismatch.value.detail["code"] == "KAED_FED_APPLICATION_MANAGEMENT_CALLER_MISMATCH"

    redis.set.return_value = False
    with pytest.raises(HTTPException) as replayed:
        await authorize_application_management_request(
            session,
            redis,
            settings,
            FederationPrincipal(origin="users.example", key_id="key-1"),
            10,
            request(),
        )
    assert replayed.value.detail["code"] == "KAED_FED_APPLICATION_MANAGEMENT_REQUEST_REPLAYED"


@pytest.mark.asyncio
async def test_management_response_is_request_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(
            {
                "request_id": "kaam_" + "C" * 32,
                "application": {"id": "10", "domain": "apps.example"},
                "operation": "application.get",
                "status_code": 200,
                "body": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "ref": "10@apps.example",
                },
            }
        ).encode(),
        headers={},
    )
    signed = AsyncMock(return_value=upstream)
    monkeypatch.setattr("app.federation.application_management.signed_request", signed)
    with pytest.raises(HTTPException) as invalid:
        await request_application_management(
            SimpleNamespace(),
            SimpleNamespace(),
            request(),
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"

    upstream.content = json.dumps(
        {
            "request_id": "kaam_" + "A" * 32,
            "application": {"id": "10", "domain": "apps.example"},
            "operation": "application.get",
            "status_code": 201,
            "body": {},
        }
    ).encode()
    with pytest.raises(HTTPException) as wrong_status:
        await request_application_management(
            SimpleNamespace(),
            SimpleNamespace(),
            request(),
        )
    assert wrong_status.value.status_code == 502


@pytest.mark.asyncio
async def test_directory_preview_management_response_is_strict_and_request_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = request().model_copy(update={"operation": "application.directory_preview"})
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(
            {
                "request_id": current.request_id,
                "application": current.application.model_dump(mode="json"),
                "operation": current.operation,
                "status_code": 200,
                "body": preview_body(),
            }
        ).encode(),
        headers={},
    )
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(return_value=upstream),
    )

    result = await request_application_management(
        SimpleNamespace(),
        SimpleNamespace(),
        current,
    )
    assert result.body["application_ref"] == "10@apps.example"

    upstream.content = json.dumps(
        {
            "request_id": current.request_id,
            "application": current.application.model_dump(mode="json"),
            "operation": current.operation,
            "status_code": 200,
            "body": preview_body(application_ref="11@apps.example"),
        }
    ).encode()
    with pytest.raises(HTTPException) as invalid:
        await request_application_management(
            SimpleNamespace(),
            SimpleNamespace(),
            current,
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"

    inconsistent = preview_body()
    inconsistent_readiness = inconsistent["readiness"]
    assert isinstance(inconsistent_readiness, dict)
    inconsistent_readiness["status"] = "approved"
    upstream.content = json.dumps(
        {
            "request_id": current.request_id,
            "application": current.application.model_dump(mode="json"),
            "operation": current.operation,
            "status_code": 200,
            "body": inconsistent,
        }
    ).encode()
    with pytest.raises(HTTPException) as invalid:
        await request_application_management(
            SimpleNamespace(),
            SimpleNamespace(),
            current,
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("application", "operation"),
    [
        ({"id": "11", "domain": "apps.example"}, "application.get"),
        ({"id": "10", "domain": "apps.example"}, "application.update"),
    ],
)
async def test_management_response_rejects_swapped_outer_identity(
    monkeypatch: pytest.MonkeyPatch,
    application: dict[str, str],
    operation: str,
) -> None:
    current = request()
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(
            {
                "request_id": current.request_id,
                "application": application,
                "operation": operation,
                "status_code": 200,
                "body": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "ref": "10@apps.example",
                },
            }
        ).encode(),
        headers={},
    )
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(return_value=upstream),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_application_management(SimpleNamespace(), SimpleNamespace(), current)

    assert invalid.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status_code", "body"),
    [
        ("application.get", 200, None),
        ("credential.list", 200, {}),
        ("application.get", True, {}),
    ],
)
async def test_management_response_rejects_malformed_success_contracts(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    status_code: object,
    body: object,
) -> None:
    current = request().model_copy(update={"operation": operation})
    upstream = SimpleNamespace(
        status_code=200,
        content=json.dumps(
            {
                "request_id": current.request_id,
                "application": current.application.model_dump(mode="json"),
                "operation": current.operation,
                "status_code": status_code,
                "body": body,
            }
        ).encode(),
        headers={},
    )
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(return_value=upstream),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_application_management(
            SimpleNamespace(),
            SimpleNamespace(),
            current,
        )

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "status_code", "body"),
    [
        (
            "application.get",
            {},
            200,
            {"id": "10", "origin_domain": "apps.example", "ref": "11@apps.example"},
        ),
        (
            "application.get",
            {},
            200,
            {
                "id": "10",
                "origin_domain": "apps.example",
                "ref": "10@apps.example",
                "team_ref": "21@apps.example",
                "bot_user": {
                    "id": "30",
                    "origin_domain": "apps.example",
                    "ref": "30@apps.example",
                    "bot": True,
                },
            },
        ),
        (
            "application.get",
            {},
            200,
            {
                "id": "10",
                "origin_domain": "apps.example",
                "ref": "10@apps.example",
                "team_ref": "20@apps.example",
                "bot_user": {
                    "id": "31",
                    "origin_domain": "apps.example",
                    "ref": "31@apps.example",
                    "bot": True,
                },
            },
        ),
        (
            "asset.get",
            {"resource_id": 50},
            200,
            {
                "id": "51",
                "ref": "51@apps.example",
                "application_ref": "10@apps.example",
            },
        ),
        (
            "asset.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@apps.example",
                    "application_ref": "11@apps.example",
                }
            ],
        ),
        (
            "guild_command.list",
            {"guild_ref": "90@guilds.example"},
            200,
            [
                {
                    "id": "60",
                    "ref": "60@apps.example",
                    "origin_domain": "apps.example",
                    "guild_ref": "91@guilds.example",
                }
            ],
        ),
        (
            "command.list",
            {},
            200,
            [
                {
                    "id": "60",
                    "ref": "60@apps.example",
                    "origin_domain": "apps.example",
                    "application_ref": "11@apps.example",
                    "guild_ref": None,
                }
            ],
        ),
        (
            "command.list",
            {},
            200,
            [
                {
                    "id": "60",
                    "ref": "60@apps.example",
                    "origin_domain": "apps.example",
                    "application_ref": "10@apps.example",
                    "guild_ref": "90@guilds.example",
                }
            ],
        ),
        (
            "asset.create",
            {"data": {"attachment_id": "50", "kind": "cover", "name": "Cover"}},
            202,
            {
                "status": "processing",
                "application_ref": "10@apps.example",
                "attachment": {"id": "51", "origin_domain": "apps.example"},
            },
        ),
        (
            "credential.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@apps.example",
                    "application_ref": "11@apps.example",
                }
            ],
        ),
        (
            "worker.create",
            {"data": {}},
            201,
            {
                "id": "50",
                "ref": "51@apps.example",
                "application_ref": "10@apps.example",
            },
        ),
        (
            "instance_rule.put",
            {"target_domain": "TARGET.EXAMPLE.", "data": {"effect": "allow"}},
            200,
            {
                "application_ref": "10@apps.example",
                "target_domain": "other.example",
                "effect": "allow",
            },
        ),
        (
            "installation.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@other.example",
                    "application_ref": "10@apps.example",
                    "guild_ref": "90@guilds.example",
                }
            ],
        ),
    ],
)
async def test_management_response_rejects_swapped_body_identities(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    payload: dict[str, object],
    status_code: int,
    body: object,
) -> None:
    current = request().model_copy(update={"operation": operation, "payload": payload})
    response = {
        "request_id": current.request_id,
        "application": current.application.model_dump(mode="json"),
        "operation": current.operation,
        "status_code": status_code,
        "body": body,
    }
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                content=json.dumps(response).encode(),
                headers={},
            )
        ),
    )

    with pytest.raises(HTTPException) as invalid:
        await request_application_management(SimpleNamespace(), SimpleNamespace(), current)

    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_command_replace_accepts_its_dict_response_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = request().model_copy(update={"operation": "command.replace"})
    response = {
        "request_id": current.request_id,
        "application": current.application.model_dump(mode="json"),
        "operation": current.operation,
        "status_code": 200,
        "body": {
            "generation": "2",
            "commands": 1,
            "items": [
                {
                    "id": "60",
                    "ref": "60@apps.example",
                    "origin_domain": "apps.example",
                    "application_ref": "10@apps.example",
                    "guild_ref": None,
                }
            ],
        },
    }
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                content=json.dumps(response).encode(),
                headers={},
            )
        ),
    )

    result = await request_application_management(SimpleNamespace(), SimpleNamespace(), current)

    assert result.body == response["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "status_code", "body"),
    [
        (
            "credential.create",
            {"data": {}},
            201,
            {
                "id": "50",
                "ref": "50@apps.example",
                "application_ref": "10@apps.example",
            },
        ),
        (
            "worker.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@apps.example",
                    "application_ref": "10@apps.example",
                }
            ],
        ),
        (
            "template.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@apps.example",
                    "application_ref": "10@apps.example",
                }
            ],
        ),
        (
            "instance_rule.put",
            {"target_domain": "TARGET.EXAMPLE.", "data": {"effect": "allow"}},
            200,
            {
                "application_ref": "10@apps.example",
                "target_domain": "target.example",
                "effect": "allow",
            },
        ),
        (
            "installation.list",
            {},
            200,
            [
                {
                    "id": "50",
                    "ref": "50@guilds.example",
                    "application_ref": "10@apps.example",
                    "guild_ref": "90@guilds.example",
                }
            ],
        ),
    ],
)
async def test_application_inventory_responses_are_qualified_and_request_bound(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    payload: dict[str, object],
    status_code: int,
    body: object,
) -> None:
    current = request().model_copy(update={"operation": operation, "payload": payload})
    response = {
        "request_id": current.request_id,
        "application": current.application.model_dump(mode="json"),
        "operation": current.operation,
        "status_code": status_code,
        "body": body,
    }
    monkeypatch.setattr(
        "app.federation.application_management.signed_request",
        AsyncMock(
            return_value=SimpleNamespace(
                status_code=200,
                content=json.dumps(response).encode(),
                headers={},
            )
        ),
    )

    result = await request_application_management(SimpleNamespace(), SimpleNamespace(), current)

    assert result.body == body


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["application_asset", "application_emoji"])
async def test_remote_developer_application_ticket_uses_bounded_authority_storage(
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
) -> None:
    from app.api import application_assets

    remote_actor = actor(local=False)
    current_application = application()
    attachment = Attachment(
        id=50,
        origin_domain="apps.example",
        uploader_id=remote_actor.id,
        uploader_domain=remote_actor.origin_domain,
        filename="asset.png",
        content_type="image/png",
        size=128,
        object_key="apps.example/50/staging/original",
        purpose=purpose,
    )
    create_upload = AsyncMock(return_value=(attachment, "https://uploads.example/50"))
    monkeypatch.setattr(application_assets, "create_upload_ticket", create_upload)
    monkeypatch.setattr(
        application_assets,
        "ticket_payload",
        lambda item, _url: {"id": str(item.id)},
    )
    session = SimpleNamespace(commit=AsyncMock())

    rendered = await application_assets._ticket(
        session,
        SimpleNamespace(domain="apps.example", media_max_attachment_bytes=1024),
        SimpleNamespace(),
        application_assets.AppAccess(current_application, remote_actor),
        application_assets.UploadTicketRequest(
            filename="asset.png",
            content_type="image/png",
            size=128,
        ),
        purpose=purpose,
    )

    assert rendered == {"id": "50", "application_ref": "10@apps.example"}
    assert create_upload.await_args.kwargs["federated_application_upload"] is True
    assert attachment.asset_binding == (f"application_upload:apps.example:10:{purpose}:50")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "purpose"),
    [("asset", "application_asset"), ("emoji", "application_emoji")],
)
async def test_remote_developer_application_commit_finalizes_with_narrow_authority(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    purpose: str,
) -> None:
    from app.api import application_assets

    remote_actor = actor(local=False)
    current_application = application()
    access = application_assets.AppAccess(current_application, remote_actor)
    attachment = Attachment(
        id=50,
        origin_domain="apps.example",
        uploader_id=remote_actor.id,
        uploader_domain=remote_actor.origin_domain,
        filename="asset.png",
        content_type="image/png",
        size=128,
        object_key="apps.example/50/staging/original",
        purpose=purpose,
        scan_status="pending",
        asset_binding=f"application_upload:apps.example:10:{purpose}:50",
    )
    finalize = AsyncMock(return_value=attachment)
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    monkeypatch.setattr(application_assets, "finalize_attachment", finalize)
    monkeypatch.setattr(application_assets, "enqueue_best_effort", AsyncMock())
    scalar_results = [None, 0, 0]
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=scalar_results),
        commit=AsyncMock(),
        add=Mock(),
    )
    response = Response()
    settings = SimpleNamespace(domain="apps.example")

    if kind == "asset":
        rendered = await application_assets._commit_asset(
            session,
            settings,
            SimpleNamespace(),
            response,
            access,
            application_assets.ApplicationAssetCommit(
                attachment_id="50",
                kind="store",
                name="Cover",
            ),
        )
    else:
        rendered = await application_assets._commit_emoji(
            session,
            settings,
            SimpleNamespace(),
            response,
            access,
            application_assets.ApplicationEmojiCommit(
                attachment_id="50",
                name="wave",
            ),
        )

    assert rendered["status"] == "processing"
    assert response.status_code == 202
    assert finalize.await_args.kwargs["federated_application_upload"] is True


def test_public_directory_asset_changes_require_reapproval() -> None:
    from app.api.application_assets import directory_asset_change_requires_reapproval

    assert directory_asset_change_requires_reapproval(None, "store")
    assert directory_asset_change_requires_reapproval("store", None)
    assert directory_asset_change_requires_reapproval("activity", "cover")
    assert not directory_asset_change_requires_reapproval("store", "store", changed=False)
    assert not directory_asset_change_requires_reapproval("activity", "achievement")


def test_application_asset_patch_rejects_null_fields() -> None:
    from app.api.application_assets import ApplicationAssetPatch

    with pytest.raises(ValidationError, match="cannot be null"):
        ApplicationAssetPatch.model_validate({"name": None})
    with pytest.raises(ValidationError, match="cannot be null"):
        ApplicationAssetPatch.model_validate({"kind": None})


@pytest.mark.asyncio
async def test_application_asset_limit_is_enforced_before_media_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_assets

    current_application = application()
    access = application_assets.AppAccess(current_application, actor())
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    finalize = AsyncMock()
    monkeypatch.setattr(application_assets, "finalize_attachment", finalize)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, application_assets.APPLICATION_ASSET_LIMIT])
    )

    with pytest.raises(HTTPException) as limited:
        await application_assets._commit_asset(
            session,
            SimpleNamespace(domain="apps.example"),
            SimpleNamespace(),
            Response(),
            access,
            application_assets.ApplicationAssetCommit(
                attachment_id="50",
                kind="activity",
                name="Map",
            ),
        )

    assert limited.value.status_code == 409
    assert limited.value.detail["code"] == "APPLICATION_ASSET_LIMIT_REACHED"
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_application_store_asset_limit_is_enforced_before_media_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_assets

    current_application = application()
    access = application_assets.AppAccess(current_application, actor())
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    finalize = AsyncMock()
    monkeypatch.setattr(application_assets, "finalize_attachment", finalize)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 0, application_assets.APPLICATION_STORE_ASSET_LIMIT])
    )

    with pytest.raises(HTTPException) as limited:
        await application_assets._commit_asset(
            session,
            SimpleNamespace(domain="apps.example"),
            SimpleNamespace(),
            Response(),
            access,
            application_assets.ApplicationAssetCommit(
                attachment_id="50",
                kind="store",
                name="Screenshot 6",
            ),
        )

    assert limited.value.status_code == 409
    assert limited.value.detail["code"] == "APPLICATION_STORE_ASSET_LIMIT_REACHED"
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_store_asset_commit_appends_to_the_ordered_directory_carousel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_assets

    current_application = application()
    current_application.directory_media = [{"type": "youtube", "video_id": "dQw4w9WgXcQ"}]
    current_application.directory_approved = True
    current_application.manifest_generation = 1
    access = application_assets.AppAccess(current_application, actor())
    attachment = Attachment(
        id=50,
        origin_domain="apps.example",
        uploader_id=40,
        uploader_domain="users.example",
        filename="dashboard.png",
        content_type="image/png",
        detected_content_type="image/png",
        content_sha256="a" * 64,
        size=128,
        object_key="apps.example/50/original",
        purpose="application_asset",
        scan_status="clean",
        asset_binding="application_upload:apps.example:10:application_asset:50",
        width=1280,
        height=720,
    )
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    monkeypatch.setattr(
        application_assets,
        "finalize_attachment",
        AsyncMock(return_value=attachment),
    )
    monkeypatch.setattr(application_assets, "bind_asset", AsyncMock(return_value=None))
    commit = AsyncMock()
    monkeypatch.setattr(
        application_assets,
        "commit_developer_application_mutation",
        commit,
    )
    monkeypatch.setattr(
        application_assets,
        "asset_payload",
        lambda asset: {"id": str(asset.id)},
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 0, 0]),
        add=Mock(),
    )

    rendered = await application_assets._commit_asset(
        session,
        SimpleNamespace(domain="apps.example"),
        SimpleNamespace(mint=AsyncMock(return_value=77)),
        Response(),
        access,
        application_assets.ApplicationAssetCommit(
            attachment_id="50",
            kind="store",
            name="Dashboard",
        ),
    )

    assert rendered == {"id": "77"}
    assert current_application.directory_media == [
        {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
        {"type": "image", "asset_id": "77"},
    ]
    assert current_application.directory_approved is False
    commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_asset_delete_removes_only_its_directory_media_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_assets

    current_application = application()
    current_application.directory_media = [
        {"type": "image", "asset_id": "77"},
        {"type": "youtube", "video_id": "dQw4w9WgXcQ"},
    ]
    current_application.directory_approved = True
    current_application.manifest_generation = 1
    access = application_assets.AppAccess(current_application, actor())
    asset = SimpleNamespace(id=77, kind="store", media_hash="a" * 64)
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    monkeypatch.setattr(application_assets, "_asset", AsyncMock(return_value=asset))
    commit = AsyncMock()
    monkeypatch.setattr(
        application_assets,
        "commit_developer_application_mutation",
        commit,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        delete=AsyncMock(),
    )

    await application_assets._delete_asset(
        session,
        SimpleNamespace(domain="apps.example"),
        access,
        77,
    )

    assert current_application.directory_media == [{"type": "youtube", "video_id": "dQw4w9WgXcQ"}]
    assert current_application.directory_approved is False
    session.delete.assert_awaited_once_with(asset)
    commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_store_asset_upload_counts_youtube_entries_toward_the_shared_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_assets

    current_application = application()
    current_application.directory_media = [
        {"type": "youtube", "video_id": f"video{index:06d}"} for index in range(5)
    ]
    access = application_assets.AppAccess(current_application, actor())
    monkeypatch.setattr(application_assets, "_locked_access", AsyncMock(return_value=access))
    finalize = AsyncMock()
    monkeypatch.setattr(application_assets, "finalize_attachment", finalize)
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, 0]))

    with pytest.raises(HTTPException) as limited:
        await application_assets._commit_asset(
            session,
            SimpleNamespace(domain="apps.example"),
            SimpleNamespace(),
            Response(),
            access,
            application_assets.ApplicationAssetCommit(
                attachment_id="50",
                kind="store",
                name="Dashboard",
            ),
        )

    assert limited.value.detail == {"code": "APPLICATION_STORE_ASSET_LIMIT_REACHED"}
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_application_media_uses_bounded_quota_without_a_local_user_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_actor = actor(local=False)
    settings = SimpleNamespace(
        domain="apps.example",
        media_max_attachment_bytes=1024,
        media_inflight_limit=4,
        media_inflight_quota_bytes=1024,
        media_user_quota_bytes=4096,
        media_upload_ttl_seconds=300,
        media_attachments_bucket="attachments",
    )

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        def presign(self, *_args: object, **_kwargs: object) -> str:
            return "https://uploads.example/60"

        async def head(self, *_args: object) -> SimpleNamespace:
            return SimpleNamespace(size=128, content_type="image/png")

    monkeypatch.setattr(media_service, "S3Storage", Storage)
    local_ledger = AsyncMock(side_effect=AssertionError("remote developers have no local ledger"))
    monkeypatch.setattr(media_service, "locked_usage", local_ledger)
    ticket_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 0, 0, 0]),
        add=Mock(),
        flush=AsyncMock(),
    )
    attachment, _ = await media_service.create_upload_ticket(
        ticket_session,
        settings,
        SimpleNamespace(mint=AsyncMock(return_value=60)),
        remote_actor,
        filename="asset.png",
        content_type="image/png",
        size=128,
        purpose="application_asset",
        federated_application_upload=True,
    )
    attachment.upload_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    finalize_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[attachment, None, 128, 0]),
    )

    finalized = await media_service.finalize_attachment(
        finalize_session,
        settings,
        remote_actor,
        attachment.id,
        required_purpose="application_asset",
        federated_application_upload=True,
    )

    assert finalized.finalized_at is not None
    local_ledger.assert_not_awaited()
    assert ticket_session.scalar.await_count == 4
    assert finalize_session.scalar.await_count == 4

    quota_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 0, 0, settings.media_inflight_limit]),
        add=Mock(),
        flush=AsyncMock(),
    )
    with pytest.raises(HTTPException) as bounded:
        await media_service.create_upload_ticket(
            quota_session,
            settings,
            SimpleNamespace(mint=AsyncMock()),
            remote_actor,
            filename="other.png",
            content_type="image/png",
            size=128,
            purpose="application_asset",
            federated_application_upload=True,
        )
    assert bounded.value.status_code == 429
    assert bounded.value.detail == {"code": "UPLOAD_INFLIGHT_LIMIT"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "service_name"),
    [
        (
            "asset.create",
            {"data": {"attachment_id": "50", "kind": "cover", "name": "Cover"}},
            "create_application_asset",
        ),
        (
            "emoji.create",
            {"data": {"attachment_id": "50", "name": "wave"}},
            "create_application_emoji",
        ),
    ],
)
@pytest.mark.parametrize("processing", [False, True])
async def test_application_media_management_preserves_processing_status(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    payload: dict[str, object],
    service_name: str,
    processing: bool,
) -> None:
    from app.api import application_assets, application_management_federation

    async def create(*args: object, **_kwargs: object) -> dict[str, object]:
        response = next(item for item in args if isinstance(item, Response))
        if processing:
            response.status_code = 202
        return {"status": "processing" if processing else "ready"}

    monkeypatch.setattr(application_assets, service_name, create)
    current_request = request().model_copy(update={"operation": operation, "payload": payload})
    dispatcher = (
        application_management_federation._dispatch_assets
        if operation.startswith("asset.")
        else application_management_federation._dispatch_emojis
    )

    result = await dispatcher(
        current_request,
        actor(local=False),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="apps.example"),
    )

    assert result.status_code == (202 if processing else 201)


@pytest.mark.asyncio
async def test_authority_converts_nested_payload_errors_to_public_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import application_management_federation

    monkeypatch.setattr(
        application_management_federation,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(
        application_management_federation,
        "authorize_application_management_request",
        AsyncMock(return_value=(application(), actor(local=False))),
    )
    malformed = request().model_copy(
        update={"operation": "asset.get", "payload": {"resource_id": True}}
    )

    with pytest.raises(RequestValidationError):
        await application_management_federation.application_management_authority(
            10,
            malformed,
            FederationPrincipal(origin="users.example", key_id="key-1"),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
        )
