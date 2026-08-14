from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.admin.auth import ROLE_CAPABILITIES, AdminPrincipal
from app.api.applications import (
    ApplicationPatch,
    CommandDefinition,
    CommandOptionDefinition,
    CredentialCreate,
    WorkerCreate,
    bot_username,
    normalize_values,
)
from app.api.bot_federation import _target_policy_allows
from app.api.bot_gateway import encrypted_message_event, event_intent, filtered_event
from app.api.interactions import InteractionCreate
from app.bots.auth import (
    BOT_APPLICATION_REQUEST_LIMIT,
    BOT_WORKER_REQUEST_LIMIT,
    BotPrincipal,
    dpop_message,
    worker_assertion_message,
)
from app.db.bot_models import BotApplication, BotToken, BotWorker
from app.db.models import User


def principal(*, scopes: set[str], intents: set[str]) -> BotPrincipal:
    now = datetime.now(UTC)
    user = User(
        id=10,
        origin_domain="apps.example",
        is_local=False,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
        profile_resolved=True,
        federation_introduced_by_domain="apps.example",
    )
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=30,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
    )
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"x" * 32,
        scopes=sorted(scopes),
        intents=sorted(intents),
        target_domains=[],
    )
    token = BotToken(
        id=50,
        token_hash=b"y" * 32,
        application_id=20,
        application_domain="apps.example",
        worker_id=40,
        scopes=sorted(scopes),
        intents=sorted(intents),
        issued_at=now,
        expires_at=now + timedelta(minutes=8),
    )
    return BotPrincipal(user, application, worker, token, frozenset(scopes), frozenset(intents))


def test_bot_username_is_normal_account_format_and_unique_suffix() -> None:
    assert bot_username("Weather Bot!", 123456789012345678) == "weather_bot_12345678"
    assert len(bot_username("x" * 100, 123456789012345678)) <= 32


def test_scope_and_worker_validation_is_fail_closed() -> None:
    assert normalize_values(
        ["messages.send", "messages.send"], frozenset({"messages.send"}), "scope"
    ) == ["messages.send"]
    with pytest.raises(ValueError, match="unsupported scope"):
        normalize_values(["administrator"], frozenset({"messages.send"}), "scope")
    with pytest.raises(ValidationError):
        WorkerCreate(name="x", public_key="A" * 43, scopes=["unknown"], intents=[])


def test_target_policy_explicit_deny_always_wins() -> None:
    assert _target_policy_allows("open", {}, "target.example")
    assert not _target_policy_allows("open", {"target.example": "deny"}, "target.example")
    assert _target_policy_allows("allowlist", {"target.example": "allow"}, "target.example")
    assert not _target_policy_allows("allowlist", {}, "target.example")
    assert not _target_policy_allows("local_only", {"target.example": "allow"}, "target.example")


def test_message_gateway_requires_intent_and_both_content_scopes() -> None:
    bot = principal(scopes={"messages.content"}, intents={"guild_messages", "message_content"})
    event = {
        "t": "MESSAGE_CREATE",
        "topic_seq": 7,
        "d": {"content": "secret", "attachments": [{"id": "1"}]},
    }
    redacted = filtered_event(
        bot, event, {"guild_messages", "message_content"}, {"messages.metadata"}
    )
    assert redacted is not None
    assert redacted["d"]["content"] is None
    assert redacted["d"]["attachments"] == []
    assert redacted["d"]["content_unavailable"] is True
    visible = filtered_event(
        bot, event, {"guild_messages", "message_content"}, {"messages.content"}
    )
    assert visible is not None and visible["d"]["content"] == "secret"
    assert filtered_event(bot, event, {"interactions"}, {"messages.content"}) is None


def test_gateway_intent_mapping_orders_reactions_before_messages() -> None:
    assert event_intent("MESSAGE_REACTION_ADD") == "message_reactions"
    assert event_intent("MESSAGE_CREATE") == "guild_messages"
    assert event_intent("INTERACTION_CREATE") == "interactions"


def test_gateway_encrypted_message_delivery_fails_closed() -> None:
    encrypted_channels = {(7, "guild.example")}
    plaintext = {
        "t": "MESSAGE_CREATE",
        "d": {"channel_id": "8", "channel_domain": "guild.example", "e2ee": None},
    }
    encrypted_channel = {
        "t": "MESSAGE_CREATE",
        "d": {"channel_id": "7", "channel_domain": "guild.example", "e2ee": None},
    }
    encrypted_envelope = {
        "t": "MESSAGE_CREATE",
        "d": {
            "channel_id": "8",
            "channel_domain": "guild.example",
            "e2ee": {"ciphertext": "opaque"},
        },
    }
    malformed = {"t": "MESSAGE_CREATE", "d": {}}
    interaction = {"t": "INTERACTION_CREATE", "d": {"channel_id": "7"}}
    assert not encrypted_message_event(plaintext, encrypted_channels)
    assert encrypted_message_event(encrypted_channel, encrypted_channels)
    assert encrypted_message_event(encrypted_envelope, encrypted_channels)
    assert encrypted_message_event(malformed, encrypted_channels)
    assert not encrypted_message_event(interaction, encrypted_channels)


def test_interaction_options_reject_non_json_and_resource_abuse() -> None:
    base = {"application_ref": "1@apps.example", "command_name": "poll"}
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={"value": float("nan")})
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={str(index): index for index in range(26)})
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={"value": "x" * (64 * 1024 + 1)})


def test_command_names_and_permissions_are_bounded() -> None:
    assert CommandDefinition(name="weather", description="Current weather").name == "weather"
    with pytest.raises(ValidationError):
        CommandDefinition(name="Not Valid")
    with pytest.raises(ValidationError):
        ApplicationPatch(default_permissions=1 << 63)


def test_admin_roles_are_fixed_and_owner_is_unbounded() -> None:
    assert set(ROLE_CAPABILITIES) == {
        "owner",
        "administrator",
        "trust_safety",
        "bot_reviewer",
        "operations",
        "auditor",
    }
    owner = AdminPrincipal(
        User(
            id=1,
            origin_domain="local.example",
            is_local=True,
            username="owner",
            password_hash="hash",
        ),
        frozenset({"owner"}),
        ROLE_CAPABILITIES["owner"],
    )
    owner.require("future.capability")


def test_worker_assertion_binds_target_and_nonce() -> None:
    first = worker_assertion_message(
        "1@apps.example", 2, "https://one.example/api/v1/bots/token", 10, 20, "nonce-a"
    )
    second = worker_assertion_message(
        "1@apps.example", 2, "https://two.example/api/v1/bots/token", 10, 20, "nonce-a"
    )
    replay = worker_assertion_message(
        "1@apps.example", 2, "https://one.example/api/v1/bots/token", 10, 20, "nonce-b"
    )
    assert first != second
    assert first != replay


def test_control_credentials_have_separate_minimal_scopes() -> None:
    assert CredentialCreate(label="Deployment").scopes == [
        "workers.manage",
        "commands.manage",
    ]
    with pytest.raises(ValidationError):
        CredentialCreate(label="unsafe", scopes=["messages.content"])


def test_command_options_are_typed_and_fail_closed() -> None:
    command = CommandDefinition(
        name="poll",
        description="Create a poll",
        options=[
            CommandOptionDefinition(
                type="string",
                name="question",
                description="Question",
                required=True,
                min_length=1,
                max_length=500,
            )
        ],
    )
    assert command.options[0].name == "question"
    with pytest.raises(ValidationError):
        CommandOptionDefinition(type="user", name="person", description="Person", min_length=1)
    with pytest.raises(ValidationError):
        CommandDefinition(name="poll", description="Poll", unexpected=True)


def test_dpop_proof_binds_query_parameters() -> None:
    base = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/bots/channels/1/messages",
        "headers": [],
        "scheme": "https",
        "server": ("chat.example", 443),
    }
    first = Request(base | {"query_string": b"before=2%40chat.example"})
    second = Request(base | {"query_string": b"before=3%40chat.example"})
    assert dpop_message(first, "token", 10, "nonce") != dpop_message(second, "token", 10, "nonce")


def test_bot_runtime_rate_limits_are_distinct_and_documented() -> None:
    assert BOT_WORKER_REQUEST_LIMIT.limit == 600
    assert BOT_WORKER_REQUEST_LIMIT.period_seconds == 60
    assert BOT_APPLICATION_REQUEST_LIMIT.limit == 1200
    assert BOT_APPLICATION_REQUEST_LIMIT.period_seconds == 60
