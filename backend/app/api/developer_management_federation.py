from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ConfigDict, Field, ValidationError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    federated_authenticated_user as _auth,
)
from app.api.dependencies import (
    get_redis,
    get_session,
    get_snowflake,
)
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import DeveloperTeam
from app.db.models import User
from app.federation.developer_management import (
    DeveloperManagementRequest,
    DeveloperManagementResult,
    authorize_developer_management_request,
)
from app.federation.replication import resolve_delegated_profile
from app.federation.schemas import RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(tags=["developer management federation"])


class _StrictModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class _Empty(_StrictModel):
    pass


class _Data(_StrictModel):
    data: dict[str, Any]


class _MemberAdd(_Data):
    target: RemoteUserProfile


class _MemberRef(_StrictModel):
    user_ref: str = Field(min_length=1, max_length=320)


class _MemberUpdate(_MemberRef):
    data: dict[str, Any]


def _team_ref(team: DeveloperTeam) -> EntityRef:
    return EntityRef(str(team.id))


def _result(
    request: DeveloperManagementRequest,
    body: object,
    *,
    status_code: int = 200,
) -> DeveloperManagementResult:
    return DeveloperManagementResult(
        request_id=request.request_id,
        team=request.team,
        operation=request.operation,
        status_code=status_code,
        body=body,
    )


async def _dispatch_member_management(
    request: DeveloperManagementRequest,
    team: DeveloperTeam,
    actor: User,
    session: AsyncSession,
    settings: Settings,
) -> DeveloperManagementResult:
    from app.api.applications import (
        DeveloperTeamMemberPatch,
        DeveloperTeamMemberPut,
        add_developer_team_member,
        list_developer_team_members,
        patch_developer_team_member,
        remove_developer_team_member,
    )

    ref = _team_ref(team)
    auth = _auth(actor)
    if request.operation == "member.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_developer_team_members(ref, auth, session, settings))
    if request.operation == "member.add":
        create = _MemberAdd.model_validate(request.payload)
        member_payload = DeveloperTeamMemberPut.model_validate(create.data)
        target_ref = member_payload.user_ref.resolve(settings.domain)
        if target_ref != (int(create.target.id), create.target.origin_domain):
            raise HTTPException(
                status_code=403,
                detail={"code": "KAED_FED_DEVELOPER_MANAGEMENT_MEMBER_MISMATCH"},
            )
        await resolve_delegated_profile(
            session,
            settings,
            create.target,
            authority_origin=actor.origin_domain,
        )
        body = await add_developer_team_member(
            ref,
            member_payload,
            auth,
            session,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "member.update":
        update = _MemberUpdate.model_validate(request.payload)
        body = await patch_developer_team_member(
            ref,
            EntityRef(update.user_ref),
            DeveloperTeamMemberPatch.model_validate(update.data),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    if request.operation == "member.remove":
        removal = _MemberRef.model_validate(request.payload)
        await remove_developer_team_member(
            ref,
            EntityRef(removal.user_ref),
            auth,
            session,
            settings,
        )
        return _result(request, None, status_code=204)
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


async def _dispatch_application_create(
    request: DeveloperManagementRequest,
    team: DeveloperTeam,
    actor: User,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> DeveloperManagementResult:
    from app.api.applications import ApplicationCreate, create_application

    create = _Data.model_validate(request.payload)
    payload = ApplicationCreate.model_validate(create.data)
    expected_team_ref = (team.id, team.origin_domain)
    if (
        payload.team_ref is not None
        and payload.team_ref.resolve(settings.domain) != expected_team_ref
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "KAED_FED_DEVELOPER_MANAGEMENT_TEAM_MISMATCH"},
        )
    payload = payload.model_copy(update={"team_ref": _team_ref(team)})
    body = await create_application(
        payload,
        Response(),
        _auth(actor),
        session,
        redis,
        snowflake,
        settings,
    )
    return _result(request, body, status_code=201)


@router.post("/_kaede/v1/developer-teams/{team_id}/management")
async def developer_management_authority(
    team_id: int,
    request: DeveloperManagementRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "developer-management",
        capacity=120,
        refill_per_minute=120,
    )
    team, actor = await authorize_developer_management_request(
        session,
        redis,
        settings,
        principal,
        team_id,
        request,
    )
    try:
        if request.operation.startswith("member."):
            result = await _dispatch_member_management(request, team, actor, session, settings)
        elif request.operation == "application.create":
            result = await _dispatch_application_create(
                request,
                team,
                actor,
                session,
                redis,
                snowflake,
                settings,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail={"code": "KAED_FED_DEVELOPER_MANAGEMENT_OPERATION_UNSUPPORTED"},
            )
    except ValidationError as exc:
        # Nested operation payloads are deliberately opaque at the outer wire
        # model, then validated against the exact public endpoint schema here.
        # Convert that manual validation failure back into FastAPI's ordinary
        # 422 contract instead of leaking it as an internal error.
        raise RequestValidationError(exc.errors()) from exc
    return result.model_dump(mode="json")
