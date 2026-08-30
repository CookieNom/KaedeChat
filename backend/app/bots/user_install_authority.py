from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.install_config import (
    REQUIRED_USER_INSTALL_SCOPES,
    USER_INSTALL,
    USER_INSTALL_CONTEXTS,
    USER_INSTALL_SCOPES,
)
from app.bots.target_discovery import require_application_runtime_enabled
from app.core.bot_intents import SUPPORTED_BOT_INTENTS
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, BotInstanceRule, BotUserInstallation
from app.db.models import User
from app.federation.network import FederationNetworkError

USER_INSTALLATION_AUTHORITY_LEASE = timedelta(minutes=20)
MAX_USER_INSTALLATION_AUTHORITY_CLOCK_SKEW = timedelta(minutes=15)
RemoteUserApplicationRefresher = Callable[
    [AsyncSession, Settings, SnowflakeGenerator, int, str],
    Awaitable[None],
]


class FederatedUserInstallationGrant(UnambiguousInputModel):
    """Fresh grant projection signed by the installing user's home request."""

    id: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    application_ref: EntityRef
    scopes: list[str] = Field(min_length=1, max_length=4)
    intents: list[str] = Field(min_length=1, max_length=32)
    contexts: list[str] = Field(min_length=1, max_length=3)
    grant_revision: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    authority_expires_at: datetime

    @model_validator(mode="after")
    def valid_grant(self) -> FederatedUserInstallationGrant:
        if len(self.scopes) != len(set(self.scopes)):
            raise ValueError("user-install scopes must be unique")
        if len(self.intents) != len(set(self.intents)):
            raise ValueError("user-install intents must be unique")
        if len(self.contexts) != len(set(self.contexts)):
            raise ValueError("user-install contexts must be unique")
        if not set(self.scopes) >= REQUIRED_USER_INSTALL_SCOPES:
            raise ValueError("user-install grant is missing required scopes")
        if not set(self.scopes) <= USER_INSTALL_SCOPES:
            raise ValueError("user-install grant contains an unsupported scope")
        if not set(self.contexts) <= USER_INSTALL_CONTEXTS:
            raise ValueError("user-install grant contains an unsupported context")
        if not set(self.intents) <= SUPPORTED_BOT_INTENTS or "interactions" not in self.intents:
            raise ValueError("user-install grant does not authorize interactions")
        if self.authority_expires_at.tzinfo is None:
            raise ValueError("user-install authority expiry must include a timezone")
        return self


def require_user_install_policy(
    application: BotApplication,
    scopes: list[str],
    intents: list[str],
    contexts: list[str],
) -> None:
    if USER_INSTALL not in application.supported_install_types:
        raise HTTPException(
            status_code=409,
            detail={"code": "APPLICATION_USER_INSTALL_UNAVAILABLE"},
        )
    if not set(scopes) <= set(application.user_install_scopes) or not set(scopes) <= set(
        application.default_scopes
    ):
        raise HTTPException(status_code=403, detail={"code": "APPLICATION_SCOPE_NOT_INSTALLABLE"})
    if not set(intents) <= set(application.default_intents):
        raise HTTPException(status_code=403, detail={"code": "APPLICATION_INTENT_NOT_INSTALLABLE"})
    if not set(contexts) <= set(application.user_install_contexts):
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_CONTEXT_NOT_INSTALLABLE"},
        )


async def require_user_install_target(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
) -> None:
    if application.origin_domain == settings.domain:
        return
    rule = await session.get(
        BotInstanceRule,
        (application.id, application.origin_domain, settings.domain),
    )
    if (
        application.target_policy == "local_only"
        or (rule is not None and rule.effect == "deny")
        or (application.target_policy == "allowlist" and rule is None)
    ):
        raise HTTPException(status_code=403, detail={"code": "APPLICATION_TARGET_NOT_ALLOWED"})


async def require_federated_user_application(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    application_ref: tuple[int, str],
    grant: FederatedUserInstallationGrant,
    *,
    refresh_remote_application: RemoteUserApplicationRefresher,
) -> BotApplication:
    """Lock and validate the app-home half of a signed user-install grant."""

    app_id, app_domain = application_ref
    if app_domain != settings.domain:
        try:
            await refresh_remote_application(
                session,
                settings,
                snowflake,
                app_id,
                app_domain,
            )
        except FederationNetworkError:
            raise HTTPException(
                status_code=503,
                detail={"code": "APPLICATION_MANIFEST_UNAVAILABLE"},
            ) from None
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == app_id,
            BotApplication.origin_domain == app_domain,
        )
        .with_for_update()
    )
    if application is None or application.status != "active":
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    bot = await session.get(User, (application.bot_user_id, application.bot_user_domain))
    if bot is None or bot.account_type != "bot" or bot.disabled_at is not None:
        raise HTTPException(status_code=404, detail={"code": "APPLICATION_COMMAND_NOT_FOUND"})
    require_user_install_policy(
        application,
        grant.scopes,
        grant.intents,
        grant.contexts,
    )
    await require_user_install_target(session, settings, application)
    await require_application_runtime_enabled(session, settings, application)
    return application


def federated_user_installation_lock(
    source_id: int,
    source_domain: str,
    application_ref: tuple[int, str],
    user: User,
) -> int:
    app_id, app_domain = application_ref
    return int.from_bytes(
        hashlib.blake2b(
            (
                f"federated-user-install:{source_id}@{source_domain}:"
                f"{app_id}@{app_domain}:{user.id}@{user.origin_domain}"
            ).encode(),
            digest_size=8,
        ).digest(),
        byteorder="big",
        signed=True,
    )


async def locked_federated_user_installation(
    session: AsyncSession,
    user: User,
    application_ref: tuple[int, str],
    source_ref: tuple[int, str],
) -> BotUserInstallation | None:
    app_id, app_domain = application_ref
    source_id, source_domain = source_ref
    source_row = await session.scalar(
        select(BotUserInstallation)
        .where(
            BotUserInstallation.source_id == source_id,
            BotUserInstallation.source_domain == source_domain,
            BotUserInstallation.application_id == app_id,
            BotUserInstallation.application_domain == app_domain,
            BotUserInstallation.user_id == user.id,
            BotUserInstallation.user_domain == user.origin_domain,
        )
        .with_for_update()
    )
    identity_row = await session.scalar(
        select(BotUserInstallation)
        .where(
            BotUserInstallation.application_id == app_id,
            BotUserInstallation.application_domain == app_domain,
            BotUserInstallation.user_id == user.id,
            BotUserInstallation.user_domain == user.origin_domain,
        )
        .with_for_update()
    )
    if source_row is not None and identity_row is not None and source_row is not identity_row:
        raise HTTPException(
            status_code=409,
            detail={"code": "USER_INSTALLATION_SOURCE_CONFLICT"},
        )
    installation = source_row or identity_row
    if installation is None:
        return None
    stored_source = installation.source_id, installation.source_domain
    if stored_source == (None, None):
        installation.source_id, installation.source_domain = source_ref
    elif stored_source != source_ref:
        raise HTTPException(
            status_code=409,
            detail={"code": "USER_INSTALLATION_SOURCE_CONFLICT"},
        )
    return installation


def federated_user_installation_authority_expiry(
    grant: FederatedUserInstallationGrant,
    *,
    now: datetime | None = None,
    minimum_expires_at: datetime | None = None,
    maximum_expires_at: datetime | None = None,
    clock_skew: timedelta = timedelta(),
) -> datetime:
    """Validate the absolute, bounded user-home lease in a signed request.

    Revocation is intentionally bounded-eventual across instances. The
    sender-derived maximum prevents a delayed pre-uninstall assertion from
    receiving a fresh lease window when the target finally applies it.
    """

    current = now or datetime.now(UTC)
    expiry = grant.authority_expires_at
    if (
        current.tzinfo is None
        or expiry.tzinfo is None
        or (minimum_expires_at is not None and minimum_expires_at.tzinfo is None)
        or (maximum_expires_at is not None and maximum_expires_at.tzinfo is None)
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "USER_INSTALLATION_AUTHORITY_EXPIRY_INVALID"},
        )
    current = current.astimezone(UTC)
    expiry = expiry.astimezone(UTC)
    minimum_expiry = minimum_expires_at.astimezone(UTC) if minimum_expires_at is not None else None
    maximum_expiry = maximum_expires_at.astimezone(UTC) if maximum_expires_at is not None else None
    if (
        clock_skew < timedelta()
        or clock_skew > MAX_USER_INSTALLATION_AUTHORITY_CLOCK_SKEW
        or expiry <= current
        or expiry > current + USER_INSTALLATION_AUTHORITY_LEASE + clock_skew
        or (minimum_expiry is not None and expiry < minimum_expiry)
        or (maximum_expiry is not None and expiry > maximum_expiry)
    ):
        raise HTTPException(
            status_code=422,
            detail={"code": "USER_INSTALLATION_AUTHORITY_EXPIRY_INVALID"},
        )
    return expiry


async def reconcile_federated_user_installation(
    session: AsyncSession,
    snowflake: SnowflakeGenerator,
    user: User,
    application_ref: tuple[int, str],
    source_ref: tuple[int, str],
    grant: FederatedUserInstallationGrant,
    installation: BotUserInstallation | None,
    *,
    now: datetime | None = None,
    minimum_expires_at: datetime | None = None,
    maximum_expires_at: datetime | None = None,
    clock_skew: timedelta = timedelta(),
) -> BotUserInstallation:
    """Apply one monotonic signed authority grant without local revision writes."""

    app_id, app_domain = application_ref
    source_id, source_domain = source_ref
    revision = int(grant.grant_revision)
    current = now or datetime.now(UTC)
    authority_expires_at = federated_user_installation_authority_expiry(
        grant,
        now=current,
        minimum_expires_at=minimum_expires_at,
        maximum_expires_at=maximum_expires_at,
        clock_skew=clock_skew,
    )
    expected = list(grant.scopes), list(grant.intents), list(grant.contexts)
    if installation is None:
        installation = BotUserInstallation(
            id=await snowflake.mint(),
            source_id=source_id,
            source_domain=source_domain,
            application_id=app_id,
            application_domain=app_domain,
            user_id=user.id,
            user_domain=user.origin_domain,
            granted_scopes=expected[0],
            granted_intents=expected[1],
            contexts=expected[2],
            grant_revision=revision,
            authority_expires_at=authority_expires_at,
            status="active",
        )
        session.add(installation)
        return installation
    if revision < installation.grant_revision:
        raise HTTPException(status_code=409, detail={"code": "USER_INSTALLATION_GRANT_STALE"})
    stored = (
        list(installation.granted_scopes),
        list(installation.granted_intents),
        list(installation.contexts),
    )
    if revision == installation.grant_revision:
        if stored != expected:
            raise HTTPException(
                status_code=409,
                detail={"code": "USER_INSTALLATION_GRANT_CONFLICT"},
            )
        stored_expiry = installation.authority_expires_at
        if installation.status == "suspended":
            if stored_expiry is not None and (
                stored_expiry.tzinfo is None
                or stored_expiry > current
                or authority_expires_at <= stored_expiry
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "USER_INSTALLATION_GRANT_CONFLICT"},
                )
            installation.status = "active"
            installation.revoked_at = None
        elif installation.status != "active" or installation.revoked_at is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "USER_INSTALLATION_GRANT_CONFLICT"},
            )
        if stored_expiry is None or authority_expires_at > stored_expiry:
            installation.authority_expires_at = authority_expires_at
        return installation
    installation.granted_scopes = expected[0]
    installation.granted_intents = expected[1]
    installation.contexts = expected[2]
    installation.grant_revision = revision
    installation.authority_expires_at = authority_expires_at
    installation.status = "active"
    installation.revoked_at = None
    return installation
