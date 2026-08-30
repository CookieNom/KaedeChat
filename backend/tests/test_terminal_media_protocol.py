from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, call

import pytest
from sqlalchemy.dialects import postgresql

from app.api import federation as federation_api
from app.db.models import (
    Channel,
    DMConversation,
    FederationEvent,
    Guild,
    GuildMember,
    Instance,
    PeerKey,
)
from app.federation import events as federation_events
from app.federation import history as federation_history
from app.federation import terminal_rooms
from app.federation.schemas import EventEnvelope
from app.federation.security import FederationPrincipal
from app.media import jobs as media_jobs
from app.media import tombstones
from app.media.storage import StorageError

LOCAL_DOMAIN = "relay.localhost"
ORIGIN_DOMAIN = "origin.localhost"
UPSTREAM_DOMAIN = "upstream.localhost"
CHILD_DOMAIN = "child.localhost"
SECOND_CHILD_DOMAIN = "second-child.localhost"


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "domain": LOCAL_DOMAIN,
        "federation_clock_skew_seconds": 60,
        "federation_event_retention_days": 30,
        "federation_history_merge_chunk_size": 100,
        "media_attachments_bucket": "kaede-attachments",
        "media_derived_bucket": "kaede-derived",
        "media_max_attachment_bytes": 15 * 1024 * 1024,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def sql(statement: object) -> str:
    return str(
        cast(Any, statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def media_delete_envelope(
    *, event_id: str, generation: int, actor_domain: str = ORIGIN_DOMAIN
) -> EventEnvelope:
    return EventEnvelope.model_validate(
        {
            "event_id": event_id,
            "origin": ORIGIN_DOMAIN,
            "type": "media.delete",
            "ts": int(datetime.now(UTC).timestamp() * 1000),
            "actor": {"id": "7", "domain": actor_domain},
            "context": {},
            "content": {
                "attachment_id": "41",
                "origin_domain": ORIGIN_DOMAIN,
                "generation": str(generation),
            },
            "signatures": {ORIGIN_DOMAIN: {"ed25519:e2": "signature"}},
        }
    )


class TupleResult:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def tuples(self) -> TupleResult:
        return self

    def __iter__(self) -> Any:
        return iter(self.rows)


def test_post_commit_result_preserves_cascade_retry() -> None:
    pending = federation_api.post_commit_inbox_result(
        "kcfe_2222222222222222",
        media_delete_cascade_pending=True,
    )
    complete = federation_api.post_commit_inbox_result(
        "kcfe_2222222222222222",
        media_delete_cascade_pending=False,
    )

    assert pending.status == "retry"
    assert pending.code == "KAED_FED_MEDIA_DELETE_CASCADE_PENDING"
    assert complete.status == "accepted"
    assert complete.code is None


@pytest.mark.parametrize(
    ("incoming_generation", "event_id", "expected_status", "expected_code"),
    [
        (1, "kcfe_1111111111111111", "duplicate", None),
        (
            2,
            "kcfe_conflict22222222",
            "rejected",
            "KAED_FED_MEDIA_DELETE_GENERATION_CONFLICT",
        ),
    ],
)
def test_superseded_media_delete_replay_classification(
    incoming_generation: int,
    event_id: str,
    expected_status: str,
    expected_code: str | None,
) -> None:
    result = federation_api.superseded_media_delete_result(
        event_id,
        incoming_generation=incoming_generation,
        selected_event_id="kcfe_2222222222222222",
        selected_generation=2,
    )

    assert result is not None
    assert result.status == expected_status
    assert result.code == expected_code


def test_exact_current_media_delete_uses_dynamic_cascade_path() -> None:
    assert (
        federation_api.superseded_media_delete_result(
            "kcfe_2222222222222222",
            incoming_generation=2,
            selected_event_id="kcfe_2222222222222222",
            selected_generation=2,
        )
        is None
    )


@pytest.mark.asyncio
async def test_process_event_exact_proof_dynamically_upgrades_retry_to_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = media_delete_envelope(
        event_id="kcfe_2222222222222222",
        generation=2,
        actor_domain="uploader.localhost",
    )
    source = SimpleNamespace(
        attachment_id=41,
        attachment_domain=ORIGIN_DOMAIN,
        event_id=envelope.event_id,
        generation=2,
    )

    class Session:
        def __init__(self) -> None:
            self.rollbacks = 0

        async def get(
            self,
            model: object,
            key: object,
            **_kwargs: object,
        ) -> object | None:
            if model is PeerKey and key == (ORIGIN_DOMAIN, "ed25519:e2"):
                return object()
            raise AssertionError(f"unexpected get: {model!r} {key!r}")

        async def scalar(self, _statement: object) -> object:
            return source

        async def rollback(self) -> None:
            self.rollbacks += 1

    session = Session()
    monkeypatch.setattr(
        federation_api, "federation_event_policy_code", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(federation_api, "peer_key_needs_refresh", lambda *_args: False)
    monkeypatch.setattr(federation_api, "verify_event_signature", lambda *_args: True)
    monkeypatch.setattr(
        federation_api,
        "locked_retained_media_delete_events",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(federation_api, "lock_media_tombstone_ref", AsyncMock())
    cascade = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(federation_api, "media_delete_cascade_is_complete", cascade)

    principal = FederationPrincipal(origin=UPSTREAM_DOMAIN, key_id="ed25519:upstream")
    first = await federation_api.process_event(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, settings()),
        principal,
        envelope,
        cast(Any, object()),
    )
    second = await federation_api.process_event(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, settings()),
        principal,
        envelope,
        cast(Any, object()),
    )

    assert (first.status, first.code) == (
        "retry",
        "KAED_FED_MEDIA_DELETE_CASCADE_PENDING",
    )
    assert (second.status, second.code) == ("duplicate", None)
    assert cascade.await_count == 2
    assert session.rollbacks == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "generation", "expected_status", "expected_code"),
    [
        ("kcfe_1111111111111111", 1, "duplicate", None),
        (
            "kcfe_conflict22222222",
            2,
            "rejected",
            "KAED_FED_MEDIA_DELETE_GENERATION_CONFLICT",
        ),
    ],
)
async def test_process_event_classifies_superseded_proof_before_inbox_claim(
    monkeypatch: pytest.MonkeyPatch,
    event_id: str,
    generation: int,
    expected_status: str,
    expected_code: str | None,
) -> None:
    envelope = media_delete_envelope(event_id=event_id, generation=generation)
    source = SimpleNamespace(
        attachment_id=41,
        attachment_domain=ORIGIN_DOMAIN,
        event_id="kcfe_2222222222222222",
        generation=2,
    )

    class Session:
        def __init__(self) -> None:
            self.rollback = AsyncMock()

        async def get(
            self,
            model: object,
            key: object,
            **_kwargs: object,
        ) -> object | None:
            if model is PeerKey and key == (ORIGIN_DOMAIN, "ed25519:e2"):
                return object()
            raise AssertionError(f"replay must return before another get: {model!r} {key!r}")

        async def scalar(self, _statement: object) -> object:
            return source

    session = Session()
    monkeypatch.setattr(
        federation_api, "federation_event_policy_code", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(federation_api, "peer_key_needs_refresh", lambda *_args: False)
    monkeypatch.setattr(federation_api, "verify_event_signature", lambda *_args: True)
    monkeypatch.setattr(
        federation_api,
        "locked_retained_media_delete_events",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(federation_api, "lock_media_tombstone_ref", AsyncMock())
    cascade = AsyncMock(side_effect=AssertionError("superseded proof must not check child ACKs"))
    monkeypatch.setattr(federation_api, "media_delete_cascade_is_complete", cascade)

    result = await federation_api.process_event(
        cast(Any, session),
        cast(Any, object()),
        cast(Any, settings()),
        FederationPrincipal(origin=UPSTREAM_DOMAIN, key_id="ed25519:upstream"),
        envelope,
        cast(Any, object()),
    )

    assert (result.status, result.code) == (expected_status, expected_code)
    session.rollback.assert_awaited_once()
    cascade.assert_not_awaited()


@pytest.mark.asyncio
async def test_cascade_ack_requires_exact_current_event_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    historical = AsyncMock(
        return_value={
            LOCAL_DOMAIN,
            ORIGIN_DOMAIN,
            UPSTREAM_DOMAIN,
            CHILD_DOMAIN,
            SECOND_CHILD_DOMAIN,
        }
    )
    monkeypatch.setattr(
        federation_api,
        "historical_attachment_destinations_by_ref",
        historical,
    )

    class Session:
        def __init__(self) -> None:
            self.delivered: set[str] = {CHILD_DOMAIN}
            self.statements: list[object] = []

        async def scalars(self, statement: object) -> list[str]:
            self.statements.append(statement)
            return sorted(self.delivered)

    session = Session()
    pending = await federation_api.media_delete_cascade_is_complete(
        cast(Any, session),
        cast(Any, settings()),
        attachment_id=41,
        attachment_domain=ORIGIN_DOMAIN,
        event_id="kcfe_2222222222222222",
        upstream_domain=UPSTREAM_DOMAIN,
    )
    session.delivered.add(SECOND_CHILD_DOMAIN)
    complete = await federation_api.media_delete_cascade_is_complete(
        cast(Any, session),
        cast(Any, settings()),
        attachment_id=41,
        attachment_domain=ORIGIN_DOMAIN,
        event_id="kcfe_2222222222222222",
        upstream_domain=UPSTREAM_DOMAIN,
    )

    assert pending is False
    assert complete is True
    statement_sql = sql(session.statements[0])
    assert "federation_outbox.event_id = 'kcfe_2222222222222222'" in statement_sql
    assert "federation_outbox.event_origin_domain = 'origin.localhost'" in statement_sql
    assert "federation_outbox.status = 'delivered'" in statement_sql
    assert "child.localhost" in statement_sql
    assert "second-child.localhost" in statement_sql


@pytest.mark.asyncio
async def test_retained_proof_selection_prefers_e2_over_newer_timestamp_e1() -> None:
    e1 = SimpleNamespace(
        event_id="kcfe_1111111111111111",
        envelope={
            "event_id": "kcfe_1111111111111111",
            "type": "media.delete",
            "ts": 999,
            "content": {
                "attachment_id": "41",
                "origin_domain": ORIGIN_DOMAIN,
                "generation": "1",
            },
        },
    )
    e2 = SimpleNamespace(
        event_id="kcfe_2222222222222222",
        envelope={
            "event_id": "kcfe_2222222222222222",
            "type": "media.delete",
            "ts": 1,
            "content": {
                "attachment_id": "41",
                "origin_domain": ORIGIN_DOMAIN,
                "generation": "2",
            },
        },
    )

    class Session:
        def __init__(self) -> None:
            self.statement: object | None = None

        async def scalars(self, statement: object) -> list[object]:
            self.statement = statement
            return [e1, e2]

    session = Session()
    selected = await federation_events.retained_media_delete_events(
        cast(Any, session),
        41,
        ORIGIN_DOMAIN,
    )

    assert selected == [e2, e1]
    assert session.statement is not None
    statement_sql = sql(session.statement)
    assert "federation_events.event_type = 'media.delete'" in statement_sql
    assert "origin.localhost" in statement_sql
    assert "41" in statement_sql


@pytest.mark.asyncio
async def test_key_rotation_queues_e2_and_compacts_pending_e1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    e1 = {
        "event_id": "kcfe_1111111111111111",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 1,
        "content": {
            "attachment_id": "41",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "1",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:e1": "signature"}},
    }
    e2 = {
        "event_id": "kcfe_2222222222222222",
        "origin": LOCAL_DOMAIN,
        "type": "media.delete",
        "ts": 2,
        "content": {
            "attachment_id": "41",
            "origin_domain": LOCAL_DOMAIN,
            "generation": "2",
        },
        "signatures": {LOCAL_DOMAIN: {"ed25519:e2": "signature"}},
    }
    source = SimpleNamespace(
        attachment_id=41,
        attachment_domain=LOCAL_DOMAIN,
        signer_id=7,
        signer_domain=LOCAL_DOMAIN,
        event_id=e1["event_id"],
        key_id="ed25519:e1",
        generation=1,
        updated_at=None,
    )
    instance = SimpleNamespace(is_self=True, current_key_id="ed25519:e2")

    class Session:
        def __init__(self) -> None:
            self.flush = AsyncMock()
            self.statements: list[object] = []

        async def scalar(self, _statement: object) -> object:
            return source

        async def scalars(self, _statement: object) -> list[str]:
            return [CHILD_DOMAIN]

        async def get(self, model: object, key: object) -> object | None:
            if model is Instance and key == LOCAL_DOMAIN:
                return instance
            if model is FederationEvent and key == (LOCAL_DOMAIN, e1["event_id"]):
                return SimpleNamespace(event_id=e1["event_id"], envelope=e1)
            raise AssertionError(f"unexpected get: {model!r} {key!r}")

        async def execute(self, statement: object) -> None:
            self.statements.append(statement)

    session = Session()
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", AsyncMock())
    monkeypatch.setattr(tombstones, "build_envelope", AsyncMock(return_value=e2))
    monkeypatch.setattr(
        tombstones,
        "_retain_media_delete_event",
        AsyncMock(return_value=SimpleNamespace(envelope=e2)),
    )
    queue = AsyncMock()
    monkeypatch.setattr(tombstones, "queue_event", queue)
    monkeypatch.setattr(tombstones, "record_media_tombstone_destinations", AsyncMock())

    destinations = await tombstones.queue_media_delete_tombstone(
        cast(Any, session),
        cast(Any, settings()),
        attachment_id=41,
        attachment_domain=LOCAL_DOMAIN,
        destinations=set(),
    )

    assert destinations == {CHILD_DOMAIN}
    queue.assert_awaited_once_with(session, settings(), CHILD_DOMAIN, e2)
    assert (source.event_id, source.key_id, source.generation) == (
        e2["event_id"],
        "ed25519:e2",
        2,
    )
    session.flush.assert_awaited_once()
    assert len(session.statements) == 1
    compact_sql = sql(session.statements[0])
    assert "DELETE FROM federation_events" in compact_sql
    assert "federation_events.event_id != 'kcfe_2222222222222222'" in compact_sql
    assert "media.delete" in compact_sql


def test_media_cleanup_filters_blocked_sources_before_the_batch_limit() -> None:
    now = datetime.now(UTC)
    statement_sql = sql(
        tombstones._media_tombstone_cleanup_candidates(
            cast(Any, settings()),
            now=now,
            cutoff=now - timedelta(days=30),
            limit=17,
        )
    )

    # Every reason the cleanup loop can skip a source is correlated in the
    # candidate query. A fixed prefix of live attachments, cached variants,
    # offline destinations, or retained carriers therefore cannot consume the
    # LIMIT and starve later cleanable rows.
    assert "LIMIT 17" in statement_sql
    assert "attachments.deleted_at IS NULL" in statement_sql
    assert "remote_media_cache" in statement_sql
    assert "federation_outbox.status = 'delivered'" in statement_sql
    assert "federation_events" in statement_sql
    assert "guild_events" in statement_sql
    assert "guild_history_staged_messages" in statement_sql
    assert statement_sql.count("NOT (EXISTS") >= 6


def test_terminal_cleanup_filters_blocked_rooms_before_the_batch_limit() -> None:
    now = datetime.now(UTC)
    statement_sql = sql(
        terminal_rooms._terminal_room_cleanup_candidates(
            cast(Any, settings()),
            cutoff=now - timedelta(days=30),
            limit=19,
        )
    )

    assert "LIMIT 19" in statement_sql
    assert "acknowledged_at IS NULL" in statement_sql
    assert "media_tombstone_destinations" in statement_sql
    assert "guilds" in statement_sql
    assert "dm_conversations" in statement_sql
    assert "channels.unavailable IS true" in statement_sql
    assert "dm_participants" in statement_sql
    assert "messages" in statement_sql


@pytest.mark.asyncio
async def test_terminal_sender_cleanup_waits_for_every_ack_and_then_removes_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    rows = [
        SimpleNamespace(
            room_kind="guild",
            room_id=9,
            room_domain=LOCAL_DOMAIN,
            destination_domain=CHILD_DOMAIN,
            event_id="kcfe_1111111111111111",
            acknowledged_at=old,
            updated_at=old,
        ),
        SimpleNamespace(
            room_kind="guild",
            room_id=9,
            room_domain=LOCAL_DOMAIN,
            destination_domain=SECOND_CHILD_DOMAIN,
            event_id="kcfe_2222222222222222",
            acknowledged_at=None,
            updated_at=old,
        ),
    ]

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            if sql(statement).startswith("SELECT"):
                return TupleResult([("guild", 9, LOCAL_DOMAIN)])
            return TupleResult([])

        async def scalars(self, _statement: object) -> list[object]:
            return rows

        async def get(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("sender cleanup must not consult a local room projection")

    session = Session()
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())
    first = await terminal_rooms.cleanup_terminal_room_deletions(
        cast(Any, session),
        cast(Any, settings()),
        now=datetime.now(UTC),
    )

    assert first == 0
    assert len(session.statements) == 1

    rows[1].acknowledged_at = old
    second = await terminal_rooms.cleanup_terminal_room_deletions(
        cast(Any, session),
        cast(Any, settings()),
        now=datetime.now(UTC),
    )

    assert second == 2
    delete_sql = [sql(statement) for statement in session.statements[2:]]
    assert any("DELETE FROM media_tombstone_destinations" in value for value in delete_sql)
    assert any("DELETE FROM room_federation_recipients" in value for value in delete_sql)
    assert any("DELETE FROM terminal_room_deletions" in value for value in delete_sql)
    assert any("DELETE FROM federation_inbox" in value for value in delete_sql)
    assert any("DELETE FROM federation_events" in value for value in delete_sql)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("projection_exists", "route_exists", "expected_cleaned"),
    [(True, False, 0), (False, True, 0), (False, False, 1)],
)
async def test_terminal_receiver_cleanup_requires_projection_and_routes_to_be_absent(
    monkeypatch: pytest.MonkeyPatch,
    projection_exists: bool,
    route_exists: bool,
    expected_cleaned: int,
) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    row = SimpleNamespace(
        room_kind="guild",
        room_id=9,
        room_domain=ORIGIN_DOMAIN,
        destination_domain=LOCAL_DOMAIN,
        event_id="kcfe_2222222222222222",
        acknowledged_at=old,
        updated_at=old,
    )

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return TupleResult([("guild", 9, ORIGIN_DOMAIN)])
            return TupleResult([])

        async def scalars(self, _statement: object) -> list[object]:
            return [row]

        async def get(self, model: object, key: object) -> object | None:
            assert model is Guild
            assert key == (9, ORIGIN_DOMAIN)
            return object() if projection_exists else None

        async def scalar(self, _statement: object) -> int | None:
            return 41 if route_exists else None

    session = Session()
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())
    cleaned = await terminal_rooms.cleanup_terminal_room_deletions(
        cast(Any, session),
        cast(Any, settings()),
        now=datetime.now(UTC),
    )

    assert cleaned == expected_cleaned
    destructive = [
        value for value in map(sql, session.statements[1:]) if value.startswith("DELETE")
    ]
    assert bool(destructive) is bool(expected_cleaned)


@pytest.mark.asyncio
async def test_terminal_receiver_cleanup_removes_empty_unavailable_group_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    row = SimpleNamespace(
        room_kind="group_dm",
        room_id=9,
        room_domain=ORIGIN_DOMAIN,
        destination_domain=LOCAL_DOMAIN,
        event_id="kcfe_2222222222222222",
        acknowledged_at=old,
        updated_at=old,
    )
    conversation = SimpleNamespace(type="group")
    channel = SimpleNamespace(unavailable=True, guild_id=None)

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []
            self.delete = AsyncMock()
            self.flush = AsyncMock()

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return TupleResult([("group_dm", 9, ORIGIN_DOMAIN)])
            return TupleResult([])

        async def scalars(self, _statement: object) -> list[object]:
            return [row]

        async def scalar(self, _statement: object) -> None:
            # No retained media route, participant, or message remains.
            return None

        async def get(self, model: object, key: object) -> object | None:
            assert key == (9, ORIGIN_DOMAIN)
            if model is DMConversation:
                return conversation
            if model is Channel:
                return channel
            raise AssertionError(f"unexpected projection lookup: {model!r}")

    session = Session()
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())

    cleaned = await terminal_rooms.cleanup_terminal_room_deletions(
        cast(Any, session),
        cast(Any, settings()),
        now=datetime.now(UTC),
    )

    assert cleaned == 1
    session.delete.assert_awaited_once_with(conversation)
    session.flush.assert_awaited_once()
    destructive = [
        value for value in map(sql, session.statements[1:]) if value.startswith("DELETE")
    ]
    assert any("DELETE FROM room_federation_recipients" in value for value in destructive)
    assert any("DELETE FROM terminal_room_deletions" in value for value in destructive)
    assert any("DELETE FROM federation_inbox" in value for value in destructive)
    assert any("DELETE FROM federation_events" in value for value in destructive)


@pytest.mark.asyncio
async def test_terminal_receiver_cleanup_preserves_nonterminal_group_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(days=60)
    row = SimpleNamespace(
        room_kind="group_dm",
        room_id=9,
        room_domain=ORIGIN_DOMAIN,
        destination_domain=LOCAL_DOMAIN,
        event_id="kcfe_2222222222222222",
        acknowledged_at=old,
        updated_at=old,
    )
    conversation = SimpleNamespace(type="group")
    channel = SimpleNamespace(unavailable=False, guild_id=None)

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []
            self.delete = AsyncMock()
            self.flush = AsyncMock()

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return TupleResult([("group_dm", 9, ORIGIN_DOMAIN)])
            return TupleResult([])

        async def scalars(self, _statement: object) -> list[object]:
            return [row]

        async def scalar(self, _statement: object) -> None:
            return None

        async def get(self, model: object, key: object) -> object | None:
            assert key == (9, ORIGIN_DOMAIN)
            if model is DMConversation:
                return conversation
            if model is Channel:
                return channel
            raise AssertionError(f"unexpected projection lookup: {model!r}")

    session = Session()
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())

    cleaned = await terminal_rooms.cleanup_terminal_room_deletions(
        cast(Any, session),
        cast(Any, settings()),
        now=datetime.now(UTC),
    )

    assert cleaned == 0
    session.delete.assert_not_awaited()
    session.flush.assert_not_awaited()
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_absent_room_cleanup_uses_durable_media_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_attachment = SimpleNamespace(
        id=41,
        origin_domain=LOCAL_DOMAIN,
        message_id=11,
        message_domain=ORIGIN_DOMAIN,
    )
    routes = [
        SimpleNamespace(
            attachment_id=41,
            attachment_domain=LOCAL_DOMAIN,
            destination_domain=CHILD_DOMAIN,
        ),
        SimpleNamespace(
            attachment_id=42,
            attachment_domain=ORIGIN_DOMAIN,
            destination_domain=SECOND_CHILD_DOMAIN,
        ),
    ]

    class Session:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.scalar_pages = [routes, [local_attachment], [UPSTREAM_DOMAIN]]
            self.statements: list[object] = []

        async def scalar(self, statement: object) -> object:
            self.scalar_calls += 1
            self.statements.append(statement)
            return None

        async def scalars(self, statement: object) -> list[object]:
            self.statements.append(statement)
            return self.scalar_pages.pop(0)

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            return TupleResult([])

    session = Session()
    monkeypatch.setattr(terminal_rooms, "lock_terminal_room", AsyncMock())
    media_locks = AsyncMock()
    monkeypatch.setattr(tombstones, "lock_media_tombstone_ref", media_locks)
    monkeypatch.setattr(
        tombstones,
        "historical_attachment_destinations",
        AsyncMock(return_value={"former-viewer.localhost"}),
    )
    queue = AsyncMock(return_value={"relay-child.localhost"})
    monkeypatch.setattr(tombstones, "queue_terminal_attachment_tombstone", queue)

    (
        local_purges,
        remote_purges,
        destinations,
        wakes,
    ) = await tombstones.prepare_terminal_room_media_by_ref(
        cast(Any, session),
        cast(Any, settings()),
        room_kind="guild",
        room_id=9,
        room_domain=ORIGIN_DOMAIN,
    )

    assert local_purges == [(41, LOCAL_DOMAIN)]
    assert remote_purges == [(ORIGIN_DOMAIN, 42)]
    assert destinations == {
        ORIGIN_DOMAIN,
        UPSTREAM_DOMAIN,
        CHILD_DOMAIN,
        SECOND_CHILD_DOMAIN,
        "former-viewer.localhost",
    }
    assert wakes == {"relay-child.localhost"}
    assert (local_attachment.message_id, local_attachment.message_domain) == (None, None)
    assert media_locks.await_args_list == [
        call(session, 42, ORIGIN_DOMAIN),
        call(session, 41, LOCAL_DOMAIN),
    ]
    update_sql = [value for value in map(sql, session.statements) if value.startswith("UPDATE")]
    assert len(update_sql) == 1
    assert "UPDATE remote_media_cache" in update_sql[0]
    assert ORIGIN_DOMAIN in update_sql[0]
    assert "42" in update_sql[0]


@pytest.mark.asyncio
async def test_staged_attachment_only_history_is_dropped_after_terminal_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload: dict[str, Any] = {
        "content": None,
        "e2ee": None,
        "attachments": [
            {
                "id": "41",
                "origin_domain": ORIGIN_DOMAIN,
                "filename": "terminal.png",
            }
        ],
    }
    staged = SimpleNamespace(
        message_id=11,
        message_domain=ORIGIN_DOMAIN,
        channel_id=5,
        channel_domain=ORIGIN_DOMAIN,
        payload=payload,
    )
    guild = SimpleNamespace(
        id=9,
        origin_domain=ORIGIN_DOMAIN,
        last_event_seq=20,
        permission_generation=3,
        history_policy_generation=4,
    )
    history_import = SimpleNamespace(
        export_id="export-1",
        export_domain=ORIGIN_DOMAIN,
        requester_user_id=7,
        requester_user_domain=LOCAL_DOMAIN,
        requester_member_version=2,
        permission_generation=3,
        history_policy_generation=4,
    )
    member = SimpleNamespace(member_version=2)

    class Session:
        def __init__(self) -> None:
            self.scalar_results = [guild, None]
            self.statements: list[object] = []
            self.commits = 0

        async def scalars(self, statement: object) -> list[object]:
            self.statements.append(statement)
            return [staged]

        async def scalar(self, statement: object) -> object | None:
            self.statements.append(statement)
            return self.scalar_results.pop(0)

        async def get(self, model: object, key: object) -> object | None:
            if model is GuildMember:
                return member
            raise AssertionError(
                f"terminal attachment-only row must not be merged: {model!r} {key!r}"
            )

        async def execute(self, statement: object) -> object:
            self.statements.append(statement)
            statement_sql = sql(statement)
            if statement_sql.startswith("SELECT media_tombstone_sources"):
                return TupleResult([(41, ORIGIN_DOMAIN)])
            return TupleResult([])

        async def commit(self) -> None:
            self.commits += 1

    session = Session()
    monkeypatch.setattr(
        federation_history,
        "_lock_live_history_import",
        AsyncMock(return_value=(guild, history_import)),
    )
    monkeypatch.setattr(
        federation_history,
        "_revalidate_staged_history_message",
        AsyncMock(return_value=payload),
    )
    lock = AsyncMock()
    monkeypatch.setattr(federation_history, "lock_media_tombstone_ref", lock)
    monkeypatch.setattr(federation_history, "admit_replica_storage", AsyncMock())

    imported, complete = await federation_history._merge_history_import_batch(
        cast(Any, session),
        cast(Any, settings()),
        cast(Any, guild),
        cast(Any, history_import),
        reconciled_seq=20,
        tombstone_delivery_wakes=set(),
    )

    assert (imported, complete) == (0, True)
    assert payload["attachments"] == []
    lock.assert_awaited_once_with(session, 41, ORIGIN_DOMAIN)
    assert session.commits == 1
    statement_text = "\n".join(map(sql, session.statements))
    assert "media_tombstone_sources" in statement_text
    assert "DELETE FROM guild_history_staged_messages" in statement_text
    assert "INSERT INTO messages" not in statement_text


@pytest.mark.asyncio
async def test_terminal_object_purge_marker_survives_failure_and_clears_after_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=41,
        object_key="clean/41/original",
        staging_object_key="staging/41/original",
        variants={"thumbnail_128": {"object_key": "derived/41/thumbnail"}},
        scan_status="rejected",
        deleted_at=datetime.now(UTC),
        finalized_at=datetime.now(UTC),
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
    )

    class Session:
        def __init__(self) -> None:
            self.commits = 0

        async def scalars(self, _statement: object) -> list[object]:
            return [attachment]

        async def commit(self) -> None:
            self.commits += 1

    class FailingStorage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, _bucket: str, _key: str) -> None:
            raise StorageError("temporarily unavailable", retryable=True)

    session = Session()
    monkeypatch.setattr(media_jobs, "S3Storage", FailingStorage)
    first = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )

    assert first == 0
    assert attachment.staging_object_key == "staging/41/original"
    assert attachment.variants == {"thumbnail_128": {"object_key": "derived/41/thumbnail"}}

    deleted: set[tuple[str, str]] = set()

    class WorkingStorage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.add((bucket, key))

    monkeypatch.setattr(media_jobs, "S3Storage", WorkingStorage)
    second = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )

    assert second == 1
    assert attachment.staging_object_key is None
    assert attachment.variants == {}
    assert deleted == {
        ("kaede-attachments", "clean/41/original"),
        ("kaede-attachments", "staging/41/original"),
        ("kaede-derived", "derived/41/thumbnail"),
    }
    assert session.commits == 2


@pytest.mark.asyncio
async def test_deleted_clean_staging_retry_deletes_main_and_variants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=44,
        object_key="clean/44/original",
        staging_object_key="staging/44/original",
        variants={"thumbnail_128": {"object_key": "derived/44/thumbnail"}},
        scan_status="clean",
        deleted_at=datetime.now(UTC),
        finalized_at=datetime.now(UTC),
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
    )

    class Session:
        async def scalars(self, _statement: object) -> list[object]:
            return [attachment]

        async def commit(self) -> None:
            return None

    deleted: set[tuple[str, str]] = set()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.add((bucket, key))

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    removed = await media_jobs.sweep_staging_objects(
        cast(Any, Session()),
        cast(Any, settings()),
    )

    assert removed == 1
    assert deleted == {
        ("kaede-attachments", "clean/44/original"),
        ("kaede-attachments", "staging/44/original"),
        ("kaede-derived", "derived/44/thumbnail"),
    }
    assert attachment.staging_object_key is None
    assert attachment.variants == {}


@pytest.mark.asyncio
async def test_active_encrypted_staging_retry_preserves_ciphertext_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=45,
        object_key="clean/45/ciphertext",
        staging_object_key="staging/45/ciphertext",
        variants={},
        scan_status="encrypted",
        deleted_at=None,
        finalized_at=datetime.now(UTC),
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
    )

    class Session:
        async def scalars(self, _statement: object) -> list[object]:
            return [attachment]

        async def commit(self) -> None:
            return None

    deleted: list[tuple[str, str]] = []

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.append((bucket, key))

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    removed = await media_jobs.sweep_staging_objects(
        cast(Any, Session()),
        cast(Any, settings()),
    )

    assert removed == 1
    assert deleted == [("kaede-attachments", "staging/45/ciphertext")]
    assert attachment.staging_object_key is None
    assert attachment.object_key == "clean/45/ciphertext"


def test_staging_upload_completion_grace_is_strict_at_boundary() -> None:
    expires_at = datetime(2026, 8, 19, tzinfo=UTC)
    boundary = expires_at + timedelta(seconds=media_jobs.STAGING_UPLOAD_COMPLETION_GRACE_SECONDS)

    assert media_jobs.STAGING_UPLOAD_COMPLETION_GRACE_SECONDS == 960
    assert not media_jobs.staging_upload_grace_elapsed(
        expires_at,
        now=boundary - timedelta(microseconds=1),
    )
    assert not media_jobs.staging_upload_grace_elapsed(expires_at, now=boundary)
    assert media_jobs.staging_upload_grace_elapsed(
        expires_at,
        now=boundary + timedelta(microseconds=1),
    )


@pytest.mark.asyncio
async def test_staging_sweep_rotates_a_full_prefix_of_delete_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = datetime.now(UTC) - timedelta(days=1)
    attachments = [
        SimpleNamespace(
            id=item_id,
            staging_object_key=f"staging/{item_id}/original",
            upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
            scan_status="clean",
            deleted_at=None,
            finalized_at=datetime.now(UTC),
            updated_at=old,
        )
        for item_id in range(1, 102)
    ]
    selected_pages: list[list[int]] = []
    statements: list[object] = []

    class Session:
        def __init__(self) -> None:
            self.commits = 0

        async def scalars(self, statement: object) -> list[object]:
            statements.append(statement)
            selected = sorted(attachments, key=lambda item: (item.updated_at, item.id))[:100]
            selected_pages.append([item.id for item in selected])
            return selected

        async def commit(self) -> None:
            self.commits += 1

    deleted: list[int] = []

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, _bucket: str, key: str) -> None:
            item_id = int(key.split("/")[1])
            if item_id <= 100:
                raise StorageError("temporary failure", retryable=True)
            deleted.append(item_id)

    session = Session()
    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    first = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )
    second = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )

    assert first == 0
    assert second == 1
    assert selected_pages[0] == list(range(1, 101))
    assert selected_pages[1][0] == 101
    assert deleted == [101]
    assert attachments[100].staging_object_key is None
    assert session.commits == 2
    statement_sql = str(
        statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "attachments.upload_expires_at IS NULL OR" in statement_sql
    assert "attachments.scan_status IN ('clean', 'encrypted')" in statement_sql


@pytest.mark.asyncio
async def test_expired_pending_failure_commits_quota_then_retry_deletes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=50,
        origin_domain=LOCAL_DOMAIN,
        object_key=f"{LOCAL_DOMAIN}/50/staging/original",
        staging_object_key=None,
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
        scan_status="pending",
        deleted_at=None,
        finalized_at=None,
        updated_at=datetime.now(UTC) - timedelta(days=1),
        variants={},
    )

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.deleted: list[object] = []

        async def scalars(self, _statement: object) -> list[object]:
            return [attachment]

        async def delete(self, value: object) -> None:
            self.deleted.append(value)

        async def commit(self) -> None:
            self.commits += 1

    class Storage:
        calls = 0

        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, _bucket: str, _key: str) -> None:
            Storage.calls += 1
            if Storage.calls <= 2:
                raise StorageError("temporary failure", retryable=True)

    async def discard(_session: object, _settings: object, item: object) -> None:
        assert item is attachment
        attachment.deleted_at = datetime.now(UTC)

    session = Session()
    quota_discard = AsyncMock(side_effect=discard)
    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    monkeypatch.setattr(media_jobs, "discard_attachment", quota_discard)

    expired = await media_jobs.sweep_orphan_uploads(
        cast(Any, session),
        cast(Any, settings()),
    )
    first_retry = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )
    second_retry = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )

    assert expired == 1
    assert first_retry == 0
    assert second_retry == 1
    quota_discard.assert_awaited_once()
    assert attachment.deleted_at is not None
    assert attachment.staging_object_key == attachment.object_key
    assert session.deleted == [attachment]
    assert session.commits == 3


@pytest.mark.asyncio
async def test_live_upload_capability_keeps_deterministic_purge_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=41,
        origin_domain=LOCAL_DOMAIN,
        object_key="clean/41/original",
        staging_object_key=None,
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=5),
        deleted_at=datetime.now(UTC),
        finalized_at=datetime.now(UTC),
        scan_status="rejected",
        variants={},
        source_attachment_id=None,
        source_attachment_domain=None,
    )

    class Session:
        def __init__(self) -> None:
            self.commits = 0
            self.scalar_calls = 0

        async def scalar(self, _statement: object) -> object:
            self.scalar_calls += 1
            return attachment if self.scalar_calls == 1 else False

        async def scalars(self, _statement: object) -> list[object]:
            return [attachment]

        async def commit(self) -> None:
            self.commits += 1

    deleted: list[tuple[str, str]] = []

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.append((bucket, key))

    session = Session()
    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    result = await media_jobs.purge_local_attachment(
        cast(Any, session),
        cast(Any, settings()),
        attachment.id,
        attachment.origin_domain,
    )

    assert result == "deleted"
    assert attachment.staging_object_key == f"{LOCAL_DOMAIN}/41/staging/original"
    assert session.commits == 1
    assert deleted == [
        ("kaede-attachments", "clean/41/original"),
        ("kaede-attachments", f"{LOCAL_DOMAIN}/41/staging/original"),
    ]

    # A still-live PUT capability may recreate the staging object after the
    # immediate purge. The retained marker makes the post-expiry-grace sweep
    # delete that exact key again before clearing it.
    deleted.clear()
    attachment.upload_expires_at = datetime.now(UTC) - timedelta(minutes=17)
    swept = await media_jobs.sweep_staging_objects(
        cast(Any, session),
        cast(Any, settings()),
    )

    assert swept == 1
    assert attachment.staging_object_key is None
    assert ("kaede-attachments", f"{LOCAL_DOMAIN}/41/staging/original") in deleted
    assert session.commits == 2


@pytest.mark.asyncio
async def test_expired_purge_deletes_preexisting_staging_marker_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=42,
        origin_domain=LOCAL_DOMAIN,
        object_key="clean/42/original",
        staging_object_key="staging/42/original",
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
        deleted_at=datetime.now(UTC),
        variants={
            "thumbnail_128": {"object_key": "derived/42/thumbnail"},
            "duplicate": {"object_key": "derived/42/thumbnail"},
        },
        source_attachment_id=None,
        source_attachment_domain=None,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[attachment, False]),
        commit=AsyncMock(),
    )
    deleted: list[tuple[str, str]] = []

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.append((bucket, key))

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    result = await media_jobs.purge_local_attachment(
        cast(Any, session),
        cast(Any, settings()),
        attachment.id,
        attachment.origin_domain,
    )

    assert result == "deleted"
    assert attachment.staging_object_key is None
    assert deleted == [
        ("kaede-attachments", "clean/42/original"),
        ("kaede-attachments", "staging/42/original"),
        ("kaede-derived", "derived/42/thumbnail"),
    ]
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_clean_duplicate_purge_deletes_objects_before_one_quota_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=43,
        origin_domain=LOCAL_DOMAIN,
        object_key="clean/43/original",
        staging_object_key="staging/43/original",
        upload_expires_at=datetime.now(UTC) - timedelta(minutes=17),
        deleted_at=None,
        variants={"thumbnail_128": {"object_key": "derived/43/thumbnail"}},
        source_attachment_id=None,
        source_attachment_domain=None,
    )
    events: list[tuple[str, str] | str] = []

    class Session:
        def __init__(self) -> None:
            self.scalar_calls = 0

        async def scalar(self, _statement: object) -> object:
            self.scalar_calls += 1
            return attachment if self.scalar_calls == 1 else False

        async def commit(self) -> None:
            events.append("commit")

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            events.append((bucket, key))

    async def discard(_session: object, _settings: object, item: object) -> None:
        assert item is attachment
        events.append("discard")
        attachment.deleted_at = datetime.now(UTC)

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    quota_discard = AsyncMock(side_effect=discard)
    monkeypatch.setattr(media_jobs, "discard_attachment", quota_discard)

    result = await media_jobs.purge_local_attachment(
        cast(Any, Session()),
        cast(Any, settings()),
        attachment.id,
        attachment.origin_domain,
    )

    assert result == "deleted"
    assert events == [
        ("kaede-attachments", "clean/43/original"),
        ("kaede-attachments", "staging/43/original"),
        ("kaede-derived", "derived/43/thumbnail"),
        "discard",
        "commit",
    ]
    quota_discard.assert_awaited_once()
    assert attachment.deleted_at is not None
    assert attachment.staging_object_key is None


@pytest.mark.asyncio
async def test_purge_retains_shared_announcement_objects_until_last_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment = SimpleNamespace(
        id=44,
        origin_domain=LOCAL_DOMAIN,
        object_key="clean/44/original",
        staging_object_key=None,
        upload_expires_at=None,
        deleted_at=datetime.now(UTC),
        variants={"thumbnail_128": {"object_key": "derived/44/thumbnail"}},
        source_attachment_id=12,
        source_attachment_domain=LOCAL_DOMAIN,
    )
    session = SimpleNamespace(
        scalar=AsyncMock(side_effect=[attachment, True]),
        commit=AsyncMock(),
    )
    deleted: list[tuple[str, str]] = []

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def delete(self, bucket: str, key: str) -> None:
            deleted.append((bucket, key))

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    result = await media_jobs.purge_local_attachment(
        cast(Any, session),
        cast(Any, settings()),
        attachment.id,
        attachment.origin_domain,
    )

    assert result == "deleted"
    assert deleted == []
    session.commit.assert_awaited_once()
