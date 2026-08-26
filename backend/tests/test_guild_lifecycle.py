from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import federation, guild_lifecycle
from app.chat.schemas import GuildOwnershipTransfer
from app.core.types import EntityRef
from app.db.bot_models import BotInstallation
from app.db.models import Guild, GuildMember, User
from app.federation.schemas import GuildLeaveRequest


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
        account_type="human",
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
async def test_local_bot_leave_cleans_installation_role_and_publishes_after_commit(
    monkeypatch,
) -> None:
    guild = make_guild()
    bot = make_user(2)
    bot.account_type = "bot"
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=bot.id,
        user_domain=bot.origin_domain,
        joined_at=datetime.now(UTC),
        member_version=3,
    )
    installation = BotInstallation(
        id=50,
        application_id=20,
        application_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        installer_id=guild.owner_id,
        installer_domain=guild.owner_domain,
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="suspended",
        role_id=90,
        role_domain=guild.origin_domain,
    )
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def publish_roles(*_args) -> None:
        order.append("roles")

    async def wake_tracker(_guild: Guild) -> None:
        order.append("tracker-wake")

    async def publish_member(*_args, **_kwargs) -> None:
        order.append("member")

    session = AsyncMock()
    session.get.return_value = member
    session.commit.side_effect = commit
    revoke = AsyncMock(return_value=[installation])
    cleanup = AsyncMock(return_value=[(90, guild.origin_domain)])
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(guild_lifecycle, "revoke_installations_for_guild_member", revoke)
    monkeypatch.setattr(guild_lifecycle, "cleanup_installation_roles", cleanup)
    monkeypatch.setattr(
        guild_lifecycle,
        "cleanup_guild_member_threads",
        AsyncMock(return_value=[]),
    )
    clear_assignees = AsyncMock(return_value=[])
    monkeypatch.setattr(guild_lifecycle, "clear_tracker_assignees", clear_assignees)
    monkeypatch.setattr(guild_lifecycle, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(guild_lifecycle, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(
        guild_lifecycle,
        "wake_tracker_membership_cleanup",
        AsyncMock(side_effect=wake_tracker),
    )
    monkeypatch.setattr(guild_lifecycle, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(guild_lifecycle, "publish_guild_thread_member_cleanup", AsyncMock())
    monkeypatch.setattr(
        guild_lifecycle,
        "publish_deleted_installation_roles",
        AsyncMock(side_effect=publish_roles),
    )
    monkeypatch.setattr(
        guild_lifecycle,
        "_publish_guild_removed",
        AsyncMock(side_effect=publish_member),
    )

    response = await guild_lifecycle.leave_guild(
        EntityRef("10"),
        auth(bot),  # type: ignore[arg-type]
        session,
        AsyncMock(),
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    revoke.assert_awaited_once_with(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=bot.id,
        user_domain=bot.origin_domain,
    )
    cleanup.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        bot,
        [installation],
    )
    clear_assignees.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        bot,
        [(bot.id, bot.origin_domain)],
    )
    session.delete.assert_awaited_once_with(member)
    assert order == ["commit", "tracker-wake", "roles", "member"]


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
    installation = BotInstallation(
        id=50,
        application_id=20,
        application_domain="remote.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        bot_user_id=remote.id,
        bot_user_domain=remote.origin_domain,
        installer_id=owner.id,
        installer_domain=owner.origin_domain,
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="suspended",
        role_id=90,
        role_domain=guild.origin_domain,
    )
    revoke = AsyncMock(return_value=[installation])
    cleanup = AsyncMock(return_value=[(90, guild.origin_domain)])
    monkeypatch.setattr(federation, "queue_guild_access_revocation", queue_revocation)
    monkeypatch.setattr(federation, "queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(federation, "revoke_installations_for_guild_member", revoke)
    monkeypatch.setattr(federation, "cleanup_installation_roles", cleanup)
    monkeypatch.setattr(
        federation,
        "cleanup_guild_member_threads",
        AsyncMock(return_value=[]),
    )
    clear_assignees = AsyncMock(return_value=[])
    monkeypatch.setattr(federation, "clear_tracker_assignees", clear_assignees)

    assert await federation._apply_authoritative_guild_leave(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=remote.id,
        user_domain=remote.origin_domain,
        missing_ok=False,
    ) == (True, [(90, guild.origin_domain)], [])
    session.delete.assert_awaited_once_with(member)
    revoke.assert_awaited_once()
    cleanup.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        owner,
        [installation],
    )
    queue_revocation.assert_awaited_once()
    queue_mutation.assert_awaited_once()
    clear_assignees.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        owner,
        [(remote.id, remote.origin_domain)],
    )

    session.reset_mock()
    session.get.side_effect = lambda _model, _key: None
    assert await federation._apply_authoritative_guild_leave(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=remote.id,
        user_domain=remote.origin_domain,
        missing_ok=True,
    ) == (False, [], [])
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_authoritative_leave_publishes_role_cleanup_only_after_commit(
    monkeypatch,
) -> None:
    guild = make_guild()
    order: list[str] = []

    async def commit() -> None:
        order.append("commit")

    async def wake(_guild: Guild) -> None:
        order.append("wake")

    async def publish_roles(*_args) -> None:
        order.append("roles")

    async def publish_member(*_args) -> None:
        order.append("member")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=guild),
        commit=AsyncMock(side_effect=commit),
    )
    applied = AsyncMock(return_value=(True, [(90, guild.origin_domain)], []))
    monkeypatch.setattr(federation, "require_guild_federation_access", Mock())
    monkeypatch.setattr(federation, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation, "_apply_authoritative_guild_leave", applied)
    monkeypatch.setattr(
        federation,
        "wake_tracker_membership_cleanup",
        AsyncMock(side_effect=wake),
    )
    monkeypatch.setattr(
        federation,
        "publish_deleted_installation_roles",
        AsyncMock(side_effect=publish_roles),
    )
    monkeypatch.setattr(
        federation,
        "publish_dispatch",
        AsyncMock(side_effect=publish_member),
    )
    monkeypatch.setattr(federation, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(federation, "publish_guild_thread_member_cleanup", AsyncMock())

    response = await federation.federation_guild_leave(
        guild.id,
        GuildLeaveRequest(user={"id": "2", "domain": "remote.example"}),
        SimpleNamespace(origin="remote.example", silenced=False),  # type: ignore[arg-type]
        session,  # type: ignore[arg-type]
        AsyncMock(),
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    assert order == ["commit", "wake", "roles", "member"]
    applied.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
        guild,
        user_id=2,
        user_domain="remote.example",
        missing_ok=False,
        e2ee_policy_channels=[],
    )


@pytest.mark.parametrize(
    ("account_type", "disabled_at"),
    [("bot", None), ("human", datetime.now(UTC))],
)
@pytest.mark.asyncio
async def test_ownership_transfer_rejects_bot_or_disabled_target(
    monkeypatch,
    account_type: str,
    disabled_at: datetime | None,
) -> None:
    guild = make_guild()
    owner = make_user(1)
    target = make_user(2)
    target.account_type = account_type
    target.disabled_at = disabled_at
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=target.id,
        user_domain=target.origin_domain,
        joined_at=datetime.now(UTC),
    )
    session = AsyncMock()
    session.get.side_effect = [target, member]
    monkeypatch.setattr(guild_lifecycle, "_locked_guild", AsyncMock(return_value=guild))

    with pytest.raises(HTTPException) as caught:
        await guild_lifecycle.transfer_guild_ownership(
            EntityRef("10"),
            GuildOwnershipTransfer(owner_id="2"),
            auth(owner),  # type: ignore[arg-type]
            session,
            AsyncMock(),
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),  # type: ignore[arg-type]
            guild.updated_at.isoformat(),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "GUILD_MEMBER_NOT_FOUND"}
    session.commit.assert_not_awaited()
