from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from app.core.settings import DOMAIN_RE
from app.core.types import MAX_SNOWFLAKE

type QualifiedReference = tuple[int, str]

DEFAULT_MESSAGE_REFERENCE_TYPE = 0
FORWARD_MESSAGE_REFERENCE_TYPE = 1

# Discord message types whose public wire object has a structurally defined
# DEFAULT message reference.  Kaede qualifies every snowflake with its owning
# domain so the same object remains unambiguous after federation.
MESSAGE_REFERENCE_WITH_MESSAGE_TYPES = frozenset({6, 19, 21, 23, 46})
MESSAGE_REFERENCE_WITHOUT_MESSAGE_TYPES = frozenset({12, 18})


def _canonical_internal_ref(value: QualifiedReference, label: str) -> QualifiedReference:
    identifier, domain = value
    if (
        isinstance(identifier, bool)
        or not isinstance(identifier, int)
        or not 0 <= identifier <= MAX_SNOWFLAKE
    ):
        raise ValueError(f"{label} id is invalid")
    if (
        not isinstance(domain, str)
        or domain != domain.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(domain)
    ):
        raise ValueError(f"{label} domain is invalid")
    return identifier, domain


def _canonical_wire_ref(
    value: Mapping[str, object],
    id_key: str,
    domain_key: str,
    label: str,
) -> QualifiedReference | None:
    has_id = id_key in value
    has_domain = domain_key in value
    if has_id != has_domain:
        raise ValueError(f"{label} reference is incomplete")
    if not has_id:
        return None
    raw_id = value[id_key]
    raw_domain = value[domain_key]
    if (
        not isinstance(raw_id, str)
        or not raw_id
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or (len(raw_id) > 1 and raw_id.startswith("0"))
    ):
        raise ValueError(f"{label} id is invalid")
    identifier = int(raw_id)
    if identifier > MAX_SNOWFLAKE:
        raise ValueError(f"{label} id is invalid")
    if (
        not isinstance(raw_domain, str)
        or raw_domain != raw_domain.rstrip(".").lower()
        or not DOMAIN_RE.fullmatch(raw_domain)
    ):
        raise ValueError(f"{label} domain is invalid")
    return identifier, raw_domain


def build_qualified_message_reference(
    *,
    message_type: int,
    channel_ref: QualifiedReference,
    message_ref: QualifiedReference | None = None,
    guild_ref: QualifiedReference | None = None,
) -> dict[str, object]:
    """Build one exact Discord-compatible, federation-qualified reference.

    The helper deliberately accepts only message types with a defined DEFAULT
    reference shape.  Callers therefore cannot accidentally persist an
    arbitrary channel/guild attribution on an ordinary message.
    """

    if isinstance(message_type, bool) or not isinstance(message_type, int):
        raise ValueError("message reference type is invalid")
    channel_id, channel_domain = _canonical_internal_ref(channel_ref, "channel")
    canonical_message = (
        _canonical_internal_ref(message_ref, "message") if message_ref is not None else None
    )
    canonical_guild = _canonical_internal_ref(guild_ref, "guild") if guild_ref is not None else None
    if message_type in MESSAGE_REFERENCE_WITH_MESSAGE_TYPES:
        if canonical_message is None:
            raise ValueError("message reference requires a message")
    elif message_type in MESSAGE_REFERENCE_WITHOUT_MESSAGE_TYPES:
        if canonical_message is not None:
            raise ValueError("message reference cannot contain a message")
        if canonical_guild is None:
            raise ValueError("message reference requires a guild")
    else:
        raise ValueError("message type does not define a stored message reference")
    if message_type == 46 and canonical_guild is not None:
        raise ValueError("poll-result references do not contain a guild")

    result: dict[str, object] = {"type": DEFAULT_MESSAGE_REFERENCE_TYPE}
    if canonical_message is not None:
        result.update(
            {
                "message_id": str(canonical_message[0]),
                "message_domain": canonical_message[1],
            }
        )
    result.update(
        {
            "channel_id": str(channel_id),
            "channel_domain": channel_domain,
        }
    )
    if canonical_guild is not None:
        result.update(
            {
                "guild_id": str(canonical_guild[0]),
                "guild_domain": canonical_guild[1],
            }
        )
    return result


def normalize_qualified_message_reference(
    value: object,
    *,
    require_message: bool | None = None,
    require_guild: bool | None = None,
    allow_forward: bool = False,
    label: str = "message",
) -> dict[str, object]:
    """Validate an untrusted reference and return its sole canonical form."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} reference is invalid")
    raw_type = value.get("type")
    if isinstance(raw_type, bool) or not isinstance(raw_type, int):
        raise ValueError(f"{label} reference type is invalid")
    if raw_type == FORWARD_MESSAGE_REFERENCE_TYPE:
        if not allow_forward or set(value) != {"type"}:
            raise ValueError(f"{label} forward reference is invalid")
        return {"type": FORWARD_MESSAGE_REFERENCE_TYPE}
    if raw_type != DEFAULT_MESSAGE_REFERENCE_TYPE:
        raise ValueError(f"{label} reference type is invalid")

    allowed = {
        "type",
        "message_id",
        "message_domain",
        "channel_id",
        "channel_domain",
        "guild_id",
        "guild_domain",
    }
    if not set(value) <= allowed:
        raise ValueError(f"{label} reference contains unknown fields")
    message_ref = _canonical_wire_ref(
        value,
        "message_id",
        "message_domain",
        f"{label} message",
    )
    channel_ref = _canonical_wire_ref(
        value,
        "channel_id",
        "channel_domain",
        f"{label} channel",
    )
    guild_ref = _canonical_wire_ref(
        value,
        "guild_id",
        "guild_domain",
        f"{label} guild",
    )
    if channel_ref is None:
        raise ValueError(f"{label} reference is missing its channel")
    if require_message is not None and (message_ref is not None) != require_message:
        qualifier = "requires" if require_message else "cannot contain"
        raise ValueError(f"{label} reference {qualifier} a message")
    if require_guild is not None and (guild_ref is not None) != require_guild:
        qualifier = "requires" if require_guild else "cannot contain"
        raise ValueError(f"{label} reference {qualifier} a guild")

    normalized: dict[str, object] = {"type": DEFAULT_MESSAGE_REFERENCE_TYPE}
    if message_ref is not None:
        normalized.update(
            {
                "message_id": str(message_ref[0]),
                "message_domain": message_ref[1],
            }
        )
    normalized.update(
        {
            "channel_id": str(channel_ref[0]),
            "channel_domain": channel_ref[1],
        }
    )
    if guild_ref is not None:
        normalized.update(
            {
                "guild_id": str(guild_ref[0]),
                "guild_domain": guild_ref[1],
            }
        )
    if dict(value) != normalized:
        # Reject explicit nulls and noncanonical representations rather than
        # silently signing or storing a semantically different object.
        raise ValueError(f"{label} reference is not canonical")
    return normalized


def _projection_ref(
    projection: Mapping[str, object],
    prefix: str,
) -> QualifiedReference | None:
    identifier = projection.get(f"{prefix}_id")
    domain = projection.get(f"{prefix}_domain")
    if identifier is None or domain is None:
        return None
    return int(cast(str, identifier)), cast(str, domain)


def validate_message_reference_projection(
    value: object,
    *,
    message_type: int,
    channel_ref: QualifiedReference,
    guild_ref: QualifiedReference | None,
    referenced_message_ref: QualifiedReference | None,
    forwarded_message_ref: QualifiedReference | None = None,
    forwarded_channel_ref: QualifiedReference | None = None,
    has_forward_snapshot: bool = False,
    is_crosspost: bool = False,
    label: str = "message",
) -> dict[str, object] | None:
    """Bind a wire reference to independently validated immutable fields.

    Type 12 is the one intentional exception: it points at the followed source
    channel rather than the message's destination channel.  Its exact
    channel+guild shape is retained for the source authority/UI to resolve.
    """

    channel_ref = _canonical_internal_ref(channel_ref, "channel")
    guild_ref = _canonical_internal_ref(guild_ref, "guild") if guild_ref is not None else None
    referenced_message_ref = (
        _canonical_internal_ref(referenced_message_ref, "message")
        if referenced_message_ref is not None
        else None
    )
    forwarded_message_ref = (
        _canonical_internal_ref(forwarded_message_ref, "forwarded message")
        if forwarded_message_ref is not None
        else None
    )
    forwarded_channel_ref = (
        _canonical_internal_ref(forwarded_channel_ref, "forwarded channel")
        if forwarded_channel_ref is not None
        else None
    )

    reference_required = bool(
        message_type in {6, 12, 46}
        or referenced_message_ref is not None
        or is_crosspost
        or has_forward_snapshot
    )
    if value is None:
        if reference_required:
            raise ValueError(f"{label} is missing its message reference")
        return None

    normalized = normalize_qualified_message_reference(
        value,
        allow_forward=has_forward_snapshot,
        label=label,
    )
    if normalized["type"] == FORWARD_MESSAGE_REFERENCE_TYPE:
        if not has_forward_snapshot or is_crosspost or referenced_message_ref is not None:
            raise ValueError(f"{label} forward reference is inconsistent")
        return normalized

    if message_type == 12:
        return normalize_qualified_message_reference(
            value,
            require_message=False,
            require_guild=True,
            label=label,
        )
    if message_type == 18:
        return normalize_qualified_message_reference(
            value,
            require_message=False,
            require_guild=True,
            label=label,
        )
    if message_type == 6:
        if referenced_message_ref is None:
            raise ValueError(f"{label} pin reference is missing its message")
        expected = build_qualified_message_reference(
            message_type=message_type,
            message_ref=referenced_message_ref,
            channel_ref=channel_ref,
            guild_ref=guild_ref,
        )
        if normalized != expected:
            raise ValueError(f"{label} pin reference does not match its message")
        return normalized
    if message_type == 46:
        if referenced_message_ref is None:
            raise ValueError(f"{label} poll-result reference is missing its message")
        expected = build_qualified_message_reference(
            message_type=message_type,
            message_ref=referenced_message_ref,
            channel_ref=channel_ref,
        )
        if normalized != expected:
            raise ValueError(f"{label} poll-result reference does not match its message")
        return normalized
    if is_crosspost:
        if forwarded_message_ref is None or forwarded_channel_ref is None:
            raise ValueError(f"{label} crosspost reference is incomplete")
        expected = {
            "type": DEFAULT_MESSAGE_REFERENCE_TYPE,
            "message_id": str(forwarded_message_ref[0]),
            "message_domain": forwarded_message_ref[1],
            "channel_id": str(forwarded_channel_ref[0]),
            "channel_domain": forwarded_channel_ref[1],
        }
        if normalized != expected:
            raise ValueError(f"{label} crosspost reference does not match its source")
        return normalized
    if referenced_message_ref is not None:
        if message_type not in {0, 19, 21, 23}:
            raise ValueError(f"{label} type cannot contain a reply reference")
        if (
            _projection_ref(normalized, "message") != referenced_message_ref
            or _projection_ref(normalized, "channel") != channel_ref
        ):
            raise ValueError(f"{label} reply reference does not match its source")
        projected_guild = _projection_ref(normalized, "guild")
        if projected_guild is not None and projected_guild != guild_ref:
            raise ValueError(f"{label} reply reference has the wrong guild")
        return normalized
    raise ValueError(f"{label} contains an unsupported message reference")


def validate_channel_follow_message_fields(
    raw: Mapping[str, object],
    rich: Mapping[str, object],
    *,
    message_type: int,
    channel_type: int,
    content: object,
    e2ee: object,
    attachments: Sequence[object],
    webhook: object,
    mention_user_refs: Sequence[object],
    mention_role_refs: Sequence[object],
    mention_everyone: bool,
    flags: int,
    tts: object,
    client_nonce: object,
    referenced_message_ref: QualifiedReference | None,
    label: str = "channel follow notice",
) -> None:
    """Enforce Discord's immutable type-12 system-message field set once."""

    if message_type != 12:
        return
    if channel_type != 0:
        raise ValueError(f"{label} target is not a text channel")
    if (
        not isinstance(content, str)
        or not 1 <= len(content) <= 100
        or content != content.strip()
        or e2ee is not None
        or attachments
        or rich.get("embeds")
        or rich.get("components")
        or rich.get("sticker_items")
        or rich.get("poll") is not None
        or raw.get("message_snapshots", []) != []
        or rich.get("application_ref") is not None
        or rich.get("interaction_metadata") is not None
        or rich.get("forwarded_ref") is not None
        or rich.get("forwarded_channel_ref") is not None
        or rich.get("forward_snapshot") is not None
        or rich.get("has_encrypted_forward")
        or webhook is not None
        or mention_user_refs
        or mention_role_refs
        or mention_everyone
        or flags != 0
        or tts is not False
        or client_nonce is not None
        or referenced_message_ref is not None
    ):
        raise ValueError(f"{label} fields are invalid")


__all__ = [
    "DEFAULT_MESSAGE_REFERENCE_TYPE",
    "FORWARD_MESSAGE_REFERENCE_TYPE",
    "build_qualified_message_reference",
    "normalize_qualified_message_reference",
    "validate_channel_follow_message_fields",
    "validate_message_reference_projection",
]
