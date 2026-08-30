from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.dm import group_dm_key
from app.core.federation import authority_attested_group_event_ref, sign_envelope
from app.core.settings import Settings
from app.db.models import User
from app.federation.events import build_envelope
from app.federation.replication import dm_message_origin_is_authorized
from app.federation.security import validated_event_envelope

AUTHORITY = "alpha.localhost"
ACTOR = (42, "beta.localhost")


def group_state() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "conversation": {
                "id": "12",
                "origin_domain": AUTHORITY,
                "pair_key": group_dm_key(AUTHORITY, 12),
                "type": "group",
                "authority_domain": AUTHORITY,
                "owner": {"id": "7", "origin_domain": AUTHORITY},
                "name": "Federated group",
                "state_version": "3",
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
            },
            "participants": [
                {"id": "7", "origin_domain": AUTHORITY},
                {"id": "42", "origin_domain": "beta.localhost"},
            ],
        },
        {},
    )


def group_message() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "message": {
                "id": "80",
                "origin_domain": "beta.localhost",
                "channel_id": "12",
                "channel_domain": AUTHORITY,
                "author_id": "42",
                "author_domain": "beta.localhost",
            },
            "author": {"id": "42", "origin_domain": "beta.localhost"},
        },
        {
            "conversation_id": "12",
            "conversation_domain": AUTHORITY,
            "state_version": "3",
        },
    )


def group_call() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "call": {
                "id": "90",
                "channel_id": "12",
                "channel_domain": AUTHORITY,
                "authority_domain": AUTHORITY,
                "room": "d.12.90",
                "state": "ringing",
                "created_at": 1_000,
                "ended_at": None,
                "caller": "42@beta.localhost",
                "participants": ["7@alpha.localhost", "42@beta.localhost"],
            }
        },
        {
            "conversation_id": "12",
            "conversation_domain": AUTHORITY,
            "state_version": "3",
        },
    )


@pytest.mark.parametrize(
    ("event_type", "factory"),
    [
        ("dm.group.state", group_state),
        ("dm.group.message.committed", group_message),
        ("dm.group.call.create", group_call),
    ],
)
def test_closed_group_authority_contract_accepts_exact_projection(
    event_type: str,
    factory: Any,
) -> None:
    content, context = factory()

    assert authority_attested_group_event_ref(
        event_type,
        content,
        context,
        expected_authority=AUTHORITY,
        actor_id=str(ACTOR[0]),
        actor_domain=ACTOR[1],
    ) == (12, AUTHORITY, 3)


@pytest.mark.parametrize(
    ("event_type", "factory", "mutation"),
    [
        (
            "dm.group.state",
            group_state,
            lambda content, _context: content["conversation"].update(
                {"pair_key": group_dm_key(AUTHORITY, 13)}
            ),
        ),
        (
            "dm.group.state",
            group_state,
            lambda content, _context: content["conversation"].update(
                {"authority_domain": "gamma.localhost"}
            ),
        ),
        (
            "dm.group.message.committed",
            group_message,
            lambda content, _context: content["author"].update({"id": "43"}),
        ),
        (
            "dm.group.message.committed",
            group_message,
            lambda content, _context: content["message"].update({"channel_id": "13"}),
        ),
        (
            "dm.group.message.committed",
            group_message,
            lambda content, _context: content["message"].update(
                {"origin_domain": "gamma.localhost"}
            ),
        ),
        (
            "dm.group.message.committed",
            group_message,
            lambda _content, context: context.update({"state_version": "03"}),
        ),
        (
            "dm.group.call.create",
            group_call,
            lambda content, _context: content["call"].update({"caller": "43@beta.localhost"}),
        ),
        (
            "dm.group.call.create",
            group_call,
            lambda content, _context: content["call"].update({"room": "d.13.90"}),
        ),
        (
            "dm.group.call.create",
            group_call,
            lambda content, _context: content["call"].update(
                {"authority_domain": "gamma.localhost"}
            ),
        ),
    ],
)
def test_closed_group_authority_contract_rejects_swapped_identities(
    event_type: str,
    factory: Any,
    mutation: Any,
) -> None:
    content, context = factory()
    mutation(content, context)

    assert (
        authority_attested_group_event_ref(
            event_type,
            content,
            context,
            expected_authority=AUTHORITY,
            actor_id=str(ACTOR[0]),
            actor_domain=ACTOR[1],
        )
        is None
    )


@pytest.mark.asyncio
async def test_builder_rejects_a_swapped_group_call_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=42,
        origin_domain="beta.localhost",
        is_local=False,
        account_type="human",
        username="remote",
    )
    monkeypatch.setattr(
        "app.federation.events.self_private_key",
        AsyncMock(return_value=("ed25519:test", object())),
    )
    monkeypatch.setattr("app.federation.events.sign_envelope", lambda *_args: "signature")
    content, context = group_call()
    envelope = await build_envelope(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain=AUTHORITY)),
        "dm.group.call.create",
        actor,
        content,
        context=context,
        authority_attested_actor=True,
    )
    assert envelope["actor"] == {"id": "42", "domain": "beta.localhost"}

    cast(dict[str, object], content["call"])["caller"] = "43@beta.localhost"
    with pytest.raises(ValueError, match="only sign events for its own users"):
        await build_envelope(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain=AUTHORITY)),
            "dm.group.call.create",
            actor,
            content,
            context=context,
            authority_attested_actor=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["human", "bot"])
async def test_builder_accepts_authority_committed_remote_human_and_bot_messages(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
) -> None:
    actor = User(
        id=42,
        origin_domain="beta.localhost",
        is_local=False,
        account_type=account_type,
        username="remote",
    )
    monkeypatch.setattr(
        "app.federation.events.self_private_key",
        AsyncMock(return_value=("ed25519:test", object())),
    )
    monkeypatch.setattr("app.federation.events.sign_envelope", lambda *_args: "signature")
    content, context = group_message()
    cast(dict[str, object], content["message"])["origin_domain"] = AUTHORITY

    envelope = await build_envelope(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain=AUTHORITY)),
        "dm.group.message.committed",
        actor,
        content,
        context=context,
        authority_attested_actor=True,
    )

    assert envelope["actor"] == {"id": "42", "domain": "beta.localhost"}


def test_remote_group_message_allows_only_author_or_group_authority_minted_id() -> None:
    common = {
        "author_domain": "beta.localhost",
        "conversation_type": "group",
        "conversation_authority": AUTHORITY,
        "authority_control": False,
    }

    assert dm_message_origin_is_authorized(
        message_origin="beta.localhost",
        event_origin=AUTHORITY,
        **common,
    )
    assert dm_message_origin_is_authorized(
        message_origin=AUTHORITY,
        event_origin=AUTHORITY,
        **common,
    )
    assert not dm_message_origin_is_authorized(
        message_origin="gamma.localhost",
        event_origin=AUTHORITY,
        **common,
    )
    assert not dm_message_origin_is_authorized(
        message_origin=AUTHORITY,
        event_origin="gamma.localhost",
        **common,
    )
    assert not dm_message_origin_is_authorized(
        message_origin=AUTHORITY,
        event_origin=AUTHORITY,
        **{**common, "conversation_type": "direct"},
    )


def validation_settings() -> Settings:
    return cast(
        Settings,
        SimpleNamespace(
            domain="replica.localhost",
            federation_clock_skew_seconds=300,
            federation_event_retention_days=7,
        ),
    )


def signed_group_message(
    private_key: Ed25519PrivateKey,
    *,
    swap_room: bool = False,
) -> dict[str, object]:
    content, context = group_message()
    if swap_room:
        cast(dict[str, object], content["message"])["channel_id"] = "13"
    envelope: dict[str, object] = {
        "event_id": "kcfe_groupauthority001",
        "origin": AUTHORITY,
        "type": "dm.group.message.committed",
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": "42", "domain": "beta.localhost"},
        "context": context,
        "content": content,
    }
    envelope["signatures"] = {AUTHORITY: {"ed25519:test": sign_envelope(envelope, private_key)}}
    return envelope


@pytest.mark.asyncio
async def test_signed_receiver_uses_the_same_closed_group_authority_contract() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))

    class KeySession:
        async def get(self, _model: object, _key: object) -> object:
            return SimpleNamespace(
                public_key=private_key.public_key().public_bytes_raw(),
                expired_at=None,
            )

    valid = await validated_event_envelope(
        cast(Any, KeySession()),
        validation_settings(),
        AUTHORITY,
        signed_group_message(private_key),
        allow_authority_attested_actor=True,
    )
    assert (valid.actor.id, valid.actor.domain) == ("42", "beta.localhost")

    with pytest.raises(ValueError, match="actor does not belong"):
        await validated_event_envelope(
            cast(Any, KeySession()),
            validation_settings(),
            AUTHORITY,
            signed_group_message(private_key, swap_room=True),
            allow_authority_attested_actor=True,
        )


def test_signed_group_fixture_has_a_real_signature() -> None:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    envelope = signed_group_message(private_key)
    signature = cast(dict[str, dict[str, str]], envelope["signatures"])[AUTHORITY]["ed25519:test"]
    assert len(base64.b64decode(signature, validate=True)) == 64
