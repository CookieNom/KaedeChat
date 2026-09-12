from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api.media import update_attachment_spoiler
from app.db.models import Attachment
from app.media.payloads import attachment_payload, federation_attachment_payload
from app.media.schemas import AttachmentSpoilerUpdate


def attachment(**changes):
    return Attachment(
        **{
            "id": 10,
            "origin_domain": "home.example",
            "uploader_id": 20,
            "uploader_domain": "home.example",
            "filename": "photo.png",
            "purpose": "attachment",
            "encryption_mode": "plaintext",
            **changes,
        }
    )


async def update(item, spoiler=True):
    session = SimpleNamespace(scalar=AsyncMock(return_value=item), commit=AsyncMock())
    result = await update_attachment_spoiler(
        "10",
        AttachmentSpoilerUpdate(spoiler=spoiler),
        auth=SimpleNamespace(user=SimpleNamespace(id=20, origin_domain="home.example")),
        session=session,
        settings=SimpleNamespace(domain="home.example"),
    )
    assert "FOR UPDATE" in str(session.scalar.call_args.args[0])
    session.commit.assert_awaited_once()
    return result


@pytest.mark.asyncio
async def test_spoiler_toggle_is_idempotent_and_preserves_filename():
    item = attachment()
    assert await update(item) == {"filename": "SPOILER_photo.png"}
    assert attachment_payload(item)["filename"] == "SPOILER_photo.png"
    assert federation_attachment_payload(item)["filename"] == "SPOILER_photo.png"
    assert await update(item) == {"filename": "SPOILER_photo.png"}
    assert await update(item, False) == {"filename": "photo.png"}
    item.filename = "SPOILER_SPOILER_photo.png"
    assert await update(item, False) == {"filename": "photo.png"}


@pytest.mark.asyncio
async def test_spoiler_long_filename_keeps_extension_within_storage_limit():
    item = attachment(filename="a" * 251 + ".png")
    await update(item)
    assert len(item.filename) == 255
    assert item.filename.startswith("SPOILER_")
    assert item.filename.endswith(".png")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes, code",
    [
        ({"uploader_id": 21}, 404),
        ({"uploader_domain": "other.example"}, 404),
        ({"deleted_at": datetime.now(UTC)}, 404),
        ({"finalized_at": datetime.now(UTC)}, 409),
        ({"message_id": 30}, 409),
        ({"interaction_response_id": 40}, 409),
        ({"purpose": "avatar"}, 409),
        ({"encryption_mode": "e2ee"}, 409),
    ],
)
async def test_spoiler_mutation_rejects_unowned_or_immutable_uploads(changes, code):
    item = attachment(**changes)
    with pytest.raises(HTTPException) as caught:
        await update(item)
    assert caught.value.status_code == code
    assert item.filename == "photo.png"


def test_spoiler_requires_explicit_boolean():
    for value in ["false", "true", 1, None]:
        with pytest.raises(ValidationError):
            AttachmentSpoilerUpdate(spoiler=value)
