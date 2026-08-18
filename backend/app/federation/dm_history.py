from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from app.chat.e2ee import validate_e2ee_envelope, validate_e2ee_message_projection
from app.core.settings import Settings
from app.federation.network import FederationNetworkError, normalize_domain
from app.federation.replication import (
    database_snowflake,
    remote_media_dimensions,
    sanitized_remote_blurhash,
    sanitized_remote_variants,
    validate_snowflake_timestamp,
)
from app.federation.schemas import RemoteUserProfile
from app.media.processing import normalize_declared_type, sanitize_filename

MAX_DM_HISTORY_RESPONSE_BYTES = 2 * 1024 * 1024
DM_HISTORY_MEDIA_CAPABILITY_SECONDS = 15 * 60


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


def _profile_payload(profile: RemoteUserProfile) -> dict[str, object]:
    return {
        **profile.model_dump(mode="json"),
        "profile_resolved": True,
        "handle": f"{profile.username}@{profile.origin_domain}",
    }


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
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


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
            or reference[1] != author_ref[1]
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
        if content is None and e2ee is None and not raw_attachments and deleted_at is None:
            raise FederationNetworkError("DM history message has no content")
        message_type = raw.get("message_type", 0)
        flags = raw.get("flags", 0)
        if (
            isinstance(message_type, bool)
            or not isinstance(message_type, int)
            or not 0 <= message_type <= 2_147_483_647
            or isinstance(flags, bool)
            or not isinstance(flags, int)
            or not 0 <= flags <= 2_147_483_647
        ):
            raise FederationNetworkError("DM history message flags are invalid")
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
        created_at_raw = raw.get("created_at")
        if not isinstance(created_at_raw, str) or not 1 <= len(created_at_raw) <= 64:
            raise FederationNetworkError("DM history creation timestamp is invalid")
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
        except (ValueError, OverflowError) as exc:
            raise FederationNetworkError("DM history creation timestamp is invalid") from exc
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
        result.append(
            {
                "id": str(reference[0]),
                "origin_domain": reference[1],
                "channel_id": str(conversation_ref[0]),
                "channel_domain": conversation_ref[1],
                "author_id": str(author_ref[0]),
                "author_domain": author_ref[1],
                "author": _profile_payload(profile),
                "content": None if deleted_at is not None else content,
                "e2ee": None if deleted_at is not None else e2ee,
                "message_type": message_type,
                "flags": flags,
                "client_nonce": client_nonce,
                "referenced_message_id": referenced_id,
                "referenced_message_domain": referenced_domain,
                "mention_user_refs": mention_refs,
                "attachments": attachments,
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
