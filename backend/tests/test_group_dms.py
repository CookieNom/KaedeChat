from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.chat.group_conversations import (
    apply_authoritative_group_mutation,
    group_conversation_content,
)
from app.chat.payloads import dm_channel_payload
from app.chat.schemas import DMGroupCreate
from app.core.dm import group_dm_key
from app.core.settings import Settings
from app.db.models import Channel, DMConversation, User
from app.voice.schemas import CallResponse


def user(identifier: int, domain: str = "alpha.localhost") -> User:
    return User(
        id=identifier,
        origin_domain=domain,
        is_local=domain == "alpha.localhost",
        username=f"user{identifier}",
        password_hash="hash" if domain == "alpha.localhost" else None,
        email=f"user{identifier}@alpha.localhost" if domain == "alpha.localhost" else None,
    )


def test_group_create_requires_distinct_friends_and_accepts_an_optional_name() -> None:
    payload = DMGroupCreate(
        handles=["one@alpha.localhost", "two@beta.localhost"],
        name="  Weekend plans  ",
    )
    assert payload.name == "Weekend plans"
    assert payload.handles == ["one@alpha.localhost", "two@beta.localhost"]

    with pytest.raises(ValidationError):
        DMGroupCreate(handles=["one@alpha.localhost", "ONE@alpha.localhost"])
    with pytest.raises(ValidationError):
        DMGroupCreate(handles=["one@alpha.localhost"])


def test_group_lookup_key_is_stable_and_authority_scoped() -> None:
    assert group_dm_key("ALPHA.localhost.", 42) == group_dm_key("alpha.localhost", 42)
    assert group_dm_key("alpha.localhost", 42) != group_dm_key("beta.localhost", 42)
    assert group_dm_key("alpha.localhost", 42) != group_dm_key("alpha.localhost", 43)


def test_group_payload_exposes_owner_and_full_state_version() -> None:
    owner = user(1)
    peer = user(2, "beta.localhost")
    conversation = DMConversation(
        id=100,
        origin_domain="alpha.localhost",
        pair_key=group_dm_key("alpha.localhost", 100),
        type="group",
        authority_domain="alpha.localhost",
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
        state_version=7,
    )
    channel = Channel(
        id=100,
        origin_domain="alpha.localhost",
        type=1,
        name="Trip",
        created_floor_id=100,
    )

    state = group_conversation_content(conversation, channel, [owner, peer])
    assert state["conversation"] == {
        "id": "100",
        "origin_domain": "alpha.localhost",
        "pair_key": conversation.pair_key,
        "type": "group",
        "authority_domain": "alpha.localhost",
        "owner": {"id": "1", "origin_domain": "alpha.localhost"},
        "name": "Trip",
        "state_version": "7",
        "deleted": False,
    }
    rendered = dm_channel_payload(channel, [peer], conversation=conversation)
    assert rendered["conversation_type"] == "group"
    assert rendered["owner_id"] == "1"
    assert rendered["owner_domain"] == "alpha.localhost"


@pytest.mark.asyncio
async def test_owner_leave_transfers_ownership_to_the_earliest_remaining_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = user(1)
    successor = user(2, "beta.localhost")
    third = user(3, "gamma.localhost")
    conversation = cast(
        Any,
        SimpleNamespace(
            id=100,
            origin_domain="alpha.localhost",
            owner_id=owner.id,
            owner_domain=owner.origin_domain,
            state_version=5,
        ),
    )
    channel = cast(Any, SimpleNamespace(name="Trip", unavailable=False))
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace()),
        delete=AsyncMock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.chat.group_conversations.group_participants",
        AsyncMock(
            side_effect=[
                [owner, successor, third],
                [successor, third],
                [successor, third],
            ]
        ),
    )

    before, after, deleted = await apply_authoritative_group_mutation(
        cast(Any, session),
        cast(Settings, SimpleNamespace()),
        conversation,
        channel,
        owner,
        action="leave",
    )

    assert before == [owner, successor, third]
    assert after == [successor, third]
    assert not deleted
    assert (conversation.owner_id, conversation.owner_domain) == (
        successor.id,
        successor.origin_domain,
    )
    assert conversation.state_version == 6


@pytest.mark.asyncio
async def test_only_owner_can_remove_another_group_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = user(1)
    actor = user(2)
    target = user(3)
    conversation = cast(
        Any,
        SimpleNamespace(
            id=100,
            origin_domain="alpha.localhost",
            owner_id=owner.id,
            owner_domain=owner.origin_domain,
            state_version=5,
        ),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(
        "app.chat.group_conversations.group_participants",
        AsyncMock(return_value=[owner, actor, target]),
    )

    with pytest.raises(HTTPException) as error:
        await apply_authoritative_group_mutation(
            cast(Any, session),
            cast(Settings, SimpleNamespace()),
            conversation,
            cast(Any, SimpleNamespace(name=None, unavailable=False)),
            actor,
            action="remove",
            target=target,
        )
    assert error.value.detail == {"code": "GROUP_DM_OWNER_REQUIRED"}
    assert conversation.state_version == 5


def test_group_call_schema_supports_all_ten_members() -> None:
    participants = [f"{index}@instance{index}.example" for index in range(10)]
    call = CallResponse(
        id="56",
        channel_id="34",
        channel_domain="alpha.localhost",
        authority_domain="alpha.localhost",
        room="d.34.56",
        state="ringing",
        created_at=1,
        caller=participants[0],
        participants=participants,
    )
    assert len(call.participants) == 10
