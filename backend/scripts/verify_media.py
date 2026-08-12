from __future__ import annotations

import io
import logging
from collections.abc import Callable
from typing import cast
from urllib.parse import urlparse

import httpx
from fastapi.testclient import TestClient
from PIL import Image

import app.api.auth as auth_api
from app.core.settings import get_settings
from app.email.backends import OutboundEmail
from app.email.outbox import drain_email_outbox
from app.main import app
from app.media.jobs import process_attachment_record, purge_local_attachment
from scripts.email_tokens import token_from_email
from scripts.verification import VerificationFailure, failure_message, require

_png_buffer = io.BytesIO()
Image.new("RGB", (16, 16), (181, 57, 34)).save(_png_buffer, format="PNG")
PNG = _png_buffer.getvalue()
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def register(
    client: TestClient, emails: list[str], deliver_mail: Callable[[], None]
) -> dict[str, str]:
    proxy_secret = get_settings().proxy_secret
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
            "password": "correct horse battery staple",
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
        json={"identifier": "mediaowner", "password": "correct horse battery staple"},
    )
    require(login.status_code == 200, f"login failed: {login.text}")
    return cast(dict[str, str], login.json())


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

        login = register(api, emails, deliver_mail)
        headers = bearer(login["access_token"])
        created = api.post("/api/v1/guilds", headers=headers, json={"name": "Media Lab"})
        require(created.status_code == 201, f"guild create failed: {created.text}")
        guild = created.json()
        guild_id = guild["id"]
        channel_id = guild["channels"][0]["id"]

        mismatch = api.post(
            f"/api/v1/channels/{channel_id}/attachments",
            headers=headers,
            json={"filename": "wrong.png", "content_type": "image/png", "size": len(PNG) + 1},
        )
        require(mismatch.status_code == 201, f"mismatch ticket failed: {mismatch.text}")
        require(
            garage_request(
                mismatch.json()["upload_url"], "PUT", PNG, content_type="image/png"
            ).status_code
            == 403,
            "size-constrained presign accepted a mismatched PUT",
        )
        rejected = api.post(
            f"/api/v1/channels/{channel_id}/messages",
            headers=headers,
            json={"attachment_ids": [mismatch.json()["id"]]},
        )
        require(rejected.status_code == 400, "size-mismatched upload was accepted")

        ticket = api.post(
            f"/api/v1/channels/{channel_id}/attachments",
            headers=headers,
            json={"filename": "lantern.png", "content_type": "image/png", "size": len(PNG)},
        )
        require(ticket.status_code == 201, f"upload ticket failed: {ticket.text}")
        attachment_id = ticket.json()["id"]
        uploaded = garage_request(ticket.json()["upload_url"], "PUT", PNG, content_type="image/png")
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
        require(sent.status_code == 201, f"attachment-only message failed: {sent.text}")
        message_id = sent.json()["id"]
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
                ticket.json()["upload_url"],
                "PUT",
                replacement,
                content_type="image/png",
            ).status_code
            == 200,
            "staging URL was unexpectedly unavailable for the overwrite regression test",
        )
        status = api.get(f"/api/v1/attachments/{attachment_id}", headers=headers)
        require(
            status.status_code == 200
            and status.json()["scan_status"] == "clean"
            and "thumbnail_128" in status.json()["variants"],
            f"derivative processing failed: {status.text}",
        )
        redirect = api.get(
            f"/media/{get_settings().domain}/{attachment_id}/original",
            headers=headers,
            follow_redirects=False,
        )
        require(redirect.status_code == 302, f"authorized media failed: {redirect.text}")
        downloaded = garage_request(redirect.headers["location"], "GET")
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
        require(
            edited.status_code == 200
            and [item["id"] for item in edited.json()["attachments"]] == [attachment_id],
            f"message edit lost its attachment: {edited.text}",
        )
        require(
            api.put(f"/api/v1/channels/{channel_id}/pins/{message_id}", headers=headers).status_code
            == 204,
            "attachment message could not be pinned",
        )
        pins = api.get(f"/api/v1/channels/{channel_id}/pins", headers=headers)
        require(
            pins.status_code == 200
            and [item["id"] for item in pins.json()[0]["attachments"]] == [attachment_id],
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
        infected_id = infected_ticket.json()["id"]
        require(
            garage_request(
                infected_ticket.json()["upload_url"],
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
            infected_message.status_code == 201,
            "infected-file test message expected HTTP 201; received "
            f"HTTP {infected_message.status_code}: {infected_message.text}",
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
        require(webhook.status_code == 201, f"webhook create failed: {webhook.text}")
        webhook_id = webhook.json()["id"]
        token = webhook.json()["token"]
        executed = api.post(
            f"/api/v1/webhooks/{webhook_id}/{token}", json={"content": "Build complete"}
        )
        require(
            executed.status_code == 201
            and executed.json()["author"] is None
            and executed.json()["webhook"]["name"] == "Build Lantern",
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
        new_token = rotated.json()["token"]
        require(
            api.post(
                f"/api/v1/webhooks/{webhook_id}/{new_token}", json={"content": "fresh"}
            ).status_code
            == 201,
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
