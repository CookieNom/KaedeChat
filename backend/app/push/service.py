from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from app.auth.security import decrypt_secret
from app.core.settings import Settings
from app.db.models import PushDevice
from app.push.sync import PUSH_SYNC_TOKEN_RE

PUSH_TOKEN_CONTEXT = b"kaede-push-device-v1"
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def decrypt_device_token(device: PushDevice, settings: Settings) -> str:
    if device.token_encrypted is None:
        raise ValueError("push device does not contain a direct provider token")
    return decrypt_secret(
        device.token_encrypted,
        settings.secret_key_bytes,
        context=PUSH_TOKEN_CONTEXT,
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


@dataclass(slots=True)
class FcmResult:
    delivered: bool
    token_invalid: bool = False


def fcm_sync_payload(token: str, event_token: str, platform: str) -> dict[str, Any]:
    """Build a content-free wake message with no Kaede entity references."""

    if not PUSH_SYNC_TOKEN_RE.fullmatch(event_token):
        raise ValueError("invalid push sync token")
    message: dict[str, Any] = {
        "token": token,
        "data": {"sync_version": "1", "event_token": event_token},
    }
    if platform == "android":
        message["android"] = {
            "priority": "high",
            "ttl": "600s",
        }
    elif platform == "ios":
        message["apns"] = {
            "headers": {
                "apns-push-type": "background",
                "apns-priority": "5",
            },
            "payload": {"aps": {"content-available": 1}},
        }
    else:
        raise ValueError("unsupported push platform")
    return {"message": message}


def fcm_relay_payload(
    token: str,
    *,
    route_id: str,
    event_token: str,
    delivery_id: str,
    expires_at: int,
    wake_mac: str,
    platform: str,
) -> dict[str, Any]:
    """Build a MAC-authenticated, content-free relay wake."""

    for value in (route_id, event_token, delivery_id, wake_mac):
        if not PUSH_SYNC_TOKEN_RE.fullmatch(value):
            raise ValueError("invalid relay wake field")
    data = {
        "sync_version": "2",
        "route_id": route_id,
        "event_token": event_token,
        "delivery_id": delivery_id,
        "expires_at": str(expires_at),
        "wake_mac": wake_mac,
    }
    message: dict[str, Any] = {"token": token, "data": data}
    ttl = max(0, min(600, expires_at - int(time.time())))
    if platform == "android":
        message["android"] = {"priority": "high", "ttl": f"{ttl}s"}
    elif platform == "ios":
        message["apns"] = {
            "headers": {"apns-push-type": "background", "apns-priority": "5"},
            "payload": {"aps": {"content-available": 1}},
        }
    else:
        raise ValueError("unsupported push platform")
    return {"message": message}


class FcmClient:
    """Small FCM HTTP v1 client with a process-local OAuth token cache."""

    def __init__(self, encoded: str | None) -> None:
        if encoded is None:
            raise RuntimeError("Firebase service account is not configured")
        document = json.loads(base64.b64decode(encoded))
        self.project_id = str(document["project_id"])
        self.client_email = str(document["client_email"])
        self.token_uri = str(document["token_uri"])
        private_key = serialization.load_pem_private_key(
            str(document["private_key"]).encode("utf-8"), password=None
        )
        if not isinstance(private_key, RSAPrivateKey):
            raise ValueError("Firebase service account private key must be RSA")
        self.private_key = private_key
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _token(self) -> str:
        if self._access_token is not None and time.time() < self._expires_at - 60:
            return self._access_token
        async with self._lock:
            now = int(time.time())
            if self._access_token is not None and now < self._expires_at - 60:
                return self._access_token
            header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
            claims = _b64url(
                json.dumps(
                    {
                        "iss": self.client_email,
                        "scope": FCM_SCOPE,
                        "aud": self.token_uri,
                        "iat": now,
                        "exp": now + 3600,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            )
            unsigned = f"{header}.{claims}".encode("ascii")
            signature = self.private_key.sign(unsigned, padding.PKCS1v15(), hashes.SHA256())
            assertion = f"{header}.{claims}.{_b64url(signature)}"
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    self.token_uri,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            response.raise_for_status()
            body = response.json()
            self._access_token = str(body["access_token"])
            self._expires_at = now + int(body.get("expires_in", 3600))
            return self._access_token

    async def send_sync(
        self,
        token: str,
        *,
        event_token: str,
        platform: str,
    ) -> FcmResult:
        payload = fcm_sync_payload(token, event_token, platform)
        response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            for attempt in range(3):
                access_token = await self._token()
                response = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                    headers={"Authorization": f"Bearer {access_token}"},
                    json=payload,
                )
                if response.status_code == 401 and attempt == 0:
                    self._access_token = None
                    continue
                if response.status_code != 429 and response.status_code < 500:
                    break
                if attempt < 2:
                    try:
                        delay = min(2.0, max(0.0, float(response.headers["Retry-After"])))
                    except (KeyError, ValueError):
                        delay = 0.25 * (2**attempt)
                    await asyncio.sleep(delay)
        if response is None:  # pragma: no cover - loop always executes
            raise RuntimeError("FCM request did not execute")
        if response.is_success:
            return FcmResult(delivered=True)
        invalid = response.status_code in {400, 404}
        if invalid:
            try:
                details = response.json()["error"].get("details", [])
                codes = {str(item.get("errorCode", "")) for item in details}
                invalid = bool(codes & {"UNREGISTERED", "INVALID_ARGUMENT"})
            except (KeyError, TypeError, ValueError):
                invalid = response.status_code == 404
        if invalid:
            return FcmResult(delivered=False, token_invalid=True)
        response.raise_for_status()
        return FcmResult(delivered=False, token_invalid=invalid)

    async def send_relay(
        self,
        token: str,
        *,
        route_id: str,
        event_token: str,
        delivery_id: str,
        expires_at: int,
        wake_mac: str,
        platform: str,
    ) -> FcmResult:
        payload = fcm_relay_payload(
            token,
            route_id=route_id,
            event_token=event_token,
            delivery_id=delivery_id,
            expires_at=expires_at,
            wake_mac=wake_mac,
            platform=platform,
        )
        response: httpx.Response | None = None
        async with httpx.AsyncClient(timeout=10) as client:
            for attempt in range(3):
                response = await client.post(
                    f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
                    headers={"Authorization": f"Bearer {await self._token()}"},
                    json=payload,
                )
                if response.status_code == 401 and attempt == 0:
                    self._access_token = None
                    continue
                if response.status_code != 429 and response.status_code < 500:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.25 * (2**attempt))
        if response is None:
            raise RuntimeError("FCM relay request did not execute")
        if response.is_success:
            return FcmResult(delivered=True)
        if response.status_code in {400, 404}:
            return FcmResult(delivered=False, token_invalid=True)
        response.raise_for_status()
        return FcmResult(delivered=False)


_clients: dict[str, FcmClient] = {}


def fcm_client(settings: Settings) -> FcmClient:
    key = f"direct:{settings.domain}"
    client = _clients.get(key)
    if client is None:
        credential = settings.push_fcm_service_account_b64
        client = FcmClient(credential.get_secret_value() if credential is not None else None)
        _clients[key] = client
    return client


def relay_fcm_client(settings: Settings) -> FcmClient:
    key = f"relay:{settings.domain}"
    client = _clients.get(key)
    if client is None:
        credential = settings.push_relay_fcm_service_account_b64
        client = FcmClient(credential.get_secret_value() if credential is not None else None)
        _clients[key] = client
    return client
