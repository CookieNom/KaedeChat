from __future__ import annotations

import asyncio
import base64
import io
import json
import warnings
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.core.settings import Settings
from app.db.bot_models import AbuseReport
from app.db.models import Attachment
from app.media import jobs as media_jobs
from app.media import photodna as photodna_module
from app.media import photodna_sdk
from app.media.photodna import (
    PhotoDNAError,
    PhotoDNAFinding,
    PhotoDNAHash,
    PhotoDNAInputRejected,
    PhotoDNAMatchFlag,
    PhotoDNAUnavailable,
    image_ineligibility_reason,
    match_hashes,
    parse_generated_hashes,
    parse_match_response,
    photodna_report_values,
    scan_image,
    sdk_hash_generation_lock,
)
from app.media.processing import content_digest


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


def edge_hash() -> PhotoDNAHash:
    return PhotoDNAHash("PreHashV2", base64.b64encode(b"x" * 924).decode("ascii"))


def distinct_edge_hash(index: int) -> PhotoDNAHash:
    body = index.to_bytes(2, "big") + b"x" * 922
    return PhotoDNAHash("PreHashV2", base64.b64encode(body).decode("ascii"))


def test_edge_hash_requires_exact_sdk_v2_shape() -> None:
    assert len(edge_hash().value) == 1232
    with pytest.raises(PhotoDNAError, match="length"):
        PhotoDNAHash("PreHashV2", base64.b64encode(b"short").decode("ascii"))
    with pytest.raises(PhotoDNAError, match="non-base64"):
        PhotoDNAHash("PreHashV2", "!" * 1232)


def test_generated_animation_hash_collection_uses_the_full_safe_output_budget() -> None:
    rendered = json.dumps(
        {
            "DataRepresentation": "PreHashV2",
            "Values": [edge_hash().value for _ in range(256)],
        }
    ).encode()

    assert len(rendered) > 64 * 1024
    assert len(parse_generated_hashes(rendered)) == 256
    with pytest.raises(PhotoDNAError, match="oversized"):
        parse_generated_hashes(b" " * (384 * 1024 + 1))


def test_match_response_preserves_only_bounded_incident_metadata() -> None:
    payload = {
        "TrackingId": "request-track",
        "MatchResults": [
            {
                "Status": {"Code": 3000, "Description": "OK", "Exception": None},
                "IsMatch": True,
                "TrackingId": "result-track",
                "MatchDetails": {
                    "MatchFlags": [
                        {
                            "Source": "Test",
                            "Violations": ["A1"],
                            "MatchDistance": 179,
                            "AdvancedInfo": [{"Key": "MatchId", "Value": "2600000"}],
                        }
                    ]
                },
            }
        ],
    }

    finding = parse_match_response(payload, 1)[0]

    assert finding == PhotoDNAFinding(
        tracking_id="result-track",
        flags=(
            PhotoDNAMatchFlag(
                source="Test",
                violations=("A1",),
                match_distance=179,
                match_id="2600000",
            ),
        ),
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"TrackingId": "track", "MatchResults": []},
        {
            "TrackingId": "track",
            "MatchResults": [
                {
                    "Status": {"Code": 3002},
                    "IsMatch": False,
                    "TrackingId": "track",
                }
            ],
        },
        {
            "TrackingId": "track",
            "MatchResults": [
                {
                    "Status": {"Code": 3000},
                    "IsMatch": "true",
                    "TrackingId": "track",
                }
            ],
        },
    ],
)
def test_match_response_fails_closed_on_ambiguous_results(payload: object) -> None:
    with pytest.raises(PhotoDNAError):
        parse_match_response(payload, 1)


def test_documented_cloud_size_ineligibility_is_terminal() -> None:
    payload = {
        "TrackingId": "track",
        "MatchResults": [{"Status": {"Code": 3208}}],
    }

    with pytest.raises(PhotoDNAInputRejected, match="could not verify"):
        parse_match_response(payload, 1)


def test_documented_non_image_result_is_terminal_rejection() -> None:
    payload = {
        "TrackingId": "track",
        "MatchResults": [{"Status": {"Code": 3206}}],
    }

    with pytest.raises(PhotoDNAInputRejected, match="could not verify"):
        parse_match_response(payload, 1)


def test_positive_match_wins_over_non_image_result_in_same_batch() -> None:
    payload = {
        "TrackingId": "request-track",
        "MatchResults": [
            {"Status": {"Code": 3206}},
            {
                "Status": {"Code": 3000},
                "IsMatch": True,
                "TrackingId": "matched-frame",
                "MatchDetails": {
                    "MatchFlags": [
                        {
                            "Source": "Test",
                            "Violations": ["A1"],
                            "MatchDistance": 1,
                        }
                    ]
                },
            },
        ],
    }

    findings = parse_match_response(payload, 2)

    assert findings[0] is None
    assert findings[1] is not None
    assert findings[1].tracking_id == "matched-frame"


def test_positive_match_wins_over_size_rejection_in_same_batch() -> None:
    payload = {
        "TrackingId": "request-track",
        "MatchResults": [
            {"Status": {"Code": 3208}},
            {
                "Status": {"Code": 3000},
                "IsMatch": True,
                "TrackingId": "matched-frame",
                "MatchDetails": {
                    "MatchFlags": [
                        {
                            "Source": "Test",
                            "Violations": ["A1"],
                            "MatchDistance": 1,
                        }
                    ]
                },
            },
        ],
    }

    findings = parse_match_response(payload, 2)

    assert findings[0] is None
    assert findings[1] is not None


async def test_match_client_pins_endpoint_headers_and_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "TrackingId": "track",
                "MatchResults": [
                    {
                        "Status": {"Code": 3000, "Description": "OK"},
                        "IsMatch": False,
                        "TrackingId": "track",
                        "MatchDetails": None,
                    }
                ],
            },
            request=request,
        )

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=transport, **kwargs),
    )
    configured = settings(photodna_subscription_key="p" * 32)

    assert await match_hashes([edge_hash()], configured) == [None]
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == (
        "https://api.microsoftmoderator.com/photodna/v1.0/MatchHash?enhance=false"
    )
    assert request.headers["Ocp-Apim-Subscription-Key"] == "p" * 32
    assert request.headers["Cache-Control"] == "no-cache"
    assert json.loads(request.content) == [
        {"DataRepresentation": "PreHashV2", "Value": edge_hash().value}
    ]


def test_settings_require_complete_enabled_photodna_configuration() -> None:
    with pytest.raises(ValidationError, match="photodna_subscription_key"):
        settings(photodna_enabled=True, photodna_sdk_root="/opt/photodna")
    with pytest.raises(ValidationError, match="PHOTODNA_EDGEHASHGENERATOR"):
        settings(photodna_enabled=True, photodna_subscription_key="p" * 32)
    with pytest.raises(ValidationError, match="must not be a placeholder"):
        settings(
            photodna_enabled=True,
            photodna_subscription_key="replace-with-real-key",
            photodna_sdk_root="/opt/photodna",
        )
    with pytest.raises(ValidationError, match="Microsoft PhotoDNA MatchHash"):
        settings(photodna_match_url="http://api.microsoftmoderator.com/photodna/v1.0/MatchHash")


def test_sdk_adapter_uses_microsoft_edge_v2_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(b"x" * 924)
    calls: list[tuple[int, int, int]] = []

    class Generator:
        def PhotoDnaEdgeHash(
            self,
            pixels: object,
            output: Any,
            width: int,
            height: int,
            stride: int,
            options: object,
        ) -> int:
            del pixels, options
            output.raw = encoded
            calls.append((width, height, stride))
            return 0

    edge = SimpleNamespace(
        PhotoDnaOptions=SimpleNamespace(
            Rgb=0,
            Rgba=0x100,
            Cmyk=0x300,
            HashFormatEdgeV2Base64=0x90,
        ),
        HashSize=SimpleNamespace(EdgeV2Base64=SimpleNamespace(value=1232)),
    )
    monkeypatch.setattr(photodna_sdk, "_sdk", lambda: (edge, Generator()))
    rendered = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(rendered, format="PNG")

    assert photodna_sdk._hash(rendered.getvalue()) == encoded.decode("ascii")
    assert calls == [(160, 160, 0)]


def test_sdk_adapter_hashes_every_animation_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    encoded = base64.b64encode(b"x" * 924)
    calls: list[tuple[int, int, int]] = []

    class Generator:
        def PhotoDnaEdgeHash(
            self,
            pixels: object,
            output: Any,
            width: int,
            height: int,
            stride: int,
            options: object,
        ) -> int:
            del pixels, options
            output.raw = encoded
            calls.append((width, height, stride))
            return 0

    edge = SimpleNamespace(
        PhotoDnaOptions=SimpleNamespace(
            Rgb=0,
            Rgba=0x100,
            Cmyk=0x300,
            HashFormatEdgeV2Base64=0x90,
        ),
        HashSize=SimpleNamespace(EdgeV2Base64=SimpleNamespace(value=1232)),
    )
    monkeypatch.setattr(photodna_sdk, "_sdk", lambda: (edge, Generator()))
    rendered = io.BytesIO()
    first = Image.new("RGB", (160, 160), "teal")
    second = Image.new("RGB", (160, 160), "purple")
    first.save(rendered, format="GIF", save_all=True, append_images=[second], duration=100)

    assert photodna_sdk._hashes(rendered.getvalue()) == [
        encoded.decode("ascii"),
        encoded.decode("ascii"),
    ]
    assert calls == [(160, 160, 0), (160, 160, 0)]


def test_sdk_adapter_rejects_images_below_the_native_eligibility_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    edge = SimpleNamespace(PhotoDnaOptions=SimpleNamespace(Rgb=0))
    monkeypatch.setattr(photodna_sdk, "_sdk", lambda: (edge, object()))
    rendered = io.BytesIO()
    Image.new("RGB", (49, 160), "teal").save(rendered, format="PNG")

    with pytest.raises(photodna_sdk.PhotoDNASDKError, match="at least 50 pixels"):
        photodna_sdk._hash(rendered.getvalue())


def test_sdk_adapter_normalizes_small_icons_to_the_cloud_dimension_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(b"x" * 924)
    calls: list[tuple[int, int]] = []

    class Generator:
        def PhotoDnaEdgeHash(
            self,
            pixels: object,
            output: Any,
            width: int,
            height: int,
            stride: int,
            options: object,
        ) -> int:
            del pixels, stride, options
            output.raw = encoded
            calls.append((width, height))
            return 0

    edge = SimpleNamespace(
        PhotoDnaOptions=SimpleNamespace(
            Rgb=0,
            Rgba=0x100,
            Cmyk=0x300,
            HashFormatEdgeV2Base64=0x90,
        ),
        HashSize=SimpleNamespace(EdgeV2Base64=SimpleNamespace(value=1232)),
    )
    monkeypatch.setattr(photodna_sdk, "_sdk", lambda: (edge, Generator()))
    rendered = io.BytesIO()
    Image.new("RGB", (128, 128), "teal").save(rendered, format="JPEG")

    assert photodna_sdk._hash(rendered.getvalue()) == encoded.decode("ascii")
    assert calls == [(160, 160)]


def test_cloud_eligibility_allows_safe_small_icon_normalization() -> None:
    too_small = io.BytesIO()
    Image.new("RGB", (159, 160), "teal").save(too_small, format="PNG")
    large_source = io.BytesIO()
    Image.new("RGB", (1200, 1200), "teal").save(large_source, format="BMP")

    assert image_ineligibility_reason(too_small.getvalue()) is None
    assert len(large_source.getvalue()) > 4_000_000
    # MatchHash receives the fixed-size Edge Hash, not the source file. Do not
    # create an unscanned large-file bypass; Microsoft can return status 3208
    # if the generated hash is outside its current image eligibility window.
    assert image_ineligibility_reason(large_source.getvalue()) is None


def test_cloud_eligibility_terminally_rejects_pillow_decompression_bombs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bomb(_stream: object) -> None:
        raise Image.DecompressionBombError("declared image dimensions exceed the decode limit")

    monkeypatch.setattr(photodna_module.Image, "open", bomb)

    with pytest.raises(PhotoDNAInputRejected, match="too large to scan safely"):
        image_ineligibility_reason(b"compressed image header")


def test_parent_and_isolated_adapter_reject_images_above_decoded_pixel_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = io.BytesIO()
    # One-bit pixels keep this regression fixture small while its declared
    # decode shape crosses the production 25-million-pixel ceiling.
    Image.new("1", (5001, 5000)).save(rendered, format="PNG")
    body = rendered.getvalue()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(PhotoDNAInputRejected, match="too large to scan safely"):
            image_ineligibility_reason(body)

        monkeypatch.setattr(photodna_sdk, "_sdk", lambda: (object(), object()))
        with pytest.raises(photodna_sdk.PhotoDNASDKError, match="pixel limit exceeded"):
            photodna_sdk._hashes(body)


async def test_sdk_directory_lock_serializes_independent_process_admission(tmp_path: Any) -> None:
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with sdk_hash_generation_lock(str(tmp_path)):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        async with sdk_hash_generation_lock(str(tmp_path)):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(photodna_module.HASH_LOCK_POLL_SECONDS * 2)
    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()


async def test_cancelled_hash_generation_kills_and_reaps_decoder_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    started = asyncio.Event()

    class Process:
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self, _data: bytes) -> tuple[bytes, bytes]:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            return self.returncode or 0

    process = Process()

    async def spawn(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(photodna_module.asyncio, "create_subprocess_exec", spawn)
    configured = settings(photodna_sdk_root=str(tmp_path))
    task = asyncio.create_task(photodna_module.generate_edge_hashes(b"image", configured))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed is True
    assert process.waited is True


async def test_hash_generator_spawn_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    async def spawn(*_args: object, **_kwargs: object) -> None:
        raise OSError("process table unavailable")

    monkeypatch.setattr(photodna_module.asyncio, "create_subprocess_exec", spawn)
    configured = settings(photodna_sdk_root=str(tmp_path))

    with pytest.raises(PhotoDNAUnavailable, match="generation is unavailable"):
        await photodna_module.generate_edge_hashes(b"image", configured)


async def test_scan_image_matches_all_frames_in_bounded_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = [distinct_edge_hash(index) for index in range(7)]
    finding = PhotoDNAFinding(
        tracking_id="tracking",
        flags=(PhotoDNAMatchFlag("Test", ("A1",), 0, "match"),),
    )
    batch_sizes: list[int] = []

    async def generate(_data: bytes, _settings: Settings) -> list[PhotoDNAHash]:
        return hashes

    clients: list[httpx.AsyncClient] = []

    async def match(
        batch: list[PhotoDNAHash],
        _settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PhotoDNAFinding | None]:
        batch_sizes.append(len(batch))
        assert client is not None
        clients.append(client)
        return [None] * (len(batch) - 1) + ([finding] if len(batch_sizes) == 2 else [None])

    monkeypatch.setattr(photodna_module, "generate_edge_hashes", generate)
    monkeypatch.setattr(photodna_module, "match_hashes", match)
    rendered = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(rendered, format="PNG")
    configured = settings(
        photodna_enabled=True,
        photodna_subscription_key="p" * 32,
        photodna_sdk_root="/opt/photodna",
    )

    assert await scan_image(rendered.getvalue(), configured) == finding
    assert batch_sizes == [5, 2]
    assert clients[0] is clients[1]


async def test_scan_image_deduplicates_hashes_and_bounds_batch_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = [distinct_edge_hash(index) for index in range(25)] + [distinct_edge_hash(0)] * 5
    active = 0
    peak = 0
    batch_sizes: list[int] = []

    async def generate(_data: bytes, _settings: Settings) -> list[PhotoDNAHash]:
        return hashes

    async def match(
        batch: list[PhotoDNAHash],
        _settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PhotoDNAFinding | None]:
        nonlocal active, peak
        assert client is not None
        active += 1
        peak = max(peak, active)
        batch_sizes.append(len(batch))
        await asyncio.sleep(0.01)
        active -= 1
        return [None] * len(batch)

    monkeypatch.setattr(photodna_module, "generate_edge_hashes", generate)
    monkeypatch.setattr(photodna_module, "match_hashes", match)
    rendered = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(rendered, format="PNG")
    configured = settings(
        photodna_enabled=True,
        photodna_subscription_key="p" * 32,
        photodna_sdk_root="/opt/photodna",
    )

    assert await scan_image(rendered.getvalue(), configured) is None
    assert batch_sizes == [5, 5, 5, 5, 5]
    assert peak == photodna_module.MAX_CONCURRENT_MATCH_BATCHES


async def test_later_batch_match_wins_over_earlier_non_image_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hashes = [distinct_edge_hash(index) for index in range(7)]
    finding = PhotoDNAFinding(
        tracking_id="matched-later-frame",
        flags=(PhotoDNAMatchFlag("Test", ("A1",), 0, "match"),),
    )
    batch_sizes: list[int] = []

    async def generate(_data: bytes, _settings: Settings) -> list[PhotoDNAHash]:
        return hashes

    async def match(
        batch: list[PhotoDNAHash],
        _settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> list[PhotoDNAFinding | None]:
        assert client is not None
        batch_sizes.append(len(batch))
        if len(batch_sizes) == 1:
            raise PhotoDNAInputRejected("one frame was not verifiable")
        return [finding, None]

    monkeypatch.setattr(photodna_module, "generate_edge_hashes", generate)
    monkeypatch.setattr(photodna_module, "match_hashes", match)
    rendered = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(rendered, format="PNG")
    configured = settings(
        photodna_enabled=True,
        photodna_subscription_key="p" * 32,
        photodna_sdk_root="/opt/photodna",
    )

    assert await scan_image(rendered.getvalue(), configured) == finding
    assert batch_sizes == [5, 2]


def test_automated_report_never_retains_image_or_photodna_hash() -> None:
    finding = PhotoDNAFinding(
        tracking_id="tracking",
        flags=(PhotoDNAMatchFlag("NCMEC", ("A1",), 0, "match"),),
    )

    values = photodna_report_values(
        report_id=7,
        attachment_ref="7@alpha.localhost",
        finding=finding,
        uploader_ref="3@alpha.localhost",
        detected_content_type="image/png",
        content_sha256="a" * 64,
    )

    evidence = cast(dict[str, Any], values["evidence"])
    assert values["source"] == "photodna"
    assert values["reporter_id"] is None
    assert values["target_type"] == "attachment"
    assert evidence["bytes_retained"] is False
    assert evidence["photodna_hash_retained"] is False
    assert "Value" not in str(evidence)


async def test_local_match_is_durably_quarantined_before_staging_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(image, format="PNG")
    body = image.getvalue()
    attachment = Attachment(
        id=77,
        origin_domain="alpha.localhost",
        uploader_id=3,
        uploader_domain="alpha.localhost",
        filename="photo.png",
        content_type="image/png",
        size=len(body),
        object_key="alpha.localhost/77/staging/original",
        staging_object_key="alpha.localhost/77/staging/original",
        scan_status="pending",
        encryption_mode="plaintext",
        purpose="attachment",
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        finalized_at=datetime.now(UTC),
        variants={},
    )
    finding = PhotoDNAFinding(
        tracking_id="tracking",
        flags=(PhotoDNAMatchFlag("Test", ("A1",), 10, "match"),),
    )
    events: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.added: list[object] = []

        async def scalar(self, _statement: object) -> object | None:
            self.scalar_calls += 1
            # The lifecycle now acquires the per-media advisory fence before
            # selecting the Attachment row.
            return attachment if self.scalar_calls == 2 else None

        async def get(self, model: object, _key: object) -> object | None:
            assert model is AbuseReport
            return None

        def add(self, value: object) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            events.append("rollback")

    session = Session()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> bytes:
            return body

        async def delete(self, _bucket: str, key: str) -> None:
            assert key == attachment.object_key
            events.append("delete")

        async def put(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("matched bytes must never be promoted")

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)

    async def clean_scan(*_args: object) -> str:
        return "clean"

    async def match(*_args: object) -> PhotoDNAFinding:
        return finding

    monkeypatch.setattr(media_jobs, "clamav_scan", clean_scan)
    monkeypatch.setattr(media_jobs, "scan_image", match)
    monkeypatch.setattr(media_jobs, "try_lock_asset_digest", AsyncMock(return_value=True))

    result = await media_jobs.process_attachment_record(
        cast(Any, session), settings(), attachment.id, attachment.origin_domain
    )

    assert result == "quarantined"
    assert attachment.scan_status == "quarantined"
    assert attachment.deleted_at is not None
    assert events == ["commit", "delete"]
    assert attachment.staging_object_key == attachment.object_key
    assert len(session.added) == 1
    report = cast(AbuseReport, session.added[0])
    assert report.source == "photodna"
    assert report.evidence["bytes_retained"] is False


async def test_photodna_input_rejection_is_terminal_without_match_report_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(image, format="PNG")
    body = image.getvalue()
    attachment = Attachment(
        id=78,
        origin_domain="alpha.localhost",
        uploader_id=3,
        uploader_domain="alpha.localhost",
        filename="oversized.png",
        content_type="image/png",
        size=len(body),
        object_key="alpha.localhost/78/staging/original",
        staging_object_key="alpha.localhost/78/staging/original",
        scan_status="pending",
        encryption_mode="plaintext",
        purpose="attachment",
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        finalized_at=datetime.now(UTC),
        variants={},
    )
    events: list[str] = []

    class Session:
        def __init__(self) -> None:
            self.scalar_calls = 0

        async def scalar(self, _statement: object) -> object | None:
            self.scalar_calls += 1
            return attachment if self.scalar_calls == 2 else None

        async def commit(self) -> None:
            events.append("commit")

        async def rollback(self) -> None:
            raise AssertionError("terminal policy rejection must not roll back for retry")

        async def get(self, *_args: object) -> object | None:
            raise AssertionError("policy rejection must not query or create a PhotoDNA report")

        def add(self, _value: object) -> None:
            raise AssertionError("policy rejection must not create a PhotoDNA report")

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> bytes:
            return body

        async def delete(self, _bucket: str, key: str) -> None:
            assert key == attachment.object_key
            events.append("delete")

        async def put(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("policy-rejected bytes must never be promoted")

    async def clean_scan(*_args: object) -> str:
        return "clean"

    async def reject(*_args: object) -> None:
        raise PhotoDNAInputRejected("image is too large to scan safely")

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    monkeypatch.setattr(media_jobs, "clamav_scan", clean_scan)
    monkeypatch.setattr(media_jobs, "scan_image", reject)
    monkeypatch.setattr(media_jobs, "try_lock_asset_digest", AsyncMock(return_value=True))

    result = await media_jobs.process_attachment_record(
        cast(Any, Session()), settings(), attachment.id, attachment.origin_domain
    )

    assert result == "rejected"
    assert attachment.scan_status == "rejected"
    assert attachment.deleted_at is not None
    assert events == ["commit", "delete"]


async def test_clean_public_asset_transition_propagates_retained_terminal_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(image, format="PNG")
    body = image.getvalue()
    attachment = Attachment(
        id=80,
        origin_domain="alpha.localhost",
        uploader_id=3,
        uploader_domain="alpha.localhost",
        filename="avatar.png",
        content_type="image/png",
        size=len(body),
        object_key="alpha.localhost/80/staging/original",
        staging_object_key="alpha.localhost/80/staging/original",
        scan_status="pending",
        encryption_mode="plaintext",
        purpose="avatar",
        upload_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        finalized_at=datetime.now(UTC),
        variants={},
    )
    scalar_values = iter((None, attachment, 77))
    commits: list[str] = []

    class Session:
        async def scalar(self, _statement: object) -> object | None:
            return next(scalar_values)

        async def commit(self) -> None:
            commits.append("commit")

    put = AsyncMock()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> bytes:
            return body

        async def put(self, *args: object, **kwargs: object) -> None:
            await put(*args, **kwargs)

    async def discard(_session: object, _settings: object, item: Attachment) -> None:
        item.deleted_at = datetime.now(UTC)

    digest_try_lock = AsyncMock(return_value=True)
    before_terminal = AsyncMock()
    delete_objects = AsyncMock(return_value=True)
    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    monkeypatch.setattr(media_jobs, "clamav_scan", AsyncMock(return_value="clean"))
    monkeypatch.setattr(media_jobs, "scan_image", AsyncMock(return_value=None))
    monkeypatch.setattr(media_jobs, "discard_attachment", discard)
    monkeypatch.setattr(media_jobs, "try_lock_asset_digest", digest_try_lock)
    monkeypatch.setattr(media_jobs, "delete_terminal_attachment_objects", delete_objects)

    result = await media_jobs.process_attachment_record(
        cast(Any, Session()),
        settings(),
        attachment.id,
        attachment.origin_domain,
        before_terminal_commit=before_terminal,
    )

    assert result == "rejected"
    assert attachment.scan_status == "rejected"
    assert attachment.deleted_at is not None
    assert commits == ["commit"]
    # One acquisition gates clean publication; the reentrant acquisition is
    # the ordinary terminal-commit preparation before its signed source work.
    assert digest_try_lock.await_count == 2
    before_terminal.assert_awaited_once_with(attachment)
    put.assert_not_awaited()
    delete_objects.assert_awaited_once()


async def test_legacy_reprocessing_persists_photodna_policy_rejection_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = io.BytesIO()
    Image.new("RGB", (160, 160), "teal").save(image, format="GIF")
    body = image.getvalue()
    variant_keys = {
        name: {"object_key": f"alpha.localhost/79/{name}.webp"}
        for name in ("thumbnail_128", "thumbnail_512", "thumbnail_1024")
    }
    attachment = Attachment(
        id=79,
        origin_domain="alpha.localhost",
        uploader_id=3,
        uploader_domain="alpha.localhost",
        filename="legacy.gif",
        content_type="image/gif",
        detected_content_type="image/gif",
        content_sha256=content_digest(body),
        size=len(body),
        object_key="alpha.localhost/79/clean/original",
        staging_object_key=None,
        scan_status="clean",
        encryption_mode="plaintext",
        purpose="attachment",
        upload_expires_at=datetime.now(UTC) - timedelta(days=1),
        finalized_at=datetime.now(UTC) - timedelta(days=1),
        variants=variant_keys,
    )
    deleted: set[tuple[str, str]] = set()

    class Session:
        def __init__(self) -> None:
            self.scalar_calls = 0
            self.commits = 0

        async def scalar(self, _statement: object) -> object | None:
            self.scalar_calls += 1
            return attachment if self.scalar_calls == 2 else None

        async def commit(self) -> None:
            self.commits += 1

        async def rollback(self) -> None:
            raise AssertionError("deterministic policy rejection must not be retried")

        async def get(self, *_args: object) -> object | None:
            raise AssertionError("policy rejection must not create a PhotoDNA report")

        def add(self, _value: object) -> None:
            raise AssertionError("policy rejection must not create a PhotoDNA report")

    session = Session()

    class Storage:
        def __init__(self, _settings: object) -> None:
            pass

        async def get(self, *_args: object, **_kwargs: object) -> bytes:
            return body

        async def delete(self, bucket: str, key: str) -> None:
            deleted.add((bucket, key))

        async def put(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("policy-rejected legacy bytes must never be promoted")

    async def reject(*_args: object) -> None:
        raise PhotoDNAInputRejected("animated image has too many frames to scan safely")

    monkeypatch.setattr(media_jobs, "S3Storage", Storage)
    monkeypatch.setattr(media_jobs, "scan_image", reject)
    monkeypatch.setattr(media_jobs, "try_lock_asset_digest", AsyncMock(return_value=True))

    result = await media_jobs.process_attachment_record(
        cast(Any, session), settings(), attachment.id, attachment.origin_domain
    )

    assert result == "rejected"
    assert attachment.scan_status == "rejected"
    assert attachment.deleted_at is not None
    assert attachment.staging_object_key == attachment.object_key
    assert session.commits == 1
    assert deleted == {
        ("kaede-attachments", attachment.object_key),
        *(("kaede-derived", item["object_key"]) for item in variant_keys.values()),
    }
