from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol
from urllib.parse import unquote, urlparse

import aiosmtplib
import httpx
import structlog

from app.core.settings import Settings

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class OutboundEmail:
    to: str
    subject: str
    text: str
    html: str | None = None


class EmailBackend(Protocol):
    async def send(self, message: OutboundEmail) -> None: ...


class ConsoleEmailBackend:
    async def send(self, message: OutboundEmail) -> None:
        # This backend deliberately exposes the delivery content for local
        # development. Production settings reject it.
        log.warning(
            "email_console",
            recipient=message.to,
            subject=message.subject,
            text=message.text,
        )


class SmtpEmailBackend:
    def __init__(self, url: str, from_address: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"smtp", "smtps"} or not parsed.hostname:
            raise ValueError("KAEDE_SMTP_URL must be smtp:// or smtps://")
        self.host = parsed.hostname
        self.port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        self.username = unquote(parsed.username) if parsed.username else None
        self.password = unquote(parsed.password) if parsed.password else None
        self.use_tls = parsed.scheme == "smtps"
        self.from_address = from_address

    async def send(self, message: OutboundEmail) -> None:
        email = EmailMessage()
        email["From"] = self.from_address
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")
        await aiosmtplib.send(
            email,
            hostname=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            use_tls=self.use_tls,
            start_tls=not self.use_tls,
            timeout=15,
        )


class MailtrapApiBackend:
    def __init__(self, token: str, from_address: str) -> None:
        self.token = token
        self.from_address = from_address

    async def send(self, message: OutboundEmail) -> None:
        payload: dict[str, object] = {
            "from": {"email": self.from_address, "name": "Kaede Chat"},
            "to": [{"email": message.to}],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                "https://send.api.mailtrap.io/api/send",
                headers={"Authorization": f"Bearer {self.token}"},
                json=payload,
            )
            response.raise_for_status()


def create_email_backend(settings: Settings) -> EmailBackend:
    if settings.email_backend == "disabled":
        raise ValueError("email delivery is disabled")
    if settings.email_backend == "console":
        return ConsoleEmailBackend()
    if settings.email_backend == "smtp":
        if settings.smtp_url is None:
            raise ValueError("KAEDE_SMTP_URL is required for the smtp backend")
        return SmtpEmailBackend(settings.smtp_url.get_secret_value(), settings.email_from_address)
    if settings.mailtrap_api_token is None:
        raise ValueError("KAEDE_MAILTRAP_API_TOKEN is required for the mailtrap_api backend")
    return MailtrapApiBackend(
        settings.mailtrap_api_token.get_secret_value(), settings.email_from_address
    )
