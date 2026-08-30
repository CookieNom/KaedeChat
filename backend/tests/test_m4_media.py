import io
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import pyvips  # type: ignore[import-untyped]
from fastapi import HTTPException, Response
from PIL import Image

from app.api import media as media_api
from app.api.channels import require_attachment_upload_channel
from app.api.media import select_variant
from app.api.webhooks import new_webhook_token, token_digest
from app.chat.payloads import guild_payload
from app.chat.schemas import MessageCreate
from app.core.settings import Settings
from app.core.types import EntityRef
from app.db.models import Attachment, Channel, Guild, Role, User
from app.media.jobs import image_derivatives_are_current
from app.media.processing import (
    IMAGE_PIPELINE_VERSION,
    MediaValidationError,
    image_derivative_sizes,
    image_derivatives,
    normalize_declared_type,
    sanitize_filename,
    sniff_content_type,
    validate_detected_type,
)
from app.media.service import clean_object_key, media_redirect_response, original_object_key
from app.media.storage import (
    S3Storage,
    StorageError,
    media_url_origin,
    validate_media_url_origin,
)


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
    assert media_url_origin(url) == "https://kaede-attachments.s3.example.com"


def test_media_capability_origin_is_exact_and_exposed_on_redirects() -> None:
    location = "https://objects.example:8443/object?signature=opaque"
    origin = "https://objects.example:8443"
    validate_media_url_origin(location, origin)
    with pytest.raises(ValueError, match="escaped"):
        validate_media_url_origin(location, "https://objects.example")

    response = media_redirect_response(location)
    assert response.headers["location"] == location
    assert response.headers["X-Kaede-Media-Origin"] == origin


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


async def test_remote_channel_ticket_stays_on_the_uploader_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        id=20,
        origin_domain="alpha.localhost",
        is_local=True,
        username="owner",
        password_hash="unused",
    )
    channel = Channel(
        id=30,
        origin_domain="guild.example",
        guild_id=40,
        guild_domain="guild.example",
        type=0,
        name="general",
        created_floor_id=1,
    )
    access = SimpleNamespace(channel=channel, guild=SimpleNamespace())
    issue = AsyncMock(return_value={"id": "50", "origin_domain": "alpha.localhost"})
    monkeypatch.setattr(media_api, "enforce_client_rate_limit", AsyncMock())
    monkeypatch.setattr(
        media_api,
        "require_channel_attachment_access",
        AsyncMock(return_value=access),
    )
    monkeypatch.setattr(media_api, "issue_channel_attachment_ticket", issue)
    signed = AsyncMock(side_effect=AssertionError("channel tickets must not move authorities"))
    monkeypatch.setattr(media_api, "signed_request", signed)

    rendered = await media_api.create_channel_attachment_ticket(
        EntityRef("30@guild.example"),
        media_api.UploadTicketRequest(
            filename="photo.png",
            content_type="image/png",
            size=128,
        ),
        Response(),
        SimpleNamespace(user=user),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(domain="alpha.localhost"),
    )

    assert rendered == {"id": "50", "origin_domain": "alpha.localhost"}
    issue.assert_awaited_once()
    assert issue.await_args.args[3] is user
    assert issue.await_args.args[4] is access
    signed.assert_not_awaited()


def test_channel_ticket_provenance_cannot_be_rebound() -> None:
    channel = Channel(
        id=30,
        origin_domain="guild.example",
        guild_id=40,
        guild_domain="guild.example",
        type=0,
        name="general",
        created_floor_id=1,
    )
    attachment = Attachment(
        id=50,
        origin_domain="alpha.localhost",
        uploader_id=20,
        uploader_domain="alpha.localhost",
        filename="photo.png",
        content_type="image/png",
        size=128,
        object_key="alpha.localhost/50/staging/original",
        purpose="attachment",
        upload_channel_id=31,
        upload_channel_domain="guild.example",
    )

    with pytest.raises(HTTPException) as caught:
        require_attachment_upload_channel(attachment, channel)

    assert caught.value.status_code == 404
    assert caught.value.detail == {"code": "ATTACHMENT_NOT_FOUND"}

    attachment.upload_channel_id = channel.id
    require_attachment_upload_channel(attachment, channel)

    thread = Channel(
        id=32,
        origin_domain="guild.example",
        guild_id=40,
        guild_domain="guild.example",
        type=11,
        name="forum post",
        parent_id=channel.id,
        parent_domain=channel.origin_domain,
        created_floor_id=1,
    )
    require_attachment_upload_channel(attachment, thread, allow_parent=True)
    with pytest.raises(HTTPException):
        require_attachment_upload_channel(attachment, thread)

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


def test_guild_payload_exposes_safe_replica_health_without_internal_detail() -> None:
    guild = Guild(
        id=10,
        origin_domain="remote.example",
        name="Remote guild",
        owner_id=20,
        owner_domain="remote.example",
        permission_generation=1,
        unavailable=False,
        sync_status="quota_paused",
        sync_error_code="KAED_FED_REPLICA_QUOTA_EXCEEDED",
        sync_error="internal database sizing detail",
    )

    payload = guild_payload(guild)

    assert payload["sync_status"] == "quota_paused"
    assert payload["sync_error_code"] == "KAED_FED_REPLICA_QUOTA_EXCEEDED"
    assert "sync_error" not in payload


@pytest.mark.parametrize(
    ("kind", "purpose", "field"),
    (("icon", "guild_icon", "icon_hash"), ("banner", "guild_banner", "banner_hash")),
)
async def test_guild_asset_commit_refreshes_server_version_before_render(
    monkeypatch: pytest.MonkeyPatch, kind: str, purpose: str, field: str
) -> None:
    digest = "a" * 64
    user = User(
        id=20,
        origin_domain="alpha.localhost",
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain="alpha.localhost",
        name="Paper Lantern",
        owner_id=user.id,
        owner_domain=user.origin_domain,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    attachment = Attachment(
        id=30,
        origin_domain="alpha.localhost",
        uploader_id=user.id,
        uploader_domain=user.origin_domain,
        filename="icon.png",
        content_type="image/png",
        detected_content_type="image/png",
        size=128,
        object_key="alpha.localhost/30/clean/original",
        content_sha256=digest,
        variants={},
        scan_status="clean",
        purpose=purpose,
    )
    calls: list[str] = []

    class Session:
        async def flush(self) -> None:
            calls.append("flush")

        async def refresh(self, value: object) -> None:
            assert value is guild
            calls.append("refresh")
            guild.updated_at = datetime(2026, 8, 11, tzinfo=UTC)

        async def commit(self) -> None:
            calls.append("commit")

    async def no_op(*args: object, **kwargs: object) -> None:
        return None

    async def local_guild(*args: object, **kwargs: object) -> Guild:
        return guild

    async def finalize_attachment(*args: object, **kwargs: object) -> Attachment:
        return attachment

    async def bind_asset(*args: object, **kwargs: object) -> None:
        return None

    original_guild_payload = media_api.guild_payload

    def render_guild(value: Guild) -> dict[str, object]:
        # A synchronous serializer must never be the operation that triggers
        # an async lazy load for PostgreSQL's expired ``updated_at`` value.
        assert "refresh" in calls
        calls.append("render")
        return original_guild_payload(value)

    monkeypatch.setattr(media_api, "local_guild", local_guild)
    monkeypatch.setattr(media_api, "require_permissions", no_op)
    monkeypatch.setattr(media_api, "finalize_attachment", finalize_attachment)
    monkeypatch.setattr(media_api, "bind_asset", bind_asset)
    monkeypatch.setattr(media_api, "queue_guild_mutation", no_op)
    monkeypatch.setattr(media_api, "wake_queued_guild_federation", no_op)
    monkeypatch.setattr(media_api, "publish_dispatch", no_op)
    monkeypatch.setattr(media_api, "guild_payload", render_guild)

    rendered = await media_api.commit_guild_asset(
        guild_id=media_api.EntityRef(f"{guild.id}@{guild.origin_domain}"),
        kind=kind,  # type: ignore[arg-type]
        payload=media_api.AssetCommitRequest(attachment_id=str(attachment.id)),
        response=Response(),
        auth=SimpleNamespace(user=user),  # type: ignore[arg-type]
        session=Session(),  # type: ignore[arg-type]
        redis=object(),  # type: ignore[arg-type]
        settings=settings(),
    )

    assert getattr(guild, field) == digest
    assert rendered["id"] == str(attachment.id)
    assert calls[:3] == ["flush", "refresh", "render"]


async def test_role_icon_commit_binds_digest_and_publishes_role_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "c" * 64
    user = User(
        id=20,
        origin_domain="alpha.localhost",
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    guild = Guild(
        id=10,
        origin_domain="alpha.localhost",
        name="Paper Lantern",
        owner_id=user.id,
        owner_domain=user.origin_domain,
        permission_generation=1,
        history_policy_generation=1,
        federated_history_policy="disabled",
        next_event_seq=1,
        last_event_seq=0,
        sync_status="ready",
        unavailable=False,
    )
    role = Role(
        id=11,
        origin_domain=guild.origin_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        name="Grand Guard",
        permissions=0,
        color=0,
        position=1,
        hoist=False,
        mentionable=False,
    )
    attachment = Attachment(
        id=30,
        origin_domain=guild.origin_domain,
        uploader_id=user.id,
        uploader_domain=user.origin_domain,
        filename="guard.png",
        content_type="image/png",
        detected_content_type="image/png",
        size=128,
        object_key="alpha.localhost/30/clean/original",
        content_sha256=digest,
        variants={},
        scan_status="clean",
        purpose="role_icon",
    )
    mutations: list[tuple[str, dict[str, object]]] = []

    class Session:
        async def flush(self) -> None:
            return None

        async def refresh(self, value: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    async def no_op(*args: object, **kwargs: object) -> None:
        return None

    async def local_guild(*args: object, **kwargs: object) -> Guild:
        return guild

    async def manageable_role(*args: object, **kwargs: object) -> Role:
        return role

    async def finalize_attachment(*args: object, **kwargs: object) -> Attachment:
        return attachment

    async def bind_asset(*args: object, **kwargs: object) -> None:
        assert args[2] == f"role:{role.origin_domain}:{role.id}:icon"
        return None

    async def queue_mutation(*args: object, **kwargs: object) -> None:
        mutations.append((str(args[4]), args[5]))  # type: ignore[arg-type]

    monkeypatch.setattr(media_api, "local_guild", local_guild)
    monkeypatch.setattr(media_api, "require_permissions", no_op)
    monkeypatch.setattr(media_api, "local_manageable_role", manageable_role)
    monkeypatch.setattr(media_api, "finalize_attachment", finalize_attachment)
    monkeypatch.setattr(media_api, "bind_asset", bind_asset)
    monkeypatch.setattr(media_api, "queue_guild_mutation", queue_mutation)
    monkeypatch.setattr(media_api, "wake_queued_guild_federation", no_op)
    monkeypatch.setattr(media_api, "publish_dispatch", no_op)

    rendered = await media_api.commit_role_icon(
        guild_id=media_api.EntityRef(f"{guild.id}@{guild.origin_domain}"),
        role_id=media_api.EntityRef(f"{role.id}@{role.origin_domain}"),
        payload=media_api.AssetCommitRequest(attachment_id=str(attachment.id)),
        response=Response(),
        auth=SimpleNamespace(user=user),  # type: ignore[arg-type]
        session=Session(),  # type: ignore[arg-type]
        redis=object(),  # type: ignore[arg-type]
        settings=settings(),
    )

    assert role.icon_hash == digest
    assert rendered["icon_hash"] == digest
    assert mutations == [("guild.role.update", {"role": rendered})]


def test_role_icons_only_generate_the_compact_chat_derivative() -> None:
    assert image_derivative_sizes("role_icon") == (128,)


@pytest.mark.parametrize(("kind", "field"), (("avatar", "avatar_hash"), ("banner", "banner_hash")))
async def test_user_asset_commit_updates_each_profile_image_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str, field: str
) -> None:
    digest = "b" * 64
    user = User(
        id=20,
        origin_domain="alpha.localhost",
        is_local=True,
        username="owner",
        password_hash="unused",
        profile_version=1,
        profile_resolved=True,
    )
    attachment = Attachment(
        id=31,
        origin_domain=user.origin_domain,
        uploader_id=user.id,
        uploader_domain=user.origin_domain,
        filename=f"{kind}.png",
        content_type="image/png",
        detected_content_type="image/png",
        size=128,
        object_key=f"{user.origin_domain}/31/clean/original",
        content_sha256=digest,
        variants={},
        scan_status="clean",
        purpose=kind,
    )

    class Session:
        async def scalar(self, statement: object) -> User:
            return user

        async def commit(self) -> None:
            return None

    async def finalize_attachment(*args: object, **kwargs: object) -> Attachment:
        return attachment

    async def bind_asset(*args: object, **kwargs: object) -> None:
        return None

    async def friend_updates(*args: object, **kwargs: object) -> set[str]:
        return set()

    async def no_op(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(media_api, "finalize_attachment", finalize_attachment)
    monkeypatch.setattr(media_api, "bind_asset", bind_asset)
    monkeypatch.setattr(media_api, "queue_profile_updates", friend_updates)
    monkeypatch.setattr(media_api, "publish_dispatch", no_op)

    rendered = await media_api.commit_user_asset(
        kind=kind,  # type: ignore[arg-type]
        payload=media_api.AssetCommitRequest(attachment_id=str(attachment.id)),
        response=Response(),
        auth=SimpleNamespace(user=user),  # type: ignore[arg-type]
        session=Session(),  # type: ignore[arg-type]
        redis=object(),  # type: ignore[arg-type]
        settings=settings(),
    )

    assert getattr(user, field) == digest
    assert user.profile_version == 2
    assert rendered["id"] == str(attachment.id)


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


def test_legacy_animated_image_derivatives_are_marked_for_repair() -> None:
    attachment = Attachment(
        id=31,
        origin_domain="alpha.localhost",
        uploader_id=20,
        uploader_domain="alpha.localhost",
        filename="legacy.gif",
        content_type="image/gif",
        detected_content_type="image/gif",
        size=128,
        object_key="alpha.localhost/31/clean/original",
        variants={
            name: {"object_key": f"alpha.localhost/31/{name}.webp"}
            for name in ("thumbnail_128", "thumbnail_512", "thumbnail_1024")
        },
        scan_status="clean",
        purpose="avatar",
    )

    assert not image_derivatives_are_current(attachment)
    for variant in attachment.variants.values():
        variant["processing_version"] = IMAGE_PIPELINE_VERSION
    assert image_derivatives_are_current(attachment)


def test_compact_profile_assets_require_only_their_display_variant() -> None:
    attachment = Attachment(
        id=32,
        origin_domain="alpha.localhost",
        uploader_id=20,
        uploader_domain="alpha.localhost",
        filename="avatar.gif",
        content_type="image/gif",
        detected_content_type="image/gif",
        size=128,
        object_key="alpha.localhost/32/clean/original",
        variants={
            "thumbnail_128": {
                "object_key": "alpha.localhost/32/thumbnail_128.webp",
                "processing_version": IMAGE_PIPELINE_VERSION,
            }
        },
        scan_status="clean",
        purpose="avatar",
    )

    assert image_derivative_sizes("avatar") == (128,)
    assert image_derivatives_are_current(attachment)


def test_webhook_tokens_are_prefixed_random_and_only_stored_as_digests() -> None:
    first = new_webhook_token()
    second = new_webhook_token()
    assert first.startswith("kwh_")
    assert first != second
    assert len(token_digest(first)) == 32
    assert first.encode() not in token_digest(first)


def test_image_pipeline_emits_safe_metadata_and_preserves_animation() -> None:
    source = io.BytesIO()
    frames = [Image.new("RGB", (320, 180), color) for color in ("red", "blue", "green")]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=0,
    )
    derivatives, blurhash, perceptual_hash, width, height = image_derivatives(source.getvalue())
    assert [item.name for item in derivatives] == [
        "thumbnail_128",
        "thumbnail_512",
        "thumbnail_1024",
    ]
    assert (width, height) == (320, 180)
    assert blurhash
    assert len(perceptual_hash) == 16
    expected_sizes = [(128, 72), (320, 180), (320, 180)]
    for derivative, expected_size in zip(derivatives, expected_sizes, strict=True):
        assert (derivative.width, derivative.height) == expected_size
        with Image.open(io.BytesIO(derivative.content)) as rendered:
            assert rendered.format == "WEBP"
            assert rendered.size == expected_size
            assert rendered.n_frames == 3
            rendered.seek(0)
            first_frame = rendered.convert("RGB").getpixel((0, 0))
            rendered.seek(1)
            second_frame = rendered.convert("RGB").getpixel((0, 0))
            assert first_frame != second_frame
    # Variants that resolve to the same dimensions reuse one expensive
    # animated WebP encode while retaining both public variant names.
    assert derivatives[1].content is derivatives[2].content


def test_sticker_crop_preserves_animation_timing() -> None:
    source = io.BytesIO()
    frames = [Image.new("RGBA", (120, 80), color) for color in ("red", "blue")]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[120, 280],
        loop=0,
    )
    derivatives, _, _, width, height = image_derivatives(
        source.getvalue(),
        transform={"crop": {"x": 0.25, "y": 0, "width": 0.5, "height": 1}},
    )
    assert (width, height) == (60, 80)
    with Image.open(io.BytesIO(derivatives[0].content)) as rendered:
        assert rendered.size == (60, 80)
        assert rendered.n_frames == 2
    rendered_vips = pyvips.Image.new_from_buffer(derivatives[0].content, "", n=-1)
    assert rendered_vips.get("delay") == [120, 280]


def test_sticker_pipeline_enforces_dimensions_duration_and_derivative_metadata() -> None:
    source = io.BytesIO()
    frames = [Image.new("RGBA", (320, 320), color) for color in ("red", "blue")]
    frames[0].save(
        source,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[120, 280],
        loop=0,
    )
    derivatives, _, _, width, height = image_derivatives(
        source.getvalue(),
        transform={"sticker": True},
    )
    assert (width, height) == (320, 320)
    assert all(item.animated and item.duration_ms == 400 for item in derivatives)

    wrong_size = io.BytesIO()
    Image.new("RGBA", (319, 320), "red").save(wrong_size, format="PNG")
    with pytest.raises(MediaValidationError, match="320 by 320"):
        image_derivatives(wrong_size.getvalue(), transform={"sticker": True})

    too_long = io.BytesIO()
    frames[0].save(
        too_long,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[2_600, 2_600],
        loop=0,
    )
    with pytest.raises(MediaValidationError, match="5 seconds"):
        image_derivatives(too_long.getvalue(), transform={"sticker": True})
