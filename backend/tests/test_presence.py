from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.auth.schemas import SettingsPatch
from app.chat import presence as account_presence
from app.chat.presence_control import (
    apply_presence_control_patch,
    next_presence_transition_at,
    notifications_suppressed_by_presence,
    resolve_effective_presence,
)
from app.db.models import User


@pytest.mark.asyncio
async def test_account_presence_broadcast_includes_private_preference_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list[tuple[str, dict[str, object], int]] = []

    async def set_state(
        _redis: object,
        _user: User,
        status: str,
        **projection: object,
    ) -> int:
        assert status == "invisible"
        assert projection == {"activities": [], "since": None, "afk": False}
        return 17

    async def publish(
        _redis: object,
        topic: str,
        data: dict[str, object],
        *,
        user_domain: str,
        user_id: int,
        generation: int,
    ) -> bool:
        assert (user_domain, user_id) == ("alpha.test", 7)
        published.append((topic, data, generation))
        return True

    monkeypatch.setattr(account_presence, "set_presence_state", set_state)
    monkeypatch.setattr(account_presence, "publish_presence", publish)
    user = User(
        id=7,
        origin_domain="alpha.test",
        is_local=True,
        username="maple",
        email="maple@example.com",
        password_hash="hash",
    )

    visible, generation = await account_presence.broadcast_presence_preference(
        object(),  # type: ignore[arg-type]
        user,
        "invisible",
        [
            "user:alpha.test:7",
            "guild:alpha.test:42",
            "user:other.test:8",
            "guild:alpha.test:42",
        ],
    )

    assert (visible, generation) == ("offline", 17)
    expected = [
        (
            "user:alpha.test:7",
            {
                "user_id": "7",
                "user_domain": "alpha.test",
                "status": "offline",
                "activities": [],
                "since": None,
                "afk": False,
                "client_status": {},
                "preference": "invisible",
            },
            17,
        ),
        (
            "guild:alpha.test:42",
            {
                "user_id": "7",
                "user_domain": "alpha.test",
                "status": "offline",
                "activities": [],
                "since": None,
                "afk": False,
                "client_status": {},
            },
            17,
        ),
    ]

    assert len(published) == len(expected)
    assert {topic: (data, generation) for topic, data, generation in published} == {
        topic: (data, generation) for topic, data, generation in expected
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["online", "idle", "dnd", "invisible"])
async def test_cached_friend_presence_preserves_status_and_hides_invisible(monkeypatch, status):
    from unittest.mock import AsyncMock

    redis = AsyncMock()
    redis.get.return_value = account_presence.encode_presence_state(
        status,
        generation=17,
        expires_at=9999999999,
        activities=[{"name": "Private activity", "type": 0}],
        since=123,
        afk=True,
    )
    publish = AsyncMock()
    monkeypatch.setattr(account_presence, "publish_presence", publish)
    user = User(id=7, origin_domain="alpha.test", username="maple", is_local=True)
    await account_presence.publish_cached_presence(redis, user, ["user:alpha.test:8"])
    data = publish.await_args.args[2]
    assert data["status"] == ("offline" if status == "invisible" else status)
    assert "preference" not in data
    assert publish.await_args.kwargs["generation"] == 17
    if status == "invisible":
        assert data["activities"] == []
        assert data["since"] is None
        assert data["afk"] is False


def test_timed_presence_expires_back_to_online_or_active_schedule() -> None:
    start = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    updated, effective = apply_presence_control_patch(
        {},
        presence_preference="dnd",
        presence_duration_minutes=60,
        now_utc=start,
    )
    assert effective.effective_status == "dnd"
    assert effective.source == "timed"
    assert effective.suppress_notifications is True
    assert effective.timed_expires_at == datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
    assert next_presence_transition_at(updated, start) == datetime(2026, 11, 26, 11, 0, tzinfo=UTC)

    # After 1 hour, reverts to Online (Discord behavior).
    after = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
    reverted = resolve_effective_presence(updated, after)
    assert reverted.effective_status == "online"
    assert reverted.source == "manual"
    assert reverted.suppress_notifications is False


def test_overnight_schedule_and_skip_once_behavior() -> None:
    # Friday 23:00 -> Saturday 07:00 in America/Los_Angeles (PST = UTC-8 in November).
    # 2026-11-27 is Friday (day index 5 when Sunday=0).
    schedules: list[dict[str, object]] = [
        {
            "id": "sleep",
            "name": "Sleep",
            "status": "dnd",
            "days": [5],
            "start_minute": 23 * 60,
            "end_minute": 7 * 60,
            "enabled": True,
        }
    ]
    before_sleep = datetime(2026, 11, 28, 6, 30, tzinfo=UTC)  # Fri 22:30 PST
    stored, eff_before = apply_presence_control_patch(
        {},
        presence_timezone="America/Los_Angeles",
        presence_schedules=schedules,
        now_utc=before_sleep,
    )
    assert eff_before.effective_status == "online"
    assert eff_before.source == "manual"
    assert next_presence_transition_at(stored, before_sleep) == datetime(
        2026, 11, 28, 7, 0, tzinfo=UTC
    )

    during_night = datetime(2026, 11, 28, 7, 30, tzinfo=UTC)  # Fri 23:30 PST
    eff_night = resolve_effective_presence(stored, during_night)
    assert eff_night.effective_status == "dnd"
    assert eff_night.source == "schedule"
    assert eff_night.active_schedule_name == "Sleep"
    assert eff_night.active_schedule_ends_at == datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
    assert notifications_suppressed_by_presence(stored, during_night) is True

    early_morning = datetime(2026, 11, 28, 14, 30, tzinfo=UTC)  # Sat 06:30 PST
    eff_morning = resolve_effective_presence(stored, early_morning)
    assert eff_morning.effective_status == "dnd"
    assert eff_morning.source == "schedule"

    # Manually picking Online during Sleep skips only tonight's occurrence.
    overridden, eff_override = apply_presence_control_patch(
        stored,
        presence_preference="online",
        now_utc=during_night,
    )
    assert eff_override.effective_status == "online"
    assert eff_override.source == "override"
    assert eff_override.skipped_schedule_name == "Sleep"
    assert eff_override.skipped_schedule_until == datetime(2026, 11, 28, 15, 0, tzinfo=UTC)
    assert notifications_suppressed_by_presence(overridden, during_night) is False

    # Saturday 23:30 PST does not match a Friday-only schedule.
    sat_night = datetime(2026, 11, 29, 7, 30, tzinfo=UTC)
    assert resolve_effective_presence(stored, sat_night).effective_status == "online"

    # Next Friday 23:30 PST activates Sleep again automatically.
    next_fri_night = datetime(2026, 12, 5, 7, 30, tzinfo=UTC)
    assert resolve_effective_presence(overridden, next_fri_night).effective_status == "dnd"


def test_schedule_ends_once_when_clocks_turn_back() -> None:
    first = datetime(2026, 11, 1, 8, 15, tzinfo=UTC)  # First 01:15 in Los Angeles.
    stored, effective = apply_presence_control_patch(
        {},
        presence_timezone="America/Los_Angeles",
        presence_schedules=[
            dict(id="quiet", name="Quiet", status="dnd", days=[0], start_minute=60, end_minute=90)
        ],
        now_utc=first,
    )
    assert effective.effective_status == "dnd"
    assert next_presence_transition_at(stored, first) == first + timedelta(minutes=15)
    assert resolve_effective_presence(stored, first + timedelta(hours=1)).source == "manual"


def test_malformed_legacy_schedule_is_ignored() -> None:
    settings = {
        "presence_control": {
            "schedules": [
                dict(id="old", name="Old", status={}, days=[0], start_minute=0, end_minute=60)
            ]
        }
    }
    assert resolve_effective_presence(settings).schedules == ()


async def test_stale_schedule_edit_preserves_the_newer_list(postgres_schema, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api import users

    await postgres_schema.execute(
        text("CREATE TABLE user_settings (LIKE public.user_settings INCLUDING DEFAULTS)")
    )
    await postgres_schema.execute(text("CREATE TABLE guild_members (LIKE public.guild_members)"))
    await postgres_schema.execute(
        text("INSERT INTO user_settings (user_id, user_domain) VALUES (7, 'home.test')")
    )
    monkeypatch.setattr(users, "publish_committed_presence", AsyncMock())
    editor = SimpleNamespace(user=User(id=7, origin_domain="home.test"))
    rule = dict(id="quiet", name="Quiet", status="dnd", days=[1], start_minute=60, end_minute=90)
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:

        async def save(**changes):
            return await users.patch_user_settings(
                SettingsPatch.model_validate(changes), editor, session, AsyncMock()
            )

        await save(presence_schedules=[rule], expected_presence_schedules=[])
        with pytest.raises(HTTPException) as error:
            await save(presence_schedules=[], expected_presence_schedules=[])
        assert error.value.status_code == 409
        assert error.value.detail["code"] == "PRESENCE_SCHEDULES_CHANGED"
        updated = await save(presence_schedules=[], expected_presence_schedules=[rule])
        assert updated["presence_state"]["schedules"] == []


async def test_delayed_status_publication_respects_a_newer_invisible_setting(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.api.users import patch_user_settings
    from app.chat import presence_control
    from app.db.models import UserSettings

    user = User(id=7, origin_domain="home.test")
    session = AsyncMock()
    # The second read follows commit, after another device changed the setting.
    session.scalar.side_effect = [
        UserSettings(notification_settings={}),
        {"presence_control": {"base_status": "invisible"}},
    ]
    session.execute.return_value = SimpleNamespace(all=lambda: [])
    publish = AsyncMock(return_value=("offline", 2))
    monkeypatch.setattr(presence_control, "broadcast_presence_preference", publish)
    monkeypatch.setattr(presence_control, "enqueue_best_effort", AsyncMock())
    await patch_user_settings(
        SettingsPatch(presence_preference="online"),
        SimpleNamespace(user=user),
        session,
        AsyncMock(),
    )
    assert publish.await_args.args[2] == "invisible"


def test_invisible_forever_keeps_privacy_while_silencing_dnd_schedule() -> None:
    now = datetime(2026, 11, 26, 23, 30, tzinfo=UTC)
    stored, _ = apply_presence_control_patch(
        {},
        presence_preference="invisible",
        presence_timezone="UTC",
        presence_schedules=[
            {
                "id": "sleep",
                "name": "Sleep",
                "status": "dnd",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "start_minute": 23 * 60,
                "end_minute": 7 * 60,
                "enabled": True,
            }
        ],
        now_utc=now,
    )
    resolved = resolve_effective_presence(stored, now)
    assert resolved.effective_status == "invisible"
    assert resolved.suppress_notifications is True
    assert resolved.active_schedule_name == "Sleep"
    assert notifications_suppressed_by_presence(stored, now) is True


def test_online_schedule_and_top_down_overlap_priority() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)  # Thursday (day 4) 12:00 UTC
    stored, resolved = apply_presence_control_patch(
        {},
        presence_preference="dnd",
        presence_timezone="UTC",
        presence_schedules=[
            {
                "id": "office",
                "name": "Office Hours",
                "status": "online",
                "days": [1, 2, 3, 4, 5],
                "start_minute": 9 * 60,
                "end_minute": 17 * 60,
                "enabled": True,
            },
            {
                "id": "all_day_quiet",
                "name": "Quiet",
                "status": "dnd",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "start_minute": 0,
                "end_minute": 23 * 60 + 59,
                "enabled": True,
            },
        ],
        now_utc=now,
    )
    # Setting preference="dnd" at 12:00 when Office Hours (online) is active skips once;
    # resetting override lets Office Hours (topmost schedule) win with Online.
    _, resumed = apply_presence_control_patch(stored, presence_reset_override=True, now_utc=now)
    assert resumed.effective_status == "online"
    assert resumed.source == "schedule"
    assert resumed.active_schedule_id == "office"
    assert resumed.suppress_notifications is False
    assert resolved.effective_status == "dnd"


def test_skip_an_online_schedule_preserves_the_idle_base():
    before = datetime(2026, 11, 23, 8, tzinfo=UTC)
    during = before + timedelta(hours=2)
    stored, _ = apply_presence_control_patch(
        {},
        presence_preference="idle",
        presence_timezone="UTC",
        presence_schedules=[
            {
                "id": "work",
                "name": "Work",
                "status": "online",
                "days": [1],
                "start_minute": 540,
                "end_minute": 1020,
                "enabled": True,
            }
        ],
        now_utc=before,
    )
    assert resolve_effective_presence(stored, during).effective_status == "online"
    changed, effective = apply_presence_control_patch(
        stored, presence_skip_active_schedule=True, now_utc=during
    )
    assert effective.effective_status == effective.base_status == "idle"
    assert resolve_effective_presence(changed, during + timedelta(days=7)).source == "schedule"


def test_settings_patch_validates_presence_durations_and_schedules() -> None:
    valid = SettingsPatch(
        presence_preference="dnd",
        presence_duration_minutes=60,
        presence_timezone="America/New_York",
        presence_schedules=[
            {
                "id": "sleep",
                "name": "Sleep",
                "status": "dnd",
                "days": [0, 1, 2, 3, 4, 5, 6],
                "start_minute": 1380,
                "end_minute": 420,
                "enabled": True,
            }
        ],  # type: ignore[arg-type]
    )
    assert valid.presence_duration_minutes == 60
    forever = SettingsPatch(presence_preference="idle", presence_duration_minutes=None)
    assert forever.presence_duration_minutes is None

    with pytest.raises(ValidationError):
        SettingsPatch(presence_timezone="Mars/Olympus_Mons")
    with pytest.raises(ValidationError):
        SettingsPatch.model_validate(
            {
                "presence_schedules": [
                    {
                        "id": "bad",
                        "name": "Same",
                        "status": "dnd",
                        "days": [1],
                        "start_minute": 600,
                        "end_minute": 600,
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_broadcast_presence_state_stays_private_to_user_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: dict[str, dict[str, object]] = {}

    async def set_state(_redis: object, _user: User, _status: str, **_proj: object) -> int:
        return 21

    async def publish(
        _redis: object,
        topic: str,
        data: dict[str, object],
        *,
        user_domain: str,
        user_id: int,
        generation: int,
    ) -> bool:
        published[topic] = data
        return True

    monkeypatch.setattr(account_presence, "set_presence_state", set_state)
    monkeypatch.setattr(account_presence, "publish_presence", publish)
    user = User(id=7, origin_domain="alpha.test", is_local=True, username="maple")
    await account_presence.broadcast_presence_preference(
        object(),  # type: ignore[arg-type]
        user,
        "dnd",
        ["user:alpha.test:7", "guild:alpha.test:42"],
        presence_state={"effective_status": "dnd", "source": "timed"},
    )
    assert "presence_state" in published["user:alpha.test:7"]
    assert "presence_state" not in published["guild:alpha.test:42"]
