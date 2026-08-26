from __future__ import annotations

import ctypes
import io
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
from PIL import Image

from app.media.photodna_dimensions import (
    MAX_PHOTODNA_HASH_PIXELS,
    MAX_STATIC_SOURCE_PIXELS,
    MIN_NATIVE_IMAGE_DIMENSION,
    normalized_photodna_dimensions,
)

Image.MAX_IMAGE_PIXELS = MAX_STATIC_SOURCE_PIXELS
MAX_ANIMATION_FRAMES = 256
MAX_ANIMATION_TOTAL_PIXELS = MAX_PHOTODNA_HASH_PIXELS


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


class PhotoDNASDKError(RuntimeError):
    pass


def _sdk(root_path: str | None = None) -> tuple[Any, Any]:
    raw_root = root_path or os.environ.get("PHOTODNA_EDGEHASHGENERATOR")
    if not raw_root:
        raise PhotoDNASDKError("PhotoDNA SDK root is not configured")
    root = Path(raw_root)
    python_client = root / "clientlibrary" / "python"
    if not root.is_dir() or not python_client.is_dir():
        raise PhotoDNASDKError("PhotoDNA SDK root is incomplete")
    sys.path.insert(0, str(python_client))
    try:
        from PhotoDna import EdgeHashGenerator  # type: ignore[import-not-found]
    except (ImportError, OSError):
        raise PhotoDNASDKError("PhotoDNA Python client could not be loaded") from None
    try:
        generator = EdgeHashGenerator.PhotoDnaEdgeHashGenerator(
            str(root / "clientlibrary"), 1, False
        )
    except Exception:
        raise PhotoDNASDKError("PhotoDNA native generator could not be initialized") from None
    try:
        error = generator.GetErrorNumber()
    except Exception:
        raise PhotoDNASDKError("PhotoDNA native generator could not be queried") from None
    if error != 0:
        raise PhotoDNASDKError("PhotoDNA native generator reported an initialization error")
    return EdgeHashGenerator, generator


def validate_sdk_installation(root_path: str) -> None:
    """Load the confidential operator SDK and require a compatible 1.05+ ABI."""

    _, generator = _sdk(root_path)
    try:
        major = generator.LibraryVersionMajor()
        minor = generator.LibraryVersionMinor()
    except Exception:
        raise PhotoDNASDKError("PhotoDNA native generator version could not be read") from None
    if major != 1 or minor < 5:
        raise PhotoDNASDKError("PhotoDNA Edge Hash SDK 1.05 or newer is required")


def _hashes(data: bytes) -> list[str]:
    edge_hash, generator = _sdk()
    values: list[str] = []
    try:
        with Image.open(io.BytesIO(data)) as image:
            frames = int(getattr(image, "n_frames", 1))
            if frames < 1 or frames > MAX_ANIMATION_FRAMES:
                raise PhotoDNASDKError("PhotoDNA animation frame limit exceeded")
            source_pixels = image.width * image.height
            if frames == 1 and source_pixels > MAX_STATIC_SOURCE_PIXELS:
                raise PhotoDNASDKError("PhotoDNA image pixel limit exceeded")
            if frames > 1 and source_pixels * frames > MAX_ANIMATION_TOTAL_PIXELS:
                raise PhotoDNASDKError("PhotoDNA animation pixel limit exceeded")
            if (
                image.width < MIN_NATIVE_IMAGE_DIMENSION
                or image.height < MIN_NATIVE_IMAGE_DIMENSION
            ):
                raise PhotoDNASDKError(
                    "PhotoDNA requires both image dimensions to be at least 50 pixels"
                )
            try:
                normalized_width, normalized_height = normalized_photodna_dimensions(
                    image.width, image.height
                )
            except ValueError:
                raise PhotoDNASDKError("PhotoDNA normalized image pixel limit exceeded") from None
            if normalized_width * normalized_height * frames > MAX_ANIMATION_TOTAL_PIXELS:
                raise PhotoDNASDKError("PhotoDNA normalized image pixel limit exceeded")
            source_width, source_height = image.width, image.height
            downscaling = normalized_width < source_width or normalized_height < source_height
            if downscaling and frames == 1:
                # JPEG can select a lower-resolution decoder output before
                # Pillow materializes pixels. This is only a resource
                # optimization: resize still produces the exact bounded,
                # uncropped full-frame dimensions below.
                image.draft(image.mode, (normalized_width, normalized_height))
            for frame_index in range(frames):
                image.seek(frame_index)
                source = (
                    image.resize(
                        (normalized_width, normalized_height),
                        Image.Resampling.LANCZOS,
                        reducing_gap=3.0,
                    )
                    if downscaling
                    else image
                )
                # Pillow's seek state composites GIF/WebP disposal frames. A
                # concrete converted image keeps the complete displayed frame
                # alive while the native SDK reads its contiguous pixels.
                if source.mode == "RGB":
                    rendered = source.convert("RGB")
                    input_format = edge_hash.PhotoDnaOptions.Rgb
                elif source.mode == "RGBA":
                    rendered = source.convert("RGBA")
                    input_format = edge_hash.PhotoDnaOptions.Rgba
                elif source.mode == "CMYK":
                    rendered = source.convert("CMYK")
                    input_format = edge_hash.PhotoDnaOptions.Cmyk
                elif source.mode in {"LA", "PA", "RGBa"} or "transparency" in image.info:
                    rendered = source.convert("RGBA")
                    input_format = edge_hash.PhotoDnaOptions.Rgba
                else:
                    rendered = source.convert("RGB")
                    input_format = edge_hash.PhotoDnaOptions.Rgb
                if (rendered.width, rendered.height) != (
                    normalized_width,
                    normalized_height,
                ):
                    # This remaining branch is the small-image upscale. Large
                    # images were reduced before conversion so their temporary
                    # contiguous buffer also stays inside the hash budget.
                    rendered = rendered.resize(
                        (normalized_width, normalized_height), Image.Resampling.LANCZOS
                    )
                pixels = np.ascontiguousarray(np.asarray(rendered))
                options = input_format | edge_hash.PhotoDnaOptions.HashFormatEdgeV2Base64
                output = ctypes.create_string_buffer(
                    b"", size=edge_hash.HashSize.EdgeV2Base64.value
                )
                try:
                    result = generator.PhotoDnaEdgeHash(
                        pixels,
                        output,
                        pixels.shape[1],
                        pixels.shape[0],
                        0,
                        options,
                    )
                except Exception:
                    raise PhotoDNASDKError(
                        "PhotoDNA native generator could not hash the image"
                    ) from None
                if result != 0:
                    raise PhotoDNASDKError("PhotoDNA native generator could not hash the image")
                try:
                    values.append(output.value.decode("ascii"))
                except UnicodeDecodeError:
                    raise PhotoDNASDKError(
                        "PhotoDNA native generator returned invalid output"
                    ) from None
    except PhotoDNASDKError:
        raise
    except Image.DecompressionBombError:
        raise PhotoDNASDKError("PhotoDNA image pixel limit exceeded") from None
    except (OSError, ValueError):
        raise PhotoDNASDKError("PhotoDNA input was not a supported image") from None
    if not values:
        raise PhotoDNASDKError("PhotoDNA input contained no image frames")
    return values


def _hash(data: bytes) -> str:
    values = _hashes(data)
    if len(values) != 1:
        raise PhotoDNASDKError("PhotoDNA still-image hashing received an animation")
    return values[0]


def main() -> None:
    try:
        values = _hashes(sys.stdin.buffer.read())
    except PhotoDNASDKError as exc:
        _fail(str(exc))
    json.dump({"DataRepresentation": "PreHashV2", "Values": values}, sys.stdout)


if __name__ == "__main__":
    main()
