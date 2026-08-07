from typing import Any

import pytest
from fastapi import HTTPException

from app.chat.mentions import merge_mention_recipients, role_mention_recipients
from app.core.permissions import Permission
from app.db.models import Guild, Role

DOMAIN = "alpha.localhost"


def guild() -> Guild:
    return Guild(
        id=10,
        origin_domain=DOMAIN,
        name="guild",
        owner_id=1,
        owner_domain=DOMAIN,
    )


def role(role_id: int, *, mentionable: bool = True) -> Role:
    return Role(
        id=role_id,
        origin_domain=DOMAIN,
        guild_id=10,
        guild_domain=DOMAIN,
        name="Cooks",
        permissions=0,
        position=1,
        mentionable=mentionable,
    )


class ScalarResult:
    def __init__(self, values: list[Role]) -> None:
        self.values = values

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.values)


class RowResult:
    def __init__(self, values: list[tuple[int, str]]) -> None:
        self.values = values

    def tuples(self) -> list[tuple[int, str]]:
        return self.values


class FakeSession:
    def __init__(self, roles: list[Role], recipients: list[tuple[int, str]]) -> None:
        self.roles = roles
        self.recipients = recipients

    async def scalars(self, _statement: Any) -> ScalarResult:
        return ScalarResult(self.roles)

    async def execute(self, _statement: Any) -> RowResult:
        return RowResult(self.recipients)


async def test_role_mention_resolves_federated_recipients() -> None:
    session = FakeSession([role(20)], [(1, DOMAIN), (2, "remote.example")])

    recipients = await role_mention_recipients(
        session,  # type: ignore[arg-type]
        guild(),
        f"hello <@&20@{DOMAIN}>",
        0,
    )

    assert recipients == [(1, DOMAIN), (2, "remote.example")]
    assert merge_mention_recipients([(1, DOMAIN)], recipients) == [
        (1, DOMAIN),
        (2, "remote.example"),
    ]


async def test_unmentionable_role_requires_mention_everyone() -> None:
    session = FakeSession([role(20, mentionable=False)], [])

    with pytest.raises(HTTPException) as raised:
        await role_mention_recipients(
            session,  # type: ignore[arg-type]
            guild(),
            f"hello <@&20@{DOMAIN}>",
            0,
        )
    assert raised.value.detail == {"code": "ROLE_NOT_MENTIONABLE"}

    assert (
        await role_mention_recipients(
            session,  # type: ignore[arg-type]
            guild(),
            f"hello <@&20@{DOMAIN}>",
            int(Permission.MENTION_EVERYONE),
        )
        == []
    )


async def test_role_mentions_cannot_reference_another_guild_domain() -> None:
    session = FakeSession([], [])
    with pytest.raises(HTTPException) as raised:
        await role_mention_recipients(
            session,  # type: ignore[arg-type]
            guild(),
            "hello <@&20@remote.example>",
            int(Permission.ADMINISTRATOR),
        )
    assert raised.value.detail == {"code": "INVALID_ROLE_MENTION"}
