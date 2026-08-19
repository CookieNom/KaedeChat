from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import io
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx
from PIL import Image

from app.core.settings import Settings
from app.db.bot_models import AbuseReport
from app.media.photodna_dimensions import (
    MIN_NATIVE_IMAGE_DIMENSION,
    normalized_photodna_dimensions,
)
from app.media.processing import MediaValidationError

MAX_BATCH_SIZE = 5
MAX_CONCURRENT_MATCH_BATCHES = 4
MAX_HASH_LENGTH = 16_384
MAX_RESPONSE_BYTES = 256 * 1024
MAX_MATCH_FLAGS = 32
EDGE_V2_DECODED_BYTES = 924
MAX_ANIMATION_FRAMES = 256
MAX_ANIMATION_TOTAL_PIXELS = 25_000_000
MAX_GENERATOR_RESPONSE_BYTES = 384 * 1024
HASH_GENERATION_LIMITER = asyncio.Semaphore(1)
HASH_LOCK_POLL_SECONDS = 0.05


class PhotoDNAError(RuntimeError):
    """PhotoDNA could not produce a trustworthy terminal result."""


class PhotoDNAConfigurationError(PhotoDNAError):
    pass


class PhotoDNAUnavailable(PhotoDNAError):
    pass


class PhotoDNAInputRejected(MediaValidationError):
    """The image cannot be completely scanned within defensive decode bounds."""


@dataclass(frozen=True, slots=True)
class PhotoDNAHash:
    representation: Literal["Hash", "PreHashV2"]
    value: str

    def __post_init__(self) -> None:
        if self.representation not in {"Hash", "PreHashV2"}:
            raise PhotoDNAError("PhotoDNA generator returned an unknown representation")
        if not 1 <= len(self.value) <= MAX_HASH_LENGTH or not self.value.isascii():
            raise PhotoDNAError("PhotoDNA generator returned an invalid hash")
        try:
            decoded = base64.b64decode(self.value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise PhotoDNAError("PhotoDNA generator returned a non-base64 hash") from exc
        if not decoded:
            raise PhotoDNAError("PhotoDNA generator returned an empty hash")
        if self.representation == "PreHashV2" and len(decoded) != EDGE_V2_DECODED_BYTES:
            raise PhotoDNAError("PhotoDNA generator returned an invalid Edge Hash V2 length")


@dataclass(frozen=True, slots=True)
class PhotoDNAMatchFlag:
    source: str
    violations: tuple[str, ...]
    match_distance: int
    match_id: str | None


@dataclass(frozen=True, slots=True)
class PhotoDNAFinding:
    tracking_id: str
    flags: tuple[PhotoDNAMatchFlag, ...]


def _bounded_string(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise PhotoDNAError(f"PhotoDNA returned an invalid {name}")
    return value


def _match_id(advanced: object) -> str | None:
    if advanced is None:
        return None
    if not isinstance(advanced, list) or len(advanced) > 32:
        raise PhotoDNAError("PhotoDNA returned invalid advanced match metadata")
    for item in advanced:
        if not isinstance(item, dict):
            raise PhotoDNAError("PhotoDNA returned invalid advanced match metadata")
        key = item.get("Key")
        value = item.get("Value")
        if key == "MatchId":
            return _bounded_string(value, name="match identifier", maximum=256)
    return None


def _parse_flags(value: object) -> tuple[PhotoDNAMatchFlag, ...]:
    if not isinstance(value, dict):
        raise PhotoDNAError("PhotoDNA omitted match details")
    raw_flags = value.get("MatchFlags")
    if not isinstance(raw_flags, list) or not 1 <= len(raw_flags) <= MAX_MATCH_FLAGS:
        raise PhotoDNAError("PhotoDNA returned invalid match flags")
    flags: list[PhotoDNAMatchFlag] = []
    for raw in raw_flags:
        if not isinstance(raw, dict):
            raise PhotoDNAError("PhotoDNA returned an invalid match flag")
        source = _bounded_string(raw.get("Source"), name="match source", maximum=128)
        violations_raw = raw.get("Violations")
        if not isinstance(violations_raw, list) or len(violations_raw) > 32:
            raise PhotoDNAError("PhotoDNA returned invalid violation codes")
        violations = tuple(
            _bounded_string(item, name="violation code", maximum=32) for item in violations_raw
        )
        distance = raw.get("MatchDistance")
        if isinstance(distance, bool) or not isinstance(distance, int) or distance < 0:
            raise PhotoDNAError("PhotoDNA returned an invalid match distance")
        flags.append(
            PhotoDNAMatchFlag(
                source=source,
                violations=violations,
                match_distance=distance,
                match_id=_match_id(raw.get("AdvancedInfo")),
            )
        )
    return tuple(flags)


def parse_match_response(payload: object, expected_results: int) -> list[PhotoDNAFinding | None]:
    if not isinstance(payload, dict):
        raise PhotoDNAError("PhotoDNA returned a non-object response")
    tracking_id = _bounded_string(payload.get("TrackingId"), name="tracking ID", maximum=256)
    raw_results = payload.get("MatchResults")
    if not isinstance(raw_results, list) or len(raw_results) != expected_results:
        raise PhotoDNAError("PhotoDNA returned the wrong number of match results")
    findings: list[PhotoDNAFinding | None] = []
    input_rejected = False
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise PhotoDNAError("PhotoDNA returned an invalid match result")
        status = raw.get("Status")
        if not isinstance(status, dict):
            raise PhotoDNAError("PhotoDNA omitted a result status")
        code = status.get("Code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise PhotoDNAError("PhotoDNA returned an invalid status code")
        if code in {3206, 3208}:
            # Microsoft documents this as a deterministic input result rather
            # than a transient provider failure. A multi-frame image may still
            # contain a positive match in another result, however, so retain
            # the rejection until every result in this batch has been checked.
            input_rejected = True
            findings.append(None)
            continue
        if code != 3000:
            raise PhotoDNAError(f"PhotoDNA could not verify the hash (status {code})")
        is_match = raw.get("IsMatch")
        if not isinstance(is_match, bool):
            raise PhotoDNAError("PhotoDNA returned an invalid match decision")
        result_tracking = raw.get("TrackingId")
        if result_tracking is not None:
            result_tracking = _bounded_string(
                result_tracking, name="result tracking ID", maximum=256
            )
        findings.append(
            PhotoDNAFinding(
                tracking_id=result_tracking or tracking_id,
                flags=_parse_flags(raw.get("MatchDetails")),
            )
            if is_match
            else None
        )
    if input_rejected and not any(finding is not None for finding in findings):
        raise PhotoDNAInputRejected("PhotoDNA could not verify the submitted file as an image")
    return findings


def photodna_report_values(
    *,
    report_id: int,
    attachment_ref: str,
    finding: PhotoDNAFinding,
    detected_content_type: str,
    content_sha256: str,
    uploader_ref: str | None = None,
    message_ref: str | None = None,
    purpose: str | None = None,
    remote_variant: str | None = None,
) -> dict[str, Any]:
    """Build a metadata-only automated report; matched bytes and hashes are excluded."""

    evidence: dict[str, Any] = {
        "provider": "microsoft_photodna",
        "provider_tracking_id": finding.tracking_id,
        "attachment_ref": attachment_ref,
        "detected_content_type": detected_content_type,
        # A normal content digest identifies the quarantined upload for incident
        # response without retaining either the image or Microsoft's sensitive
        # perceptual hash.
        "content_sha256": content_sha256,
        "match_flags": [
            {
                "source": flag.source,
                "violations": list(flag.violations),
                "match_distance": flag.match_distance,
                "match_id": flag.match_id,
            }
            for flag in finding.flags
        ],
        "bytes_retained": False,
        "photodna_hash_retained": False,
    }
    if uploader_ref is not None:
        evidence["uploader_ref"] = uploader_ref
    if purpose is not None:
        evidence["purpose"] = purpose
    if remote_variant is not None:
        evidence["remote_variant"] = remote_variant
    return {
        "id": report_id,
        "source": "photodna",
        "reporter_id": None,
        "reporter_domain": None,
        "reporter_is_local": None,
        "target_type": "attachment",
        "target_ref": attachment_ref,
        "category": "illegal_content",
        "description": (
            "Microsoft PhotoDNA returned a positive match for this image. "
            "Kaede blocked it before local publication or cache admission."
        ),
        "message_ref": message_ref,
        "evidence": evidence,
        "encryption_mode": "plaintext",
        "status": "submitted",
    }


def photodna_report(**values: Any) -> AbuseReport:
    return AbuseReport(**photodna_report_values(**values))


@asynccontextmanager
async def sdk_hash_generation_lock(sdk_root: str) -> AsyncIterator[None]:
    """Serialize native decodes across every process mounting this SDK root.

    The production API uses multiple Uvicorn workers and the media worker is a
    separate container.  A process-local semaphore therefore cannot bound the
    aggregate decoded-image memory peak.  Linux ``flock`` on the shared,
    read-only SDK directory coordinates all of those processes without placing
    a mutable lock file beside the licensed SDK.
    """

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(Path(sdk_root), flags)
    except OSError as exc:
        raise PhotoDNAUnavailable("PhotoDNA SDK admission is unavailable") from exc
    locked = False
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except BlockingIOError:
                await asyncio.sleep(HASH_LOCK_POLL_SECONDS)
            except OSError as exc:
                raise PhotoDNAUnavailable("PhotoDNA SDK admission is unavailable") from exc
        yield
    finally:
        if locked:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


async def terminate_hash_process(process: asyncio.subprocess.Process) -> None:
    """Do not let timeouts or disconnected requests strand a decoder child."""

    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.kill()
    with suppress(ProcessLookupError):
        await process.wait()


async def generate_edge_hashes(data: bytes, settings: Settings) -> list[PhotoDNAHash]:
    if settings.photodna_sdk_root is None:
        raise PhotoDNAConfigurationError("the licensed PhotoDNA SDK root is not configured")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PHOTODNA_EDGEHASHGENERATOR": settings.photodna_sdk_root,
        # The adapter does not need a BLAS thread pool. These also defend
        # against future NumPy/runtime changes multiplying CPU and memory use.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    # Pillow, NumPy and the native SDK hold multiple decoded frame buffers.
    # The local semaphore bounds queued file descriptors, while the SDK-root
    # lock serializes native decoding across every service process/container.
    try:
        async with (
            asyncio.timeout(settings.photodna_hash_timeout_seconds),
            HASH_GENERATION_LIMITER,
            sdk_hash_generation_lock(settings.photodna_sdk_root),
        ):
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app.media.photodna_sdk",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=environment,
                )
            except OSError as exc:
                raise PhotoDNAUnavailable("PhotoDNA hash generation is unavailable") from exc
            try:
                stdout, _ = await process.communicate(data)
            except BaseException:
                await terminate_hash_process(process)
                raise
    except TimeoutError:
        raise PhotoDNAUnavailable("PhotoDNA hash generation timed out") from None
    if process.returncode != 0 or len(stdout) > MAX_GENERATOR_RESPONSE_BYTES:
        raise PhotoDNAUnavailable("PhotoDNA hash generation failed")
    return parse_generated_hashes(stdout)


def parse_generated_hashes(stdout: bytes) -> list[PhotoDNAHash]:
    if len(stdout) > MAX_GENERATOR_RESPONSE_BYTES:
        raise PhotoDNAError("PhotoDNA generator returned an oversized result")
    try:
        payload: Any = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhotoDNAError("PhotoDNA generator returned invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"DataRepresentation", "Values"}:
        raise PhotoDNAError("PhotoDNA generator returned an invalid result")
    representation = payload["DataRepresentation"]
    if representation not in {"Hash", "PreHashV2"}:
        raise PhotoDNAError("PhotoDNA generator returned an unknown representation")
    values = payload["Values"]
    if (
        not isinstance(values, list)
        or not 1 <= len(values) <= MAX_ANIMATION_FRAMES
        or any(not isinstance(value, str) for value in values)
    ):
        raise PhotoDNAError("PhotoDNA generator returned an invalid hash collection")
    typed_representation = cast(Literal["Hash", "PreHashV2"], representation)
    return [PhotoDNAHash(typed_representation, cast(str, value)) for value in values]


async def generate_edge_hash(data: bytes, settings: Settings) -> PhotoDNAHash:
    """Compatibility helper for callers that explicitly require a still image."""

    hashes = await generate_edge_hashes(data, settings)
    if len(hashes) != 1:
        raise PhotoDNAError("PhotoDNA still-image hashing received an animation")
    return hashes[0]


async def match_hashes(
    hashes: list[PhotoDNAHash],
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[PhotoDNAFinding | None]:
    if not 1 <= len(hashes) <= MAX_BATCH_SIZE:
        raise ValueError("PhotoDNA MatchHash batches must contain between one and five hashes")
    if settings.photodna_subscription_key is None:
        raise PhotoDNAConfigurationError("the PhotoDNA subscription key is not configured")
    request = [{"DataRepresentation": item.representation, "Value": item.value} for item in hashes]
    if client is None:
        timeout = httpx.Timeout(settings.photodna_match_timeout_seconds, connect=5)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as owned_client:
            return await match_hashes(hashes, settings, client=owned_client)
    try:
        async with client.stream(
            "POST",
            settings.photodna_match_url,
            params={"enhance": "false"},
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-cache",
                "Ocp-Apim-Subscription-Key": settings.photodna_subscription_key.get_secret_value(),
            },
            json=request,
        ) as response:
            if response.status_code != 200:
                raise PhotoDNAUnavailable(
                    f"PhotoDNA MatchHash returned HTTP {response.status_code}"
                )
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type.lower() != "application/json":
                raise PhotoDNAError("PhotoDNA returned a non-JSON content type")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise PhotoDNAError("PhotoDNA returned an oversized response")
    except httpx.HTTPError as exc:
        raise PhotoDNAUnavailable("PhotoDNA MatchHash is unavailable") from exc
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhotoDNAError("PhotoDNA returned invalid JSON") from exc
    return parse_match_response(payload, len(hashes))


def image_ineligibility_reason(data: bytes) -> str | None:
    """Apply the cloud contract before invoking the confidential native SDK."""

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            frames = int(getattr(image, "n_frames", 1))
    except Image.DecompressionBombError as exc:
        raise PhotoDNAInputRejected("image is too large to scan safely") from exc
    except (OSError, ValueError) as exc:
        raise PhotoDNAError("PhotoDNA could not read the image dimensions") from exc
    if frames < 1 or frames > MAX_ANIMATION_FRAMES:
        raise PhotoDNAInputRejected("animated image has too many frames to scan safely")
    if width * height * frames > MAX_ANIMATION_TOTAL_PIXELS:
        raise PhotoDNAInputRejected("animated image is too large to scan safely")
    if width < MIN_NATIVE_IMAGE_DIMENSION or height < MIN_NATIVE_IMAGE_DIMENSION:
        return "dimension_below_50_pixels"
    normalized_width, normalized_height = normalized_photodna_dimensions(width, height)
    if normalized_width * normalized_height * frames > MAX_ANIMATION_TOTAL_PIXELS:
        raise PhotoDNAInputRejected("normalized image is too large to scan safely")
    return None


async def scan_image(data: bytes, settings: Settings) -> PhotoDNAFinding | None:
    if not settings.photodna_enabled:
        return None
    if image_ineligibility_reason(data) is not None:
        # The native SDK cannot safely inspect images below its own input
        # floor. Publishing one as clean would turn that boundary into a
        # moderation bypass, so use the same neutral terminal rejection as
        # provider statuses 3206/3208.
        raise PhotoDNAInputRejected("PhotoDNA cannot verify this image size")
    generated = await generate_edge_hashes(data, settings)
    # Repeated animation frames produce the same immutable Edge Hash. Checking
    # one copy retains complete distinct-frame coverage while avoiding redundant
    # provider requests.
    generated = list(dict.fromkeys(generated))
    timeout = httpx.Timeout(settings.photodna_match_timeout_seconds, connect=5)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        limiter = asyncio.Semaphore(MAX_CONCURRENT_MATCH_BATCHES)

        async def check_batch(
            batch: list[PhotoDNAHash],
        ) -> list[PhotoDNAFinding | None] | PhotoDNAInputRejected:
            async with limiter:
                try:
                    return await match_hashes(batch, settings, client=client)
                except PhotoDNAInputRejected as exc:
                    return exc

        batch_results = await asyncio.gather(
            *(
                check_batch(generated[offset : offset + MAX_BATCH_SIZE])
                for offset in range(0, len(generated), MAX_BATCH_SIZE)
            ),
            return_exceptions=True,
        )
    # Positive results take precedence even if another animation batch was
    # rejected or encountered a provider error.
    for result in batch_results:
        if isinstance(result, list):
            finding = next((item for item in result if item is not None), None)
            if finding is not None:
                return finding
    input_rejected = False
    for result in batch_results:
        if isinstance(result, PhotoDNAInputRejected):
            input_rejected = True
        elif isinstance(result, BaseException):
            raise result
    if input_rejected:
        raise PhotoDNAInputRejected("PhotoDNA could not verify the submitted file as an image")
    return None
