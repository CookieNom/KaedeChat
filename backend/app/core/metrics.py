from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from functools import wraps
from typing import ParamSpec, TypeVar

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import get_settings
from app.db.models import FederationOutbox, FederationReplicaUsage, Guild, Instance

P = ParamSpec("P")
R = TypeVar("R")
METRIC_LABEL_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


async def increment_metric(redis: Redis | None, name: str, amount: int = 1) -> None:
    if redis is None or not METRIC_LABEL_RE.fullmatch(name):
        return
    with suppress(Exception):
        await redis.incr(f"metrics:counter:{name}", amount)


async def observe_job(redis: Redis, name: str, elapsed_seconds: float, failed: bool) -> None:
    if not METRIC_LABEL_RE.fullmatch(name):
        raise ValueError("invalid job metric name")
    elapsed_microseconds = max(0, round(elapsed_seconds * 1_000_000))
    pipeline = redis.pipeline(transaction=True)
    key = "metrics:jobs"
    pipeline.hincrby(key, f"{name}:count", 1)
    pipeline.hincrby(key, f"{name}:duration_us", elapsed_microseconds)
    pipeline.hset(key, f"{name}:last_duration_us", str(elapsed_microseconds))
    if failed:
        pipeline.hincrby(key, f"{name}:failures", 1)
    await pipeline.execute()


def observed_job(name: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    if not METRIC_LABEL_RE.fullmatch(name):
        raise ValueError("invalid job metric name")

    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            started = time.monotonic()
            failed = False
            try:
                return await function(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                settings = get_settings()
                redis = Redis.from_url(settings.dragonfly_url.get_secret_value())
                with suppress(Exception):
                    await observe_job(redis, name, time.monotonic() - started, failed)
                await redis.aclose()

        return wrapped

    return decorate


def _sample(name: str, value: int | float, *, labels: dict[str, str] | None = None) -> str:
    rendered_labels = ""
    if labels:
        pairs = []
        for key, raw in sorted(labels.items()):
            safe = raw.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            pairs.append(f'{key}="{safe}"')
        rendered_labels = "{" + ",".join(pairs) + "}"
    return f"{name}{rendered_labels} {value}"


async def render_metrics(redis: Redis, sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    settings = get_settings()
    connected = 0
    async for _key in redis.scan_iter(match="gateway:connection-owner:*", count=1000):
        connected += 1
    async with sessionmaker() as session:
        pending, failed = (
            await session.execute(
                select(
                    func.count(FederationOutbox.id).filter(
                        FederationOutbox.status.in_(("pending", "retry", "circuit"))
                    ),
                    func.count(FederationOutbox.id).filter(FederationOutbox.status == "failed"),
                )
            )
        ).one()
        retained_events, retained_bytes = (
            await session.execute(
                select(
                    func.coalesce(func.sum(Instance.federation_inbox_events), 0),
                    func.coalesce(func.sum(Instance.federation_inbox_event_bytes), 0),
                ).where(Instance.is_self.is_(False))
            )
        ).one()
        replica_rows, replica_bytes, quota_paused_guilds = (
            await session.execute(
                select(
                    func.coalesce(func.sum(FederationReplicaUsage.total_rows), 0),
                    func.coalesce(func.sum(FederationReplicaUsage.total_bytes), 0),
                    select(func.count(Guild.id))
                    .where(Guild.sync_status == "quota_paused")
                    .scalar_subquery(),
                )
            )
        ).one()
    failure_total = int(await redis.get("metrics:counter:federation_delivery_failures") or 0)
    quota_rejections = int(
        await redis.get("metrics:counter:federation_inbox_quota_rejections") or 0
    )
    media_cache_quota_rejections = int(
        await redis.get("metrics:counter:federation_remote_media_cache_quota_rejections") or 0
    )
    raw_jobs = await redis.hgetall("metrics:jobs")  # type: ignore[misc]
    jobs: dict[str, dict[str, int]] = {}
    for raw_key, raw_value in raw_jobs.items():
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        name, separator, field = key.rpartition(":")
        if not separator or not METRIC_LABEL_RE.fullmatch(name):
            continue
        jobs.setdefault(name, {})[field] = int(raw_value)
    lines = [
        "# HELP kaede_up Whether the API process is running.",
        "# TYPE kaede_up gauge",
        _sample("kaede_up", 1, labels={"service": "api"}),
        "# HELP kaede_gateway_connected_sessions Current resumable gateway connections.",
        "# TYPE kaede_gateway_connected_sessions gauge",
        _sample("kaede_gateway_connected_sessions", connected),
        "# HELP kaede_federation_outbox_pending Events awaiting peer delivery.",
        "# TYPE kaede_federation_outbox_pending gauge",
        _sample("kaede_federation_outbox_pending", int(pending or 0)),
        "# HELP kaede_federation_outbox_failed Permanently failed outbox rows.",
        "# TYPE kaede_federation_outbox_failed gauge",
        _sample("kaede_federation_outbox_failed", int(failed or 0)),
        "# HELP kaede_federation_delivery_failures_total Failed federation delivery attempts.",
        "# TYPE kaede_federation_delivery_failures_total counter",
        _sample("kaede_federation_delivery_failures_total", failure_total),
        "# HELP kaede_federation_inbox_retained_events "
        "Retained remote federation idempotency rows.",
        "# TYPE kaede_federation_inbox_retained_events gauge",
        _sample("kaede_federation_inbox_retained_events", int(retained_events or 0)),
        "# HELP kaede_federation_inbox_retained_bytes Retained canonical remote event bytes.",
        "# TYPE kaede_federation_inbox_retained_bytes gauge",
        _sample("kaede_federation_inbox_retained_bytes", int(retained_bytes or 0)),
        "# HELP kaede_federation_inbox_capacity_events Configured instance-wide inbox row limit.",
        "# TYPE kaede_federation_inbox_capacity_events gauge",
        _sample(
            "kaede_federation_inbox_capacity_events",
            settings.federation_inbox_max_events_total,
        ),
        "# HELP kaede_federation_inbox_capacity_bytes Configured instance-wide inbox byte limit.",
        "# TYPE kaede_federation_inbox_capacity_bytes gauge",
        _sample(
            "kaede_federation_inbox_capacity_bytes",
            settings.federation_inbox_max_bytes_total,
        ),
        "# HELP kaede_federation_inbox_quota_rejections_total "
        "Events deferred by retained federation storage quotas.",
        "# TYPE kaede_federation_inbox_quota_rejections_total counter",
        _sample("kaede_federation_inbox_quota_rejections_total", quota_rejections),
        "# HELP kaede_federation_replica_retained_rows Trigger-accounted remote guild rows.",
        "# TYPE kaede_federation_replica_retained_rows gauge",
        _sample("kaede_federation_replica_retained_rows", int(replica_rows or 0)),
        "# HELP kaede_federation_replica_retained_bytes Estimated remote guild SQL bytes.",
        "# TYPE kaede_federation_replica_retained_bytes gauge",
        _sample("kaede_federation_replica_retained_bytes", int(replica_bytes or 0)),
        "# HELP kaede_federation_replica_quota_paused_guilds "
        "Remote guilds paused by storage limits.",
        "# TYPE kaede_federation_replica_quota_paused_guilds gauge",
        _sample(
            "kaede_federation_replica_quota_paused_guilds",
            int(quota_paused_guilds or 0),
        ),
        "# HELP kaede_federation_remote_media_cache_capacity_bytes "
        "Configured retained remote-media LRU ceiling.",
        "# TYPE kaede_federation_remote_media_cache_capacity_bytes gauge",
        _sample(
            "kaede_federation_remote_media_cache_capacity_bytes",
            settings.media_remote_cache_bytes,
        ),
        "# HELP kaede_federation_remote_media_cache_quota_rejections_total "
        "Remote-media cache admissions deferred at the hard ceiling.",
        "# TYPE kaede_federation_remote_media_cache_quota_rejections_total counter",
        _sample(
            "kaede_federation_remote_media_cache_quota_rejections_total",
            media_cache_quota_rejections,
        ),
        "# HELP kaede_job_duration_seconds_total Cumulative observed task duration.",
        "# TYPE kaede_job_duration_seconds_total counter",
        "# HELP kaede_job_runs_total Observed task executions.",
        "# TYPE kaede_job_runs_total counter",
        "# HELP kaede_job_failures_total Observed failed task executions.",
        "# TYPE kaede_job_failures_total counter",
        "# HELP kaede_job_last_duration_seconds Most recent observed task duration.",
        "# TYPE kaede_job_last_duration_seconds gauge",
    ]
    for name, values in sorted(jobs.items()):
        labels = {"job": name}
        lines.extend(
            [
                _sample(
                    "kaede_job_duration_seconds_total",
                    values.get("duration_us", 0) / 1_000_000,
                    labels=labels,
                ),
                _sample("kaede_job_runs_total", values.get("count", 0), labels=labels),
                _sample("kaede_job_failures_total", values.get("failures", 0), labels=labels),
                _sample(
                    "kaede_job_last_duration_seconds",
                    values.get("last_duration_us", 0) / 1_000_000,
                    labels=labels,
                ),
            ]
        )
    return "\n".join(lines) + "\n"
