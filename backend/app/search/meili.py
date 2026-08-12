from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import (
    Attachment,
    Channel,
    DMParticipant,
    Message,
    Pin,
    SearchIndexOutbox,
    SearchIndexState,
)

LINK_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
SEARCHABLE_ATTRIBUTES = ["content"]
FILTERABLE_ATTRIBUTES = [
    "channel_ref",
    "guild_ref",
    "dm_participant_refs",
    "author_ref",
    "mention_refs",
    "content_types",
    "pinned",
    "author_type",
    "created_at_ms",
]
SORTABLE_ATTRIBUTES = ["created_at_ms"]
TYPO_TOLERANCE = {
    "enabled": True,
    "minWordSizeForTypos": {"oneTypo": 4, "twoTypos": 8},
}
_configured_indices: set[tuple[str, str]] = set()


class SearchUnavailable(RuntimeError):
    pass


def canonical_ref(identifier: int, domain: str) -> str:
    return f"{identifier}@{domain}"


def document_id(identifier: int, domain: str) -> str:
    return hashlib.sha256(canonical_ref(identifier, domain).encode()).hexdigest()


def search_index_uid(settings: Settings) -> str:
    safe_domain = re.sub(r"[^a-zA-Z0-9_-]", "_", settings.domain)
    return f"{settings.search_index_prefix}_{safe_domain}_messages"


class MeiliClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.uid = search_index_uid(settings)
        secret = settings.search_master_key
        self.headers = (
            {"Authorization": f"Bearer {secret.get_secret_value()}"} if secret is not None else {}
        )

    async def request(
        self, method: str, path: str, *, payload: object | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.search_url.rstrip("/"),
                headers=self.headers,
                timeout=self.settings.search_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.request(method, path, json=payload)
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable("message search service is unavailable") from exc
        if not isinstance(value, dict):
            raise SearchUnavailable("message search service returned an invalid response")
        return value

    async def wait_task(self, task_uid: int) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.search_request_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            task = await self.request("GET", f"/tasks/{task_uid}")
            status = task.get("status")
            if status == "succeeded":
                return
            if status in {"failed", "canceled"}:
                raise SearchUnavailable("message search indexing task failed")
            await asyncio.sleep(0.05)
        raise SearchUnavailable("message search indexing task timed out")

    async def ensure_index(self) -> None:
        cache_key = (self.settings.search_url.rstrip("/"), self.uid)
        try:
            await self.request("GET", f"/indexes/{self.uid}")
        except SearchUnavailable:
            task = await self.request(
                "POST", "/indexes", payload={"uid": self.uid, "primaryKey": "document_id"}
            )
            await self.wait_task(int(task["taskUid"]))
        if cache_key in _configured_indices:
            return
        task = await self.request(
            "PATCH",
            f"/indexes/{self.uid}/settings",
            payload={
                "searchableAttributes": SEARCHABLE_ATTRIBUTES,
                "filterableAttributes": FILTERABLE_ATTRIBUTES,
                "sortableAttributes": SORTABLE_ATTRIBUTES,
                "displayedAttributes": ["document_id", "message_ref"],
                "pagination": {"maxTotalHits": 10000},
                "typoTolerance": TYPO_TOLERANCE,
            },
        )
        await self.wait_task(int(task["taskUid"]))
        _configured_indices.add(cache_key)

    async def upsert(self, documents: list[dict[str, object]]) -> None:
        if not documents:
            return
        task = await self.request("POST", f"/indexes/{self.uid}/documents", payload=documents)
        await self.wait_task(int(task["taskUid"]))

    async def remove(self, ids: list[str]) -> None:
        if not ids:
            return
        task = await self.request(
            "POST", f"/indexes/{self.uid}/documents/delete-batch", payload=ids
        )
        await self.wait_task(int(task["taskUid"]))

    async def search(self, payload: dict[str, object]) -> dict[str, Any]:
        return await self.request("POST", f"/indexes/{self.uid}/search", payload=payload)

    async def reset_index(self) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.search_url.rstrip("/"),
                headers=self.headers,
                timeout=self.settings.search_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.delete(f"/indexes/{self.uid}")
                if response.status_code != 404:
                    response.raise_for_status()
                    value = response.json()
                    if not isinstance(value, dict) or not isinstance(value.get("taskUid"), int):
                        raise SearchUnavailable("message search returned an invalid task")
                    await self.wait_task(value["taskUid"])
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable("message search service is unavailable") from exc
        _configured_indices.discard((self.settings.search_url.rstrip("/"), self.uid))


async def build_document(session: AsyncSession, message: Message) -> dict[str, object] | None:
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if (
        channel is None
        or channel.unavailable
        or channel.encryption_mode != "plaintext"
        or message.e2ee is not None
        or message.deleted_at is not None
    ):
        return None
    attachments = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    types: set[str] = set()
    for attachment in attachments:
        content_type = attachment.detected_content_type or attachment.content_type
        if content_type.startswith("image/"):
            types.add("image")
        elif content_type.startswith("video/"):
            types.add("video")
        elif content_type.startswith("audio/"):
            types.add("audio")
        else:
            types.add("file")
    content = message.content or ""
    if LINK_RE.search(content):
        types.update(("link", "embed"))
    participant_refs: list[str] = []
    if channel.type == 1:
        participant_refs = [
            canonical_ref(user_id, user_domain)
            for user_id, user_domain in (
                await session.execute(
                    select(DMParticipant.user_id, DMParticipant.user_domain).where(
                        DMParticipant.conversation_id == channel.id,
                        DMParticipant.conversation_domain == channel.origin_domain,
                    )
                )
            ).all()
        ]
    pinned = bool(
        await session.scalar(
            select(
                exists().where(
                    Pin.message_id == message.id,
                    Pin.message_domain == message.origin_domain,
                )
            )
        )
    )
    mentions = [
        canonical_ref(int(item["id"]), str(item["origin_domain"]))
        for item in message.mention_user_refs
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and str(item["id"]).isdigit()
        and isinstance(item.get("origin_domain"), str)
    ]
    return {
        "document_id": document_id(message.id, message.origin_domain),
        "message_ref": canonical_ref(message.id, message.origin_domain),
        "channel_ref": canonical_ref(channel.id, channel.origin_domain),
        "guild_ref": (
            canonical_ref(channel.guild_id, channel.guild_domain)
            if channel.guild_id is not None and channel.guild_domain is not None
            else None
        ),
        "dm_participant_refs": participant_refs,
        "author_ref": canonical_ref(message.author_id, message.author_domain),
        "mention_refs": mentions,
        "content": content,
        "content_types": sorted(types),
        "pinned": pinned,
        "author_type": "webhook" if message.webhook_id is not None else "user",
        "created_at_ms": int(message.created_at.timestamp() * 1000),
    }


async def seed_search_backfill(session: AsyncSession, settings: Settings) -> int:
    """Advance the singleton historical backfill cursor by one bounded batch."""

    if not settings.search_enabled:
        return 0
    state = await session.get(SearchIndexState, 1, with_for_update=True)
    if state is None or state.backfill_completed:
        return 0
    query = select(Message.id, Message.origin_domain)
    if state.backfill_after_id is not None and state.backfill_after_domain is not None:
        query = query.where(
            (Message.id > state.backfill_after_id)
            | (
                (Message.id == state.backfill_after_id)
                & (Message.origin_domain > state.backfill_after_domain)
            )
        )
    rows = (
        await session.execute(
            query.order_by(Message.id, Message.origin_domain).limit(settings.search_batch_size)
        )
    ).all()
    if not rows:
        state.backfill_completed = True
        state.updated_at = datetime.now(UTC)
        await session.commit()
        return 0
    now = datetime.now(UTC)
    for message_id, message_domain in rows:
        row = await session.get(SearchIndexOutbox, (message_id, message_domain))
        if row is None:
            session.add(
                SearchIndexOutbox(
                    message_id=message_id,
                    message_domain=message_domain,
                    next_attempt_at=now,
                    updated_at=now,
                )
            )
    state.backfill_after_id = rows[-1][0]
    state.backfill_after_domain = rows[-1][1]
    state.updated_at = now
    await session.commit()
    return len(rows)


async def reconcile_search_index_state(session: AsyncSession, settings: Settings) -> bool:
    """Synchronize the database trigger gate with operator configuration.

    Disabling search drops desired-state rows instead of retaining an
    ever-growing queue. Re-enabling starts a complete resumable backfill, so
    messages changed while search was off are still indexed.
    """

    state = await session.get(SearchIndexState, 1, with_for_update=True)
    if state is None:
        state = SearchIndexState(id=1)
        session.add(state)
    if state.enabled != settings.search_enabled:
        state.enabled = settings.search_enabled
        if settings.search_enabled:
            # Re-enabling must not reuse plaintext documents left from before
            # E2EE policy changes made while indexing was disabled.
            state.reset_required = True
        state.backfill_after_id = None
        state.backfill_after_domain = None
        state.backfill_completed = False
        state.updated_at = datetime.now(UTC)
    if not settings.search_enabled:
        await session.execute(delete(SearchIndexOutbox))
    await session.commit()
    return settings.search_enabled


async def purge_index_for_encryption_transition(session: AsyncSession, settings: Settings) -> bool:
    """Erase the derived index before rebuilding after plaintext becomes E2EE."""

    state = await session.get(SearchIndexState, 1, with_for_update=True)
    if state is None or not state.reset_required:
        return False
    await MeiliClient(settings).reset_index()
    state.reset_required = False
    state.backfill_after_id = None
    state.backfill_after_domain = None
    state.backfill_completed = False
    state.updated_at = datetime.now(UTC)
    await session.commit()
    return True


async def process_search_outbox(session: AsyncSession, settings: Settings) -> int:
    if not settings.search_enabled:
        return 0
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(SearchIndexOutbox)
            .where(
                SearchIndexOutbox.next_attempt_at <= now,
                (SearchIndexOutbox.locked_at.is_(None))
                | (SearchIndexOutbox.locked_at < now - timedelta(minutes=5)),
            )
            .order_by(SearchIndexOutbox.updated_at)
            .limit(settings.search_batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return 0
    claims = {(row.message_id, row.message_domain): row.updated_at for row in rows}
    for row in rows:
        row.locked_at = now
    await session.commit()
    upserts: list[dict[str, object]] = []
    removals: list[str] = []
    for message_id, message_domain in claims:
        message = await session.get(Message, (message_id, message_domain))
        document = await build_document(session, message) if message is not None else None
        if document is None:
            removals.append(document_id(message_id, message_domain))
        else:
            upserts.append(document)
    client = MeiliClient(settings)
    try:
        await client.ensure_index()
        await client.upsert(upserts)
        await client.remove(removals)
    except SearchUnavailable:
        _configured_indices.discard((settings.search_url.rstrip("/"), client.uid))
        for (message_id, message_domain), claimed_updated_at in claims.items():
            await session.execute(
                update(SearchIndexOutbox)
                .where(
                    SearchIndexOutbox.message_id == message_id,
                    SearchIndexOutbox.message_domain == message_domain,
                    SearchIndexOutbox.updated_at == claimed_updated_at,
                )
                .values(
                    attempts=SearchIndexOutbox.attempts + 1,
                    locked_at=None,
                    last_error_code="SEARCH_BACKEND_UNAVAILABLE",
                    next_attempt_at=now + timedelta(seconds=30),
                )
            )
        await session.commit()
        raise
    for (message_id, message_domain), claimed_updated_at in claims.items():
        await session.execute(
            delete(SearchIndexOutbox).where(
                SearchIndexOutbox.message_id == message_id,
                SearchIndexOutbox.message_domain == message_domain,
                SearchIndexOutbox.updated_at == claimed_updated_at,
            )
        )
    await session.commit()
    return len(claims)


def filter_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
