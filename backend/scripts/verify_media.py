from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient
from PIL import Image

import app.api.auth as auth_api
from app.core.settings import get_settings
from app.core.types import validate_wire_snowflake
from app.email.backends import OutboundEmail
from app.email.outbox import drain_email_outbox
from app.main import app
from app.media.jobs import process_attachment_record, purge_local_attachment
from scripts.email_tokens import token_from_email
from scripts.verification import (
    PASSWORD_KDF_VERSION,
    VerificationFailure,
    authentication_secret,
    failure_message,
    password_kdf_metadata,
    require,
)

PASSWORD = "correct horse battery staple"  # noqa: S105 - disposable validation credential
AUTH_SALT = bytes(range(16))
VAULT_SALT = bytes(reversed(range(16)))

_png_buffer = io.BytesIO()
Image.new("RGB", (16, 16), (181, 57, 34)).save(_png_buffer, format="PNG")
PNG = _png_buffer.getvalue()
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


class JsonResponse(Protocol):
    def json(self) -> object: ...


def response_json(response: JsonResponse, label: str) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise VerificationFailure(f"{label} response was not valid JSON") from exc


def response_object(response: JsonResponse, label: str) -> dict[str, object]:
    value = response_json(response, label)
    if not isinstance(value, dict):
        raise VerificationFailure(f"{label} response was not a JSON object")
    return cast(dict[str, object], value)


def response_array(response: JsonResponse, label: str) -> list[object]:
    value = response_json(response, label)
    if not isinstance(value, list):
        raise VerificationFailure(f"{label} response was not a JSON array")
    return cast(list[object], value)


def required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationFailure(f"{label} was not a non-empty string")
    return value


def wire_snowflake(value: object, label: str) -> str:
    try:
        validate_wire_snowflake(value)
    except ValueError as exc:
        raise VerificationFailure(f"{label} was not a canonical decimal-string snowflake") from exc
    return cast(str, value)


def attachment_ids(payload: dict[str, object], label: str) -> list[str]:
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        raise VerificationFailure(f"{label} attachments were not a JSON array")
    values: list[str] = []
    for index, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            raise VerificationFailure(f"{label} attachment {index} was not a JSON object")
        values.append(wire_snowflake(attachment.get("id"), f"{label} attachment {index} ID"))
    return values


def register(client: TestClient, emails: list[str], deliver_mail: Callable[[], None]) -> str:
    settings = get_settings()
    proxy_secret = settings.proxy_secret
    if proxy_secret is None:
        raise VerificationFailure(
            "KAEDE_PROXY_SECRET is not configured for the validation environment"
        )
    response = client.post(
        "/api/v1/auth/register",
        headers={
            "X-Forwarded-For": "192.0.2.44",
            "X-Kaede-Proxy-Secret": proxy_secret.get_secret_value(),
        },
        json={
            "username": "mediaowner",
            "email": "mediaowner@example.com",
            "password": authentication_secret(PASSWORD, settings.domain, AUTH_SALT),
            "password_kdf": password_kdf_metadata(
                AUTH_SALT,
                vault_salt=VAULT_SALT,
            ),
        },
    )
    require(response.status_code == 201, f"registration failed: {response.text}")
    deliver_mail()
    verified = client.post(
        "/api/v1/auth/verify-email", json={"token": token_from_email(emails.pop())}
    )
    require(verified.status_code == 200, f"verification failed: {verified.text}")
    login = client.post(
        "/api/v1/auth/login",
        headers={"X-Kaede-Client": "mobile"},
        json={
            "identifier": "mediaowner",
            "password": authentication_secret(PASSWORD, settings.domain, AUTH_SALT),
            "password_kdf_version": PASSWORD_KDF_VERSION,
        },
    )
    require(login.status_code == 200, f"login failed: {login.text}")
    return required_string(
        response_object(login, "login").get("access_token"), "login access token"
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Kaede-Client": "mobile"}


def garage_request(
    url: str,
    method: str,
    body: bytes = b"",
    *,
    content_type: str | None = None,
) -> httpx.Response:
    parsed = urlparse(url)
    internal = f"http://garage:3900{parsed.path}"
    if parsed.query:
        internal += f"?{parsed.query}"
    headers = {"Host": parsed.netloc}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return httpx.request(
        method,
        internal,
        content=body,
        headers=headers,
        timeout=20,
        follow_redirects=False,
        trust_env=False,
    )


def verify() -> None:
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    emails: list[str] = []

    class CaptureEmailBackend:
        async def send(self, message: OutboundEmail) -> None:
            emails.append(message.text)

    async def suppress_immediate_wake() -> None:
        return None

    auth_api.wake_email_outbox = suppress_immediate_wake
    with TestClient(app) as api:

        async def drain_mail() -> None:
            await drain_email_outbox(
                app.state.sessionmaker,
                get_settings(),
                backend=CaptureEmailBackend(),
            )

        def deliver_mail() -> None:
            if api.portal is None:
                raise VerificationFailure(
                    "FastAPI's test portal is unavailable; verify the application lifespan started"
                )
            api.portal.call(drain_mail)

        headers = bearer(register(api, emails, deliver_mail))
        created = api.post("/api/v1/guilds", headers=headers, json={"name": "Media Lab"})
        require(created.status_code == 201, f"guild create failed: {created.text}")
        guild = response_object(created, "guild create")
        guild_id = wire_snowflake(guild.get("id"), "guild ID")
        channels = guild.get("channels")
        if not isinstance(channels, list) or not channels or not isinstance(channels[0], dict):
            raise VerificationFailure("guild create response did not include a channel object")
        channel_id = wire_snowflake(channels[0].get("id"), "guild channel ID")

        mismatch = api.post(
            f"/api/v1/channels/{channel_id}/attachments",
            headers=headers,
            json={"filename": "wrong.png", "content_type": "image/png", "size": len(PNG) + 1},
        )
        require(mismatch.status_code == 201, f"mismatch ticket failed: {mismatch.text}")
        mismatch_payload = response_object(mismatch, "mismatch attachment ticket")
        mismatch_id = wire_snowflake(mismatch_payload.get("id"), "mismatch attachment ID")
        mismatch_upload_url = required_string(
            mismatch_payload.get("upload_url"), "mismatch attachment upload URL"
        )
        require(
            garage_request(mismatch_upload_url, "PUT", PNG, content_type="image/png").status_code
            == 403,
            "size-constrained presign accepted a mismatched PUT",
        )
        rejected = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=headers,
            json={"attachment_ids": [mismatch_id]},
        )
        require(rejected.status_code == 400, "size-mismatched upload was accepted")

        ticket = api.post(
            f"/api/v1/channels/{channel_id}/attachments",
            headers=headers,
            json={"filename": "lantern.png", "content_type": "image/png", "size": len(PNG)},
        )
        require(ticket.status_code == 201, f"upload ticket failed: {ticket.text}")
        ticket_payload = response_object(ticket, "attachment ticket")
        attachment_id = wire_snowflake(ticket_payload.get("id"), "attachment ID")
        upload_url = required_string(ticket_payload.get("upload_url"), "attachment upload URL")
        uploaded = garage_request(upload_url, "PUT", PNG, content_type="image/png")
        require(
            uploaded.status_code == 200,
            "object-storage upload expected HTTP 200; received "
            f"HTTP {uploaded.status_code}: {uploaded.text}",
        )
        sent = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=headers,
            json={"attachment_ids": [attachment_id]},
        )
        require(sent.status_code == 200, f"attachment-only message failed: {sent.text}")
        sent_payload = response_object(sent, "attachment-only message")
        message_id = wire_snowflake(sent_payload.get("id"), "message ID")
        require(
            attachment_ids(sent_payload, "attachment-only message") == [attachment_id],
            "attachment-only message response lost its attachment",
        )
        unavailable = api.get(
            f"/media/{get_settings().domain}/{attachment_id}/original",
            headers=headers,
            follow_redirects=False,
        )
        require(unavailable.status_code == 404, "unscanned original became downloadable")

        async def process() -> str:
            async with app.state.sessionmaker() as session:
                return await process_attachment_record(
                    session, get_settings(), int(attachment_id), get_settings().domain
                )

        if api.portal is None:
            raise VerificationFailure(
                "FastAPI's test portal is unavailable; verify the application lifespan started"
            )
        process_result = api.portal.call(process)
        require(process_result == "clean", f"clean image processing failed: {process_result}")
        replacement = b"MZ" + b"\0" * (len(PNG) - 2)
        require(
            garage_request(
                upload_url,
                "PUT",
                replacement,
                content_type="image/png",
            ).status_code
            == 200,
            "staging URL was unexpectedly unavailable for the overwrite regression test",
        )
        status = api.get(f"/api/v1/attachments/{attachment_id}", headers=headers)
        require(status.status_code == 200, f"attachment status failed: {status.text}")
        status_payload = response_object(status, "attachment status")
        status_id = wire_snowflake(status_payload.get("id"), "attachment status ID")
        variants = status_payload.get("variants")
        require(
            status_id == attachment_id
            and status_payload.get("scan_status") == "clean"
            and isinstance(variants, dict)
            and "thumbnail_128" in variants,
            f"derivative processing failed: {status.text}",
        )
        redirect = api.get(
            f"/media/{get_settings().domain}/{attachment_id}/original",
            headers=headers,
            follow_redirects=False,
        )
        require(redirect.status_code == 302, f"authorized media failed: {redirect.text}")
        downloaded = garage_request(
            required_string(redirect.headers.get("location"), "media redirect location"),
            "GET",
        )
        require(
            downloaded.status_code == 200 and downloaded.content == PNG,
            "downloaded original expected HTTP 200 and "
            f"{len(PNG)} bytes; received HTTP {downloaded.status_code} and "
            f"{len(downloaded.content)} bytes",
        )

        edited = api.patch(
            f"/api/v1/channels/{channel_id}/messages/{message_id}",
            headers=headers,
            json={"content": "The scanned lantern is ready."},
        )
        require(edited.status_code == 200, f"message edit failed: {edited.text}")
        edited_payload = response_object(edited, "message edit")
        require(
            wire_snowflake(edited_payload.get("id"), "edited message ID") == message_id
            and attachment_ids(edited_payload, "edited message") == [attachment_id],
            f"message edit lost its attachment: {edited.text}",
        )
        require(
            api.put(f"/api/v1/channels/{channel_id}/pins/{message_id}", headers=headers).status_code
            == 204,
            "attachment message could not be pinned",
        )
        pins = api.get(f"/api/v1/channels/{channel_id}/pins", headers=headers)
        require(pins.status_code == 200, f"pin listing failed: {pins.text}")
        pin_payloads = response_array(pins, "pin listing")
        if len(pin_payloads) != 1 or not isinstance(pin_payloads[0], dict):
            raise VerificationFailure("pin listing did not contain exactly one message object")
        pinned_message = cast(dict[str, object], pin_payloads[0])
        require(
            wire_snowflake(pinned_message.get("id"), "pinned message ID") == message_id
            and attachment_ids(pinned_message, "pinned message") == [attachment_id],
            f"pin listing lost the message attachment: {pins.text}",
        )
        require(
            api.delete(
                f"/api/v1/channels/{channel_id}/pins/{message_id}", headers=headers
            ).status_code
            == 204,
            "attachment message could not be unpinned",
        )

        infected_ticket = api.post(
            f"/api/v1/channels/{channel_id}/attachments",
            headers=headers,
            json={"filename": "eicar.txt", "content_type": "text/plain", "size": len(EICAR)},
        )
        require(
            infected_ticket.status_code == 201,
            "infected-file test ticket expected HTTP 201; received "
            f"HTTP {infected_ticket.status_code}: {infected_ticket.text}",
        )
        infected_ticket_payload = response_object(infected_ticket, "infected attachment ticket")
        infected_id = wire_snowflake(infected_ticket_payload.get("id"), "infected attachment ID")
        infected_upload_url = required_string(
            infected_ticket_payload.get("upload_url"), "infected attachment upload URL"
        )
        require(
            garage_request(
                infected_upload_url,
                "PUT",
                EICAR,
                content_type="text/plain",
            ).status_code
            == 200,
            "infected test upload failed",
        )
        infected_message = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=headers,
            json={"attachment_ids": [infected_id]},
        )
        require(
            infected_message.status_code == 200,
            "infected-file test message expected HTTP 200; received "
            f"HTTP {infected_message.status_code}: {infected_message.text}",
        )
        infected_message_payload = response_object(infected_message, "infected-file test message")
        wire_snowflake(infected_message_payload.get("id"), "infected-file message ID")
        require(
            attachment_ids(infected_message_payload, "infected-file message") == [infected_id],
            "infected-file test message response lost its attachment",
        )

        async def scan_infected() -> str:
            async with app.state.sessionmaker() as session:
                return await process_attachment_record(
                    session, get_settings(), int(infected_id), get_settings().domain
                )

        require(api.portal.call(scan_infected) == "infected", "ClamAV did not reject EICAR")

        webhook = api.post(
            f"/api/v1/guilds/{guild_id}/channels/{channel_id}/webhooks",
            headers=headers,
            json={"name": "Build Lantern"},
        )
        require(webhook.status_code == 200, f"webhook create failed: {webhook.text}")
        webhook_payload = response_object(webhook, "webhook create")
        webhook_id = wire_snowflake(webhook_payload.get("id"), "webhook ID")
        token = required_string(webhook_payload.get("token"), "webhook token")
        executed = api.post(
            f"/api/v1/webhooks/{webhook_id}/{token}?wait=true",
            json={"content": "Build complete"},
        )
        require(executed.status_code == 200, f"webhook execution failed: {executed.text}")
        executed_payload = response_object(executed, "webhook execution")
        wire_snowflake(executed_payload.get("id"), "webhook message ID")
        executed_webhook = executed_payload.get("webhook")
        require(
            executed_payload.get("author") is None
            and isinstance(executed_webhook, dict)
            and executed_webhook.get("name") == "Build Lantern",
            f"webhook attribution failed: {executed.text}",
        )
        rotated = api.post(f"/api/v1/webhooks/{webhook_id}/rotate", headers=headers)
        require(
            rotated.status_code == 200,
            f"webhook rotation expected HTTP 200; received HTTP {rotated.status_code}",
        )
        require(
            api.post(
                f"/api/v1/webhooks/{webhook_id}/{token}", json={"content": "stale"}
            ).status_code
            == 404,
            "rotated webhook token remained valid",
        )
        new_token = required_string(
            response_object(rotated, "webhook rotation").get("token"),
            "rotated webhook token",
        )
        require(
            api.post(
                f"/api/v1/webhooks/{webhook_id}/{new_token}", json={"content": "fresh"}
            ).status_code
            == 204,
            "rotated webhook token failed",
        )
        require(
            api.delete(f"/api/v1/webhooks/{webhook_id}", headers=headers).status_code == 204,
            "webhook revocation failed",
        )

        deleted = api.delete(
            f"/api/v1/channels/{channel_id}/messages/{message_id}", headers=headers
        )
        require(
            deleted.status_code == 204,
            "message deletion expected HTTP 204; received "
            f"HTTP {deleted.status_code}: {deleted.text}",
        )

        async def purge() -> str:
            async with app.state.sessionmaker() as session:
                return await purge_local_attachment(
                    session, get_settings(), int(attachment_id), get_settings().domain
                )

        require(api.portal.call(purge) == "deleted", "attachment object purge failed")
        require(
            api.get(
                f"/media/{get_settings().domain}/{attachment_id}/original",
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 404,
            "deleted attachment remained available",
        )
    print("media and webhook verification passed")


if __name__ == "__main__":
    try:
        verify()
    except VerificationFailure as error:
        raise SystemExit(failure_message("media", error, "make media-check")) from None
