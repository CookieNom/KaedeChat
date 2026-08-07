from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import guild_lifecycle
from app.chat.schemas import GuildOwnershipTransfer
from app.core.types import EntityRef
from app.db.models import Guild, GuildMember, User


def make_guild() -> Guild:
    return Guild(
        id=10,
        origin_domain="chat.example",
        name="Test guild",
        owner_id=1,
        owner_domain="chat.example",
        permission_generation=1,
        history_policy_generation=1,
        unavailable=False,
        updated_at=datetime.now(UTC),
    )


def make_user(identifier: int, domain: str = "chat.example") -> User:
    return User(
        id=identifier,
        origin_domain=domain,
        username=f"user-{identifier}",
        is_local=domain == "chat.example",
    )


def auth(user: User) -> SimpleNamespace:
    return SimpleNamespace(user=user)


def test_owner_checks_use_the_full_federated_identity() -> None:
    guild = make_guild()

    assert guild_lifecycle._is_owner(guild, make_user(1))
    assert not guild_lifecycle._is_owner(guild, make_user(1, "remote.example"))


@pytest.mark.asyncio
async def test_owner_must_transfer_or_delete_instead_of_leaving(monkeypatch) -> None:
    guild = make_guild()
    owner = make_user(1)
    session = AsyncMock()
    session.get.return_value = GuildMember(
        guild_id=10,
        guild_domain="chat.example",
        user_id=1,
        user_domain="chat.example",
    )
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))

    with pytest.raises(HTTPException) as caught:
        await guild_lifecycle.leave_guild(
            EntityRef("10"),
            auth(owner),  # type: ignore[arg-type]
            session,
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 409
    assert caught.value.detail == {"code": "OWNER_MUST_TRANSFER_OR_DELETE_GUILD"}
    session.delete.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_ownership_transfer_rejects_a_remote_instance_target(monkeypatch) -> None:
    guild = make_guild()
    owner = make_user(1)
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))
    payload = GuildOwnershipTransfer(owner_id="2@remote.example")

    with pytest.raises(HTTPException) as caught:
        await guild_lifecycle.transfer_guild_ownership(
            EntityRef("10"),
            payload,
            auth(owner),  # type: ignore[arg-type]
            AsyncMock(),
            AsyncMock(),
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
            guild.updated_at.isoformat(),
        )

    assert caught.value.status_code == 400
    assert caught.value.detail == {"code": "OWNER_TRANSFER_REQUIRES_LOCAL_MEMBER"}


@pytest.mark.asyncio
async def test_non_owner_cannot_delete_a_guild(monkeypatch) -> None:
    guild = make_guild()
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))
    session = AsyncMock()

    with pytest.raises(HTTPException) as caught:
        await guild_lifecycle.delete_guild(
            EntityRef("10"),
            auth(make_user(2)),  # type: ignore[arg-type]
            session,
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
            guild.updated_at.isoformat(),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {"code": "GUILD_OWNER_REQUIRED"}
    session.delete.assert_not_awaited()
    session.commit.assert_not_awaited()
