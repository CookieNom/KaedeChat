from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException

import app.api.applications as applications_api
import app.api.bot_control as bot_control_api
import app.bots.runtime_control as runtime_control
import app.bots.target_discovery as target_discovery
from app.api.applications import (
    WorkerTokenRequest,
    authenticated_worker_assertion,
    discover_bot_worker_targets,
)
from app.api.bot_control import create_application_home_token
from app.bots.auth import encode_urlsafe, issue_bot_token, worker_assertion_message
from app.bots.runtime_control import (
    application_runtime_projection_ready,
    apply_application_runtime_snapshot,
)
from app.bots.target_contract import (
    APPLICATION_TARGET_EVENT,
    ApplicationTargetSnapshot,
    authority_attested_application_target,
)
from app.bots.target_discovery import (
    apply_application_target_snapshot,
    queue_application_target_snapshot,
    recover_incomplete_application_runtime_targets,
)
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, BotApplicationTarget, BotWorker
from app.db.models import User


def application_identity(*, local: bool = False) -> tuple[BotApplication, User]:
    domain = "apps.example"
    bot = User(
        id=10,
        origin_domain=domain,
        is_local=local,
        account_type="bot",
        username="weather",
        password_hash=None,
    )
    application = BotApplication(
        id=20,
        origin_domain=domain,
        team_id=30,
        team_domain=domain,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
    )
    return application, bot


def target_payload(*, generation: str = "4", guilds: str = "1", users: str = "2") -> dict[str, str]:
    return {
        "application_id": "20",
        "application_domain": "apps.example",
        "bot_user_id": "10",
        "bot_user_domain": "apps.example",
        "target_domain": "guilds.example",
        "generation": generation,
        "guild_installations": guilds,
        "user_installations": users,
    }


def test_target_snapshot_contract_binds_target_and_application_actor() -> None:
    payload = target_payload()
    snapshot = ApplicationTargetSnapshot.model_validate(payload)

    assert snapshot.active is True
    assert authority_attested_application_target(
        APPLICATION_TARGET_EVENT,
        payload,
        expected_authority="guilds.example",
        actor=("10", "apps.example"),
    )
    assert not authority_attested_application_target(
        APPLICATION_TARGET_EVENT,
        payload,
        expected_authority="attacker.example",
        actor=("10", "apps.example"),
    )
    assert not authority_attested_application_target(
        APPLICATION_TARGET_EVENT,
        payload,
        expected_authority="guilds.example",
        actor=("11", "apps.example"),
    )


@pytest.mark.asyncio
async def test_target_authority_queues_roster_free_monotonic_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity()
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(side_effect=[None, 3, 5]),
        add=Mock(),
    )
    build = AsyncMock(return_value={"event_id": "kcfe_test"})
    queue = AsyncMock()
    compact = AsyncMock()
    monkeypatch.setattr(target_discovery, "build_envelope", build)
    monkeypatch.setattr(target_discovery, "queue_event", queue)
    monkeypatch.setattr(target_discovery, "discard_superseded_latest_state_event", compact)

    destination = await queue_application_target_snapshot(
        session,
        SimpleNamespace(domain="guilds.example"),
        application,
        bot,
        force=True,
    )

    assert destination == "apps.example"
    stored = session.add.call_args.args[0]
    assert isinstance(stored, BotApplicationTarget)
    assert (stored.generation, stored.guild_installations, stored.user_installations) == (1, 3, 5)
    event_content = build.await_args.args[4]
    assert event_content == target_payload(generation="1", guilds="3", users="5")
    assert "guild_id" not in event_content and "user_id" not in event_content
    assert build.await_args.kwargs["authority_attested_actor"] is True
    compact.assert_awaited_once_with(
        session,
        destination="apps.example",
        event_type=APPLICATION_TARGET_EVENT,
        application_ref=(20, "apps.example"),
        target_domain="guilds.example",
    )
    queue.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="guilds.example"),
        "apps.example",
        {"event_id": "kcfe_test"},
    )


@pytest.mark.asyncio
async def test_application_home_rejects_same_generation_equivocation() -> None:
    application, bot = application_identity(local=True)
    stored = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=4,
        guild_installations=1,
        user_installations=2,
    )
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[application, stored]))

    with pytest.raises(ValueError, match="generation conflicts"):
        await apply_application_target_snapshot(
            session,
            SimpleNamespace(domain="apps.example"),
            "guilds.example",
            bot,
            target_payload(users="3"),
        )


@pytest.mark.asyncio
async def test_undiscovered_runtime_target_accepts_first_signed_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    stored = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=0,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=3,
        runtime_revocation_generation=2,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=b"r" * 32,
    )
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[application, stored]))
    queue_runtime = AsyncMock()
    monkeypatch.setattr(target_discovery, "_queue_current_application_runtime", queue_runtime)

    changed = await apply_application_target_snapshot(
        session,
        SimpleNamespace(domain="apps.example"),
        "guilds.example",
        bot,
        target_payload(generation="1", guilds="1", users="0"),
    )

    assert changed is True
    assert (stored.generation, stored.guild_installations, stored.user_installations) == (1, 1, 0)
    assert stored.runtime_fingerprint == b"r" * 32
    queue_runtime.assert_awaited_once()


@pytest.mark.asyncio
async def test_newly_discovered_target_receives_current_runtime_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[application, None]),
        add=Mock(),
    )
    queue_runtime = AsyncMock(return_value={"guilds.example"})
    wake = Mock()
    monkeypatch.setattr(
        runtime_control,
        "queue_application_runtime_snapshots",
        queue_runtime,
    )
    monkeypatch.setattr(target_discovery, "queue_postcommit_federation_wakes", wake)
    settings = SimpleNamespace(domain="apps.example")

    assert await apply_application_target_snapshot(
        session,
        settings,
        "guilds.example",
        bot,
        target_payload(),
    )

    queue_runtime.assert_awaited_once_with(
        session,
        settings,
        application,
        destination_domains={"guilds.example"},
    )
    wake.assert_called_once_with(session, ["guilds.example"])


@pytest.mark.asyncio
async def test_runtime_recovery_force_queues_only_bounded_incomplete_remote_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity()
    application.manifest_generation = 7
    application.revocation_generation = 4
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=4,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=0,
        runtime_revocation_generation=0,
        runtime_fingerprint=None,
    )
    result = Mock()
    result.all.return_value = [(application, bot, target)]
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    queue = AsyncMock(return_value="apps.example")
    monkeypatch.setattr(target_discovery, "queue_application_target_snapshot", queue)

    recovered, destinations = await recover_incomplete_application_runtime_targets(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
    )

    assert recovered == 1
    assert destinations == {"apps.example"}
    queue.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="guilds.example"),
        application,
        bot,
        force=True,
    )
    statement = str(session.execute.await_args.args[0])
    assert "bot_application_targets.runtime_fingerprint IS NULL" in statement
    assert "bot_application_targets.guild_installations >" in statement
    assert "bot_application_targets.user_installations >" in statement
    assert "LIMIT" in statement
    assert "FOR UPDATE" in statement


@pytest.mark.asyncio
async def test_preupgrade_target_reannouncement_converges_runtime_and_allows_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority_application, authority_bot = application_identity(local=True)
    authority_application.manifest_generation = 7
    authority_application.revocation_generation = 4
    authority_target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=4,
        guild_installations=1,
        user_installations=0,
    )
    authority_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[authority_application, authority_target]),
    )
    queue_runtime = AsyncMock(return_value={"guilds.example"})
    wake = Mock()
    monkeypatch.setattr(runtime_control, "queue_application_runtime_snapshots", queue_runtime)
    monkeypatch.setattr(target_discovery, "queue_postcommit_federation_wakes", wake)
    authority_settings = SimpleNamespace(domain="apps.example")

    assert await apply_application_target_snapshot(
        cast(Any, authority_session),
        cast(Any, authority_settings),
        "guilds.example",
        authority_bot,
        target_payload(generation="5", guilds="1", users="0"),
    )
    queue_runtime.assert_awaited_once_with(
        authority_session,
        authority_settings,
        authority_application,
        destination_domains={"guilds.example"},
    )
    wake.assert_called_once_with(authority_session, ["guilds.example"])

    remote_application, remote_bot = application_identity()
    remote_application.manifest_generation = 7
    remote_application.revocation_generation = 4
    remote_application.default_scopes = ["messages.send"]
    remote_application.default_intents = ["guild_messages"]
    legacy_target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=5,
        guild_installations=1,
        user_installations=0,
        runtime_manifest_generation=0,
        runtime_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=None,
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
        scopes=["messages.send"],
        intents=["guild_messages"],
        target_domains=["guilds.example"],
    )
    runtime_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[remote_application, legacy_target]),
        scalars=AsyncMock(side_effect=[[worker], [], []]),
        execute=AsyncMock(),
    )
    runtime = {
        "application_id": "20",
        "application_domain": "apps.example",
        "bot_user_id": "10",
        "bot_user_domain": "apps.example",
        "target_domain": "guilds.example",
        "manifest_generation": "7",
        "revocation_generation": "4",
        "access_revocation_generation": "0",
        "status": "active",
        "target_allowed": True,
        "workers": [
            {
                "id": "40",
                "generation": "3",
                "revoked": False,
                "target_allowed": True,
            }
        ],
    }

    assert await apply_application_runtime_snapshot(
        cast(Any, runtime_session),
        cast(Any, SimpleNamespace(domain="guilds.example")),
        "apps.example",
        remote_bot,
        runtime,
    )
    assert application_runtime_projection_ready(
        remote_application,
        legacy_target,
        target_domain="guilds.example",
    )

    token_session = SimpleNamespace(
        scalar=AsyncMock(return_value=legacy_target),
        add=Mock(),
        flush=AsyncMock(),
    )
    token, raw_token = await issue_bot_token(
        cast(Any, token_session),
        token_id=90,
        worker=worker,
        application=remote_application,
        dpop_thumbprint="thumbprint",
        target_domain="guilds.example",
    )
    assert token.id == 90
    assert raw_token.startswith("kb1_at_")


@pytest.mark.asyncio
async def test_worker_discovery_filters_targets_without_exposing_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        scopes=[],
        intents=[],
        target_domains=["guilds.example"],
    )
    target = BotApplicationTarget(
        application_id=20,
        application_domain="apps.example",
        target_domain="guilds.example",
        generation=7,
        guild_installations=4,
        user_installations=2,
    )
    authenticate = AsyncMock(return_value=(worker, application, bot))
    monkeypatch.setattr(applications_api, "authenticated_worker_assertion", authenticate)
    monkeypatch.setattr(applications_api, "enforce_keyed_rate_limit", AsyncMock())
    session = SimpleNamespace(scalars=AsyncMock(side_effect=[[], [target]]))
    payload = WorkerTokenRequest(
        application_ref=EntityRef("20@apps.example"),
        worker_id=40,
        audience="https://apps.example/api/v1/bot-workers/targets",
        issued_at=1,
        expires_at=2,
        nonce="n" * 24,
        signature="s" * 86,
    )

    result = await discover_bot_worker_targets(
        payload,
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="apps.example"),
    )

    assert result == {
        "application_ref": "20@apps.example",
        "targets": [
            {
                "domain": "guilds.example",
                "origin": "https://guilds.example",
                "generation": "7",
                "install_types": ["guild_install", "user_install"],
            }
        ],
        "poll_after_seconds": 30,
    }
    assert "guild_installations" not in result["targets"][0]
    assert "user_installations" not in result["targets"][0]
    query = str(session.scalars.await_args.args[0])
    assert "bot_application_targets.target_domain IN" in query


@pytest.mark.asyncio
async def test_worker_can_mint_home_token_without_a_local_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, bot = application_identity(local=True)
    worker = BotWorker(
        id=40,
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=b"k" * 32,
        scopes=["applications.assets.manage"],
        intents=[],
    )
    authenticate = AsyncMock(return_value=(worker, application, bot))
    token = SimpleNamespace(expires_at=datetime.now(UTC) + timedelta(minutes=8))
    issue = AsyncMock(return_value=(token, "kb1_at_home"))
    monkeypatch.setattr(bot_control_api, "authenticated_worker_assertion", authenticate)
    monkeypatch.setattr(bot_control_api, "issue_bot_token", issue)
    monkeypatch.setattr(bot_control_api, "enforce_keyed_rate_limit", AsyncMock())
    session = SimpleNamespace(commit=AsyncMock())
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=90))
    payload = WorkerTokenRequest(
        application_ref=EntityRef("20@apps.example"),
        worker_id=40,
        audience="https://apps.example/api/v1/bot-workers/home-token",
        issued_at=1,
        expires_at=2,
        nonce="n" * 24,
        signature="s" * 86,
    )

    result = await create_application_home_token(
        payload,
        SimpleNamespace(),
        session,
        SimpleNamespace(),
        snowflake,
        SimpleNamespace(domain="apps.example"),
    )

    assert result["access_token"] == "kb1_at_home"
    authenticate.assert_awaited_once()
    assert authenticate.await_args.kwargs == {
        "expected_audience": "https://apps.example/api/v1/bot-workers/home-token",
        "replay_scope": "application-home-token",
        "local_application_only": True,
    }
    issue.assert_awaited_once()
    assert issue.await_args.kwargs["target_domain"] == "apps.example"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_domains", "allowed"),
    [([], True), (["dm.example"], True), (["other.example"], False)],
)
async def test_runtime_worker_assertion_honors_wildcard_or_explicit_target_delegation(
    monkeypatch: pytest.MonkeyPatch,
    target_domains: list[str],
    allowed: bool,
) -> None:
    private_key = Ed25519PrivateKey.generate()
    bot = User(
        id=10,
        origin_domain="apps.example",
        is_local=False,
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
        status="active",
    )
    worker = BotWorker(
        id=400,
        source_id=40,
        source_domain="apps.example",
        application_id=20,
        application_domain="apps.example",
        name="production",
        public_key=private_key.public_key().public_bytes_raw(),
        scopes=["dm.send"],
        intents=["direct_messages"],
        target_domains=target_domains,
        generation=1,
    )
    result = Mock()
    result.one_or_none.return_value = (worker, application, bot)
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    monkeypatch.setattr(
        applications_api,
        "refresh_remote_worker_authorization",
        AsyncMock(),
    )
    now = int(time.time())
    audience = "https://dm.example/api/v1/bots/token"
    nonce = "n" * 24
    signature = private_key.sign(
        worker_assertion_message(
            "20@apps.example",
            40,
            audience,
            now,
            now + 30,
            nonce,
        )
    )
    request = WorkerTokenRequest(
        application_ref=EntityRef("20@apps.example"),
        worker_id=40,
        audience=audience,
        issued_at=now,
        expires_at=now + 30,
        nonce=nonce,
        signature=encode_urlsafe(signature),
    )

    if not allowed:
        with pytest.raises(HTTPException) as denied:
            await authenticated_worker_assertion(
                request,
                session,
                redis,
                SimpleNamespace(),
                SimpleNamespace(domain="dm.example"),
                expected_audience=audience,
                replay_scope="test",
                require_target_delegation=True,
            )
        assert denied.value.detail == {"code": "BOT_TARGET_NOT_DELEGATED"}
        redis.set.assert_not_awaited()
        return

    resolved = await authenticated_worker_assertion(
        request,
        session,
        redis,
        SimpleNamespace(),
        SimpleNamespace(domain="dm.example"),
        expected_audience=audience,
        replay_scope="test",
        require_target_delegation=True,
    )
    assert resolved == (worker, application, bot)
    redis.set.assert_awaited_once()
