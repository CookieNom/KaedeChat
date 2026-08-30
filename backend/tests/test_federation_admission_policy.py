from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import federation as federation_api
from app.core.permissions import Permission
from app.db.models import Channel, DMParticipant, Message, PollAnswer, User
from app.federation.schemas import (
    AnnouncementFollowActorRequest,
    ChannelPinsPageProxyRequest,
    DMGroupAuthorizeRequest,
    DMGroupMutationRequest,
    DMOpenFederationRequest,
    E2EEKeyPackageClaimRequest,
    E2EERoomOperationStatusRequest,
    E2EERoomProxyRequest,
    GuildForwardResolveRequest,
    GuildMessageOperationRequest,
    GuildPollVotersProxyRequest,
)
from app.federation.security import FederationPrincipal
from app.federation.typing import TypingRelayRequest

LOCAL_DOMAIN = "authority.example"
REMOTE_DOMAIN = "member.example"
OPERATION_ID = f"keo_{'A' * 43}"
DEVICE_ID = f"ked_{'B' * 43}"


def settings() -> SimpleNamespace:
    return SimpleNamespace(domain=LOCAL_DOMAIN)


def principal(*, silenced: bool = False) -> FederationPrincipal:
    return FederationPrincipal(
        origin=REMOTE_DOMAIN,
        key_id="ed25519:test",
        silenced=silenced,
    )


def remote_profile(user_id: int = 10) -> dict[str, object]:
    return {
        "id": str(user_id),
        "origin_domain": REMOTE_DOMAIN,
        "username": f"remote_{user_id}",
        "profile_version": 1,
    }


def local_profile(user_id: int = 20) -> dict[str, object]:
    return {
        "id": str(user_id),
        "origin_domain": LOCAL_DOMAIN,
        "username": f"local_{user_id}",
        "profile_version": 1,
    }


def dm_open_payload() -> DMOpenFederationRequest:
    return DMOpenFederationRequest.model_validate(
        {"participants": [remote_profile(), local_profile()]}
    )


def remote_user(user_id: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        origin_domain=REMOTE_DOMAIN,
        username=f"remote_{user_id}",
        is_local=False,
        account_type="human",
    )


def local_user(user_id: int = 20) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        origin_domain=LOCAL_DOMAIN,
        username=f"local_{user_id}",
        is_local=True,
        account_type="human",
    )


def room_payload() -> E2EERoomProxyRequest:
    return E2EERoomProxyRequest.model_validate(
        {
            "channel_id": "30",
            "channel_domain": LOCAL_DOMAIN,
            "actor": remote_profile(),
            "operation_id": OPERATION_ID,
            "sender_device_id": DEVICE_ID,
        }
    )


def key_package_payload() -> E2EEKeyPackageClaimRequest:
    return E2EEKeyPackageClaimRequest.model_validate(
        {
            "operation_id": OPERATION_ID,
            "operation_domain": REMOTE_DOMAIN,
            "channel_id": "30",
            "channel_domain": REMOTE_DOMAIN,
            "claimant_id": "10",
            "claimant_domain": REMOTE_DOMAIN,
            "target_id": "20",
            "target_domain": LOCAL_DOMAIN,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "bucket"),
    [
        (federation_api.federation_dm_open, "dm-open"),
        (federation_api.federation_dm_authorize, "dm-authorize"),
    ],
)
async def test_dm_route_limit_precedes_remote_profile_upserts(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    bucket: str,
) -> None:
    rejected = HTTPException(status_code=429, detail={"code": "RATE_LIMITED"})
    rate_limit = AsyncMock(side_effect=rejected)
    upsert = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)

    with pytest.raises(HTTPException) as caught:
        await handler(
            dm_open_payload(),
            principal(),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    rate_limit.assert_awaited_once_with(
        cast(Any, SimpleNamespace()),
        REMOTE_DOMAIN,
        bucket,
        capacity=120,
        refill_per_minute=120,
    )
    upsert.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_lookup_route_limit_precedes_database_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = HTTPException(status_code=429, detail={"code": "RATE_LIMITED"})
    rate_limit = AsyncMock(side_effect=rejected)
    session = SimpleNamespace(scalar=AsyncMock())
    redis = SimpleNamespace()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_user_lookup(
            f"maple@{LOCAL_DOMAIN}",
            principal(silenced=True),
            cast(Any, session),
            cast(Any, redis),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    rate_limit.assert_awaited_once_with(
        redis,
        REMOTE_DOMAIN,
        "user-lookup",
        capacity=120,
        refill_per_minute=120,
    )
    session.scalar.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [federation_api.federation_dm_open, federation_api.federation_dm_authorize],
)
async def test_dm_restricted_sender_is_rejected_before_capability_privacy_or_authority_work(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> None:
    calls: list[str] = []
    sender = remote_user()
    recipient = local_user()
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )

    async def rate_limit(*_args: object, **_kwargs: object) -> None:
        calls.append("rate-limit")

    async def upsert(_session: object, _settings: object, profile: object) -> object:
        origin_domain = cast(Any, profile).origin_domain
        calls.append(f"upsert:{origin_domain}")
        return sender if origin_domain == REMOTE_DOMAIN else recipient

    async def reject(_session: object, user: object) -> None:
        calls.append(f"restriction:{cast(Any, user).origin_domain}")
        raise rejected

    bot_capability = AsyncMock()
    privacy = AsyncMock()
    authority = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", rate_limit)
    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", reject)
    monkeypatch.setattr(federation_api, "_authorize_bot_dm_open_capability", bot_capability)
    monkeypatch.setattr(federation_api, "require_can_direct_message", privacy)
    monkeypatch.setattr(federation_api, "authoritative_dm_conversation", authority)
    session = SimpleNamespace(commit=AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await handler(
            dm_open_payload(),
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    assert calls == [
        "rate-limit",
        f"upsert:{REMOTE_DOMAIN}",
        f"upsert:{LOCAL_DOMAIN}",
        f"restriction:{REMOTE_DOMAIN}",
    ]
    bot_capability.assert_not_awaited()
    privacy.assert_not_awaited()
    authority.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_dm_authorize_checks_friendship_before_inviter_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inviter = remote_user()
    invitee = local_user()
    payload = DMGroupAuthorizeRequest.model_validate(
        {"inviter": remote_profile(), "invitee": local_profile()}
    )
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )

    async def upsert(_session: object, _settings: object, profile: object) -> object:
        return inviter if cast(Any, profile).origin_domain == REMOTE_DOMAIN else invitee

    calls: list[str] = []

    async def friendship(*_args: object) -> None:
        calls.append("friendship")

    async def reject(*_args: object) -> None:
        calls.append("admission")
        raise rejected

    session = SimpleNamespace(commit=AsyncMock())
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        reject,
    )
    monkeypatch.setattr(federation_api, "require_group_invite_friend", friendship)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_group_dm_authorize(
            payload,
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    assert calls == ["friendship", "admission"]
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_dm_add_authorizes_actor_and_invite_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    target = local_user()
    conversation = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN)
    calls: list[str] = []
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )

    async def upsert(_session: object, _settings: object, profile: object) -> object:
        return actor if cast(Any, profile).origin_domain == REMOTE_DOMAIN else target

    async def member(*_args: object) -> None:
        calls.append("membership")

    async def authorize(*_args: object) -> None:
        calls.append("invite")

    async def admission(_session: object, user: object) -> None:
        calls.append(f"admission:{cast(Any, user).origin_domain}")
        raise rejected

    load_group = AsyncMock(return_value=(conversation, channel))
    apply_mutation = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)
    monkeypatch.setattr(federation_api, "load_authoritative_group", load_group)
    monkeypatch.setattr(federation_api, "require_group_member", member)
    monkeypatch.setattr(federation_api, "authorize_group_invitee_at_home", authorize)
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)
    monkeypatch.setattr(federation_api, "apply_authoritative_group_mutation", apply_mutation)
    payload = DMGroupMutationRequest.model_validate(
        {
            "action": "add",
            "conversation_id": "30",
            "conversation_domain": LOCAL_DOMAIN,
            "actor": remote_profile(),
            "target": local_profile(),
        }
    )

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_group_dm_mutate(
            payload,
            principal(),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    assert calls == ["membership", "invite", f"admission:{REMOTE_DOMAIN}"]
    load_group.assert_awaited_once()
    assert load_group.await_args.kwargs == {}
    apply_mutation.assert_not_awaited()


@pytest.mark.asyncio
async def test_forward_resolution_is_readable_without_mutation_admission_and_rechecks_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    destination_channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
        type=0,
    )
    source_channel = SimpleNamespace(
        id=21,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
        type=0,
    )
    destination = SimpleNamespace(
        id=30,
        origin_domain=LOCAL_DOMAIN,
        channel_id=destination_channel.id,
        channel_domain=destination_channel.origin_domain,
        deleted_at=None,
        forwarded_message_id=40,
        forwarded_message_domain=LOCAL_DOMAIN,
    )
    source = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        channel_id=source_channel.id,
        channel_domain=source_channel.origin_domain,
        deleted_at=None,
        e2ee=None,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        values = {
            (Channel, (destination_channel.id, LOCAL_DOMAIN)): destination_channel,
            (Channel, (source_channel.id, LOCAL_DOMAIN)): source_channel,
            (Message, (destination.id, LOCAL_DOMAIN)): destination,
            (Message, (source.id, LOCAL_DOMAIN)): source,
        }
        return values.get((model, key))

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[None, None]),
    )
    restriction = AsyncMock()
    permissions = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", restriction)
    monkeypatch.setattr(federation_api, "require_permissions", permissions)
    monkeypatch.setattr(
        federation_api,
        "render_message_payload",
        AsyncMock(return_value={"id": str(source.id)}),
    )
    payload = GuildForwardResolveRequest.model_validate(
        {
            "actor": remote_profile(),
            "channel_id": str(destination_channel.id),
            "message_id": f"{destination.id}@{LOCAL_DOMAIN}",
        }
    )

    result = await federation_api.federation_guild_forward_resolve(
        cast(Any, guild.id),
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == {
        "id": str(source.id),
        "source_channel_ref": f"{source_channel.id}@{LOCAL_DOMAIN}",
    }
    restriction.assert_not_awaited()
    assert permissions.await_count == 2
    for permission_call in permissions.await_args_list:
        assert permission_call.args[4] == (
            Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
        )


@pytest.mark.asyncio
async def test_poll_voter_listing_is_readable_without_mutation_admission_and_rechecks_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
        type=0,
    )
    message = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        deleted_at=None,
    )
    poll = SimpleNamespace(message_id=message.id, message_domain=message.origin_domain)
    answer = SimpleNamespace(answer_id=1)

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel:
            return channel
        if model is Message:
            return message
        if model is PollAnswer:
            return answer
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=poll),
        scalars=AsyncMock(return_value=[]),
    )
    restriction = AsyncMock()
    permissions = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", restriction)
    monkeypatch.setattr(federation_api, "require_permissions", permissions)
    payload = GuildPollVotersProxyRequest.model_validate(
        {
            "actor": remote_profile(),
            "channel_id": str(channel.id),
            "message_id": f"{message.id}@{LOCAL_DOMAIN}",
            "answer_id": 1,
        }
    )

    result = await federation_api.federation_guild_poll_voters_proxy(
        cast(Any, guild.id),
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == {"users": [], "next_after": None}
    restriction.assert_not_awaited()
    permissions.assert_awaited_once_with(
        session,
        cast(Any, SimpleNamespace()),
        guild,
        actor,
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY,
        channel=channel,
    )


@pytest.mark.asyncio
async def test_pins_reader_is_readable_without_mutation_admission_and_delegates_access_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
    )
    session = SimpleNamespace(get=AsyncMock(return_value=channel))
    restriction = AsyncMock()
    pins = AsyncMock(return_value={"items": [], "next_before": None})
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", restriction)
    monkeypatch.setattr(federation_api, "list_channel_pins", pins)
    payload = ChannelPinsPageProxyRequest.model_validate(
        {"actor": remote_profile(), "channel_id": str(channel.id)}
    )

    result = await federation_api.federation_guild_pins_page(
        cast(Any, guild.id),
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == {"items": [], "next_before": None}
    restriction.assert_not_awaited()
    pins.assert_awaited_once()
    assert pins.await_args.args[0] == f"{channel.id}@{channel.origin_domain}"
    assert pins.await_args.args[3].user is actor


@pytest.mark.asyncio
async def test_announcement_follow_list_actor_is_readable_and_still_delegates_access_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    session = SimpleNamespace()
    restriction = AsyncMock()
    list_follows = AsyncMock(return_value=[])
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", restriction)
    monkeypatch.setattr(federation_api, "list_announcement_follows", list_follows)
    payload = AnnouncementFollowActorRequest.model_validate({"actor": remote_profile()})

    result = await federation_api.federation_list_announcement_follows(
        cast(Any, 20),
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == []
    restriction.assert_not_awaited()
    list_follows.assert_awaited_once()
    assert list_follows.await_args.args[1].user is actor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "needs_snowflake"),
    [
        (federation_api.federation_e2ee_room_propose, False),
        (federation_api.federation_e2ee_room_activate, True),
        (federation_api.federation_e2ee_room_rekey_propose, False),
        (federation_api.federation_e2ee_room_rekey_activate, True),
    ],
)
async def test_e2ee_room_mutators_all_use_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    needs_snowflake: bool,
) -> None:
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    actor_resolution = AsyncMock(side_effect=rejected)
    monkeypatch.setattr(federation_api, "enforce_e2ee_room_proxy_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "federated_e2ee_actor", actor_resolution)
    arguments: list[object] = [
        room_payload(),
        principal(),
        SimpleNamespace(),
        SimpleNamespace(),
    ]
    if needs_snowflake:
        arguments.append(SimpleNamespace())
    arguments.append(settings())

    with pytest.raises(HTTPException) as caught:
        await handler(*arguments)

    assert caught.value is rejected
    actor_resolution.assert_awaited_once()
    assert actor_resolution.await_args.kwargs.get("require_mutation_admission", True) is True


@pytest.mark.asyncio
async def test_e2ee_room_mutation_rejects_restricted_actor_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    channel = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN, guild_id=None)
    session = SimpleNamespace(get=AsyncMock(return_value=channel))
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    calls: list[str] = []

    async def authorize(*_args: object) -> None:
        calls.append("authorize")

    async def admission_check(*_args: object) -> None:
        calls.append("admission")
        raise rejected

    admission = AsyncMock(side_effect=admission_check)
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        federation_api,
        "load_channel_access",
        AsyncMock(return_value=SimpleNamespace(channel=channel, guild=None)),
    )
    monkeypatch.setattr(federation_api, "require_room_policy_authority", authorize)
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federated_e2ee_actor(
            room_payload(),
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    assert calls == ["authorize", "admission"]
    admission.assert_awaited_once_with(session, actor)


@pytest.mark.asyncio
async def test_e2ee_operation_status_remains_readable_for_restricted_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    channel = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN, guild_id=None)
    session = SimpleNamespace(get=AsyncMock(return_value=channel))
    admission = AsyncMock()
    status = AsyncMock(return_value={"status": "prepared"})
    monkeypatch.setattr(federation_api, "enforce_e2ee_room_proxy_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)
    monkeypatch.setattr(federation_api, "room_encryption_operation_status_for_actor", status)
    payload = E2EERoomOperationStatusRequest.model_validate(
        {
            "channel_id": "30",
            "channel_domain": LOCAL_DOMAIN,
            "actor": remote_profile(),
            "operation_id": OPERATION_ID,
        }
    )

    result = await federation_api.federation_e2ee_room_operation_status(
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == {"status": "prepared"}
    admission.assert_not_awaited()
    status.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2ee_key_package_claim_authorizes_before_admission_and_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=None)
    target = local_user()
    claimant = remote_user()

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel:
            return channel
        if model is User and key == (target.id, target.origin_domain):
            return target
        if model is User and key == (claimant.id, claimant.origin_domain):
            return claimant
        if model is DMParticipant:
            return SimpleNamespace()
        raise AssertionError(f"unexpected lookup after claimant admission: {model!r} {key!r}")

    session = SimpleNamespace(get=AsyncMock(side_effect=get), commit=AsyncMock())
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    human_claim = AsyncMock()
    bot_claim = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        AsyncMock(side_effect=rejected),
    )
    monkeypatch.setattr(federation_api, "claim_local_room_key_packages", human_claim)
    monkeypatch.setattr(federation_api, "claim_local_bot_room_key_packages", bot_claim)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_e2ee_key_packages_claim(
            key_package_payload(),
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    human_claim.assert_not_awaited()
    bot_claim.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_e2ee_key_package_claim_denies_nonparticipant_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=None)
    target = local_user()
    claimant = remote_user()
    participant_lookups = 0

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        nonlocal participant_lookups
        if model is Channel:
            return channel
        if model is User and key == (target.id, target.origin_domain):
            return target
        if model is User and key == (claimant.id, claimant.origin_domain):
            return claimant
        if model is DMParticipant:
            participant_lookups += 1
            return SimpleNamespace() if participant_lookups == 1 else None
        return None

    admission = AsyncMock()
    human_claim = AsyncMock()
    session = SimpleNamespace(get=AsyncMock(side_effect=get), commit=AsyncMock())
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)
    monkeypatch.setattr(federation_api, "claim_local_room_key_packages", human_claim)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_e2ee_key_packages_claim(
            key_package_payload(),
            principal(),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "KAED_E2EE_TARGET_NOT_FOUND"}
    admission.assert_not_awaited()
    human_claim.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_silenced_peer_can_use_dm_e2ee_room_context_but_not_guild_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    upsert = AsyncMock(return_value=actor)
    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", AsyncMock())
    dm_channel = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN, guild_id=None)
    dm_auth = await federation_api.federated_e2ee_actor(
        room_payload(),
        principal(silenced=True),
        cast(Any, SimpleNamespace(get=AsyncMock(return_value=dm_channel))),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
        require_mutation_admission=False,
    )
    assert dm_auth.user is actor

    guild_channel = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN, guild_id=60)
    with pytest.raises(HTTPException) as caught:
        await federation_api.federated_e2ee_actor(
            room_payload(),
            principal(silenced=True),
            cast(Any, SimpleNamespace(get=AsyncMock(return_value=guild_channel))),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
            require_mutation_admission=False,
        )

    assert caught.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}
    assert upsert.await_count == 1


@pytest.mark.asyncio
async def test_silenced_peer_can_claim_dm_key_package_but_not_guild_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = local_user()
    claimant = remote_user()
    dm_channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=None)

    async def dm_get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel:
            return dm_channel
        if model is User and key == (target.id, target.origin_domain):
            return target
        if model is User and key == (claimant.id, claimant.origin_domain):
            return claimant
        if model is DMParticipant:
            return SimpleNamespace()
        return None

    dm_session = SimpleNamespace(get=AsyncMock(side_effect=dm_get), commit=AsyncMock())
    claimed_packages = [{"device_id": DEVICE_ID}]
    claim = AsyncMock(return_value=claimed_packages)
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", AsyncMock())
    monkeypatch.setattr(federation_api, "claim_local_room_key_packages", claim)

    result = await federation_api.federation_e2ee_key_packages_claim(
        key_package_payload(),
        principal(silenced=True),
        cast(Any, dm_session),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )

    assert result == {"key_packages": claimed_packages}
    claim.assert_awaited_once()
    dm_session.commit.assert_awaited_once()

    guild_channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=60)
    guild_session = SimpleNamespace(
        get=AsyncMock(return_value=guild_channel),
        commit=AsyncMock(),
    )
    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_e2ee_key_packages_claim(
            key_package_payload(),
            principal(silenced=True),
            cast(Any, guild_session),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}
    assert claim.await_count == 1
    guild_session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_silenced_typing_relay_allows_dm_but_rejects_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = TypingRelayRequest.model_validate(
        {
            "channel_id": "30",
            "channel_domain": REMOTE_DOMAIN,
            "user_id": "10",
            "user_domain": REMOTE_DOMAIN,
            "observed_at": 1_000_000,
            "expires_at": 10,
            "audience_user_refs": [f"20@{LOCAL_DOMAIN}"],
            "batch_index": 0,
            "batch_count": 1,
        }
    )
    accept = AsyncMock(return_value=True)
    publish = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "typing_projection_is_fresh", lambda _payload: True)
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", AsyncMock())
    monkeypatch.setattr(federation_api, "accept_typing_generation", accept)
    monkeypatch.setattr(federation_api, "publish_local_typing", publish)

    dm_channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=None)
    monkeypatch.setattr(
        federation_api,
        "validate_typing_relay_scope",
        AsyncMock(return_value=(dm_channel, remote_user(), {f"20@{LOCAL_DOMAIN}"})),
    )
    response = await federation_api.federation_typing_relay(
        payload,
        principal(silenced=True),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )
    assert response.status_code == 204
    accept.assert_awaited_once()
    publish.assert_awaited_once()

    accept.reset_mock()
    publish.reset_mock()
    guild_channel = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN, guild_id=60)
    monkeypatch.setattr(
        federation_api,
        "validate_typing_relay_scope",
        AsyncMock(return_value=(guild_channel, remote_user(), {f"20@{LOCAL_DOMAIN}"})),
    )
    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_typing_relay(
            payload,
            principal(silenced=True),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value.detail == {"code": "KAED_FED_INSTANCE_SILENCED"}
    accept.assert_not_awaited()
    publish.assert_not_awaited()


def message_operation_payload(
    operation: str,
    **extra: object,
) -> GuildMessageOperationRequest:
    body: dict[str, object] = {
        "operation": operation,
        "actor": remote_profile(),
        "channel_id": "20",
        "message_id": f"30@{LOCAL_DOMAIN}",
    }
    body.update(extra)
    return GuildMessageOperationRequest.model_validate(body)


async def call_guild_message_operation(
    payload: GuildMessageOperationRequest,
    *,
    monkeypatch: pytest.MonkeyPatch,
    delete_author_id: int | None = None,
) -> tuple[dict[str, object], AsyncMock, AsyncMock]:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
    )
    delete_target = SimpleNamespace(
        id=30,
        origin_domain=LOCAL_DOMAIN,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=delete_author_id,
        author_domain=REMOTE_DOMAIN,
    )

    async def get(model: object, _key: object, **_kwargs: object) -> object | None:
        if model is Channel:
            return channel
        if model is Message:
            return delete_target
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    admission = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)
    monkeypatch.setattr(federation_api, "delete_message", delete)
    result = await federation_api.federation_guild_message_operation(
        cast(Any, guild.id),
        payload,
        principal(),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, settings()),
    )
    return result, admission, delete


@pytest.mark.asyncio
async def test_restricted_actor_can_delete_own_guild_message_for_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = message_operation_payload("message.delete")
    result, admission, delete = await call_guild_message_operation(
        payload,
        monkeypatch=monkeypatch,
        delete_author_id=10,
    )

    assert result == {"deleted": True}
    admission.assert_not_awaited()
    delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_deleting_another_users_guild_message_requires_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
    )
    delete_target = SimpleNamespace(
        id=30,
        origin_domain=LOCAL_DOMAIN,
        channel_id=channel.id,
        channel_domain=channel.origin_domain,
        author_id=999,
        author_domain=REMOTE_DOMAIN,
    )

    async def get(model: object, _key: object, **_kwargs: object) -> object | None:
        return channel if model is Channel else delete_target

    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    delete = AsyncMock()
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(
        federation_api,
        "require_remote_user_creation_allowed",
        AsyncMock(side_effect=rejected),
    )
    monkeypatch.setattr(federation_api, "delete_message", delete)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_guild_message_operation(
            cast(Any, guild.id),
            message_operation_payload("message.delete"),
            principal(),
            cast(Any, SimpleNamespace(get=AsyncMock(side_effect=get))),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        message_operation_payload("message.edit", edit={"content": "updated"}),
        GuildMessageOperationRequest.model_validate(
            {
                "operation": "message.bulk_delete",
                "actor": remote_profile(),
                "channel_id": "20",
                "message_ids": [f"30@{LOCAL_DOMAIN}", f"31@{LOCAL_DOMAIN}"],
            }
        ),
        message_operation_payload(
            "reaction.remove_user",
            emoji="👍",
            target_user_id=f"20@{LOCAL_DOMAIN}",
        ),
        message_operation_payload("reaction.clear"),
        message_operation_payload("announcement.crosspost"),
    ],
    ids=lambda payload: payload.operation,
)
async def test_all_non_cleanup_guild_message_operations_require_mutation_admission(
    monkeypatch: pytest.MonkeyPatch,
    payload: GuildMessageOperationRequest,
) -> None:
    actor = remote_user()
    guild = SimpleNamespace(id=60, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        unavailable=False,
    )
    rejected = HTTPException(
        status_code=403,
        detail={"code": "USER_SUSPENDED_FROM_INSTANCE"},
    )
    admission = AsyncMock(side_effect=rejected)
    monkeypatch.setattr(federation_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(federation_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(federation_api, "home_guild", AsyncMock(return_value=guild))
    monkeypatch.setattr(federation_api, "require_remote_user_creation_allowed", admission)

    with pytest.raises(HTTPException) as caught:
        await federation_api.federation_guild_message_operation(
            cast(Any, guild.id),
            payload,
            principal(),
            cast(Any, SimpleNamespace(get=AsyncMock(return_value=channel))),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, settings()),
        )

    assert caught.value is rejected
    admission.assert_awaited_once()
