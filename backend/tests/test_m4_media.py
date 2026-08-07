import io
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from PIL import Image

from app.api.media import select_variant
from app.api.webhooks import new_webhook_token, token_digest
from app.chat.payloads import guild_payload
from app.chat.schemas import MessageCreate
from app.core.settings import Settings
from app.db.models import Attachment, Guild
from app.media.processing import (
    MediaValidationError,
    image_derivatives,
    normalize_declared_type,
    sanitize_filename,
    sniff_content_type,
    validate_detected_type,
)
from app.media.service import clean_object_key, original_object_key
from app.media.storage import S3Storage, StorageError


def settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "domain": "alpha.localhost",
        "environment": "test",
        "secret_key": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        "database_url": "postgresql+asyncpg://kaede:kaede@postgres/kaede",
        "dragonfly_url": "redis://dragonfly:6379/0",
        "media_public_base_url": "https://media.alpha.localhost",
        "media_s3_access_key": "GK00000000000000000000000000000000",
        "media_s3_secret_key": "0" * 64,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_presigned_upload_is_deterministic_and_narrowly_scoped() -> None:
    url = S3Storage(settings()).presign(
        "PUT",
        "kaede-attachments",
        "alpha.localhost/123/original",
        expires=900,
        now=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
        content_length=123,
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.hostname == "media.alpha.localhost"
    assert parsed.path == "/kaede-attachments/alpha.localhost/123/original"
    assert query["X-Amz-Expires"] == ["900"]
    assert query["X-Amz-SignedHeaders"] == ["content-length;host"]
    assert query["X-Amz-Signature"] == [
        "7b07f2e07d78caf84f0b97231ba955888419cfb4e658ff2ec8679d04b4da703d"
    ]


def test_client_staging_and_server_clean_keys_cannot_alias() -> None:
    staging = original_object_key("alpha.localhost", 123)
    final = clean_object_key("alpha.localhost", 123, "a" * 64)
    assert staging == "alpha.localhost/123/staging/original"
    assert final == f"alpha.localhost/123/clean/{'a' * 64}/original"
    assert staging != final


def test_external_s3_presigning_uses_configured_region_and_session_token() -> None:
    configured = settings(
        media_storage_backend="s3",
        media_public_base_url="https://s3.us-west-004.backblazeb2.com",
        media_s3_endpoint="https://s3.us-west-004.backblazeb2.com",
        media_s3_region="us-west-004",
        media_s3_session_token="0" * 64,
    )
    url = S3Storage(configured).presign(
        "GET",
        "kaede-attachments",
        "alpha.localhost/123/original",
        expires=900,
        now=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    assert parsed.hostname == "s3.us-west-004.backblazeb2.com"
    assert parsed.path == "/kaede-attachments/alpha.localhost/123/original"
    assert "/us-west-004/s3/aws4_request" in query["X-Amz-Credential"][0]
    assert query["X-Amz-Security-Token"] == ["0" * 64]


def test_virtual_hosted_s3_presigning_places_bucket_in_host() -> None:
    configured = settings(
        media_public_base_url="https://s3.example.com",
        media_s3_addressing_style="virtual",
    )
    url = S3Storage(configured).presign(
        "GET",
        "kaede-attachments",
        "alpha.localhost/123/original",
        expires=60,
        now=datetime(2026, 7, 19, 12, 0, tzinfo=UTC),
    )
    parsed = urlsplit(url)
    assert parsed.hostname == "kaede-attachments.s3.example.com"
    assert parsed.path == "/alpha.localhost/123/original"


async def test_external_bucket_verification_never_creates_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(404, request=request)

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    storage = S3Storage(settings(media_storage_backend="s3", media_s3_region="us-east-1"))
    with pytest.raises(StorageError, match="does not exist"):
        await storage.ensure_bucket("kaede-attachments", create_if_missing=False)
    assert [request.method for request in requests] == ["HEAD"]


async def test_external_bucket_creation_uses_region_and_temporary_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "HEAD":
            return httpx.Response(404, request=request)
        assert await request.aread() == (
            b'<CreateBucketConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            b"<LocationConstraint>us-west-004</LocationConstraint>"
            b"</CreateBucketConfiguration>"
        )
        return httpx.Response(200, request=request)

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    storage = S3Storage(
        settings(
            media_storage_backend="s3",
            media_s3_region="us-west-004",
            media_s3_session_token="temporary-session-token-1234567890",
        )
    )
    await storage.ensure_bucket("kaede-attachments", create_if_missing=True)
    assert [request.method for request in requests] == ["HEAD", "PUT"]
    assert all(
        request.headers["X-Amz-Security-Token"] == "temporary-session-token-1234567890"
        for request in requests
    )


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xffrest", "image/jpeg"),
        (b"GIF89arest", "image/gif"),
        (b"%PDF-1.7", "application/pdf"),
        (b"plain text", "text/plain"),
        (b"MZ" + b"\0" * 30, "application/x-executable"),
        (b"<svg onload='x'>", "text/html"),
    ],
)
def test_magic_byte_sniffing(body: bytes, expected: str) -> None:
    assert sniff_content_type(body) == expected


def test_declared_mime_cannot_smuggle_active_content() -> None:
    assert normalize_declared_type("image/png; charset=binary") == "image/png"
    with pytest.raises(MediaValidationError):
        validate_detected_type("image/png", "text/html")
    with pytest.raises(MediaValidationError):
        normalize_declared_type("image/svg+xml")


def test_filename_is_reduced_to_a_safe_display_name() -> None:
    assert sanitize_filename("../../paper<script>.png") == "paper_script_.png"
    assert sanitize_filename("... ") == "upload"


def test_message_can_be_attachment_only_but_not_empty() -> None:
    assert MessageCreate(content=None, attachment_ids=["1"]).content is None
    with pytest.raises(ValueError):
        MessageCreate(content=None)
    with pytest.raises(ValueError):
        MessageCreate(content="hello", attachment_ids=["1", "1"])


def test_guild_payload_includes_both_public_image_hashes() -> None:
    guild = Guild(
        id=10,
        origin_domain="alpha.localhost",
        name="Paper Lantern",
        icon_hash="icon-digest",
        banner_hash="banner-digest",
        owner_id=20,
        owner_domain="alpha.localhost",
        permission_generation=1,
        unavailable=False,
    )

    payload = guild_payload(guild)

    assert payload["icon_hash"] == "icon-digest"
    assert payload["banner_hash"] == "banner-digest"


def test_missing_image_derivative_falls_back_to_scanned_original() -> None:
    attachment = Attachment(
        id=30,
        origin_domain="alpha.localhost",
        uploader_id=20,
        uploader_domain="alpha.localhost",
        filename="legacy.gif",
        content_type="image/gif",
        detected_content_type="image/gif",
        size=128,
        object_key="alpha.localhost/30/clean/original",
        variants={},
        scan_status="clean",
        purpose="attachment",
    )

    bucket, key, filename = select_variant(settings(), attachment, "thumbnail_512")

    assert bucket == "kaede-attachments"
    assert key == "alpha.localhost/30/clean/original"
    assert filename == "legacy.gif"


def test_webhook_tokens_are_prefixed_random_and_only_stored_as_digests() -> None:
    first = new_webhook_token()
    second = new_webhook_token()
    assert first.startswith("kwh_")
    assert first != second
    assert len(token_digest(first)) == 32
    assert first.encode() not in token_digest(first)


def test_image_pipeline_emits_safe_metadata_and_preserves_animation() -> None:
    source = io.BytesIO()
    frames = [Image.new("RGB", (16, 12), color) for color in ("red", "blue")]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200],
        loop=0,
    )
    derivatives, blurhash, perceptual_hash, width, height = image_derivatives(source.getvalue())
    assert [item.name for item in derivatives] == [
        "thumbnail_128",
        "thumbnail_512",
        "thumbnail_1024",
    ]
    assert (width, height) == (16, 12)
    assert blurhash
    assert len(perceptual_hash) == 16
    for derivative in derivatives:
        with Image.open(io.BytesIO(derivative.content)) as rendered:
            assert rendered.format == "WEBP"
            assert rendered.n_frames == 2
