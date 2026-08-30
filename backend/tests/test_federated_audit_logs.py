from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.moderation as moderation_api
from app.chat.audit_payloads import AuditLogEntryPayload
from app.core.federation import FEDERATION_CAPABILITIES
from app.core.permissions import Permission
from app.core.types import EntityRef
from app.db.models import Guild, GuildMember, User
from app.federation.client import silence_blocks_path
from app.federation.security import FederationPrincipal


def audit_request(
    *,
    request_id: str = "kalr_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    requester_id: str = "9",
    issued_at: int = 1_000,
    deadline: int = 1_015,
) -> moderation_api.AuditLogFederationRequest:
    return moderation_api.AuditLogFederationRequest(
        guild_id="10",
        guild_domain="beta.example",
        requester={"id": requester_id, "domain": "alpha.example"},
        requesting_instance="alpha.example",
        request_id=request_id,
        issued_at=issued_at,
        deadline=deadline,
        query={
            "limit": 25,
            "before": "900",
            "user": {"id": "11", "domain": "gamma.example"},
            "action_type": 25,
            "target_type": "instance",
        },
    )


def audit_entry(
    entry_id: str = "800",
    *,
    actor_id: str = "11",
    action_type: int = 25,
    target_type: str = "instance",
) -> AuditLogEntryPayload:
    return AuditLogEntryPayload(
        id=entry_id,
        guild_id="10",
        guild_domain="beta.example",
        actor_id=actor_id,
        actor_domain="gamma.example",
        action_type=action_type,
        target_type=target_type,
        target_ref={"domain": "blocked.example"},
        reason=None,
        changes=[],
        created_at=datetime(2026, 8, 27, tzinfo=UTC),
    )


def test_federated_audit_entry_rejects_ambiguous_action_type() -> None:
    payload = audit_entry("1")
    with pytest.raises(ValidationError):
        AuditLogEntryPayload.model_validate({**payload.model_dump(), "action_type": True})


def test_audit_log_federation_surface_is_advertised_and_silence_blocked() -> None:
    assert moderation_api.AUDIT_LOG_FEDERATION_CAPABILITY in FEDERATION_CAPABILITIES
    assert silence_blocks_path("/_kaede/v1/guilds/10/audit-logs")


@pytest.mark.asyncio
async def test_remote_human_route_requires_replica_membership_then_uses_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain="beta.example", unavailable=False)
    member = SimpleNamespace(user_id=9, user_domain="alpha.example")
    requester = SimpleNamespace(id=9, origin_domain="alpha.example")

    async def get(model: object, key: object) -> object | None:
        if model is Guild:
            assert key == (10, "beta.example")
            return guild
        if model is GuildMember:
            assert key == (10, "beta.example", 9, "alpha.example")
            return member
        raise AssertionError(f"unexpected model lookup: {model!r}")

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    remote = AsyncMock(return_value=[])
    local_permissions = AsyncMock()
    settings = SimpleNamespace(domain="alpha.example", federation_clock_skew_seconds=30)
    monkeypatch.setattr(moderation_api, "request_remote_audit_log_page", remote)
    monkeypatch.setattr(moderation_api, "require_permissions", local_permissions)

    result = await moderation_api.list_audit_logs(
        EntityRef("10@beta.example"),
        25,
        900,
        None,
        EntityRef("11@gamma.example"),
        25,
        "instance",
        SimpleNamespace(user=requester),
        session,
        SimpleNamespace(),
        settings,
    )

    assert result == []
    local_permissions.assert_not_awaited()
    remote.assert_awaited_once_with(
        session,
        settings,
        guild,
        requester,
        limit=25,
        before=900,
        after=None,
        actor_ref=(11, "gamma.example"),
        action_type=25,
        target_type="instance",
    )


@pytest.mark.asyncio
async def test_authority_rechecks_live_requester_permission_and_consumes_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(
        id=10,
        origin_domain="beta.example",
        owner_id=2,
        owner_domain="beta.example",
        unavailable=False,
    )
    requester = SimpleNamespace(id=9, origin_domain="alpha.example")
    owner = SimpleNamespace(id=2, origin_domain="beta.example")

    async def get(model: object, key: object) -> object | None:
        if model is Guild:
            return guild
        if model is User and key == (9, "alpha.example"):
            return requester
        if model is User and key == (2, "beta.example"):
            return owner
        raise AssertionError(f"unexpected model lookup: {model!r}, {key!r}")

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    redis = SimpleNamespace(set=AsyncMock(return_value=True))
    permission_check = AsyncMock()
    query = AsyncMock(return_value=[])
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(moderation_api.time, "time", lambda: 1_000)
    monkeypatch.setattr(moderation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(moderation_api, "require_permissions", permission_check)
    monkeypatch.setattr(moderation_api, "query_audit_log_entries", query)
    monkeypatch.setattr(moderation_api, "guild_authority_owner", AsyncMock(return_value=owner))
    monkeypatch.setattr(moderation_api, "build_guild_authority_envelope", signed)
    payload = audit_request()

    result = await moderation_api.federation_guild_audit_logs(
        10,
        payload,
        FederationPrincipal(origin="alpha.example", key_id="ed25519:test"),
        session,
        redis,
        SimpleNamespace(domain="beta.example", federation_clock_skew_seconds=30),
    )

    assert result == {"signed": True}
    redis.set.assert_awaited_once_with(
        "federation:audit-log-request:alpha.example:kalr_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "1",
        ex=45,
        nx=True,
    )
    permission_check.assert_awaited_once_with(
        session,
        redis,
        guild,
        requester,
        Permission.VIEW_AUDIT_LOG,
    )
    query.assert_awaited_once_with(
        session,
        guild,
        limit=25,
        before=900,
        after=None,
        actor_ref=(11, "gamma.example"),
        action_type=25,
        target_type="instance",
    )
    signed.assert_awaited_once()
    assert signed.await_args.args[5]["request"]["request_id"] == payload.request_id


@pytest.mark.asyncio
async def test_authority_rejects_requester_substitution_and_application_nonce_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_limit = AsyncMock()
    monkeypatch.setattr(moderation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(moderation_api.time, "time", lambda: 1_000)
    settings = SimpleNamespace(domain="beta.example", federation_clock_skew_seconds=30)
    principal = FederationPrincipal(origin="alpha.example", key_id="ed25519:test")

    substituted = audit_request().model_copy(update={"requesting_instance": "gamma.example"})
    with pytest.raises(HTTPException) as mismatch:
        await moderation_api.federation_guild_audit_logs(
            10,
            substituted,
            principal,
            SimpleNamespace(),
            SimpleNamespace(),
            settings,
        )
    assert mismatch.value.status_code == 403
    assert mismatch.value.detail == {"code": "KAED_FED_AUDIT_LOG_REQUESTER_MISMATCH"}

    replay_redis = SimpleNamespace(set=AsyncMock(return_value=False))
    with pytest.raises(HTTPException) as replayed:
        await moderation_api.federation_guild_audit_logs(
            10,
            audit_request(),
            principal,
            SimpleNamespace(),
            replay_redis,
            settings,
        )
    assert replayed.value.status_code == 409
    assert replayed.value.detail == {"code": "KAED_FED_AUDIT_LOG_REQUEST_REPLAYED"}


def test_signed_page_validation_rejects_request_filter_and_order_substitution() -> None:
    request = audit_request()
    wrong_request = request.model_copy(
        update={"request_id": "kalr_BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"}
    )
    with pytest.raises(ValueError, match="different request"):
        moderation_api.validate_audit_log_federation_page(
            moderation_api.AuditLogFederationPage(request=wrong_request, entries=[]),
            request,
        )

    with pytest.raises(ValueError, match="action filter"):
        moderation_api.validate_audit_log_federation_page(
            moderation_api.AuditLogFederationPage(
                request=request,
                entries=[audit_entry(action_type=26)],
            ),
            request,
        )

    with pytest.raises(ValueError, match="ordering"):
        moderation_api.validate_audit_log_federation_page(
            moderation_api.AuditLogFederationPage(
                request=request,
                entries=[audit_entry("800"), audit_entry("850")],
            ),
            request,
        )


@pytest.mark.asyncio
async def test_remote_page_accepts_only_bounded_home_signed_exact_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def signed_request(*args: object, **kwargs: object) -> httpx.Response:
        del args
        captured.update(kwargs)
        return httpx.Response(200, content=b"{}")

    async def validated_envelope(
        session: object,
        settings: object,
        expected_origin: str,
        raw: object,
    ) -> object:
        del session, settings, raw
        assert expected_origin == "beta.example"
        request_body = captured["payload"]
        assert isinstance(request_body, dict)
        return SimpleNamespace(
            type=moderation_api.AUDIT_LOG_FEDERATION_EVENT_TYPE,
            ts=int(request_body["issued_at"]) * 1_000,
            context={"guild_id": "10", "guild_domain": "beta.example"},
            content={"request": request_body, "entries": [audit_entry().model_dump(mode="json")]},
        )

    monkeypatch.setattr(moderation_api.time, "time", lambda: 1_000)
    monkeypatch.setattr(moderation_api, "signed_request", signed_request)
    monkeypatch.setattr(moderation_api, "decode_federation_response_json", lambda *a, **k: {})
    monkeypatch.setattr(moderation_api, "validated_event_envelope", validated_envelope)

    result = await moderation_api.request_remote_audit_log_page(
        SimpleNamespace(),
        SimpleNamespace(domain="alpha.example", federation_clock_skew_seconds=30),
        SimpleNamespace(id=10, origin_domain="beta.example"),
        SimpleNamespace(id=9, origin_domain="alpha.example"),
        limit=25,
        before=900,
        after=None,
        actor_ref=(11, "gamma.example"),
        action_type=25,
        target_type="instance",
    )

    assert [entry.id for entry in result] == ["800"]
    assert captured["max_response_bytes"] == moderation_api.AUDIT_LOG_FEDERATION_MAX_RESPONSE_BYTES
    assert captured["request_timeout"] == moderation_api.AUDIT_LOG_FEDERATION_DEADLINE_SECONDS
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["requester"] == {"id": "9", "domain": "alpha.example"}
    assert payload["query"] == {
        "limit": 25,
        "before": "900",
        "after": None,
        "user": {"id": "11", "domain": "gamma.example"},
        "action_type": 25,
        "target_type": "instance",
    }


@pytest.mark.asyncio
async def test_remote_page_fails_closed_when_home_signature_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(moderation_api.time, "time", lambda: 1_000)
    monkeypatch.setattr(
        moderation_api,
        "signed_request",
        AsyncMock(return_value=httpx.Response(200, content=b"{}")),
    )
    monkeypatch.setattr(moderation_api, "decode_federation_response_json", lambda *a, **k: {})
    monkeypatch.setattr(
        moderation_api,
        "validated_event_envelope",
        AsyncMock(side_effect=ValueError("invalid signed event envelope")),
    )

    with pytest.raises(HTTPException) as invalid:
        await moderation_api.request_remote_audit_log_page(
            SimpleNamespace(),
            SimpleNamespace(domain="alpha.example", federation_clock_skew_seconds=30),
            SimpleNamespace(id=10, origin_domain="beta.example"),
            SimpleNamespace(id=9, origin_domain="alpha.example"),
            limit=25,
            before=None,
            after=None,
            actor_ref=None,
            action_type=None,
            target_type=None,
        )
    assert invalid.value.status_code == 502
    assert invalid.value.detail["code"] == "FEDERATED_AUDIT_LOG_RESPONSE_INVALID"
