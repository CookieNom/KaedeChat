from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from redis.asyncio import Redis
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import (
    auth_error,
    client_ip,
    lock_current_session,
    mfa_rate_limited,
    submitted_password_protocol_matches,
    verify_submitted_password,
)
from app.api.dependencies import AuthenticatedUser, get_redis, get_session, require_user
from app.auth.account_status import account_is_banned
from app.auth.schemas import MfaSetupRequest
from app.auth.service import (
    clear_mfa_account_failures,
    mfa_attempt_locked,
    record_mfa_verification_failure,
    revoke_user_sessions,
    verify_mfa_code,
)
from app.core.settings import Settings, get_settings
from app.core.task_wake import enqueue_best_effort
from app.db.bot_models import BotApplication, DeveloperTeam, DeveloperTeamMember
from app.db.models import Guild, User

router = APIRouter(tags=["users"])


@router.get("/@me/content-deletion")
async def deletion_status(auth: AuthenticatedUser = Depends(require_user)) -> dict[str, object]:
    return auth.user.content_deletion or {"status": "idle"}


@router.delete("/@me/content", status_code=202)
@router.delete("/@me", status_code=202)
async def request_deletion(
    request: Request,
    payload: MfaSetupRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        user is None
        or account_is_banned(user)
        or not await lock_current_session(session, auth, user)
    ):
        raise auth_error("AUTHENTICATION_REQUIRED", "Authentication required", 401)
    ip = client_ip(request, settings)
    if await mfa_attempt_locked(redis, user.id, user.origin_domain, ip):
        raise mfa_rate_limited()
    if not submitted_password_protocol_matches(user, payload.password_kdf_version) or not (
        await verify_submitted_password(payload.password, user.password_hash)
    ):
        await record_mfa_verification_failure(redis, user.id, user.origin_domain, ip)
        raise auth_error("INVALID_CREDENTIALS", "Invalid credentials", 401)
    if user.totp_secret_encrypted is not None and (
        payload.current_code is None
        or not await verify_mfa_code(session, settings, user, payload.current_code)
    ):
        await record_mfa_verification_failure(redis, user.id, user.origin_domain, ip)
        raise auth_error("INVALID_MFA", "Current MFA code is invalid", 401)
    if user.content_deletion and user.content_deletion.get("status") == "pending":
        raise HTTPException(
            409,
            detail={
                "code": "DELETION_IN_PROGRESS",
                "message": "Content deletion is already in progress. Please wait for it to finish.",
            },
        )
    delete_account = request.url.path.endswith("/@me")
    if (
        delete_account
        and await session.scalar(
            select(Guild.id)
            .where(Guild.owner_id == user.id, Guild.owner_domain == user.origin_domain)
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            409,
            detail={
                "code": "GUILD_OWNER",
                "message": "Transfer ownership or delete your guilds before deleting your account.",
            },
        )
    if (
        delete_account
        and await session.scalar(
            select(DeveloperTeamMember.team_id)
            .join(
                DeveloperTeam,
                (DeveloperTeam.id == DeveloperTeamMember.team_id)
                & (DeveloperTeam.origin_domain == DeveloperTeamMember.team_domain),
            )
            .where(
                DeveloperTeamMember.user_id == user.id,
                DeveloperTeamMember.user_domain == user.origin_domain,
                DeveloperTeamMember.role == "owner",
                or_(
                    DeveloperTeam.personal.is_(False),
                    exists().where(
                        BotApplication.team_id == DeveloperTeamMember.team_id,
                        BotApplication.team_domain == DeveloperTeamMember.team_domain,
                        BotApplication.status != "deleted",
                    ),
                ),
            )
            .limit(1)
        )
        is not None
    ):
        raise HTTPException(
            409,
            detail={
                "code": "APPLICATION_OWNER",
                "message": (
                    "Transfer or delete your developer teams and applications "
                    "before deleting your account."
                ),
            },
        )
    now = datetime.now(UTC)
    user.content_deletion = {"status": "pending", "before": now.isoformat(), "remote_failures": 0}
    if delete_account:
        # Keep the identity row: its case-insensitive unique indexes permanently
        # reserve the email/username, and historical references remain valid.
        user.deleted_at = now
        user.disabled_at = now
        user.password_hash = None
        user.password_kdf_version = None
        user.password_auth_salt = None
        user.e2ee_vault_salt = None
        user.totp_secret_encrypted = None
        user.email_verified_at = None
        user.suspended_until = None
        user.age_assurance_state = "unknown"
        user.e2ee_recovery_token_hash = None
        user.e2ee_recovery_session_id = None
        user.e2ee_recovery_generation = None
        user.e2ee_recovery_expires_at = None
    from app.federation.relationships import queue_profile_updates

    user.display_name = None
    user.bio = None
    user.custom_status = None
    user.avatar_hash = None
    user.banner_hash = None
    user.profile_version += 1
    destinations = await queue_profile_updates(session, settings, user)
    await clear_mfa_account_failures(redis, user.id, user.origin_domain)
    await session.commit()
    if delete_account:
        try:
            await revoke_user_sessions(session, redis, settings, user)
        except Exception:
            # SQL already bars authentication; the durable worker retries the
            # live connection revocations if Redis is temporarily unavailable.
            from app.auth.deletion import log

            log.exception("account_deletion_session_wake_failed", user_id=user.id)
    from app.tasks import delete_account_content, federation_deliver

    for destination in destinations:
        await enqueue_best_effort(federation_deliver, destination)

    await enqueue_best_effort(delete_account_content)
    return {"status": "pending"}
