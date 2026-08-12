from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import exists, func, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings import Settings
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    FederatedDMRowCharge,
    FederatedDMStorageUsage,
    Instance,
    Message,
    MessageProjection,
    Pin,
    RemoteMediaCache,
)

MESSAGE_MINIMUM_CHARGE = 4_096
ATTACHMENT_MINIMUM_CHARGE = 4_096
PROJECTION_MINIMUM_CHARGE = 2_048
DM_QUOTA_ERROR_CODE = "FEDERATED_DM_STORAGE_QUOTA_EXCEEDED"
FEDERATION_DM_QUOTA_ERROR_CODE = "KAED_FED_DM_STORAGE_QUOTA_EXCEEDED"
DM_HISTORY_TRUNCATED_CODE = "FEDERATED_DM_HISTORY_TRUNCATED"
DM_HISTORY_PAGE_CAPABILITY = "dm-history-page/1"
MAX_EVICTIONS_PER_ADMISSION = 5_000


class FederatedDMQuotaExceeded(ValueError):
    """A durable cross-instance DM high-water mark was reached."""

    def __init__(self, scope: str, resource: str, used: int, limit: int) -> None:
        self.scope = scope
        self.resource = resource
        self.used = used
        self.limit = limit
        super().__init__(f"federated DM {scope} {resource} quota exceeded ({used} > {limit})")

    def detail(self, *, federation: bool = False) -> dict[str, object]:
        return {
            "code": FEDERATION_DM_QUOTA_ERROR_CODE if federation else DM_QUOTA_ERROR_CODE,
            "scope": self.scope,
            "resource": self.resource,
            "used": self.used,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class FederatedDMStorageDelta:
    message_rows: int
    message_bytes: int
    attachment_rows: int
    attachment_bytes: int
    projection_rows: int
    projection_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.message_bytes + self.attachment_bytes + self.projection_bytes


@dataclass(frozen=True, slots=True)
class FederatedDMPruneResult:
    evicted_messages: int = 0
    evicted_bytes: int = 0


def dm_history_metadata(
    conversation: DMConversation | None,
    *,
    local_domain: str,
    remote_available: bool = False,
) -> dict[str, object]:
    """Describe whether older history is durable locally or fetched on demand."""

    rolling = conversation is not None and conversation.authority_domain != local_domain
    truncated = bool(conversation is not None and conversation.history_truncated)
    oldest = (
        {
            "id": str(conversation.history_cache_start_id),
            "origin_domain": conversation.history_cache_start_domain,
        }
        if conversation is not None
        and conversation.history_cache_start_id is not None
        and conversation.history_cache_start_domain is not None
        else None
    )
    return {
        "history_truncated": truncated,
        "history_retention": "rolling_replica_cache" if rolling else "authoritative",
        "history_source": (
            conversation.authority_domain if conversation is not None else local_domain
        ),
        # A retained-cache boundary is not the beginning of the conversation.
        # Clients keep pagination enabled and the API obtains older pages from
        # the signed authority without persisting a second durable copy.
        "history_remote_available": rolling and truncated and remote_available,
        "oldest_available_message_ref": oldest,
        "history_degraded_code": DM_HISTORY_TRUNCATED_CODE if truncated else None,
    }


def opaque_dm_history_ref_allowed(
    conversation: DMConversation | None,
    reference: tuple[int, str],
    *,
    participant_domains: set[str],
    local_domain: str,
    remote_available: bool,
) -> bool:
    """Allow a missing reference only inside an evicted replica prefix.

    Local-authored rows are durable and therefore must never become opaque. The
    capability check is equally important during rolling upgrades: a replica
    may retain an opaque pointer only while the authority promises stateless
    access to complete older history.
    """

    if (
        conversation is None
        or conversation.authority_domain == local_domain
        or not conversation.history_truncated
        or not remote_available
        or conversation.history_truncated_before_id is None
        or conversation.history_truncated_before_domain is None
    ):
        return False
    message_id, message_domain = reference
    if message_domain == local_domain or message_domain not in participant_domains:
        return False
    return (message_id, message_domain) <= (
        conversation.history_truncated_before_id,
        conversation.history_truncated_before_domain,
    )


def _text_bytes(value: object) -> int:
    return len(value.encode("utf-8")) if isinstance(value, str) else 0


def _json_bytes(value: object) -> int:
    if value is None:
        return 0
    # PostgreSQL's JSONB textual form may contain whitespace that compact JSON
    # omits. Double the wire representation and add slack so pre-admission is
    # conservative relative to the trigger's pg_column_size charge.
    return (
        2 * len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 64
    )


def _attachment_charge(attachment: Attachment | dict[str, Any]) -> int:
    filename: object
    content_type: object
    origin: object
    object_key: object
    blurhash: object
    variants: object
    if isinstance(attachment, Attachment):
        filename = attachment.filename
        content_type = attachment.content_type
        origin = attachment.origin_domain
        object_key = attachment.object_key
        blurhash = attachment.blurhash
        variants = attachment.variants
    else:
        filename = attachment.get("filename")
        content_type = attachment.get("content_type")
        origin = attachment.get("origin_domain")
        object_key = None
        blurhash = attachment.get("blurhash")
        variants = attachment.get("variants", {})
    estimated = (
        384
        + _text_bytes(filename)
        + _text_bytes(content_type)
        + _text_bytes(origin)
        + _text_bytes(object_key)
        + _text_bytes(blurhash)
        + _json_bytes(variants)
    )
    return max(ATTACHMENT_MINIMUM_CHARGE, estimated)


def dm_message_storage_delta(
    *,
    content: str | None,
    e2ee: dict[str, Any] | None,
    mention_user_refs: list[dict[str, Any]],
    attachments: list[Attachment] | list[dict[str, Any]],
    client_nonce: str | None = None,
) -> FederatedDMStorageDelta:
    message_bytes = max(
        MESSAGE_MINIMUM_CHARGE,
        512
        + _text_bytes(content)
        + _json_bytes(e2ee)
        + _json_bytes(mention_user_refs)
        + _text_bytes(client_nonce),
    )
    attachment_bytes = sum(_attachment_charge(item) for item in attachments)
    return FederatedDMStorageDelta(
        message_rows=1,
        message_bytes=message_bytes,
        attachment_rows=len(attachments),
        attachment_bytes=attachment_bytes,
        projection_rows=1,
        projection_bytes=PROJECTION_MINIMUM_CHARGE,
    )


async def lock_federated_dm_authority(session: AsyncSession, authority_domain: str) -> None:
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(f"kaede-federated-dm:{authority_domain}", 0)
            )
        )
    )


async def admit_federated_dm_conversation(
    session: AsyncSession,
    settings: Settings,
    *,
    authority_domain: str,
    pair_key: str,
    participant_domains: set[str],
) -> bool:
    """Admit a new cross-instance conversation before its row is inserted."""

    if len(participant_domains) < 2:
        return False
    remote_origins = participant_domains - {settings.domain}
    if len(remote_origins) != 1:
        raise ValueError("federated direct conversation must have exactly one remote origin")
    remote_origin = next(iter(remote_origins))
    await lock_federated_dm_authority(session, authority_domain)
    if remote_origin != authority_domain:
        await lock_federated_dm_authority(session, remote_origin)
    existing = await session.scalar(
        select(DMConversation.id).where(DMConversation.pair_key == pair_key)
    )
    if existing is not None:
        return True
    retained_by_authority = int(
        await session.scalar(
            select(func.count())
            .select_from(FederatedDMStorageUsage)
            .where(FederatedDMStorageUsage.authority_domain == authority_domain)
        )
        or 0
    )
    if retained_by_authority >= settings.federation_dm_max_conversations_per_authority:
        raise FederatedDMQuotaExceeded(
            "authority",
            "conversations",
            retained_by_authority + 1,
            settings.federation_dm_max_conversations_per_authority,
        )
    retained_by_origin = int(
        await session.scalar(
            select(func.count())
            .select_from(FederatedDMStorageUsage)
            .where(FederatedDMStorageUsage.remote_origin_domain == remote_origin)
        )
        or 0
    )
    if retained_by_origin >= settings.federation_dm_max_conversations_per_remote_origin:
        raise FederatedDMQuotaExceeded(
            "remote origin",
            "conversations",
            retained_by_origin + 1,
            settings.federation_dm_max_conversations_per_remote_origin,
        )
    return True


async def register_federated_dm_conversation(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    *,
    participant_domains: set[str],
) -> FederatedDMStorageUsage | None:
    """Create the ledger before any message-bearing child row can be inserted."""

    if len(participant_domains) < 2:
        return None
    remote_origins = participant_domains - {settings.domain}
    if len(remote_origins) != 1:
        raise ValueError("federated direct conversation must have exactly one remote origin")
    await session.execute(
        pg_insert(FederatedDMStorageUsage)
        .values(
            conversation_id=conversation.id,
            conversation_domain=conversation.origin_domain,
            authority_domain=conversation.authority_domain,
            remote_origin_domain=next(iter(remote_origins)),
        )
        .on_conflict_do_nothing(index_elements=["conversation_id", "conversation_domain"])
    )
    return await session.get(
        FederatedDMStorageUsage,
        (conversation.id, conversation.origin_domain),
    )


async def _usage_for_conversation(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
) -> FederatedDMStorageUsage | None:
    usage = await session.get(
        FederatedDMStorageUsage,
        (conversation.id, conversation.origin_domain),
    )
    if usage is not None:
        return usage
    participant_domains = set(
        await session.scalars(
            select(DMParticipant.user_domain).where(
                DMParticipant.conversation_id == conversation.id,
                DMParticipant.conversation_domain == conversation.origin_domain,
            )
        )
    )
    if len(participant_domains) < 2:
        return None
    await admit_federated_dm_conversation(
        session,
        settings,
        authority_domain=conversation.authority_domain,
        pair_key=conversation.pair_key,
        participant_domains=participant_domains,
    )
    return await register_federated_dm_conversation(
        session,
        settings,
        conversation,
        participant_domains=participant_domains,
    )


async def _dm_usage_totals(
    session: AsyncSession,
    *,
    authority_domain: str,
    remote_origin_domain: str,
) -> tuple[int, int, int, int]:
    authority = (
        await session.execute(
            select(
                func.coalesce(func.sum(FederatedDMStorageUsage.message_rows), 0),
                func.coalesce(func.sum(FederatedDMStorageUsage.total_bytes), 0),
            ).where(FederatedDMStorageUsage.authority_domain == authority_domain)
        )
    ).one()
    origin = (
        await session.execute(
            select(
                func.coalesce(func.sum(FederatedDMStorageUsage.message_rows), 0),
                func.coalesce(func.sum(FederatedDMStorageUsage.total_bytes), 0),
            ).where(FederatedDMStorageUsage.remote_origin_domain == remote_origin_domain)
        )
    ).one()
    return int(authority[0]), int(authority[1]), int(origin[0]), int(origin[1])


async def _replaceable_replica_usage(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
) -> tuple[int, int]:
    """Return soft-target usage for remote-authored replaceable rows only."""

    row = (
        await session.execute(
            select(
                func.count().filter(FederatedDMRowCharge.category == "message"),
                func.coalesce(func.sum(FederatedDMRowCharge.charge_bytes), 0),
            ).where(
                FederatedDMRowCharge.conversation_id == conversation.id,
                FederatedDMRowCharge.conversation_domain == conversation.origin_domain,
                FederatedDMRowCharge.row_domain != settings.domain,
            )
        )
    ).one()
    return int(row[0]), int(row[1])


async def dm_authority_history_available(
    session: AsyncSession,
    conversation: DMConversation | None,
    *,
    local_domain: str,
) -> bool:
    if conversation is None or conversation.authority_domain == local_domain:
        return False
    authority = await session.get(Instance, conversation.authority_domain)
    return bool(
        authority is not None and DM_HISTORY_PAGE_CAPABILITY in (authority.capabilities or [])
    )


def _larger_ref(
    current_id: int | None,
    current_domain: str | None,
    candidate_id: int,
    candidate_domain: str,
) -> tuple[int, str]:
    current = (current_id, current_domain or "") if current_id is not None else None
    candidate = (candidate_id, candidate_domain)
    return candidate if current is None or candidate > current else (current[0], current[1])


async def _refresh_replica_cache_boundaries(
    session: AsyncSession,
    conversation_refs: set[tuple[int, str]],
    *,
    incoming_ref: tuple[int, str] | None = None,
) -> None:
    for conversation_id, conversation_domain in conversation_refs:
        conversation = await session.get(
            DMConversation,
            (conversation_id, conversation_domain),
            populate_existing=True,
        )
        if conversation is None or not conversation.history_truncated:
            continue
        oldest_row = (
            await session.execute(
                select(Message.id, Message.origin_domain)
                .where(
                    Message.channel_id == conversation.id,
                    Message.channel_domain == conversation.origin_domain,
                    (
                        tuple_(Message.id, Message.origin_domain)
                        > (
                            conversation.history_truncated_before_id,
                            conversation.history_truncated_before_domain,
                        )
                    ),
                )
                .order_by(Message.id, Message.origin_domain)
                .limit(1)
            )
        ).first()
        oldest = (int(oldest_row[0]), str(oldest_row[1])) if oldest_row is not None else None
        if (
            oldest is None
            and incoming_ref is not None
            and (
                conversation_id,
                conversation_domain,
            )
            == (
                conversation.id,
                conversation.origin_domain,
            )
        ):
            oldest = incoming_ref
        conversation.history_cache_start_id = int(oldest[0]) if oldest is not None else None
        conversation.history_cache_start_domain = str(oldest[1]) if oldest is not None else None


async def _eligible_replica_messages(
    session: AsyncSession,
    settings: Settings,
    *,
    conversation: DMConversation | None,
    authority_domain: str,
    limit: int,
    protected_refs: set[tuple[int, str]] | None = None,
) -> list[Message]:
    newer_message = Message.__table__.alias("newer_dm_message")
    conditions = [
        DMConversation.authority_domain == authority_domain,
        DMConversation.authority_domain != settings.domain,
        # Local-authored rows are durable user-owned data. A remote authority
        # acknowledgement cannot prove it retained them honestly, so rolling
        # cache eviction is limited to replaceable remote-authored replicas.
        Message.origin_domain != settings.domain,
        # The newest row anchors Channel.last_message and must remain resident.
        or_(
            Channel.last_message_id.is_(None),
            tuple_(Message.id, Message.origin_domain)
            != tuple_(Channel.last_message_id, Channel.last_message_domain),
        ),
        # A pin is explicit user intent. It remains locally available even when
        # ordinary history around it is served transparently by the authority.
        ~exists().where(
            Pin.message_id == Message.id,
            Pin.message_domain == Message.origin_domain,
            Pin.channel_id == Message.channel_id,
            Pin.channel_domain == Message.channel_domain,
        ),
        # Always retain the actual newest row even if the denormalized channel
        # cursor is temporarily null or stale.
        exists().where(
            newer_message.c.channel_id == Message.channel_id,
            newer_message.c.channel_domain == Message.channel_domain,
            tuple_(
                newer_message.c.id,
                newer_message.c.origin_domain,
            )
            > tuple_(Message.id, Message.origin_domain),
        ),
        # Mention/unread/push projection must finish before its source can roll
        # out of the cache.
        ~exists().where(
            MessageProjection.message_id == Message.id,
            MessageProjection.message_domain == Message.origin_domain,
            MessageProjection.processed_at.is_(None),
        ),
        # Locally uploaded bytes are authoritative on this instance. Preserve
        # their owning message so signed media authorization remains possible.
        ~exists().where(
            Attachment.message_id == Message.id,
            Attachment.message_domain == Message.origin_domain,
            Attachment.origin_domain == settings.domain,
            Attachment.deleted_at.is_(None),
        ),
    ]
    if conversation is not None:
        conditions.extend(
            [
                Message.channel_id == conversation.id,
                Message.channel_domain == conversation.origin_domain,
            ]
        )
    if protected_refs:
        conditions.append(~tuple_(Message.id, Message.origin_domain).in_(protected_refs))
    return list(
        await session.scalars(
            select(Message)
            .join(
                DMConversation,
                (DMConversation.id == Message.channel_id)
                & (DMConversation.origin_domain == Message.channel_domain),
            )
            .join(
                Channel,
                (Channel.id == Message.channel_id)
                & (Channel.origin_domain == Message.channel_domain),
            )
            .where(*conditions)
            .order_by(Message.id, Message.origin_domain)
            .limit(limit)
            .with_for_update(of=Message, skip_locked=True)
        )
    )


async def _eviction_charge_bytes(
    session: AsyncSession,
    messages: list[Message],
) -> dict[tuple[int, str], int]:
    """Return trigger-ledger bytes released by deleting each message.

    Message and projection charges share the message composite reference;
    attachment charges are mapped through their owning message. Keeping this
    as two grouped queries lets pruning choose the exact oldest prefix while
    deleting hundreds of rows with one flush instead of re-running aggregate
    accounting for every individual message.
    """

    refs = {(message.id, message.origin_domain) for message in messages}
    if not refs:
        return {}
    direct_rows = (
        await session.execute(
            select(
                FederatedDMRowCharge.row_id,
                FederatedDMRowCharge.row_domain,
                func.coalesce(func.sum(FederatedDMRowCharge.charge_bytes), 0),
            )
            .where(
                FederatedDMRowCharge.table_name.in_(("messages", "message_projections")),
                tuple_(
                    FederatedDMRowCharge.row_id,
                    FederatedDMRowCharge.row_domain,
                ).in_(refs),
            )
            .group_by(FederatedDMRowCharge.row_id, FederatedDMRowCharge.row_domain)
        )
    ).all()
    charges = {(int(row[0]), str(row[1])): int(row[2]) for row in direct_rows}
    attachment_rows = (
        await session.execute(
            select(
                Attachment.message_id,
                Attachment.message_domain,
                func.coalesce(func.sum(FederatedDMRowCharge.charge_bytes), 0),
            )
            .join(
                FederatedDMRowCharge,
                (FederatedDMRowCharge.table_name == "attachments")
                & (FederatedDMRowCharge.row_id == Attachment.id)
                & (FederatedDMRowCharge.row_domain == Attachment.origin_domain),
            )
            .where(tuple_(Attachment.message_id, Attachment.message_domain).in_(refs))
            .group_by(Attachment.message_id, Attachment.message_domain)
        )
    ).all()
    for message_id, message_domain, charge_bytes in attachment_rows:
        reference = (int(message_id), str(message_domain))
        charges[reference] = charges.get(reference, 0) + int(charge_bytes)
    return charges


def _minimal_eviction_prefix(
    candidates: list[Message],
    charges: dict[tuple[int, str], int],
    *,
    message_deficit: int,
    byte_deficit: int,
) -> tuple[list[Message], int]:
    """Choose only the oldest prefix needed to satisfy both deficits."""

    selected: list[Message] = []
    selected_bytes = 0
    for candidate in candidates:
        selected.append(candidate)
        selected_bytes += charges.get(
            (candidate.id, candidate.origin_domain),
            MESSAGE_MINIMUM_CHARGE,
        )
        if len(selected) >= message_deficit and selected_bytes >= byte_deficit:
            break
    return selected, selected_bytes


def _projected_quota_deficit(retained: int, incoming: int, limit: int) -> int:
    """Charge a not-yet-inserted row exactly once against a retained total."""

    return max(0, retained + incoming - limit)


async def _evict_replica_messages(
    session: AsyncSession,
    settings: Settings,
    messages: list[Message],
) -> tuple[int, set[tuple[int, str]]]:
    if not messages:
        return 0, set()
    refs = {(message.id, message.origin_domain) for message in messages}
    remote_attachment_refs = list(
        (
            await session.execute(
                select(Attachment.id, Attachment.origin_domain).where(
                    tuple_(Attachment.message_id, Attachment.message_domain).in_(refs),
                    Attachment.origin_domain != settings.domain,
                )
            )
        ).tuples()
    )
    if remote_attachment_refs:
        await session.execute(
            update(RemoteMediaCache)
            .where(
                tuple_(
                    RemoteMediaCache.attachment_id,
                    RemoteMediaCache.origin_domain,
                ).in_(remote_attachment_refs)
            )
            .values(expires_at=datetime.now(UTC))
        )
    affected: set[tuple[int, str]] = set()
    for message in messages:
        conversation = await session.get(
            DMConversation,
            (message.channel_id, message.channel_domain),
        )
        if conversation is None or conversation.authority_domain == settings.domain:
            raise RuntimeError("rolling DM eviction selected authoritative history")
        boundary_id, boundary_domain = _larger_ref(
            conversation.history_truncated_before_id,
            conversation.history_truncated_before_domain,
            message.id,
            message.origin_domain,
        )
        conversation.history_truncated = True
        conversation.history_truncated_before_id = boundary_id
        conversation.history_truncated_before_domain = boundary_domain
        affected.add((conversation.id, conversation.origin_domain))
        await session.delete(message)
    await session.flush()
    return len(messages), affected


async def prune_federated_dm_replica(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    *,
    incoming_ref: tuple[int, str],
    delta: FederatedDMStorageDelta,
    protected_refs: set[tuple[int, str]] | None = None,
    max_evictions: int = MAX_EVICTIONS_PER_ADMISSION,
) -> FederatedDMPruneResult:
    """Evict oldest replaceable rows on a non-authoritative DM replica.

    The authority remains the complete history source. Pending local delivery,
    local attachment authority, pins, the newest cursor and unfinished message
    projections are never evicted.
    """

    if conversation.authority_domain == settings.domain:
        return FederatedDMPruneResult()
    if not await dm_authority_history_available(
        session, conversation, local_domain=settings.domain
    ):
        # During a rolling upgrade the old authority has no stateless paging
        # endpoint. Keep its replica intact and let the generous hard ceiling
        # produce a clear error rather than making history inaccessible.
        return FederatedDMPruneResult()
    evicted = 0
    evicted_bytes = 0
    affected: set[tuple[int, str]] = set()
    while evicted < max_evictions:
        usage = await session.get(
            FederatedDMStorageUsage,
            (conversation.id, conversation.origin_domain),
            populate_existing=True,
        )
        if usage is None:
            return FederatedDMPruneResult(
                evicted_messages=evicted,
                evicted_bytes=evicted_bytes,
            )
        (
            retained_authority_messages,
            retained_authority_bytes,
            retained_origin_messages,
            retained_origin_bytes,
        ) = await _dm_usage_totals(
            session,
            authority_domain=conversation.authority_domain,
            remote_origin_domain=usage.remote_origin_domain,
        )
        replaceable_messages, replaceable_bytes = await _replaceable_replica_usage(
            session, settings, conversation
        )
        incoming_replaceable = incoming_ref[1] != settings.domain
        incoming_messages = delta.message_rows if incoming_replaceable else 0
        incoming_bytes = delta.total_bytes if incoming_replaceable else 0
        soft_message_deficit = _projected_quota_deficit(
            replaceable_messages,
            incoming_messages,
            settings.federation_dm_replica_cache_messages_per_conversation,
        )
        soft_byte_deficit = _projected_quota_deficit(
            replaceable_bytes,
            incoming_bytes,
            settings.federation_dm_replica_cache_bytes_per_conversation,
        )
        authority_message_deficit = _projected_quota_deficit(
            retained_authority_messages,
            delta.message_rows,
            settings.federation_dm_max_messages_per_authority,
        )
        authority_byte_deficit = _projected_quota_deficit(
            retained_authority_bytes,
            delta.total_bytes,
            settings.federation_dm_max_bytes_per_authority,
        )
        origin_message_deficit = _projected_quota_deficit(
            retained_origin_messages,
            delta.message_rows,
            settings.federation_dm_max_messages_per_remote_origin,
        )
        origin_byte_deficit = _projected_quota_deficit(
            retained_origin_bytes,
            delta.total_bytes,
            settings.federation_dm_max_bytes_per_remote_origin,
        )
        conversation_over = soft_message_deficit > 0 or soft_byte_deficit > 0
        aggregate_over = any(
            value > 0
            for value in (
                authority_message_deficit,
                authority_byte_deficit,
                origin_message_deficit,
                origin_byte_deficit,
            )
        )
        if not conversation_over and not aggregate_over:
            break
        candidates = await _eligible_replica_messages(
            session,
            settings,
            conversation=conversation if conversation_over else None,
            authority_domain=conversation.authority_domain,
            limit=min(500, max_evictions - evicted),
            protected_refs=protected_refs,
        )
        if not candidates:
            break
        charges = await _eviction_charge_bytes(session, candidates)
        message_deficit = max(
            soft_message_deficit,
            authority_message_deficit,
            origin_message_deficit,
        )
        byte_deficit = max(
            soft_byte_deficit,
            authority_byte_deficit,
            origin_byte_deficit,
        )
        selected, selected_bytes = _minimal_eviction_prefix(
            candidates,
            charges,
            message_deficit=message_deficit,
            byte_deficit=byte_deficit,
        )
        removed, changed = await _evict_replica_messages(session, settings, selected)
        evicted += removed
        evicted_bytes += selected_bytes
        affected.update(changed)
    if affected:
        await _refresh_replica_cache_boundaries(
            session,
            affected,
            incoming_ref=incoming_ref,
        )
    return FederatedDMPruneResult(
        evicted_messages=evicted,
        evicted_bytes=evicted_bytes,
    )


async def sweep_federated_dm_replica_cache(
    session: AsyncSession,
    settings: Settings,
    *,
    conversation_limit: int = 10,
    evictions_per_conversation: int = 500,
) -> set[tuple[int, str]]:
    """Converge inactive replicas after settings reductions.

    Admission handles the hot path. This bounded sweep repairs existing or
    inactive replicas without one scheduler transaction monopolizing the DB.
    """

    replaceable = (
        select(
            FederatedDMRowCharge.conversation_id.label("conversation_id"),
            FederatedDMRowCharge.conversation_domain.label("conversation_domain"),
            func.count().filter(FederatedDMRowCharge.category == "message").label("message_rows"),
            func.coalesce(func.sum(FederatedDMRowCharge.charge_bytes), 0).label("total_bytes"),
        )
        .where(FederatedDMRowCharge.row_domain != settings.domain)
        .group_by(
            FederatedDMRowCharge.conversation_id,
            FederatedDMRowCharge.conversation_domain,
        )
        .subquery()
    )
    authority_usage = (
        select(
            FederatedDMStorageUsage.authority_domain.label("authority_domain"),
            func.coalesce(func.sum(FederatedDMStorageUsage.message_rows), 0).label("message_rows"),
            func.coalesce(func.sum(FederatedDMStorageUsage.total_bytes), 0).label("total_bytes"),
        )
        .group_by(FederatedDMStorageUsage.authority_domain)
        .subquery()
    )
    origin_usage = (
        select(
            FederatedDMStorageUsage.remote_origin_domain.label("remote_origin_domain"),
            func.coalesce(func.sum(FederatedDMStorageUsage.message_rows), 0).label("message_rows"),
            func.coalesce(func.sum(FederatedDMStorageUsage.total_bytes), 0).label("total_bytes"),
        )
        .group_by(FederatedDMStorageUsage.remote_origin_domain)
        .subquery()
    )
    candidate_refs = list(
        (
            await session.execute(
                select(
                    DMConversation.id,
                    DMConversation.origin_domain,
                    DMConversation.authority_domain,
                )
                .join(
                    replaceable,
                    (replaceable.c.conversation_id == DMConversation.id)
                    & (replaceable.c.conversation_domain == DMConversation.origin_domain),
                )
                .join(
                    FederatedDMStorageUsage,
                    (FederatedDMStorageUsage.conversation_id == DMConversation.id)
                    & (FederatedDMStorageUsage.conversation_domain == DMConversation.origin_domain),
                )
                .join(
                    authority_usage,
                    authority_usage.c.authority_domain == DMConversation.authority_domain,
                )
                .join(
                    origin_usage,
                    origin_usage.c.remote_origin_domain
                    == FederatedDMStorageUsage.remote_origin_domain,
                )
                .join(Instance, Instance.domain == DMConversation.authority_domain)
                .where(
                    DMConversation.authority_domain != settings.domain,
                    Instance.capabilities.contains([DM_HISTORY_PAGE_CAPABILITY]),
                    (
                        replaceable.c.message_rows
                        > settings.federation_dm_replica_cache_messages_per_conversation
                    )
                    | (
                        replaceable.c.total_bytes
                        > settings.federation_dm_replica_cache_bytes_per_conversation
                    )
                    | (
                        authority_usage.c.message_rows
                        > settings.federation_dm_max_messages_per_authority
                    )
                    | (
                        authority_usage.c.total_bytes
                        > settings.federation_dm_max_bytes_per_authority
                    )
                    | (
                        origin_usage.c.message_rows
                        > settings.federation_dm_max_messages_per_remote_origin
                    )
                    | (
                        origin_usage.c.total_bytes
                        > settings.federation_dm_max_bytes_per_remote_origin
                    ),
                )
                .order_by(
                    replaceable.c.total_bytes.desc(),
                    replaceable.c.message_rows.desc(),
                    DMConversation.id,
                    DMConversation.origin_domain,
                )
                .limit(conversation_limit)
            )
        ).tuples()
    )
    changed: set[tuple[int, str]] = set()
    zero = FederatedDMStorageDelta(0, 0, 0, 0, 0, 0)
    for conversation_id, conversation_domain, authority_domain in candidate_refs:
        # Global lock order is authority advisory -> conversation row. Admission
        # uses the same order, avoiding scheduler/admission deadlocks.
        await lock_federated_dm_authority(session, authority_domain)
        conversation = await session.get(
            DMConversation,
            (conversation_id, conversation_domain),
            populate_existing=True,
            with_for_update=True,
        )
        if conversation is None:
            continue
        result = await prune_federated_dm_replica(
            session,
            settings,
            conversation,
            incoming_ref=(0, settings.domain),
            delta=zero,
            max_evictions=evictions_per_conversation,
        )
        if result.evicted_messages:
            changed.add((conversation.id, conversation.origin_domain))
    return changed


async def admit_federated_dm_message(
    session: AsyncSession,
    settings: Settings,
    conversation: DMConversation,
    *,
    message_id: int,
    message_domain: str,
    delta: FederatedDMStorageDelta,
    protected_refs: set[tuple[int, str]] | None = None,
) -> bool:
    """Atomically check conversation and authority limits before message insert.

    Returns ``False`` for an already-retained message so idempotent federation
    replay remains possible even after an operator lowers a limit.
    """

    existing = await session.get(Message, (message_id, message_domain))
    if existing is not None:
        return False
    await lock_federated_dm_authority(session, conversation.authority_domain)
    # A concurrent replay can win while this transaction waits for the shared
    # authority lock. Recheck before pruning so one idempotent message cannot
    # evict two cache rows.
    existing = await session.get(Message, (message_id, message_domain), populate_existing=True)
    if existing is not None:
        return False
    usage = await _usage_for_conversation(session, settings, conversation)
    if usage is None:
        return True
    usage = await session.scalar(
        select(FederatedDMStorageUsage)
        .where(
            FederatedDMStorageUsage.conversation_id == conversation.id,
            FederatedDMStorageUsage.conversation_domain == conversation.origin_domain,
        )
        .with_for_update()
    )
    if usage is None:
        raise RuntimeError("federated DM storage ledger disappeared")

    if conversation.authority_domain != settings.domain:
        await prune_federated_dm_replica(
            session,
            settings,
            conversation,
            incoming_ref=(message_id, message_domain),
            delta=delta,
            protected_refs=protected_refs,
        )
        usage = await session.get(
            FederatedDMStorageUsage,
            (conversation.id, conversation.origin_domain),
            populate_existing=True,
        )
        if usage is None:
            raise RuntimeError("federated DM storage ledger disappeared after pruning")

    conversation_messages = int(usage.message_rows) + delta.message_rows
    conversation_bytes = int(usage.total_bytes) + delta.total_bytes
    authority_rows, authority_size, origin_rows, origin_size = await _dm_usage_totals(
        session,
        authority_domain=conversation.authority_domain,
        remote_origin_domain=usage.remote_origin_domain,
    )
    authority_messages = authority_rows + delta.message_rows
    authority_bytes = authority_size + delta.total_bytes
    remote_origin_messages = origin_rows + delta.message_rows
    remote_origin_bytes = origin_size + delta.total_bytes
    limits = (
        (
            "conversation",
            "messages",
            conversation_messages,
            settings.federation_dm_max_messages_per_conversation,
        ),
        (
            "conversation",
            "bytes",
            conversation_bytes,
            settings.federation_dm_max_bytes_per_conversation,
        ),
        (
            "authority",
            "messages",
            authority_messages,
            settings.federation_dm_max_messages_per_authority,
        ),
        (
            "authority",
            "bytes",
            authority_bytes,
            settings.federation_dm_max_bytes_per_authority,
        ),
        (
            "remote origin",
            "messages",
            remote_origin_messages,
            settings.federation_dm_max_messages_per_remote_origin,
        ),
        (
            "remote origin",
            "bytes",
            remote_origin_bytes,
            settings.federation_dm_max_bytes_per_remote_origin,
        ),
    )
    for scope, resource, used, limit in limits:
        if used > limit:
            raise FederatedDMQuotaExceeded(scope, resource, used, limit)
    return True
