from __future__ import annotations

from functools import lru_cache
from typing import Any

import re2  # type: ignore[import-untyped]

MAX_COMPILED_PATTERNS = 512
MAX_REGEX_MEMORY_BYTES = 8 * 1024 * 1024


class UnsafeRegexError(ValueError):
    """Raised when a user pattern is outside Kaede's linear-time grammar."""


def _re2_pattern(pattern: str) -> str:
    # The Python wrapper follows RE2's spelling for the absolute end anchor.
    # Preserve compatibility with patterns accepted by Python's ``re`` API.
    return pattern.replace(r"\Z", r"\z")


@lru_cache(maxsize=MAX_COMPILED_PATTERNS)
def compile_safe_regex(pattern: str) -> Any:
    options = re2.Options()
    options.case_sensitive = False
    options.max_mem = MAX_REGEX_MEMORY_BYTES
    try:
        return re2.compile(_re2_pattern(pattern), options=options)
    except re2.error as exc:
        raise UnsafeRegexError(str(exc)) from exc


def validate_safe_regex(pattern: str) -> None:
    compile_safe_regex(pattern)


def safe_regex_search(pattern: str, value: str) -> bool:
    """Search with RE2, whose execution is linear in the input size."""

    return compile_safe_regex(pattern).search(value) is not None


def safe_regex_matched_text(pattern: str, value: str) -> str | None:
    """Return the bounded text selected by the same RE2 matcher used for admission."""

    match = compile_safe_regex(pattern).search(value)
    return str(match.group(0)) if match is not None else None
