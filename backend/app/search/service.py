from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import exists, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.channel_access import ChannelAccess, effective_channel_nsfw, load_channel_access
from app.chat.payloads import (
    channel_payload,
    guild_payload,
    render_message_payload,
    user_payload,
)
from app.chat.permissions import get_permissions, require_permissions
from app.core.base64url import decode_base64url, encode_base64url
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
    normalize_domain,
)
from app.search.meili import MeiliClient, SearchUnavailable, filter_value
from app.search.schemas import (
    FederatedMessageSearchResponse,
    FederatedMessageSearchResult,
    MessageSearchRequest,
)

SEARCH_PERMISSIONS = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
MAX_CANDIDATES = 150
MAX_DM_SEARCH_AUTHORITIES = 256
DM_SEARCH_CURSOR_TTL_SECONDS = 900
DM_SEARCH_FANOUT_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    ref: str
    ranking_score: float


@dataclass(slots=True)
class DMAuthorityCursor:
    cursor: str | None = None
    exhausted: bool = False
    terminal_status: str | None = None


@dataclass(slots=True)
class DMAuthorityPage:
    authority: str
    results: list[dict[str, object]]
    next_cursor: str | None
    encrypted_channel_refs: list[str]
    indexing: bool
    status: str = "complete"


def cursor_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = decode_base64url(cursor).decode("ascii")
        value = int(raw)
    except (ValueError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"}) from exc
    if not 0 <= value <= 9_975:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    return value


def encode_cursor(offset: int) -> str:
    return encode_base64url(str(offset).encode())


def dm_search_request_digest(request: MessageSearchRequest) -> str:
    payload = request.model_dump(mode="json", exclude={"cursor"})
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def dm_search_cursor_key(token: str) -> str:
    return f"search:dm-cursor:{token}"


async def active_dm_search_authorities(
    session: AsyncSession,
    actor: User,
) -> list[str]:
    rows = list(
        await session.scalars(
            select(DMConversation.authority_domain)
            .join(
                DMParticipant,
                (DMParticipant.conversation_id == DMConversation.id)
                & (DMParticipant.conversation_domain == DMConversation.origin_domain),
            )
            .where(
                DMParticipant.user_id == actor.id,
                DMParticipant.user_domain == actor.origin_domain,
            )
            .distinct()
            .order_by(DMConversation.authority_domain)
            .limit(MAX_DM_SEARCH_AUTHORITIES + 1)
        )
    )
    if len(rows) > MAX_DM_SEARCH_AUTHORITIES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SEARCH_AUTHORITY_FANOUT_TOO_LARGE",
                "message": "Account-wide search spans too many conversation authorities.",
            },
        )
    try:
        return [normalize_domain(item) for item in rows]
    except (TypeError, ValueError) as exc:
        raise SearchUnavailable("DM search contains an invalid authority") from exc


async def load_dm_search_cursor(
    redis: Redis,
    request: MessageSearchRequest,
    actor: User,
    authorities: list[str] | None,
) -> dict[str, DMAuthorityCursor]:
    if request.cursor is None:
        if authorities is None:
            raise ValueError("initial DM search requires an authority snapshot")
        return {authority: DMAuthorityCursor() for authority in authorities}
    token = request.cursor
    if (
        not token.startswith("ksc_")
        or not 20 <= len(token) <= 96
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in token
        )
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    raw = await redis.get(dm_search_cursor_key(token))
    if not isinstance(raw, (str, bytes)):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"}) from exc
    expected_actor = f"{actor.id}@{actor.origin_domain}"
    if (
        not isinstance(value, dict)
        or set(value) != {"v", "actor", "query", "authorities"}
        or value.get("v") != 1
        or value.get("actor") != expected_actor
        or value.get("query") != dm_search_request_digest(request)
        or not isinstance(value.get("authorities"), dict)
    ):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    raw_states = cast(dict[object, object], value["authorities"])
    if len(raw_states) > MAX_DM_SEARCH_AUTHORITIES:
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    if authorities is not None and set(raw_states) != set(authorities):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
    states: dict[str, DMAuthorityCursor] = {}
    for authority, raw_state in raw_states.items():
        if not isinstance(authority, str) or not isinstance(raw_state, dict):
            raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
        try:
            if normalize_domain(authority) != authority:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"}) from exc
        cursor = raw_state.get("cursor")
        exhausted = raw_state.get("exhausted")
        if (
            set(raw_state) != {"cursor", "exhausted", "terminal_status"}
            or (cursor is not None and (not isinstance(cursor, str) or len(cursor) > 512))
            or not isinstance(exhausted, bool)
            or raw_state.get("terminal_status") not in {None, "complete", "unsupported"}
        ):
            raise HTTPException(status_code=400, detail={"code": "INVALID_SEARCH_CURSOR"})
        states[authority] = DMAuthorityCursor(
            cursor=cursor,
            exhausted=exhausted,
            terminal_status=cast(str | None, raw_state.get("terminal_status")),
        )
    return states


async def save_dm_search_cursor(
    redis: Redis,
    request: MessageSearchRequest,
    actor: User,
    states: dict[str, DMAuthorityCursor],
) -> str:
    token = f"ksc_{secrets.token_urlsafe(24)}"
    value = {
        "v": 1,
        "actor": f"{actor.id}@{actor.origin_domain}",
        "query": dm_search_request_digest(request),
        "authorities": {
            authority: {
                "cursor": state.cursor,
                "exhausted": state.exhausted,
                "terminal_status": state.terminal_status,
            }
            for authority, state in sorted(states.items())
        },
    }
    await redis.set(
        dm_search_cursor_key(token),
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        ex=DM_SEARCH_CURSOR_TTL_SECONDS,
    )
    return token


def meili_filters(
    request: MessageSearchRequest,
    actor: User,
    settings: Settings,
    *,
    dm_authority: str | None = None,
) -> list[str]:
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
        if dm_authority is not None:
            values.append(f"dm_authority = {filter_value(dm_authority)}")
    if request.filters.channel_ids:
        channels = [
            f"channel_ref = {filter_value(f'{identifier}@{domain}')}"
            for item in request.filters.channel_ids
            for identifier, domain in [item.resolve(settings.domain)]
        ]
        values.append(f"({' OR '.join(channels)})")
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
    if request.filters.mentions_role_ids:
        roles = [
            f"mention_role_refs = {filter_value(f'{identifier}@{domain}')}"
            for item in request.filters.mentions_role_ids
            for identifier, domain in [item.resolve(settings.domain)]
        ]
        values.append(f"({' OR '.join(roles)})")
    if request.filters.mention_everyone is not None:
        values.append(
            f"mention_everyone = {'true' if request.filters.mention_everyone else 'false'}"
        )
    if request.filters.replied_to_user_ids:
        replied_users = [
            f"replied_to_user_ref = {filter_value(f'{identifier}@{domain}')}"
            for item in request.filters.replied_to_user_ids
            for identifier, domain in [item.resolve(settings.domain)]
        ]
        values.append(f"({' OR '.join(replied_users)})")
    if request.filters.replied_to_message_ids:
        replied_messages = [
            f"replied_to_message_ref = {filter_value(f'{identifier}@{domain}')}"
            for item in request.filters.replied_to_message_ids
            for identifier, domain in [item.resolve(settings.domain)]
        ]
        values.append(f"({' OR '.join(replied_messages)})")
    if request.filters.has:
        positive = [value for value in request.filters.has if not value.startswith("-")]
        negative = [value[1:] for value in request.filters.has if value.startswith("-")]
        if positive:
            content_filters = " OR ".join(
                f"content_types = {filter_value(value)}" for value in positive
            )
            values.append(f"({content_filters})")
        values.extend(f"content_types != {filter_value(value)}" for value in negative)
    for field, attribute in (
        (request.filters.embed_types, "embed_types"),
        (request.filters.embed_providers, "embed_providers"),
        (request.filters.link_hostnames, "link_hostnames"),
        (request.filters.attachment_filenames, "attachment_filenames"),
        (request.filters.attachment_extensions, "attachment_extensions"),
    ):
        if field:
            values.append(
                f"({' OR '.join(f'{attribute} = {filter_value(value)}' for value in field)})"
            )
    if request.filters.pinned is not None:
        values.append(f"pinned = {'true' if request.filters.pinned else 'false'}")
    author_types = (
        [request.filters.author_type]
        if request.filters.author_type is not None
        else request.filters.author_types
    )
    included_author_types = [value for value in author_types if not value.startswith("-")]
    excluded_author_types = [value[1:] for value in author_types if value.startswith("-")]
    if included_author_types:
        author_type_filters = " OR ".join(
            f"author_type = {filter_value(value)}" for value in included_author_types
        )
        values.append(f"({author_type_filters})")
    values.extend(f"author_type != {filter_value(value)}" for value in excluded_author_types)
    if request.filters.before is not None:
        values.append(
            f"created_at_ms < {int(request.filters.before.astimezone(UTC).timestamp() * 1000)}"
        )
    if request.filters.after is not None:
        values.append(
            f"created_at_ms > {int(request.filters.after.astimezone(UTC).timestamp() * 1000)}"
        )
    if request.filters.max_id is not None:
        values.append(f"message_id < {request.filters.max_id.id}")
    if request.filters.min_id is not None:
        values.append(f"message_id > {request.filters.min_id.id}")
    if not request.include_nsfw or (
        actor.account_type != "bot" and getattr(actor, "age_assurance_state", "unknown") != "adult"
    ):
        values.append("nsfw = false")
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
    if request.filters.channel_ids and request.scope != "guild":
        raise HTTPException(status_code=400, detail={"code": "SEARCH_CHANNEL_FILTER_INVALID"})
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
        encrypted_channels = list(
            await session.scalars(
                select(Channel)
                .where(
                    Channel.guild_id == guild_id,
                    Channel.guild_domain == guild_domain,
                    Channel.encryption_mode == "e2ee",
                    Channel.unavailable.is_(False),
                )
                .order_by(Channel.position, Channel.id)
                .limit(10_000)
            )
        )
        disabled = []
        for channel in encrypted_channels:
            permissions = await get_permissions(
                session,
                redis,
                guild,
                actor,
                channel=channel,
            )
            if permissions & Permission.VIEW_CHANNEL:
                disabled.append(f"{channel.id}@{channel.origin_domain}")
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
    settings: Settings,
    request: MessageSearchRequest,
    actor: User,
    *,
    dm_authority: str | None = None,
) -> tuple[list[SearchCandidate], int, int]:
    if not settings.search_enabled or settings.search_master_key is None:
        raise SearchUnavailable("message search is not enabled on this instance")
    if request.slop != 2:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SEARCH_SLOP_UNSUPPORTED",
                "message": "This search backend cannot enforce a custom word slop exactly.",
            },
        )
    offset = cursor_offset(request.cursor)
    payload: dict[str, object] = {
        "q": request.query,
        "filter": meili_filters(request, actor, settings, dm_authority=dm_authority),
        "offset": offset,
        "limit": min(MAX_CANDIDATES, request.limit * 3),
        "attributesToRetrieve": ["message_ref"],
        "showRankingScore": True,
    }
    if request.sort != "relevance":
        payload["sort"] = [f"created_at_ms:{'desc' if request.sort == 'newest' else 'asc'}"]
    result = await MeiliClient(settings).search(payload)
    hits = result.get("hits")
    if not isinstance(hits, list) or len(hits) > cast(int, payload["limit"]):
        raise SearchUnavailable("message search returned an invalid result")
    candidates: list[SearchCandidate] = []
    for item in hits:
        raw_ref = item.get("message_ref") if isinstance(item, dict) else None
        raw_score = item.get("_rankingScore") if isinstance(item, dict) else None
        if not isinstance(raw_ref, str) or len(raw_ref) > 320:
            raise SearchUnavailable("message search returned an invalid result")
        if (
            isinstance(raw_score, bool)
            or not isinstance(raw_score, (int, float))
            or not 0 <= float(raw_score) <= 1
        ):
            raise SearchUnavailable("message search returned an invalid ranking score")
        try:
            validate_entity_reference(raw_ref)
        except ValueError as exc:
            raise SearchUnavailable("message search returned an invalid result") from exc
        candidates.append(SearchCandidate(raw_ref, float(raw_score)))
    if len(candidates) != len({item.ref for item in candidates}):
        raise SearchUnavailable("message search returned duplicate results")
    estimated = result.get("estimatedTotalHits", len(candidates))
    if type(estimated) is not int or estimated < len(candidates) or estimated > 10_000:
        raise SearchUnavailable("message search returned an invalid result count")
    if not candidates and offset < estimated:
        raise SearchUnavailable("message search returned an incomplete result page")
    return candidates, offset, estimated


async def hydrate_results(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    candidates: list[SearchCandidate],
    limit: int,
    request: MessageSearchRequest,
    *,
    offset: int,
) -> tuple[list[dict[str, object]], int]:
    results: list[dict[str, object]] = []
    parsed_refs: list[tuple[int, str]] = []
    for candidate in candidates:
        ref = validate_entity_reference(candidate.ref)
        parsed_refs.append(ref.resolve(settings.domain))
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
    consumed = 0
    for index, message_ref in enumerate(parsed_refs):
        consumed = index + 1
        message = messages.get(message_ref)
        if message is None or message.deleted_at is not None or message.e2ee is not None:
            continue
        channel = await session.get(Channel, (message.channel_id, message.channel_domain))
        if channel is None or channel.encryption_mode != "plaintext":
            continue
        nsfw = await effective_channel_nsfw(session, channel)
        may_include_nsfw = request.include_nsfw and (
            actor.account_type == "bot"
            or getattr(actor, "age_assurance_state", "unknown") == "adult"
        )
        if nsfw is None or (nsfw and not may_include_nsfw):
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
                "_search_score": candidates[index].ranking_score,
                "_search_cursor_after": encode_cursor(offset + index + 1),
            }
        )
        if len(results) >= limit:
            break
    return results, consumed


async def local_search(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
    *,
    dm_authority: str | None = None,
) -> dict[str, object]:
    if dm_authority is not None and request.scope != "dms":
        raise ValueError("a DM authority filter requires account-wide DM scope")
    authority, disabled = await authorize_scope(session, redis, settings, actor, request)
    candidates, offset, total = await candidate_refs(
        settings,
        request,
        actor,
        dm_authority=dm_authority,
    )
    results, consumed = await hydrate_results(
        session,
        redis,
        settings,
        actor,
        candidates,
        request.limit,
        request,
        offset=offset,
    )
    next_offset = offset + consumed
    more = next_offset <= 9_975 and next_offset < total
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
        "next_cursor": encode_cursor(next_offset) if more else None,
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
        raise SearchUnavailable("message search produced an invalid local result")
    if len(raw_results) > 25:
        raise SearchUnavailable("message search produced too many local results")
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise SearchUnavailable("message search produced an invalid local result")
        message = raw.get("message")
        channel = raw.get("channel")
        guild = raw.get("guild")
        if not isinstance(message, dict) or not isinstance(channel, dict):
            raise SearchUnavailable("message search produced an invalid local result")
        ranking_score = raw.get("_search_score")
        cursor_after = raw.get("_search_cursor_after")
        if (
            isinstance(ranking_score, bool)
            or not isinstance(ranking_score, (int, float))
            or not isinstance(cursor_after, str)
        ):
            raise SearchUnavailable("message search produced an invalid local cursor")
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
                    "ranking_score": float(ranking_score),
                    "cursor_after": cursor_after,
                }
            ).model_dump(mode="json")
        )
    return {
        "results": wire_results,
        "next_cursor": local.get("next_cursor"),
        "encrypted_channel_refs": local.get("encrypted_channel_refs", []),
        "indexing": bool(local.get("indexing", False)),
    }


def public_search_payload(page: dict[str, object]) -> dict[str, object]:
    """Remove authority-merge cursors/scores from the public result shape."""

    raw_results = page.get("results")
    if not isinstance(raw_results, list):
        raise SearchUnavailable("message search produced an invalid result page")
    projected: list[dict[str, object]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            raise SearchUnavailable("message search produced an invalid result page")
        projected.append(
            {key: value for key, value in item.items() if not key.startswith("_search_")}
        )
    return page | {"results": projected}


async def materialize_federated_results(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
    response: FederatedMessageSearchResponse,
    *,
    expected_authority: str | None = None,
) -> list[dict[str, object]]:
    """Re-authorize and rebuild every peer result from bounded local state."""

    results: list[dict[str, object]] = []
    for remote in response.results:
        message_id, message_domain = remote.message_ref.resolve(settings.domain)
        channel_id, channel_domain = remote.channel_ref.resolve(settings.domain)
        channel = await session.get(Channel, (channel_id, channel_domain))
        if channel is None or channel.encryption_mode != "plaintext":
            raise SearchUnavailable("remote search referenced an unavailable channel")
        nsfw = await effective_channel_nsfw(session, channel)
        may_include_nsfw = request.include_nsfw and (
            actor.account_type == "bot"
            or getattr(actor, "age_assurance_state", "unknown") == "adult"
        )
        if nsfw is None or (nsfw and not may_include_nsfw):
            raise SearchUnavailable("remote search returned disallowed NSFW content")
        if request.scope == "channel" and request.scope_ref != remote.channel_ref:
            raise SearchUnavailable("remote search returned the wrong channel")
        if request.scope == "guild":
            if remote.guild_ref is None or remote.guild_ref != request.scope_ref:
                raise SearchUnavailable("remote search returned the wrong guild")
            guild_id, guild_domain = remote.guild_ref.resolve(settings.domain)
            if (channel.guild_id, channel.guild_domain) != (guild_id, guild_domain):
                raise SearchUnavailable("remote search returned inconsistent guild linkage")
        elif request.scope == "dms" and channel.type != 1:
            raise SearchUnavailable("remote DM search returned a guild channel")
        if request.scope == "dms" and expected_authority is not None:
            conversation = await session.get(DMConversation, (channel.id, channel.origin_domain))
            if conversation is None or conversation.authority_domain != expected_authority:
                raise SearchUnavailable("remote DM search returned the wrong authority")
        try:
            access = await load_channel_access(
                session, settings, actor, remote.channel_ref.reference
            )
            if access.guild is not None:
                await require_permissions(
                    session, redis, access.guild, actor, SEARCH_PERMISSIONS, channel=channel
                )
        except HTTPException:
            raise SearchUnavailable("remote search returned an unauthorized channel") from None
        stored = await session.get(Message, (message_id, message_domain))
        author_id, author_domain = remote.author_ref.resolve(settings.domain)
        # Guild history has no stateless per-message paging endpoint. Do not
        # offer a result the client cannot open; retained/history-sync rows
        # become searchable as soon as they exist in the authorized replica.
        if stored is None and access.guild is not None:
            raise SearchUnavailable("remote guild search returned unavailable history")
        if stored is not None:
            if (
                (stored.channel_id, stored.channel_domain) != (channel.id, channel.origin_domain)
                or (stored.author_id, stored.author_domain) != (author_id, author_domain)
                or stored.deleted_at is not None
                or stored.e2ee is not None
            ):
                raise SearchUnavailable("remote search returned inconsistent message linkage")
            message_payload = await render_message_payload(session, stored)
            local_content = stored.content or ""
            snippet = local_content if len(local_content) <= 280 else f"{local_content[:277]}…"
        else:
            # A remote authority cannot invent or rewrite a message authored by
            # one of our users. Locally authored messages must exist locally.
            if author_domain == settings.domain:
                raise SearchUnavailable("remote search invented a locally-authored message")
            embedded_created_ms = EPOCH_MS + (message_id >> (WORKER_BITS + SEQUENCE_BITS))
            if abs(embedded_created_ms - int(remote.created_at.timestamp() * 1000)) > 60_000:
                raise SearchUnavailable("remote search timestamp does not match its snowflake")
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
                "mention_role_refs": [],
                "mention_everyone": False,
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
                "_search_score": remote.ranking_score,
                "_search_cursor_after": remote.cursor_after,
            }
        )
    return results


async def search_authority_available(
    session: AsyncSession,
    settings: Settings,
    authority: str,
) -> bool:
    if authority == settings.domain:
        return True
    instance = await session.get(Instance, authority)
    if instance is None or "message-search/1" not in instance.capabilities:
        try:
            instance = await ensure_peer(session, settings, authority, force=True)
        except FederationNetworkError:
            return False
    return "message-search/1" in instance.capabilities


async def remote_search_wire_response(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    actor: User,
    authority: str,
    request: MessageSearchRequest,
) -> FederatedMessageSearchResponse:
    payload = request.model_dump(mode="json")
    payload["actor_ref"] = f"{actor.id}@{actor.origin_domain}"
    async with session_factory() as request_session:
        response = await signed_request(
            request_session,
            settings,
            "POST",
            authority,
            "/_kaede/v1/search/messages",
            payload=payload,
            request_timeout=settings.search_federation_timeout_seconds,
            max_response_bytes=2 * 1024 * 1024,
            allow_json_floats=True,
            guild_context=request.scope == "guild",
        )
    if response.status_code != 200:
        raise FederationNetworkError("remote search failed")
    raw_remote = decode_federation_response_json(
        response,
        max_response_bytes=2 * 1024 * 1024,
    )
    return FederatedMessageSearchResponse.model_validate(raw_remote)


def search_result_sort_key(
    item: dict[str, object],
    sort: str,
    authority: str,
    position: int,
) -> tuple[object, ...]:
    raw_message = item.get("message")
    if not isinstance(raw_message, dict):
        raise SearchUnavailable("message search produced an invalid result")
    raw_created_at = raw_message.get("created_at")
    raw_id = raw_message.get("id")
    raw_origin = raw_message.get("origin_domain")
    if (
        not isinstance(raw_created_at, str)
        or not isinstance(raw_id, str)
        or not isinstance(raw_origin, str)
    ):
        raise SearchUnavailable("message search produced an invalid result")
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except ValueError as exc:
        raise SearchUnavailable("message search produced an invalid timestamp") from exc
    if created_at.tzinfo is None:
        raise SearchUnavailable("message search produced a naive timestamp")
    score = item.get("_search_score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise SearchUnavailable("message search produced an invalid ranking score")
    identity = f"{raw_id}@{raw_origin}"
    timestamp = created_at.timestamp()
    if sort == "relevance":
        return (-float(score), -timestamp, authority, position, identity)
    if sort == "oldest":
        return (timestamp, authority, position, identity)
    return (-timestamp, authority, position, identity)


async def search_account_dms(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
) -> dict[str, object]:
    """Search every active DM authority with one actor-bound merge cursor."""

    initial_authorities = (
        await active_dm_search_authorities(session, actor) if request.cursor is None else None
    )
    states = await load_dm_search_cursor(
        redis,
        request,
        actor,
        initial_authorities,
    )
    pages: dict[str, DMAuthorityPage] = {}
    remote_requests: list[tuple[str, MessageSearchRequest]] = []
    for authority, state in sorted(states.items()):
        if state.exhausted:
            pages[authority] = DMAuthorityPage(
                authority,
                [],
                None,
                [],
                False,
                status=state.terminal_status or "complete",
            )
            continue
        authority_request = request.model_copy(update={"cursor": state.cursor})
        if authority == settings.domain:
            try:
                local = await local_search(
                    session,
                    redis,
                    settings,
                    actor,
                    authority_request,
                    dm_authority=settings.domain,
                )
                raw_results = local.get("results")
                raw_encrypted = local.get("encrypted_channel_refs", [])
                if not isinstance(raw_results, list) or not isinstance(raw_encrypted, list):
                    raise SearchUnavailable("local DM search produced an invalid result")
                pages[authority] = DMAuthorityPage(
                    authority=authority,
                    results=[cast(dict[str, object], item) for item in raw_results],
                    next_cursor=cast(str | None, local.get("next_cursor")),
                    encrypted_channel_refs=[str(item) for item in raw_encrypted],
                    indexing=bool(local.get("indexing", False)),
                )
            except SearchUnavailable:
                pages[authority] = DMAuthorityPage(
                    authority,
                    [],
                    state.cursor,
                    [],
                    False,
                    status="unavailable",
                )
            continue
        if not await search_authority_available(session, settings, authority):
            pages[authority] = DMAuthorityPage(
                authority,
                [],
                None,
                [],
                False,
                status="unsupported",
            )
            states[authority].exhausted = True
            states[authority].terminal_status = "unsupported"
            continue
        remote_requests.append((authority, authority_request))

    if remote_requests:
        bind = session.bind
        if bind is None:
            raise SearchUnavailable("DM search cannot create bounded federation sessions")
        session_factory = async_sessionmaker(bind, expire_on_commit=False)
        semaphore = asyncio.Semaphore(DM_SEARCH_FANOUT_CONCURRENCY)

        async def fetch(
            authority: str,
            authority_request: MessageSearchRequest,
        ) -> tuple[str, FederatedMessageSearchResponse | None]:
            async with semaphore:
                try:
                    remote = await remote_search_wire_response(
                        session_factory,
                        settings,
                        actor,
                        authority,
                        authority_request,
                    )
                except (FederationNetworkError, SearchUnavailable, ValueError):
                    return authority, None
                return authority, remote

        responses = await asyncio.gather(
            *(
                fetch(authority, authority_request)
                for authority, authority_request in remote_requests
            )
        )
        request_by_authority = dict(remote_requests)
        for authority, remote in responses:
            state = states[authority]
            if remote is None:
                pages[authority] = DMAuthorityPage(
                    authority,
                    [],
                    state.cursor,
                    [],
                    False,
                    status="unavailable",
                )
                continue
            try:
                results = await materialize_federated_results(
                    session,
                    redis,
                    settings,
                    actor,
                    request_by_authority[authority],
                    remote,
                    expected_authority=authority,
                )
            except SearchUnavailable:
                pages[authority] = DMAuthorityPage(
                    authority,
                    [],
                    state.cursor,
                    [],
                    False,
                    status="unavailable",
                )
                continue
            if len(results) != len(remote.results):
                raise SearchUnavailable("remote DM search silently lost results")
            pages[authority] = DMAuthorityPage(
                authority=authority,
                results=results,
                next_cursor=remote.next_cursor,
                encrypted_channel_refs=[
                    str(item.reference) for item in remote.encrypted_channel_refs
                ],
                indexing=remote.indexing,
            )

    ranked: list[tuple[tuple[object, ...], str, int, dict[str, object]]] = []
    for authority, page in sorted(pages.items()):
        for position, item in enumerate(page.results):
            ranked.append(
                (
                    search_result_sort_key(item, request.sort, authority, position),
                    authority,
                    position,
                    item,
                )
            )
    ranked.sort(key=lambda item: item[0])
    selected = ranked[: request.limit]
    selected_counts: dict[str, int] = {}
    for _key, authority, position, _item in selected:
        expected_position = selected_counts.get(authority, 0)
        if position != expected_position:
            raise SearchUnavailable("DM search merge violated source pagination order")
        selected_counts[authority] = expected_position + 1

    for authority, page in pages.items():
        if page.status != "complete":
            continue
        selected_count = selected_counts.get(authority, 0)
        if not page.results:
            states[authority].cursor = page.next_cursor
            states[authority].exhausted = page.next_cursor is None
            if states[authority].exhausted:
                states[authority].terminal_status = "complete"
        elif selected_count:
            cursor_after = page.results[selected_count - 1].get("_search_cursor_after")
            if not isinstance(cursor_after, str):
                raise SearchUnavailable("DM search result lost its source cursor")
            states[authority].cursor = cursor_after
            if selected_count == len(page.results):
                states[authority].cursor = page.next_cursor
                states[authority].exhausted = page.next_cursor is None
                if states[authority].exhausted:
                    states[authority].terminal_status = "complete"

    pending = any(not state.exhausted for state in states.values())
    next_cursor = await save_dm_search_cursor(redis, request, actor, states) if pending else None
    encrypted_refs = sorted(
        {item for page in pages.values() for item in page.encrypted_channel_refs}
    )
    if len(encrypted_refs) > 10_000:
        raise SearchUnavailable("DM search encrypted-channel disclosure is too large")
    statuses = {authority: page.status for authority, page in sorted(pages.items())}
    return public_search_payload(
        {
            "results": [item for _key, _authority, _position, item in selected],
            "next_cursor": next_cursor,
            "coverage": {
                "local": statuses.get(settings.domain, "not_needed"),
                "authority": (
                    "complete"
                    if all(status == "complete" for status in statuses.values())
                    else "partial"
                ),
                "authorities": statuses,
            },
            "encrypted_channel_refs": encrypted_refs,
            "indexing": any(page.indexing for page in pages.values()),
        }
    )


async def search_with_authority(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    actor: User,
    request: MessageSearchRequest,
) -> dict[str, object]:
    if request.scope == "dms":
        return await search_account_dms(session, redis, settings, actor, request)
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
        return public_search_payload(local)
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
            guild_context=request.scope == "guild",
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
        session,
        redis,
        settings,
        actor,
        request,
        remote,
        expected_authority=authority,
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
    return public_search_payload(local)
