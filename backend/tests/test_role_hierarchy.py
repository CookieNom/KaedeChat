from typing import Any

import pytest
from fastapi import HTTPException

from app.chat.hierarchy import (
    require_can_assign_member_role,
    require_can_manage_member,
    require_can_manage_role,
    role_rank,
    role_reorder_allowed,
)
from app.db.models import Guild, GuildMember, Role, User

DOMAIN = "alpha.localhost"


def role(role_id: int, position: int) -> Role:
    return Role(
        id=role_id,
        origin_domain=DOMAIN,
        guild_id=1,
        guild_domain=DOMAIN,
        name="test",
        permissions=0,
        position=position,
    )


def test_higher_position_outranks_lower_position() -> None:
    assert role_rank(role(50, 2)) > role_rank(role(10, 1))


def test_lower_snowflake_wins_equal_position() -> None:
    assert role_rank(role(10, 2)) > role_rank(role(50, 2))


def test_role_reorder_cannot_cross_actor_ceiling() -> None:
    actor = role(20, 5)
    target = role(30, 2)

    assert role_reorder_allowed(actor, target, 4)
    assert not role_reorder_allowed(actor, target, 5)
    assert not role_reorder_allowed(actor, role(10, 6), 1)


def guild(owner_id: int = 1) -> Guild:
    return Guild(
        id=1,
        origin_domain=DOMAIN,
        name="guild",
        owner_id=owner_id,
        owner_domain=DOMAIN,
    )


def user(user_id: int) -> User:
    return User(
        id=user_id,
        origin_domain=DOMAIN,
        is_local=True,
        username=f"user{user_id}",
        password_hash="test",
    )


@pytest.mark.parametrize("actor_position,target_position", [(2, 2), (1, 2)])
async def test_member_hierarchy_rejects_equal_or_higher_target(
    monkeypatch: pytest.MonkeyPatch,
    actor_position: int,
    target_position: int,
) -> None:
    target_member = GuildMember(
        guild_id=1,
        guild_domain=DOMAIN,
        user_id=3,
        user_domain=DOMAIN,
    )

    async def member(*_args: Any) -> GuildMember:
        return target_member

    async def highest(_session: Any, _guild: Guild, user_id: int, _domain: str) -> Role:
        role_id = 20 if user_id == 2 else 10
        return role(role_id, actor_position if user_id == 2 else target_position)

    monkeypatch.setattr("app.chat.hierarchy.guild_member", member)
    monkeypatch.setattr("app.chat.hierarchy.highest_role", highest)

    with pytest.raises(HTTPException) as raised:
        await require_can_manage_member(
            None,  # type: ignore[arg-type]
            guild(),
            user(2),
            3,
            DOMAIN,
        )
    assert raised.value.status_code == 403
    assert raised.value.detail == {"code": "ROLE_HIERARCHY"}


async def test_member_hierarchy_allows_strictly_lower_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_member = GuildMember(
        guild_id=1,
        guild_domain=DOMAIN,
        user_id=3,
        user_domain=DOMAIN,
    )

    async def member(*_args: Any) -> GuildMember:
        return target_member

    async def highest(_session: Any, _guild: Guild, user_id: int, _domain: str) -> Role:
        return role(user_id, 3 if user_id == 2 else 2)

    monkeypatch.setattr("app.chat.hierarchy.guild_member", member)
    monkeypatch.setattr("app.chat.hierarchy.highest_role", highest)

    managed = await require_can_manage_member(
        None,  # type: ignore[arg-type]
        guild(),
        user(2),
        3,
        DOMAIN,
    )
    assert managed is target_member


async def test_role_hierarchy_rejects_equal_role(monkeypatch: pytest.MonkeyPatch) -> None:
    async def highest(*_args: Any) -> Role:
        return role(20, 2)

    monkeypatch.setattr("app.chat.hierarchy.highest_role", highest)
    with pytest.raises(HTTPException) as raised:
        await require_can_manage_role(
            None,  # type: ignore[arg-type]
            guild(),
            user(2),
            role(10, 2),
        )
    assert raised.value.detail == {"code": "ROLE_HIERARCHY"}


async def test_role_assignment_allows_self_without_moderating_self(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_member = GuildMember(
        guild_id=1,
        guild_domain=DOMAIN,
        user_id=2,
        user_domain=DOMAIN,
    )

    async def member(*_args: Any) -> GuildMember:
        return target_member

    async def unexpected_highest(*_args: Any) -> Role:
        raise AssertionError("self assignment should not compare the member against themself")

    monkeypatch.setattr("app.chat.hierarchy.guild_member", member)
    monkeypatch.setattr("app.chat.hierarchy.highest_role", unexpected_highest)

    managed = await require_can_assign_member_role(
        None,  # type: ignore[arg-type]
        guild(),
        user(2),
        2,
        DOMAIN,
    )
    assert managed is target_member


async def test_role_assignment_keeps_owner_immune_from_other_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_member = GuildMember(
        guild_id=1,
        guild_domain=DOMAIN,
        user_id=1,
        user_domain=DOMAIN,
    )

    async def member(*_args: Any) -> GuildMember:
        return target_member

    monkeypatch.setattr("app.chat.hierarchy.guild_member", member)

    with pytest.raises(HTTPException) as raised:
        await require_can_assign_member_role(
            None,  # type: ignore[arg-type]
            guild(),
            user(2),
            1,
            DOMAIN,
        )
    assert raised.value.detail == {"code": "OWNER_IMMUNE"}
