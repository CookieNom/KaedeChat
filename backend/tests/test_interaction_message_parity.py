from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

import app.api.federation as federation_api
import app.api.interactions as interactions
from app.api.admin_portal import UserStatePatch, patch_user_state
from app.auth.schemas import SettingsPatch
from app.chat.allowed_mentions import allowed_mention_texts, everyone_mention_recipients
from app.chat.channel_access import ChannelAccess
from app.chat.guild_revision import federation_channel_state
from app.chat.interaction_metadata import validate_interaction_metadata
from app.chat.mention_policy import AllowedMentions, regular_message_allowed_mentions
from app.chat.schemas import ChannelCreate, MessageCreate, MessageEdit
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.base import Base
from app.db.bot_models import InstanceAuditEvent
from app.db.models import Channel, Guild, Message, User, UserSettings
from app.federation.guilds import _validated_channel_extension_state
from app.federation.message_content import validate_replicated_rich_projection
from app.federation.schemas import GuildProxyRequest, RemoteUserProfile


@pytest.mark.asyncio
async def test_public_command_metadata_is_authority_derived_and_federation_validated() -> None:
    invoker = SimpleNamespace(
        id=4,
        origin_domain="users.example",
        username="human",
        display_name="Human",
        avatar_hash=None,
        account_type="human",
    )
    session = SimpleNamespace(get=AsyncMock(return_value=invoker))
    interaction = SimpleNamespace(
        id=29,
        channel_domain="guild.example",
        application_id=5,
        application_domain="apps.example",
        integration_type="guild_install",
        guild_id=2,
        guild_domain="guild.example",
        user_id=4,
        user_domain="users.example",
        interaction_type="command",
        command_name="ship",
        command_type="chat_input",
        payload={
            "_interaction_event_snapshot": {
                "authorizing_integration_owners": {
                    "guild_install": "2@guild.example",
                    "user_install": "4@users.example",
                }
            }
        },
    )

    metadata = await interactions.interaction_message_metadata(
        cast(Any, session),
        cast(Any, interaction),
        followup=False,
    )

    assert metadata["interaction_ref"] == "29@guild.example"
    assert metadata["command_name"] == "ship"
    assert metadata["authorizing_integration_owners"] == {
        "guild_install": "2@guild.example",
        "user_install": "4@users.example",
    }
    projection = validate_replicated_rich_projection(
        {
            "attachments": [],
            "author_id": "6",
            "author_domain": "apps.example",
            "application_id": "5",
            "application_domain": "apps.example",
            "interaction_metadata": metadata,
        },
        message_id=30,
        message_origin="guild.example",
        message_created_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
        e2ee=None,
        message_type=20,
        label="guild message",
    )
    assert projection.interaction_metadata == metadata

    tampered = {**metadata, "application_ref": "7@apps.example"}
    with pytest.raises(ValueError, match="interaction metadata"):
        validate_replicated_rich_projection(
            {
                "attachments": [],
                "application_id": "5",
                "application_domain": "apps.example",
                "interaction_metadata": tampered,
            },
            message_id=30,
            message_origin="guild.example",
            message_created_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
            e2ee=None,
            message_type=20,
            label="guild message",
        )


def test_context_command_metadata_requires_type_23_and_target_reference() -> None:
    metadata = {
        "id": "29",
        "origin_domain": "guild.example",
        "interaction_ref": "29@guild.example",
        "type": "command",
        "user": {
            "id": "4",
            "origin_domain": "users.example",
            "username": "human",
            "display_name": None,
            "avatar_hash": None,
            "bot": False,
        },
        "user_ref": "4@users.example",
        "application_ref": "5@apps.example",
        "integration_type": "guild_install",
        "authorizing_integration_owners": {"guild_install": "2@guild.example"},
        "command_name": "Inspect Message",
        "command_type": "message",
        "target_message_id": "20",
        "target_message_domain": "guild.example",
        "target_message_ref": "20@guild.example",
    }

    assert (
        validate_interaction_metadata(
            metadata,
            message_type=23,
            application_ref=(5, "apps.example"),
            referenced_message_ref=(20, "guild.example"),
            message_ref=(30, "guild.example"),
        )
        == metadata
    )
    with pytest.raises(ValueError, match="message type"):
        validate_interaction_metadata(
            metadata,
            message_type=20,
            application_ref=(5, "apps.example"),
            referenced_message_ref=(20, "guild.example"),
            message_ref=(30, "guild.example"),
        )


@pytest.mark.asyncio
async def test_federated_interaction_proxy_replay_binds_type_and_metadata() -> None:
    metadata = {
        "id": "29",
        "origin_domain": "guild.example",
        "interaction_ref": "29@guild.example",
        "type": "command",
        "user": {
            "id": "4",
            "origin_domain": "users.example",
            "username": "human",
            "display_name": None,
            "avatar_hash": None,
            "bot": False,
        },
        "user_ref": "4@users.example",
        "application_ref": "5@apps.example",
        "integration_type": "guild_install",
        "authorizing_integration_owners": {"guild_install": "2@guild.example"},
        "command_name": "ship",
        "command_type": "chat_input",
    }
    payload = GuildProxyRequest(
        operation="message.create",
        actor=RemoteUserProfile(
            id="6", origin_domain="apps.example", account_type="bot", username="shipbot"
        ),
        channel_id="20",
        content="done",
        application_id="5@apps.example",
        interaction_message_type=20,
        interaction_metadata=metadata,
        client_nonce="interaction-response",
    )
    message = Message(
        id=30,
        origin_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
        author_id=6,
        author_domain="apps.example",
        application_id=5,
        application_domain="apps.example",
        content="done",
        embeds=[],
        components=[],
        sticker_items=[],
        message_type=20,
        interaction_metadata=metadata,
        mention_user_refs=[],
        client_nonce="interaction-response",
        created_at=datetime(2026, 8, 28, tzinfo=UTC),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=None), scalars=AsyncMock(return_value=[]))
    assert await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        payload,
        application_ref=(5, "apps.example"),
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )
    changed = payload.model_copy(update={"interaction_message_type": 23})
    assert not await federation_api.proxy_message_matches_request(
        cast(Any, session),
        message,
        changed,
        application_ref=(5, "apps.example"),
        forwarded_message=None,
        mentions=federation_api.ProxyMentionProjection((), (), (), frozenset(), False),
    )


def test_interaction_message_policy_is_typed_and_write_only_when_ephemeral() -> None:
    message = interactions.InteractionMessageCreate(
        content="hello <@2@chat.example>",
        allowed_mentions=AllowedMentions(users=["2@chat.example"]),
    )
    response = interactions.InteractionResponse(message=MessageCreate(content="legacy helper"))
    rendered = interactions.ephemeral_message_payload(
        message,
        flags=0,
        interaction_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        now=datetime.now(UTC),
    )

    assert isinstance(response.message, interactions.InteractionMessageCreate)
    assert "allowed_mentions" not in rendered
    with pytest.raises(ValidationError):
        AllowedMentions(parse=["users"], users=["2@chat.example"])
    with pytest.raises(ValidationError):
        AllowedMentions.model_validate({"replied_user": 1})


def test_ordinary_message_policy_survives_create_edit_and_federation_proxy() -> None:
    policy = AllowedMentions(
        users=["2@users.example"],
        replied_user=True,
    )
    created = MessageCreate(
        content="hello <@2@users.example>",
        allowed_mentions=policy,
    )
    edited = MessageEdit(allowed_mentions=policy)
    proxied = GuildProxyRequest(
        operation="message.create",
        actor=RemoteUserProfile(
            id="6",
            origin_domain="apps.example",
            account_type="bot",
            username="shipbot",
        ),
        channel_id="20",
        content=created.content,
        allowed_mentions=policy,
        application_id="5@apps.example",
        client_nonce="mention-policy",
    )

    assert created.allowed_mentions == policy
    assert edited.model_fields_set == {"allowed_mentions"}
    assert proxied.model_dump(mode="json")["allowed_mentions"] == {
        "parse": [],
        "users": ["2@users.example"],
        "roles": [],
        "replied_user": True,
    }
    assert regular_message_allowed_mentions(None).parse == [
        "everyone",
        "users",
        "roles",
    ]


def test_allowed_mentions_read_visible_text_from_stored_component_json() -> None:
    assert allowed_mention_texts(
        None,
        [
            {
                "type": 17,
                "components": [{"type": 10, "content": "hello <@2@users.example>"}],
            }
        ],
    ) == ["hello <@2@users.example>"]


@pytest.mark.asyncio
async def test_disallowed_everyone_mention_renders_without_notifying() -> None:
    session = SimpleNamespace(execute=AsyncMock())
    access = ChannelAccess(
        channel=cast(Any, SimpleNamespace()),
        guild=cast(Any, SimpleNamespace()),
        participants=[],
    )

    assert (
        await everyone_mention_recipients(
            cast(Any, session),
            access,
            Permission(0),
        )
        == set()
    )
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_edit_without_visible_mentions_clears_projection_without_access_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = AsyncMock()
    monkeypatch.setattr(interactions, "interaction_response_channel_access", access)
    message = interactions.InteractionMessageCreate(content="done")

    created_refs = await interactions.resolve_interaction_message_mentions(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        message,
    )
    edited_refs = await interactions.resolve_interaction_edit_mentions(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        interactions.InteractionResponseEdit(content="still done"),
        cast(Any, SimpleNamespace(content="old", e2ee=None)),
    )

    assert created_refs == interactions.ResolvedMentions((), (), False)
    assert edited_refs == interactions.ResolvedMentions((), (), False)
    access.assert_not_awaited()


@pytest.mark.asyncio
async def test_interaction_locale_and_age_policy_come_only_from_user_home() -> None:
    settings = SimpleNamespace(domain="home.example")
    actor = SimpleNamespace(
        id=2,
        origin_domain="home.example",
        is_local=True,
        age_assurance_state="adult",
    )
    session = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                locale="fr",
                age_restricted_dm_commands_enabled=True,
            )
        )
    )

    local = await interactions.authoritative_interaction_invoker_policy(
        cast(Any, session),
        cast(Any, settings),
        cast(Any, actor),
        interactions.InteractionInvocationOptions(
            federated_locale="de",
            federated_age_assured_adult=False,
            federated_age_restricted_dm_commands_enabled=False,
        ),
    )
    remote = await interactions.authoritative_interaction_invoker_policy(
        cast(Any, SimpleNamespace()),
        cast(Any, settings),
        cast(
            Any,
            SimpleNamespace(
                id=3,
                origin_domain="member.example",
                is_local=False,
            ),
        ),
        interactions.InteractionInvocationOptions(
            federated_locale="de",
            federated_age_assured_adult=True,
            federated_age_restricted_dm_commands_enabled=True,
        ),
    )

    assert (local.locale, local.age_assured_adult, local.age_restricted_dm_commands_enabled) == (
        "fr",
        True,
        True,
    )
    assert (remote.locale, remote.age_assured_adult) == ("de", True)
    session.get.assert_awaited_once_with(UserSettings, (2, "home.example"))


def test_age_restricted_command_requires_both_home_and_channel_authorities() -> None:
    command = SimpleNamespace(definition={"nsfw": True})
    guild = Guild(id=1, origin_domain="guild.example", name="Guild", owner_id=2, owner_domain="x")
    channel = Channel(
        id=3,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        type=0,
        nsfw=False,
        created_floor_id=3,
    )
    access = ChannelAccess(channel=channel, guild=guild, participants=[])

    with pytest.raises(HTTPException) as minor:
        interactions.require_age_restricted_command(
            cast(Any, command),
            access,
            interactions.InteractionInvokerPolicy("en-US", False, False),
        )
    assert cast(dict[str, object], minor.value.detail)["code"] == (
        "APPLICATION_COMMAND_AGE_RESTRICTED"
    )
    with pytest.raises(HTTPException) as channel_denied:
        interactions.require_age_restricted_command(
            cast(Any, command),
            access,
            interactions.InteractionInvokerPolicy("en-US", True, False),
        )
    assert cast(dict[str, object], channel_denied.value.detail)["code"] == (
        "AGE_RESTRICTED_CHANNEL_REQUIRED"
    )
    channel.nsfw = True
    interactions.require_age_restricted_command(
        cast(Any, command),
        access,
        interactions.InteractionInvokerPolicy("en-US", True, False),
    )


@pytest.mark.asyncio
async def test_age_restricted_threads_inherit_parent_channel_state() -> None:
    parent = SimpleNamespace(
        id=3,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        nsfw=True,
        unavailable=False,
    )
    thread = SimpleNamespace(
        id=4,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        parent_id=3,
        parent_domain="guild.example",
        type=11,
        nsfw=False,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=parent))

    assert await interactions.effective_channel_nsfw(
        cast(Any, session),
        cast(Any, thread),
    )
    session.get.assert_awaited_once_with(Channel, (3, "guild.example"))


def test_command_discovery_applies_age_and_user_context_send_requirements() -> None:
    commands: list[dict[str, object]] = [
        {"name": "plain", "type": "chat_input"},
        {"name": "adult", "type": "chat_input", "nsfw": True},
        {"name": "inspect", "type": "user"},
    ]
    base = int(Permission.VIEW_CHANNEL | Permission.USE_APPLICATION_COMMANDS)

    assert [
        item["name"]
        for item in interactions.filter_commands_for_permissions(
            commands,
            base,
            channel_type=0,
            channel_nsfw=True,
            age_assured_adult=False,
        )
    ] == ["plain"]
    allowed = interactions.filter_commands_for_permissions(
        commands,
        base | int(Permission.SEND_MESSAGES),
        channel_type=0,
        channel_nsfw=True,
        age_assured_adult=True,
    )
    assert [item["name"] for item in allowed] == ["plain", "adult", "inspect"]
    assert (
        interactions.filter_commands_for_permissions(
            [{"name": "inspect", "type": "user"}],
            base | int(Permission.SEND_MESSAGES),
            channel_type=11,
        )
        == []
    )
    assert (
        interactions.filter_commands_for_permissions(
            [{"name": "inspect", "type": "user"}],
            base | int(Permission.SEND_MESSAGES),
        )
        == []
    )


def test_interaction_names_locales_and_autocomplete_numbers_match_discord_contract() -> None:
    command = interactions.InteractionCreate(
        application_ref="1@apps.example",
        command_name="météo",
    )
    context = interactions.InteractionCreate(
        application_ref="1@apps.example",
        command_name="Inspect User",
        command_type="user",
        target_ref="2@apps.example",
    )
    autocomplete = interactions.InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="autocomplete",
        command_name="météo",
        focused_option="prévisions.ville",
    )

    assert command.command_name == "météo"
    assert context.command_name == "Inspect User"
    assert autocomplete.focused_option == "prévisions.ville"
    assert (
        interactions.validated_autocomplete_choices([{"name": "minimum", "value": -(2**53) + 1}])[
            0
        ]["value"]
        == -(2**53) + 1
    )
    for invalid in (float("nan"), float("inf"), 2**53):
        with pytest.raises(HTTPException) as rejected:
            interactions.validated_autocomplete_choices([{"name": "bad", "value": invalid}])
        assert cast(dict[str, object], rejected.value.detail)["code"] == (
            "AUTOCOMPLETE_CHOICES_INVALID"
        )

    federated = interactions.FederatedInteractionCreate(
        user_id="2",
        interaction=command,
        response_grant_id="igr_" + "g" * 40,
        response_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        locale="pt-BR",
        age_assured_adult=True,
        age_restricted_dm_commands_enabled=True,
    )
    assert federated.locale == "pt-BR"
    with pytest.raises(ValidationError):
        interactions.FederatedInteractionCreate.model_validate(
            federated.model_dump() | {"locale": "../../bad"}
        )


@pytest.mark.asyncio
async def test_remote_command_page_accepts_130_and_rejects_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = SimpleNamespace(status_code=200)
    request = AsyncMock(return_value=upstream)
    page: list[dict[str, object]] = [{"name": f"command-{index}"} for index in range(130)]
    monkeypatch.setattr(interactions, "signed_request", request)
    monkeypatch.setattr(interactions, "decode_federation_response_json", lambda _value: page)

    result = await interactions._remote_guild_application_commands(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(id=1, origin_domain="guild.example")),
        cast(
            Any,
            SimpleNamespace(id=2, age_assurance_state="adult"),
        ),
        channel=cast(Any, SimpleNamespace(id=3)),
    )
    assert len(result) == 130
    request_call = request.await_args
    assert request_call is not None
    assert request_call.kwargs["query"] == {
        "user_id": "2",
        "age_assured_adult": "true",
        "channel_id": "3",
    }

    page.append({"name": "too-many"})
    with pytest.raises(HTTPException) as rejected:
        await interactions._remote_guild_application_commands(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(id=1, origin_domain="guild.example")),
            cast(Any, SimpleNamespace(id=2, age_assurance_state="adult")),
        )
    assert rejected.value.status_code == 502


def test_age_assurance_storage_is_authoritative_and_migration_is_reversible() -> None:
    users = Base.metadata.tables["users"]
    channels = Base.metadata.tables["channels"]
    user_settings = Base.metadata.tables["user_settings"]
    user_checks = {
        constraint.name
        for constraint in users.constraints
        if isinstance(constraint, CheckConstraint)
    }

    assert cast(Any, users.c.age_assurance_state.default).arg == "unknown"
    assert {
        "ck_users_age_assurance_state_value",
        "ck_users_age_assurance_local_human_only",
    } <= user_checks
    assert channels.c.nsfw.nullable is False
    assert user_settings.c.age_restricted_dm_commands_enabled.nullable is False
    assert UserStatePatch(age_assurance_state="adult", reason="verified")
    assert SettingsPatch(age_restricted_dm_commands_enabled=True)
    with pytest.raises(ValidationError):
        UserStatePatch()
    with pytest.raises(ValidationError):
        UserStatePatch.model_validate({"disabled": 1})

    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "fc9a4b7d2e10_bot_parity_foundation.py"
    ).read_text()
    authority_check = 'op.f("ck_users_age_assurance_local_human_only")'
    age_column = 'op.drop_column("users", "age_assurance_state")'
    assert migration.rindex(authority_check) < migration.index(age_column)


def test_nsfw_channel_state_round_trips_through_federation_validation() -> None:
    request = ChannelCreate(name="adult-chat", type=0, nsfw=True)
    channel = Channel(
        id=3,
        origin_domain="guild.example",
        guild_id=1,
        guild_domain="guild.example",
        type=0,
        name=request.name,
        nsfw=request.nsfw,
        created_floor_id=3,
    )

    assert federation_channel_state(channel)["nsfw"] is True
    assert (
        _validated_channel_extension_state(
            {"flags": "0", "nsfw": True},
            0,
            "guild.example",
        )["nsfw"]
        is True
    )
    with pytest.raises(ValueError, match="NSFW"):
        _validated_channel_extension_state(
            {"flags": "0", "nsfw": "true"},
            0,
            "guild.example",
        )


@pytest.mark.asyncio
async def test_only_administration_can_change_age_assurance_with_audit_metadata() -> None:
    user = User(
        id=2,
        origin_domain="chat.example",
        is_local=True,
        account_type="human",
        username="member",
        password_hash="hash",
        age_assurance_state="unknown",
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=user),
        add=Mock(),
        commit=AsyncMock(),
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=1, origin_domain="chat.example"),
        require=Mock(),
    )

    rendered = await patch_user_state(
        EntityRef("2@chat.example"),
        UserStatePatch(age_assurance_state="adult", reason="vendor check 42"),
        cast(Any, principal),
        cast(Any, session),
        cast(Any, SimpleNamespace(mint=AsyncMock(return_value=90))),
        cast(Any, SimpleNamespace(domain="chat.example")),
    )

    principal.require.assert_called_once_with("users.manage")
    assert user.age_assurance_state == "adult"
    assert rendered["age_assurance_state"] == "adult"
    audit_event = session.add.call_args.args[0]
    assert isinstance(audit_event, InstanceAuditEvent)
    assert audit_event.action == "admin.user.age_assurance.update"
    assert audit_event.detail == {
        "old_state": "unknown",
        "new_state": "adult",
        "reason": "vendor check 42",
    }
