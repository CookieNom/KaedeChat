from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.api.federation as federation_api
import app.federation.guilds as federation_guilds
from app.core.settings import Settings
from app.db.models import Channel, Guild, GuildMember, Message, User
from app.federation.guilds import apply_guild_message_event


@pytest.mark.asyncio
async def test_known_pending_join_makes_pre_snapshot_events_retryable() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=42))

    pending = await federation_api.remote_guild_snapshot_is_pending(
        cast(Any, session),
        cast(Settings, SimpleNamespace(domain="gamma.localhost")),
        100,
        "alpha.localhost",
    )

    assert pending


@pytest.mark.asyncio
@pytest.mark.parametrize(("inserted", "returns_message"), [(300, True), (None, False)])
async def test_retried_join_window_message_backfills_behind_snapshot_cursor(
    monkeypatch: pytest.MonkeyPatch,
    inserted: int | None,
    returns_message: bool,
) -> None:
    guild = Guild(
        id=100,
        origin_domain="alpha.localhost",
        name="Guild",
        owner_id=1,
        owner_domain="alpha.localhost",
        last_event_seq=10,
        next_event_seq=11,
    )
    channel = Channel(
        id=200,
        origin_domain="alpha.localhost",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=200,
        unavailable=False,
    )
    author = User(
        id=2,
        origin_domain="beta.localhost",
        is_local=False,
        username="author",
    )
    created_at = datetime.now(UTC)
    message = Message(
        id=300,
        origin_domain="alpha.localhost",
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=author.id,
        author_domain=author.origin_domain,
        content="arrived during join",
        e2ee=None,
        message_type=0,
        flags=0,
        client_nonce="join-window",
        referenced_message_id=None,
        referenced_message_domain=None,
        mention_user_refs=[],
        webhook_name=None,
        webhook_avatar_hash=None,
        created_at=created_at,
    )

    async def get_model(model: object, _key: object) -> object | None:
        if model is Channel:
            return channel
        if model is GuildMember:
            return None
        if model is Message:
            return message
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[guild, inserted]),
        get=get_model,
        add=MagicMock(),
    )
    reconcile_control = AsyncMock()
    monkeypatch.setattr(
        federation_guilds,
        "resolve_delegated_profile",
        AsyncMock(return_value=author),
    )
    monkeypatch.setattr(
        federation_guilds,
        "validate_snowflake_timestamp",
        MagicMock(),
    )
    monkeypatch.setattr(
        federation_guilds,
        "replicate_message_attachments",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        federation_guilds,
        "advance_channel_cursor",
        AsyncMock(),
    )
    monkeypatch.setattr(
        federation_guilds,
        "apply_e2ee_control_metadata",
        reconcile_control,
    )
    event = {
        "type": "guild.message.committed",
        "ts": int(created_at.timestamp() * 1000),
        "actor": {"id": "1", "domain": "alpha.localhost"},
        "context": {
            "guild_id": "100",
            "guild_domain": "alpha.localhost",
            "seq": "6",
        },
        "content": {
            "author": {
                "id": "2",
                "origin_domain": "beta.localhost",
                "username": "author",
            },
            "message": {
                "id": "300",
                "origin_domain": "alpha.localhost",
                "channel_id": "200",
                "channel_domain": "alpha.localhost",
                "author_id": "2",
                "author_domain": "beta.localhost",
                "content": "arrived during join",
                "e2ee": None,
                "message_type": 0,
                "flags": 0,
                "client_nonce": "join-window",
                "mention_user_refs": [],
                "attachments": [],
                "referenced_message_id": None,
                "referenced_message_domain": None,
                "edited_at": None,
                "deleted_at": None,
                "created_at": created_at.isoformat(),
            },
        },
    }

    applied = await apply_guild_message_event(
        cast(Any, session),
        cast(Settings, SimpleNamespace(domain="gamma.localhost")),
        guild,
        event,
    )

    assert (applied is message) is returns_message
    reconcile_control.assert_awaited_once_with(
        session,
        message,
        None,
        expected_authority="alpha.localhost",
    )
    assert guild.last_event_seq == 10
    assert guild.next_event_seq == 11
