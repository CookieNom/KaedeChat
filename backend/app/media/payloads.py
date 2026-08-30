from __future__ import annotations

from app.db.models import Attachment

INTERNAL_REJECTED_SCAN_STATUSES = frozenset({"infected", "quarantined", "rejected"})


def public_scan_status(status: str) -> str:
    """Collapse private safety decisions into one client-visible terminal state."""

    return "rejected" if status in INTERNAL_REJECTED_SCAN_STATUSES else status


def attachment_payload(
    attachment: Attachment, *, include_lifecycle: bool = True
) -> dict[str, object]:
    """Render an attachment without exposing its internal moderation decision."""

    payload: dict[str, object] = {
        "id": str(attachment.id),
        "origin_domain": attachment.origin_domain,
        "filename": attachment.filename,
        "content_type": attachment.detected_content_type or attachment.content_type,
        "size": attachment.size,
        "width": attachment.width,
        "height": attachment.height,
        "duration_secs": attachment.duration_secs,
        "waveform": attachment.waveform,
        "blurhash": attachment.blurhash,
        "scan_status": public_scan_status(attachment.scan_status),
        "encryption_mode": attachment.encryption_mode,
        "encryption_protocol": attachment.encryption_protocol,
        "variants": attachment.variants,
    }
    if include_lifecycle:
        payload.update(
            {
                "purpose": attachment.purpose,
                "finalized_at": (
                    attachment.finalized_at.isoformat()
                    if attachment.finalized_at is not None
                    else None
                ),
            }
        )
    return payload


def federation_attachment_payload(attachment: Attachment) -> dict[str, object]:
    """Add authority-only integrity metadata to an attachment projection.

    The digest is never returned by client message APIs.  A plaintext room
    authority needs it to attest an immutable forward without downloading a
    remote object or trusting a caller-supplied hash.
    """

    payload = attachment_payload(attachment)
    if attachment.encryption_mode == "plaintext" and attachment.content_sha256 is not None:
        payload["content_sha256"] = attachment.content_sha256
    return payload


def attachment_update_payload(
    attachment: Attachment,
    *,
    message_id: int,
    message_domain: str,
    channel_id: int,
    channel_domain: str,
) -> dict[str, object]:
    """Render a channel-scoped attachment update for Gateway filtering."""

    return {
        "message_id": str(message_id),
        "message_domain": message_domain,
        "channel_id": str(channel_id),
        "channel_domain": channel_domain,
        "attachment": attachment_payload(attachment),
    }


def terminal_attachment_update_payload(
    attachment: Attachment,
    *,
    message_id: int,
    message_domain: str,
    channel_id: int,
    channel_domain: str,
) -> dict[str, object]:
    """Render an origin tombstone without mutating or disclosing its cause."""

    payload = attachment_update_payload(
        attachment,
        message_id=message_id,
        message_domain=message_domain,
        channel_id=channel_id,
        channel_domain=channel_domain,
    )
    projection = payload["attachment"]
    if not isinstance(projection, dict):
        raise RuntimeError("attachment projection is malformed")
    projection["scan_status"] = "rejected"
    return payload
