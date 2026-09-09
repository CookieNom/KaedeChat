from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.federation.storage import (
    FederationStorageUsage,
    current_federation_storage_usage,
    federation_storage_quota_exceeded,
    reconcile_federation_storage_usage,
)

from .test_m3_federation import settings


def test_federation_storage_accepts_the_last_available_origin_slot() -> None:
    configured = settings(
        federation_inbox_max_events_per_origin=1_000,
        federation_inbox_max_bytes_per_origin=1024 * 1024,
        federation_inbox_max_events_total=2_000,
        federation_inbox_max_bytes_total=2 * 1024 * 1024,
    )
    usage = FederationStorageUsage(
        origin_events=999,
        origin_bytes=1024,
        total_events=1_500,
        total_bytes=2048,
    )

    assert not federation_storage_quota_exceeded(configured, usage, incoming_bytes=4096)


def test_federation_storage_bounds_each_origin_and_the_global_sybil_budget() -> None:
    configured = settings(
        federation_inbox_max_events_per_origin=1_000,
        federation_inbox_max_bytes_per_origin=1024 * 1024,
        federation_inbox_max_events_total=2_000,
        federation_inbox_max_bytes_total=2 * 1024 * 1024,
    )

    assert federation_storage_quota_exceeded(
        configured,
        FederationStorageUsage(1_000, 0, 1_000, 0),
        incoming_bytes=1,
    )
    assert federation_storage_quota_exceeded(
        configured,
        FederationStorageUsage(0, 1024 * 1024, 1_000, 1024 * 1024),
        incoming_bytes=1,
    )
    assert federation_storage_quota_exceeded(
        configured,
        FederationStorageUsage(0, 0, 2_000, 0),
        incoming_bytes=1,
    )
    assert federation_storage_quota_exceeded(
        configured,
        FederationStorageUsage(0, 0, 1_000, 2 * 1024 * 1024),
        incoming_bytes=1,
    )


def test_singleton_ledger_projection() -> None:
    peer = SimpleNamespace(
        is_self=False,
        federation_inbox_events=7,
        federation_inbox_event_bytes=700,
    )
    global_ledger = SimpleNamespace(
        is_self=True,
        federation_inbox_events=19,
        federation_inbox_event_bytes=1900,
    )

    usage = current_federation_storage_usage(peer, global_ledger)  # type: ignore[arg-type]

    assert usage == FederationStorageUsage(7, 700, 19, 1900)


@pytest.mark.asyncio
async def test_global_inbox_reconciliation_excludes_local_outbound_events(postgres_schema) -> None:
    from sqlalchemy import text

    for table in ("instances", "federation_events", "federation_inbox"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    await postgres_schema.execute(
        text(
            "INSERT INTO instances "
            "(domain,is_self,federation_inbox_events,federation_inbox_event_bytes) VALUES "
            "('local.example',true,999,999),('a.example',false,999,999),('b.example',false,999,999),('empty.example',false,999,999)"
        )
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federation_events (origin_domain,event_id,envelope_bytes) VALUES "
            "('a.example','a1',100),('a.example','a2',200),('b.example','b1',700),('local.example','outbound',9000)"
        )
    )
    await postgres_schema.execute(
        text(
            "INSERT INTO federation_inbox (origin_domain,event_id) VALUES "
            "('a.example','a1'),('a.example','a2'),('a.example','receipt-only'),('b.example','b1')"
        )
    )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        await reconcile_federation_storage_usage(session)
        await session.flush()
        assert set(
            (
                await session.execute(
                    text(
                        "SELECT domain,federation_inbox_events,federation_inbox_event_bytes "
                        "FROM instances"
                    )
                )
            ).all()
        ) == {
            ("local.example", 4, 1000),
            ("a.example", 3, 300),
            ("b.example", 1, 700),
            ("empty.example", 0, 0),
        }
