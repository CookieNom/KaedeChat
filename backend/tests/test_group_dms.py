from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.dms import publish_local_group_state
from app.chat.group_conversations import (
    apply_authoritative_group_mutation,
    group_conversation_content,
    reload_group_projection,
)
from app.chat.payloads import dm_channel_payload
from app.chat.schemas import DMGroupCreate
from app.core.dm import (
    GROUP_DM_MEMBER_ADDED,
    GROUP_DM_MEMBER_LEFT,
    GROUP_DM_MEMBER_REMOVED,
    group_dm_key,
    group_dm_notice_text,
)
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


def test_group_state_carries_an_authoritative_membership_notice() -> None:
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
        state_version=2,
    )
    channel = Channel(
        id=100,
        origin_domain="alpha.localhost",
        type=1,
        created_floor_id=100,
    )
    notice = {"message": {"id": "101"}, "target": {"id": "2"}}

    state = group_conversation_content(conversation, channel, [owner, peer], notice=notice)

    assert state["notice"] is notice


def test_group_membership_notices_explain_changes_and_owner_transfer() -> None:
    assert (
        group_dm_notice_text(GROUP_DM_MEMBER_ADDED, "Alice", "Bob")
        == "Alice added Bob to the group."
    )
    assert (
        group_dm_notice_text(GROUP_DM_MEMBER_REMOVED, "Alice", "Bob")
        == "Alice removed Bob from the group."
    )
    assert (
        group_dm_notice_text(GROUP_DM_MEMBER_LEFT, "Alice", "Alice", "Bob")
        == "Alice left the group. Bob is now the owner."
    )


@pytest.mark.asyncio
async def test_group_projection_is_reloaded_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation = cast(
        Any,
        SimpleNamespace(id=100, origin_domain="alpha.localhost", type="group"),
    )
    channel = cast(Any, SimpleNamespace(id=100, origin_domain="alpha.localhost"))
    participants = [user(1), user(2, "beta.localhost")]
    session = SimpleNamespace(get=AsyncMock(side_effect=[conversation, channel]))
    load_participants = AsyncMock(return_value=participants)
    monkeypatch.setattr(
        "app.chat.group_conversations.group_participants",
        load_participants,
    )

    loaded = await reload_group_projection(
        cast(Any, session),
        conversation.id,
        conversation.origin_domain,
    )

    assert loaded == (conversation, channel, participants)
    assert session.get.await_args_list[0].kwargs == {"populate_existing": True}
    assert session.get.await_args_list[1].kwargs == {"populate_existing": True}
    load_participants.assert_awaited_once_with(session, conversation)


@pytest.mark.asyncio
async def test_new_local_group_member_receives_a_live_channel_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = user(1)
    invitee = user(2)
    conversation = cast(
        Any,
        SimpleNamespace(id=100, origin_domain="alpha.localhost", type="group"),
    )
    channel = cast(Any, SimpleNamespace(id=100, origin_domain="alpha.localhost"))
    reload_projection = AsyncMock(return_value=(conversation, channel, [owner, invitee]))
    render_channel = AsyncMock(return_value={"id": "100", "origin_domain": "alpha.localhost"})
    publish = AsyncMock()
    monkeypatch.setattr("app.api.dms.reload_group_projection", reload_projection)
    monkeypatch.setattr("app.api.dms.rendered_dm_channel", render_channel)
    monkeypatch.setattr("app.api.dms.publish_dispatch", publish)

    loaded = await publish_local_group_state(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Settings, SimpleNamespace(domain="alpha.localhost")),
        conversation.id,
        conversation.origin_domain,
        {(owner.id, owner.origin_domain)},
    )

    assert loaded == (conversation, channel, [owner, invitee])
    assert [call.args[2] for call in publish.await_args_list] == [
        "CHANNEL_UPDATE",
        "CHANNEL_CREATE",
    ]


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
