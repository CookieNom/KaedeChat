from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.message_references import build_qualified_message_reference
from app.db.models import Channel, Guild, Message, Pin

CHANNEL_PIN_LIMIT = 250
PIN_NOTICE_MESSAGE_TYPE = 6
PINNABLE_MESSAGE_TYPES = frozenset({0, 19, 20, 23})


def normalize_pin_cursor(value: datetime | None) -> datetime | None:
    """Require the timezone-aware ISO cursor used by Discord's pins API."""

    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("pin cursor must include a timezone")
    return value.astimezone(UTC)


def message_is_pinnable(message: Message) -> bool:
    """System messages are not valid pin targets."""

    return message.deleted_at is None and message.message_type in PINNABLE_MESSAGE_TYPES


def authority_attested_direct_pin_notice(
    event_type: object,
    content: object,
    *,
    expected_authority: str,
    actor: tuple[str, str],
) -> bool:
    """Recognize the exact direct-DM pin notice an authority mints for an actor."""

    if event_type != "dm.message.create" or not isinstance(content, dict):
        return False
    message = content.get("message")
    author = content.get("author")
    if (
        set(content) != {"message", "author"}
        or not isinstance(message, dict)
        or not isinstance(author, dict)
    ):
        return False
    actor_id, actor_domain = actor
    raw_reference_id = message.get("referenced_message_id")
    raw_reference_domain = message.get("referenced_message_domain")
    canonical_ids = (actor_id, message.get("id"), message.get("channel_id"), raw_reference_id)
    if any(
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or value.startswith("0")
        for value in canonical_ids
    ):
        return False
    if not isinstance(raw_reference_domain, str):
        return False
    try:
        expected_reference = build_qualified_message_reference(
            message_type=PIN_NOTICE_MESSAGE_TYPE,
            message_ref=(int(cast(str, raw_reference_id)), raw_reference_domain),
            channel_ref=(int(cast(str, message.get("channel_id"))), expected_authority),
        )
    except ValueError:
        return False
    return bool(
        message.get("message_type") == PIN_NOTICE_MESSAGE_TYPE
        and message.get("origin_domain") == expected_authority
        and message.get("channel_domain") == expected_authority
        and message.get("author_id") == actor_id
        and message.get("author_domain") == actor_domain
        and str(author.get("id")) == actor_id
        and author.get("origin_domain") == actor_domain
        and message.get("referenced_message_domain") is not None
        and message.get("message_reference") == expected_reference
        and message.get("content") is None
        and message.get("e2ee") is None
        and message.get("attachments", []) == []
        and message.get("embeds", []) == []
        and message.get("components", []) == []
        and message.get("sticker_items", []) == []
        and message.get("poll") is None
        and message.get("message_snapshots", []) == []
        and message.get("application_id") is None
        and message.get("application_domain") is None
        and message.get("interaction_metadata") is None
        and message.get("forwarded_message_id") is None
        and message.get("forwarded_message_domain") is None
        and message.get("client_nonce") is None
        and message.get("mention_user_refs", []) == []
        and message.get("mention_role_refs", []) == []
        and message.get("mention_everyone", False) is False
        and message.get("tts", False) is False
        and message.get("flags", 0) == 0
        and message.get("edited_at") is None
        and message.get("deleted_at") is None
    )


def validate_pin_page_payload(
    value: object,
    *,
    channel_ref: tuple[int, str],
    limit: int,
    before: datetime | None,
) -> dict[str, object]:
    """Fail closed on a bounded pins page returned by another authority."""

    if not isinstance(value, dict) or set(value) != {"items", "has_more"}:
        raise ValueError("pin page shape is invalid")
    raw_items = value.get("items")
    raw_has_more = value.get("has_more")
    if (
        not isinstance(raw_items, list)
        or len(raw_items) > limit
        or type(raw_has_more) is not bool
        or (raw_has_more and not raw_items)
    ):
        raise ValueError("pin page shape is invalid")
    cursor = normalize_pin_cursor(before)
    previous: datetime | None = None
    refs: set[tuple[int, str]] = set()
    for item in raw_items:
        if not isinstance(item, dict) or set(item) != {"pinned_at", "message"}:
            raise ValueError("pin page entry is invalid")
        raw_timestamp = item.get("pinned_at")
        message = item.get("message")
        if not isinstance(raw_timestamp, str) or not isinstance(message, Mapping):
            raise ValueError("pin page entry is invalid")
        try:
            pinned_at = normalize_pin_cursor(datetime.fromisoformat(raw_timestamp))
        except ValueError as exc:
            raise ValueError("pin page timestamp is invalid") from exc
        if pinned_at is None or (cursor is not None and pinned_at >= cursor):
            raise ValueError("pin page cursor did not advance")
        if previous is not None and pinned_at > previous:
            raise ValueError("pin page is not newest-first")
        raw_id = message.get("id")
        raw_domain = message.get("origin_domain")
        if (
            not isinstance(raw_id, str)
            or not raw_id.isascii()
            or not raw_id.isdecimal()
            or raw_id.startswith("0")
            or not isinstance(raw_domain, str)
            or message.get("channel_id") != str(channel_ref[0])
            or message.get("channel_domain") != channel_ref[1]
            or message.get("pinned") is not True
        ):
            raise ValueError("pin page message linkage is invalid")
        ref = (int(raw_id), raw_domain)
        if ref in refs:
            raise ValueError("pin page contains a duplicate message")
        refs.add(ref)
        previous = pinned_at
    return cast(dict[str, object], value)


async def channel_pin_count(session: AsyncSession, channel: Channel) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(Pin)
            .where(
                Pin.channel_id == channel.id,
                Pin.channel_domain == channel.origin_domain,
            )
        )
        or 0
    )


async def latest_channel_pin_at(
    session: AsyncSession,
    channel: Channel,
) -> datetime | None:
    return cast(
        datetime | None,
        await session.scalar(
            select(func.max(Pin.pinned_at)).where(
                Pin.channel_id == channel.id,
                Pin.channel_domain == channel.origin_domain,
            )
        ),
    )


async def channel_pins_update_payload(
    session: AsyncSession,
    channel: Channel,
    guild: Guild | None,
    *,
    changed_message: Message | None = None,
    pinned: bool | None = None,
) -> dict[str, object]:
    """Build Discord's Channel Pins Update with qualified Kaede extensions."""

    latest = await latest_channel_pin_at(session, channel)
    payload: dict[str, object] = {
        "channel_id": str(channel.id),
        "channel_domain": channel.origin_domain,
        "guild_id": str(guild.id) if guild is not None else None,
        "guild_domain": guild.origin_domain if guild is not None else None,
        "last_pin_timestamp": latest.isoformat() if latest is not None else None,
    }
    if changed_message is not None and pinned is not None:
        payload.update(
            {
                "message_id": str(changed_message.id),
                "message_domain": changed_message.origin_domain,
                "pinned": pinned,
            }
        )
    return payload


__all__ = [
    "CHANNEL_PIN_LIMIT",
    "PIN_NOTICE_MESSAGE_TYPE",
    "authority_attested_direct_pin_notice",
    "channel_pin_count",
    "channel_pins_update_payload",
    "message_is_pinnable",
    "normalize_pin_cursor",
    "validate_pin_page_payload",
]
