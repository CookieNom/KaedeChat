from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bot_federation import materialize_remote_manifest, worker_refresh_manifest
from app.api.dependencies import get_redis, get_session, get_snowflake
from app.bots.dm_capability import (
    BOT_DM_CAPABILITY_EVENT,
    BotDMCapabilityApplyRequest,
    BotDMCapabilityAttestRequest,
    BotDMCapabilityAuthorityUnavailable,
    BotDMCapabilityFenceExpectation,
    BotDMCapabilityPayload,
    BotDMCapabilityProofInvalid,
    BotDMCapabilitySourceRejected,
    BotDMCapabilityValidateRequest,
    apply_bot_dm_capability,
    bot_dm_capability_fence_expectation,
    bot_dm_grant_id,
    capability_authorization_fingerprint,
    capability_is_active,
    consume_bot_dm_runtime_fence,
    fence_bot_dm_capability,
    lock_bot_dm_capability_projection,
    next_capability_expiry,
    require_capability_runtime_binding,
    require_stored_capability_runtime,
    stored_bot_dm_capability_payload,
    stored_source_bot_dm_capability_payload,
    usable_dm_capability,
    validate_bot_dm_capability_at_source,
    validated_bot_dm_capability_proof,
)
from app.bots.installations import installation_has_membership, usable_user_installation
from app.bots.runtime_control import (
    application_runtime_snapshot_fingerprint,
    durably_apply_application_runtime_proof,
    promote_application_runtime_highwater,
    require_current_application_runtime_proof,
    validate_application_runtime_proof,
)
from app.chat.e2ee_membership import publish_e2ee_policy_updates
from app.chat.privacy import blocked_between, lock_dm_policy
from app.core.dm import dm_authority_domain, dm_pair_key
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.types import EntityRef
from app.db.bot_models import (
    BotApplication,
    BotApplicationTarget,
    BotDMCapability,
    BotInstallation,
    BotUserInstallation,
)
from app.db.models import DMConversation, GuildMember, User
from app.federation.events import build_envelope
from app.federation.network import ensure_peer
from app.federation.schemas import EventEnvelope
from app.federation.security import (
    FederationPrincipal,
    authenticate_federation,
    enforce_federation_route_rate_limit,
)

router = APIRouter(tags=["bot DM federation"])


@dataclass(slots=True)
class CurrentBotDMEntitlement:
    application: BotApplication
    bot: User
    target: User
    granted_scopes: list[str]
    granted_intents: list[str]
    installation_revision: int
    e2ee_mode: str
    guild_ref: str | None = None
    installing_user_ref: str | None = None
    channel_restrictions: list[str] | None = None


def _qualified_restrictions(values: list[str], authority: str) -> list[str]:
    rendered: set[str] = set()
    for value in values:
        reference = EntityRef(value)
        channel_id, channel_domain = reference.resolve(authority)
        rendered.add(f"{channel_id}@{channel_domain}")
    return sorted(rendered)


async def current_bot_dm_entitlement(
    session: AsyncSession,
    settings: Settings,
    *,
    source_kind: str,
    installation_ref: EntityRef,
    application_ref: EntityRef,
    bot_ref: EntityRef,
    target_ref: EntityRef,
    expected_target_username: str | None = None,
) -> CurrentBotDMEntitlement:
    """Reload B's exact locked installation and target-membership authority."""

    target = await session.get(
        User,
        (target_ref.id, target_ref.domain),
        populate_existing=True,
    )
    if (
        target is None
        or target.account_type != "human"
        or target.disabled_at is not None
        or (expected_target_username is not None and target.username != expected_target_username)
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_TARGET_NOT_MEMBER"})
    if source_kind == "guild":
        row = (
            await session.execute(
                select(BotInstallation, BotApplication, User)
                .join(
                    BotApplication,
                    (BotApplication.id == BotInstallation.application_id)
                    & (BotApplication.origin_domain == BotInstallation.application_domain),
                )
                .join(
                    User,
                    (User.id == BotInstallation.bot_user_id)
                    & (User.origin_domain == BotInstallation.bot_user_domain),
                )
                .where(
                    BotInstallation.id == installation_ref.id,
                    BotInstallation.application_id == application_ref.id,
                    BotInstallation.application_domain == application_ref.domain,
                    BotInstallation.bot_user_id == bot_ref.id,
                    BotInstallation.bot_user_domain == bot_ref.domain,
                    BotInstallation.status == "active",
                    BotInstallation.revoked_at.is_(None),
                    BotApplication.status == "active",
                    User.account_type == "bot",
                    User.disabled_at.is_(None),
                    installation_has_membership(),
                )
                .with_for_update(of=BotInstallation)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
        installation, application, bot = row
        target_member = await session.get(
            GuildMember,
            (
                installation.guild_id,
                installation.guild_domain,
                target_ref.id,
                target_ref.domain,
            ),
            populate_existing=True,
        )
        if target_member is None:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_TARGET_NOT_MEMBER"},
            )
        return CurrentBotDMEntitlement(
            application=application,
            bot=bot,
            target=target,
            granted_scopes=list(installation.granted_scopes),
            granted_intents=list(installation.granted_intents),
            installation_revision=installation.grant_revision,
            e2ee_mode=installation.e2ee_mode,
            guild_ref=f"{installation.guild_id}@{installation.guild_domain}",
            channel_restrictions=_qualified_restrictions(
                installation.channel_restrictions,
                settings.domain,
            ),
        )

    user_row = (
        await session.execute(
            select(BotUserInstallation, BotApplication, User)
            .join(
                BotApplication,
                (BotApplication.id == BotUserInstallation.application_id)
                & (BotApplication.origin_domain == BotUserInstallation.application_domain),
            )
            .join(
                User,
                (User.id == BotApplication.bot_user_id)
                & (User.origin_domain == BotApplication.bot_user_domain),
            )
            .where(
                BotUserInstallation.id == installation_ref.id,
                BotUserInstallation.application_id == application_ref.id,
                BotUserInstallation.application_domain == application_ref.domain,
                BotUserInstallation.user_id == target_ref.id,
                BotUserInstallation.user_domain == target_ref.domain,
                usable_user_installation(current_instance_domain=settings.domain),
                BotApplication.status == "active",
                BotApplication.bot_user_id == bot_ref.id,
                BotApplication.bot_user_domain == bot_ref.domain,
                User.account_type == "bot",
                User.disabled_at.is_(None),
            )
            .with_for_update(of=BotUserInstallation)
        )
    ).one_or_none()
    if user_row is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_NOT_INSTALLED"})
    user_installation, application, bot = user_row
    if not {"bot_dm", "private_channel"}.intersection(user_installation.contexts):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"})
    return CurrentBotDMEntitlement(
        application=application,
        bot=bot,
        target=target,
        granted_scopes=list(user_installation.granted_scopes),
        granted_intents=list(user_installation.granted_intents),
        installation_revision=user_installation.grant_revision,
        e2ee_mode=("participant" if "participant" in application.e2ee_modes else "disabled"),
        installing_user_ref=str(target_ref),
        channel_restrictions=[],
    )


async def require_current_bot_dm_entitlement(
    session: AsyncSession,
    settings: Settings,
    capability: BotDMCapabilityPayload,
) -> None:
    entitlement = await current_bot_dm_entitlement(
        session,
        settings,
        source_kind=capability.source_kind,
        installation_ref=capability.installation,
        application_ref=capability.application,
        bot_ref=capability.bot_user,
        target_ref=capability.target_user,
    )
    current_authorization = capability.model_copy(
        update={
            "guild_ref": entitlement.guild_ref,
            "installing_user_ref": entitlement.installing_user_ref,
            "scopes": sorted(
                set(entitlement.granted_scopes).intersection(entitlement.application.default_scopes)
            ),
            "intents": sorted(
                set(entitlement.granted_intents).intersection(
                    entitlement.application.default_intents
                )
            ),
            "channel_restrictions": entitlement.channel_restrictions or [],
            "e2ee_mode": entitlement.e2ee_mode,
            "installation_revision": str(entitlement.installation_revision),
        }
    )
    if capability_authorization_fingerprint(
        current_authorization
    ) != capability_authorization_fingerprint(capability):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})


async def _commit_bot_dm_capability_fence(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    expectation: BotDMCapabilityFenceExpectation,
) -> None:
    _fenced, channels = await fence_bot_dm_capability(
        session,
        redis,
        settings,
        expectation,
    )
    await session.commit()
    if channels:
        await publish_e2ee_policy_updates(session, redis, settings, channels)


@router.post("/_kaede/v1/bot-dm/capabilities/validate")
async def validate_bot_dm_capability(
    payload: BotDMCapabilityValidateRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Confirm that B still recognizes an exact active capability revision."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-dm-capability-validate",
        capacity=240,
        refill_per_minute=240,
    )
    try:
        proof, capability = await validated_bot_dm_capability_proof(
            session,
            settings,
            payload.proof,
            expected_installation_authority=settings.domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"}) from exc
    if (
        principal.origin not in {capability.application.domain, capability.authority_domain}
        or capability.grant_id != payload.grant_id
        or capability.revision != payload.revision
        or capability.status != "active"
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    # Capture the exact signed lineage without holding its row lock while the
    # installation authority is reloaded. Installation lifecycle paths lock
    # installation -> capability, so validation must use the same order.
    observed = await session.scalar(
        select(BotDMCapability)
        .where(BotDMCapability.grant_id == capability.grant_id)
        .execution_options(populate_existing=True)
    )
    try:
        source_payload = (
            stored_source_bot_dm_capability_payload(observed) if observed is not None else None
        )
    except ValueError:
        source_payload = None
    if source_payload != capability or (
        observed is not None
        and EventEnvelope.model_validate(observed.proof).content != proof.content
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    if observed is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    expectation = bot_dm_capability_fence_expectation(observed)
    try:
        await require_current_bot_dm_entitlement(session, settings, capability)
    except HTTPException as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_DM_GRANT_INVALID"},
        ) from exc
    row = await lock_bot_dm_capability_projection(session, expectation)
    try:
        locked_source = stored_source_bot_dm_capability_payload(row) if row is not None else None
    except ValueError:
        locked_source = None
    if locked_source != capability or (
        row is not None and EventEnvelope.model_validate(row.proof).content != proof.content
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    return {
        "grant_id": capability.grant_id,
        "revision": str(capability.revision),
        "expires_at_ms": str(int(capability.expires_at.timestamp() * 1000)),
    }


@router.post("/_kaede/v1/bot-dm/capabilities/apply")
async def apply_refreshed_bot_dm_capability(
    payload: BotDMCapabilityApplyRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Apply B's proof only to the exact grant already bound at authority C."""

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-dm-capability-apply",
        capacity=240,
        refill_per_minute=240,
    )
    conversation_ref = EntityRef(payload.conversation_ref)
    if conversation_ref.domain != settings.domain:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    current_identity = await session.scalar(
        select(BotDMCapability)
        .where(
            BotDMCapability.grant_id == payload.grant_id,
            BotDMCapability.authority_domain == settings.domain,
            BotDMCapability.conversation_id == conversation_ref.id,
            BotDMCapability.conversation_domain == conversation_ref.domain,
        )
        .execution_options(populate_existing=True)
    )
    if current_identity is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    if principal.origin != current_identity.application_domain:
        # C accepts runtime-ledger mutation only from application home A. A
        # relay that merely captured a valid proof must not advance C's target
        # runtime projection before the proof is cryptographically checked.
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    expectation = bot_dm_capability_fence_expectation(current_identity)
    if current_identity.expires_at <= datetime.now(UTC):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    if not capability_is_active(current_identity):
        # Let A converge its shadow only for the exact still-live lineage C
        # fenced locally. Absent, expired, or mismatched grants remain
        # deliberately ambiguous protocol failures.
        try:
            submitted = EventEnvelope.model_validate(payload.proof)
            submitted_capability = BotDMCapabilityPayload.model_validate(submitted.content)
            stored_source = stored_source_bot_dm_capability_payload(current_identity)
        except ValueError as exc:
            raise HTTPException(
                status_code=403,
                detail={"code": "BOT_DM_GRANT_INVALID"},
            ) from exc
        if (
            submitted_capability != stored_source
            or submitted.content != EventEnvelope.model_validate(current_identity.proof).content
            or submitted_capability.grant_id != payload.grant_id
            or submitted_capability.revision != payload.revision
        ):
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    try:
        initial_capability = stored_bot_dm_capability_payload(current_identity)
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "BOT_DM_GRANT_INVALID"},
        ) from exc
    runtime_bot = await session.get(
        User,
        (current_identity.bot_user_id, current_identity.bot_user_domain),
    )
    target = await session.get(
        User,
        (current_identity.target_user_id, current_identity.target_user_domain),
    )
    conversation = await session.get(
        DMConversation,
        (conversation_ref.id, conversation_ref.domain),
    )
    if (
        runtime_bot is None
        or runtime_bot.account_type != "bot"
        or runtime_bot.disabled_at is not None
    ):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    if (
        target is None
        or target.account_type != "human"
        or target.disabled_at is not None
        or conversation is None
        or conversation.pair_key != current_identity.pair_key
        or conversation.authority_domain != settings.domain
        or conversation.origin_domain != settings.domain
    ):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    try:
        runtime_envelope, runtime_snapshot = await durably_apply_application_runtime_proof(
            session,
            redis,
            settings,
            expected_origin=current_identity.application_domain,
            raw_envelope=payload.runtime_proof,
            application_ref=(
                current_identity.application_id,
                current_identity.application_domain,
            ),
            bot=runtime_bot,
            target_domain=settings.domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_INVALID"}) from exc
    if runtime_snapshot.status != "active" or not runtime_snapshot.target_allowed:
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})

    runtime_bot = await session.get(
        User,
        (current_identity.bot_user_id, current_identity.bot_user_domain),
        populate_existing=True,
    )
    target = await session.get(
        User,
        (current_identity.target_user_id, current_identity.target_user_domain),
        populate_existing=True,
    )
    conversation = await session.get(
        DMConversation,
        (conversation_ref.id, conversation_ref.domain),
        populate_existing=True,
    )
    if (
        runtime_bot is None
        or runtime_bot.account_type != "bot"
        or runtime_bot.disabled_at is not None
    ):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    if (
        target is None
        or target.account_type != "human"
        or target.disabled_at is not None
        or conversation is None
        or conversation.pair_key != current_identity.pair_key
        or conversation.authority_domain != settings.domain
        or conversation.origin_domain != settings.domain
    ):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    current = await session.scalar(
        select(BotDMCapability)
        .where(BotDMCapability.grant_id == expectation.grant_id)
        .execution_options(populate_existing=True)
    )
    if current is None or not expectation.matches(current):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    runtime_fenced_at = consume_bot_dm_runtime_fence(session, expectation)
    runtime_epoch_transition = bool(
        not capability_is_active(current)
        and runtime_fenced_at is not None
        and current.revoked_at == runtime_fenced_at
        and (
            int(runtime_snapshot.manifest_generation),
            int(runtime_snapshot.revocation_generation),
            int(runtime_snapshot.access_revocation_generation),
        )
        != (
            int(initial_capability.runtime_manifest_generation),
            int(initial_capability.runtime_revocation_generation),
            int(initial_capability.target_access_revocation_generation),
        )
    )
    if not capability_is_active(current) and not runtime_epoch_transition:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    try:
        proof, capability = await validated_bot_dm_capability_proof(
            session,
            settings,
            payload.proof,
        )
        if (
            principal.origin != capability.application.domain
            or capability.authority_domain != settings.domain
            or capability.grant_id != payload.grant_id
            or capability.revision != payload.revision
            or capability.status != "active"
            or (
                current_identity.application_id,
                current_identity.application_domain,
                current_identity.bot_user_id,
                current_identity.bot_user_domain,
            )
            != (
                capability.application.id,
                capability.application.domain,
                capability.bot_user.id,
                capability.bot_user.domain,
            )
        ):
            raise ValueError("bot DM grant changed its bound runtime identity")
        require_capability_runtime_binding(
            capability,
            runtime_envelope,
            runtime_snapshot,
        )
        if runtime_epoch_transition and int(capability.revision) <= expectation.revision:
            raise ValueError("bot DM runtime transition lacks a newer capability lineage")
        await require_stored_capability_runtime(session, settings, capability)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"}) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_INVALID"}) from exc
    try:
        await validate_bot_dm_capability_at_source(
            session,
            settings,
            proof,
            capability,
        )
        if capability.installation.domain == settings.domain:
            try:
                await require_current_bot_dm_entitlement(session, settings, capability)
            except HTTPException as exc:
                raise BotDMCapabilitySourceRejected(
                    "installation authority no longer recognizes the DM grant"
                ) from exc
    except BotDMCapabilityAuthorityUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "BOT_DM_INSTALLATION_AUTHORITY_UNAVAILABLE"},
        ) from exc
    except BotDMCapabilityProofInvalid as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": "BOT_DM_INSTALLATION_PROOF_INVALID"},
        ) from exc
    except BotDMCapabilitySourceRejected as exc:
        await lock_dm_policy(session, runtime_bot, target)
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"}) from exc
    await lock_dm_policy(session, runtime_bot, target)
    current = await lock_bot_dm_capability_projection(
        session,
        expectation,
        require_active=not runtime_epoch_transition,
    )
    if current is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    if await blocked_between(session, runtime_bot, target):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    if (
        conversation.pair_key != capability.pair_key
        or conversation.authority_domain != settings.domain
        or bot_dm_capability_fence_expectation(current) != expectation
        or (
            runtime_epoch_transition
            and (capability_is_active(current) or current.revoked_at != runtime_fenced_at)
        )
    ):
        await _commit_bot_dm_capability_fence(session, redis, settings, expectation)
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_FENCED"})
    try:
        refreshed, _ = await apply_bot_dm_capability(
            session,
            snowflake,
            proof,
            capability,
            conversation=conversation,
            runtime_admitted=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail={"code": "BOT_DM_GRANT_CONFLICT"}) from exc
    await session.commit()
    if refreshed is None:
        raise RuntimeError("active bot DM capability was not materialized")
    return {
        "grant_id": refreshed.grant_id,
        "revision": str(refreshed.revision),
        "conversation_ref": str(conversation_ref),
        "expires_at_ms": str(int(refreshed.expires_at.timestamp() * 1000)),
    }


@router.post("/_kaede/v1/bot-dm/capabilities/attest")
async def attest_bot_dm_capability(
    payload: BotDMCapabilityAttestRequest,
    principal: Annotated[FederationPrincipal, Depends(authenticate_federation)],
    session: Annotated[AsyncSession, Depends(get_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    snowflake: Annotated[SnowflakeGenerator, Depends(get_snowflake)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return await _attest_bot_dm_capability(
        payload,
        principal,
        session,
        redis,
        snowflake,
        settings,
        commit=True,
    )


async def _attest_bot_dm_capability(
    payload: BotDMCapabilityAttestRequest,
    principal: FederationPrincipal,
    session: AsyncSession,
    redis: Redis,
    snowflake: SnowflakeGenerator,
    settings: Settings,
    *,
    commit: bool,
) -> dict[str, object]:
    """Return B's original, short-lived proof for one exact installation.

    The application home authenticates its worker before calling this route.
    B still owns every authorization fact: active membership, target membership,
    scopes, intents, restrictions, and grant revision are all reloaded here.
    """

    await enforce_federation_route_rate_limit(
        redis,
        principal.origin,
        "bot-dm-capability-attest",
        capacity=120,
        refill_per_minute=120,
    )
    installation_ref = EntityRef(payload.installation_ref)
    application_ref = EntityRef(payload.application_ref)
    bot_ref = EntityRef(payload.bot_user_ref)
    if (
        installation_ref.domain != settings.domain
        or application_ref.domain != principal.origin
        or bot_ref.domain != principal.origin
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_AUTHORITY_INVALID"})
    runtime_bot = await session.get(User, (bot_ref.id, bot_ref.domain))
    if runtime_bot is None or runtime_bot.account_type != "bot":
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_AUTHORITY_INVALID"})
    try:
        source_runtime_envelope, source_runtime = await durably_apply_application_runtime_proof(
            session,
            redis,
            settings,
            expected_origin=principal.origin,
            raw_envelope=payload.source_runtime_proof,
            application_ref=(application_ref.id, application_ref.domain),
            bot=runtime_bot,
            target_domain=settings.domain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_INVALID"}) from exc
    if source_runtime.status != "active" or not source_runtime.target_allowed:
        # The newer deny is already committed above. Installation/context
        # rejection must never roll its token, E2EE, or voice fences back.
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_INVALID"})
    try:
        authority_runtime_envelope, authority_runtime = await validate_application_runtime_proof(
            session,
            settings,
            expected_origin=principal.origin,
            raw_envelope=payload.authority_runtime_proof,
            application_ref=(application_ref.id, application_ref.domain),
            bot_ref=(bot_ref.id, bot_ref.domain),
            target_domain=payload.authority_domain,
        )
        if (
            authority_runtime.status != "active"
            or not authority_runtime.target_allowed
            or source_runtime.manifest_generation != authority_runtime.manifest_generation
            or source_runtime.revocation_generation != authority_runtime.revocation_generation
        ):
            raise ValueError("bot DM runtime proofs do not authorize both targets")
        application = await session.get(
            BotApplication,
            (application_ref.id, application_ref.domain),
        )
        if application is None or application.manifest_generation < int(
            source_runtime.manifest_generation
        ):
            manifest, materialize_template = await worker_refresh_manifest(
                session,
                settings,
                application_id=application_ref.id,
                application_domain=application_ref.domain,
            )
            if int(manifest.application.manifest_generation) < int(
                source_runtime.manifest_generation
            ):
                raise ValueError("bot DM source manifest is behind runtime control")
            application, _template, _materialized_bot = await materialize_remote_manifest(
                session,
                manifest,
                settings,
                snowflake,
                materialize_template=materialize_template,
            )
            await promote_application_runtime_highwater(
                session,
                application,
                target_domain=settings.domain,
            )
        application = await session.scalar(
            select(BotApplication)
            .where(
                BotApplication.id == application_ref.id,
                BotApplication.origin_domain == application_ref.domain,
            )
            .with_for_update()
        )
        source_target = await session.scalar(
            select(BotApplicationTarget)
            .where(
                BotApplicationTarget.application_id == application_ref.id,
                BotApplicationTarget.application_domain == application_ref.domain,
                BotApplicationTarget.target_domain == settings.domain,
            )
            .with_for_update()
        )
        if application is None or source_target is None:
            raise ValueError("bot DM source runtime target is unavailable")
        require_current_application_runtime_proof(
            application,
            source_target,
            source_runtime_envelope,
            source_runtime,
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_RUNTIME_INVALID"}) from exc
    target_ref = EntityRef(f"{payload.target.id}@{payload.target.origin_domain}")
    grant_id = bot_dm_grant_id(
        payload.source_kind,
        payload.installation_ref,
        payload.application_ref,
        payload.bot_user_ref,
        payload.pair_key,
        payload.authority_domain,
    )
    if payload.refresh_grant_id is not None and grant_id != payload.refresh_grant_id:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    observed_refresh = (
        await session.scalar(
            select(BotDMCapability)
            .where(
                BotDMCapability.grant_id == payload.refresh_grant_id,
                BotDMCapability.source_kind == payload.source_kind,
                BotDMCapability.source_installation_id == installation_ref.id,
                BotDMCapability.source_installation_domain == installation_ref.domain,
                BotDMCapability.application_id == application_ref.id,
                BotDMCapability.application_domain == application_ref.domain,
                BotDMCapability.bot_user_id == bot_ref.id,
                BotDMCapability.bot_user_domain == bot_ref.domain,
                BotDMCapability.target_user_id == target_ref.id,
                BotDMCapability.target_user_domain == target_ref.domain,
                BotDMCapability.pair_key == payload.pair_key,
                BotDMCapability.authority_domain == payload.authority_domain,
                usable_dm_capability(),
            )
            .execution_options(populate_existing=True)
        )
        if payload.refresh_grant_id is not None
        else None
    )
    if payload.refresh_grant_id is not None and observed_refresh is None:
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    refresh_expectation = (
        bot_dm_capability_fence_expectation(observed_refresh)
        if observed_refresh is not None
        else None
    )
    # Peer discovery can perform federation I/O. Complete it before acquiring
    # the installation or grant locks used for the authoritative recheck.
    if payload.authority_domain != settings.domain:
        await ensure_peer(session, settings, payload.authority_domain)
    entitlement = await current_bot_dm_entitlement(
        session,
        settings,
        source_kind=payload.source_kind,
        installation_ref=installation_ref,
        application_ref=application_ref,
        bot_ref=bot_ref,
        target_ref=target_ref,
        expected_target_username=(payload.target.username if observed_refresh is None else None),
    )
    application = entitlement.application
    bot = entitlement.bot
    target = entitlement.target
    granted_scopes = entitlement.granted_scopes
    granted_intents = entitlement.granted_intents

    if (
        "dm.send" not in granted_scopes
        or "direct_messages" not in granted_intents
        or "dm.send" not in application.default_scopes
        or "direct_messages" not in application.default_intents
    ):
        raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_REQUIRED"})
    handles = [
        f"{bot.username}@{bot.origin_domain}",
        f"{target.username}@{target.origin_domain}",
    ]
    if observed_refresh is None and (
        dm_pair_key(*handles) != payload.pair_key
        or dm_authority_domain(*handles) != payload.authority_domain
    ):
        raise HTTPException(status_code=409, detail={"code": "BOT_DM_GRANT_CONTEXT_INVALID"})
    if refresh_expectation is not None:
        previous = await lock_bot_dm_capability_projection(
            session,
            refresh_expectation,
            require_active=True,
        )
        if previous is None:
            raise HTTPException(status_code=403, detail={"code": "BOT_DM_GRANT_INVALID"})
    else:
        await session.scalar(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"bot-dm-capability:{grant_id}", 0)
                )
            )
        )
        previous = await session.scalar(
            select(BotDMCapability)
            .where(BotDMCapability.grant_id == grant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    expiry = next_capability_expiry()
    candidate = BotDMCapabilityPayload(
        grant_id=grant_id,
        source_kind=payload.source_kind,
        installation_ref=payload.installation_ref,
        application_ref=payload.application_ref,
        bot_user_ref=payload.bot_user_ref,
        guild_ref=entitlement.guild_ref,
        installing_user_ref=entitlement.installing_user_ref,
        target_user_ref=str(target_ref),
        pair_key=payload.pair_key,
        authority_domain=payload.authority_domain,
        scopes=sorted(set(granted_scopes).intersection(application.default_scopes)),
        intents=sorted(set(granted_intents).intersection(application.default_intents)),
        channel_restrictions=entitlement.channel_restrictions or [],
        e2ee_mode=entitlement.e2ee_mode,
        installation_revision=str(entitlement.installation_revision),
        runtime_manifest_generation=authority_runtime.manifest_generation,
        runtime_revocation_generation=authority_runtime.revocation_generation,
        target_access_revocation_generation=authority_runtime.access_revocation_generation,
        runtime_snapshot_fingerprint=application_runtime_snapshot_fingerprint(
            authority_runtime
        ).hex(),
        revision=str(previous.revision if previous is not None else 1),
        status="active",
        expires_at_ms=str(int(expiry.timestamp() * 1000)),
    )
    revision = 1
    if previous is not None:
        try:
            previous_envelope = EventEnvelope.model_validate(previous.proof)
            previous_payload = BotDMCapabilityPayload.model_validate(previous_envelope.content)
        except ValueError as exc:
            raise RuntimeError("stored bot DM capability proof is malformed") from exc
        revision = previous.revision
        if payload.refresh_grant_id is None or capability_authorization_fingerprint(
            previous_payload
        ) != capability_authorization_fingerprint(candidate):
            revision += 1
    capability = candidate.model_copy(update={"revision": str(revision)})
    require_capability_runtime_binding(
        capability,
        authority_runtime_envelope,
        authority_runtime,
    )
    envelope = await build_envelope(
        session,
        settings,
        BOT_DM_CAPABILITY_EVENT,
        bot,
        capability.model_dump(mode="json"),
        authority_attested_actor=True,
    )
    proof = await apply_bot_dm_capability(
        session,
        snowflake,
        # Keep the same strict Pydantic form that A and C verify.
        EventEnvelope.model_validate(envelope),
        capability,
        runtime_admitted=True,
        preserve_local_fence=settings.domain == capability.authority_domain,
    )
    if proof[0] is None or proof[0].target_user_id != target.id:
        raise RuntimeError("bot DM capability target changed while locked")
    # Active grants travel only in A's synchronous admission/refresh request.
    # Asynchronous fanout could otherwise reactivate an old conversation after
    # C rejected current privacy. B still durably fans out terminal tombstones.
    if commit:
        await session.commit()
    return envelope
