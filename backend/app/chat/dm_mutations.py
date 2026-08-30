from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from app.chat.custom_emojis import canonical_reaction_emoji

DM_MESSAGE_MUTATION_EVENTS = frozenset(
    {
        "dm.message.update",
        "dm.message.delete",
        "dm.reaction.add",
        "dm.reaction.remove",
        "dm.pin.add",
        "dm.pin.remove",
    }
)


def _canonical_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
        and int(value) <= (1 << 63) - 1
    )


def _canonical_context(context: object, expected_authority: str) -> bool:
    return bool(
        isinstance(context, dict)
        and set(context) == {"conversation_id", "conversation_domain"}
        and _canonical_id(context.get("conversation_id"))
        and context.get("conversation_domain") == expected_authority
    )


def authority_attested_dm_message_mutation(
    event_type: object,
    content: object,
    context: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    """Recognize only exact authority-committed durable DM message deltas."""

    if (
        event_type not in DM_MESSAGE_MUTATION_EVENTS
        or not isinstance(content, dict)
        or not isinstance(context, dict)
        or not _canonical_context(context, expected_authority)
    ):
        return False
    conversation_id = context["conversation_id"]
    actor_id, actor_domain = actor
    if not _canonical_id(actor_id):
        return False
    common = {
        "message_id",
        "message_domain",
        "channel_id",
        "channel_domain",
    }
    if event_type == "dm.message.update":
        message = content.get("message")
        if set(content) != {"message"} or not isinstance(message, dict):
            return False
        return bool(
            _canonical_id(message.get("id"))
            and isinstance(message.get("origin_domain"), str)
            and message.get("channel_id") == conversation_id
            and message.get("channel_domain") == expected_authority
            and message.get("author_id") == actor_id
            and message.get("author_domain") == actor_domain
            and isinstance(message.get("edited_at"), str)
        )
    if not common <= set(content):
        return False
    if (
        not _canonical_id(content.get("message_id"))
        or not isinstance(content.get("message_domain"), str)
        or content.get("channel_id") != conversation_id
        or content.get("channel_domain") != expected_authority
    ):
        return False
    if event_type == "dm.message.delete":
        if set(content) != common | {"deleted_at"} or not isinstance(
            content.get("deleted_at"), str
        ):
            return False
        try:
            deleted_at = datetime.fromisoformat(content["deleted_at"])
        except ValueError:
            return False
        return deleted_at.tzinfo is not None
    if event_type in {"dm.reaction.add", "dm.reaction.remove"}:
        if (
            set(content) != common | {"user_id", "user_domain", "emoji"}
            or (content.get("user_id"), content.get("user_domain")) != actor
        ):
            return False
        emoji = content.get("emoji")
        if not isinstance(emoji, str):
            return False
        try:
            canonical_reaction_emoji(emoji)
        except (TypeError, ValueError):
            return False
        return True
    if set(content) != common | {"user_id", "user_domain"}:
        return False
    return (content.get("user_id"), content.get("user_domain")) == actor


def dm_mutation_message_ref(content: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the structurally asserted message ref without coercing wire IDs."""

    message = content.get("message")
    if isinstance(message, dict):
        identifier = message.get("id")
        domain = message.get("origin_domain")
    else:
        identifier = content.get("message_id")
        domain = content.get("message_domain")
    if not _canonical_id(identifier) or not isinstance(domain, str):
        return None
    return str(identifier), domain


__all__ = [
    "DM_MESSAGE_MUTATION_EVENTS",
    "authority_attested_dm_message_mutation",
    "dm_mutation_message_ref",
]
