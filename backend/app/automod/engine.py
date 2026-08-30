from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.automod.safe_regex import safe_regex_matched_text

MAX_EVALUATED_CHARACTERS = 4_000

# These are intentionally operator-maintained and conservative. Deployments
# can localize/extend them without changing the wire-level preset identifiers.
PRESET_PATTERNS: dict[str, tuple[str, ...]] = {
    "profanity": (
        "asshole",
        "bastard",
        "bitch",
        "bullshit",
        "damn",
        "fuck",
        "motherfucker",
        "piss",
        "shit",
    ),
    "sexual_content": (
        "explicit sexual",
        "nudes",
        "porn",
        "pornography",
        "sex video",
        "sexual content",
    ),
    "slurs": (
        "chink",
        "faggot",
        "kike",
        "nigger",
        "retard",
        "spic",
        "tranny",
    ),
}


@dataclass(frozen=True, slots=True)
class MatchResult:
    matched: bool
    keyword: str | None = None
    kind: str | None = None
    matched_content: str | None = None


def normalize_content(value: str) -> str:
    return unicodedata.normalize("NFKC", value[:MAX_EVALUATED_CHARACTERS]).casefold()


def _keyword_pattern(value: str) -> re.Pattern[str] | None:
    """Compile Discord-style edge wildcards with whitespace word boundaries."""

    keyword = normalize_content(value).strip()
    if not keyword or not keyword.strip("*"):
        return None
    leading = keyword.startswith("*")
    trailing = keyword.endswith("*")
    core = keyword[1:] if leading else keyword
    core = core[:-1] if trailing else core
    # Discord documents only prefix/suffix wildcards. An asterisk in the
    # middle is ordinary keyword text, not an unbounded regex fragment.
    translated = re.escape(core)
    translated = r"\S*" + translated if leading else r"(?<!\S)" + translated
    if trailing:
        translated += r"\S*"
    else:
        translated += r"(?!\S)"
    return re.compile(translated, flags=re.IGNORECASE)


def _mask_allowed(content: str, allow_list: list[str]) -> str:
    if not allow_list:
        return content
    masked = list(content)
    for item in allow_list:
        pattern = _keyword_pattern(item)
        if pattern is None:
            continue
        for match in pattern.finditer(content):
            masked[match.start() : match.end()] = " " * (match.end() - match.start())
    return "".join(masked)


def _keyword_matched_text(content: str, keyword: str) -> str | None:
    pattern = _keyword_pattern(keyword)
    if pattern is None:
        return None
    match = pattern.search(content)
    return match.group(0) if match is not None else None


def evaluate_trigger(
    trigger_type: str,
    metadata: dict[str, Any],
    content: str,
    *,
    mention_count: int = 0,
) -> MatchResult:
    source_content = content[:MAX_EVALUATED_CHARACTERS]
    normalized = normalize_content(source_content)
    allow_list = [str(item) for item in metadata.get("allow_list", [])]
    candidate_content = _mask_allowed(normalized, allow_list)
    candidate_source = _mask_allowed(source_content, allow_list)
    if trigger_type in {"keyword", "member_profile"}:
        for keyword in metadata.get("keyword_filter", []):
            matched_content = _keyword_matched_text(candidate_content, str(keyword))
            if matched_content is not None:
                return MatchResult(
                    True,
                    str(keyword),
                    "keyword",
                    _keyword_matched_text(candidate_source, str(keyword)) or source_content,
                )
        for pattern in metadata.get("regex_patterns", []):
            matched_content = safe_regex_matched_text(str(pattern), candidate_content)
            if matched_content is not None:
                return MatchResult(
                    True,
                    str(pattern),
                    "regex",
                    safe_regex_matched_text(str(pattern), candidate_source) or source_content,
                )
        return MatchResult(False)
    if trigger_type == "keyword_preset":
        for preset in metadata.get("presets", []):
            for keyword in PRESET_PATTERNS.get(str(preset), ()):
                matched_content = _keyword_matched_text(candidate_content, keyword)
                if matched_content is not None:
                    return MatchResult(
                        True,
                        keyword,
                        f"preset:{preset}",
                        _keyword_matched_text(candidate_source, keyword) or source_content,
                    )
        return MatchResult(False)
    if trigger_type == "mention_spam":
        limit = int(metadata.get("mention_total_limit", 50))
        return MatchResult(mention_count > limit, str(limit), "mention_spam")
    if trigger_type == "spam":
        words = re.findall(r"\w+", normalized)
        repeated = max(Counter(words).values(), default=0)
        link_count = len(re.findall(r"https?://", normalized))
        excessive_repetition = bool(re.search(r"(.)\1{14,}", normalized))
        matched = repeated >= 8 or link_count >= 6 or excessive_repetition
        return MatchResult(matched, None, "spam" if matched else None)
    return MatchResult(False)
