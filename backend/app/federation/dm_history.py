from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.chat.custom_emojis import canonical_reaction_emoji
from app.chat.e2ee import validate_e2ee_envelope, validate_e2ee_message_projection
from app.chat.message_flags import MESSAGE_FLAG_HAS_SNAPSHOT
from app.chat.message_references import validate_message_reference_projection
from app.chat.pins import PIN_NOTICE_MESSAGE_TYPE
from app.core.base64url import encode_base64url
from app.core.settings import Settings
from app.federation.message_content import validate_replicated_rich_projection
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.replication import (
    client_user_payload_from_profile,
    database_snowflake,
    remote_media_dimensions,
    sanitized_remote_blurhash,
    sanitized_remote_variants,
    validate_snowflake_timestamp,
)
from app.federation.schemas import RemoteUserProfile
from app.media.processing import normalize_declared_type, sanitize_filename
from app.media.schemas import validate_voice_attachment_metadata

MAX_DM_HISTORY_RESPONSE_BYTES = 2 * 1024 * 1024
DM_HISTORY_MEDIA_CAPABILITY_SECONDS = 15 * 60
MAX_DM_HISTORY_REACTIONS_PER_MESSAGE = 10_000


@dataclass(frozen=True, slots=True)
class ValidatedDMHistoryPage:
    messages: list[dict[str, object]]
    complete: bool
    next_before: tuple[int, str] | None
    ignored_local_refs: frozenset[tuple[int, str]] = frozenset()


def merge_dm_history_messages(
    remote_messages: list[dict[str, object]],
    local_messages: list[dict[str, object]],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """Merge one logical page while treating the local database as trusted.

    A remote DM authority orders the conversation, but it is never allowed to
    replace a locally-authored body or its attachment metadata. Inserting the
    remote page first gives the local copy deterministic precedence for the
    same composite reference.
    """

    merged = {
        (str(item["id"]), str(item["origin_domain"])): item
        for item in [*remote_messages, *local_messages]
    }
    return sorted(
        merged.values(),
        key=lambda item: (int(str(item["id"])), str(item["origin_domain"])),
        reverse=True,
    )[:limit]


def dm_history_page_is_complete(
    *,
    remote_complete: bool,
    merged_messages: list[dict[str, object]],
    remote_messages: list[dict[str, object]],
    local_has_more: bool,
) -> bool:
    """Return true only when neither the authority nor local durable rows remain."""

    if not remote_complete or local_has_more or not merged_messages:
        return False
    oldest = (
        int(str(merged_messages[-1]["id"])),
        str(merged_messages[-1]["origin_domain"]),
    )
    # A complete authority response may still have supplied rows that were
    # pushed out of this merged page by interleaved durable local messages.
    return not any(
        (int(str(item["id"])), str(item["origin_domain"])) < oldest for item in remote_messages
    )


def _optional_timestamp(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise FederationNetworkError(f"DM history {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FederationNetworkError(f"DM history {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise FederationNetworkError(f"DM history {field} must include a timezone")
    return parsed.isoformat()


def _validated_reaction_summary(
    raw_counts: object,
    raw_reacted: object,
) -> tuple[dict[str, int], list[str]]:
    """Canonicalize one signed DM history reaction summary after verification."""

    if (
        not isinstance(raw_counts, dict)
        or len(raw_counts) > MAX_DM_HISTORY_REACTIONS_PER_MESSAGE
        or not isinstance(raw_reacted, list)
        or len(raw_reacted) > MAX_DM_HISTORY_REACTIONS_PER_MESSAGE
    ):
        raise FederationNetworkError("DM history reaction summary is invalid")
    counts: dict[str, int] = {}
    total = 0
    try:
        for raw_emoji, raw_count in raw_counts.items():
            if (
                not isinstance(raw_emoji, str)
                or not 1 <= len(raw_emoji) <= 320
                or isinstance(raw_count, bool)
                or not isinstance(raw_count, int)
                or raw_count < 1
            ):
                raise ValueError
            emoji = canonical_reaction_emoji(raw_emoji)
            counts[emoji] = counts.get(emoji, 0) + raw_count
            total += raw_count
            if total > MAX_DM_HISTORY_REACTIONS_PER_MESSAGE:
                raise ValueError
        reacted: list[str] = []
        seen: set[str] = set()
        for raw_emoji in raw_reacted:
            if not isinstance(raw_emoji, str) or not 1 <= len(raw_emoji) <= 320:
                raise ValueError
            emoji = canonical_reaction_emoji(raw_emoji)
            if emoji not in counts:
                raise ValueError
            if emoji not in seen:
                seen.add(emoji)
                reacted.append(emoji)
    except (TypeError, ValueError):
        raise FederationNetworkError("DM history reaction summary is invalid") from None
    return counts, reacted


def _history_media_signature(
    settings: Settings,
    *,
    conversation_ref: tuple[int, str],
    message_ref: tuple[int, str],
    attachment_ref: tuple[int, str],
    variant: str,
    expires: int,
) -> str:
    body = "\n".join(
        (
            str(conversation_ref[0]),
            conversation_ref[1],
            str(message_ref[0]),
            message_ref[1],
            str(attachment_ref[0]),
            attachment_ref[1],
            variant,
            str(expires),
        )
    ).encode("utf-8")
    digest = hmac.new(settings.secret_key_bytes, body, hashlib.sha256).digest()
    return encode_base64url(digest)


def history_media_path(
    settings: Settings,
    *,
    conversation_ref: tuple[int, str],
    message_ref: tuple[int, str],
    attachment_ref: tuple[int, str],
    variant: str = "original",
    now: datetime | None = None,
) -> str:
    expires = int((now or datetime.now(UTC)).timestamp()) + DM_HISTORY_MEDIA_CAPABILITY_SECONDS
    signature = _history_media_signature(
        settings,
        conversation_ref=conversation_ref,
        message_ref=message_ref,
        attachment_ref=attachment_ref,
        variant=variant,
        expires=expires,
    )
    return (
        f"/api/v1/dms/{conversation_ref[0]}@{conversation_ref[1]}/history-media/"
        f"{message_ref[0]}@{message_ref[1]}/{attachment_ref[0]}@{attachment_ref[1]}/"
        f"{variant}?expires={expires}&token={signature}"
    )


def verify_history_media_capability(
    settings: Settings,
    *,
    conversation_ref: tuple[int, str],
    message_ref: tuple[int, str],
    attachment_ref: tuple[int, str],
    variant: str,
    expires: int,
    token: str,
    now: datetime | None = None,
) -> bool:
    return (
        history_media_capability_status(
            settings,
            conversation_ref=conversation_ref,
            message_ref=message_ref,
            attachment_ref=attachment_ref,
            variant=variant,
            expires=expires,
            token=token,
            now=now,
        )
        == "fresh"
    )


def history_media_capability_status(
    settings: Settings,
    *,
    conversation_ref: tuple[int, str],
    message_ref: tuple[int, str],
    attachment_ref: tuple[int, str],
    variant: str,
    expires: int,
    token: str,
    now: datetime | None = None,
) -> Literal["fresh", "renewable", "invalid"]:
    """Classify a signed history-media path without weakening its binding.

    An expired token is only a renewal proof: the authenticated media route
    still checks current DM membership and asks the attachment origin to
    attest the exact conversation/message/attachment tuple before serving any
    bytes. This lets a rendered old-history message recover after 15 minutes
    without turning the URL into an unauthenticated permanent capability.
    """

    current = int((now or datetime.now(UTC)).timestamp())
    if expires > current + DM_HISTORY_MEDIA_CAPABILITY_SECONDS + 60:
        return "invalid"
    if not 40 <= len(token) <= 48 or not token.isascii():
        return "invalid"
    expected = _history_media_signature(
        settings,
        conversation_ref=conversation_ref,
        message_ref=message_ref,
        attachment_ref=attachment_ref,
        variant=variant,
        expires=expires,
    )
    if not hmac.compare_digest(expected, token):
        return "invalid"
    return "renewable" if expires < current else "fresh"


def _validated_attachment(
    raw: object,
    *,
    settings: Settings,
    author_domain: str,
    conversation_ref: tuple[int, str],
    message_ref: tuple[int, str],
) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise FederationNetworkError("DM history attachment is invalid")
    try:
        attachment_id = database_snowflake(raw.get("id"), "DM history attachment id")
        origin = normalize_domain(str(raw.get("origin_domain", "")))
    except (ValueError, FederationNetworkError) as exc:
        raise FederationNetworkError("DM history attachment reference is invalid") from exc
    if origin != author_domain:
        raise FederationNetworkError("DM history attachment origin is not its author")
    filename = raw.get("filename")
    content_type_raw = raw.get("content_type")
    size = raw.get("size")
    if not isinstance(filename, str) or sanitize_filename(filename) != filename:
        raise FederationNetworkError("DM history attachment filename is invalid")
    if not isinstance(content_type_raw, str):
        raise FederationNetworkError("DM history attachment type is invalid")
    try:
        content_type = normalize_declared_type(content_type_raw)
    except ValueError as exc:
        raise FederationNetworkError("DM history attachment type is invalid") from exc
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 1 <= size <= settings.media_max_attachment_bytes
    ):
        raise FederationNetworkError("DM history attachment size is invalid")
    try:
        width, height = remote_media_dimensions(raw)
        blurhash = sanitized_remote_blurhash(raw.get("blurhash"))
        variants = sanitized_remote_variants(
            raw.get("variants", {}), max_bytes=settings.media_max_attachment_bytes
        )
    except ValueError as exc:
        raise FederationNetworkError("DM history attachment metadata is invalid") from exc
    encryption_mode = raw.get("encryption_mode", "plaintext")
    encryption_protocol = raw.get("encryption_protocol")
    duration_raw = raw.get("duration_secs")
    duration_secs = (
        float(duration_raw)
        if isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool)
        else None
    )
    waveform = raw.get("waveform")
    if (
        duration_raw is not None
        and duration_secs is None
        or (waveform is not None and not isinstance(waveform, str))
    ):
        raise FederationNetworkError("DM history voice metadata is invalid")
    try:
        validate_voice_attachment_metadata(
            content_type=content_type,
            encryption_mode=str(encryption_mode),
            duration_secs=duration_secs,
            waveform=waveform,
        )
    except ValueError as exc:
        raise FederationNetworkError("DM history voice metadata is invalid") from exc
    if encryption_mode == "e2ee":
        if (
            encryption_protocol != "kaede-file-v1"
            or filename != "encrypted-file"
            or content_type != "application/octet-stream"
            or raw.get("scan_status") not in {"pending", "encrypted"}
        ):
            raise FederationNetworkError("encrypted DM history attachment is invalid")
        rendered_scan_status = "encrypted"
    elif encryption_mode == "plaintext" and encryption_protocol is None:
        rendered_scan_status = "clean"
    else:
        raise FederationNetworkError("DM history attachment encryption policy is invalid")
    if encryption_mode == "plaintext" and raw.get("scan_status") != "clean":
        raise FederationNetworkError("DM history attachment is not available")
    attachment_ref = (attachment_id, origin)
    rendered: dict[str, object] = {
        "id": str(attachment_id),
        "origin_domain": origin,
        "filename": filename,
        "content_type": content_type,
        "size": size,
        "width": width,
        "height": height,
        "duration_secs": duration_secs,
        "waveform": waveform,
        "blurhash": blurhash,
        "scan_status": rendered_scan_status,
        "encryption_mode": encryption_mode,
        "encryption_protocol": encryption_protocol,
        "variants": variants,
    }
    # Locally-authored media keeps its ordinary authoritative Attachment row
    # and is served by the normal authenticated media route.  Only remote
    # bytes need the short-lived, same-origin history capability.
    if origin != settings.domain:
        rendered["history_media_url"] = history_media_path(
            settings,
            conversation_ref=conversation_ref,
            message_ref=message_ref,
            attachment_ref=attachment_ref,
        )
    return rendered


def validate_dm_history_page(
    body: object,
    *,
    settings: Settings,
    conversation_ref: tuple[int, str],
    authority_domain: str,
    participant_refs: set[tuple[int, str]],
    trusted_profiles: Mapping[tuple[int, str], Mapping[str, object]],
    before: tuple[int, str] | None,
    limit: int,
) -> ValidatedDMHistoryPage:
    """Strictly validate and sanitize an untrusted authority history page."""

    if not isinstance(body, dict):
        raise FederationNetworkError("DM history authority returned a non-object page")
    if (
        body.get("conversation_id") != str(conversation_ref[0])
        or body.get("conversation_domain") != conversation_ref[1]
    ):
        raise FederationNetworkError("DM history authority returned the wrong conversation")
    raw_messages = body.get("messages")
    complete = body.get("complete")
    if (
        not isinstance(raw_messages, list)
        or len(raw_messages) > limit
        or not isinstance(complete, bool)
    ):
        raise FederationNetworkError("DM history authority returned an invalid page")
    result: list[dict[str, object]] = []
    ignored_local_refs: set[tuple[int, str]] = set()
    previous = before
    seen: set[tuple[int, str]] = set()
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise FederationNetworkError("DM history authority returned an invalid message")
        message_type = raw.get("message_type", 0)
        if (
            isinstance(message_type, bool)
            or not isinstance(message_type, int)
            or not 0 <= message_type <= 2_147_483_647
        ):
            raise FederationNetworkError("DM history message type is invalid")
        try:
            reference = (
                database_snowflake(raw.get("id"), "DM history message id"),
                normalize_domain(str(raw.get("origin_domain", ""))),
            )
            channel_ref = (
                database_snowflake(raw.get("channel_id"), "DM history channel id"),
                normalize_domain(str(raw.get("channel_domain", ""))),
            )
            author_ref = (
                database_snowflake(raw.get("author_id"), "DM history author id"),
                normalize_domain(str(raw.get("author_domain", ""))),
            )
        except (ValueError, FederationNetworkError) as exc:
            raise FederationNetworkError(
                "DM history authority returned invalid references"
            ) from exc
        if (
            (previous is not None and reference >= previous)
            or reference in seen
            or channel_ref != conversation_ref
            or author_ref not in participant_refs
            or (
                reference[1] != author_ref[1]
                and not (
                    message_type in {PIN_NOTICE_MESSAGE_TYPE, 46}
                    and reference[1] == authority_domain
                )
            )
        ):
            raise FederationNetworkError("DM history authority returned mismatched references")
        # The authority orders the conversation, but it is not authoritative
        # for bodies authored on this home. Advance the signed cursor past
        # these rows and use only the local durable copy during merge.
        if author_ref[1] == settings.domain:
            ignored_local_refs.add(reference)
            seen.add(reference)
            previous = reference
            continue
        try:
            supplied_profile = RemoteUserProfile.model_validate(raw.get("author"))
        except (TypeError, ValueError) as exc:
            raise FederationNetworkError("DM history author profile is invalid") from exc
        if (int(supplied_profile.id), supplied_profile.origin_domain) != author_ref:
            raise FederationNetworkError("DM history author profile does not match the message")
        # A DM authority is authoritative for message ordering/content, but it
        # is not allowed to rewrite another instance's mutable user profile.
        # Use locally trusted/cached identity data unless the author is hosted
        # by the authority that signed this response.
        profile = supplied_profile
        if author_ref[1] != authority_domain:
            trusted_raw = trusted_profiles.get(author_ref)
            if trusted_raw is None:
                raise FederationNetworkError("DM history author is not locally trusted")
            try:
                profile = RemoteUserProfile.model_validate(trusted_raw)
            except (TypeError, ValueError) as exc:
                raise FederationNetworkError("trusted DM history author is invalid") from exc
            if (int(profile.id), profile.origin_domain) != author_ref:
                raise FederationNetworkError("trusted DM history author does not match")
        content = raw.get("content")
        try:
            e2ee = validate_e2ee_envelope(raw.get("e2ee"))
        except ValueError as exc:
            raise FederationNetworkError("DM history encrypted content is invalid") from exc
        if content is not None and (not isinstance(content, str) or not 1 <= len(content) <= 4_000):
            raise FederationNetworkError("DM history message content is invalid")
        if content is not None and e2ee is not None:
            raise FederationNetworkError("DM history message mixes plaintext and encryption")
        deleted_at = _optional_timestamp(raw.get("deleted_at"), "deleted timestamp")
        raw_attachments = raw.get("attachments", [])
        if not isinstance(raw_attachments, list) or len(raw_attachments) > 10:
            raise FederationNetworkError("DM history attachment list is invalid")
        created_at_raw = raw.get("created_at")
        if not isinstance(created_at_raw, str) or not 1 <= len(created_at_raw) <= 64:
            raise FederationNetworkError("DM history creation timestamp is invalid")
        tts = raw.get("tts", False)
        flags = raw.get("flags", 0)
        if (
            isinstance(flags, bool)
            or not isinstance(flags, int)
            or not 0 <= flags <= 2_147_483_647
            or not isinstance(tts, bool)
        ):
            raise FederationNetworkError("DM history message flags are invalid")
        try:
            created_at = datetime.fromisoformat(created_at_raw)
            if created_at.tzinfo is None:
                raise ValueError("DM history timestamp is missing a timezone")
            validate_snowflake_timestamp(
                reference[0],
                created_at,
                "DM history message",
                event_timestamp_ms=int(created_at.timestamp() * 1_000),
            )
            rich = validate_replicated_rich_projection(
                raw,
                message_id=reference[0],
                message_origin=reference[1],
                message_created_at=created_at,
                e2ee=e2ee,
                message_type=message_type,
                label="DM history message",
            )
        except (ValueError, OverflowError) as exc:
            raise FederationNetworkError("DM history rich content is invalid") from exc
        if bool(flags & MESSAGE_FLAG_HAS_SNAPSHOT) != (
            rich.forward_snapshot is not None or rich.has_encrypted_forward
        ):
            raise FederationNetworkError(
                "DM history snapshot flag does not match its forward projection"
            )
        forwarded_ref = rich.forwarded_ref
        if (
            content is None
            and e2ee is None
            and not raw_attachments
            and not rich.embeds
            and not rich.components
            and not rich.sticker_items
            and rich.poll is None
            and deleted_at is None
            and forwarded_ref is None
            and message_type != PIN_NOTICE_MESSAGE_TYPE
        ):
            raise FederationNetworkError("DM history message has no content")
        if forwarded_ref is not None and (
            deleted_at is not None or e2ee is not None or raw_attachments
        ):
            raise FederationNetworkError("DM history forward is invalid")
        client_nonce = raw.get("client_nonce")
        if client_nonce is not None and (
            not isinstance(client_nonce, str) or not 1 <= len(client_nonce) <= 64
        ):
            raise FederationNetworkError("DM history message nonce is invalid")
        mention_refs: list[dict[str, str]] = []
        raw_mentions = raw.get("mention_user_refs", [])
        if not isinstance(raw_mentions, list) or len(raw_mentions) > 5_000:
            raise FederationNetworkError("DM history mentions are invalid")
        mention_seen: set[tuple[int, str]] = set()
        for item in raw_mentions:
            if not isinstance(item, dict):
                raise FederationNetworkError("DM history mention is invalid")
            try:
                mention = (
                    database_snowflake(item.get("id"), "DM history mention id"),
                    normalize_domain(str(item.get("origin_domain", ""))),
                )
            except (ValueError, FederationNetworkError) as exc:
                raise FederationNetworkError("DM history mention is invalid") from exc
            if mention not in participant_refs or mention in mention_seen:
                raise FederationNetworkError("DM history mention is not a participant")
            mention_seen.add(mention)
            mention_refs.append({"id": str(mention[0]), "origin_domain": mention[1]})
        ref_id_raw = raw.get("referenced_message_id")
        ref_domain_raw = raw.get("referenced_message_domain")
        if (ref_id_raw is None) != (ref_domain_raw is None):
            raise FederationNetworkError("DM history reply reference is incomplete")
        referenced_id: str | None = None
        referenced_domain: str | None = None
        if ref_id_raw is not None:
            try:
                referenced_id = str(database_snowflake(ref_id_raw, "DM history reply message id"))
                referenced_domain = normalize_domain(str(ref_domain_raw))
            except (ValueError, FederationNetworkError) as exc:
                raise FederationNetworkError("DM history reply reference is invalid") from exc
        if forwarded_ref is not None and referenced_id is not None:
            raise FederationNetworkError("DM history forward cannot also be a reply")
        if message_type in {12, 18}:
            raise FederationNetworkError(
                "DM history message type cannot contain a guild channel reference"
            )
        try:
            message_reference = validate_message_reference_projection(
                raw.get("message_reference"),
                message_type=message_type,
                channel_ref=conversation_ref,
                guild_ref=None,
                referenced_message_ref=(
                    (int(referenced_id), referenced_domain)
                    if referenced_id is not None and referenced_domain is not None
                    else None
                ),
                forwarded_message_ref=forwarded_ref,
                forwarded_channel_ref=rich.forwarded_channel_ref,
                has_forward_snapshot=bool(
                    rich.forward_snapshot is not None or rich.has_encrypted_forward
                ),
                label="DM history message",
            )
        except ValueError as exc:
            raise FederationNetworkError("DM history message reference is invalid") from exc
        if message_type == PIN_NOTICE_MESSAGE_TYPE and (
            reference[1] != authority_domain
            or content is not None
            or e2ee is not None
            or raw_attachments
            or rich.embeds
            or rich.components
            or rich.sticker_items
            or rich.poll is not None
            or forwarded_ref is not None
            or rich.application_ref is not None
            or rich.interaction_metadata is not None
            or tts
            or flags != 0
            or client_nonce is not None
            or mention_refs
        ):
            raise FederationNetworkError("DM history pin notice fields are invalid")
        edited_at = _optional_timestamp(raw.get("edited_at"), "edited timestamp")
        if deleted_at is None:
            try:
                validate_e2ee_message_projection(
                    e2ee,
                    message_id=reference[0],
                    message_domain=reference[1],
                    edited=edited_at is not None,
                )
            except ValueError as exc:
                raise FederationNetworkError(
                    "DM history encrypted operation does not match its message"
                ) from exc
        attachments = [
            _validated_attachment(
                item,
                settings=settings,
                author_domain=author_ref[1],
                conversation_ref=conversation_ref,
                message_ref=reference,
            )
            for item in raw_attachments
        ]
        reaction_counts, reacted_emoji = _validated_reaction_summary(
            raw.get("reaction_counts", {}),
            raw.get("reacted_emoji", []),
        )
        result.append(
            {
                "id": str(reference[0]),
                "origin_domain": reference[1],
                "channel_id": str(conversation_ref[0]),
                "channel_domain": conversation_ref[1],
                "author_id": str(author_ref[0]),
                "author_domain": author_ref[1],
                "author": client_user_payload_from_profile(profile),
                "content": None if deleted_at is not None else content,
                "e2ee": None if deleted_at is not None else e2ee,
                "embeds": [] if deleted_at is not None else rich.embeds,
                "components": [] if deleted_at is not None else rich.components,
                "sticker_items": [] if deleted_at is not None else rich.sticker_items,
                "application_id": (
                    str(rich.application_ref[0])
                    if deleted_at is None and rich.application_ref is not None
                    else None
                ),
                "application_domain": (
                    rich.application_ref[1]
                    if deleted_at is None and rich.application_ref is not None
                    else None
                ),
                "interaction_metadata": (rich.interaction_metadata if deleted_at is None else None),
                "view_version": rich.view_version if deleted_at is None else 0,
                "view_persistent": rich.view_persistent if deleted_at is None else False,
                "view_expires_at": (
                    rich.view_expires_at.isoformat()
                    if deleted_at is None and rich.view_expires_at is not None
                    else None
                ),
                "interaction_integration_type": (
                    rich.interaction_integration_type if deleted_at is None else None
                ),
                "interaction_installation_ref": (
                    (
                        f"{rich.interaction_installation_ref[0]}@"
                        f"{rich.interaction_installation_ref[1]}"
                    )
                    if deleted_at is None and rich.interaction_installation_ref is not None
                    else None
                ),
                "interaction_installation_revision": (
                    str(rich.interaction_installation_revision)
                    if deleted_at is None and rich.interaction_installation_revision is not None
                    else None
                ),
                "poll": None if deleted_at is not None else raw.get("poll"),
                "message_type": message_type,
                "tts": tts,
                "flags": flags,
                "client_nonce": client_nonce,
                "referenced_message_id": referenced_id,
                "referenced_message_domain": referenced_domain,
                "forwarded_message_id": (
                    str(forwarded_ref[0])
                    if forwarded_ref is not None and rich.forward_snapshot is None
                    else None
                ),
                "forwarded_message_domain": (
                    forwarded_ref[1]
                    if forwarded_ref is not None and rich.forward_snapshot is None
                    else None
                ),
                "forwarded_message_ref": (
                    f"{forwarded_ref[0]}@{forwarded_ref[1]}"
                    if forwarded_ref is not None and rich.forward_snapshot is None
                    else None
                ),
                "forwarded_channel_id": None,
                "forwarded_channel_domain": None,
                "forward_snapshot": None,
                "message_snapshots": (
                    [{"message": rich.forward_snapshot}]
                    if rich.forward_snapshot is not None
                    else []
                ),
                "message_reference": message_reference,
                "mention_user_refs": mention_refs,
                "attachments": attachments,
                "reaction_counts": reaction_counts,
                "reacted_emoji": reacted_emoji,
                "webhook_id": None,
                "webhook": None,
                "edited_at": edited_at,
                "deleted_at": deleted_at,
                "created_at": created_at.isoformat(),
            }
        )
        seen.add(reference)
        previous = reference
    raw_next = body.get("next_before")
    next_before: tuple[int, str] | None = None
    if raw_next is not None:
        if not isinstance(raw_next, dict):
            raise FederationNetworkError("DM history next cursor is invalid")
        try:
            next_before = (
                database_snowflake(raw_next.get("id"), "DM history next message id"),
                normalize_domain(str(raw_next.get("origin_domain", ""))),
            )
        except (ValueError, FederationNetworkError) as exc:
            raise FederationNetworkError("DM history next cursor is invalid") from exc
    expected_next = previous if raw_messages and not complete else None
    if next_before != expected_next or (not raw_messages and not complete):
        raise FederationNetworkError("DM history cursor does not advance consistently")
    # The ordinary messages endpoint is intentionally a bare array for client
    # compatibility.  Mark the oldest response item so an exact-size final
    # authority page is still unambiguously terminal.
    if result:
        result[-1]["history_page_complete"] = complete
    return ValidatedDMHistoryPage(
        result,
        complete,
        next_before,
        frozenset(ignored_local_refs),
    )
