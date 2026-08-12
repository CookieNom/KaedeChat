from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from scripts.verification import VerificationFailure

_URL_PATTERN = re.compile(r"https?://[^\s<>]+")


def token_from_email(text: str) -> str:
    """Extract an action token without depending on the email's prose layout."""
    for candidate in _URL_PATTERN.findall(text):
        values = parse_qs(urlparse(candidate.rstrip(".,;:!?)]}")).fragment).get("token")
        token = values[0] if values else None
        if isinstance(token, str) and token:
            return token
    raise VerificationFailure(
        "captured email did not contain an action token URL; inspect the rendered email template"
    )
