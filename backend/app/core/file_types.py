from __future__ import annotations

import mimetypes
import re

MEDIA_FILE_TYPE_FILTERS = frozenset({"image", "video", "audio"})
FILE_EXTENSION_FILTER_RE = re.compile(r"\.[a-z0-9][a-z0-9._+-]{0,63}")


def normalize_file_types(values: list[str]) -> list[str]:
    """Normalize Discord-style upload filters and reject ambiguous duplicates."""

    normalized = [value.lower() for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError("file types must be unique")
    for value in normalized:
        if (
            value not in MEDIA_FILE_TYPE_FILTERS
            and FILE_EXTENSION_FILTER_RE.fullmatch(value) is None
        ):
            raise ValueError("file types must be image, video, audio, or a dot extension")
    return normalized


def attachment_matches_file_types(
    *,
    filename: str,
    content_type: str,
    file_types: list[str],
) -> bool:
    """Match Discord's filename-extension-only upload filter semantics.

    The declared media type is intentionally not authoritative here: a client
    cannot make ``payload.exe`` satisfy the ``image`` filter merely by claiming
    ``image/png``.  The argument remains part of the helper contract because
    callers also use it for independent media validation.
    """

    if not file_types:
        return True
    lowered_name = filename.lower()
    guessed_type, _ = mimetypes.guess_type(lowered_name, strict=False)
    extension_media_type = guessed_type.partition("/")[0] if guessed_type is not None else ""
    return any(
        candidate == extension_media_type
        if candidate in MEDIA_FILE_TYPE_FILTERS
        else lowered_name.endswith(candidate)
        for candidate in file_types
    )
