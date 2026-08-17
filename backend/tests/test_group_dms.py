from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.federation as federation_api
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
from app.federation.replication import replicate_group_notice
from app.federation.schemas import DMGroupMutationRequest
from app.federation.security import FederationPrincipal
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
        "encryption_policy": {
            "mode": "plaintext",
            "state": "plaintext",
            "generation": "0",
            "protocol": None,
            "suite": None,
            "group_id": None,
            "epoch": None,
        },
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
async def test_group_authority_attests_an_authenticated_remote_leaving_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = Settings(
        domain="alpha.localhost",
        environment="test",
        secret_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
    )
    actor = user(1, "beta.localhost")
    remaining = user(2, "gamma.localhost")
    conversation = DMConversation(
        id=100,
        origin_domain="alpha.localhost",
        pair_key=group_dm_key("alpha.localhost", 100),
        type="group",
        authority_domain="alpha.localhost",
        owner_id=remaining.id,
        owner_domain=remaining.origin_domain,
        state_version=2,
    )
    channel = Channel(
        id=100,
        origin_domain="alpha.localhost",
        type=1,
        created_floor_id=100,
    )
    payload = DMGroupMutationRequest(
        action="leave",
        conversation_id="100",
        conversation_domain="alpha.localhost",
        actor={
            "id": str(actor.id),
            "origin_domain": actor.origin_domain,
            "username": actor.username,
        },
    )
    session = SimpleNamespace(commit=AsyncMock())
    build = AsyncMock(return_value={"event_id": "kcfe_test"})
    queue = AsyncMock()

    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        federation_api,
        "load_authoritative_group",
        AsyncMock(return_value=(conversation, channel)),
    )
    monkeypatch.setattr(
        federation_api,
        "apply_authoritative_group_mutation",
        AsyncMock(return_value=([actor, remaining], [remaining], False)),
    )
    monkeypatch.setattr(
        federation_api,
        "create_group_mutation_notice",
        AsyncMock(return_value=None),
    )
    content = {"conversation": {"id": "100"}, "participants": []}
    monkeypatch.setattr(federation_api, "group_conversation_content", lambda *_args, **_kw: content)
    monkeypatch.setattr(federation_api, "build_envelope", build)
    monkeypatch.setattr(federation_api, "queue_event", queue)
    monkeypatch.setattr(
        federation_api,
        "reload_group_projection",
        AsyncMock(return_value=(conversation, channel, [remaining])),
    )
    monkeypatch.setattr(federation_api, "enqueue_best_effort", AsyncMock())

    result = await federation_api.federation_group_dm_mutate(
        payload=payload,
        principal=FederationPrincipal("beta.localhost", "ed25519:test"),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        snowflake=cast(Any, SimpleNamespace()),
        settings=configured,
    )

    assert result is content
    assert build.await_args.args[:5] == (
        session,
        configured,
        "dm.group.state",
        actor,
        content,
    )
    assert build.await_args.kwargs == {"authority_attested_actor": True}
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_group_snapshot_accepts_add_notice_for_its_local_invitee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = user(1)
    invitee = user(2, "beta.localhost")
    existing_member = user(3, "gamma.localhost")
    conversation = DMConversation(
        id=100,
        origin_domain="alpha.localhost",
        pair_key=group_dm_key("alpha.localhost", 100),
        type="group",
        authority_domain="alpha.localhost",
        owner_id=author.id,
        owner_domain=author.origin_domain,
        state_version=7,
    )
    channel = Channel(
        id=100,
        origin_domain="alpha.localhost",
        type=1,
        created_floor_id=100,
    )
    created_at = datetime.now(UTC)
    raw_notice = {
        "author": {
            "id": str(author.id),
            "origin_domain": author.origin_domain,
            "username": author.username,
            "profile_version": 1,
        },
        "target": {
            "id": str(invitee.id),
            "origin_domain": invitee.origin_domain,
        },
        "message": {
            "id": "101",
            "origin_domain": author.origin_domain,
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "author_id": str(author.id),
            "author_domain": author.origin_domain,
            "content": group_dm_notice_text(
                GROUP_DM_MEMBER_ADDED,
                author.username,
                invitee.username,
            ),
            "e2ee": None,
            "message_type": GROUP_DM_MEMBER_ADDED,
            "flags": 4,
            "mention_user_refs": [],
            "attachments": [],
            "referenced_message_id": None,
            "referenced_message_domain": None,
            "client_nonce": None,
            "edited_at": None,
            "deleted_at": None,
            "created_at": created_at.isoformat(),
        },
    }
    session = SimpleNamespace(get=AsyncMock(return_value=None), add=MagicMock())
    monkeypatch.setattr(
        "app.federation.replication.upsert_remote_user",
        AsyncMock(return_value=author),
    )
    admit_message = AsyncMock()
    advance_cursor = AsyncMock()
    monkeypatch.setattr(
        "app.federation.replication.admit_federated_dm_message",
        admit_message,
    )
    monkeypatch.setattr(
        "app.federation.replication.advance_channel_cursor",
        advance_cursor,
    )
    monkeypatch.setattr(
        "app.federation.replication.validate_snowflake_timestamp",
        MagicMock(),
    )
    after = [author, existing_member, invitee]

    with pytest.raises(ValueError, match="membership transition"):
        await replicate_group_notice(
            cast(Any, session),
            cast(Settings, SimpleNamespace(domain="beta.localhost")),
            raw_notice,
            conversation,
            channel,
            [],
            after,
            previous_owner=(None, None),
            expected_actor=(author.id, author.origin_domain),
        )

    message = await replicate_group_notice(
        cast(Any, session),
        cast(Settings, SimpleNamespace(domain="beta.localhost")),
        raw_notice,
        conversation,
        channel,
        [],
        after,
        previous_owner=(None, None),
        expected_actor=(author.id, author.origin_domain),
        initial_snapshot=True,
    )

    assert message is not None
    assert (message.channel_id, message.channel_domain) == (
        conversation.id,
        conversation.origin_domain,
    )
    admit_message.assert_awaited_once()
    advance_cursor.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_group_snapshot_does_not_accept_add_notice_for_a_remote_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    author = user(1)
    target = user(2, "gamma.localhost")
    conversation = DMConversation(
        id=100,
        origin_domain="alpha.localhost",
        pair_key=group_dm_key("alpha.localhost", 100),
        type="group",
        authority_domain="alpha.localhost",
        owner_id=author.id,
        owner_domain=author.origin_domain,
        state_version=7,
    )
    channel = Channel(
        id=100,
        origin_domain="alpha.localhost",
        type=1,
        created_floor_id=100,
    )
    raw_notice = {
        "author": {
            "id": str(author.id),
            "origin_domain": author.origin_domain,
            "username": author.username,
            "profile_version": 1,
        },
        "target": {"id": str(target.id), "origin_domain": target.origin_domain},
        "message": {
            "id": "101",
            "origin_domain": author.origin_domain,
            "channel_id": str(channel.id),
            "channel_domain": channel.origin_domain,
            "author_id": str(author.id),
            "author_domain": author.origin_domain,
            "content": group_dm_notice_text(
                GROUP_DM_MEMBER_ADDED,
                author.username,
                target.username,
            ),
            "e2ee": None,
            "message_type": GROUP_DM_MEMBER_ADDED,
            "flags": 4,
            "mention_user_refs": [],
            "attachments": [],
            "referenced_message_id": None,
            "referenced_message_domain": None,
            "client_nonce": None,
            "edited_at": None,
            "deleted_at": None,
            "created_at": datetime.now(UTC).isoformat(),
        },
    }
    session = SimpleNamespace(get=AsyncMock(return_value=None), add=MagicMock())
    monkeypatch.setattr(
        "app.federation.replication.upsert_remote_user",
        AsyncMock(return_value=author),
    )

    with pytest.raises(ValueError, match="membership transition"):
        await replicate_group_notice(
            cast(Any, session),
            cast(Settings, SimpleNamespace(domain="beta.localhost")),
            raw_notice,
            conversation,
            channel,
            [],
            [author, target],
            previous_owner=(None, None),
            expected_actor=(author.id, author.origin_domain),
            initial_snapshot=True,
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
        AsyncMock(),
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
            AsyncMock(),
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


@pytest.mark.asyncio
async def test_group_authority_confirms_a_third_domain_invitee_at_their_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = cast(
        Settings,
        SimpleNamespace(domain="alpha.localhost"),
    )
    inviter = user(1, "beta.localhost")
    invitee = user(2, "gamma.localhost")
    conversation = cast(
        DMConversation,
        SimpleNamespace(id=100, origin_domain="alpha.localhost"),
    )
    session = cast(Any, SimpleNamespace())
    request = AsyncMock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(federation_api, "signed_request", request)

    await federation_api.authorize_group_invitee_at_home(
        session,
        configured,
        conversation,
        inviter,
        invitee,
    )

    assert request.await_args.args[:5] == (
        session,
        configured,
        "POST",
        "gamma.localhost",
        "/_kaede/v1/dm/groups/authorize",
    )
    assert request.await_args.kwargs["payload"] == {
        "conversation_id": "100",
        "conversation_domain": "alpha.localhost",
        "inviter": federation_api.profile_from_user(inviter),
        "invitee": federation_api.profile_from_user(invitee),
    }
