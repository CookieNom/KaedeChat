from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.api.bot_gateway as bot_gateway
import app.bots.interaction_dispatch as interaction_dispatch
from app.bots.interaction_owners import (
    INTERACTION_EVENT_SNAPSHOT_KEY,
    INTERACTION_INSTALLATION_LINEAGE_KEY,
    installation_authority_lineage,
)
from app.chat.events import PUBLISH_EPHEMERAL_ONCE_SCRIPT, publish_ephemeral_once
from app.core.permissions import Permission
from app.db.bot_models import (
    ApplicationCommand,
    BotApplication,
    BotDMCapability,
    BotInstallation,
    BotInteraction,
    BotUserInstallation,
)
from app.db.models import Channel, User


class _QueueSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)


class _LookupSession:
    def __init__(
        self,
        *,
        interaction: BotInteraction,
        application: object,
        command: object,
        installation: object,
        rows: list[tuple[object, BotInteraction]] | None = None,
    ) -> None:
        self.interaction = interaction
        self.application = application
        self.command = command
        self.installation = installation
        self.channel = SimpleNamespace(
            id=interaction.channel_id,
            origin_domain=interaction.channel_domain,
            guild_id=interaction.guild_id,
            guild_domain=interaction.guild_domain,
            parent_id=None,
            parent_domain=None,
            unavailable=False,
            type=0,
        )
        self.channels: dict[tuple[int, str], object] = {
            (self.channel.id, self.channel.origin_domain): self.channel
        }
        self.invoker = SimpleNamespace(
            id=interaction.user_id,
            origin_domain=interaction.user_domain,
            account_type="human",
            disabled_at=None,
        )
        self.rows = rows or []
        self.deleted: list[object] = []
        self.commit = AsyncMock()

    async def __aenter__(self) -> _LookupSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, model: object, key: object, **_kwargs: object) -> object | None:
        if model is BotInteraction:
            return self.interaction if key == self.interaction.id else None
        if model is BotApplication:
            return self.application
        if model is ApplicationCommand:
            return self.command
        if model is Channel:
            return self.channels.get(key)  # type: ignore[arg-type]
        if model is User:
            return self.invoker
        if model in {BotInstallation, BotUserInstallation, BotDMCapability}:
            return self.installation
        raise AssertionError(f"unexpected model lookup: {model!r}")

    async def scalar(self, _statement: object) -> object:
        return self.installation

    async def scalars(self, _statement: object) -> list[object]:
        return [self.rows[0][0]] if self.rows else []

    async def execute(self, _statement: object) -> object:
        return SimpleNamespace(tuples=lambda: self.rows)

    async def delete(self, value: object) -> None:
        self.deleted.append(value)


class _SessionFactory:
    def __init__(self, session: _LookupSession) -> None:
        self.session = session

    def __call__(self) -> _LookupSession:
        return self.session


def _interaction_fixture(
    *, integration_type: str = "user_install"
) -> tuple[
    SimpleNamespace,
    BotInteraction,
    dict[str, object],
    object,
    object,
    object,
]:
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    is_capability = integration_type == "dm_capability"
    interaction = BotInteraction(
        id=101,
        application_id=1,
        application_domain="apps.example",
        installation_id=None,
        user_installation_id=None if is_capability else 77,
        dm_capability_id=88 if is_capability else None,
        guild_id=None,
        guild_domain=None,
        channel_id=5,
        channel_domain="chat.example",
        user_id=10,
        user_domain="chat.example",
        interaction_type="command",
        context="bot_dm" if is_capability else "private_channel",
        integration_type=integration_type,
        invocation_permissions=None,
        installation_revision=4,
        command_id=55,
        command_name="weather",
        command_type="chat_input",
        payload={
            "options": [{"name": "city", "value": "Paris"}],
            "values": [],
            "components": [],
            "resolved": {"users": {}},
        },
        encrypted_payload=None,
        message_id=None,
        message_domain=None,
        custom_id=None,
        token_hash=hashlib.sha256(token.encode()).digest(),
        callback_type=None,
        acknowledged_at=None,
        autocomplete_generation=None,
        status="pending",
        expires_at=now + timedelta(minutes=15),
        responded_at=None,
        response_message_id=None,
        response_message_domain=None,
        created_at=now,
        updated_at=now,
    )
    definition: dict[str, object] = {
        "name": "weather",
        "type": "chat_input",
        "description": "Show the forecast",
    }
    event: dict[str, object] = {
        "id": str(interaction.id),
        "interaction_ref": f"{interaction.id}@chat.example",
        "token": token,
        "type": "command",
        "context": interaction.context,
        "integration_type": integration_type,
        "application_ref": "1@apps.example",
        "installation_id": None,
        "user_installation_id": None if is_capability else "77",
        "bot_dm_capability_id": "kbdg_" + "g" * 43 if is_capability else None,
        "bot_dm_capability_revision": "4" if is_capability else None,
        "installation_ref": "66@guild.example" if is_capability else None,
        "installation_type": "guild" if is_capability else None,
        "installation_revision": "4",
        "guild_ref": None,
        "channel_ref": "5@chat.example",
        "channel_id": "5",
        "channel_domain": "chat.example",
        "message_ref": None,
        "target_ref": None,
        "target_id": None,
        "resolved": {"users": {}},
        "response_id": None,
        "response_ref": None,
        "view_version": None,
        "custom_id": None,
        "component_type": None,
        "focused_option": None,
        "autocomplete_generation": None,
        "values": [],
        "components": [],
        "source_component": None,
        "source_modal": None,
        "user": {
            "id": "10",
            "origin_domain": "chat.example",
            "username": "alice",
            "display_name": "Alice",
        },
        "user_ref": "10@chat.example",
        "command": definition,
        "command_id": "55",
        "options": [{"name": "city", "value": "Paris"}],
        "encrypted_payload": None,
        "expires_at": interaction.expires_at.isoformat(),
        "ack_deadline": (now + timedelta(seconds=3)).isoformat(),
        "bot_user_ref": "99@apps.example",
    }
    snapshot: dict[str, object] = {
        "version": 1,
        "locale": "en-US",
        "app_permissions": str(
            int(
                Permission.ATTACH_FILES
                | Permission.EMBED_LINKS
                | Permission.MENTION_EVERYONE
                | (Permission.USE_EXTERNAL_EMOJIS if is_capability else Permission(0))
            )
        ),
        "authorizing_integration_owners": (
            {"guild_install": "0"} if is_capability else {"user_install": "20@installer.example"}
        ),
        "attachment_size_limit": 15 * 1024 * 1024,
        "entitlements": [],
        "user": event["user"],
    }
    interaction.payload[INTERACTION_EVENT_SNAPSHOT_KEY] = snapshot
    event.update(snapshot)
    settings = SimpleNamespace(domain="chat.example", secret_key_bytes=b"k" * 32)
    application = SimpleNamespace(
        id=1,
        origin_domain="apps.example",
        status="active",
        bot_user_id=99,
        bot_user_domain="apps.example",
    )
    command = SimpleNamespace(
        id=55,
        state="active",
        application_id=1,
        application_domain="apps.example",
        name="weather",
        type="chat_input",
        definition=definition,
    )
    if is_capability:
        installation = BotDMCapability(
            id=88,
            status="active",
            revoked_at=None,
            expires_at=interaction.expires_at,
            authority_domain="chat.example",
            revision=4,
            application_id=1,
            application_domain="apps.example",
            bot_user_id=99,
            bot_user_domain="apps.example",
            target_user_id=10,
            target_user_domain="chat.example",
            conversation_id=5,
            conversation_domain="chat.example",
            grant_id="kbdg_" + "g" * 43,
            source_installation_id=66,
            source_installation_domain="guild.example",
            source_kind="guild",
            guild_id=66,
            guild_domain="guild.example",
            installing_user_id=None,
            installing_user_domain=None,
            granted_scopes=["applications.commands", "interactions.respond"],
        )
    else:
        installation = BotUserInstallation(
            id=77,
            status="active",
            revoked_at=None,
            grant_revision=4,
            application_id=1,
            application_domain="apps.example",
            # The installer and the user who clicks a public component need not
            # be the same person.
            user_id=20,
            user_domain="installer.example",
            authority_expires_at=interaction.expires_at + timedelta(minutes=5),
            granted_scopes=["applications.commands", "interactions.respond"],
        )
    interaction.payload[INTERACTION_INSTALLATION_LINEAGE_KEY] = installation_authority_lineage(
        installation
    )
    return settings, interaction, event, application, command, installation


def _queue_fixture(
    *, integration_type: str = "user_install"
) -> tuple[
    SimpleNamespace,
    BotInteraction,
    dict[str, object],
    object,
    object,
    object,
    object,
]:
    settings, interaction, event, application, command, installation = _interaction_fixture(
        integration_type=integration_type
    )
    session = _QueueSession()
    row = interaction_dispatch.queue_interaction_create_dispatch(
        session,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        interaction,
        topic="user:apps.example:99",
        audience_user_ref="99@apps.example",
        event=event,
    )
    row.attempts = 0
    row.next_attempt_at = interaction.created_at
    row.dispatched_at = None
    assert session.added == [row]
    return settings, interaction, event, application, command, installation, row


def _guild_queue_fixture() -> tuple[
    SimpleNamespace,
    BotInteraction,
    dict[str, object],
    object,
    object,
    BotInstallation,
    object,
]:
    settings, interaction, event, application, command, _ = _interaction_fixture()
    interaction.installation_id = 77
    interaction.user_installation_id = None
    interaction.guild_id = 70
    interaction.guild_domain = "guild.example"
    interaction.channel_domain = "guild.example"
    interaction.context = "guild"
    interaction.integration_type = "guild_install"
    installation = BotInstallation(
        id=77,
        application_id=1,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="guild.example",
        bot_user_id=99,
        bot_user_domain="apps.example",
        installer_id=10,
        installer_domain="chat.example",
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=4,
        status="active",
        revoked_at=None,
    )
    snapshot = dict(interaction.payload[INTERACTION_EVENT_SNAPSHOT_KEY])
    user = snapshot.pop("user")
    snapshot.update(
        {
            "authorizing_integration_owners": {"guild_install": "70@guild.example"},
            "guild_locale": "en-US",
            "member": {
                "guild_id": "70",
                "guild_domain": "guild.example",
                "permissions": "0",
                "user": user,
            },
        }
    )
    interaction.payload[INTERACTION_EVENT_SNAPSHOT_KEY] = snapshot
    interaction.payload[INTERACTION_INSTALLATION_LINEAGE_KEY] = installation_authority_lineage(
        installation
    )
    event.pop("user")
    event.update(
        {
            **snapshot,
            "interaction_ref": f"{interaction.id}@guild.example",
            "context": "guild",
            "integration_type": "guild_install",
            "installation_id": "77",
            "user_installation_id": None,
            "guild_ref": "70@guild.example",
            "channel_ref": "5@guild.example",
            "channel_domain": "guild.example",
        }
    )
    session = _QueueSession()
    row = interaction_dispatch.queue_interaction_create_dispatch(
        session,  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        interaction,
        topic="guild:guild.example:70",
        audience_user_ref="99@apps.example",
        event=event,
    )
    assert session.added == [row]
    return settings, interaction, event, application, command, installation, row


def test_sealed_create_authenticates_topic_and_exact_payload() -> None:
    settings, interaction, event, _, _, _, row = _queue_fixture()
    token = str(event["token"])

    assert token.encode() not in row.event_ciphertext
    assert interaction.dispatch_fingerprint == row.event_fingerprint
    assert (
        interaction_dispatch.unseal_interaction_create_event(
            settings,  # type: ignore[arg-type]
            interaction,
            row,  # type: ignore[arg-type]
        )
        == event
    )

    row.topic = "user:evil.example:99"
    with pytest.raises(interaction_dispatch.InteractionCreateDispatchError):
        interaction_dispatch.unseal_interaction_create_event(
            settings,  # type: ignore[arg-type]
            interaction,
            row,  # type: ignore[arg-type]
        )

    substituted = dict(event)
    substituted["bot_user_ref"] = "100@apps.example"
    with pytest.raises(interaction_dispatch.InteractionCreateDispatchError):
        interaction_dispatch.seal_interaction_create_event(
            settings,  # type: ignore[arg-type]
            interaction,
            "user:apps.example:99",
            "99@apps.example",
            substituted,
        )

    for field, replacement in (
        ("app_permissions", "0"),
        ("attachment_size_limit", 1),
        ("authorizing_integration_owners", {"user_install": "21@installer.example"}),
        ("locale", "de"),
        ("version", 2),
    ):
        tampered = dict(event)
        tampered[field] = replacement
        with pytest.raises(interaction_dispatch.InteractionCreateDispatchError):
            interaction_dispatch.seal_interaction_create_event(
                settings,  # type: ignore[arg-type]
                interaction,
                "user:apps.example:99",
                "99@apps.example",
                tampered,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("integration_type", ["user_install", "dm_capability"])
async def test_durable_create_binding_uses_admission_snapshot_across_config_revisions(
    integration_type: str,
) -> None:
    _, interaction, event, application, command, installation, row = _queue_fixture(
        integration_type=integration_type
    )
    session = _LookupSession(
        interaction=interaction,
        application=application,
        command=command,
        installation=installation,
    )

    assert await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="chat.example",
    )
    if integration_type == "user_install":
        assert installation.user_id != interaction.user_id
    if integration_type == "dm_capability":
        installation.revision += 1
    else:
        installation.grant_revision += 1
    command.state = "deleted"
    command.definition = {"name": "weather", "description": "edited after admission"}
    assert await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="chat.example",
    )

    installation.status = "revoked"
    installation.revoked_at = datetime.now(UTC)
    assert not await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="chat.example",
    )


@pytest.mark.asyncio
async def test_guild_create_delivery_rechecks_current_channel_restriction() -> None:
    _, interaction, event, application, command, installation, row = _guild_queue_fixture()
    session = _LookupSession(
        interaction=interaction,
        application=application,
        command=command,
        installation=installation,
    )
    principal = SimpleNamespace(
        application=application,
        user=SimpleNamespace(id=99, origin_domain="apps.example"),
        dm_capability_grant_id=None,
    )

    assert await bot_gateway.current_interaction_create_access(
        _SessionFactory(session),
        principal,  # type: ignore[arg-type]
        {"t": "INTERACTION_CREATE", "d": event},
        authority_domain="guild.example",
    )
    assert await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="guild.example",
    )

    installation.channel_restrictions = ["6@guild.example"]

    assert not await bot_gateway.current_interaction_create_access(
        _SessionFactory(session),
        principal,  # type: ignore[arg-type]
        {"t": "INTERACTION_CREATE", "d": event},
        authority_domain="guild.example",
    )
    assert not await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="guild.example",
    )


@pytest.mark.asyncio
async def test_guild_create_gateway_and_durable_delivery_allow_category_thread() -> None:
    _, interaction, event, application, command, installation, row = _guild_queue_fixture()
    session = _LookupSession(
        interaction=interaction,
        application=application,
        command=command,
        installation=installation,
    )
    session.channel.type = 11
    session.channel.parent_id = 6
    session.channel.parent_domain = "guild.example"
    forum = SimpleNamespace(
        id=6,
        origin_domain="guild.example",
        guild_id=70,
        guild_domain="guild.example",
        parent_id=8,
        parent_domain="guild.example",
        unavailable=False,
        type=15,
    )
    category = SimpleNamespace(
        id=8,
        origin_domain="guild.example",
        guild_id=70,
        guild_domain="guild.example",
        parent_id=None,
        parent_domain=None,
        unavailable=False,
        type=4,
    )
    session.channels.update(
        {
            (forum.id, forum.origin_domain): forum,
            (category.id, category.origin_domain): category,
        }
    )
    installation.channel_restrictions = ["8@guild.example"]
    principal = SimpleNamespace(
        application=application,
        user=SimpleNamespace(id=99, origin_domain="apps.example"),
        dm_capability_grant_id=None,
    )

    assert await bot_gateway.current_interaction_create_access(
        _SessionFactory(session),
        principal,  # type: ignore[arg-type]
        {"t": "INTERACTION_CREATE", "d": event},
        authority_domain="guild.example",
    )
    assert await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="guild.example",
    )

    del session.channels[(forum.id, forum.origin_domain)]

    assert not await bot_gateway.current_interaction_create_access(
        _SessionFactory(session),
        principal,  # type: ignore[arg-type]
        {"t": "INTERACTION_CREATE", "d": event},
        authority_domain="guild.example",
    )
    assert not await interaction_dispatch.durable_interaction_create_binding_matches(
        session,  # type: ignore[arg-type]
        interaction,
        row,  # type: ignore[arg-type]
        event,
        authority_domain="guild.example",
    )


@pytest.mark.asyncio
async def test_publish_failure_survives_and_sql_poll_delivers_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, interaction, event, application, command, installation, row = _queue_fixture()
    session = _LookupSession(
        interaction=interaction,
        application=application,
        command=command,
        installation=installation,
        rows=[(row, interaction)],
    )
    publish = AsyncMock(return_value=False)
    monkeypatch.setattr(interaction_dispatch, "publish_ephemeral_once", publish)

    delivered = await interaction_dispatch.drain_interaction_create_dispatch_outbox(
        session,  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        interaction_id=interaction.id,
    )

    assert delivered == 0
    assert row.dispatched_at is None
    assert row.attempts == 1
    assert session.deleted == []
    session.commit.assert_awaited_once()

    disclose = AsyncMock(return_value=True)
    monkeypatch.setattr(bot_gateway, "get_settings", lambda: settings)
    monkeypatch.setattr(
        bot_gateway,
        "filtered_event",
        lambda *_args, **_kwargs: {"op": 0, "t": "INTERACTION_CREATE", "d": event},
    )
    monkeypatch.setattr(bot_gateway, "disclose_current_event", disclose)
    runtime = SimpleNamespace(
        topic_grants={
            row.topic: (
                {"direct_messages"},
                {"interactions.respond"},
                None,
                frozenset({77}),
                None,
                (),
            )
        },
        interaction_create_ids=set(),
        sessionmaker=_SessionFactory(session),
        principal=SimpleNamespace(),
        websocket=SimpleNamespace(),
        authorization_guard=SimpleNamespace(),
        encrypted_by_topic={},
    )

    assert bot_gateway.INTERACTION_SQL_POLL_SECONDS < 3
    await bot_gateway.replay_pending_interaction_creates(runtime)
    await bot_gateway.replay_pending_interaction_creates(runtime)

    disclose.assert_awaited_once()
    raw = disclose.await_args.args[4]
    assert raw["d"]["token"] == event["token"]
    assert runtime.interaction_create_ids == {interaction.id}


class _CapturingRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.streams: dict[str, list[str]] = {}
        self.call: tuple[object, ...] | None = None

    async def eval(self, *args: object) -> int:
        self.call = args
        marker_key = str(args[3])
        self.values[marker_key] = "1"
        return 1


@pytest.mark.asyncio
async def test_ephemeral_create_retains_only_token_free_marker() -> None:
    redis = _CapturingRedis()
    token = secrets.token_urlsafe(32)

    published = await publish_ephemeral_once(
        redis,  # type: ignore[arg-type]
        "user:apps.example:99",
        "INTERACTION_CREATE",
        {"id": "101", "token": token, "bot_user_ref": "99@apps.example"},
        idempotency_key="interaction-create:101:" + "f" * 64,
        ttl_seconds=960,
        audience_user_refs=("99@apps.example",),
    )

    assert published
    assert redis.call is not None
    script, key_count, channel_key, marker_key, encoded, ttl = redis.call
    assert script == PUBLISH_EPHEMERAL_ONCE_SCRIPT
    assert key_count == 2
    assert channel_key == "dispatch:user:apps.example:99"
    assert str(marker_key).startswith("dispatch:once:user:apps.example:99:")
    assert "dispatch:stream:" not in str(redis.call)
    assert "dispatch:seq:" not in str(redis.call)
    assert token in str(encoded)
    assert ttl == "960"
    assert redis.streams == {}
    assert token not in json.dumps(redis.values)
