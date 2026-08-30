from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.responses import Response

from app.api import bots as bots_api
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Channel, DMConversation, DMParticipant, Guild, GuildMember, User
from app.federation import typing as federated_typing
from app.federation.typing import TypingProjection, TypingPublishRequest, TypingRelayRequest


def projection(**changes: object) -> TypingProjection:
    values: dict[str, object] = {
        "channel_id": "77",
        "channel_domain": "authority.example",
        "user_id": "42",
        "user_domain": "member.example",
        "observed_at": 1_000_000_000,
        "expires_at": 1_010,
    }
    values.update(changes)
    return TypingProjection.model_validate(values)


def actor_profile() -> dict[str, object]:
    return {
        "id": "42",
        "origin_domain": "member.example",
        "username": "maple",
        "display_name": None,
        "avatar_hash": None,
        "banner_hash": None,
        "bio": None,
        "custom_status": None,
        "account_type": "human",
        "profile_version": 1,
        "e2ee_device_generation": 0,
    }


def relay_projection(**changes: object) -> TypingRelayRequest:
    values: dict[str, object] = {
        **projection().model_dump(mode="json"),
        "audience_user_refs": ["7@local.example"],
        "batch_index": 0,
        "batch_count": 1,
    }
    values.update(changes)
    return TypingRelayRequest.model_validate(values)


def test_typing_wire_is_strict_canonical_and_actor_bound() -> None:
    valid = TypingPublishRequest(
        **projection().model_dump(mode="json"),
        actor=actor_profile(),
    )
    assert valid.user_id == "42"

    with pytest.raises(ValidationError):
        projection(observed_at=True)
    with pytest.raises(ValidationError):
        projection(channel_id="01")
    with pytest.raises(ValidationError):
        projection(expires_at=1_100)
    with pytest.raises(ValidationError):
        TypingPublishRequest(
            **projection().model_dump(mode="json"),
            actor=actor_profile() | {"id": "43"},
        )
    with pytest.raises(ValidationError):
        relay_projection(audience_user_refs=["7"])
    with pytest.raises(ValidationError):
        relay_projection(audience_user_refs=["8@local.example", "7@local.example"])
    with pytest.raises(ValidationError):
        relay_projection(batch_index=1)


def test_typing_freshness_rejects_expired_and_future_projections() -> None:
    assert federated_typing.typing_projection_is_fresh(projection(), now=1_000)
    assert not federated_typing.typing_projection_is_fresh(projection(), now=1_010)
    assert not federated_typing.typing_projection_is_fresh(
        projection(observed_at=1_006_000_000, expires_at=1_016),
        now=1_000,
    )


@pytest.mark.asyncio
async def test_typing_generation_is_monotonic_and_room_scoped() -> None:
    redis = SimpleNamespace(eval=AsyncMock(side_effect=[1, 0]))
    item = projection()

    assert await federated_typing.accept_typing_generation(cast(Any, redis), item)
    assert not await federated_typing.accept_typing_generation(cast(Any, redis), item)
    key = redis.eval.await_args_list[0].args[2]
    assert key == "federation:typing:authority.example:77:member.example:42"
    assert redis.eval.await_args_list[0].args[3] == f"{key}:batch:local"


def relay_objects(*, guild: bool) -> dict[str, object]:
    channel = SimpleNamespace(
        id=77,
        origin_domain="authority.example",
        unavailable=False,
        guild_id=9 if guild else None,
        guild_domain="authority.example" if guild else None,
    )
    actor = SimpleNamespace(id=42, origin_domain="member.example")
    return {
        "channel": channel,
        "actor": actor,
        "guild": SimpleNamespace(
            id=9,
            origin_domain="authority.example",
            unavailable=False,
        ),
        "conversation": SimpleNamespace(authority_domain="authority.example"),
        "membership": SimpleNamespace(),
    }


def relay_session(objects: dict[str, object], *, guild: bool) -> SimpleNamespace:
    async def get(model: type[object], _key: object) -> object | None:
        if model is Channel:
            return objects["channel"]
        if model is User:
            return objects["actor"]
        if model is Guild:
            return objects["guild"] if guild else None
        if model is DMConversation:
            return objects["conversation"] if not guild else None
        if model in {GuildMember, DMParticipant}:
            return objects["membership"]
        return None

    return SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=7),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("guild", [True, False])
async def test_typing_relay_requires_authority_actor_membership_and_local_recipient(
    guild: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objects = relay_objects(guild=guild)
    recipient_lookup = AsyncMock(return_value={"local.example": ["7@local.example"]})
    monkeypatch.setattr(
        federated_typing,
        "typing_recipient_refs_by_domain",
        recipient_lookup,
    )
    monkeypatch.setattr(
        federated_typing,
        "calculate_permissions",
        AsyncMock(return_value=(int(Permission.VIEW_CHANNEL), objects["membership"])),
    )
    channel, actor, audience = await federated_typing.validate_typing_relay_scope(
        cast(Any, relay_session(objects, guild=guild)),
        cast(Any, SimpleNamespace(domain="local.example")),
        relay_projection(),
        authority_domain="authority.example",
    )
    assert channel is objects["channel"]
    assert actor is objects["actor"]
    assert audience == {"7@local.example"}

    with pytest.raises(ValueError, match="another instance"):
        await federated_typing.validate_typing_relay_scope(
            cast(Any, relay_session(objects, guild=guild)),
            cast(Any, SimpleNamespace(domain="local.example")),
            relay_projection(audience_user_refs=["7@peer.example"]),
            authority_domain="authority.example",
        )

    objects["membership"] = None
    with pytest.raises(ValueError, match="scope"):
        await federated_typing.validate_typing_relay_scope(
            cast(Any, relay_session(objects, guild=guild)),
            cast(Any, SimpleNamespace(domain="local.example")),
            relay_projection(),
            authority_domain="authority.example",
        )

    objects["membership"] = SimpleNamespace()
    recipient_lookup.return_value = {}
    with pytest.raises(ValueError, match="recipient"):
        await federated_typing.validate_typing_relay_scope(
            cast(Any, relay_session(objects, guild=guild)),
            cast(Any, SimpleNamespace(domain="local.example")),
            relay_projection(),
            authority_domain="authority.example",
        )


@pytest.mark.asyncio
async def test_local_typing_dispatches_only_to_local_dm_participants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    channel = SimpleNamespace(
        id=77,
        origin_domain="authority.example",
        guild_id=None,
        guild_domain=None,
    )
    dispatched: list[tuple[str, str, dict[str, object]]] = []

    async def publish(
        _redis: object,
        topic: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        dispatched.append((topic, event_type, payload))

    monkeypatch.setattr(
        federated_typing,
        "typing_recipient_refs_by_domain",
        AsyncMock(
            return_value={
                "local.example": ["7@local.example", "3@local.example", "7@local.example"]
            }
        ),
    )
    monkeypatch.setattr(federated_typing, "publish_ephemeral", publish)
    await federated_typing.publish_local_typing(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, SimpleNamespace(domain="local.example")),
        cast(Any, channel),
        projection(),
        audience_user_refs={"3@local.example", "7@local.example", "99@local.example"},
    )

    assert [item[0] for item in dispatched] == [
        "user:local.example:3",
        "user:local.example:7",
    ]
    assert all(item[1] == "TYPING_START" for item in dispatched)
    assert all(item[2]["timestamp"] == 1_000 for item in dispatched)


@pytest.mark.asyncio
async def test_authority_typing_is_ephemeral_and_does_not_queue_durable_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace()
    calls: list[str] = []
    monkeypatch.setattr(
        federated_typing,
        "typing_projection_is_fresh",
        lambda _projection: True,
    )
    monkeypatch.setattr(
        federated_typing,
        "accept_typing_generation",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        federated_typing,
        "typing_recipient_refs_by_domain",
        AsyncMock(
            return_value={
                "authority.example": ["7@authority.example"],
                "peer.example": ["8@peer.example"],
            }
        ),
    )
    monkeypatch.setattr(
        federated_typing,
        "publish_local_typing",
        AsyncMock(side_effect=lambda *_args, **_kwargs: calls.append("local")),
    )
    monkeypatch.setattr(
        federated_typing,
        "fanout_typing",
        AsyncMock(side_effect=lambda *_args, **_kwargs: calls.append("direct")),
    )

    assert await federated_typing.publish_authoritative_typing(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, object()),
        cast(Any, SimpleNamespace(domain="authority.example")),
        cast(Any, relay_objects(guild=False)["channel"]),
        projection(),
    )
    assert calls == ["local", "direct"]


@pytest.mark.asyncio
async def test_typing_fanout_batches_exact_destination_home_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    signed = AsyncMock(return_value=SimpleNamespace(status_code=204))
    monkeypatch.setattr(federated_typing, "signed_request", signed)
    peer_audience = [f"{value}@peer.example" for value in range(1, 514)]

    await federated_typing.fanout_typing(
        cast(Any, lambda: SessionContext()),
        cast(Any, SimpleNamespace(domain="authority.example")),
        projection(),
        {
            "authority.example": ["9@authority.example"],
            "peer.example": peer_audience,
        },
        guild_context=True,
    )

    assert signed.await_count == 2
    payloads = [call.kwargs["payload"] for call in signed.await_args_list]
    assert [item["batch_index"] for item in payloads] == [0, 1]
    assert [item["batch_count"] for item in payloads] == [2, 2]
    assert sum(len(item["audience_user_refs"]) for item in payloads) == 513
    assert all(
        ref.endswith("@peer.example") for item in payloads for ref in item["audience_user_refs"]
    )
    assert all(call.kwargs["guild_context"] is True for call in signed.await_args_list)


@pytest.mark.asyncio
async def test_guild_typing_audience_excludes_members_without_channel_visibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=9, origin_domain="authority.example", unavailable=False)
    channel = SimpleNamespace(
        id=77,
        origin_domain="authority.example",
        guild_id=9,
        guild_domain="authority.example",
    )
    users = [
        SimpleNamespace(id=1, origin_domain="local.example"),
        SimpleNamespace(id=2, origin_domain="local.example"),
        SimpleNamespace(id=3, origin_domain="peer.example"),
    ]
    session = SimpleNamespace(
        get=AsyncMock(return_value=guild),
        scalars=AsyncMock(return_value=users),
    )
    monkeypatch.setattr(
        federated_typing,
        "calculate_permissions",
        AsyncMock(
            side_effect=[
                (int(Permission.VIEW_CHANNEL), object()),
                (0, object()),
                (int(Permission.VIEW_CHANNEL), object()),
            ]
        ),
    )

    assert await federated_typing.typing_recipient_refs_by_domain(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="local.example")),
        cast(Any, channel),
    ) == {
        "local.example": ["1@local.example"],
        "peer.example": ["3@peer.example"],
    }


@pytest.mark.asyncio
async def test_bot_typing_reuses_exact_installation_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = AsyncMock(return_value=(object(), object()))
    delegated = AsyncMock(return_value=Response(status_code=204))
    monkeypatch.setattr(bots_api, "installation_for_channel", installation)
    monkeypatch.setattr(bots_api, "typing", delegated)
    principal = SimpleNamespace(user=object())
    response = Response()
    request = SimpleNamespace()
    session = SimpleNamespace()
    redis = SimpleNamespace()
    local_settings = SimpleNamespace(domain="authority.example")

    result = await bots_api.bot_typing(
        EntityRef("77@authority.example"),
        response,
        cast(Any, request),
        cast(Any, principal),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, local_settings),
        91,
    )

    assert result.status_code == 204
    installation.assert_awaited_once_with(
        session,
        local_settings,
        principal,
        EntityRef("77@authority.example"),
        "messages.send",
        91,
    )
    delegated.assert_awaited_once()
