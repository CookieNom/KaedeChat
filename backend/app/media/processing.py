from __future__ import annotations

import asyncio
import hashlib
import io
import re
import tempfile
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import blurhash  # type: ignore[import-untyped]
import imagehash
import numpy as np
import pyvips  # type: ignore[import-untyped]
from PIL import Image, ImageFile, UnidentifiedImageError

from app.core.settings import Settings

Image.MAX_IMAGE_PIXELS = 100_000_000
ImageFile.LOAD_TRUNCATED_IMAGES = False

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
ALLOWED_CONTENT_TYPES = {
    "application/json",
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "audio/mpeg",
    "audio/ogg",
    "audio/wav",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
    "text/csv",
    "text/plain",
    "video/mp4",
    "video/webm",
}
IMAGE_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
VIDEO_TYPES = {"video/mp4", "video/webm"}
IMAGE_PIPELINE_VERSION = 2


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Derivative:
    name: str
    content: bytes
    content_type: str
    width: int
    height: int


def sanitize_filename(value: str) -> str:
    name = value.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")
    name = SAFE_FILENAME_RE.sub("_", name).strip(" .")
    if not name:
        name = "upload"
    return name[:255]


def normalize_declared_type(value: str) -> str:
    rendered = value.split(";", 1)[0].strip().lower()
    if rendered not in ALLOWED_CONTENT_TYPES:
        raise MediaValidationError("unsupported attachment content type")
    return rendered


def sniff_content_type(data: bytes) -> str:
    prefix = data[:512]
    stripped = prefix.lstrip().lower()
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE":
        return "audio/wav"
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    if prefix.startswith(b"OggS"):
        return "audio/ogg"
    if prefix.startswith(b"ID3") or (
        len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    if len(prefix) >= 12 and prefix[4:8] == b"ftyp":
        return "video/mp4"
    if prefix.startswith(b"\x1aE\xdf\xa3") and b"webm" in prefix.lower():
        return "video/webm"
    if prefix.startswith((b"MZ", b"\x7fELF", b"\xca\xfe\xba\xbe")):
        return "application/x-executable"
    if stripped.startswith((b"<!doctype html", b"<html", b"<script", b"<?xml", b"<svg")):
        return "text/html"
    if prefix and b"\x00" not in prefix:
        try:
            prefix.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            return "text/plain"
    return "application/octet-stream"


def validate_detected_type(declared: str, detected: str) -> None:
    if detected not in ALLOWED_CONTENT_TYPES:
        raise MediaValidationError("attachment contains a prohibited file type")
    if declared == "application/octet-stream":
        return
    aliases = {
        ("text/csv", "text/plain"),
        ("application/json", "text/plain"),
    }
    if declared != detected and (declared, detected) not in aliases:
        raise MediaValidationError("declared content type does not match the file")


async def _clamav_scan_chunks(chunks: AsyncIterable[bytes], settings: Settings) -> str:
    if not settings.media_scan_enabled:
        if settings.environment == "production":
            raise RuntimeError("malware scanning cannot be disabled in production")
        return "clean"
    writer: asyncio.StreamWriter | None = None
    try:
        reader, connected_writer = await asyncio.wait_for(
            asyncio.open_connection(settings.media_clamav_host, settings.media_clamav_port),
            timeout=5,
        )
        writer = connected_writer
        connected_writer.write(b"zINSTREAM\0")
        async for chunk in chunks:
            connected_writer.write(len(chunk).to_bytes(4, "big") + chunk)
            await connected_writer.drain()
        connected_writer.write(b"\0\0\0\0")
        await connected_writer.drain()
        result = await asyncio.wait_for(reader.readuntil(b"\0"), timeout=60)
    except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
        raise RuntimeError("malware scanner is unavailable") from exc
    finally:
        if writer is not None:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
    rendered = result.rstrip(b"\0").decode("utf-8", "replace")
    if rendered.endswith(" OK"):
        return "clean"
    if rendered.endswith(" FOUND"):
        return "infected"
    raise RuntimeError("malware scanner returned an indeterminate result")


async def clamav_scan(data: bytes, settings: Settings) -> str:
    async def chunks() -> AsyncIterator[bytes]:
        for offset in range(0, len(data), 64 * 1024):
            yield data[offset : offset + 64 * 1024]

    return await _clamav_scan_chunks(chunks(), settings)


async def clamav_scan_file(path: Path, settings: Settings) -> str:
    """Scan a spool file without retaining the complete attachment in memory."""

    async def chunks() -> AsyncIterator[bytes]:
        import anyio

        async with await anyio.open_file(path, "rb") as source:
            while chunk := await source.read(64 * 1024):
                yield chunk

    return await _clamav_scan_chunks(chunks(), settings)


def _flatten_alpha(image: pyvips.Image) -> pyvips.Image:
    if image.hasalpha():
        return image.flatten(background=[255, 255, 255])
    return image


IMAGE_DERIVATIVE_SIZES = (128, 512, 1024)
COMPACT_IMAGE_PURPOSES = frozenset({"avatar", "guild_icon", "webhook_avatar"})


def image_derivative_sizes(purpose: str) -> tuple[int, ...]:
    """Return the variants needed by each media presentation surface."""

    return (128,) if purpose in COMPACT_IMAGE_PURPOSES else IMAGE_DERIVATIVE_SIZES


def image_derivatives(
    data: bytes,
    *,
    sizes: tuple[int, ...] = IMAGE_DERIVATIVE_SIZES,
    transform: dict[str, Any] | None = None,
) -> tuple[list[Derivative], str, str, int, int]:
    try:
        image = pyvips.Image.new_from_buffer(data, "", access="random", fail_on="error", n=-1)
    except pyvips.Error as exc:
        if "does not support optional argument n" not in str(exc):
            raise MediaValidationError("image decoder rejected the upload") from exc
        try:
            image = pyvips.Image.new_from_buffer(data, "", access="random", fail_on="error")
        except pyvips.Error as fallback_exc:
            raise MediaValidationError("image decoder rejected the upload") from fallback_exc
    width = int(image.width)
    page_height = int(image.get("page-height")) if image.get_typeof("page-height") else image.height
    height = int(page_height)
    pages = max(1, image.height // page_height)
    if width <= 0 or height <= 0 or width * height * pages > 100_000_000:
        raise MediaValidationError("image dimensions exceed the processing limit")
    image = image.autorot()
    if transform:
        raw_crop = transform.get("crop")
        if raw_crop is not None:
            if not isinstance(raw_crop, dict):
                raise MediaValidationError("sticker crop is invalid")
            try:
                crop_x = float(raw_crop["x"])
                crop_y = float(raw_crop["y"])
                crop_width = float(raw_crop["width"])
                crop_height = float(raw_crop["height"])
            except (KeyError, TypeError, ValueError):
                raise MediaValidationError("sticker crop is invalid") from None
            if (
                crop_x < 0
                or crop_y < 0
                or crop_width <= 0
                or crop_height <= 0
                or crop_x + crop_width > 1.000001
                or crop_y + crop_height > 1.000001
            ):
                raise MediaValidationError("sticker crop is outside the image")
            left = min(width - 1, round(crop_x * width))
            top = min(height - 1, round(crop_y * height))
            crop_pixel_width = max(1, min(width - left, round(crop_width * width)))
            crop_pixel_height = max(1, min(height - top, round(crop_height * height)))
            frames = [
                image.crop(left, (frame * page_height) + top, crop_pixel_width, crop_pixel_height)
                for frame in range(pages)
            ]
            delays = image.get("delay") if image.get_typeof("delay") else None
            loop = int(image.get("loop")) if image.get_typeof("loop") else None
            image = frames[0] if pages == 1 else pyvips.Image.arrayjoin(frames, across=1)
            if pages > 1:
                image.set_type(pyvips.GValue.gint_type, "page-height", crop_pixel_height)
                if isinstance(delays, list):
                    image.set_type(pyvips.GValue.array_int_type, "delay", delays)
                if loop is not None:
                    image.set_type(pyvips.GValue.gint_type, "loop", loop)
            width = crop_pixel_width
            height = crop_pixel_height
            page_height = crop_pixel_height
        if transform.get("remove_background"):
            if pages > 1:
                raise MediaValidationError(
                    "background removal is currently available for static stickers only"
                )
            try:
                from rembg import remove  # type: ignore[import-not-found]
            except ImportError as exc:
                raise MediaValidationError("background removal is unavailable") from exc
            try:
                source = image.write_to_buffer(".png", strip=True)
                removed = remove(source)
                if not isinstance(removed, bytes):
                    raise TypeError("background remover returned a non-byte result")
                image = pyvips.Image.new_from_buffer(removed, "", access="random", fail_on="error")
                width = int(image.width)
                height = int(image.height)
                page_height = height
            except (TypeError, ValueError, pyvips.Error) as exc:
                raise MediaValidationError("background removal failed") from exc
    derivatives: list[Derivative] = []
    encoded_outputs: dict[tuple[int, int], bytes] = {}
    for size in sizes:
        scale = min(1.0, size / max(width, height))
        resized = image.resize(scale) if scale < 1 else image
        resized_page_height = max(1, round(page_height * scale))
        if pages > 1:
            # libvips stores animation frames in one vertical image. resize()
            # scales those pixels but leaves page-height unchanged, causing the
            # encoded thumbnail to render as a tall strip instead of frames.
            resized.set_type(pyvips.GValue.gint_type, "page-height", resized_page_height)
        output_dimensions = (int(resized.width), resized_page_height)
        output = encoded_outputs.get(output_dimensions)
        if output is None:
            # Animated WebP effort 4 spends substantial CPU for a modest size
            # improvement while users wait on the scan gate. Keep visual
            # quality constant and use the faster encoder path for animations.
            output = resized.write_to_buffer(
                ".webp", Q=82, keep="none", effort=2 if pages > 1 else 4
            )
            encoded_outputs[output_dimensions] = output
        derivatives.append(
            Derivative(
                name=f"thumbnail_{size}",
                content=output,
                content_type="image/webp",
                width=output_dimensions[0],
                height=resized_page_height,
            )
        )
    # Hash what people initially see, not libvips' stacked animation sheet.
    preview = _flatten_alpha(image.crop(0, 0, image.width, page_height))
    preview = preview.thumbnail_image(32, height=32, size="down").colourspace("srgb")
    preview = preview.cast("uchar")
    pixels = np.frombuffer(preview.write_to_memory(), dtype=np.uint8).reshape(
        preview.height, preview.width, preview.bands
    )[:, :, :3]
    encoded_blurhash = blurhash.encode(pixels, components_x=4, components_y=3)
    try:
        with Image.open(io.BytesIO(derivatives[0].content)) as pillow_image:
            perceptual_hash = str(imagehash.phash(pillow_image.convert("RGB")))
    except (UnidentifiedImageError, OSError) as exc:
        raise MediaValidationError("generated image derivative was invalid") from exc
    return derivatives, encoded_blurhash, perceptual_hash, width, height


async def video_poster(data: bytes, content_type: str) -> Derivative:
    suffix = ".mp4" if content_type == "video/mp4" else ".webm"
    with tempfile.TemporaryDirectory(prefix="kaede-media-") as directory:
        input_path = Path(directory) / f"input{suffix}"
        output_path = Path(directory) / "poster.webp"
        input_path.write_bytes(data)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe,crypto",
            "-i",
            str(input_path),
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1024,iw)':-2",
            "-c:v",
            "libwebp",
            "-quality",
            "82",
            "-y",
            str(output_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise MediaValidationError("video poster generation timed out") from None
        if process.returncode != 0 or not output_path.exists():
            raise MediaValidationError(
                f"video decoder rejected the upload: {stderr[:120].decode('utf-8', 'replace')}"
            )
        poster = output_path.read_bytes()
    with Image.open(io.BytesIO(poster)) as image:
        width, height = image.size
    return Derivative("poster", poster, "image/webp", width, height)


def content_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
