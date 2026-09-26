from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import wake_email_outbox
from app.api.dependencies import get_redis, get_session
from app.auth.turnstile import TurnstileUnavailableError, verify_turnstile_token
from app.core.proxy import connection_client_ip
from app.core.rate_limits import ClientRateLimit, enforce_keyed_rate_limit
from app.core.settings import Settings, get_settings
from app.email.backends import OutboundEmail
from app.email.outbox import enqueue_email_intent

router = APIRouter(prefix="/api/v1/support", tags=["support"])


class SupportReason(StrEnum):
    ACCOUNT = "Account access"
    TECHNICAL = "Technical problem"
    SAFETY = "Report abuse or a safety concern"
    PRIVACY = "Privacy or account deletion"
    OTHER = "Other"


class SupportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: EmailStr = Field(max_length=320)
    reason: SupportReason
    message: Annotated[str, StringConstraints(min_length=1, max_length=5000)]
    turnstile_token: str | None = Field(default=None, min_length=1, max_length=2048)


def support_enabled(settings: Settings) -> bool:
    return settings.legal_contact_email is not None and settings.email_backend != "disabled"


@router.get("/config")
async def support_configuration(settings: Settings = Depends(get_settings)) -> dict[str, object]:
    return {
        "enabled": support_enabled(settings),
        "reasons": list(SupportReason),
        "turnstile": {
            "enabled": settings.turnstile_enabled,
            "site_key": settings.turnstile_site_key if settings.turnstile_enabled else None,
        },
    }


@router.post("", status_code=202)
async def submit_support(
    payload: SupportRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not support_enabled(settings):
        raise HTTPException(status_code=503, detail="Support requests are currently unavailable")
    ip = connection_client_ip(request, settings)
    await enforce_keyed_rate_limit(
        redis, response, ClientRateLimit("support", 3, 3600), identity=ip
    )
    if settings.turnstile_enabled:
        if not payload.turnstile_token:
            raise HTTPException(status_code=400, detail="Complete the security check")
        try:
            verified = await verify_turnstile_token(
                settings, payload.turnstile_token, ip, action="kaede-support"
            )
        except TurnstileUnavailableError as exc:
            raise HTTPException(status_code=503, detail="Security check is unavailable") from exc
        if not verified:
            raise HTTPException(status_code=400, detail="Complete the security check again")
    await enforce_keyed_rate_limit(
        redis,
        response,
        ClientRateLimit("support-global", 100, 3600),
        identity=settings.domain,
    )
    enqueue_email_intent(
        session,
        settings,
        None,
        OutboundEmail(
            to=str(settings.legal_contact_email),
            subject=f"[Kaede support] {payload.reason.value}",
            text=(
                f"Support request for {settings.domain}\n"
                f"Reason: {payload.reason.value}\n"
                f"Reply to: {payload.email} (provided by the visitor; not verified)\n\n"
                f"{payload.message}"
            ),
        ),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    await session.commit()
    await wake_email_outbox()
    return {"status": "accepted"}
