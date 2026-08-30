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
from app.db.bot_models import DeveloperTeam, DeveloperTeamMember
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

DEVELOPER_MANAGEMENT_DEADLINE_SECONDS = MANAGEMENT_RPC_DEADLINE_SECONDS
DEVELOPER_MANAGEMENT_MAX_RESPONSE_BYTES = MANAGEMENT_RPC_MAX_RESPONSE_BYTES

_DEVELOPER_MANAGEMENT_ERRORS = ManagementRPCErrorContract(
    unavailable={
        "code": "FEDERATED_DEVELOPER_MANAGEMENT_UNAVAILABLE",
        "message": (
            "The developer-team authority could not complete that request. Try again shortly."
        ),
    },
    failed={
        "code": "FEDERATED_DEVELOPER_MANAGEMENT_FAILED",
        "message": "The developer-team authority rejected that request.",
    },
    invalid_response={"code": "FEDERATED_DEVELOPER_MANAGEMENT_RESPONSE_INVALID"},
)

DeveloperManagementOperation = Literal[
    "member.list",
    "member.add",
    "member.update",
    "member.remove",
    "application.create",
]

DEVELOPER_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS: frozenset[DeveloperManagementOperation] = (
    frozenset({"member.list"})
)


def developer_management_requires_mutation_admission(
    request: DeveloperManagementRequest,
    actor: User,
) -> bool:
    if request.operation in DEVELOPER_MANAGEMENT_ADMISSION_EXEMPT_OPERATIONS:
        return False
    if request.operation != "member.remove":
        return True
    raw_target = request.payload.get("user_ref")
    if not isinstance(raw_target, str):
        return True
    try:
        target = EntityRef(raw_target).resolve(request.team.domain)
    except ValueError:
        return True
    return target != (actor.id, actor.origin_domain)


_OPERATION_STATUS = {
    "member.list": 200,
    "member.add": 201,
    "member.update": 200,
    "member.remove": 204,
    "application.create": 201,
}


def _invalid_developer_management_response() -> HTTPException:
    return HTTPException(
        status_code=502,
        detail={"code": "FEDERATED_DEVELOPER_MANAGEMENT_RESPONSE_INVALID"},
    )


class DeveloperManagementRef(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: SnowflakeString
    domain: FederationDomain


class DeveloperManagementRequest(UnambiguousInputModel):
    """Short-lived call from a developer's home to a team authority."""

    model_config = ConfigDict(extra="forbid")

    team: DeveloperManagementRef
    actor: DeveloperManagementRef
    requesting_instance: FederationDomain
    request_id: str = Field(pattern=r"^kdtm_[A-Za-z0-9_-]{32}$")
    issued_at: StrictInt = Field(ge=0)
    deadline: StrictInt = Field(ge=1)
    operation: DeveloperManagementOperation
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def bounded_request(self) -> DeveloperManagementRequest:
        validate_management_request_shape(
            self.issued_at,
            self.deadline,
            label="developer-management",
        )
        validate_management_json(
            self.payload,
            label="developer-management payload",
        )
        return self


class DeveloperManagementResult(UnambiguousInputModel):
    """Response bound to the exact request, authority, and operation."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(pattern=r"^kdtm_[A-Za-z0-9_-]{32}$")
    team: DeveloperManagementRef
    operation: DeveloperManagementOperation
    status_code: StrictInt = Field(ge=200, le=299)
    body: Any = None

    @model_validator(mode="after")
    def bounded_body(self) -> DeveloperManagementResult:
        validate_management_json(
            self.body,
            label="developer-management response",
        )
        if self.status_code == 204 and self.body is not None:
            raise ValueError("empty developer-management responses cannot include a body")
        return self


def _qualified_ref(value: object) -> EntityRef:
    if not isinstance(value, str):
        raise _invalid_developer_management_response()
    try:
        ref = EntityRef(value)
    except ValueError:
        raise _invalid_developer_management_response() from None
    if ref.domain is None:
        raise _invalid_developer_management_response()
    return ref


def _require_user_payload(body: dict[str, Any], expected_ref: object | None = None) -> None:
    user = body.get("user")
    if not isinstance(user, dict):
        raise _invalid_developer_management_response()
    ref = _qualified_ref(user.get("ref"))
    if user.get("id") != str(ref.id) or user.get("origin_domain") != ref.domain:
        raise _invalid_developer_management_response()
    if expected_ref is not None and str(ref) != str(_qualified_ref(expected_ref)):
        raise _invalid_developer_management_response()


def _validate_developer_body_binding(
    request: DeveloperManagementRequest,
    result: DeveloperManagementResult,
) -> None:
    if request.operation == "member.list":
        if not isinstance(result.body, list):
            raise _invalid_developer_management_response()
        for item in result.body:
            if not isinstance(item, dict):
                raise _invalid_developer_management_response()
            _require_user_payload(item)
        return
    if request.operation in {"member.add", "member.update"}:
        if not isinstance(result.body, dict):
            raise _invalid_developer_management_response()
        expected_ref: object | None
        if request.operation == "member.add":
            data = request.payload.get("data")
            expected_ref = data.get("user_ref") if isinstance(data, dict) else None
        else:
            expected_ref = request.payload.get("user_ref")
        _require_user_payload(result.body, expected_ref)
        return
    if request.operation == "application.create":
        if not isinstance(result.body, dict):
            raise _invalid_developer_management_response()
        application_ref = _qualified_ref(result.body.get("ref"))
        if (
            application_ref.domain != request.team.domain
            or result.body.get("id") != str(application_ref.id)
            or result.body.get("origin_domain") != application_ref.domain
            or str(_qualified_ref(result.body.get("team_ref")))
            != f"{request.team.id}@{request.team.domain}"
        ):
            raise _invalid_developer_management_response()
        bot = result.body.get("bot_user")
        if not isinstance(bot, dict):
            raise _invalid_developer_management_response()
        bot_ref = _qualified_ref(bot.get("ref"))
        if (
            bot_ref.domain != request.team.domain
            or bot.get("id") != str(bot_ref.id)
            or bot.get("origin_domain") != bot_ref.domain
            or bot.get("bot") is not True
        ):
            raise _invalid_developer_management_response()


def validate_developer_management_result(
    request: DeveloperManagementRequest,
    result: DeveloperManagementResult,
) -> DeveloperManagementResult:
    if result.status_code != _OPERATION_STATUS[result.operation]:
        raise _invalid_developer_management_response()
    if result.operation == "member.list":
        valid_body = isinstance(result.body, list)
    elif result.operation == "member.remove":
        valid_body = result.body is None
    else:
        valid_body = isinstance(result.body, dict)
    if not valid_body:
        raise _invalid_developer_management_response()
    if isinstance(result.body, list) and any(not isinstance(item, dict) for item in result.body):
        raise _invalid_developer_management_response()
    _validate_developer_body_binding(request, result)
    return result


def developer_management_dict_body(result: DeveloperManagementResult) -> dict[str, Any]:
    if not isinstance(result.body, dict):
        raise _invalid_developer_management_response()
    return result.body


def developer_management_list_body(result: DeveloperManagementResult) -> list[Any]:
    if not isinstance(result.body, list):
        raise _invalid_developer_management_response()
    return result.body


def require_developer_management_empty(result: DeveloperManagementResult) -> None:
    if result.body is not None:
        raise _invalid_developer_management_response()


def new_developer_management_request(
    settings: Settings,
    team: DeveloperTeam,
    actor: User,
    operation: DeveloperManagementOperation,
    payload: dict[str, Any] | None = None,
) -> DeveloperManagementRequest:
    issued_at = int(time.time())
    return DeveloperManagementRequest(
        team=DeveloperManagementRef(id=str(team.id), domain=team.origin_domain),
        actor=DeveloperManagementRef(id=str(actor.id), domain=actor.origin_domain),
        requesting_instance=settings.domain,
        request_id=f"kdtm_{secrets.token_urlsafe(24)}",
        issued_at=issued_at,
        deadline=issued_at + DEVELOPER_MANAGEMENT_DEADLINE_SECONDS,
        operation=operation,
        payload=payload or {},
    )


async def remote_management_developer_team(
    session: AsyncSession,
    settings: Settings,
    team_ref: EntityRef,
    actor: User,
) -> DeveloperTeam | None:
    """Return an accessible remote team projection, or ``None`` locally."""

    team_id, team_domain = team_ref.resolve(settings.domain)
    if team_domain == settings.domain:
        return None
    if (
        actor.origin_domain != settings.domain
        or not actor.is_local
        or actor.account_type != "human"
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    team = await session.get(DeveloperTeam, (team_id, team_domain))
    member = await session.get(
        DeveloperTeamMember,
        (team_id, team_domain, actor.id, actor.origin_domain),
    )
    if team is None or member is None or not member.user_is_local:
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    return team


def _result_matches_request(
    result: DeveloperManagementResult,
    request: DeveloperManagementRequest,
) -> bool:
    return (
        result.request_id == request.request_id
        and result.team == request.team
        and result.operation == request.operation
        and result.status_code == _OPERATION_STATUS[request.operation]
    )


async def request_developer_management(
    session: AsyncSession,
    settings: Settings,
    request: DeveloperManagementRequest,
) -> DeveloperManagementResult:
    """Send one typed team-management RPC and preserve public API errors."""

    result = await request_management_rpc(
        session,
        settings,
        authority_domain=request.team.domain,
        path=f"/_kaede/v1/developer-teams/{request.team.id}/management",
        payload=request.model_dump(mode="json"),
        response_model=DeveloperManagementResult,
        response_matches=lambda result: _result_matches_request(result, request),
        label="developer-management",
        errors=_DEVELOPER_MANAGEMENT_ERRORS,
        send=signed_request,
    )
    return validate_developer_management_result(request, result)


async def proxy_remote_developer_management(
    session: AsyncSession,
    settings: Settings,
    team_ref: EntityRef,
    actor: User,
    operation: DeveloperManagementOperation,
    payload: dict[str, Any] | None = None,
) -> DeveloperManagementResult | None:
    team = await remote_management_developer_team(session, settings, team_ref, actor)
    if team is None:
        return None
    request = new_developer_management_request(settings, team, actor, operation, payload)
    return await request_developer_management(session, settings, request)


async def authorize_developer_management_request(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    principal: FederationPrincipal,
    team_id: int,
    request: DeveloperManagementRequest,
) -> tuple[DeveloperTeam, User]:
    """Bind the peer, remote human, local team, and replay token."""

    if (
        principal.origin == settings.domain
        or request.requesting_instance != principal.origin
        or request.actor.domain != principal.origin
        or request.team.domain != settings.domain
        or int(request.team.id) != team_id
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_DEVELOPER_MANAGEMENT_CALLER_MISMATCH"},
        )
    await consume_management_request_once(
        redis,
        settings,
        origin=principal.origin,
        namespace="developer-management",
        request_id=request.request_id,
        issued_at=request.issued_at,
        deadline=request.deadline,
        now=int(time.time()),
        expired_code="KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_EXPIRED",
        replayed_code="KAED_FED_DEVELOPER_MANAGEMENT_REQUEST_REPLAYED",
    )
    team = await session.get(DeveloperTeam, (team_id, settings.domain))
    actor = await session.get(User, (int(request.actor.id), request.actor.domain))
    if (
        team is None
        or actor is None
        or actor.is_local
        or actor.account_type != "human"
        or actor.disabled_at is not None
    ):
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    member = await session.get(
        DeveloperTeamMember,
        (team.id, team.origin_domain, actor.id, actor.origin_domain),
    )
    if member is None or member.user_is_local:
        raise HTTPException(status_code=404, detail={"code": "DEVELOPER_TEAM_NOT_FOUND"})
    if developer_management_requires_mutation_admission(request, actor):
        await require_remote_user_creation_allowed(session, actor)
    return team, actor
