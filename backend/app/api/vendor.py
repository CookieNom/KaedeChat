from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.audit import audit
from app.admin.auth import AdminPrincipal, require_admin
from app.api.dependencies import AuthenticatedUser, get_session, get_snowflake, require_user
from app.core.snowflake import SnowflakeGenerator
from app.db.models import UserSettings, VendorConfiguration
from app.vendor import Platform, extensions

router = APIRouter(prefix="/api/v1")
Session = Annotated[AsyncSession, Depends(get_session)]
User = Annotated[AuthenticatedUser, Depends(require_user)]


async def require_vendor_admin(
    principal: Annotated[AdminPrincipal, Depends(require_admin)],
) -> AdminPrincipal:
    principal.require("extensions.manage")
    return principal


Admin = Annotated[AdminPrincipal, Depends(require_vendor_admin)]


class Records(BaseModel):
    version: int = Field(ge=0)
    records: list[dict[str, Any]] = Field(max_length=100)


@router.get("/administration/extensions")
async def panes(admin: Admin, session: Session) -> list[dict[str, Any]]:
    rows = {
        r.package: r
        for r in await session.scalars(
            select(VendorConfiguration).where(
                VendorConfiguration.domain == admin.user.origin_domain
            )
        )
    }
    return [
        dict(
            id=key,
            title=ext.title,
            description=ext.description,
            fields=ext.fields,
            actions=ext.actions,
            records=rows[key].records if key in rows else [],
            version=rows[key].version if key in rows else 0,
        )
        for key, ext in extensions.items()
    ]


@router.put("/administration/extensions/{key}")
async def save_pane(
    key: str,
    body: Records,
    admin: Admin,
    session: Session,
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
) -> dict[str, Any]:
    ext = extensions.get(key)
    if ext is None:
        raise HTTPException(404, "Extension unavailable")
    try:
        records = ext.validate(body.records)
    except ValueError as error:
        raise HTTPException(
            422, {"code": "VENDOR_CONFIGURATION_INVALID", "reason": str(error)}
        ) from error
    domain = admin.user.origin_domain
    if body.version == 0:
        version = await session.scalar(
            insert(VendorConfiguration)
            .values(domain=domain, package=key, records=records, version=1)
            .on_conflict_do_nothing()
            .returning(VendorConfiguration.version)
        )
    else:
        version = await session.scalar(
            update(VendorConfiguration)
            .where(
                VendorConfiguration.domain == domain,
                VendorConfiguration.package == key,
                VendorConfiguration.version == body.version,
            )
            .values(records=records, version=body.version + 1)
            .returning(VendorConfiguration.version)
        )
    if version is None:
        raise HTTPException(409, {"code": "VENDOR_CONFIGURATION_STALE"})
    await audit(
        session,
        snowflake,
        admin,
        "vendor.configuration.update",
        "extension",
        key,
        metadata={"version": version},
    )
    await session.commit()
    return {"version": version, "records": records}


class Choice(BaseModel):
    enabled: StrictBool


async def shopping_state(
    platform: Platform, auth: AuthenticatedUser, session: AsyncSession, claim: bool
) -> dict[str, Any]:
    user = auth.user
    rules = []
    if user.account_type == "human" and user.is_local and extensions:
        for config in await session.scalars(
            select(VendorConfiguration)
            .where(VendorConfiguration.domain == user.origin_domain)
            .order_by(VendorConfiguration.package)
        ):
            ext = extensions.get(config.package)
            if ext and ext.link_rules:
                rules.extend(ext.link_rules(config.records, platform))
    preference = (UserSettings.user_id == user.id, UserSettings.user_domain == user.origin_domain)
    enabled = await session.scalar(select(UserSettings.shopping_links).where(*preference))
    prompt = False
    if (
        claim
        and rules
        and enabled is None
        and timedelta(days=14) <= datetime.now(UTC) - user.created_at < timedelta(days=15)
    ):
        # Claim before displaying. A closed or interrupted prompt counts as declined.
        prompt = (
            await session.scalar(
                update(UserSettings)
                .where(*preference, UserSettings.shopping_links.is_(None))
                .values(shopping_links=False)
                .returning(UserSettings.user_id)
            )
            is not None
        )
        await session.commit()
    return {
        "available": bool(rules),
        "prompt": prompt,
        "enabled": enabled is True,
        "rules": rules if enabled else [],
    }


@router.post("/users/@me/shopping-links/open")
async def open_shopping(platform: Platform, auth: User, session: Session) -> dict[str, Any]:
    return await shopping_state(platform, auth, session, True)


@router.get("/users/@me/shopping-links")
async def get_shopping(platform: Platform, auth: User, session: Session) -> dict[str, Any]:
    return await shopping_state(platform, auth, session, False)


@router.put("/users/@me/shopping-links")
async def choose_shopping(
    platform: Platform, body: Choice, auth: User, session: Session
) -> dict[str, Any]:
    await session.execute(
        update(UserSettings)
        .where(
            UserSettings.user_id == auth.user.id,
            UserSettings.user_domain == auth.user.origin_domain,
        )
        .values(shopping_links=body.enabled)
    )
    await session.commit()
    return await shopping_state(platform, auth, session, False)


for key, extension in extensions.items():
    if extension.admin_router is not None:
        router.include_router(
            extension.admin_router,
            prefix=f"/administration/extensions/{key}",
            dependencies=[Depends(require_vendor_admin)],
        )
