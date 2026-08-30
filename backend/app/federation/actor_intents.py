from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ConfigDict, Field, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.auth import decode_urlsafe, worker_runtime_ready
from app.core.federation import canonical_json
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.db.bot_models import BotApplication, BotApplicationTarget, BotWorker
from app.db.models import User
from app.federation.network import normalize_domain

ACTOR_INTENT_MAX_LIFETIME_SECONDS = 120
ACTOR_INTENT_CLOCK_SKEW_SECONDS = 60
HUMAN_ACTOR_INTENT_EVENT_TYPE = "federation.actor.intent"


def actor_intent_for_authority(
    actor_intent: dict[str, object] | None,
    actor_intents: Mapping[str, dict[str, object]] | None,
    authority_domain: str,
) -> dict[str, object] | None:
    """Select the proof minted for one receiver, preserving legacy input.

    The validator at the receiving authority still binds the proof's signed
    audience. A legacy single proof therefore cannot be replayed successfully
    at a second authority with a different domain.
    """

    authority = normalize_domain(authority_domain)
    if actor_intents:
        return actor_intents.get(authority)
    return actor_intent


class HumanActorIntentClaims(UnambiguousInputModel):
    """Instance-signed authority proof for one exact human action."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    action: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9._-]+$")
    audience: str = Field(min_length=1, max_length=253)
    actor_ref: str = Field(min_length=3, max_length=320)
    resources: dict[str, str] = Field(min_length=1, max_length=16)
    issued_at: int = Field(ge=1)
    expires_at: int = Field(ge=1)
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("audience")
    @classmethod
    def canonical_audience(cls, value: str) -> str:
        return normalize_domain(value)

    @field_validator("resources")
    @classmethod
    def canonical_resources(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_resources(value)

    @model_validator(mode="after")
    def bounded_window(self) -> HumanActorIntentClaims:
        _validate_window(self.issued_at, self.expires_at)
        return self


class FederatedActorIntent(UnambiguousInputModel):
    """One worker-signed, operation-specific authorization safe to relay.

    The ordinary bot token and its request DPoP stay at the resource authority.
    This proof contains no bearer material and is useful only for the exact
    action/resources signed by the worker and the named relay audience.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    action: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9._-]+$")
    audience: str = Field(min_length=1, max_length=253)
    application_ref: str = Field(min_length=3, max_length=320)
    actor_ref: str = Field(min_length=3, max_length=320)
    worker_id: str = Field(min_length=1, max_length=20, pattern=r"^[1-9][0-9]*$")
    worker_generation: str = Field(min_length=1, max_length=20, pattern=r"^[1-9][0-9]*$")
    runtime_target: str = Field(min_length=1, max_length=253)
    runtime_manifest_generation: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^[1-9][0-9]*$",
    )
    runtime_revocation_generation: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^[1-9][0-9]*$",
    )
    runtime_access_revocation_generation: str = Field(
        min_length=1,
        max_length=20,
        pattern=r"^(0|[1-9][0-9]*)$",
    )
    resources: dict[str, str] = Field(min_length=1, max_length=16)
    issued_at: int = Field(ge=1)
    expires_at: int = Field(ge=1)
    nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    signature: str = Field(min_length=86, max_length=88)

    @field_validator("audience")
    @classmethod
    def canonical_audience(cls, value: str) -> str:
        return normalize_domain(value)

    @field_validator("runtime_target")
    @classmethod
    def canonical_runtime_target(cls, value: str) -> str:
        return normalize_domain(value)

    @field_validator("resources")
    @classmethod
    def canonical_resources(cls, value: dict[str, str]) -> dict[str, str]:
        return _canonical_resources(value)

    @model_validator(mode="after")
    def bounded_window(self) -> FederatedActorIntent:
        _validate_window(self.issued_at, self.expires_at)
        return self


def actor_intent_signing_bytes(intent: FederatedActorIntent | Mapping[str, object]) -> bytes:
    raw = (
        intent.model_dump(mode="json") if isinstance(intent, FederatedActorIntent) else dict(intent)
    )
    raw.pop("signature", None)
    return b"kaede-federated-actor-intent-v1\n" + canonical_json(raw)


def worker_actor_runtime_revision(
    application: BotApplication,
    worker: BotWorker,
    runtime_target: BotApplicationTarget | None,
    *,
    target_domain: str,
) -> dict[str, str]:
    """Return the canonical, signed worker/runtime high-water tuple."""

    target_domain = normalize_domain(target_domain)
    if application.origin_domain == target_domain:
        manifest_generation = int(application.manifest_generation)
        revocation_generation = int(application.revocation_generation)
        access_revocation_generation = 0
    else:
        if runtime_target is None or runtime_target.target_domain != target_domain:
            raise ValueError("actor intent runtime target is unavailable")
        manifest_generation = int(runtime_target.runtime_manifest_generation or 0)
        revocation_generation = int(runtime_target.runtime_revocation_generation or 0)
        access_revocation_generation = int(runtime_target.runtime_access_revocation_generation or 0)
    if manifest_generation < 1 or revocation_generation < 1:
        raise ValueError("actor intent runtime revision is unavailable")
    return {
        "worker_generation": str(worker.generation),
        "runtime_target": target_domain,
        "runtime_manifest_generation": str(manifest_generation),
        "runtime_revocation_generation": str(revocation_generation),
        "runtime_access_revocation_generation": str(access_revocation_generation),
    }


async def build_human_actor_intent(
    session: AsyncSession,
    settings: Settings,
    actor: User,
    *,
    action: str,
    audience: str,
    resources: Mapping[str, str],
    now: datetime | None = None,
) -> dict[str, object]:
    """Build a relayable, actor-home instance-signed human intent envelope."""

    from app.federation.events import build_envelope

    if actor.account_type != "human" or actor.origin_domain != settings.domain:
        raise ValueError("only a human's home instance may mint this actor intent")
    issued_at = int((now or datetime.now(UTC)).timestamp())
    claims = HumanActorIntentClaims(
        action=action,
        audience=audience,
        actor_ref=f"{actor.id}@{actor.origin_domain}",
        resources=dict(resources),
        issued_at=issued_at,
        expires_at=issued_at + ACTOR_INTENT_MAX_LIFETIME_SECONDS,
        nonce=secrets.token_urlsafe(24),
    )
    return await build_envelope(
        session,
        settings,
        HUMAN_ACTOR_INTENT_EVENT_TYPE,
        actor,
        claims.model_dump(mode="json"),
    )


async def validate_human_actor_intent(
    session: AsyncSession,
    settings: Settings,
    raw_intent: object,
    *,
    expected_action: str,
    expected_audience: str,
    expected_actor_ref: tuple[int, str],
    expected_resources: Mapping[str, str],
    redis: Redis | None = None,
    now: datetime | None = None,
) -> HumanActorIntentClaims:
    """Verify a nested actor-home envelope without trusting its relay."""

    from app.federation.security import validated_event_envelope

    actor_ref = _normalized_ref(expected_actor_ref)
    if not isinstance(raw_intent, dict):
        raise ValueError("human actor intent is not an envelope")
    envelope = await validated_event_envelope(
        session,
        settings,
        actor_ref[1],
        raw_intent,
    )
    claims = HumanActorIntentClaims.model_validate(envelope.content)
    current = now or datetime.now(UTC)
    now_seconds = int(current.timestamp())
    if (
        envelope.type != HUMAN_ACTOR_INTENT_EVENT_TYPE
        or envelope.context
        or (int(envelope.actor.id), envelope.actor.domain) != actor_ref
        or _qualified_ref(claims.actor_ref) != actor_ref
        or claims.action != expected_action
        or claims.audience != normalize_domain(expected_audience)
        or claims.resources != _canonical_resources(dict(expected_resources))
        or claims.issued_at > now_seconds + ACTOR_INTENT_CLOCK_SKEW_SECONDS
        or claims.expires_at <= now_seconds
        or abs((envelope.ts // 1000) - claims.issued_at) > 1
    ):
        raise ValueError("human actor intent binding is invalid")
    if redis is not None:
        await consume_actor_intent_nonce(
            redis,
            authority_domain=actor_ref[1],
            intent_kind="human-instance",
            action=claims.action,
            actor_ref=actor_ref,
            audience=claims.audience,
            nonce=claims.nonce,
            expires_at=claims.expires_at,
            fingerprint=canonical_json(claims.model_dump(mode="json")),
            now=current,
        )
    return claims


async def validate_worker_actor_intent(
    session: AsyncSession,
    settings_domain: str,
    raw_intent: object,
    *,
    expected_action: str,
    expected_audience: str,
    expected_application_ref: tuple[int, str],
    expected_actor_ref: tuple[int, str],
    expected_resources: Mapping[str, str],
    runtime_target_domain: str,
    redis: Redis | None = None,
    now: datetime | None = None,
) -> FederatedActorIntent:
    """Verify an exact worker intent against the current local runtime ledger."""

    del settings_domain  # The caller supplies the explicit runtime target.
    intent = FederatedActorIntent.model_validate(raw_intent)
    current = now or datetime.now(UTC)
    now_seconds = int(current.timestamp())
    application_ref = _qualified_ref(intent.application_ref)
    actor_ref = _qualified_ref(intent.actor_ref)
    audience = normalize_domain(expected_audience)
    target_domain = normalize_domain(runtime_target_domain)
    if (
        intent.action != expected_action
        or intent.audience != audience
        or application_ref != expected_application_ref
        or actor_ref != expected_actor_ref
        or intent.resources != dict(sorted(expected_resources.items()))
        or intent.issued_at > now_seconds + ACTOR_INTENT_CLOCK_SKEW_SECONDS
        or intent.expires_at <= now_seconds
    ):
        raise ValueError("actor intent binding is invalid")

    application = await session.get(BotApplication, application_ref)
    actor = await session.get(User, actor_ref)
    worker = await session.scalar(
        select(BotWorker).where(
            BotWorker.application_id == application_ref[0],
            BotWorker.application_domain == application_ref[1],
            BotWorker.source_id == int(intent.worker_id),
            BotWorker.source_domain == application_ref[1],
        )
    )
    if worker is None:
        # Application-home rows use their local ID before a mirrored source ID
        # exists. Supporting both forms keeps the wire identity stable.
        worker = await session.get(BotWorker, int(intent.worker_id))
    runtime_target = await session.get(
        BotApplicationTarget,
        (*application_ref, target_domain),
    )
    expected_runtime_revision: dict[str, str] = {}
    if application is not None and worker is not None:
        with suppress(AttributeError, TypeError, ValueError):
            expected_runtime_revision = worker_actor_runtime_revision(
                application,
                worker,
                runtime_target,
                target_domain=target_domain,
            )
    if (
        application is None
        or actor is None
        or actor.account_type != "bot"
        or (application.bot_user_id, application.bot_user_domain) != actor_ref
        or worker is None
        or (worker.application_id, worker.application_domain) != application_ref
        or worker.authority_id != int(intent.worker_id)
        or {
            "worker_generation": intent.worker_generation,
            "runtime_target": intent.runtime_target,
            "runtime_manifest_generation": intent.runtime_manifest_generation,
            "runtime_revocation_generation": intent.runtime_revocation_generation,
            "runtime_access_revocation_generation": (intent.runtime_access_revocation_generation),
        }
        != expected_runtime_revision
        or not worker_runtime_ready(
            application,
            worker,
            runtime_target,
            target_domain=target_domain,
            now=current,
        )
    ):
        raise ValueError("actor intent runtime is unavailable")
    try:
        signature = decode_urlsafe(intent.signature, length=64)
        Ed25519PublicKey.from_public_bytes(worker.public_key).verify(
            signature,
            actor_intent_signing_bytes(intent),
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise ValueError("actor intent signature is invalid") from exc
    if redis is not None:
        await consume_actor_intent_nonce(
            redis,
            authority_domain=application_ref[1],
            intent_kind="bot-worker",
            action=intent.action,
            actor_ref=actor_ref,
            audience=intent.audience,
            nonce=intent.nonce,
            expires_at=intent.expires_at,
            fingerprint=actor_intent_signing_bytes(intent),
            now=current,
        )
    return intent


async def consume_actor_intent_nonce(
    redis: Redis,
    *,
    authority_domain: str,
    intent_kind: str,
    action: str,
    actor_ref: tuple[int, str],
    audience: str,
    nonce: str,
    expires_at: int,
    fingerprint: bytes,
    now: datetime | None = None,
) -> bool:
    """Consume a nonce, while allowing the byte-identical idempotent retry.

    Returns ``True`` on first use and ``False`` for the exact same signed
    intent. Reusing a nonce for different claims fails closed. Callers must
    still make the bound operation idempotent before accepting ``False``.
    """

    current = now or datetime.now(UTC)
    now_seconds = int(current.timestamp())
    ttl = max(1, expires_at - now_seconds + ACTOR_INTENT_CLOCK_SKEW_SECONDS)
    authority = normalize_domain(authority_domain)
    normalized_actor = _normalized_ref(actor_ref)
    normalized_audience = normalize_domain(audience)
    key_material = canonical_json(
        {
            "authority": authority,
            "intent_kind": intent_kind,
            "action": action,
            "actor_ref": f"{normalized_actor[0]}@{normalized_actor[1]}",
            "audience": normalized_audience,
            "nonce": nonce,
        }
    )
    key = f"federation:actor-intent:v1:{hashlib.sha256(key_material).hexdigest()}"
    digest = hashlib.sha256(fingerprint).hexdigest()
    if await redis.set(key, digest, ex=ttl, nx=True):
        return True
    existing = await redis.get(key)
    if isinstance(existing, bytes):
        existing = existing.decode("ascii", errors="ignore")
    if not isinstance(existing, str) or not hmac.compare_digest(existing, digest):
        raise ValueError("actor intent nonce was reused with different claims")
    return False


def _qualified_ref(value: str) -> tuple[int, str]:
    if "@" not in value:
        raise ValueError("actor intent reference must be qualified")
    raw_id, raw_domain = value.rsplit("@", 1)
    if (
        not raw_id.isascii()
        or not raw_id.isdecimal()
        or raw_id.startswith("0")
        or int(raw_id) > (1 << 63) - 1
    ):
        raise ValueError("actor intent reference is invalid")
    return int(raw_id), normalize_domain(raw_domain)


def _normalized_ref(value: tuple[int, str]) -> tuple[int, str]:
    raw_id, raw_domain = value
    if isinstance(raw_id, bool) or not 1 <= raw_id <= (1 << 63) - 1:
        raise ValueError("actor intent reference is invalid")
    return raw_id, normalize_domain(raw_domain)


def _canonical_resources(value: Mapping[str, str]) -> dict[str, str]:
    if any(
        not key
        or len(key) > 64
        or not all(
            character.islower() or character.isdigit() or character in "._-" for character in key
        )
        or not isinstance(item, str)
        or not 1 <= len(item) <= 512
        for key, item in value.items()
    ):
        raise ValueError("actor intent resources are invalid")
    return dict(sorted(value.items()))


def _validate_window(issued_at: int, expires_at: int) -> None:
    if expires_at <= issued_at or expires_at - issued_at > ACTOR_INTENT_MAX_LIFETIME_SECONDS:
        raise ValueError("actor intent lifetime is invalid")
