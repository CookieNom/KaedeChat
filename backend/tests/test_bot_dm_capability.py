from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.api.bot_dm_federation as bot_dm_api
import app.bots.dm_capability as dm_capability
from app.api.bot_dm_federation import attest_bot_dm_capability
from app.bots.auth import worker_runtime_ready
from app.bots.dm_capability import (
    BOT_DM_CAPABILITY_EVENT,
    BOT_DM_CAPABILITY_HIGHWATER_RETENTION,
    BOT_DM_CAPABILITY_LEASE,
    MAX_BOT_DM_CAPABILITY_HIGHWATERS_PER_INSTALLATION_AUTHORITY,
    BotDMCapabilityApplyRequest,
    BotDMCapabilityAttestRequest,
    BotDMCapabilityAuthorityUnavailable,
    BotDMCapabilityPayload,
    BotDMCapabilityProofInvalid,
    BotDMCapabilitySourceRejected,
    apply_bot_dm_capability,
    bot_dm_capability_fence_expectation,
    bot_dm_grant_id,
    capability_authorization_fingerprint,
    capability_fingerprint,
    capability_is_active,
    fence_bot_dm_capability,
    fence_bot_dm_capability_projections,
    require_capability_runtime_binding,
    revoke_bot_dm_capabilities,
    usable_dm_capability,
    validate_bot_dm_capability_at_source,
    validated_bot_dm_capability_context,
)
from app.bots.runtime_control import (
    APPLICATION_RUNTIME_EVENT,
    ApplicationRuntimeSnapshot,
    application_runtime_snapshot_fingerprint,
)
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotDMCapabilityHighwater,
    BotInstallation,
    BotWorker,
)
from app.db.models import DMConversation, GuildMember, User
from app.federation.network import FederationNetworkError
from app.federation.schemas import EventEnvelope, RemoteUserProfile

APP_DOMAIN = "apps.example"
INSTALL_DOMAIN = "guilds.example"
TARGET_DOMAIN = "users.example"
DM_DOMAIN = dm_authority_domain(
    f"weather@{APP_DOMAIN}",
    f"alice@{TARGET_DOMAIN}",
)
PAIR_KEY = dm_pair_key(
    f"weather@{APP_DOMAIN}",
    f"alice@{TARGET_DOMAIN}",
)


def test_usable_dm_capability_sql_uses_default_and_explicit_time_boundaries() -> None:
    default_statement = select(BotDMCapability.id).where(usable_dm_capability())
    default_compiled = default_statement.compile()
    default_sql = str(default_compiled)

    assert "bot_dm_capabilities.status =" in default_sql
    assert "bot_dm_capabilities.revoked_at IS NULL" in default_sql
    assert "bot_dm_capabilities.expires_at > now()" in default_sql
    assert "active" in default_compiled.params.values()

    boundary = datetime(2026, 8, 29, tzinfo=UTC)
    explicit_statement = select(BotDMCapability.id).where(usable_dm_capability(at=boundary))
    explicit_compiled = explicit_statement.compile()
    explicit_sql = str(explicit_compiled)

    assert "bot_dm_capabilities.expires_at >" in explicit_sql
    assert "now()" not in explicit_sql
    assert boundary in explicit_compiled.params.values()


def bot_user() -> User:
    return User(
        id=10,
        origin_domain=APP_DOMAIN,
        is_local=False,
        account_type="bot",
        username="weather",
        password_hash=None,
        profile_version=1,
        e2ee_device_generation=1,
    )


def target_user() -> User:
    return User(
        id=20,
        origin_domain=TARGET_DOMAIN,
        is_local=False,
        account_type="human",
        username="alice",
        password_hash=None,
        profile_version=1,
        e2ee_device_generation=1,
    )


def payload(
    *,
    source_kind: str = "guild",
    revision: str = "1",
    runtime_manifest_generation: str = "3",
    runtime_revocation_generation: str = "2",
    target_access_revocation_generation: str = "0",
    runtime_snapshot_fingerprint: str = "a" * 64,
    status: str = "active",
    expires_at: datetime | None = None,
) -> BotDMCapabilityPayload:
    source_domain = INSTALL_DOMAIN if source_kind == "guild" else TARGET_DOMAIN
    installation_ref = f"30@{source_domain}"
    return BotDMCapabilityPayload.model_validate(
        {
            "grant_id": bot_dm_grant_id(
                source_kind,  # type: ignore[arg-type]
                installation_ref,
                f"40@{APP_DOMAIN}",
                f"10@{APP_DOMAIN}",
                PAIR_KEY,
                DM_DOMAIN,
            ),
            "source_kind": source_kind,
            "installation_ref": installation_ref,
            "application_ref": f"40@{APP_DOMAIN}",
            "bot_user_ref": f"10@{APP_DOMAIN}",
            "guild_ref": f"50@{INSTALL_DOMAIN}" if source_kind == "guild" else None,
            "installing_user_ref": (f"20@{TARGET_DOMAIN}" if source_kind == "user" else None),
            "target_user_ref": f"20@{TARGET_DOMAIN}",
            "pair_key": PAIR_KEY,
            "authority_domain": DM_DOMAIN,
            "scopes": ["attachments.read", "attachments.write", "dm.send"],
            "intents": ["direct_messages"],
            "channel_restrictions": [],
            "e2ee_mode": "participant",
            "installation_revision": "7",
            "runtime_manifest_generation": runtime_manifest_generation,
            "runtime_revocation_generation": runtime_revocation_generation,
            "target_access_revocation_generation": target_access_revocation_generation,
            "runtime_snapshot_fingerprint": runtime_snapshot_fingerprint,
            "revision": revision,
            "status": status,
            "expires_at_ms": str(
                int((expires_at or datetime.now(UTC) + timedelta(minutes=5)).timestamp() * 1000)
            ),
        }
    )


def envelope(capability: BotDMCapabilityPayload) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": "kcfe_botdmcapability0001",
            "origin": INSTALL_DOMAIN,
            "type": BOT_DM_CAPABILITY_EVENT,
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "10", "domain": APP_DOMAIN},
            "context": {},
            "content": capability.model_dump(mode="json"),
            "signatures": {INSTALL_DOMAIN: {"ed25519:test": "signature"}},
        }
    )


def capability_row(
    capability: BotDMCapabilityPayload | None = None,
    *,
    status: str = "active",
    revoked_at: datetime | None = None,
    admission_revision: int | None = None,
) -> BotDMCapability:
    signed = capability or payload()
    guild = signed.guild
    installing_user = signed.installing_user
    return BotDMCapability(
        id=90,
        grant_id=signed.grant_id,
        source_kind=signed.source_kind,
        source_installation_id=signed.installation.id,
        source_installation_domain=signed.installation.domain,
        application_id=signed.application.id,
        application_domain=signed.application.domain,
        bot_user_id=signed.bot_user.id,
        bot_user_domain=signed.bot_user.domain,
        guild_id=guild.id if guild is not None else None,
        guild_domain=guild.domain if guild is not None else None,
        installing_user_id=installing_user.id if installing_user is not None else None,
        installing_user_domain=(installing_user.domain if installing_user is not None else None),
        target_user_id=signed.target_user.id,
        target_user_domain=signed.target_user.domain,
        pair_key=signed.pair_key,
        authority_domain=signed.authority_domain,
        conversation_id=60,
        conversation_domain=signed.authority_domain,
        granted_scopes=list(signed.scopes),
        granted_intents=list(signed.intents),
        channel_restrictions=list(signed.channel_restrictions),
        e2ee_mode=signed.e2ee_mode,
        revision=int(signed.revision),
        admission_revision=(
            int(signed.revision) if admission_revision is None else admission_revision
        ),
        target_access_revocation_generation=int(signed.target_access_revocation_generation),
        status=status,
        proof_fingerprint=capability_fingerprint(signed),
        proof=envelope(signed).model_dump(mode="json"),
        expires_at=signed.expires_at,
        revoked_at=revoked_at,
    )


def runtime_snapshot(
    *,
    target_domain: str = DM_DOMAIN,
    manifest_generation: str = "3",
    revocation_generation: str = "2",
    access_revocation_generation: str = "0",
) -> ApplicationRuntimeSnapshot:
    return ApplicationRuntimeSnapshot.model_validate(
        {
            "application_id": "40",
            "application_domain": APP_DOMAIN,
            "bot_user_id": "10",
            "bot_user_domain": APP_DOMAIN,
            "target_domain": target_domain,
            "manifest_generation": manifest_generation,
            "revocation_generation": revocation_generation,
            "access_revocation_generation": access_revocation_generation,
            "status": "active",
            "target_allowed": True,
            "workers": [],
        }
    )


def runtime_envelope(snapshot: ApplicationRuntimeSnapshot) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": "kcfe_runtimeproof0001",
            "origin": APP_DOMAIN,
            "type": APPLICATION_RUNTIME_EVENT,
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "10", "domain": APP_DOMAIN},
            "context": {},
            "content": snapshot.model_dump(mode="json"),
            "signatures": {APP_DOMAIN: {"ed25519:test": "signature"}},
        }
    )


@pytest.mark.parametrize("source_kind", ["guild", "user"])
def test_capability_contract_binds_exact_install_source_and_conversation(
    source_kind: str,
) -> None:
    capability = payload(source_kind=source_kind)

    expected_source = INSTALL_DOMAIN if source_kind == "guild" else TARGET_DOMAIN
    assert capability.installation == EntityRef(f"30@{expected_source}")
    assert capability.application == EntityRef(f"40@{APP_DOMAIN}")
    assert capability.bot_user == EntityRef(f"10@{APP_DOMAIN}")
    assert capability.target_user == EntityRef(f"20@{TARGET_DOMAIN}")
    assert capability.guild is not None if source_kind == "guild" else capability.guild is None


def test_capability_contract_requires_exact_runtime_lineage_fields() -> None:
    capability = payload()

    assert capability.runtime_manifest_generation == "3"
    assert capability.runtime_revocation_generation == "2"
    assert capability.target_access_revocation_generation == "0"
    assert capability.runtime_snapshot_fingerprint == "a" * 64

    for field in (
        "runtime_manifest_generation",
        "runtime_revocation_generation",
        "target_access_revocation_generation",
        "runtime_snapshot_fingerprint",
    ):
        raw = capability.model_dump(mode="json")
        raw.pop(field)
        with pytest.raises(ValueError):
            BotDMCapabilityPayload.model_validate(raw)


def test_capability_wire_contract_rejects_ambiguous_integers_and_nul_text() -> None:
    raw = payload().model_dump(mode="json")
    with pytest.raises(ValueError, match="integer"):
        BotDMCapabilityPayload.model_validate(raw | {"version": True})
    with pytest.raises(ValueError, match="NUL"):
        BotDMCapabilityPayload.model_validate(raw | {"pair_key": "a" * 63 + "\x00"})


def test_capability_runtime_binding_requires_exact_target_tuple_and_fingerprint() -> None:
    snapshot = runtime_snapshot()
    proof = runtime_envelope(snapshot)
    capability = payload(
        runtime_snapshot_fingerprint=application_runtime_snapshot_fingerprint(snapshot).hex()
    )

    require_capability_runtime_binding(capability, proof, snapshot)

    with pytest.raises(ValueError, match="runtime proof binding"):
        require_capability_runtime_binding(
            capability.model_copy(update={"target_access_revocation_generation": "1"}),
            proof,
            snapshot,
        )
    with pytest.raises(ValueError, match="runtime proof binding"):
        require_capability_runtime_binding(
            capability.model_copy(update={"runtime_snapshot_fingerprint": "0" * 64}),
            proof,
            snapshot,
        )
    wrong_target = runtime_snapshot(target_domain="other.example")
    with pytest.raises(ValueError, match="runtime proof binding"):
        require_capability_runtime_binding(
            capability,
            runtime_envelope(wrong_target),
            wrong_target,
        )


def test_live_dm_worker_requires_exact_capability_runtime_and_worker_target() -> None:
    runtime_domain = "chat.example"
    snapshot = runtime_snapshot(target_domain=runtime_domain)
    accepted_fingerprint = application_runtime_snapshot_fingerprint(snapshot)
    raw_signed = payload(runtime_snapshot_fingerprint=accepted_fingerprint.hex()).model_dump(
        mode="json"
    )
    raw_signed["authority_domain"] = runtime_domain
    raw_signed["grant_id"] = bot_dm_grant_id(
        "guild",
        f"30@{INSTALL_DOMAIN}",
        f"40@{APP_DOMAIN}",
        f"10@{APP_DOMAIN}",
        PAIR_KEY,
        runtime_domain,
    )
    signed = BotDMCapabilityPayload.model_validate(raw_signed)
    application = BotApplication(
        id=40,
        origin_domain=APP_DOMAIN,
        team_id=70,
        team_domain=APP_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        name="Weather",
        status="active",
        manifest_generation=3,
        revocation_generation=2,
    )
    target = BotApplicationTarget(
        application_id=40,
        application_domain=APP_DOMAIN,
        target_domain=runtime_domain,
        generation=1,
        guild_installations=0,
        user_installations=0,
        runtime_manifest_generation=3,
        runtime_revocation_generation=2,
        runtime_access_revocation_generation=0,
        runtime_status="active",
        runtime_target_allowed=True,
        runtime_fingerprint=accepted_fingerprint,
    )
    row = BotDMCapability(
        id=90,
        grant_id=signed.grant_id,
        source_kind="guild",
        source_installation_id=30,
        source_installation_domain=INSTALL_DOMAIN,
        application_id=40,
        application_domain=APP_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        guild_id=50,
        guild_domain=INSTALL_DOMAIN,
        target_user_id=20,
        target_user_domain=TARGET_DOMAIN,
        pair_key=PAIR_KEY,
        authority_domain=runtime_domain,
        conversation_id=60,
        conversation_domain=runtime_domain,
        granted_scopes=list(signed.scopes),
        granted_intents=list(signed.intents),
        channel_restrictions=list(signed.channel_restrictions),
        e2ee_mode=signed.e2ee_mode,
        revision=1,
        target_access_revocation_generation=0,
        status="active",
        proof_fingerprint=capability_fingerprint(signed),
        proof=envelope(signed).model_dump(mode="json"),
        expires_at=signed.expires_at,
    )
    worker = BotWorker(
        id=80,
        source_id=80,
        source_domain=APP_DOMAIN,
        application_id=40,
        application_domain=APP_DOMAIN,
        name="production",
        public_key=b"k" * 32,
        scopes=list(signed.scopes),
        intents=list(signed.intents),
        target_domains=[],
        generation=1,
    )

    assert worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    assert not worker_runtime_ready(
        application,
        None,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    worker.target_domains = ["other.example"]
    assert not worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    worker.target_domains = []
    worker.revoked_at = datetime.now(UTC)
    assert not worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    worker.revoked_at = None

    target.runtime_target_allowed = False
    assert not worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    target.runtime_target_allowed = True
    target.runtime_access_revocation_generation = 1
    assert not worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=runtime_domain,
        dm_capability=row,
    )
    target.runtime_access_revocation_generation = 0
    application.manifest_generation = 4
    assert not worker_runtime_ready(
        application,
        worker,
        target,
        target_domain=DM_DOMAIN,
        dm_capability=row,
    )


def test_capability_request_contracts_carry_both_runtime_proofs() -> None:
    target = target_user()
    source_proof = runtime_envelope(runtime_snapshot(target_domain=INSTALL_DOMAIN))
    authority_proof = runtime_envelope(runtime_snapshot())
    attest = BotDMCapabilityAttestRequest(
        source_kind="guild",
        installation_ref=f"30@{INSTALL_DOMAIN}",
        application_ref=f"40@{APP_DOMAIN}",
        bot_user_ref=f"10@{APP_DOMAIN}",
        target=RemoteUserProfile.model_validate(
            {
                "id": str(target.id),
                "origin_domain": target.origin_domain,
                "account_type": "human",
                "username": target.username,
                "profile_version": 1,
                "e2ee_device_generation": 1,
            }
        ),
        pair_key=PAIR_KEY,
        authority_domain=DM_DOMAIN,
        source_runtime_proof=source_proof.model_dump(mode="json"),
        authority_runtime_proof=authority_proof.model_dump(mode="json"),
    )
    apply = BotDMCapabilityApplyRequest(
        proof=envelope(payload()).model_dump(mode="json"),
        runtime_proof=authority_proof.model_dump(mode="json"),
        grant_id=payload().grant_id,
        revision="1",
        conversation_ref=f"60@{DM_DOMAIN}",
    )

    assert attest.source_runtime_proof["type"] == APPLICATION_RUNTIME_EVENT
    assert attest.authority_runtime_proof["type"] == APPLICATION_RUNTIME_EVENT
    assert apply.runtime_proof["content"] == authority_proof.content


def test_user_install_capability_cannot_authorize_another_target() -> None:
    raw = payload(source_kind="user").model_dump(mode="json")
    raw["installing_user_ref"] = f"21@{TARGET_DOMAIN}"

    with pytest.raises(ValueError, match="only its installing user"):
        BotDMCapabilityPayload.model_validate(raw)


@pytest.mark.asyncio
async def test_context_validation_rejects_replay_to_another_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = payload()
    proof = envelope(capability)
    monkeypatch.setattr(
        dm_capability,
        "validated_bot_dm_capability_proof",
        AsyncMock(return_value=(proof, capability)),
    )

    with pytest.raises(ValueError, match="does not match"):
        await validated_bot_dm_capability_context(
            SimpleNamespace(),
            SimpleNamespace(),
            proof.model_dump(mode="json"),
            relay_domain=APP_DOMAIN,
            bot=bot_user(),
            target=target_user(),
            pair_key="0" * 64,
            authority_domain=DM_DOMAIN,
        )


@pytest.mark.asyncio
async def test_active_apply_requires_runtime_admission_and_renews_a_stable_revision() -> None:
    initial_expiry = datetime.now(UTC) + timedelta(minutes=5)
    capability = payload(expires_at=initial_expiry)
    proof = envelope(capability)
    rejected_session = SimpleNamespace(scalar=AsyncMock())

    with pytest.raises(ValueError, match="lacks current application runtime proof"):
        await apply_bot_dm_capability(
            rejected_session,
            SimpleNamespace(mint=AsyncMock(return_value=90)),
            proof,
            capability,
        )

    rejected_session.scalar.assert_not_awaited()
    bot = bot_user()
    target = target_user()
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None, None, 0, None]),
        execute=AsyncMock(),
        get=AsyncMock(side_effect=[bot, target]),
        add=Mock(),
    )
    snowflake = SimpleNamespace(mint=AsyncMock(return_value=90))
    conversation = DMConversation(
        id=60,
        origin_domain=DM_DOMAIN,
        pair_key=PAIR_KEY,
        type="direct",
        authority_domain=DM_DOMAIN,
    )

    row, changed = await apply_bot_dm_capability(
        session,
        snowflake,
        proof,
        capability,
        conversation=conversation,
        runtime_admitted=True,
    )

    assert changed is True
    assert row is not None
    assert (row.conversation_id, row.conversation_domain) == (60, DM_DOMAIN)
    assert capability_is_active(row)
    added = [call.args[0] for call in session.add.call_args_list]
    highwater = next(item for item in added if isinstance(item, BotDMCapabilityHighwater))
    assert row in added
    assert highwater.revision == 1
    assert highwater.authorization_fingerprint == capability_authorization_fingerprint(capability)
    assert highwater.expires_at > datetime.now(UTC) + BOT_DM_CAPABILITY_LEASE

    renewed = payload(expires_at=initial_expiry + timedelta(minutes=1))
    replay_session = SimpleNamespace(scalar=AsyncMock(side_effect=[None, highwater, row]))
    renewed_row, renewed_changed = await apply_bot_dm_capability(
        replay_session,
        snowflake,
        envelope(renewed),
        renewed,
        conversation=conversation,
        runtime_admitted=True,
    )
    assert renewed_changed is True
    assert renewed_row is row
    assert renewed_row.revision == 1
    assert renewed_row.expires_at == renewed.expires_at
    assert highwater.revision == 1
    assert highwater.authorization_fingerprint == capability_authorization_fingerprint(renewed)

    row.status = "suspended"
    row.revoked_at = datetime.now(UTC)
    recovered = payload(expires_at=renewed.expires_at + timedelta(minutes=1))
    recovered_proof = envelope(recovered)
    recovered_row, recovered_changed = await apply_bot_dm_capability(
        SimpleNamespace(scalar=AsyncMock(side_effect=[None, highwater, row])),
        snowflake,
        recovered_proof,
        recovered,
        conversation=conversation,
        runtime_admitted=True,
        admit_fenced_projection=True,
    )
    assert recovered_changed is True
    assert recovered_row is row
    assert row.status == "suspended"
    assert row.revoked_at is not None
    assert row.proof == recovered_proof.model_dump(mode="json")
    assert row.expires_at == recovered.expires_at

    newer = payload(
        revision="2",
        expires_at=recovered.expires_at + timedelta(minutes=1),
    )
    newer_proof = envelope(newer)
    admitted_row, admitted_changed = await apply_bot_dm_capability(
        SimpleNamespace(scalar=AsyncMock(side_effect=[None, highwater, row])),
        snowflake,
        newer_proof,
        newer,
        conversation=conversation,
        runtime_admitted=True,
        admit_fenced_projection=True,
    )
    assert admitted_changed is True
    assert admitted_row is row
    assert row.status == "active"
    assert row.revoked_at is None
    assert row.revision == 2
    assert row.admission_revision == 2
    assert row.proof == newer_proof.model_dump(mode="json")

    conflicting = payload(revision="2").model_copy(
        update={"scopes": ["attachments.read", "dm.send"]}
    )
    with pytest.raises(ValueError, match="equivocated"):
        await apply_bot_dm_capability(
            SimpleNamespace(scalar=AsyncMock(side_effect=[None, highwater])),
            snowflake,
            envelope(conflicting),
            conflicting,
            conversation=conversation,
            runtime_admitted=True,
        )


@pytest.mark.asyncio
async def test_exact_capability_fence_revokes_tokens_and_e2ee_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = capability_row()
    expectation = bot_dm_capability_fence_expectation(row)
    channel = SimpleNamespace(id=60, origin_domain=DM_DOMAIN)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, row]),
        execute=AsyncMock(),
    )
    revoke_e2ee = AsyncMock(return_value=[channel])
    monkeypatch.setattr("app.bots.e2ee.revoke_bot_e2ee_access", revoke_e2ee)

    fenced, channels = await fence_bot_dm_capability(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain=DM_DOMAIN),
        expectation,
    )

    assert fenced is True
    assert channels == [channel]
    assert row.status == "suspended"
    assert row.revoked_at is not None
    assert "pg_advisory_xact_lock" in str(session.scalar.await_args_list[0].args[0])
    row_lock = session.scalar.await_args_list[1].args[0]
    assert "FOR UPDATE" in str(row_lock)
    assert row_lock.get_execution_options()["populate_existing"] is True
    token_revoke = str(session.execute.await_args.args[0])
    assert "UPDATE bot_tokens" in token_revoke
    assert "bot_tokens.dm_capability_id" in token_revoke
    revoke_e2ee.assert_awaited_once()
    assert revoke_e2ee.await_args.kwargs["dm_capability_ids"] == (row.id,)


@pytest.mark.asyncio
async def test_stale_exact_fence_cannot_suspend_concurrent_newer_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = capability_row()
    stale = bot_dm_capability_fence_expectation(row)
    newer = payload(revision="2")
    row.revision = 2
    row.admission_revision = 2
    row.proof = envelope(newer).model_dump(mode="json")
    row.proof_fingerprint = capability_fingerprint(newer)
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, row]),
        execute=AsyncMock(),
    )
    revoke_e2ee = AsyncMock()
    monkeypatch.setattr("app.bots.e2ee.revoke_bot_e2ee_access", revoke_e2ee)

    fenced, channels = await fence_bot_dm_capability(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain=DM_DOMAIN),
        stale,
    )

    assert fenced is False
    assert channels == []
    assert row.status == "active"
    assert row.revision == 2
    session.execute.assert_not_awaited()
    revoke_e2ee.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_exception"),
    [
        (403, BotDMCapabilitySourceRejected),
        (401, BotDMCapabilityProofInvalid),
        (404, BotDMCapabilityProofInvalid),
        (409, BotDMCapabilityProofInvalid),
        (422, BotDMCapabilityProofInvalid),
        (429, BotDMCapabilityAuthorityUnavailable),
        (500, BotDMCapabilityAuthorityUnavailable),
    ],
)
async def test_only_exact_source_403_is_a_definitive_capability_rejection(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    expected_exception: type[Exception],
) -> None:
    response = httpx.Response(status_code, request=httpx.Request("POST", "https://source"))
    monkeypatch.setattr(
        dm_capability,
        "signed_request",
        AsyncMock(return_value=response),
    )

    with pytest.raises(expected_exception):
        await validate_bot_dm_capability_at_source(
            SimpleNamespace(),
            SimpleNamespace(domain=DM_DOMAIN),
            envelope(payload()),
            payload(),
        )


@pytest.mark.asyncio
async def test_source_transport_and_malformed_success_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = httpx.Request("POST", "https://source")
    signed = AsyncMock(side_effect=FederationNetworkError("offline"))
    monkeypatch.setattr(dm_capability, "signed_request", signed)
    with pytest.raises(BotDMCapabilityAuthorityUnavailable):
        await validate_bot_dm_capability_at_source(
            SimpleNamespace(),
            SimpleNamespace(domain=DM_DOMAIN),
            envelope(payload()),
            payload(),
        )

    signed.side_effect = None
    signed.return_value = httpx.Response(200, json={"grant_id": "wrong"}, request=request)
    with pytest.raises(BotDMCapabilityProofInvalid):
        await validate_bot_dm_capability_at_source(
            SimpleNamespace(),
            SimpleNamespace(domain=DM_DOMAIN),
            envelope(payload()),
            payload(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expires_at", "expected_code"),
    [
        ("suspended", None, "BOT_DM_GRANT_FENCED"),
        ("revoked", None, "BOT_DM_GRANT_FENCED"),
        ("active", datetime(2020, 1, 1, tzinfo=UTC), "BOT_DM_GRANT_INVALID"),
    ],
)
async def test_inactive_capability_apply_never_calls_runtime_or_source(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expires_at: datetime | None,
    expected_code: str,
) -> None:
    signed = payload(expires_at=expires_at)
    row = capability_row(
        signed,
        status=status,
        revoked_at=(datetime.now(UTC) if status != "active" else None),
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=row))
    runtime = AsyncMock()
    source = AsyncMock()
    monkeypatch.setattr(bot_dm_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(bot_dm_api, "durably_apply_application_runtime_proof", runtime)
    monkeypatch.setattr(bot_dm_api, "validate_bot_dm_capability_at_source", source)

    with pytest.raises(HTTPException) as rejected:
        await bot_dm_api.apply_refreshed_bot_dm_capability(
            BotDMCapabilityApplyRequest(
                proof=envelope(signed).model_dump(mode="json"),
                runtime_proof=runtime_envelope(runtime_snapshot()).model_dump(mode="json"),
                grant_id=signed.grant_id,
                revision=signed.revision,
                conversation_ref=f"60@{DM_DOMAIN}",
            ),
            SimpleNamespace(origin=APP_DOMAIN),
            session,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain=DM_DOMAIN),
        )

    assert rejected.value.detail["code"] == expected_code
    runtime.assert_not_awaited()
    source.assert_not_awaited()


@pytest.mark.asyncio
async def test_third_peer_cannot_advance_c_runtime_ledger_before_proof_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.federation as federation_api
    from app.federation.schemas import DMOpenFederationRequest

    signed = payload()
    request = DMOpenFederationRequest(
        participants=[
            RemoteUserProfile.model_validate(
                {
                    "id": "10",
                    "origin_domain": APP_DOMAIN,
                    "account_type": "bot",
                    "username": "weather",
                    "profile_version": 1,
                    "e2ee_device_generation": 1,
                }
            ),
            RemoteUserProfile.model_validate(
                {
                    "id": "20",
                    "origin_domain": TARGET_DOMAIN,
                    "account_type": "human",
                    "username": "alice",
                    "profile_version": 1,
                    "e2ee_device_generation": 1,
                }
            ),
        ],
        bot_capability=envelope(signed),
        bot_runtime_proof=runtime_envelope(runtime_snapshot()),
    )
    durable_runtime = AsyncMock()
    monkeypatch.setattr(
        federation_api,
        "durably_apply_application_runtime_proof",
        durable_runtime,
    )

    with pytest.raises(HTTPException) as rejected:
        await federation_api._authorize_bot_dm_open_capability(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain=DM_DOMAIN),
            request,
            [bot_user(), target_user()],
            relay_domain="captured.example",
            pair_key=PAIR_KEY,
            authority_domain=DM_DOMAIN,
        )

    assert rejected.value.status_code == 403
    assert rejected.value.detail["code"] == "BOT_DM_GRANT_INVALID"
    durable_runtime.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_block_fences_bot_capability_before_commit_and_publishes_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.relationships as relationships_api

    human = target_user()
    bot = bot_user()
    events: list[str] = []
    session = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(side_effect=lambda: events.append("commit")),
    )
    fence = AsyncMock(
        side_effect=lambda *_args, **_kwargs: (
            events.append("fence") or [SimpleNamespace(id=60, origin_domain=DM_DOMAIN)]
        )
    )
    publish = AsyncMock(side_effect=lambda *_args: events.append("publish"))
    monkeypatch.setattr(relationships_api, "target_by_id", AsyncMock(return_value=bot))
    monkeypatch.setattr(relationships_api, "lock_relationship_pair", AsyncMock())
    monkeypatch.setattr(relationships_api, "relationship", AsyncMock(return_value=None))
    monkeypatch.setattr(relationships_api, "queue_relationship_event", AsyncMock())
    monkeypatch.setattr(relationships_api, "fence_bot_dm_capabilities_for_pair", fence)
    monkeypatch.setattr(relationships_api, "publish_e2ee_policy_updates", publish)
    monkeypatch.setattr(relationships_api, "enqueue_best_effort", AsyncMock())
    monkeypatch.setattr(relationships_api, "notify_relationship", AsyncMock())

    response = await relationships_api.block_user(
        EntityRef(f"{bot.id}@{bot.origin_domain}"),
        SimpleNamespace(user=human),
        session,
        SimpleNamespace(),
        SimpleNamespace(domain=DM_DOMAIN),
    )

    assert response.status_code == 204
    assert events == ["fence", "commit", "publish"]
    fence.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_code", "expected_status", "fenced"),
    [
        ("BOT_DM_GRANT_FENCED", 403, True),
        ("BOT_DM_GRANT_INVALID", 502, False),
    ],
)
async def test_a_mirrors_only_c_explicit_exact_fence(
    monkeypatch: pytest.MonkeyPatch,
    remote_code: str,
    expected_status: int,
    fenced: bool,
) -> None:
    import app.api.bots as bots_api

    authority = "dm.remote.example"
    raw = payload().model_dump(mode="json")
    raw["authority_domain"] = authority
    raw["grant_id"] = bot_dm_grant_id(
        "guild",
        f"30@{INSTALL_DOMAIN}",
        f"40@{APP_DOMAIN}",
        f"10@{APP_DOMAIN}",
        PAIR_KEY,
        authority,
    )
    signed = BotDMCapabilityPayload.model_validate(raw)
    row = capability_row(signed)
    response = httpx.Response(
        403,
        json={"detail": {"code": remote_code}},
        request=httpx.Request("POST", f"https://{authority}"),
    )
    monkeypatch.setattr(bots_api, "signed_request", AsyncMock(return_value=response))
    local_fence = AsyncMock()
    monkeypatch.setattr(
        bots_api,
        "_commit_local_bot_dm_capability_fence",
        local_fence,
    )

    with pytest.raises(HTTPException) as rejected:
        await bots_api.relay_refreshed_bot_dm_capability(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain=APP_DOMAIN),
            row,
            row.proof,
            runtime_envelope(runtime_snapshot(target_domain=authority)),
        )

    assert rejected.value.status_code == expected_status
    assert (local_fence.await_count == 1) is fenced


@pytest.mark.asyncio
async def test_suspended_projection_fence_is_idempotent() -> None:
    capability = payload()
    row = BotDMCapability(
        id=90,
        grant_id=capability.grant_id,
        source_kind="guild",
        source_installation_id=30,
        source_installation_domain=INSTALL_DOMAIN,
        application_id=40,
        application_domain=APP_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        guild_id=50,
        guild_domain=INSTALL_DOMAIN,
        target_user_id=20,
        target_user_domain=TARGET_DOMAIN,
        pair_key=PAIR_KEY,
        authority_domain=DM_DOMAIN,
        conversation_id=60,
        conversation_domain=DM_DOMAIN,
        granted_scopes=capability.scopes,
        granted_intents=capability.intents,
        channel_restrictions=[],
        e2ee_mode="participant",
        revision=1,
        target_access_revocation_generation=0,
        status="active",
        proof_fingerprint=b"f" * 32,
        proof=envelope(capability).model_dump(mode="json"),
        expires_at=capability.expires_at,
    )
    fenced_at = datetime.now(UTC)
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[[row], []]),
        scalar=AsyncMock(side_effect=[None, row]),
        execute=AsyncMock(),
    )

    fenced = await fence_bot_dm_capability_projections(
        session,
        application_ref=(40, APP_DOMAIN),
        now=fenced_at,
    )
    assert fenced == [row]
    assert row.status == "suspended"
    assert row.revoked_at == fenced_at
    assert not capability_is_active(row)

    replayed = await fence_bot_dm_capability_projections(
        session,
        application_ref=(40, APP_DOMAIN),
        now=fenced_at + timedelta(minutes=1),
    )
    assert replayed == []
    assert row.revoked_at == fenced_at
    assert session.scalars.await_count == 2
    assert session.scalar.await_count == 2
    assert session.execute.await_count == 1
    assert "UPDATE bot_tokens" in str(session.execute.await_args.args[0])


@pytest.mark.asyncio
async def test_terminal_highwater_precedes_materialization_and_blocks_delayed_active() -> None:
    terminal = payload(revision="2", status="revoked")
    applied_at = datetime(2026, 1, 1, tzinfo=UTC)
    terminal_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None, None, 0, None]),
        execute=AsyncMock(),
        get=AsyncMock(),
        add=Mock(),
    )

    terminal_row, terminal_changed = await apply_bot_dm_capability(
        terminal_session,
        None,
        envelope(terminal),
        terminal,
        now=applied_at,
    )

    assert terminal_row is None
    assert terminal_changed is True
    highwater = terminal_session.add.call_args.args[0]
    assert isinstance(highwater, BotDMCapabilityHighwater)
    assert (highwater.revision, highwater.status) == (2, "revoked")
    assert highwater.expires_at == applied_at + BOT_DM_CAPABILITY_HIGHWATER_RETENTION
    assert highwater.authorization_fingerprint == capability_authorization_fingerprint(terminal)
    assert terminal_session.add.call_count == 1
    terminal_session.get.assert_not_awaited()

    delayed = payload(revision="1")
    delayed_snowflake = SimpleNamespace(mint=AsyncMock(return_value=90))
    delayed_session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, highwater, None]),
        get=AsyncMock(),
        add=Mock(),
    )
    delayed_row, delayed_changed = await apply_bot_dm_capability(
        delayed_session,
        delayed_snowflake,
        envelope(delayed),
        delayed,
        runtime_admitted=True,
        now=applied_at + BOT_DM_CAPABILITY_HIGHWATER_RETENTION - timedelta(seconds=1),
    )
    assert delayed_row is None
    assert delayed_changed is False
    assert (highwater.revision, highwater.status) == (2, "revoked")
    delayed_session.get.assert_not_awaited()
    delayed_session.add.assert_not_called()
    delayed_snowflake.mint.assert_not_awaited()

    equal_active = payload(revision="2")
    with pytest.raises(ValueError, match="equivocated"):
        await apply_bot_dm_capability(
            SimpleNamespace(scalar=AsyncMock(side_effect=[None, highwater])),
            SimpleNamespace(mint=AsyncMock(return_value=91)),
            envelope(equal_active),
            equal_active,
            runtime_admitted=True,
            now=applied_at + BOT_DM_CAPABILITY_HIGHWATER_RETENTION - timedelta(seconds=1),
        )


@pytest.mark.asyncio
async def test_highwater_retention_outlives_every_valid_active_lease_and_prunes_expiry() -> None:
    # Settings cap federation clock skew at 900 seconds. The extra margin
    # prevents collection at the exact boundary.
    assert BOT_DM_CAPABILITY_LEASE + timedelta(seconds=900) < BOT_DM_CAPABILITY_HIGHWATER_RETENTION
    current_time = datetime(2026, 1, 1, tzinfo=UTC)
    terminal = payload(revision="2", status="revoked")
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None, None, 0, None]),
        execute=AsyncMock(),
        get=AsyncMock(),
        add=Mock(),
    )

    await apply_bot_dm_capability(
        session,
        None,
        envelope(terminal),
        terminal,
        now=current_time,
    )

    cleanup = str(session.execute.await_args.args[0])
    assert "DELETE FROM bot_dm_capability_highwaters" in cleanup
    assert "installation_authority_domain" in cleanup
    assert "expires_at" in cleanup
    highwater = session.add.call_args.args[0]
    assert highwater.expires_at == current_time + BOT_DM_CAPABILITY_HIGHWATER_RETENTION


@pytest.mark.asyncio
async def test_new_highwater_rejects_installation_authority_over_live_quota() -> None:
    terminal = payload(revision="2", status="revoked")
    session = SimpleNamespace(
        scalar=AsyncMock(
            side_effect=[
                None,
                None,
                None,
                MAX_BOT_DM_CAPABILITY_HIGHWATERS_PER_INSTALLATION_AUTHORITY,
            ]
        ),
        execute=AsyncMock(),
        add=Mock(),
    )

    with pytest.raises(ValueError, match="high-water quota exceeded"):
        await apply_bot_dm_capability(
            session,
            None,
            envelope(terminal),
            terminal,
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert session.scalar.await_count == 4
    authority_lock = session.scalar.await_args_list[2].args[0]
    authority_lock_sql = str(authority_lock)
    assert "pg_advisory_xact_lock" in authority_lock_sql
    assert f"bot-dm-capability-highwater-authority:{INSTALL_DOMAIN}" in (
        authority_lock.compile().params.values()
    )
    session.add.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(("previous_revision", "expected_revision"), [(None, "1"), (1, "2")])
async def test_install_authority_serializes_and_fresh_open_advances_lineage(
    monkeypatch: pytest.MonkeyPatch,
    previous_revision: int | None,
    expected_revision: str,
) -> None:
    bot = bot_user()
    target = target_user()
    application = BotApplication(
        id=40,
        origin_domain=APP_DOMAIN,
        team_id=70,
        team_domain=APP_DOMAIN,
        bot_user_id=bot.id,
        bot_user_domain=bot.origin_domain,
        name="Weather",
        status="active",
        default_scopes=["attachments.read", "attachments.write", "dm.send"],
        default_intents=["direct_messages"],
        manifest_generation=3,
        revocation_generation=2,
    )
    installation = BotInstallation(
        id=30,
        application_id=40,
        application_domain=APP_DOMAIN,
        guild_id=50,
        guild_domain=INSTALL_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        installer_id=20,
        installer_domain=TARGET_DOMAIN,
        granted_scopes=["attachments.read", "attachments.write", "dm.send"],
        granted_intents=["direct_messages"],
        granted_permissions=0,
        channel_restrictions=[],
        e2ee_mode="participant",
        grant_revision=7,
        status="active",
    )
    result = Mock()
    result.one_or_none.return_value = (installation, application, bot)

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User:
            return bot if key == (bot.id, bot.origin_domain) else target
        if model is BotApplication:
            return application
        if model is GuildMember:
            return GuildMember(
                guild_id=50,
                guild_domain=INSTALL_DOMAIN,
                user_id=20,
                user_domain=TARGET_DOMAIN,
            )
        return None

    source_runtime = runtime_snapshot(target_domain=INSTALL_DOMAIN)
    authority_runtime = runtime_snapshot()
    source_target = SimpleNamespace(target_domain=INSTALL_DOMAIN)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(
            side_effect=[
                application,
                source_target,
                None,
                (
                    capability_row(payload(revision=str(previous_revision)))
                    if previous_revision is not None
                    else None
                ),
            ]
        ),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(bot_dm_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(bot_dm_api, "ensure_peer", AsyncMock())
    monkeypatch.setattr(
        bot_dm_api,
        "durably_apply_application_runtime_proof",
        AsyncMock(
            return_value=(runtime_envelope(source_runtime), source_runtime),
        ),
    )
    monkeypatch.setattr(
        bot_dm_api,
        "validate_application_runtime_proof",
        AsyncMock(return_value=(runtime_envelope(authority_runtime), authority_runtime)),
    )
    monkeypatch.setattr(bot_dm_api, "require_current_application_runtime_proof", Mock())
    build = AsyncMock(
        side_effect=lambda *args, **kwargs: envelope(payload()).model_dump(mode="json")
    )
    applied = AsyncMock(
        return_value=(
            SimpleNamespace(target_user_id=target.id),
            True,
        )
    )
    monkeypatch.setattr(bot_dm_api, "build_envelope", build)
    monkeypatch.setattr(bot_dm_api, "apply_bot_dm_capability", applied)
    request = BotDMCapabilityAttestRequest(
        source_kind="guild",
        installation_ref=f"30@{INSTALL_DOMAIN}",
        application_ref=f"40@{APP_DOMAIN}",
        bot_user_ref=f"10@{APP_DOMAIN}",
        target=RemoteUserProfile.model_validate(
            {
                "id": "20",
                "origin_domain": TARGET_DOMAIN,
                "account_type": "human",
                "username": "alice",
                "profile_version": 1,
                "e2ee_device_generation": 1,
            }
        ),
        pair_key=PAIR_KEY,
        authority_domain=DM_DOMAIN,
        source_runtime_proof=runtime_envelope(
            runtime_snapshot(target_domain=INSTALL_DOMAIN)
        ).model_dump(mode="json"),
        authority_runtime_proof=runtime_envelope(runtime_snapshot()).model_dump(mode="json"),
    )

    await attest_bot_dm_capability(
        request,
        SimpleNamespace(origin=APP_DOMAIN),
        session,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain=INSTALL_DOMAIN),
    )

    assert session.scalar.await_count == 4
    lock_sql = str(session.scalar.await_args_list[2].args[0])
    assert "pg_advisory_xact_lock" in lock_sql
    assert "hashtextextended" in lock_sql
    signed = build.await_args.args[4]
    assert signed["runtime_manifest_generation"] == "3"
    assert signed["runtime_revocation_generation"] == "2"
    assert signed["target_access_revocation_generation"] == "0"
    assert signed["revision"] == expected_revision
    assert (
        signed["runtime_snapshot_fingerprint"]
        == application_runtime_snapshot_fingerprint(authority_runtime).hex()
    )
    assert applied.await_args.kwargs["runtime_admitted"] is True
    session.commit.assert_awaited_once()


def test_capability_expiry_is_a_fail_closed_runtime_fence() -> None:
    row = BotDMCapability(
        id=90,
        grant_id=payload().grant_id,
        source_kind="guild",
        source_installation_id=30,
        source_installation_domain=INSTALL_DOMAIN,
        application_id=40,
        application_domain=APP_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        guild_id=50,
        guild_domain=INSTALL_DOMAIN,
        target_user_id=20,
        target_user_domain=TARGET_DOMAIN,
        pair_key=PAIR_KEY,
        authority_domain=DM_DOMAIN,
        granted_scopes=["dm.send"],
        granted_intents=["direct_messages"],
        channel_restrictions=[],
        e2ee_mode="disabled",
        revision=1,
        target_access_revocation_generation=0,
        status="active",
        proof_fingerprint=b"f" * 32,
        proof={},
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )

    assert capability_is_active(row) is False


@pytest.mark.asyncio
async def test_install_revocation_queues_distinct_monotonic_tombstones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dm_authority = "dm.example"
    current_raw = payload().model_dump(mode="json")
    current_raw["authority_domain"] = dm_authority
    current_raw["grant_id"] = bot_dm_grant_id(
        "guild",
        f"30@{INSTALL_DOMAIN}",
        f"40@{APP_DOMAIN}",
        f"10@{APP_DOMAIN}",
        PAIR_KEY,
        dm_authority,
    )
    current = BotDMCapabilityPayload.model_validate(current_raw)
    row = BotDMCapability(
        id=90,
        grant_id=current.grant_id,
        source_kind="guild",
        source_installation_id=30,
        source_installation_domain=INSTALL_DOMAIN,
        application_id=40,
        application_domain=APP_DOMAIN,
        bot_user_id=10,
        bot_user_domain=APP_DOMAIN,
        guild_id=50,
        guild_domain=INSTALL_DOMAIN,
        target_user_id=20,
        target_user_domain=TARGET_DOMAIN,
        pair_key=PAIR_KEY,
        authority_domain=dm_authority,
        conversation_id=60,
        conversation_domain=dm_authority,
        granted_scopes=current.scopes,
        granted_intents=current.intents,
        channel_restrictions=[],
        e2ee_mode="participant",
        revision=1,
        target_access_revocation_generation=0,
        status="active",
        proof_fingerprint=b"f" * 32,
        proof=envelope(current).model_dump(mode="json"),
        expires_at=current.expires_at,
    )
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[row]),
        scalar=AsyncMock(side_effect=[None, row]),
        get=AsyncMock(return_value=bot_user()),
    )
    built: list[dict[str, object]] = []

    async def build(
        _session: object,
        settings: object,
        _event_type: str,
        _actor: object,
        content: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        value = envelope(BotDMCapabilityPayload.model_validate(content)).model_dump(mode="json")
        value["origin"] = settings.domain  # type: ignore[attr-defined]
        value["event_id"] = f"kcfe_projection_event_{len(built)}"
        built.append(value)
        return value

    applied = AsyncMock(return_value=(row, True))
    compact = AsyncMock()
    queued = AsyncMock()
    monkeypatch.setattr(dm_capability, "apply_bot_dm_capability", applied)
    monkeypatch.setattr("app.federation.events.build_envelope", build)
    monkeypatch.setattr(
        "app.federation.events.discard_superseded_latest_state_event",
        compact,
    )
    monkeypatch.setattr("app.federation.events.queue_event", queued)

    rows, destinations = await revoke_bot_dm_capabilities(
        session,
        SimpleNamespace(domain=INSTALL_DOMAIN),
        guild_installation_ids=(30,),
    )

    assert rows == [row]
    assert destinations == {APP_DOMAIN, dm_authority}
    lock_sql = str(session.scalar.await_args_list[0].args[0])
    row_sql = str(session.scalar.await_args_list[1].args[0])
    assert "pg_advisory_xact_lock" in lock_sql
    assert "FOR UPDATE" in row_sql
    assert len({item["event_id"] for item in built}) == 3
    assert all(item["content"]["status"] == "revoked" for item in built)  # type: ignore[index]
    assert all(item["content"]["revision"] == "2" for item in built)  # type: ignore[index]
    assert all(item["content"]["target_access_revocation_generation"] == "0" for item in built)  # type: ignore[index]
    assert {call.kwargs["destination"] for call in compact.await_args_list} == {
        APP_DOMAIN,
        dm_authority,
    }
    assert all(call.kwargs["grant_id"] == row.grant_id for call in compact.await_args_list)
    assert queued.await_count == 2


def test_empty_worker_target_list_is_the_documented_approved_target_wildcard() -> None:
    source = bot_dm_api.__file__
    assert source is not None
    # The runtime assertion itself is covered in target-discovery tests; this
    # regression keeps every runtime surface aligned with the public
    # empty-list-means-every-approved-target contract.
    from app.bots.runtime_control import _runtime_snapshot

    application = SimpleNamespace(
        id=40,
        origin_domain=APP_DOMAIN,
        manifest_generation=1,
        revocation_generation=1,
        status="active",
        target_policy="open",
    )
    worker = SimpleNamespace(
        authority_id=80,
        generation=1,
        revoked_at=None,
        target_domains=[],
    )
    snapshot = _runtime_snapshot(
        application,
        bot_user(),
        [worker],
        "unrelated.example",
        target_allowed=True,
        access_revocation_generation=0,
    )

    assert snapshot.workers[0].target_allowed is True
    worker.target_domains = ["trusted.example"]
    restricted = _runtime_snapshot(
        application,
        bot_user(),
        [worker],
        "unrelated.example",
        target_allowed=True,
        access_revocation_generation=0,
    )
    assert restricted.workers[0].target_allowed is False
