from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

import app.core.snowflake as snowflake_module
from app.core.snowflake import (
    EPOCH_MS,
    LEASE_TTL_SECONDS,
    MAX_SEQUENCE,
    MAX_TIMESTAMP,
    SnowflakeGenerator,
    WorkerLease,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> object:
        del ex
        if nx and name in self.values:
            return None
        self.values[name] = value
        return True

    async def eval(self, script: str, numkeys: int, *args: Any) -> object:
        del numkeys
        key, expected = str(args[0]), str(args[1])
        if self.values.get(key) != expected:
            return 0
        if script == snowflake_module.ACTIVATE_SCRIPT:
            self.values[key] = str(args[2])
        return 1


class DelayedActivationRedis(FakeRedis):
    def __init__(self, clock: list[float], response_time: float) -> None:
        super().__init__()
        self.clock = clock
        self.response_time = response_time

    async def eval(self, script: str, numkeys: int, *args: Any) -> object:
        result = await super().eval(script, numkeys, *args)
        if script == snowflake_module.ACTIVATE_SCRIPT:
            self.clock[0] = self.response_time
        return result


class DelayedRenewalRedis(FakeRedis):
    def __init__(self, clock: list[float], response_time: float) -> None:
        super().__init__()
        self.clock = clock
        self.response_time = response_time

    async def eval(self, script: str, numkeys: int, *args: Any) -> object:
        result = await super().eval(script, numkeys, *args)
        self.clock[0] = self.response_time
        return result


def active_lease(
    redis: FakeRedis,
    *,
    worker_id: int = 0,
    owner: str = "test",
    valid_until: float | None = None,
) -> WorkerLease:
    lease = WorkerLease(
        redis,
        worker_id=worker_id,
        owner=owner,
        _valid_until=valid_until,
    )
    redis.values[lease.key] = lease.active_value
    return lease


async def skip_sleep(delay: float) -> None:
    del delay


@pytest.mark.asyncio
async def test_layout_and_sequence() -> None:
    redis = FakeRedis()
    lease = active_lease(redis, worker_id=7)
    generator = SnowflakeGenerator(lease, clock_ms=lambda: EPOCH_MS + 42)
    first = await generator.mint()
    second = await generator.mint()
    assert first >> 22 == 42
    assert (first >> 12) & 0x3FF == 7
    assert first & MAX_SEQUENCE == 0
    assert second & MAX_SEQUENCE == 1


@pytest.mark.asyncio
async def test_large_clock_regression_refuses_to_mint() -> None:
    times = iter((EPOCH_MS + 10_000, EPOCH_MS + 4_999))
    redis = FakeRedis()
    generator = SnowflakeGenerator(active_lease(redis), clock_ms=lambda: next(times))
    await generator.mint()
    with pytest.raises(RuntimeError, match="regressed"):
        await generator.mint()


@pytest.mark.asyncio
async def test_lost_lease_refuses_to_mint() -> None:
    lease = WorkerLease(FakeRedis(), worker_id=0, owner="test", lost=True)
    with pytest.raises(RuntimeError, match="lease"):
        await SnowflakeGenerator(lease, clock_ms=lambda: EPOCH_MS).mint()


@pytest.mark.asyncio
async def test_signed_bigint_boundary() -> None:
    redis = FakeRedis()
    lease = active_lease(redis, worker_id=1023)
    maximum = await SnowflakeGenerator(lease, clock_ms=lambda: EPOCH_MS + MAX_TIMESTAMP).mint()
    assert maximum == (1 << 63) - (1 << 12)
    with pytest.raises(RuntimeError, match="signed BIGINT"):
        await SnowflakeGenerator(lease, clock_ms=lambda: EPOCH_MS + MAX_TIMESTAMP + 1).mint()


def test_worker_lease_rejects_invalid_identity() -> None:
    with pytest.raises(ValueError, match="worker ID"):
        WorkerLease(FakeRedis(), worker_id=1024, owner="test")
    with pytest.raises(ValueError, match="owner"):
        WorkerLease(FakeRedis(), worker_id=0, owner="")


@pytest.mark.asyncio
async def test_close_quarantines_worker_until_lease_ttl() -> None:
    redis = FakeRedis()
    first = await WorkerLease.acquire(redis, sleep=skip_sleep)
    await first.close()

    replacement = await WorkerLease.acquire(redis, sleep=skip_sleep)

    assert first.lost
    assert first.key in redis.values
    assert replacement.worker_id == first.worker_id + 1


@pytest.mark.asyncio
async def test_acquire_quarantines_for_a_complete_former_lease_ttl() -> None:
    redis = FakeRedis()
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    lease = await WorkerLease.acquire(redis, sleep=record_sleep)

    assert delays == [LEASE_TTL_SECONDS]
    assert redis.values[lease.key] == lease.active_value
    assert lease.valid


@pytest.mark.asyncio
async def test_acquire_rejects_activation_response_delayed_past_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(snowflake_module, "MAX_WORKER_ID", 0)
    redis = DelayedActivationRedis(clock, 100.0 + LEASE_TTL_SECONDS)

    with pytest.raises(RuntimeError, match="all snowflake worker IDs"):
        await WorkerLease.acquire(redis, sleep=skip_sleep)


@pytest.mark.asyncio
async def test_heartbeat_deadline_is_measured_from_request_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    redis = DelayedRenewalRedis(clock, 100.0 + LEASE_TTL_SECONDS)
    lease = active_lease(redis, valid_until=100.0 + LEASE_TTL_SECONDS)

    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    lease._sleep = skip_sleep

    await lease._heartbeat()

    assert lease.lost


@pytest.mark.asyncio
async def test_store_loss_fails_closed_before_next_mint() -> None:
    redis = FakeRedis()
    lease = await WorkerLease.acquire(redis, sleep=skip_sleep)
    generator = SnowflakeGenerator(lease, clock_ms=lambda: EPOCH_MS + 100)
    await generator.mint()

    redis.values.clear()

    with pytest.raises(RuntimeError, match="lease"):
        await generator.mint()
    assert lease.lost


@pytest.mark.asyncio
async def test_reassignment_quarantines_and_fences_former_owner() -> None:
    redis = FakeRedis()
    first = await WorkerLease.acquire(redis, sleep=skip_sleep)
    wall_ms = [EPOCH_MS + 100]
    first_generator = SnowflakeGenerator(first, clock_ms=lambda: wall_ms[0])
    first_id = await first_generator.mint()

    # Simulate Dragonfly losing all volatile lease state. The replacement can
    # claim the same numeric worker ID, but its value remains non-minting until
    # a complete former TTL has elapsed.
    redis.values.clear()
    quarantine_started = asyncio.Event()
    release_quarantine = asyncio.Event()

    async def controlled_quarantine(delay: float) -> None:
        assert delay == LEASE_TTL_SECONDS
        quarantine_started.set()
        await release_quarantine.wait()
        wall_ms[0] += (LEASE_TTL_SECONDS * 1_000) + 1

    second_task = asyncio.create_task(WorkerLease.acquire(redis, sleep=controlled_quarantine))
    await quarantine_started.wait()

    assert redis.values[first.key].startswith("quarantine:")
    with pytest.raises(RuntimeError, match="lease"):
        await first_generator.mint()

    release_quarantine.set()
    second = await second_task
    assert second.worker_id == first.worker_id
    assert redis.values[second.key] == second.active_value

    second_id = await SnowflakeGenerator(second, clock_ms=lambda: wall_ms[0]).mint()
    assert second_id > first_id
