from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.calls import require_call_policy
from app.api.channels import require_dm_send
from app.chat.channel_access import ChannelAccess
from app.chat.privacy import (
    can_direct_message,
    dm_privacy_lock_id,
    lock_dm_policy,
    relationship_pair_lock_id,
    share_guild,
)
from app.core.settings import Settings
from app.db.models import Relationship, User


def user(user_id: int, domain: str) -> User:
    return User(
        id=user_id,
        origin_domain=domain,
        is_local=domain == "alpha.localhost",
        username=f"user{user_id}",
        password_hash="hash" if domain == "alpha.localhost" else None,
        email=f"user{user_id}@example.com" if domain == "alpha.localhost" else None,
    )


def test_relationship_pair_lock_is_direction_independent() -> None:
    first = user(10, "alpha.localhost")
    second = user(20, "beta.localhost")
    assert relationship_pair_lock_id(first, second) == relationship_pair_lock_id(second, first)


@pytest.mark.asyncio
async def test_dm_policy_locks_pair_and_recipient_in_global_order() -> None:
    sender = user(10, "alpha.localhost")
    recipient = user(20, "alpha.localhost")
    execute = AsyncMock()
    session = cast(AsyncSession, SimpleNamespace(execute=execute))

    await lock_dm_policy(session, sender, recipient)

    lock_ids = [
        call.args[0].compile().params["pg_advisory_xact_lock_2"] for call in execute.await_args_list
    ]
    assert lock_ids == sorted(
        {
            relationship_pair_lock_id(sender, recipient),
            dm_privacy_lock_id(recipient),
        }
    )


@pytest.mark.asyncio
async def test_shared_guild_policy_share_locks_both_memberships() -> None:
    sender = user(10, "alpha.localhost")
    recipient = user(20, "alpha.localhost")
    scalar = AsyncMock(return_value=30)
    session = cast(AsyncSession, SimpleNamespace(scalar=scalar))

    assert await share_guild(session, sender, recipient)

    statement = scalar.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR SHARE OF guild_members_1, guild_members_2" in sql


@pytest.mark.asyncio
async def test_shared_guild_policy_always_allows_accepted_friends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = user(10, "beta.localhost")
    recipient = user(20, "alpha.localhost")
    session = cast(
        AsyncSession,
        SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(dm_privacy="shared_guild"))),
    )
    relation = Relationship(type="friend")
    shared_guild = AsyncMock(return_value=False)
    monkeypatch.setattr("app.chat.privacy.blocked_between", AsyncMock(return_value=False))
    monkeypatch.setattr("app.chat.privacy.relationship", AsyncMock(return_value=relation))
    monkeypatch.setattr("app.chat.privacy.share_guild", shared_guild)

    assert await can_direct_message(session, sender, recipient)
    shared_guild.assert_not_awaited()


@pytest.mark.asyncio
async def test_shared_guild_policy_rejects_unrelated_users_without_a_shared_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sender = user(10, "beta.localhost")
    recipient = user(20, "alpha.localhost")
    session = cast(
        AsyncSession,
        SimpleNamespace(scalar=AsyncMock(return_value=SimpleNamespace(dm_privacy="shared_guild"))),
    )
    monkeypatch.setattr("app.chat.privacy.blocked_between", AsyncMock(return_value=False))
    monkeypatch.setattr("app.chat.privacy.relationship", AsyncMock(return_value=None))
    monkeypatch.setattr("app.chat.privacy.share_guild", AsyncMock(return_value=False))

    assert not await can_direct_message(session, sender, recipient)


@pytest.mark.asyncio
async def test_call_policy_rechecks_blocks_before_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user(10, "alpha.localhost")
    peer = user(20, "beta.localhost")
    monkeypatch.setattr("app.api.calls.blocked_between", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as error:
        await require_call_policy(
            cast(AsyncSession, object()),
            cast(Settings, SimpleNamespace(domain="alpha.localhost")),
            {
                "channel_id": "30",
                "channel_domain": "alpha.localhost",
                "caller": "10@alpha.localhost",
                "participants": ["10@alpha.localhost", "20@beta.localhost"],
            },
            actor,
            [actor, peer],
        )

    assert error.value.status_code == 403
    assert error.value.detail == {"code": "DM_PRIVACY_REJECTED"}


@pytest.mark.asyncio
async def test_remote_dm_send_serializes_and_rechecks_local_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = user(10, "alpha.localhost")
    remote = user(20, "beta.localhost")
    lock = AsyncMock()
    monkeypatch.setattr("app.api.channels.lock_relationship_pair", lock)
    monkeypatch.setattr("app.api.channels.blocked_between", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as error:
        await require_dm_send(
            cast(AsyncSession, object()),
            cast(ChannelAccess, SimpleNamespace(guild=None, participants=[actor, remote])),
            actor,
        )

    lock.assert_awaited_once()
    assert error.value.status_code == 403
    assert error.value.detail == {"code": "DM_PRIVACY_REJECTED"}
