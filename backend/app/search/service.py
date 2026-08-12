from __future__ import annotations

import base64
from datetime import UTC
from typing import cast

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.channel_access import ChannelAccess, load_channel_access
from app.chat.payloads import (
    channel_payload,
    guild_payload,
    render_message_payload,
    user_payload,
)
from app.chat.permissions import require_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import EPOCH_MS, SEQUENCE_BITS, WORKER_BITS
from app.core.types import EntityRef, validate_entity_reference
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    Guild,
    GuildMember,
    Instance,
    Message,
    SearchIndexOutbox,
    SearchIndexState,
    User,
)
from app.federation.client import signed_request
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    ensure_peer,
)
from app.search.meili import MeiliClient, SearchUnavailable, filter_value
from app.search.schemas import (
    FederatedMessageSearchResponse,
    FederatedMessageSearchResult,
    MessageSearchRequest,
)

SEARCH_PERMISSIONS = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
MAX_CANDIDATES = 150


def cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("ascii")
        value = int(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"}) from exc
    if not 0 <= value <= 10_000:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    return value


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def meili_filters(request: MessageSearchRequest, actor: User, settings: Settings) -> list[str]:
    values: list[str] = []
    if request.scope == "channel":
        if request.scope_ref is None:
            raise ValueError("channel search requires a scope reference")
        channel_id, channel_domain = request.scope_ref.resolve(settings.domain)
        values.append(f"channel_ref = {filter_value(f'{channel_id}@{channel_domain}')}")
    elif request.scope == "guild":
        if request.scope_ref is None:
            raise ValueError("guild search requires a scope reference")
        guild_id, guild_domain = request.scope_ref.resolve(settings.domain)
        values.append(f"guild_ref = {filter_value(f'{guild_id}@{guild_domain}')}")
    else:
        values.append(f"dm_participant_refs = {filter_value(f'{actor.id}@{actor.origin_domain}')}")
    if request.filters.authors:
        authors = []
        for ref in request.filters.authors:
            identifier, domain = ref.resolve(settings.domain)
            authors.append(f"author_ref = {filter_value(f'{identifier}@{domain}')}")
        values.append(f"({' OR '.join(authors)})")
    if request.filters.mentions:
        mentions = []
        for ref in request.filters.mentions:
            identifier, domain = ref.resolve(settings.domain)
            mentions.append(f"mention_refs = {filter_value(f'{identifier}@{domain}')}")
        values.append(f"({' OR '.join(mentions)})")
    if request.filters.has:
        content_filters = " OR ".join(
            f"content_types = {filter_value(value)}" for value in request.filters.has
        )
        values.append(f"({content_filters})")
    if request.filters.pinned is not None:
        values.append(f"pinned = {'true' if request.filters.pinned else 'false'}")
    if request.filters.author_type is not None:
        values.append(f"author_type = {filter_value(request.filters.author_type)}")
    if request.filters.before is not None:
        values.append(
            f"created_at_ms < {int(request.filters.before.astimezone(UTC).timestamp() * 1000)}"
        )
    if request.filters.after is not None:
        values.append(
            f"created_at_ms > {int(request.filters.after.astimezone(UTC).timestamp() * 1000)}"
        )
    return values


async def authorize_scope(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
) -> tuple[str | None, list[str]]:
    disabled: list[str] = []
    authority: str | None = None
    if request.scope == "channel":
        if request.scope_ref is None:
            raise ValueError("channel search requires a scope reference")
        access = await load_channel_access(session, settings, actor, request.scope_ref.reference)
        if access.guild is not None:
            await require_permissions(
                session, redis, access.guild, actor, SEARCH_PERMISSIONS, channel=access.channel
            )
            authority = access.guild.origin_domain
        else:
            conversation = await session.get(
                DMConversation, (access.channel.id, access.channel.origin_domain)
            )
            authority = conversation.authority_domain if conversation is not None else None
        if access.channel.encryption_mode == "e2ee":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "SEARCH_DISABLED_FOR_E2EE",
                    "message": (
                        "Message search is unavailable in end-to-end encrypted conversations."
                    ),
                },
            )
    elif request.scope == "guild":
        if request.scope_ref is None:
            raise ValueError("guild search requires a scope reference")
        guild_id, guild_domain = request.scope_ref.resolve(settings.domain)
        guild = await session.get(Guild, (guild_id, guild_domain))
        member = await session.get(
            GuildMember, (guild_id, guild_domain, actor.id, actor.origin_domain)
        )
        if guild is None or member is None or guild.unavailable:
            raise HTTPException(status_code=404, detail={"code": "GUILD_NOT_FOUND"})
        authority = guild.origin_domain
        disabled = [
            f"{channel_id}@{channel_domain}"
            for channel_id, channel_domain in (
                await session.execute(
                    select(Channel.id, Channel.origin_domain).where(
                        Channel.guild_id == guild_id,
                        Channel.guild_domain == guild_domain,
                        Channel.encryption_mode == "e2ee",
                    )
                )
            ).all()
        ]
    else:
        authority = None
        disabled = [
            f"{channel_id}@{channel_domain}"
            for channel_id, channel_domain in (
                await session.execute(
                    select(Channel.id, Channel.origin_domain)
                    .join(
                        DMParticipant,
                        (DMParticipant.conversation_id == Channel.id)
                        & (DMParticipant.conversation_domain == Channel.origin_domain),
                    )
                    .where(
                        DMParticipant.user_id == actor.id,
                        DMParticipant.user_domain == actor.origin_domain,
                        Channel.encryption_mode == "e2ee",
                    )
                    .limit(10_000)
                )
            ).all()
        ]
    return authority, disabled


async def candidate_refs(
    settings: Settings, request: MessageSearchRequest, actor: User
) -> tuple[list[str], int, bool]:
    if not settings.search_enabled or settings.search_master_key is None:
        raise SearchUnavailable("message search is not enabled on this instance")
    offset = cursor_offset(request.cursor)
    payload: dict[str, object] = {
        "q": request.query,
        "filter": meili_filters(request, actor, settings),
        "offset": offset,
        "limit": min(MAX_CANDIDATES, request.limit * 3),
        "attributesToRetrieve": ["message_ref"],
        "showRankingScore": True,
    }
    if request.sort != "relevance":
        payload["sort"] = [f"created_at_ms:{'desc' if request.sort == 'newest' else 'asc'}"]
    result = await MeiliClient(settings).search(payload)
    hits = result.get("hits")
    if not isinstance(hits, list):
        raise SearchUnavailable("message search returned an invalid result")
    raw_refs = [item.get("message_ref") for item in hits if isinstance(item, dict)]
    refs: list[str] = [item for item in raw_refs if isinstance(item, str) and len(item) <= 320]
    estimated = result.get("estimatedTotalHits", len(refs))
    total = int(estimated) if isinstance(estimated, int) else len(refs)
    return refs, offset, offset + len(refs) < min(total, 10_000)


async def hydrate_results(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    refs: list[str],
    limit: int,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    parsed_refs: list[tuple[int, str]] = []
    for raw in refs:
        try:
            ref = validate_entity_reference(raw)
            parsed_refs.append(ref.resolve(settings.domain))
        except ValueError:
            continue
    stored = (
        list(
            await session.scalars(
                select(Message).where(tuple_(Message.id, Message.origin_domain).in_(parsed_refs))
            )
        )
        if parsed_refs
        else []
    )
    messages = {(message.id, message.origin_domain): message for message in stored}
    access_cache: dict[tuple[int, str], object | None] = {}
    for message_ref in parsed_refs:
        message = messages.get(message_ref)
        if message is None or message.deleted_at is not None or message.e2ee is not None:
            continue
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if channel is None or channel.encryption_mode != "plaintext":
            continue
        channel_ref = (channel.id, channel.origin_domain)
        if channel_ref not in access_cache:
            try:
                checked = await load_channel_access(
                    session,
                    settings,
                    actor,
                    EntityRef(f"{channel.id}@{channel.origin_domain}").reference,
                )
                if checked.guild is not None:
                    await require_permissions(
                        session,
                        redis,
                        checked.guild,
                        actor,
                        SEARCH_PERMISSIONS,
                        channel=channel,
                    )
                access_cache[channel_ref] = checked
            except HTTPException:
                access_cache[channel_ref] = None
        access = access_cache[channel_ref]
        if access is None:
            continue
        access = cast(ChannelAccess, access)
        content = message.content or ""
        snippet = content if len(content) <= 280 else f"{content[:277]}…"
        results.append(
            {
                "message": await render_message_payload(session, message),
                "channel": channel_payload(channel),
                "guild": guild_payload(access.guild) if access.guild is not None else None,
                "snippet": snippet,
            }
        )
        if len(results) >= limit:
            break
    return results


async def local_search(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
) -> dict[str, object]:
    authority, disabled = await authorize_scope(session, redis, settings, actor, request)
    refs, offset, more = await candidate_refs(settings, request, actor)
    results = await hydrate_results(session, redis, settings, actor, refs, request.limit)
    backfill = await session.get(SearchIndexState, 1)
    indexing = (backfill is not None and not backfill.backfill_completed) or bool(
        await session.scalar(select(exists().where(SearchIndexOutbox.attempts >= 0)))
    )
    remote_dm_authorities = False
    if request.scope == "dms":
        remote_dm_authorities = bool(
            await session.scalar(
                select(
                    exists()
                    .where(
                        DMParticipant.user_id == actor.id,
                        DMParticipant.user_domain == actor.origin_domain,
                        DMParticipant.conversation_id == DMConversation.id,
                        DMParticipant.conversation_domain == DMConversation.origin_domain,
                        DMConversation.authority_domain != settings.domain,
                    )
                    .correlate(None)
                )
            )
        )
    return {
        "results": results,
        "next_cursor": encode_cursor(offset + len(refs)) if more else None,
        "coverage": {
            "local": (
                "cached"
                if remote_dm_authorities or authority not in {None, settings.domain}
                else "complete"
            ),
            "authority": (
                "not_queried"
                if remote_dm_authorities or authority not in {None, settings.domain}
                else "not_needed"
            ),
        },
        "encrypted_channel_refs": disabled,
        "indexing": indexing,
    }


def federated_search_payload(local: dict[str, object]) -> dict[str, object]:
    """Reduce a local response to the small, peer-safe search wire shape."""

    wire_results: list[dict[str, object]] = []
    raw_results = local.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
    for raw in raw_results[:50]:
        if not isinstance(raw, dict):
            continue
        message = raw.get("message")
        channel = raw.get("channel")
        guild = raw.get("guild")
        if not isinstance(message, dict) or not isinstance(channel, dict):
            continue
        guild_ref = None
        if isinstance(guild, dict):
            guild_ref = f"{guild.get('id')}@{guild.get('origin_domain')}"
        wire_results.append(
            FederatedMessageSearchResult.model_validate(
                {
                    "message_ref": f"{message.get('id')}@{message.get('origin_domain')}",
                    "channel_ref": f"{channel.get('id')}@{channel.get('origin_domain')}",
                    "guild_ref": guild_ref,
                    "author_ref": f"{message.get('author_id')}@{message.get('author_domain')}",
                    "snippet": str(raw.get("snippet") or ""),
                    "created_at": message.get("created_at"),
                }
            ).model_dump(mode="json")
        )
    return {
        "results": wire_results,
        "next_cursor": local.get("next_cursor"),
        "encrypted_channel_refs": local.get("encrypted_channel_refs", []),
    }


async def materialize_federated_results(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
    response: FederatedMessageSearchResponse,
) -> list[dict[str, object]]:
    """Re-authorize and rebuild every peer result from bounded local state."""

    results: list[dict[str, object]] = []
    for remote in response.results:
        message_id, message_domain = remote.message_ref.resolve(settings.domain)
        channel_id, channel_domain = remote.channel_ref.resolve(settings.domain)
        channel = await session.get(Channel, (channel_id, channel_domain))
        if channel is None or channel.encryption_mode != "plaintext":
            continue
        if request.scope == "channel" and request.scope_ref != remote.channel_ref:
            continue
        if request.scope == "guild":
            if remote.guild_ref is None or remote.guild_ref != request.scope_ref:
                continue
            guild_id, guild_domain = remote.guild_ref.resolve(settings.domain)
            if (channel.guild_id, channel.guild_domain) != (guild_id, guild_domain):
                continue
        elif request.scope == "dms" and channel.type != 1:
            continue
        try:
            access = await load_channel_access(
                session, settings, actor, remote.channel_ref.reference
            )
            if access.guild is not None:
                await require_permissions(
                    session, redis, access.guild, actor, SEARCH_PERMISSIONS, channel=channel
                )
        except HTTPException:
            continue
        stored = await session.get(Message, (message_id, message_domain))
        author_id, author_domain = remote.author_ref.resolve(settings.domain)
        # Guild history has no stateless per-message paging endpoint. Do not
        # offer a result the client cannot open; retained/history-sync rows
        # become searchable as soon as they exist in the authorized replica.
        if stored is None and access.guild is not None:
            continue
        if stored is not None:
            if (
                (stored.channel_id, stored.channel_domain) != (channel.id, channel.origin_domain)
                or (stored.author_id, stored.author_domain) != (author_id, author_domain)
                or stored.deleted_at is not None
                or stored.e2ee is not None
            ):
                continue
            message_payload = await render_message_payload(session, stored)
            local_content = stored.content or ""
            snippet = local_content if len(local_content) <= 280 else f"{local_content[:277]}…"
        else:
            # A remote authority cannot invent or rewrite a message authored by
            # one of our users. Locally authored messages must exist locally.
            if author_domain == settings.domain:
                continue
            embedded_created_ms = EPOCH_MS + (message_id >> (WORKER_BITS + SEQUENCE_BITS))
            if abs(embedded_created_ms - int(remote.created_at.timestamp() * 1000)) > 60_000:
                continue
            author = await session.get(User, (author_id, author_domain))
            message_payload = {
                "id": str(message_id),
                "origin_domain": message_domain,
                "channel_id": str(channel.id),
                "channel_domain": channel.origin_domain,
                "author_id": str(author_id),
                "author_domain": author_domain,
                "author": user_payload(author) if author is not None else None,
                "content": None,
                "e2ee": None,
                "message_type": 0,
                "flags": 0,
                "client_nonce": None,
                "referenced_message_id": None,
                "referenced_message_domain": None,
                "mention_user_refs": [],
                "attachments": [],
                "webhook_id": None,
                "webhook": None,
                "edited_at": None,
                "deleted_at": None,
                "created_at": remote.created_at.isoformat(),
            }
            snippet = remote.snippet
        results.append(
            {
                "message": message_payload,
                "channel": channel_payload(channel),
                "guild": guild_payload(access.guild) if access.guild is not None else None,
                "snippet": snippet,
            }
        )
    return results


async def search_with_authority(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
) -> dict[str, object]:
    authority, _ = await authorize_scope(session, redis, settings, actor, request)
    local: dict[str, object]
    try:
        local = await local_search(session, redis, settings, actor, request)
    except SearchUnavailable:
        local = {
            "results": [],
            "next_cursor": None,
            "coverage": {"local": "unavailable", "authority": "not_queried"},
            "encrypted_channel_refs": [],
            "indexing": False,
        }
    if authority in {None, settings.domain}:
        if local["coverage"] == {"local": "unavailable", "authority": "not_queried"}:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "SEARCH_UNAVAILABLE",
                    "message": "Message search is temporarily unavailable.",
                },
            )
        return local
    authority = cast(str, authority)
    instance = await session.get(Instance, authority)
    if instance is None or "message-search/1" not in instance.capabilities:
        try:
            instance = await ensure_peer(session, settings, authority, force=True)
        except FederationNetworkError:
            instance = None
    if instance is None or "message-search/1" not in instance.capabilities:
        coverage = cast(dict[str, object], local["coverage"])
        local["coverage"] = {"local": coverage["local"], "authority": "unsupported"}
        return local
    payload = request.model_dump(mode="json")
    payload["actor_ref"] = f"{actor.id}@{actor.origin_domain}"
    try:
        response = await signed_request(
            session,
            settings,
            "POST",
            str(authority),
            "/_kaede/v1/search/messages",
            payload=payload,
            request_timeout=settings.search_federation_timeout_seconds,
            max_response_bytes=2 * 1024 * 1024,
        )
        if response.status_code != 200:
            raise FederationNetworkError("remote search failed")
        raw_remote = decode_federation_response_json(response, max_response_bytes=2 * 1024 * 1024)
        remote = FederatedMessageSearchResponse.model_validate(raw_remote)
    except (FederationNetworkError, ValueError):
        coverage = cast(dict[str, object], local["coverage"])
        local["coverage"] = {"local": coverage["local"], "authority": "unavailable"}
        return local
    remote_results = await materialize_federated_results(
        session, redis, settings, actor, request, remote
    )
    merged: list[dict[str, object]] = []
    seen: set[str] = set()
    local_results = local.get("results")
    if not isinstance(local_results, list):
        local_results = []
    for item in [*remote_results, *local_results]:
        if not isinstance(item, dict):
            continue
        message = item.get("message")
        if not isinstance(message, dict):
            continue
        key = f"{message.get('id')}@{message.get('origin_domain')}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
        if len(merged) >= request.limit:
            break
    local["results"] = merged
    local["next_cursor"] = remote.next_cursor
    coverage = cast(dict[str, object], local["coverage"])
    local["coverage"] = {"local": coverage["local"], "authority": "complete"}
    return local
