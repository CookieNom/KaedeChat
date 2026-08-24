from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import threads as threads_api
from app.api.channels import require_thread_message_delete_state
from app.api.dependencies import AuthenticatedUser
from app.api.threads import (
    FORUM_FLAG_REQUIRE_TAG,
    THREAD_FLAG_PINNED,
    GuildThreadProxyRequest,
    ThreadCreate,
    ThreadUpdate,
    _decode_thread_cursor,
    _encode_thread_cursor,
    _thread_type,
    bot_remove_thread_member,
    update_thread,
    validate_applied_tags,
)
from app.auth.tokens import AccessGrant
from app.chat.guild_revision import federation_channel_state, guild_mutation_signer
from app.chat.payloads import channel_payload, thread_source_starter_payload
from app.chat.schemas import ForumTag
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import User
from app.federation.security import FederationPrincipal


def settings(domain: str = "home.example") -> Settings:
    return Settings(
        domain=domain,
        environment="test",
        secret_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
        database_url="postgresql+asyncpg://test:test@postgres/test",
        dragonfly_url="redis://dragonfly:6379/0",
        media_s3_access_key="GK00000000000000000000000000000000",
        media_s3_secret_key="0" * 64,
    )


def channel(**overrides: object) -> SimpleNamespace:
    created_at = datetime(2026, 8, 24, 12, tzinfo=UTC)
    values: dict[str, object] = {
        "id": 10,
        "origin_domain": "guild.example",
        "guild_id": 1,
        "guild_domain": "guild.example",
        "type": 11,
        "created_at": created_at,
        "updated_at": created_at,
        "name": "post",
        "topic": None,
        "position": 0,
        "parent_id": 2,
        "parent_domain": "guild.example",
        "permissions_synced": False,
        "rate_limit_per_user": 0,
        "flags": 0,
        "owner_id": 3,
        "owner_domain": "guild.example",
        "archived": False,
        "locked": False,
        "invitable": None,
        "auto_archive_duration": 1440,
        "archive_timestamp": created_at,
        "last_activity_at": created_at,
        "message_count": 0,
        "total_message_sent": 0,
        "member_count": 1,
        "starter_message_id": 10,
        "starter_message_domain": "guild.example",
        "default_auto_archive_duration": None,
        "default_thread_rate_limit_per_user": None,
        "available_tags": [],
        "applied_tag_ids": [],
        "default_reaction_emoji": None,
        "default_sort_order": None,
        "default_forum_layout": None,
        "e2ee_required": False,
        "federated_history_policy": "inherit",
        "encryption_mode": "plaintext",
        "encryption_state": "plaintext",
        "encryption_policy_generation": 0,
        "encryption_protocol": None,
        "encryption_suite": None,
        "encryption_group_id": None,
        "encryption_epoch": None,
        "encryption_activated_at": None,
        "last_message_id": None,
        "last_message_domain": None,
        "last_thread_id": None,
        "last_thread_domain": None,
        "created_floor_id": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_thread_parent_type_defaults_and_invariants() -> None:
    assert _thread_type(channel(type=0), None) == 12
    assert _thread_type(channel(type=0), 11) == 11
    assert _thread_type(channel(type=0), 12) == 12
    assert _thread_type(channel(type=5), None) == 10
    assert _thread_type(channel(type=15), None) == 11
    with pytest.raises(HTTPException, match="TEXT_THREAD_TYPE_INVALID"):
        _thread_type(channel(type=0), 10)
    with pytest.raises(HTTPException, match="ANNOUNCEMENT_THREAD_TYPE_REQUIRED"):
        _thread_type(channel(type=5), 11)
    with pytest.raises(HTTPException, match="FORUM_PUBLIC_THREADS_ONLY"):
        _thread_type(channel(type=15), 12)


def test_thread_create_and_patch_keep_discord_compatible_aliases() -> None:
    empty = ThreadCreate(name="private without starter")
    assert empty.starter() is None
    attachment_only = ThreadCreate(
        name="forum post",
        attachment_ids=["42"],
        applied_tags=["7"],
    )
    assert attachment_only.starter() is not None
    assert attachment_only.starter().content is None
    assert [int(item) for item in attachment_only.applied_tag_ids] == [7]

    patched = ThreadUpdate(applied_tags=["7"], flags=THREAD_FLAG_PINNED)
    assert [int(item) for item in patched.applied_tag_ids or []] == [7]
    assert patched.flags == THREAD_FLAG_PINNED
    with pytest.raises(ValidationError, match="pinned and flags disagree"):
        ThreadUpdate(flags=THREAD_FLAG_PINNED, pinned=False)


def test_forum_require_tag_is_creation_only_and_names_follow_wire_limit() -> None:
    parent = channel(
        type=15,
        flags=FORUM_FLAG_REQUIRE_TAG,
        available_tags=[{"id": "7", "name": "", "moderated": False}],
    )
    with pytest.raises(HTTPException, match="FORUM_TAG_REQUIRED"):
        validate_applied_tags(parent, [])
    validate_applied_tags(parent, [], require_tag=False)
    validate_applied_tags(parent, [7])
    assert ForumTag(name="").name == ""
    with pytest.raises(ValidationError):
        ForumTag(name="x" * 21)


def test_thread_cursor_round_trip_fences_sort_and_archive_modes() -> None:
    created_at = datetime(2026, 8, 24, 12, 30, tzinfo=UTC)
    thread = channel(id=99, flags=THREAD_FLAG_PINNED)
    encoded = _encode_thread_cursor(thread, created_at, archived=False, sort_order=0)
    assert _decode_thread_cursor(encoded, archived=False, sort_order=0) == (
        True,
        created_at,
        99,
        "guild.example",
    )
    with pytest.raises(HTTPException, match="INVALID_THREAD_CURSOR"):
        _decode_thread_cursor(encoded, archived=True, sort_order=0)
    with pytest.raises(HTTPException, match="INVALID_THREAD_CURSOR"):
        _decode_thread_cursor(encoded, archived=False, sort_order=1)


def test_thread_and_forum_channel_payloads_match_wire_shape() -> None:
    rendered_thread = channel_payload(channel(flags=THREAD_FLAG_PINNED))
    assert rendered_thread["flags"] == THREAD_FLAG_PINNED
    assert rendered_thread["thread_metadata"] == {
        "archived": False,
        "auto_archive_duration": 1440,
        "archive_timestamp": "2026-08-24T12:00:00+00:00",
        "locked": False,
        "invitable": None,
        "create_timestamp": "2026-08-24T12:00:00+00:00",
    }

    rendered_forum = channel_payload(
        channel(
            type=15,
            owner_id=None,
            owner_domain=None,
            archived=None,
            locked=None,
            auto_archive_duration=None,
            archive_timestamp=None,
            message_count=None,
            total_message_sent=None,
            member_count=None,
            starter_message_id=None,
            starter_message_domain=None,
            default_auto_archive_duration=1440,
            default_thread_rate_limit_per_user=0,
            default_forum_layout=0,
            last_thread_id=88,
            last_thread_domain="guild.example",
        )
    )
    assert rendered_forum["last_message_id"] == "88"
    assert rendered_forum["last_message_domain"] == "guild.example"
    assert "thread_metadata" not in rendered_forum


def test_locked_archived_message_deletion_requires_manage_threads() -> None:
    active_locked = channel(locked=True, archived=False)
    archived_unlocked = channel(locked=False, archived=True)
    archived_locked = channel(locked=True, archived=True)

    require_thread_message_delete_state(active_locked, Permission.VIEW_CHANNEL)
    require_thread_message_delete_state(archived_unlocked, Permission.VIEW_CHANNEL)
    require_thread_message_delete_state(archived_locked, Permission.MANAGE_THREADS)

    with pytest.raises(HTTPException) as caught:
        require_thread_message_delete_state(archived_locked, Permission.VIEW_CHANNEL)
    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "THREAD_LOCKED"


def test_federated_thread_request_is_strictly_operation_shaped() -> None:
    actor = {"id": "9", "origin_domain": "home.example", "username": "maple"}
    parsed = GuildThreadProxyRequest.model_validate(
        {
            "operation": "thread.update",
            "actor": actor,
            "channel_id": "10",
            "payload": {"archived": True},
        }
    )
    assert parsed.operation == "thread.update"
    with pytest.raises(ValidationError):
        GuildThreadProxyRequest.model_validate(
            {
                "operation": "thread.delete",
                "actor": actor,
                "channel_id": "10",
                "payload": {"name": "not allowed"},
            }
        )
    with pytest.raises(ValidationError):
        GuildThreadProxyRequest.model_validate(
            {
                "operation": "thread.member.put",
                "actor": actor,
                "channel_id": "10",
                "payload": {},
            }
        )


@pytest.mark.asyncio
async def test_remote_thread_create_uses_the_signed_guild_authority_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=True,
    )
    parent = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=15,
        owner_id=None,
        owner_domain=None,
        e2ee_required=False,
    )
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=parent,
    )
    proxy = AsyncMock(return_value={"id": "88", "origin_domain": "guild.example"})
    lock = AsyncMock()
    monkeypatch.setattr(threads_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(threads_api, "proxy_remote_thread_mutation", proxy)
    monkeypatch.setattr(threads_api, "lock_local_channel_mutation", lock)
    payload = ThreadCreate(name="Support", content="Need help", client_nonce="post-1")

    result = await threads_api.create_thread_service(
        EntityRef("20@guild.example"),
        payload,
        AuthenticatedUser(actor, AccessGrant(9, "home.example", "session"), "", False),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings(),
    )

    assert result["id"] == "88"
    lock.assert_not_awaited()
    assert proxy.await_args.args[4] == "thread.create"
    assert proxy.await_args.kwargs["payload"]["client_nonce"] == "post-1"


@pytest.mark.asyncio
async def test_remote_source_thread_uses_the_signed_guild_authority_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=True,
    )
    parent = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=0,
        owner_id=None,
        owner_domain=None,
    )
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=parent,
    )
    proxy = AsyncMock(return_value={"id": "81", "origin_domain": "guild.example"})
    lock = AsyncMock()
    monkeypatch.setattr(threads_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(threads_api, "proxy_remote_thread_mutation", proxy)
    monkeypatch.setattr(threads_api, "lock_local_channel_mutation", lock)

    result = await threads_api.create_thread_from_message(
        EntityRef("20@guild.example"),
        EntityRef("81@guild.example"),
        threads_api.ThreadFromMessageCreate(name="Source discussion"),
        AuthenticatedUser(actor, AccessGrant(9, "home.example", "session"), "", False),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings(),
    )

    assert result["id"] == "81"
    lock.assert_not_awaited()
    assert proxy.await_args.args[4] == "thread.create_from_message"
    assert proxy.await_args.kwargs["message_ref"].resolve("home.example") == (
        81,
        "guild.example",
    )


@pytest.mark.asyncio
async def test_remote_lifecycle_and_member_services_all_proxy_without_local_locks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=True,
    )
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=channel(id=20, type=11),
    )
    access_check = AsyncMock(return_value=(access, int(Permission.ADMINISTRATOR)))
    proxy = AsyncMock(
        side_effect=[
            {"id": "20", "origin_domain": "guild.example"},
            {"deleted": True},
            {"updated": True},
            {"updated": True},
        ]
    )
    monkeypatch.setattr(threads_api, "thread_access", access_check)
    monkeypatch.setattr(threads_api, "proxy_remote_thread_mutation", proxy)
    auth = AuthenticatedUser(actor, AccessGrant(9, "home.example", "session"), "", False)
    session = cast(Any, SimpleNamespace())
    redis = cast(Any, SimpleNamespace())
    configured = settings()

    updated = await threads_api.update_thread(
        EntityRef("20@guild.example"),
        ThreadUpdate(name="Renamed"),
        auth,
        session,
        redis,
        cast(Any, SimpleNamespace()),
        configured,
    )
    deleted = await threads_api.delete_thread(
        EntityRef("20@guild.example"),
        auth,
        session,
        redis,
        cast(Any, SimpleNamespace()),
        configured,
    )
    await threads_api.put_thread_member_service(
        EntityRef("20@guild.example"),
        EntityRef("12@member.example"),
        threads_api.ThreadMemberUpdate(),
        actor,
        session,
        redis,
        configured,
    )
    await threads_api.delete_thread_member_service(
        EntityRef("20@guild.example"),
        EntityRef("12@member.example"),
        actor,
        session,
        redis,
        configured,
    )

    assert updated["id"] == "20"
    assert deleted.status_code == 204
    assert [call.args[4] for call in proxy.await_args_list] == [
        "thread.update",
        "thread.delete",
        "thread.member.put",
        "thread.member.delete",
    ]
    assert all(call.kwargs["lock"] is False for call in access_check.await_args_list)


@pytest.mark.asyncio
async def test_remote_thread_proxy_validates_and_targets_the_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = {
        "id": "88",
        "origin_domain": "guild.example",
        "guild_id": "44",
        "guild_domain": "guild.example",
        "parent_id": "20",
        "parent_domain": "guild.example",
        "owner_id": "9",
        "owner_domain": "home.example",
        "type": 11,
        "name": "Support",
        "starter_message": {
            "author_id": "9",
            "author_domain": "home.example",
            "content": "Need help",
            "e2ee": None,
            "client_nonce": "post-1",
            "attachments": [],
        },
    }
    signed = AsyncMock(return_value=httpx.Response(200, json={"thread": rendered}))
    monkeypatch.setattr(threads_api, "signed_request", signed)
    actor = User(id=9, origin_domain="home.example", username="maple", is_local=True)
    access = cast(
        Any,
        SimpleNamespace(
            guild=SimpleNamespace(id=44, origin_domain="guild.example"),
            channel=channel(id=20, type=15),
        ),
    )

    result = await threads_api.proxy_remote_thread_mutation(
        cast(Any, SimpleNamespace()),
        settings(),
        access,
        actor,
        "thread.create",
        payload={"name": "Support", "content": "Need help", "client_nonce": "post-1"},
    )

    assert result == rendered
    assert signed.await_args.args[3:] == (
        "guild.example",
        "/_kaede/v1/guilds/44/proxy-thread",
    )
    assert signed.await_args.kwargs["payload"]["actor"]["id"] == "9"


@pytest.mark.asyncio
async def test_thread_proxy_endpoint_reuses_the_local_authority_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=False,
    )
    guild = SimpleNamespace(id=44, origin_domain="guild.example", unavailable=False)
    session = SimpleNamespace(get=AsyncMock(return_value=guild), rollback=AsyncMock())
    create = AsyncMock(return_value={"id": "88", "origin_domain": "guild.example"})
    monkeypatch.setattr(threads_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(threads_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(threads_api, "record_room_federation_recipient", AsyncMock())
    monkeypatch.setattr(threads_api, "create_thread_service", create)
    payload = GuildThreadProxyRequest.model_validate(
        {
            "operation": "thread.create",
            "actor": {
                "id": "9",
                "origin_domain": "home.example",
                "username": "maple",
            },
            "channel_id": "20",
            "payload": {"name": "Support", "content": "Need help", "client_nonce": "post-1"},
        }
    )

    result = await threads_api.federation_guild_thread_proxy(
        44,
        payload,
        FederationPrincipal("home.example", "ed25519:test"),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings("guild.example"),
    )

    assert result["thread"] == {"id": "88", "origin_domain": "guild.example"}
    parent_ref = create.await_args.args[0]
    assert parent_ref.resolve("guild.example") == (20, "guild.example")
    assert create.await_args.args[2].user is actor


@pytest.mark.asyncio
async def test_thread_proxy_endpoint_routes_lifecycle_and_membership_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=False,
    )
    guild = SimpleNamespace(id=44, origin_domain="guild.example", unavailable=False)
    session = SimpleNamespace(get=AsyncMock(return_value=guild), rollback=AsyncMock())
    update = AsyncMock(return_value={"id": "20", "origin_domain": "guild.example"})
    create_from_message = AsyncMock(return_value={"id": "81", "origin_domain": "guild.example"})
    delete = AsyncMock(return_value=threads_api.Response(status_code=204))
    put_member = AsyncMock()
    delete_member = AsyncMock()
    monkeypatch.setattr(threads_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(threads_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(threads_api, "record_room_federation_recipient", AsyncMock())
    monkeypatch.setattr(threads_api, "update_thread", update)
    monkeypatch.setattr(threads_api, "create_thread_from_message", create_from_message)
    monkeypatch.setattr(threads_api, "delete_thread", delete)
    monkeypatch.setattr(threads_api, "put_thread_member_service", put_member)
    monkeypatch.setattr(threads_api, "delete_thread_member_service", delete_member)
    base = {
        "actor": {
            "id": "9",
            "origin_domain": "home.example",
            "username": "maple",
        },
        "channel_id": "20",
    }
    principal = FederationPrincipal("home.example", "ed25519:test")
    configured = settings("guild.example")

    updated = await threads_api.federation_guild_thread_proxy(
        44,
        GuildThreadProxyRequest.model_validate(
            base | {"operation": "thread.update", "payload": {"archived": True}}
        ),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        configured,
    )
    source_created = await threads_api.federation_guild_thread_proxy(
        44,
        GuildThreadProxyRequest.model_validate(
            base
            | {
                "operation": "thread.create_from_message",
                "message_id": "81@guild.example",
                "payload": {"name": "Source discussion"},
            }
        ),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        configured,
    )
    deleted = await threads_api.federation_guild_thread_proxy(
        44,
        GuildThreadProxyRequest.model_validate(
            base | {"operation": "thread.delete", "payload": {}}
        ),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        configured,
    )
    member_payload = base | {
        "target_user_id": "12@member.example",
        "payload": {},
    }
    member_added = await threads_api.federation_guild_thread_proxy(
        44,
        GuildThreadProxyRequest.model_validate(member_payload | {"operation": "thread.member.put"}),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        configured,
    )
    member_removed = await threads_api.federation_guild_thread_proxy(
        44,
        GuildThreadProxyRequest.model_validate(
            member_payload | {"operation": "thread.member.delete"}
        ),
        principal,
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        configured,
    )

    assert updated["thread"]["id"] == "20"
    assert source_created["thread"]["id"] == "81"
    assert deleted == {"deleted": True}
    assert member_added == {"updated": True}
    assert member_removed == {"updated": True}
    update.assert_awaited_once()
    create_from_message.assert_awaited_once()
    delete.assert_awaited_once()
    put_member.assert_awaited_once()
    delete_member.assert_awaited_once()


def test_federated_channel_state_preserves_authoritative_creation_time() -> None:
    rendered = federation_channel_state(cast(Any, channel()))
    assert rendered["created_at"] == "2026-08-24T12:00:00+00:00"


@pytest.mark.asyncio
async def test_remote_thread_actor_is_attested_by_the_local_guild_signer() -> None:
    remote_actor = User(
        id=9,
        origin_domain="home.example",
        username="maple",
        is_local=False,
    )
    owner = User(
        id=1,
        origin_domain="guild.example",
        username="owner",
        is_local=True,
    )
    guild = cast(
        Any,
        SimpleNamespace(
            id=44,
            origin_domain="guild.example",
            owner_id=owner.id,
            owner_domain=owner.origin_domain,
        ),
    )
    session = SimpleNamespace(get=AsyncMock(return_value=owner))

    signer = await guild_mutation_signer(
        cast(Any, session), settings("guild.example"), guild, remote_actor
    )

    assert signer is owner


def test_type21_starter_is_a_clean_reference_and_deleted_source_resolves_null() -> None:
    thread = channel()
    source = {
        "id": "10",
        "origin_domain": "guild.example",
        "channel_id": "2",
        "channel_domain": "guild.example",
        "author_id": "3",
        "author_domain": "guild.example",
        "content": "source body",
        "attachments": [{"id": "9"}],
        "deleted_at": None,
        "created_at": "2026-08-24T12:00:00+00:00",
    }
    wrapper = thread_source_starter_payload(thread, source)
    assert wrapper["message_type"] == 21
    assert wrapper["channel_id"] == "10"
    assert wrapper["content"] is None
    assert wrapper["attachments"] == []
    assert wrapper["referenced_message"] is source
    assert wrapper["message_reference"]["message_id"] == "10"

    tombstone = thread_source_starter_payload(thread, source | {"deleted_at": "now"})
    assert tombstone["referenced_message"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        ThreadUpdate(archived=True),
        ThreadUpdate(auto_archive_duration=60),
        ThreadUpdate(invitable=False),
    ],
)
async def test_locked_thread_owner_cannot_mutate_properties_without_manage_threads(
    monkeypatch: pytest.MonkeyPatch,
    payload: ThreadUpdate,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    thread = channel(type=12, locked=True)
    access = SimpleNamespace(
        guild=SimpleNamespace(id=1, origin_domain="guild.example"),
        channel=thread,
    )
    monkeypatch.setattr(
        threads_api,
        "thread_access",
        AsyncMock(side_effect=[(access, 0), (access, 0)]),
    )

    with pytest.raises(HTTPException) as caught:
        await update_thread(
            EntityRef("10@guild.example"),
            payload,
            SimpleNamespace(user=actor),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert caught.value.status_code == 403
    assert caught.value.detail["code"] == "THREAD_LOCKED"


@pytest.mark.asyncio
async def test_changing_auto_archive_duration_advances_thread_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    prior_activity = datetime(2026, 8, 23, 12, tzinfo=UTC)
    thread = channel(type=11, last_activity_at=prior_activity)
    parent = channel(id=2, type=0)
    guild = SimpleNamespace(id=1, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=thread)
    session = SimpleNamespace(get=AsyncMock(return_value=parent), commit=AsyncMock())
    monkeypatch.setattr(
        threads_api,
        "thread_access",
        AsyncMock(
            side_effect=[
                (access, int(Permission.VIEW_CHANNEL)),
                (access, int(Permission.VIEW_CHANNEL)),
            ]
        ),
    )
    monkeypatch.setattr(threads_api, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(threads_api, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(threads_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(threads_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(threads_api, "rendered_thread", AsyncMock(return_value={}))

    await update_thread(
        EntityRef("10@guild.example"),
        ThreadUpdate(auto_archive_duration=60),
        SimpleNamespace(user=actor),
        cast(Any, session),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
    )

    assert thread.last_activity_at > prior_activity
    assert thread.archive_timestamp == thread.last_activity_at


@pytest.mark.asyncio
async def test_human_thread_member_routes_forward_pagination_and_rich_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    auth = SimpleNamespace(user=actor)
    list_members = AsyncMock(return_value=[{"user_id": "4"}])
    get_member = AsyncMock(return_value={"user_id": "4", "member": {}})
    monkeypatch.setattr(threads_api, "list_thread_members_service", list_members)
    monkeypatch.setattr(threads_api, "get_thread_member_service", get_member)
    thread_ref = EntityRef("10@guild.example")
    user_ref = EntityRef("4@guild.example")
    after = EntityRef("2@guild.example")
    session = SimpleNamespace()
    redis = SimpleNamespace()
    configured = SimpleNamespace(domain="guild.example")

    listed = await threads_api.list_thread_members(
        thread_ref,
        25,
        after,
        True,
        auth,
        session,
        redis,
        configured,
    )
    fetched = await threads_api.get_thread_member(
        thread_ref,
        user_ref,
        True,
        auth,
        session,
        redis,
        configured,
    )

    assert listed == [{"user_id": "4"}]
    assert fetched == {"user_id": "4", "member": {}}
    assert list_members.await_args.kwargs == {
        "limit": 25,
        "after": after,
        "with_member": True,
    }
    assert get_member.await_args.kwargs == {"with_member": True}


@pytest.mark.asyncio
async def test_bot_remove_other_thread_member_requires_manage_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = AsyncMock()
    remove = AsyncMock()
    monkeypatch.setattr(threads_api, "installation_for_channel", installation)
    monkeypatch.setattr(threads_api, "delete_thread_member_service", remove)
    principal = SimpleNamespace(user=SimpleNamespace(id=7, origin_domain="bot.example"))
    session = SimpleNamespace()
    settings = SimpleNamespace(domain="guild.example")

    await bot_remove_thread_member(
        EntityRef("10@guild.example"),
        EntityRef("8@guild.example"),
        principal,
        session,
        SimpleNamespace(),
        settings,
    )

    installation.assert_awaited_once_with(
        session,
        settings,
        principal,
        EntityRef("10@guild.example"),
        "channels.manage",
    )
    remove.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "scope"),
    [
        ("create", "messages.send"),
        ("create_from_message", "messages.send"),
        ("list", "channels.read"),
        ("update", "channels.manage"),
        ("delete", "channels.manage"),
        ("member_list", "members.read"),
        ("member_get", "members.read"),
        ("join", "channels.read"),
        ("leave", "channels.read"),
        ("add_member", "messages.send"),
        ("remove_member", "channels.manage"),
    ],
)
async def test_bot_thread_route_scope_matrix(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    scope: str,
) -> None:
    parent = channel(type=0, e2ee_required=False, encryption_mode="plaintext")
    installation = SimpleNamespace(granted_scopes=[])
    require_installation = AsyncMock(return_value=(parent, installation))
    monkeypatch.setattr(threads_api, "installation_for_channel", require_installation)
    monkeypatch.setattr(threads_api, "create_thread_service", AsyncMock(return_value={}))
    monkeypatch.setattr(threads_api, "create_thread_from_message", AsyncMock(return_value={}))
    monkeypatch.setattr(
        threads_api,
        "list_parent_threads_service",
        AsyncMock(return_value={"threads": []}),
    )
    monkeypatch.setattr(threads_api, "update_thread", AsyncMock(return_value={}))
    monkeypatch.setattr(threads_api, "delete_thread", AsyncMock())
    monkeypatch.setattr(threads_api, "list_thread_members_service", AsyncMock(return_value=[]))
    monkeypatch.setattr(threads_api, "get_thread_member_service", AsyncMock(return_value={}))
    monkeypatch.setattr(threads_api, "put_thread_member_service", AsyncMock())
    monkeypatch.setattr(threads_api, "delete_thread_member_service", AsyncMock())

    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        scopes=[],
    )
    ref = EntityRef("10@guild.example")
    user_ref = EntityRef("8@guild.example")
    session = SimpleNamespace()
    redis = SimpleNamespace()
    snowflake = SimpleNamespace()
    config = SimpleNamespace(domain="guild.example")

    if operation == "create":
        await threads_api.bot_create_thread(
            ref, ThreadCreate(name="post"), principal, session, redis, snowflake, config
        )
    elif operation == "create_from_message":
        await threads_api.bot_create_thread_from_message(
            ref,
            ref,
            threads_api.ThreadFromMessageCreate(name="thread"),
            principal,
            session,
            redis,
            snowflake,
            config,
        )
    elif operation == "list":
        await threads_api.bot_list_parent_threads(
            ref,
            principal,
            session,
            redis,
            config,
            False,
            False,
            None,
            None,
            50,
            None,
            None,
            None,
        )
    elif operation == "update":
        await threads_api.bot_update_thread(
            ref, ThreadUpdate(name="renamed"), principal, session, redis, snowflake, config
        )
    elif operation == "delete":
        await threads_api.bot_delete_thread(ref, principal, session, redis, snowflake, config)
    elif operation == "member_list":
        await threads_api.bot_list_thread_members(ref, principal, session, redis, config)
    elif operation == "member_get":
        await threads_api.bot_get_thread_member(ref, user_ref, principal, session, redis, config)
    elif operation == "join":
        await threads_api.bot_join_thread(ref, principal, session, redis, config)
    elif operation == "leave":
        await threads_api.bot_leave_thread(ref, principal, session, redis, config)
    elif operation == "add_member":
        await threads_api.bot_add_thread_member(ref, user_ref, principal, session, redis, config)
    else:
        await threads_api.bot_remove_thread_member(ref, user_ref, principal, session, redis, config)

    assert require_installation.await_args.args[-1] == scope


def bot_thread_result(*, e2ee_required: bool = False) -> dict[str, object]:
    starter: dict[str, object] = {
        "content": "starter secret",
        "e2ee": None,
        "attachments": [{"id": "99"}],
        "referenced_message": {
            "content": "source secret",
            "e2ee": None,
            "attachments": [{"id": "98"}],
        },
    }
    return {
        "id": "10",
        "origin_domain": "guild.example",
        "type": 11,
        "e2ee_required": e2ee_required,
        "encryption_mode": "plaintext",
        "starter_message": starter,
        "message": dict(starter),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "create_from_message", "update"])
async def test_bot_thread_mutation_responses_apply_content_grants(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    parent = channel(type=0, e2ee_required=False, encryption_mode="plaintext")
    granted_scopes = ["messages.content", "attachments.read"]
    installation = SimpleNamespace(granted_scopes=granted_scopes)
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(parent, installation)),
    )
    monkeypatch.setattr(
        threads_api,
        "create_thread_service",
        AsyncMock(return_value=bot_thread_result()),
    )
    monkeypatch.setattr(
        threads_api,
        "create_thread_from_message",
        AsyncMock(return_value=bot_thread_result()),
    )
    monkeypatch.setattr(
        threads_api,
        "update_thread",
        AsyncMock(return_value=bot_thread_result()),
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        scopes=granted_scopes,
    )
    ref = EntityRef("10@guild.example")
    session = SimpleNamespace()
    redis = SimpleNamespace()
    snowflake = SimpleNamespace()
    config = SimpleNamespace(domain="guild.example")

    if operation == "create":
        result = await threads_api.bot_create_thread(
            ref,
            ThreadCreate(name="post"),
            principal,
            session,
            redis,
            snowflake,
            config,
        )
    elif operation == "create_from_message":
        result = await threads_api.bot_create_thread_from_message(
            ref,
            ref,
            threads_api.ThreadFromMessageCreate(name="thread"),
            principal,
            session,
            redis,
            snowflake,
            config,
        )
    else:
        result = await threads_api.bot_update_thread(
            ref,
            ThreadUpdate(name="renamed"),
            principal,
            session,
            redis,
            snowflake,
            config,
        )

    for key in ("starter_message", "message"):
        starter = cast(dict[str, object], result[key])
        assert starter["content"] is None
        assert starter["attachments"] == []
        assert starter["content_unavailable"] is True
        assert starter["attachments_unavailable"] is True
        source = cast(dict[str, object], starter["referenced_message"])
        assert source["content"] is None
        assert source["attachments"] == []


@pytest.mark.asyncio
async def test_bot_update_thread_never_returns_e2ee_required_starter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = channel(type=11, e2ee_required=True, encryption_mode="plaintext")
    scopes = ["messages.history", "messages.content", "attachments.read"]
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(thread, SimpleNamespace(granted_scopes=scopes))),
    )
    monkeypatch.setattr(
        threads_api,
        "update_thread",
        AsyncMock(return_value=bot_thread_result(e2ee_required=True)),
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        scopes=scopes,
    )

    result = await threads_api.bot_update_thread(
        EntityRef("10@guild.example"),
        ThreadUpdate(name="renamed"),
        principal,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
    )

    starter = cast(dict[str, object], result["starter_message"])
    assert starter["content"] is None
    assert starter["attachments"] == []
    assert starter["content_unavailable"] is True
    assert starter["attachments_unavailable"] is True
