from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError

from app.api import federation as federation_api
from app.bots.runtime_control import (
    APPLICATION_RUNTIME_EVENT,
    ApplicationRuntimeSnapshot,
)
from app.core.dm import group_dm_key
from app.db.models import (
    Channel,
    DMConversation,
    FederationInbox,
    Guild,
    Instance,
    MediaTombstoneDestination,
    MediaTombstoneSource,
    Message,
    PeerKey,
    User,
)
from app.federation import terminal_rooms
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


def guild_terminal(
    *,
    terminal: bool,
    actor_domain: str = AUTHORITY_DOMAIN,
) -> EventEnvelope:
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
        actor_domain=actor_domain,
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
async def test_inbox_quota_locks_do_not_block_federation_foreign_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    monkeypatch.setattr(
        federation_api,
        "federation_storage_quota_exceeded",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(federation_api, "increment_metric", AsyncMock())
    event = envelope(
        "kcfe_quotalockmode001",
        "presence.update",
        actor_id=10,
        actor_domain=AUTHORITY_DOMAIN,
        content={},
        context={},
    )
    global_ledger, peer = ledgers()

    class LockModeSession(ProcessSession):
        def __init__(self) -> None:
            super().__init__(event, [global_ledger, peer], {})
            self.scalar_statements: list[object] = []

        async def scalar(self, statement: object) -> object | None:
            self.scalar_statements.append(statement)
            return await super().scalar(statement)

    session = LockModeSession()

    result = await process(event, session)

    assert cast(Any, result).status == "retry"
    statements = [
        str(cast(Any, statement).compile(dialect=postgresql.dialect()))
        for statement in session.scalar_statements
    ]
    assert len(statements) == 2
    assert all("FOR NO KEY UPDATE" in statement for statement in statements)


@pytest.mark.asyncio
async def test_bot_dm_runtime_preflight_leaves_a_terminal_inbox_after_event_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable runtime update must not commit through the event savepoint."""

    patch_process_dependencies(monkeypatch)
    remote_domain = "zeta.localhost"
    runtime = ApplicationRuntimeSnapshot.model_validate(
        {
            "application_id": "40",
            "application_domain": remote_domain,
            "bot_user_id": "10",
            "bot_user_domain": remote_domain,
            "target_domain": LOCAL_DOMAIN,
            "manifest_generation": "3",
            "revocation_generation": "2",
            "access_revocation_generation": "0",
            "status": "active",
            "target_allowed": True,
            "workers": [],
        }
    )
    runtime_proof = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_runtimepreflight01",
            "origin": remote_domain,
            "type": APPLICATION_RUNTIME_EVENT,
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "10", "domain": remote_domain},
            "context": {},
            "content": runtime.model_dump(mode="json"),
            "signatures": {remote_domain: {"ed25519:test": "signature"}},
        }
    )
    capability_proof = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_capabilitypreflight1",
            "origin": remote_domain,
            "type": "bot.dm.installation-capability",
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "10", "domain": remote_domain},
            "context": {},
            "content": {},
            "signatures": {remote_domain: {"ed25519:test": "signature"}},
        }
    )
    participants = [
        {
            "id": "10",
            "origin_domain": remote_domain,
            "account_type": "bot",
            "username": "weather",
        },
        {
            "id": "20",
            "origin_domain": LOCAL_DOMAIN,
            "account_type": "human",
            "username": "alice",
        },
    ]
    from app.core.dm import dm_pair_key

    pair_key = dm_pair_key("weather@zeta.localhost", "alice@local.localhost")
    event = EventEnvelope.model_validate(
        {
            "event_id": "kcfe_dmruntimepreflight1",
            "origin": remote_domain,
            "type": "dm.open.request",
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "10", "domain": remote_domain},
            "context": {},
            "content": {
                "participants": participants,
                "pair_key": pair_key,
                "bot_capability": capability_proof.model_dump(mode="json"),
                "bot_runtime_proof": runtime_proof.model_dump(mode="json"),
            },
            "signatures": {remote_domain: {"ed25519:test": "signature"}},
        }
    )
    global_ledger, peer = ledgers()
    bot = SimpleNamespace(
        id=10,
        origin_domain=remote_domain,
        account_type="bot",
        username="weather",
    )
    human = SimpleNamespace(
        id=20,
        origin_domain=LOCAL_DOMAIN,
        account_type="human",
        username="alice",
    )

    class StrictProcessSession(ProcessSession):
        def __init__(self) -> None:
            super().__init__(
                event,
                [global_ledger, peer, event.event_id, event.event_id],
                {(User, (10, remote_domain)): bot},
            )
            self.open_savepoints: list[Savepoint] = []
            self.all_savepoints: list[Savepoint] = []

        async def begin_nested(self) -> Savepoint:
            owner = self

            class TrackedSavepoint(Savepoint):
                async def commit(self) -> None:
                    await super().commit()
                    owner.open_savepoints.remove(self)

                async def rollback(self) -> None:
                    await super().rollback()
                    owner.open_savepoints.remove(self)

            savepoint = TrackedSavepoint()
            self.open_savepoints.append(savepoint)
            self.all_savepoints.append(savepoint)
            return savepoint

        async def commit(self) -> None:
            assert not self.open_savepoints, "outer commit crossed an active event savepoint"
            await super().commit()

    session = StrictProcessSession()
    monkeypatch.setattr(
        federation_api,
        "validate_application_runtime_proof",
        AsyncMock(return_value=(runtime_proof, runtime)),
    )
    apply_runtime = AsyncMock(return_value=(True, []))
    monkeypatch.setattr(
        federation_api,
        "apply_application_runtime_control",
        apply_runtime,
    )

    async def upsert(_session: object, _settings: object, profile: object) -> object:
        return bot if cast(Any, profile).account_type == "bot" else human

    monkeypatch.setattr(federation_api, "upsert_remote_user", upsert)
    authorize = AsyncMock(side_effect=ValueError("reject after the runtime preflight"))
    monkeypatch.setattr(federation_api, "_authorize_bot_dm_open_capability", authorize)

    result = await federation_api.process_event(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, settings()),
        FederationPrincipal(origin=remote_domain, key_id="ed25519:test"),
        event,
        cast(Any, object()),
    )

    assert (cast(Any, result).status, session.inbox.status) == ("rejected", "rejected")
    assert session.inbox.processed_at is not None
    assert session.commits == 1
    assert not session.open_savepoints
    assert [(item.commits, item.rollbacks) for item in session.all_savepoints] == [
        (1, 0),
        (0, 1),
    ]
    apply_runtime.assert_awaited_once()
    assert authorize.await_args.kwargs["runtime_preapplied"] is True
    assert authorize.await_args.kwargs["commit_runtime"] is False


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
async def test_terminal_guild_delete_accepts_authority_attested_remote_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=True, actor_domain=CURRENT_ACTOR_DOMAIN)
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
    assert len(session.added) == 1
    receipt = session.added[0]
    assert (cast(Any, receipt).actor_id, cast(Any, receipt).actor_domain) == (
        99,
        CURRENT_ACTOR_DOMAIN,
    )


@pytest.mark.asyncio
async def test_terminal_queue_marks_exact_remote_guild_actor_as_authority_attested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QueueSession:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.added: list[object] = []

        async def scalars(self, _statement: object) -> list[object]:
            self.scalar_calls += 1
            return []

        async def get(self, model: object, _key: object) -> object | None:
            if model is Instance:
                return SimpleNamespace(
                    is_self=True,
                    current_key_id="ed25519:test",
                )
            return None

        async def execute(self, _statement: object) -> None:
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

    session = QueueSession()
    built = {
        "event_id": "kcfe_terminalqueue0001",
        "origin": AUTHORITY_DOMAIN,
        "type": "guild.instance_access.revoked",
        "ts": int(datetime.now(UTC).timestamp() * 1000),
        "actor": {"id": "99", "domain": CURRENT_ACTOR_DOMAIN},
        "content": {
            "reason": "guild_deleted",
            "target_domain": LOCAL_DOMAIN,
            "_terminal_generation": "1",
        },
        "context": {"guild_id": "9", "guild_domain": AUTHORITY_DOMAIN},
        "signatures": {AUTHORITY_DOMAIN: {"ed25519:test": "signature"}},
    }
    build = AsyncMock(return_value=built)
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())
    monkeypatch.setattr(terminal_rooms, "build_envelope", build)
    monkeypatch.setattr(terminal_rooms, "queue_event", AsyncMock())

    wakes = await terminal_rooms.queue_terminal_room_deletion(
        cast(Any, session),
        cast(Any, SimpleNamespace(domain=AUTHORITY_DOMAIN)),
        room_kind="guild",
        room_id=9,
        room_domain=AUTHORITY_DOMAIN,
        actor=terminal_rooms.TerminalRoomActorRef(99, CURRENT_ACTOR_DOMAIN),
        event_type="guild.instance_access.revoked",
        content={"reason": "guild_deleted"},
        context={"guild_id": "9", "guild_domain": AUTHORITY_DOMAIN},
        destinations={LOCAL_DOMAIN},
    )

    assert wakes == {LOCAL_DOMAIN}
    assert build.await_args.kwargs["retained_authority_attested_actor"] is True
    assert len(session.added) == 1


@pytest.mark.asyncio
async def test_nonterminal_guild_control_still_requires_retained_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=False, actor_domain=CURRENT_ACTOR_DOMAIN)
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
async def test_transient_redis_failure_releases_inbox_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=False)
    global_ledger, peer = ledgers()
    guild = SimpleNamespace(
        id=9,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=99,
        owner_domain=AUTHORITY_DOMAIN,
    )
    session = ProcessSession(
        event,
        [global_ledger, peer, event.event_id, event.event_id],
        {(Guild, (9, AUTHORITY_DOMAIN)): guild},
    )
    monkeypatch.setattr(
        federation_api,
        "apply_guild_instance_access_revocation",
        AsyncMock(side_effect=RedisConnectionError("cache unavailable")),
    )

    result = await process(event, session)

    assert (cast(Any, result).status, cast(Any, result).code) == (
        "retry",
        "KAED_FED_EVENT_RETRY",
    )
    assert session.deleted == [session.inbox]
    assert session.inbox.status == "pending"
    assert session.savepoint.rollbacks == 1
    assert peer.federation_inbox_events == 0
    assert global_ledger.federation_inbox_events == 0


@pytest.mark.asyncio
async def test_postgres_deadlock_releases_inbox_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_process_dependencies(monkeypatch)
    event = guild_terminal(terminal=False)
    global_ledger, peer = ledgers()
    guild = SimpleNamespace(
        id=9,
        origin_domain=AUTHORITY_DOMAIN,
        owner_id=99,
        owner_domain=AUTHORITY_DOMAIN,
    )
    session = ProcessSession(
        event,
        [global_ledger, peer, event.event_id, event.event_id],
        {(Guild, (9, AUTHORITY_DOMAIN)): guild},
    )
    asyncpg_deadlock = AsyncAdapt_asyncpg_dbapi.Error(
        "<class 'asyncpg.exceptions.DeadlockDetectedError'>: deadlock detected"
    )
    asyncpg_deadlock.sqlstate = "40P01"
    deadlock = DBAPIError("SELECT ... FOR UPDATE", None, asyncpg_deadlock)
    asyncpg_constraint = AsyncAdapt_asyncpg_dbapi.Error("unique violation")
    asyncpg_constraint.sqlstate = "23505"
    assert not federation_api._is_transient_event_infrastructure_error(
        DBAPIError("INSERT ...", None, asyncpg_constraint)
    )
    monkeypatch.setattr(
        federation_api,
        "apply_guild_instance_access_revocation",
        AsyncMock(side_effect=deadlock),
    )

    result = await process(event, session)

    assert (cast(Any, result).status, cast(Any, result).code) == (
        "retry",
        "KAED_FED_EVENT_RETRY",
    )
    assert session.deleted == [session.inbox]
    assert session.savepoint.rollbacks == 1
    assert peer.federation_inbox_events == 0
    assert global_ledger.federation_inbox_events == 0


@pytest.mark.asyncio
async def test_replicated_group_call_accepts_only_an_exact_redis_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call = {
        "id": "90",
        "channel_id": "12",
        "channel_domain": AUTHORITY_DOMAIN,
        "authority_domain": AUTHORITY_DOMAIN,
        "room": "d.12.90",
        "state": "ringing",
        "created_at": 1_000,
        "ended_at": None,
        "caller": f"99@{AUTHORITY_DOMAIN}",
        "participants": [f"99@{AUTHORITY_DOMAIN}", f"7@{LOCAL_DOMAIN}"],
    }
    identities = set(cast(list[str], call["participants"]))
    monkeypatch.setattr(federation_api, "create_call", AsyncMock(return_value=False))
    monkeypatch.setattr(federation_api, "get_call", AsyncMock(return_value=call))
    monkeypatch.setattr(federation_api, "get_active_call", AsyncMock(return_value=call))
    monkeypatch.setattr(federation_api, "is_call_accepted", AsyncMock(return_value=True))

    await federation_api._ensure_replicated_group_call(
        cast(Any, object()),
        cast(Any, SimpleNamespace()),
        call,
        identities,
    )

    federation_api.get_active_call.return_value = call | {"state": "active"}
    with pytest.raises(ValueError, match="conflicts with active replica state"):
        await federation_api._ensure_replicated_group_call(
            cast(Any, object()),
            cast(Any, SimpleNamespace()),
            call,
            identities,
        )


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
async def test_nonterminal_group_delete_is_not_authority_attested(
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
        "KAED_FED_AUTHOR_ORIGIN_MISMATCH",
    )
    prepare.assert_not_awaited()
    assert channel.unavailable is False
    assert not session.get_calls
