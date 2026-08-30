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
) -> None:
    guild = Guild(
        id=10,
        origin_domain="home.example",
        name="Community",
        owner_id=1,
        owner_domain="home.example",
    )
    found_roles = Mock()
    found_roles.tuples.return_value = [(20, "home.example")]
    candidates = Mock()
    candidates.tuples.return_value = []
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[found_roles, candidates]),
    )
    monkeypatch.setattr(
        bulk_moderation,
        "highest_role",
        AsyncMock(
            return_value=Role(
                id=11,
                origin_domain="home.example",
                guild_id=10,
                guild_domain="home.example",
                name="Moderator",
                position=5,
            )
        ),
    )

    await _prune_candidates(
        session,
        SimpleNamespace(domain="home.example"),
        guild,
        days=7,
        include_roles=[EntityRef("20@home.example")],
        actor_ref=(2, "home.example"),
    )

    candidate_query = str(session.execute.await_args_list[1].args[0])
    assert "NOT (EXISTS" in candidate_query
    assert "member_roles.role_id NOT IN" in candidate_query
    assert "member_roles.role_id !=" in candidate_query
    assert "roles.position" in candidate_query


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
    assert kick.await_count == 2
    assert all(call.kwargs["record_kick_audit"] is False for call in kick.await_args_list)
    assert audit.await_count == 1
    assert audit.await_args.args[4] == 21
    assert audit.await_args.kwargs["reason"] == "inactive cleanup"
    assert {change["key"] for change in audit.await_args.kwargs["changes"]} == {
        "delete_member_days",
        "members_removed",
        "include_roles",
    }
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    assert session.begin_nested.call_count == 2
    assert publish_postcommit.await_count == 2
    publish.assert_awaited_once()
