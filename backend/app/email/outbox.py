from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import EmailOutbox, OneTimeToken
from app.email.backends import EmailBackend, OutboundEmail, create_email_backend

log = structlog.get_logger()

PAYLOAD_VERSION = 1
MAX_PAYLOAD_BYTES = 1_000_000
CLAIM_TIMEOUT = timedelta(minutes=10)
TERMINAL_RETENTION = timedelta(days=7)
RETRY_DELAYS = (
    timedelta(seconds=5),
    timedelta(seconds=30),
    timedelta(minutes=2),
    timedelta(minutes=10),
    timedelta(minutes=30),
    timedelta(hours=1),
)


class EmailPayloadError(ValueError):
    """The encrypted outbox payload is invalid or cannot be authenticated."""


def retry_delay(attempt: int) -> timedelta:
    """Return bounded exponential-ish backoff for a one-based attempt number."""

    return RETRY_DELAYS[min(max(attempt, 1) - 1, len(RETRY_DELAYS) - 1)]


def _payload_context(settings: Settings, outbox_id: str, one_time_token_id: str) -> bytes:
    return (
        f"kaede-email-outbox:v{PAYLOAD_VERSION}:{settings.domain}:{outbox_id}:{one_time_token_id}"
    ).encode()


def encrypt_email_payload(
    settings: Settings,
    outbox_id: str,
    one_time_token_id: str,
    message: OutboundEmail,
) -> bytes:
    payload = {
        "html": message.html,
        "subject": message.subject,
        "text": message.text,
        "to": message.to,
        "version": PAYLOAD_VERSION,
    }
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if not message.to or not message.subject or not message.text:
        raise EmailPayloadError("email payload fields must not be empty")
    if len(plaintext) > MAX_PAYLOAD_BYTES:
        raise EmailPayloadError("email payload is too large")
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(settings.secret_key_bytes).encrypt(
        nonce,
        plaintext,
        _payload_context(settings, outbox_id, one_time_token_id),
    )
    return nonce + ciphertext


def decrypt_email_payload(
    settings: Settings,
    outbox_id: str,
    one_time_token_id: str,
    encrypted_payload: bytes,
) -> OutboundEmail:
    if not 29 <= len(encrypted_payload) <= 1_048_576:
        raise EmailPayloadError("encrypted email payload is malformed")
    try:
        plaintext = AESGCM(settings.secret_key_bytes).decrypt(
            encrypted_payload[:12],
            encrypted_payload[12:],
            _payload_context(settings, outbox_id, one_time_token_id),
        )
        decoded: Any = json.loads(plaintext)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise EmailPayloadError("encrypted email payload is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("version") != PAYLOAD_VERSION:
        raise EmailPayloadError("encrypted email payload has an unsupported version")
    to = decoded.get("to")
    subject = decoded.get("subject")
    text = decoded.get("text")
    html = decoded.get("html")
    if (
        not isinstance(to, str)
        or not to
        or not isinstance(subject, str)
        or not subject
        or not isinstance(text, str)
        or not text
        or (html is not None and not isinstance(html, str))
    ):
        raise EmailPayloadError("encrypted email payload fields are invalid")
    return OutboundEmail(to=to, subject=subject, text=text, html=html)


def enqueue_email_intent(
    session: AsyncSession,
    settings: Settings,
    token: OneTimeToken,
    message: OutboundEmail,
) -> EmailOutbox:
    """Add a delivery intent to the caller's token-issuance transaction."""

    outbox_id = secrets.token_urlsafe(24)
    record = EmailOutbox(
        id=outbox_id,
        one_time_token_id=token.id,
        encrypted_payload=encrypt_email_payload(
            settings,
            outbox_id,
            token.id,
            message,
        ),
        expires_at=token.expires_at,
    )
    session.add(record)
    return record


async def _claim_batch(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    limit: int,
    now: datetime,
) -> tuple[list[EmailOutbox], int]:
    stale_before = now - CLAIM_TIMEOUT
    async with sessionmaker() as session:
        expired_result = await session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.expires_at <= now,
                or_(
                    EmailOutbox.status.in_(("pending", "retry")),
                    and_(
                        EmailOutbox.status == "processing",
                        EmailOutbox.claimed_at <= stale_before,
                    ),
                ),
            )
            .values(
                status="expired",
                completed_at=now,
                claimed_at=None,
                claim_token=None,
                last_error_code="token_expired",
                updated_at=now,
            )
        )
        candidates = list(
            (
                await session.scalars(
                    select(EmailOutbox)
                    .where(
                        EmailOutbox.expires_at > now,
                        or_(
                            and_(
                                EmailOutbox.status.in_(("pending", "retry")),
                                EmailOutbox.next_attempt_at <= now,
                            ),
                            and_(
                                EmailOutbox.status == "processing",
                                EmailOutbox.claimed_at <= stale_before,
                            ),
                        ),
                    )
                    .order_by(EmailOutbox.next_attempt_at, EmailOutbox.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(limit)
                )
            ).all()
        )
        for candidate in candidates:
            candidate.status = "processing"
            candidate.attempts += 1
            candidate.claimed_at = now
            candidate.claim_token = secrets.token_urlsafe(24)
            candidate.completed_at = None
            candidate.last_error_code = None
            candidate.updated_at = now
        await session.commit()
        return candidates, cast(CursorResult[Any], expired_result).rowcount or 0


async def _finish_delivery(
    sessionmaker: async_sessionmaker[AsyncSession],
    record: EmailOutbox,
    *,
    now: datetime,
) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.id == record.id,
                EmailOutbox.status == "processing",
                EmailOutbox.claim_token == record.claim_token,
            )
            .values(
                status="delivered",
                completed_at=now,
                claimed_at=None,
                claim_token=None,
                last_error_code=None,
                updated_at=now,
            )
        )
        await session.commit()


async def _finish_failure(
    sessionmaker: async_sessionmaker[AsyncSession],
    record: EmailOutbox,
    *,
    now: datetime,
    error_code: str,
    retryable: bool,
) -> str:
    next_attempt = now + retry_delay(record.attempts)
    should_expire = not retryable or next_attempt >= record.expires_at
    status = "expired" if should_expire else "retry"
    async with sessionmaker() as session:
        await session.execute(
            update(EmailOutbox)
            .where(
                EmailOutbox.id == record.id,
                EmailOutbox.status == "processing",
                EmailOutbox.claim_token == record.claim_token,
            )
            .values(
                status=status,
                next_attempt_at=next_attempt,
                completed_at=now if should_expire else None,
                claimed_at=None,
                claim_token=None,
                last_error_code=error_code,
                updated_at=now,
            )
        )
        await session.commit()
    return status


async def drain_email_outbox(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    backend: EmailBackend | None = None,
    limit: int = 25,
) -> dict[str, int]:
    """Claim and deliver due messages with concurrent-worker safety.

    A successful provider call followed by process death can result in a
    duplicate on the stale-claim retry; SMTP and the supported HTTP provider do
    not offer a shared exactly-once transaction.  Claims prevent concurrent
    duplicates and guarantee at-least-once attempts until token expiry.
    """

    if not 1 <= limit <= 500:
        raise ValueError("email outbox batch limit must be between 1 and 500")
    claimed, expired = await _claim_batch(
        sessionmaker,
        limit=limit,
        now=datetime.now(UTC),
    )
    result = {
        "claimed": len(claimed),
        "delivered": 0,
        "retried": 0,
        "expired": expired,
    }
    if settings.email_backend == "disabled":
        for record in claimed:
            await _finish_failure(
                sessionmaker,
                record,
                now=datetime.now(UTC),
                error_code="email_disabled",
                retryable=False,
            )
            result["expired"] += 1
        return result
    delivery_backend = backend or create_email_backend(settings)
    for record in claimed:
        current_time = datetime.now(UTC)
        if record.expires_at <= current_time:
            await _finish_failure(
                sessionmaker,
                record,
                now=current_time,
                error_code="token_expired",
                retryable=False,
            )
            result["expired"] += 1
            continue
        try:
            message = decrypt_email_payload(
                settings,
                record.id,
                record.one_time_token_id,
                record.encrypted_payload,
            )
        except EmailPayloadError:
            await _finish_failure(
                sessionmaker,
                record,
                now=current_time,
                error_code="payload_unavailable",
                retryable=False,
            )
            log.error("email_outbox_payload_unavailable", outbox_id=record.id)
            result["expired"] += 1
            continue
        try:
            await delivery_backend.send(message)
        except Exception:
            outcome = await _finish_failure(
                sessionmaker,
                record,
                now=datetime.now(UTC),
                error_code="delivery_failed",
                retryable=True,
            )
            log.warning(
                "email_outbox_delivery_failed",
                outbox_id=record.id,
                attempt=record.attempts,
            )
            result["expired" if outcome == "expired" else "retried"] += 1
            continue
        await _finish_delivery(sessionmaker, record, now=datetime.now(UTC))
        result["delivered"] += 1
    return result


async def cleanup_email_outbox(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    cutoff = (now or datetime.now(UTC)) - TERMINAL_RETENTION
    result = await session.execute(
        delete(EmailOutbox).where(
            EmailOutbox.status.in_(("delivered", "expired")),
            EmailOutbox.completed_at < cutoff,
        )
    )
    await session.commit()
    return cast(CursorResult[Any], result).rowcount or 0
