from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api import e2ee as e2ee_api
from app.api.e2ee import (
    encode_base64url,
    proxy_room_e2ee_request,
    queue_device_change_updates,
    room_encryption_control_log,
    validate_remote_room_commit_response,
)
from app.chat.e2ee_membership import (
    GUILD_E2EE_ACCESS_MUTATION_EVENTS,
    e2ee_policy_destinations,
    pause_guild_e2ee_for_membership_change,
    remote_e2ee_authorities_for_user,
)
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.core.types import EntityRef
from app.db.models import Channel
from app.federation.history import _validate_history_message
from app.federation.network import FederationNetworkError
from app.federation.schemas import E2EERoomProxyRequest

OPERATION_ID = "keo_" + "o" * 43
DEVICE_ID = "ked_" + "d" * 43
GROUP_ID = encode_base64url(b"g" * 32)
VAULT_DIGEST = encode_base64url(b"v" * 32)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(domain="alpha.localhost")


def _actor(home: str) -> dict[str, object]:
    return {
        "id": "7",
        "origin_domain": home,
        "username": "remote_user",
        "profile_version": 1,
        "e2ee_device_generation": 3,
    }


def _remote_commit_result(
    kind: str,
) -> tuple[dict[str, object], dict[str, object]]:
    rendered: dict[str, object] = {
        "id": "10",
        "origin_domain": "alpha.localhost",
        "operation_id": OPERATION_ID,
        "operation_status": "committed",
        "encryption_mode": "e2ee",
        "encryption_state": "active",
        "encryption_policy_generation": "2",
        "encryption_protocol": "mls10",
        "encryption_suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        "encryption_group_id": GROUP_ID,
        "encryption_epoch": "1",
        "controls": [
            {
                "id": "101",
                "origin_domain": "alpha.localhost",
                "operation": "welcome",
                "apply": True,
            },
            {
                "id": "102",
                "origin_domain": "alpha.localhost",
                "operation": "commit",
                "apply": False,
            },
        ],
    }
    status: dict[str, object] = {
        "operation_id": OPERATION_ID,
        "kind": kind,
        "status": "committed",
        "prepared": {
            "operation_id": OPERATION_ID,
            "status": "prepared",
            "policy": {
                "mode": "plaintext" if kind == "activate" else "e2ee",
                "state": "proposed" if kind == "activate" else "rekeying",
                "generation": "2",
                "protocol": "mls10",
                "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
                "group_id": GROUP_ID,
                "epoch": None,
            },
            "key_packages": [],
        },
        "committed": rendered,
    }
    return rendered, status


def _control(
    identifier: int,
    *,
    author_domain: str,
    operation: str,
    apply_mode: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        origin_domain="alpha.localhost",
        channel_id=10,
        channel_domain="alpha.localhost",
        author_id=7,
        author_domain=author_domain,
        envelope={"version": 2, "operation": operation, "ciphertext": "AQ"},
        policy_generation=2,
        epoch=1,
        apply_mode=apply_mode,
        room_operation_id=OPERATION_ID,
        room_operation_domain="alpha.localhost",
    )


def _snowflake_at(value: datetime, sequence: int = 0) -> int:
    timestamp = int(value.timestamp() * 1000) - EPOCH_MS
    return (timestamp << (WORKER_BITS + SEQUENCE_BITS)) | sequence


@pytest.mark.asyncio
async def test_room_policy_fans_out_once_to_every_remote_member_home() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=[
            "beta.localhost",
            "gamma.localhost",
            "beta.localhost",
        ]
    )
    guild_channel = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        guild_id=20,
        guild_domain="alpha.localhost",
    )

    destinations = await e2ee_policy_destinations(session, _settings(), guild_channel)

    assert destinations == {"beta.localhost", "gamma.localhost"}


@pytest.mark.asyncio
async def test_remote_device_change_reaches_all_authorities_across_three_homes() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(
        side_effect=[
            ["beta.localhost", "gamma.localhost", "beta.localhost"],
            ["gamma.localhost", "delta.localhost"],
        ]
    )
    user = SimpleNamespace(
        id=7,
        origin_domain="alpha.localhost",
        username="local_user",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        e2ee_device_generation=3,
    )

    authorities = await remote_e2ee_authorities_for_user(session, _settings(), user)

    assert authorities == {
        "beta.localhost",
        "gamma.localhost",
        "delta.localhost",
    }


@pytest.mark.asyncio
async def test_every_guild_access_change_pauses_all_active_encrypted_channels() -> None:
    first = SimpleNamespace(encryption_state="active")
    second = SimpleNamespace(encryption_state="active")
    session = MagicMock()
    session.scalars = AsyncMock(return_value=[first, second])
    guild = SimpleNamespace(id=20, origin_domain="alpha.localhost")

    paused = await pause_guild_e2ee_for_membership_change(session, guild)

    assert paused == [first, second]
    assert first.encryption_state == second.encryption_state == "rekeying"
    assert {
        "guild.member.add",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
    } == GUILD_E2EE_ACCESS_MUTATION_EVENTS


@pytest.mark.parametrize("actor_home", ["beta.localhost", "gamma.localhost"])
@pytest.mark.parametrize("kind", ["activate", "rekey"])
def test_three_home_activation_and_rekey_results_remain_authority_bound(
    actor_home: str,
    kind: str,
) -> None:
    request = E2EERoomProxyRequest.model_validate(
        {
            "channel_id": "10",
            "channel_domain": "alpha.localhost",
            "actor": _actor(actor_home),
            "operation_id": OPERATION_ID,
            "sender_device_id": DEVICE_ID,
            "policy_generation": "2",
            "epoch": "1",
            "group_id": GROUP_ID,
            "commit": encode_base64url(b"commit"),
            "welcome": encode_base64url(b"welcome"),
            "prepared_vault_revision": "3",
            "prepared_vault_digest": VAULT_DIGEST,
            "vault_attested": True,
        }
    )
    rendered, status = _remote_commit_result(kind)
    channel = cast(Channel, SimpleNamespace(id=10, origin_domain="alpha.localhost"))

    validate_remote_room_commit_response(
        rendered,
        status,
        kind=kind,
        operation_id=OPERATION_ID,
        channel=channel,
        policy_generation="2",
        group_id=GROUP_ID,
        authority="alpha.localhost",
    )

    assert request.actor.origin_domain == actor_home
    assert all(
        cast(dict[str, object], item)["origin_domain"] == "alpha.localhost"
        for item in cast(list[object], rendered["controls"])
    )


@pytest.mark.asyncio
async def test_device_change_queues_durable_updates_for_every_affected_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id=7,
        origin_domain="alpha.localhost",
        username="local_user",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        e2ee_device_generation=3,
    )
    guild_channel = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        guild_id=20,
        guild_domain="alpha.localhost",
        encryption_mode="e2ee",
        encryption_state="rekeying",
        encryption_policy_generation=2,
        encryption_protocol="mls10",
        encryption_suite="MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
        encryption_group_id=GROUP_ID,
        encryption_epoch=1,
    )
    dm_channel = SimpleNamespace(
        **{
            **guild_channel.__dict__,
            "id": 11,
            "guild_id": None,
            "guild_domain": None,
        }
    )
    monkeypatch.setattr(
        e2ee_api,
        "remote_e2ee_authorities_for_user",
        AsyncMock(return_value={"beta.localhost", "gamma.localhost"}),
    )
    monkeypatch.setattr(
        e2ee_api,
        "e2ee_policy_destinations",
        AsyncMock(
            side_effect=[
                {"beta.localhost", "gamma.localhost"},
                {"gamma.localhost", "delta.localhost"},
            ]
        ),
    )

    async def envelope(
        _session: object,
        _settings: object,
        event_type: str,
        _actor: object,
        content: dict[str, object],
        *,
        context: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if event_type == "e2ee.room-policy.changed":
            assert context is not None
            assert context["reason"] == "e2ee.device-list.changed"
        return {
            "event_id": f"event-{event_type}-{content.get('channel_id', 'account')}",
            "type": event_type,
        }

    queued: list[tuple[str, str]] = []

    async def queue(
        _session: object,
        _settings: object,
        destination: str,
        payload: dict[str, object],
    ) -> None:
        queued.append((destination, cast(str, payload["event_id"])))

    monkeypatch.setattr(e2ee_api, "build_envelope", envelope)
    monkeypatch.setattr(e2ee_api, "queue_event", queue)

    destinations = await queue_device_change_updates(
        cast(Any, SimpleNamespace()),
        cast(Any, _settings()),
        cast(Any, user),
        cast(list[Channel], [guild_channel, dm_channel]),
    )

    assert destinations == {
        "beta.localhost",
        "gamma.localhost",
        "delta.localhost",
    }
    assert set(queued) == {
        ("beta.localhost", "event-e2ee.device-list.changed-account"),
        ("gamma.localhost", "event-e2ee.device-list.changed-account"),
        ("beta.localhost", "event-e2ee.room-policy.changed-10"),
        ("gamma.localhost", "event-e2ee.room-policy.changed-10"),
        ("gamma.localhost", "event-e2ee.room-policy.changed-11"),
        ("delta.localhost", "event-e2ee.room-policy.changed-11"),
    }


@pytest.mark.asyncio
async def test_remote_room_authority_outage_is_retryable_without_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = httpx.Response(
        200,
        json={"status": "prepared", "operation_id": OPERATION_ID},
        request=httpx.Request("POST", "https://alpha.localhost/_kaede/v1/e2ee"),
    )
    signed_request = AsyncMock(side_effect=[FederationNetworkError("offline"), response])
    monkeypatch.setattr(e2ee_api, "signed_request", signed_request)
    actor = SimpleNamespace(
        id=7,
        origin_domain="beta.localhost",
        username="remote_user",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        e2ee_device_generation=3,
    )
    channel = cast(Channel, SimpleNamespace(id=10, origin_domain="alpha.localhost"))

    with pytest.raises(HTTPException) as unavailable:
        await proxy_room_e2ee_request(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="beta.localhost")),
            "alpha.localhost",
            "/_kaede/v1/e2ee/rooms/propose",
            channel=channel,
            actor=cast(Any, actor),
            body={"operation_id": OPERATION_ID},
        )
    assert unavailable.value.status_code == 503
    assert unavailable.value.detail == {"code": "E2EE_ROOM_AUTHORITY_UNREACHABLE"}

    recovered = await proxy_room_e2ee_request(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="beta.localhost")),
        "alpha.localhost",
        "/_kaede/v1/e2ee/rooms/propose",
        channel=channel,
        actor=cast(Any, actor),
        body={"operation_id": OPERATION_ID},
    )
    assert recovered == {"status": "prepared", "operation_id": OPERATION_ID}
    assert signed_request.await_count == 2


@pytest.mark.asyncio
async def test_three_home_control_catch_up_is_ascending_paginated_and_floor_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = SimpleNamespace(
        guild=SimpleNamespace(id=20, origin_domain="alpha.localhost"),
        channel=SimpleNamespace(
            id=10,
            origin_domain="alpha.localhost",
            created_floor_id=100,
        ),
    )
    load_access = AsyncMock(return_value=access)
    require_permissions = AsyncMock()
    monkeypatch.setattr(e2ee_api, "load_channel_access", load_access)
    monkeypatch.setattr(e2ee_api, "require_permissions", require_permissions)
    first_page = [
        _control(101, author_domain="beta.localhost", operation="welcome", apply_mode="join"),
        _control(102, author_domain="beta.localhost", operation="commit", apply_mode="audit"),
        _control(103, author_domain="gamma.localhost", operation="commit", apply_mode="process"),
    ]
    second_page = [first_page[2]]
    session = MagicMock()
    session.scalars = AsyncMock(side_effect=[first_page, second_page])
    auth = SimpleNamespace(user=SimpleNamespace(id=9, origin_domain="gamma.localhost"))
    configured = SimpleNamespace(domain="beta.localhost")

    first = await room_encryption_control_log(
        EntityRef("10@alpha.localhost"),
        after=None,
        limit=2,
        auth=cast(Any, auth),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=cast(Any, configured),
    )
    second = await room_encryption_control_log(
        EntityRef("10@alpha.localhost"),
        after=EntityRef(cast(str, first["next_after"])),
        limit=2,
        auth=cast(Any, auth),
        session=cast(Any, session),
        redis=cast(Any, SimpleNamespace()),
        settings=cast(Any, configured),
    )

    assert [item["id"] for item in cast(list[dict[str, object]], first["controls"])] == [
        "101",
        "102",
    ]
    assert first["next_after"] == "102@alpha.localhost"
    assert cast(list[dict[str, object]], first["controls"])[1]["apply"] is False
    assert cast(list[dict[str, object]], second["controls"])[0]["author_domain"] == (
        "gamma.localhost"
    )
    assert second["next_after"] is None

    statements = [
        call.args[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        for call in session.scalars.await_args_list
    ]
    assert "e2ee_control_records.id >= 100" in str(statements[0])
    assert (
        "(e2ee_control_records.id, e2ee_control_records.origin_domain) > (102, 'alpha.localhost')"
    ) in str(statements[1])


def test_three_home_encrypted_history_accepts_authority_projected_third_home_author() -> None:
    created_at = datetime.now(UTC)
    message_id = _snowflake_at(created_at, 12)
    raw = {
        "id": str(message_id),
        "origin_domain": "alpha.localhost",
        "channel_id": "10",
        "channel_domain": "alpha.localhost",
        "author_id": "7",
        "author_domain": "gamma.localhost",
        "content": None,
        "e2ee": {
            "version": 2,
            "protocol": "mls10",
            "suite": "MLS_128_DHKEMX25519_AES128GCM_SHA256_Ed25519",
            "group_id": GROUP_ID,
            "policy_generation": "2",
            "epoch": "1",
            "sender_device_id": DEVICE_ID,
            "operation": "create",
            "ciphertext": "AQ",
        },
        "message_type": 0,
        "flags": 0,
        "mention_user_refs": [],
        "attachments": [],
        "reactions": [],
        "pin": None,
        "created_at": created_at.isoformat(),
        "edited_at": None,
        "deleted_at": None,
        "history_author": {
            "id": "7",
            "origin_domain": "gamma.localhost",
            "username": "third_home_member",
            "display_name": None,
            "avatar_hash": None,
            "banner_hash": None,
            "bio": None,
            "custom_status": None,
            "profile_version": 1,
        },
    }

    validated_id, validated = _validate_history_message(
        raw,
        guild_origin="alpha.localhost",
        channel_id=10,
        after=0,
        upper_bound=message_id,
    )

    assert validated_id == message_id
    assert cast(dict[str, object], validated["history_author"])["origin_domain"] == (
        "gamma.localhost"
    )
