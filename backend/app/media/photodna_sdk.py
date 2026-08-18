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

Image.MAX_IMAGE_PIXELS = 25_000_000
MAX_ANIMATION_FRAMES = 256
MAX_ANIMATION_TOTAL_PIXELS = 25_000_000


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
            if image.width * image.height * frames > MAX_ANIMATION_TOTAL_PIXELS:
                raise PhotoDNASDKError("PhotoDNA animation pixel limit exceeded")
            for frame_index in range(frames):
                image.seek(frame_index)
                # Pillow's seek state composites GIF/WebP disposal frames. A
                # concrete converted image keeps the complete displayed frame
                # alive while the native SDK reads its contiguous pixels.
                if image.mode == "RGB":
                    rendered = image.convert("RGB")
                    input_format = edge_hash.PhotoDnaOptions.Rgb
                elif image.mode == "RGBA":
                    rendered = image.convert("RGBA")
                    input_format = edge_hash.PhotoDnaOptions.Rgba
                elif image.mode == "CMYK":
                    rendered = image.convert("CMYK")
                    input_format = edge_hash.PhotoDnaOptions.Cmyk
                elif image.mode in {"LA", "PA", "RGBa"} or "transparency" in image.info:
                    rendered = image.convert("RGBA")
                    input_format = edge_hash.PhotoDnaOptions.Rgba
                else:
                    rendered = image.convert("RGB")
                    input_format = edge_hash.PhotoDnaOptions.Rgb
                if rendered.width < 50 or rendered.height < 50:
                    raise PhotoDNASDKError(
                        "PhotoDNA requires both image dimensions to be at least 50 pixels"
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
