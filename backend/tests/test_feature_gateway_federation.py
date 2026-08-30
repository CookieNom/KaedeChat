from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import Channel
from app.federation.guilds import (
    GUILD_MUTATION_EVENT_TYPES,
    SNAPSHOT_NEUTRAL_GUILD_EVENTS,
    _validated_automod_execution,
    _validated_automod_rule,
    _validated_scheduled_event,
    _validated_soundboard_collection,
    _validated_soundboard_sound,
)

DOMAIN = "chat.example"


def guild() -> SimpleNamespace:
    return SimpleNamespace(id=10, origin_domain=DOMAIN)


def scheduled_event_payload() -> dict[str, object]:
    return {
        "id": "20",
        "origin_domain": DOMAIN,
        "guild_id": "10",
        "guild_domain": DOMAIN,
        "channel_id": "30",
        "channel_domain": DOMAIN,
        "creator_id": "40",
        "creator_domain": "users.example",
        "name": "Town hall",
        "description": "Monthly update",
        "scheduled_start_time": "2026-09-01T18:00:00+00:00",
        "scheduled_end_time": None,
        "privacy_level": 2,
        "status": 1,
        "entity_type": 2,
        "entity_id": None,
        "entity_domain": None,
        "entity_metadata": None,
        "recurrence_rule": None,
        "image": None,
        "created_at": "2026-08-28T10:00:00+00:00",
        "updated_at": "2026-08-28T10:00:00+00:00",
        "version": "version-1",
        "user_count": 3,
    }


def sound_payload() -> dict[str, object]:
    return {
        "id": "50",
        "origin_domain": DOMAIN,
        "guild_id": "10",
        "guild_domain": DOMAIN,
        "name": "Horn",
        "media_hash": "a" * 64,
        "content_type": "audio/ogg",
        "volume": 1.0,
        "emoji_id": None,
        "emoji_domain": None,
        "emoji_name": "📣",
        "available": True,
        "duration_ms": 900,
        "created_by_id": "40",
        "created_by_domain": "users.example",
        "version": "1",
    }


def automod_rule_payload() -> dict[str, object]:
    return {
        "id": "60",
        "origin_domain": DOMAIN,
        "guild_id": "10",
        "guild_domain": DOMAIN,
        "name": "No invites",
        "creator_id": "40",
        "creator_domain": "users.example",
        "event_type": "message_send",
        "trigger_type": "keyword",
        "trigger_metadata": {
            "keyword_filter": ["discord.gg/*"],
            "regex_patterns": [],
            "presets": [],
            "allow_list": [],
            "mention_total_limit": None,
            "mention_raid_protection_enabled": False,
        },
        "actions": [{"type": "block_message", "metadata": {"custom_message": "No ads"}}],
        "enabled": True,
        "exempt_roles": [f"70@{DOMAIN}"],
        "exempt_channels": [f"30@{DOMAIN}"],
        "version": 1,
        "created_at": "2026-08-28T10:00:00+00:00",
        "updated_at": "2026-08-28T10:00:00+00:00",
    }


def automod_execution_payload() -> dict[str, object]:
    return {
        "guild_id": "10",
        "guild_domain": DOMAIN,
        "channel_id": "30",
        "channel_domain": DOMAIN,
        "rule_id": "60",
        "rule_domain": DOMAIN,
        "rule_trigger_type": "keyword",
        "user_id": "40",
        "user_domain": "users.example",
        "action": {"type": "block_message", "metadata": {}},
        "outcome": "blocked",
        "content": "",
        "matched_keyword": "discord.gg/example",
        "matched_content": None,
        "alert_system_message_id": None,
        "alert_system_message_domain": None,
        "content_digest": "b" * 64,
    }


class ProjectionSession:
    async def get(self, model: object, key: object) -> object:
        assert model is Channel
        assert key == (30, DOMAIN)
        return SimpleNamespace(
            id=30,
            origin_domain=DOMAIN,
            guild_id=10,
            guild_domain=DOMAIN,
            type=2,
            unavailable=False,
        )


def test_new_gateway_projections_are_ordered_but_snapshot_neutral() -> None:
    expected = {
        "guild.scheduled_event.create",
        "guild.scheduled_event.update",
        "guild.scheduled_event.delete",
        "guild.scheduled_event.user.add",
        "guild.scheduled_event.user.remove",
        "guild.soundboard.sound.create",
        "guild.soundboard.sound.update",
        "guild.soundboard.sound.delete",
        "guild.soundboard.sounds.update",
        "guild.automod.rule.create",
        "guild.automod.rule.update",
        "guild.automod.rule.delete",
        "guild.automod.execution",
    }

    assert expected <= GUILD_MUTATION_EVENT_TYPES
    assert expected <= SNAPSHOT_NEUTRAL_GUILD_EVENTS


@pytest.mark.asyncio
async def test_new_gateway_projection_payloads_are_strictly_validated() -> None:
    target_guild = guild()
    session = ProjectionSession()

    scheduled = await _validated_scheduled_event(
        session,  # type: ignore[arg-type]
        target_guild,  # type: ignore[arg-type]
        scheduled_event_payload(),
    )
    sound = _validated_soundboard_sound(  # type: ignore[arg-type]
        target_guild,
        sound_payload(),
    )
    collection = _validated_soundboard_collection(  # type: ignore[arg-type]
        target_guild,
        {
            "guild_id": "10",
            "guild_domain": DOMAIN,
            "soundboard_sounds": [sound_payload()],
        },
    )
    rule = _validated_automod_rule(  # type: ignore[arg-type]
        target_guild,
        automod_rule_payload(),
    )
    execution = await _validated_automod_execution(
        session,  # type: ignore[arg-type]
        target_guild,  # type: ignore[arg-type]
        automod_execution_payload(),
    )

    assert scheduled["id"] == "20"
    assert sound["id"] == "50"
    assert len(collection["soundboard_sounds"]) == 1  # type: ignore[arg-type]
    assert rule["id"] == "60"
    assert execution["content_digest"] == "b" * 64


@pytest.mark.asyncio
async def test_scheduled_event_projection_validates_discord_recurrence_contract() -> None:
    target_guild = guild()
    session = ProjectionSession()
    scheduled = scheduled_event_payload()
    scheduled["recurrence_rule"] = {
        "start": scheduled["scheduled_start_time"],
        "end": None,
        "frequency": 2,
        "interval": 1,
        "by_weekday": [1],
        "by_n_weekday": None,
        "by_month": None,
        "by_month_day": None,
        "by_year_day": None,
        "count": None,
    }

    validated = await _validated_scheduled_event(
        session,  # type: ignore[arg-type]
        target_guild,  # type: ignore[arg-type]
        scheduled,
    )
    assert validated["recurrence_rule"] == {
        **scheduled["recurrence_rule"],
        "start": "2026-09-01T18:00:00Z",
    }
    recurrence = scheduled["recurrence_rule"]
    assert isinstance(recurrence, dict)

    wrong_frequency = {**scheduled}
    wrong_frequency["recurrence_rule"] = {
        **recurrence,
        "frequency": 1,
    }
    with pytest.raises(ValueError, match="recurrence is invalid"):
        await _validated_scheduled_event(
            session,  # type: ignore[arg-type]
            target_guild,  # type: ignore[arg-type]
            wrong_frequency,
        )

    wrong_start = {**scheduled}
    wrong_start["recurrence_rule"] = {
        **recurrence,
        "start": "2026-09-08T18:00:00+00:00",
    }
    with pytest.raises(ValueError, match="start does not match"):
        await _validated_scheduled_event(
            session,  # type: ignore[arg-type]
            target_guild,  # type: ignore[arg-type]
            wrong_start,
        )


@pytest.mark.asyncio
async def test_gateway_projection_validation_rejects_cross_guild_and_oversized_state() -> None:
    target_guild = guild()
    session = ProjectionSession()
    scheduled = scheduled_event_payload()
    scheduled["guild_id"] = "11"
    with pytest.raises(ValueError, match="wrong guild"):
        await _validated_scheduled_event(
            session,  # type: ignore[arg-type]
            target_guild,  # type: ignore[arg-type]
            scheduled,
        )

    with pytest.raises(ValueError, match="collection is invalid"):
        _validated_soundboard_collection(  # type: ignore[arg-type]
            target_guild,
            {
                "guild_id": "10",
                "guild_domain": DOMAIN,
                "soundboard_sounds": [sound_payload() for _ in range(49)],
            },
        )

    rule = automod_rule_payload()
    rule["unexpected"] = True
    with pytest.raises(ValueError, match="rule mutation is invalid"):
        _validated_automod_rule(target_guild, rule)  # type: ignore[arg-type]

    execution = automod_execution_payload()
    execution["actions"] = [execution.pop("action")]
    with pytest.raises(ValueError, match="execution mutation is invalid"):
        await _validated_automod_execution(
            session,  # type: ignore[arg-type]
            target_guild,  # type: ignore[arg-type]
            execution,
        )

    execution = automod_execution_payload()
    execution["action"] = {
        "type": "timeout",
        "metadata": {"duration_seconds": True},
    }
    with pytest.raises(ValueError, match="execution action is invalid"):
        await _validated_automod_execution(
            session,  # type: ignore[arg-type]
            target_guild,  # type: ignore[arg-type]
            execution,
        )
