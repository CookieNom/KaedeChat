from __future__ import annotations

from datetime import UTC, datetime
from inspect import unwrap
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy.dialects import postgresql

from app import tasks
from app.core.settings import Settings
from app.db.bot_models import BotInstallation
from app.db.models import (
    Attachment,
    Channel,
    FederationEvent,
    Guild,
    Instance,
    MediaTombstoneSource,
    Message,
    RemoteMediaTombstone,
)
from app.federation import events as federation_events
from app.federation.events import message_attachment_refs
from app.federation.replication import replicate_message_attachments
from app.media import jobs as media_jobs
from app.media import tombstones

LOCAL_DOMAIN = "alpha.localhost"
REMOTE_DOMAIN = "beta.localhost"


def settings() -> Settings:
    return cast(
        Settings,
        SimpleNamespace(
            domain=LOCAL_DOMAIN,
            media_max_attachment_bytes=8 * 1024 * 1024,
        ),
    )


@pytest.mark.asyncio
async def test_remote_tombstone_wins_when_message_create_arrives_later() -> None:
    message = SimpleNamespace(id=20, origin_domain=REMOTE_DOMAIN)
    author = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN)
    added: list[object] = []

    async def get_model(model: object, _key: object) -> object | None:
        if model is Attachment:
            return None
        if model is RemoteMediaTombstone:
            return SimpleNamespace(event_id="kcfe_delete")
        if model is MediaTombstoneSource:
            return None
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(get=get_model, add=added.append)
    rendered = await replicate_message_attachments(
        cast(Any, session),
        settings(),
        cast(Any, message),
        cast(Any, author),
        [
            {
                "id": "40",
                "origin_domain": REMOTE_DOMAIN,
                "filename": "photo.png",
                "content_type": "image/png",
                "size": 512,
                "variants": {},
            }
        ],
    )

    assert rendered == []
    assert added == []


@pytest.mark.asyncio
async def test_remote_bot_attachment_uses_local_guild_owner_as_tombstone_signer() -> None:
    owner = SimpleNamespace(id=10, origin_domain=LOCAL_DOMAIN, is_local=True)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=owner),
        get=AsyncMock(side_effect=AssertionError("installer fallback must not be used")),
    )
    attachment = SimpleNamespace(
        uploader_id=50,
        uploader_domain="bot-origin.localhost",
        bot_installation_id=70,
    )
    guild = SimpleNamespace(
        id=1,
        origin_domain=LOCAL_DOMAIN,
        owner_id=owner.id,
        owner_domain=owner.origin_domain,
    )

    signer = await tombstones.resolve_media_delete_signer(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, guild),
    )

    assert signer is owner
    session.scalar.assert_awaited_once()
    signer_lock_sql = str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE NOWAIT" in signer_lock_sql


@pytest.mark.asyncio
async def test_remote_guild_bot_attachment_uses_local_installer_as_tombstone_signer() -> None:
    installer = SimpleNamespace(id=11, origin_domain=LOCAL_DOMAIN, is_local=True)
    installation = SimpleNamespace(
        id=70,
        installer_id=installer.id,
        installer_domain=installer.origin_domain,
    )

    async def get_model(model: object, key: object) -> object | None:
        if model is BotInstallation and key == installation.id:
            return installation
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=installer),
        get=get_model,
    )
    attachment = SimpleNamespace(
        uploader_id=50,
        uploader_domain="bot-origin.localhost",
        bot_installation_id=installation.id,
    )
    guild = SimpleNamespace(
        id=1,
        origin_domain=REMOTE_DOMAIN,
        owner_id=90,
        owner_domain=REMOTE_DOMAIN,
    )

    signer = await tombstones.resolve_media_delete_signer(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, guild),
    )

    assert signer is installer
    session.scalar.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_guild_human_attachment_uses_local_uploader_as_tombstone_signer() -> None:
    uploader = SimpleNamespace(id=12, origin_domain=LOCAL_DOMAIN, is_local=True)
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=uploader),
        get=AsyncMock(side_effect=AssertionError("no fallback signer should be needed")),
    )
    attachment = SimpleNamespace(
        uploader_id=uploader.id,
        uploader_domain=uploader.origin_domain,
        bot_installation_id=None,
    )
    remote_guild = SimpleNamespace(
        id=1,
        origin_domain=REMOTE_DOMAIN,
        owner_id=90,
        owner_domain=REMOTE_DOMAIN,
    )

    signer = await tombstones.resolve_media_delete_signer(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, remote_guild),
    )

    assert signer is uploader


@pytest.mark.asyncio
@pytest.mark.parametrize("account_type", ["human", "bot"])
async def test_local_attachment_retains_remote_uploader_for_tombstone_attribution(
    account_type: str,
) -> None:
    uploader = SimpleNamespace(
        id=12,
        origin_domain=REMOTE_DOMAIN,
        is_local=False,
        account_type=account_type,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(return_value=uploader),
        get=AsyncMock(side_effect=AssertionError("no fallback signer should be needed")),
    )
    attachment = SimpleNamespace(
        uploader_id=uploader.id,
        uploader_domain=uploader.origin_domain,
        bot_installation_id=None,
        bot_user_installation_id=None,
    )
    guild = SimpleNamespace(
        id=1,
        origin_domain=LOCAL_DOMAIN,
        owner_id=90,
        owner_domain=REMOTE_DOMAIN,
    )

    signer = await tombstones.resolve_media_delete_signer(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, guild),
    )

    assert signer is uploader
    signer_sql = str(session.scalar.await_args.args[0].compile(dialect=postgresql.dialect()))
    assert "users.is_local IS false" in signer_sql


def test_message_fanout_records_every_attachment_origin_for_authority_relay() -> None:
    assert message_attachment_refs(
        {
            "content": {
                "message": {
                    "attachments": [
                        {"id": "40", "origin_domain": LOCAL_DOMAIN},
                        {"id": "41", "origin_domain": REMOTE_DOMAIN},
                    ]
                }
            }
        },
    ) == {(40, LOCAL_DOMAIN), (41, REMOTE_DOMAIN)}


def test_remote_guild_proxy_proposal_records_direct_attachment_shape() -> None:
    assert message_attachment_refs(
        {
            "type": "guild.proxy.message.create",
            "content": {
                "attachments": [{"id": "40", "origin_domain": LOCAL_DOMAIN}],
            },
        }
    ) == {(40, LOCAL_DOMAIN)}


def test_legacy_media_tombstone_without_generation_is_generation_zero() -> None:
    envelope = {
        "event_id": "kcfe_legacy",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
        },
    }

    assert federation_events.media_delete_generation(envelope) == 0
    assert federation_events.media_delete_order(envelope) == (0, 1, "kcfe_legacy")


def test_explicit_generation_zero_is_not_a_legacy_media_tombstone() -> None:
    envelope = {
        "event_id": "kcfe_invalid_zero",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "0",
        },
    }

    with pytest.raises(ValueError, match="generation is invalid"):
        federation_events.media_delete_generation(envelope)


@pytest.mark.asyncio
async def test_legacy_media_tombstone_is_resigned_on_the_current_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy_envelope = {
        "event_id": "kcfe_legacy",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:current": "legacy-signature"}},
    }
    event = SimpleNamespace(event_id="kcfe_legacy", envelope=legacy_envelope)
    instance = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        is_self=True,
        current_key_id="ed25519:current",
    )
    source = SimpleNamespace(
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        signer_id=30,
        signer_domain=LOCAL_DOMAIN,
        event_id=event.event_id,
        key_id="ed25519:current",
        generation=0,
        updated_at=None,
    )
    current_envelope = {
        "event_id": "kcfe_current",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 2,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "1",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:current": "current-signature"}},
    }

    async def get_model(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Instance:
            return instance
        if model is FederationEvent and key == (LOCAL_DOMAIN, event.event_id):
            return event
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        get=get_model,
        scalar=AsyncMock(return_value=source),
        scalars=AsyncMock(return_value=[REMOTE_DOMAIN]),
        execute=AsyncMock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", AsyncMock())
    build = AsyncMock(return_value=current_envelope)
    monkeypatch.setattr(tombstones, "build_envelope", build)
    monkeypatch.setattr(
        tombstones,
        "_retain_media_delete_event",
        AsyncMock(return_value=SimpleNamespace(envelope=current_envelope)),
    )
    queued = AsyncMock()
    monkeypatch.setattr(tombstones, "queue_event", queued)
    monkeypatch.setattr(tombstones, "record_media_tombstone_destinations", AsyncMock())

    destinations = await tombstones.queue_media_delete_tombstone(
        cast(Any, session),
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations=set(),
    )

    assert destinations == {REMOTE_DOMAIN}
    build.assert_awaited_once()
    queued.assert_awaited_once_with(session, settings(), REMOTE_DOMAIN, current_envelope)
    assert (source.event_id, source.key_id, source.generation) == (
        "kcfe_current",
        "ed25519:current",
        1,
    )


@pytest.mark.asyncio
async def test_rotated_media_tombstone_envelope_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_envelope = {
        "event_id": "kcfe_old",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "1",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:old": "signature"}},
    }
    event = SimpleNamespace(event_id="kcfe_old", envelope=old_envelope)
    instance = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        is_self=True,
        current_key_id="ed25519:new",
    )
    source = SimpleNamespace(
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        signer_id=30,
        signer_domain=LOCAL_DOMAIN,
        event_id=event.event_id,
        key_id="ed25519:old",
        generation=1,
        updated_at=None,
    )
    new_envelope = {
        "event_id": "kcfe_new",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 2,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "2",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:new": "signature"}},
    }

    async def get_model(model: object, key: object, **_kwargs: object) -> object | None:
        if model is Instance:
            return instance
        if model is FederationEvent and key == (LOCAL_DOMAIN, event.event_id):
            return event
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        get=get_model,
        scalar=AsyncMock(return_value=source),
        scalars=AsyncMock(return_value=[REMOTE_DOMAIN]),
        execute=AsyncMock(),
        flush=AsyncMock(),
    )
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", AsyncMock())
    build = AsyncMock(return_value=new_envelope)
    retain = AsyncMock(return_value=SimpleNamespace(envelope=new_envelope))
    queued = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(tombstones, "build_envelope", build)
    monkeypatch.setattr(tombstones, "_retain_media_delete_event", retain)
    monkeypatch.setattr(tombstones, "queue_event", queued)
    monkeypatch.setattr(tombstones, "record_media_tombstone_destinations", record)

    destinations = await tombstones.queue_media_delete_tombstone(
        cast(Any, session),
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations=set(),
    )

    assert destinations == {REMOTE_DOMAIN}
    build.assert_awaited_once()
    retain.assert_awaited_once_with(session, new_envelope)
    queued.assert_awaited_once_with(session, settings(), REMOTE_DOMAIN, new_envelope)
    assert (source.event_id, source.key_id, source.generation) == (
        "kcfe_new",
        "ed25519:new",
        2,
    )
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_initial_media_tombstone_persists_proof_without_destinations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        is_self=True,
        current_key_id="ed25519:current",
    )
    envelope = {
        "event_id": "kcfe_initial",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "1",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:current": "signature"}},
    }
    added: list[object] = []

    async def get_model(model: object, _key: object, **_kwargs: object) -> object | None:
        if model is Instance:
            return instance
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        get=get_model,
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
        execute=AsyncMock(),
        flush=AsyncMock(),
        add=added.append,
    )
    signer = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN)
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(tombstones, "build_envelope", AsyncMock(return_value=envelope))
    retain = AsyncMock(return_value=SimpleNamespace(envelope=envelope))
    monkeypatch.setattr(tombstones, "_retain_media_delete_event", retain)
    queue = AsyncMock()
    monkeypatch.setattr(tombstones, "queue_event", queue)
    monkeypatch.setattr(tombstones, "record_media_tombstone_destinations", AsyncMock())

    destinations = await tombstones.queue_media_delete_tombstone(
        cast(Any, session),
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations=set(),
        signer=cast(Any, signer),
    )

    assert destinations == set()
    queue.assert_not_awaited()
    retain.assert_awaited_once_with(session, envelope)
    assert len(added) == 1
    source = cast(MediaTombstoneSource, added[0])
    assert (
        source.attachment_id,
        source.attachment_domain,
        source.signer_id,
        source.signer_domain,
        source.event_id,
        source.key_id,
        source.generation,
    ) == (
        40,
        LOCAL_DOMAIN,
        30,
        LOCAL_DOMAIN,
        "kcfe_initial",
        "ed25519:current",
        1,
    )


@pytest.mark.asyncio
async def test_initial_remote_uploader_tombstone_uses_narrow_retained_actor_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        is_self=True,
        current_key_id="ed25519:current",
    )
    envelope = {
        "event_id": "kcfe_remote_uploader",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "actor": {"id": "30", "domain": REMOTE_DOMAIN},
        "context": {},
        "content": {
            "attachment_id": "40",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "1",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:current": "signature"}},
    }
    added: list[object] = []

    async def get_model(model: object, _key: object, **_kwargs: object) -> object | None:
        if model is Instance:
            return instance
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        get=get_model,
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
        execute=AsyncMock(),
        flush=AsyncMock(),
        add=added.append,
    )
    signer = SimpleNamespace(id=30, origin_domain=REMOTE_DOMAIN)
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", AsyncMock())
    build = AsyncMock(return_value=envelope)
    monkeypatch.setattr(tombstones, "build_envelope", build)
    monkeypatch.setattr(
        tombstones,
        "_retain_media_delete_event",
        AsyncMock(return_value=SimpleNamespace(envelope=envelope)),
    )
    monkeypatch.setattr(tombstones, "queue_event", AsyncMock())
    monkeypatch.setattr(tombstones, "record_media_tombstone_destinations", AsyncMock())

    await tombstones.queue_media_delete_tombstone(
        cast(Any, session),
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations=set(),
        signer=cast(Any, signer),
    )

    assert build.await_args.kwargs["retained_authority_attested_actor"] is True
    source = cast(MediaTombstoneSource, added[0])
    assert (source.signer_id, source.signer_domain) == (30, REMOTE_DOMAIN)


@pytest.mark.asyncio
async def test_late_remote_metadata_disclosure_requeues_retained_origin_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_source = SimpleNamespace(
        attachment_id=40,
        attachment_domain=REMOTE_DOMAIN,
    )
    session = SimpleNamespace(scalars=AsyncMock(return_value=[remote_source]))
    record = AsyncMock(return_value={(40, REMOTE_DOMAIN)})
    queue_proof = AsyncMock(return_value=True)
    monkeypatch.setattr(federation_events, "record_attachment_recipients", record)
    monkeypatch.setattr(
        tombstones,
        "queue_retained_media_delete_proof",
        queue_proof,
    )

    recorded, wakes, terminal_refs = await federation_events.record_disclosed_attachment_recipients(
        cast(Any, session),
        settings(),
        {(40, REMOTE_DOMAIN)},
        "gamma.localhost",
    )

    assert recorded == {(40, REMOTE_DOMAIN)}
    assert wakes == {"gamma.localhost"}
    assert terminal_refs == {(40, REMOTE_DOMAIN)}
    queue_proof.assert_awaited_once_with(session, settings(), 40, REMOTE_DOMAIN, "gamma.localhost")


@pytest.mark.asyncio
async def test_disclosure_to_attachment_origin_never_replays_tombstone_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = SimpleNamespace(scalars=AsyncMock(return_value=[]))
    record = AsyncMock(return_value=set())
    queue_proof = AsyncMock(return_value=True)
    monkeypatch.setattr(federation_events, "record_attachment_recipients", record)
    monkeypatch.setattr(
        tombstones,
        "queue_retained_media_delete_proof",
        queue_proof,
    )

    recorded, wakes, terminal_refs = await federation_events.record_disclosed_attachment_recipients(
        cast(Any, session),
        settings(),
        {(40, REMOTE_DOMAIN)},
        REMOTE_DOMAIN,
    )

    assert recorded == set()
    assert wakes == set()
    assert terminal_refs == set()
    queue_proof.assert_not_awaited()


@pytest.mark.asyncio
async def test_former_dm_participant_remains_a_terminal_tombstone_destination() -> None:
    attachment = SimpleNamespace(id=40, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(id=2, origin_domain=REMOTE_DOMAIN)
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[["former.localhost"], [], [], []]),
    )

    destinations = await tombstones.terminal_attachment_destinations(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, channel),
        None,
    )

    assert destinations == {"former.localhost"}


@pytest.mark.asyncio
async def test_former_guild_viewer_remains_a_terminal_tombstone_destination() -> None:
    attachment = SimpleNamespace(id=40, origin_domain=LOCAL_DOMAIN)
    channel = SimpleNamespace(id=2, origin_domain=REMOTE_DOMAIN)
    guild = SimpleNamespace(id=1, origin_domain=REMOTE_DOMAIN)
    session = SimpleNamespace(
        scalars=AsyncMock(side_effect=[["former.localhost"], [], [], []]),
    )

    destinations = await tombstones.terminal_attachment_destinations(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
        cast(Any, channel),
        cast(Any, guild),
    )

    assert destinations == {"former.localhost"}


@pytest.mark.asyncio
async def test_terminal_attachment_tombstone_is_queued_to_every_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        uploader_id=30,
        uploader_domain=LOCAL_DOMAIN,
        bot_installation_id=None,
        scan_status="quarantined",
        deleted_at=object(),
        message_id=20,
        message_domain=REMOTE_DOMAIN,
    )
    message = SimpleNamespace(
        id=20,
        origin_domain=REMOTE_DOMAIN,
        channel_id=2,
        channel_domain=REMOTE_DOMAIN,
    )
    channel = SimpleNamespace(
        id=2,
        origin_domain=REMOTE_DOMAIN,
        guild_id=1,
        guild_domain=REMOTE_DOMAIN,
    )
    guild = SimpleNamespace(id=1, origin_domain=REMOTE_DOMAIN)

    async def get_model(model: object, key: object) -> object | None:
        if model is Message and key == (message.id, message.origin_domain):
            return message
        if model is Channel and key == (channel.id, channel.origin_domain):
            return channel
        if model is Guild and key == (guild.id, guild.origin_domain):
            return guild
        if model is MediaTombstoneSource:
            return None
        raise AssertionError("unexpected model lookup")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
        execute=AsyncMock(),
        get=get_model,
    )
    signer = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN)
    monkeypatch.setattr(
        tombstones,
        "terminal_attachment_destinations",
        AsyncMock(return_value={REMOTE_DOMAIN, "gamma.localhost"}),
    )
    monkeypatch.setattr(
        tombstones,
        "resolve_media_delete_signer",
        AsyncMock(return_value=signer),
    )
    queue_delete = AsyncMock(return_value={REMOTE_DOMAIN, "gamma.localhost"})
    monkeypatch.setattr(tombstones, "queue_media_delete_tombstone", queue_delete)

    destinations = await tombstones.queue_terminal_attachment_tombstone(
        cast(Any, session),
        settings(),
        cast(Any, attachment),
    )

    assert destinations == {REMOTE_DOMAIN, "gamma.localhost"}
    queue_delete.assert_awaited_once_with(
        session,
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations={REMOTE_DOMAIN, "gamma.localhost"},
        signer=signer,
        room_ref=("guild", 1, REMOTE_DOMAIN),
    )


@pytest.mark.asyncio
async def test_unbound_terminal_proxy_attachment_uses_historical_recipient_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        uploader_id=30,
        uploader_domain=LOCAL_DOMAIN,
        bot_installation_id=None,
        scan_status="rejected",
        deleted_at=object(),
        message_id=None,
        message_domain=None,
    )

    async def get_model(model: object, _key: object) -> object | None:
        if model is MediaTombstoneSource:
            return None
        raise AssertionError("unbound tombstone must not require message or guild state")

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=None),
        scalars=AsyncMock(return_value=[]),
        get=get_model,
    )
    signer = SimpleNamespace(id=30, origin_domain=LOCAL_DOMAIN)
    monkeypatch.setattr(
        tombstones,
        "historical_attachment_destinations",
        AsyncMock(return_value={REMOTE_DOMAIN}),
    )
    monkeypatch.setattr(
        tombstones,
        "resolve_media_delete_signer",
        AsyncMock(return_value=signer),
    )
    queue_delete = AsyncMock(return_value={REMOTE_DOMAIN})
    monkeypatch.setattr(tombstones, "queue_media_delete_tombstone", queue_delete)

    destinations = await tombstones.queue_terminal_attachment_tombstone(
        cast(Any, session), settings(), cast(Any, attachment)
    )

    assert destinations == {REMOTE_DOMAIN}
    queue_delete.assert_awaited_once_with(
        session,
        settings(),
        attachment_id=40,
        attachment_domain=LOCAL_DOMAIN,
        destinations={REMOTE_DOMAIN},
        signer=signer,
        room_ref=None,
    )


@pytest.mark.asyncio
async def test_media_process_commits_tombstone_before_delivery_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        message_id=20,
        message_domain=REMOTE_DOMAIN,
        content_sha256=None,
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, model: object, _key: object) -> object | None:
            if model is Attachment:
                return attachment
            if model is Message:
                return None
            raise AssertionError("unexpected model lookup")

        async def commit(self) -> None:
            self.committed = True

    class FakeEngine:
        dispose = AsyncMock()

    class FakeRedis:
        aclose = AsyncMock()

    session = FakeSession()
    engine = FakeEngine()
    redis = FakeRedis()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
        dragonfly_url=SimpleNamespace(get_secret_value=lambda: "redis://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(
        tasks,
        "queue_terminal_attachment_tombstone",
        AsyncMock(return_value={REMOTE_DOMAIN}),
    )

    async def process_with_atomic_callback(
        _session: object,
        _settings: object,
        _attachment_id: int,
        _origin_domain: str,
        *,
        before_terminal_commit: Any,
    ) -> str:
        await before_terminal_commit(attachment)
        await session.commit()
        return "quarantined"

    monkeypatch.setattr(tasks, "process_attachment_record", process_with_atomic_callback)

    async def assert_committed_before_wake(*_args: object) -> None:
        assert session.committed

    wake = AsyncMock(side_effect=assert_committed_before_wake)
    monkeypatch.setattr(tasks, "enqueue_best_effort", wake)

    result = await unwrap(tasks.media_process.original_func)(40, LOCAL_DOMAIN)

    assert result == "quarantined"
    wake.assert_awaited_once_with(tasks.federation_deliver, REMOTE_DOMAIN)
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_verdict_does_not_commit_when_tombstone_preparation_crashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"image-bytes"
    attachment = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        uploader_id=30,
        uploader_domain=LOCAL_DOMAIN,
        bot_installation_id=None,
        message_id=20,
        message_domain=REMOTE_DOMAIN,
        content_type="image/png",
        detected_content_type=None,
        content_sha256=None,
        size=len(data),
        object_key="staging/40",
        staging_object_key="staging/40",
        variants={},
        finalized_at=object(),
        deleted_at=None,
        encryption_mode="plaintext",
        scan_status="pending",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.commit = AsyncMock()

        async def scalar(self, _statement: object) -> object:
            return attachment

    storage_delete = AsyncMock()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> bytes:
            return data

        async def delete(self, *args: object, **kwargs: object) -> None:
            await storage_delete(*args, **kwargs)

    async def discard(_session: object, _settings: object, item: object) -> None:
        cast(Any, item).deleted_at = object()

    async def crash_before_commit(_attachment: object) -> None:
        raise RuntimeError("simulated outbox failure")

    session = FakeSession()
    configured = cast(
        Settings,
        SimpleNamespace(
            media_attachments_bucket="attachments",
            media_derived_bucket="derived",
            media_max_attachment_bytes=1024,
        ),
    )
    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    monkeypatch.setattr(media_jobs, "sniff_content_type", lambda _data: "image/png")
    monkeypatch.setattr(media_jobs, "validate_detected_type", lambda *_args: None)
    monkeypatch.setattr(media_jobs, "clamav_scan", AsyncMock(return_value="infected"))
    monkeypatch.setattr(media_jobs, "discard_attachment", discard)

    with pytest.raises(media_jobs.TerminalCommitPreparationError):
        await media_jobs.process_attachment_record(
            cast(Any, session),
            configured,
            attachment.id,
            attachment.origin_domain,
            before_terminal_commit=crash_before_commit,
        )

    session.commit.assert_not_awaited()
    storage_delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_guild_gap_sync_queues_late_bound_terminal_tombstone_before_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(
        id=1,
        origin_domain=REMOTE_DOMAIN,
        updated_at=datetime.now(UTC),
    )
    message = SimpleNamespace(id=20, origin_domain=REMOTE_DOMAIN)
    attachment = SimpleNamespace(id=40, origin_domain=LOCAL_DOMAIN)

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.flushed = False
            self.refreshed: list[object] = []

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalar(self, _statement: object) -> bool:
            return True

        async def get(self, model: object, key: object, **_kwargs: object) -> object | None:
            if model is Guild and key == (guild.id, guild.origin_domain):
                return guild
            if model is Attachment and key == (attachment.id, attachment.origin_domain):
                return attachment
            raise AssertionError("unexpected model lookup")

        async def scalars(self, _statement: object) -> list[object]:
            return []

        async def flush(self) -> None:
            assert not self.committed
            self.flushed = True

        async def refresh(self, value: object) -> None:
            assert self.flushed
            assert not self.committed
            self.refreshed.append(value)

        async def commit(self) -> None:
            self.committed = True

    class FakeEngine:
        dispose = AsyncMock()

    class FakeRedis:
        aclose = AsyncMock()

    session = FakeSession()
    engine = FakeEngine()
    redis = FakeRedis()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
        dragonfly_url=SimpleNamespace(get_secret_value=lambda: "redis://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    monkeypatch.setattr(tasks.Redis, "from_url", lambda *_args, **_kwargs: redis)
    monkeypatch.setattr(tasks, "synchronize_guild", AsyncMock(return_value=[message]))
    refs = AsyncMock(return_value=[(attachment.id, attachment.origin_domain)])
    monkeypatch.setattr(tasks, "terminal_attachment_refs_for_messages", refs)
    queue_tombstone = AsyncMock(return_value={"gamma.localhost"})
    monkeypatch.setattr(tasks, "queue_terminal_attachment_tombstone", queue_tombstone)

    def materialize_guild_payload(value: object) -> dict[str, object]:
        # TimestampMixin.updated_at is expired by its SQL-expression onupdate.
        # The old post-commit renderer raised MissingGreenlet at this access.
        assert not session.committed
        return {"version": cast(Any, value).updated_at.isoformat()}

    async def materialize_message_payload(
        _session: object,
        value: object,
    ) -> dict[str, object]:
        assert not session.committed
        return {"id": str(cast(Any, value).id)}

    async def assert_committed_before_publish(*_args: object) -> None:
        assert session.committed

    monkeypatch.setattr(tasks, "guild_payload", materialize_guild_payload)
    render_message = AsyncMock(side_effect=materialize_message_payload)
    monkeypatch.setattr(tasks, "render_message_payload", render_message)
    publish = AsyncMock(side_effect=assert_committed_before_publish)
    monkeypatch.setattr(tasks, "publish_dispatch", publish)

    async def assert_committed_before_wake(*_args: object) -> None:
        assert session.committed

    wake = AsyncMock(side_effect=assert_committed_before_wake)
    monkeypatch.setattr(tasks, "enqueue_best_effort", wake)

    result = await unwrap(tasks.federation_guild_sync.original_func)(REMOTE_DOMAIN, guild.id)

    assert result == 1
    refs.assert_awaited_once_with(session, worker_settings, {(message.id, message.origin_domain)})
    queue_tombstone.assert_awaited_once_with(session, worker_settings, attachment)
    assert session.flushed
    assert session.refreshed == [guild, message]
    render_message.assert_awaited_once_with(session, message)
    assert publish.await_count == 2
    assert call(tasks.federation_deliver, "gamma.localhost") in wake.await_args_list
    redis.aclose.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_tombstone_repair_commits_before_delivery_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=40,
        origin_domain=LOCAL_DOMAIN,
        asset_binding=None,
        scan_status="clean",
    )

    class FakeSession:
        def __init__(self) -> None:
            self.committed = False
            self.scalar_pages = [[attachment], []]

        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _statement: object) -> list[object]:
            return self.scalar_pages.pop(0)

        async def scalar(self, _statement: object) -> object:
            if not hasattr(self, "returned_current_key"):
                self.returned_current_key = True
                return "ed25519:current"
            return attachment

        async def execute(self, _statement: object) -> object:
            return SimpleNamespace(all=lambda: [])

        async def commit(self) -> None:
            self.committed = True

    class FakeEngine:
        dispose = AsyncMock()

    session = FakeSession()
    engine = FakeEngine()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    queue_tombstone = AsyncMock(return_value={REMOTE_DOMAIN})
    monkeypatch.setattr(tasks, "queue_terminal_attachment_tombstone", queue_tombstone)
    monkeypatch.setattr(tasks, "lock_media_tombstone_ref", AsyncMock())

    async def assert_committed_before_wake(*_args: object) -> None:
        assert session.committed

    wake = AsyncMock(side_effect=assert_committed_before_wake)
    monkeypatch.setattr(tasks, "enqueue_best_effort", wake)

    result = await unwrap(tasks.media_terminal_tombstone_sweep.original_func)()

    assert result == 1
    queue_tombstone.assert_awaited_once_with(
        session,
        worker_settings,
        attachment,
        force_authoritative=False,
    )
    wake.assert_awaited_once_with(tasks.federation_deliver, REMOTE_DOMAIN)
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_tombstone_repair_pages_all_rows_in_one_scheduled_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachments = [
        SimpleNamespace(
            id=index,
            origin_domain=LOCAL_DOMAIN,
            asset_binding=None,
            scan_status="clean",
        )
        for index in range(1, 102)
    ]
    pages = [attachments[:100], attachments[100:]]

    class FakeSession:
        async def __aenter__(self) -> FakeSession:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def scalars(self, _statement: object) -> list[object]:
            return pages.pop(0) if pages else []

        async def scalar(self, _statement: object) -> object:
            if not hasattr(self, "returned_current_key"):
                self.returned_current_key = True
                return "ed25519:current"
            return attachments.pop(0)

        async def execute(self, _statement: object) -> object:
            return SimpleNamespace(all=lambda: [])

        async def commit(self) -> None:
            return None

    class FakeEngine:
        dispose = AsyncMock()

    session = FakeSession()
    engine = FakeEngine()
    worker_settings = SimpleNamespace(
        domain=LOCAL_DOMAIN,
        database_url=SimpleNamespace(get_secret_value=lambda: "postgresql://unused"),
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: worker_settings)
    monkeypatch.setattr(
        tasks,
        "create_engine_and_sessionmaker",
        lambda _url: (engine, lambda: session),
    )
    queue_tombstone = AsyncMock(return_value=set())
    monkeypatch.setattr(tasks, "queue_terminal_attachment_tombstone", queue_tombstone)
    monkeypatch.setattr(tasks, "lock_media_tombstone_ref", AsyncMock())

    result = await unwrap(tasks.media_terminal_tombstone_sweep.original_func)()

    assert result == 101
    assert queue_tombstone.await_count == 101
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_tombstone_purge_removes_every_cached_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = [
        SimpleNamespace(
            origin_domain=REMOTE_DOMAIN,
            attachment_id=40,
            variant="original",
            object_key="remote/40/original",
            size=512,
        ),
        SimpleNamespace(
            origin_domain=REMOTE_DOMAIN,
            attachment_id=40,
            variant="thumbnail_128",
            object_key="remote/40/thumbnail_128",
            size=128,
        ),
    ]
    session = SimpleNamespace(
        scalars=AsyncMock(return_value=cached),
        execute=AsyncMock(),
        delete=AsyncMock(),
        commit=AsyncMock(),
    )
    drain = AsyncMock(return_value=2)
    monkeypatch.setattr(media_jobs, "S3Storage", lambda _settings: SimpleNamespace())
    monkeypatch.setattr(media_jobs, "drain_remote_media_orphans", drain)

    removed = await media_jobs.purge_remote_attachment_cache(
        cast(Any, session),
        settings(),
        REMOTE_DOMAIN,
        40,
    )

    assert removed == 2
    assert session.execute.await_count == 2
    assert session.delete.await_args_list == [call(cached[0]), call(cached[1])]
    session.commit.assert_awaited_once()
    drain.assert_awaited_once()
