from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import federation, guild_lifecycle
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


@pytest.mark.asyncio
async def test_remote_leave_revokes_locally_and_queues_without_peer_network(
    monkeypatch,
) -> None:
    guild = make_guild()
    guild.origin_domain = "remote.example"
    actor = make_user(2)
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=actor.id,
        user_domain=actor.origin_domain,
    )
    session = AsyncMock()
    session.get.return_value = member
    queued = AsyncMock()
    revoked = AsyncMock(return_value=True)
    marked_departed = AsyncMock()
    delivered = AsyncMock()
    published = AsyncMock()
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        guild_lifecycle,
        "build_envelope",
        AsyncMock(return_value={"event_id": "kcfe_leave", "type": "guild.leave.request"}),
    )
    monkeypatch.setattr(guild_lifecycle, "queue_event", queued)
    monkeypatch.setattr(guild_lifecycle, "mark_remote_guild_departed", marked_departed)
    monkeypatch.setattr(guild_lifecycle, "apply_guild_access_revocation", revoked)
    monkeypatch.setattr(guild_lifecycle, "enqueue_best_effort", delivered)
    monkeypatch.setattr(guild_lifecycle, "_publish_guild_removed", published)

    response = await guild_lifecycle.leave_guild(
        EntityRef("10@remote.example"),
        auth(actor),  # type: ignore[arg-type]
        session,
        AsyncMock(),
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    queued.assert_awaited_once()
    assert queued.await_args.args[2] == "remote.example"
    marked_departed.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild_id=10,
        guild_domain="remote.example",
        user_id=2,
        user_domain="chat.example",
    )
    revoked.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=2,
        user_domain="chat.example",
    )
    session.commit.assert_awaited_once()
    delivered.assert_awaited_once()
    published.assert_awaited_once()


@pytest.mark.asyncio
async def test_authority_applies_durable_leave_request_idempotently(monkeypatch) -> None:
    guild = make_guild()
    remote = make_user(2, "remote.example")
    owner = make_user(1)
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=remote.id,
        user_domain=remote.origin_domain,
    )
    session = AsyncMock()

    async def get_model(_model: object, key: object) -> object | None:
        if key == (guild.id, guild.origin_domain, remote.id, remote.origin_domain):
            return member
        if key == (guild.owner_id, guild.owner_domain):
            return owner
        return None

    session.get.side_effect = get_model
    queue_revocation = AsyncMock()
    queue_mutation = AsyncMock()
    monkeypatch.setattr(federation, "queue_guild_access_revocation", queue_revocation)
    monkeypatch.setattr(federation, "queue_guild_mutation", queue_mutation)

    assert await federation._apply_authoritative_guild_leave(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=remote.id,
        user_domain=remote.origin_domain,
        missing_ok=False,
    )
    session.delete.assert_awaited_once_with(member)
    queue_revocation.assert_awaited_once()
    queue_mutation.assert_awaited_once()

    session.reset_mock()
    session.get.side_effect = lambda _model, _key: None
    assert not await federation._apply_authoritative_guild_leave(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=remote.id,
        user_domain=remote.origin_domain,
        missing_ok=True,
    )
    session.delete.assert_not_awaited()
