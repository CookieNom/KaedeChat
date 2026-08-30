from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.e2ee import revoke_bot_e2ee_access
from app.bots.installations import installation_has_membership, usable_user_installation
from app.bots.target_contract import (
    APPLICATION_TARGET_EVENT,
    ApplicationTargetSnapshot,
)
from app.chat.postcommit import queue_postcommit_federation_wakes
from app.core.settings import Settings
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotInstallation,
    BotUserInstallation,
)
from app.db.models import Channel, User
from app.federation.events import (
    build_envelope,
    discard_superseded_latest_state_event,
    queue_event,
)

APPLICATION_RUNTIME_RECOVERY_BATCH_SIZE = 100
USER_INSTALLATION_AUTHORITY_SWEEP_BATCH_SIZE = 100


async def require_application_runtime_enabled(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
) -> None:
    """Fence grant creation against the signed state for this runtime target.

    The caller holds the application row lock before any installation row
    lock. Runtime snapshot application uses the same order, serializing a
    concurrent install with suspend/reactivate and target-policy transitions.
    A target row is absent before an application's first presence; in that
    case the freshly fetched manifest and target policy remain authoritative.
    """

    if application.origin_domain == settings.domain:
        return
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == settings.domain,
        )
        .with_for_update()
    )
    if target is not None and (
        (target.runtime_status or "active") != "active" or target.runtime_target_allowed is False
    ):
        raise HTTPException(
            status_code=403,
            detail={"code": "APPLICATION_TARGET_NOT_ALLOWED"},
        )


async def application_target_counts(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
) -> tuple[int, int]:
    """Count usable local target grants without disclosing their identities."""

    guild_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BotInstallation)
            .where(
                BotInstallation.application_id == application.id,
                BotInstallation.application_domain == application.origin_domain,
                BotInstallation.status == "active",
                BotInstallation.revoked_at.is_(None),
                installation_has_membership(),
            )
        )
        or 0
    )
    user_count = int(
        await session.scalar(
            select(func.count())
            .select_from(BotUserInstallation)
            .where(
                BotUserInstallation.application_id == application.id,
                BotUserInstallation.application_domain == application.origin_domain,
                usable_user_installation(current_instance_domain=settings.domain),
            )
        )
        or 0
    )
    return guild_count, user_count


def application_target_payload(
    application: BotApplication,
    bot: User,
    target: BotApplicationTarget,
) -> dict[str, str]:
    return {
        "application_id": str(application.id),
        "application_domain": application.origin_domain,
        "bot_user_id": str(bot.id),
        "bot_user_domain": bot.origin_domain,
        "target_domain": target.target_domain,
        "generation": str(target.generation),
        "guild_installations": str(target.guild_installations),
        "user_installations": str(target.user_installations),
    }


async def queue_application_target_snapshot(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    bot: User,
    *,
    force: bool = False,
) -> str | None:
    """Persist and, for remote apps, durably publish one target snapshot.

    Callers invoke this before their mutation commit. The target ledger and
    signed federation outbox therefore commit atomically with the installation
    state that produced the aggregate.
    """

    if (
        bot.account_type != "bot"
        or bot.disabled_at is not None
        or (application.bot_user_id, application.bot_user_domain) != (bot.id, bot.origin_domain)
        or application.origin_domain != bot.origin_domain
    ):
        raise ValueError("application target snapshot bot identity is invalid")
    await session.flush()
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == settings.domain,
        )
        .with_for_update()
    )
    guild_count, user_count = await application_target_counts(session, settings, application)
    if target is None:
        target = BotApplicationTarget(
            application_id=application.id,
            application_domain=application.origin_domain,
            target_domain=settings.domain,
            generation=1,
            guild_installations=guild_count,
            user_installations=user_count,
        )
        session.add(target)
        changed = True
    else:
        changed = (target.guild_installations, target.user_installations) != (
            guild_count,
            user_count,
        )
        if changed or force:
            target.generation += 1
            target.guild_installations = guild_count
            target.user_installations = user_count
    if not changed and not force:
        return None
    if application.origin_domain == settings.domain:
        return None
    await discard_superseded_latest_state_event(
        session,
        destination=application.origin_domain,
        event_type=APPLICATION_TARGET_EVENT,
        application_ref=(application.id, application.origin_domain),
        target_domain=settings.domain,
    )
    envelope = await build_envelope(
        session,
        settings,
        APPLICATION_TARGET_EVENT,
        bot,
        application_target_payload(application, bot, target),
        authority_attested_actor=True,
    )
    await queue_event(session, settings, application.origin_domain, envelope)
    return application.origin_domain


async def queue_application_target_snapshots_for_refs(
    session: AsyncSession,
    settings: Settings,
    application_refs: set[tuple[int, str]],
    *,
    force: bool = False,
) -> set[str]:
    """Queue one coalesced target snapshot for each mutated application."""

    destinations: set[str] = set()
    for application_id, application_domain in sorted(
        application_refs, key=lambda item: (item[1], item[0])
    ):
        row = (
            await session.execute(
                select(BotApplication, User)
                .join(
                    User,
                    (User.id == BotApplication.bot_user_id)
                    & (User.origin_domain == BotApplication.bot_user_domain),
                )
                .where(
                    BotApplication.id == application_id,
                    BotApplication.origin_domain == application_domain,
                )
            )
        ).one_or_none()
        if row is None:
            raise ValueError("installation references an unknown application identity")
        destination = await queue_application_target_snapshot(
            session,
            settings,
            row[0],
            row[1],
            force=force,
        )
        if destination is not None:
            destinations.add(destination)
    return destinations


async def recover_incomplete_application_runtime_targets(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int = APPLICATION_RUNTIME_RECOVERY_BATCH_SIZE,
) -> tuple[int, set[str]]:
    """Re-announce one bounded page of legacy runtime targets.

    Runtime target authorization was added after installation target ledgers.
    An upgraded remote instance can therefore have live installations but no
    A-signed runtime fingerprint, and no ordinary mutation to trigger target
    discovery. Re-announcing the aggregate makes the application authority
    return its current signed runtime state. Rows stop matching as soon as
    that proof is applied, while latest-state outbox compaction bounds retries
    if delivery is unavailable.
    """

    if not 1 <= limit <= APPLICATION_RUNTIME_RECOVERY_BATCH_SIZE:
        raise ValueError("application runtime recovery limit is out of bounds")
    rows = (
        await session.execute(
            select(BotApplication, User, BotApplicationTarget)
            .join(
                BotApplicationTarget,
                (BotApplicationTarget.application_id == BotApplication.id)
                & (BotApplicationTarget.application_domain == BotApplication.origin_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotApplicationTarget.target_domain == settings.domain,
                BotApplication.origin_domain != settings.domain,
                BotApplication.origin_domain == User.origin_domain,
                User.account_type == "bot",
                User.disabled_at.is_(None),
                (BotApplicationTarget.guild_installations > 0)
                | (BotApplicationTarget.user_installations > 0),
                or_(
                    BotApplicationTarget.runtime_fingerprint.is_(None),
                    BotApplicationTarget.runtime_manifest_generation
                    < BotApplication.manifest_generation,
                    BotApplicationTarget.runtime_revocation_generation
                    < BotApplication.revocation_generation,
                ),
            )
            .order_by(
                BotApplicationTarget.updated_at,
                BotApplication.origin_domain,
                BotApplication.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True, of=BotApplicationTarget)
        )
    ).all()
    destinations: set[str] = set()
    for application, bot, _target in rows:
        destination = await queue_application_target_snapshot(
            session,
            settings,
            application,
            bot,
            force=True,
        )
        if destination is not None:
            destinations.add(destination)
    return len(rows), destinations


async def expire_foreign_user_installation_leases(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    limit: int = USER_INSTALLATION_AUTHORITY_SWEEP_BATCH_SIZE,
) -> tuple[int, set[str], list[Channel]]:
    """Expire one locked page of foreign mirrors and converge their effects.

    The signed expiry itself is the synchronous authorization fence.  This
    sweep performs the bounded cleanup work: revoke derived E2EE access,
    suspend the mirror without changing its authority-owned revision, and
    atomically queue the resulting zero/updated target snapshots.
    """

    if not 1 <= limit <= USER_INSTALLATION_AUTHORITY_SWEEP_BATCH_SIZE:
        raise ValueError("user installation authority sweep limit is out of bounds")
    rows = list(
        await session.scalars(
            select(BotUserInstallation)
            .where(
                BotUserInstallation.user_domain != settings.domain,
                BotUserInstallation.status == "active",
                BotUserInstallation.revoked_at.is_(None),
                or_(
                    BotUserInstallation.authority_expires_at.is_(None),
                    BotUserInstallation.authority_expires_at <= func.now(),
                ),
            )
            .order_by(
                BotUserInstallation.authority_expires_at.asc().nullsfirst(),
                BotUserInstallation.id,
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    if not rows:
        return 0, set(), []
    now = datetime.now(UTC)
    paused_channels = await revoke_bot_e2ee_access(
        session,
        redis,
        settings,
        user_installation_ids=tuple(row.id for row in rows),
        now=now,
    )
    application_refs = {(row.application_id, row.application_domain) for row in rows}
    for row in rows:
        # ``grant_revision`` belongs to the user's home.  A local runtime or
        # lease transition must never outrun the next signed authority grant.
        row.status = "suspended"
    await session.flush()
    destinations = await queue_application_target_snapshots_for_refs(
        session,
        settings,
        application_refs,
    )
    return len(rows), destinations, list(paused_channels)


async def wake_application_target_deliveries(destinations: set[str]) -> None:
    """Best-effort immediate wake; the durable federation sweep remains the fallback."""

    if not destinations:
        return
    from app.core.task_wake import enqueue_best_effort
    from app.tasks import federation_deliver

    for destination in sorted(destinations):
        await enqueue_best_effort(federation_deliver, destination)


async def _queue_current_application_runtime(
    session: AsyncSession,
    settings: Settings,
    application: BotApplication,
    target_domain: str,
) -> None:
    """Reply to accepted target discovery with current signed runtime state."""

    from app.bots.runtime_control import queue_application_runtime_snapshots

    runtime_destinations = await queue_application_runtime_snapshots(
        session,
        settings,
        application,
        destination_domains={target_domain},
    )
    queue_postcommit_federation_wakes(session, sorted(runtime_destinations))


async def apply_application_target_snapshot(
    session: AsyncSession,
    settings: Settings,
    origin: str,
    actor: User,
    raw: object,
) -> bool:
    """Apply one monotonic target snapshot at the application authority."""

    snapshot = ApplicationTargetSnapshot.model_validate(raw)
    if (
        snapshot.application_domain != settings.domain
        or snapshot.target_domain != origin
        or snapshot.bot_user_id != str(actor.id)
        or snapshot.bot_user_domain != actor.origin_domain
    ):
        raise ValueError("application target snapshot authority is invalid")
    application = await session.scalar(
        select(BotApplication)
        .where(
            BotApplication.id == int(snapshot.application_id),
            BotApplication.origin_domain == settings.domain,
            BotApplication.bot_user_id == actor.id,
            BotApplication.bot_user_domain == actor.origin_domain,
        )
        .with_for_update()
    )
    if application is None:
        raise ValueError("application target snapshot references an unknown local application")
    incoming = (
        int(snapshot.generation),
        int(snapshot.guild_installations),
        int(snapshot.user_installations),
    )
    target = await session.scalar(
        select(BotApplicationTarget)
        .where(
            BotApplicationTarget.application_id == application.id,
            BotApplicationTarget.application_domain == application.origin_domain,
            BotApplicationTarget.target_domain == origin,
        )
        .with_for_update()
    )
    if target is None:
        target = BotApplicationTarget(
            application_id=application.id,
            application_domain=application.origin_domain,
            target_domain=origin,
            generation=incoming[0],
            guild_installations=incoming[1],
            user_installations=incoming[2],
        )
        session.add(target)
        # A target-count event can arrive after an application mutation. Send
        # the current authority state immediately so this newly discovered
        # runtime cannot remain active on stale or default policy.
        await _queue_current_application_runtime(session, settings, application, origin)
        return True
    stored = (target.generation, target.guild_installations, target.user_installations)
    if incoming[0] < stored[0]:
        return False
    if incoming[0] == stored[0]:
        if incoming != stored:
            raise ValueError("application target generation conflicts with stored state")
        return False
    target.generation = incoming[0]
    target.guild_installations = incoming[1]
    target.user_installations = incoming[2]
    # A forced re-announcement is also the bounded recovery handshake for a
    # pre-runtime-control target. Always answer a newly accepted generation;
    # equal-generation retries remain idempotent above.
    await _queue_current_application_runtime(session, settings, application, origin)
    return True
