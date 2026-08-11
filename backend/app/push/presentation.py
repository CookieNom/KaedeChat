from __future__ import annotations

from collections.abc import Mapping

from app.db.models import Message


def notification_previews_enabled(preferences: Mapping[str, object]) -> bool:
    """Default previews on while honoring an explicit account opt-out."""

    return bool(preferences.get("show_notification_previews", True))


def push_body(message: Message, has_attachment: bool) -> str:
    if message.e2ee is not None:
        return "Sent an encrypted message"
    content = " ".join((message.content or "").split())
    if content:
        return content[:157] + "..." if len(content) > 160 else content
    if has_attachment:
        return "Sent an attachment"
    return "Sent a message"


def push_presentation(
    *,
    show_preview: bool,
    is_dm: bool,
    is_mention: bool,
    title: str,
    body: str,
) -> tuple[str, str]:
    """Return notification text without exposing message data by default."""

    if show_preview:
        return title, body
    if is_dm:
        private_body = "New direct message"
    elif is_mention:
        private_body = "You were mentioned"
    else:
        private_body = "New guild message"
    return "Kaede Chat", private_body
