from __future__ import annotations

import unicodedata


def sanitize_single_line_text(value: str | None, *, max_characters: int) -> str | None:
    """Collapse whitespace and remove display-spoofing Unicode controls."""

    if value is None:
        return None
    safe = "".join(
        " " if char.isspace() else "" if unicodedata.category(char) in {"Cc", "Cf"} else char
        for char in value
    )
    cleaned = " ".join(safe.split()).strip()
    return cleaned[:max_characters] or None
