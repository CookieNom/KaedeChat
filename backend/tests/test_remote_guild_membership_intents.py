from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException, Response
from sqlalchemy.dialects import postgresql

from app.api import invites
from app.db.models import Guild, RemoteGuildMembershipIntent, User
from app.federation.guilds import (
    REMOTE_GUILD_DEPARTED,
    REMOTE_GUILD_JOIN_INTENT_LIMIT_PER_USER,
    REMOTE_GUILD_JOINING,
    apply_guild_member_event,
    begin_remote_guild_join,
    complete_remote_guild_join,
    filter_remote_snapshot_memberships,
    purge_stale_remote_guild_membership_intents,
    stale_remote_guild_membership_intent_candidates,
)


def intent(*, guild_domain: str = "remote.example", state: str) -> RemoteGuildMembershipIntent:
    return RemoteGuildMembershipIntent(
        guild_id=10,
        guild_domain=guild_domain,
        user_id=42,
        user_domain="local.example",
        user_is_local=True,
        state=state,
    )


def membership_snapshot() -> dict[str, Any]:
    return {
        "members": [
            {
                "user": {
                    "id": "42",
                    "origin_domain": "local.example",
                    "username": "local_user",
                }
            },
            {
                "user": {
                    "id": "42",
                    "origin_domain": "other.example",
                    "username": "same-id-elsewhere",
                }
            },
        ],
        "member_roles": [
            {
                "user_id": "42",
                "user_domain": "local.example",
                "role_id": "1",
                "role_domain": "remote.example",
            },
            {
                "user_id": "42",
                "user_domain": "other.example",
                "role_id": "1",
                "role_domain": "remote.example",
            },
        ],
        "overwrites": [
            {
                "target_type": "member",
                "target_id": "42",
                "target_domain": "local.example",
            },
            {
                "target_type": "member",
                "target_id": "42",
                "target_domain": "other.example",
            },
        ],
    }


def test_departure_filters_only_the_matching_composite_member_reference() -> None:
    departed = intent(state=REMOTE_GUILD_DEPARTED)

    members, member_roles, overwrites, required_intent = filter_remote_snapshot_memberships(
        membership_snapshot(),
        {(42, "local.example"): departed},
        local_domain="local.example",
        required_member=None,
        existing_required_member=False,
    )

    assert [raw["user"]["origin_domain"] for raw in members] == ["other.example"]
    assert [raw["user_domain"] for raw in member_roles] == ["other.example"]
    assert [raw["target_domain"] for raw in overwrites] == ["other.example"]
    assert required_intent is None
    assert intent(guild_domain="elsewhere.example", state=REMOTE_GUILD_DEPARTED).guild_domain != (
        departed.guild_domain
    )


def test_snapshot_cannot_enroll_a_local_user_without_local_consent() -> None:
    members, member_roles, overwrites, _required_intent = filter_remote_snapshot_memberships(
        membership_snapshot(),
        {},
        local_domain="local.example",
        required_member=None,
        existing_required_member=False,
    )

    assert [raw["user"]["origin_domain"] for raw in members] == ["other.example"]
    assert [raw["user_domain"] for raw in member_roles] == ["other.example"]
    assert [raw["target_domain"] for raw in overwrites] == ["other.example"]


def test_snapshot_preserves_an_existing_consented_local_membership() -> None:
    members, _member_roles, _overwrites, _required_intent = filter_remote_snapshot_memberships(
        membership_snapshot(),
        {},
        local_domain="local.example",
        required_member=None,
        existing_required_member=False,
        existing_local_members={(42, "local.example")},
    )

    assert {raw["user"]["origin_domain"] for raw in members} == {
        "local.example",
        "other.example",
    }


@pytest.mark.asyncio
async def test_only_explicit_joining_intent_allows_and_clears_departure() -> None:
    pending = intent(state=REMOTE_GUILD_DEPARTED)
    intents = {(42, "local.example"): pending}

    with pytest.raises(ValueError, match="explicit rejoin"):
        filter_remote_snapshot_memberships(
            membership_snapshot(),
            intents,
            local_domain="local.example",
            required_member=(42, "local.example"),
            existing_required_member=False,
        )

    pending.state = REMOTE_GUILD_JOINING
    members, _roles, _overwrites, required_intent = filter_remote_snapshot_memberships(
        membership_snapshot(),
        intents,
        local_domain="local.example",
        required_member=(42, "local.example"),
        existing_required_member=False,
    )
    assert required_intent is pending
    assert any(raw["user"]["origin_domain"] == "local.example" for raw in members)

    session = AsyncMock()
    await complete_remote_guild_join(session, pending)
    session.delete.assert_awaited_once_with(pending)


@pytest.mark.asyncio
async def test_explicit_first_join_records_a_pending_composite_intent() -> None:
    session = AsyncMock()
    session.get.side_effect = [None, None]
    session.scalar.side_effect = [None, 0]

    started = await begin_remote_guild_join(
        session,
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
        guild_id=10,
        guild_domain="remote.example",
        user_id=42,
        user_domain="local.example",
    )

    assert started
    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    params = statement.compile(
        dialect=postgresql.dialect()  # type: ignore[no-untyped-call]
    ).params
    assert params["guild_id"] == 10
    assert params["guild_domain"] == "remote.example"
    assert params["user_id"] == 42
    assert params["user_domain"] == "local.example"
    assert params["state"] == REMOTE_GUILD_JOINING


@pytest.mark.asyncio
async def test_explicit_rejoin_transitions_the_persisted_departure() -> None:
    pending = intent(state=REMOTE_GUILD_DEPARTED)
    session = AsyncMock()
    session.get.side_effect = [None, pending]
    session.scalar.side_effect = [None, 0]

    started = await begin_remote_guild_join(
        session,
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
        guild_id=10,
        guild_domain="remote.example",
        user_id=42,
        user_domain="local.example",
    )

    assert started
    assert pending.state == REMOTE_GUILD_JOINING
    session.flush.assert_awaited_once()
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_invite_commits_join_intent_before_authority_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=42,
        origin_domain="local.example",
        username="local_user",
        is_local=True,
    )
    guild = Guild(
        id=10,
        origin_domain="remote.example",
        name="Remote guild",
        owner_id=9,
        owner_domain="remote.example",
        last_event_seq=1,
        next_event_seq=2,
        snapshot_generation=1,
        sync_status="ready",
    )
    session = AsyncMock()
    begin_join = AsyncMock(return_value=True)
    apply_snapshot = AsyncMock(return_value=guild)

    async def signed_request(
        _session: object,
        _settings: object,
        _method: str,
        _domain: str,
        path: str,
        **_kwargs: object,
    ) -> httpx.Response:
        if path == "/_kaede/v1/invites/resolve":
            return httpx.Response(200, json={"guild": {"id": "10"}})
        assert path == "/_kaede/v1/guilds/10/join"
        assert session.commit.await_count == 1
        return httpx.Response(200, json={"guild": {"id": "10"}})

    monkeypatch.setattr(invites, "enforce_client_rate_limit", AsyncMock())
    monkeypatch.setattr(invites, "begin_remote_guild_join", begin_join)
    monkeypatch.setattr(invites, "signed_request", signed_request)
    monkeypatch.setattr(invites, "fetch_guild_snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(invites, "apply_guild_snapshot", apply_snapshot)
    monkeypatch.setattr(invites, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(invites, "enqueue_best_effort", AsyncMock())
    monkeypatch.setattr(invites, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(invites, "guild_payload", lambda _guild: {"id": "10"})

    result = await invites.accept_invite(
        "invite@remote.example",
        Response(),
        SimpleNamespace(user=user),  # type: ignore[arg-type]
        session,
        AsyncMock(),
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
    )

    assert result == {"id": "10"}
    begin_join.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="local.example"),
        guild_id=10,
        guild_domain="remote.example",
        user_id=42,
        user_domain="local.example",
    )
    assert session.commit.await_count == 2
    apply_snapshot.assert_awaited_once()
    assert apply_snapshot.await_args is not None
    assert apply_snapshot.await_args.kwargs["required_member"] == (42, "local.example")


@pytest.mark.asyncio
async def test_stale_member_add_is_consumed_without_regranting_departed_local_user() -> None:
    guild = Guild(
        id=10,
        origin_domain="remote.example",
        name="Remote guild",
        owner_id=9,
        owner_domain="remote.example",
        last_event_seq=4,
        next_event_seq=5,
        snapshot_generation=1,
        sync_status="ready",
    )
    departed = intent(state=REMOTE_GUILD_DEPARTED)
    session = AsyncMock()
    session.scalar.side_effect = [guild, departed]
    session.get.return_value = None
    event = {
        "seq": "5",
        "type": "guild.member.add",
        "actor": {"id": "9", "domain": "remote.example"},
        "content": {
            "user": {
                "id": "42",
                "origin_domain": "local.example",
                "username": "local_user",
            },
            "joined_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
        },
        "context": {"guild_id": "10", "guild_domain": "remote.example"},
    }

    applied = await apply_guild_member_event(
        session,
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
        guild,
        event,
    )

    assert applied is None
    assert guild.last_event_seq == 5
    assert guild.next_event_seq == 6
    assert guild.snapshot_generation == 2
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_unsolicited_first_member_add_is_consumed_without_durable_state() -> None:
    guild = Guild(
        id=10,
        origin_domain="remote.example",
        name="Remote guild",
        owner_id=9,
        owner_domain="remote.example",
        last_event_seq=4,
        next_event_seq=5,
        snapshot_generation=1,
        sync_status="ready",
    )
    session = AsyncMock()
    session.scalar.side_effect = [guild, None]
    session.get.return_value = None
    event = {
        "seq": "5",
        "type": "guild.member.add",
        "actor": {"id": "9", "domain": "remote.example"},
        "content": {
            "user": {
                "id": "42",
                "origin_domain": "local.example",
                "username": "local_user",
            },
            "joined_at": datetime(2026, 8, 12, tzinfo=UTC).isoformat(),
        },
        "context": {"guild_id": "10", "guild_domain": "remote.example"},
    }

    applied = await apply_guild_member_event(
        session,
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
        guild,
        event,
    )

    assert applied is None
    assert guild.last_event_seq == 5
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_remote_join_limit_is_serialized_and_actionable() -> None:
    session = AsyncMock()
    session.get.side_effect = [None, None]
    session.scalar.side_effect = [None, REMOTE_GUILD_JOIN_INTENT_LIMIT_PER_USER]

    with pytest.raises(HTTPException) as rejected:
        await begin_remote_guild_join(
            session,
            SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
            guild_id=10,
            guild_domain="remote.example",
            user_id=42,
            user_domain="local.example",
        )

    assert rejected.value.status_code == 429
    assert rejected.value.detail["code"] == "KAED_FED_REMOTE_GUILD_JOIN_LIMIT"
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_membership_intents_are_cleaned_in_a_bounded_batch() -> None:
    old_join = intent(state=REMOTE_GUILD_JOINING)
    old_departure = intent(guild_domain="another.example", state=REMOTE_GUILD_DEPARTED)
    session = AsyncMock()
    session.scalars.return_value = [old_join, old_departure]
    now = datetime(2026, 8, 12, tzinfo=UTC)

    assert (
        await purge_stale_remote_guild_membership_intents(
            session,
            now=now,
            limit=20,
        )
        == 2
    )
    statement = session.scalars.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "updated_at <" in compiled
    assert statement._limit_clause.value == 20  # type: ignore[attr-defined]
    assert old_join in [call.args[0] for call in session.delete.await_args_list]
    assert old_departure in [call.args[0] for call in session.delete.await_args_list]

    query = stale_remote_guild_membership_intent_candidates(
        now=now - timedelta(hours=1),
        limit=1,
    )
    assert query._limit_clause.value == 1  # type: ignore[attr-defined]
