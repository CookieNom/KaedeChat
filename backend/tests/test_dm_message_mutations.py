from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.chat.dm_mutations import authority_attested_dm_message_mutation
from app.federation import dm_mutations
from app.federation.dm_mutations import apply_dm_message_mutation

AUTHORITY = "authority.example"
ACTOR_REF = (42, "member.example")
CONVERSATION_REF = (77, AUTHORITY)
MESSAGE_REF = (123, "member.example")


def context() -> dict[str, object]:
    return {"conversation_id": "77", "conversation_domain": AUTHORITY}


def common_content(**extra: object) -> dict[str, object]:
    return {
        "message_id": "123",
        "message_domain": "member.example",
        "channel_id": "77",
        "channel_domain": AUTHORITY,
        **extra,
    }


@pytest.mark.parametrize(
    ("event_type", "content"),
    [
        (
            "dm.reaction.add",
            common_content(user_id="42", user_domain="member.example", emoji="👍"),
        ),
        (
            "dm.reaction.remove",
            common_content(user_id="42", user_domain="member.example", emoji="👍"),
        ),
        ("dm.pin.add", common_content(user_id="42", user_domain="member.example")),
        ("dm.pin.remove", common_content(user_id="42", user_domain="member.example")),
        (
            "dm.message.delete",
            common_content(deleted_at="2026-08-28T10:00:00+00:00"),
        ),
        (
            "dm.message.update",
            {
                "message": {
                    "id": "123",
                    "origin_domain": "member.example",
                    "channel_id": "77",
                    "channel_domain": AUTHORITY,
                    "author_id": "42",
                    "author_domain": "member.example",
                    "edited_at": "2026-08-28T10:00:00+00:00",
                }
            },
        ),
    ],
)
def test_authority_attested_dm_mutations_accept_only_exact_shapes(
    event_type: str,
    content: dict[str, object],
) -> None:
    assert authority_attested_dm_message_mutation(
        event_type,
        content,
        context(),
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )
    swapped = copy.deepcopy(content)
    swapped["unexpected"] = True
    assert not authority_attested_dm_message_mutation(
        event_type,
        swapped,
        context(),
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )


def test_authority_attested_dm_mutation_rejects_actor_and_authority_swaps() -> None:
    content = common_content(
        user_id="42",
        user_domain="member.example",
        emoji="👍",
    )

    assert not authority_attested_dm_message_mutation(
        "dm.reaction.add",
        content | {"user_id": "43"},
        context(),
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )
    assert not authority_attested_dm_message_mutation(
        "dm.reaction.add",
        content,
        context() | {"conversation_domain": "other.example"},
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )


@pytest.mark.parametrize(
    "emoji",
    [
        "❤️",
        "<:lantern:75512661369970689@HOME.EXAMPLE.>",
    ],
)
def test_authority_attested_dm_mutation_accepts_reaction_aliases(emoji: str) -> None:
    assert authority_attested_dm_message_mutation(
        "dm.reaction.add",
        common_content(
            user_id="42",
            user_domain="member.example",
            emoji=emoji,
        ),
        context(),
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )


@pytest.mark.parametrize("emoji", ["lantern", "🏮🔥", "\ufe0f"])
def test_authority_attested_dm_mutation_rejects_invalid_reactions(emoji: str) -> None:
    assert not authority_attested_dm_message_mutation(
        "dm.reaction.add",
        common_content(
            user_id="42",
            user_domain="member.example",
            emoji=emoji,
        ),
        context(),
        expected_authority=AUTHORITY,
        actor=(str(ACTOR_REF[0]), ACTOR_REF[1]),
    )


def mutation_objects() -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    conversation = SimpleNamespace(
        id=CONVERSATION_REF[0],
        origin_domain=CONVERSATION_REF[1],
        authority_domain=AUTHORITY,
    )
    channel = SimpleNamespace(
        id=CONVERSATION_REF[0],
        origin_domain=CONVERSATION_REF[1],
        guild_id=None,
    )
    actor = SimpleNamespace(id=ACTOR_REF[0], origin_domain=ACTOR_REF[1])
    message = SimpleNamespace(
        id=MESSAGE_REF[0],
        origin_domain=MESSAGE_REF[1],
        channel_id=CONVERSATION_REF[0],
        channel_domain=CONVERSATION_REF[1],
        author_id=ACTOR_REF[0],
        author_domain=ACTOR_REF[1],
        created_at=datetime(2026, 8, 28, 9, tzinfo=UTC),
        edited_at=None,
        deleted_at=None,
        message_type=0,
        content="before",
        e2ee=None,
    )
    return conversation, channel, actor, message


def mutation_session(
    conversation: SimpleNamespace,
    channel: SimpleNamespace,
    actor: SimpleNamespace,
    message: SimpleNamespace,
    *,
    changed: int | None = 123,
    scalar_results: list[object] | None = None,
) -> SimpleNamespace:
    async def get(model: type[object], key: object, **_kwargs: object) -> object | None:
        name = model.__name__
        if name == "DMConversation":
            return conversation
        if name == "Channel":
            return channel
        if name == "User":
            return actor
        if name == "DMParticipant":
            return SimpleNamespace()
        if name == "Message":
            return message
        return None

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=(
            AsyncMock(side_effect=scalar_results)
            if scalar_results is not None
            else AsyncMock(return_value=changed)
        ),
        execute=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "content", "expected"),
    [
        (
            "dm.reaction.add",
            common_content(user_id="42", user_domain="member.example", emoji="👍"),
            {"reaction": "👍", "removed": False},
        ),
        (
            "dm.reaction.remove",
            common_content(user_id="42", user_domain="member.example", emoji="👍"),
            {"reaction": "👍", "removed": True},
        ),
        (
            "dm.pin.add",
            common_content(user_id="42", user_domain="member.example"),
            {"pinned": True},
        ),
        (
            "dm.pin.remove",
            common_content(user_id="42", user_domain="member.example"),
            {"pinned": False},
        ),
    ],
)
async def test_remote_home_applies_reaction_and_pin_deltas_idempotently(
    event_type: str,
    content: dict[str, object],
    expected: dict[str, object],
) -> None:
    conversation, channel, actor, message = mutation_objects()
    latest_pin_at = datetime(2026, 8, 28, 10, tzinfo=UTC)
    scalar_results = {
        "dm.pin.add": [0, 123, latest_pin_at],
        "dm.pin.remove": [123, None],
    }.get(event_type)
    session = mutation_session(
        conversation,
        channel,
        actor,
        message,
        scalar_results=scalar_results,
    )

    result = await apply_dm_message_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        event_type=event_type,
        content=content,
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=1_777_000_000_000,
    )

    assert len(result.dispatches) == 1
    assert expected.items() <= result.dispatches[0][1].items()

    duplicate_session = mutation_session(
        conversation,
        channel,
        actor,
        message,
        changed=None,
        scalar_results=([0, None] if event_type == "dm.pin.add" else None),
    )
    duplicate = await apply_dm_message_mutation(
        cast(Any, duplicate_session),
        cast(Any, SimpleNamespace(domain="local.example")),
        event_type=event_type,
        content=content,
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=1_777_000_000_000,
    )
    assert duplicate.dispatches == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["dm.reaction.add", "dm.reaction.remove"])
@pytest.mark.parametrize(
    ("emoji", "canonical"),
    [
        ("❤️", "❤"),
        (
            "<:lantern:75512661369970689@HOME.EXAMPLE.>",
            "<:lantern:75512661369970689@home.example>",
        ),
    ],
)
async def test_remote_home_canonicalizes_reaction_aliases_before_persist_and_dispatch(
    event_type: str,
    emoji: str,
    canonical: str,
) -> None:
    conversation, channel, actor, message = mutation_objects()
    session = mutation_session(conversation, channel, actor, message)

    result = await apply_dm_message_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        event_type=event_type,
        content=common_content(
            user_id="42",
            user_domain="member.example",
            emoji=emoji,
        ),
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=1_777_000_000_000,
    )

    statement = session.scalar.await_args.args[0]
    assert canonical in statement.compile().params.values()
    assert result.dispatches[0][1]["reaction"] == canonical
    assert result.dispatches[0][1]["removed"] is event_type.endswith("remove")


@pytest.mark.asyncio
async def test_remote_home_applies_message_tombstone_once() -> None:
    conversation, channel, actor, message = mutation_objects()
    session = mutation_session(conversation, channel, actor, message)
    deleted_at = message.created_at + timedelta(minutes=1)

    result = await apply_dm_message_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        event_type="dm.message.delete",
        content=common_content(deleted_at=deleted_at.isoformat()),
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=int((deleted_at + timedelta(milliseconds=1)).timestamp() * 1_000),
    )

    assert message.content is None
    assert message.deleted_at == deleted_at
    session.execute.assert_awaited_once()
    assert result.dispatches[0][0] == "MESSAGE_DELETE"


@pytest.mark.asyncio
async def test_full_message_update_uses_strict_projection_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation, channel, actor, message = mutation_objects()
    edited_at = message.created_at + timedelta(minutes=1)
    raw = {
        "id": "123",
        "origin_domain": "member.example",
        "channel_id": "77",
        "channel_domain": AUTHORITY,
        "author_id": "42",
        "author_domain": "member.example",
        "edited_at": edited_at.isoformat(),
    }
    session = mutation_session(conversation, channel, actor, message)
    strict_update = AsyncMock(return_value=None)
    monkeypatch.setattr(dm_mutations, "_apply_message_update", strict_update)

    result = await apply_dm_message_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        event_type="dm.message.update",
        content={"message": raw},
        context=context(),
        event_origin=AUTHORITY,
        actor_ref=ACTOR_REF,
        event_timestamp_ms=int(edited_at.timestamp() * 1_000),
    )

    strict_update.assert_awaited_once()
    assert result.dispatches == ()
    assert result.render_message_update is True
