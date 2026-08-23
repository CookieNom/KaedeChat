from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import asdict, dataclass

from redis.asyncio import Redis

PUSH_SYNC_TTL_SECONDS = 600
PUSH_SYNC_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


@dataclass(frozen=True, slots=True)
class PushSyncEvent:
    device_id: str
    user_id: int
    user_domain: str
    message_id: int
    message_domain: str
    kind: str
    title: str | None = None
    body: str | None = None
    channel_ref: str | None = None
    event_ref: str | None = None
    sent_at: str | None = None


def push_sync_key(token: str) -> str:
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    return f"push:sync:v1:{digest}"


def _decode_event(value: str | bytes) -> PushSyncEvent:
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    document = json.loads(value)
    if not isinstance(document, dict):
        raise ValueError("invalid push sync event")
    return PushSyncEvent(
        device_id=str(document["device_id"]),
        user_id=int(document["user_id"]),
        user_domain=str(document["user_domain"]),
        message_id=int(document["message_id"]),
        message_domain=str(document["message_domain"]),
        kind=str(document["kind"]),
        title=str(document["title"]) if document.get("title") is not None else None,
        body=str(document["body"]) if document.get("body") is not None else None,
        channel_ref=(
            str(document["channel_ref"]) if document.get("channel_ref") is not None else None
        ),
        event_ref=(str(document["event_ref"]) if document.get("event_ref") is not None else None),
        sent_at=str(document["sent_at"]) if document.get("sent_at") is not None else None,
    )


async def issue_push_sync(redis: Redis, event: PushSyncEvent) -> str:
    encoded = json.dumps(asdict(event), separators=(",", ":"), sort_keys=True)
    for _ in range(4):
        token = secrets.token_urlsafe(32)
        if await redis.set(
            push_sync_key(token),
            encoded,
            ex=PUSH_SYNC_TTL_SECONDS,
            nx=True,
        ):
            return token
    raise RuntimeError("could not allocate a unique push sync token")


async def load_push_sync(redis: Redis, token: str) -> tuple[str | bytes, PushSyncEvent] | None:
    if not PUSH_SYNC_TOKEN_RE.fullmatch(token):
        return None
    encoded = await redis.get(push_sync_key(token))
    if encoded is None:
        return None
    try:
        return encoded, _decode_event(encoded)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        await redis.delete(push_sync_key(token))
        return None


async def claim_push_sync(redis: Redis, token: str, expected: str | bytes) -> bool:
    claimed = await redis.getdel(push_sync_key(token))
    if claimed is None:
        return False
    if isinstance(claimed, bytes) != isinstance(expected, bytes):
        claimed = claimed.decode("utf-8") if isinstance(claimed, bytes) else claimed.encode()
    return bool(claimed == expected)


async def discard_push_sync(redis: Redis, token: str) -> None:
    if PUSH_SYNC_TOKEN_RE.fullmatch(token):
        await redis.delete(push_sync_key(token))
