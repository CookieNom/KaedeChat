from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.chat.schemas import ProfilePatch
from app.core.dm import dm_pair_key
from app.db.models import Relationship, User
from app.federation.delivery import (
    MAX_QUEUE_EVENTS,
    FederationOutboxCapacityExceeded,
    dm_open_failure_target,
    enforce_queue_limits,
    expire_stale_outbox,
    reconcile_relationship_capacity_rejection,
)
from app.federation.identity_storage import FederationIdentityQuotaExceeded
from app.federation.network import FederationInstanceQuotaExceeded, FederationNetworkError
from app.federation.relationships import (
    acceptance_matches,
    apply_relationship_event,
    queue_profile_updates,
    validated_guild_profile_source,
)
from app.federation.schemas import EventEnvelope, RelationshipEventContent, RemoteUserProfile
from app.federation.users import (
    PROFILE_BY_REF_CAPABILITY,
    discover_profile_by_ref_capability,
    refresh_remote_user_by_ref,
    resolve_handle,
    split_handle,
    unresolved_profile_peer_candidates,
)

from .test_settings import settings


def test_federated_handle_accepts_display_and_wire_forms() -> None:
    assert split_handle("turtle@example.test") == ("turtle", "example.test")
    assert split_handle("@Turtle@Example.Test") == ("turtle", "example.test")


def test_profile_patch_trims_text_and_allows_explicit_clearing() -> None:
    patch = ProfilePatch(
        display_name="  Maple  ",
        bio="  A quiet profile.  ",
        custom_status="   ",
    )
    assert patch.display_name == "Maple"
    assert patch.bio == "A quiet profile."
    assert patch.custom_status is None


def test_profile_patch_requires_a_field_and_bounds_public_text() -> None:
    with pytest.raises(ValidationError):
        ProfilePatch()
    with pytest.raises(ValidationError):
        ProfilePatch(custom_status="x" * 129)
    with pytest.raises(ValidationError):
        ProfilePatch(bio="x\x00y")


def test_federated_profile_carries_all_mutable_versioned_fields() -> None:
    profile = RemoteUserProfile(
        id="42",
        origin_domain="remote.example",
        username="maple",
        display_name="Maple",
        avatar_hash="avatar",
        banner_hash="banner",
        bio="About Maple",
        custom_status="Out walking",
        profile_version=7,
    )
    assert profile.profile_version == 7
    assert profile.bio == "About Maple"
    assert profile.custom_status == "Out walking"


@pytest.mark.asyncio
async def test_exact_profile_refresh_accepts_only_home_signed_composite_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    placeholder = User(
        id=42,
        origin_domain="remote.example",
        username="history_deadbeef",
        is_local=False,
        profile_resolved=False,
        federation_introduced_by_domain="guild.example",
    )
    resolved = User(
        id=42,
        origin_domain="remote.example",
        username="maple",
        display_name="Maple",
        is_local=False,
        profile_resolved=True,
        federation_introduced_by_domain="guild.example",
    )
    instance = SimpleNamespace(capabilities=[PROFILE_BY_REF_CAPABILITY])
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[placeholder, instance]),
        scalar=AsyncMock(return_value=placeholder),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    response = httpx.Response(
        200,
        json={"signed": True},
        request=httpx.Request("GET", "https://remote.example/_kaede/v1/users/profile"),
    )
    envelope = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_abcdefghijklmnop",
            "origin": "remote.example",
            "type": "user.profile",
            "ts": 1,
            "actor": {"id": "42", "domain": "remote.example"},
            "context": {},
            "content": {
                "subject": {"id": "42", "origin_domain": "remote.example"},
                "profile": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "maple",
                    "display_name": "Maple",
                    "profile_version": 2,
                },
            },
            "signatures": {"remote.example": {"ed25519:key": "c2ln"}},
        }
    )
    monkeypatch.setattr("app.federation.users.signed_request", AsyncMock(return_value=response))
    monkeypatch.setattr(
        "app.federation.users.validated_event_envelope", AsyncMock(return_value=envelope)
    )
    monkeypatch.setattr("app.federation.users.upsert_remote_user", AsyncMock(return_value=resolved))
    monkeypatch.setattr(
        "app.federation.users._profile_refresh_topics",
        AsyncMock(return_value=["guild:guild.example:7"]),
    )
    published = AsyncMock()
    monkeypatch.setattr("app.federation.users.publish_dispatch", published)

    result = await refresh_remote_user_by_ref(
        cast(Any, session), local_settings, cast(Any, redis), 42, "remote.example"
    )

    assert result is resolved
    assert "federation:user-ref-refresh:rate:target:remote.example:" in str(
        redis.eval.await_args.args[2]
    )
    assert "federation:user-lookup:rate:target:" not in str(redis.eval.await_args.args[2])
    published.assert_awaited_once()
    assert published.await_args.args[2] == "USER_UPDATE"
    assert published.await_args.args[3]["username"] == "maple"


@pytest.mark.asyncio
async def test_exact_profile_refresh_rate_limit_rotates_the_placeholder() -> None:
    placeholder = User(
        id=42,
        origin_domain="remote.example",
        username="history_deadbeef",
        is_local=False,
        profile_resolved=False,
        federation_introduced_by_domain="guild.example",
    )
    original_updated_at = placeholder.updated_at
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[placeholder, SimpleNamespace(capabilities=[PROFILE_BY_REF_CAPABILITY])]
        ),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(eval=AsyncMock(return_value=121))

    result = await refresh_remote_user_by_ref(
        cast(Any, session), settings(), cast(Any, redis), 42, "remote.example"
    )

    assert result is None
    assert placeholder.updated_at != original_updated_at
    session.commit.assert_awaited_once()
    assert "federation:user-ref-refresh:rate:target:remote.example:" in str(
        redis.eval.await_args.args[2]
    )


@pytest.mark.asyncio
async def test_exact_profile_refresh_backs_off_when_authoritative_handle_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    placeholder = User(
        id=42,
        origin_domain="remote.example",
        username="history_deadbeef",
        is_local=False,
        profile_resolved=False,
        federation_introduced_by_domain="guild.example",
    )
    instance = SimpleNamespace(capabilities=[PROFILE_BY_REF_CAPABILITY])
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[placeholder, instance]),
        scalar=AsyncMock(return_value=placeholder),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    response = httpx.Response(
        200,
        json={"signed": True},
        request=httpx.Request("GET", "https://remote.example/_kaede/v1/users/profile"),
    )
    envelope = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_abcdefghijklmnop",
            "origin": "remote.example",
            "type": "user.profile",
            "ts": 1,
            "actor": {"id": "42", "domain": "remote.example"},
            "context": {},
            "content": {
                "subject": {"id": "42", "origin_domain": "remote.example"},
                "profile": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "already_owned",
                    "profile_version": 2,
                },
            },
            "signatures": {"remote.example": {"ed25519:key": "c2ln"}},
        }
    )
    monkeypatch.setattr("app.federation.users.signed_request", AsyncMock(return_value=response))
    monkeypatch.setattr(
        "app.federation.users.validated_event_envelope", AsyncMock(return_value=envelope)
    )
    monkeypatch.setattr(
        "app.federation.users.upsert_remote_user", AsyncMock(return_value=placeholder)
    )
    topics = AsyncMock()
    monkeypatch.setattr("app.federation.users._profile_refresh_topics", topics)
    published = AsyncMock()
    monkeypatch.setattr("app.federation.users.publish_dispatch", published)

    result = await refresh_remote_user_by_ref(
        cast(Any, session), local_settings, cast(Any, redis), 42, "remote.example"
    )

    assert result is None
    assert not placeholder.profile_resolved
    assert placeholder.updated_at is not None
    session.commit.assert_awaited_once()
    topics.assert_not_awaited()
    published.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_refresh_rejects_a_signed_proof_for_another_composite_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = User(
        id=42,
        origin_domain="remote.example",
        username="history_deadbeef",
        is_local=False,
        profile_resolved=False,
        federation_introduced_by_domain="guild.example",
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[placeholder, SimpleNamespace(capabilities=[PROFILE_BY_REF_CAPABILITY])]
        ),
        commit=AsyncMock(),
    )
    redis = SimpleNamespace(eval=AsyncMock(return_value=1))
    response = httpx.Response(200, json={}, request=httpx.Request("GET", "https://remote.example"))
    wrong = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_abcdefghijklmnop",
            "origin": "remote.example",
            "type": "user.profile",
            "ts": 1,
            "actor": {"id": "43", "domain": "remote.example"},
            "content": {},
            "signatures": {"remote.example": {"ed25519:key": "c2ln"}},
        }
    )
    monkeypatch.setattr("app.federation.users.signed_request", AsyncMock(return_value=response))
    monkeypatch.setattr(
        "app.federation.users.validated_event_envelope", AsyncMock(return_value=wrong)
    )

    with pytest.raises(Exception, match="proof is invalid"):
        await refresh_remote_user_by_ref(
            cast(Any, session), settings(), cast(Any, redis), 42, "remote.example"
        )

    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_unresolved_legacy_peer_is_selected_for_bounded_discovery() -> None:
    session = SimpleNamespace(scalars=AsyncMock(return_value=["legacy.example"]))
    assert await unresolved_profile_peer_candidates(cast(Any, session), settings()) == [
        "legacy.example"
    ]
    statement = session.scalars.await_args.args[0]
    assert "profile_resolved" in str(statement)
    assert "capabilities" in str(statement)


@pytest.mark.asyncio
async def test_legacy_peer_discovery_unlocks_exact_profile_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    cached = SimpleNamespace(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    session = SimpleNamespace(get=AsyncMock(return_value=cached))
    discovered = SimpleNamespace(capabilities=[PROFILE_BY_REF_CAPABILITY])
    ensure = AsyncMock(return_value=discovered)
    monkeypatch.setattr("app.federation.users.ensure_peer", ensure)

    assert await discover_profile_by_ref_capability(
        cast(Any, session), local_settings, "legacy.example"
    )
    ensure.assert_awaited_once_with(
        session,
        local_settings,
        "legacy.example",
        force=True,
    )
    assert cached.updated_at > datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_failed_legacy_peer_discovery_rotates_without_failing_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = SimpleNamespace(updated_at=datetime(2026, 1, 1, tzinfo=UTC))
    session = SimpleNamespace(get=AsyncMock(return_value=cached))
    monkeypatch.setattr(
        "app.federation.users.ensure_peer",
        AsyncMock(side_effect=FederationNetworkError("offline")),
    )

    assert not await discover_profile_by_ref_capability(
        cast(Any, session), settings(domain="local.example"), "offline.example"
    )
    assert cached.updated_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_relationship_event_requires_bounded_correlation_token() -> None:
    with pytest.raises(ValidationError):
        RelationshipEventContent.model_validate(
            {
                "actor": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "maple",
                },
                "target": {"id": "7", "domain": "local.example"},
                "request_id": "guessable",
            }
        )


def test_stale_acceptance_cannot_resurrect_local_relationship_state() -> None:
    request_id = "kcr_abcdefghijklmnopqrstuvwxyz"
    assert acceptance_matches("pending_out", request_id, request_id)
    assert not acceptance_matches(None, None, request_id)
    assert not acceptance_matches("blocked", None, request_id)
    assert not acceptance_matches("pending_out", request_id, f"{request_id}2")


def test_identity_quota_uses_stable_private_and_federation_codes() -> None:
    rejected = FederationIdentityQuotaExceeded("remote identities", 100, 100)
    assert rejected.detail() == {"code": "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED"}
    assert rejected.detail(federation=True) == {"code": "KAED_FED_IDENTITY_STORAGE_QUOTA_EXCEEDED"}
    assert "used" not in rejected.detail()
    assert "limit" not in rejected.detail()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            FederationIdentityQuotaExceeded("remote identities", 100, 100),
            "FEDERATION_IDENTITY_STORAGE_QUOTA_EXCEEDED",
        ),
        (
            FederationInstanceQuotaExceeded(100, 100),
            "FEDERATION_INSTANCE_STORAGE_QUOTA_EXCEEDED",
        ),
    ],
)
async def test_remote_lookup_reports_local_capacity_as_507(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    expected_code: str,
) -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    redis = SimpleNamespace(
        exists=AsyncMock(return_value=False),
        eval=AsyncMock(return_value=[1, 1]),
    )
    response = httpx.Response(
        200,
        json={
            "id": "42",
            "origin_domain": "remote.example",
            "username": "maple",
        },
        request=httpx.Request("GET", "https://remote.example/_kaede/v1/users/lookup"),
    )
    if isinstance(failure, FederationInstanceQuotaExceeded):
        monkeypatch.setattr(
            "app.federation.users.signed_request",
            AsyncMock(side_effect=failure),
        )
    else:
        monkeypatch.setattr(
            "app.federation.users.signed_request",
            AsyncMock(return_value=response),
        )
        monkeypatch.setattr(
            "app.federation.users.upsert_remote_user",
            AsyncMock(side_effect=failure),
        )

    with pytest.raises(HTTPException) as caught:
        await resolve_handle(
            cast(Any, session),
            settings(),
            cast(Any, redis),
            "local.example:7",
            "maple@remote.example",
        )

    assert caught.value.status_code == 507
    assert caught.value.detail == {"code": expected_code}


@pytest.mark.asyncio
async def test_terminal_relationship_capacity_rejection_removes_only_matching_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    actor = User(id=7, origin_domain="local.example", username="local", is_local=True)
    target = User(id=42, origin_domain="remote.example", username="maple", is_local=False)
    pending = Relationship(
        user_id=actor.id,
        user_domain=actor.origin_domain,
        user_is_local=True,
        target_id=target.id,
        target_domain=target.origin_domain,
        type="pending_out",
        request_id="kcr_abcdefghijklmnopqrstuvwxyz",
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[actor, target]),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.federation.delivery.lock_relationship_pair", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.federation.delivery.relationship", AsyncMock(return_value=pending))
    event = SimpleNamespace(
        event_type="relationship.request",
        envelope={
            "actor": {"id": "7", "domain": "local.example"},
            "content": {
                "actor": {
                    "id": "7",
                    "origin_domain": "local.example",
                    "username": "local",
                },
                "target": {"id": "42", "domain": "remote.example"},
                "request_id": "kcr_abcdefghijklmnopqrstuvwxyz",
            },
        },
    )

    reconciled = await reconcile_relationship_capacity_rejection(
        cast(Any, session),
        local_settings,
        cast(Any, event),
        "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED",
    )

    assert reconciled == (actor, target)
    session.delete.assert_awaited_once_with(pending)


@pytest.mark.asyncio
async def test_late_relationship_capacity_rejection_preserves_newer_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    actor = User(id=7, origin_domain="local.example", username="local", is_local=True)
    target = User(id=42, origin_domain="remote.example", username="maple", is_local=False)
    newer = Relationship(
        user_id=actor.id,
        user_domain=actor.origin_domain,
        user_is_local=True,
        target_id=target.id,
        target_domain=target.origin_domain,
        type="pending_out",
        request_id="kcr_newerabcdefghijklmnopqrst",
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[actor, target]),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.federation.delivery.lock_relationship_pair", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.federation.delivery.relationship", AsyncMock(return_value=newer))
    event = SimpleNamespace(
        event_type="relationship.request",
        envelope={
            "actor": {"id": "7", "domain": "local.example"},
            "content": {
                "actor": {
                    "id": "7",
                    "origin_domain": "local.example",
                    "username": "local",
                },
                "target": {"id": "42", "domain": "remote.example"},
                "request_id": "kcr_abcdefghijklmnopqrstuvwxyz",
            },
        },
    )

    reconciled = await reconcile_relationship_capacity_rejection(
        cast(Any, session),
        local_settings,
        cast(Any, event),
        "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED",
    )

    assert reconciled is None
    session.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbox_capacity_uses_stable_private_error_without_queue_depth() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=MAX_QUEUE_EVENTS))

    with pytest.raises(FederationOutboxCapacityExceeded) as caught:
        await enforce_queue_limits(cast(Any, session), "remote.example")

    assert caught.value.detail() == {"code": "FEDERATION_OUTBOX_CAPACITY_EXCEEDED"}
    assert caught.value.detail(federation=True) == {"code": "KAED_FED_OUTBOX_CAPACITY_EXCEEDED"}


def test_expired_dm_open_targets_only_the_valid_local_initiator() -> None:
    local_settings = settings(domain="local.example")
    pair_key = dm_pair_key("local@local.example", "maple@remote.example")
    event = SimpleNamespace(
        event_type="dm.open.request",
        envelope={
            "actor": {"id": "7", "domain": "local.example"},
            "content": {
                "participants": [
                    {
                        "id": "7",
                        "origin_domain": "local.example",
                        "username": "local",
                    },
                    {
                        "id": "42",
                        "origin_domain": "remote.example",
                        "username": "maple",
                    },
                ],
                "pair_key": pair_key,
            },
        },
    )

    assert dm_open_failure_target(
        local_settings,
        cast(Any, event),
        "KAED_FED_DELIVERY_EXPIRED",
    ) == (
        7,
        "local.example",
        {
            "pair_key": pair_key,
            "code": "KAED_FED_DELIVERY_EXPIRED",
        },
    )
    event.envelope["actor"]["domain"] = "remote.example"
    assert (
        dm_open_failure_target(
            local_settings,
            cast(Any, event),
            "KAED_FED_DELIVERY_EXPIRED",
        )
        is None
    )


@pytest.mark.asyncio
async def test_stale_outbox_resolves_exact_relationship_and_dm_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_settings = settings(domain="local.example")
    pair_key = dm_pair_key("local@local.example", "maple@remote.example")
    actor = User(id=7, origin_domain="local.example", username="local", is_local=True)
    target = User(id=42, origin_domain="remote.example", username="maple", is_local=False)
    pending = Relationship(
        user_id=actor.id,
        user_domain=actor.origin_domain,
        user_is_local=True,
        target_id=target.id,
        target_domain=target.origin_domain,
        type="pending_out",
        request_id="kcr_abcdefghijklmnopqrstuvwxyz",
    )
    relationship_event = SimpleNamespace(
        origin_domain="local.example",
        event_id="relationship-event",
        event_type="relationship.request",
        envelope={
            "actor": {"id": "7", "domain": "local.example"},
            "content": {
                "actor": {
                    "id": "7",
                    "origin_domain": "local.example",
                    "username": "local",
                },
                "target": {"id": "42", "domain": "remote.example"},
                "request_id": "kcr_abcdefghijklmnopqrstuvwxyz",
            },
        },
    )
    dm_event = SimpleNamespace(
        origin_domain="local.example",
        event_id="dm-event",
        event_type="dm.open.request",
        envelope={
            "actor": {"id": "7", "domain": "local.example"},
            "content": {
                "participants": [
                    {
                        "id": "7",
                        "origin_domain": "local.example",
                        "username": "local",
                    },
                    {
                        "id": "42",
                        "origin_domain": "remote.example",
                        "username": "maple",
                    },
                ],
                "pair_key": pair_key,
            },
        },
    )
    relationship_row = SimpleNamespace(
        destination="remote.example",
        event_origin_domain="local.example",
        event_id="relationship-event",
        status="retry",
        last_error=None,
    )
    dm_row = SimpleNamespace(
        destination="remote.example",
        event_origin_domain="local.example",
        event_id="dm-event",
        status="retry",
        last_error=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(
            side_effect=[
                ["remote.example"],
                [relationship_row, dm_row],
                [relationship_event, dm_event],
            ]
        ),
        get=AsyncMock(side_effect=[actor, target]),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.federation.delivery.lock_relationship_pair", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.federation.delivery.relationship", AsyncMock(return_value=pending))
    publish = AsyncMock()
    monkeypatch.setattr("app.federation.delivery.publish_dispatch", publish)

    assert (
        await expire_stale_outbox(
            cast(Any, session),
            local_settings,
            cast(Any, SimpleNamespace()),
        )
        == 2
    )

    assert relationship_row.status == "expired"
    assert dm_row.status == "expired"
    assert relationship_row.last_error == "KAED_FED_DELIVERY_EXPIRED"
    session.delete.assert_awaited_once_with(pending)
    assert [call.args[2] for call in publish.await_args_list] == [
        "USER_UPDATE",
        "DM_OPEN_REJECTED",
    ]


@pytest.mark.asyncio
async def test_profile_update_is_ignored_after_friendship_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recipient = SimpleNamespace(id=7, origin_domain="local.example", is_local=True)
    actor = SimpleNamespace(id=42, origin_domain="remote.example", is_local=False)

    class FakeSession:
        async def get(self, _model: object, identity: object) -> object | None:
            return recipient if identity == (7, "local.example") else actor

    monkeypatch.setattr(
        "app.federation.relationships.lock_relationship_pair", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.federation.relationships.relationship", AsyncMock(return_value=None))
    upsert = AsyncMock()
    monkeypatch.setattr("app.federation.relationships.upsert_remote_user", upsert)
    envelope = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_abcdefghijklmnop",
            "origin": "remote.example",
            "type": "relationship.profile",
            "ts": 1,
            "actor": {"id": "42", "domain": "remote.example"},
            "content": {
                "actor": {
                    "id": "42",
                    "origin_domain": "remote.example",
                    "username": "maple",
                    "avatar_hash": "a" * 64,
                    "profile_version": 2,
                },
                "target": {"id": "7", "domain": "local.example"},
            },
            "signatures": {"remote.example": {"ed25519:test": "signature"}},
        }
    )

    result = await apply_relationship_event(
        cast(Any, FakeSession()),
        cast(Any, SimpleNamespace(domain="local.example")),
        envelope,
    )

    assert result.relation_type is None
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_updates_are_queued_for_remote_friends_and_guild_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Rows:
        def all(self) -> list[tuple[int, str]]:
            return [(42, "remote.example"), (43, "remote.example")]

    class GuildRows:
        def all(self) -> list[tuple[int, str]]:
            return [(70, "guild.example")]

    class EmptyMembershipRows:
        def all(self) -> list[tuple[object, object]]:
            return []

    class FakeSession:
        calls = 0

        async def execute(self, _statement: object) -> Rows | GuildRows | EmptyMembershipRows:
            self.calls += 1
            if self.calls == 1:
                return Rows()
            if self.calls == 2:
                return GuildRows()
            return EmptyMembershipRows()

    actor = SimpleNamespace(
        id=7,
        origin_domain="local.example",
        account_type="human",
        username="maple",
        display_name="Maple",
        avatar_hash="a" * 64,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=3,
    )
    build = AsyncMock(side_effect=lambda *_args, **_kwargs: {"event_id": "test"})
    queue = AsyncMock()
    monkeypatch.setattr("app.federation.relationships.build_envelope", build)
    monkeypatch.setattr("app.federation.relationships.queue_event", queue)

    destinations = await queue_profile_updates(
        cast(Any, FakeSession()),
        cast(Any, SimpleNamespace(domain="local.example")),
        cast(Any, actor),
    )

    assert destinations == {"remote.example", "guild.example"}
    assert build.await_count == 3
    assert queue.await_count == 3
    assert build.await_args_list[-1].args[2] == "guild.member.profile"
    assert build.await_args_list[-1].kwargs["context"] == {
        "guild_id": "70",
        "guild_domain": "guild.example",
    }


@pytest.mark.asyncio
async def test_local_guild_profile_update_is_direct_home_event_and_gateway_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(
        id=7,
        origin_domain="local.example",
        is_local=True,
        account_type="human",
        username="maple",
        display_name="Maple",
        avatar_hash=None,
        banner_hash=None,
        bio="about",
        custom_status="working",
        profile_version=4,
        e2ee_device_generation=2,
    )
    guild = SimpleNamespace(id=70, origin_domain="local.example", unavailable=False)
    member = SimpleNamespace()

    class Rows:
        def __init__(self, values: list[object]) -> None:
            self.values = values

        def all(self) -> list[object]:
            return self.values

    session = SimpleNamespace(
        execute=AsyncMock(
            side_effect=[
                Rows([]),
                Rows([]),
                Rows([(guild, member)]),
            ]
        ),
        scalars=AsyncMock(side_effect=[["member-home.example"], [5, 9]]),
    )
    source = {"event_id": "kcfe_profile_source"}
    build = AsyncMock(return_value=source)
    queue = AsyncMock()
    postcommit = Mock()
    monkeypatch.setattr("app.federation.relationships.build_envelope", build)
    monkeypatch.setattr("app.federation.relationships.queue_event", queue)
    monkeypatch.setattr(
        "app.federation.relationships.member_payload", Mock(return_value={"ok": True})
    )
    monkeypatch.setattr("app.federation.relationships.queue_postcommit_dispatch", postcommit)

    local_settings = SimpleNamespace(domain="local.example")
    destinations = await queue_profile_updates(
        cast(Any, session),
        cast(Any, local_settings),
        cast(Any, actor),
    )

    assert destinations == {"member-home.example"}
    queue.assert_awaited_once_with(
        session,
        local_settings,
        "member-home.example",
        source,
        discover_destination=False,
    )
    postcommit.assert_called_once_with(
        session,
        "guild:local.example:70",
        "GUILD_MEMBER_UPDATE",
        {"ok": True},
    )


@pytest.mark.asyncio
async def test_guild_profile_relay_preserves_exact_user_home_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_source = {
        "event_id": "kcfe_abcdefghijklmnop",
        "origin": "member.example",
        "type": "guild.member.profile",
        "ts": 1,
        "actor": {"id": "42", "domain": "member.example"},
        "context": {"guild_id": "70", "guild_domain": "guild.example"},
        "content": {
            "actor": {
                "id": "42",
                "origin_domain": "member.example",
                "username": "maple",
                "profile_version": 3,
            }
        },
        "signatures": {"member.example": {"ed25519:test": "signature"}},
    }
    verified = EventEnvelope.model_validate(raw_source)
    validator = AsyncMock(return_value=verified)
    monkeypatch.setattr("app.federation.relationships.validated_event_envelope", validator)

    profile = await validated_guild_profile_source(
        cast(Any, object()),
        cast(Any, SimpleNamespace()),
        raw_source,
        guild_ref=(70, "guild.example"),
    )

    assert (profile.id, profile.origin_domain, profile.profile_version) == (
        "42",
        "member.example",
        3,
    )
    assert validator.await_args.args[2] == "member.example"

    validator.return_value = EventEnvelope.model_validate(
        {
            **raw_source,
            "content": {
                "actor": {
                    "id": "43",
                    "origin_domain": "member.example",
                    "username": "maple",
                    "profile_version": 3,
                }
            },
        }
    )
    with pytest.raises(ValueError, match="actor"):
        await validated_guild_profile_source(
            cast(Any, object()),
            cast(Any, SimpleNamespace()),
            raw_source,
            guild_ref=(70, "guild.example"),
        )
