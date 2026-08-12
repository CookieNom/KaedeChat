from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
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


def test_storage_usage_reads_the_locked_singleton_global_ledger() -> None:
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
async def test_global_inbox_reconciliation_excludes_local_outbound_events() -> None:
    totals = SimpleNamespace(one=lambda: (19, 1900))
    global_ledger = SimpleNamespace(
        is_self=True,
        federation_inbox_events=0,
        federation_inbox_event_bytes=0,
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            execute=AsyncMock(side_effect=[object(), totals]),
            scalar=AsyncMock(return_value=global_ledger),
        ),
    )

    await reconcile_federation_storage_usage(session)

    totals_statement = session.execute.await_args_list[1].args[0]  # type: ignore[attr-defined]
    sql = str(totals_statement.compile(dialect=postgresql.dialect()))
    assert "JOIN instances ON instances.domain = federation_events.origin_domain" in sql
    assert "instances.is_self IS false" in sql
    assert global_ledger.federation_inbox_events == 19
    assert global_ledger.federation_inbox_event_bytes == 1900
