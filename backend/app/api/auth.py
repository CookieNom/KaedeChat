from __future__ import annotations

import hashlib
import html
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pyotp
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from redis.asyncio import Redis
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    AuthenticatedUser,
    get_redis,
    get_session,
    get_snowflake,
    require_user,
)
from app.auth.schemas import (
    EmailChangeRequest,
    LoginRequest,
    MfaCodeRequest,
    MfaDisableRequest,
    MfaLoginRequest,
    MfaSetupRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    SessionSummary,
    TokenRequest,
    VerificationResendRequest,
)
from app.auth.security import (
    PasswordHashBusy,
    decrypt_secret,
    encrypt_secret,
    hash_password_async,
    recovery_code,
    verify_password_async,
)
from app.auth.service import (
    InvalidTokenError,
    IssuedSession,
    claim_mfa_ticket,
    clear_mfa_account_failures,
    consume_mfa_ticket,
    consume_one_time_token,
    create_one_time_token,
    create_session,
    credential_fingerprint,
    enable_totp,
    invalidate_active_mfa_ticket,
    issue_mfa_ticket,
    load_mfa_setup,
    mfa_attempt_locked,
    mfa_ip_locked,
    mfa_setup_key,
    record_mfa_ip_failure,
    record_mfa_ticket_failure,
    record_mfa_verification_failure,
    revoke_user_sessions,
    rotate_refresh_token,
    store_mfa_setup,
    verify_mfa_code,
)
from app.auth.tokens import AccessTokenStore, LoginLimiter
from app.auth.turnstile import (
    LOGIN_ACTION,
    REGISTER_ACTION,
    TurnstileUnavailableError,
    verify_turnstile_token,
)
from app.core.proxy import resolve_client_ip
from app.core.settings import Settings, get_settings
from app.core.snowflake import SnowflakeGenerator
from app.core.task_wake import enqueue_best_effort
from app.db.models import OneTimeToken, RecoveryCode, Session, User, UserSettings
from app.email.outbox import enqueue_email_intent
from app.email.templates import email_change_confirmation, password_reset_email, verification_email
from app.tasks import email_outbox_drain

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def auth_error(code: str, message: str, status_code: int, **details: object) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **details},
    )


def mfa_rate_limited() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={
            "code": "MFA_RATE_LIMITED",
            "message": "Too many MFA attempts; try again later",
            "retry_after_ms": 900_000,
        },
        headers={"Retry-After": "900"},
    )


def password_work_busy() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail={"code": "PASSWORD_WORK_BUSY", "message": "Too many password requests"},
        headers={"Retry-After": "1"},
    )


def email_delivery_enabled(settings: Settings) -> bool:
    return settings.email_backend != "disabled"


def email_verification_required(user: User, settings: Settings) -> bool:
    return (
        email_delivery_enabled(settings)
        and user.email is not None
        and user.email_verified_at is None
    )


async def verify_submitted_password(password: str, password_hash: str | None) -> bool:
    try:
        return await verify_password_async(password, password_hash)
    except PasswordHashBusy:
        raise password_work_busy() from None


async def hash_submitted_password(password: str) -> str:
    try:
        return await hash_password_async(password)
    except PasswordHashBusy:
        raise password_work_busy() from None


async def reject_invalid_login(
    limiter: LoginLimiter,
    admission_key: str,
    ip: str,
    *,
    turnstile_enabled: bool,
    failed_account_key: str | None = None,
) -> NoReturn:
    if failed_account_key is not None:
        await limiter.failure(failed_account_key, ip)
    if turnstile_enabled:
        await limiter.require_challenge(admission_key, ip)
    raise auth_error(
        "INVALID_CREDENTIALS",
        "Invalid credentials",
        401,
        turnstile_required=turnstile_enabled,
    )


async def lock_current_session(session: AsyncSession, auth: AuthenticatedUser, user: User) -> bool:
    now = datetime.now(UTC)
    session_id = await session.scalar(
        select(Session.id)
        .where(
            Session.id == auth.grant.session_id,
            Session.user_id == user.id,
            Session.user_domain == user.origin_domain,
            Session.revoked_at.is_(None),
            Session.expires_at > now,
            Session.absolute_expires_at > now,
        )
        .with_for_update()
    )
    return session_id is not None


def client_ip(request: Request, settings: Settings) -> str:
    supplied_secret = request.headers.get("X-Kaede-Proxy-Secret")
    configured_secret = (
        settings.proxy_secret.get_secret_value() if settings.proxy_secret is not None else None
    )
    return resolve_client_ip(
        supplied_secret=supplied_secret,
        configured_secret=configured_secret,
        forwarded_for=request.headers.get("X-Forwarded-For"),
        direct_host=request.client.host if request.client is not None else None,
    )


async def wake_email_outbox() -> None:
    # Redis is only a low-latency wake-up path.  The encrypted SQL row has
    # already committed, and the scheduled sweep will recover a failed wake.
    await enqueue_best_effort(email_outbox_drain)


def token_response(
    request: Request, issued: IssuedSession, settings: Settings, *, status_code: int = 200
) -> JSONResponse:
    access = issued.access_token
    refresh = issued.refresh_token
    client_kind = request.headers.get("X-Kaede-Client", "").strip().lower()
    native_client = client_kind in {"desktop", "mobile"}
    response = JSONResponse(
        {
            "access_token": access if native_client else None,
            "refresh_token": refresh if native_client else None,
            "token_type": "opaque",
            "expires_in": settings.access_token_ttl_seconds,
            "mfa_required": False,
            "mfa_ticket": None,
        },
        status_code=status_code,
    )
    if not native_client:
        secure = settings.environment == "production"
        response.set_cookie(
            "kc_access",
            access,
            max_age=settings.access_token_ttl_seconds,
            httponly=True,
            secure=secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            "kc_refresh",
            refresh,
            max_age=settings.refresh_sliding_days * 86400,
            httponly=True,
            secure=secure,
            samesite="strict",
            path="/api/v1/auth",
        )
    return response


@router.get("/config")
async def auth_configuration(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    email_enabled = email_delivery_enabled(settings)
    return {
        "email_required": email_enabled,
        "password_recovery_enabled": email_enabled,
        "turnstile": {
            "enabled": settings.turnstile_enabled,
            "site_key": settings.turnstile_site_key if settings.turnstile_enabled else None,
        },
        "gif_picker_enabled": settings.klipy_enabled,
        "message_search_enabled": settings.search_enabled,
    }


@router.get("/native-challenge", response_class=HTMLResponse, include_in_schema=False)
async def native_turnstile_challenge(
    action: str,
    request_id: str,
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Render Turnstile inside the packaged desktop client's restricted WebView.

    The page never accepts credentials and does not exchange the challenge itself.
    Its only output is the short-lived provider token delivered over the native
    WebView IPC bridge, where the desktop parent verifies ``request_id`` before
    attaching it to login or registration.
    """

    if not settings.turnstile_enabled or settings.turnstile_site_key is None:
        raise auth_error("TURNSTILE_DISABLED", "Verification is not enabled", 404)
    if action not in {LOGIN_ACTION, REGISTER_ACTION}:
        raise auth_error("TURNSTILE_ACTION_INVALID", "Invalid verification action", 400)
    if re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id) is None:
        raise auth_error("TURNSTILE_REQUEST_INVALID", "Invalid verification request", 400)
    safe_site_key = html.escape(settings.turnstile_site_key, quote=True)
    safe_action = html.escape(action, quote=True)
    safe_request_id = html.escape(request_id, quote=True)
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kaede verification</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"
 async defer></script>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;
place-items:center;background:#111210;color:#f4eee5;font:15px system-ui,sans-serif}}
main{{width:min(430px,calc(100vw - 32px));padding:28px;border:1px solid #3b3c36;border-radius:20px;
background:#20211e}}h1{{margin:0 0 8px;font-size:24px}}p{{color:#aaa298;margin:0 0 22px}}
#challenge{{min-height:70px;display:grid;place-items:center}}#error{{color:#ef6b68;margin-top:14px}}
</style></head><body><main><h1>One quick check</h1>
<p>Complete this verification to continue securely in Kaede Desktop.</p>
<div id="challenge"></div><div id="error" role="alert"></div></main>
<script>
const requestId={safe_request_id!r};
function emit(kind,value){{
  const payload=JSON.stringify({{kind,request_id:requestId,value}});
  if(window.ipc&&window.ipc.postMessage) window.ipc.postMessage(payload);
}}
window.addEventListener('load',()=>{{
 const wait=setInterval(()=>{{if(!window.turnstile)return;clearInterval(wait);
  window.turnstile.render('#challenge',{{sitekey:{safe_site_key!r},action:{safe_action!r},
   callback:(token)=>emit('complete',token),
   'error-callback':()=>{{
     document.querySelector('#error').textContent='Verification failed. Try again.';
     emit('error','provider');
   }},
   'expired-callback':()=>emit('expired','expired')}});
 }},50);
}});
</script></body></html>"""
    return HTMLResponse(
        body,
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' 'unsafe-inline' "
                "https://challenges.cloudflare.com; frame-src https://challenges.cloudflare.com; "
                "connect-src https://challenges.cloudflare.com; style-src 'unsafe-inline'; "
                "img-src https://challenges.cloudflare.com data:; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    payload: RegisterRequest,
    session: AsyncSession = Depends(get_session),
    snowflake: SnowflakeGenerator = Depends(get_snowflake),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
) -> dict[str, str | bool]:
    # Password hashing is intentionally expensive. Bound anonymous callers
    # before spending Argon2 work or enqueueing mail.
    if not await redis.set(f"auth:register:{client_ip(request, settings)}", "1", ex=5, nx=True):
        raise auth_error("RATE_LIMITED", "Try again shortly", 429)
    email_enabled = email_delivery_enabled(settings)
    if email_enabled and payload.email is None:
        raise auth_error("EMAIL_REQUIRED", "Email is required on this instance", 422)
    if settings.turnstile_enabled:
        if payload.turnstile_token is None:
            raise auth_error("TURNSTILE_REQUIRED", "Complete the verification challenge", 403)
        try:
            verified = await verify_turnstile_token(
                settings,
                payload.turnstile_token,
                client_ip(request, settings),
                action=REGISTER_ACTION,
            )
        except TurnstileUnavailableError as exc:
            raise auth_error(
                "TURNSTILE_UNAVAILABLE",
                "Verification is temporarily unavailable; try again",
                503,
            ) from exc
        if not verified:
            raise auth_error(
                "TURNSTILE_INVALID",
                "Verification expired or was unsuccessful; try again",
                403,
            )
    email = str(payload.email).lower() if email_enabled and payload.email is not None else None
    user = User(
        id=await snowflake.mint(),
        origin_domain=settings.domain,
        is_local=True,
        username=payload.username,
        email=email,
        password_hash=await hash_submitted_password(payload.password),
    )
    session.add(user)
    try:
        await session.flush()
        session.add(
            UserSettings(user_id=user.id, user_domain=user.origin_domain, user_is_local=True)
        )
        if email_enabled:
            token, token_record = await create_one_time_token(
                session,
                user,
                purpose="email_verify",
                expires_in=timedelta(hours=settings.verification_ttl_hours),
            )
            enqueue_email_intent(
                session,
                settings,
                token_record,
                verification_email(
                    to=str(user.email),
                    app_url=settings.app_url,
                    token=token,
                    expires_in_hours=settings.verification_ttl_hours,
                ),
            )
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise auth_error(
            "REGISTRATION_CONFLICT",
            "That username or email is unavailable",
            status.HTTP_409_CONFLICT,
        ) from exc
    if email_enabled:
        await wake_email_outbox()
    return {
        "id": str(user.id),
        "handle": f"{user.username}@{user.origin_domain}",
        "email_verification_required": email_enabled,
    }


@router.post("/verify-email")
async def verify_email(
    payload: TokenRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, str]:
    try:
        _, user = await consume_one_time_token(session, payload.token, purpose="email_verify")
    except InvalidTokenError as exc:
        raise auth_error("INVALID_TOKEN", "Token is invalid or expired", 400) from exc
    user.email_verified_at = datetime.now(UTC)
    await session.commit()
    return {"status": "verified"}


@router.post("/verify-email/resend", status_code=status.HTTP_202_ACCEPTED)
async def resend_verification_email(
    payload: VerificationResendRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not email_delivery_enabled(settings):
        return {"status": "accepted"}
    email = str(payload.email).lower()
    limiter_key = hashlib.sha256(email.encode()).hexdigest()
    allowed = await redis.set(
        f"auth:verification_resend:{client_ip(request, settings)}:{limiter_key}",
        "1",
        ex=60,
        nx=True,
    )
    if not allowed:
        return {"status": "accepted"}
    user = await session.scalar(
        select(User)
        .where(User.is_local.is_(True), func.lower(User.email) == email)
        .with_for_update()
    )
    if user is not None and user.email_verified_at is None:
        try:
            token, token_record = await create_one_time_token(
                session,
                user,
                purpose="email_verify",
                expires_in=timedelta(hours=settings.verification_ttl_hours),
            )
        except ValueError:
            # A concurrent account purge or administrative deletion must not
            # turn this enumeration-resistant endpoint into an existence leak.
            await session.rollback()
            return {"status": "accepted"}
        enqueue_email_intent(
            session,
            settings,
            token_record,
            verification_email(
                to=str(user.email),
                app_url=settings.app_url,
                token=token,
                expires_in_hours=settings.verification_ttl_hours,
            ),
        )
        await session.commit()
        await wake_email_outbox()
    return {"status": "accepted"}


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    identifier = payload.identifier.strip().lower()
    limiter = LoginLimiter(redis)
    ip = client_ip(request, settings)
    admission_key = hashlib.sha256(identifier.encode()).hexdigest()
    if not await limiter.admit(admission_key, ip):
        raise HTTPException(
            status_code=429,
            detail={"code": "LOGIN_RATE_LIMITED", "message": "Too many login attempts"},
            headers={"Retry-After": "5"},
        )
    challenge_required = settings.turnstile_enabled and await limiter.challenge_required(
        admission_key, ip
    )
    if challenge_required:
        if payload.turnstile_token is None:
            raise auth_error(
                "TURNSTILE_REQUIRED",
                "Complete the verification challenge before trying again",
                403,
                turnstile_required=True,
            )
        try:
            verified = await verify_turnstile_token(
                settings,
                payload.turnstile_token,
                ip,
                action=LOGIN_ACTION,
            )
        except TurnstileUnavailableError as exc:
            raise auth_error(
                "TURNSTILE_UNAVAILABLE",
                "Verification is temporarily unavailable; try again",
                503,
                turnstile_required=True,
            ) from exc
        if not verified:
            raise auth_error(
                "TURNSTILE_INVALID",
                "Verification expired or was unsuccessful; try again",
                403,
                turnstile_required=True,
            )
        await limiter.clear_challenge(admission_key, ip)
    if await limiter.is_locked(admission_key, ip):
        await reject_invalid_login(
            limiter,
            admission_key,
            ip,
            turnstile_enabled=settings.turnstile_enabled,
        )
    handle_username: str | None = None
    if "@" in identifier:
        candidate_username, candidate_domain = identifier.rsplit("@", 1)
        if candidate_domain == settings.domain:
            handle_username = candidate_username
    user = await session.scalar(
        select(User).where(
            User.is_local.is_(True),
            or_(
                func.lower(User.email) == identifier,
                func.lower(User.username) == identifier,
                func.lower(User.username) == handle_username,
            ),
        )
    )
    account_identity = (
        f"{user.id}@{user.origin_domain}" if user is not None else f"unknown:{identifier}"
    )
    account_key = hashlib.sha256(account_identity.encode()).hexdigest()
    if await limiter.is_locked(account_key, ip):
        await reject_invalid_login(
            limiter,
            admission_key,
            ip,
            turnstile_enabled=settings.turnstile_enabled,
        )
    password_valid = await verify_submitted_password(
        payload.password, user.password_hash if user else None
    )
    if user is None or not password_valid:
        await reject_invalid_login(
            limiter,
            admission_key,
            ip,
            turnstile_enabled=settings.turnstile_enabled,
            failed_account_key=account_key,
        )
    verified_hash = user.password_hash
    locked_user = await session.scalar(
        select(User)
        .where(User.id == user.id, User.origin_domain == user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_user is None or (
        locked_user.password_hash != verified_hash
        and not await verify_submitted_password(payload.password, locked_user.password_hash)
    ):
        await reject_invalid_login(
            limiter,
            admission_key,
            ip,
            turnstile_enabled=settings.turnstile_enabled,
            failed_account_key=account_key,
        )
    user = locked_user
    if user.disabled_at is not None:
        await reject_invalid_login(
            limiter,
            admission_key,
            ip,
            turnstile_enabled=settings.turnstile_enabled,
            failed_account_key=account_key,
        )
    await limiter.success(account_key)
    if settings.turnstile_enabled:
        await limiter.clear_challenge(admission_key, ip)
    if email_verification_required(user, settings):
        raise auth_error("EMAIL_NOT_VERIFIED", "Verify your email before signing in", 403)
    if user.totp_secret_encrypted is not None:
        if await mfa_attempt_locked(redis, user.id, user.origin_domain, ip):
            raise mfa_rate_limited()
        ticket = await issue_mfa_ticket(redis, user)
        return JSONResponse({"mfa_required": True, "mfa_ticket": ticket, "token_type": "opaque"})
    issued = await create_session(
        session,
        redis,
        settings,
        user,
        device_name=payload.device_name,
        user_agent=request.headers.get("User-Agent"),
        ip_address=ip,
    )
    return token_response(request, issued, settings)


@router.post("/mfa")
async def complete_mfa(
    payload: MfaLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    ip = client_ip(request, settings)
    if await mfa_ip_locked(redis, ip):
        raise mfa_rate_limited()
    try:
        user_id, domain, ticket_fingerprint = await consume_mfa_ticket(redis, payload.ticket)
    except InvalidTokenError as exc:
        await record_mfa_ip_failure(redis, ip)
        raise auth_error("INVALID_MFA", "MFA ticket or code is invalid", 401) from exc
    if await mfa_attempt_locked(redis, user_id, domain, ip):
        raise mfa_rate_limited()
    user = await session.scalar(
        select(User).where(User.id == user_id, User.origin_domain == domain).with_for_update()
    )
    if (
        user is None
        or not user.is_local
        or user.password_hash is None
        or not secrets.compare_digest(
            ticket_fingerprint,
            credential_fingerprint(user),
        )
        or user.disabled_at is not None
        or email_verification_required(user, settings)
        or not await verify_mfa_code(session, settings, user, payload.code)
    ):
        await record_mfa_ticket_failure(
            redis,
            payload.ticket,
            user_id=user_id,
            user_domain=domain,
            ip=ip,
        )
        raise auth_error("INVALID_MFA", "MFA ticket or code is invalid", 401)
    if not await claim_mfa_ticket(redis, payload.ticket):
        raise auth_error("INVALID_MFA", "MFA ticket or code is invalid", 401)
    await clear_mfa_account_failures(redis, user_id, domain)
    issued = await create_session(
        session,
        redis,
        settings,
        user,
        device_name=payload.device_name,
        user_agent=request.headers.get("User-Agent"),
        ip_address=ip,
    )
    return token_response(request, issued, settings)


@router.post("/refresh")
async def refresh(
    payload: RefreshRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    cookie_refresh = request.cookies.get("kc_refresh")
    raw = payload.refresh_token or cookie_refresh
    if raw is None:
        raise auth_error("INVALID_REFRESH_TOKEN", "Refresh token is invalid", 401)
    if payload.refresh_token is None and request.headers.get("X-Kaede-Client") != "web":
        raise auth_error("CSRF_GUARD", "Missing web client header", 403)
    try:
        issued = await rotate_refresh_token(session, redis, settings, raw)
    except InvalidTokenError as exc:
        raise auth_error("INVALID_REFRESH_TOKEN", "Refresh token is invalid", 401) from exc
    return token_response(request, issued, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    await session.execute(
        update(Session)
        .where(Session.id == auth.grant.session_id)
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    await AccessTokenStore(redis, settings.access_token_ttl_seconds).revoke_session(
        auth.grant.session_id
    )
    response = Response(status_code=204)
    response.delete_cookie("kc_access", path="/")
    response.delete_cookie("kc_refresh", path="/api/v1/auth")
    return response


@router.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> list[SessionSummary]:
    now = datetime.now(UTC)
    records = (
        await session.scalars(
            select(Session)
            .where(
                Session.user_id == auth.user.id,
                Session.user_domain == auth.user.origin_domain,
                Session.revoked_at.is_(None),
                Session.absolute_expires_at > now,
            )
            .order_by(Session.last_used_at.desc(), Session.created_at.desc())
        )
    ).all()
    return [
        SessionSummary(
            id=record.id,
            device_name=record.device_name,
            user_agent=record.user_agent,
            ip_address=record.ip_address,
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            expires_at=min(record.expires_at, record.absolute_expires_at),
            current=record.id == auth.grant.session_id,
        )
        for record in records
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: str,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> Response:
    if len(session_id) > 64:
        raise auth_error("SESSION_NOT_FOUND", "Session was not found", 404)
    record = await session.scalar(
        select(Session)
        .where(
            Session.id == session_id,
            Session.user_id == auth.user.id,
            Session.user_domain == auth.user.origin_domain,
            Session.revoked_at.is_(None),
        )
        .with_for_update()
    )
    if record is None:
        raise auth_error("SESSION_NOT_FOUND", "Session was not found", 404)
    record.revoked_at = datetime.now(UTC)
    await session.commit()
    await AccessTokenStore(redis, settings.access_token_ttl_seconds).revoke_session(record.id)
    return Response(status_code=204)


@router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    payload: PasswordForgotRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not email_delivery_enabled(settings):
        return {"status": "accepted"}
    email = str(payload.email).lower()
    limiter_key = hashlib.sha256(email.encode()).hexdigest()
    allowed = await redis.set(
        f"auth:password_recovery:{client_ip(request, settings)}:{limiter_key}",
        "1",
        ex=60,
        nx=True,
    )
    if not allowed:
        return {"status": "accepted"}
    user = await session.scalar(
        select(User).where(User.is_local.is_(True), func.lower(User.email) == email)
    )
    if user is not None and user.email_verified_at is not None:
        try:
            token, token_record = await create_one_time_token(
                session,
                user,
                purpose="password_reset",
                expires_in=timedelta(minutes=settings.password_reset_ttl_minutes),
            )
        except ValueError:
            await session.rollback()
            return {"status": "accepted"}
        enqueue_email_intent(
            session,
            settings,
            token_record,
            password_reset_email(
                to=str(user.email),
                app_url=settings.app_url,
                token=token,
                expires_in_minutes=settings.password_reset_ttl_minutes,
            ),
        )
        await session.commit()
        await wake_email_outbox()
    return {"status": "accepted"}


@router.post("/password/reset")
async def reset_password(
    payload: PasswordResetRequest,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    try:
        _, user = await consume_one_time_token(session, payload.token, purpose="password_reset")
    except InvalidTokenError as exc:
        raise auth_error("INVALID_TOKEN", "Token is invalid or expired", 400) from exc
    user.password_hash = await hash_submitted_password(payload.password)
    await session.execute(
        delete(OneTimeToken).where(
            OneTimeToken.user_id == user.id,
            OneTimeToken.user_domain == user.origin_domain,
            OneTimeToken.consumed_at.is_(None),
        )
    )
    await revoke_user_sessions(session, redis, settings, user)
    return {"status": "password_updated"}


@router.post("/email/change")
async def request_email_change(
    payload: EmailChangeRequest,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not email_delivery_enabled(settings):
        raise auth_error("EMAIL_DISABLED", "Email is disabled on this instance", 409)
    # Re-read and lock the credentials before authorizing the change.  A
    # password reset can revoke this session concurrently; checking the user
    # object loaded by the authentication dependency would otherwise admit a
    # narrow stale-password race.
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_user is None
        or locked_user.disabled_at is not None
        or not await verify_submitted_password(payload.password, locked_user.password_hash)
    ):
        raise auth_error("INVALID_CREDENTIALS", "Invalid credentials", 401)
    email = str(payload.email).lower()
    exists = await session.scalar(
        select(User.id).where(User.is_local.is_(True), func.lower(User.email) == email)
    )
    if exists is not None:
        raise auth_error("EMAIL_UNAVAILABLE", "That email is unavailable", 409)
    token, token_record = await create_one_time_token(
        session,
        locked_user,
        purpose="email_change",
        expires_in=timedelta(minutes=30),
    )
    token_record.payload = {
        "email_encrypted": encrypt_secret(
            email,
            settings.secret_key_bytes,
            context=f"kaede-email-change:v1:{token_record.id}".encode(),
        ).hex()
    }
    enqueue_email_intent(
        session,
        settings,
        token_record,
        email_change_confirmation(
            to=email,
            app_url=settings.app_url,
            token=token,
        ),
    )
    await session.commit()
    await wake_email_outbox()
    return {"status": "confirmation_sent"}


@router.post("/email/change/confirm")
async def confirm_email_change(
    payload: TokenRequest,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    try:
        record, user = await consume_one_time_token(session, payload.token, purpose="email_change")
    except InvalidTokenError as exc:
        raise auth_error("INVALID_TOKEN", "Token is invalid or expired", 400) from exc
    encrypted_email = record.payload.get("email_encrypted")
    if not isinstance(encrypted_email, str):
        await session.rollback()
        raise auth_error("INVALID_TOKEN", "Token is invalid or expired", 400)
    try:
        user.email = decrypt_secret(
            bytes.fromhex(encrypted_email),
            settings.secret_key_bytes,
            context=f"kaede-email-change:v1:{record.id}".encode(),
        )
    except (InvalidTag, ValueError, UnicodeDecodeError) as exc:
        await session.rollback()
        raise auth_error("INVALID_TOKEN", "Token is invalid or expired", 400) from exc
    user.email_verified_at = datetime.now(UTC)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise auth_error("EMAIL_UNAVAILABLE", "That email is unavailable", 409) from exc
    return {"status": "email_updated"}


@router.post("/mfa/setup")
async def setup_mfa(
    payload: MfaSetupRequest,
    request: Request,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_user is None
        or locked_user.disabled_at is not None
        or not await lock_current_session(session, auth, locked_user)
    ):
        raise auth_error("AUTHENTICATION_REQUIRED", "Authentication required", 401)
    ip = client_ip(request, settings)
    if await mfa_attempt_locked(redis, locked_user.id, locked_user.origin_domain, ip):
        raise mfa_rate_limited()
    if not await verify_submitted_password(payload.password, locked_user.password_hash):
        await record_mfa_verification_failure(redis, locked_user.id, locked_user.origin_domain, ip)
        raise auth_error("INVALID_CREDENTIALS", "Invalid credentials", 401)
    if locked_user.totp_secret_encrypted is not None and (
        payload.current_code is None
        or not await verify_mfa_code(session, settings, locked_user, payload.current_code)
    ):
        await record_mfa_verification_failure(redis, locked_user.id, locked_user.origin_domain, ip)
        raise auth_error("INVALID_MFA", "Current MFA code is invalid", 401)
    await clear_mfa_account_failures(redis, locked_user.id, locked_user.origin_domain)
    # A recovery code used as the current factor is consumed before a pending
    # replacement secret is issued.
    await session.commit()
    secret = pyotp.random_base32()
    await store_mfa_setup(redis, locked_user, auth.grant.session_id, secret)
    uri = pyotp.TOTP(secret).provisioning_uri(
        name=f"{locked_user.username}@{locked_user.origin_domain}", issuer_name="Kaede Chat"
    )
    return {"secret": secret, "uri": uri}


@router.post("/mfa/enable")
async def enable_mfa(
    payload: MfaCodeRequest,
    request: Request,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_user is None
        or locked_user.disabled_at is not None
        or not await lock_current_session(session, auth, locked_user)
    ):
        raise auth_error("AUTHENTICATION_REQUIRED", "Authentication required", 401)
    ip = client_ip(request, settings)
    if await mfa_attempt_locked(redis, locked_user.id, locked_user.origin_domain, ip):
        raise mfa_rate_limited()
    key = mfa_setup_key(locked_user)
    secret = await load_mfa_setup(redis, locked_user, auth.grant.session_id)
    if secret is None or not pyotp.TOTP(secret).verify(payload.code, valid_window=1):
        await record_mfa_verification_failure(redis, locked_user.id, locked_user.origin_domain, ip)
        raise auth_error("INVALID_MFA", "MFA code is invalid", 400)
    codes = [recovery_code() for _ in range(10)]
    await enable_totp(session, settings, locked_user, secret, codes)
    await revoke_user_sessions(
        session,
        redis,
        settings,
        locked_user,
        keep_session_id=auth.grant.session_id,
    )
    await invalidate_active_mfa_ticket(redis, locked_user)
    await redis.delete(key)
    await clear_mfa_account_failures(redis, locked_user.id, locked_user.origin_domain)
    return {"status": "enabled", "recovery_codes": codes}


@router.post("/mfa/disable")
async def disable_mfa(
    payload: MfaDisableRequest,
    request: Request,
    auth: AuthenticatedUser = Depends(require_user),
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    locked_user = await session.scalar(
        select(User)
        .where(User.id == auth.user.id, User.origin_domain == auth.user.origin_domain)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        locked_user is None
        or locked_user.disabled_at is not None
        or not await lock_current_session(session, auth, locked_user)
    ):
        raise auth_error("AUTHENTICATION_REQUIRED", "Authentication required", 401)
    ip = client_ip(request, settings)
    if await mfa_attempt_locked(redis, locked_user.id, locked_user.origin_domain, ip):
        raise mfa_rate_limited()
    if not await verify_submitted_password(payload.password, locked_user.password_hash):
        await record_mfa_verification_failure(redis, locked_user.id, locked_user.origin_domain, ip)
        raise auth_error("INVALID_CREDENTIALS", "Invalid credentials", 401)
    if not await verify_mfa_code(session, settings, locked_user, payload.code):
        await record_mfa_verification_failure(redis, locked_user.id, locked_user.origin_domain, ip)
        raise auth_error("INVALID_MFA", "MFA code is invalid", 400)
    await clear_mfa_account_failures(redis, locked_user.id, locked_user.origin_domain)
    locked_user.totp_secret_encrypted = None
    await session.execute(
        delete(RecoveryCode).where(
            RecoveryCode.user_id == auth.user.id,
            RecoveryCode.user_domain == auth.user.origin_domain,
        )
    )
    await revoke_user_sessions(
        session,
        redis,
        settings,
        locked_user,
        keep_session_id=auth.grant.session_id,
    )
    await invalidate_active_mfa_ticket(redis, locked_user)
    await redis.delete(mfa_setup_key(locked_user))
    return {"status": "disabled"}
