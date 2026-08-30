from __future__ import annotations

import asyncio
import base64
import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.responses import Response

import app.api.channels as channels
import app.api.federation as federation
import app.api.interactions as interactions
import app.bots.interaction_authority as authority
from app.bots.interaction_authority import (
    resolve_component_entities,
    validate_component_submission,
    validate_modal_submission,
)
from app.bots.interaction_events import authority_attested_interaction_response
from app.bots.interaction_owners import (
    INTERACTION_INSTALLATION_LINEAGE_KEY,
    installation_authority_lineage,
)
from app.chat.channel_access import ChannelAccess
from app.chat.rich_content import (
    ActionRow,
    Button,
    ChannelSelect,
    MentionableSelect,
    Modal,
    SelectOption,
    StringSelect,
)
from app.chat.schemas import MessageEdit
from app.core.federation import canonical_json
from app.core.json_limits import strict_json_loads
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import BotDMCapability, BotInstallation, BotUserInstallation
from app.db.models import Channel, Guild, MessageView, Role, User
from app.federation.schemas import GuildProxyRequest


def signed_private_response_projection() -> dict[str, object]:
    return {
        "application_ref": "5@apps.example",
        "authority_domain": "target.example",
        "autocomplete_generation": None,
        "callback_type": 4,
        "channel_ref": "20@target.example",
        "data": {"content": "private"},
        "deleted_at": None,
        "ephemeral": True,
        "expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "interaction_id": "30",
        "interaction_ref": "30@target.example",
        "invoker_ref": "10@home.example",
        "message_ref": None,
        "operation": "CREATE",
        "response_grant_id": base64.urlsafe_b64encode(b"g" * 32).decode().rstrip("="),
        "response_id": "40",
        "response_ref": "40@target.example",
        "revision": "1",
        "sequence": 0,
        "user_ref": "10@home.example",
    }


def attest_private_response(content: object) -> tuple[int, int, int, str] | None:
    return authority_attested_interaction_response(
        "bot.interaction.response",
        content,
        expected_authority="target.example",
        actor=("10", "home.example"),
    )


def test_signed_private_response_projection_requires_exact_canonical_shape() -> None:
    payload = signed_private_response_projection()

    assert attest_private_response(payload) == (30, 40, 1, "CREATE")

    malformed: list[dict[str, object]] = []
    for key, value in (
        ("interaction_id", "030"),
        ("interaction_ref", "-30@target.example"),
        ("response_id", "0"),
        ("response_ref", "40@TARGET.example"),
        ("callback_type", True),
        ("callback_type", 5),
        ("ephemeral", 1),
        ("sequence", -1),
        ("revision", "01"),
        ("response_grant_id", "not-a-grant"),
    ):
        candidate = copy.deepcopy(payload)
        candidate[key] = value
        malformed.append(candidate)
    unknown = copy.deepcopy(payload)
    unknown["untrusted"] = "ignored-by-old-clients"
    malformed.append(unknown)

    assert all(attest_private_response(candidate) is None for candidate in malformed)


def test_signed_private_response_projection_enforces_operation_tombstone_semantics() -> None:
    payload = signed_private_response_projection()

    update = copy.deepcopy(payload)
    update.update(operation="UPDATE", revision="2")
    assert attest_private_response(update) == (30, 40, 2, "UPDATE")

    tombstone = copy.deepcopy(payload)
    tombstone.update(
        operation="DELETE",
        revision="2",
        data={},
        deleted_at=datetime.now(UTC).isoformat(),
    )
    assert attest_private_response(tombstone) == (30, 40, 2, "DELETE")

    invalid_delete = copy.deepcopy(tombstone)
    invalid_delete["data"] = {"content": "must not survive deletion"}
    assert attest_private_response(invalid_delete) is None

    invalid_update = copy.deepcopy(update)
    invalid_update["deleted_at"] = datetime.now(UTC).isoformat()
    assert attest_private_response(invalid_update) is None

    replayed_create = copy.deepcopy(payload)
    replayed_create["revision"] = "2"
    assert attest_private_response(replayed_create) is None


def rows(*components: object) -> list[dict[str, object]]:
    return [
        ActionRow.model_validate({"components": [component]}).model_dump(
            mode="json", exclude_none=True
        )
        for component in components
    ]


def public_installation() -> BotInstallation:
    return BotInstallation(
        id=50,
        application_id=1,
        application_domain="apps.example",
        guild_id=10,
        guild_domain="chat.example",
        bot_user_id=2,
        bot_user_domain="apps.example",
        installer_id=3,
        installer_domain="chat.example",
        grant_revision=3,
        status="active",
    )


def public_principal() -> SimpleNamespace:
    return SimpleNamespace(
        application=SimpleNamespace(id=1, origin_domain="apps.example"),
        user=SimpleNamespace(id=2, origin_domain="apps.example"),
        worker=SimpleNamespace(id=3),
    )


@pytest.mark.asyncio
async def test_private_response_rich_emoji_uses_interaction_channel_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    channel = SimpleNamespace(
        id=20,
        origin_domain="guild.example",
        guild_id=10,
        guild_domain="guild.example",
    )
    session = SimpleNamespace(get=AsyncMock(side_effect=[guild, channel]))
    redis = object()
    principal = SimpleNamespace(
        application=SimpleNamespace(id=1, origin_domain="apps.example"),
        user=SimpleNamespace(
            id=2,
            origin_domain="apps.example",
            account_type="bot",
        ),
    )
    interaction = SimpleNamespace(
        guild_id=10,
        guild_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
    )
    components = [
        ActionRow.model_validate(
            {
                "components": [
                    {
                        "type": 2,
                        "custom_id": "wave",
                        "label": "Wave",
                        "emoji": {"id": "30@emoji.example", "name": "wave"},
                    }
                ]
            }
        )
    ]
    permission_lookup = AsyncMock(return_value=int(Permission.USE_EXTERNAL_EMOJIS))
    resolver = AsyncMock()
    monkeypatch.setattr(interactions, "get_permissions", permission_lookup)
    monkeypatch.setattr(interactions, "resolve_rich_custom_emojis", resolver)

    await interactions.resolve_interaction_response_rich_emojis(
        session,
        redis,
        SimpleNamespace(domain="apps.example"),
        principal,
        interaction,
        components=components,
        poll=None,
    )

    permission_lookup.assert_awaited_once_with(
        session,
        redis,
        guild,
        principal.user,
        channel=channel,
    )
    resolver.assert_awaited_once_with(
        session,
        principal.user,
        components=components,
        poll=None,
        default_domain="apps.example",
        target_guild=guild,
        target_permissions=int(Permission.USE_EXTERNAL_EMOJIS),
        trusted_external_domain="apps.example",
    )


@pytest.mark.asyncio
async def test_private_response_rich_emoji_fails_when_target_channel_is_unavailable() -> None:
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    interaction = SimpleNamespace(
        guild_id=10,
        guild_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
    )
    components = [
        ActionRow.model_validate(
            {
                "components": [
                    {
                        "type": 2,
                        "custom_id": "wave",
                        "label": "Wave",
                        "emoji": {"id": "30@emoji.example", "name": "wave"},
                    }
                ]
            }
        )
    ]

    with pytest.raises(HTTPException) as unavailable:
        await interactions.resolve_interaction_response_rich_emojis(
            session,
            object(),
            SimpleNamespace(domain="apps.example"),
            public_principal(),
            interaction,
            components=components,
            poll=None,
        )

    assert unavailable.value.status_code == 409
    assert unavailable.value.detail["code"] == "INTERACTION_CHANNEL_UNAVAILABLE"


def test_decimal_command_values_are_bounded_and_federation_serializable() -> None:
    interaction = interactions.InteractionCreate(
        application_ref="1@apps.example",
        command_name="scale",
        options={"factor": 1.5},
    )
    payload = {"interaction": interaction.model_dump(mode="json")}
    encoded = canonical_json(payload, allow_floats=True)

    assert strict_json_loads(encoded, allow_floats=True) == payload
    with pytest.raises(ValueError, match="floating-point"):
        canonical_json(payload)


@pytest.mark.asyncio
async def test_federated_component_uses_authority_lineage_without_clicker_install() -> None:
    payload = interactions.InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="component",
        response_id=99,
        view_version=1,
        custom_id="continue",
    )
    assert interactions.interaction_inherits_authority_installation(payload)


@pytest.mark.asyncio
async def test_remote_guild_install_create_remaps_lineage_for_component_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = BotInstallation(
        id=50,
        application_id=1,
        application_domain="apps.example",
        guild_id=10,
        guild_domain="guild.example",
        bot_user_id=2,
        bot_user_domain="apps.example",
        installer_id=3,
        installer_domain="guild.example",
        granted_scopes=["applications.commands", "messages.send"],
        grant_revision=3,
        status="active",
        revoked_at=None,
    )
    proposed = await channels.message_view_installation_lineage(
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=installation))),
        cast(Any, SimpleNamespace(domain="apps.example")),
        channels.MessageAdmissionOptions(
            application_id=1,
            application_domain="apps.example",
            bot_installation_id=50,
        ),
        federated_transport=True,
    )
    assert proposed == ("guild_install", 50, "guild.example", 3)

    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    bot_actor = SimpleNamespace(id=2, origin_domain="apps.example")
    application = SimpleNamespace(
        id=1,
        origin_domain="apps.example",
        bot_user_id=2,
        bot_user_domain="apps.example",
        status="active",
    )
    bot = SimpleNamespace(
        id=2,
        origin_domain="apps.example",
        account_type="bot",
        disabled_at=None,
    )
    authority_session = SimpleNamespace(
        scalar=AsyncMock(return_value=installation),
        execute=AsyncMock(
            return_value=SimpleNamespace(one_or_none=lambda: (installation, application, bot))
        ),
    )
    proxy_payload = GuildProxyRequest.model_validate(
        {
            "operation": "message.create",
            "actor": {
                "id": "2",
                "origin_domain": "apps.example",
                "username": "bot",
                "account_type": "bot",
            },
            "channel_id": "20",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 1,
                            "label": "Continue",
                            "custom_id": "continue",
                        }
                    ],
                }
            ],
            "application_id": "1@apps.example",
            "interaction_integration_type": proposed[0],
            "interaction_installation_ref": f"{proposed[1]}@{proposed[2]}",
            "interaction_installation_revision": str(proposed[3]),
            "client_nonce": "guild-lineage",
        }
    )
    interaction_projection = await federation.authoritative_proxy_interaction_projection(
        cast(Any, authority_session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, guild),
        cast(Any, bot_actor),
        (1, "apps.example"),
        proxy_payload,
        None,
    )
    stored_lineage = interaction_projection.installation_lineage
    assert interaction_projection.transport_lineage == proposed
    assert stored_lineage == ("guild_install", 50, "guild.example", 3)
    view = MessageView(
        message_id=40,
        message_domain="guild.example",
        application_id=1,
        application_domain="apps.example",
        integration_type=stored_lineage[0],
        installation_id=stored_lineage[1],
        installation_domain=stored_lineage[2],
        installation_revision=stored_lineage[3],
        version=1,
        persistent=True,
    )
    monkeypatch.setattr(interactions, "installation_allows_channel", AsyncMock(return_value=True))
    resolved = await interactions.resolve_interaction_application(
        cast(Any, authority_session),
        ChannelAccess(
            channel=cast(Any, SimpleNamespace(id=20, origin_domain="guild.example")),
            guild=cast(Any, guild),
            participants=[],
        ),
        cast(Any, SimpleNamespace(id=21, origin_domain="clicker.example")),
        interactions.InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="component",
            message_ref="40@guild.example",
            custom_id="continue",
        ),
        (1, "apps.example"),
        None,
        (
            view.integration_type,
            view.installation_id,
            view.installation_domain,
            view.installation_revision,
        ),
        authority_domain="guild.example",
    )
    assert resolved.installation is installation


@pytest.mark.asyncio
async def test_remote_user_install_create_remaps_surrogate_for_component_resolution() -> None:
    sender_installation = BotUserInstallation(
        id=700,
        source_id=77,
        source_domain="owner.example",
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        granted_scopes=["applications.commands"],
        contexts=["guild"],
        grant_revision=4,
        status="active",
        authority_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    proposed = await channels.message_view_installation_lineage(
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=sender_installation))),
        cast(Any, SimpleNamespace(domain="apps.example")),
        channels.MessageAdmissionOptions(
            application_id=12,
            application_domain="apps.example",
            bot_user_installation_id=700,
        ),
        federated_transport=True,
    )
    assert proposed == ("user_install", 77, "owner.example", 4)

    authority_installation = BotUserInstallation(
        id=900,
        source_id=77,
        source_domain="owner.example",
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        granted_scopes=["applications.commands"],
        contexts=["guild"],
        grant_revision=4,
        status="active",
        authority_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    guild = SimpleNamespace(id=10, origin_domain="guild.example")
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        status="active",
    )
    bot = SimpleNamespace(
        id=13,
        origin_domain="apps.example",
        account_type="bot",
        disabled_at=None,
    )

    async def get_model(model: type[object], _identity: object) -> object:
        if model.__name__ == "BotApplication":
            return application
        if model.__name__ == "User":
            return bot
        raise AssertionError(f"unexpected model lookup: {model}")

    authority_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[authority_installation, authority_installation]),
        get=AsyncMock(side_effect=get_model),
    )
    interaction_metadata = {
        "id": "29",
        "origin_domain": "guild.example",
        "interaction_ref": "29@guild.example",
        "type": "command",
        "user": {
            "id": "20",
            "origin_domain": "owner.example",
            "username": "owner",
            "display_name": None,
            "avatar_hash": None,
            "bot": False,
        },
        "user_ref": "20@owner.example",
        "application_ref": "12@apps.example",
        "integration_type": "user_install",
        "authorizing_integration_owners": {"user_install": "20@owner.example"},
        "command_name": "ship",
        "command_type": "chat_input",
    }
    proxy_payload = GuildProxyRequest.model_validate(
        {
            "operation": "message.create",
            "actor": {
                "id": "13",
                "origin_domain": "apps.example",
                "username": "bot",
                "account_type": "bot",
            },
            "channel_id": "20",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 1,
                            "label": "Continue",
                            "custom_id": "continue",
                        }
                    ],
                }
            ],
            "application_id": "12@apps.example",
            "interaction_integration_type": proposed[0],
            "interaction_installation_ref": f"{proposed[1]}@{proposed[2]}",
            "interaction_installation_revision": str(proposed[3]),
            "interaction_message_type": 20,
            "interaction_metadata": interaction_metadata,
            "client_nonce": "user-lineage",
        }
    )
    interaction_projection = await federation.authoritative_proxy_interaction_projection(
        cast(Any, authority_session),
        cast(Any, SimpleNamespace(domain="guild.example")),
        cast(Any, guild),
        cast(Any, SimpleNamespace(id=13, origin_domain="apps.example")),
        (12, "apps.example"),
        proxy_payload,
        None,
    )
    stored_lineage = interaction_projection.installation_lineage
    assert interaction_projection.transport_lineage == proposed
    assert interaction_projection.message_type == 20
    assert interaction_projection.metadata == interaction_metadata
    assert stored_lineage == ("user_install", 900, "guild.example", 4)
    view = MessageView(
        message_id=41,
        message_domain="guild.example",
        application_id=12,
        application_domain="apps.example",
        integration_type=stored_lineage[0],
        installation_id=stored_lineage[1],
        installation_domain=stored_lineage[2],
        installation_revision=stored_lineage[3],
        version=1,
        persistent=True,
    )
    resolved = await interactions.resolve_interaction_application(
        cast(Any, authority_session),
        ChannelAccess(
            channel=cast(Any, SimpleNamespace(id=20, origin_domain="guild.example")),
            guild=cast(Any, guild),
            participants=[],
        ),
        cast(Any, SimpleNamespace(id=21, origin_domain="clicker.example")),
        interactions.InteractionCreate(
            application_ref="12@apps.example",
            interaction_type="component",
            message_ref="41@guild.example",
            custom_id="continue",
        ),
        (12, "apps.example"),
        None,
        (
            view.integration_type,
            view.installation_id,
            view.installation_domain,
            view.installation_revision,
        ),
        authority_domain="guild.example",
    )
    assert resolved.user_installation is authority_installation


@pytest.mark.asyncio
async def test_dm_capability_create_keeps_conversation_authority_lineage_for_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = BotDMCapability(
        id=500,
        grant_id="kbdg_" + "a" * 43,
        source_kind="user",
        source_installation_id=77,
        source_installation_domain="owner.example",
        application_id=12,
        application_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        installing_user_id=20,
        installing_user_domain="owner.example",
        target_user_id=20,
        target_user_domain="owner.example",
        pair_key="b" * 64,
        authority_domain="dm.example",
        conversation_id=30,
        conversation_domain="dm.example",
        revision=5,
        status="active",
    )
    stored_lineage = await channels.message_view_installation_lineage(
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=capability))),
        cast(Any, SimpleNamespace(domain="dm.example")),
        channels.MessageAdmissionOptions(
            application_id=12,
            application_domain="apps.example",
            bot_dm_capability_id=500,
        ),
    )
    assert stored_lineage == ("dm_capability", 500, "dm.example", 5)

    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        status="active",
    )
    bot = SimpleNamespace(
        id=13,
        origin_domain="apps.example",
        account_type="bot",
        disabled_at=None,
    )

    async def get_model(model: type[object], _identity: object) -> object:
        if model.__name__ == "BotApplication":
            return application
        if model.__name__ == "User":
            return bot
        if model.__name__ == "DMConversation":
            return SimpleNamespace(type="direct")
        if model.__name__ == "BotApplicationTarget":
            return object()
        raise AssertionError(f"unexpected model lookup: {model}")

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get_model),
        scalar=AsyncMock(side_effect=[None, capability]),
    )
    monkeypatch.setattr(
        interactions,
        "stored_bot_dm_capability_payload",
        MagicMock(return_value=object()),
    )
    monkeypatch.setattr(
        interactions,
        "dm_capability_runtime_ready",
        MagicMock(return_value=True),
    )
    resolved = await interactions.resolve_interaction_application(
        cast(Any, session),
        ChannelAccess(
            channel=cast(Any, SimpleNamespace(id=30, origin_domain="dm.example")),
            guild=None,
            participants=[cast(Any, bot)],
        ),
        cast(Any, SimpleNamespace(id=21, origin_domain="clicker.example")),
        interactions.InteractionCreate(
            application_ref="12@apps.example",
            interaction_type="component",
            message_ref="42@dm.example",
            custom_id="continue",
        ),
        (12, "apps.example"),
        None,
        stored_lineage,
        authority_domain="dm.example",
    )
    assert resolved.dm_capability is capability


@pytest.mark.asyncio
async def test_remote_public_component_omits_clicker_user_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant = SimpleNamespace(
        authority_domain="target.example",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        interaction_id=None,
        interaction_domain=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=grant),
        add=MagicMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    signed = AsyncMock(
        return_value=httpx.Response(
            202,
            json={
                "id": "30",
                "interaction_ref": "30@target.example",
                "status": "pending",
                "ack_deadline": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
            },
        )
    )
    monkeypatch.setattr(interactions, "signed_request", signed)
    actor = User(
        id=10,
        origin_domain="member.example",
        is_local=True,
        account_type="human",
        username="clicker",
    )
    payload = interactions.InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="component",
        message_ref="40@target.example",
        custom_id="continue",
    )
    admission = SimpleNamespace(
        application_ref=(1, "apps.example"),
        access=SimpleNamespace(
            channel=SimpleNamespace(
                id=20,
                origin_domain="target.example",
                encryption_mode="plaintext",
            ),
            guild=None,
        ),
        invoker_policy=SimpleNamespace(
            locale="en-US",
            age_assured_adult=False,
            age_restricted_dm_commands_enabled=False,
        ),
    )

    result = await interactions.proxy_remote_interaction(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, SimpleNamespace(user=actor)),
        payload,
        cast(Any, admission),
        "target.example",
    )

    assert result["interaction_ref"] == "30@target.example"
    assert signed.await_args.kwargs["payload"]["user_installation"] is None
    # Only the post-ack response grant is loaded; no clicker installation query ran.
    assert session.scalar.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interaction_type", ["command", "autocomplete"])
@pytest.mark.parametrize("integration_type", ["guild_install", "user_install"])
async def test_remote_command_proxy_sends_only_selected_user_install_grant(
    monkeypatch: pytest.MonkeyPatch,
    interaction_type: str,
    integration_type: str,
) -> None:
    actor = User(
        id=10,
        origin_domain="member.example",
        is_local=True,
        account_type="human",
        username="member",
    )
    user_installation = BotUserInstallation(
        id=11,
        application_id=1,
        application_domain="apps.example",
        user_id=actor.id,
        user_domain=actor.origin_domain,
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["guild"],
        grant_revision=2,
        status="active",
    )
    response_grant = SimpleNamespace(
        authority_domain="guild.example",
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        interaction_id=None,
        interaction_domain=None,
    )
    queried_user_installation = False

    async def scalar(statement: object) -> object:
        nonlocal queried_user_installation
        if "bot_user_installations" in str(statement):
            queried_user_installation = True
            return user_installation
        return response_grant

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=scalar),
        add=MagicMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    signed = AsyncMock(
        return_value=httpx.Response(
            202,
            json={
                "id": "30",
                "interaction_ref": "30@guild.example",
                "status": "pending",
                "ack_deadline": (datetime.now(UTC) + timedelta(seconds=3)).isoformat(),
            },
        )
    )
    monkeypatch.setattr(interactions, "signed_request", signed)
    monkeypatch.setattr(
        interactions,
        "prepare_federated_interaction_attachments",
        AsyncMock(return_value=[]),
    )
    payload = interactions.InteractionCreate(
        application_ref="1@apps.example",
        interaction_type=cast(Any, interaction_type),
        command_name="ship",
        command_id=20,
        integration_type=cast(Any, integration_type),
        focused_option="target" if interaction_type == "autocomplete" else None,
    )
    admission = SimpleNamespace(
        application_ref=(1, "apps.example"),
        access=SimpleNamespace(
            channel=SimpleNamespace(
                id=20,
                origin_domain="guild.example",
                encryption_mode="plaintext",
            ),
            guild=SimpleNamespace(),
        ),
        invoker_policy=SimpleNamespace(
            locale="en-US",
            age_assured_adult=False,
            age_restricted_dm_commands_enabled=False,
        ),
    )

    await interactions.proxy_remote_interaction(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, SimpleNamespace(user=actor)),
        payload,
        cast(Any, admission),
        "guild.example",
    )

    forwarded_grant = signed.await_args.kwargs["payload"]["user_installation"]
    if integration_type == "user_install":
        assert queried_user_installation
        assert forwarded_grant["id"] == "11"
    else:
        assert not queried_user_installation
        assert forwarded_grant is None


def test_modal_create_requires_exact_type_nine_response_capability() -> None:
    submitted = valid_modal_submission()
    created = interactions.InteractionCreate(
        application_ref="1@apps.example",
        interaction_type="modal_submit",
        response_id=20,
        custom_id="deploy",
        components=submitted,
    )
    assert created.response_id == 20

    with pytest.raises(ValueError):
        interactions.InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="modal_submit",
            custom_id="deploy",
            components=submitted,
        )

    with pytest.raises(ValueError):
        interactions.InteractionCreate(
            application_ref="1@apps.example",
            interaction_type="modal_submit",
            response_id=20,
            custom_id="deploy",
            command_type="user",
            components=submitted,
        )


def test_component_submission_is_bound_to_exact_authored_kind_and_values() -> None:
    source = rows(
        Button(custom_id="run", label="Run"),
        StringSelect(
            custom_id="fruit",
            min_values=1,
            max_values=2,
            options=[
                SelectOption(label="Apple", value="apple"),
                SelectOption(label="Pear", value="pear"),
            ],
        ),
    )

    button = validate_component_submission(source, "run", [])
    assert button.component_type == 2
    assert button.values == []
    assert validate_component_submission(source, "fruit", ["pear"]).values == ["pear"]

    for custom_id, values in [
        ("run", ["admin"]),
        ("fruit", ["forged"]),
        ("fruit", ["pear", "pear"]),
    ]:
        with pytest.raises(HTTPException) as denied:
            validate_component_submission(source, custom_id, values)
        assert denied.value.status_code == 422


def test_disabled_or_removed_component_fails_closed() -> None:
    source = rows(Button(custom_id="run", label="Run", disabled=True))

    with pytest.raises(HTTPException) as disabled:
        validate_component_submission(source, "run", [])
    assert disabled.value.status_code == 409
    assert disabled.value.detail["code"] == "COMPONENT_DISABLED"

    with pytest.raises(HTTPException) as removed:
        validate_component_submission(source, "other", [])
    assert removed.value.status_code == 404
    assert removed.value.detail["code"] == "COMPONENT_NOT_FOUND"


def modal_payload() -> dict[str, object]:
    return Modal.model_validate(
        {
            "title": "Deploy",
            "custom_id": "deploy",
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 4,
                            "custom_id": "reason",
                            "label": "Reason",
                            "min_length": 2,
                            "max_length": 20,
                            "required": True,
                        }
                    ],
                },
                {
                    "type": 18,
                    "label": "Environment",
                    "component": {
                        "type": 3,
                        "custom_id": "environment",
                        "options": [
                            {"label": "Production", "value": "prod"},
                            {"label": "Staging", "value": "stage"},
                        ],
                    },
                },
                {
                    "type": 18,
                    "label": "Confirm",
                    "component": {
                        "type": 23,
                        "custom_id": "confirm",
                    },
                },
            ],
        }
    ).model_dump(mode="json", exclude_none=True)


def valid_modal_submission() -> list[dict[str, object]]:
    return [
        {
            "type": 1,
            "components": [{"type": 4, "custom_id": "reason", "value": "Ship it"}],
        },
        {
            "type": 18,
            "id": 3,
            "component": {"type": 3, "custom_id": "environment", "values": ["prod"]},
        },
        {
            "type": 18,
            "id": 5,
            "component": {"type": 23, "custom_id": "confirm", "value": True},
        },
    ]


def test_modal_submission_is_exact_schema_validated() -> None:
    validated = validate_modal_submission(
        modal_payload(),
        "deploy",
        valid_modal_submission(),
    )
    assert validated.components == valid_modal_submission()

    attacks = [
        ("forged", valid_modal_submission()),
        ("deploy", valid_modal_submission()[:-1]),
        (
            "deploy",
            [
                *valid_modal_submission()[:1],
                {
                    "type": 18,
                    "id": 3,
                    "component": {
                        "type": 3,
                        "custom_id": "environment",
                        "values": ["root"],
                    },
                },
                valid_modal_submission()[2],
            ],
        ),
        (
            "deploy",
            [
                {
                    "type": 1,
                    "components": [{"type": 4, "custom_id": "other", "value": "Ship it"}],
                },
                *valid_modal_submission()[1:],
            ],
        ),
    ]
    for custom_id, submitted in attacks:
        with pytest.raises(HTTPException):
            validate_modal_submission(modal_payload(), custom_id, submitted)


def test_modal_uploads_are_exposed_and_optional_radio_submits_null() -> None:
    modal = {
        "title": "Evidence",
        "custom_id": "evidence",
        "components": [
            {
                "type": 18,
                "label": "Priority",
                "component": {
                    "type": 21,
                    "custom_id": "priority",
                    "required": False,
                    "options": [
                        {"label": "Normal", "value": "normal"},
                        {"label": "Urgent", "value": "urgent"},
                    ],
                },
            },
            {
                "type": 18,
                "label": "Files",
                "component": {
                    "type": 19,
                    "custom_id": "files",
                    "required": False,
                    "min_values": 0,
                    "max_values": 2,
                    "file_types": ["image", ".PDF"],
                },
            },
        ],
    }
    submitted = [
        {
            "type": 18,
            "component": {"type": 21, "custom_id": "priority", "value": None},
        },
        {
            "type": 18,
            "component": {
                "type": 19,
                "custom_id": "files",
                "values": ["101", "102"],
            },
        },
    ]

    validated = validate_modal_submission(modal, "evidence", submitted)

    assert validated.components[0]["component"] == submitted[0]["component"]
    assert validated.components[1]["component"] == submitted[1]["component"]
    assert len(validated.file_fields) == 1
    assert validated.file_fields[0][0].file_types == ["image", ".pdf"]
    assert validated.file_fields[0][1] == ["101", "102"]

    modal["components"][0]["component"]["required"] = True  # type: ignore[index]
    with pytest.raises(HTTPException) as required:
        validate_modal_submission(modal, "evidence", submitted)
    assert required.value.detail["code"] == "MODAL_SUBMISSION_INVALID"


def test_modal_attachment_capabilities_are_numeric_unique_and_bounded() -> None:
    upload = authority.FileUpload(
        custom_id="files",
        required=False,
        min_values=0,
        max_values=2,
        file_types=["image"],
    )
    ids, filters = interactions.modal_attachment_file_types([(upload, ["4", "5"])])
    assert ids == [4, 5]
    assert filters == {4: ["image"], 5: ["image"]}

    for values in (["forged"], ["4", "4"]):
        with pytest.raises(HTTPException):
            interactions.modal_attachment_file_types([(upload, values)])


@pytest.mark.asyncio
async def test_plaintext_modal_files_relay_across_user_channel_and_app_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    submitted = [
        {
            "type": 18,
            "component": {
                "type": 19,
                "custom_id": "files",
                "values": ["101", "102"],
            },
        }
    ]
    payload = interactions.InteractionCreate(
        application_ref="5@apps.example",
        interaction_type="modal_submit",
        response_id=40,
        custom_id="evidence",
        components=submitted,
    )
    selected_ids, untrusted_filters = await interactions.proxy_interaction_attachment_selection(
        cast(Any, SimpleNamespace()),
        payload,
        (5, "apps.example"),
    )
    assert selected_ids == [101, 102]
    assert untrusted_filters == {}

    modal = {
        "title": "Evidence",
        "custom_id": "evidence",
        "components": [
            {
                "type": 18,
                "label": "Files",
                "component": {
                    "type": 19,
                    "custom_id": "files",
                    "required": True,
                    "min_values": 2,
                    "max_values": 2,
                    "file_types": ["image", ".pdf"],
                },
            }
        ],
    }
    validated = validate_modal_submission(modal, "evidence", submitted)
    authoritative_ids, authoritative_filters = interactions.modal_attachment_file_types(
        validated.file_fields
    )
    assert authoritative_ids == selected_ids
    assert authoritative_filters == {101: ["image", ".pdf"], 102: ["image", ".pdf"]}

    def source_attachment(
        attachment_id: int,
        filename: str,
        content_type: str,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=attachment_id,
            origin_domain="home.example",
            filename=filename,
            content_type=content_type,
            detected_content_type=None,
            size=128,
            width=None,
            height=None,
            duration_secs=None,
            waveform=None,
            blurhash=None,
            scan_status="clean",
            encryption_mode="plaintext",
            encryption_protocol=None,
            variants={},
            content_sha256=("a" if attachment_id == 101 else "b") * 64,
            upload_channel_id=7,
            upload_channel_domain="channel.example",
            message_id=None,
            message_domain=None,
            interaction_id=None,
            interaction_response_id=None,
            bot_installation_id=None,
            bot_user_installation_id=None,
            bot_dm_capability_id=None,
            asset_binding=None,
            report_id=None,
        )

    source_attachments = {
        101: source_attachment(101, "proof.png", "image/png"),
        102: source_attachment(102, "notes.pdf", "application/pdf"),
    }

    async def finalize_source(
        _session: object,
        _settings: object,
        _actor: object,
        attachment_id: int,
        *,
        required_purpose: str,
    ) -> SimpleNamespace:
        assert required_purpose == "attachment"
        return source_attachments[attachment_id]

    lock = AsyncMock(return_value=None)
    monkeypatch.setattr(interactions, "lock_media_tombstone_ref", lock)
    monkeypatch.setattr(interactions, "finalize_attachment", finalize_source)
    source_session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        added=[],
    )
    source_session.add = source_session.added.append
    response_grant_id = "r" * 43
    expires_at = datetime.now(UTC) + timedelta(minutes=15)
    projections = await interactions.prepare_federated_interaction_attachments(
        cast(Any, source_session),
        cast(
            Any,
            SimpleNamespace(
                domain="home.example",
                media_max_attachment_bytes=8_388_608,
            ),
        ),
        cast(Any, SimpleNamespace(id=1, origin_domain="home.example")),
        payload,
        cast(
            Any,
            SimpleNamespace(
                application_ref=(5, "apps.example"),
                access=SimpleNamespace(
                    channel=SimpleNamespace(
                        id=7,
                        origin_domain="channel.example",
                        encryption_mode="plaintext",
                    )
                ),
            ),
        ),
        "channel.example",
        response_grant_id,
        expires_at,
    )
    assert [item.attachment["origin_domain"] for item in projections] == [
        "home.example",
        "home.example",
    ]
    source_grants = [
        item
        for item in source_session.added
        if type(item).__name__ == "FederatedInteractionAttachmentGrant"
    ]
    assert len(source_grants) == 2
    assert all(item.destination_domain == "channel.example" for item in source_grants)

    target_session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        added=[],
    )
    target_session.add = target_session.added.append
    interaction = SimpleNamespace(
        id=900,
        application_id=5,
        application_domain="apps.example",
        user_id=1,
        user_domain="home.example",
        channel_id=7,
        channel_domain="channel.example",
        response_grant_id=response_grant_id,
        created_at=datetime.now(UTC),
        expires_at=expires_at,
    )
    resolved, materialized = await interactions.materialize_federated_interaction_attachments(
        cast(Any, target_session),
        cast(
            Any,
            SimpleNamespace(
                domain="channel.example",
                media_max_attachment_bytes=8_388_608,
            ),
        ),
        cast(Any, interaction),
        tuple(projections),
        authoritative_ids,
        authoritative_filters,
        expected_encryption_mode="plaintext",
    )
    assert set(resolved) == {"101", "102"}
    assert [item.origin_domain for item in materialized] == [
        "home.example",
        "home.example",
    ]
    assert all(item.interaction_id == 900 for item in materialized)
    target_grants = [
        item
        for item in target_session.added
        if type(item).__name__ == "FederatedInteractionAttachmentGrant"
    ]
    assert len(target_grants) == 2
    assert all(item.attachment_domain == "home.example" for item in target_grants)
    assert all(item.destination_domain == "channel.example" for item in target_grants)

    with pytest.raises(HTTPException) as missing_projection:
        await interactions.materialize_federated_interaction_attachments(
            cast(Any, target_session),
            cast(
                Any,
                SimpleNamespace(
                    domain="channel.example",
                    media_max_attachment_bytes=8_388_608,
                ),
            ),
            cast(Any, interaction),
            tuple(projections[:-1]),
            authoritative_ids,
            authoritative_filters,
            expected_encryption_mode="plaintext",
        )
    assert missing_projection.value.detail["code"] == "ATTACHMENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attachment_changes", "status_code", "code"),
    [
        ({"upload_channel_id": 99}, 404, "ATTACHMENT_NOT_FOUND"),
        ({"interaction_id": 55}, 409, "ATTACHMENT_ALREADY_USED"),
    ],
)
async def test_invocation_attachment_binding_rejects_cross_channel_and_replay(
    monkeypatch: pytest.MonkeyPatch,
    attachment_changes: dict[str, object],
    status_code: int,
    code: str,
) -> None:
    attachment = SimpleNamespace(
        id=8,
        origin_domain="chat.example",
        filename="proof.png",
        content_type="image/png",
        upload_channel_id=10,
        upload_channel_domain="chat.example",
        message_id=None,
        message_domain=None,
        interaction_id=None,
        interaction_response_id=None,
        bot_installation_id=None,
        bot_user_installation_id=None,
        asset_binding=None,
        report_id=None,
        encryption_mode="plaintext",
    )
    for name, value in attachment_changes.items():
        setattr(attachment, name, value)
    monkeypatch.setattr(interactions, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(interactions, "finalize_attachment", AsyncMock(return_value=attachment))

    with pytest.raises(HTTPException) as rejected:
        await interactions.bind_invocation_attachments(
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),
            SimpleNamespace(),
            SimpleNamespace(id=7, channel_id=10, channel_domain="chat.example"),
            [8],
            {8: ["image"]},
            expected_encryption_mode="plaintext",
        )

    assert rejected.value.status_code == status_code
    assert rejected.value.detail["code"] == code


@pytest.mark.asyncio
async def test_invocation_attachment_binding_preserves_owner_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(interactions, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(
        interactions,
        "finalize_attachment",
        AsyncMock(side_effect=HTTPException(403, detail={"code": "ATTACHMENT_NOT_OWNED"})),
    )

    with pytest.raises(HTTPException) as rejected:
        await interactions.bind_invocation_attachments(
            AsyncMock(),
            SimpleNamespace(domain="chat.example"),
            SimpleNamespace(),
            SimpleNamespace(id=7, channel_id=10, channel_domain="chat.example"),
            [8],
            {},
            expected_encryption_mode="plaintext",
        )

    assert rejected.value.detail["code"] == "ATTACHMENT_NOT_OWNED"


@pytest.mark.asyncio
async def test_local_user_installation_preserves_null_foreign_source_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_order: list[str] = []
    application = SimpleNamespace(
        id=12,
        origin_domain="chat.example",
        name="Helper",
        description=None,
        icon_hash=None,
        e2ee_modes=[],
        bot_user_id=13,
        bot_user_domain="chat.example",
    )

    async def lock_application(*_args: object) -> object:
        lock_order.append("application")
        return application

    async def take_advisory_lock(*_args: object) -> None:
        lock_order.append("advisory")

    async def lock_installation(*_args: object) -> None:
        lock_order.append("installation")

    monkeypatch.setattr(interactions, "installable_user_application", lock_application)
    queue_targets = AsyncMock(return_value={"apps.example"})
    wake_targets = AsyncMock()
    monkeypatch.setattr(
        interactions,
        "queue_application_target_snapshots_for_refs",
        queue_targets,
    )
    monkeypatch.setattr(
        interactions,
        "wake_application_target_deliveries",
        wake_targets,
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=take_advisory_lock),
        scalar=AsyncMock(side_effect=lock_installation),
        add=MagicMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )
    actor = SimpleNamespace(id=20, origin_domain="chat.example")

    rendered = await interactions.create_user_installation(
        interactions.UserInstallationCreate(
            application_ref="12@chat.example",
            contexts=["bot_dm"],
        ),
        SimpleNamespace(user=actor),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=77)),
        SimpleNamespace(domain="chat.example"),
    )

    installation = session.add.call_args.args[0]
    assert isinstance(installation, BotUserInstallation)
    assert (installation.id, installation.source_id, installation.source_domain) == (77, None, None)
    assert installation.contexts == ["bot_dm"]
    assert rendered["source_ref"] is None
    assert lock_order == ["application", "advisory", "installation"]
    queue_targets.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),
        {(12, "chat.example")},
    )
    wake_targets.assert_awaited_once_with({"apps.example"})


@pytest.mark.asyncio
async def test_user_install_update_materializes_timestamp_and_renders_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_time = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed_at = old_time + timedelta(minutes=1)
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        name="Helper",
        description=None,
        icon_hash=None,
        e2ee_modes=[],
        bot_user_id=13,
        bot_user_domain="apps.example",
    )
    actor = SimpleNamespace(id=20, origin_domain="chat.example")
    installation = BotUserInstallation(
        id=77,
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="chat.example",
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision=1,
        status="active",
        created_at=old_time,
        updated_at=old_time,
    )
    lifecycle: list[str] = []
    committed = False

    async def queue_targets(*_args: object, **_kwargs: object) -> set[str]:
        installation.updated_at = None  # type: ignore[assignment]
        lifecycle.append("queue")
        return {"apps.example"}

    async def refresh(value: object, *, attribute_names: tuple[str, ...]) -> None:
        assert value is installation
        assert attribute_names == ("updated_at",)
        installation.updated_at = refreshed_at
        lifecycle.append("refresh")

    async def commit() -> None:
        nonlocal committed
        committed = True
        lifecycle.append("commit")

    session = SimpleNamespace(
        flush=AsyncMock(side_effect=lambda: lifecycle.append("flush")),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
    monkeypatch.setattr(
        interactions,
        "owned_user_installation_application_ref",
        AsyncMock(return_value=(12, "apps.example")),
    )
    monkeypatch.setattr(
        interactions,
        "locked_installable_user_application",
        AsyncMock(return_value=application),
    )
    monkeypatch.setattr(
        interactions,
        "locked_owned_user_installation",
        AsyncMock(return_value=installation),
    )
    monkeypatch.setattr(interactions, "require_user_install_policy", MagicMock())
    monkeypatch.setattr(interactions, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        interactions,
        "queue_application_target_snapshots_for_refs",
        AsyncMock(side_effect=queue_targets),
    )
    monkeypatch.setattr(interactions, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(interactions, "wake_application_target_deliveries", AsyncMock())
    original_payload = interactions.user_installation_payload

    def render_before_commit(value: BotUserInstallation, app: object) -> dict[str, object]:
        assert not committed
        lifecycle.append("render")
        return original_payload(value, cast(Any, app))

    monkeypatch.setattr(interactions, "user_installation_payload", render_before_commit)

    rendered = await interactions.update_user_installation(
        77,
        interactions.UserInstallationPatch(contexts=["bot_dm"]),
        SimpleNamespace(user=actor),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
    )

    assert rendered["updated_at"] == refreshed_at.isoformat()
    assert rendered["grant_revision"] == "2"
    assert lifecycle == ["queue", "flush", "refresh", "render", "commit"]


@pytest.mark.asyncio
async def test_user_install_update_and_delete_lock_application_before_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        name="Helper",
        description=None,
        icon_hash=None,
        e2ee_modes=[],
        bot_user_id=13,
        bot_user_domain="apps.example",
    )
    actor = SimpleNamespace(id=20, origin_domain="chat.example")
    settings = SimpleNamespace(domain="chat.example")

    async def exercise(*, delete: bool) -> list[str]:
        order: list[str] = []
        installation = BotUserInstallation(
            id=77,
            source_id=77,
            source_domain="chat.example",
            application_id=12,
            application_domain="apps.example",
            user_id=20,
            user_domain="chat.example",
            granted_scopes=["applications.commands"],
            granted_intents=["interactions"],
            contexts=["bot_dm"],
            grant_revision=1,
            status="active",
        )

        async def read_ref(*_args: object) -> tuple[int, str]:
            order.append("read-ref")
            return 12, "apps.example"

        async def lock_installable(*_args: object) -> object:
            order.append("application")
            return application

        async def lock_owned(*_args: object) -> BotUserInstallation:
            order.append("installation")
            return installation

        monkeypatch.setattr(interactions, "owned_user_installation_application_ref", read_ref)
        monkeypatch.setattr(interactions, "locked_owned_user_installation", lock_owned)
        monkeypatch.setattr(
            interactions,
            "queue_application_target_snapshots_for_refs",
            AsyncMock(return_value=set()),
        )
        monkeypatch.setattr(interactions, "wake_application_target_deliveries", AsyncMock())
        monkeypatch.setattr(interactions, "publish_e2ee_policy_updates", AsyncMock())
        monkeypatch.setattr(interactions, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))
        session = SimpleNamespace(
            flush=AsyncMock(),
            refresh=AsyncMock(),
            commit=AsyncMock(),
        )
        if delete:
            session.scalar = AsyncMock(
                side_effect=lambda *_args: order.append("application") or application
            )
            await interactions.delete_user_installation(
                77,
                SimpleNamespace(user=actor),
                session,
                SimpleNamespace(),
                SimpleNamespace(),
                settings,
            )
        else:
            monkeypatch.setattr(
                interactions,
                "locked_installable_user_application",
                lock_installable,
            )
            monkeypatch.setattr(interactions, "require_user_install_policy", MagicMock())
            await interactions.update_user_installation(
                77,
                interactions.UserInstallationPatch(contexts=["bot_dm"]),
                SimpleNamespace(user=actor),
                session,
                SimpleNamespace(),
                SimpleNamespace(),
                settings,
            )
        return order

    assert await exercise(delete=False) == ["read-ref", "application", "installation"]
    assert await exercise(delete=True) == ["read-ref", "application", "installation"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"supported_install_types": ["guild_install"]}, "APPLICATION_USER_INSTALL_UNAVAILABLE"),
        ({"user_install_scopes": ["applications.commands"]}, "APPLICATION_SCOPE_NOT_INSTALLABLE"),
        ({"user_install_contexts": ["bot_dm"]}, "APPLICATION_CONTEXT_NOT_INSTALLABLE"),
    ],
)
def test_user_install_policy_rejects_tampered_grants(changes: dict[str, object], code: str) -> None:
    values: dict[str, object] = {
        "supported_install_types": ["user_install"],
        "user_install_scopes": ["applications.commands", "interactions.respond"],
        "user_install_contexts": ["guild", "bot_dm"],
        "default_scopes": ["applications.commands", "interactions.respond"],
        "default_intents": ["interactions"],
    }
    values.update(changes)
    with pytest.raises(HTTPException) as rejected:
        interactions.require_user_install_policy(
            SimpleNamespace(**values),
            ["applications.commands", "interactions.respond"],
            ["interactions"],
            ["guild"],
        )
    assert rejected.value.detail["code"] == code


@pytest.mark.asyncio
async def test_direct_remote_user_install_refreshes_signed_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        status="active",
        bot_user_id=13,
        bot_user_domain="apps.example",
        supported_install_types=["user_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["bot_dm"],
        default_scopes=["applications.commands", "interactions.respond"],
        default_intents=["interactions"],
        target_policy="open",
    )
    bot = SimpleNamespace(account_type="bot", disabled_at=None)

    async def get(model: object, _key: object) -> object | None:
        if model is User:
            return bot
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, 99]),
        get=AsyncMock(side_effect=get),
    )
    refresh = AsyncMock()
    runtime_fence = AsyncMock()
    monkeypatch.setattr(interactions, "refresh_user_bot_application", refresh)
    monkeypatch.setattr(
        interactions,
        "require_application_runtime_enabled",
        runtime_fence,
    )

    resolved = await interactions.installable_user_application(
        session,
        SimpleNamespace(domain="chat.example"),
        SimpleNamespace(),
        EntityRef("12@apps.example"),
        ["applications.commands", "interactions.respond"],
        ["interactions"],
        ["bot_dm"],
    )

    assert resolved is application
    refresh.assert_awaited_once()
    assert refresh.await_args.args[-2:] == (12, "apps.example")
    assert "FOR UPDATE" in str(session.scalar.await_args_list[0].args[0])
    runtime_fence.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),
        application,
    )


@pytest.mark.asyncio
async def test_federated_user_installation_uses_source_ref_and_surrogate_id() -> None:
    application = SimpleNamespace(
        id=12,
        origin_domain="chat.example",
        status="active",
        bot_user_id=13,
        bot_user_domain="chat.example",
        default_scopes=["applications.commands", "interactions.respond"],
        default_intents=["interactions"],
        supported_install_types=["user_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["bot_dm"],
        target_policy="open",
    )
    bot = SimpleNamespace(account_type="bot", disabled_at=None)
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key: bot if model is User else None),
        execute=AsyncMock(),
        scalar=AsyncMock(side_effect=[application, None, None]),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    user = SimpleNamespace(id=20, origin_domain="home.example")
    grant = interactions.FederatedUserInstallationGrant(
        id="88",
        application_ref="12@chat.example",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision="3",
        authority_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    installation = await interactions.materialize_federated_user_installation(
        session,
        SimpleNamespace(domain="chat.example", federation_clock_skew_seconds=300),
        SimpleNamespace(mint=AsyncMock(return_value=999)),
        user,
        interactions.InteractionCreate(
            application_ref="12@chat.example",
            command_name="help",
        ),
        grant,
    )

    assert (installation.id, installation.source_id, installation.source_domain) == (
        999,
        88,
        "home.example",
    )
    assert installation.grant_revision == 3


@pytest.mark.asyncio
async def test_federated_user_installation_locks_application_before_grant_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    application = SimpleNamespace(id=12, origin_domain="apps.example")

    async def lock_application(*_args: object, **_kwargs: object) -> object:
        order.append("application")
        return application

    async def advisory(*_args: object) -> None:
        order.append("advisory")

    async def installation_row(*_args: object) -> None:
        order.append("installation")

    monkeypatch.setattr(interactions, "require_federated_user_application", lock_application)
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=advisory),
        scalar=AsyncMock(side_effect=installation_row),
        add=MagicMock(),
        flush=AsyncMock(),
    )
    grant = interactions.FederatedUserInstallationGrant(
        id="88",
        application_ref="12@apps.example",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision="3",
        authority_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    await interactions.materialize_federated_user_installation(
        session,
        SimpleNamespace(domain="chat.example", federation_clock_skew_seconds=300),
        SimpleNamespace(mint=AsyncMock(return_value=999)),
        SimpleNamespace(id=20, origin_domain="home.example"),
        interactions.InteractionCreate(
            application_ref="12@apps.example",
            command_name="help",
        ),
        grant,
    )

    assert order == ["application", "advisory", "installation", "installation"]


@pytest.mark.asyncio
async def test_federated_user_installation_rejects_source_remapping() -> None:
    application = SimpleNamespace(
        id=12,
        origin_domain="chat.example",
        status="active",
        bot_user_id=13,
        bot_user_domain="chat.example",
        default_scopes=["applications.commands", "interactions.respond"],
        default_intents=["interactions"],
        supported_install_types=["user_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["bot_dm"],
        target_policy="open",
    )
    existing = BotUserInstallation(
        id=999,
        source_id=87,
        source_domain="home.example",
        application_id=12,
        application_domain="chat.example",
        user_id=20,
        user_domain="home.example",
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=lambda model, key: (
                SimpleNamespace(account_type="bot", disabled_at=None) if model is User else None
            )
        ),
        execute=AsyncMock(),
        scalar=AsyncMock(side_effect=[application, None, existing]),
    )
    grant = interactions.FederatedUserInstallationGrant(
        id="88",
        application_ref="12@chat.example",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision="3",
        authority_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )

    with pytest.raises(HTTPException) as rejected:
        await interactions.materialize_federated_user_installation(
            session,
            SimpleNamespace(domain="chat.example", federation_clock_skew_seconds=300),
            SimpleNamespace(mint=AsyncMock(return_value=1000)),
            SimpleNamespace(id=20, origin_domain="home.example"),
            interactions.InteractionCreate(
                application_ref="12@chat.example",
                command_name="help",
            ),
            grant,
        )

    assert rejected.value.detail["code"] == "USER_INSTALLATION_SOURCE_CONFLICT"


@pytest.mark.asyncio
async def test_entity_select_rejects_cross_guild_and_invisible_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=1,
        origin_domain="chat.example",
        name="Guild",
        owner_id=10,
        owner_domain="chat.example",
        unavailable=False,
    )
    source_channel = Channel(
        id=2,
        origin_domain="chat.example",
        guild_id=1,
        guild_domain="chat.example",
        type=0,
        name="general",
        position=0,
        unavailable=False,
    )
    hidden_channel = Channel(
        id=3,
        origin_domain="chat.example",
        guild_id=1,
        guild_domain="chat.example",
        type=0,
        name="staff",
        position=1,
        unavailable=False,
    )
    foreign_role = Role(
        id=4,
        origin_domain="other.example",
        guild_id=99,
        guild_domain="other.example",
        name="Foreign",
        permissions=0,
        position=1,
        color=0,
        hoist=False,
        mentionable=True,
    )
    actor = User(
        id=10,
        origin_domain="chat.example",
        is_local=True,
        account_type="human",
        username="member",
        profile_resolved=True,
    )
    values = {
        (Channel, (hidden_channel.id, hidden_channel.origin_domain)): hidden_channel,
        (Role, (foreign_role.id, foreign_role.origin_domain)): foreign_role,
    }
    session = SimpleNamespace(
        get=AsyncMock(side_effect=lambda model, key: values.get((model, key)))
    )
    monkeypatch.setattr(authority, "get_permissions", AsyncMock(return_value=0))
    access = ChannelAccess(source_channel, guild, [])

    with pytest.raises(HTTPException) as invisible:
        await resolve_component_entities(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
            access,
            actor,
            [(ChannelSelect(custom_id="channel"), ["3@chat.example"])],
        )
    assert invisible.value.detail["code"] == "COMPONENT_VALUE_INVALID"

    with pytest.raises(HTTPException) as cross_guild:
        await resolve_component_entities(
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
            access,
            actor,
            [(MentionableSelect(custom_id="mention"), ["4@other.example"])],
        )
    assert cross_guild.value.detail["code"] == "COMPONENT_VALUE_INVALID"


@pytest.mark.asyncio
async def test_entity_select_projects_only_authority_loaded_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=1, origin_domain="chat.example")
    source_channel = SimpleNamespace(id=2, origin_domain="chat.example")
    selected_channel = Channel(
        id=3,
        origin_domain="chat.example",
        guild_id=1,
        guild_domain="chat.example",
        type=0,
        name="general",
        position=0,
        unavailable=False,
    )
    actor = SimpleNamespace(id=10, origin_domain="chat.example")
    session = SimpleNamespace(get=AsyncMock(return_value=selected_channel))
    monkeypatch.setattr(
        authority,
        "get_permissions",
        AsyncMock(return_value=int(Permission.VIEW_CHANNEL)),
    )
    monkeypatch.setattr(
        authority,
        "channel_payload",
        lambda channel: {"id": str(channel.id), "name": channel.name},
    )

    resolved = await resolve_component_entities(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
        ChannelAccess(source_channel, guild, []),
        actor,
        [(ChannelSelect(custom_id="channel", channel_types=[0]), ["3@chat.example"])],
    )

    assert resolved == {
        "channels": {
            "3@chat.example": {
                "id": "3",
                "name": "general",
                "permissions": str(int(Permission.VIEW_CHANNEL)),
            }
        }
    }


@pytest.mark.asyncio
async def test_deprecated_premium_callback_is_rejected_not_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(id=10, status="pending")
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, SimpleNamespace())),
    )
    session = SimpleNamespace(add=AsyncMock(), commit=AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await interactions.callback_interaction(
            10,
            interactions.InteractionCallback(type=10),
            Response(),
            SimpleNamespace(),
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="chat.example"),
        )

    assert denied.value.status_code == 400
    assert denied.value.detail["code"] == "INTERACTION_CALLBACK_UNSUPPORTED"
    session.commit.assert_not_awaited()


@pytest.mark.parametrize("callback_type", [True, 11, 12])
def test_unadvertised_or_ambiguous_callback_types_are_schema_invalid(
    callback_type: object,
) -> None:
    with pytest.raises(ValidationError):
        interactions.InteractionCallback.model_validate({"type": callback_type})


@pytest.mark.parametrize(
    "payload",
    [
        {"flags": True},
        {"view_version": True},
        {"view_timeout_seconds": True},
    ],
)
def test_interaction_response_edit_rejects_boolean_numeric_fields(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="must be an integer"):
        interactions.InteractionResponseEdit.model_validate(payload)


def test_interaction_response_edit_accepts_discord_noop_body() -> None:
    edit = interactions.InteractionResponseEdit.model_validate({})

    assert edit.model_fields_set == set()


def test_callback_message_validation_returns_a_typed_client_error() -> None:
    with pytest.raises(HTTPException) as denied:
        interactions.interaction_message_from_data({"content": "   "})

    assert denied.value.status_code == 422
    assert denied.value.detail["code"] == "INTERACTION_CALLBACK_DATA_INVALID"


def test_empty_deferred_materialization_returns_a_typed_client_error() -> None:
    with pytest.raises(HTTPException) as denied:
        interactions.deferred_interaction_message(interactions.InteractionResponseEdit())

    assert denied.value.status_code == 422
    assert denied.value.detail["code"] == "INTERACTION_RESPONSE_EDIT_INVALID"


def test_interaction_create_rejects_boolean_view_version() -> None:
    with pytest.raises(ValidationError, match="view_version must be an integer"):
        interactions.InteractionCreate.model_validate(
            {"application_ref": "1@apps.example", "view_version": True}
        )


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (interactions.InteractionDefer, {"ephemeral": 1}),
        (
            interactions.InteractionFollowup,
            {"message": {"content": "hello"}, "ephemeral": "true"},
        ),
        (
            interactions.FederatedInteractionCreate,
            {
                "user_id": "1",
                "interaction": {"application_ref": "1@apps.example"},
                "response_grant_id": "a" * 32,
                "response_expires_at": datetime.now(UTC).isoformat(),
                "age_assured_adult": 1,
            },
        ),
    ],
)
def test_interaction_boolean_request_fields_reject_coercion(
    model: type[object], payload: dict[str, object]
) -> None:
    with pytest.raises(ValidationError, match="must be a boolean"):
        model.model_validate(payload)  # type: ignore[attr-defined]


def test_voice_message_callback_flag_normalizes_discord_style_body() -> None:
    message = interactions.interaction_message_from_data(
        {"flags": 1 << 13, "attachment_ids": ["42"]}
    )
    assert message.voice_message is True
    assert message.flags & (1 << 13)


@pytest.mark.asyncio
async def test_public_component_keeps_user_install_owner_across_clickers() -> None:
    installation = BotUserInstallation(
        id=77,
        source_id=77,
        source_domain="guild.example",
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        grant_revision=4,
        status="active",
        granted_scopes=["applications.commands"],
        contexts=["guild"],
    )
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
    )
    bot = SimpleNamespace(id=13, origin_domain="apps.example")
    owner = SimpleNamespace(
        id=20,
        origin_domain="owner.example",
        account_type="human",
        disabled_at=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=installation),
        get=AsyncMock(side_effect=[application, bot]),
    )
    clicker = SimpleNamespace(id=21, origin_domain="clicker.example")
    payload = SimpleNamespace(interaction_type="component", integration_type=None)

    resolved, _, _, _ = await interactions.guild_user_installation(
        session,
        SimpleNamespace(origin_domain="guild.example"),
        clicker,
        payload,
        (12, "apps.example"),
        None,
        ("user_install", 77, "guild.example", 4),
        authority_domain="guild.example",
    )
    assert resolved is installation
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile()
    sql = str(compiled)
    assert "bot_user_installations.id =" in sql
    assert "bot_user_installations.grant_revision =" in sql
    assert "bot_user_installations.status =" in sql
    assert "bot_user_installations.user_id =" not in sql
    assert 77 in compiled.params.values()
    assert 4 in compiled.params.values()
    assert 21 not in compiled.params.values()

    session.scalar = AsyncMock(return_value=None)
    missing = await interactions.guild_user_installation(
        session,
        SimpleNamespace(origin_domain="guild.example"),
        clicker,
        payload,
        (12, "apps.example"),
        None,
        ("user_install", 77, "guild.example", 5),
        authority_domain="guild.example",
    )
    assert missing == (None, None, None, None)

    session.get = AsyncMock(return_value=owner)
    interaction = SimpleNamespace(
        user_id=21,
        user_domain="clicker.example",
        invocation_permissions=int(Permission.SEND_MESSAGES),
    )
    automod_owner, permissions = await interactions.user_install_automod_attribution(
        session,
        interaction,
        installation,
    )
    assert automod_owner is owner
    assert permissions == int(Permission.SEND_MESSAGES)


@pytest.mark.asyncio
async def test_callback_rejoins_user_installation_without_requiring_clicker_ownership() -> None:
    result = SimpleNamespace(one_or_none=lambda: None)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    principal = SimpleNamespace(
        require_scope=MagicMock(),
        application=SimpleNamespace(id=12, origin_domain="apps.example"),
        user=SimpleNamespace(id=13, origin_domain="apps.example"),
    )

    with pytest.raises(HTTPException) as missing:
        await interactions.bot_interaction(
            session,
            principal,
            29,
            "interactions.respond",
            authority_domain="guild.example",
        )

    assert missing.value.status_code == 404
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile())
    assert "bot_user_installations.user_id = bot_interactions.user_id" not in sql
    assert "bot_user_installations.user_domain = bot_interactions.user_domain" not in sql
    assert "bot_user_installations.revoked_at IS NULL" in sql
    assert "bot_installations.revoked_at IS NULL" in sql
    assert (
        "bot_user_installations.grant_revision = bot_interactions.installation_revision" not in sql
    )
    assert "bot_installations.grant_revision = bot_interactions.installation_revision" not in sql


def test_callback_scopes_are_frozen_at_interaction_admission() -> None:
    installation = BotUserInstallation(
        id=77,
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        grant_revision=4,
        status="active",
        revoked_at=None,
        granted_scopes=["applications.commands", "interactions.respond"],
        contexts=["private_channel"],
    )
    interaction = SimpleNamespace(
        integration_type="user_install",
        installation_revision=4,
        payload={
            INTERACTION_INSTALLATION_LINEAGE_KEY: installation_authority_lineage(installation)
        },
    )

    installation.grant_revision = 5
    installation.granted_scopes = ["applications.commands"]
    interactions.require_interaction_installation_scope(
        installation,
        interaction,
        "interactions.respond",
    )

    with pytest.raises(HTTPException) as denied:
        interactions.require_interaction_installation_scope(
            installation,
            interaction,
            "attachments.write",
        )
    assert denied.value.detail == {
        "code": "BOT_INSTALLATION_SCOPE_REQUIRED",
        "scope": "attachments.write",
    }


@pytest.mark.asyncio
async def test_interaction_owner_snapshot_supports_guild_and_user_authority() -> None:
    user_installation = BotUserInstallation(
        id=77,
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        grant_revision=4,
        status="active",
        granted_scopes=["applications.commands"],
        contexts=["guild"],
    )
    guild_installation = BotInstallation(
        id=78,
        application_id=12,
        application_domain="apps.example",
        guild_id=2,
        guild_domain="guild.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        grant_revision=5,
        status="active",
        granted_scopes=["applications.commands"],
    )
    context = SimpleNamespace(
        session=SimpleNamespace(scalar=AsyncMock(return_value=guild_installation)),
        source=SimpleNamespace(authority_parent=None, message=None),
        payload=SimpleNamespace(interaction_type="command"),
        application=SimpleNamespace(
            installation=None,
            user_installation=user_installation,
            dm_capability=None,
            command=SimpleNamespace(integration_types=["guild_install", "user_install"]),
            application=SimpleNamespace(id=12, origin_domain="apps.example"),
            bot=SimpleNamespace(id=13, origin_domain="apps.example"),
            interaction_context="guild",
        ),
        access=SimpleNamespace(
            guild=SimpleNamespace(id=2, origin_domain="guild.example"),
            channel=SimpleNamespace(
                id=3,
                origin_domain="guild.example",
                guild_id=2,
                guild_domain="guild.example",
                unavailable=False,
                parent_id=None,
                parent_domain=None,
                type=0,
            ),
        ),
        actor=SimpleNamespace(id=21, origin_domain="clicker.example"),
    )

    owners = await interactions.command_authorizing_integration_owners(context)

    assert owners == {
        "guild_install": "2@guild.example",
        "user_install": "20@owner.example",
    }


@pytest.mark.asyncio
async def test_dm_capability_owner_snapshot_retains_underlying_guild_and_user_install() -> None:
    capability = BotDMCapability(
        id=80,
        authority_domain="dm.example",
        grant_id="kbdg_" + "g" * 43,
        revision=4,
        source_kind="guild",
        source_installation_id=78,
        source_installation_domain="guild.example",
        application_id=12,
        application_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        guild_id=2,
        guild_domain="guild.example",
        installing_user_id=None,
        installing_user_domain=None,
        target_user_id=21,
        target_user_domain="clicker.example",
        granted_scopes=["applications.commands", "interactions.respond"],
    )
    user_installation = BotUserInstallation(
        id=77,
        application_id=12,
        application_domain="apps.example",
        user_id=21,
        user_domain="clicker.example",
        status="active",
        granted_scopes=["applications.commands"],
        contexts=["bot_dm"],
    )
    context = SimpleNamespace(
        session=SimpleNamespace(scalar=AsyncMock(return_value=user_installation)),
        settings=SimpleNamespace(domain="dm.example"),
        source=SimpleNamespace(authority_parent=None, message=None),
        payload=SimpleNamespace(interaction_type="command"),
        application=SimpleNamespace(
            installation=None,
            user_installation=None,
            dm_capability=capability,
            command=SimpleNamespace(integration_types=["guild_install", "user_install"]),
            application=SimpleNamespace(id=12, origin_domain="apps.example"),
            bot=SimpleNamespace(id=13, origin_domain="apps.example"),
            interaction_context="bot_dm",
        ),
        access=SimpleNamespace(
            guild=None,
            channel=SimpleNamespace(id=7, origin_domain="dm.example"),
        ),
        actor=SimpleNamespace(id=21, origin_domain="clicker.example"),
    )

    owners = await interactions.command_authorizing_integration_owners(context)
    app_permissions = await interactions.interaction_application_permissions_snapshot(context)
    lineage = installation_authority_lineage(capability)

    assert owners == {
        "guild_install": "0",
        "user_install": "21@clicker.example",
    }
    assert lineage["owner_ref"] == "2@guild.example"
    assert lineage["installation_ref"] == "78@guild.example"
    assert lineage["dm_capability_ref"] == "80@dm.example"
    assert app_permissions == int(
        Permission.ATTACH_FILES
        | Permission.EMBED_LINKS
        | Permission.MENTION_EVERYONE
        | Permission.USE_EXTERNAL_EMOJIS
    )


@pytest.mark.asyncio
async def test_interaction_event_snapshot_uses_guild_member_and_opaque_source_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(
        id=21,
        origin_domain="clicker.example",
        username="clicker",
        display_name="Clicker",
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=2,
        e2ee_device_generation=0,
        profile_resolved=True,
        account_type="human",
    )
    member = SimpleNamespace(
        guild_id=2,
        guild_domain="guild.example",
        nickname=None,
        joined_at=datetime(2026, 8, 1, tzinfo=UTC),
        temporary=False,
        timeout_until=None,
        timeout_indefinite=False,
        voice_flags=0,
        member_version=3,
    )
    user_installation = BotUserInstallation(
        id=77,
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="owner.example",
        grant_revision=4,
        status="active",
        granted_scopes=["applications.commands"],
        contexts=["guild"],
    )
    context = SimpleNamespace(
        session=SimpleNamespace(
            get=AsyncMock(return_value=member),
            scalars=AsyncMock(return_value=[30, 31]),
        ),
        redis=SimpleNamespace(),
        settings=SimpleNamespace(media_max_attachment_bytes=8_388_608),
        source=SimpleNamespace(authority_parent=None, message=None),
        payload=SimpleNamespace(interaction_type="component"),
        application=SimpleNamespace(
            installation=None,
            user_installation=user_installation,
            dm_capability=None,
            command=None,
            application=SimpleNamespace(id=12, origin_domain="apps.example"),
            bot=SimpleNamespace(id=13, origin_domain="apps.example"),
            interaction_context="guild",
        ),
        access=SimpleNamespace(
            guild=SimpleNamespace(
                id=2,
                origin_domain="guild.example",
                preferred_locale="ko",
            ),
            channel=SimpleNamespace(),
        ),
        actor=actor,
        invoker_policy=SimpleNamespace(locale="fr"),
        invocation_permissions=int(Permission.SEND_MESSAGES),
    )

    source_message = SimpleNamespace(id=40, e2ee={"ciphertext": "opaque"})
    rendered_source = {
        "id": "40",
        "origin_domain": "guild.example",
        "channel_id": "7",
        "channel_domain": "guild.example",
        "content": None,
        "e2ee": {"ciphertext": "opaque"},
    }
    render = AsyncMock(return_value=rendered_source)
    monkeypatch.setattr(interactions, "render_message_payload", render)

    snapshot = await interactions.interaction_event_snapshot(context, source_message)

    assert "user" not in snapshot
    member_projection = cast(dict[str, object], snapshot["member"])
    member_user = cast(dict[str, object], member_projection["user"])
    assert member_user["id"] == "21"
    assert member_projection["permissions"] == str(int(Permission.SEND_MESSAGES))
    assert member_projection["role_ids"] == ["30", "31"]
    assert snapshot["guild_locale"] == "ko"
    assert snapshot["locale"] == "fr"
    assert snapshot["attachment_size_limit"] == 8_388_608
    assert snapshot["message"] is rendered_source
    assert snapshot["authorizing_integration_owners"] == {"user_install": "20@owner.example"}
    render.assert_awaited_once_with(context.session, source_message, viewer=actor)


@pytest.mark.asyncio
async def test_ephemeral_component_snapshot_includes_safe_nondurable_source_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    response = SimpleNamespace(
        id=40,
        sequence=0,
        revision=2,
        created_at=now,
    )
    parent = SimpleNamespace(
        id=30,
        application_id=5,
        application_domain="apps.example",
        channel_id=7,
        channel_domain="channel.example",
        created_at=now,
    )
    context = SimpleNamespace(
        session=SimpleNamespace(),
        settings=SimpleNamespace(media_max_attachment_bytes=8_388_608),
        access=SimpleNamespace(guild=None),
        actor=SimpleNamespace(id=1, origin_domain="home.example"),
        invoker_policy=SimpleNamespace(locale="en-US"),
        application=SimpleNamespace(
            application=SimpleNamespace(id=5, origin_domain="apps.example"),
            bot=SimpleNamespace(id=6, origin_domain="apps.example"),
        ),
    )
    monkeypatch.setattr(
        interactions,
        "interaction_application_permissions_snapshot",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        interactions,
        "command_authorizing_integration_owners",
        AsyncMock(return_value={"user_install": "1@home.example"}),
    )
    monkeypatch.setattr(
        interactions,
        "interaction_invoker_event_projection",
        AsyncMock(
            return_value={
                "user": {
                    "id": "1",
                    "origin_domain": "home.example",
                    "username": "member",
                }
            }
        ),
    )
    monkeypatch.setattr(
        interactions,
        "interaction_message_metadata",
        AsyncMock(return_value={"id": "30", "type": 2}),
    )
    monkeypatch.setattr(
        interactions,
        "interaction_response_event_payload",
        lambda *_args: {
            "data": {
                "content": "Private controls",
                "e2ee": None,
                "embeds": [],
                "components": [
                    {
                        "type": 1,
                        "components": [{"type": 2, "custom_id": "next"}],
                    }
                ],
                "attachments": [{"id": "50", "origin_domain": "home.example"}],
                "poll": None,
                "flags": (1 << 6) | (1 << 15),
                "tts": False,
                "view_version": 3,
                "view_expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        },
    )
    monkeypatch.setattr(
        interactions,
        "user_payload",
        lambda _user: {
            "id": "6",
            "origin_domain": "apps.example",
            "username": "bot",
            "bot": True,
        },
    )

    snapshot = await interactions.interaction_event_snapshot(
        cast(Any, context),
        None,
        cast(Any, (response, parent)),
    )
    source = cast(dict[str, object], snapshot["message"])

    assert source["id"] == "40"
    assert source["response_ref"] == "40@channel.example"
    assert source["interaction_ref"] == "30@channel.example"
    assert source["channel_ref"] == "7@channel.example"
    assert source["application_ref"] == "5@apps.example"
    assert source["author_id"] == "6"
    assert source["content"] == "Private controls"
    assert source["components"]
    assert source["attachments"]
    assert source["flags"] == (1 << 6) | (1 << 15)
    assert source["view_version"] == 3
    assert source["ephemeral"] is True
    assert source["durable"] is False
    assert "message_ref" not in source


@pytest.mark.asyncio
async def test_modal_submit_resolves_its_ephemeral_component_source() -> None:
    now = datetime.now(UTC)
    source_response = SimpleNamespace(
        id=40,
        ephemeral=True,
        deleted_at=None,
    )
    source_parent = SimpleNamespace(
        id=30,
        application_id=5,
        application_domain="apps.example",
        user_id=1,
        user_domain="home.example",
        channel_id=7,
        channel_domain="channel.example",
        expires_at=now + timedelta(minutes=10),
    )
    modal_parent = SimpleNamespace(
        message_id=None,
        message_domain=None,
        payload={"response_id": "40", "view_version": "3"},
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(one_or_none=lambda: (source_response, source_parent))
        )
    )
    context = SimpleNamespace(
        session=session,
        source=SimpleNamespace(
            message=None,
            ephemeral_response=None,
            ephemeral_parent=None,
            modal_parent=modal_parent,
        ),
        application=SimpleNamespace(
            application=SimpleNamespace(id=5, origin_domain="apps.example")
        ),
        actor=SimpleNamespace(id=1, origin_domain="home.example"),
        access=SimpleNamespace(channel=SimpleNamespace(id=7, origin_domain="channel.example")),
    )

    message, response_id, view_version = await interactions.resolve_modal_source_message(
        cast(Any, context)
    )
    resolved = await interactions.resolve_ephemeral_source_response(
        cast(Any, context),
        response_id,
    )

    assert message is None
    assert response_id == 40
    assert view_version == 3
    assert resolved == (source_response, source_parent)


@pytest.mark.asyncio
async def test_public_component_rejects_tampered_lineage_authority() -> None:
    access = SimpleNamespace(
        channel=SimpleNamespace(origin_domain="guild.example"),
        guild=SimpleNamespace(),
    )
    with pytest.raises(HTTPException) as denied:
        await interactions.resolve_interaction_application(
            SimpleNamespace(),
            access,
            SimpleNamespace(id=21, origin_domain="clicker.example"),
            SimpleNamespace(),
            (12, "apps.example"),
            None,
            ("user_install", 77, "forged.example", 4),
            authority_domain="guild.example",
        )
    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "APPLICATION_COMMAND_NOT_FOUND"}


@pytest.mark.asyncio
async def test_federated_private_component_keeps_user_install_owner_across_clickers() -> None:
    installation = BotUserInstallation(
        id=77,
        source_id=77,
        source_domain="dm-authority.example",
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="dm-authority.example",
        grant_revision=4,
        status="active",
        granted_scopes=["applications.commands"],
        contexts=["private_channel"],
    )
    application = SimpleNamespace(
        id=12,
        origin_domain="apps.example",
        bot_user_id=13,
        bot_user_domain="apps.example",
        status="active",
    )
    bot = SimpleNamespace(
        id=13,
        origin_domain="apps.example",
        account_type="bot",
        disabled_at=None,
    )

    async def get_model(model: type[object], _identity: object) -> object:
        if model.__name__ == "BotApplication":
            return application
        if model.__name__ == "User":
            return bot
        if model.__name__ == "DMConversation":
            return SimpleNamespace(type="group")
        raise AssertionError(f"unexpected model lookup: {model}")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=installation),
        get=AsyncMock(side_effect=get_model),
    )
    clicker = SimpleNamespace(id=21, origin_domain="clicker.example")
    payload = SimpleNamespace(
        interaction_type="component",
        integration_type=None,
    )
    access = ChannelAccess(
        channel=SimpleNamespace(id=33, origin_domain="dm-authority.example"),
        guild=None,
        participants=[clicker],
    )

    resolved = await interactions.resolve_private_interaction_application(
        session,
        access,
        clicker,
        payload,
        (12, "apps.example"),
        None,
        ("user_install", 77, "dm-authority.example", 4),
        authority_domain="chat.example",
    )

    assert resolved.user_installation is installation
    statement = session.scalar.await_args.args[0]
    compiled = statement.compile()
    sql = str(compiled)
    assert "bot_user_installations.id =" in sql
    assert "bot_user_installations.grant_revision =" in sql
    assert "bot_user_installations.user_id =" not in sql
    assert 77 in compiled.params.values()
    assert 4 in compiled.params.values()
    assert 21 not in compiled.params.values()
    assert "chat.example" in compiled.params.values()
    assert "bot_user_installations.authority_expires_at" in sql

    session.scalar = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as revoked:
        await interactions.resolve_private_interaction_application(
            session,
            access,
            clicker,
            payload,
            (12, "apps.example"),
            None,
            ("user_install", 77, "dm-authority.example", 5),
            authority_domain="chat.example",
        )
    assert revoked.value.status_code == 404
    assert revoked.value.detail == {"code": "APPLICATION_COMMAND_NOT_FOUND"}


@pytest.mark.asyncio
async def test_concurrent_acknowledgements_have_exactly_one_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = SimpleNamespace(
        id=10,
        status="pending",
        interaction_type="command",
        payload={},
        message_id=None,
        message_domain=None,
        callback_type=None,
        acknowledged_at=None,
        responded_at=None,
        response_message_id=None,
        response_message_domain=None,
    )
    installation = SimpleNamespace()
    transaction_lock = asyncio.Lock()

    async def locked_interaction(
        *args: object,
        **kwargs: object,
    ) -> tuple[object, object]:
        del args, kwargs
        await transaction_lock.acquire()
        return interaction, installation

    class Session:
        def __init__(self) -> None:
            self.rows: list[object] = []
            self.commits = 0

        def add(self, row: object) -> None:
            self.rows.append(row)

        async def execute(self, _statement: object) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1
            transaction_lock.release()

    monkeypatch.setattr(interactions, "bot_interaction", locked_interaction)
    monkeypatch.setattr(
        interactions,
        "publish_interaction_response_event",
        AsyncMock(),
    )
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    sessions = [Session(), Session()]

    async def acknowledge(session: Session, response_id: int) -> object:
        return await interactions.callback_interaction(
            10,
            interactions.InteractionCallback(type=5),
            Response(),
            SimpleNamespace(),
            session,
            SimpleNamespace(),
            SimpleNamespace(mint=AsyncMock(return_value=response_id)),
            SimpleNamespace(domain="chat.example"),
        )

    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                acknowledge(sessions[0], 30),
                acknowledge(sessions[1], 31),
                return_exceptions=True,
            ),
            timeout=2,
        )
    finally:
        if transaction_lock.locked():
            transaction_lock.release()

    assert sum(isinstance(result, Response) for result in results) == 1
    failures = [result for result in results if isinstance(result, HTTPException)]
    assert len(failures) == 1
    assert failures[0].status_code == 409
    assert failures[0].detail["code"] == "INTERACTION_ALREADY_ACKNOWLEDGED"
    assert sum(session.commits for session in sessions) == 1
    assert sum(len(session.rows) for session in sessions) == 1


class RecordingMessagePostCommit:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def publish(self, *args: object) -> None:
        del args
        self.events.append("message_postcommit")


@pytest.mark.asyncio
async def test_public_callback_commits_message_and_response_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    metadata = {"interaction_ref": "10@chat.example"}
    monkeypatch.setattr(
        interactions,
        "interaction_message_metadata",
        AsyncMock(return_value=metadata),
    )
    events: list[str] = []
    interaction = SimpleNamespace(
        id=10,
        status="pending",
        interaction_type="command",
        payload={},
        message_id=None,
        message_domain=None,
        channel_id=20,
        channel_domain="chat.example",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        callback_type=None,
        acknowledged_at=None,
        responded_at=None,
        response_message_id=None,
        response_message_domain=None,
    )
    principal = public_principal()
    assert principal.application.origin_domain != interaction.channel_domain
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, public_installation())),
    )

    async def create(*args: object) -> dict[str, object]:
        events.append("message_staged")
        assert cast(EntityRef, args[0]).resolve("unused.example") == (20, "chat.example")
        assert cast(Any, args[7]).domain == interaction.channel_domain
        options = args[8]
        assert isinstance(options, interactions.MessageAdmissionOptions)
        assert options.interaction_metadata is metadata
        assert options.transaction is not None
        options.transaction.stage(RecordingMessagePostCommit(events))  # type: ignore[arg-type]
        return {"id": "40", "origin_domain": "chat.example"}

    async def publish(*args: object) -> None:
        del args
        events.append("interaction_projection")

    class Session:
        def __init__(self) -> None:
            self.rows: list[object] = []

        def add(self, row: object) -> None:
            self.rows.append(row)

        async def execute(self, _statement: object) -> None:
            return None

        async def commit(self) -> None:
            events.append("commit")

    monkeypatch.setattr(interactions, "create_message", create)
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    session = Session()

    result = await interactions.callback_interaction(
        10,
        interactions.InteractionCallback(type=4, data={"content": "done"}),
        Response(),
        principal,
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=30)),
        SimpleNamespace(domain="chat.example"),
        True,
    )

    assert events == [
        "message_staged",
        "commit",
        "message_postcommit",
        "interaction_projection",
    ]
    assert isinstance(result, dict)
    message = result["resource"]["message"]
    assert message["id"] == "40"
    assert message["response_id"] == "30"
    stored = session.rows[0]
    assert stored.message_id == 40
    assert interaction.response_message_id == 40


@pytest.mark.asyncio
async def test_public_deferred_original_commits_message_and_state_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    metadata = {"interaction_ref": "10@chat.example"}
    monkeypatch.setattr(
        interactions,
        "interaction_message_metadata",
        AsyncMock(return_value=metadata),
    )
    events: list[str] = []
    interaction = SimpleNamespace(
        id=10,
        status="deferred",
        responded_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        channel_id=20,
        channel_domain="chat.example",
        response_message_id=None,
        response_message_domain=None,
    )
    principal = public_principal()
    assert principal.application.origin_domain != interaction.channel_domain
    stored = SimpleNamespace(
        id=30,
        response_type=5,
        ephemeral=False,
        message_id=None,
        message_domain=None,
        payload={},
    )
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, public_installation())),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )

    async def create(*args: object) -> dict[str, object]:
        events.append("message_staged")
        assert cast(EntityRef, args[0]).resolve("unused.example") == (20, "chat.example")
        assert cast(Any, args[7]).domain == interaction.channel_domain
        options = args[8]
        assert isinstance(options, interactions.MessageAdmissionOptions)
        assert options.interaction_metadata is metadata
        assert options.transaction is not None
        options.transaction.stage(RecordingMessagePostCommit(events))  # type: ignore[arg-type]
        return {"id": "40", "origin_domain": "chat.example"}

    async def publish(*args: object) -> None:
        del args
        events.append("interaction_projection")

    session = SimpleNamespace(commit=AsyncMock(side_effect=lambda: events.append("commit")))
    monkeypatch.setattr(interactions, "create_message", create)
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)

    result = await interactions.edit_original_interaction_response(
        10,
        interactions.InteractionResponseEdit(content="done"),
        principal,
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert events == [
        "message_staged",
        "commit",
        "message_postcommit",
        "interaction_projection",
    ]
    assert result == {"id": "40", "origin_domain": "chat.example"}
    assert stored.message_id == 40
    assert interaction.status == "responded"


@pytest.mark.asyncio
async def test_public_followup_commits_message_and_response_before_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    metadata = {"interaction_ref": "10@chat.example"}
    monkeypatch.setattr(
        interactions,
        "interaction_message_metadata",
        AsyncMock(return_value=metadata),
    )
    events: list[str] = []
    interaction = SimpleNamespace(
        id=10,
        status="responded",
        channel_id=20,
        channel_domain="chat.example",
    )
    principal = public_principal()
    assert principal.application.origin_domain != interaction.channel_domain
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, public_installation())),
    )

    async def create(*args: object) -> dict[str, object]:
        events.append("message_staged")
        assert cast(EntityRef, args[0]).resolve("unused.example") == (20, "chat.example")
        assert cast(Any, args[7]).domain == interaction.channel_domain
        options = args[8]
        assert isinstance(options, interactions.MessageAdmissionOptions)
        assert options.interaction_metadata is metadata
        assert options.transaction is not None
        options.transaction.stage(RecordingMessagePostCommit(events))  # type: ignore[arg-type]
        return {"id": "40", "origin_domain": "chat.example"}

    async def publish(*args: object) -> None:
        del args
        events.append("interaction_projection")

    stored_rows: list[object] = []
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=0),
        add=lambda row: stored_rows.append(row),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    monkeypatch.setattr(interactions, "create_message", create)
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    monkeypatch.setattr(
        interactions,
        "interaction_response_payload",
        AsyncMock(return_value={"id": "40", "response_id": "30"}),
    )

    result = await interactions.create_interaction_followup(
        10,
        interactions.InteractionFollowup(message={"content": "more"}),
        Response(),
        principal,
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=30)),
        SimpleNamespace(domain="chat.example"),
    )

    assert events == [
        "message_staged",
        "commit",
        "message_postcommit",
        "interaction_projection",
    ]
    assert result == {"id": "40", "response_id": "30"}
    assert stored_rows[0].message_id == 40


@pytest.mark.asyncio
async def test_private_source_authority_expires_with_its_parent() -> None:
    now = datetime.now(UTC)
    interaction = SimpleNamespace(
        application_id=1,
        application_domain="apps.example",
        user_id=2,
        user_domain="chat.example",
        channel_id=3,
        channel_domain="chat.example",
        payload={"view_version": 1},
    )
    source = SimpleNamespace(
        ephemeral=True,
        deleted_at=None,
        payload={"view_version": 1},
    )
    parent = SimpleNamespace(
        application_id=1,
        application_domain="apps.example",
        user_id=2,
        user_domain="chat.example",
        channel_id=3,
        channel_domain="chat.example",
        expires_at=now - timedelta(seconds=1),
    )
    result = SimpleNamespace(one_or_none=lambda: (source, parent))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException) as expired:
        await interactions.source_ephemeral_response(
            session,
            interaction,
            20,
            for_update=True,
        )

    assert expired.value.status_code == 409
    assert expired.value.detail["code"] == "INTERACTION_VIEW_INVALID"


@pytest.mark.asyncio
async def test_deferred_update_acknowledges_exact_private_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    interaction = SimpleNamespace(
        id=10,
        status="pending",
        interaction_type="component",
        payload={"response_id": "20", "view_version": 4},
        message_id=None,
        message_domain=None,
        callback_type=None,
        acknowledged_at=None,
        responded_at=None,
        response_message_id=None,
        response_message_domain=None,
    )
    source = SimpleNamespace(id=20, payload={"view_version": 4})
    installation = SimpleNamespace()
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, installation)),
    )
    source_lookup = AsyncMock(return_value=(source, SimpleNamespace()))
    monkeypatch.setattr(interactions, "source_ephemeral_response", source_lookup)
    publish = AsyncMock()
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    stored_rows: list[object] = []
    session = SimpleNamespace(
        add=lambda row: stored_rows.append(row),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )

    result = await interactions.callback_interaction(
        10,
        interactions.InteractionCallback(type=6),
        Response(),
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=30)),
        SimpleNamespace(domain="chat.example"),
    )

    assert result.status_code == 204
    assert interaction.status == "deferred"
    assert interaction.callback_type == 6
    assert interaction.responded_at is None
    stored = stored_rows[0]
    assert stored.response_type == 6
    assert stored.ephemeral is True
    assert stored.payload == {"source_response_id": "20", "view_version": 4}
    source_lookup.assert_awaited_once_with(session, interaction, 20, for_update=True)
    session.commit.assert_awaited_once()
    publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_immediate_private_update_returns_and_rebinds_exact_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    now = datetime.now(UTC)
    interaction = SimpleNamespace(
        id=10,
        status="pending",
        interaction_type="component",
        payload={"response_id": "40", "view_version": 2},
        message_id=None,
        message_domain=None,
        callback_type=None,
        acknowledged_at=None,
        responded_at=None,
        response_message_id=None,
        response_message_domain=None,
        expires_at=now + timedelta(minutes=10),
    )
    source = SimpleNamespace(
        id=40,
        sequence=0,
        response_type=4,
        ephemeral=True,
        deleted_at=None,
        payload={
            "content": "before",
            "flags": 64,
            "attachments": [],
            "view_version": 2,
            "view_persistent": False,
            "view_expires_at": (now + timedelta(minutes=5)).isoformat(),
        },
    )
    source_parent = SimpleNamespace(
        id=5,
        channel_id=20,
        channel_domain="chat.example",
        expires_at=now + timedelta(minutes=5),
    )
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, SimpleNamespace())),
    )
    source_lookup = AsyncMock(return_value=(source, source_parent))
    monkeypatch.setattr(interactions, "source_ephemeral_response", source_lookup)
    publish = AsyncMock()
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    stored_rows: list[object] = []
    session = SimpleNamespace(
        add=lambda row: stored_rows.append(row),
        execute=AsyncMock(),
        commit=AsyncMock(),
    )
    components = rows(Button(custom_id="next", label="Next"))

    result = await interactions.callback_interaction(
        10,
        interactions.InteractionCallback(
            type=7,
            data={
                "content": "after",
                "components": components,
                "view_version": 2,
            },
        ),
        Response(),
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=30)),
        SimpleNamespace(domain="chat.example"),
        True,
    )

    assert isinstance(result, dict)
    message = result["resource"]["message"]
    stored = stored_rows[0]
    assert message["id"] == "40"
    assert message["interaction_id"] == "10"
    assert message["content"] == "after"
    assert message["view_version"] == 3
    assert stored.response_type == 7
    assert stored.payload == {"source_response_id": "40", "view_version": 3}
    assert interaction.payload == {"response_id": "40", "view_version": 3}
    assert interaction.status == "responded"
    assert publish.await_count == 2


@pytest.mark.asyncio
async def test_deferred_update_edits_the_exact_source_message_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    now = datetime.now(UTC)
    interaction = SimpleNamespace(
        id=10,
        status="deferred",
        responded_at=None,
        expires_at=now + timedelta(minutes=10),
        channel_id=20,
        channel_domain="chat.example",
    )
    stored = SimpleNamespace(
        id=30,
        response_type=6,
        ephemeral=False,
        message_id=40,
        message_domain="chat.example",
        payload={},
    )
    installation = SimpleNamespace(id=50)
    message = SimpleNamespace(
        id=40,
        origin_domain="chat.example",
        channel_id=20,
        channel_domain="chat.example",
    )
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, installation)),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )
    edit = AsyncMock(return_value={"id": "40", "content": "updated"})
    publish = AsyncMock()
    monkeypatch.setattr(interactions, "edit_message", edit)
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    session = SimpleNamespace(get=AsyncMock(return_value=message), commit=AsyncMock())

    result = await interactions.edit_original_interaction_response(
        10,
        interactions.InteractionResponseEdit(content="updated"),
        SimpleNamespace(
            application=SimpleNamespace(id=1, origin_domain="chat.example"),
            worker=SimpleNamespace(id=3),
        ),
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert result == {"id": "40", "content": "updated"}
    assert interaction.status == "responded"
    assert interaction.responded_at is not None
    edit.assert_awaited_once()
    assert isinstance(edit.await_args.args[2], MessageEdit)
    session.commit.assert_awaited_once()
    publish.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_type", "interaction_status"),
    [(6, "deferred"), (7, "responded")],
)
async def test_update_original_edits_exact_private_source_and_advances_version(
    monkeypatch: pytest.MonkeyPatch,
    response_type: int,
    interaction_status: str,
) -> None:
    monkeypatch.setattr(
        interactions,
        "queue_interaction_response_relays",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        interactions,
        "require_interaction_response_encryption",
        AsyncMock(),
    )
    now = datetime.now(UTC)
    interaction = SimpleNamespace(
        id=10,
        status=interaction_status,
        responded_at=None,
        expires_at=now + timedelta(minutes=10),
        channel_id=20,
        channel_domain="chat.example",
        payload={"response_id": "40", "view_version": 2},
    )
    stored = SimpleNamespace(
        id=30,
        response_type=response_type,
        ephemeral=True,
        message_id=None,
        message_domain=None,
        payload={"source_response_id": "40", "view_version": 2},
    )
    source = SimpleNamespace(
        id=40,
        payload={
            "content": "before",
            "flags": 64,
            "attachments": [],
            "view_version": 2,
            "view_persistent": False,
            "view_expires_at": (now + timedelta(minutes=5)).isoformat(),
        },
    )
    source_parent = SimpleNamespace(expires_at=now + timedelta(minutes=5))
    monkeypatch.setattr(
        interactions,
        "bot_interaction",
        AsyncMock(return_value=(interaction, SimpleNamespace())),
    )
    monkeypatch.setattr(
        interactions,
        "stored_interaction_response",
        AsyncMock(return_value=stored),
    )
    source_lookup = AsyncMock(return_value=(source, source_parent))
    monkeypatch.setattr(interactions, "source_ephemeral_response", source_lookup)
    publish = AsyncMock()
    monkeypatch.setattr(interactions, "publish_interaction_response_event", publish)
    monkeypatch.setattr(
        interactions,
        "interaction_response_payload",
        AsyncMock(return_value={"id": "40", "content": "after", "ephemeral": True}),
    )
    session = SimpleNamespace(commit=AsyncMock())

    result = await interactions.edit_original_interaction_response(
        10,
        interactions.InteractionResponseEdit(
            content="after",
            components=[ActionRow(components=[Button(custom_id="next", label="Next")])],
        ),
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="chat.example"),
    )

    assert result["interaction_id"] == "10"
    assert source.payload["content"] == "after"
    assert source.payload["view_version"] == 3
    assert stored.payload == {"source_response_id": "40", "view_version": 3}
    assert interaction.payload["view_version"] == 3
    assert interaction.status == "responded"
    assert publish.await_count == 2
