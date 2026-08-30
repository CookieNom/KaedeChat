from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

import app.api.expressions as expressions_api
import app.federation.expression_authorization as expression_federation
from app.chat.custom_stickers import sticker_item_payload
from app.chat.expression_authorization import (
    EXPRESSION_USE_AUTHORIZATION_EVENT,
    authority_attested_expression_use,
    build_expression_use_authorization,
    expression_actor_intent_resources,
    expression_custom_emoji_tokens,
)
from app.chat.rich_content import (
    ActionRow,
    Button,
    PartialEmoji,
    PollAnswer,
    PollCreate,
    PollMedia,
)
from app.core.permissions import Permission
from app.db.bot_models import BotApplication
from app.db.models import Guild, Sticker, User
from app.federation.expression_authorization import (
    acquire_expression_use_authorizations,
    validate_attested_expression_target,
    validate_expression_authorization_map,
    validated_expression_use_authorization,
)
from app.federation.schemas import RemoteUserProfile
from app.federation.security import FederationPrincipal


def _human() -> User:
    return User(
        id=1,
        origin_domain="a.example",
        username="alice",
        account_type="human",
        is_local=True,
        profile_version=1,
    )


def _authorization_content(
    *,
    now: datetime | None = None,
    operation_id: str = "create-1",
) -> dict[str, object]:
    return build_expression_use_authorization(
        source_authority="s.example",
        requester_ref="1@a.example",
        requester_type="human",
        application_ref=None,
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        target_message_ref=None,
        operation="message.create",
        operation_id=operation_id,
        emoji_tokens=["<:wave:88@s.example>"],
        sticker_items=[],
        nonce="n" * 24,
        now=now,
    )


def test_expression_projection_covers_content_components_polls_and_e2ee() -> None:
    component = ActionRow(
        components=[
            Button(
                style=1,
                label="Wave",
                custom_id="wave",
                emoji=PartialEmoji(id="89@s.example", name="button"),
            )
        ]
    )
    poll = PollCreate(
        question=PollMedia(text="Choose"),
        answers=[
            PollAnswer(
                poll_media=PollMedia(
                    text="One",
                    emoji=PartialEmoji(id="90@s.example", name="poll"),
                )
            ),
            PollAnswer(poll_media=PollMedia(text="Two")),
        ],
        duration=24,
    )

    assert expression_custom_emoji_tokens(
        content="hello <:text:88@s.example>",
        components=[component],
        poll=poll,
        e2ee=None,
        default_domain="t.example",
    ) == [
        "<:button:89@s.example>",
        "<:poll:90@s.example>",
        "<:text:88@s.example>",
    ]
    assert expression_custom_emoji_tokens(
        content=None,
        components=None,
        poll=None,
        e2ee={
            "rich_payload_digest": "0" * 64,
            "message_custom_emoji_refs": ["<a:secret:91@s.example>"],
        },
        default_domain="t.example",
    ) == ["<a:secret:91@s.example>"]


def test_expression_receipt_is_a_closed_authority_attested_actor_contract() -> None:
    content = _authorization_content()
    context = {
        "source_authority": "s.example",
        "target_channel_ref": "20@t.example",
    }
    assert authority_attested_expression_use(
        EXPRESSION_USE_AUTHORIZATION_EVENT,
        content,
        context,
        expected_authority="s.example",
        actor=("1", "a.example"),
    )
    assert not authority_attested_expression_use(
        EXPRESSION_USE_AUTHORIZATION_EVENT,
        content,
        context | {"target_channel_ref": "21@t.example"},
        expected_authority="s.example",
        actor=("1", "a.example"),
    )
    assert not authority_attested_expression_use(
        EXPRESSION_USE_AUTHORIZATION_EVENT,
        content,
        context,
        expected_authority="s.example",
        actor=("2", "a.example"),
    )


@pytest.mark.asyncio
async def test_actor_home_acquires_source_receipt_bound_to_third_party_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    content = _authorization_content(now=now)
    human_intent = {"signed": "by-a"}
    build_intent = AsyncMock(return_value=human_intent)
    signed_request = AsyncMock(
        return_value=httpx.Response(
            200,
            request=httpx.Request("POST", "https://s.example/_kaede/v1/expressions/authorize"),
            json={"content": content},
        )
    )
    validate_receipt = AsyncMock()
    monkeypatch.setattr(expression_federation, "build_human_actor_intent", build_intent)
    monkeypatch.setattr(expression_federation, "signed_request", signed_request)
    monkeypatch.setattr(
        expression_federation,
        "validated_expression_use_authorization",
        validate_receipt,
    )

    proofs, sticker_items = await acquire_expression_use_authorizations(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="a.example")),
        _human(),
        application_ref=None,
        actor_intents={},
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        target_message_ref=None,
        operation="message.create",
        operation_id="create-1",
        emoji_tokens=["<:wave:88@s.example>"],
        sticker_refs=[],
    )

    assert set(proofs) == {"s.example"}
    assert sticker_items == []
    assert build_intent.await_args.kwargs["audience"] == "s.example"
    resources = build_intent.await_args.kwargs["resources"]
    assert resources["source_authority"] == "s.example"
    assert resources["target_guild_ref"] == "10@t.example"
    assert resources["target_channel_ref"] == "20@t.example"
    request_payload = signed_request.await_args.kwargs["payload"]
    assert request_payload["actor_intent"] == human_intent
    assert request_payload["nonce"] == resources["authorization_nonce"]
    assert signed_request.await_args.args[3] == "s.example"
    validate_receipt.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_accepts_target_relay_only_with_actor_home_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = _human()
    validate_human = AsyncMock()
    issued = AsyncMock(return_value={"signed": "by-s"})
    monkeypatch.setattr(expressions_api, "validate_human_actor_intent", validate_human)
    monkeypatch.setattr(expressions_api, "upsert_remote_user", AsyncMock(return_value=actor))
    remote_user_allowed = AsyncMock()
    monkeypatch.setattr(
        expressions_api,
        "require_remote_user_creation_allowed",
        remote_user_allowed,
    )
    monkeypatch.setattr(expressions_api, "issue_expression_use_authorization", issued)
    monkeypatch.setattr(expressions_api, "enforce_federation_route_rate_limit", AsyncMock())
    payload = expressions_api.ExpressionUseAuthorizeRequest(
        actor=RemoteUserProfile(
            id="1",
            origin_domain="a.example",
            username="alice",
        ),
        actor_intent={"signed": "by-a"},
        source_authority="s.example",
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        operation="message.create",
        operation_id="create-1",
        emoji_tokens=["<:wave:88@s.example>"],
        nonce="n" * 24,
    )

    result = await expressions_api.federation_authorize_expression_use(
        payload,
        FederationPrincipal(origin="t.example", key_id="ed25519:test"),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="s.example")),
    )

    assert result == {"signed": "by-s"}
    assert remote_user_allowed.await_args.args[1] is actor
    assert validate_human.await_args.kwargs["expected_audience"] == "s.example"
    assert validate_human.await_args.kwargs["expected_actor_ref"] == (1, "a.example")
    expected_resources = expression_actor_intent_resources(
        source_authority="s.example",
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        target_message_ref=None,
        operation="message.create",
        operation_id="create-1",
        emoji_tokens=["<:wave:88@s.example>"],
        sticker_refs=[],
        authorization_nonce="n" * 24,
    )
    assert validate_human.await_args.kwargs["expected_resources"] == expected_resources
    assert validate_human.await_args.kwargs["redis"] is not None

    with pytest.raises(HTTPException) as unrelated_relay:
        await expressions_api.federation_authorize_expression_use(
            payload,
            FederationPrincipal(origin="relay.example", key_id="ed25519:test"),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="s.example")),
        )
    assert unrelated_relay.value.detail == {"code": "EXPRESSION_AUTHORIZATION_RELAY_INVALID"}


@pytest.mark.asyncio
async def test_source_bot_intent_binds_application_receiver_runtime_and_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=2,
        origin_domain="apps.example",
        username="helper",
        account_type="bot",
        is_local=False,
        profile_version=1,
    )
    application = SimpleNamespace(
        id=3,
        origin_domain="apps.example",
        bot_user_id=2,
        bot_user_domain="apps.example",
        status="active",
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda model, ref: (
                actor
                if model is User and ref == (2, "apps.example")
                else application
                if model is BotApplication and ref == (3, "apps.example")
                else None
            )
        )
    )
    validate_worker = AsyncMock()
    issued = AsyncMock(return_value={"signed": "by-s"})
    monkeypatch.setattr(expressions_api, "validate_worker_actor_intent", validate_worker)
    monkeypatch.setattr(expressions_api, "issue_expression_use_authorization", issued)
    monkeypatch.setattr(expressions_api, "enforce_federation_route_rate_limit", AsyncMock())
    payload = expressions_api.ExpressionUseAuthorizeRequest(
        actor=RemoteUserProfile(
            id="2",
            origin_domain="apps.example",
            account_type="bot",
            username="helper",
        ),
        application_ref="3@apps.example",
        actor_intent={"signed": "by-app-home-worker"},
        source_authority="s.example",
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        operation="message.create",
        operation_id="create-1",
        emoji_tokens=["<:wave:88@s.example>"],
        nonce="n" * 24,
    )
    redis = SimpleNamespace()

    result = await expressions_api.federation_authorize_expression_use(
        payload,
        FederationPrincipal(origin="t.example", key_id="ed25519:test"),
        cast(Any, session),
        cast(Any, redis),
        cast(Any, SimpleNamespace(domain="s.example")),
    )

    assert result == {"signed": "by-s"}
    assert validate_worker.await_args.kwargs == {
        "expected_action": "expression.use.authorize",
        "expected_audience": "s.example",
        "expected_application_ref": (3, "apps.example"),
        "expected_actor_ref": (2, "apps.example"),
        "expected_resources": expression_actor_intent_resources(
            source_authority="s.example",
            target_guild_ref="10@t.example",
            target_channel_ref="20@t.example",
            target_message_ref=None,
            operation="message.create",
            operation_id="create-1",
            emoji_tokens=["<:wave:88@s.example>"],
            sticker_refs=[],
            authorization_nonce="n" * 24,
        ),
        "runtime_target_domain": "t.example",
        "redis": redis,
    }
    assert issued.await_args.args[2] is application

    application.status = "suspended"
    with pytest.raises(HTTPException) as suspended:
        await expressions_api.federation_authorize_expression_use(
            payload,
            FederationPrincipal(origin="t.example", key_id="ed25519:test"),
            cast(Any, session),
            cast(Any, redis),
            cast(Any, SimpleNamespace(domain="s.example")),
        )
    assert suspended.value.detail == {"code": "BOT_NOT_INSTALLED"}
    assert validate_worker.await_count == 1


@pytest.mark.asyncio
async def test_source_rechecks_bot_installation_for_every_expression_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=2,
        origin_domain="apps.example",
        username="helper",
        account_type="bot",
        is_local=False,
        profile_version=1,
    )
    application = SimpleNamespace(id=3, origin_domain="apps.example")
    emoji = SimpleNamespace(guild_id=77, guild_domain="s.example")
    session = SimpleNamespace(get=AsyncMock(return_value=emoji))
    require_installations = AsyncMock()
    monkeypatch.setattr(expressions_api, "consume_actor_intent_nonce", AsyncMock())
    monkeypatch.setattr(expressions_api, "validate_custom_emoji_tokens", AsyncMock())
    monkeypatch.setattr(expressions_api, "resolve_sticker_items", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        expressions_api,
        "_require_bot_expression_source_installations",
        require_installations,
    )
    monkeypatch.setattr(
        expressions_api,
        "build_envelope",
        AsyncMock(return_value={"signed": "by-s"}),
    )
    payload = expressions_api.ExpressionUseAuthorizeRequest(
        actor=RemoteUserProfile(
            id="2",
            origin_domain="apps.example",
            account_type="bot",
            username="helper",
        ),
        application_ref="3@apps.example",
        actor_intent={"signed": "by-app-home-worker"},
        source_authority="s.example",
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        operation="message.create",
        operation_id="create-1",
        emoji_tokens=["<:wave:88@s.example>"],
        nonce="n" * 24,
    )

    await expressions_api.issue_expression_use_authorization(
        payload,
        actor,
        cast(Any, application),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="s.example")),
    )

    require_installations.assert_awaited_once_with(
        session,
        application,
        actor,
        {(77, "s.example")},
    )


def test_source_rejects_unavailable_sticker_before_disclosing_metadata() -> None:
    sticker = Sticker(
        id=99,
        origin_domain="s.example",
        guild_id=77,
        guild_domain="s.example",
        name="sticker",
        description=None,
        media_hash="0" * 64,
        animated=False,
        tags=[],
        available=False,
        creator_id=1,
        creator_domain="a.example",
    )

    with pytest.raises(HTTPException) as unavailable:
        sticker_item_payload(sticker)
    assert unavailable.value.detail == {"code": "CUSTOM_STICKER_UNAVAILABLE"}


@pytest.mark.asyncio
async def test_destination_verifies_exact_receipt_and_consumes_replay_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    content = _authorization_content(now=now)
    envelope = SimpleNamespace(
        type=EXPRESSION_USE_AUTHORIZATION_EVENT,
        actor=SimpleNamespace(id="1", domain="a.example"),
        context={
            "source_authority": "s.example",
            "target_channel_ref": "20@t.example",
        },
        content=content,
        ts=int(now.timestamp() * 1_000),
    )
    monkeypatch.setattr(
        expression_federation,
        "validated_event_envelope",
        AsyncMock(return_value=envelope),
    )
    redis = SimpleNamespace(set=AsyncMock(return_value=True), get=AsyncMock())

    authorization = await validated_expression_use_authorization(
        cast(Any, SimpleNamespace()),
        cast(Any, redis),
        cast(Any, SimpleNamespace(federation_clock_skew_seconds=60)),
        {},
        source_authority="s.example",
        requester_ref="1@a.example",
        requester_type="human",
        application_ref=None,
        target_guild_ref="10@t.example",
        target_channel_ref="20@t.example",
        target_message_ref=None,
        operation="message.create",
        operation_id="create-1",
        expected_emoji_tokens=["<:wave:88@s.example>"],
        expected_sticker_items=[],
        now=now,
    )

    assert authorization.operation_id == "create-1"
    redis.set.assert_awaited_once()
    with pytest.raises(ValueError, match="binding"):
        await validated_expression_use_authorization(
            cast(Any, SimpleNamespace()),
            cast(Any, redis),
            cast(Any, SimpleNamespace(federation_clock_skew_seconds=60)),
            {},
            source_authority="s.example",
            requester_ref="1@a.example",
            requester_type="human",
            application_ref=None,
            target_guild_ref="10@t.example",
            target_channel_ref="21@t.example",
            target_message_ref=None,
            operation="message.create",
            operation_id="create-1",
            expected_emoji_tokens=["<:wave:88@s.example>"],
            expected_sticker_items=[],
            now=now,
        )


@pytest.mark.asyncio
async def test_destination_rejects_future_dated_source_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    future = now + timedelta(minutes=10)
    envelope = SimpleNamespace(
        type=EXPRESSION_USE_AUTHORIZATION_EVENT,
        actor=SimpleNamespace(id="1", domain="a.example"),
        context={
            "source_authority": "s.example",
            "target_channel_ref": "20@t.example",
        },
        content=_authorization_content(now=future),
        ts=int(future.timestamp() * 1_000),
    )
    monkeypatch.setattr(
        expression_federation,
        "validated_event_envelope",
        AsyncMock(return_value=envelope),
    )

    with pytest.raises(ValueError, match="binding"):
        await validated_expression_use_authorization(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(federation_clock_skew_seconds=60)),
            {},
            source_authority="s.example",
            requester_ref="1@a.example",
            requester_type="human",
            application_ref=None,
            target_guild_ref="10@t.example",
            target_channel_ref="20@t.example",
            target_message_ref=None,
            operation="message.create",
            operation_id="create-1",
            expected_emoji_tokens=["<:wave:88@s.example>"],
            expected_sticker_items=[],
            now=now,
        )


@pytest.mark.asyncio
async def test_destination_requires_complete_authority_partition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = AsyncMock()
    monkeypatch.setattr(
        expression_federation,
        "validated_expression_use_authorization",
        validate,
    )
    kwargs: dict[str, Any] = {
        "requester_ref": "1@a.example",
        "requester_type": "human",
        "application_ref": None,
        "target_guild_ref": "10@t.example",
        "target_channel_ref": "20@t.example",
        "target_message_ref": None,
        "operation": "message.create",
        "operation_id": "create-1",
        "emoji_tokens": ["<:wave:88@s.example>"],
        "sticker_items": [],
    }
    with pytest.raises(ValueError, match="incomplete"):
        await validate_expression_authorization_map(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            {},
            **kwargs,
        )
    with pytest.raises(ValueError, match="incomplete"):
        await validate_expression_authorization_map(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            {"evil.example": {}},
            **kwargs,
        )
    validate.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_requires_external_expression_permissions() -> None:
    guild = Guild(
        id=10,
        origin_domain="t.example",
        name="Target",
        owner_id=1,
        owner_domain="a.example",
    )
    actor = _human()
    with pytest.raises(HTTPException) as emoji_denied:
        await validate_attested_expression_target(
            cast(Any, SimpleNamespace()),
            actor,
            guild,
            0,
            ["<:wave:88@s.example>"],
            [],
        )
    assert emoji_denied.value.detail == {"code": "USE_EXTERNAL_EMOJIS_REQUIRED"}
    with pytest.raises(HTTPException) as sticker_denied:
        await validate_attested_expression_target(
            cast(Any, SimpleNamespace()),
            actor,
            guild,
            int(Permission.USE_EXTERNAL_EMOJIS),
            [],
            [
                {
                    "id": "99",
                    "origin_domain": "s.example",
                    "name": "sticker",
                    "format_type": 1,
                    "media_hash": "0" * 64,
                }
            ],
        )
    assert sticker_denied.value.detail == {"code": "USE_EXTERNAL_STICKERS_REQUIRED"}

    await validate_attested_expression_target(
        cast(Any, SimpleNamespace()),
        actor,
        guild,
        int(Permission.USE_EXTERNAL_EMOJIS | Permission.USE_EXTERNAL_STICKERS),
        ["<:wave:88@s.example>"],
        [
            {
                "id": "99",
                "origin_domain": "s.example",
                "name": "sticker",
                "format_type": 1,
                "media_hash": "0" * 64,
            }
        ],
    )
