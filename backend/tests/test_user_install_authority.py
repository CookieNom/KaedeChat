from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import app.api.bot_e2ee as bot_e2ee_api
import app.bots.e2ee as bot_e2ee_service
import app.bots.target_discovery as target_discovery
from app.bots.installations import usable_user_installation, user_installation_is_usable
from app.bots.target_discovery import (
    application_target_counts,
    expire_foreign_user_installation_leases,
)
from app.bots.user_install_authority import (
    USER_INSTALLATION_AUTHORITY_LEASE,
    FederatedUserInstallationGrant,
    federated_user_installation_authority_expiry,
    reconcile_federated_user_installation,
)
from app.db.bot_models import BotApplication, BotUserInstallation
from app.federation.schemas import RemoteUserProfile
from app.federation.security import FederationPrincipal


def grant(
    expiry: datetime,
    *,
    revision: int = 7,
) -> FederatedUserInstallationGrant:
    return FederatedUserInstallationGrant(
        id="41",
        application_ref="12@apps.example",
        scopes=["applications.commands", "interactions.respond"],
        intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision=str(revision),
        authority_expires_at=expiry,
    )


def installation(
    *,
    user_domain: str = "member.example",
    expiry: datetime | None = None,
    status: str = "active",
    revision: int = 7,
) -> BotUserInstallation:
    return BotUserInstallation(
        id=51,
        source_id=41,
        source_domain="member.example",
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain=user_domain,
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision=revision,
        authority_expires_at=expiry,
        status=status,
    )


def dm_e2ee_body(
    *,
    channel_ref: str = "90@chat.example",
    application_ref: str = "12@apps.example",
) -> dict[str, object]:
    return {
        "channel_ref": channel_ref,
        "application_ref": application_ref,
        "status": "active",
    }


@pytest.mark.asyncio
async def test_dm_bot_e2ee_authority_rejects_malformed_installation_before_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumed = AsyncMock()
    monkeypatch.setattr(bot_e2ee_api, "consume_management_request_once", consumed)
    api = FastAPI()
    api.include_router(bot_e2ee_api.router)
    api.dependency_overrides[bot_e2ee_api.authenticate_federation] = lambda: FederationPrincipal(
        origin="member.example", key_id="main"
    )
    api.dependency_overrides[bot_e2ee_api.get_session] = lambda: SimpleNamespace()
    api.dependency_overrides[bot_e2ee_api.get_redis] = lambda: SimpleNamespace()
    api.dependency_overrides[bot_e2ee_api.get_snowflake] = lambda: SimpleNamespace()
    api.dependency_overrides[bot_e2ee_api.get_settings] = lambda: SimpleNamespace(
        domain="chat.example"
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/_kaede/v1/e2ee/dm-bots/manage",
            json={
                "request_id": "kadme_" + "a" * 32,
                "issued_at": 1_000,
                "deadline": 1_015,
                "operation": "grant",
                "channel_ref": "90@chat.example",
                "application_ref": "12@apps.example",
                "actor": {
                    "id": "20",
                    "origin_domain": "member.example",
                    "username": "member",
                },
                "user_installation": {"id": "41"},
                "device_snapshot": {"version": 1},
            },
        )

    assert response.status_code == 422
    consumed.assert_not_awaited()


def test_user_installation_authority_predicate_fails_closed_for_foreign_null_or_expiry() -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    local = installation(user_domain="chat.example")
    foreign_null = installation()
    foreign_live = installation(expiry=now + timedelta(seconds=1))

    assert user_installation_is_usable(
        local,
        current_instance_domain="chat.example",
        at=now,
    )
    assert not user_installation_is_usable(
        foreign_null,
        current_instance_domain="chat.example",
        at=now,
    )
    assert user_installation_is_usable(
        foreign_live,
        current_instance_domain="chat.example",
        at=now,
    )
    assert not user_installation_is_usable(
        foreign_live,
        current_instance_domain="chat.example",
        at=now + timedelta(seconds=1),
    )

    statement = str(usable_user_installation(current_instance_domain="chat.example", at=now))
    assert "bot_user_installations.status" in statement
    assert "bot_user_installations.revoked_at IS NULL" in statement
    assert "bot_user_installations.user_domain" in statement
    assert "bot_user_installations.authority_expires_at IS NOT NULL" in statement
    assert "bot_user_installations.authority_expires_at >" in statement


def test_authority_expiry_accepts_the_configured_ahead_clock_boundary() -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    skew = timedelta(minutes=15)
    boundary = now + USER_INSTALLATION_AUTHORITY_LEASE + skew

    assert (
        federated_user_installation_authority_expiry(
            grant(boundary),
            now=now,
            minimum_expires_at=now + timedelta(minutes=15),
            clock_skew=skew,
        )
        == boundary
    )
    with pytest.raises(HTTPException) as beyond:
        federated_user_installation_authority_expiry(
            grant(boundary + timedelta(microseconds=1)),
            now=now,
            clock_skew=skew,
        )
    assert beyond.value.detail["code"] == "USER_INSTALLATION_AUTHORITY_EXPIRY_INVALID"
    with pytest.raises(HTTPException) as below_lifecycle_floor:
        federated_user_installation_authority_expiry(
            grant(now + timedelta(minutes=5)),
            now=now,
            minimum_expires_at=now + timedelta(minutes=10),
            clock_skew=skew,
        )
    assert (
        below_lifecycle_floor.value.detail["code"] == "USER_INSTALLATION_AUTHORITY_EXPIRY_INVALID"
    )


@pytest.mark.asyncio
async def test_equal_revision_only_extends_or_reactivates_an_expired_mirror() -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    row = installation(
        expiry=now - timedelta(seconds=1),
        status="suspended",
    )
    session = SimpleNamespace(add=Mock())
    renewed_expiry = now + timedelta(minutes=10)

    renewed = await reconcile_federated_user_installation(
        cast(Any, session),
        cast(Any, SimpleNamespace(mint=AsyncMock())),
        cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
        (12, "apps.example"),
        (41, "member.example"),
        grant(renewed_expiry),
        row,
        now=now,
    )

    assert renewed is row
    assert (row.status, row.grant_revision, row.revoked_at) == ("active", 7, None)
    assert row.authority_expires_at == renewed_expiry

    await reconcile_federated_user_installation(
        cast(Any, session),
        cast(Any, SimpleNamespace(mint=AsyncMock())),
        cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
        (12, "apps.example"),
        (41, "member.example"),
        grant(now + timedelta(minutes=5)),
        row,
        now=now,
    )
    assert row.authority_expires_at == renewed_expiry

    with pytest.raises(HTTPException) as stale:
        await reconcile_federated_user_installation(
            cast(Any, session),
            cast(Any, SimpleNamespace(mint=AsyncMock())),
            cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
            (12, "apps.example"),
            (41, "member.example"),
            grant(now + timedelta(minutes=10), revision=6),
            row,
            now=now,
        )
    assert stale.value.detail["code"] == "USER_INSTALLATION_GRANT_STALE"


@pytest.mark.asyncio
async def test_delayed_pre_uninstall_grant_keeps_its_original_absolute_lease_bound() -> None:
    issued_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    applied_at = issued_at + timedelta(minutes=19)
    absolute_expiry = issued_at + USER_INSTALLATION_AUTHORITY_LEASE
    row = installation(
        expiry=issued_at + timedelta(minutes=10),
        status="suspended",
    )

    await reconcile_federated_user_installation(
        cast(Any, SimpleNamespace(add=Mock())),
        cast(Any, SimpleNamespace(mint=AsyncMock())),
        cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
        (12, "apps.example"),
        (41, "member.example"),
        grant(absolute_expiry),
        row,
        now=applied_at,
        maximum_expires_at=absolute_expiry,
    )

    assert row.status == "active"
    assert row.authority_expires_at == absolute_expiry
    assert not user_installation_is_usable(
        row,
        current_instance_domain="chat.example",
        at=absolute_expiry,
    )

    # A target that has already observed a post-uninstall/newer authority
    # revision rejects the delayed old assertion outright.
    row.grant_revision = 8
    with pytest.raises(HTTPException) as stale:
        await reconcile_federated_user_installation(
            cast(Any, SimpleNamespace(add=Mock())),
            cast(Any, SimpleNamespace(mint=AsyncMock())),
            cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
            (12, "apps.example"),
            (41, "member.example"),
            grant(absolute_expiry, revision=7),
            row,
            now=applied_at,
            maximum_expires_at=absolute_expiry,
        )
    assert stale.value.detail["code"] == "USER_INSTALLATION_GRANT_STALE"

    # Even if receiver-relative validation would allow a fresh twenty-minute
    # window, the signed operation's issuance bound does not.
    with pytest.raises(HTTPException) as rebased:
        federated_user_installation_authority_expiry(
            grant(applied_at + USER_INSTALLATION_AUTHORITY_LEASE),
            now=applied_at,
            maximum_expires_at=absolute_expiry,
        )
    assert rebased.value.detail["code"] == "USER_INSTALLATION_AUTHORITY_EXPIRY_INVALID"


@pytest.mark.asyncio
async def test_expiry_sweep_preserves_authority_revision_and_converges_derived_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = installation(expiry=datetime.now(UTC) - timedelta(seconds=1))
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        flush=AsyncMock(),
    )
    paused_channel = SimpleNamespace(id=90, origin_domain="chat.example")
    revoke = AsyncMock(return_value=[paused_channel])

    async def queue_targets(*_args: object, **_kwargs: object) -> set[str]:
        assert row.status == "suspended"
        return {"apps.example"}

    queue = AsyncMock(side_effect=queue_targets)
    monkeypatch.setattr(target_discovery, "revoke_bot_e2ee_access", revoke)
    monkeypatch.setattr(
        target_discovery,
        "queue_application_target_snapshots_for_refs",
        queue,
    )

    expired, destinations, channels = await expire_foreign_user_installation_leases(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
    )

    assert expired == 1
    assert destinations == {"apps.example"}
    assert channels == [paused_channel]
    assert (row.status, row.grant_revision) == ("suspended", 7)
    revoke.assert_awaited_once()
    assert revoke.await_args.kwargs["user_installation_ids"] == (51,)
    queue.assert_awaited_once_with(
        session,
        SimpleNamespace(domain="chat.example"),
        {(12, "apps.example")},
    )
    sweep_sql = str(session.scalars.await_args.args[0])
    assert "bot_user_installations.user_domain !=" in sweep_sql
    assert "bot_user_installations.authority_expires_at IS NULL" in sweep_sql
    assert "bot_user_installations.authority_expires_at <= now()" in sweep_sql
    assert "FOR UPDATE" in sweep_sql


@pytest.mark.asyncio
async def test_application_target_count_uses_the_shared_authority_lease_predicate() -> None:
    session = SimpleNamespace(scalar=AsyncMock(side_effect=[2, 3]))
    application = BotApplication(
        id=12,
        origin_domain="apps.example",
        team_id=1,
        team_domain="apps.example",
        bot_user_id=2,
        bot_user_domain="apps.example",
        name="weather",
    )
    counts = await application_target_counts(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="chat.example")),
        application,
    )

    assert counts == (2, 3)
    user_count_sql = str(session.scalar.await_args_list[1].args[0])
    assert "bot_user_installations.user_domain" in user_count_sql
    assert "bot_user_installations.authority_expires_at" in user_count_sql


@pytest.mark.asyncio
async def test_dm_e2ee_renewal_after_expiry_sweep_reannounces_the_runtime_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    row = installation(expiry=now - timedelta(seconds=1))
    sweep_session = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(target_discovery, "revoke_bot_e2ee_access", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        target_discovery,
        "queue_application_target_snapshots_for_refs",
        AsyncMock(return_value={"apps.example"}),
    )
    await expire_foreign_user_installation_leases(
        cast(Any, sweep_session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
    )
    assert row.status == "suspended"

    application = SimpleNamespace(id=12, origin_domain="apps.example")
    bot = SimpleNamespace(
        id=13,
        origin_domain="apps.example",
        e2ee_device_generation=0,
    )
    actor = SimpleNamespace(
        id=20,
        origin_domain="member.example",
        account_type="human",
        disabled_at=None,
    )
    renewed_expiry = now + timedelta(minutes=10)
    request = bot_e2ee_api.BotDME2EEAuthorityRequest(
        request_id="kadme_" + "a" * 32,
        issued_at=int(now.timestamp()),
        deadline=int(now.timestamp()) + 10,
        operation="grant",
        channel_ref="90@chat.example",
        application_ref="12@apps.example",
        actor=RemoteUserProfile(
            id="20",
            origin_domain="member.example",
            username="member",
        ),
        user_installation=grant(renewed_expiry).model_dump(mode="json"),
        device_snapshot={"version": 1},
    )
    session = SimpleNamespace(
        execute=AsyncMock(),
        flush=AsyncMock(),
    )
    settings = SimpleNamespace(
        domain="chat.example",
        federation_clock_skew_seconds=300,
    )
    order: list[str] = []

    async def queue_target(*_args: object, **_kwargs: object) -> set[str]:
        order.append("queue")
        assert row.status == "active"
        return {"apps.example"}

    async def grant_e2ee(*_args: object, **_kwargs: object) -> dict[str, object]:
        order.append("commit")
        return dm_e2ee_body()

    async def wake_target(destinations: set[str]) -> None:
        order.append("wake")
        assert destinations == {"apps.example"}

    guild_peer_admission = Mock(side_effect=AssertionError("DM management is not guild traffic"))
    monkeypatch.setattr(
        bot_e2ee_api,
        "require_guild_federation_access",
        guild_peer_admission,
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(bot_e2ee_api, "consume_management_request_once", AsyncMock())
    monkeypatch.setattr(bot_e2ee_api, "upsert_remote_user", AsyncMock(return_value=actor))
    mutation_admission = AsyncMock()
    monkeypatch.setattr(
        bot_e2ee_api,
        "require_remote_user_creation_allowed",
        mutation_admission,
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "require_federated_user_application",
        AsyncMock(return_value=application),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "locked_federated_user_installation",
        AsyncMock(return_value=row),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_user_installation_for_e2ee",
        AsyncMock(return_value=(application, bot, row)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "validated_bot_e2ee_snapshot",
        AsyncMock(return_value=SimpleNamespace(generation=1)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "materialize_bot_e2ee_snapshot",
        AsyncMock(return_value=[SimpleNamespace(id=1)]),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "queue_application_target_snapshots_for_refs",
        AsyncMock(side_effect=queue_target),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_grant_dm_bot_e2ee",
        AsyncMock(side_effect=grant_e2ee),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "wake_application_target_deliveries",
        AsyncMock(side_effect=wake_target),
    )

    result = await bot_e2ee_api.federation_dm_bot_e2ee_management(
        request,
        cast(Any, SimpleNamespace(silenced=True, origin="member.example")),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, settings),
    )

    assert result["body"] == dm_e2ee_body()
    assert row.grant_revision == 7
    assert row.authority_expires_at == renewed_expiry
    assert order == ["queue", "commit", "wake"]
    mutation_admission.assert_awaited_once_with(session, actor)
    guild_peer_admission.assert_not_called()


@pytest.mark.asyncio
async def test_dm_e2ee_proxy_materializes_then_renews_the_same_authority_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home_session = SimpleNamespace()
    mirror: BotUserInstallation | None = None

    def capture_mirror(row: BotUserInstallation) -> None:
        nonlocal mirror
        mirror = row

    target_session = SimpleNamespace(
        execute=AsyncMock(),
        flush=AsyncMock(),
        add=Mock(side_effect=capture_mirror),
    )
    home_settings = SimpleNamespace(domain="member.example")
    target_settings = SimpleNamespace(
        domain="chat.example",
        federation_clock_skew_seconds=300,
    )
    channel = SimpleNamespace(id=90, origin_domain="chat.example")
    conversation = SimpleNamespace(authority_domain="chat.example")
    application = SimpleNamespace(id=12, origin_domain="apps.example")
    bot = SimpleNamespace(
        id=13,
        origin_domain="apps.example",
        e2ee_device_generation=0,
    )
    actor = SimpleNamespace(
        id=20,
        origin_domain="member.example",
        account_type="human",
        disabled_at=None,
    )
    local_installation = BotUserInstallation(
        id=41,
        application_id=12,
        application_domain="apps.example",
        user_id=20,
        user_domain="member.example",
        granted_scopes=["applications.commands", "interactions.respond"],
        granted_intents=["interactions"],
        contexts=["bot_dm"],
        grant_revision=7,
        status="active",
    )
    snapshot_envelope = {"type": "e2ee.device-list.changed", "content": {"generation": "1"}}
    devices = [SimpleNamespace(id=501)]

    async def installation_for_side(
        session: object,
        *_args: object,
        **_kwargs: object,
    ) -> tuple[object, object, BotUserInstallation]:
        if session is home_session:
            return application, bot, local_installation
        assert session is target_session
        assert mirror is not None
        return application, bot, mirror

    locked = AsyncMock(side_effect=lambda *_args, **_kwargs: mirror)
    queue_targets = AsyncMock(return_value={"apps.example"})
    wake_targets = AsyncMock()
    grant_e2ee = AsyncMock(return_value=dm_e2ee_body())
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_remote_authority",
        AsyncMock(return_value=(channel, conversation)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "profile_from_user",
        Mock(
            return_value={
                "id": "20",
                "origin_domain": "member.example",
                "username": "member",
            }
        ),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_user_installation_for_e2ee",
        AsyncMock(side_effect=installation_for_side),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_device_snapshot_envelope",
        AsyncMock(return_value=snapshot_envelope),
    )
    monkeypatch.setattr(bot_e2ee_api, "require_guild_federation_access", Mock())
    monkeypatch.setattr(
        bot_e2ee_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr(bot_e2ee_api, "consume_management_request_once", AsyncMock())
    monkeypatch.setattr(bot_e2ee_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        bot_e2ee_api,
        "require_remote_user_creation_allowed",
        AsyncMock(),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "require_federated_user_application",
        AsyncMock(return_value=application),
    )
    monkeypatch.setattr(bot_e2ee_api, "locked_federated_user_installation", locked)
    monkeypatch.setattr(
        bot_e2ee_api,
        "validated_bot_e2ee_snapshot",
        AsyncMock(return_value=SimpleNamespace(generation=1)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "materialize_bot_e2ee_snapshot",
        AsyncMock(return_value=devices),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "queue_application_target_snapshots_for_refs",
        queue_targets,
    )
    monkeypatch.setattr(bot_e2ee_api, "_grant_dm_bot_e2ee", grant_e2ee)
    monkeypatch.setattr(bot_e2ee_api, "wake_application_target_deliveries", wake_targets)

    async def relay_to_authority(
        *_args: object,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bot_e2ee_api.BotDME2EEAuthorityResult:
        request = bot_e2ee_api.BotDME2EEAuthorityRequest.model_validate(payload)
        response = await bot_e2ee_api.federation_dm_bot_e2ee_management(
            request,
            cast(Any, SimpleNamespace(silenced=False, origin="member.example")),
            cast(Any, target_session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(mint=AsyncMock(return_value=901))),
            cast(Any, target_settings),
        )
        return bot_e2ee_api.BotDME2EEAuthorityResult.model_validate(response)

    monkeypatch.setattr(bot_e2ee_api, "request_management_rpc", relay_to_authority)

    first = await bot_e2ee_api._proxy_dm_bot_e2ee(
        cast(Any, home_session),
        cast(Any, home_settings),
        cast(Any, actor),
        bot_e2ee_api.EntityRef("90@chat.example"),
        bot_e2ee_api.EntityRef("12@apps.example"),
        "grant",
    )

    assert first == dm_e2ee_body()
    assert mirror is not None
    assert (mirror.source_id, mirror.source_domain, mirror.grant_revision) == (
        41,
        "member.example",
        7,
    )
    assert mirror.status == "active"
    first_expiry = mirror.authority_expires_at
    assert first_expiry is not None and first_expiry > datetime.now(UTC)

    mirror.status = "suspended"
    mirror.authority_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    second = await bot_e2ee_api._proxy_dm_bot_e2ee(
        cast(Any, home_session),
        cast(Any, home_settings),
        cast(Any, actor),
        bot_e2ee_api.EntityRef("90@chat.example"),
        bot_e2ee_api.EntityRef("12@apps.example"),
        "grant",
    )

    assert second == dm_e2ee_body()
    assert mirror.status == "active"
    assert mirror.grant_revision == 7
    assert mirror.authority_expires_at is not None
    assert mirror.authority_expires_at > datetime.now(UTC)
    assert target_session.add.call_count == 1
    assert queue_targets.await_count == 2
    assert wake_targets.await_count == 2
    for call in grant_e2ee.await_args_list:
        assert call.args[7] == (application, bot, mirror, devices)


@pytest.mark.asyncio
async def test_dm_e2ee_proxy_omits_user_proofs_for_capability_only_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=90, origin_domain="chat.example")
    conversation = SimpleNamespace(authority_domain="chat.example")
    no_user_install = HTTPException(
        status_code=404,
        detail={"code": "BOT_E2EE_PARTICIPANT_INSTALLATION_NOT_FOUND"},
    )
    snapshot = AsyncMock()
    seen: dict[str, object] = {}

    async def rpc(
        *_args: object,
        payload: dict[str, object],
        **_kwargs: object,
    ) -> bot_e2ee_api.BotDME2EEAuthorityResult:
        seen.update(payload)
        return bot_e2ee_api.BotDME2EEAuthorityResult(
            request_id=str(payload["request_id"]),
            operation=cast(Any, payload["operation"]),
            channel_ref=cast(Any, payload["channel_ref"]),
            application_ref=cast(Any, payload["application_ref"]),
            body=dm_e2ee_body(),
        )

    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_remote_authority",
        AsyncMock(return_value=(channel, conversation)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "profile_from_user",
        Mock(
            return_value={
                "id": "20",
                "origin_domain": "member.example",
                "username": "member",
            }
        ),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_user_installation_for_e2ee",
        AsyncMock(side_effect=no_user_install),
    )
    monkeypatch.setattr(bot_e2ee_api, "_dm_device_snapshot_envelope", snapshot)
    monkeypatch.setattr(bot_e2ee_api, "request_management_rpc", rpc)

    result = await bot_e2ee_api._proxy_dm_bot_e2ee(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
        bot_e2ee_api.EntityRef("90@chat.example"),
        bot_e2ee_api.EntityRef("12@apps.example"),
        "grant",
    )

    assert result == dm_e2ee_body()
    assert seen["user_installation"] is None
    assert seen["device_snapshot"] is None
    snapshot.assert_not_awaited()


def test_dm_e2ee_authority_result_requires_qualified_matching_body_identity() -> None:
    base = {
        "request_id": "kadme_" + "a" * 32,
        "operation": "get",
        "channel_ref": "90@chat.example",
        "application_ref": "12@apps.example",
        "body": dm_e2ee_body(),
    }

    bot_e2ee_api.BotDME2EEAuthorityResult.model_validate(base)
    for change in (
        {"channel_ref": "90"},
        {"application_ref": "12"},
        {"body": dm_e2ee_body(channel_ref="91@chat.example")},
        {"body": dm_e2ee_body(application_ref="13@apps.example")},
        {"body": {"status": "active"}},
    ):
        with pytest.raises(ValueError):
            bot_e2ee_api.BotDME2EEAuthorityResult.model_validate(base | change)


@pytest.mark.asyncio
async def test_dm_e2ee_proxy_qualifies_application_and_binds_all_response_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=90, origin_domain="chat.example")
    conversation = SimpleNamespace(authority_domain="chat.example")

    async def rpc(
        *_args: object,
        payload: dict[str, object],
        response_matches: object,
        **_kwargs: object,
    ) -> bot_e2ee_api.BotDME2EEAuthorityResult:
        assert payload["application_ref"] == "12@member.example"
        body = dm_e2ee_body(application_ref="12@member.example")
        result = bot_e2ee_api.BotDME2EEAuthorityResult(
            request_id=cast(str, payload["request_id"]),
            operation=cast(Any, payload["operation"]),
            channel_ref=cast(Any, payload["channel_ref"]),
            application_ref=cast(Any, payload["application_ref"]),
            body=body,
        )
        matches = cast(Any, response_matches)
        assert matches(result)
        for substitution in (
            {"request_id": "kadme_" + "b" * 32},
            {"operation": "revoke"},
            {
                "channel_ref": "91@chat.example",
                "body": dm_e2ee_body(
                    channel_ref="91@chat.example",
                    application_ref="12@member.example",
                ),
            },
            {
                "application_ref": "13@member.example",
                "body": dm_e2ee_body(application_ref="13@member.example"),
            },
        ):
            assert not matches(
                bot_e2ee_api.BotDME2EEAuthorityResult.model_validate(
                    result.model_dump(mode="json") | substitution
                )
            )
        return result

    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_remote_authority",
        AsyncMock(return_value=(channel, conversation)),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "profile_from_user",
        Mock(
            return_value={
                "id": "20",
                "origin_domain": "member.example",
                "username": "member",
            }
        ),
    )
    monkeypatch.setattr(bot_e2ee_api, "request_management_rpc", rpc)

    result = await bot_e2ee_api._proxy_dm_bot_e2ee(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="member.example")),
        cast(Any, SimpleNamespace(id=20, origin_domain="member.example")),
        bot_e2ee_api.EntityRef("90@chat.example"),
        bot_e2ee_api.EntityRef("12"),
        "get",
    )

    assert result == dm_e2ee_body(application_ref="12@member.example")


@pytest.mark.asyncio
async def test_dm_e2ee_grant_reactivates_the_matching_revoked_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=20, origin_domain="member.example")
    channel = SimpleNamespace(
        id=90,
        origin_domain="chat.example",
        last_message_id=80,
        last_message_domain="chat.example",
        encryption_mode="e2ee",
        encryption_state="active",
    )
    grant_row = SimpleNamespace(
        id=70,
        consent_state="active",
        consent_generation=4,
        history_floor_message_id=None,
        history_floor_message_domain=None,
    )
    consent = SimpleNamespace(
        consent_generation=4,
        status="active",
        consented_at=None,
        revoked_at=None,
    )
    revoked = SimpleNamespace(
        device_id=501,
        status="revoked",
        revoked_at=datetime.now(UTC),
        consent_generation=2,
        joined_epoch=9,
        history_floor_message_id=1,
        history_floor_message_domain="chat.example",
        consenting_actor_id=1,
        consenting_actor_domain="old.example",
    )
    active = SimpleNamespace(
        device_id=502,
        status="active",
        revoked_at=None,
        consent_generation=4,
        joined_epoch=3,
        history_floor_message_id=2,
        history_floor_message_domain="chat.example",
        consenting_actor_id=20,
        consenting_actor_domain="member.example",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=grant_row),
        get=AsyncMock(return_value=consent),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                tuples=Mock(return_value=[(actor.id, actor.origin_domain)])
            )
        ),
        scalars=AsyncMock(return_value=[revoked, active]),
        add=Mock(),
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_e2ee_context",
        AsyncMock(return_value=(SimpleNamespace(), channel, [actor])),
    )
    monkeypatch.setattr(
        bot_e2ee_api,
        "_dm_grant_state",
        AsyncMock(return_value=dm_e2ee_body()),
    )
    monkeypatch.setattr(bot_e2ee_api, "publish_e2ee_policy_updates", AsyncMock())
    app = SimpleNamespace(id=12, origin_domain="apps.example")
    bot = SimpleNamespace(id=13, origin_domain="apps.example")
    prepared = (
        app,
        bot,
        installation(expiry=datetime.now(UTC) + timedelta(minutes=5)),
        [SimpleNamespace(id=501), SimpleNamespace(id=502)],
    )

    await bot_e2ee_api._grant_dm_bot_e2ee(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="chat.example")),
        cast(Any, actor),
        bot_e2ee_api.EntityRef("90@chat.example"),
        bot_e2ee_api.EntityRef("12@apps.example"),
        prepared,
    )

    assert revoked.status == "pending"
    assert revoked.revoked_at is None
    assert revoked.consent_generation == 4
    assert revoked.joined_epoch == 0
    assert (revoked.consenting_actor_id, revoked.consenting_actor_domain) == (
        20,
        "member.example",
    )
    assert (active.status, active.joined_epoch) == ("active", 3)


@pytest.mark.asyncio
async def test_dm_e2ee_snapshot_rejects_another_application_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = SimpleNamespace(
        type=bot_e2ee_service.BOT_E2EE_DEVICE_SNAPSHOT_EVENT,
        actor=SimpleNamespace(id="13", domain="apps.example"),
        content={
            "application_id": "99",
            "application_domain": "apps.example",
            "bot_user_id": "13",
            "bot_user_domain": "apps.example",
            "generation": "1",
            "devices": [],
        },
    )
    monkeypatch.setattr(
        bot_e2ee_service,
        "validated_event_envelope",
        AsyncMock(return_value=envelope),
    )

    with pytest.raises(ValueError, match="identity is inconsistent"):
        await bot_e2ee_service.validated_bot_e2ee_snapshot(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            "apps.example",
            {},
            application_id=12,
            bot_user_ref=(13, "apps.example"),
        )
