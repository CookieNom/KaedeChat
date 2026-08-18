from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

import app.api.moderation as moderation_api
from app.admin.auth import ROLE_CAPABILITIES, AdminPrincipal
from app.api.applications import (
    SUPPORTED_SCOPES,
    ApplicationPatch,
    CommandDefinition,
    CommandOptionDefinition,
    CredentialCreate,
    WorkerCreate,
    _uninstall_bot_from_local_guild,
    bot_username,
    ensure_bot_install_allowed,
    ensure_personal_developer_team,
    install_bot,
    normalize_values,
    team_payload,
)
from app.api.bot_federation import (
    BotManifest,
    _target_policy_allows,
    activate_remote_application_if_permitted,
    enabled_bot_identity,
    federation_worker_authorization,
    local_manifest,
    materialize_remote_manifest,
    restore_remote_worker_if_new,
)
from app.api.bot_gateway import (
    GatewayAuthorizationGuard,
    GatewayAuthorizationState,
    current_gateway_authorization,
    encrypted_message_event,
    event_intent,
    event_scope,
    filtered_event,
    gateway_authorization_fingerprint,
    guild_context_from_topic,
    normalized_bot_event_type,
)
from app.api.bots import exact_installation_by_id
from app.api.interactions import (
    InteractionCreate,
    InteractionResponse,
    _local_application_commands,
    defer_interaction,
    respond_interaction,
)
from app.bots.auth import (
    BOT_APPLICATION_REQUEST_LIMIT,
    BOT_WORKER_REQUEST_LIMIT,
    BotPrincipal,
    dpop_message,
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
from app.chat.schemas import BanCreate, InstanceBanCreate, MessageCreate
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
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
    Role,
    User,
)


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


def test_supported_scopes_cover_runtime_resource_contracts() -> None:
    assert {
        "guilds.manage",
        "channels.manage",
        "roles.manage",
        "attachments.read",
        "attachments.write",
        "moderation.messages",
        "voice.moderate",
        "invites.manage",
        "webhooks.manage",
        "emojis.manage",
        "dm.send",
    } <= SUPPORTED_SCOPES


def test_target_policy_explicit_deny_always_wins() -> None:
    assert _target_policy_allows("open", {}, "target.example")
    assert not _target_policy_allows("open", {"target.example": "deny"}, "target.example")
    assert _target_policy_allows("allowlist", {"target.example": "allow"}, "target.example")
    assert not _target_policy_allows("allowlist", {}, "target.example")
    assert not _target_policy_allows("local_only", {"target.example": "allow"}, "target.example")


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
    guard = GatewayAuthorizationGuard(lambda: SessionContext(), bot, ("original",))

    assert not await guard.current(force=True)
    current.assert_awaited_once()


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
        await require_bot(request, session, SimpleNamespace())

    assert denied.value.status_code == 401
    assert denied.value.detail == {"code": "BOT_TOKEN_INVALID"}
    query = str(session.execute.await_args.args[0])
    assert "users.disabled_at IS NULL" in query
    assert "guild_members" in query


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

    guard = GatewayAuthorizationGuard(lambda: SessionContext(), bot, ("authorized",))

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
    session = SimpleNamespace(scalars=AsyncMock(return_value=[installed]))

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
    session = SimpleNamespace(scalars=AsyncMock(return_value=[first, second]))

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
    for name in (
        "add_audit_entry",
        "pause_guild_e2ee_for_membership_change",
        "queue_guild_instance_access_revocation",
        "queue_guild_mutation",
        "wake_queued_guild_federation",
        "publish_dispatch",
    ):
        monkeypatch.setattr(moderation_api, name, AsyncMock())
    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Mock(),
                Mock(),
                [(installed.bot_user_id, installed.bot_user_domain)],
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


def test_active_installation_authority_requires_exact_bot_membership() -> None:
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
        )
    )
    assert "bot_installations.status" in authority_sql
    assert "guild_members" in authority_sql


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

    assert await current_gateway_authorization(session, bot) is None
    installation_query = str(session.scalars.await_args.args[0])
    assert "guild_members" in installation_query
    assert "bot_installations.status" in installation_query


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
    for name in (
        "add_audit_entry",
        "cleanup_installation_roles",
        "pause_guild_e2ee_for_membership_change",
        "publish_deleted_installation_roles",
        "queue_guild_access_revocation",
        "queue_guild_mutation",
        "wake_queued_guild_federation",
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
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[installed]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )

    await moderation_api.kick_member(
        EntityRef("70@guild.example"),
        EntityRef("10@apps.example"),
        auth,
        session,
        SimpleNamespace(),
        SimpleNamespace(),
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
        scalar=AsyncMock(side_effect=[target, member]),
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
        SimpleNamespace(),
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


@pytest.mark.parametrize("prior_status", ["revoked", "suspended"])
@pytest.mark.asyncio
async def test_nonactive_reinstall_removes_stale_role_and_reduces_permissions(
    monkeypatch: pytest.MonkeyPatch,
    prior_status: str,
) -> None:
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
        channel_restrictions=[],
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
        scalar=AsyncMock(side_effect=[guild, actor, None, None, installed, existing_member]),
        execute=AsyncMock(side_effect=[invite_result, [], Mock(), [], Mock()]),
        scalars=AsyncMock(side_effect=[[stale_role], []]),
        add=Mock(),
        delete=AsyncMock(),
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
    signer_query = str(session.scalar.await_args_list[1].args[0])
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
        scalar=AsyncMock(side_effect=[guild, owner, None, None, installed, member]),
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
    session.delete.assert_not_awaited()
    assert publish_dispatch.await_args_list[-1].args[2] == "GUILD_MEMBER_ADD"
    assert publish_dispatch.await_args_list[-1].args[3]["role_ids"] == ["91"]


@pytest.mark.asyncio
async def test_federated_installer_uses_local_owner_signer_and_preserves_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, owner, _, _ = moderation_fixture()
    guild.permission_generation = 1
    remote_installer = User(
        id=80,
        origin_domain="remote.example",
        is_local=False,
        account_type="human",
        username="remote-admin",
        password_hash=None,
    )
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

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[guild, owner, None, None, None, None]),
        execute=AsyncMock(return_value=invite_result),
        scalars=AsyncMock(return_value=[]),
        add=Mock(),
        commit=AsyncMock(),
    )
    queue_mutation = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr("app.api.applications.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr("app.api.applications.wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr("app.api.applications.publish_dispatch", AsyncMock())
    monkeypatch.setattr(
        "app.api.applications.publish_deleted_installation_roles",
        AsyncMock(),
    )

    response = await install_bot(
        EntityRef(f"{guild.id}@{guild.origin_domain}"),
        EntityRef(f"{application.id}@{application.origin_domain}"),
        template.slug,
        SimpleNamespace(user=remote_installer),
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(side_effect=[91, 92])),
        SimpleNamespace(domain=guild.origin_domain),
    )

    assert response["id"] == "92"
    added = [call.args[0] for call in session.add.call_args_list]
    installed = next(item for item in added if isinstance(item, BotInstallation))
    assert (installed.installer_id, installed.installer_domain) == (
        remote_installer.id,
        remote_installer.origin_domain,
    )
    assert queue_mutation.await_count == 3
    assert all(call.args[3] is owner for call in queue_mutation.await_args_list)
    signer_query = str(session.scalar.await_args_list[1].args[0])
    assert "users.is_local IS true" in signer_query
    assert "FOR UPDATE" in signer_query
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_federated_uninstall_preserves_human_shared_role_and_uses_owner_signer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild, owner, member, _ = moderation_fixture()
    remote_installer = User(
        id=80,
        origin_domain="remote.example",
        is_local=False,
        account_type="human",
        username="remote-admin",
        password_hash=None,
    )
    installed = installation(scopes={"guilds.read"})
    installed.application_domain = guild.origin_domain
    installed.bot_user_domain = member.user_domain
    installed.installer_id = remote_installer.id
    installed.installer_domain = remote_installer.origin_domain
    installed.role_id = 90
    installed.role_domain = guild.origin_domain
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
        scalar=AsyncMock(side_effect=[owner, installed]),
        scalars=AsyncMock(side_effect=[[role], [member]]),
        execute=AsyncMock(side_effect=[[], Mock(), [(role.id, role.origin_domain)]]),
        get=AsyncMock(return_value=member),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    queue_mutation = AsyncMock()
    cleanup_mutation = AsyncMock()
    publish_roles = AsyncMock()
    monkeypatch.setattr(
        "app.api.applications.get_permissions",
        AsyncMock(return_value=Permission.MANAGE_GUILD),
    )
    monkeypatch.setattr("app.api.applications.queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(
        "app.bots.installations.queue_guild_mutation",
        cleanup_mutation,
    )
    monkeypatch.setattr(
        "app.api.applications.pause_guild_e2ee_for_membership_change",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.applications.wake_queued_guild_federation", AsyncMock())
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
        SimpleNamespace(user=remote_installer),
        session,
        redis,
        settings,
    )

    assert installed.status == "revoked"
    assert installed.revoked_at is not None
    assert installed.role_id is None
    assert installed.role_domain is None
    assert (installed.installer_id, installed.installer_domain) == (
        remote_installer.id,
        remote_installer.origin_domain,
    )
    session.delete.assert_awaited_once_with(member)
    bot_grant_delete = str(session.execute.await_args_list[1].args[0])
    assert "DELETE FROM member_roles" in bot_grant_delete
    assert "member_roles.user_id" in bot_grant_delete
    assert member.member_version == 4
    cleanup_mutation.assert_awaited_once()
    assert cleanup_mutation.await_args.args[3] is owner
    assert cleanup_mutation.await_args.args[4] == "guild.member.role.remove"
    assert cleanup_mutation.await_args.args[5]["member_version"] == "4"
    queue_mutation.assert_awaited_once()
    assert queue_mutation.await_args.args[3] is owner
    publish_roles.assert_awaited_once_with(redis, guild, [])
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
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[owner_bot, installed]),
        delete=AsyncMock(),
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
    manifest = BotManifest.model_validate(
        {
            "application": {
                "id": "20",
                "origin_domain": "apps.example",
                "name": "Weather refreshed",
                "status": "active",
                "target_policy": "open",
                "default_scopes": [],
                "default_intents": [],
                "default_permissions": "0",
                "e2ee_modes": ["interaction_only"],
                "manifest_generation": "2",
                "command_generation": "2",
                "bot_user": {
                    "id": "10",
                    "origin_domain": "apps.example",
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
        }
    )

    async def get(model, key):
        return {
            User: remote_bot,
            DeveloperTeam: team,
            BotApplication: application,
            BotInstallTemplate: template,
        }.get(model)

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        add=Mock(),
        flush=AsyncMock(),
        scalars=AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "app.api.bot_federation.upsert_remote_user",
        AsyncMock(return_value=remote_bot),
    )

    refreshed, _, _ = await materialize_remote_manifest(
        session,
        manifest,
        SimpleNamespace(domain="local.example"),
    )

    assert refreshed.name == "Weather refreshed"
    assert refreshed.status == "suspended"
    # install_bot selects only status="active", so a reinstall stays denied.
    assert refreshed.status != "active"


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


def test_gateway_intent_mapping_orders_reactions_before_messages() -> None:
    assert event_intent("MESSAGE_REACTION_ADD") == "message_reactions"
    assert event_intent("MESSAGE_CREATE") == "guild_messages"
    assert event_intent("INTERACTION_CREATE") == "interactions"
    assert event_intent("TYPING_START") == "guild_typing"


def test_gateway_scope_mapping_is_event_specific() -> None:
    assert event_scope("MESSAGE_REACTION_ADD") == "reactions.read"
    assert event_scope("MESSAGE_CREATE") == "messages.metadata"
    assert event_scope("PRESENCE_UPDATE") == "members.read"
    assert event_scope("VOICE_STATE_UPDATE") == "voice.states.read"
    assert event_scope("GUILD_ROLE_UPDATE") == "roles.read"
    assert event_scope("CHANNEL_UPDATE") == "channels.read"


def test_interactions_are_isolated_to_the_exact_application_and_installation() -> None:
    bot = principal(scopes={"applications.commands"}, intents={"interactions"})
    shared = {
        "t": "INTERACTION_CREATE",
        "topic_seq": 9,
        "d": {
            "id": "100",
            "application_ref": "20@apps.example",
            "installation_id": "60",
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
        )
        is not None
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
        )
        is None
    )


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
        await defer_interaction(100, bot, session)

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
    bot = principal(scopes=scopes, intents={"interactions"})
    exact = installation(scopes=scopes, installation_id=60, guild_id=70)
    other = installation(scopes=scopes, installation_id=61, guild_id=71)
    interaction = pending_interaction(exact)
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
    assert guild_context_from_topic("guild:bad") is None


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
