from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.allowed_mentions import EVERYONE_MENTION
from app.chat.channel_access import effective_channel_nsfw
from app.chat.mentions import USER_MENTION, role_mention_refs
from app.chat.rich_content import message_automod_text
from app.core.settings import Settings
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Message,
    Pin,
    Poll,
    PollAnswer,
    SearchIndexOutbox,
    SearchIndexState,
    User,
)

LINK_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
SEARCHABLE_ATTRIBUTES = ["content"]
FILTERABLE_ATTRIBUTES = [
    "channel_ref",
    "guild_ref",
    "dm_participant_refs",
    "dm_authority",
    "author_ref",
    "mention_refs",
    "mention_role_refs",
    "mention_everyone",
    "replied_to_user_ref",
    "replied_to_message_ref",
    "content_types",
    "embed_types",
    "embed_providers",
    "link_hostnames",
    "attachment_filenames",
    "attachment_extensions",
    "pinned",
    "author_type",
    "nsfw",
    "message_id",
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


def message_author_type(message: Message, author: User | None) -> str:
    """Classify webhook delivery before bot identity, matching Discord filters."""

    if message.webhook_id is not None:
        return "webhook"
    if author is not None and author.account_type == "bot":
        return "bot"
    return "user"


def search_link_hostnames(*values: str) -> list[str]:
    hostnames: set[str] = set()
    for value in values:
        for match in LINK_RE.finditer(value):
            candidate = match.group(0).rstrip('.,;:!?)]}"')
            hostname = urlsplit(candidate).hostname
            if hostname:
                hostnames.add(hostname.rstrip(".").lower())
    return sorted(hostnames)


def embed_search_projection(
    embeds: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    types: set[str] = set()
    providers: set[str] = set()
    urls: list[str] = []
    type_aliases = {
        "image": "image",
        "video": "video",
        "gif": "gif",
        "gifv": "gif",
        "sound": "sound",
        "audio": "sound",
        "article": "article",
        "link": "article",
    }
    for embed in embeds:
        raw_type = embed.get("type")
        if isinstance(raw_type, str) and raw_type.lower() in type_aliases:
            types.add(type_aliases[raw_type.lower()])
        if isinstance(embed.get("image"), dict) or isinstance(embed.get("thumbnail"), dict):
            types.add("image")
        if isinstance(embed.get("video"), dict):
            types.add("video")
        if isinstance(embed.get("audio"), dict):
            types.add("sound")
        provider = embed.get("provider")
        provider_name = provider.get("name") if isinstance(provider, dict) else provider
        if isinstance(provider_name, str) and provider_name:
            providers.add(provider_name)
        for candidate in (
            embed.get("url"),
            *(
                nested.get("url")
                for key in ("image", "thumbnail", "video", "audio", "author")
                if isinstance((nested := embed.get(key)), dict)
            ),
        ):
            if isinstance(candidate, str):
                urls.append(candidate)
    return sorted(types), sorted(providers), search_link_hostnames(*urls)


def attachment_search_projection(
    attachments: list[Attachment],
) -> tuple[set[str], list[str], list[str]]:
    content_types: set[str] = set()
    filenames: set[str] = set()
    extensions: set[str] = set()
    for attachment in attachments:
        content_types.add("file")
        media_type = attachment.detected_content_type or attachment.content_type
        if media_type.startswith("image/"):
            content_types.add("image")
        elif media_type.startswith("video/"):
            content_types.add("video")
        elif media_type.startswith("audio/"):
            content_types.update(("sound", "audio"))
        filename = attachment.filename.casefold()
        filenames.add(filename)
        stem, separator, extension = filename.rpartition(".")
        if separator and stem and extension:
            extensions.add(extension)
    return content_types, sorted(filenames), sorted(extensions)


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

    async def configured_filterable_attributes(self) -> frozenset[str] | None:
        """Read the live index schema; ``None`` means the index does not exist."""

        try:
            async with httpx.AsyncClient(
                base_url=self.settings.search_url.rstrip("/"),
                headers=self.headers,
                timeout=self.settings.search_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(f"/indexes/{self.uid}/settings")
                if response.status_code == 404:
                    return None
                response.raise_for_status()
                value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchUnavailable("message search service is unavailable") from exc
        raw = value.get("filterableAttributes") if isinstance(value, dict) else None
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise SearchUnavailable("message search returned an invalid index schema")
        return frozenset(raw)

    async def reset_index(self) -> None:
        try:
            async with httpx.AsyncClient(
                base_url=self.settings.search_url.rstrip("/"),
                headers=self.headers,
                timeout=self.settings.search_request_timeout_seconds,
                follow_redirects=False,
            ) as client:
                current = await client.get(f"/indexes/{self.uid}")
                if current.status_code == 404:
                    _configured_indices.discard((self.settings.search_url.rstrip("/"), self.uid))
                    return
                current.raise_for_status()
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
    effective_nsfw = await effective_channel_nsfw(session, channel)
    if effective_nsfw is None:
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
    types, attachment_filenames, attachment_extensions = attachment_search_projection(attachments)
    poll = await session.get(Poll, (message.id, message.origin_domain))
    poll_answers = (
        list(
            await session.scalars(
                select(PollAnswer)
                .where(
                    PollAnswer.message_id == message.id,
                    PollAnswer.message_domain == message.origin_domain,
                )
                .order_by(PollAnswer.answer_id)
            )
        )
        if poll is not None
        else []
    )
    poll_projection: dict[str, object] | None = None
    if poll is not None:
        poll_projection = {
            "question": poll.question,
            "answers": [
                {"poll_media": {"text": answer.text}}
                for answer in poll_answers
                if answer.text is not None
            ],
        }
        types.add("poll")
    content = (
        message_automod_text(
            message.content,
            poll=poll_projection,
            components=message.components or [],
        )
        or ""
    )
    if LINK_RE.search(content):
        types.update(("link", "embed"))
    embeds = [item for item in message.embeds if isinstance(item, dict)]
    embed_types, embed_providers, embed_hostnames = embed_search_projection(embeds)
    if embeds:
        types.add("embed")
    if message.sticker_items:
        types.add("sticker")
    if message.forward_snapshot is not None or message.flags & (1 << 14):
        types.add("snapshot")
    link_hostnames = sorted(set(search_link_hostnames(content)) | set(embed_hostnames))
    participant_refs: list[str] = []
    dm_authority: str | None = None
    if channel.type == 1:
        conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
        if conversation is None:
            return None
        dm_authority = conversation.authority_domain
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
    # Search the visible mention tokens, not the expanded notification route.
    # A role or @everyone may notify thousands of users, but Discord's
    # `mentions` filter must not report all of those users as direct mentions.
    mentions = sorted(
        {
            canonical_ref(
                int(match.group("id")),
                (match.group("domain") or channel.origin_domain).lower(),
            )
            for match in USER_MENTION.finditer(content)
        }
    )
    role_mentions = sorted(
        canonical_ref(role_id, role_domain) for role_id, role_domain in role_mention_refs(content)
    )
    replied_to_message_ref = (
        canonical_ref(message.referenced_message_id, message.referenced_message_domain)
        if message.referenced_message_id is not None
        and message.referenced_message_domain is not None
        else None
    )
    replied_to_user_ref = None
    if message.referenced_message_id is not None and message.referenced_message_domain is not None:
        referenced = await session.get(
            Message,
            (message.referenced_message_id, message.referenced_message_domain),
        )
        if referenced is not None:
            replied_to_user_ref = canonical_ref(
                referenced.author_id,
                referenced.author_domain,
            )
    author = await session.get(User, (message.author_id, message.author_domain))
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
        "dm_authority": dm_authority,
        "author_ref": canonical_ref(message.author_id, message.author_domain),
        "mention_refs": mentions,
        "mention_role_refs": role_mentions,
        "mention_everyone": EVERYONE_MENTION.search(content) is not None,
        "replied_to_user_ref": replied_to_user_ref,
        "replied_to_message_ref": replied_to_message_ref,
        "content": content,
        "content_types": sorted(types),
        "embed_types": embed_types,
        "embed_providers": embed_providers,
        "link_hostnames": link_hostnames,
        "attachment_filenames": attachment_filenames,
        "attachment_extensions": attachment_extensions,
        "pinned": pinned,
        "author_type": message_author_type(message, author),
        "nsfw": effective_nsfw,
        # Keep the numeric snowflake alongside its authority-qualified ref so
        # max_id/min_id filters remain exact for messages created in the same
        # millisecond.  Deriving only created_at_ms loses worker/sequence bits.
        "message_id": message.id,
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
    if settings.search_enabled and not state.reset_required:
        client = MeiliClient(settings)
        cache_key = (settings.search_url.rstrip("/"), client.uid)
        if cache_key not in _configured_indices:
            schema_available = True
            try:
                current_attributes = await client.configured_filterable_attributes()
            except SearchUnavailable:
                schema_available = False
                current_attributes = None
            if schema_available and current_attributes != frozenset(FILTERABLE_ATTRIBUTES):
                state.reset_required = True
                state.backfill_after_id = None
                state.backfill_after_domain = None
                state.backfill_completed = False
                state.updated_at = datetime.now(UTC)
            elif schema_available:
                _configured_indices.add(cache_key)
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
