from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser, get_session, require_user
from app.db.bot_models import InstanceAdminGrant
from app.db.models import User

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "administrator": frozenset(
        {
            "admin.read",
            "operators.manage",
            "users.manage",
            "instances.manage",
            "bots.manage",
            "reports.read",
            "reports.manage",
            "audit.read",
        }
    ),
    "trust_safety": frozenset(
        {"admin.read", "reports.read", "reports.manage", "users.manage", "instances.manage"}
    ),
    "bot_reviewer": frozenset({"admin.read", "bots.manage", "audit.read"}),
    "operations": frozenset({"admin.read", "instances.manage", "audit.read"}),
    "auditor": frozenset({"admin.read", "reports.read", "audit.read"}),
}


@dataclass(frozen=True, slots=True)
class AdminPrincipal:
    user: User
    roles: frozenset[str]
    capabilities: frozenset[str]

    def require(self, capability: str) -> None:
        if "*" not in self.capabilities and capability not in self.capabilities:
            raise HTTPException(
                status_code=403,
                detail={"code": "ADMIN_CAPABILITY_REQUIRED", "capability": capability},
            )


async def require_admin(
    auth: Annotated[AuthenticatedUser, Depends(require_user)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AdminPrincipal:
    if not auth.user.is_local or auth.user.account_type != "human":
        raise HTTPException(status_code=403, detail={"code": "ADMIN_ACCOUNT_REQUIRED"})
    now = datetime.now(UTC)
    grants = list(
        await session.scalars(
            select(InstanceAdminGrant).where(
                InstanceAdminGrant.user_id == auth.user.id,
                InstanceAdminGrant.user_domain == auth.user.origin_domain,
                InstanceAdminGrant.revoked_at.is_(None),
                (InstanceAdminGrant.expires_at.is_(None) | (InstanceAdminGrant.expires_at > now)),
            )
        )
    )
    if not grants:
        raise HTTPException(status_code=403, detail={"code": "ADMIN_AUTHENTICATION_REQUIRED"})
    roles = frozenset(grant.role for grant in grants)
    capabilities = frozenset(
        capability for role in roles for capability in ROLE_CAPABILITIES.get(role, frozenset())
    )
    return AdminPrincipal(auth.user, roles, capabilities)
