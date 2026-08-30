from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.poll_results import authority_attested_dm_poll_mutation
from app.core.settings import Settings
from app.db.models import (
    Channel,
    DMConversation,
    DMParticipant,
    Message,
    Poll,
    PollAnswer,
    PollVote,
    User,
)
from app.federation.network import normalize_domain
from app.federation.replication import database_snowflake


@dataclass(frozen=True, slots=True)
class DMPollMutationResult:
    channel: Channel
    message: Message
    actor: User
    vote_events: tuple[tuple[str, dict[str, object]], ...] = ()
    finalized: bool = False


async def apply_dm_poll_mutation(
    session: AsyncSession,
    settings: Settings,
    *,
    event_type: str,
    content: dict[str, object],
    context: dict[str, object],
    event_origin: str,
    actor_ref: tuple[int, str],
    event_timestamp_ms: int,
) -> DMPollMutationResult:
    """Apply one authority-signed DM poll delta idempotently on a participant home."""

    if not authority_attested_dm_poll_mutation(
        event_type,
        content,
        context,
        expected_authority=event_origin,
    ):
        raise ValueError("DM poll mutation projection is invalid")
    conversation_ref = (
        database_snowflake(context["conversation_id"], "DM conversation id"),
        normalize_domain(str(context["conversation_domain"])),
    )
    conversation = await session.get(DMConversation, conversation_ref)
    channel = await session.get(Channel, conversation_ref)
    if (
        conversation is None
        or channel is None
        or channel.guild_id is not None
        or conversation.authority_domain != event_origin
        or conversation.origin_domain != event_origin
    ):
        raise ValueError("DM poll mutation is not bound to its conversation authority")
    actor = await session.get(User, actor_ref)
    if (
        actor is None
        or await session.get(
            DMParticipant,
            (conversation.id, conversation.origin_domain, actor.id, actor.origin_domain),
        )
        is None
    ):
        raise ValueError("DM poll mutation actor is not a participant")
    message_ref = (
        database_snowflake(content["message_id"], "DM poll message id"),
        normalize_domain(str(content["message_domain"])),
    )
    message = await session.get(Message, message_ref)
    if message is None or (message.channel_id, message.channel_domain) != conversation_ref:
        raise ValueError("DM poll mutation source is unavailable")
    poll = await session.scalar(
        select(Poll)
        .where(
            Poll.message_id == message.id,
            Poll.message_domain == message.origin_domain,
        )
        .with_for_update()
    )
    if poll is None:
        raise ValueError("DM poll mutation source has no poll")

    if event_type == "dm.poll.finalize":
        finalized_at = datetime.fromisoformat(str(content["finalized_at"]))
        if finalized_at.tzinfo is None:
            raise ValueError("DM poll finalization timestamp is naive")
        finalized_at = finalized_at.astimezone(UTC)
        event_time = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
        if finalized_at > event_time + timedelta(milliseconds=1):
            raise ValueError("DM poll finalization postdates its signed event")
        if poll.finalized_at is not None:
            if poll.finalized_at != finalized_at:
                raise ValueError("DM poll finalization conflicts with stored authority state")
            return DMPollMutationResult(channel, message, actor)
        poll.finalized_at = finalized_at
        return DMPollMutationResult(channel, message, actor, finalized=True)

    raw_answer_id = content["answer_id"]
    if not isinstance(raw_answer_id, int) or isinstance(raw_answer_id, bool):
        raise RuntimeError("validated DM poll mutation lost its answer")
    answer_id = raw_answer_id
    if (
        await session.get(
            PollAnswer,
            (message.id, message.origin_domain, answer_id),
        )
        is None
    ):
        raise ValueError("DM poll mutation answer does not exist")
    event_time = datetime.fromtimestamp(event_timestamp_ms / 1000, tz=UTC)
    if event_time > poll.expires_at or (
        poll.finalized_at is not None and event_time > poll.finalized_at
    ):
        raise ValueError("DM poll vote postdates finalization")
    base_payload: dict[str, object] = {
        "message_id": str(message.id),
        "message_domain": message.origin_domain,
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
        "guild_id": None,
        "guild_domain": None,
        "user_id": str(actor.id),
        "user_domain": actor.origin_domain,
    }
    gateway_events: list[tuple[str, dict[str, object]]] = []
    if event_type == "dm.poll.vote.add":
        removed_answers: list[int] = []
        if not poll.allow_multiselect:
            removed_answers = list(
                await session.scalars(
                    delete(PollVote)
                    .where(
                        PollVote.message_id == message.id,
                        PollVote.message_domain == message.origin_domain,
                        PollVote.user_id == actor.id,
                        PollVote.user_domain == actor.origin_domain,
                        PollVote.answer_id != answer_id,
                    )
                    .returning(PollVote.answer_id)
                )
            )
        inserted = await session.scalar(
            pg_insert(PollVote)
            .values(
                message_id=message.id,
                message_domain=message.origin_domain,
                answer_id=answer_id,
                user_id=actor.id,
                user_domain=actor.origin_domain,
            )
            .on_conflict_do_nothing()
            .returning(PollVote.answer_id)
        )
        gateway_events.extend(
            (
                "MESSAGE_POLL_VOTE_REMOVE",
                base_payload | {"answer_id": removed_answer},
            )
            for removed_answer in removed_answers
        )
        if inserted is not None:
            gateway_events.append(
                ("MESSAGE_POLL_VOTE_ADD", base_payload | {"answer_id": answer_id})
            )
    else:
        removed = await session.scalar(
            delete(PollVote)
            .where(
                PollVote.message_id == message.id,
                PollVote.message_domain == message.origin_domain,
                PollVote.answer_id == answer_id,
                PollVote.user_id == actor.id,
                PollVote.user_domain == actor.origin_domain,
            )
            .returning(PollVote.answer_id)
        )
        if removed is not None:
            gateway_events.append(
                ("MESSAGE_POLL_VOTE_REMOVE", base_payload | {"answer_id": answer_id})
            )
    return DMPollMutationResult(
        channel,
        message,
        actor,
        vote_events=tuple(gateway_events),
    )
