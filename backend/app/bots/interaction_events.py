from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import structlog
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.events import (
    interaction_response_dispatch_expired,
    publish_ephemeral,
    user_topic,
)
from app.core.base64url import decode_base64url
from app.core.settings import Settings
from app.db.bot_models import (
    BotInteraction,
    BotInteractionResponse,
    InteractionDispatchOutbox,
)
from app.db.models import FederationEvent, User
from app.federation.events import build_envelope, queue_event
from app.federation.network import FederationNetworkError, normalize_domain

INTERACTION_RESPONSE_EVENT = "bot.interaction.response"
INTERACTION_RESPONSE_CALLBACK_TYPES = frozenset({4, 7, 8, 9})
INTERACTION_RESPONSE_EVENT_FIELDS = frozenset(
    {
        "application_ref",
        "authority_domain",
        "autocomplete_generation",
        "callback_type",
        "channel_ref",
        "data",
        "deleted_at",
        "ephemeral",
        "expires_at",
        "interaction_id",
        "interaction_ref",
        "invoker_ref",
        "message_ref",
        "operation",
        "response_grant_id",
        "response_id",
        "response_ref",
        "revision",
        "sequence",
        "user_ref",
    }
)
MAX_SIGNED_BIGINT = (1 << 63) - 1
log = structlog.get_logger()


def _qualified_ref(identifier: int, authority: str) -> str:
    return f"{identifier}@{authority}"


def _canonical_positive_id(value: object) -> tuple[str, int]:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
    ):
        raise ValueError("identifier is not canonical")
    parsed = int(value)
    if not 1 <= parsed <= MAX_SIGNED_BIGINT or str(parsed) != value:
        raise ValueError("identifier is outside the accepted range")
    return value, parsed


def _canonical_qualified_ref(value: object) -> tuple[str, int, str]:
    if not isinstance(value, str):
        raise ValueError("qualified identifier is invalid")
    raw_id, raw_domain = value.rsplit("@", 1)
    identifier, parsed = _canonical_positive_id(raw_id)
    domain = normalize_domain(raw_domain)
    if raw_domain != domain or value != _qualified_ref(parsed, domain):
        raise ValueError("qualified identifier is not canonical")
    return identifier, parsed, domain


def _canonical_optional_positive_id(value: object) -> int | None:
    if value is None:
        return None
    return _canonical_positive_id(value)[1]


def _valid_response_grant(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 43:
        return False
    try:
        decoded = decode_base64url(value, size=32)
    except ValueError:
        return False
    return bool(decoded)


def private_attachment_path(
    interaction: BotInteraction,
    stored: BotInteractionResponse,
    attachment: dict[str, object],
) -> str | None:
    attachment_id = attachment.get("id")
    attachment_domain = attachment.get("origin_domain")
    if not isinstance(attachment_id, str) or not isinstance(attachment_domain, str):
        return None
    authority = interaction.channel_domain
    return (
        f"/api/v1/interactions/{interaction.id}@{authority}/responses/"
        f"{stored.id}@{authority}/attachments/{attachment_id}@{attachment_domain}"
    )


def interaction_response_event_payload(
    interaction: BotInteraction,
    stored: BotInteractionResponse,
    operation: Literal["CREATE", "UPDATE", "DELETE"],
) -> dict[str, object]:
    authority = interaction.channel_domain
    # Tombstones carry identity and sequence only.  Re-emitting the previous
    # private body (especially attachment URLs or modal fields) defeats both
    # deletion and the bounded interaction-retention contract.
    data = {} if operation == "DELETE" else dict(stored.payload)
    if isinstance(data.get("e2ee"), dict):
        encrypted_data: dict[str, object] = {
            "e2ee": data["e2ee"],
            "attachments": (
                list(data["attachments"]) if isinstance(data.get("attachments"), list) else []
            ),
        }
        # These values contain no response plaintext. They are the exact
        # server-side replay fence clients must echo for an encrypted control.
        for key in ("view_version", "view_expires_at", "view_persistent"):
            if key in data:
                encrypted_data[key] = data[key]
        data = encrypted_data
    raw_attachments = data.get("attachments")
    if isinstance(raw_attachments, list):
        attachments: list[object] = []
        for raw in raw_attachments:
            if not isinstance(raw, dict):
                attachments.append(raw)
                continue
            rendered = {str(key): value for key, value in raw.items()}
            private_path = private_attachment_path(interaction, stored, rendered)
            if private_path is not None:
                rendered["private_media_url"] = private_path
            attachments.append(rendered)
        data["attachments"] = attachments
    return {
        "authority_domain": authority,
        "interaction_id": str(interaction.id),
        "interaction_ref": _qualified_ref(interaction.id, authority),
        "response_id": str(stored.id),
        "response_ref": _qualified_ref(stored.id, authority),
        "user_ref": _qualified_ref(interaction.user_id, interaction.user_domain),
        "invoker_ref": _qualified_ref(interaction.user_id, interaction.user_domain),
        "channel_ref": _qualified_ref(interaction.channel_id, interaction.channel_domain),
        "application_ref": _qualified_ref(
            interaction.application_id,
            interaction.application_domain,
        ),
        "response_grant_id": interaction.response_grant_id,
        "sequence": stored.sequence,
        "callback_type": stored.response_type,
        "ephemeral": stored.ephemeral,
        "data": data,
        "message_ref": (
            _qualified_ref(stored.message_id, stored.message_domain)
            if stored.message_id is not None and stored.message_domain is not None
            else None
        ),
        "autocomplete_generation": (
            str(interaction.autocomplete_generation)
            if interaction.autocomplete_generation is not None
            else None
        ),
        "revision": str(int(getattr(stored, "revision", 1) or 1)),
        "operation": operation,
        "expires_at": interaction.expires_at.isoformat(),
        "deleted_at": stored.deleted_at.isoformat() if stored.deleted_at is not None else None,
    }


def authority_attested_interaction_response(
    event_type: str,
    content: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> tuple[int, int, int, str] | None:
    """Recognize only an exact C-signed response for its remote invoker."""

    if (
        event_type != INTERACTION_RESPONSE_EVENT
        or not isinstance(content, dict)
        or set(content) != INTERACTION_RESPONSE_EVENT_FIELDS
    ):
        return None
    try:
        raw_authority = content["authority_domain"]
        if not isinstance(raw_authority, str):
            return None
        authority = normalize_domain(raw_authority)
        expected = normalize_domain(expected_authority)
        actor_id, parsed_actor_id = _canonical_positive_id(actor[0])
        actor_domain = normalize_domain(actor[1])
        interaction_id, parsed_interaction_id, interaction_authority = _canonical_qualified_ref(
            content["interaction_ref"]
        )
        response_id, parsed_response_id, response_authority = _canonical_qualified_ref(
            content["response_ref"]
        )
        user_id, parsed_user_id, user_authority = _canonical_qualified_ref(content["user_ref"])
        _, _, channel_authority = _canonical_qualified_ref(content["channel_ref"])
        _, _, application_authority = _canonical_qualified_ref(content["application_ref"])
        _, parsed_revision = _canonical_positive_id(content["revision"])
        autocomplete_generation = _canonical_optional_positive_id(
            content["autocomplete_generation"]
        )
        expires_at = datetime.fromisoformat(content["expires_at"])
        deleted_at = (
            datetime.fromisoformat(content["deleted_at"])
            if content["deleted_at"] is not None
            else None
        )
    except (TypeError, ValueError, FederationNetworkError):
        return None
    operation = content["operation"]
    callback_type = content["callback_type"]
    sequence = content["sequence"]
    raw_message_ref = content["message_ref"]
    if raw_message_ref is not None:
        try:
            _, _, message_authority = _canonical_qualified_ref(raw_message_ref)
        except (TypeError, ValueError, FederationNetworkError):
            return None
        if message_authority != authority:
            return None
    create = operation == "CREATE"
    update = operation == "UPDATE"
    delete = operation == "DELETE"
    if (
        raw_authority != authority
        or authority != expected
        or interaction_authority != authority
        or response_authority != authority
        or channel_authority != authority
        or not application_authority
        or (parsed_user_id, user_authority) != (parsed_actor_id, actor_domain)
        or user_id != actor_id
        or content.get("invoker_ref") != content.get("user_ref")
        or content.get("interaction_id") != interaction_id
        or content.get("response_id") != response_id
        or isinstance(callback_type, bool)
        or callback_type not in INTERACTION_RESPONSE_CALLBACK_TYPES
        or not isinstance(content["ephemeral"], bool)
        or not (create or update or delete)
        or (create and parsed_revision != 1)
        or ((update or delete) and parsed_revision <= 1)
        or expires_at.tzinfo is None
        or (deleted_at is None) != (not delete)
        or (delete and content["data"] != {})
        or isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 0 <= sequence <= MAX_SIGNED_BIGINT
        or not isinstance(content["data"], dict)
        or not _valid_response_grant(content["response_grant_id"])
        or (callback_type == 8) != (autocomplete_generation is not None)
        or (content["ephemeral"] and raw_message_ref is not None)
        or (callback_type in {8, 9} and raw_message_ref is not None)
    ):
        return None
    return parsed_interaction_id, parsed_response_id, parsed_revision, cast(str, operation)


async def queue_interaction_response_event(
    session: AsyncSession,
    settings: Settings,
    interaction: BotInteraction,
    stored: BotInteractionResponse,
    operation: Literal["CREATE", "UPDATE", "DELETE"],
) -> str | None:
    """Atomically queue the signed private projection when its user is remote."""

    if operation != "CREATE":
        stored.revision = int(getattr(stored, "revision", 1) or 1) + 1
    if interaction.user_domain == settings.domain:
        session.add(
            InteractionDispatchOutbox(
                user_id=interaction.user_id,
                user_domain=interaction.user_domain,
                interaction_id=interaction.id,
                interaction_domain=interaction.channel_domain,
                response_id=stored.id,
                response_domain=interaction.channel_domain,
                revision=int(getattr(stored, "revision", 1) or 1),
                operation=operation,
                expires_at=interaction.expires_at,
            )
        )
        return None
    actor = await session.get(User, (interaction.user_id, interaction.user_domain))
    if actor is None or actor.account_type != "human":
        raise RuntimeError("interaction invoker disappeared before response relay")
    envelope = await build_envelope(
        session,
        settings,
        INTERACTION_RESPONSE_EVENT,
        actor,
        interaction_response_event_payload(interaction, stored, operation),
        authority_attested_actor=True,
    )
    await queue_event(session, settings, interaction.user_domain, envelope)
    return interaction.user_domain


async def queue_received_interaction_dispatch(
    session: AsyncSession,
    *,
    event_origin_domain: str,
    event_id: str,
    user_id: int,
    user_domain: str,
    interaction_id: int,
    response_id: int,
    revision: int,
    operation: Literal["CREATE", "UPDATE", "DELETE"],
    expires_at: datetime,
) -> None:
    """Reference one retained signed envelope from A's local dispatch outbox."""

    session.add(
        InteractionDispatchOutbox(
            user_id=user_id,
            user_domain=user_domain,
            interaction_id=interaction_id,
            interaction_domain=event_origin_domain,
            response_id=response_id,
            response_domain=event_origin_domain,
            revision=revision,
            operation=operation,
            event_origin_domain=event_origin_domain,
            event_id=event_id,
            expires_at=expires_at,
        )
    )


def _dispatch_retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(60, 2 ** min(attempts, 6)))


async def interaction_dispatch_payload(
    session: AsyncSession,
    row: InteractionDispatchOutbox,
    *,
    now: datetime,
) -> dict[str, object] | None:
    """Reconstruct one exact unexpired private projection from durable SQL."""

    if row.expires_at <= now:
        return None
    if row.event_id is not None:
        event = await session.get(
            FederationEvent,
            (row.event_origin_domain, row.event_id),
        )
        envelope = event.envelope if event is not None else None
        content = envelope.get("content") if isinstance(envelope, dict) else None
        actor = envelope.get("actor") if isinstance(envelope, dict) else None
        if (
            event is None
            or event.event_type != INTERACTION_RESPONSE_EVENT
            or event.origin_domain != row.interaction_domain
            or event.expires_at is None
            or event.expires_at <= now
            or not isinstance(content, dict)
            or not isinstance(actor, dict)
        ):
            return None
        attested = authority_attested_interaction_response(
            event.event_type,
            content,
            expected_authority=event.origin_domain,
            actor=(str(actor.get("id", "")), str(actor.get("domain", ""))),
        )
        if attested != (
            row.interaction_id,
            row.response_id,
            row.revision,
            row.operation,
        ):
            return None
        return {str(key): value for key, value in content.items()}
    interaction = await session.get(BotInteraction, row.interaction_id)
    stored = await session.get(BotInteractionResponse, row.response_id)
    if (
        interaction is None
        or stored is None
        or stored.interaction_id != interaction.id
        or (interaction.user_id, interaction.user_domain) != (row.user_id, row.user_domain)
        or interaction.channel_domain != row.interaction_domain
        or row.response_domain != interaction.channel_domain
        or interaction.expires_at != row.expires_at
        or interaction.expires_at <= now
        or int(getattr(stored, "revision", 1) or 1) != row.revision
    ):
        return None
    return interaction_response_event_payload(
        interaction,
        stored,
        cast(Literal["CREATE", "UPDATE", "DELETE"], row.operation),
    )


async def interaction_response_replay_events(
    session: AsyncSession,
    *,
    user_id: int,
    user_domain: str,
    now: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, object]]:
    """Load each response's latest exact state for human Gateway reconnect."""

    if not 1 <= limit <= 1000:
        raise ValueError("interaction response replay limit must be between 1 and 1000")
    current = now or datetime.now(UTC)
    ranked = (
        select(
            InteractionDispatchOutbox.id.label("outbox_id"),
            func.row_number()
            .over(
                partition_by=(
                    InteractionDispatchOutbox.response_id,
                    InteractionDispatchOutbox.response_domain,
                ),
                order_by=(
                    InteractionDispatchOutbox.revision.desc(),
                    InteractionDispatchOutbox.id.desc(),
                ),
            )
            .label("response_rank"),
        )
        .where(
            InteractionDispatchOutbox.user_id == user_id,
            InteractionDispatchOutbox.user_domain == user_domain,
            InteractionDispatchOutbox.expires_at > current,
        )
        .subquery()
    )
    rows = list(
        await session.scalars(
            select(InteractionDispatchOutbox)
            .join(
                ranked,
                ranked.c.outbox_id == InteractionDispatchOutbox.id,
            )
            .where(ranked.c.response_rank == 1)
            .order_by(InteractionDispatchOutbox.id)
            .limit(limit)
        )
    )
    latest: list[tuple[InteractionDispatchOutbox, dict[str, object]]] = []
    for row in rows:
        payload = await interaction_dispatch_payload(session, row, now=current)
        if payload is not None:
            latest.append((row, payload))
    return [
        {
            "t": f"INTERACTION_RESPONSE_{row.operation}",
            "d": payload,
            "ephemeral": True,
        }
        for row, payload in latest
    ]


async def drain_interaction_dispatch_outbox(
    session: AsyncSession,
    redis: Redis,
    *,
    limit: int = 100,
) -> int:
    """Project private responses at least once without copying their bodies."""

    if not 1 <= limit <= 500:
        raise ValueError("interaction dispatch limit must be between 1 and 500")
    now = datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(InteractionDispatchOutbox)
            .where(InteractionDispatchOutbox.next_attempt_at <= now)
            .order_by(InteractionDispatchOutbox.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    delivered = 0
    for row in rows:
        payload = await interaction_dispatch_payload(session, row, now=now)
        if payload is None:
            await session.delete(row)
            continue
        try:
            published = await publish_ephemeral(
                redis,
                user_topic(row.user_domain, row.user_id),
                f"INTERACTION_RESPONSE_{row.operation}",
                payload,
            )
        except Exception:
            published = False
            log.exception(
                "interaction_dispatch_failed",
                outbox_id=row.id,
                response_ref=f"{row.response_id}@{row.response_domain}",
            )
        if published:
            # Keep only metadata until the protocol deadline so reconnect can
            # rebuild state from the exact SQL response or signed event.
            row.next_attempt_at = row.expires_at
            delivered += 1
        else:
            row.attempts += 1
            row.next_attempt_at = now + _dispatch_retry_delay(row.attempts)
    await session.commit()
    return delivered


async def wake_interaction_dispatch_outbox() -> bool:
    """Wake the durable drain; the scheduled sweep remains the fallback."""

    try:
        from app.tasks import interaction_dispatch_outbox_drain
    except Exception:
        log.exception("interaction_dispatch_wake_unavailable")
        return False
    from app.core.task_wake import enqueue_best_effort

    return await enqueue_best_effort(interaction_dispatch_outbox_drain)


async def purge_expired_interaction_response_streams(
    redis: Redis,
    topics: set[str],
    *,
    now: datetime,
) -> int:
    """Physically remove expired private bodies from the Redis projection."""

    removed = 0
    for topic in sorted(topics):
        stream = f"dispatch:stream:{topic}"
        entries = await redis.xrange(stream, min="-", max="+")
        expired_ids: list[int | bytes | str] = []
        for entry_id, fields in entries:
            raw = fields.get("event") if isinstance(fields, dict) else None
            if raw is None and isinstance(fields, dict):
                raw = fields.get(b"event")
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            try:
                event = json.loads(raw) if isinstance(raw, str) else None
            except json.JSONDecodeError:
                event = None
            if isinstance(event, dict) and interaction_response_dispatch_expired(
                event,
                now=now,
            ):
                expired_ids.append(cast(int | bytes | str, entry_id))
        if expired_ids:
            removed += int(await redis.xdel(stream, *expired_ids))
    return removed


async def publish_interaction_response_event(
    redis: Redis,
    interaction: BotInteraction,
    stored: BotInteractionResponse,
    operation: Literal["CREATE", "UPDATE", "DELETE"],
) -> None:
    """Project committed private callback state to the invoking user's topic."""

    if (
        interaction.expires_at <= datetime.now(UTC)
        or interaction.user_domain != interaction.channel_domain
    ):
        # A remote invoker has no authenticated Gateway session on the
        # interaction authority.  Its projection is carried by the durable,
        # signed federation outbox instead.
        return

    await publish_ephemeral(
        redis,
        user_topic(interaction.user_domain, interaction.user_id),
        f"INTERACTION_RESPONSE_{operation}",
        interaction_response_event_payload(interaction, stored, operation),
    )
