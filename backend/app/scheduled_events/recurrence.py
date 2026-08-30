from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.chat.schemas import RequestModel

RECURRENCE_YEARLY = 0
RECURRENCE_MONTHLY = 1
RECURRENCE_WEEKLY = 2
RECURRENCE_DAILY = 3

DAILY_WEEKDAY_SETS = frozenset(
    {
        (0, 1, 2, 3, 4),
        (1, 2, 3, 4, 5),
        (6, 0, 1, 2, 3),
        (4, 5),
        (5, 6),
        (6, 0),
    }
)


class ScheduledEventNWeekday(RequestModel):
    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1, le=5)
    day: int = Field(ge=0, le=6)

    @field_validator("n", "day", mode="before")
    @classmethod
    def strict_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("recurrence n-weekday values must be integers")
        return value


class ScheduledEventRecurrenceRule(RequestModel):
    """Discord's currently writable subset of its iCalendar recurrence rule."""

    model_config = ConfigDict(extra="forbid")

    start: datetime
    end: None = None
    frequency: Literal[0, 1, 2, 3]
    interval: int = Field(default=1, ge=1, le=2)
    by_weekday: list[int] | None = Field(default=None, min_length=1, max_length=7)
    by_n_weekday: list[ScheduledEventNWeekday] | None = Field(
        default=None, min_length=1, max_length=1
    )
    by_month: list[int] | None = Field(default=None, min_length=1, max_length=1)
    by_month_day: list[int] | None = Field(default=None, min_length=1, max_length=1)
    by_year_day: None = None
    count: None = None

    @field_validator("frequency", "interval", mode="before")
    @classmethod
    def strict_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("recurrence frequency and interval must be integers")
        return value

    @field_validator("by_weekday", "by_month", "by_month_day", mode="before")
    @classmethod
    def strict_integer_list(cls, value: object) -> object:
        if value is not None and (
            not isinstance(value, list)
            or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
        ):
            raise ValueError("recurrence selectors must be integer arrays")
        return value

    @field_validator("start")
    @classmethod
    def timezone_start(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recurrence start must include a timezone offset")
        return value.astimezone(UTC)

    @field_validator("by_weekday")
    @classmethod
    def valid_weekdays(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (
            any(day < 0 or day > 6 for day in value) or len(value) != len(set(value))
        ):
            raise ValueError("recurrence weekdays must be unique values from 0 through 6")
        return value

    @field_validator("by_month")
    @classmethod
    def valid_months(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(month < 1 or month > 12 for month in value):
            raise ValueError("recurrence months must be values from 1 through 12")
        return value

    @field_validator("by_month_day")
    @classmethod
    def valid_month_days(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and any(day < 1 or day > 31 for day in value):
            raise ValueError("recurrence month days must be values from 1 through 31")
        return value

    @model_validator(mode="after")
    def discord_writable_subset(self) -> ScheduledEventRecurrenceRule:
        groups = sum(
            (
                self.by_weekday is not None,
                self.by_n_weekday is not None,
                self.by_month is not None or self.by_month_day is not None,
            )
        )
        if groups > 1:
            raise ValueError("recurrence selectors are mutually exclusive")
        if self.interval != 1 and not (self.frequency == RECURRENCE_WEEKLY and self.interval == 2):
            raise ValueError("only weekly recurrence supports an interval of 2")
        if self.by_weekday is not None:
            if self.frequency == RECURRENCE_DAILY:
                if tuple(self.by_weekday) not in DAILY_WEEKDAY_SETS:
                    raise ValueError("daily recurrence uses one of Discord's weekday sets")
            elif self.frequency == RECURRENCE_WEEKLY:
                if len(self.by_weekday) != 1:
                    raise ValueError("weekly recurrence accepts exactly one weekday")
            else:
                raise ValueError("weekdays are valid only for daily or weekly recurrence")
        if self.by_n_weekday is not None and self.frequency != RECURRENCE_MONTHLY:
            raise ValueError("n-weekday recurrence is valid only for monthly events")
        if (self.by_month is None) != (self.by_month_day is None):
            raise ValueError("yearly recurrence requires both month and month day")
        if self.by_month is not None and self.frequency != RECURRENCE_YEARLY:
            raise ValueError("month and month-day recurrence is valid only for yearly events")
        return self


def validate_recurrence_projection(
    raw: object,
    *,
    scheduled_start_time: datetime,
) -> dict[str, object] | None:
    """Validate and normalize an authority-signed recurrence projection."""

    if raw is None:
        return None
    try:
        rule = ScheduledEventRecurrenceRule.model_validate(raw)
    except ValueError as exc:
        raise ValueError("scheduled event recurrence is invalid") from exc
    if rule.start != scheduled_start_time.astimezone(UTC):
        raise ValueError("scheduled event recurrence start does not match its event")
    return rule.model_dump(mode="json")


def _replace_date(value: datetime, year: int, month: int, day: int) -> datetime | None:
    try:
        return value.replace(year=year, month=month, day=day)
    except ValueError:
        return None


def _next_month(value: datetime) -> tuple[int, int]:
    return (value.year + 1, 1) if value.month == 12 else (value.year, value.month + 1)


def _next_rule_occurrence(
    rule: ScheduledEventRecurrenceRule,
    current: datetime,
) -> datetime:
    current = current.astimezone(UTC)
    if rule.frequency == RECURRENCE_DAILY:
        weekdays = set(rule.by_weekday or ())
        candidate = current
        while True:
            candidate += timedelta(days=rule.interval)
            if not weekdays or candidate.weekday() in weekdays:
                return candidate

    if rule.frequency == RECURRENCE_WEEKLY:
        if rule.by_weekday is None:
            return current + timedelta(weeks=rule.interval)
        target_weekday = rule.by_weekday[0]
        anchor_week = rule.start.date() - timedelta(days=rule.start.weekday())
        candidate = current
        while True:
            candidate += timedelta(days=1)
            candidate_week = candidate.date() - timedelta(days=candidate.weekday())
            week_offset = (candidate_week - anchor_week).days // 7
            if (
                candidate.weekday() == target_weekday
                and week_offset >= 0
                and week_offset % rule.interval == 0
            ):
                return candidate

    if rule.frequency == RECURRENCE_MONTHLY:
        year, month = _next_month(current)
        while True:
            if rule.by_n_weekday is not None:
                selector = rule.by_n_weekday[0]
                first = datetime(
                    year,
                    month,
                    1,
                    current.hour,
                    current.minute,
                    current.second,
                    current.microsecond,
                    tzinfo=current.tzinfo,
                )
                day = 1 + (selector.day - first.weekday()) % 7 + 7 * (selector.n - 1)
                monthly_candidate = (
                    first.replace(day=day) if day <= monthrange(year, month)[1] else None
                )
            else:
                monthly_candidate = _replace_date(current, year, month, rule.start.day)
            if monthly_candidate is not None and monthly_candidate > current:
                return monthly_candidate
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)

    month = rule.by_month[0] if rule.by_month is not None else rule.start.month
    day = rule.by_month_day[0] if rule.by_month_day is not None else rule.start.day
    year = current.year + 1
    while True:
        yearly_candidate = _replace_date(current, year, month, day)
        if yearly_candidate is not None and yearly_candidate > current:
            return yearly_candidate
        year += 1


def next_recurrence_start(
    raw: object,
    *,
    current_start: datetime,
    after: datetime | None = None,
) -> datetime:
    """Return the first Discord-subset recurrence strictly after a watermark."""

    rule = ScheduledEventRecurrenceRule.model_validate(raw)
    current = current_start.astimezone(UTC)
    watermark = (after or current).astimezone(UTC)
    for _ in range(10_000):
        current = _next_rule_occurrence(rule, current)
        if current > watermark:
            return current
    raise ValueError("scheduled event recurrence exceeded its bounded search window")


__all__ = (
    "ScheduledEventNWeekday",
    "ScheduledEventRecurrenceRule",
    "next_recurrence_start",
    "validate_recurrence_projection",
)
