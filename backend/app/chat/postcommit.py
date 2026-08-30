from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.chat.events import publish_dispatch

_LISTENERS_INSTALLED = "kaede_postcommit_listeners_installed"
_PENDING_DISPATCHES = "kaede_pending_dispatches"
_COMMITTED_DISPATCHES = "kaede_committed_dispatches"
_PENDING_FEDERATION_WAKES = "kaede_pending_federation_wakes"
_COMMITTED_FEDERATION_WAKES = "kaede_committed_federation_wakes"


@dataclass(frozen=True, slots=True)
class PostCommitDispatch:
    topic: str
    event_type: str
    data: dict[str, Any]
    audience_user_refs: tuple[str, ...] | None = None


def _after_commit(session: Session) -> None:
    pending = session.info.pop(_PENDING_DISPATCHES, [])
    if pending:
        session.info.setdefault(_COMMITTED_DISPATCHES, []).extend(pending)
    federation_wakes = session.info.pop(_PENDING_FEDERATION_WAKES, set())
    if federation_wakes:
        session.info.setdefault(_COMMITTED_FEDERATION_WAKES, set()).update(federation_wakes)


def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING_DISPATCHES, None)
    session.info.pop(_PENDING_FEDERATION_WAKES, None)


def _sync_session(session: AsyncSession) -> Session | None:
    sync_session = getattr(session, "sync_session", None)
    return sync_session if isinstance(sync_session, Session) else None


def _install_listeners(session: AsyncSession) -> Session | None:
    sync_session = _sync_session(session)
    if sync_session is None:
        # Lightweight service-unit fixtures may supply a protocol-compatible
        # mock instead of SQLAlchemy's AsyncSession. Production dependencies
        # always provide the real transaction-bearing session.
        return None
    if sync_session.info.get(_LISTENERS_INSTALLED):
        return sync_session
    event.listen(sync_session, "after_commit", _after_commit)
    event.listen(sync_session, "after_rollback", _after_rollback)
    sync_session.info[_LISTENERS_INSTALLED] = True
    return sync_session


def queue_postcommit_dispatch(
    session: AsyncSession,
    topic: str,
    event_type: str,
    data: dict[str, Any],
    *,
    audience_user_refs: Sequence[str] | None = None,
) -> None:
    """Queue a gateway projection in the caller's current SQL transaction.

    The queue moves to the publishable set only from SQLAlchemy's
    ``after_commit`` hook. A rollback therefore cannot leak an event for state
    that never became authoritative.
    """

    sync_session = _install_listeners(session)
    if sync_session is None:
        return
    audience = tuple(dict.fromkeys(audience_user_refs)) if audience_user_refs else None
    sync_session.info.setdefault(_PENDING_DISPATCHES, []).append(
        PostCommitDispatch(
            topic=topic,
            event_type=event_type,
            data=data,
            audience_user_refs=audience,
        )
    )


def queue_postcommit_federation_wakes(
    session: AsyncSession,
    destinations: Sequence[str],
) -> None:
    """Wake durable federation outboxes only after their SQL commit."""

    sync_session = _install_listeners(session)
    if sync_session is None:
        return
    sync_session.info.setdefault(_PENDING_FEDERATION_WAKES, set()).update(destinations)


async def publish_committed_dispatches(session: AsyncSession, redis: Redis) -> int:
    """Publish every committed request dispatch as a best-effort projection."""

    sync_session = _sync_session(session)
    if sync_session is None:
        return 0
    queued = list(sync_session.info.pop(_COMMITTED_DISPATCHES, []))
    for item in queued:
        await publish_dispatch(
            redis,
            item.topic,
            item.event_type,
            item.data,
            audience_user_refs=item.audience_user_refs,
        )
    destinations = set(sync_session.info.pop(_COMMITTED_FEDERATION_WAKES, set()))
    if destinations:
        from app.core.task_wake import wake_federation_destinations

        await wake_federation_destinations(destinations)
    return len(queued) + len(destinations)
