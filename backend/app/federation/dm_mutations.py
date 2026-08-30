from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.custom_emojis import canonical_reaction_emoji
from app.chat.dm_mutations import authority_attested_dm_message_mutation
from app.chat.e2ee import (
    validate_e2ee_envelope,
    validate_e2ee_message_projection,
    validate_e2ee_message_revision,
    validate_message_encryption_policy,
)
from app.chat.message_flags import (
    MESSAGE_FLAG_IS_COMPONENTS_V2,
    MESSAGE_FLAG_SUPPRESS_EMBEDS,
)
from app.chat.pins import (
    CHANNEL_PIN_LIMIT,
    channel_pin_count,
    channel_pins_update_payload,
    message_is_pinnable,
)
from app.chat.reaction_payloads import reaction_event_payload
from app.core.settings import Settings
from app.db.bot_models import BotApplication
from app.db.models import (
    Attachment,
    Channel,
    DMConversation,
    DMParticipant,
    Message,
    MessageProjection,
    MessageView,
    Pin,
    Reaction,
    User,
)
from app.federation.message_content import (
    stored_poll_matches_projection,
    stored_view_matches_projection,
    validate_replicated_rich_projection,
)
from app.federation.network import normalize_domain
from app.federation.replication import (
    database_snowflake,
    replicate_message_attachments,
)


@dataclass(frozen=True, slots=True)
class DMMutationResult:
    channel: Channel
    message: Message
    actor: User
    dispatches: tuple[tuple[str, dict[str, object]], ...] = ()
    render_message_update: bool = False


def _event_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} is missing its timezone")
    return parsed.astimezone(UTC)


async def _apply_message_update(
    session: AsyncSession,
    settings: Settings,
    *,
    channel: Channel,
    message: Message,
    actor: User,
    raw: dict[str, object],
    event_timestamp_ms: int,
) -> None:
    if message.deleted_at is not None or message.message_type == 46:
        raise ValueError("DM message update references an immutable message")
    if (message.author_id, message.author_domain) != (actor.id, actor.origin_domain):
        raise ValueError("DM message update changed its author")
    created_at = _event_time(raw.get("created_at"), "DM message creation timestamp")
    edited_at = _event_time(raw.get("edited_at"), "DM message edit timestamp")
    event_time = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
    if (
        created_at != message.created_at
        or edited_at < created_at
        or edited_at > event_time + timedelta(milliseconds=1)
        or (message.edited_at is not None and edited_at < message.edited_at)
    ):
        raise ValueError("DM message update timestamp is inconsistent")
    if message.edited_at == edited_at:
        # Exact envelope retries are stopped by the durable federation inbox.
        # A distinct signed event at the same revision timestamp is therefore
        # authority equivocation, not a convergence replay.
        raise ValueError("DM message update conflicts with stored authority state")
    raw_message_type = raw.get("message_type")
    raw_flags = raw.get("flags")
    if (
        isinstance(raw_message_type, bool)
        or raw_message_type != message.message_type
        or not isinstance(raw_flags, int)
        or isinstance(raw_flags, bool)
        or raw_flags < 0
        or raw.get("tts") is not bool(message.tts)
        or raw.get("client_nonce") != message.client_nonce
        or raw.get("deleted_at") is not None
        or raw.get("published_at")
        != (message.published_at.isoformat() if message.published_at is not None else None)
    ):
        raise ValueError("DM message update changed immutable metadata")
    editable_flags = MESSAGE_FLAG_SUPPRESS_EMBEDS | MESSAGE_FLAG_IS_COMPONENTS_V2
    if raw_flags & ~editable_flags != int(message.flags or 0) & ~editable_flags:
        raise ValueError("DM message update changed immutable flags")
    content = raw.get("content")
    if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4_000):
        raise ValueError("DM message update content is invalid")
    e2ee = validate_e2ee_envelope(raw.get("e2ee"))
    raw_attachments = raw.get("attachments", [])
    if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
        raise ValueError("DM message update attachment list is invalid")
    rich = validate_replicated_rich_projection(
        raw,
        message_id=message.id,
        message_origin=message.origin_domain,
        message_created_at=message.created_at,
        e2ee=e2ee,
        message_type=message.message_type,
        label="DM message update",
    )
    if (
        content is None
        and e2ee is None
        and not raw_attachments
        and not rich.embeds
        and not rich.components
        and not rich.sticker_items
        and rich.poll is None
        and rich.forwarded_ref is None
    ):
        raise ValueError("DM message update contains no body")
    validate_message_encryption_policy(
        channel.encryption_mode,
        content=content,
        e2ee=e2ee,
        attachment_count=len(raw_attachments),
        policy_generation=channel.encryption_policy_generation,
        policy_epoch=channel.encryption_epoch,
        policy_group_id=channel.encryption_group_id,
    )
    validate_e2ee_message_projection(
        e2ee,
        message_id=message.id,
        message_domain=message.origin_domain,
        edited=True,
    )
    validate_e2ee_message_revision(e2ee, message.e2ee)

    def wire_ref(identifier: object, domain: object, label: str) -> tuple[int, str] | None:
        if identifier is None and domain is None:
            return None
        if identifier is None or not isinstance(domain, str):
            raise ValueError(f"{label} is incomplete")
        return database_snowflake(identifier, label), normalize_domain(domain)

    if wire_ref(
        raw.get("referenced_message_id"),
        raw.get("referenced_message_domain"),
        "DM reply reference",
    ) != (
        (message.referenced_message_id, message.referenced_message_domain)
        if message.referenced_message_id is not None
        and message.referenced_message_domain is not None
        else None
    ):
        raise ValueError("DM message update changed its reply reference")
    if rich.forwarded_ref != (
        (message.forwarded_message_id, message.forwarded_message_domain)
        if message.forwarded_message_id is not None and message.forwarded_message_domain is not None
        else None
    ) or rich.forwarded_channel_ref != (
        (message.forwarded_channel_id, message.forwarded_channel_domain)
        if message.forwarded_channel_id is not None and message.forwarded_channel_domain is not None
        else None
    ):
        raise ValueError("DM message update changed its forward source")
    if rich.forward_snapshot != message.forward_snapshot:
        raise ValueError("DM message update changed its immutable forward snapshot")
    if list(message.sticker_items or []) != rich.sticker_items:
        raise ValueError("DM message update changed immutable sticker items")
    if message.interaction_metadata != rich.interaction_metadata:
        raise ValueError("DM message update changed immutable interaction metadata")
    if not await stored_poll_matches_projection(session, message, rich.poll):
        raise ValueError("DM message update changed its poll definition")

    application_ref = rich.application_ref
    stored_application_ref = (
        (message.application_id, message.application_domain)
        if message.application_id is not None and message.application_domain is not None
        else None
    )
    if stored_application_ref is not None and application_ref != stored_application_ref:
        raise ValueError("DM message update changed its application identity")
    if application_ref is not None:
        application = await session.get(BotApplication, application_ref)
        if (
            application is None
            or application.status != "active"
            or (application.bot_user_id, application.bot_user_domain)
            != (actor.id, actor.origin_domain)
        ):
            raise ValueError("DM message update application is not bound to its bot author")

    raw_mentions = raw.get("mention_user_refs", [])
    if not isinstance(raw_mentions, list) or len(raw_mentions) > 5_000:
        raise ValueError("DM message update mention list is invalid")
    mention_pairs: list[tuple[int, str]] = []
    for item in raw_mentions:
        if not isinstance(item, dict):
            raise ValueError("DM message update mention reference is invalid")
        mention_pairs.append(
            (
                database_snowflake(item.get("id"), "DM mentioned user id"),
                normalize_domain(str(item.get("origin_domain", ""))),
            )
        )
    if mention_pairs != list(dict.fromkeys(mention_pairs)):
        raise ValueError("DM message update mentions are not canonical")
    participant_refs = set(
        (
            await session.execute(
                select(DMParticipant.user_id, DMParticipant.user_domain).where(
                    DMParticipant.conversation_id == channel.id,
                    DMParticipant.conversation_domain == channel.origin_domain,
                )
            )
        ).tuples()
    )
    if any(pair not in participant_refs for pair in mention_pairs):
        raise ValueError("DM message update mentions a non-participant")
    if raw.get("mention_role_refs", []) != [] or raw.get("mention_everyone", False) is not False:
        raise ValueError("DM message update contains guild-only mention routing")

    stored_view = await session.get(
        MessageView,
        (message.id, message.origin_domain),
        with_for_update=True,
    )
    has_controls = bool(rich.components or rich.has_encrypted_controls)
    current_view_version = int(message.view_version or 0)
    if rich.view_version == current_view_version:
        if not await stored_view_matches_projection(session, message, rich):
            raise ValueError("DM message update changed a view without a revision")
    elif rich.view_version != current_view_version + 1:
        raise ValueError("DM message view revision is not monotonic")
    elif has_controls:
        if (
            application_ref is None
            or rich.interaction_installation_ref is None
            or rich.interaction_integration_type is None
            or rich.interaction_installation_revision is None
        ):
            raise ValueError("DM message view lineage is incomplete")
        installation_id, installation_domain = rich.interaction_installation_ref
        if stored_view is None:
            stored_view = MessageView(
                message_id=message.id,
                message_domain=message.origin_domain,
                application_id=application_ref[0],
                application_domain=application_ref[1],
                integration_type=rich.interaction_integration_type,
                installation_id=installation_id,
                installation_domain=installation_domain,
                installation_revision=rich.interaction_installation_revision,
                version=rich.view_version,
                persistent=rich.view_persistent,
                expires_at=rich.view_expires_at,
            )
            session.add(stored_view)
        else:
            stored_view.application_id = application_ref[0]
            stored_view.application_domain = application_ref[1]
            stored_view.integration_type = rich.interaction_integration_type
            stored_view.installation_id = installation_id
            stored_view.installation_domain = installation_domain
            stored_view.installation_revision = rich.interaction_installation_revision
            stored_view.version = rich.view_version
            stored_view.persistent = rich.view_persistent
            stored_view.expires_at = rich.view_expires_at
    elif stored_view is not None:
        await session.delete(stored_view)

    replicated_attachments = await replicate_message_attachments(
        session,
        settings,
        message,
        actor,
        raw_attachments,
        allowed_attachment_origins={actor.origin_domain},
    )
    incoming_refs = {(item.id, item.origin_domain) for item in replicated_attachments}
    stored_attachments = list(
        await session.scalars(
            select(Attachment).where(
                Attachment.message_id == message.id,
                Attachment.message_domain == message.origin_domain,
                Attachment.deleted_at.is_(None),
            )
        )
    )
    for attachment in stored_attachments:
        if (attachment.id, attachment.origin_domain) not in incoming_refs:
            attachment.deleted_at = edited_at

    message.content = content
    message.e2ee = e2ee
    message.embeds = rich.embeds
    message.components = rich.components
    message.application_id = application_ref[0] if application_ref is not None else None
    message.application_domain = application_ref[1] if application_ref is not None else None
    message.view_version = rich.view_version
    message.mention_user_refs = [
        {"id": str(user_id), "origin_domain": domain} for user_id, domain in mention_pairs
    ]
    message.mention_role_refs = []
    message.mention_everyone = False
    projection = await session.get(
        MessageProjection,
        (message.id, message.origin_domain),
        with_for_update=True,
    )
    if projection is None:
        session.add(
            MessageProjection(
                message_id=message.id,
                message_domain=message.origin_domain,
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                mention_user_refs=message.mention_user_refs,
            )
        )
    else:
        projection.mention_user_refs = message.mention_user_refs
    message.encryption_policy_generation = channel.encryption_policy_generation
    message.encryption_epoch = channel.encryption_epoch
    message.flags = raw_flags
    message.edited_at = edited_at
    # Gateway projection is rendered after commit for each local participant.
    # Poll vote state and other viewer-sensitive fields must never be copied
    # from the authority's wire body or rendered once using the editing actor.


async def _mutation_scope(
    session: AsyncSession,
    settings: Settings,
    *,
    content: dict[str, object],
    context: dict[str, object],
    event_origin: str,
    actor_ref: tuple[int, str],
) -> tuple[Channel, Message, User]:
    conversation_ref = (
        database_snowflake(context["conversation_id"], "DM conversation id"),
        normalize_domain(str(context["conversation_domain"])),
    )
    conversation = await session.get(DMConversation, conversation_ref, with_for_update=True)
    channel = await session.get(Channel, conversation_ref)
    if (
        conversation is None
        or channel is None
        or channel.guild_id is not None
        or conversation.authority_domain != event_origin
        or conversation.origin_domain != event_origin
        or event_origin == settings.domain
    ):
        raise ValueError("DM mutation is not bound to a remote conversation authority")
    actor = await session.get(User, actor_ref)
    if (
        actor is None
        or await session.get(
            DMParticipant,
            (conversation.id, conversation.origin_domain, actor.id, actor.origin_domain),
        )
        is None
    ):
        raise ValueError("DM mutation actor is not a participant")
    raw_message = content.get("message")
    message_id = (
        raw_message.get("id") if isinstance(raw_message, dict) else content.get("message_id")
    )
    message_domain = (
        raw_message.get("origin_domain")
        if isinstance(raw_message, dict)
        else content.get("message_domain")
    )
    message_ref = (
        database_snowflake(message_id, "DM mutation message id"),
        normalize_domain(str(message_domain)),
    )
    message = await session.get(Message, message_ref, with_for_update=True)
    if message is None or (message.channel_id, message.channel_domain) != conversation_ref:
        raise ValueError("DM mutation source is unavailable")
    return channel, message, actor


async def apply_dm_message_mutation(
    session: AsyncSession,
    settings: Settings,
    *,
    event_type: str,
    content: dict[str, object],
    context: dict[str, object],
    event_origin: str,
    actor_ref: tuple[int, str],
    event_timestamp_ms: int,
) -> DMMutationResult:
    """Apply a signed authority DM delta idempotently on a participant home."""

    actor_wire = (str(actor_ref[0]), actor_ref[1])
    if not authority_attested_dm_message_mutation(
        event_type,
        content,
        context,
        expected_authority=event_origin,
        actor=actor_wire,
    ):
        raise ValueError("DM mutation projection is invalid")
    channel, message, actor = await _mutation_scope(
        session,
        settings,
        content=content,
        context=context,
        event_origin=event_origin,
        actor_ref=actor_ref,
    )
    base_payload: dict[str, object] = {
        "id": str(message.id),
        "origin_domain": message.origin_domain,
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
    }
    if event_type == "dm.message.update":
        raw_message = content["message"]
        if not isinstance(raw_message, dict):
            raise RuntimeError("validated DM message update lost its body")
        await _apply_message_update(
            session,
            settings,
            channel=channel,
            message=message,
            actor=actor,
            raw=raw_message,
            event_timestamp_ms=event_timestamp_ms,
        )
        return DMMutationResult(
            channel,
            message,
            actor,
            render_message_update=True,
        )
    if event_type == "dm.message.delete":
        if (message.author_id, message.author_domain) != actor_ref:
            raise ValueError("DM message deletion changed its author")
        deleted_at = datetime.fromisoformat(str(content["deleted_at"])).astimezone(UTC)
        event_time = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
        if deleted_at < message.created_at or deleted_at > event_time + timedelta(milliseconds=1):
            raise ValueError("DM message deletion timestamp is invalid")
        if message.deleted_at is not None:
            if message.deleted_at != deleted_at:
                raise ValueError("DM message deletion conflicts with stored authority state")
            return DMMutationResult(channel, message, actor)
        message.content = None
        message.e2ee = None
        message.deleted_at = deleted_at
        await session.execute(
            delete(Pin).where(
                Pin.message_id == message.id,
                Pin.message_domain == message.origin_domain,
            )
        )
        return DMMutationResult(
            channel,
            message,
            actor,
            dispatches=(("MESSAGE_DELETE", base_payload),),
        )

    if event_type in {"dm.reaction.add", "dm.reaction.remove"}:
        raw_emoji = content["emoji"]
        if not isinstance(raw_emoji, str):
            raise RuntimeError("validated DM reaction lost its emoji")
        emoji = canonical_reaction_emoji(raw_emoji)
        changed: int | None
        if event_type.endswith("add"):
            if message.deleted_at is not None:
                raise ValueError("DM reaction references a deleted message")
            changed = await session.scalar(
                pg_insert(Reaction)
                .values(
                    message_id=message.id,
                    message_domain=message.origin_domain,
                    user_id=actor.id,
                    user_domain=actor.origin_domain,
                    emoji_key=emoji,
                )
                .on_conflict_do_nothing()
                .returning(Reaction.message_id)
            )
        else:
            changed = await session.scalar(
                delete(Reaction)
                .where(
                    Reaction.message_id == message.id,
                    Reaction.message_domain == message.origin_domain,
                    Reaction.user_id == actor.id,
                    Reaction.user_domain == actor.origin_domain,
                    Reaction.emoji_key == emoji,
                )
                .returning(Reaction.message_id)
            )
        if changed is None:
            return DMMutationResult(channel, message, actor)
        return DMMutationResult(
            channel,
            message,
            actor,
            dispatches=(
                (
                    (
                        "MESSAGE_REACTION_REMOVE"
                        if event_type.endswith("remove")
                        else "MESSAGE_REACTION_ADD"
                    ),
                    reaction_event_payload(
                        message_id=message.id,
                        message_domain=message.origin_domain,
                        channel_id=channel.id,
                        channel_domain=channel.origin_domain,
                        user_id=actor.id,
                        user_domain=actor.origin_domain,
                        emoji=emoji,
                        message_author_id=message.author_id,
                        message_author_domain=message.author_domain,
                        removed=event_type.endswith("remove"),
                    ),
                ),
            ),
        )

    changed = None
    if event_type == "dm.pin.add":
        if not message_is_pinnable(message):
            raise ValueError("DM pin references a non-pinnable message")
        existing_pin = await session.get(
            Pin,
            (channel.id, channel.origin_domain, message.id, message.origin_domain),
        )
        if existing_pin is None and await channel_pin_count(session, channel) >= CHANNEL_PIN_LIMIT:
            raise ValueError("DM pin exceeds the channel pin limit")
        changed = await session.scalar(
            pg_insert(Pin)
            .values(
                channel_id=channel.id,
                channel_domain=channel.origin_domain,
                message_id=message.id,
                message_domain=message.origin_domain,
                pinned_by_id=actor.id,
                pinned_by_domain=actor.origin_domain,
            )
            .on_conflict_do_nothing()
            .returning(Pin.message_id)
        )
    else:
        changed = await session.scalar(
            delete(Pin)
            .where(
                Pin.channel_id == channel.id,
                Pin.channel_domain == channel.origin_domain,
                Pin.message_id == message.id,
                Pin.message_domain == message.origin_domain,
            )
            .returning(Pin.message_id)
        )
    if changed is None:
        return DMMutationResult(channel, message, actor)
    pinned = event_type.endswith("add")
    pins_update = await channel_pins_update_payload(
        session,
        channel,
        None,
        changed_message=message,
        pinned=pinned,
    )
    return DMMutationResult(
        channel,
        message,
        actor,
        dispatches=(
            (
                "CHANNEL_PINS_UPDATE",
                pins_update,
            ),
        ),
    )


__all__ = ["DMMutationResult", "apply_dm_message_mutation"]
