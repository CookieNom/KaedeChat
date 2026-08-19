from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.api import federation as federation_api
from app.core.dm import group_dm_key
from app.db.models import (
    Channel,
    DMConversation,
    FederationInbox,
    Guild,
    MediaTombstoneDestination,
    MediaTombstoneSource,
    Message,
    PeerKey,
)
from app.federation.schemas import EventEnvelope
from app.federation.security import FederationPrincipal

LOCAL_DOMAIN = "local.localhost"
AUTHORITY_DOMAIN = "authority.localhost"
CURRENT_ACTOR_DOMAIN = "current-owner.localhost"


def settings() -> SimpleNamespace:
    return SimpleNamespace(
        domain=LOCAL_DOMAIN,
        federation_clock_skew_seconds=60,
        federation_event_retention_days=30,
    )


def envelope(
    event_id: str,
    event_type: str,
    *,
    actor_id: int,
    actor_domain: str,
    content: dict[str, object],
    context: dict[str, object],
) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": event_id,
            "origin": AUTHORITY_DOMAIN,
            "type": event_type,
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": str(actor_id), "domain": actor_domain},
            "content": content,
            "context": context,
            "signatures": {AUTHORITY_DOMAIN: {"ed25519:test": "signature"}},
        }
    )


def guild_media_request() -> EventEnvelope:
    return envelope(
        "kcfe_mediarequest0001",
        "guild.media.delete.request",
        actor_id=99,
        actor_domain=AUTHORITY_DOMAIN,
        content={
            "guild": {"id": "9", "origin_domain": AUTHORITY_DOMAIN},
            "message": {"id": "11", "origin_domain": AUTHORITY_DOMAIN},
            "attachment": {"id": "41", "origin_domain": LOCAL_DOMAIN},
            "deleted_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "_deletion_generation": "1",
        },
        context={},
    )


def guild_terminal(*, terminal: bool) -> EventEnvelope:
    content: dict[str, object] = {
        "target_domain": LOCAL_DOMAIN,
        "reason": "guild_deleted" if terminal else "instance_banned",
    }
    if terminal:
        content["_terminal_generation"] = "1"
    return envelope(
        "kcfe_guildterminal001" if terminal else "kcfe_guildordinary001",
        "guild.instance_access.revoked",
        actor_id=99,
        actor_domain=AUTHORITY_DOMAIN,
        content=content,
        context={"guild_id": "9", "guild_domain": AUTHORITY_DOMAIN},
    )


def group_terminal(*, terminal: bool) -> EventEnvelope:
    content: dict[str, object] = {
        "conversation": {
            "id": "12",
            "origin_domain": AUTHORITY_DOMAIN,
            "pair_key": group_dm_key(AUTHORITY_DOMAIN, 12),
            "type": "group",
            "authority_domain": AUTHORITY_DOMAIN,
            "owner": {"id": "99", "origin_domain": CURRENT_ACTOR_DOMAIN},
            "name": "Transferred room",
            "state_version": "2",
            "deleted": True,
            "encryption_policy": {
                "mode": "plaintext",
                "state": "plaintext",
                "generation": "0",
                "protocol": None,
                "suite": None,
                "group_id": None,
                "epoch": None,
            },
        },
        "participants": [],
    }
    if terminal:
        content["_terminal_generation"] = "1"
    return envelope(
        "kcfe_groupterminal001" if terminal else "kcfe_groupordinary001",
        "dm.group.state",
        actor_id=99,
        actor_domain=CURRENT_ACTOR_DOMAIN,
        content=content,
        context={},
    )


class Savepoint:
    def __init__(self) -> None:
        self.is_active = True
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1
        self.is_active = False

    async def rollback(self) -> None:
        self.rollbacks += 1
        self.is_active = False


class ProcessSession:
    def __init__(
        self,
        event: EventEnvelope,
        scalar_results: list[object | None],
        values: dict[tuple[object, object], object | None],
    ) -> None:
        self.event = event
        self.scalar_results = deque(scalar_results)
        self.values = values
        self.inbox = SimpleNamespace(
            status="pending",
            result_code=None,
            error=None,
            processed_at=None,
        )
        self.inbox_claimed = False
        self.savepoint = Savepoint()
        self.deleted: list[object] = []
        self.added: list[object] = []
        self.get_calls: list[tuple[object, object]] = []
        self.commits = 0
        self.rollbacks = 0

    async def get(
        self,
        model: object,
        key: object,
        **_kwargs: object,
    ) -> object | None:
        self.get_calls.append((model, key))
        if model is PeerKey:
            return object()
        if model is FederationInbox:
            return self.inbox if self.inbox_claimed else None
        return self.values.get((model, key))

    async def scalar(self, _statement: object) -> object | None:
        if not self.scalar_results:
            raise AssertionError("unexpected scalar query")
        result = self.scalar_results.popleft()
        if result == self.event.event_id and not self.inbox_claimed:
            self.inbox_claimed = True
        return result

    async def scalars(self, _statement: object) -> list[object]:
        return []

    async def execute(self, _statement: object) -> list[object]:
        return []

    async def flush(self) -> None:
        return None

    async def begin_nested(self) -> Savepoint:
        return self.savepoint

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    def add(self, value: object) -> None:
        self.added.append(value)


def ledgers() -> tuple[SimpleNamespace, SimpleNamespace]:
    global_ledger = SimpleNamespace(
        is_self=True,
        federation_inbox_events=0,
        federation_inbox_event_bytes=0,
    )
    peer = SimpleNamespace(
        is_self=False,
        federation_inbox_events=0,
        federation_inbox_event_bytes=0,
    )
    return global_ledger, peer


def patch_process_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(federation_api, "peer_key_needs_refresh", lambda *_args: False)
    monkeypatch.setattr(federation_api, "verify_event_signature", lambda *_args: True)
    monkeypatch.setattr(federation_api, "lock_terminal_room", AsyncMock())
    monkeypatch.setattr(federation_api, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(federation_api, "lock_terminal_room_media_fences", AsyncMock())
    monkeypatch.setattr(
        federation_api, "federation_storage_quota_exceeded", lambda *_a, **_k: False
    )
    monkeypatch.setattr(federation_api, "admit_replica_storage", AsyncMock())
    monkeypatch.setattr(federation_api, "publish_dispatch", AsyncMock())
    monkeypatch.setattr(federation_api, "enqueue_best_effort", AsyncMock())


async def process(event: EventEnvelope, session: ProcessSession) -> object:
    return await federation_api.process_event(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, settings()),
        FederationPrincipal(origin=AUTHORITY_DOMAIN, key_id="ed25519:test"),
        event,
        cast(Any, object()),
    )


@pytest.mark.asyncio
async def test_exact_guild_media_request_does_not_consult_stale_guild_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_media_request()
    global_ledger, peer = ledgers()
    attachment = SimpleNamespace(
        id=41,
        origin_domain=LOCAL_DOMAIN,
        message_id=11,
        message_domain=AUTHORITY_DOMAIN,
        staging_object_key=None,
        object_key="local/41/clean/original",
    )
    route = SimpleNamespace(
        room_kind="guild",
        room_id=9,
        room_domain=AUTHORITY_DOMAIN,
    )
    session = ProcessSession(
        event,
        [global_ledger, peer, event.event_id, event.event_id, attachment],
        {
            (MediaTombstoneSource, (41, LOCAL_DOMAIN)): None,
            (
                MediaTombstoneDestination,
                (41, LOCAL_DOMAIN, AUTHORITY_DOMAIN),
            ): route,
            (Message, (11, AUTHORITY_DOMAIN)): None,
        },
    )
    queue = AsyncMock(return_value=set())
    monkeypatch.setattr(federation_api, "queue_terminal_attachment_tombstone", queue)
    monkeypatch.setattr(federation_api, "discard_attachment", AsyncMock())

    result = await process(event, session)

    assert cast(Any, result).status == "accepted", session.inbox.error
    assert all(model is not Guild for model, _key in session.get_calls)
    queue.assert_awaited_once_with(
        session,
        settings(),
        attachment,
        force_authoritative=True,
    )
    assert attachment.staging_object_key == attachment.object_key
    assert (attachment.message_id, attachment.message_domain) == (None, None)


@pytest.mark.asyncio
async def test_terminal_guild_delete_accepts_current_actor_over_stale_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=True)
    global_ledger, peer = ledgers()
    stale_guild = SimpleNamespace(
        id=9,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=7,
        owner_domain=AUTHORITY_DOMAIN,
    )
    session = ProcessSession(
        event,
        [None, global_ledger, peer, event.event_id, event.event_id],
        {(Guild, (9, AUTHORITY_DOMAIN)): stale_guild},
    )
    prepare = AsyncMock(return_value=([], set(), set()))
    apply = AsyncMock(return_value=[])
    monkeypatch.setattr(federation_api, "prepare_terminal_guild_media", prepare)
    monkeypatch.setattr(federation_api, "apply_guild_instance_access_revocation", apply)

    result = await process(event, session)

    assert cast(Any, result).status == "accepted", session.inbox.error
    prepare.assert_awaited_once_with(session, settings(), stale_guild)
    apply.assert_awaited_once_with(
        session,
        settings(),
        stale_guild,
        target_domain=LOCAL_DOMAIN,
    )
    assert len(session.added) == 1
    receipt = session.added[0]
    assert (cast(Any, receipt).actor_id, cast(Any, receipt).actor_domain) == (
        99,
        AUTHORITY_DOMAIN,
    )


@pytest.mark.asyncio
async def test_nonterminal_guild_control_still_requires_retained_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=False)
    global_ledger, peer = ledgers()
    stale_guild = SimpleNamespace(
        id=9,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=7,
        owner_domain=AUTHORITY_DOMAIN,
    )
    session = ProcessSession(
        event,
        [global_ledger, peer, event.event_id, event.event_id],
        {(Guild, (9, AUTHORITY_DOMAIN)): stale_guild},
    )
    apply = AsyncMock()
    monkeypatch.setattr(federation_api, "apply_guild_instance_access_revocation", apply)

    result = await process(event, session)

    assert (cast(Any, result).status, cast(Any, result).code) == (
        "rejected",
        "KAED_FED_EVENT_REJECTED",
    )
    apply.assert_not_awaited()
    assert "not signed for the guild owner" in session.inbox.error


@pytest.mark.asyncio
async def test_terminal_group_delete_accepts_actor_absent_from_stale_participants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = group_terminal(terminal=True)
    global_ledger, peer = ledgers()
    conversation = SimpleNamespace(
        id=12,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=7,
        owner_domain=AUTHORITY_DOMAIN,
        state_version=1,
    )
    channel = SimpleNamespace(
        id=12,
        origin_domain=AUTHORITY_DOMAIN,
        unavailable=False,
    )
    stale_participant = SimpleNamespace(id=7, origin_domain=AUTHORITY_DOMAIN)
    session = ProcessSession(
        event,
        [None, global_ledger, peer, event.event_id, event.event_id],
        {
            (DMConversation, (12, AUTHORITY_DOMAIN)): conversation,
            (Channel, (12, AUTHORITY_DOMAIN)): channel,
        },
    )
    participants = AsyncMock(return_value=[stale_participant])
    prepare = AsyncMock(return_value=([], set(), set()))
    monkeypatch.setattr(federation_api, "group_participants", participants)
    monkeypatch.setattr(federation_api, "prepare_terminal_channel_media", prepare)
    monkeypatch.setattr(
        federation_api,
        "reload_group_projection",
        AsyncMock(return_value=(conversation, channel, [])),
    )
    monkeypatch.setattr(federation_api, "dm_history_metadata", lambda *_a, **_k: {})
    monkeypatch.setattr(
        federation_api,
        "dm_authority_history_available",
        AsyncMock(return_value=False),
    )

    result = await process(event, session)

    assert cast(Any, result).status == "accepted"
    prepare.assert_awaited_once_with(session, settings(), channel)
    assert channel.unavailable is True
    assert conversation.state_version == 2
    assert len(session.added) == 1
    receipt = session.added[0]
    assert (cast(Any, receipt).actor_id, cast(Any, receipt).actor_domain) == (
        99,
        CURRENT_ACTOR_DOMAIN,
    )


@pytest.mark.asyncio
async def test_nonterminal_group_delete_still_requires_stored_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = group_terminal(terminal=False)
    global_ledger, peer = ledgers()
    conversation = SimpleNamespace(
        id=12,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=7,
        owner_domain=AUTHORITY_DOMAIN,
        state_version=1,
    )
    channel = SimpleNamespace(
        id=12,
        origin_domain=AUTHORITY_DOMAIN,
        unavailable=False,
    )
    session = ProcessSession(
        event,
        [global_ledger, peer, event.event_id, event.event_id],
        {
            (DMConversation, (12, AUTHORITY_DOMAIN)): conversation,
            (Channel, (12, AUTHORITY_DOMAIN)): channel,
        },
    )
    monkeypatch.setattr(
        federation_api,
        "group_participants",
        AsyncMock(return_value=[SimpleNamespace(id=7, origin_domain=AUTHORITY_DOMAIN)]),
    )
    prepare = AsyncMock()
    monkeypatch.setattr(federation_api, "prepare_terminal_channel_media", prepare)

    result = await process(event, session)

    assert (cast(Any, result).status, cast(Any, result).code) == (
        "rejected",
        "KAED_FED_EVENT_REJECTED",
    )
    prepare.assert_not_awaited()
    assert channel.unavailable is False
    assert "actor is not a participant" in session.inbox.error
