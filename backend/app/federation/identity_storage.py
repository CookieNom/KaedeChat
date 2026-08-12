from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import Instance, User
from app.federation.network import ensure_remote_instance_record, normalize_domain


class FederationIdentityQuotaExceeded(ValueError):
    """An introducing peer exhausted its retained identity allowance."""

    code = "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED"
    federation_code = "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED"

    def __init__(self, resource: str, used: int, limit: int) -> None:
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(f"federated identity {resource} quota exceeded ({used} >= {limit})")

    def detail(self, *, federation: bool = False) -> dict[str, object]:
        # Do not expose counts here. Identity capacity can be shared across
        # users, and exact values would give remote peers a storage oracle.
        return {"code": self.federation_code if federation else self.code}


async def _lock_introducer(session: AsyncSession, introducer: str) -> None:
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-federation-identities:{introducer}", 0)
            )
        )
    )


async def ensure_identity_instance(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    *,
    introduced_by_domain: str,
) -> Instance:
    """Materialize one namespace with durable, quota-bound provenance.

    A guild authority may carry opaque references homed on a third instance.
    Such namespaces are charged to that authority, not to the uninvolved third
    party. The transaction-scoped introducer lock makes the count and insert
    one serial admission decision across workers.
    """

    normalized_origin = normalize_domain(origin)
    introducer = normalize_domain(introduced_by_domain)
    if normalized_origin == settings.domain:
        raise ValueError("a local identity cannot be introduced as remote")

    await _lock_introducer(session, introducer)
    instance = await session.get(Instance, normalized_origin, populate_existing=True)
    if instance is not None:
        if instance.is_self:
            raise ValueError("remote identity namespace conflicts with this instance")
        # Preserve first-introducer accounting. An operator-created row has no
        # provenance yet and becomes charged when federation first uses it.
        if instance.federation_introduced_by_domain is None:
            instance.federation_introduced_by_domain = introducer
        return instance

    if normalized_origin != introducer:
        third_party_count = int(
            await session.scalar(
                select(func.count())
                .select_from(Instance)
                .where(
                    Instance.is_self.is_(False),
                    Instance.federation_introduced_by_domain == introducer,
                    Instance.domain != introducer,
                )
            )
            or 0
        )
        if third_party_count >= settings.federation_third_party_instances_per_introducer:
            raise FederationIdentityQuotaExceeded(
                "third-party instance namespaces",
                third_party_count,
                settings.federation_third_party_instances_per_introducer,
            )

    instance = await ensure_remote_instance_record(session, settings, normalized_origin)
    if instance.federation_introduced_by_domain is None:
        instance.federation_introduced_by_domain = introducer
    return instance


async def admit_remote_user_identity(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    origin: str,
    *,
    introduced_by_domain: str,
) -> tuple[User | None, str]:
    """Admit a remote User row and return its immutable introducer charge.

    The caller must insert before committing the current transaction. Existing
    rows remain admitted regardless of later limit changes and retain their
    original charge until physical garbage collection.
    """

    normalized_origin = normalize_domain(origin)
    introducer = normalize_domain(introduced_by_domain)
    await ensure_identity_instance(
        session,
        settings,
        normalized_origin,
        introduced_by_domain=introducer,
    )
    await _lock_introducer(session, introducer)
    existing = await session.get(User, (user_id, normalized_origin), populate_existing=True)
    if existing is not None:
        if existing.is_local:
            raise ValueError("remote user identity conflicts with a local user")
        if existing.federation_introduced_by_domain is None:
            # Only legacy/operator-created rows can reach this branch after the
            # migration backfill. Charge them before they are reused.
            existing.federation_introduced_by_domain = introducer
        return existing, existing.federation_introduced_by_domain

    retained = int(
        await session.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.is_local.is_(False),
                User.federation_introduced_by_domain == introducer,
            )
        )
        or 0
    )
    if retained >= settings.federation_remote_users_per_introducer:
        raise FederationIdentityQuotaExceeded(
            "remote identities",
            retained,
            settings.federation_remote_users_per_introducer,
        )
    return None, introducer
