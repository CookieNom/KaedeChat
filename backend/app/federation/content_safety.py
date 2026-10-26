"""Safety projections carried by an already authenticated conversation authority.

These helpers do not authenticate envelopes. Callers must bind the signed event
or history response to the guild/DM authority before using them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.e2ee import (
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_message_encryption_policy,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_HAS_SNAPSHOT,
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_IS_CROSSPOST,
    MESSAGE_FLAG_IS_VOICE_MESSAGE,
)
from app.core.settings import Settings
from app.db.models import Attachment, Channel, Message, MessageProjection, MessageView, Poll, User

REASONS = {
    "pending_review": "This content is temporarily unavailable pending a child-safety review.",
    "removed": "This content was removed following a child-safety review.",
}


def safety_state(raw: dict[str, Any]) -> str | None:
    value = raw.get("safety_state")
    if value is not None and (not isinstance(value, str) or value not in REASONS):
        raise ValueError("invalid authoritative content safety state")
    return value


def safety_timestamp(raw: dict[str, Any]) -> datetime | None:
    value = raw.get("safety_updated_at")
    if value is None:
        return None
    try:
        timestamp = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("invalid safety transition timestamp") from exc
    if timestamp.tzinfo is None or timestamp > datetime.now(UTC) + timedelta(seconds=60):
        raise ValueError("invalid safety transition timestamp")
    return timestamp


def safety_projection(raw: dict[str, Any]) -> dict[str, Any]:
    """Never relay original bodies alongside an authority's safety marker."""
    state = safety_state(raw)
    safety_timestamp(raw)
    if state is None:
        return dict(raw)
    flags = raw.get("flags", 0)
    if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
        raise ValueError("invalid safety placeholder flags")
    result = dict(raw)
    result.update(
        safety_state=state,
        safety_locked=True,
        safety_reason=REASONS[state],
        content=REASONS[state],
        e2ee=None,
        embeds=[],
        components=[],
        sticker_items=[],
        attachments=[],
        message_snapshots=[],
        forward_snapshot=None,
        forwarded_message_id=None,
        forwarded_message_domain=None,
        forwarded_message_ref=None,
        forwarded_channel_id=None,
        forwarded_channel_domain=None,
        message_reference=None,
        referenced_message_id=None,
        referenced_message_domain=None,
        referenced_message=None,
        poll=None,
        poll_result=None,
        reactions=[],
        poll_votes=[],
        reaction_counts={},
        reacted_emoji=[],
        mention_user_refs=[],
        mention_role_refs=[],
        mention_everyone=False,
        application_id=None,
        application_domain=None,
        interaction_metadata=None,
        view_version=0,
        view_persistent=False,
        view_expires_at=None,
        interaction_integration_type=None,
        interaction_installation_ref=None,
        interaction_installation_revision=None,
        tts=False,
        # The placeholder is ordinary text, including when the original was
        # a poll result, voice message, snapshot, or interactive bot response.
        message_type=0,
        flags=flags
        & ~(
            MESSAGE_FLAG_HAS_SNAPSHOT
            | MESSAGE_FLAG_IS_COMPONENTS_V2
            | MESSAGE_FLAG_IS_CROSSPOST
            | MESSAGE_FLAG_IS_VOICE_MESSAGE
        ),
    )
    return result


def require_unprotected(message: Message) -> None:
    if getattr(message, "safety_state", None):
        raise ValueError("message is protected by a child-safety review")


async def project_safety(
    session: AsyncSession, settings: Settings, message: Message, state: str
) -> None:
    # Lazy import prevents federation -> safety -> federation import cycles.
    from app.safety.enforcement import message_recipients, project_message

    attachments = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
            )
        )
    )
    if state == "removed" and not getattr(message, "safety_state", None):
        from app.safety.service import record_client_event

        await record_client_event(
            session,
            user_refs=await message_recipients(session, message),
            message_refs=[f"{message.id}@{message.origin_domain}"],
            attachment_refs=[f"{item.id}@{item.origin_domain}" for item in attachments],
            state="removed",
            reason="This message was deleted.",
        )
    else:
        transition_at = getattr(message, "safety_updated_at", None) or message.edited_at
        await project_message(
            session, settings, message, attachments, state, safety_updated_at=transition_at
        )


async def apply_safety_update(
    session: AsyncSession,
    settings: Settings,
    channel: Channel,
    message: Message,
    raw: dict[str, Any],
    *,
    event_timestamp_ms: int,
) -> bool:
    """Apply an explicit authority safety transition; ordinary edits return False."""
    state = safety_state(raw)
    current = getattr(message, "safety_state", None)
    release = "safety_state" in raw and state is None and current == "pending_review"
    if state is None and not release:
        require_unprotected(message)
        return False
    if current == "removed" and state != "removed":
        raise ValueError("confirmed content cannot be restored")
    if (
        str(raw.get("id")) != str(message.id)
        or raw.get("origin_domain") != message.origin_domain
        or str(raw.get("channel_id")) != str(channel.id)
        or raw.get("channel_domain") != channel.origin_domain
        or str(raw.get("author_id")) != str(message.author_id)
        or raw.get("author_domain") != message.author_domain
    ):
        raise ValueError("safety update changed message identity")
    try:
        created_at = datetime.fromisoformat(str(raw.get("created_at")))
        edited_at = datetime.fromisoformat(str(raw["edited_at"])) if raw.get("edited_at") else None
    except ValueError as exc:
        raise ValueError("safety update timestamp is invalid") from exc
    event_time = datetime.fromtimestamp(event_timestamp_ms / 1000, UTC)
    transition_at = safety_timestamp(raw) or event_time
    previous_transition = getattr(message, "safety_updated_at", None)
    if (
        created_at != message.created_at
        or transition_at is None
        or transition_at.tzinfo is None
        or transition_at < created_at
        or transition_at > event_time + timedelta(milliseconds=1)
        or (message.edited_at is not None and transition_at < message.edited_at)
        or (
            previous_transition is not None
            and (
                transition_at < previous_transition
                or (transition_at == previous_transition and state != current)
            )
        )
        or (
            edited_at is not None
            and (
                edited_at.tzinfo is None
                or edited_at < created_at
                or edited_at > event_time + timedelta(milliseconds=1)
            )
        )
    ):
        raise ValueError("safety update regressed authoritative state")
    if release:
        if message.deleted_at is not None or raw.get("safety_locked") is not False:
            raise ValueError("invalid safety release")
        from app.chat.message_references import validate_message_reference_projection
        from app.federation.message_content import (
            add_poll_projection,
            stored_poll_matches_projection,
            validate_replicated_rich_projection,
        )
        from app.federation.network import normalize_domain
        from app.federation.replication import database_snowflake, replicate_message_attachments

        content = raw.get("content")
        if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4000):
            raise ValueError("released content is invalid")
        e2ee = validate_e2ee_envelope(raw.get("e2ee"))
        if content is not None and e2ee is not None:
            raise ValueError("released message mixes plaintext and encryption")
        validate_e2ee_message_projection(
            e2ee,
            message_id=message.id,
            message_domain=message.origin_domain,
            # Restoration reuses the original authenticated MLS operation.
            edited=edited_at is not None,
        )
        message_type = raw.get("message_type", message.message_type)
        flags = raw.get("flags", 0)
        if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
            raise ValueError("released message flags are invalid")
        if isinstance(message_type, bool) or not isinstance(message_type, int) or message_type < 0:
            raise ValueError("released message type is invalid")
        rich = validate_replicated_rich_projection(
            raw,
            message_id=message.id,
            message_origin=message.origin_domain,
            message_created_at=message.created_at,
            e2ee=e2ee,
            message_type=message_type,
            label="released safety message",
        )
        raw_attachments = raw.get("attachments", [])
        if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
            raise ValueError("released message attachments are invalid")
        # A dismissal restores an authored historical body, whose MLS epoch
        # may predate a rekey or whose plaintext predates encryption activation.
        validate_message_encryption_policy(
            "e2ee" if e2ee is not None else "plaintext",
            content=content,
            e2ee=e2ee,
            attachment_count=len(raw_attachments),
        )
        message.encryption_policy_generation = (
            int(e2ee["policy_generation"]) if e2ee and e2ee.get("version") == 2 else 0
        )
        message.encryption_epoch = int(e2ee["epoch"]) if e2ee and e2ee.get("version") == 2 else None
        author = await session.get(User, (message.author_id, message.author_domain))
        if author is None:
            raise ValueError("released message author is unavailable")
        await replicate_message_attachments(
            session,
            settings,
            message,
            author,
            raw_attachments,
            allowed_attachment_origins=(
                {author.origin_domain, message.origin_domain}
                if flags & MESSAGE_FLAG_IS_CROSSPOST
                else {author.origin_domain}
            ),
        )
        reply = None
        if raw.get("referenced_message_id") is not None:
            reply = (
                database_snowflake(raw["referenced_message_id"], "released reply"),
                normalize_domain(str(raw.get("referenced_message_domain", ""))),
            )
        message.message_reference = validate_message_reference_projection(
            raw.get("message_reference"),
            message_type=message_type,
            channel_ref=(channel.id, channel.origin_domain),
            guild_ref=(channel.guild_id, channel.guild_domain)
            if channel.guild_id is not None and channel.guild_domain is not None
            else None,
            referenced_message_ref=reply,
            forwarded_message_ref=rich.forwarded_ref,
            forwarded_channel_ref=rich.forwarded_channel_ref,
            has_forward_snapshot=rich.forward_snapshot is not None or rich.has_encrypted_forward,
            is_crosspost=bool(flags & MESSAGE_FLAG_IS_CROSSPOST),
            label="released message",
        )
        message.referenced_message_id, message.referenced_message_domain = reply or (None, None)
        message.forwarded_message_id, message.forwarded_message_domain = rich.forwarded_ref or (
            None,
            None,
        )
        message.forwarded_channel_id, message.forwarded_channel_domain = (
            rich.forwarded_channel_ref or (None, None)
        )
        message.application_id, message.application_domain = rich.application_ref or (None, None)
        message.interaction_metadata = rich.interaction_metadata
        message.view_version = rich.view_version
        message.message_type = message_type
        tts = raw.get("tts", False)
        if not isinstance(tts, bool):
            raise ValueError("released message TTS marker is invalid")
        message.tts = tts

        def mention_refs(key: str, limit: int) -> list[dict[str, Any]]:
            values = raw.get(key, [])
            if not isinstance(values, list) or len(values) > limit:
                raise ValueError("released mention list is invalid")
            result: list[dict[str, Any]] = []
            seen: set[tuple[int, str]] = set()
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError("released mention reference is invalid")
                identifier = database_snowflake(value.get("id"), "released mention")
                domain = normalize_domain(str(value.get("origin_domain", "")))
                if (identifier, domain) in seen or (
                    key == "mention_role_refs" and domain != channel.guild_domain
                ):
                    raise ValueError("released mention reference is invalid")
                seen.add((identifier, domain))
                result.append({"id": str(identifier), "origin_domain": domain})
            return result

        message.mention_user_refs = mention_refs("mention_user_refs", 5000)
        message.mention_role_refs = mention_refs("mention_role_refs", 100)
        everyone = raw.get("mention_everyone", False)
        if not isinstance(everyone, bool):
            raise ValueError("released everyone mention is invalid")
        message.mention_everyone = everyone
        projection = await session.get(MessageProjection, (message.id, message.origin_domain))
        if projection is None:
            session.add(
                MessageProjection(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    channel_id=message.channel_id,
                    channel_domain=message.channel_domain,
                    mention_user_refs=message.mention_user_refs,
                )
            )
        else:
            projection.mention_user_refs = message.mention_user_refs
        if rich.poll is not None:
            if await session.get(Poll, (message.id, message.origin_domain)) is None:
                add_poll_projection(session, message, rich.poll, created_at=message.created_at)
            elif not await stored_poll_matches_projection(session, message, rich.poll):
                raise ValueError("released poll differs from its original definition")
        if (rich.components or rich.has_encrypted_controls) and rich.application_ref is not None:
            view = await session.get(MessageView, (message.id, message.origin_domain))
            if view is None:
                if (
                    rich.interaction_installation_ref is None
                    or rich.interaction_installation_revision is None
                ):
                    raise ValueError("released interaction lineage is missing")
                session.add(
                    MessageView(
                        message_id=message.id,
                        message_domain=message.origin_domain,
                        application_id=rich.application_ref[0],
                        application_domain=rich.application_ref[1],
                        integration_type=rich.interaction_integration_type,
                        installation_id=rich.interaction_installation_ref[0],
                        installation_domain=rich.interaction_installation_ref[1],
                        installation_revision=rich.interaction_installation_revision,
                        version=rich.view_version,
                        persistent=rich.view_persistent,
                        expires_at=rich.view_expires_at,
                    )
                )
        message.content = content
        message.e2ee = e2ee
        message.embeds = rich.embeds
        message.components = rich.components
        message.sticker_items = rich.sticker_items
        message.forward_snapshot = rich.forward_snapshot
        message.poll_result = rich.poll_result
        flags = raw.get("flags", 0)
        if isinstance(flags, bool) or not isinstance(flags, int) or flags < 0:
            raise ValueError("released message flags are invalid")
        message.flags = flags
        message.safety_state = None
        message.safety_reason = None
        projected_state = "released"
    else:
        if state is None:
            raise RuntimeError("safety transition lost its state")
        message.safety_state = state
        message.safety_reason = REASONS[state]
        if current != state:
            message.content = REASONS[state]
            message.e2ee = None
        message.embeds = []
        message.components = []
        message.sticker_items = []
        message.forward_snapshot = None
        message.poll_result = None
        message.mention_user_refs = []
        message.mention_role_refs = []
        message.mention_everyone = False
        message.flags = int(message.flags or 0) & ~MESSAGE_FLAG_IS_COMPONENTS_V2
        projected_state = state
    message.edited_at = edited_at
    message.safety_updated_at = transition_at
    await project_safety(session, settings, message, projected_state)
    return True


async def apply_safety_delete(
    session: AsyncSession, settings: Settings, message: Message, deleted_at: datetime
) -> None:
    """The legacy delete event can also carry a new authority's removal marker."""
    previous_transition = getattr(message, "safety_updated_at", None)
    if (
        deleted_at.tzinfo is None
        or deleted_at < message.created_at
        or (message.edited_at is not None and deleted_at < message.edited_at)
        or (previous_transition is not None and deleted_at < previous_transition)
    ):
        raise ValueError("safety deletion regressed authoritative state")
    already_removed = getattr(message, "safety_state", None) == "removed"
    message.safety_state = "removed"
    message.deleted_at = None
    message.safety_reason = REASONS["removed"]
    if not already_removed:
        message.content = REASONS["removed"]
        message.e2ee = None
    message.embeds = []
    message.components = []
    message.sticker_items = []
    message.forward_snapshot = None
    message.poll_result = None
    message.mention_user_refs = []
    message.mention_role_refs = []
    message.mention_everyone = False
    message.edited_at = deleted_at
    message.safety_updated_at = deleted_at
    await project_safety(session, settings, message, "removed")


async def reconcile_safety_history(
    session: AsyncSession, settings: Settings, message: Message, raw: dict[str, Any]
) -> None:
    """Persist a validated authority history marker without importing its body.

    The caller binds the verified history response to this message's channel.
    Explicit releases use durable authority mutations, never a stale history page.
    """
    state = safety_state(raw)
    current = getattr(message, "safety_state", None)
    if current == "removed":
        return
    if (
        str(raw.get("id")) != str(message.id)
        or raw.get("origin_domain") != message.origin_domain
        or str(raw.get("channel_id")) != str(message.channel_id)
        or raw.get("channel_domain") != message.channel_domain
    ):
        raise ValueError("history safety marker references the wrong message")
    timestamp = safety_timestamp(raw) or datetime.fromisoformat(
        str(raw.get("edited_at") or raw.get("created_at"))
    )
    if timestamp.tzinfo is None:
        raise ValueError("history safety timestamp has no timezone")
    previous_transition = getattr(message, "safety_updated_at", None)
    if state is None:
        # Retain ordering from already-released history without restoring a
        # locally held message from an unsigned or stale body.
        if current is None and raw.get("safety_updated_at") is not None:
            message.safety_updated_at = max(timestamp, previous_transition or timestamp)
        return
    if current == state:
        message.safety_updated_at = max(timestamp, previous_transition or timestamp)
        return
    if state == "pending_review" and (
        message.deleted_at is not None
        or (message.edited_at is not None and timestamp < message.edited_at)
        or (previous_transition is not None and timestamp <= previous_transition)
    ):
        return
    timestamp = max(
        timestamp,
        message.edited_at or message.created_at,
        previous_transition or message.created_at,
        message.created_at,
    )
    if state == "removed":
        await apply_safety_delete(session, settings, message, timestamp)
        return
    channel = await session.get(Channel, (message.channel_id, message.channel_domain))
    if channel is None:
        raise ValueError("history safety channel is unavailable")
    # Only the authority's restriction is imported for a local author's copy;
    # all identity metadata comes from the retained message.
    update = {
        "id": str(message.id),
        "origin_domain": message.origin_domain,
        "channel_id": str(message.channel_id),
        "channel_domain": message.channel_domain,
        "author_id": str(message.author_id),
        "author_domain": message.author_domain,
        "created_at": message.created_at.isoformat(),
        "edited_at": timestamp.isoformat(),
        "safety_state": state,
        "safety_updated_at": timestamp.isoformat(),
    }
    await apply_safety_update(
        session,
        settings,
        channel,
        message,
        update,
        event_timestamp_ms=int(timestamp.timestamp() * 1000),
    )
