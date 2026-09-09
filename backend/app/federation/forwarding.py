from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.chat.forwarding import (
    FORWARD_SOURCE_AUTHORIZATION_EVENT,
    validate_forward_source_authorization,
)
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import FederatedChannelFollow, FederatedMessageCrosspost
from app.federation.client import signed_request
from app.federation.network import FederationNetworkError, decode_federation_response_json
from app.federation.security import validated_event_envelope


async def validated_forward_source_proof(
    session: AsyncSession,
    settings: Settings,
    raw_proof: object,
    *,
    requester_ref: str,
    requester_type: Literal["human", "bot"],
    source_message_ref: str,
    source_channel_ref: str,
    destination_channel_ref: str,
    destination_encryption_mode: Literal["plaintext", "e2ee"],
    nonce: str,
    application_ref: str | None,
    e2ee_device_id: str | None,
    validation_time: datetime | None = None,
) -> dict[str, object]:
    """Verify one source-authority proof and every destination-use binding."""

    if not isinstance(raw_proof, dict):
        raise ValueError("forward source proof is not an event envelope")
    source_authority = EntityRef(source_channel_ref).domain
    if source_authority is None:
        raise ValueError("forward source channel is not qualified")
    envelope = await validated_event_envelope(
        session,
        settings,
        source_authority,
        raw_proof,
        allow_authority_attested_actor=True,
    )
    if envelope.type != FORWARD_SOURCE_AUTHORIZATION_EVENT:
        raise ValueError("forward source proof has an unexpected event type")
    content = validate_forward_source_authorization(
        envelope.content,
        expected_authority=source_authority,
        requester_ref=requester_ref,
        destination_channel_ref=destination_channel_ref,
        destination_encryption_mode=destination_encryption_mode,
        nonce=nonce,
        now=validation_time,
    )
    requester = EntityRef(requester_ref)
    proof_device = content.get("e2ee_device_id")
    if (
        requester.domain is None
        or envelope.actor.id != str(requester.id)
        or envelope.actor.domain != requester.domain
        or content.get("requester_type") != requester_type
        or content.get("source_message_ref") != source_message_ref
        or content.get("source_channel_ref") != source_channel_ref
        or content.get("application_ref") != application_ref
        or (e2ee_device_id is not None and proof_device != e2ee_device_id)
        or (e2ee_device_id is None and proof_device is not None and requester_type != "bot")
    ):
        raise ValueError("forward source proof use binding is invalid")
    return content


async def resolve_announcement_crosspost(
    session: AsyncSession,
    settings: Settings,
    follow: FederatedChannelFollow,
    federated_crosspost: FederatedMessageCrosspost,
    *,
    generation: int,
) -> dict[str, object]:
    """Resolve the exact source projection after the caller authorizes the destination."""
    try:
        upstream = await signed_request(
            session,
            settings,
            "POST",
            follow.source_authority_domain,
            (f"/_kaede/v1/channels/{follow.source_channel_id}/announcement-crossposts/resolve"),
            payload={
                "follow_id": str(follow.id),
                "generation": str(generation),
                "source_message_ref": (
                    f"{federated_crosspost.source_message_id}@"
                    f"{federated_crosspost.source_message_domain}"
                ),
            },
            request_timeout=10,
            max_response_bytes=512 * 1024,
        )
    except FederationNetworkError:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
        ) from None
    if upstream.status_code == 404:
        raise HTTPException(status_code=404, detail={"code": "FORWARDED_MESSAGE_NOT_FOUND"})
    if upstream.status_code != 200:
        raise HTTPException(
            status_code=503,
            detail={"code": "FEDERATED_FORWARD_UNAVAILABLE"},
        )
    remote = decode_federation_response_json(upstream)
    if (
        not isinstance(remote, dict)
        or remote.get("id") != str(federated_crosspost.source_message_id)
        or remote.get("origin_domain") != federated_crosspost.source_message_domain
        or remote.get("source_channel_ref")
        != f"{follow.source_channel_id}@{follow.source_channel_domain}"
    ):
        raise HTTPException(
            status_code=502,
            detail={"code": "FEDERATED_FORWARD_RESPONSE_INVALID"},
        )
    return {str(key): value for key, value in remote.items()}
