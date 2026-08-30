from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import app.federation.actor_intents as actor_intents
from app.db.bot_models import BotApplication, BotApplicationTarget
from app.db.models import User
from app.federation.actor_intents import (
    FederatedActorIntent,
    actor_intent_signing_bytes,
    consume_actor_intent_nonce,
    validate_human_actor_intent,
    validate_worker_actor_intent,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: object) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)


@pytest.mark.asyncio
async def test_actor_intent_nonce_allows_only_byte_identical_retry() -> None:
    redis = MemoryRedis()
    now = datetime(2026, 8, 29, tzinfo=UTC)
    arguments = {
        "authority_domain": "apps.example",
        "intent_kind": "bot-worker",
        "action": "announcement.follow.create",
        "actor_ref": (30, "apps.example"),
        "audience": "target.example",
        "nonce": "nonce_value_123456789",
        "expires_at": int(now.timestamp()) + 60,
        "now": now,
    }

    assert await consume_actor_intent_nonce(
        cast(Any, redis),
        **arguments,
        fingerprint=b"exact signed claims",
    )
    assert not await consume_actor_intent_nonce(
        cast(Any, redis),
        **arguments,
        fingerprint=b"exact signed claims",
    )
    with pytest.raises(ValueError, match="different claims"):
        await consume_actor_intent_nonce(
            cast(Any, redis),
            **arguments,
            fingerprint=b"substituted claims",
        )


@pytest.mark.asyncio
async def test_human_actor_intent_is_receiver_and_resource_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    issued_at = int(now.timestamp())
    envelope = SimpleNamespace(
        type="federation.actor.intent",
        actor=SimpleNamespace(id="30", domain="actor.example"),
        context={},
        ts=issued_at * 1000,
        content={
            "version": 1,
            "action": "announcement.follow.create",
            "audience": "target.example",
            "actor_ref": "30@actor.example",
            "resources": {
                "source_channel": "10@source.example",
                "target_channel": "20@target.example",
            },
            "issued_at": issued_at,
            "expires_at": issued_at + 60,
            "nonce": "human_nonce_123456789",
        },
    )
    validated = AsyncMock(return_value=envelope)
    monkeypatch.setattr(
        "app.federation.security.validated_event_envelope",
        validated,
    )
    resources = {
        "source_channel": "10@source.example",
        "target_channel": "20@target.example",
    }

    claims = await validate_human_actor_intent(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
        {"signed": True},
        expected_action="announcement.follow.create",
        expected_audience="target.example",
        expected_actor_ref=(30, "actor.example"),
        expected_resources=resources,
        now=now,
    )
    assert claims.audience == "target.example"
    assert validated.await_args.args[2] == "actor.example"

    with pytest.raises(ValueError, match="binding"):
        await validate_human_actor_intent(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="target.example")),
            {"signed": True},
            expected_action="announcement.follow.create",
            expected_audience="relay.example",
            expected_actor_ref=(30, "actor.example"),
            expected_resources=resources,
            now=now,
        )
    with pytest.raises(ValueError, match="binding"):
        await validate_human_actor_intent(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="target.example")),
            {"signed": True},
            expected_action="announcement.follow.create",
            expected_audience="target.example",
            expected_actor_ref=(30, "actor.example"),
            expected_resources=resources | {"follow_id": "44"},
            now=now,
        )


@pytest.mark.asyncio
async def test_worker_actor_intent_binds_live_worker_and_runtime_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    private_key = Ed25519PrivateKey.generate()
    application = SimpleNamespace(
        id=80,
        origin_domain="apps.example",
        bot_user_id=30,
        bot_user_domain="apps.example",
    )
    actor = SimpleNamespace(id=30, origin_domain="apps.example", account_type="bot")
    worker = SimpleNamespace(
        id=9,
        source_id=9,
        authority_id=9,
        application_id=80,
        application_domain="apps.example",
        generation=7,
        public_key=private_key.public_key().public_bytes_raw(),
    )
    runtime_target = SimpleNamespace(
        target_domain="target.example",
        runtime_manifest_generation=11,
        runtime_revocation_generation=13,
        runtime_access_revocation_generation=17,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is BotApplication and key == (80, "apps.example"):
            return application
        if model is User and key == (30, "apps.example"):
            return actor
        if model is BotApplicationTarget and key == (
            80,
            "apps.example",
            "target.example",
        ):
            return runtime_target
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=worker),
    )
    monkeypatch.setattr(actor_intents, "worker_runtime_ready", lambda *_args, **_kwargs: True)
    issued_at = int(now.timestamp())
    raw: dict[str, object] = {
        "version": 1,
        "action": "announcement.follow.create",
        "audience": "target.example",
        "application_ref": "80@apps.example",
        "actor_ref": "30@apps.example",
        "worker_id": "9",
        "worker_generation": "7",
        "runtime_target": "target.example",
        "runtime_manifest_generation": "11",
        "runtime_revocation_generation": "13",
        "runtime_access_revocation_generation": "17",
        "resources": {
            "source_channel": "10@source.example",
            "target_channel": "20@target.example",
        },
        "issued_at": issued_at,
        "expires_at": issued_at + 60,
        "nonce": "worker_nonce_123456789",
    }
    signature = private_key.sign(actor_intent_signing_bytes(raw))
    raw["signature"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")

    intent = await validate_worker_actor_intent(
        cast(Any, session),
        "target.example",
        raw,
        expected_action="announcement.follow.create",
        expected_audience="target.example",
        expected_application_ref=(80, "apps.example"),
        expected_actor_ref=(30, "apps.example"),
        expected_resources=cast(dict[str, str], raw["resources"]),
        runtime_target_domain="target.example",
        now=now,
    )
    assert isinstance(intent, FederatedActorIntent)

    with pytest.raises(ValueError, match="runtime"):
        await validate_worker_actor_intent(
            cast(Any, session),
            "target.example",
            raw | {"worker_generation": "8"},
            expected_action="announcement.follow.create",
            expected_audience="target.example",
            expected_application_ref=(80, "apps.example"),
            expected_actor_ref=(30, "apps.example"),
            expected_resources=cast(dict[str, str], raw["resources"]),
            runtime_target_domain="target.example",
            now=now,
        )
