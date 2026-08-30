from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import HTTPException
from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.chat.custom_emojis import canonical_reaction_emoji
from app.chat.e2ee import (
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_message_encryption_policy,
)
from app.chat.message_flags import MESSAGE_FLAG_IS_CROSSPOST
from app.chat.message_references import (
    validate_channel_follow_message_fields,
    validate_message_reference_projection,
)
from app.chat.payloads import message_payload, render_poll_payload
from app.chat.permissions import calculate_permissions
from app.chat.poll_results import (
    POLL_RESULT_MESSAGE_TYPE,
    validate_poll_result_wire_body,
)
from app.core.channel_types import GUILD_MESSAGE_CHANNEL_TYPES, is_message_capable_channel_type
from app.core.permissions import Permission
from app.core.settings import Settings
from app.core.snowflake import SnowflakeGenerator
from app.db.models import (
    Attachment,
    Channel,
    E2EEControlRecord,
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
    MediaTombstoneSource,
    Message,
    MessageView,
    Pin,
    Poll,
    PollAnswer,
    PollVote,
    Reaction,
    ReadState,
    TerminalRoomDeletion,
    User,
)
from app.federation.client import signed_request
from app.federation.events import attachment_refs_from_payloads
from app.federation.guilds import _validated_message_rich_projection
from app.federation.identity_storage import admit_remote_user_identity
from app.federation.message_content import validate_webhook_attribution
from app.federation.network import (
    FederationNetworkError,
    decode_federation_response_json,
    ensure_peer,
    normalize_domain,
)
from app.federation.replica_storage import (
    FederationReplicaQuotaExceeded,
    admit_replica_storage,
    mark_replica_quota_paused,
    reconcile_replica_storage,
)
from app.federation.replication import (
    advance_channel_cursor,
    database_snowflake,
    insert_unresolved_remote_user,
    profile_from_user,
    remote_media_dimensions,
    replicate_message_attachments,
    resolve_delegated_profile,
    sanitized_remote_blurhash,
    sanitized_remote_variants,
    unresolved_remote_username,
    validate_snowflake_timestamp,
)
from app.federation.schemas import RemoteUserProfile
from app.federation.security import validated_event_envelope
from app.federation.terminal_rooms import lock_terminal_room
from app.media.processing import normalize_declared_type, sanitize_filename
from app.media.schemas import validate_voice_attachment_metadata
from app.media.tombstones import (
    lock_media_tombstone_ref,
    queue_terminal_attachment_tombstone,
    terminal_attachment_refs_for_messages,
)

HISTORY_CAPABILITY = "guild-history-sync/1"
HISTORY_RECENT_FIRST_CAPABILITY = "guild-history-sync/2"
HISTORY_EVENT_TYPES = frozenset(
    {
        "guild.message.update",
        "guild.message.delete",
        "guild.message.bulk_delete",
        "guild.message.purge",
        "guild.reaction.add",
        "guild.reaction.remove",
        "guild.reaction.clear",
        "guild.poll.vote.add",
        "guild.poll.vote.remove",
        "guild.poll.finalize",
        "guild.pin.add",
        "guild.pin.remove",
    }
)

ABANDONED_IMPORT_RETENTION = timedelta(days=7)
HISTORY_DELTA_EVENTS_PER_PAGE = 500
HISTORY_DELTA_MAX_REQUESTS = 1_000
HISTORY_TIMESTAMP_TOLERANCE_MS = 60_000
ACTIVE_HISTORY_EXPORT_STATUSES = ("active", "completed")

HISTORY_CAPACITY_CODE = "KAED_FED_HISTORY_CAPACITY"
HISTORY_TEMPORARILY_UNAVAILABLE_CODE = "FEDERATED_GUILD_HISTORY_TEMPORARILY_UNAVAILABLE"
HISTORY_LIMIT_REACHED_CODE = "FEDERATED_GUILD_HISTORY_LIMIT_REACHED"
HISTORY_REJECTED_CODE = "FEDERATED_GUILD_HISTORY_REJECTED"
HISTORY_LIMIT_RESOURCES = frozenset(
    {"pages", "messages", "bytes", "reactions", "duration", "delta_requests"}
)


class FederatedHistorySyncError(RuntimeError):
    """A safe, stable failure that the history worker may expose to a user.

    Federation response bodies and ordinary exception strings are untrusted
    operator data.  Keeping the client-facing code and retry metadata on a
    typed exception prevents the worker from leaking either while still
    preserving the authority's bounded Retry-After guidance.
    """

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        retry_after_ms: int | None = None,
        resource: str | None = None,
    ) -> None:
        self.code = code
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.resource = resource if resource in HISTORY_LIMIT_RESOURCES else None
        super().__init__(code)

    def dispatch_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "code": self.code,
            "retryable": self.retryable,
        }
        if self.retry_after_ms is not None:
            payload["retry_after_ms"] = self.retry_after_ms
        if self.resource is not None:
            payload["resource"] = self.resource
        return payload


class FederatedHistoryLimitExceeded(FederatedHistorySyncError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            HISTORY_LIMIT_REACHED_CODE,
            retryable=False,
            resource=resource,
        )


async def _lock_live_history_import(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
) -> tuple[Guild, GuildHistoryImport]:
    """Fence a post-network history write against terminal guild deletion."""

    await lock_terminal_room(session, "guild", guild.id, guild.origin_domain)
    terminal_receipt = await session.get(
        TerminalRoomDeletion,
        ("guild", guild.id, guild.origin_domain, settings.domain),
    )
    live_guild = await session.get(
        Guild,
        (guild.id, guild.origin_domain),
        populate_existing=True,
    )
    live_import = await session.get(
        GuildHistoryImport,
        (history_import.export_id, history_import.export_domain),
        populate_existing=True,
    )
    if (
        terminal_receipt is not None
        or live_guild is None
        or live_guild.unavailable
        or live_import is None
        or live_import.status in {"revoked", "failed"}
    ):
        raise RuntimeError("history import room is no longer available")
    current_member = await session.get(
        GuildMember,
        (
            live_guild.id,
            live_guild.origin_domain,
            live_import.requester_user_id,
            live_import.requester_user_domain,
        ),
        populate_existing=True,
    )
    if (
        current_member is None
        or current_member.member_version != live_import.requester_member_version
        or live_guild.permission_generation != live_import.permission_generation
        or live_guild.history_policy_generation != live_import.history_policy_generation
    ):
        raise RuntimeError("history grant changed while a remote page was in flight")
    return live_guild, live_import


def _history_response_detail(response: Any) -> dict[str, object]:
    try:
        body = decode_federation_response_json(response)
    except Exception:
        return {}
    if isinstance(body, dict):
        detail = body.get("detail", body)
        if isinstance(detail, dict):
            return detail
    return {}


def _retry_after_ms(
    response: Any,
    fallback_ms: int,
    *,
    detail: dict[str, object] | None = None,
) -> int:
    """Read a bounded Retry-After value from a signed peer response."""

    candidate = (detail or _history_response_detail(response)).get("retry_after_ms")
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return min(86_400_000, max(1_000, candidate))
    raw_header = response.headers.get("Retry-After")
    if isinstance(raw_header, str):
        try:
            seconds = float(raw_header)
        except ValueError:
            pass
        else:
            if seconds >= 0:
                return min(86_400_000, max(1_000, int(seconds * 1_000)))
    return min(86_400_000, max(1_000, fallback_ms))


def history_response_error(
    response: Any,
    *,
    fallback_retry_after_ms: int = 2_000,
) -> FederatedHistorySyncError:
    """Classify a non-success federation response without relaying its body."""

    detail = _history_response_detail(response)
    upstream_code = detail.get("code")
    if response.status_code == 429:
        code = (
            HISTORY_CAPACITY_CODE
            if upstream_code == HISTORY_CAPACITY_CODE
            else HISTORY_TEMPORARILY_UNAVAILABLE_CODE
        )
        return FederatedHistorySyncError(
            code,
            retryable=True,
            retry_after_ms=_retry_after_ms(response, 60_000, detail=detail),
        )
    if response.status_code in {408, 425, 500, 502, 503, 504}:
        return FederatedHistorySyncError(
            HISTORY_TEMPORARILY_UNAVAILABLE_CODE,
            retryable=True,
            retry_after_ms=_retry_after_ms(
                response,
                fallback_retry_after_ms,
                detail=detail,
            ),
        )
    return FederatedHistorySyncError(HISTORY_REJECTED_CODE, retryable=False)


def user_facing_history_error(exc: Exception) -> FederatedHistorySyncError:
    """Collapse an importer failure to stable status safe for user dispatch."""

    if isinstance(exc, FederatedHistorySyncError):
        return exc
    if isinstance(exc, FederationReplicaQuotaExceeded):
        return FederatedHistorySyncError(
            "KAED_FED_REPLICA_QUOTA_EXCEEDED",
            retryable=False,
            resource=exc.resource,
        )
    if isinstance(exc, FederationNetworkError):
        return FederatedHistorySyncError(
            HISTORY_TEMPORARILY_UNAVAILABLE_CODE,
            retryable=True,
            retry_after_ms=2_000,
        )
    if isinstance(exc, ValueError):
        return FederatedHistorySyncError(HISTORY_REJECTED_CODE, retryable=False)
    # Unknown local failures can recover after a worker/database outage. Do
    # not relay the exception and do not require a user to click Retry.
    return FederatedHistorySyncError(
        HISTORY_TEMPORARILY_UNAVAILABLE_CODE,
        retryable=True,
        retry_after_ms=2_000,
    )


class HistoryDeltaAdvanced(RuntimeError):
    def __init__(self, required_seq: int) -> None:
        super().__init__("live guild delivery advanced during history finalization")
        self.required_seq = required_seq


class UnresolvedHistoryIdentity(ValueError):
    pass


async def cleanup_history_transfers(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Prune expired grants and abandoned resumable-import state.

    Imports are retained only while their identity is a provenance anchor for
    imported messages. Stale attempts and completed empty/fully-purged imports
    can be retried after deletion, while the provenance guard protects against
    deleting a still-referenced transfer.
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
    abandoned_import_refs = select(
        GuildHistoryImport.export_id,
        GuildHistoryImport.export_domain,
    ).where(GuildHistoryImport.updated_at < current_time - ABANDONED_IMPORT_RETENTION)
    # A provenance anchor can intentionally keep its import parent for the
    # lifetime of imported messages, but unfinished staged payloads are merely
    # transient raw carriers. Drop those children after the abandoned-import
    # horizon so they cannot pin deleted attachment metadata indefinitely.
    abandoned_staged = await session.execute(
        delete(GuildHistoryStagedMessage).where(
            tuple_(
                GuildHistoryStagedMessage.export_id,
                GuildHistoryStagedMessage.export_domain,
            ).in_(abandoned_import_refs)
        )
    )
    abandoned_imports = await session.execute(
        delete(GuildHistoryImport).where(
            GuildHistoryImport.updated_at < current_time - ABANDONED_IMPORT_RETENTION,
            ~provenance_exists,
        )
    )
    return {
        "history_exports": int(expired_exports.rowcount or 0),  # type: ignore[attr-defined]
        "history_imports": int(abandoned_imports.rowcount or 0),  # type: ignore[attr-defined]
        "history_staged_messages": int(abandoned_staged.rowcount or 0),  # type: ignore[attr-defined]
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
    if not is_message_capable_channel_type(channel.type, guild_channel=True) or channel.unavailable:
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
                Channel.type.in_(tuple(sorted(GUILD_MESSAGE_CHANNEL_TYPES))),
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


async def lock_history_export_capacity(session: AsyncSession, requester_origin: str) -> None:
    """Serialize active grant admission globally, then for one requesting origin."""

    await session.scalar(
        select(func.pg_advisory_xact_lock(func.hashtextextended("kaede-history-export-global", 0)))
    )
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-history-export-origin:{requester_origin}", 0)
            )
        )
    )


async def ensure_history_export_capacity(
    session: AsyncSession,
    settings: Settings,
    requester_origin: str,
    channel_count: int,
    now: datetime,
) -> None:
    """Reject a new authority-side export before its short-lived rows amplify."""

    active_filter = (
        GuildHistoryExport.status.in_(ACTIVE_HISTORY_EXPORT_STATUSES),
        GuildHistoryExport.expires_at > now,
    )
    active_exports_total = int(
        await session.scalar(
            select(func.count()).select_from(GuildHistoryExport).where(*active_filter)
        )
        or 0
    )
    active_exports_origin = int(
        await session.scalar(
            select(func.count())
            .select_from(GuildHistoryExport)
            .where(
                *active_filter,
                GuildHistoryExport.requester_origin == requester_origin,
            )
        )
        or 0
    )
    active_grants_total = int(
        await session.scalar(
            select(func.count())
            .select_from(GuildHistoryExportChannel)
            .join(GuildHistoryExport, GuildHistoryExport.id == GuildHistoryExportChannel.export_id)
            .where(*active_filter)
        )
        or 0
    )
    active_grants_origin = int(
        await session.scalar(
            select(func.count())
            .select_from(GuildHistoryExportChannel)
            .join(GuildHistoryExport, GuildHistoryExport.id == GuildHistoryExportChannel.export_id)
            .where(
                *active_filter,
                GuildHistoryExport.requester_origin == requester_origin,
            )
        )
        or 0
    )
    if (
        active_exports_total + 1 > settings.federation_history_max_active_exports_total
        or active_exports_origin + 1 > settings.federation_history_max_active_exports_per_origin
        or active_grants_total + channel_count
        > settings.federation_history_max_active_channel_grants_total
        or active_grants_origin + channel_count
        > settings.federation_history_max_active_channel_grants_per_origin
    ):
        raise HTTPException(
            status_code=429,
            detail={"code": "KAED_FED_HISTORY_CAPACITY", "retry_after_ms": 60_000},
            headers={"Retry-After": "60"},
        )


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
    # Serialize active-grant admission globally and then per origin. This
    # prevents many legitimate member identities on one hostile instance from
    # multiplying short-lived export/channel rows beyond a bounded footprint.
    await lock_history_export_capacity(session, requester_origin)
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
            GuildHistoryExport.status.in_(ACTIVE_HISTORY_EXPORT_STATUSES),
            GuildHistoryExport.expires_at > now,
        )
        .order_by(GuildHistoryExport.created_at.desc())
        .limit(1)
    )
    if existing is not None:
        return existing
    await ensure_history_export_capacity(
        session,
        settings,
        requester_origin,
        len(channels),
        now,
    )
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
                ~exists(
                    select(E2EEControlRecord.id).where(
                        E2EEControlRecord.id == Message.id,
                        E2EEControlRecord.origin_domain == Message.origin_domain,
                    )
                ),
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
    poll_votes_by_message: dict[tuple[int, str], list[PollVote]] = {}
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
        for vote in await session.scalars(
            select(PollVote).where(
                tuple_(PollVote.message_id, PollVote.message_domain).in_(message_refs)
            )
        ):
            poll_votes_by_message.setdefault((vote.message_id, vote.message_domain), []).append(
                vote
            )
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
            poll=await render_poll_payload(session, message),
        )
        # History is a federation protocol, not a client response.  Keep both
        # author fields on the strict RemoteUserProfile wire contract: the
        # general API user payload renders numeric generations as strings.
        author_profile = profile_from_user(author)
        payload["author"] = author_profile
        payload["history_author"] = author_profile
        reactions = reactions_by_message.get(message_ref, [])
        selected_pin = pins_by_message.get(message_ref)
        payload["reactions"] = [
            {
                "user_id": str(reaction.user_id),
                "user_domain": reaction.user_domain,
                "emoji": canonical_reaction_emoji(reaction.emoji_key),
                "created_at": reaction.created_at.isoformat(),
            }
            for reaction in reactions
        ]
        payload["poll_votes"] = [
            {
                "answer_id": vote.answer_id,
                "user_id": str(vote.user_id),
                "user_domain": vote.user_domain,
                "created_at": vote.created_at.isoformat(),
            }
            for vote in poll_votes_by_message.get(message_ref, [])
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


def _history_timestamp_ceiling(
    settings: Settings,
    *,
    authenticated_event_ms: int | None = None,
) -> int:
    reference_ms = (
        authenticated_event_ms
        if authenticated_event_ms is not None
        else int(datetime.now(UTC).timestamp() * 1000)
    )
    return reference_ms + settings.federation_clock_skew_seconds * 1000


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
            ~exists(
                select(Attachment.id).where(
                    Attachment.message_id == purged_message.id,
                    Attachment.message_domain == purged_message.origin_domain,
                    Attachment.origin_domain == settings.domain,
                )
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
                    ~exists(
                        select(Attachment.id).where(
                            Attachment.message_id == purged_message.id,
                            Attachment.message_domain == purged_message.origin_domain,
                            Attachment.origin_domain == settings.domain,
                        )
                    ),
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
        await reconcile_replica_storage(
            session,
            guild.id,
            guild.origin_domain,
        )

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
    """Compatibility wrapper for the randomized local-only placeholder alias."""

    return unresolved_remote_username(user_id, origin)


def _history_ref(raw: object, field: str) -> tuple[int, str]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} reference is invalid")
    domain = raw.get("origin_domain", raw.get("domain"))
    return (
        database_snowflake(raw.get("id"), f"{field} id"),
        normalize_domain(str(domain or "")),
    )


def _validated_history_reaction_emoji(raw: object) -> str:
    if not isinstance(raw, str) or not 1 <= len(raw) <= 320:
        raise ValueError("historical reaction emoji is invalid")
    try:
        canonical = canonical_reaction_emoji(raw)
    except ValueError:
        raise ValueError("historical reaction emoji is invalid") from None
    return canonical


async def _ensure_history_identity(
    session: AsyncSession,
    settings: Settings,
    user_id: int,
    origin: str,
    *,
    profile: RemoteUserProfile | None = None,
    authority_origin: str,
) -> User:
    origin = normalize_domain(origin)
    authority = normalize_domain(authority_origin)
    existing = await session.get(User, (user_id, origin))
    if profile is not None:
        if (int(profile.id), profile.origin_domain) != (user_id, origin):
            raise ValueError("historical author profile identity is invalid")
        # A delegated guild-history response is not authoritative for a user
        # homed on some third instance.  In particular, never turn failed
        # delegated-profile validation into a placeholder in that third
        # party's namespace.
        return await resolve_delegated_profile(
            session,
            settings,
            profile,
            authority_origin=authority,
        )
    if existing is not None:
        return existing
    if origin == settings.domain:
        raise ValueError("historical data references an unknown local user")
    if origin != authority:
        raise UnresolvedHistoryIdentity(
            "historical data references an unresolved third-party identity"
        )
    # The guild home may identify an otherwise-unknown user in its own
    # namespace (for example a departed reaction author), but the peer record
    # must already be the authenticated guild authority.  History data may not
    # manufacture arbitrary Instance rows.
    instance = await session.get(Instance, origin)
    if instance is None or instance.is_self:
        raise ValueError("historical identity authority is unavailable")
    existing, introducer = await admit_remote_user_identity(
        session,
        settings,
        user_id,
        origin,
        introduced_by_domain=authority,
    )
    placeholder = existing or await insert_unresolved_remote_user(
        session,
        user_id=user_id,
        origin_domain=origin,
        introduced_by_domain=introducer,
    )
    if placeholder is None:
        raise RuntimeError("historical identity insert did not converge")
    return placeholder


def _validate_manifest(
    payload: object,
    guild: Guild,
    user: User,
) -> tuple[int, int, int, int, int, list[tuple[int, int]]]:
    expected_fields = {
        "available",
        "export_id",
        "guild_id",
        "guild_domain",
        "requester_user",
        "baseline_seq",
        "requester_member_version",
        "permission_generation",
        "history_policy_generation",
        "expires_at",
        "channels",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_fields
        or payload.get("available") is not True
    ):
        raise ValueError("history export is unavailable")
    export_id = database_snowflake(payload.get("export_id"), "history export id")
    if (
        database_snowflake(payload.get("guild_id"), "history guild id"),
        normalize_domain(str(payload.get("guild_domain", ""))),
    ) != (guild.id, guild.origin_domain):
        raise ValueError("history export references the wrong guild")
    requester = _history_ref(payload.get("requester_user"), "history requester")
    if not isinstance(payload.get("requester_user"), dict) or set(
        cast(dict[str, object], payload["requester_user"])
    ) != {"id", "domain"}:
        raise ValueError("history export requester has an invalid shape")
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
    try:
        expires_at = datetime.fromisoformat(str(payload.get("expires_at")))
    except ValueError:
        raise ValueError("history export expiry is invalid") from None
    if expires_at.tzinfo is None or expires_at <= datetime.now(UTC):
        raise ValueError("history export is expired")
    raw_channels = payload.get("channels")
    if not isinstance(raw_channels, list) or len(raw_channels) > 10_000:
        raise ValueError("history export channel set is invalid")
    channels: list[tuple[int, int]] = []
    seen: set[int] = set()
    previous_channel_id: int | None = None
    for raw in raw_channels:
        if not isinstance(raw, dict) or set(raw) != {"id", "origin_domain", "upper_bound_id"}:
            raise ValueError("history export channel has an invalid shape")
        channel_ref = _history_ref(raw, "history channel")
        upper = database_snowflake(
            raw.get("upper_bound_id") if isinstance(raw, dict) else None,
            "history upper bound",
        )
        if (
            channel_ref[1] != guild.origin_domain
            or channel_ref[0] in seen
            or (previous_channel_id is not None and channel_ref[0] <= previous_channel_id)
        ):
            raise ValueError("history export contains an invalid channel")
        seen.add(channel_ref[0])
        previous_channel_id = channel_ref[0]
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
    guild_id: int | None = None,
    channel_id: int,
    channel_type: int | None = None,
    after: int,
    upper_bound: int,
    before: int | None = None,
    previous_id: int | None = None,
    timestamp_upper_bound_ms: int | None = None,
    attachment_max_bytes: int = 15 * 1024 * 1024,
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
    message_type = raw.get("message_type", 0)
    if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
        raise ValueError("historical message type is invalid")
    if raw.get("deleted_at") is not None:
        raise ValueError("history export included deleted content")
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
    created_ms = int(created_at.timestamp() * 1000)
    validate_snowflake_timestamp(
        message_id,
        created_at,
        "historical message",
        # Historical messages normally predate the export event.  Passing the
        # creation instant here applies the snowflake-to-created_at half of the
        # live-event invariant; the explicit upper bound below prevents future
        # cursor and partition poisoning.
        event_timestamp_ms=created_ms,
        tolerance_ms=HISTORY_TIMESTAMP_TOLERANCE_MS,
    )
    timestamp_ceiling = (
        timestamp_upper_bound_ms
        if timestamp_upper_bound_ms is not None
        else int(datetime.now(UTC).timestamp() * 1000) + HISTORY_TIMESTAMP_TOLERANCE_MS
    )
    if created_ms > timestamp_ceiling:
        raise ValueError("historical message timestamp is after its authenticated bound")
    if edited_at is not None:
        edited_ms = int(edited_at.timestamp() * 1000)
        if edited_at < created_at or edited_ms > timestamp_ceiling:
            raise ValueError("historical message edit timestamp is invalid")
    content = raw.get("content")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    validate_e2ee_message_projection(
        e2ee,
        message_id=message_id,
        message_domain=guild_origin,
        edited=edited_at is not None,
    )
    attachments = raw.get("attachments", [])
    reactions = raw.get("reactions", [])
    poll_votes = raw.get("poll_votes", [])
    pin = raw.get("pin")
    mentions = raw.get("mention_user_refs", [])
    role_mentions = raw.get("mention_role_refs", [])
    mention_everyone = raw.get("mention_everyone", False)
    webhook = raw.get("webhook")
    if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4000):
        raise ValueError("historical message content is invalid")
    if not isinstance(attachments, list) or len(attachments) > 10:
        raise ValueError("historical message attachments are invalid")
    if not isinstance(reactions, list) or len(reactions) > 10_000:
        raise ValueError("historical message reactions are invalid")
    if not isinstance(poll_votes, list) or len(poll_votes) > 10_000:
        raise ValueError("historical message poll votes are invalid")
    if not isinstance(mentions, list) or len(mentions) > 5_000:
        raise ValueError("historical message mentions are invalid")
    if not isinstance(role_mentions, list) or len(role_mentions) > 100:
        raise ValueError("historical message role mentions are invalid")
    if not isinstance(mention_everyone, bool):
        raise ValueError("historical message everyone mention marker is invalid")
    if pin is not None and not isinstance(pin, dict):
        raise ValueError("historical message pin is invalid")
    if webhook is not None and not isinstance(webhook, dict):
        raise ValueError("historical message webhook is invalid")
    if content is not None and e2ee is not None:
        raise ValueError("historical message mixes plaintext and encrypted content")
    if (
        content is None
        and e2ee is None
        and not attachments
        and not raw.get("embeds")
        and not raw.get("components")
        and not raw.get("sticker_items")
        and raw.get("poll") is None
        and raw.get("forwarded_message_id") is None
        and message_type not in {6, 12, 18, 21}
    ):
        raise ValueError("historical message has no content")
    client_nonce = raw.get("client_nonce")
    if client_nonce is not None and (
        not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
    ):
        raise ValueError("historical message client nonce is invalid")
    referenced_id = raw.get("referenced_message_id")
    referenced_domain = raw.get("referenced_message_domain")
    if (referenced_id is None) != (referenced_domain is None):
        raise ValueError("historical message reference is incomplete")
    wire_referenced_ref: tuple[int, str] | None = None
    if referenced_id is not None:
        wire_referenced_ref = (
            database_snowflake(referenced_id, "historical reply"),
            normalize_domain(str(referenced_domain)),
        )
        if wire_referenced_ref[1] != guild_origin:
            raise ValueError("historical message reference has a non-authoritative origin")
    normalized_mentions: list[dict[str, str]] = []
    seen_mentions: set[tuple[int, str]] = set()
    for mention in mentions:
        mention_ref = _history_ref(mention, "historical mention")
        if mention_ref in seen_mentions:
            continue
        seen_mentions.add(mention_ref)
        normalized_mentions.append({"id": str(mention_ref[0]), "origin_domain": mention_ref[1]})
    normalized_role_mentions: list[dict[str, str]] = []
    seen_role_mentions: set[tuple[int, str]] = set()
    for mention in role_mentions:
        mention_ref = _history_ref(mention, "historical role mention")
        if mention_ref[1] != guild_origin:
            raise ValueError("historical role mention has a non-authoritative origin")
        if mention_ref in seen_role_mentions:
            raise ValueError("historical role mentions must be unique")
        seen_role_mentions.add(mention_ref)
        normalized_role_mentions.append(
            {"id": str(mention_ref[0]), "origin_domain": mention_ref[1]}
        )
    validate_webhook_attribution(
        webhook,
        message_type=message_type,
        message_origin=guild_origin,
        label="historical message",
    )
    profile = RemoteUserProfile.model_validate(raw.get("history_author"))
    if (
        database_snowflake(raw.get("author_id"), "historical author id"),
        normalize_domain(str(raw.get("author_domain", ""))),
    ) != (int(profile.id), profile.origin_domain):
        raise ValueError("historical author profile does not match the message")
    seen_attachments: set[tuple[int, str]] = set()
    normalized_attachments: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            raise ValueError("historical message attachment is invalid")
        attachment_ref = (
            database_snowflake(attachment.get("id"), "historical attachment id"),
            normalize_domain(str(attachment.get("origin_domain", ""))),
        )
        if attachment_ref[1] != profile.origin_domain or attachment_ref in seen_attachments:
            raise ValueError("historical attachment identity is invalid")
        seen_attachments.add(attachment_ref)
        filename = attachment.get("filename")
        content_type = attachment.get("content_type")
        size = attachment.get("size")
        if not isinstance(filename, str) or sanitize_filename(filename) != filename:
            raise ValueError("historical attachment filename is invalid")
        if not isinstance(content_type, str):
            raise ValueError("historical attachment content type is invalid")
        normalized_content_type = normalize_declared_type(content_type)
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= attachment_max_bytes
        ):
            raise ValueError("historical attachment size is invalid")
        width, height = remote_media_dimensions(attachment)
        blurhash = sanitized_remote_blurhash(attachment.get("blurhash"))
        duration_raw = attachment.get("duration_secs")
        duration_secs = (
            float(duration_raw)
            if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool)
            else None
        )
        waveform = attachment.get("waveform")
        if (
            duration_raw is not None
            and duration_secs is None
            or (waveform is not None and not isinstance(waveform, str))
        ):
            raise ValueError("historical voice metadata is invalid")
        validate_voice_attachment_metadata(
            content_type=normalized_content_type,
            encryption_mode=str(attachment.get("encryption_mode", "plaintext")),
            duration_secs=duration_secs,
            waveform=waveform,
        )
        variants = sanitized_remote_variants(
            attachment.get("variants", {}), max_bytes=attachment_max_bytes
        )
        normalized_attachments.append(
            {
                **attachment,
                "content_type": normalized_content_type,
                "width": width,
                "height": height,
                "duration_secs": duration_secs,
                "waveform": waveform,
                "blurhash": blurhash,
                "variants": variants,
            }
        )
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
    rich = _validated_message_rich_projection(
        raw,
        message_id=message_id,
        message_origin=guild_origin,
        message_created_at=created_at,
        e2ee=e2ee,
        message_type=message_type,
        flags=flags,
    )
    forwarded_ref = cast(tuple[int, str] | None, rich["forwarded_ref"])
    forwarded_channel_ref = cast(tuple[int, str] | None, rich["forwarded_channel_ref"])
    forward_snapshot = cast(dict[str, Any] | None, rich["forward_snapshot"])
    message_reference = validate_message_reference_projection(
        raw.get("message_reference"),
        message_type=message_type,
        channel_ref=(channel_id, guild_origin),
        guild_ref=((guild_id, guild_origin) if guild_id is not None else None),
        referenced_message_ref=wire_referenced_ref,
        forwarded_message_ref=forwarded_ref,
        forwarded_channel_ref=forwarded_channel_ref,
        has_forward_snapshot=bool(
            forward_snapshot is not None or rich.get("has_encrypted_forward")
        ),
        is_crosspost=bool(flags & MESSAGE_FLAG_IS_CROSSPOST),
        label="historical message",
    )
    validate_channel_follow_message_fields(
        raw,
        rich,
        message_type=message_type,
        channel_type=channel_type if channel_type is not None else 0,
        content=content,
        e2ee=e2ee,
        attachments=attachments,
        webhook=webhook,
        mention_user_refs=normalized_mentions,
        mention_role_refs=normalized_role_mentions,
        mention_everyone=mention_everyone,
        flags=flags,
        tts=raw.get("tts", False),
        client_nonce=client_nonce,
        referenced_message_ref=wire_referenced_ref,
        label="historical channel follow notice",
    )
    if poll_votes and rich["poll"] is None:
        raise ValueError("historical poll votes reference a message without a poll")
    normalized_reactions: list[dict[str, Any]] = []
    reaction_indexes: dict[tuple[int, str, str], int] = {}
    reaction_timestamps: dict[tuple[int, str, str], datetime] = {}
    for reaction in reactions:
        if not isinstance(reaction, dict):
            raise ValueError("historical reaction is invalid")
        reaction_user_id = database_snowflake(reaction.get("user_id"), "historical reaction user")
        reaction_user_domain = normalize_domain(str(reaction.get("user_domain", "")))
        reaction_emoji = _validated_history_reaction_emoji(reaction.get("emoji"))
        try:
            reaction_created = datetime.fromisoformat(str(reaction.get("created_at")))
        except ValueError:
            raise ValueError("historical reaction timestamp is invalid") from None
        if reaction_created.tzinfo is None:
            raise ValueError("historical reaction timestamp lacks a timezone")
        if (
            reaction_created < created_at
            or int(reaction_created.timestamp() * 1000) > timestamp_ceiling
        ):
            raise ValueError("historical reaction timestamp is outside the message bounds")
        reaction_key = (reaction_user_id, reaction_user_domain, reaction_emoji)
        normalized_reaction = {
            **reaction,
            "user_id": str(reaction_user_id),
            "user_domain": reaction_user_domain,
            "emoji": reaction_emoji,
            "created_at": reaction_created.isoformat(),
        }
        existing_index = reaction_indexes.get(reaction_key)
        if existing_index is None:
            reaction_indexes[reaction_key] = len(normalized_reactions)
            reaction_timestamps[reaction_key] = reaction_created
            normalized_reactions.append(normalized_reaction)
        elif reaction_created < reaction_timestamps[reaction_key]:
            reaction_timestamps[reaction_key] = reaction_created
            normalized_reactions[existing_index] = normalized_reaction
    seen_poll_votes: set[tuple[int, int, str]] = set()
    seen_single_select_voters: set[tuple[int, str]] = set()
    poll_projection = cast(
        tuple[
            dict[str, object],
            list[tuple[int, str | None, dict[str, object] | None]],
            bool,
            int,
            datetime,
        ]
        | None,
        rich["poll"],
    )
    for vote in poll_votes:
        if not isinstance(vote, dict):
            raise ValueError("historical poll vote is invalid")
        answer_id = vote.get("answer_id")
        if (
            isinstance(answer_id, bool)
            or not isinstance(answer_id, int)
            or not 1 <= answer_id <= 10
        ):
            raise ValueError("historical poll vote answer is invalid")
        voter_ref = (
            database_snowflake(vote.get("user_id"), "historical poll voter"),
            normalize_domain(str(vote.get("user_domain", ""))),
        )
        vote_key = (answer_id, voter_ref[0], voter_ref[1])
        if vote_key in seen_poll_votes:
            raise ValueError("historical poll votes contain a duplicate")
        seen_poll_votes.add(vote_key)
        voter_key = (voter_ref[0], voter_ref[1])
        if (
            poll_projection is not None
            and not poll_projection[2]
            and voter_key in seen_single_select_voters
        ):
            raise ValueError("historical single-select poll contains multiple votes")
        seen_single_select_voters.add(voter_key)
        try:
            voted_at = datetime.fromisoformat(str(vote.get("created_at")))
        except ValueError:
            raise ValueError("historical poll vote timestamp is invalid") from None
        if (
            voted_at.tzinfo is None
            or voted_at < created_at
            or int(voted_at.timestamp() * 1000) > timestamp_ceiling
        ):
            raise ValueError("historical poll vote timestamp is outside the message bounds")
    if isinstance(pin, dict):
        database_snowflake(pin.get("pinned_by_id"), "historical pinner")
        normalize_domain(str(pin.get("pinned_by_domain", "")))
        try:
            pinned_at = datetime.fromisoformat(str(pin.get("pinned_at")))
        except ValueError:
            raise ValueError("historical pin timestamp is invalid") from None
        if pinned_at.tzinfo is None:
            raise ValueError("historical pin timestamp lacks a timezone")
        if pinned_at < created_at or int(pinned_at.timestamp() * 1000) > timestamp_ceiling:
            raise ValueError("historical pin timestamp is outside the message bounds")
    validated = dict(raw)
    validated["embeds"] = rich["embeds"]
    validated["components"] = rich["components"]
    validated["sticker_items"] = rich["sticker_items"]
    validated["mention_user_refs"] = normalized_mentions
    validated["mention_role_refs"] = normalized_role_mentions
    validated["mention_everyone"] = mention_everyone
    validated["attachments"] = normalized_attachments
    validated["reactions"] = normalized_reactions
    validated["message_reference"] = message_reference
    return message_id, validated


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
        guild, history_import = await _lock_live_history_import(
            session,
            settings,
            guild,
            history_import,
        )
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
            await admit_replica_storage(session, settings, guild)
            await session.commit()
        elif channel_state.upper_bound_id != upper_bound:
            raise ValueError("historical channel grant changed while resuming")
        while not channel_state.complete:
            if time.monotonic() >= deadline:
                raise FederatedHistoryLimitExceeded("duration")
            if history_import.pages_downloaded >= settings.federation_history_max_pages:
                raise FederatedHistoryLimitExceeded("pages")
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
            guild, history_import = await _lock_live_history_import(
                session,
                settings,
                guild,
                history_import,
            )
            channel = await session.get(
                Channel,
                (channel_id, guild.origin_domain),
                populate_existing=True,
            )
            channel_state = await session.get(
                GuildHistoryImportChannel,
                (
                    history_import.export_id,
                    history_import.export_domain,
                    channel_id,
                    guild.origin_domain,
                ),
                populate_existing=True,
            )
            if channel is None or channel.unavailable or channel_state is None:
                raise RuntimeError("history channel disappeared while a page was in flight")
            if response.status_code != 200:
                raise history_response_error(response)
            payload = decode_federation_response_json(response)
            expected_page_fields = {
                "export_id",
                "channel_id",
                "channel_domain",
                "upper_bound_id",
                "messages",
                "page_bytes",
                "next_after",
                "next_before",
                "order",
                "complete",
            }
            if not isinstance(payload, dict) or set(payload) != expected_page_fields:
                raise ValueError("historical message page is invalid")
            page_bytes = payload.get("page_bytes")
            if (
                database_snowflake(payload.get("export_id"), "history page export")
                != history_import.export_id
                or database_snowflake(payload.get("channel_id"), "history page channel")
                != channel_id
                or payload.get("channel_domain") != guild.origin_domain
                or database_snowflake(payload.get("upper_bound_id"), "history page upper bound")
                != upper_bound
                or payload.get("order") != ("recent_first" if recent_first else "oldest_first")
                or type(payload.get("complete")) is not bool
                or type(page_bytes) is not int
                or not 0 <= page_bytes <= settings.federation_history_page_bytes
                or (recent_first and payload.get("next_after") is not None)
                or (not recent_first and payload.get("next_before") is not None)
            ):
                raise ValueError("historical message page escaped its request binding")
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
                    guild_id=guild.id,
                    channel_id=channel_id,
                    channel_type=channel.type,
                    after=last,
                    upper_bound=upper_bound,
                    before=channel_state.next_before_id if recent_first else None,
                    previous_id=previous_id if recent_first else None,
                    timestamp_upper_bound_ms=_history_timestamp_ceiling(settings),
                    attachment_max_bytes=settings.media_max_attachment_bytes,
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
                raise FederatedHistoryLimitExceeded("messages")
            if next_reactions > settings.federation_history_max_reactions:
                raise FederatedHistoryLimitExceeded("reactions")
            if next_bytes > settings.federation_history_max_bytes:
                raise FederatedHistoryLimitExceeded("bytes")
            complete = payload["complete"] is True
            cursor_name = "next_before" if recent_first else "next_after"
            next_cursor = payload.get(cursor_name)
            parsed_next: int | None = None
            if complete:
                if next_cursor is not None:
                    raise ValueError("complete historical message page carried a cursor")
            else:
                if not raw_messages or next_cursor is None:
                    raise ValueError("historical message cursor did not advance")
                parsed_next = database_snowflake(next_cursor, "historical message cursor")
                if recent_first:
                    expected = max(0, int(previous_id or 0) - 1)
                    if parsed_next != expected or parsed_next >= channel_state.next_before_id:
                        raise ValueError("historical recent-first cursor is invalid")
                elif parsed_next != last or parsed_next <= after:
                    raise ValueError("historical message cursor is invalid")
            history_import.pages_downloaded += 1
            history_import.messages_downloaded = next_messages
            history_import.reactions_downloaded = next_reactions
            history_import.bytes_downloaded = next_bytes
            channel_state.pages_downloaded += 1
            channel_state.messages_downloaded += len(raw_messages)
            channel_state.bytes_downloaded += response_bytes
            if complete:
                channel_state.complete = True
                await admit_replica_storage(session, settings, guild)
                await session.commit()
                continue
            if recent_first:
                if parsed_next is None:
                    raise RuntimeError("validated history cursor disappeared")
                channel_state.next_before_id = parsed_next
            await admit_replica_storage(session, settings, guild)
            await session.commit()
    return history_import.messages_downloaded


async def _revalidate_staged_history_message(
    session: AsyncSession,
    settings: Settings,
    history_import: GuildHistoryImport,
    staged: GuildHistoryStagedMessage,
    *,
    authenticated_event_ms: int | None = None,
) -> dict[str, Any]:
    channel_state = await session.get(
        GuildHistoryImportChannel,
        (
            history_import.export_id,
            history_import.export_domain,
            staged.channel_id,
            staged.channel_domain,
        ),
    )
    if channel_state is None:
        raise ValueError("staged history message has no channel grant")
    channel = await session.get(Channel, (staged.channel_id, staged.channel_domain))
    if channel is None or channel.unavailable:
        raise ValueError("staged history message channel is unavailable")
    message_id, validated = _validate_history_message(
        staged.payload,
        guild_origin=history_import.guild_domain,
        guild_id=history_import.guild_id,
        channel_id=staged.channel_id,
        channel_type=channel.type,
        after=0,
        upper_bound=channel_state.upper_bound_id,
        timestamp_upper_bound_ms=_history_timestamp_ceiling(settings),
        attachment_max_bytes=settings.media_max_attachment_bytes,
    )
    if authenticated_event_ms is not None:
        created_at = datetime.fromisoformat(str(validated.get("created_at")))
        if int(created_at.timestamp() * 1000) > _history_timestamp_ceiling(
            settings,
            authenticated_event_ms=authenticated_event_ms,
        ):
            raise ValueError("historical message was created after its delta event")
    if (
        message_id,
        normalize_domain(str(validated.get("origin_domain", ""))),
        database_snowflake(validated.get("channel_id"), "historical message channel"),
        normalize_domain(str(validated.get("channel_domain", ""))),
    ) != (
        staged.message_id,
        staged.message_domain,
        staged.channel_id,
        staged.channel_domain,
    ):
        raise ValueError("staged historical message identity changed during reconciliation")
    staged.payload = validated
    return validated


async def _apply_history_delta_event(
    session: AsyncSession,
    settings: Settings,
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
        await _revalidate_staged_history_message(
            session,
            settings,
            history_import,
            staged,
            authenticated_event_ms=int(event.get("ts", 0)),
        )
        return
    if event_type == "guild.message.bulk_delete":
        raw_messages = content.get("messages")
        if not isinstance(raw_messages, list) or not 2 <= len(raw_messages) <= 100:
            raise ValueError("history bulk deletion message list is invalid")
        message_refs = [_history_ref(item, "history bulk deleted message") for item in raw_messages]
        if len(message_refs) != len(set(message_refs)):
            raise ValueError("history bulk deletion contains duplicate messages")
        for message_ref in message_refs:
            staged = await session.get(
                GuildHistoryStagedMessage,
                (
                    history_import.export_id,
                    history_import.export_domain,
                    message_ref[0],
                    message_ref[1],
                ),
            )
            if staged is not None:
                await session.delete(staged)
        return
    if event_type == "guild.message.purge":
        author_ref = _history_ref(content.get("author"), "history purge author")
        try:
            cutoff = datetime.fromisoformat(str(content.get("created_after")))
            deleted_at = datetime.fromisoformat(str(content.get("deleted_at")))
        except ValueError:
            raise ValueError("history purge timestamp is invalid") from None
        if cutoff.tzinfo is None or deleted_at.tzinfo is None or cutoff > deleted_at:
            raise ValueError("history purge timestamp bounds are invalid")
        staged_rows = list(
            await session.scalars(
                select(GuildHistoryStagedMessage).where(
                    GuildHistoryStagedMessage.export_id == history_import.export_id,
                    GuildHistoryStagedMessage.export_domain == history_import.export_domain,
                )
            )
        )
        for staged in staged_rows:
            raw = await _revalidate_staged_history_message(
                session,
                settings,
                history_import,
                staged,
            )
            created_at = datetime.fromisoformat(str(raw.get("created_at")))
            if (
                int(raw.get("author_id", -1)),
                str(raw.get("author_domain", "")),
            ) == author_ref and cutoff <= created_at <= deleted_at:
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
        clear = event_type == "guild.reaction.clear"
        user_ref = None if clear else _history_ref(content.get("user"), "history reaction user")
        raw_emoji = content.get("emoji")
        emoji = _validated_history_reaction_emoji(raw_emoji) if raw_emoji is not None else None
        if not clear and (user_ref is None or emoji is None):
            raise ValueError("historical reaction mutation is incomplete")
        raw_reactions = raw.get("reactions", [])
        if not isinstance(raw_reactions, list):
            raise ValueError("historical message reactions are invalid")
        reactions: list[dict[str, Any]] = []
        for item in raw_reactions:
            if not isinstance(item, dict):
                raise ValueError("historical reaction is invalid")
            reactions.append(
                {**item, "emoji": _validated_history_reaction_emoji(item.get("emoji"))}
            )
        if clear:
            reactions = (
                [] if emoji is None else [item for item in reactions if item.get("emoji") != emoji]
            )
        else:
            if user_ref is None or emoji is None:
                raise RuntimeError("validated reaction mutation lost required fields")
            key = (str(user_ref[0]), user_ref[1], emoji)
            reactions = [
                item
                for item in reactions
                if (str(item.get("user_id")), str(item.get("user_domain")), item.get("emoji"))
                != key
            ]
        if event_type.endswith("add"):
            if user_ref is None or emoji is None:
                raise RuntimeError("validated reaction addition lost required fields")
            reactions.append(
                {
                    "user_id": str(user_ref[0]),
                    "user_domain": user_ref[1],
                    "emoji": emoji,
                    "created_at": datetime.fromtimestamp(
                        int(event.get("ts", 0)) / 1000,
                        tz=UTC,
                    ).isoformat(),
                }
            )
        raw["reactions"] = reactions
    elif event_type.startswith("guild.poll.vote."):
        user_ref = _history_ref(content.get("user"), "history poll voter")
        answer_id = content.get("answer_id")
        if (
            isinstance(answer_id, bool)
            or not isinstance(answer_id, int)
            or not 1 <= answer_id <= 10
        ):
            raise ValueError("history poll vote is invalid")
        votes = list(raw.get("poll_votes", []))
        poll_vote_key = (answer_id, str(user_ref[0]), user_ref[1])
        votes = [
            item
            for item in votes
            if (
                item.get("answer_id"),
                str(item.get("user_id")),
                str(item.get("user_domain")),
            )
            != poll_vote_key
        ]
        if event_type.endswith("add"):
            votes.append(
                {
                    "answer_id": answer_id,
                    "user_id": str(user_ref[0]),
                    "user_domain": user_ref[1],
                    "created_at": datetime.fromtimestamp(
                        int(event.get("ts", 0)) / 1000,
                        tz=UTC,
                    ).isoformat(),
                }
            )
        raw["poll_votes"] = votes
    elif event_type == "guild.poll.finalize":
        poll = raw.get("poll")
        if not isinstance(poll, dict) or not isinstance(poll.get("results"), dict):
            raise ValueError("history poll finalization is invalid")
        finalized_at = content.get("finalized_at")
        try:
            parsed_finalized_at = datetime.fromisoformat(str(finalized_at))
        except ValueError:
            raise ValueError("history poll finalization timestamp is invalid") from None
        if parsed_finalized_at.tzinfo is None:
            raise ValueError("history poll finalization timestamp lacks a timezone")
        results = dict(poll["results"])
        results["is_finalized"] = True
        poll = dict(poll)
        poll["finalized_at"] = parsed_finalized_at.isoformat()
        poll["results"] = results
        raw["poll"] = poll
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
    await _revalidate_staged_history_message(
        session,
        settings,
        history_import,
        staged,
        authenticated_event_ms=int(event.get("ts", 0)),
    )


async def _reconcile_history_delta(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
    after_seq: int,
    *,
    deadline: float,
    commit_pages: bool = True,
) -> int:
    cursor = after_seq
    if time.monotonic() >= deadline:
        raise FederatedHistoryLimitExceeded("duration")
    channel_pages = int(
        await session.scalar(
            select(func.coalesce(func.sum(GuildHistoryImportChannel.pages_downloaded), 0)).where(
                GuildHistoryImportChannel.export_id == history_import.export_id,
                GuildHistoryImportChannel.export_domain == history_import.export_domain,
            )
        )
        or 0
    )
    delta_pages_used = max(0, int(history_import.pages_downloaded) - channel_pages)
    remaining_page_budget = settings.federation_history_max_pages - int(
        history_import.pages_downloaded
    )
    request_limit = min(
        HISTORY_DELTA_MAX_REQUESTS - delta_pages_used,
        remaining_page_budget,
    )
    if request_limit <= 0:
        resource = (
            "delta_requests" if HISTORY_DELTA_MAX_REQUESTS - delta_pages_used <= 0 else "pages"
        )
        raise FederatedHistoryLimitExceeded(resource)
    for _page in range(request_limit):
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise FederatedHistoryLimitExceeded("duration")
        remaining_bytes = settings.federation_history_max_bytes - int(
            history_import.bytes_downloaded
        )
        if remaining_bytes <= 0:
            raise FederatedHistoryLimitExceeded("bytes")
        response = await signed_request(
            session,
            settings,
            "GET",
            guild.origin_domain,
            f"/_kaede/v1/guilds/{guild.id}/history-exports/{history_import.export_id}/delta",
            query={"after_seq": str(cursor)},
            request_timeout=min(30.0, remaining_time),
            max_response_bytes=min(
                settings.federation_history_page_bytes + 64 * 1024,
                remaining_bytes,
            ),
        )
        if time.monotonic() >= deadline:
            raise FederatedHistoryLimitExceeded("duration")
        if response.status_code != 200:
            raise history_response_error(response)
        guild, history_import = await _lock_live_history_import(
            session,
            settings,
            guild,
            history_import,
        )
        payload = decode_federation_response_json(response)
        raw_events = payload.get("events") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"events", "cursor_seq", "latest_seq", "complete"}
            or not isinstance(raw_events, list)
            or len(raw_events) > HISTORY_DELTA_EVENTS_PER_PAGE
            or type(payload.get("complete")) is not bool
        ):
            raise ValueError("history reconciliation response is invalid")
        next_cursor = database_snowflake(payload.get("cursor_seq"), "history delta cursor")
        latest_seq = database_snowflake(payload.get("latest_seq"), "history latest cursor")
        complete = payload["complete"] is True
        if next_cursor < cursor or (next_cursor == cursor and (bool(raw_events) or not complete)):
            raise ValueError("history reconciliation cursor did not advance")
        if latest_seq < cursor or next_cursor > latest_seq:
            raise ValueError("history reconciliation cursor is outside the remote bound")
        if complete != (next_cursor >= latest_seq):
            raise ValueError("history reconciliation completion marker is inconsistent")

        response_bytes = len(response.content)
        next_pages = int(history_import.pages_downloaded) + 1
        next_messages = int(history_import.messages_downloaded) + len(raw_events)
        next_bytes = int(history_import.bytes_downloaded) + response_bytes
        if next_pages > settings.federation_history_max_pages:
            raise FederatedHistoryLimitExceeded("pages")
        if next_messages > settings.federation_history_max_messages:
            raise FederatedHistoryLimitExceeded("messages")
        if next_bytes > settings.federation_history_max_bytes:
            raise FederatedHistoryLimitExceeded("bytes")

        validated_events: list[dict[str, Any]] = []
        previous_seq = cursor
        for raw_event in raw_events:
            envelope = await validated_event_envelope(
                session,
                settings,
                guild.origin_domain,
                raw_event,
                allow_authority_attested_actor=True,
            )
            event = envelope.model_dump(mode="json")
            context = event.get("context")
            if (
                event.get("type") not in HISTORY_EVENT_TYPES
                or not isinstance(context, dict)
                or context.get("guild_id") != str(guild.id)
                or context.get("guild_domain") != guild.origin_domain
            ):
                raise ValueError("history reconciliation event escaped its guild binding")
            event_seq = database_snowflake(
                event.get("seq") or context.get("seq"),
                "history delta event sequence",
            )
            if not previous_seq < event_seq <= next_cursor:
                raise ValueError("history reconciliation events are out of order")
            previous_seq = event_seq
            validated_events.append(event)
        reaction_events = sum(
            1
            for event in validated_events
            if (
                str(event.get("type", "")).startswith("guild.reaction.")
                or str(event.get("type", "")).startswith("guild.poll.vote.")
            )
        )
        next_reactions = int(history_import.reactions_downloaded) + reaction_events
        if next_reactions > settings.federation_history_max_reactions:
            raise FederatedHistoryLimitExceeded("reactions")

        for event in validated_events:
            await _apply_history_delta_event(
                session,
                settings,
                history_import,
                event,
            )
        history_import.pages_downloaded = next_pages
        history_import.messages_downloaded = next_messages
        history_import.bytes_downloaded = next_bytes
        history_import.reactions_downloaded = next_reactions
        cursor = next_cursor
        if commit_pages:
            await admit_replica_storage(session, settings, guild)
            await session.commit()
        if complete:
            return cursor
    raise FederatedHistoryLimitExceeded("delta_requests")


def _validate_historical_channel_follow_source(
    known_source: Channel | None,
    *,
    source_channel_ref: tuple[int, str],
    source_guild_ref: tuple[int, str],
) -> None:
    """Validate immutable source identity without comparing a mutable name."""

    if known_source is None or known_source.unavailable:
        return
    if (
        (known_source.id, known_source.origin_domain) != source_channel_ref
        or known_source.type != 5
        or (known_source.guild_id, known_source.guild_domain) != source_guild_ref
    ):
        raise ValueError("historical channel follow source does not match its channel")


async def _merge_history_import_batch(
    session: AsyncSession,
    settings: Settings,
    guild: Guild,
    history_import: GuildHistoryImport,
    *,
    reconciled_seq: int,
    tombstone_delivery_wakes: set[str],
) -> tuple[int, bool]:
    guild, history_import = await _lock_live_history_import(
        session,
        settings,
        guild,
        history_import,
    )
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
    staged_attachment_refs: set[tuple[int, str]] = set()
    for staged in staged_rows:
        staged_attachment_refs.update(attachment_refs_from_payloads([staged.payload]))
    for attachment_id, attachment_domain in sorted(
        staged_attachment_refs, key=lambda ref: (ref[1], ref[0])
    ):
        await lock_media_tombstone_ref(session, attachment_id, attachment_domain)
    locked_guild = await session.scalar(
        select(Guild)
        .where(Guild.id == guild.id, Guild.origin_domain == guild.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_guild is None:
        raise RuntimeError("replicated guild disappeared during history merge")
    if locked_guild.last_event_seq > reconciled_seq:
        raise HistoryDeltaAdvanced(locked_guild.last_event_seq)
    current_member = await session.get(
        GuildMember,
        (
            locked_guild.id,
            locked_guild.origin_domain,
            history_import.requester_user_id,
            history_import.requester_user_domain,
        ),
    )
    if (
        current_member is None
        or current_member.member_version != history_import.requester_member_version
        or locked_guild.permission_generation != history_import.permission_generation
        or locked_guild.history_policy_generation != history_import.history_policy_generation
    ):
        raise RuntimeError("history grant changed during finalization")
    guild = locked_guild
    imported = 0
    latest_by_channel: dict[tuple[int, str], int] = {}
    for staged in staged_rows:
        raw = await _revalidate_staged_history_message(
            session,
            settings,
            history_import,
            staged,
        )
        raw_attachment_refs = attachment_refs_from_payloads([raw])
        terminal_refs = (
            set(
                (
                    await session.execute(
                        select(
                            MediaTombstoneSource.attachment_id,
                            MediaTombstoneSource.attachment_domain,
                        ).where(
                            tuple_(
                                MediaTombstoneSource.attachment_id,
                                MediaTombstoneSource.attachment_domain,
                            ).in_(raw_attachment_refs)
                        )
                    )
                ).tuples()
            )
            if raw_attachment_refs
            else set()
        )
        if terminal_refs:
            raw["attachments"] = [
                attachment
                for attachment in raw.get("attachments", [])
                if not (
                    isinstance(attachment, dict)
                    and attachment_refs_from_payloads([{"attachments": [attachment]}])
                    & terminal_refs
                )
            ]
            if raw.get("content") is None and raw.get("e2ee") is None and not raw["attachments"]:
                # An attachment-only historical message whose media is already
                # terminal is itself no longer a useful projection.
                continue
        staged_channel_ref = (staged.channel_id, staged.channel_domain)
        latest_by_channel[staged_channel_ref] = max(
            latest_by_channel.get(staged_channel_ref, 0), staged.message_id
        )
        webhook = validate_webhook_attribution(
            raw.get("webhook"),
            message_type=int(raw.get("message_type", 0)),
            message_origin=staged.message_domain,
            label="historical message",
        )
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
        referenced: Message | None = None
        if referenced_id is not None:
            referenced = await session.get(Message, (referenced_id, referenced_domain))
            if referenced is None and int(raw.get("message_type", 0)) != POLL_RESULT_MESSAGE_TYPE:
                referenced_id = None
                referenced_domain = None
        channel = await session.get(Channel, staged_channel_ref)
        if channel is None or channel.guild_id != guild.id:
            raise RuntimeError("historical message channel disappeared")
        if int(raw.get("message_type", 0)) == 12:
            stored_reference = cast(dict[str, object], raw["message_reference"])
            source_channel_ref = (
                int(cast(str, stored_reference["channel_id"])),
                cast(str, stored_reference["channel_domain"]),
            )
            source_guild_ref = (
                int(cast(str, stored_reference["guild_id"])),
                cast(str, stored_reference["guild_domain"]),
            )
            known_source = await session.get(Channel, source_channel_ref)
            _validate_historical_channel_follow_source(
                known_source,
                source_channel_ref=source_channel_ref,
                source_guild_ref=source_guild_ref,
            )
        staged_e2ee = validate_e2ee_envelope(raw.get("e2ee"))
        validate_e2ee_message_projection(
            staged_e2ee,
            message_id=staged.message_id,
            message_domain=staged.message_domain,
            edited=raw.get("edited_at") is not None,
        )
        if (
            int(raw.get("message_type", 0)) != POLL_RESULT_MESSAGE_TYPE
            or raw.get("poll_result") is None
        ):
            validate_message_encryption_policy(
                channel.encryption_mode,
                content=raw.get("content"),
                e2ee=staged_e2ee,
                attachment_count=len(raw.get("attachments", [])),
                policy_generation=channel.encryption_policy_generation,
                policy_epoch=channel.encryption_epoch,
                policy_group_id=channel.encryption_group_id,
            )
        message_created_at = datetime.fromisoformat(str(raw["created_at"]))
        rich = _validated_message_rich_projection(
            raw,
            message_id=staged.message_id,
            message_origin=staged.message_domain,
            message_created_at=message_created_at,
            e2ee=staged_e2ee,
            message_type=int(raw.get("message_type", 0)),
            flags=int(raw.get("flags", 0)),
        )
        if int(raw.get("message_type", 0)) == POLL_RESULT_MESSAGE_TYPE:
            projection, _embed = validate_poll_result_wire_body(
                raw,
                author_ref=(author.id, author.origin_domain),
                channel_ref=(channel.id, channel.origin_domain),
            )
            if referenced is not None and (
                (referenced.author_id, referenced.author_domain)
                != (author.id, author.origin_domain)
                or ("e2ee" if referenced.e2ee is not None else "plaintext")
                != projection["source_encryption_mode"]
            ):
                raise ValueError("historical poll result source binding is invalid")
        application_ref = cast(tuple[int, str] | None, rich["application_ref"])
        forwarded_ref = cast(tuple[int, str] | None, rich["forwarded_ref"])
        forwarded_channel_ref = cast(
            tuple[int, str] | None,
            rich["forwarded_channel_ref"],
        )
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
                e2ee=staged_e2ee,
                embeds=cast(list[dict[str, Any]], rich["embeds"]),
                components=cast(list[dict[str, Any]], rich["components"]),
                sticker_items=cast(list[dict[str, Any]], rich["sticker_items"]),
                application_id=(application_ref[0] if application_ref is not None else None),
                application_domain=(application_ref[1] if application_ref is not None else None),
                interaction_metadata=cast(
                    dict[str, object] | None,
                    rich["interaction_metadata"],
                ),
                view_version=cast(int, rich["view_version"]),
                forwarded_message_id=(forwarded_ref[0] if forwarded_ref is not None else None),
                forwarded_message_domain=(forwarded_ref[1] if forwarded_ref is not None else None),
                forwarded_channel_id=(
                    forwarded_channel_ref[0] if forwarded_channel_ref is not None else None
                ),
                forwarded_channel_domain=(
                    forwarded_channel_ref[1] if forwarded_channel_ref is not None else None
                ),
                forward_snapshot=cast(dict[str, Any] | None, rich["forward_snapshot"]),
                poll_result=cast(dict[str, Any] | None, rich["poll_result"]),
                encryption_policy_generation=channel.encryption_policy_generation,
                encryption_epoch=channel.encryption_epoch,
                message_type=int(raw.get("message_type", 0)),
                tts=bool(raw.get("tts", False)),
                flags=int(raw.get("flags", 0)),
                client_nonce=raw.get("client_nonce"),
                referenced_message_id=referenced_id,
                referenced_message_domain=referenced_domain,
                message_reference=cast(
                    dict[str, Any] | None,
                    raw.get("message_reference"),
                ),
                mention_user_refs=raw.get("mention_user_refs", []),
                mention_role_refs=raw.get("mention_role_refs", []),
                mention_everyone=bool(raw.get("mention_everyone", False)),
                webhook_id=(webhook.webhook_ref[0] if webhook is not None else None),
                webhook_domain=(webhook.webhook_ref[1] if webhook is not None else None),
                webhook_name=(webhook.name if webhook is not None else None),
                webhook_avatar_hash=(webhook.avatar_hash if webhook is not None else None),
                webhook_avatar_url=(webhook.avatar_url if webhook is not None else None),
                edited_at=(
                    datetime.fromisoformat(str(raw["edited_at"]))
                    if raw.get("edited_at") is not None
                    else None
                ),
                created_at=message_created_at,
            )
            .on_conflict_do_nothing(index_elements=["id", "origin_domain"])
            .returning(Message.id)
        )
        if inserted is None:
            existing = await session.get(Message, (staged.message_id, staged.message_domain))
            if (
                existing is None
                or (
                    int(raw.get("message_type", 0)) in {6, 12}
                    and existing.message_reference != raw.get("message_reference")
                )
                or (
                    int(raw.get("message_type", 0)) == 12 and existing.content != raw.get("content")
                )
            ):
                raise ValueError("historical system message conflicts with stored history")
            continue
        message = await session.get(Message, (staged.message_id, staged.message_domain))
        if message is None:
            raise RuntimeError("imported historical message disappeared")
        poll_projection = cast(
            tuple[
                dict[str, object],
                list[tuple[int, str | None, dict[str, object] | None]],
                bool,
                int,
                datetime,
            ]
            | None,
            rich["poll"],
        )
        if poll_projection is not None:
            question, answers, allow_multiselect, layout_type, expiry = poll_projection
            raw_poll = raw.get("poll")
            raw_finalized_at = raw_poll.get("finalized_at") if isinstance(raw_poll, dict) else None
            session.add(
                Poll(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    question=question,
                    allow_multiselect=allow_multiselect,
                    layout_type=layout_type,
                    expires_at=expiry,
                    finalized_at=(
                        datetime.fromisoformat(str(raw_finalized_at))
                        if raw_finalized_at is not None
                        else None
                    ),
                    created_at=message_created_at,
                )
            )
            for answer_id, text, emoji in answers:
                session.add(
                    PollAnswer(
                        message_id=message.id,
                        message_domain=message.origin_domain,
                        answer_id=answer_id,
                        text=text,
                        emoji=emoji,
                    )
                )
            await session.flush()
            valid_answer_ids = {answer_id for answer_id, _text, _emoji in answers}
            for vote in raw.get("poll_votes", []):
                if not isinstance(vote, dict):
                    continue
                answer_id = int(vote["answer_id"])
                if answer_id not in valid_answer_ids:
                    raise ValueError("historical poll vote references an unknown answer")
                voter_id = database_snowflake(
                    vote.get("user_id"),
                    "historical poll voter",
                )
                voter_domain = normalize_domain(str(vote.get("user_domain", "")))
                try:
                    await _ensure_history_identity(
                        session,
                        settings,
                        voter_id,
                        voter_domain,
                        authority_origin=guild.origin_domain,
                    )
                except UnresolvedHistoryIdentity:
                    continue
                await session.execute(
                    pg_insert(PollVote)
                    .values(
                        message_id=message.id,
                        message_domain=message.origin_domain,
                        answer_id=answer_id,
                        user_id=voter_id,
                        user_domain=voter_domain,
                        created_at=datetime.fromisoformat(str(vote["created_at"])),
                    )
                    .on_conflict_do_nothing()
                )
        if bool(rich.get("has_encrypted_controls")):
            installation_ref = cast(
                tuple[int, str] | None,
                rich.get("interaction_installation_ref"),
            )
            if application_ref is None or installation_ref is None:
                raise ValueError("historical encrypted view lineage is incomplete")
            session.add(
                MessageView(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    application_id=application_ref[0],
                    application_domain=application_ref[1],
                    integration_type=cast(str, rich["interaction_integration_type"]),
                    installation_id=installation_ref[0],
                    installation_domain=installation_ref[1],
                    installation_revision=cast(
                        int,
                        rich["interaction_installation_revision"],
                    ),
                    version=cast(int, rich["view_version"]),
                    persistent=cast(bool, rich["view_persistent"]),
                    expires_at=cast(datetime | None, rich["view_expires_at"]),
                )
            )
        await replicate_message_attachments(
            session,
            settings,
            message,
            author,
            raw.get("attachments", []),
        )
        terminal_attachment_refs = await terminal_attachment_refs_for_messages(
            session,
            settings,
            {(message.id, message.origin_domain)},
        )
        for terminal_attachment_ref in terminal_attachment_refs:
            terminal_attachment = await session.get(Attachment, terminal_attachment_ref)
            if terminal_attachment is not None:
                tombstone_delivery_wakes.update(
                    await queue_terminal_attachment_tombstone(
                        session,
                        settings,
                        terminal_attachment,
                    )
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
            try:
                await _ensure_history_identity(
                    session,
                    settings,
                    user_id,
                    user_domain,
                    authority_origin=guild.origin_domain,
                )
            except UnresolvedHistoryIdentity:
                # Reaction attribution has no embedded authoritative profile in
                # v1.  Preserve the message while omitting metadata that cannot
                # safely be tied to an already-resolved third-party identity.
                continue
            await session.execute(
                pg_insert(Reaction)
                .values(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    user_id=user_id,
                    user_domain=user_domain,
                    emoji_key=reaction["emoji"],
                    created_at=datetime.fromisoformat(str(reaction["created_at"])),
                )
                .on_conflict_do_nothing()
            )
        pin = raw.get("pin")
        if isinstance(pin, dict):
            pinner_id = database_snowflake(pin.get("pinned_by_id"), "historical pinner")
            pinner_domain = normalize_domain(str(pin.get("pinned_by_domain", "")))
            try:
                await _ensure_history_identity(
                    session,
                    settings,
                    pinner_id,
                    pinner_domain,
                    authority_origin=guild.origin_domain,
                )
            except UnresolvedHistoryIdentity:
                # As with reactions, v1 pin metadata carries only a reference.
                # Do not let a guild home manufacture a third party's user row.
                pass
            else:
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
        await admit_replica_storage(session, settings, guild)
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
    *,
    reconciled_seq: int,
    deadline: float,
    tombstone_delivery_wakes: set[str],
) -> int:
    imported = 0
    cursor = reconciled_seq
    while True:
        if time.monotonic() >= deadline:
            raise FederatedHistoryLimitExceeded("duration")
        try:
            batch_imported, complete = await _merge_history_import_batch(
                session,
                settings,
                guild,
                history_import,
                reconciled_seq=cursor,
                tombstone_delivery_wakes=tombstone_delivery_wakes,
            )
        except HistoryDeltaAdvanced as exc:
            await session.rollback()
            refreshed_import = await session.get(
                GuildHistoryImport,
                (history_import.export_id, history_import.export_domain),
            )
            refreshed_guild = await session.get(Guild, (guild.id, guild.origin_domain))
            if refreshed_import is None or refreshed_guild is None:
                raise RuntimeError("history import disappeared during reconciliation") from exc
            history_import = refreshed_import
            guild = refreshed_guild
            cursor = await _reconcile_history_delta(
                session,
                settings,
                guild,
                history_import,
                cursor,
                deadline=deadline,
            )
            if cursor < exc.required_seq:
                raise ValueError(
                    "history reconciliation did not cover the locally applied guild sequence"
                ) from exc
            continue
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
    *,
    tombstone_delivery_wakes: set[str] | None = None,
) -> int:
    if tombstone_delivery_wakes is None:
        tombstone_delivery_wakes = set()
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
        raise history_response_error(response)
    raw_manifest = decode_federation_response_json(response)
    if raw_manifest == {"available": False}:
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
    await admit_replica_storage(session, settings, guild)
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
                deadline=deadline,
            )
            cursor = await _reconcile_history_delta(
                session,
                settings,
                guild,
                history_import,
                cursor,
                deadline=deadline,
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
                reconciled_seq=cursor,
                deadline=deadline,
                tombstone_delivery_wakes=tombstone_delivery_wakes,
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
            raise history_response_error(completed)
        history_import.remote_acknowledged_at = datetime.now(UTC)
        history_import.ack_error = None
        await session.commit()
        return imported
    except Exception as exc:
        await session.rollback()
        failure: Exception = user_facing_history_error(exc)
        if isinstance(exc, FederationReplicaQuotaExceeded):
            await mark_replica_quota_paused(
                session,
                settings,
                guild.id,
                guild.origin_domain,
                exc,
            )
        failed = await session.get(
            GuildHistoryImport,
            (history_import.export_id, history_import.export_domain),
        )
        if failed is not None and failed.status != "completed":
            failed.status = "failed"
            failed.error = str(failure)[:500]
            await session.commit()
        if failure is exc:
            raise
        raise failure from exc
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
