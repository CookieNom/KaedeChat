from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import ANY, AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.api.channels as channels_api
import app.tasks as tasks_api
from app.api.channels import (
    ANNOUNCEMENT_DELETED_CONTENT,
    AnnouncementCopyViewProjection,
    apply_announcement_copy_projection,
    apply_announcement_follower_attribution,
    enforce_announcement_publish_limit,
    lock_announcement_publish_mutation,
    sync_announcement_copy_view,
    sync_target_announcement_copy_view,
    target_announcement_copy_view_projection,
)
from app.api.federation import (
    validate_announcement_source_projection,
    validate_announcement_sync_author_profile,
)
from app.chat.announcement_guards import announcement_dependencies_exist
from app.chat.channel_access import ChannelAccess
from app.chat.message_flags import (
    MESSAGE_FLAG_CROSSPOSTED,
    MESSAGE_FLAG_IS_CROSSPOST,
    MESSAGE_FLAG_SOURCE_MESSAGE_DELETED,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
    MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
)
from app.core.types import EntityRef
from app.db.models import (
    Channel,
    ChannelFollow,
    FederatedChannelFollow,
    FederatedMessageCrosspost,
    Guild,
    Message,
    MessageView,
    User,
)
from app.federation.network import FederationNetworkError

FOLLOW_AUTHORIZATION_ID = "kafi_" + "a" * 43
FOLLOW_AUTHORIZATION_EXPIRES_AT = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_crosspost_locks_source_and_target_guilds_in_domain_id_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_guild = Guild(id=30, origin_domain="m.example", name="Source", owner_id=1)
    source_channel = Channel(
        id=31,
        origin_domain="m.example",
        guild_id=30,
        guild_domain="m.example",
        name="announcements",
        type=5,
    )
    targets = [
        Channel(
            id=41,
            origin_domain="m.example",
            guild_id=40,
            guild_domain="z.example",
            name="releases",
            type=0,
        ),
        Channel(
            id=21,
            origin_domain="m.example",
            guild_id=20,
            guild_domain="m.example",
            name="updates",
            type=0,
        ),
        Channel(
            id=11,
            origin_domain="m.example",
            guild_id=10,
            guild_domain="a.example",
            name="external",
            type=0,
        ),
    ]
    # Only guilds owned by this authority may be locked or mutated locally.
    settings = SimpleNamespace(domain="m.example")
    access = ChannelAccess(channel=source_channel, guild=source_guild, participants=[])
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=targets),
        scalar=AsyncMock(return_value=None),
    )
    relock = AsyncMock(return_value=access)
    monkeypatch.setattr(channels_api, "lock_local_channel_mutation", relock)

    assert (
        await lock_announcement_publish_mutation(
            cast(Any, session),
            cast(Any, settings),
            access,
        )
        is access
    )

    guild_lock_sql = [
        str(
            call.args[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for call in session.scalar.await_args_list[1:]
    ]
    assert len(guild_lock_sql) == 2
    assert "guilds.id = 20" in guild_lock_sql[0]
    assert "guilds.id = 30" in guild_lock_sql[1]
    relock.assert_awaited_once_with(session, settings, access)


def test_follow_authorization_id_is_stable_per_generation_and_pair() -> None:
    first = channels_api.announcement_follow_authorization_id(
        (10, "source.example"),
        (20, "target.example"),
        (30, "member.example"),
        1,
    )

    assert first == channels_api.announcement_follow_authorization_id(
        (10, "source.example"),
        (20, "target.example"),
        (30, "member.example"),
        1,
    )
    assert first != channels_api.announcement_follow_authorization_id(
        (10, "source.example"),
        (20, "target.example"),
        (30, "member.example"),
        2,
    )
    assert first.startswith("kafi_") and len(first) == 48


def test_signed_channel_follow_page_is_exact_unique_and_ordered() -> None:
    def follow_payload(
        follow_id: int,
        target_domain: str = "target.example",
    ) -> dict[str, object]:
        return {
            "id": str(follow_id),
            "ref": f"{follow_id}@{target_domain}",
            "source_channel_id": "10",
            "source_channel_domain": "source.example",
            "target_channel_id": "20",
            "target_channel_domain": target_domain,
            "creator_id": "30",
            "creator_domain": "member.example",
            "active": True,
            "federated": True,
            "generation": "1",
            "lifecycle_state": "active",
            "name": None,
            "avatar_hash": None,
            "created_at": "2026-08-29T00:00:00+00:00",
            "updated_at": "2026-08-29T00:00:00+00:00",
        }

    first = follow_payload(40)
    second = follow_payload(41)
    assert channels_api.validate_channel_follow_page(
        [first, second],
        source_ref=(10, "source.example"),
    ) == [first, second]
    same_id_different_authority = [
        follow_payload(42, "alpha.example"),
        follow_payload(42, "beta.example"),
    ]
    assert (
        channels_api.validate_channel_follow_page(
            same_id_different_authority,
            source_ref=(10, "source.example"),
        )
        == same_id_different_authority
    )

    for invalid in (
        [first | {"private": True}],
        [second, first],
        [first, first],
    ):
        with pytest.raises(ValueError):
            channels_api.validate_channel_follow_page(
                invalid,
                source_ref=(10, "source.example"),
            )


@pytest.mark.asyncio
async def test_source_follow_resolution_requires_authority_for_reused_id() -> None:
    def follow(authority: str) -> FederatedChannelFollow:
        return FederatedChannelFollow(
            id=44,
            local_role="source",
            source_channel_id=10,
            source_channel_domain="source.example",
            target_channel_id=20,
            target_channel_domain=authority,
            source_authority_domain="source.example",
            target_authority_domain=authority,
            creator_id=30,
            creator_domain="source.example",
            authority_receipt={},
        )

    alpha = follow("alpha.example")
    beta = follow("beta.example")
    session = SimpleNamespace(
        get=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[alpha, beta]),
    )

    with pytest.raises(HTTPException) as ambiguous:
        await channels_api.source_announcement_follow(
            cast(Any, session),
            (10, "source.example"),
            EntityRef("44"),
            local_domain="source.example",
        )
    assert ambiguous.value.status_code == 409
    assert ambiguous.value.detail["code"] == "CHANNEL_FOLLOW_REF_REQUIRED"

    session.scalars = AsyncMock(return_value=[beta])
    assert (
        await channels_api.source_announcement_follow(
            cast(Any, session),
            (10, "source.example"),
            EntityRef("44@beta.example"),
            local_domain="source.example",
        )
        is beta
    )


@pytest.mark.asyncio
async def test_deletion_guard_covers_follow_sagas_and_pending_or_live_deliveries() -> None:
    session = SimpleNamespace(scalar=AsyncMock(return_value=True))

    assert await announcement_dependencies_exist(
        cast(Any, session),
        {(10, "source.example"), (20, "target.example")},
    )

    statement = session.scalar.await_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql = str(compiled)
    assert "channel_follows.active IS true" in sql
    assert "federated_channel_follows.lifecycle_state IN" in sql
    assert all(f"'{state}'" in sql for state in ("pending", "accepted", "active"))
    assert "message_crossposts" in sql
    assert "federated_message_crossposts.delivery_status IN" in sql
    assert "'retry'" in sql
    assert "federated_message_crossposts.delivery_status = 'delivered'" in sql
    assert "messages.deleted_at IS NULL" in sql


def announcement_message(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": 10,
        "origin_domain": "source.example",
        "channel_id": 20,
        "channel_domain": "source.example",
        "author_id": 30,
        "author_domain": "member.example",
        "content": "release <@40@member.example> @everyone",
        "e2ee": None,
        "embeds": [{"title": "Release"}],
        "components": [],
        "sticker_items": [{"id": "50@source.example", "name": "party", "animated": False}],
        "application_id": None,
        "application_domain": None,
        "view_version": 0,
        "flags": MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_SUPPRESS_NOTIFICATIONS,
        "webhook_id": None,
        "webhook_domain": None,
        "webhook_name": None,
        "webhook_avatar_hash": None,
        "webhook_avatar_url": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_announcement_copy_is_attributed_rich_and_never_notifies_mentions() -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    source = announcement_message(
        webhook_id=77,
        webhook_domain="source.example",
        webhook_name="Source webhook",
        webhook_avatar_hash="b" * 64,
    )
    destination = announcement_message(
        id=99,
        origin_domain="target.example",
        channel_id=88,
        channel_domain="target.example",
        mention_user_refs=[(40, "member.example")],
        forward_snapshot={"content": "must disappear"},
        referenced_message_id=5,
        referenced_message_domain="source.example",
        client_nonce="copy",
        tts=True,
        deleted_at=now,
        edited_at=None,
        webhook_id=44,
        webhook_domain="target.example",
        webhook_name="Target follower",
        webhook_avatar_hash="a" * 64,
    )

    apply_announcement_copy_projection(
        cast(Any, destination),
        cast(Any, source),
        changed_at=now,
        source_deleted=False,
        initial=True,
    )

    assert destination.message_type == 0
    assert destination.flags == MESSAGE_FLAG_IS_CROSSPOST | MESSAGE_FLAG_SUPPRESS_EMBEDS
    assert destination.content == source.content
    assert destination.embeds == source.embeds
    assert destination.sticker_items == source.sticker_items
    assert destination.mention_user_refs == []
    assert destination.forward_snapshot is None
    assert (
        destination.forwarded_message_id,
        destination.forwarded_message_domain,
        destination.forwarded_channel_id,
        destination.forwarded_channel_domain,
    ) == (10, "source.example", 20, "source.example")
    assert destination.edited_at is None
    assert (
        destination.webhook_id,
        destination.webhook_domain,
        destination.webhook_name,
        destination.webhook_avatar_hash,
    ) == (44, "target.example", "Target follower", "a" * 64)


def test_announcement_copy_binds_initial_attribution_to_target_follower() -> None:
    destination = announcement_message(
        id=99,
        origin_domain="target.example",
        channel_id=88,
        channel_domain="target.example",
        webhook_id=None,
        webhook_domain=None,
        webhook_name=None,
        webhook_avatar_hash=None,
        webhook_avatar_url=None,
    )
    follow = SimpleNamespace(
        id=44,
        target_channel_id=88,
        target_channel_domain="target.example",
        name="Release feed",
        avatar_hash="a" * 64,
    )

    apply_announcement_follower_attribution(
        cast(Any, destination),
        cast(Any, follow),
        default_name="Source guild",
    )

    assert (
        destination.webhook_id,
        destination.webhook_domain,
        destination.webhook_name,
        destination.webhook_avatar_hash,
        destination.webhook_avatar_url,
    ) == (44, "target.example", "Release feed", "a" * 64, None)


@pytest.mark.asyncio
async def test_announcement_edit_materializes_and_removes_component_view() -> None:
    destination = announcement_message(
        id=99,
        origin_domain="target.example",
        channel_id=88,
        channel_domain="target.example",
        application_id=80,
        application_domain="apps.example",
        components=[{"type": 1}],
        view_version=3,
    )
    projection = AnnouncementCopyViewProjection(
        application_ref=(80, "apps.example"),
        integration_type="guild_install",
        installation_ref=(70, "apps.example"),
        installation_revision=4,
        version=3,
        persistent=True,
        expires_at=None,
    )
    session = SimpleNamespace(add=Mock(), delete=AsyncMock())

    view = await sync_announcement_copy_view(
        cast(Any, session),
        cast(Any, destination),
        None,
        projection,
    )

    assert view is not None
    assert (view.application_id, view.application_domain) == (80, "apps.example")
    assert (view.installation_id, view.installation_domain) == (70, "apps.example")
    assert view.installation_revision == 4
    session.add.assert_called_once_with(view)

    destination.components = []
    assert (
        await sync_announcement_copy_view(
            cast(Any, session),
            cast(Any, destination),
            view,
            projection,
        )
        is None
    )
    session.delete.assert_awaited_once_with(view)


@pytest.mark.asyncio
async def test_announcement_copy_view_rebinds_to_exact_target_installation() -> None:
    guild = Guild(id=50, origin_domain="target.example", name="Target", owner_id=1)
    source_view = AnnouncementCopyViewProjection(
        application_ref=(80, "app.example"),
        integration_type="guild_install",
        installation_ref=(90, "source.example"),
        installation_revision=3,
        version=4,
        persistent=True,
        expires_at=None,
    )
    installation = SimpleNamespace(
        id=91,
        guild_domain="target.example",
        grant_revision=7,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=installation))

    rebound = await target_announcement_copy_view_projection(
        cast(Any, session),
        guild,
        source_view,
    )

    assert rebound is not None
    assert rebound.application_ref == source_view.application_ref
    assert rebound.installation_ref == (91, "target.example")
    assert rebound.installation_revision == 7
    assert rebound.integration_type == "guild_install"


@pytest.mark.asyncio
async def test_announcement_copy_without_target_install_disables_interactive_lineage() -> None:
    guild = Guild(id=50, origin_domain="target.example", name="Target", owner_id=1)
    destination = announcement_message(
        id=71,
        origin_domain="target.example",
        channel_id=20,
        channel_domain="target.example",
        components=[{"type": 1, "components": [{"type": 2, "custom_id": "deploy"}]}],
        application_id=80,
        application_domain="app.example",
        view_version=4,
    )
    source_view = AnnouncementCopyViewProjection(
        application_ref=(80, "app.example"),
        integration_type="guild_install",
        installation_ref=(90, "source.example"),
        installation_revision=3,
        version=4,
        persistent=True,
        expires_at=None,
    )
    session = SimpleNamespace(scalar=AsyncMock(return_value=None), delete=AsyncMock())

    view = await sync_target_announcement_copy_view(
        cast(Any, session),
        guild,
        cast(Any, destination),
        None,
        source_view,
    )

    assert view is None
    assert destination.components
    assert (destination.application_id, destination.application_domain) == (None, None)
    assert destination.view_version == 0


def test_announcement_source_deletion_is_an_idempotent_retained_receipt() -> None:
    now = datetime(2026, 8, 28, tzinfo=UTC)
    source = announcement_message(content=None, embeds=[], sticker_items=[])
    destination = announcement_message(
        id=99,
        origin_domain="target.example",
        webhook_id=44,
        webhook_domain="target.example",
        webhook_name="Target follower",
    )

    for _ in range(2):
        apply_announcement_copy_projection(
            cast(Any, destination),
            cast(Any, source),
            changed_at=now,
            source_deleted=True,
        )

    assert destination.deleted_at is None
    assert destination.content == ANNOUNCEMENT_DELETED_CONTENT
    assert destination.flags & MESSAGE_FLAG_IS_CROSSPOST
    assert destination.flags & MESSAGE_FLAG_SOURCE_MESSAGE_DELETED
    assert destination.embeds == []
    assert destination.components == []
    assert destination.sticker_items == []
    assert destination.edited_at == now
    assert (destination.webhook_id, destination.webhook_domain) == (44, "target.example")


def test_crosspost_sync_author_profile_is_exactly_bound() -> None:
    profile = {
        "id": "30",
        "origin_domain": "member.example",
        "username": "member",
    }

    validated = validate_announcement_sync_author_profile(
        profile,
        author_ref=(30, "member.example"),
        source_deleted=False,
    )
    assert (int(validated.id), validated.origin_domain) == (30, "member.example")

    with pytest.raises(ValueError, match="substituted"):
        validate_announcement_sync_author_profile(
            {**profile, "id": "31"},
            author_ref=(30, "member.example"),
            source_deleted=False,
        )
    with pytest.raises(ValueError, match="missing"):
        validate_announcement_sync_author_profile(
            None,
            author_ref=(30, "member.example"),
            source_deleted=False,
        )
    assert (
        validate_announcement_sync_author_profile(
            None,
            author_ref=(30, "member.example"),
            source_deleted=True,
        )
        is None
    )


@pytest.mark.asyncio
async def test_channel_follow_add_notice_is_persisted_and_federated_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Target",
        owner_id=99,
        owner_domain="target.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="updates",
        type=0,
    )
    target = ChannelAccess(channel=channel, guild=guild, participants=[])
    actor = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
        account_type="human",
    )
    source = channels_api.AnnouncementFollowSourceProjection(
        source_ref=(10, "source.example"),
        source_guild_ref=(9, "source.example"),
        source_channel_name="announcements",
        target_ref=(20, "target.example"),
        creator_ref=(30, "member.example"),
    )
    added: list[object] = []
    session = SimpleNamespace(add=added.append, flush=AsyncMock())
    queue = AsyncMock()
    dispatch = Mock()
    monkeypatch.setattr(channels_api, "queue_guild_mutation", queue)
    monkeypatch.setattr(channels_api, "queue_postcommit_dispatch", dispatch)
    monkeypatch.setattr(
        channels_api,
        "message_payload",
        lambda message, *_args: {
            "id": str(message.id),
            "message_type": message.message_type,
            "content": message.content,
            "message_reference": message.message_reference,
        },
    )

    rendered = await channels_api.persist_channel_follow_add_message(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="target.example")),
        cast(Any, SimpleNamespace(mint=AsyncMock(return_value=70))),
        target,
        actor,
        source,
    )

    message = next(item for item in added if isinstance(item, Message))
    assert message.message_type == 12
    assert message.content == "announcements"
    assert message.message_reference == {
        "type": 0,
        "channel_id": "10",
        "channel_domain": "source.example",
        "guild_id": "9",
        "guild_domain": "source.example",
    }
    assert rendered == {
        "id": "70",
        "message_type": 12,
        "content": "announcements",
        "message_reference": message.message_reference,
    }
    assert (channel.last_message_id, channel.last_message_domain) == (70, "target.example")
    queue.assert_awaited_once()
    assert queue.await_args.args[5]["message"] == rendered
    dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_target_follow_prepare_uses_remote_owner_without_persisting_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Transferred target",
        owner_id=99,
        owner_domain="owner.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="announcements",
        type=0,
    )
    target = ChannelAccess(channel=channel, guild=guild, participants=[])
    actor = User(
        id=30,
        origin_domain="target.example",
        username="member",
        is_local=True,
    )
    owner = User(
        id=99,
        origin_domain="owner.example",
        username="remote-owner",
        is_local=False,
    )
    added: list[object] = []
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, None]),
        add=added.append,
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=target))
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_webhook_capacity_guild", AsyncMock())
    monkeypatch.setattr(channels_api, "require_webhook_capacity", AsyncMock())
    source_projection = channels_api.AnnouncementFollowSourceProjection(
        source_ref=(10, "source.example"),
        source_guild_ref=(9, "source.example"),
        source_channel_name="updates",
        target_ref=(20, "target.example"),
        creator_ref=(30, "target.example"),
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
    )
    monkeypatch.setattr(
        channels_api,
        "validated_federated_announcement_follow_source_authorization",
        AsyncMock(return_value=source_projection),
    )
    persisted = AsyncMock(return_value={"message_type": 12})
    monkeypatch.setattr(channels_api, "persist_channel_follow_add_message", persisted)
    monkeypatch.setattr(channels_api, "wake_queued_guild_federation", AsyncMock())
    owner_lookup = AsyncMock(return_value=owner)
    monkeypatch.setattr(channels_api, "guild_authority_owner", owner_lookup)
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(channels_api, "build_guild_authority_envelope", signed)
    monkeypatch.setattr(channels_api, "publish_follower_webhook_update", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())

    receipt = await channels_api.authorize_federated_announcement_follow_target(
        session,
        SimpleNamespace(),
        SimpleNamespace(mint=AsyncMock(return_value=44)),
        SimpleNamespace(domain="target.example"),
        actor,
        None,
        channels_api.EntityRef("10@source.example"),
        channels_api.EntityRef("20@target.example"),
        {"signed": "source"},
    )

    assert receipt == {"signed": True}
    owner_lookup.assert_awaited_once_with(session, ANY, guild)
    assert signed.await_args.args[2] is guild
    assert signed.await_args.args[4] is owner
    assert signed.await_args.kwargs["context"] == {
        "guild_id": "50",
        "guild_domain": "target.example",
        "channel_id": "20",
        "channel_domain": "target.example",
    }
    assert signed.await_args.args[5]["source_guild_ref"] == "9@source.example"
    assert signed.await_args.args[5]["source_channel_name"] == "updates"
    prepared = next(item for item in added if isinstance(item, FederatedChannelFollow))
    assert prepared.lifecycle_state == "pending"
    assert prepared.active is False
    assert prepared.notice_message_id is None
    assert prepared.notice_message_domain is None
    persisted.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_federated_follow_retry_does_not_duplicate_type_12_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Target",
        owner_id=99,
        owner_domain="target.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=50,
        guild_domain="target.example",
        name="updates",
        type=0,
    )
    target = ChannelAccess(channel=channel, guild=guild, participants=[])
    actor = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="target",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        generation=1,
        lifecycle_state="active",
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
        active=True,
        authority_receipt={"type": "guild.announcement.follow.finalized"},
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, follow]),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    source_projection = channels_api.AnnouncementFollowSourceProjection(
        source_ref=(10, "source.example"),
        source_guild_ref=(9, "source.example"),
        source_channel_name="announcements",
        target_ref=(20, "target.example"),
        creator_ref=(30, "member.example"),
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
    )
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=target))
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_webhook_capacity_guild", AsyncMock())
    capacity = AsyncMock()
    monkeypatch.setattr(channels_api, "require_webhook_capacity", capacity)
    monkeypatch.setattr(
        channels_api,
        "validated_federated_announcement_follow_source_authorization",
        AsyncMock(return_value=source_projection),
    )
    persisted = AsyncMock()
    monkeypatch.setattr(channels_api, "persist_channel_follow_add_message", persisted)
    monkeypatch.setattr(
        channels_api,
        "guild_authority_owner",
        AsyncMock(return_value=actor),
    )
    signed = AsyncMock(return_value={"type": "guild.announcement.follow.authorized", "fresh": True})
    monkeypatch.setattr(
        channels_api,
        "build_guild_authority_envelope",
        signed,
    )
    monkeypatch.setattr(channels_api, "publish_follower_webhook_update", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())

    receipt = await channels_api.authorize_federated_announcement_follow_target(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(mint=AsyncMock())),
        cast(Any, SimpleNamespace(domain="target.example")),
        actor,
        None,
        channels_api.EntityRef("10@source.example"),
        channels_api.EntityRef("20@target.example"),
        {"signed": "source"},
    )

    capacity.assert_not_awaited()
    persisted.assert_not_awaited()
    assert receipt == {"type": "guild.announcement.follow.authorized", "fresh": True}
    assert signed.await_args.args[3] == "guild.announcement.follow.authorized"


@pytest.mark.asyncio
async def test_target_follow_revocation_uses_remote_owner_after_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Transferred target",
        owner_id=99,
        owner_domain="owner.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="announcements",
        type=0,
    )
    target = ChannelAccess(channel=channel, guild=guild, participants=[])
    actor = User(
        id=30,
        origin_domain="target.example",
        username="member",
        is_local=True,
    )
    owner = User(
        id=99,
        origin_domain="owner.example",
        username="remote-owner",
        is_local=False,
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="target",
        active=True,
        lifecycle_state="active",
        generation=3,
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="target.example",
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
        authority_receipt={
            "type": "guild.announcement.follow.authorized",
            "content": {
                "follow_id": "44",
                "generation": "3",
                "source_channel_ref": "10@source.example",
                "source_guild_ref": "9@source.example",
                "source_channel_name": "updates",
                "target_channel_ref": "20@target.example",
                "creator_ref": "30@target.example",
                "authorization_id": FOLLOW_AUTHORIZATION_ID,
                "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
            },
        },
    )
    session = SimpleNamespace(
        get=AsyncMock(return_value=follow),
        commit=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=target))
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    owner_lookup = AsyncMock(return_value=owner)
    monkeypatch.setattr(channels_api, "guild_authority_owner", owner_lookup)
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(channels_api, "build_guild_authority_envelope", signed)
    monkeypatch.setattr(channels_api, "publish_follower_webhook_update", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_webhook_capacity_guild", AsyncMock())
    monkeypatch.setattr(
        channels_api,
        "detach_announcement_follower_avatar",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(channels_api, "queue_event", AsyncMock())
    monkeypatch.setattr(channels_api, "enqueue_best_effort", AsyncMock())

    receipt = await channels_api.revoke_federated_announcement_follow_target(
        session,
        SimpleNamespace(),
        SimpleNamespace(domain="target.example"),
        actor,
        None,
        44,
        3,
    )

    assert receipt == {"signed": True}
    owner_lookup.assert_awaited_once_with(session, ANY, guild)
    assert follow.active is False and follow.generation == 4
    assert signed.await_args.args[2] is guild
    assert signed.await_args.args[4] is owner
    assert signed.await_args.args[5]["source_guild_ref"] == "9@source.example"
    assert signed.await_args.args[5]["source_channel_name"] == "updates"
    assert signed.await_args.kwargs["context"]["channel_id"] == "20"


@pytest.mark.asyncio
async def test_source_crosspost_sync_uses_remote_owner_after_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = Guild(
        id=50,
        origin_domain="source.example",
        name="Transferred source",
        owner_id=99,
        owner_domain="owner.example",
    )
    owner = User(
        id=99,
        origin_domain="owner.example",
        username="remote-owner",
        is_local=False,
    )
    author = User(
        id=30,
        origin_domain="source.example",
        username="author",
        is_local=True,
    )
    source = announcement_message(
        id=70,
        origin_domain="source.example",
        channel_id=10,
        channel_domain="source.example",
        author_id=author.id,
        author_domain=author.origin_domain,
    )
    receipt = SimpleNamespace(
        follow_id=44,
        follow_authority_domain="target.example",
        source_message_id=source.id,
        source_message_domain=source.origin_domain,
        generation=3,
        delivery_status="delivered",
        source_projection={},
        source_author_profile={},
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="source",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="source.example",
        generation=3,
        active=True,
        authority_receipt={},
    )

    async def get_model(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User:
            return author
        if model is MessageView:
            return None
        if model is FederatedChannelFollow and key == (44, "target.example", "source"):
            return follow
        raise AssertionError(f"unexpected lookup: {model!r} {key!r}")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(side_effect=[[], [], [receipt]]),
        get=get_model,
    )
    owner_lookup = AsyncMock(return_value=owner)
    monkeypatch.setattr(channels_api, "guild_authority_owner", owner_lookup)
    monkeypatch.setattr(
        channels_api,
        "render_message_payload",
        AsyncMock(return_value={"id": "70", "origin_domain": "source.example"}),
    )
    signed = AsyncMock(return_value={"signed": True})
    monkeypatch.setattr(channels_api, "build_guild_authority_envelope", signed)
    queued = AsyncMock()
    monkeypatch.setattr(channels_api, "queue_event", queued)

    effects = await channels_api.propagate_announcement_source_change(
        session,
        SimpleNamespace(domain="source.example"),
        None,
        guild,
        source,
        author,
        source_deleted=False,
        changed_at=datetime.now(UTC),
    )

    assert effects.federation_destinations == {"target.example"}
    owner_lookup.assert_awaited_once_with(session, ANY, guild)
    assert signed.await_args.args[2] is guild
    assert signed.await_args.args[4] is owner
    assert signed.await_args.kwargs["context"] == {
        "guild_id": "50",
        "guild_domain": "source.example",
        "channel_id": "10",
        "channel_domain": "source.example",
    }
    queued.assert_awaited_once()
    assert queued.await_args.args[0] is session
    assert queued.await_args.args[1].domain == "source.example"
    assert queued.await_args.args[2:] == ("target.example", {"signed": True})


@pytest.mark.asyncio
async def test_source_accept_allows_one_generation_reactivation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    actor = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
        account_type="human",
    )
    guild = Guild(
        id=9,
        origin_domain="source.example",
        name="Source",
        owner_id=31,
        owner_domain="source.example",
    )
    channel = Channel(
        id=10,
        origin_domain="source.example",
        guild_id=9,
        guild_domain="source.example",
        name="announcements",
        type=5,
    )
    access = ChannelAccess(channel=channel, guild=guild, participants=[])
    follow = FederatedChannelFollow(
        id=44,
        local_role="source",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=actor.id,
        creator_domain=actor.origin_domain,
        generation=3,
        lifecycle_state="revoked",
        authorization_id="kafi_" + "b" * 43,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
        active=False,
        authority_receipt={},
        created_at=now,
        updated_at=now,
    )
    projection = channels_api.AnnouncementFollowReceiptProjection(
        follow_id=44,
        generation=4,
        source_ref=(10, "source.example"),
        source_guild_ref=(9, "source.example"),
        source_channel_name="announcements",
        target_ref=(20, "target.example"),
        creator_ref=(30, "member.example"),
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User and key == projection.creator_ref:
            return actor
        if model is Guild and key == (9, "source.example"):
            return guild
        if model is FederatedChannelFollow and key == (44, "target.example", "source"):
            return follow
        return None

    refreshed_at = now + timedelta(minutes=1)
    committed = False

    async def refresh(
        value: object,
        *,
        attribute_names: tuple[str, ...] | None = None,
    ) -> None:
        if value is follow:
            assert attribute_names == ("updated_at",)
            follow.updated_at = refreshed_at

    async def commit() -> None:
        nonlocal committed
        committed = True

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=follow),
        flush=AsyncMock(),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
    monkeypatch.setattr(
        channels_api,
        "validated_announcement_follow_receipt",
        AsyncMock(return_value=({"signed": True}, projection)),
    )
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=access))
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    monkeypatch.setattr(channels_api, "guild_authority_owner", AsyncMock(return_value=actor))
    monkeypatch.setattr(
        channels_api,
        "build_guild_authority_envelope",
        AsyncMock(return_value={"type": "guild.announcement.follow.accepted"}),
    )
    queued = AsyncMock()
    monkeypatch.setattr(channels_api, "queue_event", queued)
    monkeypatch.setattr(channels_api, "enqueue_best_effort", AsyncMock())
    original_payload = channels_api.channel_follow_payload

    def render_before_commit(value: ChannelFollow | FederatedChannelFollow) -> dict[str, object]:
        assert not committed
        return original_payload(value)

    monkeypatch.setattr(channels_api, "channel_follow_payload", render_before_commit)

    rendered = await channels_api.accept_federated_announcement_follow_source(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="source.example")),
        {"signed": True},
    )

    assert follow.generation == 4
    assert follow.lifecycle_state == "accepted"
    assert follow.authorization_id == FOLLOW_AUTHORIZATION_ID
    assert rendered["generation"] == "4"
    assert rendered["updated_at"] == refreshed_at.isoformat()
    session.refresh.assert_any_await(follow, attribute_names=("updated_at",))
    queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_local_follow_reactivation_updates_loaded_row_and_renders_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    refreshed_at = now + timedelta(minutes=1)
    actor = User(
        id=30,
        origin_domain="home.example",
        username="member",
        is_local=True,
        account_type="human",
    )
    source_guild = Guild(
        id=9,
        origin_domain="home.example",
        name="Source",
        owner_id=actor.id,
        owner_domain=actor.origin_domain,
    )
    target_guild = Guild(
        id=19,
        origin_domain="home.example",
        name="Target",
        owner_id=actor.id,
        owner_domain=actor.origin_domain,
    )
    source_channel = Channel(
        id=10,
        origin_domain="home.example",
        guild_id=source_guild.id,
        guild_domain=source_guild.origin_domain,
        name="announcements",
        type=5,
        encryption_mode="plaintext",
    )
    target_channel = Channel(
        id=20,
        origin_domain="home.example",
        guild_id=target_guild.id,
        guild_domain=target_guild.origin_domain,
        name="releases",
        type=0,
        encryption_mode="plaintext",
    )
    source = ChannelAccess(channel=source_channel, guild=source_guild, participants=[])
    target = ChannelAccess(channel=target_channel, guild=target_guild, participants=[])
    original_created_at = now - timedelta(days=1)
    follow = ChannelFollow(
        id=44,
        source_channel_id=source_channel.id,
        source_channel_domain=source_channel.origin_domain,
        target_channel_id=target_channel.id,
        target_channel_domain=target_channel.origin_domain,
        creator_id=31,
        creator_domain="home.example",
        active=False,
        created_at=original_created_at,
        updated_at=now,
    )
    committed = False

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Guild:
            return source_guild if key == (9, "home.example") else target_guild
        if model is ChannelFollow:
            return follow
        return None

    async def refresh(
        value: object,
        *,
        attribute_names: tuple[str, ...] | None = None,
    ) -> None:
        if value is follow:
            assert attribute_names == ("updated_at",)
            follow.updated_at = refreshed_at

    async def commit() -> None:
        nonlocal committed
        committed = True

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(return_value=follow),
        add=Mock(),
        flush=AsyncMock(),
        refresh=AsyncMock(side_effect=refresh),
        commit=AsyncMock(side_effect=commit),
    )
    monkeypatch.setattr(
        channels_api,
        "load_channel_access",
        AsyncMock(side_effect=[source, target]),
    )
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(channels_api, "require_channel_permissions", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    monkeypatch.setattr(channels_api, "lock_webhook_capacity_guild", AsyncMock())
    monkeypatch.setattr(channels_api, "require_webhook_capacity", AsyncMock())
    monkeypatch.setattr(channels_api, "persist_channel_follow_add_message", AsyncMock())
    monkeypatch.setattr(channels_api, "wake_queued_guild_federation", AsyncMock())
    monkeypatch.setattr(channels_api, "publish_follower_webhook_update", AsyncMock())
    original_payload = channels_api.channel_follow_payload

    def render_before_commit(value: ChannelFollow | FederatedChannelFollow) -> dict[str, object]:
        assert not committed
        return original_payload(value)

    monkeypatch.setattr(channels_api, "channel_follow_payload", render_before_commit)
    snowflake = SimpleNamespace(mint=AsyncMock())

    rendered = await channels_api.follow_announcement_channel(
        EntityRef("10@home.example"),
        channels_api.ChannelFollowCreate(target_channel_id="20@home.example"),
        cast(Any, SimpleNamespace(user=actor)),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, snowflake),
        cast(Any, SimpleNamespace(domain="home.example")),
    )

    assert rendered["id"] == "44"
    assert rendered["active"] is True
    assert rendered["creator_id"] == "30"
    assert rendered["updated_at"] == refreshed_at.isoformat()
    assert follow.created_at == original_created_at
    snowflake.mint.assert_not_awaited()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_pending_source_follow_accepts_target_revocation_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_channel = Channel(
        id=10,
        origin_domain="source.example",
        guild_id=9,
        guild_domain="source.example",
        name="announcements",
        type=5,
    )
    source_guild = Guild(
        id=9,
        origin_domain="source.example",
        name="Source",
        owner_id=31,
        owner_domain="source.example",
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="source",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="member.example",
        generation=3,
        lifecycle_state="accepted",
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
        active=False,
        authority_receipt={},
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel and key == (10, "source.example"):
            return source_channel
        if model is Guild and key == (9, "source.example"):
            return source_guild
        if model is FederatedChannelFollow and key == (44, "target.example", "source"):
            return follow
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    event_content = {
        "follow_id": "44",
        "generation": "4",
        "source_channel_ref": "10@source.example",
        "source_guild_ref": "9@source.example",
        "source_channel_name": "announcements",
        "target_channel_ref": "20@target.example",
        "creator_ref": "30@member.example",
        "authorization_id": FOLLOW_AUTHORIZATION_ID,
        "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
    }

    await channels_api.apply_announcement_follow_lifecycle_event(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="source.example")),
        event_type="guild.announcement.follow.revoked",
        event_origin="target.example",
        event_timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
        event_content=event_content,
        event_context={
            "guild_id": "50",
            "guild_domain": "target.example",
            "channel_id": "20",
            "channel_domain": "target.example",
        },
        raw_envelope={"type": "guild.announcement.follow.revoked"},
    )

    assert follow.generation == 4
    assert follow.lifecycle_state == "revoked"
    assert follow.active is False


@pytest.mark.asyncio
async def test_orphaned_target_rejection_creates_source_generation_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_channel = Channel(
        id=10,
        origin_domain="source.example",
        guild_id=9,
        guild_domain="source.example",
        name="announcements",
        type=5,
    )
    source_guild = Guild(
        id=9,
        origin_domain="source.example",
        name="Source",
        owner_id=31,
        owner_domain="source.example",
    )
    creator = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )
    added: list[object] = []

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Channel and key == (10, "source.example"):
            return source_channel
        if model is Guild and key == (9, "source.example"):
            return source_guild
        if model is User and key == (30, "member.example"):
            return creator
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get), add=added.append)
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    content = {
        "follow_id": "44",
        "generation": "1",
        "source_channel_ref": "10@source.example",
        "source_guild_ref": "9@source.example",
        "source_channel_name": "announcements",
        "target_channel_ref": "20@target.example",
        "creator_ref": "30@member.example",
        "authorization_id": FOLLOW_AUTHORIZATION_ID,
        "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
    }
    envelope = {"type": "guild.announcement.follow.rejected", "content": content}

    wakes = await channels_api.apply_announcement_follow_lifecycle_event(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="source.example")),
        event_type="guild.announcement.follow.rejected",
        event_origin="target.example",
        event_timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
        event_content=content,
        event_context={
            "guild_id": "50",
            "guild_domain": "target.example",
            "channel_id": "20",
            "channel_domain": "target.example",
        },
        raw_envelope=envelope,
    )

    tombstone = next(item for item in added if isinstance(item, FederatedChannelFollow))
    assert wakes == set()
    assert tombstone.local_role == "source"
    assert tombstone.lifecycle_state == "revoked"
    assert tombstone.generation == 1
    assert tombstone.authorization_id == FOLLOW_AUTHORIZATION_ID
    assert tombstone.authority_receipt == envelope


@pytest.mark.asyncio
async def test_target_finalize_rejection_is_retained_for_idempotent_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
        account_type="human",
    )
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Target",
        owner_id=99,
        owner_domain="target.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=50,
        guild_domain="target.example",
        name="updates",
        type=0,
    )
    target = ChannelAccess(channel=channel, guild=guild, participants=[])
    follow = FederatedChannelFollow(
        id=44,
        local_role="target",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="member.example",
        generation=1,
        lifecycle_state="pending",
        authorization_id=FOLLOW_AUTHORIZATION_ID,
        authorization_expires_at=FOLLOW_AUTHORIZATION_EXPIRES_AT,
        active=False,
        authority_receipt={},
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User and key == (30, "member.example"):
            return actor
        if model is FederatedChannelFollow and key == (44, "target.example", "target"):
            return follow
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get), refresh=AsyncMock())
    monkeypatch.setattr(channels_api, "lock_announcement_mutation", AsyncMock())
    monkeypatch.setattr(channels_api, "load_channel_access", AsyncMock(return_value=target))
    monkeypatch.setattr(channels_api, "lock_webhook_capacity_guild", AsyncMock())
    monkeypatch.setattr(channels_api, "require_announcement_actor_scope", AsyncMock())
    monkeypatch.setattr(
        channels_api,
        "require_channel_permissions",
        AsyncMock(
            side_effect=HTTPException(
                status_code=403,
                detail={"code": "MISSING_PERMISSIONS"},
            )
        ),
    )
    monkeypatch.setattr(channels_api, "guild_authority_owner", AsyncMock(return_value=actor))
    rejected = {
        "type": "guild.announcement.follow.rejected",
        "content": {"follow_id": "44"},
    }
    monkeypatch.setattr(
        channels_api,
        "build_guild_authority_envelope",
        AsyncMock(return_value=rejected),
    )
    queued = AsyncMock()
    monkeypatch.setattr(channels_api, "queue_event", queued)
    content = {
        "follow_id": "44",
        "generation": "1",
        "source_channel_ref": "10@source.example",
        "source_guild_ref": "9@source.example",
        "source_channel_name": "announcements",
        "target_channel_ref": "20@target.example",
        "creator_ref": "30@member.example",
        "authorization_id": FOLLOW_AUTHORIZATION_ID,
        "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
    }

    wakes = await channels_api.apply_announcement_follow_lifecycle_event(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
        event_type="guild.announcement.follow.accepted",
        event_origin="source.example",
        event_timestamp_ms=int(datetime.now(UTC).timestamp() * 1000),
        event_content=content,
        event_context={
            "guild_id": "9",
            "guild_domain": "source.example",
            "channel_id": "10",
            "channel_domain": "source.example",
        },
        raw_envelope={"type": "guild.announcement.follow.accepted"},
    )

    assert wakes == {"source.example"}
    assert follow.lifecycle_state == "revoked"
    assert follow.authority_receipt == rejected
    queued.assert_awaited_once()


@pytest.mark.asyncio
async def test_crosspost_delivery_routes_reused_follow_id_by_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    author = User(
        id=30,
        origin_domain="source.example",
        username="author",
        is_local=True,
    )
    source = announcement_message(
        id=70,
        origin_domain="source.example",
        channel_id=10,
        channel_domain="source.example",
        author_id=author.id,
        author_domain=author.origin_domain,
        published_at=now,
        deleted_at=None,
    )

    def follow(authority: str, channel_id: int) -> FederatedChannelFollow:
        return FederatedChannelFollow(
            id=44,
            local_role="source",
            source_channel_id=10,
            source_channel_domain="source.example",
            target_channel_id=channel_id,
            target_channel_domain=authority,
            source_authority_domain="source.example",
            target_authority_domain=authority,
            creator_id=author.id,
            creator_domain=author.origin_domain,
            generation=1,
            lifecycle_state="active",
            active=True,
            authority_receipt={},
        )

    def receipt(authority: str) -> FederatedMessageCrosspost:
        return FederatedMessageCrosspost(
            source_message_id=source.id,
            source_message_domain=source.origin_domain,
            follow_id=44,
            follow_authority_domain=authority,
            local_role="source",
            generation=1,
            delivery_status="pending",
            attempts=0,
            next_retry_at=now,
            source_projection={"id": "70"},
            source_author_profile={"id": "30"},
            published_at=now,
        )

    alpha_follow = follow("alpha.example", 20)
    beta_follow = follow("beta.example", 21)
    alpha_receipt = receipt("alpha.example")
    beta_receipt = receipt("beta.example")
    follows = {
        (44, "alpha.example", "source"): alpha_follow,
        (44, "beta.example", "source"): beta_follow,
    }
    receipts = {
        (70, "source.example", 44, "alpha.example", "source"): alpha_receipt,
        (70, "source.example", 44, "beta.example", "source"): beta_receipt,
    }

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is FederatedMessageCrosspost:
            return receipts.get(cast(tuple[Any, ...], key))
        if model is FederatedChannelFollow:
            return follows.get(cast(tuple[Any, ...], key))
        if model is Message and key == (70, "source.example"):
            return source
        if model is User and key == (30, "source.example"):
            return author
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(),
        get=AsyncMock(side_effect=get),
        commit=AsyncMock(),
    )
    rendered_source = {
        "id": "70",
        "origin_domain": "source.example",
    }
    monkeypatch.setattr(
        channels_api,
        "render_message_payload",
        AsyncMock(return_value=rendered_source),
    )
    signed = AsyncMock(
        return_value=httpx.Response(
            201,
            json={
                "destination_message_ref": "90@beta.example",
                "message": {
                    "id": "90",
                    "origin_domain": "beta.example",
                    "channel_id": "21",
                    "channel_domain": "beta.example",
                    "forwarded_message_id": "70",
                    "forwarded_message_domain": "source.example",
                    "forwarded_channel_id": "10",
                    "forwarded_channel_domain": "source.example",
                },
            },
        )
    )
    monkeypatch.setattr(channels_api, "signed_request", signed)

    status, wake = await channels_api.deliver_federated_announcement_crosspost_job(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="source.example")),
        source_message_id=70,
        source_message_domain="source.example",
        follow_id=44,
        follow_authority_domain="beta.example",
        now=now,
    )

    assert (status, wake) == ("delivered", None)
    assert (beta_receipt.destination_message_id, beta_receipt.destination_message_domain) == (
        90,
        "beta.example",
    )
    assert beta_receipt.attempts == 1
    assert alpha_receipt.delivery_status == "pending"
    assert alpha_receipt.attempts == 0
    assert signed.await_args.args[3:5] == (
        "beta.example",
        "/_kaede/v1/channels/21/announcement-crossposts",
    )


@pytest.mark.asyncio
async def test_legacy_crosspost_task_resolves_only_one_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(scalars=AsyncMock(return_value=["target.example"]))

    class SessionContext:
        async def __aenter__(self) -> object:
            return session

        async def __aexit__(self, *_args: object) -> None:
            return None

    engine = SimpleNamespace(dispose=AsyncMock())
    settings = SimpleNamespace(
        domain="source.example",
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
    )
    monkeypatch.setattr(tasks_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tasks_api,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: SessionContext()),
    )
    delivery = AsyncMock(return_value=("delivered", None))
    monkeypatch.setattr(
        channels_api,
        "deliver_federated_announcement_crosspost_job",
        delivery,
    )
    task_function = tasks_api.announcement_crosspost_deliver.original_func.__wrapped__

    assert await task_function(70, "source.example", 44) == 1
    assert delivery.await_args.kwargs["follow_authority_domain"] == "target.example"

    session.scalars = AsyncMock(return_value=["alpha.example", "beta.example"])
    assert await task_function(70, "source.example", 44) == 0
    assert delivery.await_count == 1


@pytest.mark.asyncio
async def test_dead_follower_delivery_becomes_terminal_without_blocking_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, 12, tzinfo=UTC)
    author = User(
        id=30,
        origin_domain="source.example",
        username="author",
        is_local=True,
    )
    source = announcement_message(
        id=70,
        origin_domain="source.example",
        channel_id=10,
        channel_domain="source.example",
        author_id=30,
        author_domain="source.example",
        published_at=now,
        deleted_at=None,
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="source",
        source_channel_id=10,
        source_channel_domain="source.example",
        target_channel_id=20,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="source.example",
        generation=1,
        lifecycle_state="active",
        active=True,
        authority_receipt={},
    )
    receipt = FederatedMessageCrosspost(
        source_message_id=70,
        source_message_domain="source.example",
        follow_id=44,
        follow_authority_domain="target.example",
        local_role="source",
        generation=1,
        delivery_status="retry",
        attempts=11,
        next_retry_at=now,
        source_projection={"id": "70"},
        source_author_profile={"id": "30"},
        published_at=now,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is FederatedMessageCrosspost:
            return receipt
        if model is FederatedChannelFollow:
            return follow
        if model is Message:
            return source
        if model is User:
            return author
        return None

    session = SimpleNamespace(
        scalar=AsyncMock(),
        get=AsyncMock(side_effect=get),
        commit=AsyncMock(),
    )
    monkeypatch.setattr(
        channels_api,
        "render_message_payload",
        AsyncMock(return_value={"id": "70", "origin_domain": "source.example"}),
    )
    monkeypatch.setattr(
        channels_api,
        "signed_request",
        AsyncMock(side_effect=FederationNetworkError("offline")),
    )

    status, wake = await channels_api.deliver_federated_announcement_crosspost_job(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="source.example")),
        source_message_id=70,
        source_message_domain="source.example",
        follow_id=44,
        follow_authority_domain="target.example",
        now=now,
    )

    assert (status, wake) == ("terminal", None)
    assert receipt.attempts == 12
    assert receipt.delivery_status == "terminal"
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_announcement_publish_limit_rejects_duplicate_publish() -> None:
    now = datetime(2026, 8, 28, 12, tzinfo=UTC)
    oldest = now - timedelta(minutes=30)
    result = SimpleNamespace(one=lambda: (10, oldest))
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    source = announcement_message(published_at=None, flags=0)

    with pytest.raises(HTTPException) as limited:
        await enforce_announcement_publish_limit(
            cast(Any, session),
            cast(Any, source),
            now=now,
        )
    assert limited.value.status_code == 429
    assert limited.value.headers == {"Retry-After": "1801"}
    assert source.published_at is None

    source.flags = MESSAGE_FLAG_CROSSPOSTED
    with pytest.raises(HTTPException) as duplicate:
        await enforce_announcement_publish_limit(
            cast(Any, session),
            cast(Any, source),
            now=now,
        )
    assert duplicate.value.status_code == 400
    assert duplicate.value.detail["code"] == "MESSAGE_ALREADY_CROSSPOSTED"
    session.execute.assert_awaited_once()


def test_signed_announcement_source_projection_rejects_user_forwarding_and_polls() -> None:
    created_at = datetime(2026, 8, 28, tzinfo=UTC)
    raw: dict[str, object] = {
        "id": "10",
        "origin_domain": "source.example",
        "channel_id": "20",
        "channel_domain": "source.example",
        "author_id": "30",
        "author_domain": "member.example",
        "created_at": created_at.isoformat(),
        "deleted_at": None,
        "content": "release",
        "e2ee": None,
        "message_type": 0,
        "flags": 0,
        "attachments": [],
        "embeds": [{"title": "Release"}],
        "components": [],
        "sticker_items": [],
        "application_id": None,
        "application_domain": None,
        "view_version": 0,
        "forwarded_message_id": None,
        "forwarded_message_domain": None,
        "forwarded_channel_id": None,
        "forwarded_channel_domain": None,
        "forward_snapshot": None,
        "referenced_message_id": None,
        "poll": None,
    }

    validated = validate_announcement_source_projection(
        cast(dict[str, Any], raw),
        source_message_ref=(10, "source.example"),
        source_channel_ref=(20, "source.example"),
        author_ref=(30, "member.example"),
    )
    assert validated.message.content == "release"
    assert validated.message.embeds[0]["title"] == "Release"

    with pytest.raises(ValueError, match="announcement source projection"):
        validate_announcement_source_projection(
            cast(dict[str, Any], raw | {"forwarded_message_id": "9"}),
            source_message_ref=(10, "source.example"),
            source_channel_ref=(20, "source.example"),
            author_ref=(30, "member.example"),
        )
    with pytest.raises(ValueError, match="announcement source projection"):
        validate_announcement_source_projection(
            cast(dict[str, Any], raw | {"poll": {"question": {"text": "No"}}}),
            source_message_ref=(10, "source.example"),
            source_channel_ref=(20, "source.example"),
            author_ref=(30, "member.example"),
        )
