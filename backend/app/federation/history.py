from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.chat.e2ee import validate_e2ee_envelope
from app.chat.payloads import message_payload, user_payload
from app.chat.permissions import calculate_permissions
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.models import (
    Attachment,
    Channel,
    FederatedHistoryMessage,
    Guild,
    GuildEvent,
    GuildHistoryExport,
    GuildHistoryExportChannel,
    GuildHistoryImport,
    GuildHistoryImportChannel,
    GuildHistoryStagedMessage,
    GuildMember,
    Instance,
    Message,
    Pin,
    Reaction,
    ReadState,
    User,
)
from app.federation.client import signed_request
from app.federation.network import ensure_peer, normalize_domain
from app.federation.replication import (
    advance_channel_cursor,
    database_snowflake,
    replicate_message_attachments,
    resolve_delegated_profile,
)
from app.federation.schemas import RemoteUserProfile
from app.federation.security import validated_event_envelope

HISTORY_CAPABILITY = "guild-history-sync/1"
HISTORY_RECENT_FIRST_CAPABILITY = "guild-history-sync/2"
HISTORY_EVENT_TYPES = frozenset(
    {
        "guild.message.update",
        "guild.message.delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.pin.add",
        "guild.pin.remove",
    }
)

ABANDONED_IMPORT_RETENTION = timedelta(days=7)


async def cleanup_history_transfers(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Prune expired grants and abandoned resumable-import state.

    Completed imports are retained because their identity is the provenance
    anchor for imported messages. Stale non-terminal imports can be safely
    retried after deletion, while the provenance guard protects against a
    malformed state transition deleting already-merged history.
    """

    current_time = now or datetime.now(UTC)
    expired_exports = await session.execute(
        delete(GuildHistoryExport).where(GuildHistoryExport.expires_at < current_time)
    )
    provenance_exists = exists(
        select(FederatedHistoryMessage.message_id).where(
            FederatedHistoryMessage.export_id == GuildHistoryImport.export_id,
            FederatedHistoryMessage.export_domain == GuildHistoryImport.export_domain,
        )
    )
    abandoned_imports = await session.execute(
        delete(GuildHistoryImport).where(
            GuildHistoryImport.status != "completed",
            GuildHistoryImport.updated_at < current_time - ABANDONED_IMPORT_RETENTION,
            ~provenance_exists,
        )
    )
    return {
        "history_exports": int(expired_exports.rowcount or 0),  # type: ignore[attr-defined]
        "history_imports": int(abandoned_imports.rowcount or 0),  # type: ignore[attr-defined]
    }


def effective_history_policy(guild: Guild, channel: Channel) -> str:
    if channel.federated_history_policy == "inherit":
        return guild.federated_history_policy
    return channel.federated_history_policy


async def history_channel_allowed(
    session: AsyncSession,
    guild: Guild,
    user: User,
    channel: Channel,
) -> bool:
    if channel.type not in {0, 5} or channel.unavailable:
        return False
    if effective_history_policy(guild, channel) != "full_retained":
        return False
    permissions, _member = await calculate_permissions(session, guild, user, channel=channel)
    required = Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY
    return permissions & required == required


async def eligible_history_channels(
    session: AsyncSession,
    guild: Guild,
    user: User,
) -> list[Channel]:
    channels = list(
        await session.scalars(
            select(Channel)
            .where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
                Channel.type.in_((0, 5)),
                Channel.unavailable.is_(False),
            )
            .order_by(Channel.position, Channel.id)
        )
    )
    return [
        channel
        for channel in channels
        if await history_channel_allowed(session, guild, user, channel)
    ]


async def create_history_export(
    session: AsyncSession,
    settings: Settings,
    snowflake: SnowflakeGenerator,
    guild: Guild,
    user: User,
    requester_origin: str,
) -> GuildHistoryExport | None:
    requester_origin = normalize_domain(requester_origin)
    if guild.origin_domain != settings.domain or user.origin_domain != requester_origin:
        raise HTTPException(status_code=403, detail={"code": "KAED_FED_HISTORY_FORBIDDEN"})
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, user.id, user.origin_domain),
    )
    if membership is None:
        raise HTTPException(status_code=403, detail={"code": "NOT_A_GUILD_MEMBER"})
    channels = await eligible_history_channels(session, guild, user)
    if not channels:
        return None
    now = datetime.now(UTC)
    existing = await session.scalar(
        select(GuildHistoryExport)
        .where(
            GuildHistoryExport.guild_id == guild.id,
            GuildHistoryExport.guild_domain == guild.origin_domain,
            GuildHistoryExport.requester_origin == requester_origin,
            GuildHistoryExport.requester_user_id == user.id,
            GuildHistoryExport.requester_member_version == membership.member_version,
            GuildHistoryExport.permission_generation == guild.permission_generation,
            GuildHistoryExport.history_policy_generation == guild.history_policy_generation,
            GuildHistoryExport.status.in_(("active", "completed")),
            GuildHistoryExport.expires_at > now,
        )
        .order_by(GuildHistoryExport.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    export = GuildHistoryExport(
        id=await snowflake.mint(),
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        requester_origin=requester_origin,
        requester_user_id=user.id,
        requester_user_domain=user.origin_domain,
        requester_member_version=membership.member_version,
        baseline_seq=guild.next_event_seq - 1,
        permission_generation=guild.permission_generation,
        history_policy_generation=guild.history_policy_generation,
        expires_at=now + timedelta(minutes=settings.federation_history_export_ttl_minutes),
    )
    session.add(export)
    await session.flush()
    upper_bound_rows = (
        await session.execute(
            select(Message.channel_id, func.max(Message.id))
            .where(
                Message.channel_domain == guild.origin_domain,
                Message.channel_id.in_([channel.id for channel in channels]),
                Message.deleted_at.is_(None),
            )
            .group_by(Message.channel_id)
        )
    ).all()
    upper_bounds: dict[int, int] = {
        channel_id: int(upper_bound)
        for channel_id, upper_bound in upper_bound_rows
        if upper_bound is not None
    }
    for channel in channels:
        session.add(
            GuildHistoryExportChannel(
                export_id=export.id,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                upper_bound_id=int(upper_bounds.get(channel.id) or 0),
            )
        )
    await session.flush()
    return export


async def _active_export(
    session: AsyncSession,
    export_id: int,
    requester_origin: str,
) -> tuple[GuildHistoryExport, Guild, User]:
    export = await session.get(GuildHistoryExport, export_id)
    now = datetime.now(UTC)
    if export is None or export.requester_origin != requester_origin:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    if export.expires_at <= now:
        export.status = "expired"
        raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_EXPIRED"})
    if export.status == "revoked":
        raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_REVOKED"})
    if export.status not in {"active", "completed"}:
        raise HTTPException(status_code=409, detail={"code": "KAED_FED_HISTORY_UNAVAILABLE"})
    guild = await session.get(Guild, (export.guild_id, export.guild_domain))
    user = await session.get(
        User,
        (export.requester_user_id, export.requester_user_domain),
    )
    if guild is None or user is None:
        raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_REVOKED"})
    if (
        export.permission_generation != guild.permission_generation
        or export.history_policy_generation != guild.history_policy_generation
    ):
        export.status = "revoked"
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_HISTORY_GRANT_STALE", "retryable": True},
        )
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, user.id, user.origin_domain),
    )
    if membership is None:
        export.status = "revoked"
        raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_REVOKED"})
    if membership.member_version != export.requester_member_version:
        export.status = "revoked"
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_HISTORY_GRANT_STALE", "retryable": True},
        )
    return export, guild, user


async def history_export_manifest(
    session: AsyncSession,
    export_id: int,
    requester_origin: str,
) -> dict[str, object]:
    export, guild, user = await _active_export(session, export_id, requester_origin)
    grant_channels = list(
        await session.scalars(
            select(GuildHistoryExportChannel)
            .where(GuildHistoryExportChannel.export_id == export.id)
            .order_by(GuildHistoryExportChannel.channel_id)
        )
    )
    visible: list[dict[str, str]] = []
    for grant in grant_channels:
        channel = await session.get(Channel, (grant.channel_id, grant.channel_domain))
        if channel is not None and await history_channel_allowed(session, guild, user, channel):
            visible.append(
                {
                    "id": str(channel.id),
                    "origin_domain": channel.origin_domain,
                    "upper_bound_id": str(grant.upper_bound_id),
                }
            )
    if len(visible) != len(grant_channels):
        export.status = "revoked"
        raise HTTPException(
            status_code=409,
            detail={"code": "KAED_FED_HISTORY_GRANT_STALE", "retryable": True},
        )
    return {
        "export_id": str(export.id),
        "guild_id": str(export.guild_id),
        "guild_domain": export.guild_domain,
        "requester_user": {
            "id": str(export.requester_user_id),
            "domain": export.requester_user_domain,
        },
        "baseline_seq": str(export.baseline_seq),
        "requester_member_version": str(export.requester_member_version),
        "permission_generation": str(export.permission_generation),
        "history_policy_generation": str(export.history_policy_generation),
        "expires_at": export.expires_at.isoformat(),
        "channels": visible,
    }


async def history_export_page(
    session: AsyncSession,
    settings: Settings,
    export_id: int,
    requester_origin: str,
    channel_id: int,
    after: int = 0,
    *,
    before: int | None = None,
) -> dict[str, object]:
    export, guild, user = await _active_export(session, export_id, requester_origin)
    grant = await session.get(
        GuildHistoryExportChannel,
        (export.id, channel_id, guild.origin_domain),
    )
    channel = await session.get(Channel, (channel_id, guild.origin_domain))
    if grant is None or channel is None:
        raise HTTPException(status_code=404, detail={"code": "KAED_FED_HISTORY_NOT_FOUND"})
    if not await history_channel_allowed(session, guild, user, channel):
        export.status = "revoked"
        raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_REVOKED"})
    if before is not None and after != 0:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HISTORY_CURSOR_INVALID"})
    if after < 0 or after > grant.upper_bound_id:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HISTORY_CURSOR_INVALID"})
    if before is not None and (before < 0 or before > grant.upper_bound_id):
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HISTORY_CURSOR_INVALID"})
    limit = settings.federation_history_page_messages
    recent_first = before is not None
    cursor = before
    message_filter = Message.id <= int(cursor or 0) if recent_first else Message.id > after
    messages = list(
        await session.scalars(
            select(Message)
            .where(
                Message.channel_id == channel.id,
                Message.channel_domain == channel.origin_domain,
                message_filter,
                Message.id <= grant.upper_bound_id,
                Message.deleted_at.is_(None),
            )
            .order_by(Message.id.desc() if recent_first else Message.id)
            .limit(limit + 1)
        )
    )
    candidates = messages[: limit + 1]
    message_refs = [(message.id, message.origin_domain) for message in candidates]
    author_refs = {(message.author_id, message.author_domain) for message in candidates}
    authors = {
        (author.id, author.origin_domain): author
        for author in await session.scalars(
            select(User).where(tuple_(User.id, User.origin_domain).in_(author_refs))
        )
    }
    attachments_by_message: dict[tuple[int, str], list[Attachment]] = {}
    reactions_by_message: dict[tuple[int, str], list[Reaction]] = {}
    pins_by_message: dict[tuple[int, str], Pin] = {}
    if message_refs:
        for attachment in await session.scalars(
            select(Attachment)
            .where(
                tuple_(Attachment.message_id, Attachment.message_domain).in_(message_refs),
                Attachment.deleted_at.is_(None),
            )
            .order_by(Attachment.id)
        ):
            attachments_by_message.setdefault(
                (int(attachment.message_id or 0), str(attachment.message_domain)), []
            ).append(attachment)
        for reaction in await session.scalars(
            select(Reaction).where(
                tuple_(Reaction.message_id, Reaction.message_domain).in_(message_refs)
            )
        ):
            reactions_by_message.setdefault(
                (reaction.message_id, reaction.message_domain), []
            ).append(reaction)
        for pin in await session.scalars(
            select(Pin).where(tuple_(Pin.message_id, Pin.message_domain).in_(message_refs))
        ):
            pins_by_message[(pin.message_id, pin.message_domain)] = pin
    rendered: list[dict[str, object]] = []
    rendered_bytes = 0
    for message in candidates[:limit]:
        message_ref = (message.id, message.origin_domain)
        author = authors.get((message.author_id, message.author_domain))
        if author is None:
            raise RuntimeError("historical message author disappeared")
        payload = message_payload(
            message,
            author,
            attachments_by_message.get(message_ref, []),
        )
        payload["history_author"] = user_payload(author)
        reactions = reactions_by_message.get(message_ref, [])
        selected_pin = pins_by_message.get(message_ref)
        payload["reactions"] = [
            {
                "user_id": str(reaction.user_id),
                "user_domain": reaction.user_domain,
                "emoji": reaction.emoji_key,
                "created_at": reaction.created_at.isoformat(),
            }
            for reaction in reactions
        ]
        payload["pin"] = (
            {
                "pinned_by_id": str(selected_pin.pinned_by_id),
                "pinned_by_domain": selected_pin.pinned_by_domain,
                "pinned_at": selected_pin.pinned_at.isoformat(),
            }
            if selected_pin is not None
            else None
        )
        payload_bytes = len(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        )
        if rendered and rendered_bytes + payload_bytes > settings.federation_history_page_bytes:
            break
        if payload_bytes > settings.federation_history_page_bytes:
            raise HTTPException(
                status_code=413,
                detail={"code": "KAED_FED_HISTORY_MESSAGE_TOO_LARGE"},
            )
        rendered.append(payload)
        rendered_bytes += payload_bytes
    has_more = len(rendered) < len(candidates)
    next_after = (
        database_snowflake(rendered[-1]["id"], "history next cursor")
        if has_more and rendered and not recent_first
        else None
    )
    last_rendered = (
        database_snowflake(rendered[-1]["id"], "history next cursor")
        if has_more and rendered and recent_first
        else None
    )
    next_before = max(0, last_rendered - 1) if last_rendered is not None else None
    return {
        "export_id": str(export.id),
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
        "upper_bound_id": str(grant.upper_bound_id),
        "messages": rendered,
        "page_bytes": rendered_bytes,
        "next_after": str(next_after) if next_after is not None else None,
        "next_before": str(next_before) if next_before is not None else None,
        "order": "recent_first" if recent_first else "oldest_first",
        "complete": not has_more,
    }


def _event_channel_id(envelope: dict[str, Any]) -> int | None:
    context = envelope.get("context")
    if not isinstance(context, dict) or context.get("channel_id") is None:
        return None
    try:
        return database_snowflake(context["channel_id"], "history event channel")
    except ValueError:
        return None


async def history_export_delta(
    session: AsyncSession,
    export_id: int,
    requester_origin: str,
    after_seq: int,
) -> dict[str, object]:
    export, guild, user = await _active_export(session, export_id, requester_origin)
    if after_seq < export.baseline_seq:
        raise HTTPException(status_code=400, detail={"code": "KAED_FED_HISTORY_CURSOR_INVALID"})
    grants = list(
        await session.scalars(
            select(GuildHistoryExportChannel).where(
                GuildHistoryExportChannel.export_id == export.id
            )
        )
    )
    allowed_ids: set[int] = set()
    for grant in grants:
        channel = await session.get(Channel, (grant.channel_id, grant.channel_domain))
        if channel is None or not await history_channel_allowed(session, guild, user, channel):
            export.status = "revoked"
            raise HTTPException(status_code=410, detail={"code": "KAED_FED_HISTORY_REVOKED"})
        allowed_ids.add(channel.id)
    latest_seq = guild.next_event_seq - 1
    rows = list(
        await session.scalars(
            select(GuildEvent)
            .where(
                GuildEvent.guild_id == guild.id,
                GuildEvent.guild_domain == guild.origin_domain,
                GuildEvent.seq > after_seq,
                GuildEvent.seq <= latest_seq,
            )
            .order_by(GuildEvent.seq)
            .limit(500)
        )
    )
    events: list[dict[str, Any]] = []
    for row in rows:
        event_type = str(row.envelope.get("type", ""))
        channel_ref = _event_channel_id(row.envelope)
        if event_type in HISTORY_EVENT_TYPES and (
            channel_ref in allowed_ids or event_type == "guild.message.purge"
        ):
            events.append(row.envelope)
    cursor_seq = rows[-1].seq if rows else latest_seq
    return {
        "events": events,
        "cursor_seq": str(cursor_seq),
        "latest_seq": str(latest_seq),
        "complete": cursor_seq >= latest_seq,
    }


async def complete_history_export(
    session: AsyncSession,
    export_id: int,
    requester_origin: str,
) -> None:
    export, _guild, _user = await _active_export(session, export_id, requester_origin)
    export.status = "completed"
    export.completed_at = datetime.now(UTC)


async def revoke_history_exports(
    session: AsyncSession,
    guild: Guild,
    *,
    requester_origin: str | None = None,
) -> list[GuildHistoryExport]:
    conditions = [
        GuildHistoryExport.guild_id == guild.id,
        GuildHistoryExport.guild_domain == guild.origin_domain,
        GuildHistoryExport.status == "active",
    ]
    if requester_origin is not None:
        conditions.append(GuildHistoryExport.requester_origin == requester_origin)
    exports = list(
        await session.scalars(select(GuildHistoryExport).where(*conditions).with_for_update())
    )
    for export in exports:
        export.status = "revoked"
    return exports


async def purge_ineligible_federated_history(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
) -> int:
    """Best-effort removal of imported history no local member may still read.

    The guild home remains authoritative for policy. A cooperative replica
    re-evaluates every imported channel against its current replicated policy
    and memberships. Content that also arrived through the live event stream is
    deliberately not tagged as historical and is therefore not removed here.
    """

    if guild.origin_domain == settings.domain:
        return 0
    members = list(
        await session.execute(
            select(GuildMember, User)
            .join(
                User,
                (User.id == GuildMember.user_id) & (User.origin_domain == GuildMember.user_domain),
            )
            .where(
                GuildMember.guild_id == guild.id,
                GuildMember.guild_domain == guild.origin_domain,
                GuildMember.user_domain == settings.domain,
            )
        )
    )
    channels = list(
        await session.scalars(
            select(Channel).where(
                Channel.guild_id == guild.id,
                Channel.guild_domain == guild.origin_domain,
            )
        )
    )
    retained_channels: set[tuple[int, str]] = set()
    for channel in channels:
        for _membership, user in members:
            if await history_channel_allowed(session, guild, user, channel):
                retained_channels.add((channel.id, channel.origin_domain))
                break

    purged_message = aliased(Message)
    purged_refs_query = (
        select(purged_message.id, purged_message.origin_domain)
        .join(
            FederatedHistoryMessage,
            (FederatedHistoryMessage.message_id == purged_message.id)
            & (FederatedHistoryMessage.message_domain == purged_message.origin_domain),
        )
        .join(
            Channel,
            (Channel.id == purged_message.channel_id)
            & (Channel.origin_domain == purged_message.channel_domain),
        )
        .where(
            Channel.guild_id == guild.id,
            Channel.guild_domain == guild.origin_domain,
            tuple_(purged_message.channel_id, purged_message.channel_domain).not_in(
                retained_channels
            ),
        )
    )
    affected_channel_refs = list(
        (
            await session.execute(
                select(
                    purged_message.channel_id,
                    purged_message.channel_domain,
                )
                .join(
                    FederatedHistoryMessage,
                    (FederatedHistoryMessage.message_id == purged_message.id)
                    & (FederatedHistoryMessage.message_domain == purged_message.origin_domain),
                )
                .join(
                    Channel,
                    (Channel.id == purged_message.channel_id)
                    & (Channel.origin_domain == purged_message.channel_domain),
                )
                .where(
                    Channel.guild_id == guild.id,
                    Channel.guild_domain == guild.origin_domain,
                    tuple_(
                        purged_message.channel_id,
                        purged_message.channel_domain,
                    ).not_in(retained_channels),
                )
                .distinct()
            )
        ).tuples()
    )
    removed = 0
    if affected_channel_refs:
        await session.execute(
            update(Message)
            .where(
                tuple_(Message.referenced_message_id, Message.referenced_message_domain).in_(
                    purged_refs_query
                )
            )
            .values(referenced_message_id=None, referenced_message_domain=None)
        )
        await session.execute(
            update(ReadState)
            .where(
                tuple_(ReadState.last_message_id, ReadState.last_message_domain).in_(
                    purged_refs_query
                )
            )
            .values(last_message_id=None, last_message_domain=None)
        )
        for channel_id, channel_domain in affected_channel_refs:
            affected_channel = await session.get(Channel, (channel_id, channel_domain))
            if (
                affected_channel is None
                or affected_channel.last_message_id is None
                or affected_channel.last_message_domain is None
            ):
                continue
            cursor_is_imported = await session.get(
                FederatedHistoryMessage,
                (
                    affected_channel.last_message_id,
                    affected_channel.last_message_domain,
                ),
            )
            if cursor_is_imported is None:
                continue
            replacement = (
                await session.execute(
                    select(Message.id, Message.origin_domain)
                    .where(
                        Message.channel_id == channel_id,
                        Message.channel_domain == channel_domain,
                        Message.deleted_at.is_(None),
                        ~exists(
                            select(FederatedHistoryMessage.message_id).where(
                                FederatedHistoryMessage.message_id == Message.id,
                                FederatedHistoryMessage.message_domain == Message.origin_domain,
                            )
                        ),
                    )
                    .order_by(Message.id.desc(), Message.origin_domain.desc())
                    .limit(1)
                )
            ).one_or_none()
            affected_channel.last_message_id = replacement[0] if replacement is not None else None
            affected_channel.last_message_domain = (
                replacement[1] if replacement is not None else None
            )
        await session.flush()
        deleted = await session.execute(
            delete(Message).where(tuple_(Message.id, Message.origin_domain).in_(purged_refs_query))
        )
        removed = int(deleted.rowcount or 0)  # type: ignore[attr-defined]

    imports = list(
        await session.scalars(
            select(GuildHistoryImport).where(
                GuildHistoryImport.guild_id == guild.id,
                GuildHistoryImport.guild_domain == guild.origin_domain,
                GuildHistoryImport.status.in_(
                    ("pending", "downloading", "reconciling", "completed")
                ),
            )
        )
    )
    member_versions = {
        (member.user_id, member.user_domain): member.member_version for member, _user in members
    }
    for history_import in imports:
        current_member_version = member_versions.get(
            (history_import.requester_user_id, history_import.requester_user_domain)
        )
        if (
            current_member_version != history_import.requester_member_version
            or history_import.permission_generation != guild.permission_generation
            or history_import.history_policy_generation != guild.history_policy_generation
        ):
            history_import.status = "revoked"
            history_import.error = "replicated access or history policy changed"
    return removed


def unresolved_history_username(user_id: int, origin: str) -> str:
    digest = hashlib.sha256(f"{user_id}@{origin}".encode()).hexdigest()[:20]
    return f"history_{digest}"


def _history_ref(raw: object, field: str) -> tuple[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} reference is invalid")
    domain = raw.get("origin_domain", raw.get("domain"))
    return (
        database_snowflake(raw.get("id"), f"{field} id"),
        normalize_domain(str(domain or "")),
    )


async def _ensure_history_identity(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    origin: str,
    *,
    profile: RemoteUserProfile | None = None,
    authority_origin: str,
) -> User:
    existing = await session.get(User, (user_id, origin))
    if profile is not None:
        if (int(profile.id), profile.origin_domain) != (user_id, origin):
            raise ValueError("historical author profile identity is invalid")
        try:
            return await resolve_delegated_profile(
                session,
                settings,
                profile,
                authority_origin=authority_origin,
            )
        except ValueError:
            if existing is not None:
                raise
    if existing is not None:
        return existing
    if origin == settings.domain:
        raise ValueError("historical data references an unknown local user")
    await session.execute(
        pg_insert(Instance)
        .values(domain=origin, is_self=False, display_name=origin, software_version="unresolved")
        .on_conflict_do_nothing(index_elements=["domain"])
    )
    await session.execute(
        pg_insert(User)
        .values(
            id=user_id,
            origin_domain=origin,
            is_local=False,
            username=unresolved_history_username(user_id, origin),
            profile_version=1,
            profile_resolved=False,
        )
        .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
    )
    placeholder = await session.get(User, (user_id, origin))
    if placeholder is None:
        raise RuntimeError("historical identity insert did not converge")
    return placeholder


def _validate_manifest(
    payload: object,
    guild: Guild,
    user: User,
) -> tuple[int, int, int, int, int, list[tuple[int, int]]]:
    if not isinstance(payload, dict) or payload.get("available") is False:
        raise ValueError("history export is unavailable")
    export_id = database_snowflake(payload.get("export_id"), "history export id")
    if (
        database_snowflake(payload.get("guild_id"), "history guild id"),
        normalize_domain(str(payload.get("guild_domain", ""))),
    ) != (guild.id, guild.origin_domain):
        raise ValueError("history export references the wrong guild")
    requester = _history_ref(payload.get("requester_user"), "history requester")
    if requester != (user.id, user.origin_domain):
        raise ValueError("history export references the wrong user")
    baseline_seq = database_snowflake(payload.get("baseline_seq"), "history baseline")
    member_version = database_snowflake(
        payload.get("requester_member_version"), "history member version"
    )
    permission_generation = database_snowflake(
        payload.get("permission_generation"), "history permission generation"
    )
    policy_generation = database_snowflake(
        payload.get("history_policy_generation"), "history policy generation"
    )
    raw_channels = payload.get("channels")
    if not isinstance(raw_channels, list) or len(raw_channels) > 10_000:
        raise ValueError("history export channel set is invalid")
    channels: list[tuple[int, int]] = []
    seen: set[int] = set()
    for raw in raw_channels:
        channel_ref = _history_ref(raw, "history channel")
        upper = database_snowflake(
            raw.get("upper_bound_id") if isinstance(raw, dict) else None,
            "history upper bound",
        )
        if channel_ref[1] != guild.origin_domain or channel_ref[0] in seen:
            raise ValueError("history export contains an invalid channel")
        seen.add(channel_ref[0])
        channels.append((channel_ref[0], upper))
    return (
        export_id,
        baseline_seq,
        member_version,
        permission_generation,
        policy_generation,
        channels,
    )


async def validate_manifest_against_replica(
    session: AsyncSession,
    guild: Guild,
    user: User,
    *,
    member_version: int,
    permission_generation: int,
    policy_generation: int,
    channels: list[tuple[int, int]],
) -> None:
    membership = await session.get(
        GuildMember,
        (guild.id, guild.origin_domain, user.id, user.origin_domain),
    )
    if membership is None or membership.member_version != member_version:
        raise ValueError("history grant does not match the replicated membership")
    if (
        guild.permission_generation != permission_generation
        or guild.history_policy_generation != policy_generation
    ):
        raise ValueError("history grant does not match the replicated policy generation")
    for channel_id, _upper_bound in channels:
        channel = await session.get(Channel, (channel_id, guild.origin_domain))
        if channel is None or not await history_channel_allowed(session, guild, user, channel):
            raise ValueError("history grant includes a locally ineligible channel")


def _validate_history_message(
    raw: object,
    *,
    guild_origin: str,
    channel_id: int,
    after: int,
    upper_bound: int,
    before: int | None = None,
    previous_id: int | None = None,
) -> tuple[int, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise ValueError("historical message is invalid")
    message_id = database_snowflake(raw.get("id"), "historical message id")
    in_range = (
        0 <= message_id <= min(before, upper_bound)
        if before is not None
        else after < message_id <= upper_bound
    )
    if not in_range or (previous_id is not None and message_id >= previous_id):
        raise ValueError("historical message is outside its granted range")
    if normalize_domain(str(raw.get("origin_domain", ""))) != guild_origin:
        raise ValueError("historical message has a non-authoritative origin")
    if (
        database_snowflake(raw.get("channel_id"), "historical message channel"),
        normalize_domain(str(raw.get("channel_domain", ""))),
    ) != (channel_id, guild_origin):
        raise ValueError("historical message references the wrong channel")
    if raw.get("deleted_at") is not None:
        raise ValueError("history export included deleted content")
    content = raw.get("content")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    attachments = raw.get("attachments", [])
    reactions = raw.get("reactions", [])
    pin = raw.get("pin")
    mentions = raw.get("mention_user_refs", [])
    webhook = raw.get("webhook")
    if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4000):
        raise ValueError("historical message content is invalid")
    if not isinstance(attachments, list) or len(attachments) > 10:
        raise ValueError("historical message attachments are invalid")
    if not isinstance(reactions, list) or len(reactions) > 10_000:
        raise ValueError("historical message reactions are invalid")
    if not isinstance(mentions, list) or len(mentions) > 5_000:
        raise ValueError("historical message mentions are invalid")
    if pin is not None and not isinstance(pin, dict):
        raise ValueError("historical message pin is invalid")
    if webhook is not None and not isinstance(webhook, dict):
        raise ValueError("historical message webhook is invalid")
    if content is not None and e2ee is not None:
        raise ValueError("historical message mixes plaintext and encrypted content")
    if content is None and e2ee is None and not attachments:
        raise ValueError("historical message has no content")
    profile = RemoteUserProfile.model_validate(raw.get("history_author"))
    if (
        database_snowflake(raw.get("author_id"), "historical author id"),
        normalize_domain(str(raw.get("author_domain", ""))),
    ) != (int(profile.id), profile.origin_domain):
        raise ValueError("historical author profile does not match the message")
    try:
        created_at = datetime.fromisoformat(str(raw.get("created_at")))
        edited_at = (
            datetime.fromisoformat(str(raw["edited_at"]))
            if raw.get("edited_at") is not None
            else None
        )
    except ValueError:
        raise ValueError("historical message timestamp is invalid") from None
    if created_at.tzinfo is None or (edited_at is not None and edited_at.tzinfo is None):
        raise ValueError("historical message timestamp lacks a timezone")
    message_type = raw.get("message_type", 0)
    flags = raw.get("flags", 0)
    if (
        isinstance(message_type, bool)
        or not isinstance(message_type, int)
        or message_type < 0
        or isinstance(flags, bool)
        or not isinstance(flags, int)
        or flags < 0
    ):
        raise ValueError("historical message flags are invalid")
    for reaction in reactions:
        if not isinstance(reaction, dict):
            raise ValueError("historical reaction is invalid")
        database_snowflake(reaction.get("user_id"), "historical reaction user")
        normalize_domain(str(reaction.get("user_domain", "")))
        emoji = reaction.get("emoji")
        if not isinstance(emoji, str) or not 1 <= len(emoji) <= 320:
            raise ValueError("historical reaction emoji is invalid")
        try:
            reaction_created = datetime.fromisoformat(str(reaction.get("created_at")))
        except ValueError:
            raise ValueError("historical reaction timestamp is invalid") from None
        if reaction_created.tzinfo is None:
            raise ValueError("historical reaction timestamp lacks a timezone")
    if isinstance(pin, dict):
        database_snowflake(pin.get("pinned_by_id"), "historical pinner")
        normalize_domain(str(pin.get("pinned_by_domain", "")))
        try:
            pinned_at = datetime.fromisoformat(str(pin.get("pinned_at")))
        except ValueError:
            raise ValueError("historical pin timestamp is invalid") from None
        if pinned_at.tzinfo is None:
            raise ValueError("historical pin timestamp lacks a timezone")
    return message_id, dict(raw)


async def _stage_history_pages(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
    channels: list[tuple[int, int]],
    *,
    recent_first: bool,
    deadline: float,
) -> int:
    for channel_id, upper_bound in channels:
        channel = await session.get(Channel, (channel_id, guild.origin_domain))
        if channel is None or channel.unavailable:
            raise ValueError("history export references an unavailable local replica channel")
        channel_state = await session.get(
            GuildHistoryImportChannel,
            (
                history_import.export_id,
                history_import.export_domain,
                channel_id,
                guild.origin_domain,
            ),
        )
        if channel_state is None:
            channel_state = GuildHistoryImportChannel(
                export_id=history_import.export_id,
                export_domain=history_import.export_domain,
                channel_id=channel_id,
                channel_domain=guild.origin_domain,
                upper_bound_id=upper_bound,
                next_before_id=upper_bound,
            )
            session.add(channel_state)
            await session.commit()
        elif channel_state.upper_bound_id != upper_bound:
            raise ValueError("historical channel grant changed while resuming")
        while not channel_state.complete:
            if time.monotonic() >= deadline:
                raise RuntimeError("historical import exceeded its duration budget")
            if history_import.pages_downloaded >= settings.federation_history_max_pages:
                raise RuntimeError("historical import exceeded its page budget")
            after = 0
            query: dict[str, str]
            if recent_first:
                query = {"before": str(channel_state.next_before_id)}
            else:
                after = int(
                    await session.scalar(
                        select(func.max(GuildHistoryStagedMessage.message_id)).where(
                            GuildHistoryStagedMessage.export_id == history_import.export_id,
                            GuildHistoryStagedMessage.export_domain == history_import.export_domain,
                            GuildHistoryStagedMessage.channel_id == channel_id,
                        )
                    )
                    or 0
                )
                query = {"after": str(after)}
            response = await signed_request(
                session,
                settings,
                "GET",
                guild.origin_domain,
                (
                    f"/_kaede/v1/guilds/{guild.id}/history-exports/"
                    f"{history_import.export_id}/channels/{channel_id}"
                ),
                query=query,
                request_timeout=30,
            )
            if response.status_code != 200:
                raise RuntimeError("historical message page request failed")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("historical message page is invalid")
            raw_messages = payload.get("messages")
            if (
                not isinstance(raw_messages, list)
                or len(raw_messages) > settings.federation_history_page_messages
            ):
                raise ValueError("historical message page exceeds its bound")
            response_bytes = len(response.content)
            if response_bytes > settings.federation_history_page_bytes + 64 * 1024:
                raise ValueError("historical message response exceeds its byte bound")
            last = after
            previous_id: int | None = None
            reactions = 0
            for raw in raw_messages:
                message_id, message_payload = _validate_history_message(
                    raw,
                    guild_origin=guild.origin_domain,
                    channel_id=channel_id,
                    after=last,
                    upper_bound=upper_bound,
                    before=channel_state.next_before_id if recent_first else None,
                    previous_id=previous_id if recent_first else None,
                )
                await session.execute(
                    pg_insert(GuildHistoryStagedMessage)
                    .values(
                        export_id=history_import.export_id,
                        export_domain=history_import.export_domain,
                        message_id=message_id,
                        message_domain=guild.origin_domain,
                        channel_id=channel_id,
                        channel_domain=guild.origin_domain,
                        payload=message_payload,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "export_id",
                            "export_domain",
                            "message_id",
                            "message_domain",
                        ],
                        set_={"payload": message_payload},
                    )
                )
                if recent_first:
                    previous_id = message_id
                else:
                    last = message_id
                reactions += len(message_payload.get("reactions", []))
            next_messages = history_import.messages_downloaded + len(raw_messages)
            next_reactions = history_import.reactions_downloaded + reactions
            next_bytes = history_import.bytes_downloaded + response_bytes
            if next_messages > settings.federation_history_max_messages:
                raise RuntimeError("historical import exceeded its message budget")
            if next_reactions > settings.federation_history_max_reactions:
                raise RuntimeError("historical import exceeded its reaction budget")
            if next_bytes > settings.federation_history_max_bytes:
                raise RuntimeError("historical import exceeded its byte budget")
            history_import.pages_downloaded += 1
            history_import.messages_downloaded = next_messages
            history_import.reactions_downloaded = next_reactions
            history_import.bytes_downloaded = next_bytes
            channel_state.pages_downloaded += 1
            channel_state.messages_downloaded += len(raw_messages)
            channel_state.bytes_downloaded += response_bytes
            complete = payload.get("complete")
            if complete is True:
                channel_state.complete = True
                await session.commit()
                continue
            cursor_name = "next_before" if recent_first else "next_after"
            next_cursor = payload.get(cursor_name)
            if not raw_messages or next_cursor is None:
                raise ValueError("historical message cursor did not advance")
            parsed_next = database_snowflake(next_cursor, "historical message cursor")
            if recent_first:
                expected = max(0, int(previous_id or 0) - 1)
                if parsed_next != expected or parsed_next >= channel_state.next_before_id:
                    raise ValueError("historical recent-first cursor is invalid")
                channel_state.next_before_id = parsed_next
            elif parsed_next != last or parsed_next <= after:
                raise ValueError("historical message cursor is invalid")
            await session.commit()
    return history_import.messages_downloaded


async def _apply_history_delta_event(
    session: AsyncSession,
    history_import: GuildHistoryImport,
    event: dict[str, Any],
) -> None:
    event_type = str(event.get("type", ""))
    if event_type not in HISTORY_EVENT_TYPES:
        return
    content = event.get("content")
    if not isinstance(content, dict):
        raise ValueError("history delta content is invalid")
    if event_type in {"guild.message.update", "guild.message.delete"}:
        message_ref = _history_ref(content.get("message"), "history delta message")
        staged = await session.get(
            GuildHistoryStagedMessage,
            (
                history_import.export_id,
                history_import.export_domain,
                message_ref[0],
                message_ref[1],
            ),
        )
        if staged is None:
            return
        if event_type.endswith("delete"):
            await session.delete(staged)
            return
        raw_message = content.get("message")
        if not isinstance(raw_message, dict):
            raise ValueError("history delta message update is invalid")
        staged.payload = {**staged.payload, **raw_message}
        return
    if event_type == "guild.message.purge":
        author_ref = _history_ref(content.get("author"), "history purge author")
        try:
            cutoff = datetime.fromisoformat(str(content.get("created_after")))
        except ValueError:
            raise ValueError("history purge cutoff is invalid") from None
        staged_rows = list(
            await session.scalars(
                select(GuildHistoryStagedMessage).where(
                    GuildHistoryStagedMessage.export_id == history_import.export_id,
                    GuildHistoryStagedMessage.export_domain == history_import.export_domain,
                )
            )
        )
        for staged in staged_rows:
            raw = staged.payload
            try:
                created_at = datetime.fromisoformat(str(raw.get("created_at")))
            except ValueError:
                raise ValueError("staged history timestamp is invalid") from None
            if (
                int(raw.get("author_id", -1)),
                str(raw.get("author_domain", "")),
            ) == author_ref and created_at >= cutoff:
                await session.delete(staged)
        return
    message_ref = _history_ref(content.get("message"), "history state message")
    staged = await session.get(
        GuildHistoryStagedMessage,
        (
            history_import.export_id,
            history_import.export_domain,
            message_ref[0],
            message_ref[1],
        ),
    )
    if staged is None:
        return
    raw = dict(staged.payload)
    if event_type.startswith("guild.reaction."):
        user_ref = _history_ref(content.get("user"), "history reaction user")
        emoji = content.get("emoji")
        if not isinstance(emoji, str) or not emoji:
            raise ValueError("history reaction is invalid")
        reactions = list(raw.get("reactions", []))
        key = (str(user_ref[0]), user_ref[1], emoji)
        reactions = [
            item
            for item in reactions
            if (str(item.get("user_id")), str(item.get("user_domain")), item.get("emoji")) != key
        ]
        if event_type.endswith("add"):
            reactions.append(
                {
                    "user_id": str(user_ref[0]),
                    "user_domain": user_ref[1],
                    "emoji": emoji,
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        raw["reactions"] = reactions
    elif event_type.startswith("guild.pin."):
        actor_ref = _history_ref(event.get("actor"), "history pin actor")
        raw["pin"] = (
            {
                "pinned_by_id": str(actor_ref[0]),
                "pinned_by_domain": actor_ref[1],
                "pinned_at": datetime.fromtimestamp(
                    int(event.get("ts", 0)) / 1000,
                    tz=UTC,
                ).isoformat(),
            }
            if event_type.endswith("add")
            else None
        )
    staged.payload = raw


async def _reconcile_history_delta(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
    after_seq: int,
    *,
    commit_pages: bool = True,
) -> int:
    cursor = after_seq
    for _page in range(10_000):
        response = await signed_request(
            session,
            settings,
            "GET",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/history-exports/{history_import.export_id}/delta",
            query={"after_seq": str(cursor)},
            request_timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError("history reconciliation request failed")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise ValueError("history reconciliation response is invalid")
        for raw_event in payload["events"]:
            envelope = await validated_event_envelope(
                session,
                settings,
                guild.origin_domain,
                raw_event,
            )
            await _apply_history_delta_event(
                session,
                history_import,
                envelope.model_dump(mode="json"),
            )
        next_cursor = database_snowflake(payload.get("cursor_seq"), "history delta cursor")
        if next_cursor < cursor:
            raise ValueError("history reconciliation cursor regressed")
        cursor = next_cursor
        if commit_pages:
            await session.commit()
        if payload.get("complete") is True:
            return cursor
    raise RuntimeError("history reconciliation exceeded its page bound")


async def _merge_history_import_batch(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
) -> tuple[int, bool]:
    staged_rows = list(
        await session.scalars(
            select(GuildHistoryStagedMessage)
            .where(
                GuildHistoryStagedMessage.export_id == history_import.export_id,
                GuildHistoryStagedMessage.export_domain == history_import.export_domain,
            )
            .order_by(
                GuildHistoryStagedMessage.channel_id,
                GuildHistoryStagedMessage.message_id,
            )
            .limit(settings.federation_history_merge_chunk_size)
        )
    )
    imported = 0
    latest_by_channel: dict[tuple[int, str], int] = {}
    for staged in staged_rows:
        staged_channel_ref = (staged.channel_id, staged.channel_domain)
        latest_by_channel[staged_channel_ref] = max(
            latest_by_channel.get(staged_channel_ref, 0), staged.message_id
        )
        raw = staged.payload
        webhook = raw.get("webhook")
        profile = RemoteUserProfile.model_validate(raw.get("history_author"))
        author = await _ensure_history_identity(
            session,
            settings,
            int(profile.id),
            profile.origin_domain,
            profile=profile,
            authority_origin=guild.origin_domain,
        )
        referenced_id = (
            database_snowflake(raw.get("referenced_message_id"), "historical reply")
            if raw.get("referenced_message_id") is not None
            else None
        )
        referenced_domain = (
            normalize_domain(str(raw.get("referenced_message_domain", "")))
            if referenced_id is not None
            else None
        )
        if referenced_id is not None:
            referenced = await session.get(Message, (referenced_id, referenced_domain))
            if referenced is None:
                referenced_id = None
                referenced_domain = None
        inserted = await session.scalar(
            pg_insert(Message)
            .values(
                id=staged.message_id,
                origin_domain=staged.message_domain,
                channel_id=staged.channel_id,
                channel_domain=staged.channel_domain,
                author_id=author.id,
                author_domain=author.origin_domain,
                content=raw.get("content"),
                e2ee=validate_e2ee_envelope(raw.get("e2ee")),
                message_type=int(raw.get("message_type", 0)),
                flags=int(raw.get("flags", 0)),
                client_nonce=raw.get("client_nonce"),
                referenced_message_id=referenced_id,
                referenced_message_domain=referenced_domain,
                mention_user_refs=raw.get("mention_user_refs", []),
                webhook_name=webhook.get("name") if isinstance(webhook, dict) else None,
                webhook_avatar_hash=(
                    webhook.get("avatar_hash") if isinstance(webhook, dict) else None
                ),
                edited_at=(
                    datetime.fromisoformat(str(raw["edited_at"]))
                    if raw.get("edited_at") is not None
                    else None
                ),
                created_at=datetime.fromisoformat(str(raw["created_at"])),
            )
            .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
            .returning(Message.id)
        )
        if inserted is None:
            continue
        message = await session.get(Message, (staged.message_id, staged.message_domain))
        if message is None:
            raise RuntimeError("imported historical message disappeared")
        await replicate_message_attachments(
            session,
            settings,
            message,
            author,
            raw.get("attachments", []),
        )
        await session.execute(
            pg_insert(FederatedHistoryMessage)
            .values(
                message_id=message.id,
                message_domain=message.origin_domain,
                export_id=history_import.export_id,
                export_domain=history_import.export_domain,
            )
            .on_conflict_do_nothing(index_elements=["message_id", "message_domain"])
        )
        for reaction in raw.get("reactions", []):
            if not isinstance(reaction, dict):
                continue
            user_id = database_snowflake(reaction.get("user_id"), "historical reaction user")
            user_domain = normalize_domain(str(reaction.get("user_domain", "")))
            await _ensure_history_identity(
                session,
                settings,
                user_id,
                user_domain,
                authority_origin=guild.origin_domain,
            )
            await session.execute(
                pg_insert(Reaction)
                .values(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    user_id=user_id,
                    user_domain=user_domain,
                    emoji_key=str(reaction.get("emoji", ""))[:320],
                    created_at=datetime.fromisoformat(str(reaction["created_at"])),
                )
                .on_conflict_do_nothing()
            )
        pin = raw.get("pin")
        if isinstance(pin, dict):
            pinner_id = database_snowflake(pin.get("pinned_by_id"), "historical pinner")
            pinner_domain = normalize_domain(str(pin.get("pinned_by_domain", "")))
            await _ensure_history_identity(
                session,
                settings,
                pinner_id,
                pinner_domain,
                authority_origin=guild.origin_domain,
            )
            await session.execute(
                pg_insert(Pin)
                .values(
                    channel_id=message.channel_id,
                    channel_domain=message.channel_domain,
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    pinned_by_id=pinner_id,
                    pinned_by_domain=pinner_domain,
                    pinned_at=datetime.fromisoformat(str(pin["pinned_at"])),
                )
                .on_conflict_do_nothing()
            )
        imported += 1
    for (channel_id, channel_domain), message_id in latest_by_channel.items():
        channel = await session.get(Channel, (channel_id, channel_domain))
        if channel is not None:
            await advance_channel_cursor(
                session,
                channel,
                message_id,
                guild.origin_domain,
            )
    if staged_rows:
        await session.execute(
            delete(GuildHistoryStagedMessage).where(
                GuildHistoryStagedMessage.export_id == history_import.export_id,
                GuildHistoryStagedMessage.export_domain == history_import.export_domain,
                tuple_(
                    GuildHistoryStagedMessage.message_id,
                    GuildHistoryStagedMessage.message_domain,
                ).in_([(row.message_id, row.message_domain) for row in staged_rows]),
            )
        )
        await session.commit()
        remaining = await session.scalar(
            select(GuildHistoryStagedMessage.message_id)
            .where(
                GuildHistoryStagedMessage.export_id == history_import.export_id,
                GuildHistoryStagedMessage.export_domain == history_import.export_domain,
            )
            .limit(1)
        )
        if remaining is not None:
            return imported, False
    return imported, True


async def _merge_history_import(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
) -> int:
    imported = 0
    while True:
        batch_imported, complete = await _merge_history_import_batch(
            session,
            settings,
            guild,
            history_import,
        )
        imported += batch_imported
        if complete:
            break
    history_import.status = "completed"
    history_import.completed_at = datetime.now(UTC)
    history_import.error = None
    return imported


async def request_and_import_history(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    user: User,
) -> int:
    if not settings.federation_history_import_enabled or guild.origin_domain == settings.domain:
        return 0
    if not await eligible_history_channels(session, guild, user):
        return 0
    peer = await ensure_peer(session, settings, guild.origin_domain)
    recent_first = HISTORY_RECENT_FIRST_CAPABILITY in peer.capabilities
    if not recent_first and HISTORY_CAPABILITY not in peer.capabilities:
        return 0
    response = await signed_request(
        session,
        settings,
        "POST",
        guild.origin_domain,
        f"/_kaede/v1/guilds/{guild.id}/history-exports",
        payload={"user": {"id": str(user.id), "domain": user.origin_domain}},
        request_timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError("history export request failed")
    raw_manifest = response.json()
    if isinstance(raw_manifest, dict) and raw_manifest.get("available") is False:
        return 0
    (
        export_id,
        baseline_seq,
        member_version,
        permission_generation,
        policy_generation,
        channels,
    ) = _validate_manifest(raw_manifest, guild, user)
    await validate_manifest_against_replica(
        session,
        guild,
        user,
        member_version=member_version,
        permission_generation=permission_generation,
        policy_generation=policy_generation,
        channels=channels,
    )
    grant_conditions = (
        GuildHistoryImport.guild_id == guild.id,
        GuildHistoryImport.guild_domain == guild.origin_domain,
        GuildHistoryImport.requester_user_id == user.id,
        GuildHistoryImport.requester_user_domain == user.origin_domain,
        GuildHistoryImport.requester_member_version == member_version,
        GuildHistoryImport.permission_generation == permission_generation,
        GuildHistoryImport.history_policy_generation == policy_generation,
    )
    await session.execute(
        pg_insert(GuildHistoryImport)
        .values(
            export_id=export_id,
            export_domain=guild.origin_domain,
            guild_id=guild.id,
            guild_domain=guild.origin_domain,
            requester_user_id=user.id,
            requester_user_domain=user.origin_domain,
            requester_member_version=member_version,
            baseline_seq=baseline_seq,
            permission_generation=permission_generation,
            history_policy_generation=policy_generation,
        )
        .on_conflict_do_nothing(constraint="uq_guild_history_imports_grant_generation")
    )
    await session.commit()
    history_import = await session.scalar(select(GuildHistoryImport).where(*grant_conditions))
    if history_import is None:
        raise RuntimeError("history import state could not be claimed")
    lease_owner = secrets.token_hex(16)
    now = datetime.now(UTC)
    claimed = await session.scalar(
        update(GuildHistoryImport)
        .where(
            GuildHistoryImport.export_id == history_import.export_id,
            GuildHistoryImport.export_domain == history_import.export_domain,
            (
                GuildHistoryImport.lease_expires_at.is_(None)
                | (GuildHistoryImport.lease_expires_at < now)
            ),
        )
        .values(
            lease_owner=lease_owner,
            lease_expires_at=now
            + timedelta(seconds=settings.federation_history_max_duration_seconds + 60),
        )
        .returning(GuildHistoryImport.export_id)
    )
    await session.commit()
    if claimed is None:
        return 0
    history_import = await session.get(
        GuildHistoryImport,
        (history_import.export_id, history_import.export_domain),
    )
    if history_import is None:
        raise RuntimeError("claimed history import disappeared")
    deadline = time.monotonic() + settings.federation_history_max_duration_seconds
    try:
        if (
            history_import.status == "completed"
            and history_import.remote_acknowledged_at is not None
        ):
            return 0
        if history_import.status != "completed":
            history_import.status = "downloading"
            history_import.error = None
            await session.commit()
            await _stage_history_pages(
                session,
                settings,
                guild,
                history_import,
                channels,
                recent_first=recent_first,
                deadline=deadline,
            )
        else:
            imported = 0
        if history_import.status != "completed":
            history_import.status = "reconciling"
            await session.commit()
            cursor = await _reconcile_history_delta(
                session,
                settings,
                guild,
                history_import,
                baseline_seq,
            )
            await _reconcile_history_delta(
                session,
                settings,
                guild,
                history_import,
                cursor,
            )
            current_guild = await session.scalar(
                select(Guild)
                .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
                .execution_options(populate_existing=True)
            )
            current_member = await session.scalar(
                select(GuildMember)
                .where(
                    GuildMember.guild_id == guild.id,
                    GuildMember.guild_domain == guild.origin_domain,
                    GuildMember.user_id == user.id,
                    GuildMember.user_domain == user.origin_domain,
                )
                .execution_options(populate_existing=True)
            )
            if (
                current_guild is None
                or current_member is None
                or current_guild.permission_generation != permission_generation
                or current_guild.history_policy_generation != policy_generation
                or current_member.member_version != member_version
            ):
                raise RuntimeError("history grant changed during finalization")
            imported = await _merge_history_import(
                session,
                settings,
                current_guild,
                history_import,
            )
            await session.commit()
        completed = await signed_request(
            session,
            settings,
            "POST",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/history-exports/{history_import.export_id}/complete",
            payload={},
        )
        if completed.status_code != 204:
            history_import.ack_error = "history export completion acknowledgement failed"
            await session.commit()
            raise RuntimeError("history export completion acknowledgement failed")
        history_import.remote_acknowledged_at = datetime.now(UTC)
        history_import.ack_error = None
        await session.commit()
        return imported
    except Exception as exc:
        await session.rollback()
        failed = await session.get(
            GuildHistoryImport,
            (history_import.export_id, history_import.export_domain),
        )
        if failed is not None and failed.status != "completed":
            failed.status = "failed"
            failed.error = str(exc)[:500]
            await session.commit()
        raise
    finally:
        await session.rollback()
        await session.execute(
            update(GuildHistoryImport)
            .where(
                GuildHistoryImport.export_id == history_import.export_id,
                GuildHistoryImport.export_domain == history_import.export_domain,
                GuildHistoryImport.lease_owner == lease_owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
        )
        await session.commit()
