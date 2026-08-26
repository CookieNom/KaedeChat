from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select

from app.core.settings import Settings
from app.db.base import Base
from app.db.models import FederationReplicaUsage, Guild, Instance, PeerKey, User

REPLICA_QUOTA_ERROR_CODE = "KAED_FED_REPLICA_QUOTA_EXCEEDED"
REMOTE_IDENTITY_GC_BATCH_SIZE = 250
REMOTE_IDENTITY_GC_QUERY_CHUNK = 1_000


@dataclass(frozen=True, slots=True)
class ReplicaStorageUsage:
    guild_rows: int
    guild_bytes: int
    origin_rows: int
    origin_bytes: int


class FederationReplicaQuotaExceeded(RuntimeError):
    """A durable replica high-water mark was reached before commit."""

    def __init__(self, scope: str, resource: str, used: int, limit: int) -> None:
        self.scope = scope
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(
            f"remote replica {scope} {resource} high-water mark reached ({used} > {limit})"
        )


def replica_storage_quota_violation(
    settings: Settings,
    usage: ReplicaStorageUsage,
) -> FederationReplicaQuotaExceeded | None:
    limits = (
        (
            "guild",
            "rows",
            usage.guild_rows,
            settings.federation_replica_max_rows_per_guild,
        ),
        (
            "guild",
            "bytes",
            usage.guild_bytes,
            settings.federation_replica_max_bytes_per_guild,
        ),
        (
            "origin",
            "rows",
            usage.origin_rows,
            settings.federation_replica_max_rows_per_origin,
        ),
        (
            "origin",
            "bytes",
            usage.origin_bytes,
            settings.federation_replica_max_bytes_per_origin,
        ),
    )
    for scope, resource, used, limit in limits:
        if used > limit:
            return FederationReplicaQuotaExceeded(scope, resource, used, limit)
    return None


async def admit_replica_storage(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
) -> ReplicaStorageUsage:
    """Fence and admit the trigger-accounted state in the current transaction.

    Row triggers update the per-guild ledger in the same transaction as each
    replica mutation. The origin-scoped advisory lock serializes admission for
    different guilds from the same peer, so concurrent workers cannot each
    admit against a stale aggregate. Call this after all bounded mutations and
    immediately before committing their transaction.
    """

    if guild.origin_domain == settings.domain:
        return ReplicaStorageUsage(0, 0, 0, 0)
    if guild.sync_error_code == REPLICA_QUOTA_ERROR_CODE and guild.sync_status != "quota_paused":
        # A replaying live event may already have restored ``ready`` before
        # admission. Clear the old pause marker before the first flush so the
        # guild-level status/error invariant remains valid.
        guild.sync_error_code = None
        guild.sync_error = None
    await session.flush()
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"kaede-federation-replica:{guild.origin_domain}",
                    0,
                )
            )
        )
    )
    ledger = await session.scalar(
        select(FederationReplicaUsage)
        .where(
            FederationReplicaUsage.guild_id == guild.id,
            FederationReplicaUsage.guild_domain == guild.origin_domain,
        )
        .with_for_update()
    )
    if ledger is None:
        raise RuntimeError("remote replica storage ledger is missing")
    totals = (
        await session.execute(
            select(
                func.coalesce(func.sum(FederationReplicaUsage.total_rows), 0),
                func.coalesce(func.sum(FederationReplicaUsage.total_bytes), 0),
            ).where(FederationReplicaUsage.guild_domain == guild.origin_domain)
        )
    ).one()
    usage = ReplicaStorageUsage(
        guild_rows=int(ledger.total_rows),
        guild_bytes=int(ledger.total_bytes),
        origin_rows=int(totals[0]),
        origin_bytes=int(totals[1]),
    )
    violation = replica_storage_quota_violation(settings, usage)
    if violation is not None:
        raise violation
    if guild.sync_status == "quota_paused":
        # The caller still owns deciding whether the successful operation has
        # fully synchronized the replica. Only remove the obsolete error here.
        guild.sync_status = "stale"
        guild.sync_error_code = None
        guild.sync_error = None
    return usage


async def mark_replica_quota_paused(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    guild_domain: str,
    violation: FederationReplicaQuotaExceeded,
) -> bool:
    """Persist a retryable pause after the over-limit savepoint is rolled back."""

    return await mark_replica_capacity_paused(
        session,
        settings,
        guild_id,
        guild_domain,
        error_code=REPLICA_QUOTA_ERROR_CODE,
        internal_error=str(violation),
    )


async def mark_replica_capacity_paused(
    session: AsyncSession,
    settings: Settings,
    guild_id: int,
    guild_domain: str,
    *,
    error_code: str,
    internal_error: str,
) -> bool:
    """Pause a remote guild whose next change cannot fit a bounded cache.

    ``error_code`` is safe client-facing metadata. ``internal_error`` is kept
    only for operators and is never included in the public guild payload.
    """

    if guild_domain == settings.domain:
        return False
    guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild_id, Guild.origin_domain == guild_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if guild is None:
        return False
    guild.sync_status = "quota_paused"
    guild.sync_error_code = error_code
    guild.sync_error = internal_error[:500]
    return True


async def reconcile_replica_storage(
    session: AsyncSession,
    guild_id: int,
    guild_domain: str,
) -> None:
    """Repair one remote guild ledger after a parent-row cascade.

    PostgreSQL removes a parent before firing cascaded child DELETE triggers,
    so a child trigger cannot always rediscover its guild through the deleted
    message/channel. Explicit cache/history purges therefore reconcile from the
    surviving rows before quota admission or commit.
    """

    await session.scalar(select(func.kaede_reconcile_replica_usage(guild_id, guild_domain)))
    await session.scalar(select(func.kaede_reconcile_tracker_replica_usage(guild_id, guild_domain)))


def _remote_user_reference_absence() -> tuple[ColumnElement[bool], ...]:
    """Build anti-reference predicates from the actual SQLAlchemy FK graph.

    This deliberately derives the list instead of maintaining a hand-written
    set of tables.  Adding a future durable foreign key to ``users`` therefore
    makes the collector preserve that user automatically.  It also avoids the
    destructive behavior of relying on ``ON DELETE CASCADE`` for relationships
    and history-transfer rows.
    """

    predicates: list[ColumnElement[bool]] = []
    user_table = User.__table__
    for table in Base.metadata.sorted_tables:
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table is not user_table:
                continue
            matches = [
                element.parent == user_table.c[element.column.name]
                for element in constraint.elements
            ]
            predicates.append(~exists(select(1).select_from(table).where(and_(*matches))))
    return tuple(predicates)


def _remote_instance_reference_absence() -> tuple[ColumnElement[bool], ...]:
    """Derive safe anti-reference checks for cached remote namespaces."""

    predicates: list[ColumnElement[bool]] = []
    instance_table = Instance.__table__
    for table in Base.metadata.sorted_tables:
        # Peer keys are a discovery cache owned by the Instance row and
        # cascade with it. Counting that child as a semantic reference would
        # make every normally discovered (including sybil) namespace immortal.
        if table is PeerKey.__table__:
            continue
        for constraint in table.foreign_key_constraints:
            if constraint.referred_table is not instance_table:
                continue
            matches = [
                element.parent == instance_table.c[element.column.name]
                for element in constraint.elements
            ]
            predicates.append(~exists(select(1).select_from(table).where(and_(*matches))))
    return tuple(predicates)


def orphaned_remote_user_candidates(
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = REMOTE_IDENTITY_GC_BATCH_SIZE,
) -> Select[tuple[User]]:
    """Return a small, lock-safe batch of aged and wholly unreferenced profiles."""

    if limit < 1 or limit > 10_000:
        raise ValueError("remote identity GC limit must be between 1 and 10000")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("remote identity GC time must be timezone-aware")
    cutoff = current - timedelta(days=settings.federation_remote_identity_retention_days)
    return (
        select(User)
        .where(
            User.is_local.is_(False),
            User.updated_at < cutoff,
            *_remote_user_reference_absence(),
        )
        .order_by(User.updated_at, User.origin_domain, User.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def purge_orphaned_remote_users(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """Delete one bounded batch of aged remote users with no durable references.

    The candidate rows are locked before deletion.  A concurrent transaction
    trying to create a new foreign-key reference must wait for that row lock and
    will either see the retained profile or fail cleanly after it is collected;
    it cannot race the anti-reference checks and silently lose related data.
    The caller owns the transaction and commit.
    """

    maximum = settings.federation_remote_identity_gc_batch_size if limit is None else limit
    if maximum < 1 or maximum > 50_000:
        raise ValueError("remote identity GC cycle limit must be between 1 and 50000")
    cleaned = 0
    while cleaned < maximum:
        chunk_size = min(REMOTE_IDENTITY_GC_QUERY_CHUNK, maximum - cleaned)
        candidates = list(
            await session.scalars(
                orphaned_remote_user_candidates(settings, now=now, limit=chunk_size)
            )
        )
        for user in candidates:
            await session.delete(user)
        cleaned += len(candidates)
        if len(candidates) < chunk_size or cleaned >= maximum:
            break
        # Make deleted rows disappear from the next anti-reference query while
        # retaining one bounded transaction for the scheduler cycle.
        await session.flush()
    return cleaned


def orphaned_remote_instance_candidates(
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int = REMOTE_IDENTITY_GC_QUERY_CHUNK,
) -> Select[tuple[Instance]]:
    """Return aged remote namespaces after every durable reference is gone."""

    if limit < 1 or limit > 10_000:
        raise ValueError("remote instance GC limit must be between 1 and 10000")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("remote instance GC time must be timezone-aware")
    cutoff = current - timedelta(days=settings.federation_remote_identity_retention_days)
    return (
        select(Instance)
        .where(
            Instance.is_self.is_(False),
            Instance.updated_at < cutoff,
            *_remote_instance_reference_absence(),
        )
        .order_by(Instance.updated_at, Instance.domain)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )


async def purge_orphaned_remote_instances(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> int:
    """Adaptively delete a bounded number of unused cached namespaces."""

    maximum = settings.federation_remote_identity_gc_batch_size if limit is None else limit
    if maximum < 1 or maximum > 50_000:
        raise ValueError("remote instance GC cycle limit must be between 1 and 50000")
    cleaned = 0
    while cleaned < maximum:
        chunk_size = min(REMOTE_IDENTITY_GC_QUERY_CHUNK, maximum - cleaned)
        candidates = list(
            await session.scalars(
                orphaned_remote_instance_candidates(settings, now=now, limit=chunk_size)
            )
        )
        for instance in candidates:
            await session.delete(instance)
        cleaned += len(candidates)
        if len(candidates) < chunk_size or cleaned >= maximum:
            break
        await session.flush()
    return cleaned
