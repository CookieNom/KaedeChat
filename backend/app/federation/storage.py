from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import FederationEvent, FederationInbox, Instance


@dataclass(frozen=True, slots=True)
class FederationStorageUsage:
    origin_events: int
    origin_bytes: int
    total_events: int
    total_bytes: int


def federation_storage_quota_exceeded(
    settings: Settings,
    usage: FederationStorageUsage,
    *,
    incoming_bytes: int,
) -> bool:
    """Return whether one new retained event would exceed a configured budget."""

    return (
        usage.origin_events + 1 > settings.federation_inbox_max_events_per_origin
        or usage.origin_bytes + incoming_bytes > settings.federation_inbox_max_bytes_per_origin
        or usage.total_events + 1 > settings.federation_inbox_max_events_total
        or usage.total_bytes + incoming_bytes > settings.federation_inbox_max_bytes_total
    )


def current_federation_storage_usage(
    peer: Instance,
    global_ledger: Instance,
) -> FederationStorageUsage:
    if not global_ledger.is_self:
        raise ValueError("federation global storage ledger must be the self instance")
    return FederationStorageUsage(
        origin_events=int(peer.federation_inbox_events),
        origin_bytes=int(peer.federation_inbox_event_bytes),
        total_events=int(global_ledger.federation_inbox_events),
        total_bytes=int(global_ledger.federation_inbox_event_bytes),
    )


async def reconcile_federation_storage_usage(session: AsyncSession) -> None:
    """Repair persisted quota counters from retained rows during the daily sweep."""

    inbox_count = (
        select(func.count(FederationInbox.event_id))
        .where(FederationInbox.origin_domain == Instance.domain)
        .correlate(Instance)
        .scalar_subquery()
    )
    event_bytes = (
        select(func.coalesce(func.sum(FederationEvent.envelope_bytes), 0))
        .where(FederationEvent.origin_domain == Instance.domain)
        .correlate(Instance)
        .scalar_subquery()
    )
    await session.execute(
        update(Instance)
        .where(Instance.is_self.is_(False))
        .values(
            federation_inbox_events=inbox_count,
            federation_inbox_event_bytes=event_bytes,
        )
    )
    retained_inbox_total = select(func.count(FederationInbox.event_id)).scalar_subquery()
    retained_event_bytes = (
        select(func.coalesce(func.sum(FederationEvent.envelope_bytes), 0))
        .join(Instance, Instance.domain == FederationEvent.origin_domain)
        .where(Instance.is_self.is_(False))
        .scalar_subquery()
    )
    totals = (await session.execute(select(retained_inbox_total, retained_event_bytes))).one()
    global_ledger = await session.scalar(
        select(Instance).where(Instance.is_self.is_(True)).with_for_update()
    )
    if global_ledger is None:
        raise RuntimeError("self instance is required for federation quota reconciliation")
    global_ledger.federation_inbox_events = int(totals[0])
    global_ledger.federation_inbox_event_bytes = int(totals[1])
