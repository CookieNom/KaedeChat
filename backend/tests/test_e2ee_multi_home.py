from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

from app.api import e2ee as e2ee_api
from app.api.e2ee import (
    encode_base64url,
    proxy_room_e2ee_request,
    queue_device_change_updates,
    room_encryption_control_log,
)
from app.chat import e2ee_membership, guild_revision
from app.chat.e2ee_membership import (
    e2ee_policy_destinations,
    pause_guild_e2ee_for_membership_change,
    publish_e2ee_policy_updates,
    remote_e2ee_authorities_for_user,
)
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.core.types import EntityRef
from app.db.models import Channel
from app.federation.history import _validate_history_message
from app.federation.network import FederationNetworkError

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
        account_type="human",
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


@pytest.mark.asyncio
async def test_policy_updates_use_thread_events_for_encrypted_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        guild_id=20,
        guild_domain="alpha.localhost",
        type=12,
    )
    publish = AsyncMock()
    redis = SimpleNamespace()
    session = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(e2ee_membership, "channel_payload", lambda item: {"id": str(item.id)})
    monkeypatch.setattr(e2ee_membership, "publish_dispatch", publish)

    await publish_e2ee_policy_updates(
        cast(Any, session),
        cast(Any, redis),
        cast(Any, _settings()),
        cast(list[Channel], [channel]),
    )

    session.flush.assert_awaited_once_with()
    session.refresh.assert_awaited_once_with(channel, attribute_names=("updated_at",))
    publish.assert_awaited_once()
    assert publish.await_args.args[0] is redis
    assert publish.await_args.args[2:] == ("THREAD_UPDATE", {"id": "10"})


@pytest.mark.parametrize(
    "access_event",
    [
        "guild.member.add",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
    ],
)
@pytest.mark.asyncio
async def test_guild_mutation_returns_paused_policy_channels_to_postcommit_caller(
    monkeypatch: pytest.MonkeyPatch,
    access_event: str,
) -> None:
    thread = SimpleNamespace(id=10, origin_domain="alpha.localhost")
    actor = SimpleNamespace(id=7, origin_domain="alpha.localhost")
    guild = SimpleNamespace(
        id=20,
        origin_domain="alpha.localhost",
        snapshot_generation=1,
        permission_generation=2,
    )
    session = SimpleNamespace(flush=AsyncMock())
    monkeypatch.setattr(
        guild_revision,
        "guild_mutation_signer",
        AsyncMock(return_value=actor),
    )
    monkeypatch.setattr(
        guild_revision,
        "lock_current_guild",
        AsyncMock(return_value=guild),
    )
    pause = AsyncMock(side_effect=[[thread], []])
    monkeypatch.setattr(guild_revision, "pause_guild_e2ee_for_membership_change", pause)
    monkeypatch.setattr(guild_revision, "assign_guild_sequence", AsyncMock(side_effect=[1, 2, 3]))
    monkeypatch.setattr(
        guild_revision,
        "build_guild_authority_envelope",
        AsyncMock(
            side_effect=[
                {"event_id": "one"},
                {"event_id": "two"},
                {"event_id": "three"},
            ]
        ),
    )
    monkeypatch.setattr(guild_revision, "store_guild_event", MagicMock())
    monkeypatch.setattr(guild_revision, "remote_guild_destinations", AsyncMock(return_value=set()))
    monkeypatch.setattr(guild_revision, "remember_guild_delivery_wakes", MagicMock())
    paused: list[Channel] = []

    for event_type in (access_event, access_event):
        await guild_revision.queue_guild_mutation(
            cast(Any, session),
            cast(Any, _settings()),
            cast(Any, guild),
            cast(Any, actor),
            event_type,
            {},
            e2ee_policy_channels=paused,
        )

    assert paused == [thread]
    assert pause.await_count == 2
    assert all(call.args == (session, guild) for call in pause.await_args_list)

    await guild_revision.queue_guild_mutation(
        cast(Any, session),
        cast(Any, _settings()),
        cast(Any, guild),
        cast(Any, actor),
        "guild.member.remove",
        {},
        e2ee_policy_channels=paused,
        pause_e2ee=False,
    )
    assert pause.await_count == 2


@pytest.mark.asyncio
async def test_device_change_queues_durable_updates_for_every_affected_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = SimpleNamespace(
        id=7,
        origin_domain="alpha.localhost",
        account_type="human",
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
        e2ee_membership,
        "e2ee_policy_destinations",
        AsyncMock(
            side_effect=[
                {"beta.localhost", "gamma.localhost"},
                {"gamma.localhost", "delta.localhost"},
            ]
        ),
    )
    compact = AsyncMock()
    monkeypatch.setattr(e2ee_api, "discard_superseded_latest_state_event", compact)
    monkeypatch.setattr(
        e2ee_membership,
        "discard_superseded_latest_state_event",
        compact,
    )
    event_sequence = 0
    built = {}

    async def envelope(
        _session: object,
        _settings: object,
        event_type: str,
        _actor: object,
        content: dict[str, object],
        *,
        context: dict[str, object] | None = None,
        authority_attested_actor: bool = False,
    ) -> dict[str, object]:
        nonlocal event_sequence
        event_sequence += 1
        if event_type == "e2ee.room-policy.changed":
            assert context is not None
            assert context["reason"] == "e2ee.device-list.changed"
            assert not authority_attested_actor
        result = {
            "event_id": (
                f"event-{event_type}-{content.get('channel_id', 'account')}-{event_sequence}"
            ),
            "type": event_type,
        }
        built[result["event_id"]] = (event_type, _actor, content, context)
        return result

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
    monkeypatch.setattr(e2ee_membership, "build_envelope", envelope)
    monkeypatch.setattr(e2ee_membership, "queue_event", queue)

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
    assert [
        (destination, built[event_id][0], built[event_id][2].get("channel_id"))
        for destination, event_id in queued
    ] == [
        ("beta.localhost", "e2ee.device-list.changed", None),
        ("gamma.localhost", "e2ee.device-list.changed", None),
        ("beta.localhost", "e2ee.room-policy.changed", "10"),
        ("gamma.localhost", "e2ee.room-policy.changed", "10"),
        ("delta.localhost", "e2ee.room-policy.changed", "11"),
        ("gamma.localhost", "e2ee.room-policy.changed", "11"),
    ]
    for event_type, actor, content, context in built.values():
        assert actor is user
        if event_type == "e2ee.device-list.changed":
            assert content["profile"]["id"] == "7"
            assert content["profile"]["origin_domain"] == "alpha.localhost"
            assert content["profile"]["e2ee_device_generation"] == 3
            assert context is None
        else:
            channel_id = content["channel_id"]
            assert content["channel_domain"] == "alpha.localhost"
            assert context == {
                "reason": "e2ee.device-list.changed",
                "actor": {"id": "7", "domain": "alpha.localhost"},
                "channel": {"id": channel_id, "domain": "alpha.localhost"},
                "scope": {
                    "type": "guild" if channel_id == "10" else "dm",
                    "id": "20" if channel_id == "10" else "11",
                    "domain": "alpha.localhost",
                },
            }
    assert len(built) == 6
    assert compact.await_count == 6


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
        account_type="human",
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
    postgres_schema,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db.models import E2EEControlRecord

    await postgres_schema.execute(
        text("CREATE TABLE e2ee_control_records AS TABLE public.e2ee_control_records WITH NO DATA")
    )
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
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        for identifier, author, operation, mode in [
            (103, "gamma.localhost", "commit", "process"),
            (99, "beta.localhost", "welcome", "join"),
            (102, "beta.localhost", "commit", "audit"),
            (101, "beta.localhost", "welcome", "join"),
        ]:
            session.add(
                E2EEControlRecord(
                    **vars(
                        _control(
                            identifier, author_domain=author, operation=operation, apply_mode=mode
                        )
                    ),
                    operation=operation,
                )
            )
        for identifier, changed in enumerate(
            [
                {"origin_domain": "foreign.example"},
                {"channel_id": 11},
                {"channel_domain": "foreign.example"},
                {"room_operation_id": None, "room_operation_domain": None},
                {"room_operation_domain": "foreign.example"},
            ],
            start=104,
        ):
            values = vars(
                _control(
                    identifier,
                    author_domain="beta.localhost",
                    operation="commit",
                    apply_mode="audit",
                )
            )
            session.add(E2EEControlRecord(**(values | changed), operation="commit"))
        await session.flush()
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

        assert [item["id"] for item in second["controls"]] == ["103"]
        assert require_permissions.await_count == 2


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
