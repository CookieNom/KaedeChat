from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.snowflake import EPOCH_MS

PARTITION_ADVISORY_LOCK_ID = 5_421_990_727_754_268_273


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def month_snowflake_bound(year: int, month: int) -> int:
    instant = datetime(year, month, 1, tzinfo=UTC)
    timestamp_ms = int(instant.timestamp() * 1_000)
    if timestamp_ms < EPOCH_MS:
        raise ValueError("message partitions cannot precede the Kaede epoch")
    return (timestamp_ms - EPOCH_MS) << 22


def required_months(now: datetime, *, months_ahead: int = 2) -> list[tuple[int, int]]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if months_ahead < 0:
        raise ValueError("months_ahead cannot be negative")
    year, month = 2026, 1
    current = now.astimezone(UTC)
    if (current.year, current.month) < (year, month):
        raise ValueError("message partitions cannot precede the Kaede epoch")
    final_year, final_month = current.year, current.month
    for _ in range(months_ahead):
        final_year, final_month = next_month(final_year, final_month)

    result: list[tuple[int, int]] = []
    while (year, month) <= (final_year, final_month):
        result.append((year, month))
        year, month = next_month(year, month)
    return result


async def ensure_message_partitions(
    connection: AsyncConnection,
    *,
    now: datetime | None = None,
    months_ahead: int = 2,
) -> None:
    current = now or datetime.now(UTC)
    # CREATE TABLE IF NOT EXISTS does not make concurrent PostgreSQL DDL fully
    # race-free: two schedulers can both pass the catalog check and contend on
    # internal relation/type rows.  Serialize the short maintenance section at
    # the database level.  The lock is released automatically with the caller's
    # transaction, including on rollback or connection loss.
    await connection.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": PARTITION_ADVISORY_LOCK_ID},
    )
    for year, month in required_months(current, months_ahead=months_ahead):
        upper_year, upper_month = next_month(year, month)
        name = f"messages_{year:04d}_{month:02d}"
        lower = month_snowflake_bound(year, month)
        upper = month_snowflake_bound(upper_year, upper_month)
        await connection.execute(
            text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF messages "
                f"FOR VALUES FROM ({lower}) TO ({upper})"
            )
        )
