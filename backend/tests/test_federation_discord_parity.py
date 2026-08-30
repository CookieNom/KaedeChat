from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import app.api.channels as channels_api
from app.api.channels import (
    MessageMutationOptions,
    _announcement_receipt_content,
    _announcement_source_authorization_content,
    require_announcement_actor_scope,
    require_available_edit_attachment,
    validated_announcement_follow_receipt,
    validated_federated_announcement_follow_source_authorization,
)
from app.api.federation import (
    _announcement_federation_actor,
    federation_authorize_announcement_follow,
    federation_authorize_announcement_follow_source,
    federation_deliver_announcement_crosspost,
)
from app.api.interactions import (
    FederatedUserInstallationGrant,
    InteractionCreate,
    materialize_federated_user_installation,
)
from app.chat.channel_access import ChannelAccess
from app.chat.message_flags import MESSAGE_FLAG_IS_CROSSPOST
from app.chat.schemas import MessageEdit
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.core.types import EntityRef
from app.db.bot_models import BotApplication, BotUserInstallation
from app.db.models import (
    Channel,
    FederatedChannelFollow,
    FederatedMessageCrosspost,
    Guild,
    Message,
    User,
)
from app.federation.dm_history import validate_dm_history_page
from app.federation.guilds import _validated_message_rich_projection
from app.federation.schemas import (
    AnnouncementCrosspostDeliverRequest,
    AnnouncementFollowAuthorizeRequest,
    AnnouncementFollowSourceAuthorizeRequest,
    GuildMessageOperationRequest,
    RemoteUserProfile,
)
from app.federation.security import FederationPrincipal

FOLLOW_AUTHORIZATION_ID = "kafi_" + "a" * 43
FOLLOW_AUTHORIZATION_EXPIRES_AT = datetime(2099, 1, 1, tzinfo=UTC)


def test_federated_edit_attachment_transport_requires_qualified_matching_refs() -> None:
    payload = {
        "operation": "message.edit",
        "actor": {
            "id": "5",
            "origin_domain": "member.example",
            "username": "member",
        },
        "channel_id": "8",
        "message_id": "9@guild.example",
        "edit": {"content": "updated", "attachment_ids": ["10", "11"]},
        "attachment_refs": ["10@member.example", "11@guild.example"],
        "attachments": [{"id": "10", "origin_domain": "member.example"}],
    }

    parsed = GuildMessageOperationRequest.model_validate(payload)
    assert [str(item) for item in parsed.attachment_refs] == [
        "10@member.example",
        "11@guild.example",
    ]
    with pytest.raises(ValidationError, match="qualified"):
        GuildMessageOperationRequest.model_validate(
            payload | {"attachment_refs": ["10", "11@guild.example"]}
        )
    with pytest.raises(ValidationError, match="do not match"):
        GuildMessageOperationRequest.model_validate(
            payload | {"attachment_refs": ["11@member.example", "10@guild.example"]}
        )
    with pytest.raises(ValidationError, match="unique subset"):
        GuildMessageOperationRequest.model_validate(
            payload | {"attachments": [{"id": "12", "origin_domain": "attacker.example"}]}
        )


def test_message_edit_rechecks_upload_ticket_channel_capability() -> None:
    attachment = SimpleNamespace(
        upload_channel_id=7,
        upload_channel_domain="guild.example",
    )
    channel = SimpleNamespace(
        id=8,
        origin_domain="guild.example",
        encryption_mode="plaintext",
    )

    with pytest.raises(HTTPException) as raised:
        require_available_edit_attachment(
            cast(Any, attachment),
            cast(Any, channel),
            MessageMutationOptions(),
        )
    assert raised.value.detail == {"code": "ATTACHMENT_NOT_FOUND"}


@pytest.mark.asyncio
async def test_remote_edit_transports_retained_refs_and_only_new_attachment_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = SimpleNamespace(id=9, origin_domain="guild.example", deleted_at=None)
    retained = SimpleNamespace(id=10, origin_domain="member.example")
    uploaded = SimpleNamespace(
        id=11,
        origin_domain="member.example",
        upload_channel_id=8,
        upload_channel_domain="guild.example",
        bot_installation_id=None,
        bot_user_installation_id=None,
        bot_dm_capability_id=None,
        message_id=None,
        message_domain=None,
        interaction_id=None,
        interaction_response_id=None,
        asset_binding=None,
        report_id=None,
        encryption_mode="plaintext",
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=message),
        scalars=AsyncMock(return_value=[retained]),
        commit=AsyncMock(),
    )
    access = ChannelAccess(
        channel=cast(
            Any,
            SimpleNamespace(
                id=8,
                origin_domain="guild.example",
                encryption_mode="plaintext",
            ),
        ),
        guild=cast(Any, SimpleNamespace(id=7, origin_domain="guild.example")),
        participants=[],
    )
    finalize = AsyncMock(return_value=uploaded)
    lock = AsyncMock()
    record = AsyncMock()
    enqueue = AsyncMock()
    monkeypatch.setattr(channels_api, "finalize_attachment", finalize)
    monkeypatch.setattr(channels_api, "lock_media_tombstone_ref", lock)
    monkeypatch.setattr(channels_api, "record_attachment_recipients", record)
    monkeypatch.setattr(channels_api, "enqueue_best_effort", enqueue)
    monkeypatch.setattr(
        channels_api,
        "attachment_payload",
        lambda attachment: {
            "id": str(attachment.id),
            "origin_domain": attachment.origin_domain,
            "filename": "new.png",
        },
    )

    transport = await channels_api.prepare_federated_edit_attachments(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain="member.example")),
        access,
        cast(Any, SimpleNamespace(id=5, origin_domain="member.example")),
        EntityRef("9@guild.example"),
        MessageEdit(content="updated", attachment_ids=["10", "11"]),
        MessageMutationOptions(),
        destination="guild.example",
    )

    assert transport.refs == ((10, "member.example"), (11, "member.example"))
    assert transport.attachments == (
        {"id": "11", "origin_domain": "member.example", "filename": "new.png"},
    )
    finalize.assert_awaited_once()
    record.assert_awaited_once_with(
        cast(Any, session),
        {(11, "member.example")},
        "guild.example",
        room_ref=("guild", 7, "guild.example"),
    )
    session.commit.assert_awaited_once()
    enqueue.assert_awaited_once()


def test_cross_authority_announcement_projection_allows_published_content() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    raw = {
        "content": None,
        "e2ee": None,
        "attachments": [],
        "embeds": [],
        "components": [],
        "poll": None,
        "forwarded_message_id": "90",
        "forwarded_message_domain": "source.example",
        "forwarded_channel_id": "80",
        "forwarded_channel_domain": "source.example",
    }

    projection = _validated_message_rich_projection(
        raw,
        message_id=100,
        message_origin="target.example",
        message_created_at=created_at,
        e2ee=None,
        message_type=0,
        flags=MESSAGE_FLAG_IS_CROSSPOST,
    )

    assert projection["forwarded_ref"] == (90, "source.example")
    with pytest.raises(ValueError, match="immutable snapshot"):
        _validated_message_rich_projection(
            raw,
            message_id=100,
            message_origin="target.example",
            message_created_at=created_at,
            e2ee=None,
            message_type=19,
        )
    published = _validated_message_rich_projection(
        raw | {"content": "published content"},
        message_id=100,
        message_origin="target.example",
        message_created_at=created_at,
        e2ee=None,
        message_type=0,
        flags=MESSAGE_FLAG_IS_CROSSPOST,
    )
    assert published["forwarded_ref"] == (90, "source.example")


def test_federated_follow_receipt_binds_pair_creator_and_generation() -> None:
    parsed = _announcement_receipt_content(
        "guild.announcement.follow.authorized",
        {
            "follow_id": "44",
            "generation": "3",
            "source_channel_ref": "10@source.example",
            "source_guild_ref": "9@source.example",
            "source_channel_name": "announcements",
            "target_channel_ref": "20@target.example",
            "creator_ref": "30@member.example",
            "authorization_id": FOLLOW_AUTHORIZATION_ID,
            "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
        },
    )

    assert parsed.follow_id == 44
    assert parsed.generation == 3
    assert parsed.source_ref == (10, "source.example")
    assert parsed.source_guild_ref == (9, "source.example")
    assert parsed.source_channel_name == "announcements"
    assert parsed.target_ref == (20, "target.example")
    assert parsed.creator_ref == (30, "member.example")
    with pytest.raises(ValueError, match="wrong type"):
        _announcement_receipt_content("guild.message.create", {})
    for forged in (
        {
            "follow_id": "044",
            "generation": "3",
            "source_channel_ref": "10@source.example",
            "source_guild_ref": "9@source.example",
            "source_channel_name": "announcements",
            "target_channel_ref": "20@target.example",
            "creator_ref": "30@member.example",
            "authorization_id": FOLLOW_AUTHORIZATION_ID,
            "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
        },
        {
            "follow_id": "44",
            "generation": "3",
            "source_channel_ref": "10@source.example",
            "source_guild_ref": "9@source.example",
            "source_channel_name": "announcements",
            "target_channel_ref": "20@target.example",
            "creator_ref": "30@member.example",
            "authorization_id": FOLLOW_AUTHORIZATION_ID,
            "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
            "extra": True,
        },
    ):
        with pytest.raises(ValueError, match="malformed"):
            _announcement_receipt_content(
                "guild.announcement.follow.authorized",
                forged,
            )


def test_federated_follow_source_authorization_binds_display_and_guild() -> None:
    parsed = _announcement_source_authorization_content(
        {
            "source_channel_ref": "10@source.example",
            "source_guild_ref": "9@source.example",
            "source_channel_name": "announcements",
            "target_channel_ref": "20@target.example",
            "creator_ref": "30@member.example",
            "generation": "1",
            "authorization_id": FOLLOW_AUTHORIZATION_ID,
            "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
        }
    )

    assert parsed.source_ref == (10, "source.example")
    assert parsed.source_guild_ref == (9, "source.example")
    assert parsed.source_channel_name == "announcements"
    assert parsed.generation == 1
    with pytest.raises(ValueError, match="metadata"):
        _announcement_source_authorization_content(
            {
                "source_channel_ref": "10@source.example",
                "source_guild_ref": "9@forged.example",
                "source_channel_name": "announcements",
                "target_channel_ref": "20@target.example",
                "creator_ref": "30@member.example",
                "generation": "1",
                "authorization_id": FOLLOW_AUTHORIZATION_ID,
                "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
            }
        )


@pytest.mark.asyncio
async def test_follow_source_authorization_rejects_forged_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = SimpleNamespace(
        type="guild.announcement.follow.source_authorized",
        context={
            "guild_id": "9",
            "guild_domain": "source.example",
            "channel_id": "10",
            "channel_domain": "source.example",
        },
        content={
            "source_channel_ref": "10@source.example",
            "source_guild_ref": "9@source.example",
            "source_channel_name": "announcements",
            "target_channel_ref": "20@target.example",
            "creator_ref": "30@member.example",
            "generation": "1",
            "authorization_id": FOLLOW_AUTHORIZATION_ID,
            "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
        },
    )
    validated = AsyncMock(return_value=envelope)
    monkeypatch.setattr(channels_api, "validated_event_envelope", validated)
    actor = User(
        id=30,
        origin_domain="member.example",
        username="member",
        is_local=False,
    )

    projection = await validated_federated_announcement_follow_source_authorization(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
        actor,
        (10, "source.example"),
        (20, "target.example"),
        {"origin": "source.example"},
    )
    assert projection.source_channel_name == "announcements"
    assert validated.await_args.kwargs["allow_authority_attested_actor"] is True

    envelope.content["target_channel_ref"] = "21@target.example"
    with pytest.raises(HTTPException) as forged:
        await validated_federated_announcement_follow_source_authorization(
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="target.example")),
            actor,
            (10, "source.example"),
            (20, "target.example"),
            {"origin": "source.example"},
        )
    assert forged.value.detail == {"code": "ANNOUNCEMENT_SOURCE_AUTHORIZATION_INVALID"}


@pytest.mark.asyncio
async def test_follow_receipt_accepts_remote_owner_and_rejects_forged_target_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = {
        "follow_id": "44",
        "generation": "3",
        "source_channel_ref": "10@source.example",
        "source_guild_ref": "9@source.example",
        "source_channel_name": "announcements",
        "target_channel_ref": "20@target.example",
        "creator_ref": "30@member.example",
        "authorization_id": FOLLOW_AUTHORIZATION_ID,
        "authorization_expires_at": FOLLOW_AUTHORIZATION_EXPIRES_AT.isoformat(),
    }
    envelope = SimpleNamespace(
        type="guild.announcement.follow.authorized",
        actor=SimpleNamespace(id="99", domain="owner.example"),
        context={
            "guild_id": "50",
            "guild_domain": "target.example",
            "channel_id": "20",
            "channel_domain": "target.example",
        },
        content=content,
        model_dump=lambda **_kwargs: {"signed": True},
    )
    validated = AsyncMock(return_value=envelope)
    monkeypatch.setattr(channels_api, "validated_event_envelope", validated)
    settings = SimpleNamespace(domain="source.example")

    receipt = await validated_announcement_follow_receipt(
        SimpleNamespace(),
        settings,
        {"origin": "target.example"},
        expected_type="guild.announcement.follow.authorized",
    )

    assert receipt[1].follow_id == 44
    assert receipt[1].generation == 3
    assert receipt[1].source_ref == (10, "source.example")
    assert receipt[1].source_guild_ref == (9, "source.example")
    assert receipt[1].source_channel_name == "announcements"
    assert receipt[1].target_ref == (20, "target.example")
    assert receipt[1].creator_ref == (30, "member.example")
    assert validated.await_args.kwargs["allow_authority_attested_actor"] is True

    envelope.context["channel_id"] = "21"
    with pytest.raises(HTTPException) as forged:
        await validated_announcement_follow_receipt(
            SimpleNamespace(),
            settings,
            {"origin": "target.example"},
            expected_type="guild.announcement.follow.authorized",
        )
    assert forged.value.detail == {"code": "ANNOUNCEMENT_FOLLOW_RECEIPT_INVALID"}


def test_federated_follow_schema_does_not_fake_remote_foreign_keys() -> None:
    assert not FederatedChannelFollow.__table__.foreign_keys
    assert [column.name for column in FederatedChannelFollow.__table__.primary_key] == [
        "id",
        "target_authority_domain",
        "local_role",
    ]
    assert [column.name for column in FederatedMessageCrosspost.__table__.primary_key] == [
        "source_message_id",
        "source_message_domain",
        "follow_id",
        "follow_authority_domain",
        "local_role",
    ]
    crosspost_targets = {
        foreign_key.target_fullname
        for foreign_key in FederatedMessageCrosspost.__table__.foreign_keys
    }
    assert crosspost_targets == {
        "federated_channel_follows.id",
        "federated_channel_follows.local_role",
        "federated_channel_follows.target_authority_domain",
    }


def test_signed_follow_request_attests_public_bot_application_without_a_token() -> None:
    request = AnnouncementFollowAuthorizeRequest.model_validate(
        {
            "actor": {
                "id": "60",
                "origin_domain": "apps.example",
                "username": "helper.bot",
            },
            "actor_application_ref": "80@apps.example",
            "source_channel_ref": "10@source.example",
            "target_channel_id": "20",
        }
    )

    encoded = request.model_dump(mode="json", exclude_none=True)
    assert encoded["actor_application_ref"] == "80@apps.example"
    assert all("token" not in str(key).lower() for key in encoded)
    with pytest.raises(ValidationError, match="Extra inputs"):
        AnnouncementFollowAuthorizeRequest.model_validate(
            encoded | {"bot_token": "must-not-cross-instances"}
        )


def test_follow_schema_keeps_receiver_proofs_separate() -> None:
    request = AnnouncementFollowAuthorizeRequest.model_validate(
        {
            "actor": {
                "id": "60",
                "origin_domain": "actor.example",
                "username": "member",
            },
            "source_channel_ref": "10@source.example",
            "target_channel_id": "20",
            "actor_intents": {
                "SOURCE.EXAMPLE.": {"proof": "source"},
                "target.example": {"proof": "target"},
            },
        }
    )

    assert request.actor_intents == {
        "source.example": {"proof": "source"},
        "target.example": {"proof": "target"},
    }
    with pytest.raises(ValidationError, match="mutually exclusive"):
        AnnouncementFollowAuthorizeRequest.model_validate(
            request.model_dump(mode="json") | {"actor_intent": {"proof": "legacy"}}
        )


@pytest.mark.asyncio
async def test_three_domain_follow_routes_select_only_the_receivers_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = SimpleNamespace(id=60, origin_domain="actor.example")
    resolve_actor = AsyncMock(return_value=(actor, None))
    source_authorize = AsyncMock(return_value={"source": "authorization"})
    target_authorize = AsyncMock(return_value={"target": "receipt"})
    validate_source = AsyncMock()
    monkeypatch.setattr(
        "app.api.federation.enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.federation._announcement_federation_actor", resolve_actor)
    monkeypatch.setattr(
        "app.api.federation.authorize_federated_announcement_follow_source",
        source_authorize,
    )
    monkeypatch.setattr(
        "app.api.federation.authorize_federated_announcement_follow_target",
        target_authorize,
    )
    monkeypatch.setattr(
        "app.api.federation.validated_federated_announcement_follow_source_authorization",
        validate_source,
    )
    intents = {
        "source.example": {"proof": "source-only"},
        "target.example": {"proof": "target-only"},
    }
    profile = RemoteUserProfile(
        id="60",
        origin_domain="actor.example",
        username="member",
    )
    principal = FederationPrincipal(origin="relay.example", key_id="relay-key")

    await federation_authorize_announcement_follow_source(
        10,
        AnnouncementFollowSourceAuthorizeRequest(
            actor=profile,
            actor_intents=intents,
            target_channel_ref=EntityRef("20@target.example"),
        ),
        principal,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="source.example")),
    )
    assert resolve_actor.await_args.kwargs["actor_intent"] == {"proof": "source-only"}

    await federation_authorize_announcement_follow(
        20,
        AnnouncementFollowAuthorizeRequest(
            actor=profile,
            actor_intents=intents,
            source_channel_ref=EntityRef("10@source.example"),
            target_channel_id="20",
            source_authorization={"source": "authorization"},
        ),
        principal,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
    )
    assert resolve_actor.await_args.kwargs["actor_intent"] == {"proof": "target-only"}
    assert source_authorize.await_count == 1
    assert target_authorize.await_count == 1


@pytest.mark.asyncio
async def test_crosspost_retry_returns_original_copy_after_follower_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    guild = Guild(
        id=50,
        origin_domain="target.example",
        name="Target",
        owner_id=99,
        owner_domain="target.example",
    )
    original_channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=50,
        guild_domain="target.example",
        name="old-destination",
        type=0,
    )
    follow = FederatedChannelFollow(
        id=44,
        local_role="target",
        source_channel_id=10,
        source_channel_domain="source.example",
        # The incoming follower was moved after the first delivery committed.
        target_channel_id=21,
        target_channel_domain="target.example",
        source_authority_domain="source.example",
        target_authority_domain="target.example",
        creator_id=30,
        creator_domain="member.example",
        generation=3,
        lifecycle_state="active",
        active=True,
        authority_receipt={},
    )
    receipt = FederatedMessageCrosspost(
        source_message_id=70,
        source_message_domain="source.example",
        follow_id=44,
        follow_authority_domain="target.example",
        local_role="target",
        generation=3,
        destination_message_id=90,
        destination_message_domain="target.example",
        delivery_status="delivered",
        attempts=1,
        next_retry_at=now,
        published_at=now,
    )
    destination = Message(
        id=90,
        origin_domain="target.example",
        channel_id=20,
        channel_domain="target.example",
        author_id=30,
        author_domain="member.example",
        content="published",
        message_type=0,
        flags=MESSAGE_FLAG_IS_CROSSPOST,
        forwarded_message_id=70,
        forwarded_message_domain="source.example",
        forwarded_channel_id=10,
        forwarded_channel_domain="source.example",
        deleted_at=None,
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is FederatedChannelFollow:
            return follow
        if model is FederatedMessageCrosspost:
            return receipt
        if model is Channel and key == (20, "target.example"):
            return original_channel
        if model is Guild:
            return guild
        if model is Message:
            return destination
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(),
        refresh=AsyncMock(),
    )
    monkeypatch.setattr(
        "app.api.federation.enforce_federation_route_rate_limit",
        AsyncMock(),
    )
    monkeypatch.setattr("app.api.federation.lock_announcement_mutation", AsyncMock())
    monkeypatch.setattr(
        "app.api.federation.render_message_payload",
        AsyncMock(return_value={"id": "90", "content": "published"}),
    )
    payload = AnnouncementCrosspostDeliverRequest(
        follow_id="44",
        generation="3",
        source_channel_ref=EntityRef("10@source.example"),
        source_message_ref=EntityRef("70@source.example"),
        source_author=RemoteUserProfile(
            id="30",
            origin_domain="member.example",
            username="member",
        ),
        source_message={},
        published_at=now,
    )

    rendered = await federation_deliver_announcement_crosspost(
        20,
        payload,
        FederationPrincipal(origin="source.example", key_id="source-key"),
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
    )

    assert rendered == {
        "destination_message_ref": "90@target.example",
        "message": {"id": "90", "content": "published"},
    }


@pytest.mark.asyncio
async def test_federated_bot_follow_actor_requires_preexisting_application_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=60,
        origin_domain="apps.example",
        is_local=False,
        username="helper.bot",
        account_type="bot",
    )
    application = BotApplication(
        id=80,
        origin_domain="apps.example",
        team_id=70,
        team_domain="apps.example",
        bot_user_id=actor.id,
        bot_user_domain=actor.origin_domain,
        name="Helper",
        status="active",
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User and key == (60, "apps.example"):
            return actor
        if model is BotApplication and key == (80, "apps.example"):
            return application
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    upsert = AsyncMock(return_value=actor)
    restriction = AsyncMock()
    monkeypatch.setattr("app.api.federation.upsert_remote_user", upsert)
    monkeypatch.setattr("app.api.federation.require_remote_user_creation_allowed", restriction)

    resolved_actor, resolved_application = await _announcement_federation_actor(
        cast(Any, session),
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(domain="target.example")),
        FederationPrincipal(origin="apps.example", key_id="apps-key"),
        RemoteUserProfile(
            id="60",
            origin_domain="apps.example",
            username="helper.bot",
        ),
        EntityRef("80@apps.example"),
    )

    assert resolved_actor is actor
    assert resolved_application is application
    upsert.assert_awaited_once()
    restriction.assert_awaited_once_with(cast(Any, session), actor)

    session.get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as missing:
        await _announcement_federation_actor(
            cast(Any, session),
            cast(Any, SimpleNamespace()),
            cast(Any, SimpleNamespace(domain="target.example")),
            FederationPrincipal(origin="apps.example", key_id="apps-key"),
            RemoteUserProfile(
                id="61",
                origin_domain="apps.example",
                username="unknown.bot",
            ),
            EntityRef("81@apps.example"),
        )
    assert missing.value.detail == {"code": "BOT_NOT_INSTALLED"}


@pytest.mark.asyncio
async def test_relayed_bot_actor_intent_is_bound_to_receiving_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = User(
        id=60,
        origin_domain="apps.example",
        is_local=False,
        username="helper.bot",
        account_type="bot",
    )
    application = BotApplication(
        id=80,
        origin_domain="apps.example",
        team_id=70,
        team_domain="apps.example",
        bot_user_id=actor.id,
        bot_user_domain=actor.origin_domain,
        name="Helper",
        status="active",
    )

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User and key == (60, "apps.example"):
            return actor
        if model is BotApplication and key == (80, "apps.example"):
            return application
        return None

    session = SimpleNamespace(get=AsyncMock(side_effect=get))
    redis = SimpleNamespace()
    validator = AsyncMock()
    monkeypatch.setattr("app.api.federation.upsert_remote_user", AsyncMock(return_value=actor))
    monkeypatch.setattr("app.api.federation.require_remote_user_creation_allowed", AsyncMock())
    monkeypatch.setattr("app.api.federation.validate_worker_actor_intent", validator)

    await _announcement_federation_actor(
        cast(Any, session),
        cast(Any, redis),
        cast(Any, SimpleNamespace(domain="target.example")),
        FederationPrincipal(origin="relay.example", key_id="relay-key"),
        RemoteUserProfile(
            id="60",
            origin_domain="apps.example",
            username="helper.bot",
        ),
        EntityRef("80@apps.example"),
        actor_intent={"signature": "worker proof"},
        expected_intent_action="announcement.follow.create",
        expected_intent_resources={
            "source_channel": "10@source.example",
            "target_channel": "20@target.example",
        },
    )

    assert validator.await_args.kwargs["expected_audience"] == "target.example"
    assert validator.await_args.kwargs["runtime_target_domain"] == "target.example"
    assert validator.await_args.kwargs["redis"] is redis


@pytest.mark.asyncio
async def test_bot_follow_scope_is_rechecked_at_each_channel_authority() -> None:
    actor = User(
        id=60,
        origin_domain="apps.example",
        is_local=False,
        username="helper.bot",
        account_type="bot",
    )
    application = BotApplication(
        id=80,
        origin_domain="apps.example",
        team_id=70,
        team_domain="apps.example",
        bot_user_id=actor.id,
        bot_user_domain=actor.origin_domain,
        name="Helper",
        status="active",
    )
    guild = Guild(
        id=30,
        origin_domain="target.example",
        name="Target",
        owner_id=40,
        owner_domain="target.example",
    )
    channel = Channel(
        id=20,
        origin_domain="target.example",
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="announcements",
    )
    access = ChannelAccess(channel=channel, guild=guild, participants=[])
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=SimpleNamespace(granted_scopes=["webhooks.manage"]))
    )

    # Manage deliberately subsumes read for list/delete orchestration.
    await require_announcement_actor_scope(
        cast(Any, session), access, actor, application, "webhooks.read"
    )
    with pytest.raises(HTTPException) as denied:
        await require_announcement_actor_scope(
            cast(Any, session), access, actor, application, "messages.send"
        )
    assert denied.value.detail == {"code": "BOT_SCOPE_REQUIRED", "scope": "messages.send"}


def test_dm_history_accepts_immutable_forward_snapshot_with_optional_note() -> None:
    created_at = datetime(2026, 8, 27, tzinfo=UTC)
    message_id = (int(created_at.timestamp() * 1_000) - EPOCH_MS) << (WORKER_BITS + SEQUENCE_BITS)
    raw = {
        "id": str(message_id),
        "origin_domain": "authority.example",
        "channel_id": "50",
        "channel_domain": "authority.example",
        "author_id": "20",
        "author_domain": "authority.example",
        "author": {
            "id": "20",
            "origin_domain": "authority.example",
            "username": "remote",
            "profile_version": 1,
        },
        "content": "optional note",
        "e2ee": None,
        "attachments": [],
        "message_type": 0,
        "flags": 1 << 14,
        "client_nonce": "forward-1",
        "referenced_message_id": None,
        "referenced_message_domain": None,
        "forwarded_message_id": str(message_id - 1),
        "forwarded_message_domain": "authority.example",
        "forwarded_channel_id": "50",
        "forwarded_channel_domain": "authority.example",
        "forward_snapshot": {
            "content": "immutable source body",
            "message_type": 0,
            "flags": 0,
            "created_at": created_at.isoformat(),
        },
        "message_reference": {"type": 1},
        "mention_user_refs": [],
        "edited_at": None,
        "deleted_at": None,
        "created_at": created_at.isoformat(),
    }
    settings = SimpleNamespace(
        domain="member.example",
        media_max_attachment_bytes=25 * 1024 * 1024,
        secret_key_bytes=b"x" * 32,
    )

    page = validate_dm_history_page(
        {
            "conversation_id": "50",
            "conversation_domain": "authority.example",
            "messages": [raw],
            "next_before": None,
            "complete": True,
        },
        settings=cast(Any, settings),
        conversation_ref=(50, "authority.example"),
        authority_domain="authority.example",
        participant_refs={(10, "member.example"), (20, "authority.example")},
        trusted_profiles={},
        before=(message_id + 1, "authority.example"),
        limit=1,
    )

    assert page.messages[0]["content"] == "optional note"
    assert page.messages[0]["forwarded_message_ref"] is None
    assert page.messages[0]["message_snapshots"] == [
        {
            "message": {
                "content": "immutable source body",
                "embeds": [],
                "components": [],
                "attachments": [],
                "mention_user_refs": [],
                "sticker_items": [],
                "message_snapshots": [],
                "message_type": 0,
                "flags": 0,
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
            }
        }
    ]
    with pytest.raises(Exception, match="rich content"):
        validate_dm_history_page(
            {
                "conversation_id": "50",
                "conversation_domain": "authority.example",
                "messages": [raw | {"forward_snapshot": None, "flags": 0}],
                "next_before": None,
                "complete": True,
            },
            settings=cast(Any, settings),
            conversation_ref=(50, "authority.example"),
            authority_domain="authority.example",
            participant_refs={(10, "member.example"), (20, "authority.example")},
            trusted_profiles={},
            before=(message_id + 1, "authority.example"),
            limit=1,
        )


def test_user_install_grant_requires_full_signed_interaction_capability() -> None:
    valid = FederatedUserInstallationGrant.model_validate(
        {
            "id": "70",
            "application_ref": "80@apps.example",
            "scopes": ["applications.commands", "interactions.respond"],
            "intents": ["interactions"],
            "contexts": ["guild"],
            "grant_revision": "2",
            "authority_expires_at": datetime.now(UTC) + timedelta(minutes=20),
        }
    )
    assert valid.grant_revision == "2"
    with pytest.raises(ValidationError, match="required scopes"):
        FederatedUserInstallationGrant.model_validate(
            {
                "id": "70",
                "application_ref": "80@apps.example",
                "scopes": ["applications.commands"],
                "intents": ["interactions"],
                "contexts": ["guild"],
                "grant_revision": "2",
                "authority_expires_at": datetime.now(UTC) + timedelta(minutes=20),
            }
        )


@pytest.mark.asyncio
async def test_remote_user_install_mirror_uses_user_and_application_authorities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=60,
        origin_domain="member.example",
        is_local=False,
        username="member",
        account_type="human",
    )
    application = SimpleNamespace(
        id=80,
        origin_domain="apps.example",
        status="active",
        bot_user_id=90,
        bot_user_domain="apps.example",
        default_scopes=["applications.commands", "interactions.respond"],
        default_intents=["interactions"],
        supported_install_types=["user_install"],
        user_install_scopes=["applications.commands", "interactions.respond"],
        user_install_contexts=["guild"],
        target_policy="open",
    )
    bot = SimpleNamespace(account_type="bot", disabled_at=None)

    async def get(model: object, key: object, **_kwargs: object) -> object | None:
        if model is User and key == (90, "apps.example"):
            return bot
        return None

    session = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        scalar=AsyncMock(side_effect=[application, None, None, None]),
        execute=AsyncMock(),
        add=Mock(),
        flush=AsyncMock(),
    )
    refresh = AsyncMock()
    monkeypatch.setattr("app.api.interactions.refresh_user_bot_application", refresh)
    grant = FederatedUserInstallationGrant.model_validate(
        {
            "id": "70",
            "application_ref": "80@apps.example",
            "scopes": ["applications.commands", "interactions.respond"],
            "intents": ["interactions"],
            "contexts": ["guild"],
            "grant_revision": "2",
            "authority_expires_at": datetime.now(UTC) + timedelta(minutes=20),
        }
    )

    installation = await materialize_federated_user_installation(
        cast(Any, session),
        cast(
            Any,
            SimpleNamespace(
                domain="guild.example",
                federation_clock_skew_seconds=300,
            ),
        ),
        cast(Any, SimpleNamespace(mint=AsyncMock(return_value=71))),
        user,
        InteractionCreate(application_ref="80@apps.example", command_name="ship"),
        grant,
    )

    assert isinstance(installation, BotUserInstallation)
    assert installation.user_domain == "member.example"
    assert installation.application_domain == "apps.example"
    assert installation.id == 71
    assert (installation.source_id, installation.source_domain) == (
        int(grant.id),
        "member.example",
    )
    assert installation.grant_revision == 2
    refresh.assert_awaited_once()
    assert refresh.await_args.args[-2:] == (80, "apps.example")
