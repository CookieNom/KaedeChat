from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import ValidationError

import app.bots.developer_projection as developer_projection
import app.bots.installations as bot_installations
import app.bots.runtime_control as runtime_control
import app.bots.target_discovery as target_discovery
from app.admin.auth import AdminPrincipal
from app.api import admin_portal
from app.api.admin_portal import (
    ApplicationStatePatch,
    patch_application_state,
    transition_application_installations,
)
from app.bots.auth import issue_bot_token
from app.bots.runtime_control import (
    APPLICATION_RUNTIME_EVENT,
    ApplicationRuntimeSnapshot,
    application_runtime_projection_ready,
    application_runtime_snapshot_fingerprint,
    apply_application_runtime_control,
    apply_application_runtime_snapshot,
    apply_pending_application_runtime_proof,
    build_current_application_runtime_proof,
    promote_application_runtime_highwater,
    queue_application_runtime_snapshots,
    require_current_application_runtime_proof,
    require_current_pending_application_runtime_proof,
    target_runtime_projection_ready,
    validate_application_runtime_proof,
)
from app.core.federation import sign_envelope
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotApplicationRuntimeHighwater,
    BotApplicationTarget,
    BotInstallation,
    BotInstanceRule,
    BotUserInstallation,
    BotWorker,
    DeveloperTeam,
)
from app.db.models import User
from app.federation.schemas import EventEnvelope


def application_identity(*, local: bool) -> tuple[BotApplication, User]:
    bot = User(
        id=10,
        origin_domain="apps.example",
        is_local=local,
        account_type="bot",
        username="weather",
        password_hash=None,
    )
    application = BotApplication(
        id=20,
        origin_domain="apps.example",
        team_id=30,
        team_domain="apps.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        name="Weather",
        directory_enabled=False,
        directory_approved=False,
        directory_tags=[],
        directory_collections=[],
        status="active",
        target_policy="blocklist",
        manifest_generation=7,
        revocation_generation=4,
    )
    return application, bot


def runtime_payload(
    *,
    manifest_generation: str = "7",
    revocation_generation: str = "4",
    access_revocation_generation: str = "0",
    target_allowed: bool = True,
    revoked: bool = True,
    status: str = "active",
) -> dict[str, object]:
    return {
        "application_id": "20",
        "application_domain": "apps.example",
        "bot_user_id": "10",
        "bot_user_domain": "apps.example",
        "target_domain": "guilds.example",
        "manifest_generation": manifest_generation,
        "revocation_generation": revocation_generation,
        "access_revocation_generation": access_revocation_generation,
        "status": status,
        "target_allowed": target_allowed,
        "workers": [
            {
                "id": "40",
                "generation": "3",
                "revoked": revoked,
                "target_allowed": True,
            },
            {
                "id": "41",
                "generation": "2",
                "revoked": False,
                "target_allowed": False,
            },
        ],
    }


def test_runtime_snapshot_rejects_ambiguous_boolean_wire_values() -> None:
    with pytest.raises(ValidationError):
        ApplicationRuntimeSnapshot.model_validate({**runtime_payload(), "target_allowed": 1})
    payload = runtime_payload()
    workers = [dict(item) for item in payload["workers"]]  # type: ignore[index]
    workers[0]["revoked"] = 0
    with pytest.raises(ValidationError):
        ApplicationRuntimeSnapshot.model_validate({**payload, "workers": workers})


def runtime_envelope(
    payload: dict[str, object] | None = None,
    *,
    event_id: str = "kcfe_runtimeproof1234",
    timestamp_ms: int = 1,
) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": event_id,
            "origin": "apps.example",
            "type": APPLICATION_RUNTIME_EVENT,
            "ts": timestamp_ms,
            "actor": {"id": "10", "domain": "apps.example"},
            "context": {},
            "content": payload or runtime_payload(revoked=False),
            "signatures": {"apps.example": {"test-key": "signature"}},
        }
    )


def test_remote_runtime_requires_an_initialized_current_target_projection() -> None:
    application, _ = application_identity(local=False)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=1,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=0,
        runtime_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=None,
    )

    assert not target_runtime_projection_ready(
        target,
        manifest_generation=7,
        revocation_generation=4,
    )
    assert not application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )

    target.runtime_fingerprint = b"r" * 32
    target.runtime_manifest_generation = 6
    target.runtime_revocation_generation = 4
    assert not application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )
    target.runtime_manifest_generation = 7
    target.runtime_revocation_generation = 3
    assert not application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )
    target.runtime_revocation_generation = 4
    assert application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )

    target.runtime_status = "suspended"
    assert not application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )
    target.runtime_status = "active"
    target.runtime_target_allowed = False
    assert not application_runtime_projection_ready(
        application,
        target,
        target_domain="guilds.example",
    )
    assert application_runtime_projection_ready(
        application,
        None,
        target_domain="apps.example",
    )


@pytest.mark.asyncio
async def test_remote_token_mint_rejects_uninitialized_runtime_target() -> None:
    application, _ = application_identity(local=False)
    application.default_scopes = ["messages.send"]
    application.default_intents = ["guild_messages"]
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        scopes=["messages.send"],
        intents=["guild_messages"],
        target_domains=["guilds.example"],
    )
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=1,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=0,
        runtime_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=target),
        add=Mock(),
        flush=AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await issue_bot_token(
            cast(Any, session),
            token_id=50,
            worker=worker,
            application=application,
            dpop_thumbprint="thumbprint",
            target_domain="guilds.example",
        )

    assert denied.value.status_code == 401
    assert denied.value.detail == {"code": "BOT_ASSERTION_INVALID"}
    session.add.assert_not_called()
    session.flush.assert_not_awaited()
    target_query = str(session.scalar.await_args.args[0])
    assert "bot_application_targets.runtime_fingerprint" in target_query
    assert "FOR UPDATE" in target_query

    target.runtime_manifest_generation = 7
    target.runtime_revocation_generation = 4
    target.runtime_fingerprint = b"r" * 32
    token, raw = await issue_bot_token(
        cast(Any, session),
        token_id=50,
        worker=worker,
        application=application,
        dpop_thumbprint="thumbprint",
        target_domain="guilds.example",
    )

    assert token.id == 50
    assert raw.startswith("kb1_at_")
    session.add.assert_called_once_with(token)
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_application_home_queues_distinct_bounded_runtime_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    targets = [
        BotApplicationTarget(
            application_id=20,
            application_domain="apps.example",
            target_domain=domain,
            generation=1,
            guild_installations=1,
            user_installations=0,
        )
        for domain in ("alpha.example", "guilds.example")
    ]
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=["guilds.example"],
    )
    rule = BotInstanceRule(
        application_id=20,
        application_domain="apps.example",
        target_domain="alpha.example",
        effect="deny",
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=application),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(side_effect=[targets, [], [worker], [rule]]),
    )
    compact = AsyncMock()
    build = AsyncMock(side_effect=[{"event_id": "kcfe_alpha"}, {"event_id": "kcfe_guilds"}])
    queue = AsyncMock()
    monkeypatch.setattr(
        runtime_control,
        "discard_superseded_latest_state_event",
        compact,
    )
    monkeypatch.setattr(runtime_control, "build_envelope", build)
    monkeypatch.setattr(runtime_control, "queue_event", queue)

    destinations = await queue_application_runtime_snapshots(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
        application,
    )

    assert destinations == {"alpha.example", "guilds.example"}
    assert [call.args[4]["target_domain"] for call in build.await_args_list] == [
        "alpha.example",
        "guilds.example",
    ]
    assert [call.args[4]["target_allowed"] for call in build.await_args_list] == [
        False,
        True,
    ]
    assert [call.args[4]["access_revocation_generation"] for call in build.await_args_list] == [
        "1",
        "0",
    ]
    assert [call.args[4]["workers"][0]["target_allowed"] for call in build.await_args_list] == [
        False,
        True,
    ]
    assert [call.args[3]["event_id"] for call in queue.await_args_list] == [
        "kcfe_alpha",
        "kcfe_guilds",
    ]
    assert compact.await_count == 2
    assert all(
        call.kwargs["event_type"] == APPLICATION_RUNTIME_EVENT for call in compact.await_args_list
    )


@pytest.mark.asyncio
async def test_capability_only_runtime_target_is_persisted_for_reactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=["dm.example"],
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=application),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(side_effect=[[], [], [worker], []]),
        add=Mock(),
    )
    monkeypatch.setattr(
        runtime_control,
        "discard_superseded_latest_state_event",
        AsyncMock(),
    )
    monkeypatch.setattr(
        runtime_control,
        "build_envelope",
        AsyncMock(return_value={"event_id": "kcfe_dm"}),
    )
    monkeypatch.setattr(runtime_control, "queue_event", AsyncMock())

    destinations = await queue_application_runtime_snapshots(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
        application,
        additional_target_domains={"dm.example"},
    )

    assert destinations == {"dm.example"}
    ledger = session.add.call_args.args[0]
    assert isinstance(ledger, BotApplicationTarget)
    assert ledger.target_domain == "dm.example"
    assert ledger.generation == 0
    assert (ledger.guild_installations, ledger.user_installations) == (0, 0)


@pytest.mark.asyncio
async def test_active_dm_runtime_targets_include_install_and_conversation_authorities() -> None:
    application, _ = application_identity(local=True)
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=["install.example", "conversation.example", "apps.example"])
    )

    domains = await runtime_control.active_dm_runtime_target_domains(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
        application,
    )

    assert domains == {"install.example", "conversation.example"}
    query = str(session.scalars.await_args.args[0])
    assert "bot_dm_capabilities.source_installation_domain" in query
    assert "bot_dm_capabilities.authority_domain" in query
    assert "UNION" in query


@pytest.mark.asyncio
async def test_coalesced_runtime_snapshot_retains_terminal_access_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=1,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=7,
        runtime_revocation_generation=4,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
    )
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=["guilds.example"],
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=application),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(
            side_effect=[
                [target],
                [],
                [worker],
                [],
                [target],
                [],
                [worker],
                [],
            ]
        ),
    )
    build = AsyncMock(side_effect=[{"event_id": "suspended"}, {"event_id": "active"}])
    monkeypatch.setattr(
        runtime_control,
        "discard_superseded_latest_state_event",
        AsyncMock(),
    )
    monkeypatch.setattr(runtime_control, "build_envelope", build)
    monkeypatch.setattr(runtime_control, "queue_event", AsyncMock())
    settings = cast(Any, SimpleNamespace(domain="apps.example"))

    application.status = "suspended"
    application.revocation_generation = 5
    await queue_application_runtime_snapshots(cast(Any, session), settings, application)

    application.status = "active"
    application.revocation_generation = 6
    await queue_application_runtime_snapshots(cast(Any, session), settings, application)

    assert [call.args[4]["access_revocation_generation"] for call in build.await_args_list] == [
        "1",
        "1",
    ]
    assert build.await_args_list[-1].args[4]["status"] == "active"
    assert target.runtime_access_revocation_generation == 1
    assert target.runtime_status == "active"


@pytest.mark.asyncio
async def test_target_deny_then_allow_retains_terminal_access_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=1,
        guild_installations=1,
        user_installations=0,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
    )
    deny = BotInstanceRule(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        effect="deny",
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=application),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(
            side_effect=[
                [target],
                [],
                [],
                [deny],
                [target],
                [],
                [],
                [],
            ]
        ),
    )
    build = AsyncMock(side_effect=[{"event_id": "denied"}, {"event_id": "allowed"}])
    monkeypatch.setattr(
        runtime_control,
        "discard_superseded_latest_state_event",
        AsyncMock(),
    )
    monkeypatch.setattr(runtime_control, "build_envelope", build)
    monkeypatch.setattr(runtime_control, "queue_event", AsyncMock())
    settings = cast(Any, SimpleNamespace(domain="apps.example"))

    await queue_application_runtime_snapshots(cast(Any, session), settings, application)
    await queue_application_runtime_snapshots(cast(Any, session), settings, application)

    assert [call.args[4]["target_allowed"] for call in build.await_args_list] == [False, True]
    assert [call.args[4]["access_revocation_generation"] for call in build.await_args_list] == [
        "1",
        "1",
    ]
    assert target.runtime_access_revocation_generation == 1
    assert target.runtime_target_allowed is True


@pytest.mark.asyncio
async def test_application_reactivation_restores_only_suspended_installations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _ = application_identity(local=True)
    installation = BotInstallation(
        id=60,
        application_id=application.id,
        application_domain=application.origin_domain,
        guild_id=70,
        guild_domain="guild.example",
        bot_user_id=application.bot_user_id,
        bot_user_domain=application.bot_user_domain,
        installer_id=80,
        installer_domain="guild.example",
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=4,
        status="active",
    )
    user_installation = BotUserInstallation(
        id=61,
        source_id=61,
        source_domain="apps.example",
        application_id=application.id,
        application_domain=application.origin_domain,
        user_id=80,
        user_domain="apps.example",
        granted_scopes=[],
        granted_intents=[],
        contexts=["private_channel"],
        grant_revision=2,
        status="active",
    )
    session = SimpleNamespace(
        scalars=AsyncMock(
            side_effect=[
                [installation],
                [installation],
            ]
        )
    )
    dispatch = Mock()
    monkeypatch.setattr(bot_installations, "queue_installation_gateway_events", dispatch)

    suspended = await transition_application_installations(
        cast(Any, session),
        application,
        previous_status="active",
        next_status="suspended",
    )
    assert suspended == [installation]
    assert (installation.status, installation.grant_revision) == ("suspended", 5)
    assert (user_installation.status, user_installation.grant_revision) == ("active", 2)

    restored = await transition_application_installations(
        cast(Any, session),
        application,
        previous_status="suspended",
        next_status="active",
    )
    assert restored == [installation]
    assert (installation.status, installation.grant_revision) == ("active", 6)
    assert (user_installation.status, user_installation.grant_revision) == ("active", 2)
    assert dispatch.call_args_list[0].args[2] == "UPDATE"
    assert dispatch.call_args_list[1].args[2] == "UPDATE"
    guild_query = str(session.scalars.await_args_list[1].args[0])
    assert "bot_installations.status" in guild_query
    assert "bot_installations.revoked_at IS NULL" in guild_query
    assert all(
        "bot_user_installations" not in str(call.args[0])
        for call in session.scalars.await_args_list
    )

    session.scalars.reset_mock()
    assert (
        await transition_application_installations(
            cast(Any, session),
            application,
            previous_status="active",
            next_status="active",
        )
        == []
    )
    session.scalars.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_suspend_reactivate_converges_installations_and_capability_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _ = application_identity(local=True)
    installation = BotInstallation(
        id=60,
        application_id=application.id,
        application_domain=application.origin_domain,
        guild_id=70,
        guild_domain="guild.example",
        bot_user_id=application.bot_user_id,
        bot_user_domain=application.bot_user_domain,
        installer_id=80,
        installer_domain="guild.example",
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="active",
    )
    user_installation = BotUserInstallation(
        id=61,
        source_id=61,
        source_domain="apps.example",
        application_id=application.id,
        application_domain=application.origin_domain,
        user_id=80,
        user_domain="apps.example",
        granted_scopes=[],
        granted_intents=[],
        contexts=["bot_dm", "private_channel"],
        grant_revision=1,
        status="active",
    )
    admin = User(
        id=1,
        origin_domain="apps.example",
        is_local=True,
        account_type="human",
        username="owner",
        password_hash="hash",
    )
    principal = AdminPrincipal(admin, frozenset({"owner"}), frozenset({"*"}))
    session = SimpleNamespace(
        get=AsyncMock(return_value=application),
        scalars=AsyncMock(
            side_effect=[
                [installation],
                [installation],
            ]
        ),
    )
    runtime_targets = AsyncMock(return_value={"cap-only.example"})
    revoke_e2ee = AsyncMock(return_value=[])
    target_snapshots = AsyncMock(return_value=set())
    commit = AsyncMock()
    monkeypatch.setattr(admin_portal, "active_dm_runtime_target_domains", runtime_targets)
    monkeypatch.setattr(admin_portal, "revoke_bot_e2ee_access", revoke_e2ee)
    monkeypatch.setattr(
        admin_portal,
        "queue_application_target_snapshots_for_refs",
        target_snapshots,
    )
    monkeypatch.setattr(admin_portal, "commit_developer_application_mutation", commit)
    monkeypatch.setattr(admin_portal, "audit", AsyncMock())
    monkeypatch.setattr(admin_portal, "publish_e2ee_policy_updates", AsyncMock())
    monkeypatch.setattr(admin_portal, "wake_application_target_deliveries", AsyncMock())
    monkeypatch.setattr(bot_installations, "queue_installation_gateway_events", Mock())
    settings = SimpleNamespace(domain="apps.example")

    await patch_application_state(
        EntityRef("20@apps.example"),
        ApplicationStatePatch(status="suspended"),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, settings),
    )

    assert (application.status, installation.status, user_installation.status) == (
        "suspended",
        "suspended",
        "active",
    )
    assert commit.await_args.kwargs["runtime_target_domains"] == {"cap-only.example"}
    revoke_e2ee.assert_awaited_once()

    await patch_application_state(
        EntityRef("20@apps.example"),
        ApplicationStatePatch(status="active"),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, settings),
    )

    assert (application.status, installation.status, user_installation.status) == (
        "active",
        "active",
        "active",
    )
    assert installation.grant_revision == 3
    assert user_installation.grant_revision == 1
    assert commit.await_args.kwargs["runtime_target_domains"] == set()
    assert target_snapshots.await_count == 2


@pytest.mark.asyncio
async def test_instance_admin_cannot_mutate_a_remote_application_authority() -> None:
    application, _ = application_identity(local=False)
    admin = User(
        id=1,
        origin_domain="guilds.example",
        is_local=True,
        account_type="human",
        username="owner",
        password_hash="hash",
    )
    principal = AdminPrincipal(admin, frozenset({"owner"}), frozenset({"*"}))
    session = SimpleNamespace(
        get=AsyncMock(return_value=application),
        scalars=AsyncMock(),
    )

    with pytest.raises(HTTPException) as denied:
        await patch_application_state(
            EntityRef("20@apps.example"),
            ApplicationStatePatch(status="suspended"),
            principal,
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guilds.example")),
        )

    assert denied.value.status_code == 409
    assert denied.value.detail == {"code": "APPLICATION_HOME_INSTANCE_REQUIRED"}
    session.scalars.assert_not_awaited()


def test_administration_application_projection_marks_only_home_authority_manageable() -> None:
    application, _ = application_identity(local=False)
    now = datetime.now(UTC)
    application.created_at = now
    application.updated_at = now

    local = admin_portal._administration_application_payload(
        application,
        cast(Any, SimpleNamespace(domain="apps.example")),
    )
    remote = admin_portal._administration_application_payload(
        application,
        cast(Any, SimpleNamespace(domain="guilds.example")),
    )

    assert local["state_authority"] == "apps.example"
    assert local["can_manage_state"] is True
    assert remote["state_authority"] == "apps.example"
    assert remote["can_manage_state"] is False


@pytest.mark.asyncio
async def test_runtime_snapshot_revokes_tokens_without_permanently_revoking_target_denial() -> None:
    application, actor = application_identity(local=False)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=2,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=0,
        runtime_revocation_generation=0,
    )
    revoked_worker = BotWorker(
        id=140,
        source_id=40,
        source_domain="apps.example",
        application_id=20,
        application_domain="apps.example",
        name="revoked",
        public_key=b"r" * 32,
        generation=3,
    )
    denied_worker = BotWorker(
        id=141,
        source_id=41,
        source_domain="apps.example",
        application_id=20,
        application_domain="apps.example",
        name="target-denied",
        public_key=b"d" * 32,
        generation=2,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        scalars=AsyncMock(side_effect=[[revoked_worker, denied_worker], [], []]),
        execute=AsyncMock(),
    )

    changed = await apply_application_runtime_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        runtime_payload(),
    )

    assert changed is True
    assert revoked_worker.revoked_at is not None
    assert denied_worker.revoked_at is None
    assert target.runtime_manifest_generation == 7
    assert target.runtime_revocation_generation == 4
    assert target.runtime_status == "active"
    assert target.runtime_target_allowed is True
    assert target.runtime_fingerprint is not None
    token_update = str(session.execute.await_args.args[0].compile())
    assert "bot_tokens.worker_id IN" in token_update
    assert "bot_tokens.revoked_at IS NULL" in token_update


@pytest.mark.asyncio
async def test_runtime_snapshot_preserves_remote_user_authority_while_fencing_guild_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, actor = application_identity(local=False)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=2,
        guild_installations=1,
        user_installations=1,
        runtime_manifest_generation=7,
        runtime_revocation_generation=4,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
    )
    guild_installation = BotInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="guilds.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="active",
    )
    user_installation = BotUserInstallation(
        id=61,
        source_id=61,
        source_domain="guilds.example",
        application_id=20,
        application_domain="apps.example",
        user_id=80,
        user_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        contexts=["private_channel"],
        grant_revision=1,
        status="active",
    )
    worker = BotWorker(
        id=140,
        source_id=40,
        source_domain="apps.example",
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        scalars=AsyncMock(side_effect=[[worker], [guild_installation]]),
        execute=AsyncMock(),
    )
    target_snapshot = AsyncMock(return_value="apps.example")
    gateway_events = Mock()
    wake = Mock()
    monkeypatch.setattr(target_discovery, "queue_application_target_snapshot", target_snapshot)
    monkeypatch.setattr(bot_installations, "queue_installation_gateway_events", gateway_events)
    monkeypatch.setattr(runtime_control, "queue_postcommit_federation_wakes", wake)
    settings = cast(Any, SimpleNamespace(domain="guilds.example"))

    assert await apply_application_runtime_snapshot(
        cast(Any, session),
        settings,
        "apps.example",
        actor,
        runtime_payload(
            revocation_generation="5",
            access_revocation_generation="1",
            status="suspended",
            revoked=False,
        ),
    )
    assert (application.status, target.runtime_status, target.runtime_target_allowed) == (
        "suspended",
        "suspended",
        True,
    )
    assert (guild_installation.status, user_installation.status) == (
        "suspended",
        "active",
    )

    session.scalar = AsyncMock(side_effect=[application, target])
    session.scalars = AsyncMock(side_effect=[[worker], [guild_installation]])
    assert await apply_application_runtime_snapshot(
        cast(Any, session),
        settings,
        "apps.example",
        actor,
        runtime_payload(
            revocation_generation="6",
            access_revocation_generation="1",
            status="active",
            revoked=False,
        ),
    )
    assert (application.status, target.runtime_status, target.runtime_target_allowed) == (
        "active",
        "active",
        True,
    )
    assert (guild_installation.status, user_installation.status) == ("active", "active")
    assert (guild_installation.grant_revision, user_installation.grant_revision) == (3, 1)
    assert target_snapshot.await_count == 2
    assert gateway_events.call_count == 2
    assert wake.call_count == 2


@pytest.mark.asyncio
async def test_coalesced_reactivation_revokes_tokens_and_rotates_grant_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, actor = application_identity(local=False)
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=2,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=7,
        runtime_revocation_generation=4,
        runtime_access_revocation_generation=0,
    )
    workers = [
        BotWorker(
            id=140,
            source_id=40,
            source_domain="apps.example",
            application_id=20,
            application_domain="apps.example",
            name="production-a",
            public_key=b"a" * 32,
            generation=3,
        ),
        BotWorker(
            id=141,
            source_id=41,
            source_domain="apps.example",
            application_id=20,
            application_domain="apps.example",
            name="production-b",
            public_key=b"b" * 32,
            generation=2,
        ),
    ]
    guild_installation = BotInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="guilds.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=1,
        status="active",
    )
    user_installation = BotUserInstallation(
        id=61,
        source_id=61,
        source_domain="guilds.example",
        application_id=20,
        application_domain="apps.example",
        user_id=80,
        user_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        contexts=["private_channel"],
        grant_revision=1,
        status="active",
    )
    payload = runtime_payload(
        revocation_generation="6",
        access_revocation_generation="1",
        revoked=False,
    )
    payload_workers = cast(list[dict[str, object]], payload["workers"])
    payload_workers[1]["target_allowed"] = True
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        scalars=AsyncMock(side_effect=[workers, [guild_installation]]),
        execute=AsyncMock(),
    )
    target_snapshot = AsyncMock(return_value=None)
    gateway_events = Mock()
    monkeypatch.setattr(target_discovery, "queue_application_target_snapshot", target_snapshot)
    monkeypatch.setattr(bot_installations, "queue_installation_gateway_events", gateway_events)
    invalidated_worker_ids: set[int] = set()
    access_revoked_targets: set[str] = set()

    changed = await apply_application_runtime_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        payload,
        invalidated_worker_ids=invalidated_worker_ids,
        access_revoked_targets=access_revoked_targets,
    )

    assert changed is True
    assert invalidated_worker_ids == {140, 141}
    assert access_revoked_targets == {"guilds.example"}
    assert all(worker.revoked_at is None for worker in workers)
    assert (guild_installation.status, user_installation.status) == ("active", "active")
    assert (guild_installation.grant_revision, user_installation.grant_revision) == (2, 1)
    target_snapshot.assert_awaited_once()
    gateway_events.assert_called_once_with(session, guild_installation, "UPDATE")
    assert application.revocation_generation == 6
    assert target.runtime_revocation_generation == 6
    assert target.runtime_access_revocation_generation == 1
    token_update = str(session.execute.await_args.args[0].compile())
    assert "bot_tokens.worker_id IN" in token_update
    assert "bot_tokens.revoked_at IS NULL" in token_update


@pytest.mark.asyncio
async def test_application_ahead_snapshot_still_applies_new_target_access_epoch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, actor = application_identity(local=False)
    application.manifest_generation = 10
    application.revocation_generation = 6
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=2,
        guild_installations=1,
        user_installations=1,
        runtime_manifest_generation=8,
        runtime_revocation_generation=5,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
    )
    workers = [
        BotWorker(
            id=140,
            source_id=40,
            source_domain="apps.example",
            application_id=20,
            application_domain="apps.example",
            name="production-a",
            public_key=b"a" * 32,
            generation=3,
        ),
        BotWorker(
            id=141,
            source_id=41,
            source_domain="apps.example",
            application_id=20,
            application_domain="apps.example",
            name="production-b",
            public_key=b"b" * 32,
            generation=2,
        ),
    ]
    guild_installation = BotInstallation(
        id=60,
        application_id=20,
        application_domain="apps.example",
        guild_id=70,
        guild_domain="guilds.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        installer_id=80,
        installer_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="disabled",
        grant_revision=3,
        status="active",
    )
    user_installation = BotUserInstallation(
        id=61,
        source_id=61,
        source_domain="guilds.example",
        application_id=20,
        application_domain="apps.example",
        user_id=80,
        user_domain="guilds.example",
        granted_scopes=[],
        granted_intents=[],
        contexts=["private_channel"],
        grant_revision=4,
        status="active",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        scalars=AsyncMock(side_effect=[workers, [guild_installation]]),
        execute=AsyncMock(),
    )
    target_snapshot = AsyncMock(return_value="apps.example")
    gateway_events = Mock()
    wake = Mock()
    monkeypatch.setattr(target_discovery, "queue_application_target_snapshot", target_snapshot)
    monkeypatch.setattr(runtime_control, "queue_installation_gateway_events", gateway_events)
    monkeypatch.setattr(runtime_control, "queue_postcommit_federation_wakes", wake)
    invalidated_worker_ids: set[int] = set()
    access_revoked_targets: set[str] = set()

    changed = await apply_application_runtime_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        runtime_payload(
            manifest_generation="9",
            revocation_generation="6",
            access_revocation_generation="1",
            status="active",
            target_allowed=True,
        ),
        invalidated_worker_ids=invalidated_worker_ids,
        access_revoked_targets=access_revoked_targets,
    )

    assert changed is True
    assert application.manifest_generation == 10
    assert application.status == "active"
    assert all(worker.revoked_at is None for worker in workers)
    assert invalidated_worker_ids == {140, 141}
    assert access_revoked_targets == {"guilds.example"}
    assert (guild_installation.grant_revision, user_installation.grant_revision) == (4, 4)
    assert (guild_installation.status, user_installation.status) == ("active", "active")
    gateway_events.assert_called_once_with(session, guild_installation, "UPDATE")
    target_snapshot.assert_awaited_once()
    wake.assert_called_once_with(session, ("apps.example",))
    assert (
        target.runtime_manifest_generation,
        target.runtime_revocation_generation,
        target.runtime_access_revocation_generation,
    ) == (9, 6, 1)
    assert (target.runtime_status, target.runtime_target_allowed) == ("active", True)
    token_update = str(session.execute.await_args.args[0].compile())
    assert "bot_tokens.worker_id IN" in token_update
    assert "bot_tokens.revoked_at IS NULL" in token_update


@pytest.mark.asyncio
async def test_runtime_snapshot_rejects_equal_generation_equivocation() -> None:
    application, actor = application_identity(local=False)
    accepted = ApplicationRuntimeSnapshot.model_validate(runtime_payload())
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=1,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=7,
        runtime_revocation_generation=4,
        runtime_fingerprint=runtime_control._runtime_fingerprint(accepted),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        scalars=AsyncMock(),
        execute=AsyncMock(),
    )

    assert not await apply_application_runtime_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        runtime_payload(),
    )
    with pytest.raises(ValueError, match="generation conflicts"):
        await apply_application_runtime_snapshot(
            cast(Any, SimpleNamespace(scalar=AsyncMock(side_effect=[application, target]))),
            cast(Any, SimpleNamespace(domain="guilds.example")),
            "apps.example",
            actor,
            runtime_payload(target_allowed=False),
        )


@pytest.mark.asyncio
async def test_local_runtime_target_ledger_advances_without_a_self_outbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=[],
    )
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=application),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(side_effect=[[], [], [worker], []]),
        add=Mock(),
    )
    compact = AsyncMock()
    build = AsyncMock()
    queue = AsyncMock()
    monkeypatch.setattr(runtime_control, "discard_superseded_latest_state_event", compact)
    monkeypatch.setattr(runtime_control, "build_envelope", build)
    monkeypatch.setattr(runtime_control, "queue_event", queue)
    settings = cast(Any, SimpleNamespace(domain="apps.example"))

    assert (
        await queue_application_runtime_snapshots(
            cast(Any, session),
            settings,
            application,
            additional_target_domains={"apps.example"},
        )
        == set()
    )
    target = session.add.call_args.args[0]
    assert isinstance(target, BotApplicationTarget)
    assert target.target_domain == "apps.example"
    assert (
        target.runtime_manifest_generation,
        target.runtime_revocation_generation,
        target.runtime_access_revocation_generation,
    ) == (7, 4, 0)
    assert (target.runtime_status, target.runtime_target_allowed) == ("active", True)
    first_fingerprint = target.runtime_fingerprint
    assert first_fingerprint is not None
    compact.assert_not_awaited()
    build.assert_not_awaited()
    queue.assert_not_awaited()

    application.status = "suspended"
    application.revocation_generation = 5
    session.scalars = AsyncMock(side_effect=[[target], [], [worker], []])
    assert (
        await queue_application_runtime_snapshots(
            cast(Any, session),
            settings,
            application,
        )
        == set()
    )
    assert (
        target.runtime_manifest_generation,
        target.runtime_revocation_generation,
        target.runtime_access_revocation_generation,
    ) == (7, 5, 1)
    assert (target.runtime_status, target.runtime_target_allowed) == ("suspended", True)
    assert target.runtime_fingerprint != first_fingerprint
    build.assert_not_awaited()
    queue.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_runtime_proof_uses_stable_content_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        generation=3,
        scopes=[],
        intents=[],
        target_domains=[],
    )
    snapshot = runtime_control._runtime_snapshot(
        application,
        bot,
        [worker],
        "apps.example",
        target_allowed=True,
        access_revocation_generation=2,
    )
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="apps.example",
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=7,
        runtime_revocation_generation=4,
        runtime_access_revocation_generation=2,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=application_runtime_snapshot_fingerprint(snapshot),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, target]),
        get=AsyncMock(return_value=bot),
        scalars=AsyncMock(side_effect=[[worker], []]),
    )

    async def build(
        _session: object,
        _settings: object,
        event_type: str,
        _actor: object,
        content: dict[str, object],
    ) -> dict[str, object]:
        assert event_type == APPLICATION_RUNTIME_EVENT
        return runtime_envelope(content).model_dump(mode="json")

    monkeypatch.setattr(runtime_control, "build_envelope", build)
    envelope, accepted = await build_current_application_runtime_proof(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
        application_ref=(20, "apps.example"),
        target_domain="apps.example",
    )

    content_fingerprint = require_current_application_runtime_proof(
        application,
        target,
        envelope,
        accepted,
    )
    refreshed_envelope = runtime_envelope(
        accepted.model_dump(mode="json"),
        event_id="kcfe_runtimeproof5678",
        timestamp_ms=2,
    )
    assert content_fingerprint == application_runtime_snapshot_fingerprint(accepted)
    assert (
        require_current_application_runtime_proof(
            application,
            target,
            refreshed_envelope,
            accepted,
        )
        == content_fingerprint
    )
    assert runtime_control.application_runtime_envelope_fingerprint(
        envelope
    ) != runtime_control.application_runtime_envelope_fingerprint(refreshed_envelope)

    application.manifest_generation = 8
    with pytest.raises(ValueError, match="behind application state"):
        require_current_application_runtime_proof(
            application,
            target,
            envelope,
            accepted,
        )


@pytest.mark.asyncio
async def test_local_runtime_proof_validates_with_the_self_signing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    now_ms = int(time.time() * 1000)
    raw: dict[str, object] = {
        "event_id": "kcfe_runtimeprooflocal1",
        "origin": "apps.example",
        "type": APPLICATION_RUNTIME_EVENT,
        "ts": now_ms,
        "actor": {"id": "10", "domain": "apps.example"},
        "context": {},
        "content": runtime_payload(revoked=False),
    }
    raw["signatures"] = {"apps.example": {"self-key": sign_envelope(raw, private_key)}}
    monkeypatch.setattr(
        runtime_control,
        "self_private_key",
        AsyncMock(return_value=("self-key", private_key)),
    )
    settings = cast(
        Any,
        SimpleNamespace(
            domain="apps.example",
            federation_clock_skew_seconds=30,
            federation_event_retention_days=30,
        ),
    )

    envelope, snapshot = await validate_application_runtime_proof(
        cast(Any, SimpleNamespace()),
        settings,
        expected_origin="apps.example",
        raw_envelope=raw,
        application_ref=(20, "apps.example"),
        bot_ref=(10, "apps.example"),
        target_domain="guilds.example",
    )

    assert envelope.origin == "apps.example"
    assert snapshot.target_domain == "guilds.example"


@pytest.mark.asyncio
async def test_pending_runtime_highwater_is_monotonic_and_exact() -> None:
    snapshot = ApplicationRuntimeSnapshot.model_validate(runtime_payload(revoked=False))
    current_time = datetime(2026, 1, 1, tzinfo=UTC)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        execute=AsyncMock(),
        add=Mock(),
    )

    assert await apply_pending_application_runtime_proof(
        cast(Any, session), snapshot, now=current_time
    )
    highwater = session.add.call_args.args[0]
    assert isinstance(highwater, BotApplicationRuntimeHighwater)
    envelope = runtime_envelope(snapshot.model_dump(mode="json"))
    expected_fingerprint = application_runtime_snapshot_fingerprint(snapshot)
    assert (
        require_current_pending_application_runtime_proof(
            highwater,
            envelope,
            snapshot,
            now=current_time,
        )
        == expected_fingerprint
    )
    assert highwater.expires_at == current_time + timedelta(hours=24)
    with pytest.raises(ValueError, match="does not authorize"):
        require_current_pending_application_runtime_proof(
            highwater,
            envelope,
            snapshot,
            now=current_time + timedelta(hours=25),
        )

    session.scalar = AsyncMock(return_value=highwater)
    assert not await apply_pending_application_runtime_proof(
        cast(Any, session), snapshot, now=current_time
    )
    stale = ApplicationRuntimeSnapshot.model_validate(
        runtime_payload(manifest_generation="6", revoked=False)
    )
    assert not await apply_pending_application_runtime_proof(
        cast(Any, session), stale, now=current_time
    )
    with pytest.raises(ValueError, match="not the exact accepted state"):
        require_current_pending_application_runtime_proof(
            highwater,
            runtime_envelope(stale.model_dump(mode="json")),
            stale,
            now=current_time,
        )
    equivocated = ApplicationRuntimeSnapshot.model_validate(
        runtime_payload(target_allowed=False, revoked=False)
    )
    with pytest.raises(ValueError, match="conflicts with stored state"):
        await apply_pending_application_runtime_proof(
            cast(Any, session),
            equivocated,
            now=current_time,
        )


@pytest.mark.asyncio
async def test_pending_runtime_highwater_enforces_the_origin_quota() -> None:
    snapshot = ApplicationRuntimeSnapshot.model_validate(runtime_payload(revoked=False))
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                None,
                None,
                runtime_control.MAX_PENDING_APPLICATION_RUNTIME_HIGHWATERS_PER_ORIGIN,
            ]
        ),
        execute=AsyncMock(),
        add=Mock(),
    )

    with pytest.raises(ValueError, match="pending-state quota exceeded"):
        await apply_pending_application_runtime_proof(
            cast(Any, session),
            snapshot,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

    session.add.assert_not_called()
    cleanup = str(session.execute.await_args.args[0])
    assert "DELETE FROM bot_application_runtime_highwaters" in cleanup
    assert "bot_application_runtime_highwaters.expires_at" in cleanup


@pytest.mark.asyncio
async def test_pending_runtime_promotes_after_exact_application_materialization() -> None:
    application, _ = application_identity(local=False)
    snapshot = ApplicationRuntimeSnapshot.model_validate(runtime_payload(revoked=False))
    highwater = BotApplicationRuntimeHighwater(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        bot_user_id=10,
        bot_user_domain="apps.example",
        manifest_generation=7,
        revocation_generation=4,
        access_revocation_generation=3,
        status="active",
        target_allowed=True,
        runtime_fingerprint=application_runtime_snapshot_fingerprint(
            snapshot.model_copy(update={"access_revocation_generation": "3"})
        ),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, None, highwater]),
        add=Mock(),
        delete=AsyncMock(),
    )

    target = await promote_application_runtime_highwater(
        cast(Any, session),
        application,
        target_domain="guilds.example",
    )

    assert target is session.add.call_args.args[0]
    assert (
        target.runtime_manifest_generation,
        target.runtime_revocation_generation,
        target.runtime_access_revocation_generation,
    ) == (7, 4, 3)
    assert target.runtime_fingerprint == highwater.runtime_fingerprint
    session.delete.assert_awaited_once_with(highwater)
    query_order = [str(call.args[0]) for call in session.scalar.await_args_list]
    assert "FROM bot_applications" in query_order[0]
    assert "FROM bot_application_targets" in query_order[1]
    assert "FROM bot_application_runtime_highwaters" in query_order[2]
    assert all("FOR UPDATE" in query for query in query_order)


@pytest.mark.asyncio
async def test_runtime_snapshot_creates_zero_count_target_without_a_capability_row() -> None:
    application, actor = application_identity(local=False)
    payload = runtime_payload(revoked=False)
    payload["workers"] = []
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, None]),
        scalars=AsyncMock(side_effect=[[], [], []]),
        execute=AsyncMock(),
        add=Mock(),
    )

    assert await apply_application_runtime_snapshot(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        payload,
        allow_target_bootstrap=True,
    )
    target = session.add.call_args.args[0]
    assert isinstance(target, BotApplicationTarget)
    assert (target.guild_installations, target.user_installations) == (0, 0)
    assert session.scalar.await_count == 2


@pytest.mark.asyncio
async def test_async_runtime_snapshot_cannot_bootstrap_an_unknown_target() -> None:
    application, actor = application_identity(local=False)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, None]),
        add=Mock(),
    )

    with pytest.raises(ValueError, match="no authoritative target ledger"):
        await apply_application_runtime_snapshot(
            cast(Any, session),
            cast(Any, SimpleNamespace(domain="guilds.example")),
            "apps.example",
            actor,
            runtime_payload(revoked=False),
        )

    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_shared_runtime_control_preserves_e2ee_and_voice_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = application_identity(local=False)[1]
    channel = cast(Any, SimpleNamespace(id=70, origin_domain="guilds.example"))

    async def access_edge(
        _session: object,
        _settings: object,
        _origin: str,
        _actor: object,
        _raw: object,
        *,
        invalidated_worker_ids: set[int],
        access_revoked_targets: set[str],
        allow_target_bootstrap: bool,
    ) -> bool:
        assert not allow_target_bootstrap
        invalidated_worker_ids.add(140)
        access_revoked_targets.add("guilds.example")
        return True

    revoke = AsyncMock(return_value=[channel])
    evict = AsyncMock()
    monkeypatch.setattr(runtime_control, "apply_application_runtime_snapshot", access_edge)
    monkeypatch.setattr("app.bots.e2ee.revoke_bot_e2ee_access", revoke)
    monkeypatch.setattr("app.voice.e2ee.evict_bot_voice_runtime_sessions", evict)

    changed, channels = await apply_application_runtime_control(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        runtime_payload(revoked=False),
    )

    assert changed is True
    assert channels == [channel]
    revoke.assert_awaited_once()
    evict.assert_not_awaited()

    async def worker_edge(
        _session: object,
        _settings: object,
        _origin: str,
        _actor: object,
        _raw: object,
        *,
        invalidated_worker_ids: set[int],
        access_revoked_targets: set[str],
        allow_target_bootstrap: bool,
    ) -> bool:
        assert not allow_target_bootstrap
        invalidated_worker_ids.add(141)
        assert not access_revoked_targets
        return True

    monkeypatch.setattr(runtime_control, "apply_application_runtime_snapshot", worker_edge)
    changed, channels = await apply_application_runtime_control(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        actor,
        runtime_payload(revoked=False),
    )
    assert changed is True
    assert channels == []
    evict.assert_awaited_once()
    assert evict.await_args.kwargs["worker_ids"] == {141}


@pytest.mark.asyncio
async def test_application_mutation_commits_developer_and_runtime_outboxes_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _bot = application_identity(local=True)
    team = DeveloperTeam(
        id=30,
        origin_domain="apps.example",
        name="Weather Team",
    )
    lifecycle: list[str] = []

    async def refresh(value: object, *, attribute_names: tuple[str, ...]) -> None:
        assert value is application
        assert attribute_names == ("updated_at",)
        lifecycle.append("refresh")

    session = SimpleNamespace(
        get=AsyncMock(return_value=team),
        flush=AsyncMock(side_effect=lambda: lifecycle.append("flush")),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=lambda: lifecycle.append("commit")),
    )
    queue_developers = AsyncMock(
        side_effect=lambda *_args, **_kwargs: (
            lifecycle.append("queue-developers") or {"developer.example"}
        )
    )
    queue_runtime = AsyncMock(
        side_effect=lambda *_args, **_kwargs: (
            lifecycle.append("queue-runtime") or {"guilds.example"}
        )
    )
    wake_developers = AsyncMock(side_effect=lambda *_args: lifecycle.append("wake-developers"))
    wake_runtime = AsyncMock(side_effect=lambda *_args: lifecycle.append("wake-runtime"))
    monkeypatch.setattr(
        developer_projection,
        "queue_developer_team_snapshots",
        queue_developers,
    )
    monkeypatch.setattr(
        developer_projection,
        "queue_application_runtime_snapshots",
        queue_runtime,
    )
    monkeypatch.setattr(
        developer_projection,
        "wake_developer_team_snapshots",
        wake_developers,
    )
    monkeypatch.setattr(
        developer_projection,
        "wake_application_runtime_deliveries",
        wake_runtime,
    )

    await developer_projection.commit_developer_application_mutation(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="apps.example")),
        application,
    )

    queue_developers.assert_awaited_once()
    queue_runtime.assert_awaited_once()
    session.commit.assert_awaited_once()
    wake_developers.assert_awaited_once_with({"developer.example"})
    wake_runtime.assert_awaited_once_with({"guilds.example"})
    assert lifecycle == [
        "queue-developers",
        "queue-runtime",
        "flush",
        "refresh",
        "commit",
        "wake-developers",
        "wake-runtime",
    ]
