from __future__ import annotations

import secrets
import time
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import ConfigDict, Field, StrictInt, model_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.instance_restrictions import require_remote_user_creation_allowed
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, DeveloperTeamMember
from app.db.models import User
from app.federation.client import signed_request
from app.federation.management_rpc import (
    MANAGEMENT_RPC_DEADLINE_SECONDS,
    MANAGEMENT_RPC_MAX_RESPONSE_BYTES,
    ManagementRPCErrorContract,
    consume_management_request_once,
    request_management_rpc,
    validate_management_json,
    validate_management_request_shape,
)
from app.federation.schemas import FederationDomain, SnowflakeString
from app.federation.security import FederationPrincipal

APPLICATION_MANAGEMENT_DEADLINE_SECONDS = MANAGEMENT_RPC_DEADLINE_SECONDS
APPLICATION_MANAGEMENT_MAX_RESPONSE_BYTES = MANAGEMENT_RPC_MAX_RESPONSE_BYTES

_APPLICATION_MANAGEMENT_ERRORS = ManagementRPCErrorContract(
    unavailable={
        "code": "FEDERATED_APPLICATION_MANAGEMENT_UNAVAILABLE",
        "message": "The application home could not complete that request. Try again shortly.",
    },
    failed={
        "code": "FEDERATED_APPLICATION_MANAGEMENT_FAILED",
        "message": "The application home rejected that request.",
    },
    invalid_response={"code": "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"},
)

ApplicationManagementOperation = Literal[
    "application.get",
    "application.directory_preview",
    "application.update",
    "credential.create",
    "credential.list",
    "credential.revoke",
    "worker.create",
    "worker.list",
    "worker.revoke",
    "command.replace",
    "command.list",
    "guild_command.replace",
    "guild_command.list",
    "template.create",
    "template.list",
    "instance_rule.list",
    "instance_rule.put",
    "instance_rule.delete",
    "installation.list",
    "asset.list",
    "asset.ticket",
    "asset.get",
    "asset.update",
    "asset.create",
    "asset.delete",
    "emoji.list",
    "emoji.ticket",
    "emoji.get",
    "emoji.update",
    "emoji.create",
    "emoji.delete",
]

# Internal management calls are all POSTs. Exempt reads and credential/worker
# revocation needed for security cleanup; shared-resource deletes still require
# admission. Unknown future operations therefore fail closed by default.
APPLICATION_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS: frozenset[ApplicationManagementOperation] = (
    frozenset(
        {
            "application.get",
            "application.directory_preview",
            "credential.list",
            "credential.revoke",
            "worker.list",
            "worker.revoke",
            "command.list",
            "guild_command.list",
            "template.list",
            "instance_rule.list",
            "installation.list",
            "asset.list",
            "asset.get",
            "emoji.list",
            "emoji.get",
        }
    )
)

_APPLICATION_MANAGEMENT_STATUS: dict[ApplicationManagementOperation, frozenset[int]] = {
    "application.get": frozenset({200}),
    "application.directory_preview": frozenset({200}),
    "application.update": frozenset({200}),
    "credential.create": frozenset({201}),
    "credential.list": frozenset({200}),
    "credential.revoke": frozenset({204}),
    "worker.create": frozenset({201}),
    "worker.list": frozenset({200}),
    "worker.revoke": frozenset({204}),
    "command.replace": frozenset({200}),
    "command.list": frozenset({200}),
    "guild_command.replace": frozenset({200}),
    "guild_command.list": frozenset({200}),
    "template.create": frozenset({201}),
    "template.list": frozenset({200}),
    "instance_rule.list": frozenset({200}),
    "instance_rule.put": frozenset({200}),
    "instance_rule.delete": frozenset({204}),
    "installation.list": frozenset({200}),
    "asset.list": frozenset({200}),
    "asset.ticket": frozenset({201}),
    "asset.get": frozenset({200}),
    "asset.update": frozenset({200}),
    "asset.create": frozenset({201, 202}),
    "asset.delete": frozenset({204}),
    "emoji.list": frozenset({200}),
    "emoji.ticket": frozenset({201}),
    "emoji.get": frozenset({200}),
    "emoji.update": frozenset({200}),
    "emoji.create": frozenset({201, 202}),
    "emoji.delete": frozenset({204}),
}

_APPLICATION_MANAGEMENT_LIST_OPERATIONS = frozenset(
    {
        "credential.list",
        "worker.list",
        "command.list",
        "guild_command.list",
        "template.list",
        "instance_rule.list",
        "installation.list",
        "asset.list",
        "emoji.list",
    }
)
_APPLICATION_MANAGEMENT_EMPTY_OPERATIONS = frozenset(
    {
        "credential.revoke",
        "worker.revoke",
        "instance_rule.delete",
        "asset.delete",
        "emoji.delete",
    }
)


def _invalid_application_management_response() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"code": "FEDERATED_APPLICATION_MANAGEMENT_RESPONSE_INVALID"},
    )


class ApplicationManagementRef(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    domain: FederationDomain


class ApplicationManagementRequest(UnambiguousInputModel):
    """Replay-bounded call from a developer's home to an application home."""

    model_config = ConfigDict(extra="forbid")

    application: ApplicationManagementRef
    team: ApplicationManagementRef
    bot_user: ApplicationManagementRef
    actor: ApplicationManagementRef
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kaam_[A-Za-z0-9_-]{32}$")
    issued_at: StrictInt = Field(ge=0)
    deadline: StrictInt = Field(ge=1)
    operation: ApplicationManagementOperation
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_request(self) -> ApplicationManagementRequest:
        validate_management_request_shape(
            self.issued_at,
            self.deadline,
            label="application-management",
        )
        validate_management_json(
            self.payload,
            label="application-management payload",
        )
        return self


class ApplicationManagementResult(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^kaam_[A-Za-z0-9_-]{32}$")
    application: ApplicationManagementRef
    operation: ApplicationManagementOperation
    status_code: StrictInt = Field(ge=200, le=299)
    body: Any = None

    @model_validator(mode="after")
    def bounded_body(self) -> ApplicationManagementResult:
        validate_management_json(
            self.body,
            label="application-management response",
        )
        if self.status_code == 204 and self.body is not None:
            raise ValueError("empty application-management responses cannot include a body")
        return self


def _qualified_ref(value: object) -> EntityRef:
    if not isinstance(value, str):
        raise _invalid_application_management_response()
    try:
        ref = EntityRef(value)
    except ValueError:
        raise _invalid_application_management_response() from None
    if ref.domain is None:
        raise _invalid_application_management_response()
    return ref


def _require_ref(value: object, expected: ApplicationManagementRef) -> EntityRef:
    ref = _qualified_ref(value)
    if (ref.id, ref.domain) != (int(expected.id), expected.domain):
        raise _invalid_application_management_response()
    return ref


def _require_application_payload(
    body: dict[str, Any],
    request: ApplicationManagementRequest,
) -> None:
    _require_ref(body.get("ref"), request.application)
    if (
        body.get("id") != request.application.id
        or body.get("origin_domain") != request.application.domain
    ):
        raise _invalid_application_management_response()
    _require_ref(body.get("team_ref"), request.team)
    bot_user = body.get("bot_user")
    if not isinstance(bot_user, dict):
        raise _invalid_application_management_response()
    _require_ref(bot_user.get("ref"), request.bot_user)
    if (
        bot_user.get("id") != request.bot_user.id
        or bot_user.get("origin_domain") != request.bot_user.domain
        or bot_user.get("bot") is not True
    ):
        raise _invalid_application_management_response()


def _require_resource_payload(
    body: dict[str, Any],
    expected: ApplicationManagementRef,
    *,
    expected_resource_id: object | None = None,
) -> None:
    _require_ref(body.get("application_ref"), expected)
    resource_ref = _qualified_ref(body.get("ref"))
    if resource_ref.domain != expected.domain or body.get("id") != str(resource_ref.id):
        raise _invalid_application_management_response()
    if expected_resource_id is not None and body.get("id") != str(expected_resource_id):
        raise _invalid_application_management_response()


def _require_application_bound_payload(
    body: dict[str, Any],
    expected: ApplicationManagementRef,
) -> None:
    _require_ref(body.get("application_ref"), expected)


def _require_command_payload(
    body: dict[str, Any],
    expected: ApplicationManagementRef,
    *,
    expected_guild_ref: object | None = None,
) -> None:
    _require_ref(body.get("application_ref"), expected)
    resource_ref = _qualified_ref(body.get("ref"))
    if resource_ref.domain != expected.domain or body.get("id") != str(resource_ref.id):
        raise _invalid_application_management_response()
    if body.get("origin_domain") != expected.domain:
        raise _invalid_application_management_response()
    if expected_guild_ref is None:
        if body.get("guild_ref") is not None:
            raise _invalid_application_management_response()
        return
    qualified_guild = _qualified_ref(expected_guild_ref)
    if body.get("guild_ref") != str(qualified_guild):
        raise _invalid_application_management_response()


def _validate_application_body_binding(
    request: ApplicationManagementRequest,
    result: ApplicationManagementResult,
) -> None:
    operation = request.operation
    body = result.body
    if operation == "application.directory_preview":
        if not isinstance(body, dict):
            raise _invalid_application_management_response()
        from app.api.application_directory import DirectoryPreviewResponse

        try:
            preview = DirectoryPreviewResponse.model_validate(body)
        except ValueError:
            raise _invalid_application_management_response() from None
        _require_ref(preview.application_ref, request.application)
        if (
            preview.application.id != request.application.id
            or preview.application.origin_domain != request.application.domain
        ):
            raise _invalid_application_management_response()
        return
    if operation in {"application.get", "application.update"}:
        if not isinstance(body, dict):
            raise _invalid_application_management_response()
        _require_application_payload(body, request)
        return
    if operation.startswith(("asset.", "emoji.")) and operation.split(".", 1)[1] != "delete":
        items = body if isinstance(body, list) else [body]
        for item in items:
            if not isinstance(item, dict):
                raise _invalid_application_management_response()
            _require_ref(item.get("application_ref"), request.application)
            if operation.endswith(".ticket"):
                attachment_ref = _qualified_ref(f"{item.get('id')}@{item.get('origin_domain')}")
                if attachment_ref.domain != request.application.domain:
                    raise _invalid_application_management_response()
                continue
            if result.status_code == 202:
                if item.get("status") != "processing" or not isinstance(
                    item.get("attachment"), dict
                ):
                    raise _invalid_application_management_response()
                attachment = item["attachment"]
                submitted = request.payload.get("data")
                if (
                    not isinstance(submitted, dict)
                    or attachment.get("id") != str(submitted.get("attachment_id"))
                    or attachment.get("origin_domain") != request.application.domain
                ):
                    raise _invalid_application_management_response()
                continue
            expected_resource_id = (
                request.payload.get("resource_id")
                if operation.endswith((".get", ".update"))
                else None
            )
            _require_resource_payload(
                item,
                request.application,
                expected_resource_id=expected_resource_id,
            )
        return
    if operation in {"command.replace", "guild_command.replace"}:
        if not isinstance(body, dict):
            raise _invalid_application_management_response()
        command_items = body.get("items")
        if not isinstance(command_items, list) or any(
            not isinstance(item, dict) for item in command_items
        ):
            raise _invalid_application_management_response()
        expected_guild = (
            request.payload.get("guild_ref") if operation.startswith("guild_") else None
        )
        for item in command_items:
            _require_command_payload(
                item,
                request.application,
                expected_guild_ref=expected_guild,
            )
        return
    if operation in {"command.list", "guild_command.list"}:
        if not isinstance(body, list):
            raise _invalid_application_management_response()
        expected_guild = (
            request.payload.get("guild_ref") if operation == "guild_command.list" else None
        )
        for item in body:
            if not isinstance(item, dict):
                raise _invalid_application_management_response()
            _require_command_payload(
                item,
                request.application,
                expected_guild_ref=expected_guild,
            )
        return
    if operation in {
        "credential.create",
        "credential.list",
        "worker.create",
        "worker.list",
        "template.create",
        "template.list",
    }:
        items = body if isinstance(body, list) else [body]
        for item in items:
            if not isinstance(item, dict):
                raise _invalid_application_management_response()
            _require_resource_payload(item, request.application)
        return
    if operation in {"instance_rule.list", "instance_rule.put"}:
        items = body if isinstance(body, list) else [body]
        for item in items:
            if not isinstance(item, dict):
                raise _invalid_application_management_response()
            _require_application_bound_payload(item, request.application)
            target_domain = item.get("target_domain")
            if not isinstance(target_domain, str):
                raise _invalid_application_management_response()
            if (
                operation == "instance_rule.put"
                and target_domain
                != str(request.payload.get("target_domain", "")).rstrip(".").lower()
            ):
                raise _invalid_application_management_response()
        return
    if operation == "installation.list":
        if not isinstance(body, list):
            raise _invalid_application_management_response()
        for item in body:
            if not isinstance(item, dict):
                raise _invalid_application_management_response()
            _require_application_bound_payload(item, request.application)
            guild_ref = _qualified_ref(item.get("guild_ref"))
            installation_ref = _qualified_ref(item.get("ref"))
            if (
                item.get("id") != str(installation_ref.id)
                or installation_ref.domain != guild_ref.domain
            ):
                raise _invalid_application_management_response()


def validate_application_management_result(
    request: ApplicationManagementRequest,
    result: ApplicationManagementResult,
) -> ApplicationManagementResult:
    operation = request.operation
    if result.status_code not in _APPLICATION_MANAGEMENT_STATUS[operation]:
        raise _invalid_application_management_response()
    if operation in _APPLICATION_MANAGEMENT_LIST_OPERATIONS:
        valid_body = isinstance(result.body, list)
    elif operation in _APPLICATION_MANAGEMENT_EMPTY_OPERATIONS:
        valid_body = result.body is None
    else:
        valid_body = isinstance(result.body, dict)
    if not valid_body:
        raise _invalid_application_management_response()
    if isinstance(result.body, list) and any(not isinstance(item, dict) for item in result.body):
        raise _invalid_application_management_response()
    _validate_application_body_binding(request, result)
    return result


def application_management_dict_body(result: ApplicationManagementResult) -> dict[str, Any]:
    if not isinstance(result.body, dict):
        raise _invalid_application_management_response()
    return result.body


def application_management_list_body(result: ApplicationManagementResult) -> list[Any]:
    if not isinstance(result.body, list):
        raise _invalid_application_management_response()
    return result.body


def require_application_management_empty(result: ApplicationManagementResult) -> None:
    if result.body is not None:
        raise _invalid_application_management_response()


def new_application_management_request(
    settings: Settings,
    application: BotApplication,
    actor: User,
    operation: ApplicationManagementOperation,
    payload: dict[str, Any] | None = None,
) -> ApplicationManagementRequest:
    issued_at = int(time.time())
    return ApplicationManagementRequest(
        application=ApplicationManagementRef(
            id=str(application.id),
            domain=application.origin_domain,
        ),
        team=ApplicationManagementRef(
            id=str(application.team_id),
            domain=application.team_domain,
        ),
        bot_user=ApplicationManagementRef(
            id=str(application.bot_user_id),
            domain=application.bot_user_domain,
        ),
        actor=ApplicationManagementRef(id=str(actor.id), domain=actor.origin_domain),
        requesting_instance=settings.domain,
        request_id=f"kaam_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + APPLICATION_MANAGEMENT_DEADLINE_SECONDS,
        operation=operation,
        payload=payload or {},
    )


async def remote_management_application(
    session: AsyncSession,
    settings: Settings,
    application_ref: EntityRef,
    actor: User,
) -> BotApplication | None:
    """Return an accessible remote application projection, or ``None`` locally."""

    application_id, application_domain = application_ref.resolve(settings.domain)
    if application_domain == settings.domain:
        return None
    if (
        actor.origin_domain != settings.domain
        or not actor.is_local
        or actor.account_type != "human"
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    application = await session.get(BotApplication, (application_id, application_domain))
    if application is None or application.status == "deleted":
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    member = await session.get(
        DeveloperTeamMember,
        (
            application.team_id,
            application.team_domain,
            actor.id,
            actor.origin_domain,
        ),
    )
    if member is None or not member.user_is_local:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    return application


async def request_application_management(
    session: AsyncSession,
    settings: Settings,
    request: ApplicationManagementRequest,
) -> ApplicationManagementResult:
    """Send one typed application-management RPC and preserve API errors."""

    result = await request_management_rpc(
        session,
        settings,
        authority_domain=request.application.domain,
        path=f"/_kaede/v1/applications/{request.application.id}/management",
        payload=request.model_dump(mode="json"),
        response_model=ApplicationManagementResult,
        response_matches=lambda result: (
            result.request_id == request.request_id
            and result.application == request.application
            and result.operation == request.operation
            and result.status_code in _APPLICATION_MANAGEMENT_STATUS[request.operation]
        ),
        label="application-management",
        errors=_APPLICATION_MANAGEMENT_ERRORS,
        send=signed_request,
    )
    return validate_application_management_result(request, result)


async def proxy_remote_application_management(
    session: AsyncSession,
    settings: Settings,
    application_ref: EntityRef,
    actor: User,
    operation: ApplicationManagementOperation,
    payload: dict[str, Any] | None = None,
) -> ApplicationManagementResult | None:
    application = await remote_management_application(
        session,
        settings,
        application_ref,
        actor,
    )
    if application is None:
        return None
    request = new_application_management_request(
        settings,
        application,
        actor,
        operation,
        payload,
    )
    return await request_application_management(session, settings, request)


async def authorize_application_management_request(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    application_id: int,
    request: ApplicationManagementRequest,
) -> tuple[BotApplication, User]:
    """Authenticate the home/actor binding and current authoritative membership."""

    if (
        principal.origin == settings.domain
        or request.requesting_instance != principal.origin
        or request.actor.domain != principal.origin
        or request.application.domain != settings.domain
        or int(request.application.id) != application_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_APPLICATION_MANAGEMENT_CALLER_MISMATCH"},
        )
    await consume_management_request_once(
        redis,
        settings,
        origin=principal.origin,
        namespace="application-management",
        request_id=request.request_id,
        issued_at=request.issued_at,
        deadline=request.deadline,
        now=int(time.time()),
        expired_code="KAED_FED_APPLICATION_MANAGEMENT_REQUEST_EXPIRED",
        replayed_code="KAED_FED_APPLICATION_MANAGEMENT_REQUEST_REPLAYED",
    )
    application = await session.get(BotApplication, (application_id, settings.domain))
    actor = await session.get(User, (int(request.actor.id), request.actor.domain))
    if (
        application is None
        or application.status == "deleted"
        or (int(request.team.id), request.team.domain)
        != (application.team_id, application.team_domain)
        or (int(request.bot_user.id), request.bot_user.domain)
        != (application.bot_user_id, application.bot_user_domain)
        or actor is None
        or actor.is_local
        or actor.account_type != "human"
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    member = await session.get(
        DeveloperTeamMember,
        (
            application.team_id,
            application.team_domain,
            actor.id,
            actor.origin_domain,
        ),
    )
    if member is None or member.user_is_local:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_NOT_FOUND"})
    if request.operation not in APPLICATION_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS:
        await require_remote_user_creation_allowed(session, actor)
    return application, actor
