from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from app.api import bulk_moderation
from app.api.bulk_moderation import (
    BulkBanRequest,
    PruneRequest,
    _perform_bulk_ban,
    _perform_prune,
    _prune_candidates,
)
from app.core.types import EntityRef
from app.db.models import Guild, Role


class Savepoint:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (PruneRequest, {"days": True}),
        (BulkBanRequest, {"user_ids": ["20"], "delete_message_seconds": False}),
    ],
)
def test_bulk_moderation_rejects_booleans_for_integer_windows(
    model: type[PruneRequest] | type[BulkBanRequest],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="must be an integer"):
        model.model_validate(payload)


@pytest.mark.asyncio
async def test_federation_aliases_cannot_duplicate_prune_roles() -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )
    session = SimpleNamespace(execute=AsyncMock())

    with pytest.raises(HTTPException) as raised:
        await _prune_candidates(
            session,  # type: ignore[arg-type]
            SimpleNamespace(domain="home.example"),  # type: ignore[arg-type]
            guild,
            days=7,
            include_roles=[EntityRef("20"), EntityRef("20@home.example")],
            actor_ref=(1, "home.example"),
        )
    assert raised.value.detail["code"] == "PRUNE_ROLE_DUPLICATE"
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_federation_aliases_cannot_run_the_same_bulk_ban_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = AsyncMock()
    monkeypatch.setattr(bulk_moderation, "stage_ban_member", stage)
    monkeypatch_session = SimpleNamespace(rollback=AsyncMock())
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )

    with pytest.raises(HTTPException) as raised:
        await _perform_bulk_ban(
            monkeypatch_session,  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(),  # type: ignore[arg-type]
            SimpleNamespace(domain="home.example"),  # type: ignore[arg-type]
            guild,
            SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="home.example")),  # type: ignore[arg-type]
            BulkBanRequest(user_ids=[EntityRef("20"), EntityRef("20@home.example")]),
            reason=None,
        )
    assert raised.value.detail["code"] == "BULK_BAN_USER_DUPLICATE"
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_remote_bulk_moderation_qualifies_refs_before_proxying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock(
        side_effect=[
            (True, {"pruned": 0, "days": 7}),
            (True, {"pruned": 0, "days": 7}),
            (
                True,
                {
                    "banned_users": ["30@caller.example"],
                    "failed_users": [],
                    "failed_user_details": [],
                },
            ),
        ]
    )
    monkeypatch.setattr(bulk_moderation, "proxy_human_guild_feature", proxy)
    settings = SimpleNamespace(domain="caller.example")
    auth = SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="caller.example"))
    dependencies = (
        EntityRef("10@home.example"),
        auth,
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
    )

    await bulk_moderation.estimate_prune(
        *dependencies,
        7,
        [EntityRef("20"), EntityRef("21@roles.example")],
    )
    await bulk_moderation.prune_members(
        dependencies[0],
        PruneRequest(include_roles=[EntityRef("20")]),
        *dependencies[1:4],
        SimpleNamespace(),
        settings,
    )
    await bulk_moderation.bulk_ban_members(
        dependencies[0],
        BulkBanRequest(user_ids=[EntityRef("30"), EntityRef("31@people.example")]),
        *dependencies[1:4],
        SimpleNamespace(),
        settings,
    )

    assert proxy.await_args_list[0].args[5]["include_roles"] == [
        "20@caller.example",
        "21@roles.example",
    ]
    assert proxy.await_args_list[1].args[5]["data"]["include_roles"] == ["20@caller.example"]
    assert proxy.await_args_list[2].args[5]["data"]["user_ids"] == [
        "30@caller.example",
        "31@people.example",
    ]


@pytest.mark.asyncio
async def test_remote_bulk_ban_rejects_resolved_alias_duplicates_before_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = AsyncMock()
    monkeypatch.setattr(bulk_moderation, "proxy_human_guild_feature", proxy)

    with pytest.raises(HTTPException) as duplicate:
        await bulk_moderation.bulk_ban_members(
            EntityRef("10@home.example"),
            BulkBanRequest(user_ids=[EntityRef("30"), EntityRef("30@caller.example")]),
            SimpleNamespace(user=SimpleNamespace(id=1, origin_domain="caller.example")),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="caller.example"),
        )

    assert duplicate.value.detail["code"] == "BULK_BAN_USER_DUPLICATE"
    proxy.assert_not_awaited()


@pytest.mark.asyncio
async def test_included_prune_roles_are_an_allow_list_for_the_complete_role_set(
    monkeypatch: pytest.MonkeyPatch,
    postgres_schema,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import GuildMember, MemberRole, User

    for table in ("users", "guild_members", "roles", "member_roles"):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} (LIKE public.{table} INCLUDING ALL)")
        )
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )
    moderator = Role(
        id=11,
        origin_domain="home.example",
        guild_id=10,
        guild_domain="home.example",
        name="Moderator",
        position=5,
    )
    monkeypatch.setattr(bulk_moderation, "highest_role", AsyncMock(return_value=moderator))
    async with AsyncSession(bind=postgres_schema) as session:
        for identifier, position in [(10, 0), (20, 1), (21, 2), (22, 6)]:
            session.add(
                Role(
                    id=identifier,
                    origin_domain="home.example",
                    guild_id=10,
                    guild_domain="home.example",
                    name=f"role-{identifier}",
                    position=position,
                )
            )
        for identifier in range(1, 10):
            session.add(
                User(
                    id=identifier,
                    origin_domain="home.example",
                    is_local=False,
                    federation_introduced_by_domain="home.example",
                    username=f"user_{identifier}",
                )
            )
            session.add(
                GuildMember(
                    guild_id=10,
                    guild_domain="home.example",
                    user_id=identifier,
                    user_domain="home.example",
                    joined_at=datetime.now(UTC) - timedelta(days=30),
                )
            )
        # 3 roleless, 4 allowed, 5 mixed, 6 forbidden, 7 higher allowed, 8 default+allowed.
        for user_id, role_ids in [(4, [20]), (5, [20, 21]), (6, [21]), (7, [22]), (8, [10, 20])]:
            for role_id in role_ids:
                session.add(
                    MemberRole(
                        guild_id=10,
                        guild_domain="home.example",
                        user_id=user_id,
                        user_domain="home.example",
                        role_id=role_id,
                        role_domain="home.example",
                    )
                )
        await session.flush()
        recent = await session.get(GuildMember, (10, "home.example", 9, "home.example"))
        recent.last_guild_activity_at = datetime.now(UTC)
        await session.flush()
        result = await _prune_candidates(
            session,
            SimpleNamespace(domain="home.example"),
            guild,
            days=7,
            include_roles=[EntityRef("20@home.example"), EntityRef("22@home.example")],
            actor_ref=(2, "home.example"),
        )
        assert result == [(3, "home.example"), (4, "home.example"), (8, "home.example")]


@pytest.mark.asyncio
async def test_default_role_actor_has_no_prune_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )
    session = SimpleNamespace(execute=AsyncMock())
    monkeypatch.setattr(
        bulk_moderation,
        "highest_role",
        AsyncMock(
            return_value=Role(
                id=10,
                origin_domain="home.example",
                guild_id=10,
                guild_domain="home.example",
                name="@everyone",
                position=0,
            )
        ),
    )

    candidates = await _prune_candidates(
        session,
        SimpleNamespace(domain="home.example"),
        guild,
        days=7,
        include_roles=[],
        actor_ref=(2, "home.example"),
    )

    assert candidates == []
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_prune_records_one_summary_audit_entry_not_manual_kicks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )
    actor = SimpleNamespace(id=1, origin_domain="home.example")
    session = SimpleNamespace(
        begin_nested=Mock(side_effect=Savepoint),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    kick = AsyncMock()
    publish_postcommit = AsyncMock()
    kick.return_value = SimpleNamespace(publish=publish_postcommit)
    audit = AsyncMock()
    publish = AsyncMock()
    monkeypatch.setattr(
        bulk_moderation,
        "_prune_candidates",
        AsyncMock(return_value=[(20, "home.example"), (21, "remote.example")]),
    )
    monkeypatch.setattr(bulk_moderation, "stage_kick_member", kick)
    monkeypatch.setattr(bulk_moderation, "add_audit_entry", audit)
    monkeypatch.setattr(bulk_moderation, "publish_dispatch", publish)

    result = await _perform_prune(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(domain="home.example"),  # type: ignore[arg-type]
        guild,
        SimpleNamespace(user=actor),  # type: ignore[arg-type]
        PruneRequest(days=14, include_roles=[EntityRef("30@home.example")]),
        reason="  inactive cleanup  ",
    )

    assert result["pruned"] == 2
    assert [call.args[:2] for call in kick.await_args_list] == [
        (EntityRef("10@home.example"), EntityRef("20@home.example")),
        (EntityRef("10@home.example"), EntityRef("21@remote.example")),
    ]
    assert all(call.kwargs["record_kick_audit"] is False for call in kick.await_args_list)
    assert audit.await_count == 1
    assert audit.await_args.args[4] == 21
    assert audit.await_args.kwargs["reason"] == "inactive cleanup"
    assert {
        change["key"]: change["new_value"] for change in audit.await_args.kwargs["changes"]
    } == {"delete_member_days": "14", "members_removed": "2", "include_roles": ["30@home.example"]}
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert session.begin_nested.call_count == 2
    assert publish_postcommit.await_count == 2
    publish.assert_awaited_once()
