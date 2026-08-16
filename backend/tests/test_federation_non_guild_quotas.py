from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DMConversation, FederatedDMStorageUsage, Message, User
from app.federation.dm_storage import (
    FederatedDMQuotaExceeded,
    FederatedDMStorageDelta,
    admit_federated_dm_conversation,
    admit_federated_dm_message,
    register_federated_dm_conversation,
)
from app.federation.relationships import (
    RelationshipQuotaExceeded,
    admit_pending_relationship_request,
)

from .test_settings import settings


def test_non_guild_fairness_limits_must_fit_inside_aggregate_budgets() -> None:
    with pytest.raises(ValidationError, match="recipient_origin"):
        settings(
            federation_pending_relationships_per_recipient=100,
            federation_pending_relationships_per_recipient_origin=101,
        )
    with pytest.raises(ValidationError, match="conversations_per_remote_origin"):
        settings(
            federation_dm_max_conversations_per_authority=1_000,
            federation_dm_max_conversations_per_remote_origin=1_000,
        )
    with pytest.raises(ValidationError, match="messages_per_remote_origin"):
        settings(
            federation_dm_max_messages_per_conversation=1_000,
            federation_dm_max_messages_per_authority=5_000,
            federation_dm_max_messages_per_remote_origin=999,
        )
    with pytest.raises(ValidationError, match="bytes_per_remote_origin"):
        settings(
            federation_dm_max_bytes_per_conversation=2 * 1024 * 1024,
            federation_dm_max_bytes_per_authority=10 * 1024 * 1024,
            federation_dm_max_bytes_per_remote_origin=1024 * 1024,
        )


@pytest.mark.asyncio
async def test_one_origin_cannot_fill_a_local_users_entire_friend_request_queue() -> None:
    recipient = User(
        id=42,
        origin_domain="local.example",
        username="local",
        is_local=True,
    )
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, None, None, 50, 100]),
        ),
    )
    configured = settings(
        federation_pending_relationships_per_recipient=1_000,
        federation_pending_relationships_per_recipient_origin=100,
        federation_pending_relationships_per_origin=10_000,
    )

    with pytest.raises(RelationshipQuotaExceeded, match="for this recipient") as rejected:
        await admit_pending_relationship_request(
            session,
            configured,
            recipient,
            actor_id=7,
            actor_domain="malicious.example",
        )

    assert session.scalar.await_count == 5  # type: ignore[attr-defined]
    assert rejected.value.code == "KAED_FED_RELATIONSHIP_REQUEST_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_remote_origin_conversation_cap_reserves_local_authority_capacity() -> None:
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, None, None, 20_000, 10_000]),
        ),
    )
    configured = settings(
        federation_dm_max_conversations_per_authority=100_000,
        federation_dm_max_conversations_per_remote_origin=10_000,
    )

    with pytest.raises(FederatedDMQuotaExceeded) as rejected:
        await admit_federated_dm_conversation(
            session,
            configured,
            authority_domain=configured.domain,
            pair_key=f"42@{configured.domain}:7@malicious.example",
            participant_domains={configured.domain, "malicious.example"},
        )

    assert rejected.value.scope == "remote origin"
    assert rejected.value.limit == 10_000


@pytest.mark.asyncio
async def test_multi_home_group_uses_its_authority_as_the_replica_quota_scope() -> None:
    usage = SimpleNamespace(remote_origin_domain="beta.localhost")
    session = cast(
        AsyncSession,
        SimpleNamespace(
            scalar=AsyncMock(side_effect=[None, None, 0, 0]),
            execute=AsyncMock(),
            get=AsyncMock(return_value=usage),
        ),
    )
    configured = settings(domain="alpha.localhost")

    admitted = await admit_federated_dm_conversation(
        session,
        configured,
        authority_domain="beta.localhost",
        pair_key="a" * 64,
        participant_domains={
            "alpha.localhost",
            "gamma.localhost",
            "delta.localhost",
        },
        conversation_type="group",
    )
    conversation = DMConversation(
        id=99,
        origin_domain="beta.localhost",
        authority_domain="beta.localhost",
        pair_key="a" * 64,
        type="group",
        owner_id=7,
        owner_domain="beta.localhost",
    )
    registered = await register_federated_dm_conversation(
        session,
        configured,
        conversation,
        participant_domains={
            "alpha.localhost",
            "gamma.localhost",
            "delta.localhost",
        },
    )

    assert admitted
    assert registered is usage
    assert session.execute.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_local_group_authority_does_not_charge_an_arbitrary_invited_home() -> None:
    session = cast(
        AsyncSession,
        SimpleNamespace(
            # Authority lock, existing conversation, authority conversation count.
            scalar=AsyncMock(side_effect=[None, None, 0]),
        ),
    )
    configured = settings(domain="alpha.localhost")

    admitted = await admit_federated_dm_conversation(
        session,
        configured,
        authority_domain="alpha.localhost",
        pair_key="b" * 64,
        participant_domains={
            "alpha.localhost",
            "beta.localhost",
            "gamma.localhost",
        },
        conversation_type="group",
    )

    assert admitted
    assert session.scalar.await_count == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_remote_origin_message_cap_is_lower_than_shared_authority_cap() -> None:
    conversation = DMConversation(
        id=99,
        origin_domain="local.example",
        authority_domain="local.example",
        pair_key="a" * 64,
    )
    usage = SimpleNamespace(
        remote_origin_domain="malicious.example",
        message_rows=10,
        total_bytes=20,
    )

    async def get_by_model(model: object, _key: object, **_kwargs: object) -> object | None:
        if model is Message:
            return None
        if model is FederatedDMStorageUsage:
            return usage
        return None

    session = cast(
        AsyncSession,
        SimpleNamespace(
            # The message id is checked both before and after the authority
            # lock so a concurrent replay cannot trigger duplicate pruning.
            get=AsyncMock(side_effect=get_by_model),
            scalar=AsyncMock(side_effect=[None, usage]),
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(one=lambda: (100, 100)),
                    SimpleNamespace(one=lambda: (1_000_000, 100)),
                ]
            ),
        ),
    )
    delta = FederatedDMStorageDelta(1, 1, 0, 0, 1, 1)

    with pytest.raises(FederatedDMQuotaExceeded) as rejected:
        await admit_federated_dm_message(
            session,
            settings(
                federation_dm_max_messages_per_conversation=100,
                federation_dm_replica_cache_messages_per_conversation=100,
                federation_dm_max_messages_per_authority=2_000_000,
                federation_dm_max_messages_per_remote_origin=1_000_000,
            ),
            conversation,
            message_id=101,
            message_domain="malicious.example",
            delta=delta,
        )

    assert rejected.value.scope == "remote origin"
    assert rejected.value.resource == "messages"
