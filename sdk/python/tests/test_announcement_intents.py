from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.errors import ApiError
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def authority_target(ref: EntityRef, target: str | None = None) -> str:
    if target is not None:
        return target
    return f"https://{ref.domain}"


def follow_payload(
    *,
    follow_id: int = 44,
    source_id: int = 10,
    source_domain: str = "source.example",
    target_id: int = 20,
    target_domain: str = "target.example",
) -> dict[str, object]:
    return {
        "id": str(follow_id),
        "ref": f"{follow_id}@{target_domain}",
        "source_channel_id": str(source_id),
        "source_channel_domain": source_domain,
        "target_channel_id": str(target_id),
        "target_channel_domain": target_domain,
        "creator_id": "2",
        "creator_domain": "apps.example",
        "active": True,
        "federated": source_domain != target_domain,
        "generation": "1" if source_domain != target_domain else None,
        "lifecycle_state": "active",
        "name": None,
        "avatar_hash": None,
        "created_at": "2026-08-29T00:00:00+00:00",
        "updated_at": "2026-08-29T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_follow_from_third_party_relay_signs_distinct_source_and_target_proofs() -> (
    None
):
    bot = client()
    bot._authority_target = authority_target  # type: ignore[method-assign]
    bot._federated_actor_intent = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **kwargs: {"audience": kwargs["audience"]}
    )
    bot.request = AsyncMock(return_value=follow_payload())  # type: ignore[method-assign]

    await bot.follow_announcement_channel(
        EntityRef(10, "source.example"),
        EntityRef(20, "target.example"),
        target="https://relay.example",
    )

    body = bot.request.await_args.kwargs["json"]
    assert body["actor_intents"] == {
        "source.example": {"audience": "https://source.example"},
        "target.example": {"audience": "https://target.example"},
    }
    calls = bot._federated_actor_intent.await_args_list  # type: ignore[attr-defined]
    assert {call.kwargs["runtime_target"] for call in calls} == {
        "https://source.example",
        "https://target.example",
    }


@pytest.mark.asyncio
async def test_list_from_third_party_relay_signs_only_for_source_authority() -> None:
    bot = client()
    bot._authority_target = authority_target  # type: ignore[method-assign]
    bot._federated_actor_intent = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **kwargs: {
            "action": kwargs["action"],
            "audience": kwargs["audience"],
        }
    )
    bot.request = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await bot.announcement_follows(
        EntityRef(10, "source.example"),
        target="https://relay.example",
    )

    encoded = bot.request.await_args.kwargs["headers"]["X-Kaede-Actor-Intents"]
    assert json.loads(encoded) == {
        "source.example": {
            "action": "announcement.follow.list",
            "audience": "https://source.example",
        }
    }
    assert bot._federated_actor_intent.await_args.kwargs["resources"] == {
        "source_channel": "10@source.example"
    }


@pytest.mark.asyncio
async def test_delete_from_third_party_relay_carries_both_receiver_proofs() -> None:
    bot = client()
    bot._authority_target = authority_target  # type: ignore[method-assign]
    bot._federated_actor_intent = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **kwargs: {"audience": kwargs["audience"]}
    )
    bot.announcement_follows = AsyncMock(  # type: ignore[method-assign]
        return_value=[follow_payload()]
    )
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await bot.delete_announcement_follow(
        EntityRef(10, "source.example"),
        EntityRef(44, "target.example"),
        target="https://relay.example",
    )

    encoded = bot.request.await_args.kwargs["headers"]["X-Kaede-Actor-Intents"]
    assert json.loads(encoded) == {
        "source.example": {"audience": "https://source.example"},
        "target.example": {"audience": "https://target.example"},
    }
    assert bot._federated_actor_intent.await_args_list[0].kwargs["resources"] == {
        "source_channel": "10@source.example",
        "follow_id": "44@target.example",
    }
    assert bot.request.await_args.args[1].endswith("/followers/44@target.example")


@pytest.mark.asyncio
async def test_delete_requires_qualified_ref_when_authorities_reuse_id() -> None:
    bot = client()
    bot._authority_target = authority_target  # type: ignore[method-assign]
    bot.announcement_follows = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            follow_payload(target_domain="alpha.example"),
            follow_payload(target_id=21, target_domain="beta.example"),
        ]
    )

    with pytest.raises(ApiError) as ambiguous:
        await bot.delete_announcement_follow(EntityRef(10, "source.example"), 44)
    assert ambiguous.value.status == 409
    assert ambiguous.value.code == "CHANNEL_FOLLOW_REF_REQUIRED"
