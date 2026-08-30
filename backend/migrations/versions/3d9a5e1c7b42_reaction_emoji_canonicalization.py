"""Canonicalize durable reaction identities.

Revision ID: 3d9a5e1c7b42
Revises: 2c8f4d0b6e31
Create Date: 2026-08-29
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import Connection

revision: str = "3d9a5e1c7b42"
down_revision: str | None = "2c8f4d0b6e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VARIATION_SELECTORS = {"\ufe0e", "\ufe0f"}
_ZWJ = "\u200d"
_KEYCAP = "\u20e3"
_MAX_SNOWFLAKE = (1 << 63) - 1
_CUSTOM_EMOJI_PATTERN = re.compile(
    r"<(?P<animated>a?):(?P<name>[A-Za-z0-9_]{2,32}):"
    r"(?P<id>[1-9][0-9]{0,18})@(?P<domain>[A-Za-z0-9.-]{1,253})>"
)
_DOMAIN_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def _is_emoji_base(codepoint: int) -> bool:
    # Frozen with the API validator at this revision. Migrations must not import
    # mutable application validation code when repairing historical rows.
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or codepoint
        in {
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x2194,
            0x2195,
            0x2196,
            0x2197,
            0x2198,
            0x2199,
            0x21A9,
            0x21AA,
            0x231A,
            0x231B,
            0x2328,
            0x23CF,
            0x24C2,
            0x25AA,
            0x25AB,
            0x25B6,
            0x25C0,
            0x25FB,
            0x25FC,
            0x25FD,
            0x25FE,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        }
    )


def _is_unicode_emoji_sequence(value: str) -> bool:
    codepoints = [ord(character) for character in value]
    if len(codepoints) == 2 and all(0x1F1E6 <= item <= 0x1F1FF for item in codepoints):
        return True
    if (
        len(codepoints) == 2
        and chr(codepoints[0]) in "#*0123456789"
        and codepoints[1] == ord(_KEYCAP)
    ):
        return True
    if (
        len(codepoints) >= 3
        and codepoints[0] == 0x1F3F4
        and codepoints[-1] == 0xE007F
        and all(0xE0061 <= item <= 0xE007A for item in codepoints[1:-1])
    ):
        return True
    segments = value.split(_ZWJ)
    if not segments or any(not segment for segment in segments):
        return False
    for segment in segments:
        points = [ord(character) for character in segment]
        if len(points) not in {1, 2} or not _is_emoji_base(points[0]):
            return False
        if len(points) == 2 and not 0x1F3FB <= points[1] <= 0x1F3FF:
            return False
    return True


def _canonical_unicode_reaction_emoji(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    normalized = "".join(
        character for character in normalized if character not in _VARIATION_SELECTORS
    )
    if not normalized or not _is_unicode_emoji_sequence(normalized):
        raise ValueError("reaction must contain exactly one valid emoji")
    return normalized


def _canonical_reaction_emoji(value: str) -> str:
    custom = _CUSTOM_EMOJI_PATTERN.fullmatch(value)
    if custom is None:
        return _canonical_unicode_reaction_emoji(value)
    domain = custom.group("domain").rstrip(".").lower()
    emoji_id = int(custom.group("id"))
    if not _DOMAIN_PATTERN.fullmatch(domain) or emoji_id > _MAX_SNOWFLAKE:
        raise ValueError("reaction custom emoji identity is invalid")
    animated = "a" if custom.group("animated") == "a" else ""
    return f"<{animated}:{custom.group('name')}:{emoji_id}@{domain}>"


# Stage source-to-canonical mappings instead of updating the primary-key field
# in place. The latter fails when one actor already has both `❤️` and `❤` rows.
REACTION_MAPPING_SQL = r"""
CREATE TEMPORARY TABLE kaede_reaction_emoji_canonicalization
ON COMMIT DROP AS
WITH candidates AS (
    SELECT reaction.message_id,
           reaction.message_domain,
           reaction.user_id,
           reaction.user_domain,
           reaction.emoji_key AS legacy_emoji_key,
           reaction.created_at,
           CASE
               WHEN reaction.emoji_key ~
                    '^<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{0,18}@[A-Za-z0-9.-]{1,253}>$'
                AND lower(rtrim(
                        split_part(split_part(reaction.emoji_key, '@', 2), '>', 1),
                        '.'
                    )) ~
                    '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
                AND (
                    char_length(split_part(split_part(reaction.emoji_key, ':', 3), '@', 1)) < 19
                    OR (
                        char_length(
                            split_part(split_part(reaction.emoji_key, ':', 3), '@', 1)
                        ) = 19
                        AND split_part(
                                split_part(reaction.emoji_key, ':', 3), '@', 1
                            ) <= '9223372036854775807'
                    )
                )
               THEN regexp_replace(
                   reaction.emoji_key,
                   '@[A-Za-z0-9.-]{1,253}>$',
                   '@' || lower(rtrim(
                       split_part(split_part(reaction.emoji_key, '@', 2), '>', 1),
                       '.'
                   )) || '>'
               )
               ELSE replace(
                   replace(normalize(reaction.emoji_key, NFC), chr(65038), ''),
                   chr(65039),
                   ''
               )
           END AS canonical_emoji_key
    FROM reactions AS reaction
)
SELECT message_id,
       message_domain,
       user_id,
       user_domain,
       legacy_emoji_key,
       canonical_emoji_key,
       created_at
FROM candidates
WHERE canonical_emoji_key <> legacy_emoji_key
  AND char_length(canonical_emoji_key) BETWEEN 1 AND 320
"""

REACTION_MERGE_SQL = """
INSERT INTO reactions (
    message_id,
    message_domain,
    user_id,
    user_domain,
    emoji_key,
    created_at
)
SELECT mapping.message_id,
       mapping.message_domain,
       mapping.user_id,
       mapping.user_domain,
       mapping.canonical_emoji_key,
       min(mapping.created_at)
FROM kaede_reaction_emoji_canonicalization AS mapping
GROUP BY mapping.message_id,
         mapping.message_domain,
         mapping.user_id,
         mapping.user_domain,
         mapping.canonical_emoji_key
ON CONFLICT (message_id, message_domain, user_id, user_domain, emoji_key)
DO UPDATE SET created_at = least(reactions.created_at, excluded.created_at)
"""

REACTION_DELETE_LEGACY_SQL = """
DELETE FROM reactions AS reaction
USING kaede_reaction_emoji_canonicalization AS mapping
WHERE reaction.message_id = mapping.message_id
  AND reaction.message_domain = mapping.message_domain
  AND reaction.user_id = mapping.user_id
  AND reaction.user_domain = mapping.user_domain
  AND reaction.emoji_key = mapping.legacy_emoji_key
"""

FORUM_DEFAULT_SELECT_SQL = sa.text(
    """
    SELECT id, origin_domain, default_reaction_emoji
    FROM channels
    WHERE default_reaction_emoji IS NOT NULL
      AND jsonb_typeof(default_reaction_emoji) = 'object'
      AND default_reaction_emoji ->> 'emoji_id' IS NULL
    FOR UPDATE
    """
)

FORUM_DEFAULT_UPDATE_SQL = sa.text(
    """
    UPDATE channels
    SET default_reaction_emoji = CAST(:payload AS jsonb)
    WHERE id = :channel_id
      AND origin_domain = :channel_domain
    """
)

STAGED_HISTORY_SELECT_SQL = sa.text(
    """
    SELECT export_id, export_domain, message_id, message_domain, payload
    FROM guild_history_staged_messages
    WHERE jsonb_typeof(payload -> 'reactions') = 'array'
    FOR UPDATE
    """
)

STAGED_HISTORY_UPDATE_SQL = sa.text(
    """
    UPDATE guild_history_staged_messages
    SET payload = CAST(:payload AS jsonb)
    WHERE export_id = :export_id
      AND export_domain = :export_domain
      AND message_id = :message_id
      AND message_domain = :message_domain
    """
)


def _canonicalize_forum_defaults(connection: Connection) -> None:
    updates: list[dict[str, Any]] = []
    rows = connection.execute(FORUM_DEFAULT_SELECT_SQL).mappings()
    for row in rows:
        payload = row["default_reaction_emoji"]
        if not isinstance(payload, dict) or payload.get("emoji_id") is not None:
            continue
        emoji_name = payload.get("emoji_name")
        try:
            canonical = (
                _canonical_unicode_reaction_emoji(emoji_name)
                if isinstance(emoji_name, str)
                else None
            )
        except ValueError:
            canonical = None
        if canonical == emoji_name:
            continue
        canonical_payload = {**payload, "emoji_name": canonical} if canonical is not None else None
        updates.append(
            {
                "channel_id": row["id"],
                "channel_domain": row["origin_domain"],
                "payload": (
                    json.dumps(
                        canonical_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if canonical_payload is not None
                    else None
                ),
            }
        )
    if updates:
        connection.execute(FORUM_DEFAULT_UPDATE_SQL, updates)


def _reaction_created_at(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _canonical_staged_reactions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    canonical: list[dict[str, Any]] = []
    positions: dict[tuple[object, object, str], int] = {}
    for raw in value:
        if not isinstance(raw, dict):
            continue
        emoji = raw.get("emoji")
        if not isinstance(emoji, str):
            continue
        try:
            canonical_emoji = _canonical_reaction_emoji(emoji)
        except ValueError:
            continue
        item = {**raw, "emoji": canonical_emoji}
        key = (raw.get("user_id"), raw.get("user_domain"), canonical_emoji)
        existing_position = positions.get(key)
        if existing_position is None:
            positions[key] = len(canonical)
            canonical.append(item)
            continue
        previous = canonical[existing_position]
        previous_created_at = _reaction_created_at(previous.get("created_at"))
        item_created_at = _reaction_created_at(item.get("created_at"))
        if item_created_at is not None and (
            previous_created_at is None or item_created_at < previous_created_at
        ):
            canonical[existing_position] = item
    return canonical


def _canonicalize_staged_history_reactions(connection: Connection) -> None:
    updates: list[dict[str, Any]] = []
    rows = connection.execute(STAGED_HISTORY_SELECT_SQL).mappings()
    for row in rows:
        payload = row["payload"]
        if not isinstance(payload, dict):
            continue
        reactions = payload.get("reactions")
        canonical_reactions = _canonical_staged_reactions(reactions)
        if canonical_reactions == reactions:
            continue
        updates.append(
            {
                "export_id": row["export_id"],
                "export_domain": row["export_domain"],
                "message_id": row["message_id"],
                "message_domain": row["message_domain"],
                "payload": json.dumps(
                    {**payload, "reactions": canonical_reactions},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            }
        )
    if updates:
        connection.execute(STAGED_HISTORY_UPDATE_SQL, updates)


def upgrade() -> None:
    op.execute(REACTION_MAPPING_SQL)
    op.execute(REACTION_MERGE_SQL)
    op.execute(REACTION_DELETE_LEGACY_SQL)
    connection = op.get_bind()
    _canonicalize_forum_defaults(connection)
    _canonicalize_staged_history_reactions(connection)


def downgrade() -> None:
    # Canonical identities have no unique inverse: selector variants may have
    # merged, and invalid optional forum defaults may have been cleared. Older
    # application versions already accept the canonical values.
    pass
