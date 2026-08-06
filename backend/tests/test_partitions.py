from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.core.snowflake import EPOCH_MS
from app.db.partitions import (
    PARTITION_ADVISORY_LOCK_ID,
    ensure_message_partitions,
    month_snowflake_bound,
    next_month,
    required_months,
)
from app.tasks import partitions_ensure


def test_month_bound_matches_snowflake_epoch() -> None:
    assert month_snowflake_bound(2026, 1) == 0
    assert month_snowflake_bound(2026, 2) == (31 * 24 * 60 * 60 * 1_000) << 22


def test_required_months_cover_epoch_through_two_ahead() -> None:
    months = required_months(datetime(2026, 7, 12, tzinfo=UTC))
    assert months[0] == (2026, 1)
    assert months[-1] == (2026, 9)
    assert len(months) == 9


def test_december_rolls_into_next_year() -> None:
    assert next_month(2026, 12) == (2027, 1)


def test_pre_epoch_partition_is_rejected() -> None:
    assert EPOCH_MS > 0
    with pytest.raises(ValueError, match="epoch"):
        month_snowflake_bound(2025, 12)
    with pytest.raises(ValueError, match="epoch"):
        required_months(datetime(2025, 12, 31, tzinfo=UTC))


def test_negative_partition_horizon_is_rejected() -> None:
    with pytest.raises(ValueError, match="months_ahead"):
        required_months(datetime(2026, 1, 1, tzinfo=UTC), months_ahead=-1)


@pytest.mark.asyncio
async def test_partition_creation_takes_database_advisory_lock_first() -> None:
    connection = AsyncMock()

    await ensure_message_partitions(
        connection,
        now=datetime(2026, 1, 15, tzinfo=UTC),
        months_ahead=0,
    )

    calls = connection.execute.await_args_list
    assert len(calls) == 2
    assert "pg_advisory_xact_lock" in str(calls[0].args[0])
    assert calls[0].args[1] == {"lock_id": PARTITION_ADVISORY_LOCK_ID}
    assert "CREATE TABLE IF NOT EXISTS messages_2026_01" in str(calls[1].args[0])


def test_partition_maintenance_is_scheduled_nightly() -> None:
    assert partitions_ensure.labels["schedule"] == [{"cron": "17 3 * * *"}]
