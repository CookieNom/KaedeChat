from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response

import app.api.federation as federation_api
import app.api.moderation as moderation_api
from app.chat.moderation_status import guild_self_moderation_status
from app.chat.payloads import member_payload
from app.core.types import EntityRef
from app.federation.schemas import GuildSelfModerationStatus
from app.federation.security import FederationPrincipal
from app.tasks import purge_remote_member_private_state


def member(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "guild_id": 10,
        "guild_domain": "alpha.localhost",
        "timeout_until": datetime.now(UTC) + timedelta(hours=2),
        "timeout_indefinite": False,
        "timeout_reason": "  repeated\n  spam  ",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_self_projection_sanitizes_reason_and_hides_expired_state() -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    active = guild_self_moderation_status(
        cast(Any, member(timeout_until=now + timedelta(hours=1))),
        now=now,
    )
    assert active.reason == "repeated spam"
    assert active.timed_out is True

    expired = guild_self_moderation_status(
        cast(Any, member(timeout_until=now - timedelta(seconds=1))),
        now=now,
    )
    assert expired.model_dump(mode="json") == {
        "guild_id": "10",
        "guild_domain": "alpha.localhost",
        "timed_out": False,
        "timeout_until": None,
        "timeout_indefinite": False,
        "reason": None,
        "details_available": True,
    }


def test_self_projection_removes_bidi_and_control_spoofing() -> None:
    now = datetime(2026, 8, 12, tzinfo=UTC)
    active = guild_self_moderation_status(
        cast(
            Any,
            member(
                timeout_until=now + timedelta(hours=1),
                timeout_reason="  first\nline\u202esecond\u200b\x00\tend  ",
            ),
        ),
        now=now,
    )
    assert active.reason == "first linesecond end"


def test_self_status_schema_rejects_ambiguous_timeout_modes() -> None:
    with pytest.raises(ValueError, match="exactly one duration mode"):
        GuildSelfModerationStatus(
            guild_id="10",
            guild_domain="alpha.localhost",
            timed_out=True,
            timeout_until=datetime.now(UTC) + timedelta(hours=1),
            timeout_indefinite=True,
        )

    sanitized = GuildSelfModerationStatus(
        guild_id="10",
        guild_domain="alpha.localhost",
        timed_out=True,
        timeout_until=datetime.now(UTC) + timedelta(hours=1),
        reason="safe\u202espoof\u200b",
    )
    assert sanitized.reason == "safespoof"


def test_remote_member_payload_zeros_legacy_private_voice_flags() -> None:
    remote_member = member(
        nickname=None,
        joined_at=datetime.now(UTC),
        timeout_reason="legacy reason",
        voice_flags=7,
        member_version=2,
    )
    user = SimpleNamespace(
        id=42,
        origin_domain="beta.localhost",
        username="alice",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        profile_resolved=True,
        account_type="user",
        updated_at=datetime.now(UTC),
    )
    payload = member_payload(cast(Any, remote_member), cast(Any, user))
    assert payload["voice_flags"] == 0
    assert "timeout_reason" not in payload


def test_federated_timeout_rejection_reason_is_display_safe() -> None:
    assert (
        federation_api.validated_rejection_timeout_reason(
            "  first\nline\u202esecond\u200b\x00\tend  "
        )
        == "first linesecond end"
    )
    with pytest.raises(ValueError, match="timeout reason is invalid"):
        federation_api.validated_rejection_timeout_reason("x" * 513)


@pytest.mark.asyncio
async def test_remote_replica_private_state_purge_is_bounded() -> None:
    legacy = member(timeout_reason="legacy reason", voice_flags=3)
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=[legacy]),
        flush=AsyncMock(),
    )
    purged = await purge_remote_member_private_state(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
        limit=1,
    )
    assert purged == 1
    assert legacy.timeout_reason is None
    assert legacy.voice_flags == 0
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_federation_self_status_is_bound_to_signing_user_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_keys: list[tuple[object, object]] = []

    class Session:
        async def get(self, model: object, key: object) -> object | None:
            requested_keys.append((model, key))
            if key == (10, "alpha.localhost", 42, "beta.localhost"):
                return member()
            return None

    class Redis:
        set = AsyncMock(return_value=True)
        delete = AsyncMock()

    monkeypatch.setattr(
        federation_api,
        "home_guild",
        AsyncMock(return_value=SimpleNamespace(id=10, origin_domain="alpha.localhost")),
    )
    monkeypatch.setattr(
        federation_api,
        "enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    status = await federation_api.federation_self_moderation_status(
        10,
        42,
        FederationPrincipal("beta.localhost", "ed25519:test"),
        cast(Any, Session()),
        cast(Any, Redis()),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )
    assert status["reason"] == "repeated spam"
    assert requested_keys[-1][1] == (10, "alpha.localhost", 42, "beta.localhost")

    with pytest.raises(HTTPException) as rejected:
        await federation_api.federation_self_moderation_status(
            10,
            42,
            FederationPrincipal("gamma.localhost", "ed25519:test"),
            cast(Any, Session()),
            cast(Any, object()),
            cast(Any, SimpleNamespace(domain="alpha.localhost")),
        )
    assert rejected.value.status_code == 404
    assert requested_keys[-1][1] == (10, "alpha.localhost", 42, "gamma.localhost")


@pytest.mark.asyncio
async def test_remote_self_status_capability_fallback_never_copies_legacy_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_member = member(
        guild_domain="beta.localhost",
        timeout_reason="legacy replica secret",
    )

    class Session:
        async def get(self, model: object, key: object) -> object | None:
            name = getattr(model, "__name__", "")
            if name == "Guild":
                return SimpleNamespace(id=10, origin_domain="beta.localhost")
            if name == "GuildMember":
                return active_member
            if name == "Instance":
                return SimpleNamespace(capabilities=[])
            return None

    outbound = AsyncMock()
    queued = AsyncMock(return_value=True)
    redis = SimpleNamespace(set=AsyncMock(return_value=True), delete=AsyncMock())
    monkeypatch.setattr(moderation_api, "signed_request", outbound)
    monkeypatch.setattr(moderation_api, "enqueue_best_effort", queued)
    monkeypatch.setattr(moderation_api, "enforce_client_rate_limit", AsyncMock())
    result = await moderation_api.self_moderation_status(
        EntityRef("10@beta.localhost"),
        Response(),
        cast(
            Any,
            SimpleNamespace(user=SimpleNamespace(id=42, origin_domain="alpha.localhost")),
        ),
        cast(Any, Session()),
        cast(Any, redis),
        cast(Any, SimpleNamespace(domain="alpha.localhost")),
    )
    assert result["timed_out"] is True
    assert result["reason"] is None
    assert result["details_available"] is False
    outbound.assert_not_awaited()
    queued.assert_awaited_once()
    queued_args = queued.await_args.args
    assert queued_args[1:] == ("beta.localhost",)
    assert "42" not in repr(queued_args)
    redis.set.assert_awaited_once_with(
        "federation:self-moderation-capability-refresh:beta.localhost",
        "1",
        ex=60 * 60,
        nx=True,
    )
    redis.delete.assert_not_awaited()
