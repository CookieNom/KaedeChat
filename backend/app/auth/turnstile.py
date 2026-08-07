from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.settings import Settings

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
REGISTER_ACTION = "kaede-register"
LOGIN_ACTION = "kaede-login"


class TurnstileUnavailableError(RuntimeError):
    """Cloudflare could not be reached or returned an unusable response."""


@dataclass(frozen=True, slots=True)
class TurnstileResult:
    success: bool
    action: str | None
    hostname: str | None


def _parse_result(payload: object) -> TurnstileResult:
    if not isinstance(payload, dict):
        raise TurnstileUnavailableError("invalid verification response")
    action = payload.get("action")
    hostname = payload.get("hostname")
    return TurnstileResult(
        success=payload.get("success") is True,
        action=action if isinstance(action, str) else None,
        hostname=hostname.lower().removesuffix(".") if isinstance(hostname, str) else None,
    )


async def verify_turnstile_token(
    settings: Settings,
    token: str,
    remote_ip: str,
    *,
    action: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Verify a single-use Turnstile token and bind it to this form and host."""

    secret = settings.turnstile_secret
    if secret is None:
        raise TurnstileUnavailableError("Turnstile is not configured")
    form = {
        "secret": secret.get_secret_value(),
        "response": token,
        "remoteip": remote_ip,
    }
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(5.0),
            follow_redirects=False,
            trust_env=False,
        )
    try:
        response = await client.post(VERIFY_URL, data=form)
        if response.status_code != 200:
            raise TurnstileUnavailableError("verification service returned an error")
        try:
            result = _parse_result(response.json())
        except ValueError as exc:
            raise TurnstileUnavailableError("invalid verification response") from exc
    except httpx.HTTPError as exc:
        raise TurnstileUnavailableError("verification service is unavailable") from exc
    finally:
        if owns_client:
            await client.aclose()
    return result.success and result.action == action and result.hostname == settings.domain
