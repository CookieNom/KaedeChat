"""Recheck already-issued object capabilities at the public media edge."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_session
from app.core.settings import Settings, get_settings
from app.core.types import EntityRef
from app.db.models import (
    Attachment,
    MediaTombstoneSource,
    Message,
    RemoteMediaCache,
    RemoteMediaTombstone,
)
from app.safety.models import ChildSafetyCase, ChildSafetyEvidence

router = APIRouter(prefix="/api/v1/safety", include_in_schema=False)


def public_object(path: str, settings: Settings) -> tuple[str, str, int, str]:
    """Accept only canonical path-addressed objects issued by this instance."""
    if len(path) > 2048 or not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("invalid media path")
    decoded = unquote(path, errors="strict")
    if any(character in decoded for character in ("%", "\\", "\x00")):
        raise ValueError("invalid media path")
    parts = decoded[1:].split("/")
    if len(parts) < 4 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid media path")
    bucket, domain, identifier, *_ = parts
    if bucket not in {
        settings.media_attachments_bucket,
        settings.media_derived_bucket,
        settings.media_remote_cache_bucket,
    } or domain in {"evidence", "scanning", "reviewed"}:
        raise ValueError("private object")
    ref = EntityRef(f"{identifier}@{domain}")
    attachment_id, canonical_domain = ref.resolve(settings.domain)
    if str(attachment_id) != identifier or canonical_domain != domain:
        raise ValueError("noncanonical object")
    key = "/".join(parts[1:])
    if "staging" in parts[3:] or "evidence" in parts[3:]:
        raise ValueError("private object")
    return bucket, key, attachment_id, domain


async def safety_blocked(session: AsyncSession, refs: set[str]) -> bool:
    return (
        await session.scalar(
            select(ChildSafetyCase.id)
            .outerjoin(ChildSafetyEvidence, ChildSafetyEvidence.case_id == ChildSafetyCase.id)
            .where(
                or_(
                    ChildSafetyCase.attachment_ref.in_(refs),
                    ChildSafetyEvidence.attachment_ref.in_(refs),
                ),
                or_(
                    ChildSafetyCase.status == "confirmed",
                    and_(
                        ChildSafetyCase.status == "pending",
                        ChildSafetyCase.source == "photodna",
                    ),
                ),
            )
            .limit(1)
        )
        is not None
    )


async def object_readable(
    session: AsyncSession, settings: Settings, target: tuple[str, str, int, str]
) -> bool:
    bucket, key, attachment_id, domain = target
    refs = {f"{attachment_id}@{domain}"}
    parts = key.split("/")
    if bucket == settings.media_remote_cache_bucket and len(parts) == 4:
        if (
            await session.get(RemoteMediaTombstone, (domain, attachment_id)) is not None
            or await session.get(MediaTombstoneSource, (attachment_id, domain)) is not None
        ):
            return False
        cached = await session.get(RemoteMediaCache, (domain, attachment_id, parts[2]))
        if (
            cached is None
            or cached.object_key != key
            or cached.scan_status != "clean"
            or cached.expires_at <= datetime.now(UTC)
        ):
            return False
        attachment = await session.get(Attachment, (attachment_id, domain))
        if attachment is not None:
            if attachment.deleted_at is not None or attachment.scan_status not in {
                "clean",
                "encrypted",
            }:
                return False
            if attachment.message_id is not None:
                message = await session.get(
                    Message, (attachment.message_id, attachment.message_domain)
                )
                if message is None or message.deleted_at is not None or message.safety_state:
                    return False
        # Authority history may intentionally have no local replica. An existing
        # replica's withdrawal still overrides an otherwise valid cache capability.
        return not await safety_blocked(session, refs)
    if bucket == settings.media_derived_bucket and len(parts) == 3 and parts[2].endswith(".webp"):
        matches = Attachment.variants.contains({parts[2][:-5]: {"object_key": key}})
    elif bucket == settings.media_attachments_bucket:
        matches = Attachment.object_key == key
    else:
        return False
    # Shared/crossposted objects may outlive their original row. Look up the
    # actual object across all owners, rather than assuming its path is its owner.
    attachments = list(await session.scalars(select(Attachment).where(matches)))
    if not attachments:
        return False
    refs.update(f"{attachment.id}@{attachment.origin_domain}" for attachment in attachments)
    if await safety_blocked(session, refs):
        return False
    for attachment in attachments:
        if attachment.deleted_at is not None or attachment.scan_status not in {
            "clean",
            "encrypted",
        }:
            continue
        if attachment.message_id is not None:
            message = await session.get(Message, (attachment.message_id, attachment.message_domain))
            if message is None or message.deleted_at is not None or message.safety_state:
                continue
        return True
    return False


@router.get("/media-access", status_code=204)
async def media_access(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    # A distinct header is set only on the media forward-auth hop. Ordinary API
    # requests already receive X-Kaede-Proxy-Secret and must not gain this access.
    supplied = request.headers.get("X-Kaede-Media-Gate-Secret")
    expected = settings.proxy_secret.get_secret_value() if settings.proxy_secret else None
    if (
        supplied is None
        or expected is None
        or not hmac.compare_digest(supplied.encode(), expected.encode())
    ):
        raise HTTPException(status_code=404)
    try:
        target = public_object(request.headers.get("X-Kaede-Media-Path", ""), settings)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=404) from None
    if not await object_readable(session, settings, target):
        raise HTTPException(status_code=404)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
