from __future__ import annotations

from collections.abc import Awaitable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.events import guild_topic, user_topic
from app.chat.presence import broadcast_presence_preference, decode_presence_state
from app.core.task_wake import enqueue_best_effort
from app.db.models import GuildMember, User, UserSettings

log = structlog.get_logger()

PresencePreferenceStatus = Literal["online", "idle", "dnd", "invisible"]
PRESENCE_STATUSES: frozenset[str] = frozenset({"online", "idle", "dnd", "invisible"})
PRESENCE_DURATIONS_MINUTES: tuple[int, ...] = (15, 60, 480, 1440, 4320)
MAX_PRESENCE_DURATION_MINUTES = 10_080  # 7 days
MAX_PRESENCE_SCHEDULES = 10
PRESENCE_TRANSITIONS_KEY = "presence:transitions"
PRESENCE_TRANSITIONS_SEEDED_KEY = "presence:transitions:seeded"
PRESENCE_TRANSITIONS_SEED_TTL_SECONDS = 3600


def parse_timezone(value: object) -> tuple[str, ZoneInfo]:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate and len(candidate) <= 64:
            try:
                return candidate, ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                pass
    return "UTC", ZoneInfo("UTC")


def is_valid_timezone(value: str) -> bool:
    candidate = value.strip()
    if not candidate or len(candidate) > 64:
        return False
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def _parse_iso_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class PresenceScheduleRule:
    id: str
    name: str
    status: PresencePreferenceStatus
    days: tuple[int, ...]  # 0=Sun .. 6=Sat
    start_minute: int  # 0..1439
    end_minute: int  # 0..1439
    enabled: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "days": list(self.days),
            "start_minute": self.start_minute,
            "end_minute": self.end_minute,
            "enabled": self.enabled,
        }


@dataclass(frozen=True, slots=True)
class PresenceControlConfig:
    base_status: PresencePreferenceStatus
    timed_status: PresencePreferenceStatus | None
    timed_expires_at: datetime | None
    skip_schedule_until: datetime | None
    skipped_schedule_name: str | None
    timezone: str
    schedules: tuple[PresenceScheduleRule, ...]

    def to_storage(self) -> dict[str, object]:
        return {
            "base_status": self.base_status,
            "timed_status": self.timed_status,
            "timed_expires_at": _format_iso_utc(self.timed_expires_at),
            "skip_schedule_until": _format_iso_utc(self.skip_schedule_until),
            "skipped_schedule_name": self.skipped_schedule_name,
            "timezone": self.timezone,
            "schedules": [rule.to_dict() for rule in self.schedules],
        }


@dataclass(frozen=True, slots=True)
class EffectivePresence:
    effective_status: PresencePreferenceStatus
    suppress_notifications: bool
    source: Literal["manual", "timed", "schedule", "override"]
    base_status: PresencePreferenceStatus
    timed_status: PresencePreferenceStatus | None
    timed_expires_at: datetime | None
    active_schedule_id: str | None
    active_schedule_name: str | None
    active_schedule_status: PresencePreferenceStatus | None
    active_schedule_ends_at: datetime | None
    skipped_schedule_name: str | None
    skipped_schedule_until: datetime | None
    timezone: str
    schedules: tuple[PresenceScheduleRule, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "effective_status": self.effective_status,
            "suppress_notifications": self.suppress_notifications,
            "source": self.source,
            "base_status": self.base_status,
            "timed_status": self.timed_status,
            "timed_expires_at": _format_iso_utc(self.timed_expires_at),
            "active_schedule_id": self.active_schedule_id,
            "active_schedule_name": self.active_schedule_name,
            "active_schedule_status": self.active_schedule_status,
            "active_schedule_ends_at": _format_iso_utc(self.active_schedule_ends_at),
            "skipped_schedule_name": self.skipped_schedule_name,
            "skipped_schedule_until": _format_iso_utc(self.skipped_schedule_until),
            "timezone": self.timezone,
            "schedules": [rule.to_dict() for rule in self.schedules],
        }


def _normalize_schedule_rule(raw: object) -> PresenceScheduleRule | None:
    if not isinstance(raw, Mapping):
        return None
    rule_id = raw.get("id")
    name = raw.get("name")
    status = raw.get("status")
    days_raw = raw.get("days")
    start_minute = raw.get("start_minute")
    end_minute = raw.get("end_minute")
    enabled = raw.get("enabled", True)
    if (
        not isinstance(rule_id, str)
        or not rule_id.strip()
        or len(rule_id) > 36
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(status, str)
        or status not in PRESENCE_STATUSES
        or not isinstance(days_raw, list)
        or type(start_minute) is not int
        or type(end_minute) is not int
        or type(enabled) is not bool
    ):
        return None
    if not (0 <= start_minute <= 1439 and 0 <= end_minute <= 1439) or start_minute == end_minute:
        return None
    cleaned_days = tuple(sorted({day for day in days_raw if type(day) is int and 0 <= day <= 6}))
    if not cleaned_days:
        return None
    return PresenceScheduleRule(
        id=rule_id.strip(),
        name=name.strip()[:32],
        status=cast(PresencePreferenceStatus, status),
        days=cleaned_days,
        start_minute=start_minute,
        end_minute=end_minute,
        enabled=enabled,
    )


def parse_presence_control(
    notification_settings: Mapping[str, object] | None,
) -> PresenceControlConfig:
    """Decode presence_control safely from UserSettings.notification_settings."""

    settings_map = notification_settings if isinstance(notification_settings, Mapping) else {}
    legacy_pref = settings_map.get("presence_preference")
    fallback_status: PresencePreferenceStatus = (
        cast(PresencePreferenceStatus, legacy_pref)
        if isinstance(legacy_pref, str) and legacy_pref in PRESENCE_STATUSES
        else "online"
    )
    raw = settings_map.get("presence_control")
    if not isinstance(raw, Mapping):
        return PresenceControlConfig(
            base_status=fallback_status,
            timed_status=None,
            timed_expires_at=None,
            skip_schedule_until=None,
            skipped_schedule_name=None,
            timezone="UTC",
            schedules=(),
        )

    base_raw = raw.get("base_status")
    base_status: PresencePreferenceStatus = (
        cast(PresencePreferenceStatus, base_raw)
        if isinstance(base_raw, str) and base_raw in PRESENCE_STATUSES
        else fallback_status
    )
    timed_raw = raw.get("timed_status")
    timed_status: PresencePreferenceStatus | None = (
        cast(PresencePreferenceStatus, timed_raw)
        if isinstance(timed_raw, str) and timed_raw in PRESENCE_STATUSES
        else None
    )
    timed_expires_at = _parse_iso_utc(raw.get("timed_expires_at"))
    if timed_status is None or timed_expires_at is None:
        timed_status = None
        timed_expires_at = None

    skip_schedule_until = _parse_iso_utc(raw.get("skip_schedule_until"))
    skipped_raw = raw.get("skipped_schedule_name")
    skipped_schedule_name = (
        skipped_raw.strip()[:32]
        if skip_schedule_until is not None and isinstance(skipped_raw, str) and skipped_raw.strip()
        else None
    )

    timezone_name, _ = parse_timezone(raw.get("timezone"))
    schedules_raw = raw.get("schedules")
    schedules: list[PresenceScheduleRule] = []
    seen_ids: set[str] = set()
    if isinstance(schedules_raw, list):
        for item in schedules_raw[:MAX_PRESENCE_SCHEDULES]:
            rule = _normalize_schedule_rule(item)
            if rule is not None and rule.id not in seen_ids:
                seen_ids.add(rule.id)
                schedules.append(rule)

    return PresenceControlConfig(
        base_status=base_status,
        timed_status=timed_status,
        timed_expires_at=timed_expires_at,
        skip_schedule_until=skip_schedule_until,
        skipped_schedule_name=skipped_schedule_name,
        timezone=timezone_name,
        schedules=tuple(schedules),
    )


def _local_boundary_utc(local_date: datetime, minute_of_day: int, tz: ZoneInfo) -> datetime:
    hour, minute = divmod(minute_of_day, 60)
    return datetime(
        local_date.year,
        local_date.month,
        local_date.day,
        hour,
        minute,
        tzinfo=tz,
    ).astimezone(UTC)


def _match_schedule_at(
    rule: PresenceScheduleRule,
    local_now: datetime,
    tz: ZoneInfo,
) -> datetime | None:
    """If `rule` is active at `local_now`, return its current occurrence end time in UTC."""

    if not rule.enabled or not rule.days or rule.start_minute == rule.end_minute:
        return None
    now = local_now.astimezone(UTC)
    overnight = rule.start_minute > rule.end_minute
    for offset in (0, 1) if overnight else (0,):
        start_date = local_now - timedelta(days=offset)
        if (start_date.weekday() + 1) % 7 not in rule.days:
            continue
        end_date = start_date + timedelta(days=int(overnight))
        start = _local_boundary_utc(start_date, rule.start_minute, tz)
        end = _local_boundary_utc(end_date, rule.end_minute, tz)
        if start <= now < end:
            return end
    return None


def _find_active_schedule(
    config: PresenceControlConfig,
    now_utc: datetime,
) -> tuple[PresenceScheduleRule, datetime] | None:
    _, tz = parse_timezone(config.timezone)
    local_now = now_utc.astimezone(tz)
    for rule in config.schedules:
        ends_at = _match_schedule_at(rule, local_now, tz)
        if ends_at is not None:
            return rule, ends_at
    return None


def resolve_effective_presence(
    notification_settings: Mapping[str, object] | None,
    now_utc: datetime | None = None,
) -> EffectivePresence:
    """Resolve the authoritative effective presence and notification suppression state."""

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    config = parse_presence_control(notification_settings)
    active_match = _find_active_schedule(config, now)
    active_rule = active_match[0] if active_match is not None else None
    active_ends_at = active_match[1] if active_match is not None else None

    timed_active = (
        config.timed_status is not None
        and config.timed_expires_at is not None
        and config.timed_expires_at > now
    )
    skip_active = config.skip_schedule_until is not None and config.skip_schedule_until > now

    if timed_active and config.timed_status is not None and config.timed_expires_at is not None:
        return EffectivePresence(
            effective_status=config.timed_status,
            suppress_notifications=(config.timed_status == "dnd"),
            source="timed",
            base_status=config.base_status,
            timed_status=config.timed_status,
            timed_expires_at=config.timed_expires_at,
            active_schedule_id=active_rule.id if active_rule else None,
            active_schedule_name=active_rule.name if active_rule else None,
            active_schedule_status=active_rule.status if active_rule else None,
            active_schedule_ends_at=active_ends_at,
            skipped_schedule_name=None,
            skipped_schedule_until=None,
            timezone=config.timezone,
            schedules=config.schedules,
        )

    if config.base_status == "invisible":
        # Privacy guard: manual Invisible (Forever) never flips to a visible
        # status on a schedule, but an active DND schedule still silences alerts.
        silent_dnd = active_rule is not None and not skip_active and active_rule.status == "dnd"
        return EffectivePresence(
            effective_status="invisible",
            suppress_notifications=silent_dnd,
            source="manual",
            base_status=config.base_status,
            timed_status=None,
            timed_expires_at=None,
            active_schedule_id=active_rule.id if active_rule else None,
            active_schedule_name=active_rule.name if active_rule else None,
            active_schedule_status=active_rule.status if active_rule else None,
            active_schedule_ends_at=active_ends_at,
            skipped_schedule_name=(
                config.skipped_schedule_name if skip_active and active_rule else None
            ),
            skipped_schedule_until=(
                config.skip_schedule_until if skip_active and active_rule else None
            ),
            timezone=config.timezone,
            schedules=config.schedules,
        )

    if active_rule is not None:
        if skip_active:
            return EffectivePresence(
                effective_status=config.base_status,
                suppress_notifications=(config.base_status == "dnd"),
                source="override",
                base_status=config.base_status,
                timed_status=None,
                timed_expires_at=None,
                active_schedule_id=active_rule.id,
                active_schedule_name=active_rule.name,
                active_schedule_status=active_rule.status,
                active_schedule_ends_at=active_ends_at,
                skipped_schedule_name=config.skipped_schedule_name or active_rule.name,
                skipped_schedule_until=config.skip_schedule_until,
                timezone=config.timezone,
                schedules=config.schedules,
            )
        return EffectivePresence(
            effective_status=active_rule.status,
            suppress_notifications=(active_rule.status == "dnd"),
            source="schedule",
            base_status=config.base_status,
            timed_status=None,
            timed_expires_at=None,
            active_schedule_id=active_rule.id,
            active_schedule_name=active_rule.name,
            active_schedule_status=active_rule.status,
            active_schedule_ends_at=active_ends_at,
            skipped_schedule_name=None,
            skipped_schedule_until=None,
            timezone=config.timezone,
            schedules=config.schedules,
        )

    return EffectivePresence(
        effective_status=config.base_status,
        suppress_notifications=(config.base_status == "dnd"),
        source="manual",
        base_status=config.base_status,
        timed_status=None,
        timed_expires_at=None,
        active_schedule_id=None,
        active_schedule_name=None,
        active_schedule_status=None,
        active_schedule_ends_at=None,
        skipped_schedule_name=None,
        skipped_schedule_until=None,
        timezone=config.timezone,
        schedules=config.schedules,
    )


def notifications_suppressed_by_presence(
    notification_settings: Mapping[str, object] | None,
    now_utc: datetime | None = None,
) -> bool:
    return resolve_effective_presence(notification_settings, now_utc).suppress_notifications


def next_presence_transition_at(
    notification_settings: Mapping[str, object] | None,
    now_utc: datetime | None = None,
) -> datetime | None:
    """Return the earliest future UTC timestamp when presence state could change."""

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    config = parse_presence_control(notification_settings)
    candidates: list[datetime] = []

    if (
        config.timed_status is not None
        and config.timed_expires_at is not None
        and config.timed_expires_at > now
    ):
        candidates.append(config.timed_expires_at)

    if config.skip_schedule_until is not None and config.skip_schedule_until > now:
        candidates.append(config.skip_schedule_until)

    _, tz = parse_timezone(config.timezone)
    local_now = now.astimezone(tz)
    for rule in config.schedules:
        if not rule.enabled or not rule.days or rule.start_minute == rule.end_minute:
            continue
        active_end = _match_schedule_at(rule, local_now, tz)
        if active_end is not None and active_end > now:
            candidates.append(active_end)
        for day_offset in range(0, 8):
            target_date = local_now + timedelta(days=day_offset)
            target_day = (target_date.weekday() + 1) % 7
            if target_day not in rule.days:
                continue
            start_utc = _local_boundary_utc(target_date, rule.start_minute, tz)
            if start_utc > now:
                candidates.append(start_utc)
                end_date = (
                    target_date + timedelta(days=1)
                    if rule.start_minute > rule.end_minute
                    else target_date
                )
                end_utc = _local_boundary_utc(end_date, rule.end_minute, tz)
                if end_utc > now:
                    candidates.append(end_utc)
                break

    return min(candidates) if candidates else None


def apply_presence_control_patch(
    notification_settings: Mapping[str, object] | None,
    *,
    presence_preference: PresencePreferenceStatus | None = None,
    presence_duration_minutes: int | None = None,
    presence_duration_provided: bool = False,
    presence_reset_override: bool = False,
    presence_skip_active_schedule: bool = False,
    presence_timezone: str | None = None,
    presence_schedules: list[dict[str, object]] | None = None,
    now_utc: datetime | None = None,
) -> tuple[dict[str, object], EffectivePresence]:
    """Return updated notification_settings dict and resolved EffectivePresence."""

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    existing = dict(notification_settings) if isinstance(notification_settings, Mapping) else {}
    config = parse_presence_control(existing)

    base_status = config.base_status
    timed_status = config.timed_status
    timed_expires_at = config.timed_expires_at
    skip_schedule_until = config.skip_schedule_until
    skipped_schedule_name = config.skipped_schedule_name
    timezone_name = config.timezone
    schedules = config.schedules

    if presence_timezone is not None:
        timezone_name, _ = parse_timezone(presence_timezone)

    if presence_schedules is not None:
        parsed_rules: list[PresenceScheduleRule] = []
        seen_ids: set[str] = set()
        for raw in presence_schedules[:MAX_PRESENCE_SCHEDULES]:
            rule = _normalize_schedule_rule(raw)
            if rule is not None and rule.id not in seen_ids:
                seen_ids.add(rule.id)
                parsed_rules.append(rule)
        schedules = tuple(parsed_rules)
        # Re-evaluate schedules cleanly after editing schedule definitions.
        skip_schedule_until = None
        skipped_schedule_name = None

    working_config = PresenceControlConfig(
        base_status=base_status,
        timed_status=timed_status,
        timed_expires_at=timed_expires_at,
        skip_schedule_until=skip_schedule_until,
        skipped_schedule_name=skipped_schedule_name,
        timezone=timezone_name,
        schedules=schedules,
    )

    if presence_reset_override:
        timed_status = None
        timed_expires_at = None
        skip_schedule_until = None
        skipped_schedule_name = None

    if presence_skip_active_schedule:
        active_match = _find_active_schedule(working_config, now)
        if active_match is not None:
            active_rule, skip_schedule_until = active_match
            skipped_schedule_name = active_rule.name

    if presence_preference is not None:
        if presence_duration_minutes is not None:
            # Timed status (Discord-style): expires back to Online (or active schedule).
            base_status = "online"
            timed_status = presence_preference
            timed_expires_at = now + timedelta(minutes=presence_duration_minutes)
            skip_schedule_until = None
            skipped_schedule_name = None
        else:
            # Forever / immediate manual status.
            base_status = presence_preference
            timed_status = None
            timed_expires_at = None
            active_match = _find_active_schedule(working_config, now)
            if active_match is not None and presence_preference != "invisible":
                active_rule, active_ends_at = active_match
                if active_rule.status != presence_preference:
                    skip_schedule_until = active_ends_at
                    skipped_schedule_name = active_rule.name
                else:
                    skip_schedule_until = None
                    skipped_schedule_name = None
            else:
                skip_schedule_until = None
                skipped_schedule_name = None
    elif presence_duration_provided and presence_duration_minutes is None:
        # Explicit clear of timer without changing status.
        timed_status = None
        timed_expires_at = None

    # Garbage-collect already expired timestamps on write.
    if timed_expires_at is not None and timed_expires_at <= now:
        timed_status = None
        timed_expires_at = None
    if skip_schedule_until is not None and skip_schedule_until <= now:
        skip_schedule_until = None
        skipped_schedule_name = None

    updated_config = PresenceControlConfig(
        base_status=base_status,
        timed_status=timed_status,
        timed_expires_at=timed_expires_at,
        skip_schedule_until=skip_schedule_until,
        skipped_schedule_name=skipped_schedule_name,
        timezone=timezone_name,
        schedules=schedules,
    )
    existing["presence_control"] = updated_config.to_storage()
    effective = resolve_effective_presence(existing, now)
    existing["presence_preference"] = effective.effective_status
    return existing, effective


async def sync_presence_transition_index(
    redis: Redis,
    user_domain: str,
    user_id: int,
    notification_settings: Mapping[str, object] | None,
    now_utc: datetime | None = None,
) -> None:
    """Register or remove the user's next presence transition timestamp in Redis."""

    handle = f"{user_domain}:{user_id}"
    next_at = next_presence_transition_at(notification_settings, now_utc)
    if next_at is None:
        await cast(Awaitable[int], redis.zrem(PRESENCE_TRANSITIONS_KEY, handle))
    else:
        await cast(
            Awaitable[int],
            redis.zadd(PRESENCE_TRANSITIONS_KEY, {handle: int(next_at.timestamp())}),
        )


async def publish_committed_presence(
    redis: Redis,
    session: AsyncSession,
    user: User,
    topics: list[str],
    *,
    online_only: bool = False,
    now_utc: datetime | None = None,
) -> None:
    from app.tasks import federation_presence_fanout

    # A newer settings edit can commit between the original commit and fanout.
    # Read and project the latest preference under the same lock as gateway updates.
    preferences = await session.scalar(
        select(UserSettings.notification_settings)
        .where(UserSettings.user_id == user.id, UserSettings.user_domain == user.origin_domain)
        .with_for_update(read=True)
    )
    if preferences is None:
        return
    now = now_utc or datetime.now(UTC)
    await sync_presence_transition_index(redis, user.origin_domain, user.id, preferences, now)
    if online_only and not decode_presence_state(
        await redis.get(f"presence:{user.origin_domain}:{user.id}")
    ):
        return
    effective = resolve_effective_presence(preferences, now)
    status, generation = await broadcast_presence_preference(
        redis, user, effective.effective_status, topics, presence_state=effective.to_payload()
    )
    await enqueue_best_effort(
        federation_presence_fanout, user.id, user.origin_domain, status, generation
    )


async def seed_presence_transitions(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    now_utc: datetime | None = None,
) -> int:
    """Rebuild the Redis transition ZSET from Postgres after a Redis restart."""

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    seeded = 0
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    UserSettings.user_id,
                    UserSettings.user_domain,
                    UserSettings.notification_settings,
                ).where(UserSettings.notification_settings.has_key("presence_control"))
            )
        ).all()
    mapping: dict[str, int] = {}
    for user_id, user_domain, notification_settings in rows:
        next_at = next_presence_transition_at(notification_settings, now)
        if next_at is not None:
            mapping[f"{user_domain}:{user_id}"] = int(next_at.timestamp())
    if mapping:
        # An hourly reseed must preserve transitions already due for processing.
        seeded = await cast(Awaitable[int], redis.zadd(PRESENCE_TRANSITIONS_KEY, mapping, nx=True))
    await redis.set(
        PRESENCE_TRANSITIONS_SEEDED_KEY,
        "1",
        ex=PRESENCE_TRANSITIONS_SEED_TTL_SECONDS,
    )
    return seeded


async def process_due_presence_transitions(
    redis: Redis,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now_utc: datetime | None = None,
    limit: int = 200,
) -> int:
    """Advance any timed statuses or recurring schedules whose boundary has passed."""

    now = (now_utc or datetime.now(UTC)).astimezone(UTC)
    due = await cast(
        Awaitable[list[Any]],
        redis.zrangebyscore(
            PRESENCE_TRANSITIONS_KEY,
            min=0,
            max=int(now.timestamp()),
            start=0,
            num=limit,
        ),
    )
    if not due:
        return 0

    processed = 0
    for raw_handle in due:
        handle = raw_handle.decode("utf-8") if isinstance(raw_handle, bytes) else str(raw_handle)
        try:
            domain, user_id_text = handle.rsplit(":", 1)
            user_id = int(user_id_text)
        except ValueError:
            await cast(Awaitable[int], redis.zrem(PRESENCE_TRANSITIONS_KEY, handle))
            continue

        async with sessionmaker() as session:
            user_settings = await session.scalar(
                select(UserSettings)
                .where(
                    UserSettings.user_id == user_id,
                    UserSettings.user_domain == domain,
                )
                .with_for_update()
            )
            if user_settings is None:
                await cast(Awaitable[int], redis.zrem(PRESENCE_TRANSITIONS_KEY, handle))
                continue
            user = await session.get(User, (user_id, domain))
            if user is None:
                await cast(Awaitable[int], redis.zrem(PRESENCE_TRANSITIONS_KEY, handle))
                continue

            updated_settings, _ = apply_presence_control_patch(
                user_settings.notification_settings,
                now_utc=now,
            )
            if updated_settings != user_settings.notification_settings:
                user_settings.notification_settings = updated_settings
            guild_refs = (
                await session.execute(
                    select(GuildMember.guild_id, GuildMember.guild_domain).where(
                        GuildMember.user_id == user.id,
                        GuildMember.user_domain == user.origin_domain,
                    )
                )
            ).all()
            topics = [
                user_topic(user.origin_domain, user.id),
                *(guild_topic(g_domain, g_id) for g_id, g_domain in guild_refs),
            ]
            await session.commit()
            await publish_committed_presence(
                redis, session, user, topics, online_only=True, now_utc=now_utc
            )
        processed += 1
    return processed
