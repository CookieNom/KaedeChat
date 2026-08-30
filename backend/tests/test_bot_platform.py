import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

import app.api.applications as applications_api
import app.api.bots as bots_api
import app.api.moderation as moderation_api
import app.gateway as gateway
from app.admin.auth import ROLE_CAPABILITIES, AdminPrincipal
from app.api.application_assets import _bot_write_access
from app.api.applications import (
    SUPPORTED_INTENTS,
    SUPPORTED_SCOPES,
    ApplicationCreate,
    ApplicationPatch,
    CommandDefinition,
    CommandOptionDefinition,
    CommandsPut,
    CredentialCreate,
    FederatedBotInstallRequest,
    FederatedBotInstallResult,
    FederatedBotUninstallRequest,
    TemplateCreate,
    WorkerCreate,
    WorkerTokenRequest,
    _uninstall_bot_from_local_guild,
    bot_username,
    create_bot_token,
    ensure_bot_install_allowed,
    ensure_personal_developer_team,
    federated_human_installer,
    install_bot,
    list_guild_bot_integrations,
    normalize_values,
    team_payload,
    uninstall_bot,
    validate_command_install_types,
)
from app.api.bot_federation import (
    BotManifest,
    ManifestApplication,
    ManifestApplicationEmoji,
    ManifestTemplate,
    ManifestWorker,
    activate_remote_application_if_permitted,
    enabled_bot_identity,
    federation_worker_authorization,
    local_manifest,
    materialize_remote_manifest,
    refresh_remote_worker_authorization,
    restore_remote_worker_if_new,
)
from app.api.bot_gateway import (
    GatewayAuthorizationGuard,
    GatewayAuthorizationState,
    GatewayBootstrap,
    GatewayGuildAuthorization,
    GatewayInstallationGrant,
    GatewayProtocolError,
    _active_bot_event_participation,
    canonical_direct_channel_tombstone,
    current_bot_e2ee_event_access,
    current_direct_event_access,
    current_gateway_authorization,
    direct_event_channel_reference,
    disclose_current_event,
    encrypted_bot_content_channel_refs,
    encrypted_bot_content_event,
    encrypted_direct_channels,
    encrypted_message_event,
    event_intent,
    event_scope,
    filtered_event,
    gateway_authorization_fingerprint,
    gateway_effective_permissions,
    gateway_ready_event,
    gateway_topic_grants,
    guild_context_from_topic,
    load_gateway_bootstrap,
    normalized_bot_event_type,
    replay_topic,
    requested_gateway_e2ee_device_id,
    resume_cursors,
)
from app.api.bots import (
    bot_guilds,
    exact_installation_by_id,
    redact_bot_message_payload,
    redact_bot_thread_payload,
)
from app.api.interactions import (
    InteractionCreate,
    InteractionResponse,
    _local_application_commands,
    command_attachment_ids,
    command_channel_type_requirements,
    defer_interaction,
    respond_interaction,
    validate_command_options,
    validate_resolved_command_channel_types,
)
from app.bots.auth import (
    BOT_APPLICATION_REQUEST_LIMIT,
    BOT_WORKER_REQUEST_LIMIT,
    BotPrincipal,
    decode_urlsafe,
    dpop_message,
    encode_urlsafe,
    require_application_home_bot,
    require_bot,
    worker_assertion_message,
)
from app.bots.installations import (
    active_installation_exists,
    cleanup_installation_roles,
    installation_has_membership,
    revoke_installations_for_guild_instance,
    revoke_installations_for_guild_member,
)
from app.bots.target_contract import target_policy_allows
from app.chat.schemas import BanCreate, InstanceBanCreate, MessageCreate
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import (
    ApplicationCommand,
    ApplicationEmoji,
    BotApplication,
    BotDMCapability,
    BotE2EEDevice,
    BotInstallation,
    BotInstallTemplate,
    BotInteraction,
    BotToken,
    BotWorker,
    DeveloperTeam,
    DeveloperTeamMember,
)
from app.db.models import (
    Attachment,
    Channel,
    Guild,
    GuildMember,
    InstanceBlock,
    MemberRole,
    Message,
    Role,
    Sticker,
    User,
)
from app.federation.network import FederationNetworkError


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


def test_application_media_writes_are_home_instance_only() -> None:
    bot = principal(scopes={"applications.emojis.manage"}, intents=set())

    with pytest.raises(HTTPException) as denied:
        _bot_write_access(
            bot,
            SimpleNamespace(domain="local.example"),
            "applications.emojis.manage",
        )

    assert denied.value.status_code == 409
    assert denied.value.detail == {
        "code": "APPLICATION_HOME_INSTANCE_REQUIRED",
        "message": (
            "Application assets and emoji can only be changed on the application's home instance."
        ),
        "home_domain": "apps.example",
    }


def test_bot_username_is_normal_account_format_and_unique_suffix() -> None:
    assert bot_username("Weather Bot!", 123456789012345678) == "weather_bot_12345678"
    assert len(bot_username("x" * 100, 123456789012345678)) <= 32


def test_personal_team_payload_has_a_stable_product_name() -> None:
    team = DeveloperTeam(
        id=1,
        origin_domain="local.example",
        name="Old display name's applications",
        personal=True,
    )
    team.created_at = datetime.now(UTC)
    assert team_payload(team, "owner")["name"] == "Personal"


@pytest.mark.asyncio
async def test_personal_team_is_provisioned_for_every_local_human() -> None:
    user = User(
        id=7,
        origin_domain="local.example",
        is_local=True,
        account_type="human",
        username="alice",
        password_hash="hash",
    )
    result = Mock()
    result.one_or_none.return_value = None
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=result),
        add_all=Mock(),
        flush=AsyncMock(),
    )
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=99))

    team, member = await ensure_personal_developer_team(
        session,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(user=user),
        snowflake,
    )

    assert team.name == "Personal"
    assert team.personal is True
    assert member.role == "owner"
    assert (member.user_id, member.user_domain) == (7, "local.example")
    session.add_all.assert_called_once_with([team, member])
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_personal_team_name_is_normalized() -> None:
    user = User(
        id=7,
        origin_domain="local.example",
        is_local=True,
        account_type="human",
        username="alice",
        password_hash="hash",
    )
    team = DeveloperTeam(
        id=8,
        origin_domain="local.example",
        name="Alice's applications",
        personal=True,
    )
    member = DeveloperTeamMember(
        team_id=8,
        team_domain="local.example",
        user_id=7,
        user_domain="local.example",
        user_is_local=True,
        role="owner",
    )
    result = Mock()
    result.one_or_none.return_value = (team, member)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=result),
    )

    resolved_team, resolved_member = await ensure_personal_developer_team(
        session,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(user=user),
        SimpleNamespace(mint=AsyncMock()),
    )

    assert resolved_team is team
    assert resolved_team.name == "Personal"
    assert resolved_member.role == "owner"


def test_scope_and_worker_validation_is_fail_closed() -> None:
    assert normalize_values(
        ["messages.send", "messages.send"], frozenset({"messages.send"}), "scope"
    ) == ["messages.send"]
    with pytest.raises(ValueError, match="unsupported scope"):
        normalize_values(["administrator"], frozenset({"messages.send"}), "scope")
    with pytest.raises(ValidationError):
        WorkerCreate(name="x", public_key="A" * 43, scopes=["unknown"], intents=[])


@pytest.mark.parametrize("installer_id", [True, 1, "01", str(1 << 63)])
def test_federated_bot_install_actor_id_is_a_canonical_snowflake(installer_id: object) -> None:
    with pytest.raises(ValidationError):
        FederatedBotInstallRequest.model_validate(
            {
                "installer_id": installer_id,
                "application_ref": "2@app.example",
                "template_slug": "default",
            }
        )
    with pytest.raises(ValidationError):
        FederatedBotUninstallRequest.model_validate(
            {
                "installer_id": installer_id,
                "application_ref": "2@app.example",
            }
        )

    with pytest.raises(ValidationError):
        FederatedBotInstallRequest.model_validate(
            {
                "installer_id": "1",
                "application_ref": "2@app.example",
                "template_slug": "default",
                "unexpected": True,
            }
        )


def test_federated_bot_install_contract_requires_qualified_resource_refs() -> None:
    with pytest.raises(ValidationError):
        FederatedBotInstallRequest.model_validate(
            {
                "installer_id": "1",
                "application_ref": "2",
                "template_slug": "default",
            }
        )
    with pytest.raises(ValidationError):
        FederatedBotUninstallRequest.model_validate({"installer_id": "1", "application_ref": "2"})
    valid_result = {
        "id": "3",
        "status": "active",
        "application_ref": "2@app.example",
        "guild_ref": "4@guild.example",
        "channel_restrictions": [],
        "grant_revision": "1",
    }
    FederatedBotInstallResult.model_validate(valid_result)
    for field in ("application_ref", "guild_ref"):
        with pytest.raises(ValidationError):
            FederatedBotInstallResult.model_validate(valid_result | {field: "2"})


@pytest.mark.asyncio
async def test_federated_installer_must_be_an_active_remote_human() -> None:
    principal = SimpleNamespace(origin="remote.example")
    valid = User(
        id=7,
        origin_domain=principal.origin,
        is_local=False,
        account_type="human",
        username="valid",
        password_hash=None,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=valid))
    assert await federated_human_installer(session, principal, "7") is valid

    for invalid in (
        User(
            id=7,
            origin_domain=principal.origin,
            is_local=True,
            account_type="human",
            username="local",
            password_hash=None,
        ),
        User(
            id=7,
            origin_domain=principal.origin,
            is_local=False,
            account_type="bot",
            username="bot",
            password_hash=None,
        ),
        User(
            id=7,
            origin_domain=principal.origin,
            is_local=False,
            account_type="human",
            username="disabled",
            password_hash=None,
            disabled_at=datetime.now(UTC),
        ),
    ):
        session.get.return_value = invalid
        with pytest.raises(HTTPException) as denied:
            await federated_human_installer(session, principal, "7")
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_remote_bot_install_and_uninstall_qualify_application_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_user = User(
        id=7,
        origin_domain="home.example",
        is_local=True,
        account_type="human",
        username="installer",
        password_hash=None,
    )
    auth = SimpleNamespace(user=local_user)
    settings = SimpleNamespace(domain=local_user.origin_domain)
    signed = AsyncMock(
        return_value=httpx.Response(
            201,
            json={
                "id": "60",
                "status": "active",
                "application_ref": "20@home.example",
                "guild_ref": "70@guild.example",
                "channel_restrictions": [],
                "grant_revision": "1",
            },
        )
    )
    monkeypatch.setattr("app.api.applications.signed_request", signed)

    installed = await install_bot(
        EntityRef("70@guild.example"),
        EntityRef("20"),
        "default",
        auth,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
    )

    assert installed["application_ref"] == "20@home.example"
    assert signed.await_args.kwargs["payload"]["application_ref"] == "20@home.example"

    signed.return_value = httpx.Response(204)
    response = await uninstall_bot(
        EntityRef("70@guild.example"),
        EntityRef("20"),
        auth,
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
    )
    assert response.status_code == 204
    assert signed.await_args.kwargs["payload"]["application_ref"] == "20@home.example"


@pytest.mark.asyncio
async def test_remote_bot_install_rejects_mismatched_authority_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signed = AsyncMock(
        return_value=httpx.Response(
            201,
            json={
                "id": "60",
                "status": "active",
                "application_ref": "21@home.example",
                "guild_ref": "70@guild.example",
                "channel_restrictions": [],
                "grant_revision": "1",
            },
        )
    )
    monkeypatch.setattr("app.api.applications.signed_request", signed)

    with pytest.raises(HTTPException) as invalid:
        await install_bot(
            EntityRef("70@guild.example"),
            EntityRef("20"),
            "default",
            SimpleNamespace(user=SimpleNamespace(id=7)),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="home.example"),
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail == {"code": "REMOTE_BOT_INSTALL_INVALID"}


@pytest.mark.asyncio
async def test_remote_bot_installation_list_proxies_before_replica_permission_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_user = User(
        id=7,
        origin_domain="home.example",
        is_local=True,
        account_type="human",
        username="manager",
        password_hash=None,
    )
    guild = Guild(
        id=70,
        origin_domain="guild.example",
        name="Remote",
        owner_id=8,
        owner_domain="guild.example",
        unavailable=False,
    )
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=local_user.id,
        user_domain=local_user.origin_domain,
        joined_at=datetime.now(UTC),
    )

    async def get_model(model: object, _key: object, **_kwargs: object) -> object | None:
        if model is Guild:
            return guild
        if model is GuildMember:
            return member
        return None

    permission_check = AsyncMock(side_effect=AssertionError("replica permission was consulted"))
    monkeypatch.setattr("app.api.applications.get_permissions", permission_check)
    monkeypatch.setattr(
        "app.api.applications.signed_request",
        AsyncMock(return_value=httpx.Response(200, json=[])),
    )

    result = await list_guild_bot_integrations(
        EntityRef("70@guild.example"),
        SimpleNamespace(user=local_user),
        SimpleNamespace(get=AsyncMock(side_effect=get_model)),
        SimpleNamespace(),
        SimpleNamespace(domain="home.example"),
    )

    assert result == []
    permission_check.assert_not_awaited()


def test_supported_scopes_cover_runtime_resource_contracts() -> None:
    assert {
        "audit_logs.read",
        "automod.executions.read",
        "automod.rules.read",
        "automod.rules.manage",
        "guilds.manage",
        "guilds.assets.manage",
        "channels.manage",
        "channels.overwrites.read",
        "channels.overwrites.manage",
        "roles.manage",
        "events.read",
        "events.manage",
        "expressions.read",
        "expressions.manage",
        "installations.read",
        "integrations.read",
        "integrations.manage",
        "attachments.read",
        "attachments.write",
        "moderation.bans",
        "moderation.messages",
        "moderation.prune",
        "polls.read",
        "polls.write",
        "soundboard.read",
        "soundboard.use",
        "soundboard.manage",
        "voice.connect",
        "voice.listen",
        "voice.speak",
        "voice.stream",
        "invites.read",
        "voice.moderate",
        "invites.manage",
        "webhooks.read",
        "webhooks.manage",
        "emojis.manage",
        "tasks.read",
        "tasks.write",
        "tasks.manage",
        "dm.send",
    } <= SUPPORTED_SCOPES
    assert {
        "guild_moderation",
        "guild_expressions",
        "guild_integrations",
        "guild_webhooks",
        "guild_invites",
        "guild_scheduled_events",
        "guild_message_polls",
        "direct_message_polls",
        "auto_moderation_configuration",
        "auto_moderation_execution",
        "guild_tasks",
    } <= SUPPORTED_INTENTS


@pytest.mark.asyncio
async def test_bot_sticker_discovery_uses_read_scope_and_returns_typed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"guilds.read"}, intents=set())
    guild = Guild(
        id=70,
        origin_domain="guild.example",
        name="Guild",
        owner_id=80,
        owner_domain="guild.example",
    )
    installed = installation(scopes={"guilds.read"})
    sticker = Sticker(
        id=90,
        origin_domain="guild.example",
        guild_id=70,
        guild_domain="guild.example",
        name="wave",
        description="Hello",
        media_hash="a" * 64,
        animated=True,
        creator_id=10,
        creator_domain="apps.example",
    )
    authorize = AsyncMock(return_value=(guild, installed))
    monkeypatch.setattr(bots_api, "installation_for_guild_any_scope", authorize)
    session = SimpleNamespace(scalars=AsyncMock(return_value=[sticker]))

    response = await bots_api.bot_list_stickers(
        EntityRef("70@guild.example"),
        bot,
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
    )

    assert response == [
        {
            "id": "90",
            "origin_domain": "guild.example",
            "guild_id": "70",
            "guild_domain": "guild.example",
            "name": "wave",
            "description": "Hello",
            "animated": True,
            "available": True,
            "tags": [],
            "media_hash": "a" * 64,
            "creator_id": "10",
            "creator_domain": "apps.example",
            "version": None,
        }
    ]
    assert authorize.await_args.args[-2:] == ("expressions.read", "guilds.read")


def test_target_policy_explicit_deny_always_wins() -> None:
    assert target_policy_allows("open", {}, "target.example")
    assert not target_policy_allows("open", {"target.example": "deny"}, "target.example")
    assert target_policy_allows("allowlist", {"target.example": "allow"}, "target.example")
    assert not target_policy_allows("allowlist", {}, "target.example")
    assert not target_policy_allows("local_only", {"target.example": "allow"}, "target.example")


def installation(
    *,
    scopes: set[str],
    installation_id: int = 60,
    guild_id: int = 70,
) -> BotInstallation:
    return BotInstallation(
        id=installation_id,
        application_id=20,
        application_domain="apps.example",
        guild_id=guild_id,
        guild_domain="guild.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="guild.example",
        granted_scopes=sorted(scopes),
        granted_intents=["guild_messages"],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="active",
    )


def dm_capability(
    *,
    grant_id: str = "kbdg_" + "a" * 43,
    conversation_id: int = 55,
    scopes: set[str] | None = None,
    intents: set[str] | None = None,
) -> BotDMCapability:
    now = datetime.now(UTC)
    return BotDMCapability(
        id=61,
        grant_id=grant_id,
        source_kind="guild",
        source_installation_id=60,
        source_installation_domain="guild.example",
        application_id=20,
        application_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        guild_id=70,
        guild_domain="guild.example",
        installing_user_id=None,
        installing_user_domain=None,
        target_user_id=80,
        target_user_domain="users.example",
        pair_key="a" * 64,
        authority_domain="chat.example",
        conversation_id=conversation_id,
        conversation_domain="chat.example",
        granted_scopes=sorted(scopes or {"dm.send", "messages.metadata"}),
        granted_intents=sorted(intents or {"direct_messages"}),
        channel_restrictions=[],
        e2ee_mode="disabled",
        revision=2,
        status="active",
        proof_fingerprint=b"p" * 32,
        proof={},
        expires_at=now + timedelta(minutes=10),
    )


def e2ee_device(*, protocol_id: str = "kbe_" + "d" * 43) -> BotE2EEDevice:
    return BotE2EEDevice(
        id=90,
        source_id=90,
        source_domain="apps.example",
        protocol_id=protocol_id,
        application_id=20,
        application_domain="apps.example",
        worker_id=40,
        identity_key=b"i" * 32,
        credential=b"credential",
        capabilities=["e2ee-mls/1"],
        generation=1,
        trust_state="trusted",
    )


@pytest.mark.asyncio
async def test_dm_actions_require_an_exact_active_installation_scope() -> None:
    bot = principal(scopes={"messages.send", "dm.send"}, intents=set())
    installed = installation(scopes={"messages.send", "dm.send"})
    session = SimpleNamespace(scalar=AsyncMock(return_value=installed))

    assert (
        await exact_installation_by_id(session, bot, installed.id, "messages.send", "dm.send")
        is installed
    )
    query = str(session.scalar.await_args.args[0])
    assert "bot_installations.id" in query
    assert "bot_installations.bot_user_id" in query
    assert "bot_installations.status" in query
    assert "guild_members" in query

    with pytest.raises(HTTPException) as missing:
        await exact_installation_by_id(session, bot, None, "dm.send")
    assert missing.value.detail == {"code": "BOT_INSTALLATION_REQUIRED"}

    installed.granted_scopes = ["messages.send"]
    with pytest.raises(HTTPException) as reduced:
        await exact_installation_by_id(session, bot, installed.id, "messages.send", "dm.send")
    assert reduced.value.detail == {"code": "BOT_SCOPE_REQUIRED", "scope": "dm.send"}


@pytest.mark.asyncio
async def test_dm_capability_mutation_application_lock_is_active_and_for_update() -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    session = SimpleNamespace(scalar=AsyncMock(return_value=bot.application))

    assert await bots_api.locked_active_principal_application(session, bot) is bot.application
    statement = str(session.scalar.await_args.args[0])
    assert "FOR UPDATE" in statement
    assert "bot_applications.status" in statement
    assert "bot_applications.bot_user_id" in statement

    session.scalars = AsyncMock(
        return_value=[SimpleNamespace(target_domain="chat.example", effect="deny")]
    )
    assert await bots_api.locked_application_target_rules(session, bot.application) == {
        "chat.example": "deny"
    }
    assert "FOR UPDATE" in str(session.scalars.await_args.args[0])

    session.scalar.return_value = None
    with pytest.raises(HTTPException) as inactive:
        await bots_api.locked_active_principal_application(session, bot)
    assert inactive.value.status_code == 401
    assert inactive.value.detail == {"code": "BOT_TOKEN_INVALID"}


@pytest.mark.asyncio
async def test_dm_capability_open_and_refresh_lock_application_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    bot.application.status = "active"
    bot.application.target_policy = "open"
    settings = SimpleNamespace(domain="apps.example")
    target = SimpleNamespace(id=80, origin_domain="users.example", username="alice")
    capability = dm_capability()
    order: list[str] = []

    async def lock_application(*_args: object) -> BotApplication:
        order.append("application")
        return bot.application

    async def resolve(*_args: object) -> object:
        order.append("target")
        return target

    async def rules(*_args: object) -> dict[str, str]:
        order.append("rules")
        return {}

    async def attest(*_args: object, **_kwargs: object) -> tuple[object, object, bool]:
        order.append("capability")
        return object(), capability, True

    async def runtime(*_args: object, **_kwargs: object) -> set[str]:
        order.append("runtime")
        return set()

    async def runtime_proofs(*_args: object, **_kwargs: object) -> tuple[object, object]:
        order.append("proofs")
        return object(), object()

    async def open_dm(*_args: object, **_kwargs: object) -> dict[str, object]:
        order.append("open")
        return {"id": "55", "origin_domain": "chat.example"}

    monkeypatch.setattr(bots_api, "locked_active_principal_application", lock_application)
    monkeypatch.setattr(bots_api, "locked_application_target_rules", rules)
    monkeypatch.setattr(bots_api, "resolve_handle", resolve)
    monkeypatch.setattr(bots_api, "fetch_bot_dm_capability_proof", attest)
    monkeypatch.setattr(bots_api, "queue_application_runtime_snapshots", runtime)
    monkeypatch.setattr(bots_api, "current_bot_dm_runtime_proofs", runtime_proofs)
    monkeypatch.setattr(bots_api, "open_direct_message_for", open_dm)
    monkeypatch.setattr(bots_api, "wake_application_runtime_deliveries", AsyncMock())

    await bots_api.bot_open_direct_message(
        bots_api.DMOpenRequest(handle="alice@users.example"),
        Response(),
        bot,
        SimpleNamespace(commit=AsyncMock()),
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
        "60@guild.example",
        "guild",
    )
    assert order == [
        "application",
        "rules",
        "target",
        "runtime",
        "application",
        "rules",
        "proofs",
        "capability",
        "open",
    ]

    order.clear()

    async def refresh_proof(
        *_args: object, **_kwargs: object
    ) -> tuple[object, object, BotDMCapability]:
        order.append("proof")
        proof = SimpleNamespace(model_dump=lambda **_kwargs: {"proof": True})
        return proof, object(), capability

    async def relay(*_args: object) -> None:
        order.append("relay")

    async def select_capability(*_args: object) -> BotDMCapability:
        order.append("capability")
        return capability

    monkeypatch.setattr(bots_api, "refresh_bot_dm_capability_proof", refresh_proof)
    monkeypatch.setattr(bots_api, "relay_refreshed_bot_dm_capability", relay)
    monkeypatch.setattr(
        bots_api,
        "bot_dm_capability_bootstrap_payload",
        lambda *_args: {"grant_id": capability.grant_id},
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=select_capability),
        get=AsyncMock(return_value=SimpleNamespace(unavailable=False)),
        commit=AsyncMock(),
    )

    await bots_api.refresh_bot_dm_capability(
        capability.grant_id,
        bot,
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        settings,
    )
    assert order == [
        "application",
        "capability",
        "rules",
        "runtime",
        "application",
        "rules",
        "capability",
        "proofs",
        "proof",
        "relay",
    ]


@pytest.mark.asyncio
async def test_expired_dm_capability_refresh_stops_before_runtime_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    bot.application.status = "active"
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        commit=AsyncMock(),
    )
    runtime = AsyncMock()
    refresh = AsyncMock()
    relay = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "locked_active_principal_application",
        AsyncMock(return_value=bot.application),
    )
    monkeypatch.setattr(bots_api, "queue_application_runtime_snapshots", runtime)
    monkeypatch.setattr(bots_api, "refresh_bot_dm_capability_proof", refresh)
    monkeypatch.setattr(bots_api, "relay_refreshed_bot_dm_capability", relay)

    with pytest.raises(HTTPException) as rejected:
        await bots_api.refresh_bot_dm_capability(
            "kbdg_" + "a" * 43,
            bot,
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
        )

    assert rejected.value.status_code == 404
    query = str(session.scalar.await_args.args[0])
    assert "bot_dm_capabilities.expires_at > now()" in query
    runtime.assert_not_awaited()
    refresh.assert_not_awaited()
    relay.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_open_rejects_denied_conversation_authority_before_capability_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    bot.application.status = "active"
    bot.application.target_policy = "allowlist"
    target = SimpleNamespace(id=80, origin_domain="aaa.example", username="alice")
    conversation_authority = bots_api.dm_authority_domain(
        "weather_bot@apps.example",
        "alice@aaa.example",
    )
    fetch = AsyncMock()
    open_dm = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "locked_active_principal_application",
        AsyncMock(return_value=bot.application),
    )
    monkeypatch.setattr(
        bots_api,
        "locked_application_target_rules",
        AsyncMock(
            return_value={
                "guild.example": "allow",
                conversation_authority: "deny",
            }
        ),
    )
    monkeypatch.setattr(bots_api, "resolve_handle", AsyncMock(return_value=target))
    monkeypatch.setattr(bots_api, "fetch_bot_dm_capability_proof", fetch)
    monkeypatch.setattr(bots_api, "open_direct_message_for", open_dm)

    with pytest.raises(HTTPException) as denied:
        await bots_api.bot_open_direct_message(
            bots_api.DMOpenRequest(handle="alice@aaa.example"),
            Response(),
            bot,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
            "60@guild.example",
            "guild",
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "APPLICATION_TARGET_NOT_ALLOWED"}
    fetch.assert_not_awaited()
    open_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_open_rejects_worker_restricted_authority_before_runtime_or_attest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    bot.application.status = "active"
    bot.application.target_policy = "open"
    bot.worker.target_domains = ["guild.example"]
    target = SimpleNamespace(id=80, origin_domain="aaa.example", username="alice")
    runtime = AsyncMock()
    attest = AsyncMock()
    open_dm = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "locked_active_principal_application",
        AsyncMock(return_value=bot.application),
    )
    monkeypatch.setattr(
        bots_api,
        "locked_application_target_rules",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(bots_api, "resolve_handle", AsyncMock(return_value=target))
    monkeypatch.setattr(bots_api, "queue_application_runtime_snapshots", runtime)
    monkeypatch.setattr(bots_api, "fetch_bot_dm_capability_proof", attest)
    monkeypatch.setattr(bots_api, "open_direct_message_for", open_dm)

    with pytest.raises(HTTPException) as denied:
        await bots_api.bot_open_direct_message(
            bots_api.DMOpenRequest(handle="alice@aaa.example"),
            Response(),
            bot,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
            "60@guild.example",
            "guild",
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_TARGET_NOT_DELEGATED"}
    runtime.assert_not_awaited()
    attest.assert_not_awaited()
    open_dm.assert_not_awaited()


@pytest.mark.asyncio
async def test_dm_channel_access_requires_participation_and_one_exact_capability() -> None:
    base = principal(
        scopes={"dm.send", "messages.history", "messages.content"},
        intents={"direct_messages"},
    )
    bot = BotPrincipal(
        user=base.user,
        application=base.application,
        worker=base.worker,
        token=base.token,
        scopes=base.scopes,
        intents=base.intents,
        dm_capability_grant_id="kbdg_" + "a" * 43,
        dm_capability_revision=2,
        installation_ref="60@guild.example",
        installation_type="guild",
    )
    capability = dm_capability(
        scopes={"dm.send", "messages.history", "messages.content"},
    )
    capability.conversation_domain = "apps.example"
    capability.authority_domain = "apps.example"
    channel = SimpleNamespace(
        id=55,
        origin_domain="apps.example",
        unavailable=False,
        encryption_mode="plaintext",
        e2ee_required=False,
        guild_id=None,
    )
    participant = SimpleNamespace(user_id=bot.user.id)

    async def get(model: object, key: object) -> object | None:
        if model is Channel:
            return channel
        if model is bots_api.DMParticipant:
            assert key == (55, "apps.example", 10, "apps.example")
            return participant
        raise AssertionError(f"unexpected model lookup: {model!r}")

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=capability),
    )
    resolved, selected = await bots_api.installation_for_channel(
        session,
        SimpleNamespace(domain="apps.example"),
        bot,
        EntityRef("55@apps.example"),
        "messages.history",
    )
    assert (resolved, selected) == (channel, capability)

    async def without_participant(model: object, key: object) -> object | None:
        if model is Channel:
            return channel
        if model is bots_api.DMParticipant:
            return None
        raise AssertionError(f"unexpected model lookup: {model!r}")

    session.get = AsyncMock(side_effect=without_participant)
    session.scalar.reset_mock()
    channel.encryption_mode = "e2ee"
    with pytest.raises(HTTPException) as hidden:
        await bots_api.installation_for_channel(
            session,
            SimpleNamespace(domain="apps.example"),
            bot,
            EntityRef("55@apps.example"),
            "messages.history",
        )
    assert hidden.value.status_code == 404
    assert hidden.value.detail == {"code": "CHANNEL_NOT_FOUND"}
    session.scalar.assert_not_awaited()

    session.get = AsyncMock(side_effect=get)
    stripped = BotPrincipal(
        base.user,
        base.application,
        base.worker,
        base.token,
        base.scopes,
        base.intents,
    )
    with pytest.raises(HTTPException) as downgraded:
        await bots_api.installation_for_channel(
            session,
            SimpleNamespace(domain="apps.example"),
            stripped,
            EntityRef("55@apps.example"),
            "messages.history",
            60,
        )
    assert downgraded.value.detail == {"code": "BOT_DM_GRANT_REQUIRED"}
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
async def test_unbound_attachment_status_requires_its_exact_owning_installation() -> None:
    bot = principal(scopes={"attachments.read"}, intents=set())
    owner = installation(scopes={"attachments.read"}, installation_id=60)
    attachment = SimpleNamespace(
        deleted_at=None,
        bot_installation_id=owner.id,
        message_id=None,
        message_domain=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=attachment),
        scalar=AsyncMock(return_value=owner),
    )

    for selected_id in (None, 61):
        with pytest.raises(HTTPException) as denied:
            await bots_api.installation_attachment(
                session,
                SimpleNamespace(domain="apps.example"),
                bot,
                EntityRef("900@apps.example"),
                "attachments.read",
                require_bound_message=False,
                installation_id=selected_id,
            )
        assert denied.value.status_code == 404
        assert denied.value.detail == {"code": "MEDIA_NOT_FOUND"}

    resolved, selected = await bots_api.installation_attachment(
        session,
        SimpleNamespace(domain="apps.example"),
        bot,
        EntityRef("900@apps.example"),
        "attachments.read",
        require_bound_message=False,
        installation_id=owner.id,
    )
    assert (resolved, selected) == (attachment, owner)


@pytest.mark.asyncio
async def test_bound_attachment_hides_unauthorized_channel_existence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"attachments.read"}, intents=set())
    attachment = SimpleNamespace(
        deleted_at=None,
        bot_installation_id=None,
        bot_dm_capability_id=None,
        message_id=901,
        message_domain="private.example",
    )
    message = SimpleNamespace(
        deleted_at=None,
        channel_id=55,
        channel_domain="private.example",
    )

    async def get(model: object, _key: object) -> object | None:
        return {Attachment: attachment, Message: message}.get(model)

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    channel_access = AsyncMock(
        side_effect=HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    )
    monkeypatch.setattr(bots_api, "installation_for_channel", channel_access)

    with pytest.raises(HTTPException) as denied:
        await bots_api.installation_attachment(
            session,
            SimpleNamespace(domain="apps.example"),
            bot,
            EntityRef("900@apps.example"),
            "attachments.read",
            require_bound_message=True,
            installation_id=60,
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "MEDIA_NOT_FOUND"}


def test_gateway_fingerprint_changes_for_every_live_grant_boundary() -> None:
    bot = principal(
        scopes={"messages.metadata", "messages.content"},
        intents={"guild_messages", "message_content"},
    )
    bot.application.status = "active"
    bot.application.manifest_generation = 1
    bot.application.revocation_generation = 1
    bot.application.default_scopes = sorted(bot.scopes)
    bot.application.default_intents = sorted(bot.intents)
    bot.worker.generation = 1
    bot.worker.session_limit = 1
    bot.worker.expires_at = None
    bot.worker.revoked_at = None
    bot.token.revoked_at = None
    bot.token.dpop_thumbprint = "thumbprint"
    installed = installation(scopes={"messages.metadata", "messages.content"})

    original = gateway_authorization_fingerprint(
        bot.application, bot.worker, bot.token, [installed]
    )
    installed.grant_revision += 1
    assert original != gateway_authorization_fingerprint(
        bot.application, bot.worker, bot.token, [installed]
    )
    installed.grant_revision -= 1
    installed.granted_scopes = ["messages.metadata"]
    assert original != gateway_authorization_fingerprint(
        bot.application, bot.worker, bot.token, [installed]
    )
    installed.granted_scopes = ["messages.metadata", "messages.content"]
    bot.worker.revoked_at = datetime.now(UTC)
    assert original != gateway_authorization_fingerprint(
        bot.application, bot.worker, bot.token, [installed]
    )
    bot.worker.revoked_at = None
    bot.application.status = "suspended"
    assert original != gateway_authorization_fingerprint(
        bot.application, bot.worker, bot.token, [installed]
    )

    bot.application.status = "active"
    live_authorization = GatewayGuildAuthorization(
        installation_id=installed.id,
        guild_id=installed.guild_id,
        guild_domain=installed.guild_domain,
        permission_generation=3,
        member_version=7,
        effective_permissions=int(Permission.VIEW_AUDIT_LOG),
    )
    live_fingerprint = gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [installed],
        guild_authorizations=[live_authorization],
    )
    assert live_fingerprint != gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [installed],
        guild_authorizations=[
            GatewayGuildAuthorization(
                installation_id=installed.id,
                guild_id=installed.guild_id,
                guild_domain=installed.guild_domain,
                permission_generation=4,
                member_version=7,
                effective_permissions=0,
            )
        ],
    )


def test_gateway_fingerprint_fences_dm_capability_revision_and_lease() -> None:
    bot = principal(scopes={"dm.send", "messages.metadata"}, intents={"direct_messages"})
    bot.application.status = "active"
    bot.application.manifest_generation = 1
    bot.application.revocation_generation = 1
    bot.application.default_scopes = sorted(bot.scopes)
    bot.application.default_intents = sorted(bot.intents)
    bot.worker.generation = 1
    bot.worker.session_limit = 1
    bot.worker.expires_at = None
    bot.worker.revoked_at = None
    bot.token.revoked_at = None
    capability = dm_capability()

    original = gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [],
        dm_capabilities=[capability],
    )
    capability.revision -= 1
    capability.expires_at += timedelta(seconds=1)
    assert original != gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [],
        dm_capabilities=[capability],
    )
    capability.revision += 1
    assert original != gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [],
        dm_capabilities=[capability],
    )


def test_gateway_fingerprint_fences_the_identified_e2ee_device() -> None:
    bot = principal(scopes={"messages.metadata"}, intents={"direct_messages"})
    bot.application.default_scopes = sorted(bot.scopes)
    bot.application.default_intents = sorted(bot.intents)
    bot.application.manifest_generation = 1
    bot.application.revocation_generation = 1
    bot.worker.generation = 1
    bot.worker.session_limit = 1
    device = e2ee_device()
    original = gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [],
        e2ee_device=device,
    )

    device.generation += 1

    assert original != gateway_authorization_fingerprint(
        bot.application,
        bot.worker,
        bot.token,
        [],
        e2ee_device=device,
    )


def test_gateway_identify_accepts_only_canonical_e2ee_device_ids() -> None:
    device_id = "kbe_" + "d" * 43
    assert requested_gateway_e2ee_device_id({}) is None
    assert requested_gateway_e2ee_device_id({"e2ee_device_id": device_id}) == device_id

    for invalid in (42, "", "kbe_short", "kbe_" + "=" * 43, device_id + "x"):
        with pytest.raises(GatewayProtocolError) as denied:
            requested_gateway_e2ee_device_id({"e2ee_device_id": invalid})
        assert denied.value.code == 4403


def test_gateway_resume_cursors_are_strict_bounded_integers() -> None:
    assert resume_cursors({"cursors": {"guild:chat.example:7": 0}}) == {"guild:chat.example:7": 0}
    for invalid in (
        {"guild:chat.example:7": True},
        {"guild:chat.example:7": -1},
        {"guild:chat.example:7": 1 << 63},
        {"guild:chat.example:7": "1"},
        {7: 1},
    ):
        with pytest.raises(GatewayProtocolError) as denied:
            resume_cursors({"cursors": invalid})
        assert denied.value.code == 4400


def test_gateway_live_permissions_are_bounded_by_install_and_current_role_authority() -> None:
    installed = int(Permission.VIEW_AUDIT_LOG | Permission.MANAGE_WEBHOOKS)

    assert gateway_effective_permissions(installed, 0) == 0
    assert gateway_effective_permissions(
        installed,
        int(Permission.VIEW_AUDIT_LOG),
    ) == int(Permission.VIEW_AUDIT_LOG)


@pytest.mark.asyncio
async def test_gateway_authorization_loads_current_bot_member_permissions() -> None:
    bot = principal(scopes={"audit_logs.read"}, intents={"guild_moderation"})
    bot.application.status = "active"
    bot.application.manifest_generation = 1
    bot.application.revocation_generation = 1
    bot.application.default_scopes = sorted(bot.scopes)
    bot.application.default_intents = sorted(bot.intents)
    bot.worker.generation = 1
    bot.worker.session_limit = 1
    bot.worker.expires_at = None
    bot.worker.revoked_at = None
    bot.token.revoked_at = None
    installed = installation(scopes={"audit_logs.read"})
    installed.granted_permissions = int(Permission.VIEW_AUDIT_LOG)
    guild, _, member, _ = moderation_fixture()
    guild.permission_generation = 3
    member.member_version = 7
    role = Role(
        id=guild.id,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="@everyone",
        permissions=int(Permission.VIEW_CHANNEL | Permission.VIEW_AUDIT_LOG),
        position=0,
    )
    result = Mock()
    result.one_or_none.return_value = (
        bot.token,
        bot.worker,
        bot.application,
        bot.user,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(side_effect=[[installed], [], [role]]),
        scalar=AsyncMock(return_value=member),
        get=AsyncMock(return_value=guild),
    )

    state = await current_gateway_authorization(
        session,
        bot,
        authority_domain="chat.example",
    )

    assert state is not None
    assert state.guild_authorizations == (
        GatewayGuildAuthorization(
            installation_id=installed.id,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            permission_generation=3,
            member_version=7,
            effective_permissions=int(Permission.VIEW_AUDIT_LOG),
        ),
    )


@pytest.mark.asyncio
async def test_gateway_authorization_accepts_only_bound_live_dm_capability() -> None:
    bot = principal(scopes={"dm.send", "messages.metadata"}, intents={"direct_messages"})
    bot.application.status = "active"
    bot.application.manifest_generation = 1
    bot.application.revocation_generation = 1
    bot.application.default_scopes = sorted(bot.scopes)
    bot.application.default_intents = sorted(bot.intents)
    bot.worker.generation = 1
    bot.worker.session_limit = 1
    bot.worker.expires_at = None
    bot.worker.revoked_at = None
    bot.token.revoked_at = None
    capability = dm_capability()
    device = e2ee_device()
    bot.token.dm_capability_id = capability.id
    bot.token.dm_capability_revision = capability.revision
    bot = BotPrincipal(
        user=bot.user,
        application=bot.application,
        worker=bot.worker,
        token=bot.token,
        scopes=bot.scopes,
        intents=bot.intents,
        dm_capability_grant_id=capability.grant_id,
        dm_capability_revision=capability.revision,
        installation_ref=(
            f"{capability.source_installation_id}@{capability.source_installation_domain}"
        ),
        installation_type=capability.source_kind,
    )
    result = Mock()
    result.one_or_none.return_value = (
        bot.token,
        bot.worker,
        bot.application,
        bot.user,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(return_value=[capability]),
        scalar=AsyncMock(return_value=device),
    )

    state = await current_gateway_authorization(
        session,
        bot,
        e2ee_device_id=device.protocol_id,
        authority_domain="chat.example",
    )

    assert state is not None
    assert state.installations == ()
    assert state.user_installations == ()
    assert state.dm_capabilities == (capability,)
    assert state.e2ee_device is device
    capability_query = str(session.scalars.await_args_list[0].args[0])
    assert "JOIN channels" in capability_query
    assert "JOIN dm_participants" in capability_query
    assert "bot_dm_capabilities.expires_at" in capability_query
    assert "users.disabled_at IS NULL" in capability_query
    device_query = str(session.scalar.await_args.args[0])
    assert "bot_e2ee_devices.protocol_id" in device_query
    assert "bot_e2ee_devices.worker_id" in device_query
    assert "bot_e2ee_devices.trust_state" in device_query
    assert "bot_e2ee_devices.revoked_at IS NULL" in device_query
    assert device.protocol_id in session.scalar.await_args.args[0].compile().params.values()


def test_gateway_dm_capability_grant_is_conversation_bound_and_visible_in_ready() -> None:
    bot = principal(scopes={"dm.send", "messages.metadata"}, intents={"direct_messages"})
    capability = dm_capability()
    authorization = GatewayAuthorizationState(
        ("fingerprint",),
        (),
        (),
        (capability,),
    )
    bootstrap = GatewayBootstrap(bot, authorization, [], [], [capability], [], {})

    grants = gateway_topic_grants(bootstrap)
    topic = "user:apps.example:10"
    assert set(grants) == {topic}
    direct = grants[topic][5]
    assert len(direct) == 1
    assert direct[0].dm_capability_grant_id == capability.grant_id
    assert (direct[0].conversation_id, direct[0].conversation_domain) == (
        55,
        "chat.example",
    )
    assert direct[0].installation_domain == "guild.example"
    ready = gateway_ready_event(bootstrap)
    assert ready["d"]["dm_capabilities"] == [  # type: ignore[index]
        {
            "grant_id": capability.grant_id,
            "installation_ref": "60@guild.example",
            "installation_type": "guild",
            "channel_ref": "55@chat.example",
            "capability_revision": "2",
            "expires_at": capability.expires_at.isoformat(),
        }
    ]


@pytest.mark.asyncio
async def test_gateway_bootstrap_preserves_capability_lineage_and_binds_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = principal(scopes={"dm.send", "messages.metadata"}, intents={"direct_messages"})
    capability = dm_capability()
    bound = BotPrincipal(
        user=base.user,
        application=base.application,
        worker=base.worker,
        token=base.token,
        scopes=base.scopes,
        intents=base.intents,
        dm_capability_grant_id=capability.grant_id,
        dm_capability_revision=capability.revision,
        installation_ref="60@guild.example",
        installation_type="guild",
    )
    device = e2ee_device()
    authorization = GatewayAuthorizationState(
        fingerprint=("fingerprint",),
        installations=(),
        dm_capabilities=(capability,),
        e2ee_device=device,
    )
    current = AsyncMock(return_value=authorization)
    monkeypatch.setattr("app.api.bot_gateway.require_bot", AsyncMock(return_value=bound))
    monkeypatch.setattr("app.api.bot_gateway.current_gateway_authorization", current)
    monkeypatch.setattr(
        "app.api.bot_gateway.encrypted_direct_channels", AsyncMock(return_value=set())
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    websocket = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(sessionmaker=lambda: SessionContext())),
        client=("127.0.0.1", 443),
        scope={"server": ("chat.example", 443)},
    )
    identify = {
        "token": "kb1_at_test",
        "timestamp": 1,
        "nonce": "nonce",
        "proof": "proof",
        "intents": ["direct_messages"],
        "e2ee_device_id": device.protocol_id,
    }

    bootstrap = await load_gateway_bootstrap(websocket, SimpleNamespace(), identify)

    assert bootstrap.principal.dm_capability_grant_id == capability.grant_id
    assert bootstrap.principal.dm_capability_revision == capability.revision
    assert bootstrap.principal.installation_ref == "60@guild.example"
    assert bootstrap.principal.installation_type == "guild"
    assert current.await_args.args[3] == device.protocol_id
    assert gateway_ready_event(bootstrap)["d"]["e2ee_device_id"] == device.protocol_id  # type: ignore[index]


@pytest.mark.asyncio
async def test_bot_guild_listing_requires_the_scope_on_each_installation() -> None:
    bot = principal(scopes={"guilds.read"}, intents=set())
    result = Mock()
    result.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(return_value=result))

    assert await bot_guilds(bot, session) == []

    statement = session.execute.await_args.args[0]
    query = str(statement)
    assert "bot_installations.granted_scopes" in query
    assert ["guilds.read"] in statement.compile().params.values()
    assert "guild_members" in query
    assert gateway_effective_permissions(
        int(Permission.ADMINISTRATOR),
        int(Permission.VIEW_AUDIT_LOG),
    ) == int(Permission.VIEW_AUDIT_LOG)


@pytest.mark.asyncio
async def test_gateway_guard_reloads_authorization_before_disclosure(monkeypatch) -> None:
    bot = principal(scopes={"guilds.read"}, intents={"guilds"})

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    current = AsyncMock(return_value=GatewayAuthorizationState(("changed",), ()))
    monkeypatch.setattr("app.api.bot_gateway.current_gateway_authorization", current)
    guard = GatewayAuthorizationGuard(
        lambda: SessionContext(),
        bot,
        ("original",),
        "chat.example",
    )

    assert not await guard.current(force=True)
    current.assert_awaited_once()


@pytest.mark.parametrize(
    "event",
    [
        {"t": "MESSAGE_CREATE", "d": {"channel_id": "7"}},
        {
            "t": "MESSAGE_CREATE",
            "d": {"channel_id": "7", "channel_domain": "Chat.Example"},
        },
        {
            "t": "MESSAGE_CREATE",
            "d": {"channel_id": "07", "channel_domain": "chat.example"},
        },
        {
            "t": "CHANNEL_UPDATE",
            "d": {"channel_id": "7", "channel_domain": "chat.example"},
        },
    ],
)
def test_direct_gateway_channel_references_fail_closed(event: dict[str, object]) -> None:
    assert direct_event_channel_reference(event) is None


def test_direct_gateway_uses_canonical_resource_refs_and_minimal_tombstones() -> None:
    assert direct_event_channel_reference(
        {
            "t": "CHANNEL_UPDATE",
            "d": {"id": "7", "origin_domain": "chat.example", "name": "Group"},
        }
    ) == (7, "chat.example")
    tombstone = {
        "t": "CHANNEL_DELETE",
        "d": {"id": "7", "origin_domain": "chat.example"},
    }
    assert canonical_direct_channel_tombstone(tombstone)
    assert not canonical_direct_channel_tombstone(
        {**tombstone, "d": {**tombstone["d"], "name": "private"}}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("participant_id", "expected"), [(10, True), (None, False)])
async def test_direct_gateway_rechecks_authoritative_dm_participation(
    participant_id: int | None,
    expected: bool,
) -> None:
    bot = principal(scopes={"messages.metadata", "dm.send"}, intents={"direct_messages"})
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, None, participant_id]))

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    event = {
        "t": "MESSAGE_CREATE",
        "d": {"channel_id": "7", "channel_domain": "chat.example"},
    }

    assert (
        await current_direct_event_access(
            lambda: SessionContext(),
            bot,
            "user:apps.example:10",
            event,
        )
        is expected
    )
    statement = session.scalar.await_args.args[0]
    query = str(statement)
    assert "JOIN channels" in query
    assert "dm_participants.conversation_id" in query
    assert "dm_participants.conversation_domain" in query
    assert "dm_participants.user_id" in query
    assert "dm_participants.user_domain" in query
    assert "channels.unavailable IS false" in query


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active_capability", "lineage", "expected"),
    [(61, 61, True), (None, 61, False)],
)
async def test_direct_gateway_capability_cannot_fall_back_to_unrelated_install(
    active_capability: int | None,
    lineage: int,
    expected: bool,
) -> None:
    bot = principal(scopes={"messages.metadata", "dm.send"}, intents={"direct_messages"})
    scalar = AsyncMock(side_effect=[active_capability, lineage])
    session = SimpleNamespace(scalar=scalar)

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    allowed = await current_direct_event_access(
        lambda: SessionContext(),
        bot,
        "user:apps.example:10",
        {
            "t": "MESSAGE_CREATE",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
    )

    assert allowed is expected
    active_query = str(scalar.await_args_list[0].args[0])
    assert "bot_dm_capabilities.grant_id" not in active_query
    assert "bot_dm_capabilities.expires_at" in active_query
    assert "JOIN dm_participants" in active_query
    if active_capability is None:
        assert scalar.await_count == 2


def test_direct_dm_projection_uses_only_the_bound_capability_grant() -> None:
    bot = principal(
        scopes={"dm.send", "messages.metadata", "messages.content", "attachments.read"},
        intents={"direct_messages", "message_content"},
    )
    bound = GatewayInstallationGrant(
        installation_id=60,
        installation_domain="guild.example",
        user_installation=False,
        intents=frozenset({"direct_messages"}),
        scopes=frozenset({"dm.send", "messages.metadata"}),
        dm_capability_grant_id="kbdg_" + "a" * 43,
        dm_capability_revision=2,
        conversation_id=55,
        conversation_domain="chat.example",
    )
    unrelated = GatewayInstallationGrant(
        installation_id=70,
        installation_domain="other.example",
        user_installation=False,
        intents=frozenset({"direct_messages", "message_content"}),
        scopes=frozenset({"dm.send", "messages.metadata", "messages.content", "attachments.read"}),
    )
    event = {
        "t": "MESSAGE_CREATE",
        "d": {
            "id": "80",
            "origin_domain": "chat.example",
            "channel_id": "55",
            "channel_domain": "chat.example",
            "content": "secret",
            "attachments": [{"id": "81", "filename": "secret.txt"}],
        },
    }

    projected = filtered_event(
        bot,
        event,
        set(),
        set(),
        topic="user:apps.example:10",
        installation_grants=(unrelated, bound),
    )

    assert projected is not None
    assert projected["d"]["bot_dm_capability_id"] == bound.dm_capability_grant_id
    assert projected["d"]["installation_ref"] == "60@guild.example"
    # Direct-message content is Discord-exempt, but the unrelated richer
    # installation still cannot contribute its attachment-read scope.
    assert projected["d"]["content"] == "secret"
    assert projected["d"]["attachments"] == []
    assert projected["d"]["attachments_unavailable"] is True


@pytest.mark.parametrize(
    "event",
    [
        {
            "t": "CHANNEL_CREATE",
            "d": {"id": "55", "origin_domain": "chat.example", "guild_id": None},
        },
        {
            "t": "MESSAGE_CREATE",
            "d": {
                "id": "80",
                "origin_domain": "chat.example",
                "channel_id": "55",
                "channel_domain": "chat.example",
                "attachments": [],
            },
        },
        {
            "t": "MESSAGE_REACTION_ADD",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "MESSAGE_POLL_VOTE_ADD",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "ATTACHMENT_UPDATE",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "TYPING_START",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "CALL_RING",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "VOICE_STATE_UPDATE",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
        {
            "t": "VOICE_TOKEN",
            "d": {"channel_id": "55", "channel_domain": "chat.example"},
        },
    ],
)
def test_gateway_dm_capability_covers_only_its_bound_event_stream(
    event: dict[str, object],
) -> None:
    intents = {
        "direct_messages",
        "direct_message_reactions",
        "direct_message_polls",
        "direct_message_typing",
    }
    scopes = {
        "dm.send",
        "channels.read",
        "messages.metadata",
        "reactions.read",
        "polls.read",
        "attachments.read",
        "voice.connect",
        "voice.states.read",
    }
    bot = principal(scopes=scopes, intents=intents)
    grant = GatewayInstallationGrant(
        installation_id=60,
        installation_domain="guild.example",
        user_installation=False,
        intents=frozenset(intents),
        scopes=frozenset(scopes),
        dm_capability_grant_id="kbdg_" + "a" * 43,
        dm_capability_revision=2,
        conversation_id=55,
        conversation_domain="chat.example",
    )

    assert (
        filtered_event(
            bot,
            event,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=(grant,),
        )
        is not None
    )
    unrelated = {"t": event["t"], "d": dict(event["d"])}
    if event["t"] == "CHANNEL_CREATE":
        unrelated["d"]["id"] = "56"  # type: ignore[index]
    else:
        unrelated["d"]["channel_id"] = "56"  # type: ignore[index]
    assert (
        filtered_event(
            bot,
            unrelated,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=(grant,),
        )
        is None
    )


@pytest.mark.asyncio
async def test_direct_gateway_tombstone_and_interaction_exceptions_are_data_safe() -> None:
    bot = principal(scopes={"channels.read", "dm.send"}, intents={"guilds"})

    def unexpected_session() -> object:
        raise AssertionError("the exception must not query DM participation")

    assert await current_direct_event_access(
        unexpected_session,
        bot,
        "user:apps.example:10",
        {
            "t": "CHANNEL_DELETE",
            "d": {"id": "7", "origin_domain": "chat.example"},
        },
    )
    assert await current_direct_event_access(
        unexpected_session,
        bot,
        "user:apps.example:10",
        {
            "t": "INTERACTION_CREATE",
            "d": {"channel_id": "7", "channel_domain": "chat.example"},
        },
    )
    assert not await current_direct_event_access(
        unexpected_session,
        bot,
        "user:apps.example:10",
        {"t": "INTERACTION_CREATE", "d": {"channel_id": "7"}},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id, expected", [("event-id", True), (None, False)])
async def test_dm_open_rejection_requires_exact_outbound_correlation(
    request_id: str | None,
    expected: bool,
) -> None:
    bot = principal(scopes={"dm.send"}, intents={"direct_messages"})
    session = SimpleNamespace(scalar=AsyncMock(return_value=request_id))

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    event = {
        "t": "DM_OPEN_REJECTED",
        "d": {
            "pair_key": "a" * 64,
            "code": "DM_OPEN_REJECTED",
            "authority_domain": "chat.example",
        },
    }
    assert (
        await current_direct_event_access(
            lambda: SessionContext(),
            bot,
            "user:apps.example:10",
            event,
        )
        is expected
    )
    statement = session.scalar.await_args.args[0]
    query = str(statement)
    assert "JOIN federation_outbox" in query
    assert "dm.open.request" in statement.compile().params.values()

    malformed = {**event, "d": {**event["d"], "extra": "unsafe"}}
    assert not await current_direct_event_access(
        lambda: SessionContext(),
        bot,
        "user:apps.example:10",
        malformed,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("installation_id", "restrictions", "expected"),
    [
        (60, [], True),
        (60, ["7@guild.example"], True),
        (60, ["8@guild.example"], False),
        (None, [], False),
    ],
)
async def test_private_guild_voice_event_rechecks_exact_live_installation(
    installation_id: int | None,
    restrictions: list[str],
    expected: bool,
) -> None:
    bot = principal(scopes={"voice.connect"}, intents={"guild_voice_states"})
    installed = installation(scopes={"voice.connect"})
    installed.channel_restrictions = restrictions
    channel = SimpleNamespace(
        id=7,
        origin_domain="guild.example",
        guild_id=70,
        guild_domain="guild.example",
        parent_id=None,
        parent_domain=None,
        unavailable=False,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=installed if installation_id is not None else None),
        get=AsyncMock(return_value=channel),
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    event = {
        "t": "VOICE_TOKEN",
        "d": {
            "guild_id": "70",
            "guild_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "grant": {"token": "opaque"},
        },
    }
    assert (
        await current_direct_event_access(
            lambda: SessionContext(),
            bot,
            "user:apps.example:10",
            event,
        )
        is expected
    )
    query = str(session.scalar.await_args.args[0])
    assert "EXISTS (SELECT guild_members" in query
    assert "bot_installations.guild_id" in query
    if installation_id is None:
        session.get.assert_not_awaited()
    else:
        session.get.assert_awaited_once_with(
            Channel,
            (7, "guild.example"),
            populate_existing=True,
        )


@pytest.mark.asyncio
async def test_live_gateway_last_mile_skips_events_after_dm_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"messages.metadata", "dm.send"}, intents={"direct_messages"})
    socket = SimpleNamespace(send_json=AsyncMock())
    guard = SimpleNamespace(current=AsyncMock(return_value=True), target_domain=None)
    participant_check = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.api.bot_gateway.current_direct_event_access",
        participant_check,
    )
    raw = {
        "t": "MESSAGE_CREATE",
        "d": {"channel_id": "7", "channel_domain": "chat.example"},
    }
    projected = {"op": 0, "t": "MESSAGE_CREATE", "s": 1, "d": {}}

    assert await disclose_current_event(
        socket,
        object(),
        bot,
        "user:apps.example:10",
        raw,
        projected,
        guard,
    )
    guard.current.assert_awaited_once_with(force=True)
    participant_check.assert_awaited_once()
    socket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_bot_is_rejected_before_rest_dpop_processing() -> None:
    bot = principal(scopes={"guilds.read"}, intents=set())
    bot.user.disabled_at = datetime.now(UTC)
    result = Mock()
    result.one_or_none.return_value = (bot.token, bot.worker, bot.application, bot.user)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/bots/guilds",
            "query_string": b"",
            "headers": [(b"authorization", b"Bot kb1_at_disabled")],
            "scheme": "https",
            "server": ("guild.example", 443),
        }
    )

    with pytest.raises(HTTPException) as denied:
        await require_bot(
            request,
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert denied.value.status_code == 401
    assert denied.value.detail == {"code": "BOT_TOKEN_INVALID"}
    query = str(session.execute.await_args.args[0])
    assert "users.disabled_at IS NULL" in query
    assert "guild_members" in query
    assert "bot_user_installations.status" in query
    assert "bot_application_targets.runtime_fingerprint IS NOT NULL" in query
    assert "bot_application_targets.runtime_manifest_generation" in query
    assert "bot_application_targets.runtime_revocation_generation" in query
    assert "bot_application_targets.runtime_status" in query
    assert "bot_application_targets.runtime_target_allowed IS true" in query


@pytest.mark.asyncio
async def test_application_home_bot_auth_does_not_require_a_local_installation() -> None:
    bot = principal(scopes={"applications.assets.manage"}, intents=set())
    bot.user.disabled_at = datetime.now(UTC)
    result = Mock()
    result.one_or_none.return_value = (bot.token, bot.worker, bot.application, bot.user)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/bots/applications/@me/assets",
            "query_string": b"",
            "headers": [(b"authorization", b"Bot kb1_at_disabled")],
            "scheme": "https",
            "server": ("apps.example", 443),
        }
    )

    with pytest.raises(HTTPException) as denied:
        await require_application_home_bot(
            request,
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="apps.example"),
        )

    assert denied.value.detail == {"code": "BOT_TOKEN_INVALID"}
    query = str(session.execute.await_args.args[0])
    assert "bot_applications.origin_domain" in query
    assert "guild_members" not in query
    assert "bot_user_installations.status" not in query


@pytest.mark.asyncio
async def test_gateway_periodic_authorization_reloads_and_rejects_disabled_bot() -> None:
    bot = principal(scopes={"guilds.read"}, intents={"guilds"})
    bot.application.status = "active"
    bot.application.default_scopes = ["guilds.read"]
    bot.application.default_intents = ["guilds"]
    bot.worker.revoked_at = None
    bot.worker.expires_at = None
    bot.token.revoked_at = None
    bot.user.disabled_at = datetime.now(UTC)
    result = Mock()
    result.one_or_none.return_value = (bot.token, bot.worker, bot.application, bot.user)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(),
    )

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_: object) -> None:
            return None

    guard = GatewayAuthorizationGuard(
        lambda: SessionContext(),
        bot,
        ("authorized",),
        "chat.example",
    )

    assert not await guard.current(force=True)
    session.scalars.assert_not_awaited()
    query = str(session.execute.await_args.args[0])
    assert "JOIN users" in query
    assert "users.disabled_at IS NULL" in query


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status", ["active", "suspended"])
async def test_member_removal_revokes_the_installation_until_explicit_reinstall(
    prior_status: str,
) -> None:
    installed = installation(scopes={"guilds.read"})
    installed.status = prior_status
    original_revision = installed.grant_revision
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[installed]),
        sync_session=Session(),
    )

    revoked = await revoke_installations_for_guild_member(
        session,
        guild_id=installed.guild_id,
        guild_domain=installed.guild_domain,
        user_id=installed.bot_user_id,
        user_domain=installed.bot_user_domain,
    )

    assert revoked == [installed]
    assert installed.status == "revoked"
    assert installed.revoked_at is not None
    assert installed.grant_revision == original_revision + 1
    query = str(session.scalars.await_args.args[0])
    assert "bot_installations.status !=" in query
    assert "revoked" in query
    assert "FOR UPDATE" in query


@pytest.mark.asyncio
async def test_instance_ban_atomically_revokes_every_matching_installation() -> None:
    first = installation(scopes={"guilds.read"}, installation_id=60)
    second = installation(scopes={"messages.send"}, installation_id=61)
    second.status = "suspended"
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[first, second]),
        sync_session=Session(),
    )

    revoked = await revoke_installations_for_guild_instance(
        session,
        guild_id=70,
        guild_domain="guild.example",
        instance_domain="apps.example",
    )

    assert revoked == [first, second]
    assert {item.status for item in revoked} == {"revoked"}
    assert all(item.revoked_at is not None for item in revoked)
    assert {item.grant_revision for item in revoked} == {2}
    query = str(session.scalars.await_args.args[0])
    assert "bot_installations.bot_user_domain" in query
    assert "bot_installations.guild_id" in query
    assert "bot_installations.status !=" in query
    assert "revoked" in query
    assert "FOR UPDATE" in query


@pytest.mark.asyncio
async def test_revoked_installation_role_cleanup_is_atomic_and_federated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, _, _ = moderation_fixture()
    guild.permission_generation = 4
    installed = installation(scopes={"guilds.read"})
    installed.status = "revoked"
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
    role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Weather",
        permissions=0,
        position=1,
    )
    queue_mutation = AsyncMock()
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        queue_mutation,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[role]),
        execute=AsyncMock(side_effect=[[], Mock(), [], Mock()]),
        delete=AsyncMock(),
    )
    settings = SimpleNamespace(domain=guild.origin_domain)

    deleted = await cleanup_installation_roles(
        session,
        settings,
        guild,
        actor,
        [installed],
    )

    assert deleted == [(role.id, role.origin_domain)]
    assert installed.role_id is None
    assert installed.role_domain is None
    assert guild.permission_generation == 5
    role_query = str(session.scalars.await_args.args[0])
    assert "roles.guild_id" in role_query
    assert "roles.id !=" in role_query
    assert "FOR UPDATE" in role_query
    member_role_delete = str(session.execute.await_args_list[1].args[0])
    assert "DELETE FROM member_roles" in member_role_delete
    assert "member_roles.user_id" in member_role_delete
    remaining_grants_query = str(session.execute.await_args_list[2].args[0])
    assert "SELECT DISTINCT member_roles.role_id" in remaining_grants_query
    overwrite_query = str(session.execute.await_args_list[3].args[0])
    assert "channel_overwrites.guild_id" in overwrite_query
    assert "channel_overwrites.target_type" in overwrite_query
    queue_mutation.assert_awaited_once_with(
        session,
        settings,
        guild,
        actor,
        "guild.role.delete",
        {"role": {"id": "90", "origin_domain": guild.origin_domain}},
        snapshot_required=True,
    )
    session.delete.assert_awaited_once_with(role)


@pytest.mark.asyncio
async def test_role_cleanup_retains_a_role_shared_with_another_active_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, _, _ = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    installed.status = "revoked"
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
    role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Shared",
        permissions=0,
        position=1,
    )
    queue_mutation = AsyncMock()
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        queue_mutation,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[role]),
        execute=AsyncMock(
            side_effect=[
                [
                    (
                        installed.bot_user_id,
                        installed.bot_user_domain,
                        role.id,
                        role.origin_domain,
                    )
                ],
                [(role.id, role.origin_domain)],
            ]
        ),
        delete=AsyncMock(),
    )

    assert (
        await cleanup_installation_roles(
            session,
            SimpleNamespace(domain=guild.origin_domain),
            guild,
            actor,
            [installed],
        )
        == []
    )
    assert installed.role_id is None
    assert installed.role_domain is None
    assert session.execute.await_count == 2
    active_grant_query = str(session.execute.await_args_list[0].args[0])
    assert "bot_installations.bot_user_id" in active_grant_query
    assert all(
        "DELETE FROM member_roles" not in str(call.args[0])
        for call in session.execute.await_args_list
    )
    queue_mutation.assert_not_awaited()
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_cleanup_retains_human_shared_role_but_removes_only_bot_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, actor, _, _ = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    installed.status = "revoked"
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
    role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Human shared",
        permissions=int(Permission.MANAGE_GUILD),
        position=1,
    )
    bot_member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=installed.bot_user_id,
        user_domain=installed.bot_user_domain,
        joined_at=datetime.now(UTC),
        member_version=4,
    )
    queue_mutation = AsyncMock()
    monkeypatch.setattr("app.bots.installations.queue_guild_mutation", queue_mutation)
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[role], [bot_member]]),
        execute=AsyncMock(
            side_effect=[
                [],
                Mock(),
                [(role.id, role.origin_domain)],
            ]
        ),
        delete=AsyncMock(),
    )

    deleted = await cleanup_installation_roles(
        session,
        SimpleNamespace(domain=guild.origin_domain),
        guild,
        actor,
        [installed],
    )

    assert deleted == []
    assert installed.role_id is None
    assert installed.role_domain is None
    bot_grant_delete = str(session.execute.await_args_list[1].args[0])
    assert "DELETE FROM member_roles" in bot_grant_delete
    assert "member_roles.user_id" in bot_grant_delete
    remaining_grants_query = str(session.execute.await_args_list[2].args[0])
    assert "SELECT DISTINCT member_roles.role_id" in remaining_grants_query
    member_query = str(session.scalars.await_args_list[1].args[0])
    assert "guild_members.user_id" in member_query
    assert "FOR UPDATE" in member_query
    assert bot_member.member_version == 5
    queue_mutation.assert_awaited_once_with(
        session,
        SimpleNamespace(domain=guild.origin_domain),
        guild,
        actor,
        "guild.member.role.remove",
        {
            "user": {
                "id": str(installed.bot_user_id),
                "origin_domain": installed.bot_user_domain,
            },
            "role": {"id": str(role.id), "origin_domain": role.origin_domain},
            "member_version": "5",
        },
        snapshot_required=True,
    )
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_instance_ban_endpoint_revokes_installations_in_member_delete_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _, _, auth = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    installed.status = "suspended"
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
    revoke = AsyncMock(return_value=[installed])
    cleanup = AsyncMock(return_value=[(90, guild.origin_domain)])
    publish_roles = AsyncMock()
    monkeypatch.setattr(moderation_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(moderation_api, "require_permissions", AsyncMock())
    monkeypatch.setattr(moderation_api, "revoke_installations_for_guild_instance", revoke)
    monkeypatch.setattr(moderation_api, "cleanup_installation_roles", cleanup)
    monkeypatch.setattr(moderation_api, "publish_deleted_installation_roles", publish_roles)
    monkeypatch.setattr(moderation_api, "cleanup_guild_member_threads", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation_api, "clear_tracker_assignees", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation_api, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))
    monkeypatch.setattr(moderation_api, "publish_guild_thread_member_cleanup", AsyncMock())
    monkeypatch.setattr(moderation_api, "publish_e2ee_policy_updates", AsyncMock())
    for name in (
        "add_audit_entry",
        "queue_guild_instance_access_revocation",
        "queue_guild_mutation",
        "wake_queued_guild_federation",
        "wake_tracker_membership_cleanup",
        "publish_dispatch",
    ):
        monkeypatch.setattr(moderation_api, name, AsyncMock())
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Mock(),
                Mock(),
                Mock(
                    all=Mock(
                        return_value=[
                            (
                                installed.bot_user_id,
                                installed.bot_user_domain,
                                "bot",
                            )
                        ]
                    )
                ),
                Mock(),
            ]
        ),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain="guild.example")

    await moderation_api.ban_instance(
        EntityRef("70@guild.example"),
        "apps.example",
        InstanceBanCreate(),
        auth,
        session,
        redis,
        SimpleNamespace(),
        settings,
        None,
    )

    revoke.assert_awaited_once_with(
        session,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        instance_domain="apps.example",
    )
    cleanup.assert_awaited_once_with(
        session,
        settings,
        guild,
        auth.user,
        [installed],
    )
    session.commit.assert_awaited_once()
    publish_roles.assert_awaited_once_with(
        redis,
        guild,
        [(90, guild.origin_domain)],
    )


@pytest.mark.asyncio
async def test_install_fails_closed_on_active_bot_or_instance_ban() -> None:
    guild, _, _, _ = moderation_fixture()
    bot = principal(scopes=set(), intents=set()).user

    user_banned_session = SimpleNamespace(scalar=AsyncMock(return_value=bot.id))
    with pytest.raises(HTTPException) as user_denied:
        await ensure_bot_install_allowed(user_banned_session, guild, bot)
    assert user_denied.value.status_code == 403
    assert user_denied.value.detail == {"code": "BOT_USER_BANNED"}
    user_query = str(user_banned_session.scalar.await_args.args[0])
    assert "bans.user_id" in user_query
    assert "bans.expires_at IS NULL" in user_query
    assert "FOR UPDATE" in user_query

    instance_banned_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, bot.origin_domain])
    )
    with pytest.raises(HTTPException) as instance_denied:
        await ensure_bot_install_allowed(instance_banned_session, guild, bot)
    assert instance_denied.value.status_code == 403
    assert instance_denied.value.detail == {"code": "BOT_INSTANCE_BANNED"}
    instance_query = str(instance_banned_session.scalar.await_args_list[1].args[0])
    assert "guild_instance_bans.instance_domain" in instance_query
    assert "guild_instance_bans.expires_at IS NULL" in instance_query
    assert "FOR UPDATE" in instance_query


@pytest.mark.asyncio
async def test_install_ban_fence_allows_only_when_both_active_checks_are_clear() -> None:
    guild, _, _, _ = moderation_fixture()
    bot = principal(scopes=set(), intents=set()).user
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, None]))

    await ensure_bot_install_allowed(session, guild, bot)

    assert session.scalar.await_count == 2


def test_active_installation_authority_accepts_guild_membership_or_user_install() -> None:
    membership_sql = str(installation_has_membership())
    assert "guild_members" in membership_sql
    assert "guild_members.guild_id = bot_installations.guild_id" in membership_sql
    assert "guild_members.user_id = bot_installations.bot_user_id" in membership_sql
    assert "guild_members.user_domain = bot_installations.bot_user_domain" in membership_sql

    authority_sql = str(
        active_installation_exists(
            application_id=20,
            application_domain="apps.example",
            bot_user_id=10,
            bot_user_domain="apps.example",
            current_instance_domain="guild.example",
        )
    )
    assert "bot_installations.status" in authority_sql
    assert "guild_members" in authority_sql
    assert "bot_user_installations.status" in authority_sql
    assert "bot_user_installations.revoked_at IS NULL" in authority_sql


@pytest.mark.asyncio
async def test_gateway_rejects_dangling_active_installation() -> None:
    bot = principal(scopes={"guilds.read"}, intents={"guilds"})
    bot.application.status = "active"
    bot.application.default_scopes = ["guilds.read"]
    bot.application.default_intents = ["guilds"]
    bot.worker.revoked_at = None
    bot.worker.expires_at = None
    bot.token.revoked_at = None
    result = Mock()
    result.one_or_none.return_value = (bot.token, bot.worker, bot.application, bot.user)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(return_value=[]),
    )

    assert (
        await current_gateway_authorization(
            session,
            bot,
            authority_domain="chat.example",
        )
        is None
    )
    installation_queries = [str(call.args[0]) for call in session.scalars.await_args_list]
    assert any(
        "guild_members" in query and "bot_installations.status" in query
        for query in installation_queries
    )
    assert any("bot_user_installations.status" in query for query in installation_queries)


@pytest.mark.asyncio
async def test_command_listing_filters_disabled_or_orphaned_bot_authority() -> None:
    result = Mock()
    result.all.return_value = []
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    guild, _, _, _ = moderation_fixture()

    assert await _local_application_commands(session, guild) == []
    query = str(session.execute.await_args.args[0])
    assert "bot_applications.status" in query
    assert "users.account_type" in query
    assert "users.disabled_at IS NULL" in query
    assert "guild_members" in query
    assert "bot_installations.bot_user_id = bot_applications.bot_user_id" in query


def moderation_fixture() -> tuple[Guild, User, GuildMember, SimpleNamespace]:
    actor = User(
        id=1,
        origin_domain="guild.example",
        is_local=True,
        account_type="human",
        username="owner",
        password_hash="hash",
    )
    guild = Guild(
        id=70,
        origin_domain="guild.example",
        name="Bots",
        owner_id=actor.id,
        owner_domain=actor.origin_domain,
        unavailable=False,
    )
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=10,
        user_domain="apps.example",
        joined_at=datetime.now(UTC),
    )
    return guild, actor, member, SimpleNamespace(user=actor)


def patch_moderation_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    guild: Guild,
    member: GuildMember,
) -> None:
    monkeypatch.setattr(moderation_api, "local_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(moderation_api, "require_permissions", AsyncMock())
    monkeypatch.setattr(moderation_api, "require_can_manage_member", AsyncMock(return_value=member))
    monkeypatch.setattr(moderation_api, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))
    for name in (
        "add_audit_entry",
        "clear_tracker_assignees",
        "cleanup_installation_roles",
        "cleanup_guild_member_threads",
        "publish_deleted_installation_roles",
        "publish_e2ee_policy_updates",
        "publish_guild_thread_member_cleanup",
        "queue_guild_access_revocation",
        "queue_guild_mutation",
        "wake_queued_guild_federation",
        "wake_tracker_membership_cleanup",
        "publish_dispatch",
    ):
        monkeypatch.setattr(moderation_api, name, AsyncMock())


@pytest.mark.asyncio
async def test_generic_kick_atomically_revokes_bot_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _, member, auth = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    installed.status = "suspended"
    patch_moderation_side_effects(monkeypatch, guild, member)
    settings = SimpleNamespace(domain="guild.example")
    target = principal(scopes=set(), intents=set()).user
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[installed]),
        get=AsyncMock(return_value=target),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )

    await moderation_api.kick_member(
        EntityRef("70@guild.example"),
        EntityRef("10@apps.example"),
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=123)),
        settings,
        None,
    )

    assert installed.status == "revoked"
    assert installed.revoked_at is not None
    moderation_api.cleanup_installation_roles.assert_awaited_once_with(
        session,
        settings,
        guild,
        auth.user,
        [installed],
    )
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_ban_revokes_bot_and_unban_never_reactivates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _, member, auth = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    target = principal(scopes=set(), intents=set()).user
    patch_moderation_side_effects(monkeypatch, guild, member)
    settings = SimpleNamespace(domain="guild.example")
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, target, member]),
        scalars=AsyncMock(return_value=[installed]),
        execute=AsyncMock(),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )

    await moderation_api.ban_member(
        EntityRef("70@guild.example"),
        EntityRef("10@apps.example"),
        BanCreate(),
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=123)),
        settings,
        None,
    )

    assert installed.status == "revoked"
    assert installed.revoked_at is not None
    moderation_api.cleanup_installation_roles.assert_awaited_once_with(
        session,
        settings,
        guild,
        auth.user,
        [installed],
    )

    delete_result = Mock()
    delete_result.scalar_one_or_none.return_value = target.id
    unban_session = SimpleNamespace(
        execute=AsyncMock(return_value=delete_result),
        commit=AsyncMock(),
    )
    await moderation_api.remove_ban(
        EntityRef("70@guild.example"),
        EntityRef("10@apps.example"),
        auth,
        unban_session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
        None,
    )

    assert installed.status == "revoked"
    assert installed.revoked_at is not None


def stub_application_target_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep role/membership lifecycle tests independent of target projection."""

    monkeypatch.setattr(
        "app.api.applications.revoke_bot_e2ee_access",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.api.applications.queue_application_target_snapshot",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.api.applications.queue_application_target_snapshots_for_refs",
        AsyncMock(return_value=set()),
    )
    monkeypatch.setattr(
        "app.api.applications.wake_application_target_deliveries",
        AsyncMock(),
    )


@pytest.mark.parametrize(
    ("application_domain", "expected_order"),
    [
        (
            "guild.example",
            ["permission", "guild.lock", "permission", "signer.lock", "application.query"],
        ),
        (
            "apps.example",
            [
                "permission",
                "manifest.fetch",
                "guild.lock",
                "permission",
                "signer.lock",
                "manifest.materialize",
                "application.query",
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_install_manifest_work_respects_guild_lock_order(
    monkeypatch: pytest.MonkeyPatch,
    application_domain: str,
    expected_order: list[str],
) -> None:
    guild, actor, _, auth = moderation_fixture()
    settings = SimpleNamespace(domain=guild.origin_domain)
    order: list[str] = []

    async def permissions(*_args: object) -> Permission:
        order.append("permission")
        return Permission.MANAGE_GUILD

    async def fetch_manifest(*_args: object) -> object:
        order.append("manifest.fetch")
        return object()

    async def lock_guild(statement: object) -> Guild:
        assert "FOR UPDATE" in str(statement)
        order.append("guild.lock")
        return guild

    async def lock_signer(*_args: object, **_kwargs: object) -> User:
        order.append("signer.lock")
        return actor

    async def materialize_manifest(*_args: object) -> tuple[object, object, object]:
        order.append("manifest.materialize")
        return object(), object(), object()

    async def stop_at_application_query(statement: object) -> None:
        assert "FOR UPDATE" in str(statement)
        order.append("application.query")
        raise RuntimeError("application query reached")

    fetch = AsyncMock(side_effect=fetch_manifest)
    materialize = AsyncMock(side_effect=materialize_manifest)
    monkeypatch.setattr(applications_api, "get_permissions", permissions)
    monkeypatch.setattr(applications_api, "fetch_bot_manifest", fetch)
    monkeypatch.setattr(applications_api, "guild_authority_owner", lock_signer)
    monkeypatch.setattr(applications_api, "materialize_remote_manifest", materialize)
    session = SimpleNamespace(
        get=AsyncMock(return_value=guild),
        scalar=AsyncMock(side_effect=lock_guild),
        execute=AsyncMock(side_effect=stop_at_application_query),
    )

    with pytest.raises(RuntimeError, match="application query reached"):
        await install_bot(
            EntityRef(f"{guild.id}@{guild.origin_domain}"),
            EntityRef(f"20@{application_domain}"),
            "default",
            auth,
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            settings,
        )

    assert order == expected_order
    if application_domain == settings.domain:
        fetch.assert_not_awaited()
        materialize.assert_not_awaited()
    else:
        fetch.assert_awaited_once()
        materialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_install_recheck_denial_does_not_materialize_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _, _, auth = moderation_fixture()
    settings = SimpleNamespace(domain=guild.origin_domain)
    order: list[str] = []
    permission_results = iter((Permission.MANAGE_GUILD, Permission(0)))

    async def permissions(*_args: object) -> Permission:
        order.append("permission")
        return next(permission_results)

    async def fetch_manifest(*_args: object) -> object:
        order.append("manifest.fetch")
        return object()

    async def lock_guild(_statement: object) -> Guild:
        order.append("guild.lock")
        return guild

    materialize = AsyncMock()
    signer = AsyncMock()
    monkeypatch.setattr(applications_api, "get_permissions", permissions)
    monkeypatch.setattr(applications_api, "fetch_bot_manifest", fetch_manifest)
    monkeypatch.setattr(applications_api, "guild_authority_owner", signer)
    monkeypatch.setattr(applications_api, "materialize_remote_manifest", materialize)
    session = SimpleNamespace(
        get=AsyncMock(return_value=guild),
        scalar=AsyncMock(side_effect=lock_guild),
    )

    with pytest.raises(HTTPException) as denied:
        await install_bot(
            EntityRef(f"{guild.id}@{guild.origin_domain}"),
            EntityRef("20@apps.example"),
            "default",
            auth,
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            settings,
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "MISSING_PERMISSIONS"}
    assert order == ["permission", "manifest.fetch", "guild.lock", "permission"]
    signer.assert_not_awaited()
    materialize.assert_not_awaited()


@pytest.mark.parametrize("prior_status", ["revoked", "suspended"])
@pytest.mark.asyncio
async def test_nonactive_reinstall_removes_stale_role_and_reduces_permissions(
    monkeypatch: pytest.MonkeyPatch,
    prior_status: str,
) -> None:
    stub_application_target_notifications(monkeypatch)
    guild, actor, _, auth = moderation_fixture()
    guild.permission_generation = 8
    bot = User(
        id=10,
        origin_domain=guild.origin_domain,
        is_local=True,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
    )
    application = BotApplication(
        id=20,
        origin_domain=guild.origin_domain,
        team_id=30,
        team_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
        supported_install_types=["guild_install"],
    )
    template = BotInstallTemplate(
        id=40,
        application_id=application.id,
        application_domain=application.origin_domain,
        slug="default",
        name="Default",
        scopes=["guilds.read"],
        intents=["guild_messages"],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    stale_role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Stale administrator",
        permissions=int(Permission.ADMINISTRATOR),
        position=1,
    )
    existing_member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=bot.id,
        user_domain=bot.origin_domain,
        joined_at=datetime.now(UTC),
        member_version=7,
    )
    installed = BotInstallation(
        id=60,
        application_id=application.id,
        application_domain=application.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        role_id=stale_role.id,
        role_domain=stale_role.origin_domain,
        installer_id=actor.id,
        installer_domain=actor.origin_domain,
        granted_scopes=["guilds.read"],
        granted_intents=["guild_messages"],
        granted_permissions=0,
        channel_restrictions=[f"123@{guild.origin_domain}"],
        e2ee_mode="disabled",
        grant_revision=2,
        status=prior_status,
        revoked_at=datetime.now(UTC) if prior_status == "revoked" else None,
    )
    invite_result = Mock()
    invite_result.one_or_none.return_value = (application, template, bot)

    async def get(model, key, **kwargs):
        del key, kwargs
        if model is Guild:
            return guild
        if model is InstanceBlock:
            return None
        raise AssertionError(f"unexpected get for {model}")

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[guild, guild, actor, None, None, installed, existing_member]),
        execute=AsyncMock(side_effect=[invite_result, [], Mock(), [], Mock()]),
        scalars=AsyncMock(side_effect=[[stale_role], []]),
        add=Mock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace()
    settings = SimpleNamespace(domain=guild.origin_domain)
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=91))
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    queue_mutation = AsyncMock()
    monkeypatch.setattr("app.api.applications.queue_guild_mutation", queue_mutation)
    cleanup_mutation = AsyncMock()
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        cleanup_mutation,
    )
    publish_deleted_roles = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.publish_deleted_installation_roles",
        publish_deleted_roles,
    )
    monkeypatch.setattr(
        "app.api.applications.wake_queued_guild_federation",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.applications.publish_dispatch", AsyncMock())

    response = await install_bot(
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        EntityRef(f"{application.id}@{application.origin_domain}"),
        template.slug,
        auth,
        session,
        redis,
        snowflake,
        settings,
    )

    assert response["status"] == "active"
    assert installed.status == "active"
    assert installed.revoked_at is None
    assert installed.grant_revision == 3
    assert installed.channel_restrictions == [f"123@{guild.origin_domain}"]
    assert response["channel_restrictions"] == [f"123@{guild.origin_domain}"]
    assert (installed.role_id, installed.role_domain) == (91, guild.origin_domain)
    assert installed.granted_permissions == 0
    assert guild.permission_generation == 10
    added = [call.args[0] for call in session.add.call_args_list]
    new_role = next(item for item in added if isinstance(item, Role))
    new_member_role = next(item for item in added if isinstance(item, MemberRole))
    assert all(not isinstance(item, GuildMember) for item in added)
    assert existing_member.member_version == 8
    assert new_role.id == 91
    assert new_role.permissions == 0
    assert new_member_role.role_id == 91
    assert new_member_role.role_id != stale_role.id
    signer_query = str(session.scalar.await_args_list[2].args[0])
    assert "users.id" in signer_query
    assert "FOR UPDATE" in signer_query
    invite_query = str(session.execute.await_args_list[0].args[0])
    assert "bot_applications.status" in invite_query
    assert "FOR UPDATE" in invite_query
    session.delete.assert_awaited_once_with(stale_role)
    stale_grant_delete = str(session.execute.await_args_list[2].args[0])
    assert "DELETE FROM member_roles" in stale_grant_delete
    assert "member_roles.user_id" in stale_grant_delete
    cleanup_mutation.assert_awaited_once()
    assert queue_mutation.await_count == 3
    session.commit.assert_awaited_once()
    publish_deleted_roles.assert_awaited_once_with(
        redis,
        guild,
        [(stale_role.id, stale_role.origin_domain)],
    )


@pytest.mark.asyncio
async def test_reinstall_federates_retained_old_role_removal_before_new_role_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_application_target_notifications(monkeypatch)
    guild, owner, _, auth = moderation_fixture()
    guild.permission_generation = 8
    bot = User(
        id=10,
        origin_domain=guild.origin_domain,
        is_local=True,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
    )
    application = BotApplication(
        id=20,
        origin_domain=guild.origin_domain,
        team_id=30,
        team_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
        supported_install_types=["guild_install"],
    )
    template = BotInstallTemplate(
        id=40,
        application_id=application.id,
        application_domain=application.origin_domain,
        slug="default",
        name="Default",
        scopes=["guilds.read"],
        intents=["guild_messages"],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    old_role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Shared stale administrator",
        permissions=int(Permission.ADMINISTRATOR),
        position=1,
    )
    member = GuildMember(
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=bot.id,
        user_domain=bot.origin_domain,
        joined_at=datetime.now(UTC),
        member_version=7,
    )
    installed = BotInstallation(
        id=60,
        application_id=application.id,
        application_domain=application.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        role_id=old_role.id,
        role_domain=old_role.origin_domain,
        installer_id=owner.id,
        installer_domain=owner.origin_domain,
        granted_scopes=["guilds.read"],
        granted_intents=["guild_messages"],
        granted_permissions=int(Permission.ADMINISTRATOR),
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=2,
        status="suspended",
    )
    invite_result = Mock()
    invite_result.one_or_none.return_value = (application, template, bot)

    async def get(model, key, **kwargs):
        del key, kwargs
        if model is Guild:
            return guild
        if model is InstanceBlock:
            return None
        raise AssertionError(f"unexpected get for {model}")

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[guild, guild, owner, None, None, installed, member]),
        execute=AsyncMock(
            side_effect=[
                invite_result,
                [],
                Mock(),
                [(old_role.id, old_role.origin_domain)],
            ]
        ),
        # Cleanup locks the old role and member; install then locks all retained roles.
        scalars=AsyncMock(side_effect=[[old_role], [member], [old_role]]),
        add=Mock(),
        delete=AsyncMock(),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )
    mutation_order: list[tuple[str, dict[str, object]]] = []

    async def record_mutation(*args, **kwargs) -> None:
        del kwargs
        mutation_order.append((args[4], args[5]))

    cleanup_mutation = AsyncMock(side_effect=record_mutation)
    application_mutation = AsyncMock(side_effect=record_mutation)
    publish_dispatch = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        cleanup_mutation,
    )
    monkeypatch.setattr(
        "app.api.applications.queue_guild_mutation",
        application_mutation,
    )
    monkeypatch.setattr("app.api.applications.wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(
        "app.api.applications.publish_deleted_installation_roles",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.applications.publish_dispatch", publish_dispatch)

    await install_bot(
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        EntityRef(f"{application.id}@{application.origin_domain}"),
        template.slug,
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=91)),
        SimpleNamespace(domain=guild.origin_domain),
    )

    assert [event for event, _ in mutation_order] == [
        "guild.member.role.remove",
        "guild.role.create",
        "guild.member.add",
        "guild.member.role.add",
    ]
    assert mutation_order[0][1]["member_version"] == "8"
    assert mutation_order[-1][1]["member_version"] == "9"
    assert member.member_version == 9
    assert guild.permission_generation == 9
    assert (installed.role_id, installed.role_domain) == (91, guild.origin_domain)
    assert installed.granted_permissions == 0
    assert old_role.position == 2
    session.refresh.assert_awaited_once_with(old_role, attribute_names=("updated_at",))
    session.delete.assert_not_awaited()
    assert publish_dispatch.await_args_list[-1].args[2] == "GUILD_MEMBER_ADD"
    assert publish_dispatch.await_args_list[-1].args[3]["role_ids"] == ["91"]


@pytest.mark.parametrize("federated_caller", [False, True])
@pytest.mark.asyncio
async def test_post_transfer_install_uses_remote_owner_for_local_and_federated_callers(
    monkeypatch: pytest.MonkeyPatch,
    federated_caller: bool,
) -> None:
    guild, local_manager, _, _ = moderation_fixture()
    guild.permission_generation = 1
    remote_installer = User(
        id=80,
        origin_domain="remote.example",
        is_local=False,
        account_type="human",
        username="remote-admin",
        password_hash=None,
    )
    remote_owner = User(
        id=81,
        origin_domain="owner.example",
        is_local=False,
        account_type="human",
        username="remote-owner",
        password_hash=None,
    )
    guild.owner_id = remote_owner.id
    guild.owner_domain = remote_owner.origin_domain
    caller = remote_installer if federated_caller else local_manager
    bot = User(
        id=10,
        origin_domain=guild.origin_domain,
        is_local=True,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
    )
    application = BotApplication(
        id=20,
        origin_domain=guild.origin_domain,
        team_id=30,
        team_domain=guild.origin_domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
        supported_install_types=["guild_install"],
    )
    template = BotInstallTemplate(
        id=40,
        application_id=application.id,
        application_domain=application.origin_domain,
        slug="default",
        name="Default",
        scopes=["guilds.read"],
        intents=["guild_messages"],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    invite_result = Mock()
    invite_result.one_or_none.return_value = (application, template, bot)

    async def get(model, key, **kwargs):
        del key, kwargs
        if model is Guild:
            return guild
        if model is InstanceBlock:
            return None
        raise AssertionError(f"unexpected get for {model}")

    persistence_order: list[str] = []

    def record_add(value: object) -> None:
        if isinstance(value, (Role, BotInstallation)):
            persistence_order.append(f"add:{type(value).__name__}")

    async def record_flush(**kwargs: object) -> None:
        objects = kwargs.get("objects")
        if objects is None:
            return
        assert isinstance(objects, list)
        assert len(objects) == 1 and isinstance(objects[0], Role)
        persistence_order.append("flush")

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[guild, guild, remote_owner, None, None, None, None]),
        execute=AsyncMock(return_value=invite_result),
        scalars=AsyncMock(return_value=[]),
        add=Mock(side_effect=record_add),
        flush=AsyncMock(side_effect=record_flush),
        commit=AsyncMock(),
    )
    queue_mutation = AsyncMock()
    target_snapshot = AsyncMock(return_value="target.example")
    wake_targets = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr("app.api.applications.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr("app.api.applications.wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(
        "app.api.applications.queue_application_target_snapshot",
        target_snapshot,
    )
    monkeypatch.setattr(
        "app.api.applications.wake_application_target_deliveries",
        wake_targets,
    )
    monkeypatch.setattr("app.api.applications.publish_dispatch", AsyncMock())
    monkeypatch.setattr(
        "app.api.applications.publish_deleted_installation_roles",
        AsyncMock(),
    )

    response = await install_bot(
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        EntityRef(f"{application.id}@{application.origin_domain}"),
        template.slug,
        SimpleNamespace(user=caller),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(side_effect=[91, 92])),
        SimpleNamespace(domain=guild.origin_domain),
    )

    assert response["id"] == "92"
    assert persistence_order == ["add:Role", "flush", "add:BotInstallation"]
    added = [call.args[0] for call in session.add.call_args_list]
    installed = next(item for item in added if isinstance(item, BotInstallation))
    assert (installed.installer_id, installed.installer_domain) == (
        caller.id,
        caller.origin_domain,
    )
    assert queue_mutation.await_count == 3
    assert [call.args[4] for call in queue_mutation.await_args_list] == [
        "guild.role.create",
        "guild.member.add",
        "guild.member.role.add",
    ]
    assert all(call.args[3] is remote_owner for call in queue_mutation.await_args_list)
    signer_query = str(session.scalar.await_args_list[2].args[0])
    assert "users.origin_domain" in signer_query
    assert "users.is_local IS true" not in signer_query
    assert "FOR UPDATE" in signer_query
    target_snapshot.assert_awaited_once_with(
        session,
        SimpleNamespace(domain=guild.origin_domain),
        application,
        bot,
        force=True,
    )
    wake_targets.assert_awaited_once_with({"target.example"})
    session.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "application_present",
    [True, False],
    ids=["normal-application-projection", "missing-application-orphan"],
)
@pytest.mark.parametrize("federated_caller", [False, True])
@pytest.mark.asyncio
async def test_post_transfer_uninstall_uses_remote_owner_for_local_and_federated_callers(
    monkeypatch: pytest.MonkeyPatch,
    federated_caller: bool,
    application_present: bool,
) -> None:
    guild, local_manager, member, _ = moderation_fixture()
    remote_installer = User(
        id=80,
        origin_domain="remote.example",
        is_local=False,
        account_type="human",
        username="remote-admin",
        password_hash=None,
    )
    remote_owner = User(
        id=81,
        origin_domain="owner.example",
        is_local=False,
        account_type="human",
        username="remote-owner",
        password_hash=None,
    )
    guild.owner_id = remote_owner.id
    guild.owner_domain = remote_owner.origin_domain
    caller = remote_installer if federated_caller else local_manager
    installed = installation(scopes={"guilds.read"})
    installed.application_domain = guild.origin_domain
    installed.bot_user_domain = member.user_domain
    installed.installer_id = caller.id
    installed.installer_domain = caller.origin_domain
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
    application = (
        SimpleNamespace(
            id=installed.application_id,
            origin_domain=installed.application_domain,
            bot_user_id=installed.bot_user_id,
            bot_user_domain=installed.bot_user_domain,
        )
        if application_present
        else None
    )
    bot = User(
        id=installed.bot_user_id,
        origin_domain=installed.bot_user_domain,
        is_local=True,
        account_type="bot",
        username="installed-bot",
        password_hash=None,
    )
    member.member_version = 3
    role = Role(
        id=90,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Shared with human",
        permissions=int(Permission.MANAGE_GUILD),
        position=1,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[guild, remote_owner, application, installed]),
        scalars=AsyncMock(side_effect=[[role], [member]]),
        execute=AsyncMock(side_effect=[[], Mock(), [(role.id, role.origin_domain)]]),
        get=AsyncMock(side_effect=[member, bot] if application_present else [member]),
        delete=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    queue_mutation = AsyncMock()
    cleanup_mutation = AsyncMock()
    publish_roles = AsyncMock()
    target_snapshots = AsyncMock(return_value={"target.example"})
    wake_targets = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr("app.api.applications.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(
        "app.api.applications.revoke_bot_e2ee_access",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        cleanup_mutation,
    )
    monkeypatch.setattr(
        "app.api.applications.cleanup_guild_member_threads",
        AsyncMock(return_value=[]),
    )
    clear_assignees = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "app.api.applications.clear_tracker_assignees",
        clear_assignees,
    )
    monkeypatch.setattr("app.api.applications.publish_guild_thread_member_cleanup", AsyncMock())
    monkeypatch.setattr("app.api.applications.wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr("app.api.applications.wake_tracker_membership_cleanup", AsyncMock())
    monkeypatch.setattr(
        "app.api.applications.queue_application_target_snapshots_for_refs",
        target_snapshots,
    )
    monkeypatch.setattr(
        "app.api.applications.wake_application_target_deliveries",
        wake_targets,
    )
    monkeypatch.setattr(
        "app.api.applications.publish_deleted_installation_roles",
        publish_roles,
    )
    monkeypatch.setattr("app.api.applications.publish_dispatch", AsyncMock())
    settings = SimpleNamespace(domain=guild.origin_domain)
    redis = SimpleNamespace()

    await _uninstall_bot_from_local_guild(
        guild,
        EntityRef(f"{installed.application_id}@{installed.application_domain}"),
        SimpleNamespace(user=caller),
        session,
        redis,
        settings,
    )

    assert installed.status == "revoked"
    assert installed.revoked_at is not None
    assert installed.role_id is None
    assert installed.role_domain is None
    assert (installed.installer_id, installed.installer_domain) == (
        caller.id,
        caller.origin_domain,
    )
    session.delete.assert_awaited_once_with(member)
    bot_grant_delete = str(session.execute.await_args_list[1].args[0])
    assert "DELETE FROM member_roles" in bot_grant_delete
    assert "member_roles.user_id" in bot_grant_delete
    assert member.member_version == 4
    cleanup_mutation.assert_awaited_once()
    assert cleanup_mutation.await_args.args[3] is remote_owner
    assert cleanup_mutation.await_args.args[4] == "guild.member.role.remove"
    assert cleanup_mutation.await_args.args[5]["member_version"] == "4"
    queue_mutation.assert_awaited_once()
    assert queue_mutation.await_args.args[3] is remote_owner
    assert queue_mutation.await_args.args[4] == "guild.member.remove"
    publish_roles.assert_awaited_once_with(redis, guild, [])
    clear_assignees.assert_awaited_once_with(
        session,
        settings,
        guild,
        remote_owner,
        [(installed.bot_user_id, installed.bot_user_domain)],
    )
    if application_present:
        target_snapshots.assert_awaited_once_with(
            session,
            settings,
            {(installed.application_id, installed.application_domain)},
            force=True,
        )
        wake_targets.assert_awaited_once_with({"target.example"})
    else:
        target_snapshots.assert_not_awaited()
        wake_targets.assert_not_awaited()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_uninstall_cannot_remove_a_legacy_bot_guild_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, _, _, _ = moderation_fixture()
    installed = installation(scopes={"guilds.read"})
    guild.owner_id = installed.bot_user_id
    guild.owner_domain = installed.bot_user_domain
    owner_bot = principal(scopes=set(), intents=set()).user
    owner_bot.is_local = True
    owner_bot.origin_domain = guild.origin_domain
    application = SimpleNamespace(
        id=installed.application_id,
        origin_domain=installed.application_domain,
        bot_user_id=installed.bot_user_id,
        bot_user_domain=installed.bot_user_domain,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[guild, owner_bot, application, installed]),
        delete=AsyncMock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )

    with pytest.raises(HTTPException) as denied:
        await _uninstall_bot_from_local_guild(
            guild,
            EntityRef(f"{installed.application_id}@{installed.application_domain}"),
            SimpleNamespace(user=owner_bot),
            session,
            SimpleNamespace(),
            SimpleNamespace(domain=guild.origin_domain),
        )

    assert denied.value.status_code == 409
    assert denied.value.detail == {"code": "OWNER_MUST_TRANSFER_OR_DELETE_GUILD"}
    assert installed.status == "active"
    session.delete.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_remote_refresh_preserves_local_application_and_worker_holds() -> None:
    bot = principal(scopes=set(), intents=set())
    for status in ("review_required", "suspended", "deleting", "deleted"):
        bot.application.status = status
        activate_remote_application_if_permitted(bot.application, created=False)
        assert bot.application.status == status
        assert bot.application.status != "active"  # reinstall queries remain fail-closed

    revoked_at = datetime.now(UTC)
    bot.worker.revoked_at = revoked_at
    restore_remote_worker_if_new(bot.worker, created=False)
    assert bot.worker.revoked_at == revoked_at


@pytest.mark.parametrize(
    ("account_type", "disabled_at"),
    [
        ("human", None),
        ("bot", datetime.now(UTC)),
    ],
)
@pytest.mark.asyncio
async def test_home_manifest_never_exports_non_bot_or_disabled_identity(
    account_type: str,
    disabled_at: datetime | None,
) -> None:
    bot = principal(scopes=set(), intents=set()).user
    bot.account_type = account_type
    bot.disabled_at = disabled_at
    application = BotApplication(
        id=20,
        origin_domain="local.example",
        team_id=30,
        team_domain="local.example",
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
    )
    template = BotInstallTemplate(
        id=30,
        application_id=application.id,
        application_domain=application.origin_domain,
        slug="default",
        name="Default",
        scopes=[],
        intents=[],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    result = Mock()
    result.one_or_none.return_value = (application, template, bot)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        scalars=AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await local_manifest(
            session, application.id, template.slug, SimpleNamespace(domain="local.example")
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "BOT_INVITE_NOT_FOUND"}
    session.scalars.assert_not_awaited()
    query = str(session.execute.await_args.args[0])
    assert "users.account_type" in query
    assert "users.disabled_at IS NULL" in query
    assert not enabled_bot_identity(bot)


@pytest.mark.asyncio
async def test_home_manifest_exports_all_discord_command_type_limits() -> None:
    bot = User(
        id=10,
        origin_domain="local.example",
        is_local=True,
        account_type="bot",
        username="commands_bot",
        password_hash=None,
        profile_version=1,
        e2ee_device_generation=0,
        profile_resolved=True,
    )
    application = BotApplication(
        id=20,
        origin_domain="local.example",
        team_id=30,
        team_domain="local.example",
        bot_user_id=10,
        bot_user_domain="local.example",
        name="Commands",
        status="active",
        target_policy="open",
        default_scopes=[],
        default_intents=[],
        default_permissions=0,
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild", "bot_dm", "private_channel"],
        e2ee_modes=["participant"],
        manifest_generation=1,
        command_generation=1,
    )
    template = BotInstallTemplate(
        id=30,
        application_id=20,
        application_domain="local.example",
        slug="default",
        name="Default",
        scopes=[],
        intents=[],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    definitions = (
        [
            CommandDefinition(name=f"command-{index}", description="Run a command")
            for index in range(100)
        ]
        + [CommandDefinition(name=f"User action {index}", type="user") for index in range(15)]
        + [CommandDefinition(name=f"Message action {index}", type="message") for index in range(15)]
    )
    commands = [
        ApplicationCommand(
            id=1000 + index,
            application_id=20,
            application_domain="local.example",
            name=definition.name,
            type=definition.type,
            definition=definition.model_dump(mode="json"),
            contexts=list(definition.contexts),
            integration_types=list(definition.integration_types),
            generation=1,
            state="active",
        )
        for index, definition in enumerate(definitions)
    ]
    row = Mock()
    row.one_or_none.return_value = (application, template, bot)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=row),
        scalars=AsyncMock(side_effect=[[], commands, []]),
    )

    manifest, _ = await local_manifest(
        session,
        application.id,
        template.slug,
        SimpleNamespace(domain="local.example"),
    )

    assert len(manifest.commands) == 130
    assert {
        kind: sum(command.type == kind for command in manifest.commands)
        for kind in {
            "chat_input",
            "user",
            "message",
        }
    } == {"chat_input": 100, "user": 15, "message": 15}
    command_query = session.scalars.await_args_list[1].args[0]
    assert "LIMIT 130" in str(command_query.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    ("account_type", "disabled_at"),
    [
        ("human", None),
        ("bot", datetime.now(UTC)),
    ],
)
@pytest.mark.asyncio
async def test_disabled_home_bot_cannot_renew_federated_worker_authorization(
    monkeypatch: pytest.MonkeyPatch,
    account_type: str,
    disabled_at: datetime | None,
) -> None:
    bot = principal(scopes=set(), intents=set()).user
    bot.account_type = account_type
    bot.disabled_at = disabled_at
    application = BotApplication(
        id=20,
        origin_domain="local.example",
        team_id=30,
        team_domain="local.example",
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
    )
    worker = BotWorker(
        id=40,
        application_id=application.id,
        application_domain=application.origin_domain,
        name="production",
        public_key=b"x" * 32,
        scopes=[],
        intents=[],
        target_domains=[],
    )
    result = Mock()
    result.one_or_none.return_value = (application, worker, bot)
    session = SimpleNamespace(execute=AsyncMock(return_value=result), scalars=AsyncMock())
    monkeypatch.setattr(
        "app.api.bot_federation.enforce_federation_route_rate_limit",
        AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await federation_worker_authorization(
            application.id,
            worker.id,
            SimpleNamespace(origin="target.example", silenced=False),
            session,
            SimpleNamespace(),
            SimpleNamespace(domain="local.example"),
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "BOT_WORKER_NOT_FOUND"}
    session.scalars.assert_not_awaited()
    query = str(session.execute.await_args.args[0])
    assert "users.account_type" in query
    assert "users.disabled_at IS NULL" in query


@pytest.mark.asyncio
async def test_remote_manifest_refresh_cannot_reactivate_a_suspended_mirror(
    monkeypatch,
) -> None:
    remote_bot = User(
        id=10,
        origin_domain="apps.example",
        is_local=False,
        account_type="bot",
        username="weather_bot",
        password_hash=None,
        profile_resolved=True,
        federation_introduced_by_domain="apps.example",
    )
    team = DeveloperTeam(
        id=20,
        origin_domain="apps.example",
        name="Remote developer",
        personal=False,
    )
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=20,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="suspended",
    )
    template = BotInstallTemplate(
        id=30,
        application_id=20,
        application_domain="apps.example",
        slug="default",
        name="Default",
        scopes=[],
        intents=[],
        permissions=0,
        contexts=["guild"],
        e2ee_mode="disabled",
        generation=1,
        active=True,
    )
    emoji = ApplicationEmoji(
        id=81,
        application_id=20,
        application_domain="apps.example",
        name="old_name",
        name_casefold="old_name",
        media_hash="a" * 64,
        object_key="must-not-survive-on-a-mirror",
        animated=False,
        available=True,
        creator_id=10,
        creator_domain="apps.example",
        version=1,
    )
    absent_worker = BotWorker(
        id=91,
        application_id=20,
        application_domain="apps.example",
        name="removed",
        public_key=b"x" * 32,
        scopes=[],
        intents=[],
        target_domains=[],
        generation=1,
    )
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": "20",
                "origin_domain": "apps.example",
                "team_id": "20",
                "team_domain": "apps.example",
                "name": "Weather refreshed",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": "0",
                "e2ee_modes": ["participant"],
                "manifest_generation": "2",
                "command_generation": "2",
                "bot_user": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "account_type": "bot",
                    "username": "weather_bot",
                },
            },
            "template": {
                "id": "30",
                "slug": "default",
                "name": "Default",
                "scopes": [],
                "intents": [],
                "permissions": "0",
                "contexts": ["guild"],
                "e2ee_mode": "disabled",
                "generation": "2",
            },
            "workers": [],
            "commands": [],
            "emojis": [
                {
                    "id": "81",
                    "name": "party_blob",
                    "media_hash": "b" * 64,
                    "animated": True,
                    "available": False,
                    "version": "3",
                }
            ],
        }
    )

    async def get(model, key):
        return {
            User: remote_bot,
            DeveloperTeam: team,
            BotApplication: application,
            BotInstallTemplate: template,
            ApplicationEmoji: emoji,
        }.get(model)

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        add=Mock(),
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(side_effect=[[], [], [absent_worker]]),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.upsert_remote_user",
        AsyncMock(return_value=remote_bot),
    )

    refreshed, _, _ = await materialize_remote_manifest(
        session,
        manifest,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(mint=AsyncMock(return_value=500)),
    )

    assert refreshed.name == "Weather refreshed"
    assert refreshed.status == "suspended"
    # install_bot selects only status="active", so a reinstall stays denied.
    assert refreshed.status != "active"
    assert absent_worker.revoked_at is not None
    assert emoji.name == "party_blob"
    assert emoji.name_casefold == "party_blob"
    assert emoji.media_hash == "b" * 64
    assert emoji.object_key is None
    assert emoji.animated is True
    assert emoji.available is False
    assert emoji.version == 3


@pytest.mark.asyncio
async def test_remote_manifest_preserves_generations_and_worker_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = datetime(2026, 9, 1, tzinfo=UTC)
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=20,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="active",
        target_policy="open",
        default_scopes=[],
        default_intents=[],
        default_permissions=0,
        supported_install_types=["guild_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild", "bot_dm", "private_channel"],
        e2ee_modes=["participant"],
        manifest_generation=1,
        command_generation=1,
    )
    bot = SimpleNamespace(id=10, origin_domain="apps.example", account_type="bot")
    team = SimpleNamespace(id=20, origin_domain="apps.example")
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="old",
        public_key=b"x" * 32,
        scopes=[],
        intents=[],
        target_domains=[],
        generation=1,
    )
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": "20",
                "origin_domain": "apps.example",
                "team_id": "20",
                "team_domain": "apps.example",
                "name": "Weather",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": "0",
                "e2ee_modes": ["participant"],
                "manifest_generation": "2",
                "command_generation": "2",
                "bot_user": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "account_type": "bot",
                    "username": "weather_bot",
                },
            },
            "template": {
                "id": "30",
                "slug": "default",
                "name": "Default",
                "scopes": [],
                "intents": [],
                "permissions": "0",
                "contexts": ["guild"],
                "e2ee_mode": "disabled",
                "generation": "2",
            },
            "workers": [
                {
                    "id": "40",
                    "name": "current",
                    "public_key": base64.urlsafe_b64encode(b"y" * 32).decode().rstrip("="),
                    "scopes": [],
                    "intents": [],
                    "target_domains": [],
                    "generation": "2",
                    "expires_at": expires_at.isoformat(),
                }
            ],
            "commands": [],
            "emojis": [],
        }
    )

    async def get(model: object, _key: object) -> object | None:
        return {
            BotApplication: application,
            User: bot,
            DeveloperTeam: team,
            BotWorker: worker,
        }.get(model)

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        add=Mock(),
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(side_effect=[[], [], []]),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.upsert_remote_user",
        AsyncMock(return_value=bot),
    )

    await materialize_remote_manifest(
        session,
        manifest,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(mint=AsyncMock(return_value=500)),
        materialize_template=False,
    )

    assert application.manifest_generation == 2
    assert application.command_generation == 2
    assert worker.name == "current"
    assert worker.public_key == b"y" * 32
    assert worker.generation == 2
    assert worker.expires_at == expires_at

    application.manifest_generation = 3
    with pytest.raises(FederationNetworkError, match="rolls back"):
        await materialize_remote_manifest(
            session,
            manifest,
            SimpleNamespace(domain="local.example"),
            SimpleNamespace(mint=AsyncMock(return_value=500)),
            materialize_template=False,
        )


@pytest.mark.asyncio
async def test_remote_manifest_mints_local_children_when_authority_ids_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=20,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="active",
        manifest_generation=1,
        command_generation=1,
    )
    bot = SimpleNamespace(id=10, origin_domain="apps.example", account_type="bot")
    team = SimpleNamespace(id=20, origin_domain="apps.example")
    colliding_command = ApplicationCommand(
        id=77,
        application_id=30,
        application_domain="other.example",
        name="local-command",
        type="chat_input",
        definition={"name": "local-command", "type": "chat_input"},
        contexts=["guild"],
        integration_types=["guild_install"],
        generation=1,
        state="active",
    )
    colliding_worker = BotWorker(
        id=88,
        application_id=30,
        application_domain="other.example",
        name="local-worker",
        public_key=b"z" * 32,
    )
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": "20",
                "origin_domain": "apps.example",
                "team_id": "20",
                "team_domain": "apps.example",
                "name": "Weather",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": "0",
                "e2ee_modes": ["participant"],
                "manifest_generation": "2",
                "command_generation": "2",
                "bot_user": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "account_type": "bot",
                    "username": "weather_bot",
                },
            },
            "template": {
                "id": "30",
                "slug": "default",
                "name": "Default",
                "scopes": [],
                "intents": [],
                "permissions": "0",
                "contexts": ["guild"],
                "e2ee_mode": "disabled",
                "generation": "2",
            },
            "workers": [
                {
                    "id": "88",
                    "name": "production",
                    "public_key": base64.urlsafe_b64encode(b"y" * 32).decode().rstrip("="),
                    "scopes": [],
                    "intents": [],
                    "target_domains": [],
                    "generation": "2",
                }
            ],
            "commands": [
                {
                    "id": "77",
                    "name": "weather",
                    "description": "Show the weather",
                    "type": "chat_input",
                    "contexts": ["guild"],
                    "integration_types": ["guild_install"],
                }
            ],
            "emojis": [],
        }
    )

    async def get(model: object, key: object) -> object | None:
        if model is BotApplication:
            return application
        if model is User:
            return bot
        if model is DeveloperTeam:
            return team
        if model is ApplicationCommand and key == 77:
            return colliding_command
        if model is BotWorker and key == 88:
            return colliding_worker
        return None

    added: list[object] = []
    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(side_effect=[[], [], []]),
        add=added.append,
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.upsert_remote_user",
        AsyncMock(return_value=bot),
    )

    await materialize_remote_manifest(
        session,  # type: ignore[arg-type]
        manifest,
        SimpleNamespace(domain="local.example"),  # type: ignore[arg-type]
        SimpleNamespace(mint=AsyncMock(side_effect=[500, 501])),  # type: ignore[arg-type]
        materialize_template=False,
    )

    command = next(item for item in added if isinstance(item, ApplicationCommand))
    worker = next(item for item in added if isinstance(item, BotWorker))
    assert (command.id, command.source_id, command.source_domain) == (
        500,
        77,
        "apps.example",
    )
    assert command.authority_id == 77
    assert (worker.id, worker.source_id, worker.source_domain) == (
        501,
        88,
        "apps.example",
    )
    assert worker.authority_id == 88
    assert colliding_command.application_domain == "other.example"
    assert colliding_worker.application_domain == "other.example"


@pytest.mark.asyncio
async def test_remote_worker_refresh_materializes_changed_commands_and_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=20,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="active",
        manifest_generation=1,
        command_generation=1,
        revocation_generation=1,
    )
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"x" * 32,
        scopes=[],
        intents=[],
        target_domains=[],
        generation=1,
    )
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": "20",
                "origin_domain": "apps.example",
                "team_id": "20",
                "team_domain": "apps.example",
                "name": "Weather",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": "0",
                "e2ee_modes": ["participant"],
                "manifest_generation": "3",
                "command_generation": "4",
                "bot_user": {
                    "id": "10",
                    "origin_domain": "apps.example",
                    "account_type": "bot",
                    "username": "weather_bot",
                },
            },
            "template": {
                "id": "30",
                "slug": "default",
                "name": "Default",
                "scopes": [],
                "intents": [],
                "permissions": "0",
                "contexts": ["guild"],
                "e2ee_mode": "disabled",
                "generation": "3",
            },
            "workers": [],
            "commands": [],
            "emojis": [],
        }
    )
    authorization = {
        "application_id": "20",
        "application_domain": "apps.example",
        "bot_user_id": "10",
        "worker": {
            "id": "40",
            "name": "production-v2",
            "public_key": base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
            "scopes": ["guilds.read"],
            "intents": ["guilds"],
            "target_domains": [],
            "generation": "2",
        },
        "manifest_generation": "3",
        "command_generation": "4",
        "revocation_generation": "2",
    }

    async def get(model: object, _key: object) -> object | None:
        return {BotApplication: application, BotWorker: worker}.get(model)

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=["default", None]),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.signed_request",
        AsyncMock(return_value=SimpleNamespace(status_code=200)),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.decode_federation_response_json", Mock(return_value={})
    )
    monkeypatch.setattr(
        "app.api.bot_federation.validated_event_envelope",
        AsyncMock(
            return_value=SimpleNamespace(
                type="bot.worker.authorization",
                actor=SimpleNamespace(id="10", domain="apps.example"),
                content=authorization,
            )
        ),
    )
    fetch = AsyncMock(return_value=manifest)

    async def materialize(*_args: object, **_kwargs: object) -> tuple[object, object, object]:
        application.manifest_generation = 3
        application.command_generation = 4
        worker.generation = 3  # Simulate a manifest racing ahead of authorization.
        return application, object(), object()

    materialize_mock = AsyncMock(side_effect=materialize)
    monkeypatch.setattr("app.api.bot_federation.fetch_bot_manifest", fetch)
    monkeypatch.setattr("app.api.bot_federation.materialize_remote_manifest", materialize_mock)

    await refresh_remote_worker_authorization(
        session,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(mint=AsyncMock(return_value=500)),
        20,
        "apps.example",
        40,
    )

    fetch.assert_awaited_once()
    materialize_mock.assert_awaited_once()
    assert application.manifest_generation == 3
    assert application.command_generation == 4
    assert application.revocation_generation == 2
    # The older authorization response must not roll back a newer worker from
    # the subsequently fetched manifest.
    assert worker.generation == 3
    assert worker.name == "production"


@pytest.mark.asyncio
async def test_remote_worker_refresh_supports_user_install_only_mirrors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=20,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        status="active",
        manifest_generation=1,
        command_generation=1,
        revocation_generation=1,
    )
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"x" * 32,
        scopes=[],
        intents=[],
        target_domains=[],
        generation=1,
    )
    authorization = {
        "application_id": "20",
        "application_domain": "apps.example",
        "bot_user_id": "10",
        "worker": {
            "id": "40",
            "name": "production",
            "public_key": base64.urlsafe_b64encode(b"x" * 32).decode().rstrip("="),
            "scopes": [],
            "intents": [],
            "target_domains": [],
            "generation": "2",
        },
        "manifest_generation": "2",
        "command_generation": "2",
        "revocation_generation": "2",
    }

    async def get(model: object, _key: object) -> object | None:
        return {BotApplication: application, BotWorker: worker}.get(model)

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[None, 77, None]),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.signed_request",
        AsyncMock(return_value=SimpleNamespace(status_code=200)),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.decode_federation_response_json", Mock(return_value={})
    )
    monkeypatch.setattr(
        "app.api.bot_federation.validated_event_envelope",
        AsyncMock(
            return_value=SimpleNamespace(
                type="bot.worker.authorization",
                actor=SimpleNamespace(id="10", domain="apps.example"),
                content=authorization,
            )
        ),
    )
    manifest = SimpleNamespace(
        application=SimpleNamespace(manifest_generation="2", command_generation="2")
    )
    fetch_user = AsyncMock(return_value=manifest)
    materialize = AsyncMock()
    monkeypatch.setattr("app.api.bot_federation.fetch_user_bot_manifest", fetch_user)
    monkeypatch.setattr("app.api.bot_federation.materialize_remote_manifest", materialize)

    await refresh_remote_worker_authorization(
        session,
        SimpleNamespace(domain="local.example"),
        SimpleNamespace(mint=AsyncMock(return_value=500)),
        20,
        "apps.example",
        40,
    )

    fetch_user.assert_awaited_once()
    assert materialize.await_args.kwargs == {"materialize_template": False}


def test_message_gateway_requires_intent_and_both_content_scopes() -> None:
    bot = principal(
        scopes={"messages.metadata", "messages.content"},
        intents={"guild_messages", "message_content"},
    )
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
        bot,
        event,
        {"guild_messages", "message_content"},
        {"messages.metadata", "messages.content"},
    )
    assert visible is not None and visible["d"]["content"] == "secret"
    assert visible["d"]["attachments"] == []
    assert visible["d"]["attachments_unavailable"] is True
    attachment_bot = principal(
        scopes={"messages.metadata", "messages.content", "attachments.read"},
        intents={"guild_messages", "message_content"},
    )
    fully_visible = filtered_event(
        attachment_bot,
        event,
        {"guild_messages", "message_content"},
        {"messages.metadata", "messages.content", "attachments.read"},
    )
    assert fully_visible is not None
    assert fully_visible["d"]["attachments"] == [{"id": "1"}]
    assert (
        filtered_event(
            bot,
            event,
            {"interactions"},
            {"messages.metadata", "messages.content"},
        )
        is None
    )


def test_message_content_redaction_covers_resolved_thread_source() -> None:
    payload = {
        "content": None,
        "attachments": [],
        "message_type": 21,
        "referenced_message": {
            "content": "retained source secret",
            "e2ee": None,
            "attachments": [{"id": "9", "filename": "secret.png"}],
        },
    }
    redacted = redact_bot_message_payload(
        payload,
        can_read_content=False,
        can_read_attachments=False,
    )
    source = redacted["referenced_message"]
    assert isinstance(source, dict)
    assert source["content"] is None
    assert source["content_unavailable"] is True
    assert source["attachments"] == []
    assert source["attachments_unavailable"] is True

    bot = principal(
        scopes={"messages.metadata", "messages.content"},
        intents={"guild_messages", "message_content"},
    )
    event = {"t": "MESSAGE_CREATE", "topic_seq": 8, "d": payload}
    gateway_projection = filtered_event(
        bot,
        event,
        {"guild_messages", "message_content"},
        {"messages.metadata"},
    )
    assert gateway_projection is not None
    nested = gateway_projection["d"]["referenced_message"]
    assert nested["content"] is None
    assert nested["attachments"] == []


@pytest.mark.parametrize(
    ("worker_scope", "worker_intent", "grant_scope", "grant_intent", "expected"),
    [
        (True, True, True, True, True),
        (False, True, True, True, False),
        (True, False, True, True, False),
        (True, True, False, True, False),
        (True, True, True, False, False),
    ],
)
def test_rest_ambient_message_content_requires_scope_and_intent_on_exact_grant(
    worker_scope: bool,
    worker_intent: bool,
    grant_scope: bool,
    grant_intent: bool,
    expected: bool,
) -> None:
    bot = principal(
        scopes={"messages.content"} if worker_scope else set(),
        intents={"message_content"} if worker_intent else set(),
    )
    exact_installation = installation(scopes={"messages.content"} if grant_scope else set())
    exact_installation.granted_intents = ["message_content"] if grant_intent else []

    assert bots_api.bot_can_read_ambient_message_content(bot, exact_installation) is expected


def test_rest_message_content_exemptions_survive_missing_ambient_intent() -> None:
    bot = principal(scopes={"messages.content"}, intents=set())
    exact_installation = installation(scopes={"messages.content"})
    exact_installation.granted_intents = ["message_content"]
    assert not bots_api.bot_can_read_ambient_message_content(bot, exact_installation)

    base = {"content": "visible exemption", "embeds": [], "attachments": []}
    variants = (
        ({"author_id": "10", "author_domain": "apps.example"}, False, False),
        (
            {"mention_user_refs": [{"id": "10", "origin_domain": "apps.example"}]},
            False,
            False,
        ),
        ({"application_id": "20", "application_domain": "apps.example"}, False, False),
        ({}, True, False),
        ({}, False, True),
    )
    for identity, direct_message, interaction_context in variants:
        rendered = bots_api.redact_bot_message_payload(
            base | identity,
            can_read_content=False,
            can_read_attachments=False,
            principal=bot,
            direct_message=direct_message,
            interaction_context=interaction_context,
        )
        assert rendered["content"] == "visible exemption"


def test_e2ee_redaction_remains_fail_closed_for_nested_and_thread_messages() -> None:
    encrypted_message = {
        "content": None,
        "e2ee": {"ciphertext": "secret"},
        "attachments": [],
    }
    nested = redact_bot_message_payload(
        {
            "content": "visible metadata wrapper",
            "e2ee": None,
            "attachments": [],
            "referenced_message": dict(encrypted_message),
        },
        can_read_content=True,
        can_read_attachments=True,
        can_read_e2ee=False,
    )
    referenced = nested["referenced_message"]
    assert isinstance(referenced, dict)
    assert referenced["e2ee"] is None
    assert referenced["content_unavailable"] is True

    thread = redact_bot_thread_payload(
        {
            "encryption_mode": "e2ee",
            "e2ee_required": True,
            "starter_message": dict(encrypted_message),
        },
        can_read_history=True,
        can_read_content=True,
        can_read_attachments=True,
        can_read_e2ee=False,
    )
    starter = thread["starter_message"]
    assert isinstance(starter, dict)
    assert starter["e2ee"] is None
    assert starter["content_unavailable"] is True


def test_message_content_exceptions_and_snapshot_redaction_are_recursive() -> None:
    bot_ref = (10, "apps.example")
    application_ref = (20, "apps.example")
    secret = {
        "content": "outer secret",
        "embeds": [{"description": "embed secret"}],
        "components": [{"type": 1, "components": []}],
        "poll": {"question": {"text": "poll secret"}},
        "attachments": [{"id": "1"}],
        "message_snapshots": [
            {
                "message": {
                    "content": "snapshot secret",
                    "embeds": [{"description": "snapshot embed"}],
                    "components": [{"type": 1, "components": []}],
                    "poll": {"question": {"text": "snapshot poll"}},
                    "attachments": [{"id": "2"}],
                }
            }
        ],
        "forward_snapshot": {
            "content": "private source copy",
            "embeds": [],
            "components": [],
            "attachments": [{"id": "3"}],
        },
        "forwarded_message": {
            "content": "legacy private copy",
            "embeds": [],
            "components": [],
            "attachments": [{"id": "4"}],
        },
    }
    hidden = redact_bot_message_payload(
        dict(secret),
        can_read_content=False,
        can_read_attachments=False,
        bot_user_ref=bot_ref,
        bot_application_ref=application_ref,
    )
    assert hidden["content"] is None
    assert "poll" not in hidden
    assert hidden["attachments"] == []
    wrapped = hidden["message_snapshots"]
    assert isinstance(wrapped, list)
    snapshot = wrapped[0]["message"]
    assert snapshot["content"] is None
    assert "poll" not in snapshot
    assert snapshot["attachments"] == []
    assert hidden["forward_snapshot"]["content"] is None
    assert hidden["forwarded_message"]["attachments"] == []

    for exception in (
        {"author_id": "10", "author_domain": "apps.example"},
        {
            "mention_user_refs": [
                {"id": "10", "origin_domain": "apps.example"},
            ]
        },
        {"application_id": "20", "application_domain": "apps.example"},
    ):
        visible = redact_bot_message_payload(
            dict(secret) | exception,
            can_read_content=False,
            can_read_attachments=False,
            bot_user_ref=bot_ref,
            bot_application_ref=application_ref,
        )
        assert visible["content"] == "outer secret"
        assert visible["embeds"] == [{"description": "embed secret"}]
        assert visible["poll"] == {"question": {"text": "poll secret"}}
        assert visible["attachments"] == []
        assert visible["message_snapshots"][0]["message"]["content"] == "snapshot secret"
        assert visible["message_snapshots"][0]["message"]["attachments"] == []

    direct = redact_bot_message_payload(
        dict(secret),
        can_read_content=False,
        can_read_attachments=True,
        bot_user_ref=bot_ref,
        bot_application_ref=application_ref,
        direct_message=True,
    )
    assert direct["content"] == "outer secret"
    assert direct["attachments"] == [{"id": "1"}]


def test_gateway_message_content_exception_for_explicit_bot_mention() -> None:
    bot = principal(scopes={"messages.metadata"}, intents={"guild_messages"})
    event = {
        "t": "MESSAGE_CREATE",
        "topic_seq": 9,
        "d": {
            "author_id": "99",
            "author_domain": "guild.example",
            "content": "hello <@10@apps.example>",
            "embeds": [{"description": "visible"}],
            "components": [{"type": 1, "components": []}],
            "attachments": [{"id": "1"}],
            "mention_user_refs": [{"id": "10", "origin_domain": "apps.example"}],
        },
    }
    rendered = filtered_event(
        bot,
        event,
        {"guild_messages"},
        {"messages.metadata"},
    )
    assert rendered is not None
    assert rendered["d"]["content"] == "hello <@10@apps.example>"
    assert rendered["d"]["embeds"] == [{"description": "visible"}]
    assert rendered["d"]["components"] == [{"type": 1, "components": []}]
    assert rendered["d"]["attachments"] == []


def test_thread_gateway_starter_requires_history_and_metadata_grants() -> None:
    payload = {
        "id": "8",
        "type": 11,
        "encryption_mode": "plaintext",
        "e2ee_required": False,
        "starter_message": {
            "content": "historical secret",
            "e2ee": None,
            "attachments": [{"id": "9", "filename": "secret.png"}],
        },
    }
    content_scopes = {
        "channels.read",
        "messages.metadata",
        "messages.content",
        "attachments.read",
    }
    content_bot = principal(
        scopes=content_scopes,
        intents={"guilds", "message_content"},
    )
    event = {"t": "THREAD_LIST_SYNC", "topic_seq": 9, "d": {"threads": [payload]}}
    redacted = filtered_event(
        content_bot,
        event,
        {"guilds", "message_content"},
        content_scopes,
    )
    assert redacted is not None
    redacted_starter = redacted["d"]["threads"][0]["starter_message"]
    assert redacted_starter["content"] is None
    assert redacted_starter["attachments"] == []
    assert redacted_starter["content_unavailable"] is True
    assert redacted_starter["attachments_unavailable"] is True

    history_scopes = content_scopes | {"messages.history"}
    history_bot = principal(
        scopes=history_scopes,
        intents={"guilds", "message_content"},
    )
    visible = filtered_event(
        history_bot,
        event,
        {"guilds", "message_content"},
        history_scopes,
    )
    assert visible is not None
    visible_starter = visible["d"]["threads"][0]["starter_message"]
    assert visible_starter["content"] == "historical secret"
    assert visible_starter["attachments"] == [{"id": "9", "filename": "secret.png"}]


@pytest.mark.asyncio
async def test_bot_pin_listing_applies_content_intent_and_attachment_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace(
        id=77,
        granted_scopes=["messages.history", "messages.content"],
        granted_intents=[],
    )
    monkeypatch.setattr(
        bots_api,
        "installation_for_channel",
        AsyncMock(return_value=(SimpleNamespace(guild_id=70), installation)),
    )
    monkeypatch.setattr(
        bots_api,
        "list_pins",
        AsyncMock(
            return_value=[
                {
                    "content": "pinned secret",
                    "e2ee": None,
                    "attachments": [{"id": "9"}],
                    "referenced_message": {
                        "content": "source secret",
                        "e2ee": None,
                        "attachments": [{"id": "10"}],
                    },
                }
            ]
        ),
    )
    bot = principal(scopes={"messages.history", "messages.content"}, intents=set())

    pins = await bots_api.bot_list_pins(
        EntityRef("8@guild.example"),
        bot,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
    )

    assert pins[0]["content"] is None
    assert pins[0]["attachments"] == []
    assert pins[0]["content_unavailable"] is True
    assert pins[0]["attachments_unavailable"] is True
    assert pins[0]["bot_installation_id"] == "77"
    source = pins[0]["referenced_message"]
    assert source["content"] is None
    assert source["attachments"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_intent", "grant_intent"),
    [(False, True), (True, False)],
)
async def test_bot_guild_member_collection_requires_exact_guild_members_intent(
    monkeypatch: pytest.MonkeyPatch,
    worker_intent: bool,
    grant_intent: bool,
) -> None:
    bot = principal(
        scopes={"members.read"},
        intents={"guild_members"} if worker_intent else set(),
    )
    exact_installation = installation(scopes={"members.read"})
    exact_installation.granted_intents = ["guild_members"] if grant_intent else []
    member_list = AsyncMock(return_value=[])
    monkeypatch.setattr(
        bots_api,
        "installation_for_guild",
        AsyncMock(return_value=(SimpleNamespace(), exact_installation)),
    )
    monkeypatch.setattr(bots_api, "list_members", member_list)

    with pytest.raises(HTTPException) as denied:
        await bots_api.bot_guild_members(
            EntityRef("70@guild.example"),
            bot,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert denied.value.detail == {
        "code": "BOT_INTENT_REQUIRED",
        "intent": "guild_members",
    }
    member_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_guild_member_collection_accepts_both_intent_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"members.read"}, intents={"guild_members"})
    exact_installation = installation(scopes={"members.read"})
    exact_installation.granted_intents = ["guild_members"]
    member_list = AsyncMock(return_value=[])
    monkeypatch.setattr(
        bots_api,
        "installation_for_guild",
        AsyncMock(return_value=(SimpleNamespace(), exact_installation)),
    )
    monkeypatch.setattr(bots_api, "list_members", member_list)

    assert (
        await bots_api.bot_guild_members(
            EntityRef("70@guild.example"),
            bot,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )
        == []
    )
    member_list.assert_awaited_once()


def test_gateway_intent_mapping_orders_reactions_before_messages() -> None:
    assert event_intent("MESSAGE_REACTION_ADD") == "guild_message_reactions"
    assert event_intent("MESSAGE_REACTION_ADD", direct=True) == "direct_message_reactions"
    assert event_intent("MESSAGE_POLL_VOTE_ADD") == "guild_message_polls"
    assert event_intent("MESSAGE_CREATE") == "guild_messages"
    assert event_intent("ATTACHMENT_UPDATE") == "guild_messages"
    assert event_intent("INTERACTION_CREATE") == "interactions"
    assert event_intent("TYPING_START") == "guild_message_typing"
    assert event_intent("TYPING_START", direct=True) == "direct_message_typing"
    assert event_intent("CHANNEL_CREATE", direct=True) == "direct_messages"
    assert event_intent("CHANNEL_CREATE") == "guilds"
    assert event_intent("VOICE_STATE_UPDATE", direct=True) == "direct_messages"
    assert event_intent("VOICE_STATE_UPDATE") == "guild_voice_states"
    assert event_intent("DM_OPEN_REJECTED", direct=True) == "direct_messages"
    assert event_intent("GUILD_BAN_ADD") == "guild_moderation"
    assert event_intent("GUILD_AUDIT_LOG_ENTRY_CREATE") == "guild_moderation"
    assert event_intent("GUILD_EMOJI_UPDATE") == "guild_expressions"
    assert event_intent("INTEGRATION_CREATE") == "guild_integrations"
    assert event_intent("WEBHOOKS_UPDATE") == "guild_webhooks"
    assert event_intent("INVITE_CREATE") == "guild_invites"
    assert event_intent("GUILD_SCHEDULED_EVENT_CREATE") == "guild_scheduled_events"
    assert event_intent("GUILD_SOUNDBOARD_SOUND_CREATE") == "guild_expressions"
    assert event_intent("AUTO_MODERATION_RULE_CREATE") == "auto_moderation_configuration"
    assert event_intent("AUTO_MODERATION_ACTION_EXECUTION") == "auto_moderation_execution"
    assert event_intent("FUTURE_SECRET_EVENT") == ""


def test_gateway_scope_mapping_is_event_specific() -> None:
    assert event_scope("MESSAGE_REACTION_ADD") == "reactions.read"
    assert event_scope("MESSAGE_CREATE") == "messages.metadata"
    assert event_scope("ATTACHMENT_UPDATE") == "attachments.read"
    assert event_scope("PRESENCE_UPDATE") == "members.read"
    assert event_scope("VOICE_STATE_UPDATE") == "voice.states.read"
    assert event_scope("VOICE_CHANNEL_MOVE") == "voice.connect"
    assert event_scope("VOICE_TOKEN") == "voice.connect"
    assert event_scope("VOICE_CHANNEL_EFFECT_SEND") == "soundboard.read"
    assert event_scope("DM_OPEN_REJECTED") == "dm.send"
    assert event_scope("GUILD_ROLE_UPDATE") == "roles.read"
    assert event_scope("CHANNEL_UPDATE") == "channels.read"
    assert event_scope("GUILD_BAN_ADD") == "moderation.bans"
    assert event_scope("GUILD_MEMBERS_PRUNED") == "moderation.prune"
    assert event_scope("GUILD_AUDIT_LOG_ENTRY_CREATE") == "audit_logs.read"
    assert event_scope("GUILD_EMOJIS_UPDATE") == "expressions.read"
    assert event_scope("INTEGRATION_UPDATE") == "integrations.read"
    assert event_scope("WEBHOOKS_UPDATE") == "webhooks.read"
    assert event_scope("INVITE_DELETE") == "invites.read"
    assert event_scope("GUILD_SCHEDULED_EVENT_UPDATE") == "events.read"
    assert event_scope("GUILD_SOUNDBOARD_SOUNDS_UPDATE") == "soundboard.read"
    assert event_scope("AUTO_MODERATION_RULE_DELETE") == "automod.rules.read"
    assert event_scope("AUTO_MODERATION_ACTION_EXECUTION") == "automod.executions.read"
    assert event_scope("FUTURE_SECRET_EVENT") == ""


def test_automod_execution_content_requires_message_content_intent_and_scope() -> None:
    event = {
        "t": "AUTO_MODERATION_ACTION_EXECUTION",
        "topic_seq": 4,
        "d": {
            "guild_id": "1",
            "guild_domain": "guild.example",
            "channel_id": "2",
            "channel_domain": "guild.example",
            "rule_id": "3",
            "rule_domain": "guild.example",
            "rule_trigger_type": "keyword",
            "user_id": "4",
            "user_domain": "users.example",
            "action": {"type": "block_message", "metadata": {}},
            "outcome": "blocked",
            "content": "private blocked phrase",
            "matched_keyword": "blocked*",
            "matched_content": "blocked phrase",
            "alert_system_message_id": None,
            "alert_system_message_domain": None,
            "content_digest": "a" * 64,
        },
    }
    basic = principal(
        scopes={"automod.executions.read"},
        intents={"auto_moderation_execution"},
    )
    hidden = filtered_event(
        basic,
        event,
        {"auto_moderation_execution"},
        {"automod.executions.read"},
        topic="guild:guild.example:1",
        granted_permissions=int(Permission.MANAGE_GUILD),
    )
    assert hidden is not None
    assert hidden["d"]["content"] == ""
    assert hidden["d"]["matched_content"] is None
    assert hidden["d"]["content_digest"] is None
    assert hidden["d"]["matched_keyword"] == "blocked*"

    privileged = principal(
        scopes={"automod.executions.read", "messages.content"},
        intents={"auto_moderation_execution", "message_content"},
    )
    visible = filtered_event(
        privileged,
        event,
        {"auto_moderation_execution", "message_content"},
        {"automod.executions.read", "messages.content"},
        topic="guild:guild.example:1",
        granted_permissions=int(Permission.MANAGE_GUILD),
    )
    assert visible is not None
    assert visible["d"]["content"] == "private blocked phrase"
    assert visible["d"]["matched_content"] == "blocked phrase"


def test_expression_events_accept_new_grants_and_published_legacy_grants() -> None:
    event = {"t": "GUILD_EMOJI_UPDATE", "topic_seq": 4, "d": {"id": "9"}}
    granular = principal(scopes={"expressions.read"}, intents={"guild_expressions"})
    assert (
        filtered_event(
            granular,
            event,
            {"guild_expressions"},
            {"expressions.read"},
            topic="guild:guild.example:1",
        )
        is not None
    )

    legacy = principal(scopes={"guilds.read"}, intents={"guilds"})
    assert (
        filtered_event(
            legacy,
            event,
            {"guilds"},
            {"guilds.read"},
            topic="guild:guild.example:1",
        )
        is not None
    )


def test_unknown_additive_bot_events_are_dropped_fail_closed() -> None:
    bot = principal(scopes={"guilds.read"}, intents={"guilds"})
    assert (
        filtered_event(
            bot,
            {"t": "FUTURE_SECRET_EVENT", "topic_seq": 8, "d": {"secret": "value"}},
            {"guilds"},
            {"guilds.read"},
            topic="guild:guild.example:1",
        )
        is None
    )


def test_attachment_updates_require_message_intent_and_attachment_scope() -> None:
    event = {
        "t": "ATTACHMENT_UPDATE",
        "topic_seq": 8,
        "d": {
            "channel_id": "9",
            "channel_domain": "guild.example",
            "message_id": "10",
            "message_domain": "guild.example",
            "attachment": {
                "id": "11",
                "filename": "private.png",
                "scan_status": "rejected",
            },
        },
    }
    allowed = principal(scopes={"attachments.read"}, intents={"guild_messages"})
    assert (
        filtered_event(
            allowed,
            event,
            {"guild_messages"},
            {"attachments.read"},
            topic="guild:guild.example:1",
        )
        is not None
    )
    assert (
        filtered_event(
            allowed,
            event,
            {"guilds"},
            {"attachments.read"},
            topic="guild:guild.example:1",
        )
        is None
    )
    assert (
        filtered_event(
            allowed,
            event,
            {"guild_messages"},
            {"guilds.read"},
            topic="guild:guild.example:1",
        )
        is None
    )


def test_private_guild_voice_events_bind_one_installation_without_dm_scope() -> None:
    bot = principal(
        scopes={"voice.connect", "voice.listen", "soundboard.read"},
        intents={"guild_voice_states"},
    )
    token = {
        "t": "VOICE_TOKEN",
        "d": {
            "guild_id": "70",
            "guild_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "grant": {"token": "opaque"},
        },
    }
    complete = GatewayInstallationGrant(
        installation_id=60,
        user_installation=False,
        intents=frozenset({"guild_voice_states"}),
        scopes=frozenset({"voice.connect", "voice.listen", "soundboard.read"}),
        guild_id=70,
        guild_domain="guild.example",
    )
    assert (
        filtered_event(
            bot,
            token,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=(complete,),
        )
        is not None
    )

    split = (
        GatewayInstallationGrant(
            installation_id=60,
            user_installation=False,
            intents=frozenset({"guild_voice_states"}),
            scopes=frozenset(),
            guild_id=70,
            guild_domain="guild.example",
        ),
        GatewayInstallationGrant(
            installation_id=61,
            user_installation=False,
            intents=frozenset(),
            scopes=frozenset({"voice.connect"}),
            guild_id=71,
            guild_domain="other.example",
        ),
    )
    assert (
        filtered_event(
            bot,
            token,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=split,
        )
        is None
    )

    effect = {
        "t": "VOICE_CHANNEL_EFFECT_SEND",
        "d": {
            "guild_id": "70",
            "guild_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "sound_id": "8",
        },
    }
    assert (
        filtered_event(
            bot,
            effect,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=(complete,),
        )
        is not None
    )
    no_listener = GatewayInstallationGrant(
        installation_id=60,
        user_installation=False,
        intents=frozenset({"guild_voice_states"}),
        scopes=frozenset({"soundboard.read"}),
        guild_id=70,
        guild_domain="guild.example",
    )
    assert (
        filtered_event(
            bot,
            effect,
            set(),
            set(),
            topic="user:apps.example:10",
            installation_grants=(no_listener,),
        )
        is None
    )


def test_direct_dm_channel_and_status_events_keep_dm_grant_contract() -> None:
    bot = principal(
        scopes={"dm.send", "channels.read", "messages.metadata"},
        intents={"direct_messages"},
    )
    grant = GatewayInstallationGrant(
        installation_id=60,
        user_installation=False,
        intents=frozenset({"direct_messages"}),
        scopes=frozenset({"dm.send", "channels.read", "messages.metadata"}),
    )
    for event in (
        {
            "t": "CHANNEL_CREATE",
            "d": {"id": "7", "origin_domain": "chat.example", "guild_id": None},
        },
        {
            "t": "MESSAGE_DELIVERY_UPDATE",
            "d": {
                "channel_id": "7",
                "channel_domain": "chat.example",
                "status": "delivered",
            },
        },
        {
            "t": "MESSAGE_SEND_REJECTED",
            "d": {
                "channel_id": "7",
                "channel_domain": "chat.example",
                "client_nonce": "nonce",
                "code": "MESSAGE_REJECTED",
            },
        },
        {
            "t": "DM_OPEN_REJECTED",
            "d": {
                "pair_key": "a" * 64,
                "code": "DM_OPEN_REJECTED",
                "authority_domain": "chat.example",
            },
        },
    ):
        assert (
            filtered_event(
                bot,
                event,
                set(),
                set(),
                topic="user:apps.example:10",
                installation_grants=(grant,),
            )
            is not None
        )


def test_interactions_are_isolated_to_the_exact_application_and_installation() -> None:
    bot = principal(scopes={"applications.commands"}, intents={"interactions"})
    grant = GatewayInstallationGrant(
        installation_id=60,
        user_installation=False,
        intents=frozenset({"interactions"}),
        scopes=frozenset({"applications.commands"}),
        guild_id=70,
        guild_domain="guild.example",
        installation_domain="guild.example",
        installation_revision=1,
    )
    shared = {
        "t": "INTERACTION_CREATE",
        "topic_seq": 9,
        "audience_user_refs": ["10@apps.example"],
        "d": {
            "id": "100",
            "application_ref": "20@apps.example",
            "installation_id": "60",
            "installation_revision": "1",
            "bot_user_ref": "10@apps.example",
        },
    }
    assert (
        filtered_event(
            bot,
            shared,
            {"interactions"},
            {"applications.commands"},
            topic="guild:guild.example:70",
            installation_id=60,
            installation_grants=(grant,),
        )
        is not None
    )
    assert (
        filtered_event(
            bot,
            {**shared, "audience_user_refs": ["10@apps.example", "7@people.example"]},
            {"interactions"},
            {"applications.commands"},
            topic="guild:guild.example:70",
            installation_id=60,
            installation_grants=(grant,),
        )
        is None
    )
    other_application = {
        **shared,
        "d": {
            **shared["d"],
            "application_ref": "21@apps.example",
            "installation_id": "61",
        },
    }
    assert (
        filtered_event(
            bot,
            other_application,
            {"interactions"},
            {"applications.commands"},
            topic="guild:guild.example:70",
            installation_id=60,
            installation_grants=(grant,),
        )
        is None
    )
    wrong_installation = {
        **shared,
        "d": {**shared["d"], "installation_id": "61"},
    }
    assert (
        filtered_event(
            bot,
            wrong_installation,
            {"interactions"},
            {"applications.commands"},
            topic="guild:guild.example:70",
            installation_id=60,
            installation_grants=(grant,),
        )
        is None
    )


@pytest.mark.asyncio
async def test_interaction_replay_delivers_only_to_the_explicit_bot_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"applications.commands"}, intents={"interactions"})
    monkeypatch.setattr(gateway, "current_acl_fence", AsyncMock(return_value=(1, 1)))
    monkeypatch.setattr(
        "app.api.bot_gateway.current_interaction_create_access",
        AsyncMock(return_value=True),
    )

    def interaction(sequence: int, audience: object = None) -> dict[str, object]:
        event: dict[str, object] = {
            "t": "INTERACTION_CREATE",
            "topic_seq": sequence,
            "d": {
                "application_ref": "20@apps.example",
                "installation_id": "60",
                "installation_revision": "1",
                "bot_user_ref": "10@apps.example",
                "channel_id": "7",
                "channel_domain": "guild.example",
                "options": {"secret": str(sequence)},
            },
        }
        if audience is not None:
            event["audience_user_refs"] = audience
        return event

    events = [
        interaction(1),
        interaction(2, ["11@apps.example"]),
        interaction(3, ["10@apps.example"]),
        interaction(4, ["10@apps.example", "7@people.example"]),
    ]

    class Redis:
        async def xrange(self, *_args: object, **_kwargs: object) -> list[object]:
            return [
                (f"{index}-0", {"event": json.dumps(event)})
                for index, event in enumerate(events, start=1)
            ]

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    socket = Socket()
    current = AsyncMock(return_value=True)
    replayed = await replay_topic(
        socket,  # type: ignore[arg-type]
        Redis(),  # type: ignore[arg-type]
        bot,
        "guild:guild.example:70",
        0,
        {"interactions"},
        {"applications.commands"},
        60,
        frozenset(),
        int(Permission.ADMINISTRATOR),
        object(),
        gateway.VisibilitySummary(
            {(70, "guild.example")},
            {(70, "guild.example"): {(7, "guild.example")}},
            {(70, "guild.example"): (1, 1)},
        ),
        set(),
        SimpleNamespace(current=current, target_domain=None),  # type: ignore[arg-type]
        installation_grants=(
            GatewayInstallationGrant(
                installation_id=60,
                user_installation=False,
                intents=frozenset({"interactions"}),
                scopes=frozenset({"applications.commands"}),
                guild_id=70,
                guild_domain="guild.example",
                installation_domain="guild.example",
                installation_revision=1,
            ),
        ),
    )

    assert replayed
    assert len(socket.sent) == 1
    assert socket.sent[0]["d"]["options"] == {"secret": "3"}  # type: ignore[index]


@pytest.mark.asyncio
async def test_direct_replay_skips_retained_events_after_dm_participant_removal() -> None:
    bot = principal(scopes={"messages.metadata", "dm.send"}, intents={"direct_messages"})
    raw = {
        "t": "MESSAGE_CREATE",
        "topic_seq": 1,
        "d": {
            "id": "9",
            "origin_domain": "chat.example",
            "channel_id": "7",
            "channel_domain": "chat.example",
            "content": "retained secret",
            "attachments": [],
        },
    }

    class Redis:
        async def xrange(self, *_args: object, **_kwargs: object) -> list[object]:
            return [("1-0", {"event": json.dumps(raw)})]

    class Socket:
        def __init__(self) -> None:
            self.sent: list[dict[str, object]] = []

        async def send_json(self, value: dict[str, object]) -> None:
            self.sent.append(value)

    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    socket = Socket()
    current = AsyncMock(return_value=True)
    replayed = await replay_topic(
        socket,  # type: ignore[arg-type]
        Redis(),  # type: ignore[arg-type]
        bot,
        "user:apps.example:10",
        0,
        set(),
        set(),
        None,
        frozenset(),
        None,
        lambda: SessionContext(),
        gateway.VisibilitySummary(set(), {}),
        set(),
        SimpleNamespace(current=current, target_domain=None),  # type: ignore[arg-type]
        installation_grants=(
            GatewayInstallationGrant(
                installation_id=60,
                user_installation=False,
                intents=frozenset({"direct_messages"}),
                scopes=frozenset({"messages.metadata", "dm.send"}),
            ),
        ),
    )

    assert replayed
    assert socket.sent == []
    assert current.await_count == 2
    # Current disclosure first rejects any stale signed capability lineage,
    # then falls back to the legacy participant-only authorization path.
    assert session.scalar.await_count == 3


def pending_interaction(installed: BotInstallation) -> BotInteraction:
    return BotInteraction(
        id=100,
        application_id=installed.application_id,
        application_domain=installed.application_domain,
        installation_id=installed.id,
        guild_id=installed.guild_id,
        guild_domain=installed.guild_domain,
        channel_id=90,
        channel_domain=installed.guild_domain,
        user_id=80,
        user_domain=installed.guild_domain,
        command_name="weather",
        command_type="chat_input",
        payload={},
        status="pending",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_deferred_interaction_cannot_respond_after_exact_installation_revocation() -> None:
    bot = principal(scopes={"interactions.respond"}, intents={"interactions"})
    result = Mock()
    # The joined active-installation query returns no row after revocation.
    result.one_or_none.return_value = None
    session = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())

    with pytest.raises(HTTPException) as denied:
        await defer_interaction(
            100,
            Response(),
            bot,
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "INTERACTION_NOT_FOUND"}
    session.commit.assert_not_awaited()
    query = str(session.execute.await_args.args[0])
    assert "bot_installations.id = bot_interactions.installation_id" in query
    assert "bot_installations.status" in query


@pytest.mark.asyncio
async def test_interaction_response_rejects_attachment_from_second_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = {"interactions.respond", "attachments.write"}
    interaction_token = "t" * 43
    bot = replace(
        principal(scopes=scopes, intents={"interactions"}),
        interaction_token=interaction_token,
    )
    exact = installation(scopes=scopes, installation_id=60, guild_id=70)
    other = installation(scopes=scopes, installation_id=61, guild_id=71)
    interaction = pending_interaction(exact)
    interaction.token_hash = hashlib.sha256(interaction_token.encode()).digest()
    channel = Channel(
        id=interaction.channel_id,
        origin_domain=interaction.channel_domain,
        guild_id=exact.guild_id,
        guild_domain=exact.guild_domain,
        name="general",
        type=0,
        position=0,
        encryption_mode="plaintext",
        unavailable=False,
    )
    cross_install_attachment = Attachment(
        id=900,
        origin_domain=exact.guild_domain,
        uploader_id=bot.user.id,
        uploader_domain=bot.user.origin_domain,
        bot_installation_id=other.id,
        filename="cross-install.png",
        content_type="image/png",
        size=10,
        object_key="cross-install/900",
        purpose="attachment",
        scan_status="clean",
        encryption_mode="plaintext",
        variants={},
    )
    interaction_result = Mock()
    interaction_result.one_or_none.return_value = (interaction, exact)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=interaction_result),
        get=AsyncMock(return_value=channel),
        scalar=AsyncMock(return_value=exact),
        scalars=AsyncMock(return_value=[cross_install_attachment]),
        commit=AsyncMock(),
    )
    create = AsyncMock()
    monkeypatch.setattr("app.api.interactions.create_message", create)

    with pytest.raises(HTTPException) as denied:
        await respond_interaction(
            interaction.id,
            InteractionResponse(message=MessageCreate(content="result", attachment_ids=["900"])),
            SimpleNamespace(),
            bot,
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain=exact.guild_domain),
        )

    assert denied.value.status_code == 404
    assert denied.value.detail == {"code": "ATTACHMENT_NOT_FOUND"}
    create.assert_not_awaited()
    session.commit.assert_not_awaited()


def test_gateway_projects_authoritative_guild_context_into_sparse_events() -> None:
    bot = principal(scopes={"voice.states.read"}, intents={"voice_states"})
    rendered = filtered_event(
        bot,
        {
            "t": "VOICE_STATE_UPDATE",
            "topic_seq": 3,
            "d": {"channel_id": "8", "user_id": "9", "user_domain": "users.example"},
        },
        {"voice_states"},
        {"voice.states.read"},
        topic="guild:guild.example:7",
    )
    assert rendered is not None
    assert rendered["d"]["guild_id"] == "7"
    assert rendered["d"]["guild_domain"] == "guild.example"
    assert rendered["d"]["channel_domain"] == "guild.example"
    for malformed in (
        "guild:bad",
        "guild:guild.example:0",
        "guild:guild.example:00",
        "guild:Guild.Example:7",
        "guild:guild.example:9223372036854775808",
    ):
        assert guild_context_from_topic(malformed) is None


def test_sparse_shared_events_have_stable_bot_event_names() -> None:
    assert (
        normalized_bot_event_type("MESSAGE_UPDATE", {"reaction": "wave", "removed": False})
        == "MESSAGE_REACTION_ADD"
    )
    assert (
        normalized_bot_event_type("MESSAGE_UPDATE", {"reaction": "wave", "removed": True})
        == "MESSAGE_REACTION_REMOVE"
    )
    assert normalized_bot_event_type("MESSAGE_UPDATE", {"pinned": True}) == "MESSAGE_PIN_UPDATE"
    assert normalized_bot_event_type("MESSAGE_UPDATE", {"content": "edited"}) == "MESSAGE_UPDATE"


def test_reaction_projection_uses_reaction_intent_without_message_intent() -> None:
    bot = principal(scopes={"reactions.read"}, intents={"message_reactions"})
    event = {
        "t": "MESSAGE_UPDATE",
        "topic_seq": 8,
        "d": {
            "id": "4",
            "origin_domain": "guild.example",
            "channel_id": "7",
            "channel_domain": "guild.example",
            "reaction": "wave",
            "user_id": "9",
            "user_domain": "guild.example",
        },
    }
    rendered = filtered_event(bot, event, {"message_reactions"}, {"reactions.read"})
    assert rendered is not None
    assert rendered["t"] == "MESSAGE_REACTION_ADD"


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


def test_gateway_encrypted_thread_content_uses_exact_nested_channel_refs() -> None:
    encrypted_channels = {(7, "guild.example"), (9, "guild.example")}
    thread = {
        "id": "7",
        "origin_domain": "guild.example",
        "type": 11,
        "encryption_mode": "e2ee",
        "e2ee_required": True,
        "starter_message": {
            "id": "70",
            "origin_domain": "guild.example",
            "content": None,
            "e2ee": {"ciphertext": "opaque"},
            "attachments": [],
        },
    }
    create = {"t": "THREAD_CREATE", "d": thread}
    sync = {
        "t": "THREAD_LIST_SYNC",
        "d": {
            "threads": [
                thread,
                {
                    **thread,
                    "id": "9",
                    "starter_message": {
                        **thread["starter_message"],
                        "id": "90",
                    },
                },
            ]
        },
    }

    assert encrypted_bot_content_channel_refs(create, encrypted_channels) == frozenset(
        {(7, "guild.example")}
    )
    assert encrypted_bot_content_channel_refs(sync, encrypted_channels) == frozenset(
        {(7, "guild.example"), (9, "guild.example")}
    )
    assert encrypted_bot_content_event(create, encrypted_channels)
    malformed = {
        "t": "THREAD_CREATE",
        "d": {key: value for key, value in thread.items() if key != "origin_domain"},
    }
    assert encrypted_bot_content_channel_refs(malformed, encrypted_channels) is None
    assert encrypted_bot_content_event(malformed, encrypted_channels)


def test_gateway_e2ee_classifier_handles_sparse_resources_and_status_events() -> None:
    encrypted_channels = {(7, "chat.example")}
    sparse_events = [
        {
            "t": event_type,
            "d": {
                "message_id": "70",
                "message_domain": "chat.example",
                "channel_id": "7",
                "channel_domain": "chat.example",
            },
        }
        for event_type in (
            "MESSAGE_REACTION_REMOVE_ALL",
            "MESSAGE_REACTION_REMOVE_EMOJI",
            "MESSAGE_POLL_VOTE_ADD",
            "MESSAGE_POLL_VOTE_REMOVE",
            "ATTACHMENT_UPDATE",
        )
    ]
    sparse_events.append(
        {
            "t": "MESSAGE_DELETE_BULK",
            "d": {
                "ids": [{"id": "70", "origin_domain": "chat.example"}],
                "channel_id": "7",
                "channel_domain": "chat.example",
            },
        }
    )

    for event in sparse_events:
        assert encrypted_bot_content_channel_refs(event, encrypted_channels) == frozenset(
            {(7, "chat.example")}
        )
        assert encrypted_bot_content_event(event, encrypted_channels)

    for event_type in ("MESSAGE_DELIVERY_UPDATE", "MESSAGE_SEND_REJECTED"):
        status = {
            "t": event_type,
            "d": {
                "channel_id": "7",
                "channel_domain": "chat.example",
                "status": "rejected",
            },
        }
        assert encrypted_bot_content_channel_refs(status, encrypted_channels) == frozenset()
        assert not encrypted_bot_content_event(status, encrypted_channels)

    malformed_attachment = {
        "t": "ATTACHMENT_UPDATE",
        "d": {"message_id": "70", "message_domain": "chat.example"},
    }
    assert encrypted_bot_content_channel_refs(malformed_attachment, encrypted_channels) is None
    assert encrypted_bot_content_event(malformed_attachment, encrypted_channels)


@pytest.mark.asyncio
async def test_gateway_direct_topic_tracks_only_participating_encrypted_rooms() -> None:
    bot = principal(scopes=set(), intents=set()).user
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=[
                (7, "chat.example"),
                (9, "remote.example"),
            ]
        )
    )

    assert await encrypted_direct_channels(session, bot) == {
        (7, "chat.example"),
        (9, "remote.example"),
    }
    query = str(session.execute.await_args.args[0])
    assert "JOIN dm_participants" in query
    assert "channels.encryption_mode" in query
    assert "dm_participants.user_id" in query


@pytest.mark.asyncio
async def test_gateway_e2ee_participation_is_bound_to_the_exact_dm_capability() -> None:
    base = principal(scopes={"messages.metadata"}, intents={"direct_messages"})
    bound = BotPrincipal(
        user=base.user,
        application=base.application,
        worker=base.worker,
        token=base.token,
        scopes=base.scopes,
        intents=base.intents,
        dm_capability_grant_id="kbdg_" + "x" * 43,
        dm_capability_revision=9,
        installation_ref="61@users.example",
        installation_type="user",
    )
    channel = SimpleNamespace(id=7, origin_domain="chat.example")
    participation = SimpleNamespace(id=90)
    device_id = "kbe_" + "d" * 43
    session = SimpleNamespace(scalar=AsyncMock(return_value=participation))

    assert (
        await _active_bot_event_participation(
            session,
            bound,
            channel,
            installation_id=None,
            e2ee_device_id=device_id,
        )
        is participation
    )
    statement = session.scalar.await_args.args[0]
    query = str(statement)
    parameters = statement.compile().params
    assert "JOIN bot_dm_capabilities" in query
    assert "bot_dm_capabilities.id = bot_dm_grants.dm_capability_id" in query
    assert "bot_dm_capabilities.grant_id" in query
    assert "bot_dm_capabilities.revision" in query
    assert "bot_dm_capabilities.e2ee_mode" in query
    assert "bot_dm_capabilities.revoked_at IS NULL" in query
    assert "bot_dm_capabilities.expires_at" in query
    assert "bot_e2ee_devices.protocol_id" in query
    assert bound.dm_capability_grant_id in parameters.values()
    assert bound.dm_capability_revision in parameters.values()
    assert device_id in parameters.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        {
            "t": "MESSAGE_POLL_VOTE_ADD",
            "d": {
                "message_id": "70",
                "message_domain": "chat.example",
                "channel_id": "7",
                "channel_domain": "chat.example",
            },
        },
        {
            "t": "MESSAGE_REACTION_REMOVE_ALL",
            "d": {
                "message_id": "70",
                "message_domain": "chat.example",
                "channel_id": "7",
                "channel_domain": "chat.example",
            },
        },
        {
            "t": "ATTACHMENT_UPDATE",
            "d": {
                "message_id": "70",
                "message_domain": "chat.example",
                "channel_id": "7",
                "channel_domain": "chat.example",
                "attachment": {"id": "80", "filename": "private.txt"},
            },
        },
        {
            "t": "MESSAGE_DELETE_BULK",
            "d": {
                "ids": [{"id": "70", "origin_domain": "chat.example"}],
                "channel_id": "7",
                "channel_domain": "chat.example",
            },
        },
    ],
)
@pytest.mark.parametrize("above_floor", [False, True])
async def test_gateway_sparse_e2ee_events_enforce_message_history_floor(
    event: dict[str, object],
    above_floor: bool,
) -> None:
    bot = principal(scopes={"messages.metadata"}, intents={"direct_messages"})
    floor_time = datetime(2026, 1, 1, tzinfo=UTC)
    channel = SimpleNamespace(id=7, origin_domain="chat.example", encryption_mode="e2ee")
    participation = SimpleNamespace(
        dm_grant_id=None,
        history_floor_message_id=60,
        history_floor_message_domain="chat.example",
    )
    floor = SimpleNamespace(
        id=60,
        origin_domain="chat.example",
        created_at=floor_time,
    )
    current = SimpleNamespace(
        id=70,
        origin_domain="chat.example",
        created_at=floor_time + (timedelta(seconds=1) if above_floor else -timedelta(seconds=1)),
    )

    async def get(model: object, key: object) -> object | None:
        if model is Channel:
            return channel
        if key == (60, "chat.example"):
            return floor
        if key == (70, "chat.example"):
            return current
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=participation),
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    assert (
        await current_bot_e2ee_event_access(
            lambda: SessionContext(),
            bot,
            event,  # type: ignore[arg-type]
            encrypted_channels={(7, "chat.example")},
            installation_id=60,
            e2ee_device_id="kbe_" + "d" * 43,
        )
        is above_floor
    )


def test_gateway_encrypted_thread_redaction_respects_verified_participation() -> None:
    scopes = {
        "channels.read",
        "messages.metadata",
        "messages.content",
        "messages.history",
        "attachments.read",
    }
    bot = principal(scopes=scopes, intents={"guilds", "message_content"})
    event = {
        "t": "THREAD_CREATE",
        "topic_seq": 7,
        "d": {
            "id": "7",
            "origin_domain": "guild.example",
            "type": 11,
            "encryption_mode": "e2ee",
            "e2ee_required": True,
            "starter_message": {
                "id": "70",
                "origin_domain": "guild.example",
                "content": None,
                "e2ee": {"ciphertext": "opaque"},
                "attachments": [],
            },
        },
    }

    denied = filtered_event(
        bot,
        event,
        {"guilds", "message_content"},
        scopes,
        can_read_e2ee=False,
    )
    admitted = filtered_event(
        bot,
        event,
        {"guilds", "message_content"},
        scopes,
        can_read_e2ee=True,
    )

    assert denied is not None and admitted is not None
    assert denied["d"]["starter_message"]["e2ee"] is None
    assert denied["d"]["starter_message"]["content_unavailable"] is True
    assert admitted["d"]["starter_message"]["e2ee"] == {"ciphertext": "opaque"}


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_gateway_encrypted_thread_rechecks_worker_participation(admitted: bool) -> None:
    bot = principal(scopes={"channels.read"}, intents={"guilds"})
    channel = SimpleNamespace(id=7, origin_domain="guild.example", encryption_mode="e2ee")
    participation = SimpleNamespace(
        dm_grant_id=None,
        history_floor_message_id=None,
        history_floor_message_domain=None,
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=channel),
        scalar=AsyncMock(return_value=participation if admitted else None),
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    event = {
        "t": "THREAD_UPDATE",
        "d": {
            "id": "7",
            "origin_domain": "guild.example",
            "encryption_mode": "e2ee",
            "starter_message": {
                "id": "70",
                "origin_domain": "guild.example",
                "e2ee": {"ciphertext": "opaque"},
            },
        },
    }
    assert (
        await current_bot_e2ee_event_access(
            lambda: SessionContext(),
            bot,
            event,
            encrypted_channels={(7, "guild.example")},
            installation_id=60,
            e2ee_device_id="kbe_" + "d" * 43,
        )
        is admitted
    )


def test_interaction_options_reject_non_json_and_resource_abuse() -> None:
    base = {"application_ref": "1@apps.example", "command_name": "poll"}
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={"value": float("nan")})
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={str(index): index for index in range(26)})
    with pytest.raises(ValidationError):
        InteractionCreate(**base, options={"value": "x" * (64 * 1024 + 1)})


def test_command_names_and_permissions_are_bounded() -> None:
    default_command = CommandDefinition(name="weather", description="Current weather")
    assert default_command.name == "weather"
    assert default_command.contexts == ["guild", "bot_dm", "private_channel"]
    assert default_command.integration_types == ["guild_install"]
    user_command = CommandDefinition(
        name="share",
        description="Share something",
        contexts=["guild", "bot_dm", "private_channel"],
        integration_types=["guild_install", "user_install"],
    )
    assert user_command.integration_types == ["guild_install", "user_install"]
    with pytest.raises(ValidationError):
        CommandDefinition(
            name="duplicate",
            description="Duplicate context",
            contexts=["guild", "guild"],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(name="Not Valid")
    with pytest.raises(ValidationError):
        ApplicationPatch(default_permissions=1 << 63)
    with pytest.raises(ValidationError):
        ApplicationPatch(default_permissions=1 << 19)
    assert ApplicationPatch(default_permissions=str(1 << 58)).default_permissions == 1 << 58
    for invalid_mask in (True, 1.0, "+1", "01"):
        with pytest.raises(ValidationError):
            ApplicationPatch(default_permissions=invalid_mask)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        TemplateCreate(slug="default", name="Default", permissions=str(1 << 19))
    with pytest.raises(ValidationError):
        ApplicationCreate(name="Unsafe", support_url="http://apps.example/support")
    with pytest.raises(ValidationError):
        ApplicationPatch(privacy_url="http://apps.example/privacy")
    assert (
        str(ApplicationCreate(name="Safe", support_url="https://apps.example/support").support_url)
        == "https://apps.example/support"
    )


def test_application_install_config_bounds_commands_and_user_grants() -> None:
    patch = ApplicationPatch(
        supported_install_types=["guild_install", "user_install"],
        user_install_scopes=[
            "applications.commands",
            "interactions.respond",
            "attachments.read",
        ],
        user_install_contexts=["bot_dm", "private_channel"],
    )
    assert patch.user_install_contexts == ["bot_dm", "private_channel"]

    with pytest.raises(ValidationError):
        ApplicationPatch(user_install_scopes=["applications.commands", "attachments.read"])
    with pytest.raises(ValidationError):
        ApplicationPatch(user_install_contexts=[])

    commands = CommandsPut(
        commands=[
            CommandDefinition(
                name="share",
                description="Share something",
                integration_types=["user_install"],
            )
        ]
    )
    with pytest.raises(HTTPException) as unsupported:
        validate_command_install_types(commands, ["guild_install"])
    assert unsupported.value.detail["code"] == "COMMAND_INSTALL_TYPE_NOT_CONFIGURED"
    validate_command_install_types(commands, ["guild_install", "user_install"])
    with pytest.raises(ValidationError):
        TemplateCreate(slug="default", name="Default", contexts=[])
    with pytest.raises(ValidationError):
        ManifestTemplate(
            id="1",
            slug="default",
            name="Default",
            scopes=[],
            intents=[],
            permissions="0",
            contexts=[],
            e2ee_mode="disabled",
            generation="1",
        )
    with pytest.raises(ValidationError):
        ManifestTemplate(
            id="1",
            slug="default",
            name="Default",
            scopes=[],
            intents=[],
            permissions=str(1 << 19),
            contexts=["guild"],
            e2ee_mode="disabled",
            generation="1",
        )
    with pytest.raises(ValidationError):
        ManifestApplication.model_validate(
            {
                "id": "01",
                "origin_domain": "apps.example",
                "team_id": "3",
                "team_domain": "apps.example",
                "name": "Unsafe",
                "support_url": "http://apps.example/support",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": str(1 << 63),
                "e2ee_modes": ["participant", "participant"],
                "manifest_generation": "0",
                "command_generation": "1",
                "bot_user": {
                    "id": "2",
                    "origin_domain": "apps.example",
                    "username": "unsafe",
                },
            }
        )
    with pytest.raises(ValidationError):
        ManifestApplication.model_validate(
            {
                "id": "1",
                "origin_domain": "apps.example",
                "team_id": "3",
                "team_domain": "apps.example",
                "name": "Reserved permission",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": str(1 << 19),
                "e2ee_modes": [],
                "manifest_generation": "1",
                "command_generation": "1",
                "bot_user": {
                    "id": "2",
                    "origin_domain": "apps.example",
                    "username": "reserved",
                },
            }
        )
    with pytest.raises(ValidationError):
        ManifestWorker(
            id="1",
            name="Unsafe",
            public_key="eA",
            scopes=["unknown.scope"],
            intents=[],
            target_domains=["UPPER.example"],
            generation="1",
            expires_at="2026-08-28T12:00:00",
        )


def test_federated_bot_manifest_rejects_ambiguous_booleans_and_nul_text() -> None:
    base = {
        "id": "8",
        "name": "party_blob",
        "media_hash": "a" * 64,
        "animated": False,
        "available": True,
        "version": "1",
    }
    with pytest.raises(ValidationError, match="boolean"):
        ManifestApplicationEmoji.model_validate(base | {"animated": 1})
    with pytest.raises(ValidationError, match="NUL"):
        ManifestApplicationEmoji.model_validate(base | {"name": "bad\x00name"})


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


@pytest.mark.parametrize("raw", [bytes(range(32)), bytes(range(64))])
def test_bot_key_material_requires_canonical_unpadded_base64url(raw: bytes) -> None:
    canonical = encode_urlsafe(raw)
    assert decode_urlsafe(canonical, length=len(raw)) == raw

    with pytest.raises(ValueError, match="canonical"):
        decode_urlsafe(canonical + "=", length=len(raw))

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alias = next(
        canonical[:-1] + candidate
        for candidate in alphabet
        if candidate != canonical[-1]
        and base64.urlsafe_b64decode(canonical[:-1] + candidate + "=" * (-len(canonical) % 4))
        == raw
    )
    with pytest.raises(ValueError, match="canonical"):
        decode_urlsafe(alias, length=len(raw))


@pytest.mark.asyncio
async def test_wildcard_worker_still_needs_an_installation_for_a_broad_runtime_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = principal(scopes={"guilds.read"}, intents={"guilds"})
    bot.application.status = "active"
    bot.worker.target_domains = []
    authenticate = AsyncMock(return_value=(bot.worker, bot.application, bot.user))
    monkeypatch.setattr(applications_api, "authenticated_worker_assertion", authenticate)
    monkeypatch.setattr(applications_api, "enforce_keyed_rate_limit", AsyncMock())
    session = SimpleNamespace(scalar=AsyncMock(return_value=False))
    request = WorkerTokenRequest(
        application_ref=EntityRef("20@apps.example"),
        worker_id=40,
        audience="https://guilds.example/api/v1/bots/token",
        issued_at=1,
        expires_at=2,
        nonce="n" * 24,
        signature="s" * 86,
    )

    with pytest.raises(HTTPException) as denied:
        await create_bot_token(
            request,
            Response(),
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guilds.example"),
        )

    assert denied.value.status_code == 403
    assert denied.value.detail == {"code": "BOT_NOT_INSTALLED"}
    authenticate.assert_awaited_once()


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
    channel_command = CommandDefinition(
        name="move",
        description="Move a conversation",
        options=[
            CommandOptionDefinition(
                type="channel",
                name="destination",
                description="Destination",
                channel_types=[0, 5, 10, 11, 12, 15, 17],
            )
        ],
    )
    serialized = channel_command.model_dump(mode="json")
    assert serialized["options"][0]["channel_types"] == [0, 5, 10, 11, 12, 15, 17]
    assert CommandDefinition.model_validate(serialized) == channel_command
    with pytest.raises(ValidationError):
        CommandOptionDefinition(type="user", name="person", description="Person", min_length=1)
    with pytest.raises(ValidationError, match="require a channel option"):
        CommandOptionDefinition(
            type="user",
            name="person",
            description="Person",
            channel_types=[0],
        )
    with pytest.raises(ValidationError, match="must be unique"):
        CommandOptionDefinition(
            type="channel",
            name="destination",
            description="Destination",
            channel_types=[0, 0],
        )
    with pytest.raises(ValidationError, match="unsupported channel type"):
        CommandOptionDefinition(
            type="channel",
            name="destination",
            description="Destination",
            channel_types=[99],
        )
    with pytest.raises(ValidationError):
        CommandOptionDefinition(
            type="channel",
            name="destination",
            description="Destination",
            channel_types=[True],
        )
    with pytest.raises(ValidationError):
        CommandDefinition(name="poll", description="Poll", unexpected=True)


def test_command_invocation_options_are_validated_from_registered_definition() -> None:
    command = ApplicationCommand(
        id=1,
        application_id=2,
        application_domain="apps.example",
        name="submit",
        type="chat_input",
        generation=1,
        definition={
            "name": "submit",
            "type": "chat_input",
            "description": "Submit a document",
            "options": [
                {
                    "type": "subcommand_group",
                    "name": "reports",
                    "description": "Reports",
                    "options": [
                        {
                            "type": "subcommand",
                            "name": "create",
                            "description": "Create",
                            "options": [
                                {
                                    "type": "string",
                                    "name": "kind",
                                    "description": "Kind",
                                    "required": True,
                                    "choices": [{"name": "Bug", "value": "bug"}],
                                },
                                {
                                    "type": "attachment",
                                    "name": "document",
                                    "description": "Document",
                                    "required": True,
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    )
    supplied = {"reports": {"create": {"kind": "bug", "document": "41"}}}
    assert validate_command_options(command, supplied, require_complete=True) == supplied
    assert command_attachment_ids(command, supplied) == [41]

    with pytest.raises(HTTPException) as missing:
        validate_command_options(
            command,
            {"reports": {"create": {"kind": "bug"}}},
            require_complete=True,
        )
    assert missing.value.detail["option"] == "reports.create.document"

    with pytest.raises(HTTPException) as forged:
        validate_command_options(
            command,
            {"reports": {"create": {"kind": "feature", "document": "41"}}},
            require_complete=True,
        )
    assert forged.value.detail["message"] == "Choose one of the command's allowed values."


def test_command_invocation_enforces_advertised_channel_types() -> None:
    command = ApplicationCommand(
        id=1,
        application_id=2,
        application_domain="apps.example",
        name="move",
        type="chat_input",
        generation=1,
        definition={
            "name": "move",
            "type": "chat_input",
            "description": "Move a conversation",
            "options": [
                {
                    "type": "subcommand",
                    "name": "thread",
                    "description": "Move a thread",
                    "options": [
                        {
                            "type": "channel",
                            "name": "destination",
                            "description": "Destination",
                            "required": True,
                            "channel_types": [0, 5],
                        }
                    ],
                }
            ],
        },
    )
    supplied = {"thread": {"destination": "41@chat.example"}}
    normalized = validate_command_options(command, supplied, require_complete=True)
    requirements = command_channel_type_requirements(
        command,
        normalized,
        local_domain="local.example",
    )

    assert requirements == [("thread.destination", (41, "chat.example"), frozenset({0, 5}))]
    validate_resolved_command_channel_types(
        requirements,
        {(41, "chat.example"): 5},
    )
    with pytest.raises(HTTPException) as wrong_type:
        validate_resolved_command_channel_types(
            requirements,
            {(41, "chat.example"): 2},
        )
    assert wrong_type.value.detail == {
        "code": "COMMAND_OPTION_INVALID",
        "option": "thread.destination",
        "message": "Choose a channel type allowed by this command.",
    }

    unrestricted = ApplicationCommand(
        id=2,
        application_id=2,
        application_domain="apps.example",
        name="inspect",
        type="chat_input",
        generation=1,
        definition={
            "name": "inspect",
            "type": "chat_input",
            "description": "Inspect a channel",
            "options": [
                {
                    "type": "channel",
                    "name": "channel",
                    "description": "Channel",
                }
            ],
        },
    )
    assert (
        command_channel_type_requirements(
            unrestricted,
            {"channel": "41@chat.example"},
            local_domain="local.example",
        )
        == []
    )


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
