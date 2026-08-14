from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_redis, get_session
from app.chat.payloads import user_payload
from app.core.settings import Settings, get_settings
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotInstallTemplate,
    BotInstanceRule,
    BotWorker,
    DeveloperTeam,
)
from app.db.models import User
from app.federation.client import signed_request
from app.federation.events import build_envelope
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    normalize_domain,
)
from app.federation.replication import upsert_remote_user
from app.federation.schemas import RemoteUserProfile
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
    validated_event_envelope,
)

router = APIRouter(tags=["bot federation"])
BOT_MANIFEST_CAPABILITY = "bot-direct-auth/1"
BOT_MANIFEST_EVENT = "bot.application.manifest"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestApplication(StrictModel):
    id: str
    origin_domain: str = Field(min_length=1, max_length=253)
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    icon_hash: str | None = Field(default=None, max_length=128)
    support_url: str | None = Field(default=None, max_length=2048)
    privacy_url: str | None = Field(default=None, max_length=2048)
    status: Literal["active"]
    target_policy: Literal["open", "allowlist", "blocklist", "local_only"]
    default_scopes: list[str] = Field(max_length=64)
    default_intents: list[str] = Field(max_length=32)
    default_permissions: str
    e2ee_modes: list[Literal["interaction_only", "participant"]] = Field(max_length=2)
    manifest_generation: str
    command_generation: str
    bot_user: RemoteUserProfile


class ManifestTemplate(StrictModel):
    id: str
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    permissions: str
    contexts: list[Literal["guild"]] = Field(max_length=1)
    e2ee_mode: Literal["disabled", "interaction_only", "participant"]
    generation: str


class ManifestWorker(StrictModel):
    id: str
    name: str = Field(min_length=1, max_length=100)
    public_key: str = Field(min_length=43, max_length=44)
    scopes: list[str] = Field(max_length=64)
    intents: list[str] = Field(max_length=32)
    target_domains: list[str] = Field(max_length=100)
    generation: str
    expires_at: str | None = None


class ManifestCommand(StrictModel):
    id: str
    name: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    type: Literal["chat_input", "user", "message"] = "chat_input"
    description: str = Field(default="", max_length=100)
    default_member_permissions: list[str] = Field(default_factory=list, max_length=64)
    contexts: list[Literal["guild"]] = Field(max_length=1)
    options: list[dict[str, object]] = Field(default_factory=list, max_length=25)


class WorkerAuthorization(StrictModel):
    application_id: str
    application_domain: str = Field(min_length=1, max_length=253)
    bot_user_id: str
    worker: ManifestWorker
    manifest_generation: str
    revocation_generation: str


def _target_policy_allows(policy: str, rules: dict[str, str], target_domain: str) -> bool:
    if policy == "local_only" or rules.get(target_domain) == "deny":
        return False
    return policy != "allowlist" or rules.get(target_domain) == "allow"


class BotManifest(StrictModel):
    application: ManifestApplication
    template: ManifestTemplate
    workers: list[ManifestWorker] = Field(max_length=100)
    commands: list[ManifestCommand] = Field(max_length=100)


async def local_manifest(
    session: AsyncSession,
    application_id: int,
    template_slug: str,
    settings: Settings,
) -> tuple[BotManifest, User]:
    row = (
        await session.execute(
            select(BotApplication, BotInstallTemplate, User)
            .join(
                BotInstallTemplate,
                (BotInstallTemplate.application_id == BotApplication.id)
                & (BotInstallTemplate.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
                BotInstallTemplate.slug == template_slug,
                BotInstallTemplate.active.is_(True),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    application, template, bot = row
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
            )
            .order_by(BotWorker.id)
            .limit(100)
        )
    )
    commands = list(
        await session.scalars(
            select(ApplicationCommand)
            .where(
                ApplicationCommand.application_id == application.id,
                ApplicationCommand.application_domain == application.origin_domain,
                ApplicationCommand.guild_id.is_(None),
                ApplicationCommand.state == "active",
            )
            .order_by(ApplicationCommand.type, ApplicationCommand.name)
            .limit(100)
        )
    )
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": str(application.id),
                "origin_domain": application.origin_domain,
                "name": application.name,
                "description": application.description,
                "icon_hash": application.icon_hash,
                "support_url": application.support_url,
                "privacy_url": application.privacy_url,
                "status": "active",
                "target_policy": application.target_policy,
                "default_scopes": application.default_scopes,
                "default_intents": application.default_intents,
                "default_permissions": str(application.default_permissions),
                "e2ee_modes": application.e2ee_modes,
                "manifest_generation": str(application.manifest_generation),
                "command_generation": str(application.command_generation),
                "bot_user": user_payload(bot),
            },
            "template": {
                "id": str(template.id),
                "slug": template.slug,
                "name": template.name,
                "description": template.description,
                "scopes": template.scopes,
                "intents": template.intents,
                "permissions": str(template.permissions),
                "contexts": template.contexts,
                "e2ee_mode": template.e2ee_mode,
                "generation": str(template.generation),
            },
            "workers": [
                {
                    "id": str(worker.id),
                    "name": worker.name,
                    "public_key": base64.urlsafe_b64encode(worker.public_key)
                    .decode("ascii")
                    .rstrip("="),
                    "scopes": worker.scopes,
                    "intents": worker.intents,
                    "target_domains": worker.target_domains,
                    "generation": str(worker.generation),
                    "expires_at": worker.expires_at.isoformat() if worker.expires_at else None,
                }
                for worker in workers
            ],
            "commands": [{"id": str(command.id), **command.definition} for command in commands],
        }
    )
    return manifest, bot


@router.get("/_kaede/v1/applications/{application_id}/manifest")
async def federation_bot_manifest(
    application_id: int,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
    template: Annotated[str, Query(min_length=2, max_length=64)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-manifest",
        capacity=300,
        refill_per_minute=300,
    )
    manifest, bot = await local_manifest(session, application_id, template, settings)
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application_id,
                BotInstanceRule.application_domain == settings.domain,
            )
        )
    }
    policy = manifest.application.target_policy
    if not _target_policy_allows(policy, rules, principal.origin):
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    return await build_envelope(
        session,
        settings,
        BOT_MANIFEST_EVENT,
        bot,
        manifest.model_dump(mode="json"),
    )


@router.get("/_kaede/v1/applications/{application_id}/workers/{worker_id}/authorization")
async def federation_worker_authorization(
    application_id: int,
    worker_id: int,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    if principal.silenced:
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    await enforce_federation_route_rate_limit(
        redis, principal.origin, "bot-worker-authorization", capacity=600, refill_per_minute=600
    )
    row = (
        await session.execute(
            select(BotApplication, BotWorker, User)
            .join(
                BotWorker,
                (BotWorker.application_id == BotApplication.id)
                & (BotWorker.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplication.id == application_id,
                BotApplication.origin_domain == settings.domain,
                BotApplication.status == "active",
                BotWorker.id == worker_id,
                BotWorker.revoked_at.is_(None),
                (BotWorker.expires_at.is_(None)) | (BotWorker.expires_at > datetime.now(UTC)),
            )
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    application, worker, bot = row
    rules = {
        rule.target_domain: rule.effect
        for rule in await session.scalars(
            select(BotInstanceRule).where(
                BotInstanceRule.application_id == application.id,
                BotInstanceRule.application_domain == application.origin_domain,
            )
        )
    }
    if not _target_policy_allows(application.target_policy, rules, principal.origin) or (
        worker.target_domains and principal.origin not in worker.target_domains
    ):
        raise HTTPException(status_code=404, detail={"code": "BOT_WORKER_NOT_FOUND"})
    authorization = WorkerAuthorization.model_validate(
        {
            "application_id": str(application.id),
            "application_domain": application.origin_domain,
            "bot_user_id": str(bot.id),
            "worker": {
                "id": str(worker.id),
                "name": worker.name,
                "public_key": base64.urlsafe_b64encode(worker.public_key)
                .decode("ascii")
                .rstrip("="),
                "scopes": worker.scopes,
                "intents": worker.intents,
                "target_domains": worker.target_domains,
                "generation": str(worker.generation),
                "expires_at": worker.expires_at.isoformat() if worker.expires_at else None,
            },
            "manifest_generation": str(application.manifest_generation),
            "revocation_generation": str(application.revocation_generation),
        }
    )
    return await build_envelope(
        session, settings, "bot.worker.authorization", bot, authorization.model_dump(mode="json")
    )


async def refresh_remote_worker_authorization(
    session: AsyncSession,
    settings: Settings,
    application_id: int,
    application_domain: str,
    worker_id: int,
) -> None:
    application_domain = normalize_domain(application_domain)
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/workers/{worker_id}/authorization",
        request_timeout=8,
        max_response_bytes=64 * 1024,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=401, detail={"code": "BOT_ASSERTION_INVALID"})
    if response.status_code != 200:
        raise FederationNetworkError("remote worker authorization failed")
    raw = decode_federation_response_json(response)
    try:
        envelope = await validated_event_envelope(session, settings, application_domain, raw)
        if envelope.type != "bot.worker.authorization":
            raise ValueError("worker authorization has the wrong type")
        authorization = WorkerAuthorization.model_validate(envelope.content)
        remote_worker = authorization.worker
        if (
            int(authorization.application_id) != application_id
            or authorization.application_domain != application_domain
            or int(authorization.bot_user_id) != int(envelope.actor.id)
            or envelope.actor.domain != application_domain
            or int(remote_worker.id) != worker_id
        ):
            raise ValueError("worker authorization identity mismatch")
        public_key = base64.b64decode(
            remote_worker.public_key + "=" * (-len(remote_worker.public_key) % 4),
            altchars=b"-_",
            validate=True,
        )
        if len(public_key) != 32:
            raise ValueError("worker public key length is invalid")
        application = await session.get(BotApplication, (application_id, application_domain))
        worker = await session.get(BotWorker, worker_id)
        if application is None:
            raise ValueError("worker authorization has no installed application")
        if worker is not None and (worker.application_id, worker.application_domain) != (
            application_id,
            application_domain,
        ):
            raise ValueError("worker authorization reuses a worker identity")
        if worker is None:
            worker = BotWorker(
                id=worker_id,
                application_id=application_id,
                application_domain=application_domain,
                name=remote_worker.name,
                public_key=public_key,
            )
            session.add(worker)
        application.status = "active"
        application.manifest_generation = int(authorization.manifest_generation)
        application.revocation_generation = int(authorization.revocation_generation)
        worker.name = remote_worker.name
        worker.public_key = public_key
        worker.scopes = remote_worker.scopes
        worker.intents = remote_worker.intents
        worker.target_domains = remote_worker.target_domains
        worker.generation = int(remote_worker.generation)
        worker.expires_at = (
            datetime.fromisoformat(remote_worker.expires_at)
            if remote_worker.expires_at is not None
            else None
        )
        worker.revoked_at = None
        await session.flush()
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote worker authorization is invalid") from exc


async def fetch_bot_manifest(
    session: AsyncSession,
    settings: Settings,
    application_id: int,
    application_domain: str,
    template_slug: str,
) -> BotManifest:
    application_domain = normalize_domain(application_domain)
    response = await signed_request(
        session,
        settings,
        "GET",
        application_domain,
        f"/_kaede/v1/applications/{application_id}/manifest",
        query={"template": template_slug},
        request_timeout=8,
        max_response_bytes=512 * 1024,
    )
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "BOT_INVITE_NOT_FOUND"})
    if response.status_code != 200:
        raise FederationNetworkError("remote bot manifest request failed")
    raw = decode_federation_response_json(response)
    try:
        envelope = await validated_event_envelope(session, settings, application_domain, raw)
        if envelope.type != BOT_MANIFEST_EVENT:
            raise ValueError("manifest envelope has the wrong type")
        manifest = BotManifest.model_validate(envelope.content)
        if (
            int(manifest.application.id) != application_id
            or manifest.application.origin_domain != application_domain
            or int(manifest.application.bot_user.id) != int(envelope.actor.id)
            or manifest.application.bot_user.origin_domain != envelope.actor.domain
            or manifest.template.slug != template_slug
        ):
            raise ValueError("manifest identity does not match the request")
        return manifest
    except (TypeError, ValueError) as exc:
        raise FederationNetworkError("remote bot manifest is invalid") from exc


async def materialize_remote_manifest(
    session: AsyncSession,
    manifest: BotManifest,
    settings: Settings,
) -> tuple[BotApplication, BotInstallTemplate, User]:
    app_id = int(manifest.application.id)
    domain = normalize_domain(manifest.application.origin_domain)
    if domain == settings.domain:
        raise ValueError("remote manifest materializer received a local application")
    profile = manifest.application.bot_user
    bot_id = int(profile.id)
    existing_bot = await session.get(User, (bot_id, domain))
    if existing_bot is not None and existing_bot.account_type != "bot":
        raise FederationNetworkError("bot manifest reuses a human identity")
    bot = await upsert_remote_user(session, settings, profile)
    bot.account_type = "bot"
    await session.flush()
    team = await session.get(DeveloperTeam, (app_id, domain))
    if team is None:
        team = DeveloperTeam(
            id=app_id,
            origin_domain=domain,
            name=f"Remote developer · {domain}",
            personal=False,
        )
        session.add(team)
        await session.flush()
    application = await session.get(BotApplication, (app_id, domain))
    if application is None:
        application = BotApplication(
            id=app_id,
            origin_domain=domain,
            team_id=team.id,
            team_domain=team.origin_domain,
            bot_user_id=bot.id,
            bot_user_domain=bot.origin_domain,
            name=manifest.application.name,
        )
        session.add(application)
    application.name = manifest.application.name
    application.description = manifest.application.description
    application.icon_hash = manifest.application.icon_hash
    application.support_url = manifest.application.support_url
    application.privacy_url = manifest.application.privacy_url
    application.status = "active"
    application.target_policy = manifest.application.target_policy
    application.default_scopes = manifest.application.default_scopes
    application.default_intents = manifest.application.default_intents
    application.default_permissions = int(manifest.application.default_permissions)
    application.e2ee_modes = list[str](manifest.application.e2ee_modes)
    application.manifest_generation = int(manifest.application.manifest_generation)
    application.command_generation = int(manifest.application.command_generation)
    await session.flush()
    template_id = int(manifest.template.id)
    template = await session.get(BotInstallTemplate, template_id)
    if template is not None and (template.application_id, template.application_domain) != (
        app_id,
        domain,
    ):
        raise FederationNetworkError("bot manifest reuses a template identity")
    if template is None:
        template = BotInstallTemplate(
            id=template_id,
            application_id=app_id,
            application_domain=domain,
            slug=manifest.template.slug,
            name=manifest.template.name,
        )
        session.add(template)
    template.name = manifest.template.name
    template.description = manifest.template.description
    template.scopes = manifest.template.scopes
    template.intents = manifest.template.intents
    template.permissions = int(manifest.template.permissions)
    template.contexts = list[str](manifest.template.contexts)
    template.e2ee_mode = manifest.template.e2ee_mode
    template.generation = int(manifest.template.generation)
    template.active = True
    command_ids: list[int] = []
    for remote_command in manifest.commands:
        command_id = int(remote_command.id)
        existing_command = await session.get(ApplicationCommand, command_id)
        if existing_command is not None and (
            existing_command.application_id,
            existing_command.application_domain,
        ) != (app_id, domain):
            raise FederationNetworkError("bot manifest reuses a command identity")
        command_ids.append(command_id)
        definition = remote_command.model_dump(mode="json", exclude={"id"})
        if existing_command is None:
            existing_command = ApplicationCommand(
                id=command_id,
                application_id=app_id,
                application_domain=domain,
                name=remote_command.name,
                type=remote_command.type,
                definition=definition,
                generation=int(manifest.application.command_generation),
                state="active",
            )
            session.add(existing_command)
        else:
            existing_command.name = remote_command.name
            existing_command.type = remote_command.type
            existing_command.definition = definition
            existing_command.generation = int(manifest.application.command_generation)
            existing_command.state = "active"
    for existing_command in await session.scalars(
        select(ApplicationCommand).where(
            ApplicationCommand.application_id == app_id,
            ApplicationCommand.application_domain == domain,
            ApplicationCommand.guild_id.is_(None),
        )
    ):
        if existing_command.id not in command_ids:
            existing_command.state = "superseded"
    worker_ids: list[int] = []
    for remote_worker in manifest.workers:
        worker_id = int(remote_worker.id)
        worker_ids.append(worker_id)
        try:
            public_key = base64.b64decode(
                remote_worker.public_key + "=" * (-len(remote_worker.public_key) % 4),
                altchars=b"-_",
                validate=True,
            )
        except ValueError as exc:
            raise FederationNetworkError("bot worker public key is invalid") from exc
        if len(public_key) != 32:
            raise FederationNetworkError("bot worker public key length is invalid")
        existing_worker = await session.get(BotWorker, worker_id)
        if existing_worker is not None and (
            existing_worker.application_id,
            existing_worker.application_domain,
        ) != (app_id, domain):
            raise FederationNetworkError("bot manifest reuses a worker identity")
        if existing_worker is None:
            existing_worker = BotWorker(
                id=worker_id,
                application_id=app_id,
                application_domain=domain,
                name=remote_worker.name,
                public_key=public_key,
            )
            session.add(existing_worker)
        existing_worker.name = remote_worker.name
        existing_worker.public_key = public_key
        existing_worker.scopes = remote_worker.scopes
        existing_worker.intents = remote_worker.intents
        existing_worker.target_domains = remote_worker.target_domains
        existing_worker.generation = int(remote_worker.generation)
        existing_worker.revoked_at = None
    if worker_ids:
        for worker in await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == app_id,
                BotWorker.application_domain == domain,
                BotWorker.id.not_in(worker_ids),
                BotWorker.revoked_at.is_(None),
            )
            .with_for_update()
        ):
            worker.revoked_at = datetime.now(UTC)
    return application, template, bot
