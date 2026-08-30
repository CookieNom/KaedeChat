from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from pydantic import ConfigDict, Field, StrictInt, ValidationError
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
from app.federation.application_management import (
    ApplicationManagementRequest,
    ApplicationManagementResult,
    authorize_application_management_request,
)
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(tags=["application management federation"])


class _StrictModel(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")


class _Empty(_StrictModel):
    pass


class _Data(_StrictModel):
    data: dict[str, Any]


class _Resource(_StrictModel):
    resource_id: StrictInt = Field(ge=0)


class _ResourceData(_Data):
    resource_id: StrictInt = Field(ge=0)


class _Guild(_StrictModel):
    guild_ref: str = Field(min_length=1, max_length=320)


class _GuildData(_Guild):
    data: dict[str, Any]


class _Domain(_StrictModel):
    target_domain: str = Field(min_length=1, max_length=253)


class _DomainData(_Domain):
    data: dict[str, Any]


def _application_ref(application_id: int) -> EntityRef:
    return EntityRef(str(application_id))


def _result(
    request: ApplicationManagementRequest,
    body: object,
    *,
    status_code: int = 200,
) -> ApplicationManagementResult:
    return ApplicationManagementResult(
        request_id=request.request_id,
        application=request.application,
        operation=request.operation,
        status_code=status_code,
        body=body,
    )


async def _dispatch_application_and_credentials(
    request: ApplicationManagementRequest,
    actor: object,
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> ApplicationManagementResult:
    from app.api.applications import (
        ApplicationPatch,
        CredentialCreate,
        create_credential,
        get_application,
        get_application_directory_preview,
        list_credentials,
        patch_application,
        revoke_credential,
    )

    ref = _application_ref(int(request.application.id))
    auth = _auth(actor)
    if request.operation == "application.get":
        _Empty.model_validate(request.payload)
        return _result(request, await get_application(ref, auth, session, settings))
    if request.operation == "application.directory_preview":
        _Empty.model_validate(request.payload)
        return _result(
            request,
            await get_application_directory_preview(ref, auth, session, settings),
        )
    if request.operation == "application.update":
        update = _Data.model_validate(request.payload)
        body: object = await patch_application(
            ref,
            ApplicationPatch.model_validate(update.data),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    if request.operation == "credential.create":
        create = _Data.model_validate(request.payload)
        body = await create_credential(
            ref,
            CredentialCreate.model_validate(create.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "credential.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_credentials(ref, auth, session, settings))
    if request.operation == "credential.revoke":
        revoke = _Resource.model_validate(request.payload)
        await revoke_credential(ref, revoke.resource_id, auth, session, settings)
        return _result(request, None, status_code=204)
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


async def _dispatch_workers_and_commands(
    request: ApplicationManagementRequest,
    actor: object,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> ApplicationManagementResult:
    from app.api.applications import (
        CommandsPut,
        WorkerCreate,
        create_worker,
        get_commands,
        get_guild_commands,
        list_workers,
        put_commands,
        put_guild_commands,
        revoke_worker,
    )

    ref = _application_ref(int(request.application.id))
    auth = _auth(actor)
    if request.operation == "worker.create":
        worker_create = _Data.model_validate(request.payload)
        body: object = await create_worker(
            ref,
            WorkerCreate.model_validate(worker_create.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "worker.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_workers(ref, auth, session, settings))
    if request.operation == "worker.revoke":
        worker_revoke = _Resource.model_validate(request.payload)
        await revoke_worker(
            ref,
            worker_revoke.resource_id,
            auth,
            session,
            redis,
            settings,
        )
        return _result(request, None, status_code=204)
    if request.operation == "command.replace":
        command_replace = _Data.model_validate(request.payload)
        body = await put_commands(
            ref,
            CommandsPut.model_validate(command_replace.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body)
    if request.operation == "command.list":
        _Empty.model_validate(request.payload)
        return _result(request, await get_commands(ref, auth, session, settings))
    if request.operation == "guild_command.replace":
        guild_replace = _GuildData.model_validate(request.payload)
        body = await put_guild_commands(
            ref,
            EntityRef(guild_replace.guild_ref),
            CommandsPut.model_validate(guild_replace.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body)
    if request.operation == "guild_command.list":
        guild_list = _Guild.model_validate(request.payload)
        body = await get_guild_commands(
            ref,
            EntityRef(guild_list.guild_ref),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


async def _dispatch_policy_and_inventory(
    request: ApplicationManagementRequest,
    actor: object,
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> ApplicationManagementResult:
    from app.api.applications import (
        InstanceRulePut,
        TemplateCreate,
        create_template,
        delete_instance_rule,
        list_installations,
        list_instance_rules,
        list_templates,
        put_instance_rule,
    )

    ref = _application_ref(int(request.application.id))
    auth = _auth(actor)
    if request.operation == "template.create":
        template_create = _Data.model_validate(request.payload)
        body: object = await create_template(
            ref,
            TemplateCreate.model_validate(template_create.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "template.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_templates(ref, auth, session, settings))
    if request.operation == "instance_rule.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_instance_rules(ref, auth, session, settings))
    if request.operation == "instance_rule.put":
        rule_put = _DomainData.model_validate(request.payload)
        body = await put_instance_rule(
            ref,
            rule_put.target_domain,
            InstanceRulePut.model_validate(rule_put.data),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    if request.operation == "instance_rule.delete":
        rule_delete = _Domain.model_validate(request.payload)
        await delete_instance_rule(ref, rule_delete.target_domain, auth, session, settings)
        return _result(request, None, status_code=204)
    if request.operation == "installation.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_installations(ref, auth, session, settings))
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


async def _dispatch_assets(
    request: ApplicationManagementRequest,
    actor: object,
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> ApplicationManagementResult:
    from app.api.application_assets import (
        ApplicationAssetCommit,
        ApplicationAssetPatch,
        create_application_asset,
        create_application_asset_ticket,
        delete_application_asset,
        get_application_asset,
        list_application_assets,
        patch_application_asset,
    )
    from app.media.schemas import UploadTicketRequest

    ref = _application_ref(int(request.application.id))
    auth = _auth(actor)
    if request.operation == "asset.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_application_assets(ref, auth, session, settings))
    if request.operation == "asset.ticket":
        ticket = _Data.model_validate(request.payload)
        body: object = await create_application_asset_ticket(
            ref,
            UploadTicketRequest.model_validate(ticket.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "asset.get":
        get_asset = _Resource.model_validate(request.payload)
        body = await get_application_asset(ref, get_asset.resource_id, auth, session, settings)
        return _result(request, body)
    if request.operation == "asset.update":
        update_asset = _ResourceData.model_validate(request.payload)
        body = await patch_application_asset(
            ref,
            update_asset.resource_id,
            ApplicationAssetPatch.model_validate(update_asset.data),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    if request.operation == "asset.create":
        create_asset = _Data.model_validate(request.payload)
        response = Response()
        body = await create_application_asset(
            ref,
            ApplicationAssetCommit.model_validate(create_asset.data),
            response,
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(
            request,
            body,
            status_code=202 if response.status_code == 202 else 201,
        )
    if request.operation == "asset.delete":
        delete_asset = _Resource.model_validate(request.payload)
        await delete_application_asset(ref, delete_asset.resource_id, auth, session, settings)
        return _result(request, None, status_code=204)
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


async def _dispatch_emojis(
    request: ApplicationManagementRequest,
    actor: object,
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    settings: Settings,
) -> ApplicationManagementResult:
    from app.api.application_assets import (
        ApplicationEmojiCommit,
        ApplicationEmojiPatch,
        create_application_emoji,
        create_application_emoji_ticket,
        delete_application_emoji,
        get_application_emoji,
        list_application_emojis,
        patch_application_emoji,
    )
    from app.media.schemas import UploadTicketRequest

    ref = _application_ref(int(request.application.id))
    auth = _auth(actor)
    if request.operation == "emoji.list":
        _Empty.model_validate(request.payload)
        return _result(request, await list_application_emojis(ref, auth, session, settings))
    if request.operation == "emoji.ticket":
        ticket = _Data.model_validate(request.payload)
        body: object = await create_application_emoji_ticket(
            ref,
            UploadTicketRequest.model_validate(ticket.data),
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(request, body, status_code=201)
    if request.operation == "emoji.get":
        get_emoji = _Resource.model_validate(request.payload)
        body = await get_application_emoji(ref, get_emoji.resource_id, auth, session, settings)
        return _result(request, body)
    if request.operation == "emoji.update":
        update_emoji = _ResourceData.model_validate(request.payload)
        body = await patch_application_emoji(
            ref,
            update_emoji.resource_id,
            ApplicationEmojiPatch.model_validate(update_emoji.data),
            auth,
            session,
            settings,
        )
        return _result(request, body)
    if request.operation == "emoji.create":
        create_emoji = _Data.model_validate(request.payload)
        response = Response()
        body = await create_application_emoji(
            ref,
            ApplicationEmojiCommit.model_validate(create_emoji.data),
            response,
            auth,
            session,
            snowflake,
            settings,
        )
        return _result(
            request,
            body,
            status_code=202 if response.status_code == 202 else 201,
        )
    if request.operation == "emoji.delete":
        delete_emoji = _Resource.model_validate(request.payload)
        await delete_application_emoji(ref, delete_emoji.resource_id, auth, session, settings)
        return _result(request, None, status_code=204)
    raise HTTPException(status_code=400, detail={"code": "KAED_FED_BAD_REQUEST"})


@router.post("/_kaede/v1/applications/{application_id}/management")
async def application_management_authority(
    application_id: int,
    request: ApplicationManagementRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "application-management",
        capacity=240,
        refill_per_minute=240,
    )
    _, actor = await authorize_application_management_request(
        session,
        redis,
        settings,
        principal,
        application_id,
        request,
    )
    try:
        if request.operation.startswith(("application.", "credential.")):
            result = await _dispatch_application_and_credentials(
                request, actor, session, snowflake, settings
            )
        elif request.operation.startswith(("worker.", "command.", "guild_command.")):
            result = await _dispatch_workers_and_commands(
                request,
                actor,
                session,
                redis,
                snowflake,
                settings,
            )
        elif request.operation.startswith(("template.", "instance_rule.", "installation.")):
            result = await _dispatch_policy_and_inventory(
                request, actor, session, snowflake, settings
            )
        elif request.operation.startswith("asset."):
            result = await _dispatch_assets(request, actor, session, snowflake, settings)
        elif request.operation.startswith("emoji."):
            result = await _dispatch_emojis(request, actor, session, snowflake, settings)
        else:
            raise HTTPException(
                status_code=400,
                detail={"code": "KAED_FED_APPLICATION_MANAGEMENT_OPERATION_UNSUPPORTED"},
            )
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    return result.model_dump(mode="json")
