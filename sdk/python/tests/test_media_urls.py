from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import kaede_bot.client as client_module
from kaede_bot import Attachment, Client, EntityRef, WorkerState
from kaede_bot.errors import ApiError
from kaede_bot.media_urls import (
    MediaURLValidationError,
    media_url_origin,
    resolve_target_media_location,
    validate_authority_media_url,
    validate_signed_media_url,
    validate_target_media_url,
)


TARGET = "https://chat.example"


def client() -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        )
    )


def attachment(*, upload_url: str, media_origin: str | None = None) -> Attachment:
    return Attachment(
        client(),
        TARGET,
        EntityRef(41, "chat.example"),
        "report.pdf",
        "application/pdf",
        4,
        "pending",
        upload_url=upload_url,
        media_origin=media_origin,
    )


def test_media_capabilities_are_bound_to_the_exact_target_authority() -> None:
    capability = "https://media.chat.example/object?X-Amz-Signature=secret"
    assert validate_target_media_url(capability, TARGET) == capability
    assert (
        resolve_target_media_location(
            "//media.chat.example/other?signature=secret",
            target_origin=TARGET,
        )
        == "https://media.chat.example/other?signature=secret"
    )

    external = "https://kaede-attachments.s3.example.com/object?signature=secret"
    assert (
        validate_signed_media_url(
            external,
            "https://kaede-attachments.s3.example.com",
        )
        == external
    )
    with pytest.raises(MediaURLValidationError):
        validate_signed_media_url(external, "https://s3.example.com")

    ipv6 = "https://[2001:db8::1]/object?signature=secret"
    assert validate_signed_media_url(ipv6, "https://[2001:db8::1]") == ipv6
    assert media_url_origin("https://[2001:db8::1]:443/object") == (
        "https://[2001:db8::1]"
    )
    with pytest.raises(MediaURLValidationError):
        validate_signed_media_url(ipv6, "https://[2001:db8::1]:443")

    development = "https://media.alpha.localhost:18443/object?signature=secret"
    assert (
        validate_target_media_url(
            development,
            "https://alpha.localhost:18443",
        )
        == development
    )


@pytest.mark.parametrize(
    "location",
    [
        "http://media.chat.example/object",
        "https://objects.example/object",
        "https://127.0.0.1/object",
        "https://media.chat.example.attacker.test/object",
        "https://media.chat.example:8443/object",
        "https://user@media.chat.example/object",
        "https://media.chat.example/object#secret",
        "https://media.chat.example:invalid/object",
    ],
)
def test_media_capabilities_reject_unsafe_hosts_and_url_features(location: str) -> None:
    with pytest.raises(MediaURLValidationError):
        validate_authority_media_url(location, "chat.example")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "upload_url",
    [
        "https://objects.example/staged/41",
        "https://127.0.0.1/staged/41",
        "https://media.chat.example.attacker.test/staged/41",
    ],
)
async def test_upload_ticket_rejects_an_unrelated_https_host_before_sending_bytes(
    monkeypatch: pytest.MonkeyPatch,
    upload_url: str,
) -> None:
    bot = client()

    def unexpected_client(**_: Any) -> object:
        raise AssertionError("an HTTP client must not be opened for an unsafe URL")

    monkeypatch.setattr(client_module.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ApiError) as raised:
        await bot._put_upload_ticket(
            attachment(upload_url=upload_url),
            b"data",
            content_type="application/pdf",
        )
    assert raised.value.code == "UPLOAD_TICKET_INVALID"
    assert "media host" in str(raised.value)


@pytest.mark.asyncio
async def test_upload_redirect_location_is_revalidated_against_the_same_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()

    class RedirectResponse:
        is_redirect = True
        headers = {"Location": "https://attacker.example/capture"}

        def raise_for_status(self) -> None:
            return None

    class UploadClient:
        async def __aenter__(self) -> UploadClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def put(self, *_: object, **__: object) -> RedirectResponse:
            return RedirectResponse()

    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **_: UploadClient(),
    )
    with pytest.raises(ApiError) as raised:
        await bot._put_upload_ticket(
            attachment(upload_url="https://media.chat.example/staged/41"),
            b"data",
            content_type="application/pdf",
        )
    assert raised.value.code == "UPLOAD_REDIRECT_REJECTED"
    assert "outside" in str(raised.value)


@pytest.mark.asyncio
async def test_external_s3_upload_uses_exact_ticket_origin_without_bot_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    requests: list[tuple[str, bytes, dict[str, str]]] = []

    class UploadResponse:
        is_redirect = False
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

    class UploadClient:
        async def __aenter__(self) -> UploadClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def put(
            self,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> UploadResponse:
            requests.append((url, content, headers))
            return UploadResponse()

    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **_: UploadClient())
    location = "https://kaede-attachments.s3.example.com/staged/41?signature=opaque"
    await bot._put_upload_ticket(
        attachment(
            upload_url=location,
            media_origin="https://kaede-attachments.s3.example.com",
        ),
        b"data",
        content_type="application/pdf",
    )

    assert requests == [
        (
            location,
            b"data",
            {"Content-Type": "application/pdf", "Content-Length": "4"},
        )
    ]
    assert "Authorization" not in requests[0][2]
    assert "DPoP" not in requests[0][2]


@pytest.mark.asyncio
async def test_external_s3_upload_origin_mismatch_is_rejected_before_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()

    def unexpected_client(**_: Any) -> object:
        raise AssertionError("an HTTP client must not be opened for an unsafe URL")

    monkeypatch.setattr(client_module.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ApiError) as raised:
        await bot._put_upload_ticket(
            attachment(
                upload_url="https://kaede-attachments.s3.example.com/staged/41",
                media_origin="https://s3.example.com",
            ),
            b"data",
            content_type="application/pdf",
        )
    assert raised.value.code == "UPLOAD_TICKET_INVALID"


@pytest.mark.asyncio
async def test_external_s3_upload_status_does_not_disclose_presigned_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    location = (
        "https://kaede-attachments.s3.example.com/staged/41"
        "?X-Amz-Signature=upload-secret"
    )

    class UploadClient:
        async def __aenter__(self) -> UploadClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def put(self, *_: object, **__: object) -> httpx.Response:
            request = httpx.Request("PUT", location)
            return httpx.Response(403, request=request)

    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **_: UploadClient())
    with pytest.raises(ApiError) as raised:
        await bot._put_upload_ticket(
            attachment(
                upload_url=location,
                media_origin="https://kaede-attachments.s3.example.com",
            ),
            b"data",
            content_type="application/pdf",
        )

    assert raised.value.code == "MEDIA_UPLOAD_FAILED"
    assert raised.value.detail == {"upstream_status": 403}
    assert "upload-secret" not in str(raised.value)
    assert "upload-secret" not in repr(raised.value.detail)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_location",
    [
        "https://attacker.example/object",
        "https://127.0.0.1/object",
        "https://media.chat.example.attacker.test/object",
    ],
)
async def test_api_attachment_redirect_is_bound_before_the_media_get(
    redirect_location: str,
) -> None:
    bot = client()

    class RedirectResponse:
        status_code = 307
        is_redirect = True
        headers = {"Location": redirect_location}

    class ApiTarget:
        async def get(self, *_: object, **__: object) -> RedirectResponse:
            return RedirectResponse()

    bot._targets[TARGET] = ApiTarget()  # type: ignore[assignment]
    bot._token = AsyncMock(return_value="token")  # type: ignore[method-assign]
    with pytest.raises(ApiError) as raised:
        await bot._redirect_location(
            "/api/v1/bots/attachments/41/original", target=TARGET
        )
    assert raised.value.code == "MEDIA_REDIRECT_INVALID"
    assert "media host" in str(raised.value)


@pytest.mark.asyncio
async def test_api_attachment_redirect_accepts_exact_attested_external_s3_origin() -> (
    None
):
    bot = client()
    location = "https://kaede-attachments.s3.example.com/object?signature=opaque"

    class RedirectResponse:
        status_code = 302
        is_redirect = True
        headers = {
            "Location": location,
            "X-Kaede-Media-Origin": "https://kaede-attachments.s3.example.com",
        }

    class ApiTarget:
        async def get(self, *_: object, **__: object) -> RedirectResponse:
            return RedirectResponse()

    bot._targets[TARGET] = ApiTarget()  # type: ignore[assignment]
    bot._token = AsyncMock(return_value="token")  # type: ignore[method-assign]

    assert await bot._redirect_location(
        "/api/v1/bots/attachments/41/original",
        target=TARGET,
    ) == (location, "https://kaede-attachments.s3.example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_location",
    [
        "https://attacker.example/object",
        "https://127.0.0.1/object",
        "https://media.chat.example.attacker.test/object",
    ],
)
async def test_follow_on_media_redirect_is_revalidated_against_the_same_authority(
    monkeypatch: pytest.MonkeyPatch,
    redirect_location: str,
) -> None:
    bot = client()
    bot._redirect_location = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "https://media.chat.example/object",
            "https://media.chat.example",
        )
    )

    class RedirectResponse:
        is_redirect = True
        headers = {"Location": redirect_location}

    class StreamContext:
        async def __aenter__(self) -> RedirectResponse:
            return RedirectResponse()

        async def __aexit__(self, *_: object) -> None:
            return None

    class MediaClient:
        async def __aenter__(self) -> MediaClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def stream(self, *_: object, **__: object) -> StreamContext:
            return StreamContext()

    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **_: MediaClient(),
    )
    with pytest.raises(ApiError) as raised:
        await bot.download_attachment(
            EntityRef(41, "chat.example"),
            target=TARGET,
        )
    assert raised.value.code == "MEDIA_REDIRECT_INVALID"
    assert "outside" in str(raised.value)


@pytest.mark.asyncio
async def test_download_revalidates_a_resolved_url_before_opening_the_media_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    bot._redirect_location = AsyncMock(  # type: ignore[method-assign]
        return_value=(
            "https://attacker.example/object",
            "https://media.chat.example",
        )
    )

    def unexpected_client(**_: Any) -> object:
        raise AssertionError("an HTTP client must not be opened for an unsafe URL")

    monkeypatch.setattr(client_module.httpx, "AsyncClient", unexpected_client)
    with pytest.raises(ApiError) as raised:
        await bot.download_attachment(
            EntityRef(41, "chat.example"),
            target=TARGET,
        )
    assert raised.value.code == "MEDIA_REDIRECT_INVALID"
    assert "media host" in str(raised.value)


@pytest.mark.asyncio
async def test_external_s3_download_status_does_not_disclose_presigned_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = client()
    location = (
        "https://kaede-attachments.s3.example.com/object"
        "?X-Amz-Signature=download-secret"
    )
    bot._redirect_location = AsyncMock(  # type: ignore[method-assign]
        return_value=(location, "https://kaede-attachments.s3.example.com")
    )
    response = httpx.Response(403, request=httpx.Request("GET", location))

    class StreamContext:
        async def __aenter__(self) -> httpx.Response:
            return response

        async def __aexit__(self, *_: object) -> None:
            return None

    class MediaClient:
        async def __aenter__(self) -> MediaClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def stream(self, *_: object, **__: object) -> StreamContext:
            return StreamContext()

    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **_: MediaClient())
    with pytest.raises(ApiError) as raised:
        await bot.download_attachment(EntityRef(41, "chat.example"), target=TARGET)

    assert raised.value.code == "MEDIA_DOWNLOAD_FAILED"
    assert raised.value.detail == {"upstream_status": 403}
    assert "download-secret" not in str(raised.value)
    assert "download-secret" not in repr(raised.value.detail)
