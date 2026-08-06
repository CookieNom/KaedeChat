from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.chat.guild_revision import federation_channel_state
from app.chat.schemas import ChannelUpdate, GuildUpdate
from app.db.models import Channel, Guild
from app.federation.history import (
    HISTORY_EVENT_TYPES,
    _validate_history_message,
    cleanup_history_transfers,
    effective_history_policy,
    unresolved_history_username,
)


@pytest.mark.asyncio
async def test_history_cleanup_reports_expired_and_abandoned_rows_without_committing() -> None:
    session = AsyncMock()
    session.execute.side_effect = [
        SimpleNamespace(rowcount=3),
        SimpleNamespace(rowcount=2),
    ]

    assert await cleanup_history_transfers(
        session,
        now=datetime(2026, 8, 5, tzinfo=UTC),
    ) == {"history_exports": 3, "history_imports": 2}
    assert session.execute.await_count == 2
    session.commit.assert_not_awaited()


def guild(policy: str = "disabled") -> Guild:
    return Guild(
        id=10,
        origin_domain="home.example",
        name="History guild",
        owner_id=20,
        owner_domain="home.example",
        federated_history_policy=policy,
        history_policy_generation=1,
    )


def channel(policy: str = "inherit") -> Channel:
    return Channel(
        id=30,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=0,
        name="general",
        created_floor_id=30,
        federated_history_policy=policy,
    )


def test_history_policy_defaults_to_disabled_and_supports_channel_overrides() -> None:
    parent = guild()
    child = channel()
    assert effective_history_policy(parent, child) == "disabled"
    parent.federated_history_policy = "full_retained"
    assert effective_history_policy(parent, child) == "full_retained"
    child.federated_history_policy = "disabled"
    assert effective_history_policy(parent, child) == "disabled"


def test_new_channel_federation_state_materializes_the_pre_flush_policy_default() -> None:
    new_channel = Channel(
        id=31,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        type=0,
        name="new-channel",
        position=1,
        rate_limit_per_user=0,
        created_floor_id=31,
    )

    assert federation_channel_state(new_channel)["federated_history_policy"] == "inherit"


def test_history_policy_schemas_reject_unknown_and_explicit_null_values() -> None:
    assert GuildUpdate(federated_history_policy="full_retained").federated_history_policy == (
        "full_retained"
    )
    assert ChannelUpdate(federated_history_policy="inherit").federated_history_policy == "inherit"
    with pytest.raises(ValidationError):
        GuildUpdate(federated_history_policy=None)
    with pytest.raises(ValidationError):
        ChannelUpdate(federated_history_policy="public")  # type: ignore[arg-type]


def test_history_placeholder_handles_are_stable_and_do_not_expose_the_id() -> None:
    first = unresolved_history_username(1234, "remote.example")
    assert first == unresolved_history_username(1234, "remote.example")
    assert first != unresolved_history_username(1235, "remote.example")
    assert "1234" not in first
    assert first.startswith("history_")


def test_history_delta_registry_contains_only_mutations_needed_for_reconciliation() -> None:
    assert {
        "guild.message.update",
        "guild.message.delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.pin.add",
        "guild.pin.remove",
    } == HISTORY_EVENT_TYPES


def test_history_message_validation_binds_author_channel_and_range() -> None:
    now = datetime.now(UTC).isoformat()
    raw = {
        "id": "40",
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": "retained message",
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": now,
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }
    message_id, validated = _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=40,
    )
    assert message_id == 40
    assert validated["content"] == "retained message"

    with pytest.raises(ValueError, match="outside its granted range"):
        _validate_history_message(
            {**raw, "id": "41"},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=40,
        )
    with pytest.raises(ValueError, match="profile does not match"):
        _validate_history_message(
            {**raw, "author_id": "21"},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=40,
        )


def test_recent_first_history_validation_requires_strict_descending_pages() -> None:
    now = datetime.now(UTC).isoformat()
    raw = {
        "id": "40",
        "origin_domain": "home.example",
        "channel_id": "30",
        "channel_domain": "home.example",
        "author_id": "20",
        "author_domain": "home.example",
        "content": "newest retained message",
        "e2ee": None,
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": now,
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "20",
            "origin_domain": "home.example",
            "username": "member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }
    message_id, _validated = _validate_history_message(
        raw,
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=50,
        before=50,
    )
    assert message_id == 40
    next_id, _validated = _validate_history_message(
        {**raw, "id": "35"},
        guild_origin="home.example",
        channel_id=30,
        after=0,
        upper_bound=50,
        before=50,
        previous_id=40,
    )
    assert next_id == 35
    with pytest.raises(ValueError, match="outside its granted range"):
        _validate_history_message(
            {**raw, "id": "41"},
            guild_origin="home.example",
            channel_id=30,
            after=0,
            upper_bound=50,
            before=50,
            previous_id=40,
        )
