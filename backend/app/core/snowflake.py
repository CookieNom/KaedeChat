from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Protocol, cast

EPOCH_MS = 1_767_225_600_000  # 2026-01-01T00:00:00Z
WORKER_BITS = 10
SEQUENCE_BITS = 12
MAX_WORKER_ID = (1 << WORKER_BITS) - 1
MAX_SEQUENCE = (1 << SEQUENCE_BITS) - 1
# PostgreSQL BIGINT is signed, so the high bit cannot be used by identifiers.
MAX_TIMESTAMP = (1 << 41) - 1
LEASE_TTL_SECONDS = 60
HEARTBEAT_SECONDS = 20

ACTIVATE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3], 'XX')
  return 1
end
return 0
"""

RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class RedisLeaseClient(Protocol):
    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> object: ...
    async def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object: ...


@dataclass(slots=True)
class WorkerLease:
    client: RedisLeaseClient
    worker_id: int
    owner: str
    _heartbeat_task: asyncio.Task[None] | None = None
    _valid_until: float | None = None
    _lease_ttl_seconds: int = LEASE_TTL_SECONDS
    _sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep, repr=False)
    _ownership_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    lost: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.worker_id <= MAX_WORKER_ID:
            raise ValueError("snowflake worker ID is outside the 10-bit range")
        if not self.owner:
            raise ValueError("snowflake lease owner cannot be empty")
        if self._lease_ttl_seconds <= 0:
            raise ValueError("snowflake lease TTL must be positive")

    @property
    def key(self) -> str:
        return f"snowflake:worker:{self.worker_id}"

    @property
    def quarantine_value(self) -> str:
        return f"quarantine:{self.owner}"

    @property
    def active_value(self) -> str:
        return f"active:{self.owner}"

    @classmethod
    async def acquire(
        cls,
        client: RedisLeaseClient,
        *,
        lease_ttl_seconds: int = LEASE_TTL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> WorkerLease:
        if lease_ttl_seconds <= 0:
            raise ValueError("snowflake lease TTL must be positive")
        owner = str(uuid.uuid4())
        for worker_id in range(MAX_WORKER_ID + 1):
            key = f"snowflake:worker:{worker_id}"
            quarantine_value = f"quarantine:{owner}"
            active_value = f"active:{owner}"
            acquired = await client.set(
                key,
                quarantine_value,
                nx=True,
                # Leave enough time for the full quarantine and activation.
                # Any excessive response delay simply makes activation fail.
                ex=lease_ttl_seconds * 3,
            )
            if acquired:
                # A missing key may mean Dragonfly was flushed while its former
                # owner still has a locally cached deadline. During quarantine,
                # neither the former owner nor this claimant can mint. Waiting
                # a complete former TTL guarantees that owner has failed closed
                # before this worker ID becomes active again.
                await sleep(lease_ttl_seconds)
                requested_at = time.monotonic()
                activated = await client.eval(
                    ACTIVATE_SCRIPT,
                    1,
                    key,
                    quarantine_value,
                    active_value,
                    lease_ttl_seconds,
                )
                valid_until = requested_at + lease_ttl_seconds
                if int(cast(int, activated)) != 1:
                    continue
                if time.monotonic() >= valid_until:
                    # The activation response arrived after the active lease
                    # could already have expired and been reassigned.
                    continue
                return cls(
                    client=client,
                    worker_id=worker_id,
                    owner=owner,
                    _valid_until=valid_until,
                    _lease_ttl_seconds=lease_ttl_seconds,
                    _sleep=sleep,
                )
        raise RuntimeError("all snowflake worker IDs are leased")

    def start_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            if self._valid_until is None:
                self._valid_until = time.monotonic() + self._lease_ttl_seconds
            self._heartbeat_task = asyncio.create_task(self._heartbeat())

    @property
    def valid(self) -> bool:
        if self.lost:
            return False
        if self._valid_until is None:
            # Explicitly constructed leases are useful for deterministic unit
            # tests. Production leases always receive a monotonic deadline in
            # ``acquire``/``start_heartbeat``.
            return True
        if time.monotonic() >= self._valid_until:
            self.lost = True
            return False
        return True

    async def assert_owned(self) -> None:
        """Atomically verify and extend active ownership before an ID is returned.

        The successful guarded renewal is the mint's ownership linearization
        point. A quarantine or active lease belonging to another process never
        compares equal, including immediately after a store flush.
        """

        if not self.valid:
            raise RuntimeError("snowflake worker lease was lost")
        async with self._ownership_lock:
            if not self.valid:
                raise RuntimeError("snowflake worker lease was lost")
            requested_at = time.monotonic()
            try:
                renewed = await self.client.eval(
                    RENEW_SCRIPT,
                    1,
                    self.key,
                    self.active_value,
                    self._lease_ttl_seconds,
                )
                ownership_confirmed = int(cast(int, renewed)) == 1
            except Exception as exc:
                self.lost = True
                raise RuntimeError("snowflake worker lease ownership is uncertain") from exc
            valid_until = requested_at + self._lease_ttl_seconds
            if not ownership_confirmed or time.monotonic() >= valid_until:
                self.lost = True
                raise RuntimeError("snowflake worker lease was lost")
            self._valid_until = valid_until

    async def _heartbeat(self) -> None:
        try:
            while True:
                await self._sleep(HEARTBEAT_SECONDS)
                await self.assert_owned()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Continuing to mint after an uncertain renewal can collide with a
            # process that acquires this worker ID when the lease expires.
            self.lost = True
            return

    async def close(self) -> None:
        # Do not delete a lease during a clean shutdown. Keeping its worker ID
        # quarantined until the TTL expires prevents a replacement process from
        # resetting the per-millisecond sequence and reproducing an ID minted by
        # this process immediately before shutdown.
        self.lost = True
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task


class SnowflakeGenerator:
    def __init__(
        self,
        lease: WorkerLease,
        *,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._lease = lease
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._sleep = sleep
        self._last_ms = -1
        self._sequence = 0
        self._lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return self._lease.valid

    async def mint(self) -> int:
        async with self._lock:
            if not self._lease.valid:
                raise RuntimeError("snowflake worker lease was lost")
            now_ms = self._clock_ms()
            if now_ms < self._last_ms:
                regression = self._last_ms - now_ms
                if regression > 5_000:
                    raise RuntimeError("clock regressed by more than 5 seconds")
                await self._sleep(regression / 1_000)
                now_ms = max(self._clock_ms(), self._last_ms)

            if now_ms == self._last_ms:
                self._sequence += 1
                if self._sequence > MAX_SEQUENCE:
                    rollover_deadline = time.monotonic() + 5
                    while now_ms <= self._last_ms:
                        if not self._lease.valid:
                            raise RuntimeError("snowflake worker lease was lost")
                        if time.monotonic() >= rollover_deadline:
                            raise RuntimeError("clock did not advance during sequence rollover")
                        await self._sleep(0.0005)
                        now_ms = self._clock_ms()
                    self._sequence = 0
            else:
                self._sequence = 0

            if now_ms < EPOCH_MS:
                raise RuntimeError("clock is before the Kaede epoch")
            timestamp = now_ms - EPOCH_MS
            if timestamp > MAX_TIMESTAMP:
                raise RuntimeError("snowflake timestamp exhausted the signed BIGINT range")
            # This is deliberately the final await before committing local
            # sequence state and returning the identifier. If the key expired,
            # was flushed, or was reassigned while the ID was being prepared,
            # the guarded renewal fails and the candidate is discarded.
            await self._lease.assert_owned()
            self._last_ms = now_ms
            return (
                (timestamp << (WORKER_BITS + SEQUENCE_BITS))
                | (self._lease.worker_id << SEQUENCE_BITS)
                | self._sequence
            )
