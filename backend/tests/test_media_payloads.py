from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.chat.payloads import attachment_payload as chat_attachment_payload
from app.core.federation import canonical_json
from app.db.models import Attachment
from app.media.payloads import (
    attachment_update_payload,
    federation_attachment_payload,
    public_scan_status,
    terminal_attachment_update_payload,
)
from app.media.service import attachment_payload as service_attachment_payload


def attachment(status: str) -> Attachment:
    return Attachment(
        id=7,
        origin_domain="alpha.localhost",
        uploader_id=3,
        uploader_domain="alpha.localhost",
        filename="photo.png",
        content_type="image/png",
        size=128,
        object_key="alpha.localhost/7/staging/original",
        scan_status=status,
        encryption_mode="plaintext",
        purpose="attachment",
        finalized_at=datetime.now(UTC),
        variants={},
    )


@pytest.mark.parametrize("internal", ["infected", "quarantined", "rejected"])
def test_non_admin_attachment_payloads_collapse_internal_rejection_reason(
    internal: str,
) -> None:
    item = attachment(internal)

    assert public_scan_status(internal) == "rejected"
    assert service_attachment_payload(item)["scan_status"] == "rejected"
    assert chat_attachment_payload(item)["scan_status"] == "rejected"


@pytest.mark.parametrize("status", ["pending", "clean", "failed", "encrypted"])
def test_public_attachment_status_preserves_non_rejection_states(status: str) -> None:
    assert public_scan_status(status) == status


def test_attachment_update_is_channel_scoped_and_hides_quarantine() -> None:
    payload = attachment_update_payload(
        attachment("quarantined"),
        message_id=11,
        message_domain="alpha.localhost",
        channel_id=13,
        channel_domain="alpha.localhost",
    )

    assert payload["message_id"] == "11"
    assert payload["message_domain"] == "alpha.localhost"
    assert payload["channel_id"] == "13"
    assert payload["channel_domain"] == "alpha.localhost"
    assert payload["attachment"]["scan_status"] == "rejected"  # type: ignore[index]


def test_encrypted_tombstone_projects_rejected_without_invalid_stored_state() -> None:
    item = attachment("encrypted")
    item.encryption_mode = "e2ee"
    item.encryption_protocol = "kaede-file-v1"
    item.deleted_at = datetime.now(UTC)

    payload = terminal_attachment_update_payload(
        item,
        message_id=11,
        message_domain="alpha.localhost",
        channel_id=13,
        channel_domain="alpha.localhost",
    )

    assert item.scan_status == "encrypted"
    assert payload["attachment"]["scan_status"] == "rejected"  # type: ignore[index]


@pytest.mark.parametrize(
    "duration, expected", [(None, None), (0.3, 1), (2.0, 2), (2.25, 3), (1200.0, 1200)]
)
@pytest.mark.parametrize(
    "render", [chat_attachment_payload, service_attachment_payload, federation_attachment_payload]
)
def test_voice_attachment_projection_can_be_signed(duration, expected, render):
    item = attachment("pending")
    item.filename = "voice.m4a"
    item.content_type = "audio/mp4"
    item.duration_secs = duration
    item.waveform = "AA==" if duration is not None else None
    payload = render(item)
    assert payload["duration_secs"] == expected
    assert payload["duration_secs"] is None or type(payload["duration_secs"]) is int
    assert canonical_json({"message": {"attachments": [payload]}})
    # Display/signing precision must not modify the stored recording metadata.
    assert item.duration_secs == duration
