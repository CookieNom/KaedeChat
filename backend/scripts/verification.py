"""Consistent, actionable failures for operator-run verification commands."""

from __future__ import annotations

import re

MAX_FAILURE_MESSAGE_LENGTH = 2_000
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_SENSITIVE_QUOTED_FIELD = re.compile(
    r"""(?ix)
    (?P<prefix>
      ["']?
      (?:
        access[_-]?token|refresh[_-]?token|mfa[_-]?ticket|webhook[_-]?token|
        (?:api|client|proxy|admin|private|push)?[_-]?(?:token|secret|password|credential)|
        authorization|private[_-]?key
      )
      ["']?\s*[:=]\s*
    )
    (?P<quote>["'])(?P<value>.*?)(?P=quote)
    """,
)
_SENSITIVE_UNQUOTED_FIELD = re.compile(
    r"""(?ix)
    (?P<prefix>
      \b
      (?:
        access[_-]?token|refresh[_-]?token|mfa[_-]?ticket|webhook[_-]?token|
        (?:api|client|proxy|admin|private|push)?[_-]?(?:token|secret|password|credential)|
        authorization|private[_-]?key
      )
      \s*=\s*
    )
    [^\s,;&}\]]+
    """,
)
_BEARER_CREDENTIAL = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
_CREDENTIAL_URL = re.compile(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@")
_KAEDE_TOKEN = re.compile(r"\bkc1_(?:at|rt|mfa|ot)_[A-Za-z0-9._~-]+")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")


def _sanitize_failure_message(message: str) -> str:
    safe = _PRIVATE_KEY_BLOCK.sub("[private key redacted]", message)
    safe = _SENSITIVE_QUOTED_FIELD.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}[redacted]{match.group('quote')}"
        ),
        safe,
    )
    safe = _SENSITIVE_UNQUOTED_FIELD.sub(lambda match: f"{match.group('prefix')}[redacted]", safe)
    safe = _BEARER_CREDENTIAL.sub("Bearer [redacted]", safe)
    safe = _CREDENTIAL_URL.sub(r"\1[credentials-redacted]@", safe)
    safe = _KAEDE_TOKEN.sub("[Kaede token redacted]", safe)
    safe = _JWT.sub("[token redacted]", safe)
    if len(safe) > MAX_FAILURE_MESSAGE_LENGTH:
        safe = f"{safe[:MAX_FAILURE_MESSAGE_LENGTH]}… [output truncated]"
    return safe


class VerificationFailure(RuntimeError):
    """A verification invariant failed in a way an operator can act on."""

    def __init__(self, message: str) -> None:
        super().__init__(_sanitize_failure_message(message))


def require(condition: object, message: str) -> None:
    if not condition:
        raise VerificationFailure(message)


def failure_message(suite: str, error: VerificationFailure, rerun: str) -> str:
    return (
        f"{suite} verification failed: {error}\n"
        "Inspect the failed service's preceding logs, correct the reported invariant, "
        f"then rerun `{rerun}`."
    )
