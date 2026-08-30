from __future__ import annotations

import base64
import hashlib
import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.bots.installations import (
    queue_installation_gateway_events,
    set_application_installations_enabled,
)
from app.bots.target_contract import (
    FederationDomain,
    NonnegativeDecimal,
    PositiveDecimal,
    target_policy_allows,
)
from app.bots.worker_targets import worker_target_allowed
from app.chat.postcommit import queue_postcommit_federation_wakes
from app.core.federation import canonical_json, envelope_signing_bytes, verify_envelope
from app.core.model_validation import UnambiguousInputModel
from app.core.settings import Settings
from app.db.bot_models import (
    BotApplication,
    BotApplicationRuntimeHighwater,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallation,
    BotInstanceRule,
    BotToken,
    BotWorker,
)
from app.db.models import Channel, User
from app.federation.events import (
    build_envelope,
    discard_superseded_latest_state_event,
    queue_event,
)
from app.federation.schemas import EventEnvelope
from app.federation.security import (
    event_timestamp_allowed,
    self_private_key,
    validated_event_envelope,
)

APPLICATION_RUNTIME_EVENT = "bot.application.runtime.changed"
APPLICATION_RUNTIME_HIGHWATER_TTL = timedelta(hours=24)
MAX_PENDING_APPLICATION_RUNTIME_HIGHWATERS_PER_ORIGIN = 1_000
ApplicationRuntimeStatus = Literal[
    "draft",
    "active",
    "review_required",
    "suspended",
    "deleting",
    "deleted",
]


class ApplicationRuntimeWorker(UnambiguousInputModel):
    model_config = ConfigDict(extra="forbid")

    id: PositiveDecimal
    generation: PositiveDecimal
    revoked: bool
    target_allowed: bool


class ApplicationRuntimeSnapshot(UnambiguousInputModel):
    """Roster-free app-home authorization state for one runtime target."""

    model_config = ConfigDict(extra="forbid")

    application_id: PositiveDecimal
    application_domain: FederationDomain
    bot_user_id: PositiveDecimal
    bot_user_domain: FederationDomain
    target_domain: FederationDomain
    manifest_generation: PositiveDecimal
    revocation_generation: PositiveDecimal
    access_revocation_generation: NonnegativeDecimal
    status: ApplicationRuntimeStatus
    target_allowed: bool
    workers: list[ApplicationRuntimeWorker] = Field(max_length=100)

    @model_validator(mode="after")
    def coherent_authority(self) -> ApplicationRuntimeSnapshot:
        if self.application_domain != self.bot_user_domain:
            raise ValueError("runtime application and bot authorities must match")
        if len({worker.id for worker in self.workers}) != len(self.workers):
            raise ValueError("runtime application repeats a worker")
        return self


def application_runtime_snapshot_fingerprint(snapshot: ApplicationRuntimeSnapshot) -> bytes:
    """Return the stable digest of canonical runtime snapshot content."""

    return hashlib.sha256(canonical_json(snapshot.model_dump(mode="json"))).digest()


def _runtime_fingerprint(snapshot: ApplicationRuntimeSnapshot) -> bytes:
    """Compatibility alias for existing internal callers and migrations tests."""

    return application_runtime_snapshot_fingerprint(snapshot)


def _runtime_snapshot_order(snapshot: ApplicationRuntimeSnapshot) -> tuple[int, int, int]:
    return (
        int(snapshot.manifest_generation),
        int(snapshot.revocation_generation),
        int(snapshot.access_revocation_generation),
    )


def application_runtime_envelope_fingerprint(envelope: EventEnvelope) -> bytes:
    """Return the canonical fingerprint of the exact signed runtime proof."""

    return hashlib.sha256(envelope_signing_bytes(envelope.model_dump(mode="json"))).digest()


def require_current_application_runtime_proof(
    application: BotApplication,
    target: BotApplicationTarget,
    envelope: EventEnvelope,
    snapshot: ApplicationRuntimeSnapshot,
) -> bytes:
    """Require an exact, currently enabled runtime proof and return its digest.

    A stale runtime event can be an authenticated idempotent no-op for inbox
    delivery, but it is never authorization.  Admission therefore compares the
    full monotonic tuple and signed content fingerprint with the accepted target
    ledger, and also fences a newer application projection that arrived through
    a separate developer or worker-authority path.
    """

    envelope_snapshot = ApplicationRuntimeSnapshot.model_validate(envelope.content)
    if envelope.type != APPLICATION_RUNTIME_EVENT or envelope_snapshot != snapshot:
        raise ValueError("application runtime proof content is inconsistent")
    application_ref = (application.id, application.origin_domain)
    if (
        (target.application_id, target.application_domain) != application_ref
        or (
            int(snapshot.application_id),
            snapshot.application_domain,
        )
        != application_ref
        or target.target_domain != snapshot.target_domain
    ):
        raise ValueError("application runtime proof target is inconsistent")
    incoming = _runtime_snapshot_order(snapshot)
    stored = (
        int(target.runtime_manifest_generation or 0),
        int(target.runtime_revocation_generation or 0),
        int(target.runtime_access_revocation_generation or 0),
    )
    content_fingerprint = application_runtime_snapshot_fingerprint(snapshot)
    if incoming != stored or target.runtime_fingerprint != content_fingerprint:
        raise ValueError("application runtime proof is not the exact accepted state")
    if (
        application.manifest_generation != incoming[0]
        or application.revocation_generation != incoming[1]
    ):
        raise ValueError("application runtime proof is behind application state")
    if (
        application.status != "active"
        or snapshot.status != "active"
        or not snapshot.target_allowed
        or target.runtime_status != "active"
        or target.runtime_target_allowed is not True
    ):
        raise ValueError("application runtime proof does not authorize the target")
    return content_fingerprint


def require_current_pending_application_runtime_proof(
    highwater: BotApplicationRuntimeHighwater,
    envelope: EventEnvelope,
    snapshot: ApplicationRuntimeSnapshot,
    *,
    now: datetime | None = None,
) -> bytes:
    """Require an exact active proof retained before app materialization."""

    envelope_snapshot = ApplicationRuntimeSnapshot.model_validate(envelope.content)
    if envelope.type != APPLICATION_RUNTIME_EVENT or envelope_snapshot != snapshot:
        raise ValueError("application runtime proof content is inconsistent")
    if (
        highwater.application_id != int(snapshot.application_id)
        or highwater.application_domain != snapshot.application_domain
        or highwater.target_domain != snapshot.target_domain
        or highwater.bot_user_id != int(snapshot.bot_user_id)
        or highwater.bot_user_domain != snapshot.bot_user_domain
    ):
        raise ValueError("pending application runtime proof identity is inconsistent")
    incoming = _runtime_snapshot_order(snapshot)
    stored = (
        highwater.manifest_generation,
        highwater.revocation_generation,
        highwater.access_revocation_generation,
    )
    content_fingerprint = application_runtime_snapshot_fingerprint(snapshot)
    if incoming != stored or highwater.runtime_fingerprint != content_fingerprint:
        raise ValueError("pending application runtime proof is not the exact accepted state")
    if (
        snapshot.status != "active"
        or not snapshot.target_allowed
        or highwater.status != "active"
        or highwater.target_allowed is not True
        or highwater.expires_at <= (now or datetime.now(UTC))
    ):
        raise ValueError("pending application runtime proof does not authorize the target")
    return content_fingerprint


async def apply_pending_application_runtime_proof(
    session: AsyncSession,
    snapshot: ApplicationRuntimeSnapshot,
    *,
    access_revoked_targets: set[str] | None = None,
    now: datetime | None = None,
) -> bool:
    """Persist one monotonic A-signed high-water before the app mirror exists.

    Signature and exact app/bot/target identity validation deliberately happen
    in :func:`validate_application_runtime_proof`; this storage helper accepts
    only that already validated typed snapshot.
    """

    application_ref = (int(snapshot.application_id), snapshot.application_domain)
    target_domain = snapshot.target_domain
    incoming = _runtime_snapshot_order(snapshot)
    fingerprint = application_runtime_snapshot_fingerprint(snapshot)
    current_time = now or datetime.now(UTC)
    expires_at = current_time + APPLICATION_RUNTIME_HIGHWATER_TTL
    await session.scalar(
        select(
            func.pg_advisory_xact_lock(
                func.hashtextextended(
                    f"bot-application-runtime-highwater:{application_ref[1]}",
                    0,
                )
            )
        )
    )
    await session.execute(
        delete(BotApplicationRuntimeHighwater).where(
            BotApplicationRuntimeHighwater.application_domain == application_ref[1],
            BotApplicationRuntimeHighwater.expires_at <= current_time,
        )
    )
    highwater = await session.scalar(
        select(BotApplicationRuntimeHighwater)
        .where(
            BotApplicationRuntimeHighwater.application_id == application_ref[0],
            BotApplicationRuntimeHighwater.application_domain == application_ref[1],
            BotApplicationRuntimeHighwater.target_domain == target_domain,
        )
        .with_for_update()
    )
    if highwater is None:
        live_rows = int(
            await session.scalar(
                select(func.count())
                .select_from(BotApplicationRuntimeHighwater)
                .where(
                    BotApplicationRuntimeHighwater.application_domain == application_ref[1],
                    BotApplicationRuntimeHighwater.expires_at > current_time,
                )
            )
            or 0
        )
        if live_rows >= MAX_PENDING_APPLICATION_RUNTIME_HIGHWATERS_PER_ORIGIN:
            raise ValueError("application runtime pending-state quota exceeded")
        if incoming[2] > 0 and access_revoked_targets is not None:
            access_revoked_targets.add(target_domain)
        session.add(
            BotApplicationRuntimeHighwater(
                application_id=application_ref[0],
                application_domain=application_ref[1],
                target_domain=target_domain,
                bot_user_id=int(snapshot.bot_user_id),
                bot_user_domain=snapshot.bot_user_domain,
                manifest_generation=incoming[0],
                revocation_generation=incoming[1],
                access_revocation_generation=incoming[2],
                status=snapshot.status,
                target_allowed=snapshot.target_allowed,
                runtime_fingerprint=fingerprint,
                expires_at=expires_at,
            )
        )
        return True
    if (
        highwater.bot_user_id != int(snapshot.bot_user_id)
        or highwater.bot_user_domain != snapshot.bot_user_domain
    ):
        raise ValueError("pending application runtime bot identity was equivocated")
    stored = (
        highwater.manifest_generation,
        highwater.revocation_generation,
        highwater.access_revocation_generation,
    )
    if _runtime_snapshot_is_stale(incoming, stored):
        if incoming == stored and highwater.runtime_fingerprint != fingerprint:
            raise ValueError("pending application runtime generation conflicts with stored state")
        if incoming == stored:
            highwater.expires_at = expires_at
        return False
    if incoming[2] > stored[2] and access_revoked_targets is not None:
        access_revoked_targets.add(target_domain)
    highwater.manifest_generation = incoming[0]
    highwater.revocation_generation = incoming[1]
    highwater.access_revocation_generation = incoming[2]
    highwater.status = snapshot.status
    highwater.target_allowed = snapshot.target_allowed
    highwater.runtime_fingerprint = fingerprint
    highwater.expires_at = expires_at
    return True


async def promote_application_runtime_highwater(
    session: AsyncSession,
    application: BotApplication,
    *,
    target_domain: str,
    now: datetime | None = None,
) -> BotApplicationTarget | None:
    """Merge a pending proof only after the exact application state exists."""

    locked_application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == application.id,
            BotApplication.origin_domain == application.origin_domain,
        )
        .with_for_update()
    )
    if locked_application is None:
        raise ValueError("pending runtime promotion references an unknown application")
    application = locked_application
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == target_domain,
        )
        .with_for_update()
    )
    highwater = await session.scalar(
        select(BotApplicationRuntimeHighwater)
        .where(
            BotApplicationRuntimeHighwater.application_id == application.id,
            BotApplicationRuntimeHighwater.application_domain == application.origin_domain,
            BotApplicationRuntimeHighwater.target_domain == target_domain,
        )
        .with_for_update()
    )
    if highwater is None:
        return target
    if highwater.expires_at <= (now or datetime.now(UTC)):
        await session.delete(highwater)
        return target
    if (
        highwater.bot_user_id != application.bot_user_id
        or highwater.bot_user_domain != application.bot_user_domain
    ):
        raise ValueError("pending runtime promotion bot identity is inconsistent")
    # A runtime control carries no application scopes, intents, or credentials.
    # Leave it pending until the full mirrored application has caught up rather
    # than making an older application row look authorized by a newer target.
    if (
        highwater.manifest_generation != application.manifest_generation
        or highwater.revocation_generation != application.revocation_generation
        or highwater.status != application.status
    ):
        return target
    if target is None:
        target = BotApplicationTarget(
            application_id=application.id,
            application_domain=application.origin_domain,
            target_domain=target_domain,
            generation=1,
            guild_installations=0,
            user_installations=0,
            runtime_manifest_generation=0,
            runtime_revocation_generation=0,
            runtime_access_revocation_generation=0,
            runtime_status="active",
            runtime_target_allowed=True,
        )
        session.add(target)
    incoming = (
        highwater.manifest_generation,
        highwater.revocation_generation,
        highwater.access_revocation_generation,
    )
    stored = (
        int(target.runtime_manifest_generation or 0),
        int(target.runtime_revocation_generation or 0),
        int(target.runtime_access_revocation_generation or 0),
    )
    if _runtime_snapshot_is_stale(incoming, stored):
        if incoming == stored and target.runtime_fingerprint != highwater.runtime_fingerprint:
            raise ValueError("pending runtime promotion conflicts with target state")
    else:
        target.runtime_manifest_generation = incoming[0]
        target.runtime_revocation_generation = incoming[1]
        target.runtime_access_revocation_generation = incoming[2]
        target.runtime_status = highwater.status
        target.runtime_target_allowed = highwater.target_allowed
        target.runtime_fingerprint = highwater.runtime_fingerprint
    await session.delete(highwater)
    return target


def target_runtime_projection_ready(
    target: BotApplicationTarget | None,
    *,
    manifest_generation: int,
    revocation_generation: int,
) -> bool:
    """Return whether a target has accepted the signed state needed to run.

    A zero/default ledger is only evidence that first-install discovery has
    completed.  It is not runtime authorization: the application home must
    still deliver a fingerprinted projection covering both generations.
    """

    return bool(
        target is not None
        and target.runtime_fingerprint is not None
        and int(target.runtime_manifest_generation or 0) >= manifest_generation
        and int(target.runtime_revocation_generation or 0) >= revocation_generation
        and (target.runtime_status or "active") == "active"
        and target.runtime_target_allowed is True
    )


def application_runtime_projection_ready(
    application: BotApplication,
    target: BotApplicationTarget | None,
    *,
    target_domain: str,
) -> bool:
    """Apply the runtime fence while preserving the local app-home bypass."""

    return application.origin_domain == target_domain or target_runtime_projection_ready(
        target,
        manifest_generation=application.manifest_generation,
        revocation_generation=application.revocation_generation,
    )


def application_runtime_projection_exists(target_domain: str) -> ColumnElement[bool]:
    """Correlated SQL form of :func:`application_runtime_projection_ready`."""

    return exists(
        select(BotApplicationTarget.application_id).where(
            BotApplicationTarget.application_id == BotApplication.id,
            BotApplicationTarget.application_domain == BotApplication.origin_domain,
            BotApplicationTarget.target_domain == target_domain,
            BotApplicationTarget.runtime_fingerprint.is_not(None),
            BotApplicationTarget.runtime_manifest_generation >= BotApplication.manifest_generation,
            BotApplicationTarget.runtime_revocation_generation
            >= BotApplication.revocation_generation,
            BotApplicationTarget.runtime_status == "active",
            BotApplicationTarget.runtime_target_allowed.is_(True),
        )
    )


def _target_rule_effects(rules: list[BotInstanceRule]) -> dict[str, str]:
    return {rule.target_domain: rule.effect for rule in rules}


def _runtime_snapshot(
    application: BotApplication,
    bot: User,
    workers: list[BotWorker],
    target_domain: str,
    *,
    target_allowed: bool,
    access_revocation_generation: int,
) -> ApplicationRuntimeSnapshot:
    return ApplicationRuntimeSnapshot(
        application_id=str(application.id),
        application_domain=application.origin_domain,
        bot_user_id=str(bot.id),
        bot_user_domain=bot.origin_domain,
        target_domain=target_domain,
        manifest_generation=str(application.manifest_generation),
        revocation_generation=str(application.revocation_generation),
        access_revocation_generation=str(access_revocation_generation),
        status=application.status,
        target_allowed=target_allowed,
        workers=[
            ApplicationRuntimeWorker(
                id=str(worker.authority_id),
                generation=str(worker.generation),
                revoked=worker.revoked_at is not None,
                target_allowed=worker_target_allowed(
                    worker.target_domains,
                    application_domain=application.origin_domain,
                    target_domain=target_domain,
                ),
            )
            for worker in sorted(workers, key=lambda item: item.authority_id)
        ],
    )


async def queue_application_runtime_snapshots(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    *,
    additional_target_domains: set[str] | None = None,
    destination_domains: set[str] | None = None,
) -> set[str]:
    """Queue one signed, durable authorization snapshot per known target."""

    if application.origin_domain != settings.domain:
        raise RuntimeError("only an application authority may publish runtime state")
    await session.flush()
    locked_application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == application.id,
            BotApplication.origin_domain == application.origin_domain,
        )
        .with_for_update()
    )
    if locked_application is None:
        raise RuntimeError("application runtime snapshot authority is unavailable")
    application = locked_application
    bot = await session.get(
        User,
        (application.bot_user_id, application.bot_user_domain),
    )
    if (
        bot is None
        or not bot.is_local
        or bot.account_type != "bot"
        or (bot.id, bot.origin_domain) != (application.bot_user_id, application.bot_user_domain)
    ):
        raise RuntimeError("application runtime snapshot bot identity is invalid")
    targets = list(
        await session.scalars(
            select(BotApplicationTarget)
            .where(
                BotApplicationTarget.application_id == application.id,
                BotApplicationTarget.application_domain == application.origin_domain,
            )
            .order_by(BotApplicationTarget.target_domain)
            .with_for_update()
        )
    )
    targets_by_domain = {target.target_domain: target for target in targets}
    ledger_domains = set(targets_by_domain)
    target_domains = ledger_domains | await active_dm_runtime_target_domains(
        session,
        settings,
        application,
    )
    target_domains.update(additional_target_domains or set())
    if destination_domains is not None:
        target_domains.intersection_update(destination_domains)
    if not target_domains:
        return set()
    # A DM capability can be an application's only presence on an instance.
    # Persist that runtime destination before suspension revokes the capability,
    # otherwise a later reactivation has no durable route back to the target.
    for target_domain in sorted(target_domains - ledger_domains):
        target = BotApplicationTarget(
            application_id=application.id,
            application_domain=application.origin_domain,
            target_domain=target_domain,
            # A owns only runtime routing here; B/C still owns installation
            # counts. Generation 0 means unknown, so B's first signed gen-1
            # discovery cannot collide with a fabricated zero-count tombstone.
            generation=0,
            guild_installations=0,
            user_installations=0,
            runtime_manifest_generation=0,
            runtime_revocation_generation=0,
            runtime_access_revocation_generation=0,
            runtime_status="active",
            runtime_target_allowed=True,
        )
        session.add(target)
        targets_by_domain[target_domain] = target
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
            )
            .order_by(BotWorker.id)
            .limit(101)
        )
    )
    if len(workers) > 100:
        raise RuntimeError("application exceeds the federated worker limit")
    rules = _target_rule_effects(
        list(
            await session.scalars(
                select(BotInstanceRule).where(
                    BotInstanceRule.application_id == application.id,
                    BotInstanceRule.application_domain == application.origin_domain,
                )
            )
        )
    )
    destinations: set[str] = set()
    for target_domain in sorted(target_domains):
        target = targets_by_domain[target_domain]
        target_allowed = target_domain == settings.domain or target_policy_allows(
            application.target_policy,
            rules,
            target_domain,
        )
        previously_enabled = (target.runtime_status or "active") == "active" and (
            True if target.runtime_target_allowed is None else bool(target.runtime_target_allowed)
        )
        enabled = application.status == "active" and target_allowed
        access_revocation_generation = int(target.runtime_access_revocation_generation or 0)
        if previously_enabled and not enabled:
            access_revocation_generation += 1
        target.runtime_status = application.status
        target.runtime_target_allowed = target_allowed
        target.runtime_access_revocation_generation = access_revocation_generation
        snapshot = _runtime_snapshot(
            application,
            bot,
            workers,
            target_domain,
            target_allowed=target_allowed,
            access_revocation_generation=access_revocation_generation,
        )
        target.runtime_manifest_generation = application.manifest_generation
        target.runtime_revocation_generation = application.revocation_generation
        target.runtime_fingerprint = _runtime_fingerprint(snapshot)
        if target_domain == settings.domain:
            continue
        await discard_superseded_latest_state_event(
            session,
            destination=target_domain,
            event_type=APPLICATION_RUNTIME_EVENT,
            application_ref=(application.id, application.origin_domain),
            target_domain=target_domain,
        )
        envelope = await build_envelope(
            session,
            settings,
            APPLICATION_RUNTIME_EVENT,
            bot,
            snapshot.model_dump(mode="json"),
        )
        await queue_event(session, settings, target_domain, envelope)
        destinations.add(target_domain)
    return destinations


async def build_current_application_runtime_proof(
    session: AsyncSession,
    settings: Settings,
    *,
    application_ref: tuple[int, str],
    target_domain: str,
) -> tuple[EventEnvelope, ApplicationRuntimeSnapshot]:
    """Build an A-signed proof from A's already committed target ledger.

    The helper never creates or advances a target. Lifecycle/discovery code
    must first persist the target epoch through
    :func:`queue_application_runtime_snapshots`; this prevents an admission
    request from manufacturing its own authorization state.
    """

    if application_ref[1] != settings.domain:
        raise ValueError("only an application authority may build a runtime proof")
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == application_ref[0],
            BotApplication.origin_domain == application_ref[1],
        )
        .with_for_update()
    )
    if application is None:
        raise ValueError("application runtime proof references an unknown application")
    bot = await session.get(
        User,
        (application.bot_user_id, application.bot_user_domain),
    )
    if (
        bot is None
        or not bot.is_local
        or bot.account_type != "bot"
        or (bot.id, bot.origin_domain) != (application.bot_user_id, application.bot_user_domain)
    ):
        raise ValueError("application runtime proof bot identity is invalid")
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == target_domain,
        )
        .with_for_update()
    )
    if target is None:
        raise ValueError("application runtime proof target is not committed")
    workers = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
            )
            .order_by(BotWorker.id)
            .limit(101)
            .with_for_update()
        )
    )
    if len(workers) > 100:
        raise ValueError("application exceeds the federated worker limit")
    rules = _target_rule_effects(
        list(
            await session.scalars(
                select(BotInstanceRule).where(
                    BotInstanceRule.application_id == application.id,
                    BotInstanceRule.application_domain == application.origin_domain,
                )
            )
        )
    )
    target_allowed = target_domain == settings.domain or target_policy_allows(
        application.target_policy,
        rules,
        target_domain,
    )
    if (
        target.runtime_status != application.status
        or target.runtime_target_allowed != target_allowed
    ):
        raise ValueError("application runtime proof target ledger is not current")
    snapshot = _runtime_snapshot(
        application,
        bot,
        workers,
        target_domain,
        target_allowed=target_allowed,
        access_revocation_generation=int(target.runtime_access_revocation_generation or 0),
    )
    raw_envelope = await build_envelope(
        session,
        settings,
        APPLICATION_RUNTIME_EVENT,
        bot,
        snapshot.model_dump(mode="json"),
    )
    envelope = EventEnvelope.model_validate(raw_envelope)
    require_current_application_runtime_proof(application, target, envelope, snapshot)
    return envelope, snapshot


async def validate_application_runtime_proof(
    session: AsyncSession,
    settings: Settings,
    *,
    expected_origin: str,
    raw_envelope: object,
    application_ref: tuple[int, str],
    bot_ref: tuple[int, str],
    target_domain: str,
) -> tuple[EventEnvelope, ApplicationRuntimeSnapshot]:
    """Verify an A signature and the exact app, bot, and target identities."""

    if expected_origin != application_ref[1] or bot_ref[1] != expected_origin:
        raise ValueError("application runtime proof authority is inconsistent")
    if expected_origin == settings.domain:
        try:
            envelope = EventEnvelope.model_validate(raw_envelope)
        except ValueError as exc:
            raise ValueError("invalid signed event envelope") from exc
        if envelope.origin != settings.domain or envelope.actor.domain != settings.domain:
            raise ValueError("signed event actor does not belong to its origin")
        if not event_timestamp_allowed(
            envelope.ts,
            now_ms=int(time.time() * 1000),
            future_skew_seconds=settings.federation_clock_skew_seconds,
            retention_days=settings.federation_event_retention_days,
        ):
            raise ValueError("signed event timestamp is outside the accepted window")
        key_id, private_key = await self_private_key(session, settings)
        encoded_signature = envelope.signatures.get(settings.domain, {}).get(key_id)
        try:
            signature = (
                base64.b64decode(encoded_signature, validate=True)
                if encoded_signature is not None
                else b""
            )
        except (TypeError, ValueError):
            signature = b""
        if len(signature) != 64 or not verify_envelope(
            envelope.model_dump(mode="json"),
            signature,
            private_key.public_key(),
        ):
            raise ValueError("event envelope signature is invalid")
    else:
        envelope = await validated_event_envelope(
            session,
            settings,
            expected_origin,
            raw_envelope,
        )
    snapshot = ApplicationRuntimeSnapshot.model_validate(envelope.content)
    if (
        envelope.type != APPLICATION_RUNTIME_EVENT
        or (int(envelope.actor.id), envelope.actor.domain) != bot_ref
        or (int(snapshot.application_id), snapshot.application_domain) != application_ref
        or (int(snapshot.bot_user_id), snapshot.bot_user_domain) != bot_ref
        or snapshot.target_domain != target_domain
    ):
        raise ValueError("application runtime proof identity is invalid")
    return envelope, snapshot


async def active_dm_runtime_target_domains(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
) -> set[str]:
    """Return every remote install or conversation authority for active DM grants."""

    # Local import avoids the dm_capability -> runtime_control module cycle.
    from app.bots.dm_capability import usable_dm_capability

    active_conditions = (
        BotDMCapability.application_id == application.id,
        BotDMCapability.application_domain == application.origin_domain,
        usable_dm_capability(at=datetime.now(UTC)),
    )
    source_domains = select(BotDMCapability.source_installation_domain.label("domain")).where(
        *active_conditions
    )
    conversation_domains = select(BotDMCapability.authority_domain.label("domain")).where(
        *active_conditions
    )
    return {
        str(domain)
        for domain in await session.scalars(source_domains.union(conversation_domains))
        if domain != settings.domain
    }


async def rotate_active_application_grants(
    session: AsyncSession,
    application: BotApplication,
) -> list[BotInstallation]:
    """Rotate only currently active grants without reviving suspended rows."""

    guild_installations = list(
        await session.scalars(
            select(BotInstallation)
            .where(
                BotInstallation.application_id == application.id,
                BotInstallation.application_domain == application.origin_domain,
                BotInstallation.status == "active",
                BotInstallation.revoked_at.is_(None),
            )
            .with_for_update()
        )
    )
    for guild_installation in guild_installations:
        guild_installation.grant_revision += 1
        queue_installation_gateway_events(session, guild_installation, "UPDATE")
    # Application runtime rotations are A-owned. A target must not advance a
    # user-home-owned grant revision and make the next signed user grant stale.
    return guild_installations


def _runtime_snapshot_is_stale(
    incoming: tuple[int, int, int],
    stored: tuple[int, int, int],
) -> bool:
    has_newer_component = any(new > old for new, old in zip(incoming, stored, strict=True))
    has_older_component = any(new < old for new, old in zip(incoming, stored, strict=True))
    if has_newer_component and has_older_component:
        raise ValueError("runtime application generations cross stored state")
    return all(new <= old for new, old in zip(incoming, stored, strict=True))


async def apply_application_runtime_snapshot(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    actor: User,
    raw: object,
    *,
    invalidated_worker_ids: set[int] | None = None,
    access_revoked_targets: set[str] | None = None,
    allow_target_bootstrap: bool = False,
) -> bool:
    """Apply one monotonic app-home control and revoke unsafe live tokens."""

    snapshot = ApplicationRuntimeSnapshot.model_validate(raw)
    if (
        snapshot.application_domain != origin
        or snapshot.target_domain != settings.domain
        or snapshot.bot_user_id != str(actor.id)
        or snapshot.bot_user_domain != actor.origin_domain
        or actor.account_type != "bot"
    ):
        raise ValueError("application runtime snapshot authority is invalid")
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == int(snapshot.application_id),
            BotApplication.origin_domain == origin,
            BotApplication.bot_user_id == actor.id,
            BotApplication.bot_user_domain == actor.origin_domain,
        )
        .with_for_update()
    )
    if application is None:
        if not allow_target_bootstrap:
            raise ValueError("application runtime snapshot references an unknown application")
        return await apply_pending_application_runtime_proof(
            session,
            snapshot,
            access_revoked_targets=access_revoked_targets,
        )
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == settings.domain,
        )
        .with_for_update()
    )
    if target is None:
        if not allow_target_bootstrap:
            raise ValueError("application runtime snapshot has no authoritative target ledger")
        target = BotApplicationTarget(
            application_id=application.id,
            application_domain=application.origin_domain,
            target_domain=settings.domain,
            generation=1,
            guild_installations=0,
            user_installations=0,
            runtime_manifest_generation=0,
            runtime_revocation_generation=0,
            runtime_access_revocation_generation=0,
            runtime_status="active",
            runtime_target_allowed=True,
        )
        session.add(target)
    incoming = _runtime_snapshot_order(snapshot)
    stored = (
        int(target.runtime_manifest_generation or 0),
        int(target.runtime_revocation_generation or 0),
        int(target.runtime_access_revocation_generation or 0),
    )
    fingerprint = _runtime_fingerprint(snapshot)
    if _runtime_snapshot_is_stale(incoming, stored):
        if incoming == stored and target.runtime_fingerprint != fingerprint:
            raise ValueError("application runtime generation conflicts with stored state")
        return False
    access_revoked = incoming[2] > stored[2]
    if access_revoked and access_revoked_targets is not None:
        access_revoked_targets.add(settings.domain)
    # A worker authorization or manifest pull may already have reconciled a
    # newer application state than this delayed durable push. Acknowledge it
    # without applying stale target policy or worker membership.
    if (
        incoming[0] < application.manifest_generation
        or incoming[1] < application.revocation_generation
    ):
        target.runtime_manifest_generation = incoming[0]
        target.runtime_revocation_generation = incoming[1]
        target.runtime_access_revocation_generation = incoming[2]
        target.runtime_fingerprint = fingerprint
        if not access_revoked:
            return False
        worker_rows = list(
            await session.scalars(
                select(BotWorker)
                .where(
                    BotWorker.application_id == application.id,
                    BotWorker.application_domain == application.origin_domain,
                )
                .with_for_update()
            )
        )
        changed_installations = await rotate_active_application_grants(
            session,
            application,
        )
        if changed_installations:
            from app.bots.target_discovery import queue_application_target_snapshot

            destination = await queue_application_target_snapshot(
                session,
                settings,
                application,
                actor,
            )
            if destination is not None:
                queue_postcommit_federation_wakes(session, (destination,))
        ahead_revoke_token_worker_ids = {worker.id for worker in worker_rows}
        if invalidated_worker_ids is not None:
            invalidated_worker_ids.update(ahead_revoke_token_worker_ids)
        if ahead_revoke_token_worker_ids:
            await session.execute(
                update(BotToken)
                .where(
                    BotToken.application_id == application.id,
                    BotToken.application_domain == application.origin_domain,
                    BotToken.worker_id.in_(sorted(ahead_revoke_token_worker_ids)),
                    BotToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
        return True
    worker_rows = list(
        await session.scalars(
            select(BotWorker)
            .where(
                BotWorker.application_id == application.id,
                BotWorker.application_domain == application.origin_domain,
            )
            .with_for_update()
        )
    )
    descriptors = {int(worker.id): worker for worker in snapshot.workers}
    now = datetime.now(UTC)
    revoke_token_worker_ids: set[int] = set()
    application_enabled = snapshot.status == "active" and snapshot.target_allowed
    application.status = snapshot.status
    target.runtime_status = snapshot.status
    target.runtime_target_allowed = snapshot.target_allowed
    changed_installations = await set_application_installations_enabled(
        session,
        application,
        enabled=application_enabled,
        rotate_active_grants=access_revoked and application_enabled,
    )
    if changed_installations:
        from app.bots.target_discovery import queue_application_target_snapshot

        destination = await queue_application_target_snapshot(
            session,
            settings,
            application,
            actor,
        )
        if destination is not None:
            queue_postcommit_federation_wakes(session, (destination,))
    # ``manifest_generation`` covers application scopes/intents and worker
    # authorization material, while ``revocation_generation`` covers every
    # authority-state transition (including suspend then reactivate). Runtime
    # events are latest-state coalesced, so C may receive only the final active
    # snapshot. Either generation advancing must therefore invalidate every
    # token minted under the older state. The next worker assertion pulls and
    # verifies the complete authorization from A before minting a replacement.
    if incoming[0] > stored[0] or incoming[1] > stored[1]:
        revoke_token_worker_ids.update(worker.id for worker in worker_rows)
    for worker in worker_rows:
        descriptor = descriptors.get(worker.authority_id)
        authority_revoked = descriptor is None or descriptor.revoked
        if authority_revoked and worker.revoked_at is None:
            worker.revoked_at = now
        if (
            authority_revoked
            or not application_enabled
            or descriptor is None
            or not descriptor.target_allowed
            or int(descriptor.generation) != worker.generation
        ):
            revoke_token_worker_ids.add(worker.id)
    if revoke_token_worker_ids:
        if invalidated_worker_ids is not None:
            invalidated_worker_ids.update(revoke_token_worker_ids)
        await session.execute(
            update(BotToken)
            .where(
                BotToken.application_id == application.id,
                BotToken.application_domain == application.origin_domain,
                BotToken.worker_id.in_(sorted(revoke_token_worker_ids)),
                BotToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    target.runtime_manifest_generation = incoming[0]
    target.runtime_revocation_generation = incoming[1]
    target.runtime_access_revocation_generation = incoming[2]
    target.runtime_fingerprint = fingerprint
    # Unlike manifest_generation, revocation_generation carries no projection
    # fields that still need fetching. Retaining its accepted high-water mark on
    # the mirrored application also rejects a later stale worker authorization.
    application.revocation_generation = max(
        application.revocation_generation,
        incoming[1],
    )
    return True


async def apply_application_runtime_control(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    origin: str,
    actor: User,
    raw: object,
    *,
    allow_target_bootstrap: bool = False,
) -> tuple[bool, list[Channel]]:
    """Apply signed runtime state and atomically fence its live data paths."""

    snapshot = ApplicationRuntimeSnapshot.model_validate(raw)
    invalidated_worker_ids: set[int] = set()
    access_revoked_targets: set[str] = set()
    changed = await apply_application_runtime_snapshot(
        session,
        settings,
        origin,
        actor,
        snapshot,
        invalidated_worker_ids=invalidated_worker_ids,
        access_revoked_targets=access_revoked_targets,
        allow_target_bootstrap=allow_target_bootstrap,
    )
    if not changed:
        return False, []
    application_ref = (int(snapshot.application_id), snapshot.application_domain)
    if snapshot.status != "active" or not snapshot.target_allowed or bool(access_revoked_targets):
        from app.bots.e2ee import revoke_bot_e2ee_access

        channels = await revoke_bot_e2ee_access(
            session,
            redis,
            settings,
            application_ref=application_ref,
        )
        return True, channels
    if invalidated_worker_ids:
        from app.voice.e2ee import evict_bot_voice_runtime_sessions

        await evict_bot_voice_runtime_sessions(
            session,
            redis,
            settings,
            application_ref=application_ref,
            worker_ids=invalidated_worker_ids,
        )
    return True, []


async def durably_apply_application_runtime_proof(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    expected_origin: str,
    raw_envelope: object,
    application_ref: tuple[int, str],
    bot: User,
    target_domain: str,
) -> tuple[EventEnvelope, ApplicationRuntimeSnapshot]:
    """Verify and commit A's runtime control before later admission checks.

    DM admission intentionally has two phases.  A valid newer runtime control
    must fence tokens, E2EE, and voice even when a subsequent installation,
    privacy, or conversation check rejects the request.  Keeping that commit
    boundary here prevents the attest/open/refresh routes from drifting apart.
    """

    envelope, snapshot = await validate_application_runtime_proof(
        session,
        settings,
        expected_origin=expected_origin,
        raw_envelope=raw_envelope,
        application_ref=application_ref,
        bot_ref=(bot.id, bot.origin_domain),
        target_domain=target_domain,
    )
    _changed, policy_channels = await apply_application_runtime_control(
        session,
        redis,
        settings,
        expected_origin,
        bot,
        snapshot,
        allow_target_bootstrap=True,
    )
    await session.commit()
    if policy_channels:
        from app.chat.e2ee_membership import publish_e2ee_policy_updates

        await publish_e2ee_policy_updates(
            session,
            redis,
            settings,
            policy_channels,
        )
    return envelope, snapshot
