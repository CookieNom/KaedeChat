from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.tasks as tasks
from app.api import channels as channels_api
from app.api import federation as federation_api
from app.api import threads as threads_api
from app.api.channels import (
    MessageAdmissionOptions,
    advance_thread_message_projection,
    authoritative_message_mentions,
    bot_can_join_e2ee_thread,
    require_thread_message_delete_state,
)
from app.api.dependencies import AuthenticatedUser
from app.api.guilds import forum_reaction_payload
from app.api.threads import (
    FORUM_FLAG_REQUIRE_TAG,
    THREAD_FLAG_PINNED,
    GuildThreadProxyRequest,
    ThreadCreate,
    ThreadUpdate,
    _decode_thread_cursor,
    _encode_thread_cursor,
    _starter_reservation_identity,
    _thread_type,
    bot_remove_thread_member,
    update_thread,
    validate_applied_tags,
)
from app.auth.tokens import AccessGrant
from app.chat.guild_revision import federation_channel_state, guild_mutation_signer
from app.chat.payloads import channel_payload, thread_source_starter_payload
from app.chat.schemas import ChannelCreate, DefaultReactionEmoji, ForumTag, MessageCreate
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Attachment, User
from app.federation.guilds import _validated_channel_extension_state
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
        "bitrate": None,
        "user_limit": None,
        "rtc_region": None,
        "video_quality_mode": None,
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


def test_forum_sort_query_coerces_numeric_query_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = AsyncMock(
        return_value={"threads": [], "members": [], "has_more": False, "next_cursor": None}
    )
    monkeypatch.setattr(threads_api, "list_parent_threads_service", service)
    app = FastAPI()
    app.include_router(threads_api.router)
    app.dependency_overrides[threads_api.require_user] = lambda: SimpleNamespace(
        user=SimpleNamespace()
    )
    app.dependency_overrides[threads_api.get_session] = lambda: SimpleNamespace()
    app.dependency_overrides[threads_api.get_redis] = lambda: SimpleNamespace()
    app.dependency_overrides[threads_api.get_settings] = lambda: SimpleNamespace(
        domain="home.example"
    )

    response = TestClient(app).get("/api/v1/channels/2%40home.example/threads?sort_order=0")

    assert response.status_code == 200
    assert service.await_args.kwargs["sort_order"] == 0


@pytest.mark.asyncio
async def test_rejected_remote_thread_attachment_create_does_not_commit_local_unarchive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings("home.example")
    authority_domain = "guild.example"
    prior_archive_timestamp = datetime(2026, 8, 24, 12, tzinfo=UTC)
    thread = channel(
        archived=True,
        archive_timestamp=prior_archive_timestamp,
        origin_domain=authority_domain,
        guild_domain=authority_domain,
    )
    guild = SimpleNamespace(
        id=1,
        origin_domain=authority_domain,
        sync_status="ready",
    )
    access = channels_api.ChannelAccess(
        channel=cast(Any, thread), guild=cast(Any, guild), participants=[]
    )
    actor = User(
        id=7,
        origin_domain=configured.domain,
        is_local=True,
        account_type="human",
        username="maple",
        display_name=None,
        avatar_hash=None,
        banner_hash=None,
        bio=None,
        custom_status=None,
        profile_version=1,
        e2ee_device_generation=0,
    )
    attachment = Attachment(
        id=900,
        origin_domain=configured.domain,
        uploader_id=actor.id,
        uploader_domain=actor.origin_domain,
        filename="evidence.png",
        content_type="image/png",
        detected_content_type="image/png",
        size=128,
        object_key="home.example/900/clean/image.png",
        variants={},
        purpose="attachment",
        scan_status="clean",
        encryption_mode="plaintext",
        finalized_at=datetime(2026, 8, 29, 12, tzinfo=UTC),
    )
    committed_archive_states: list[bool] = []

    class FakeSession:
        async def get(self, *_args: object, **_kwargs: object) -> None:
            return None

        async def execute(self, _statement: object) -> None:
            return None

        async def scalar(self, _statement: object) -> None:
            return None

        async def commit(self) -> None:
            committed_archive_states.append(bool(thread.archived))

    session = cast(Any, FakeSession())
    recorded_recipients = AsyncMock()
    monkeypatch.setattr(
        channels_api,
        "load_message_create_access",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(
        channels_api,
        "lock_message_create_access",
        AsyncMock(return_value=(access, None)),
    )
    monkeypatch.setattr(channels_api, "enforce_client_rate_limit", AsyncMock())
    monkeypatch.setattr(
        channels_api,
        "require_channel_permissions",
        AsyncMock(return_value=int(Permission.ADMINISTRATOR | Permission.MANAGE_THREADS)),
    )
    monkeypatch.setattr(channels_api, "require_active_thread_capacity", AsyncMock())
    monkeypatch.setattr(channels_api, "require_dm_send", AsyncMock())
    monkeypatch.setattr(
        channels_api,
        "prepare_message_create_expressions",
        AsyncMock(
            return_value=channels_api.MessageCreateExpressions(
                encrypted_rich=False,
                encrypted_custom_emoji_tokens=[],
                application_ref=None,
                authorizations={},
                sticker_items=[],
            )
        ),
    )
    monkeypatch.setattr(
        channels_api,
        "resolve_message_create_mentions",
        AsyncMock(
            return_value=channels_api.MessageCreateMentions(
                explicit_recipients=[],
                recipients=[],
                role_recipients=set(),
                roles=[],
                everyone=False,
            )
        ),
    )
    monkeypatch.setattr(
        channels_api,
        "prepare_message_create_attachments",
        AsyncMock(
            return_value=channels_api.MessageCreateAttachments(
                replicated=[],
                local=[attachment],
            )
        ),
    )
    monkeypatch.setattr(channels_api, "enforce_message_create_slowmode", AsyncMock())
    monkeypatch.setattr(channels_api, "record_attachment_recipients", recorded_recipients)
    monkeypatch.setattr(
        channels_api,
        "signed_request",
        AsyncMock(
            return_value=httpx.Response(
                403,
                json={"detail": {"code": "MISSING_PERMISSIONS"}},
            )
        ),
    )

    with pytest.raises(HTTPException) as caught:
        await channels_api.create_message(
            EntityRef(f"{thread.id}@{authority_domain}"),
            MessageCreate(
                content="attachment from an archived replica",
                attachment_ids=[str(attachment.id)],
                client_nonce="remote-archived-attachment",
            ),
            Response(),
            SimpleNamespace(user=actor),
            session,
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            configured,
            MessageAdmissionOptions(),
        )

    assert caught.value.status_code == 403
    assert committed_archive_states == [True]
    assert thread.archived is True
    assert thread.archive_timestamp == prior_archive_timestamp
    recorded_recipients.assert_awaited_once_with(
        session,
        {(attachment.id, attachment.origin_domain)},
        authority_domain,
        room_ref=("guild", guild.id, authority_domain),
    )


@pytest.mark.asyncio
async def test_auto_archive_materializes_thread_dispatch_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = settings()
    current = datetime(2026, 8, 24, 13, tzinfo=UTC)
    guild = SimpleNamespace(id=1, origin_domain=configured.domain)
    thread = channel(
        origin_domain=configured.domain,
        guild_domain=configured.domain,
        last_activity_at=current,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.flushed = False
            self.refreshed: list[object] = []

        async def execute(self, _statement: object) -> object:
            return SimpleNamespace(
                all=lambda: [(thread.id, thread.origin_domain, guild.id, guild.origin_domain)]
            )

        async def scalar(self, _statement: object) -> object:
            return guild

        async def scalars(self, _statement: object) -> list[object]:
            return [thread]

        async def flush(self) -> None:
            assert not self.committed
            self.flushed = True

        async def refresh(self, value: object) -> None:
            assert self.flushed
            assert not self.committed
            self.refreshed.append(value)

        async def commit(self) -> None:
            self.committed = True

    session = FakeSession()
    redis = object()

    def materialize_channel_payload(value: object) -> dict[str, object]:
        # A post-commit read of this server-onupdate timestamp previously
        # attempted implicit async I/O and raised MissingGreenlet.
        assert not session.committed
        return {"version": cast(Any, value).updated_at.isoformat()}

    async def assert_committed(*_args: object) -> None:
        assert session.committed

    monkeypatch.setattr(tasks, "guild_authority_owner", AsyncMock(return_value=object()))
    queue_mutation = AsyncMock()
    monkeypatch.setattr(tasks, "queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(tasks, "channel_payload", materialize_channel_payload)
    wake = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(tasks, "wake_queued_guild_federation", wake)
    publish = AsyncMock(side_effect=assert_committed)
    monkeypatch.setattr(tasks, "publish_dispatch", publish)

    archived = await tasks.thread_auto_archive_sweep_in_session(
        cast(Any, session),
        cast(Any, redis),
        configured,
        now=current,
    )

    assert archived == 1
    assert thread.archived is True
    assert thread.archive_timestamp == current
    assert session.refreshed == [thread]
    queue_mutation.assert_awaited_once()
    wake.assert_awaited_once_with(guild)
    publish.assert_awaited_once()
    assert publish.await_args.args == (
        redis,
        tasks.guild_topic(configured.domain, guild.id),
        "THREAD_UPDATE",
        {"version": thread.updated_at.isoformat()},
    )


@pytest.mark.asyncio
async def test_federation_thread_projection_materializes_server_defaults_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thread = channel(
        origin_domain="home.example",
        guild_domain="home.example",
        member_count=2,
    )
    member = SimpleNamespace(
        thread_id=thread.id,
        thread_domain=thread.origin_domain,
        guild_id=thread.guild_id,
        guild_domain=thread.guild_domain,
        user_id=7,
        user_domain="member.example",
        joined_at=datetime(2026, 8, 24, 12, 30, tzinfo=UTC),
        flags=0,
        notification_level="inherit",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.refreshed: list[object] = []

        async def flush(self) -> None:
            assert not self.committed

        async def refresh(self, value: object) -> None:
            assert not self.committed
            self.refreshed.append(value)

    session = FakeSession()

    def render_channel(value: object) -> dict[str, object]:
        assert not session.committed
        return {"version": cast(Any, value).updated_at.isoformat()}

    async def render_rich_member(_session: object, value: object) -> dict[str, object]:
        assert not session.committed
        return {"user_id": str(cast(Any, value).user_id), "member": {}}

    monkeypatch.setattr(federation_api, "channel_payload", render_channel)
    monkeypatch.setattr(
        federation_api,
        "rich_thread_member_payload",
        render_rich_member,
    )

    projection = await federation_api.materialize_thread_dispatch(
        cast(Any, session),
        cast(Any, thread),
        [cast(Any, member)],
    )
    session.committed = True

    assert session.refreshed == [thread, member]
    assert projection.guild_ref == (thread.guild_id, thread.guild_domain)
    assert projection.channel == {"version": thread.updated_at.isoformat()}
    assert projection.added_members[0][0] == "7@member.example"
    assert projection.members_update == {
        "id": str(thread.id),
        "thread_domain": thread.origin_domain,
        "guild_id": str(thread.guild_id),
        "guild_domain": thread.guild_domain,
        "member_count": 2,
        "added_members": [{"user_id": "7", "member": {}}],
        "removed_member_ids": [],
    }


def test_encrypted_forum_starter_does_not_regress_control_log_cursor() -> None:
    control_created_at = datetime(2026, 8, 24, 12, 1, tzinfo=UTC)
    starter_created_at = datetime(2026, 8, 24, 12, 2, tzinfo=UTC)
    thread = channel(
        last_message_id=30,
        last_message_domain="guild.example",
        last_activity_at=control_created_at,
    )

    advance_thread_message_projection(
        cast(Any, thread),
        cast(
            Any,
            SimpleNamespace(
                id=10,
                origin_domain="guild.example",
                created_at=starter_created_at,
            ),
        ),
    )

    assert (thread.last_message_id, thread.last_message_domain) == (30, "guild.example")
    assert thread.last_activity_at == starter_created_at


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


@pytest.mark.asyncio
async def test_encrypted_thread_bot_join_requires_participant_installation() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))
    assert not await bot_can_join_e2ee_thread(
        cast(Any, session),
        cast(Any, SimpleNamespace(id=1, origin_domain="guild.example")),
        cast(Any, channel(e2ee_required=True, encryption_mode="e2ee")),
        cast(Any, SimpleNamespace(id=8, origin_domain="apps.example")),
    )


@pytest.mark.asyncio
async def test_encrypted_thread_bot_join_accepts_staged_trusted_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace(id=77, e2ee_mode="participant")
    session = SimpleNamespace(scalar=AsyncMock(return_value=installation))
    active = AsyncMock(return_value=(SimpleNamespace(status="pending"), SimpleNamespace()))
    monkeypatch.setattr(channels_api, "active_bot_e2ee_participation", active)
    thread = channel(e2ee_required=True, encryption_mode="e2ee")

    assert await bot_can_join_e2ee_thread(
        cast(Any, session),
        cast(Any, SimpleNamespace(id=1, origin_domain="guild.example")),
        cast(Any, thread),
        cast(Any, SimpleNamespace(id=8, origin_domain="apps.example")),
    )
    active.assert_awaited_once_with(
        session,
        installation,
        thread,
        None,
        include_pending=True,
    )


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


def test_encrypted_forum_shell_has_no_starter_body() -> None:
    shell = ThreadCreate(
        name="private post",
        starter_reservation_nonce="post-claim-1",
    )

    assert shell.starter() is None
    with pytest.raises(ValidationError):
        ThreadCreate(name="private post", starter_reservation_nonce="not allowed / spaces")


def test_authoritative_application_mentions_preserve_role_recipient_subset() -> None:
    options = MessageAdmissionOptions(
        application_id=70,
        application_domain="apps.example",
        authoritative_mention_refs=((8, "guild.example"), (9, "guild.example")),
        authoritative_mention_role_refs=((4, "guild.example"),),
        authoritative_mention_role_recipient_refs=((9, "guild.example"),),
        authoritative_mention_everyone=False,
    )

    mentions = authoritative_message_mentions(options)

    assert mentions.recipients == [(8, "guild.example"), (9, "guild.example")]
    assert mentions.role_recipients == {(9, "guild.example")}
    assert mentions.roles == [(4, "guild.example")]
    assert not mentions.everyone

    with pytest.raises(ValueError, match="matching mention recipients"):
        MessageAdmissionOptions(
            application_id=70,
            application_domain="apps.example",
            authoritative_mention_refs=((8, "guild.example"),),
            authoritative_mention_role_recipient_refs=((9, "guild.example"),),
        )


@pytest.mark.asyncio
async def test_bot_forum_reservation_binds_exact_worker_device_and_installation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(id=8, origin_domain="apps.example", username="bot", is_local=False)
    options = MessageAdmissionOptions(
        application_id=70,
        application_domain="apps.example",
        bot_installation_id=90,
        bot_worker_id=40,
    )
    with pytest.raises(HTTPException) as caught:
        await _starter_reservation_identity(
            cast(Any, SimpleNamespace()),
            settings(),
            actor,
            options,
        )
    assert caught.value.detail["code"] == "STARTER_RESERVATION_DEVICE_REQUIRED"

    lineage = AsyncMock(return_value=("guild_install", 90, "guild.example", 4))
    monkeypatch.setattr(threads_api, "message_view_installation_lineage", lineage)
    identity = await _starter_reservation_identity(
        cast(Any, SimpleNamespace()),
        settings(),
        actor,
        options,
        claimant_device_id="kbe_" + "a" * 43,
    )

    assert identity == {
        "claimant_kind": "bot",
        "claimant_id": 8,
        "claimant_domain": "apps.example",
        "worker_id": 40,
        "claimant_device_id": "kbe_" + "a" * 43,
        "application_id": 70,
        "application_domain": "apps.example",
        "installation_type": "guild_install",
        "installation_id": 90,
        "installation_domain": "guild.example",
        "installation_revision": 4,
        "webhook_id": None,
        "webhook_domain": None,
    }
    lineage.assert_awaited_once()


@pytest.mark.asyncio
async def test_e2ee_forum_rejects_plaintext_or_unreserved_first_starter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="guild.example",
        username="maple",
        is_local=True,
    )
    forum = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=15,
        owner_id=None,
        owner_domain=None,
        e2ee_required=True,
    )
    access = SimpleNamespace(
        guild=SimpleNamespace(id=44, origin_domain="guild.example"),
        channel=forum,
    )
    monkeypatch.setattr(threads_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(
        threads_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    auth = AuthenticatedUser(
        actor,
        AccessGrant(9, "guild.example", "session"),
        "",
        False,
    )
    arguments = (
        EntityRef("20@guild.example"),
        auth,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings("guild.example"),
    )

    with pytest.raises(HTTPException) as plaintext:
        await threads_api.create_thread_service(
            arguments[0],
            ThreadCreate(name="secret", content="must never leak"),
            *arguments[1:],
        )
    assert plaintext.value.detail["code"] == "E2EE_FORUM_STARTER_REQUIRES_ACTIVATION"

    with pytest.raises(HTTPException) as unreserved:
        await threads_api.create_thread_service(
            arguments[0],
            ThreadCreate(name="secret"),
            *arguments[1:],
        )
    assert unreserved.value.detail["code"] == "STARTER_RESERVATION_NONCE_REQUIRED"


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


@pytest.mark.parametrize(
    "emoji_name",
    [
        "lantern",
        "🏮🔥",
        "\ufe0f",
        "<:lantern:7@home.example>",
    ],
)
def test_default_forum_reaction_rejects_invalid_unicode_names(emoji_name: str) -> None:
    with pytest.raises(ValidationError, match="exactly one valid emoji|custom emoji ID branch"):
        ChannelCreate(
            name="forum",
            type=15,
            default_reaction_emoji={"emoji_name": emoji_name},
        )


def test_default_forum_reaction_canonicalizes_and_round_trips_api_payloads() -> None:
    request = ChannelCreate(
        name="forum",
        type=15,
        default_reaction_emoji={"emoji_name": "❤️"},
    )
    assert request.default_reaction_emoji == DefaultReactionEmoji(emoji_name="❤")
    assert request.model_dump(mode="json")["default_reaction_emoji"] == {
        "emoji_id": None,
        "emoji_name": "❤",
    }
    assert forum_reaction_payload(request.default_reaction_emoji) == {
        "emoji_id": None,
        "emoji_name": "❤",
    }

    custom = DefaultReactionEmoji(emoji_id="42")
    assert forum_reaction_payload(custom) == {"emoji_id": "42", "emoji_name": None}


def test_federated_forum_default_canonicalizes_unicode_wire_identity() -> None:
    state = {
        "flags": "0",
        "default_auto_archive_duration": 1440,
        "default_thread_rate_limit_per_user": 0,
        "default_forum_layout": 0,
        "default_reaction_emoji": {"emoji_id": None, "emoji_name": "❤"},
    }
    assert _validated_channel_extension_state(state, 15, "guild.example")[
        "default_reaction_emoji"
    ] == {"emoji_id": None, "emoji_name": "❤"}

    state["default_reaction_emoji"] = {"emoji_id": None, "emoji_name": "❤️"}
    assert _validated_channel_extension_state(state, 15, "guild.example")[
        "default_reaction_emoji"
    ] == {"emoji_id": None, "emoji_name": "❤"}


@pytest.mark.parametrize("emoji_name", ["lantern", "🏮🔥", "\ufe0f"])
def test_federated_forum_default_rejects_invalid_unicode_wire_identity(
    emoji_name: str,
) -> None:
    state = {
        "flags": "0",
        "default_auto_archive_duration": 1440,
        "default_thread_rate_limit_per_user": 0,
        "default_forum_layout": 0,
        "default_reaction_emoji": {"emoji_id": None, "emoji_name": emoji_name},
    }

    with pytest.raises(ValueError, match="default forum reaction name is invalid"):
        _validated_channel_extension_state(state, 15, "guild.example")


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
async def test_forum_webhook_capability_does_not_recheck_creator_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="guild.example",
        username="retired-owner",
        is_local=True,
    )
    forum = channel(
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
        channel=forum,
    )
    capability_access = AsyncMock(return_value=access)
    member_access = AsyncMock(side_effect=AssertionError("member authority was rechecked"))
    permission_check = AsyncMock(side_effect=AssertionError("creator roles were rechecked"))
    monkeypatch.setattr(
        threads_api,
        "load_webhook_capability_channel_access",
        capability_access,
    )
    monkeypatch.setattr(threads_api, "load_channel_access", member_access)
    monkeypatch.setattr(
        threads_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(threads_api, "require_permissions", permission_check)
    monkeypatch.setattr(
        threads_api,
        "validate_applied_tags",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("permission fence passed")),
    )

    with pytest.raises(RuntimeError, match="permission fence passed"):
        await threads_api.create_thread_service(
            EntityRef("20@guild.example"),
            ThreadCreate(name="Release notes", content="Shipped"),
            AuthenticatedUser(actor, AccessGrant(9, "guild.example", "session"), "", False),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace()),
            settings("guild.example"),
            starter_admission_options=MessageAdmissionOptions(
                webhook_id=7,
                webhook_channel_id=20,
                webhook_channel_domain="guild.example",
            ),
        )

    capability_access.assert_awaited_once()
    member_access.assert_not_awaited()
    permission_check.assert_not_awaited()


@pytest.mark.asyncio
async def test_forum_webhook_retry_renders_its_message_without_creator_read_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="guild.example",
        username="retired-owner",
        is_local=True,
    )
    forum = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=15,
        owner_id=None,
        owner_domain=None,
        e2ee_required=False,
    )
    guild = SimpleNamespace(id=44, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=forum)
    existing_thread = channel(
        id=88,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=11,
        parent_id=20,
        parent_domain="guild.example",
        owner_id=9,
        owner_domain="guild.example",
        starter_message_id=88,
        starter_message_domain="guild.example",
    )
    existing_starter = SimpleNamespace(
        id=88,
        origin_domain="guild.example",
        webhook_id=7,
        deleted_at=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(),
        scalar=AsyncMock(return_value=existing_thread),
        get=AsyncMock(return_value=existing_starter),
    )
    rendered_message = {"id": "88", "content": "Shipped"}
    permission_check = AsyncMock(side_effect=AssertionError("creator roles were rechecked"))
    monkeypatch.setattr(
        threads_api,
        "load_webhook_capability_channel_access",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(
        threads_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(threads_api, "require_permissions", permission_check)
    monkeypatch.setattr(
        threads_api,
        "rendered_thread",
        AsyncMock(return_value={"starter_message": {"content_unavailable": True}}),
    )
    render_message = AsyncMock(return_value=rendered_message)
    monkeypatch.setattr(threads_api, "render_message_payload", render_message)

    result = await threads_api.create_thread_service(
        EntityRef("20@guild.example"),
        ThreadCreate(
            name="Release notes",
            content="Shipped",
            client_nonce="w7-retry",
        ),
        AuthenticatedUser(actor, AccessGrant(9, "guild.example", "session"), "", False),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings("guild.example"),
        starter_admission_options=MessageAdmissionOptions(
            webhook_id=7,
            webhook_channel_id=20,
            webhook_channel_domain="guild.example",
        ),
    )

    assert result["message"] == rendered_message
    assert result["starter_message"] == rendered_message
    render_message.assert_awaited_once_with(session, existing_starter)
    permission_check.assert_not_awaited()


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
        "source discussion",
    )

    assert result["id"] == "81"
    lock.assert_not_awaited()
    assert proxy.await_args.args[4] == "thread.create_from_message"
    assert proxy.await_args.kwargs["message_ref"].resolve("home.example") == (
        81,
        "guild.example",
    )
    assert proxy.await_args.kwargs["reason"] == "source discussion"


@pytest.mark.asyncio
async def test_encrypted_parent_message_creates_independent_e2ee_child_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=9,
        origin_domain="guild.example",
        username="maple",
        is_local=True,
    )
    parent = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=0,
        encryption_mode="e2ee",
        encryption_state="active",
    )
    guild = SimpleNamespace(id=44, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=parent)
    source = SimpleNamespace(
        id=81,
        origin_domain="guild.example",
        channel_id=20,
        channel_domain="guild.example",
        flags=0,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[source, None]),
        get=AsyncMock(side_effect=[None, None]),
        add=Mock(),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(threads_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(
        threads_api,
        "lock_local_channel_mutation",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(
        threads_api,
        "require_permissions",
        AsyncMock(return_value=int(Permission.ADMINISTRATOR)),
    )
    monkeypatch.setattr(threads_api, "require_active_thread_capacity", AsyncMock())
    monkeypatch.setattr(threads_api, "enforce_thread_create_slowmode", AsyncMock())
    monkeypatch.setattr(threads_api, "add_audit_entry", AsyncMock())
    monkeypatch.setattr(threads_api, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(threads_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(threads_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(
        threads_api,
        "federation_channel_state",
        lambda item: {"id": str(item.id), "e2ee_required": item.e2ee_required},
    )
    monkeypatch.setattr(
        threads_api,
        "channel_payload",
        lambda item: {"id": str(item.id), "e2ee_required": item.e2ee_required},
    )
    monkeypatch.setattr(
        threads_api,
        "thread_member_payload",
        lambda _item: {},
    )
    monkeypatch.setattr(
        threads_api,
        "render_message_payload",
        AsyncMock(return_value={"id": "81", "e2ee": {"version": 2}}),
    )
    render_thread = AsyncMock(
        return_value={"id": "81", "e2ee_required": True, "starter_message": None}
    )
    monkeypatch.setattr(threads_api, "rendered_thread", render_thread)

    result = await threads_api.create_thread_from_message(
        EntityRef("20@guild.example"),
        EntityRef("81@guild.example"),
        threads_api.ThreadFromMessageCreate(name="Private discussion"),
        AuthenticatedUser(
            actor,
            AccessGrant(9, "guild.example", "session"),
            "",
            False,
        ),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        settings("guild.example"),
    )

    created_thread = next(
        call.args[0]
        for call in session.add.call_args_list
        if getattr(call.args[0], "type", None) == 11
    )
    assert created_thread.id == source.id
    assert created_thread.e2ee_required is True
    assert created_thread.encryption_mode == "plaintext"
    assert result["e2ee_required"] is True
    assert source.flags & (1 << 5)


@pytest.mark.asyncio
async def test_bot_encrypted_source_thread_requires_source_participation_and_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = channel(type=0, encryption_mode="e2ee", encryption_state="active")
    installation = SimpleNamespace(id=99, granted_scopes=["messages.history"])
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(parent, installation)),
    )
    source_access = AsyncMock()
    monkeypatch.setattr(threads_api, "require_bot_forward_source_access", source_access)
    monkeypatch.setattr(
        threads_api,
        "create_thread_from_message",
        AsyncMock(return_value=bot_thread_result(e2ee_required=True)),
    )
    monkeypatch.setattr(
        threads_api,
        "render_bot_thread_result",
        AsyncMock(return_value={}),
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        application=SimpleNamespace(id=70, origin_domain="bot.example"),
        worker=SimpleNamespace(id=40),
        scopes=["messages.history"],
    )
    source_ref = EntityRef("81@guild.example")

    await threads_api.bot_create_thread_from_message(
        EntityRef("20@guild.example"),
        source_ref,
        threads_api.ThreadFromMessageCreate(name="Private discussion"),
        principal,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        "kbe_" + "a" * 43,
    )

    source_access.assert_awaited_once()
    assert source_access.await_args.args[3] == source_ref
    assert source_access.await_args.kwargs["e2ee_device_id"] == "kbe_" + "a" * 43


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
            {"id": "20", "origin_domain": "guild.example"},
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
    assert deleted == {"id": "20", "origin_domain": "guild.example"}
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
async def test_remote_encrypted_forum_shell_response_proves_unclaimed_reservation(
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
        "name": "Private support",
        "e2ee_required": True,
        "starter_message": None,
        "message": None,
        "starter_reservation": {
            "client_nonce": "claim-1",
            "claimed": False,
        },
    }
    signed = AsyncMock(return_value=httpx.Response(200, json={"thread": rendered}))
    monkeypatch.setattr(threads_api, "signed_request", signed)
    actor = User(id=9, origin_domain="home.example", username="maple", is_local=True)
    forum = channel(
        id=20,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=15,
        e2ee_required=True,
    )
    access = cast(
        Any,
        SimpleNamespace(
            guild=SimpleNamespace(id=44, origin_domain="guild.example"),
            channel=forum,
        ),
    )

    result = await threads_api.proxy_remote_thread_mutation(
        cast(Any, SimpleNamespace()),
        settings(),
        access,
        actor,
        "thread.create",
        payload={
            "name": "Private support",
            "starter_reservation_nonce": "claim-1",
        },
    )

    assert result["starter_reservation"] == {
        "client_nonce": "claim-1",
        "claimed": False,
    }
    sent = signed.await_args.kwargs["payload"]
    assert sent["payload"]["starter_reservation_nonce"] == "claim-1"

    signed.return_value = httpx.Response(
        200,
        json={"thread": rendered | {"starter_reservation": None}},
    )
    with pytest.raises(HTTPException) as invalid:
        await threads_api.proxy_remote_thread_mutation(
            cast(Any, SimpleNamespace()),
            settings(),
            access,
            actor,
            "thread.create",
            payload={
                "name": "Private support",
                "starter_reservation_nonce": "claim-1",
            },
        )
    assert invalid.value.detail["code"] == "FEDERATED_WRITE_RESPONSE_INVALID"


@pytest.mark.asyncio
async def test_remote_encrypted_forum_claim_binds_exact_envelope_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {
        "version": 2,
        "operation": "create",
        "rich_payload_digest": "opaque",
    }
    rendered = {
        "id": "88",
        "origin_domain": "guild.example",
        "channel_id": "88",
        "channel_domain": "guild.example",
        "author_id": "9",
        "author_domain": "home.example",
        "client_nonce": "claim-1",
        "e2ee": envelope,
        "attachments": [],
    }
    signed = AsyncMock(return_value=httpx.Response(200, json={"message": rendered}))
    monkeypatch.setattr(threads_api, "signed_request", signed)
    actor = User(id=9, origin_domain="home.example", username="maple", is_local=True)
    thread = channel(
        id=88,
        origin_domain="guild.example",
        guild_id=44,
        guild_domain="guild.example",
        type=11,
        parent_id=20,
        parent_domain="guild.example",
        e2ee_required=True,
    )
    access = cast(
        Any,
        SimpleNamespace(
            guild=SimpleNamespace(id=44, origin_domain="guild.example"),
            channel=thread,
        ),
    )

    result = await threads_api.proxy_remote_thread_mutation(
        cast(Any, SimpleNamespace()),
        settings(),
        access,
        actor,
        "thread.starter.claim",
        payload={"e2ee": envelope, "client_nonce": "claim-1"},
    )

    assert result == rendered
    assert signed.await_args.kwargs["payload"]["operation"] == "thread.starter.claim"

    signed.return_value = httpx.Response(
        200,
        json={"message": rendered | {"author_id": "10"}},
    )
    with pytest.raises(HTTPException) as invalid:
        await threads_api.proxy_remote_thread_mutation(
            cast(Any, SimpleNamespace()),
            settings(),
            access,
            actor,
            "thread.starter.claim",
            payload={"e2ee": envelope, "client_nonce": "claim-1"},
        )
    assert invalid.value.detail["code"] == "FEDERATED_WRITE_RESPONSE_INVALID"

    signed.return_value = httpx.Response(
        200,
        json={"message": rendered, "unbound_metadata": True},
    )
    with pytest.raises(HTTPException) as invalid:
        await threads_api.proxy_remote_thread_mutation(
            cast(Any, SimpleNamespace()),
            settings(),
            access,
            actor,
            "thread.starter.claim",
            payload={"e2ee": envelope, "client_nonce": "claim-1"},
        )
    assert invalid.value.detail["code"] == "FEDERATED_WRITE_RESPONSE_INVALID"

    attachment = {"id": "70", "origin_domain": "home.example"}
    signed.return_value = httpx.Response(
        200,
        json={"message": rendered | {"attachments": [attachment, attachment]}},
    )
    with pytest.raises(HTTPException) as invalid:
        await threads_api.proxy_remote_thread_mutation(
            cast(Any, SimpleNamespace()),
            settings(),
            access,
            actor,
            "thread.starter.claim",
            payload={"e2ee": envelope, "client_nonce": "claim-1"},
            attachments=[attachment],
        )
    assert invalid.value.detail["code"] == "FEDERATED_WRITE_RESPONSE_INVALID"


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
    monkeypatch.setattr(
        threads_api,
        "require_remote_user_creation_allowed",
        AsyncMock(),
    )
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
            "reason": "federated create",
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
    assert create.await_args.kwargs["reason"] == "federated create"


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
    delete = AsyncMock(return_value={"id": "20", "origin_domain": "guild.example"})
    put_member = AsyncMock()
    delete_member = AsyncMock()
    monkeypatch.setattr(threads_api, "enforce_federation_route_rate_limit", AsyncMock())
    monkeypatch.setattr(threads_api, "upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        threads_api,
        "require_remote_user_creation_allowed",
        AsyncMock(),
    )
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
    assert deleted == {"thread": {"id": "20", "origin_domain": "guild.example"}}
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
    session = SimpleNamespace(
        flush=AsyncMock(),
        scalar=AsyncMock(side_effect=[guild, owner]),
    )

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
async def test_thread_owner_update_checks_interaction_policy_after_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    thread = channel(type=11)
    parent = channel(id=2, type=0)
    guild = SimpleNamespace(id=1, origin_domain="guild.example")
    access = SimpleNamespace(guild=guild, channel=thread)
    session = SimpleNamespace(get=AsyncMock(return_value=parent))
    monkeypatch.setattr(
        threads_api,
        "thread_access",
        AsyncMock(side_effect=[(access, 0), (access, 0)]),
    )

    async def deny_after_authorization(*_args: object) -> None:
        session.get.assert_awaited_once_with(
            threads_api.Channel,
            (thread.parent_id, thread.parent_domain),
        )
        raise HTTPException(status_code=403, detail={"code": "MEMBER_TIMED_OUT"})

    interaction_gate = AsyncMock(side_effect=deny_after_authorization)
    monkeypatch.setattr(
        threads_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )

    with pytest.raises(HTTPException) as caught:
        await update_thread(
            EntityRef("10@guild.example"),
            ThreadUpdate(name="Renamed"),
            SimpleNamespace(user=actor),
            cast(Any, session),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert caught.value.detail["code"] == "MEMBER_TIMED_OUT"
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.SEND_MESSAGES_IN_THREADS,
    )
    assert thread.name == "post"


@pytest.mark.asyncio
async def test_unauthorized_thread_update_does_not_probe_interaction_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=4, origin_domain="guild.example")
    access = SimpleNamespace(
        guild=SimpleNamespace(id=1, origin_domain="guild.example"),
        channel=channel(type=11),
    )
    monkeypatch.setattr(
        threads_api,
        "thread_access",
        AsyncMock(side_effect=[(access, 0), (access, 0)]),
    )
    interaction_gate = AsyncMock()
    monkeypatch.setattr(
        threads_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )

    with pytest.raises(HTTPException) as caught:
        await update_thread(
            EntityRef("10@guild.example"),
            ThreadUpdate(name="Renamed"),
            SimpleNamespace(user=actor),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert caught.value.detail["code"] == "MISSING_PERMISSIONS"
    interaction_gate.assert_not_awaited()


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
    refreshed_at = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed = False
    committed = False

    async def refresh(
        value: object,
        *,
        attribute_names: tuple[str, ...],
    ) -> None:
        nonlocal refreshed
        assert value is thread
        assert attribute_names == ("updated_at",)
        thread.updated_at = refreshed_at
        refreshed = True

    async def commit() -> None:
        nonlocal committed
        committed = True

    session = SimpleNamespace(
        get=AsyncMock(return_value=parent),
        flush=AsyncMock(),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
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
    audit = AsyncMock()
    monkeypatch.setattr(threads_api, "add_audit_entry", audit)
    monkeypatch.setattr(threads_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(threads_api, "publish_dispatch", AsyncMock())
    interaction_gate = AsyncMock()
    monkeypatch.setattr(
        threads_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )
    original_channel_payload = threads_api.channel_payload

    def render_channel(value: object) -> dict[str, object]:
        assert refreshed
        assert not committed
        return original_channel_payload(cast(Any, value))

    async def render_thread(*_args: object, **kwargs: object) -> dict[str, object]:
        assert not committed
        return dict(cast(dict[str, object], kwargs["base_payload"]))

    monkeypatch.setattr(threads_api, "channel_payload", render_channel)
    monkeypatch.setattr(threads_api, "rendered_thread", AsyncMock(side_effect=render_thread))

    await update_thread(
        EntityRef("10@guild.example"),
        ThreadUpdate(auto_archive_duration=60),
        SimpleNamespace(user=actor),
        cast(Any, session),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
        "archive policy",
    )

    assert thread.last_activity_at > prior_activity
    assert thread.archive_timestamp == thread.last_activity_at
    assert audit.await_args.kwargs["reason"] == "archive policy"
    assert refreshed
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.SEND_MESSAGES_IN_THREADS,
    )


@pytest.mark.asyncio
async def test_self_thread_join_checks_interaction_policy_after_membership_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    guild = SimpleNamespace(id=1, origin_domain="guild.example")
    thread = channel(type=11, member_count=0)
    parent = channel(id=2, type=0)
    access = SimpleNamespace(guild=guild, channel=thread)
    guild_member = SimpleNamespace()
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                None,
                guild_member,
                actor,
                parent,
                None,
            ]
        ),
        add=Mock(),
    )
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
    monkeypatch.setattr(
        threads_api,
        "get_permissions",
        AsyncMock(return_value=int(Permission.VIEW_CHANNEL)),
    )

    async def deny_after_authorization(*_args: object) -> None:
        assert session.get.await_count == 5
        raise HTTPException(status_code=403, detail={"code": "MEMBER_TIMED_OUT"})

    interaction_gate = AsyncMock(side_effect=deny_after_authorization)
    monkeypatch.setattr(
        threads_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )

    with pytest.raises(HTTPException) as caught:
        await threads_api.put_thread_member_service(
            EntityRef("10@guild.example"),
            EntityRef("3@guild.example"),
            threads_api.ThreadMemberUpdate(),
            cast(Any, actor),
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="guild.example")),
        )

    assert caught.value.detail["code"] == "MEMBER_TIMED_OUT"
    interaction_gate.assert_awaited_once_with(
        session,
        guild,
        actor,
        Permission.SEND_MESSAGES_IN_THREADS,
    )
    session.add.assert_not_called()
    assert thread.member_count == 0


@pytest.mark.asyncio
async def test_existing_self_thread_preferences_remain_available_during_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=3, origin_domain="guild.example")
    guild = SimpleNamespace(id=1, origin_domain="guild.example")
    thread = channel(type=11, member_count=1)
    parent = channel(id=2, type=0)
    access = SimpleNamespace(guild=guild, channel=thread)
    member = SimpleNamespace(
        thread_id=thread.id,
        thread_domain=thread.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        user_id=actor.id,
        user_domain=actor.origin_domain,
        joined_at=datetime(2026, 8, 24, 12, tzinfo=UTC),
        flags=0,
        notification_level="inherit",
    )
    session = SimpleNamespace(
        get=AsyncMock(
            side_effect=[
                member,
                SimpleNamespace(),
                actor,
                parent,
                member,
            ]
        ),
        flush=AsyncMock(),
        refresh=AsyncMock(),
        commit=AsyncMock(),
    )
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
    monkeypatch.setattr(
        threads_api,
        "get_permissions",
        AsyncMock(return_value=int(Permission.VIEW_CHANNEL)),
    )
    interaction_gate = AsyncMock()
    monkeypatch.setattr(
        threads_api,
        "require_member_interactions_allowed",
        interaction_gate,
    )
    monkeypatch.setattr(threads_api, "queue_guild_mutation", AsyncMock())
    monkeypatch.setattr(threads_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(threads_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(
        threads_api,
        "rich_thread_member_payload",
        AsyncMock(return_value={"user_id": "3"}),
    )

    await threads_api.put_thread_member_service(
        EntityRef("10@guild.example"),
        EntityRef("3@guild.example"),
        threads_api.ThreadMemberUpdate(notification_level="mentions"),
        cast(Any, actor),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
    )

    interaction_gate.assert_not_awaited()
    assert member.notification_level == "mentions"


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
async def test_bot_thread_update_forwards_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = SimpleNamespace(id=99, granted_scopes=[])
    update = AsyncMock(return_value={"id": "10", "origin_domain": "guild.example"})
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(channel(type=11), installation)),
    )
    monkeypatch.setattr(threads_api, "update_thread", update)
    monkeypatch.setattr(
        threads_api,
        "render_bot_thread_result",
        AsyncMock(return_value={"id": "10", "origin_domain": "guild.example"}),
    )

    await threads_api.bot_update_thread(
        EntityRef("10@guild.example"),
        ThreadUpdate(name="renamed"),
        cast(Any, SimpleNamespace(user=SimpleNamespace())),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="guild.example")),
        reason="bot moderation",
    )

    assert update.await_args.args[-1] == "bot moderation"


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
    installation = SimpleNamespace(
        id=99,
        granted_scopes=[],
        granted_intents=["guild_members"],
    )
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
    monkeypatch.setattr(
        threads_api,
        "render_bot_thread_result",
        AsyncMock(return_value={}),
    )

    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        application=SimpleNamespace(id=70, origin_domain="bot.example"),
        scopes=[],
        intents=["guild_members"],
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_intent", "grant_intent"),
    [(False, True), (True, False)],
)
async def test_bot_thread_member_collection_requires_exact_guild_members_intent(
    monkeypatch: pytest.MonkeyPatch,
    worker_intent: bool,
    grant_intent: bool,
) -> None:
    installation = SimpleNamespace(granted_intents=["guild_members"] if grant_intent else [])
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(SimpleNamespace(), installation)),
    )
    member_list = AsyncMock(return_value=[])
    monkeypatch.setattr(threads_api, "list_thread_members_service", member_list)
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        intents=["guild_members"] if worker_intent else [],
    )

    with pytest.raises(HTTPException) as denied:
        await threads_api.bot_list_thread_members(
            EntityRef("10@guild.example"),
            principal,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(domain="guild.example"),
        )

    assert denied.value.detail == {
        "code": "BOT_INTENT_REQUIRED",
        "intent": "guild_members",
    }
    member_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_get_thread_member_does_not_require_collection_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        threads_api,
        "installation_for_channel",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(granted_intents=[]))),
    )
    member_get = AsyncMock(return_value={"user_id": "8"})
    monkeypatch.setattr(threads_api, "get_thread_member_service", member_get)
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        intents=[],
    )

    result = await threads_api.bot_get_thread_member(
        EntityRef("10@guild.example"),
        EntityRef("8@guild.example"),
        principal,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
        with_member=True,
    )

    assert result == {"user_id": "8"}
    assert member_get.await_args.kwargs["with_member"] is True


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
    installation = SimpleNamespace(id=99, granted_scopes=granted_scopes)
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
        application=SimpleNamespace(id=70, origin_domain="bot.example"),
        worker=SimpleNamespace(id=40),
        scopes=granted_scopes,
    )
    ref = EntityRef("10@guild.example")
    session = SimpleNamespace(
        get=AsyncMock(return_value=channel(type=11, encryption_mode="plaintext"))
    )
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
        AsyncMock(
            return_value=(
                thread,
                SimpleNamespace(id=99, granted_scopes=scopes),
            )
        ),
    )
    monkeypatch.setattr(
        threads_api,
        "update_thread",
        AsyncMock(return_value=bot_thread_result(e2ee_required=True)),
    )
    principal = SimpleNamespace(
        user=SimpleNamespace(id=7, origin_domain="bot.example"),
        application=SimpleNamespace(id=70, origin_domain="bot.example"),
        worker=SimpleNamespace(id=40),
        scopes=scopes,
    )

    result = await threads_api.bot_update_thread(
        EntityRef("10@guild.example"),
        ThreadUpdate(name="renamed"),
        principal,
        SimpleNamespace(get=AsyncMock(return_value=thread)),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="guild.example"),
    )

    starter = cast(dict[str, object], result["starter_message"])
    assert starter["content"] is None
    assert starter["attachments"] == []
    assert starter["content_unavailable"] is True
    assert starter["attachments_unavailable"] is True
