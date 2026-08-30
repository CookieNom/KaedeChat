from __future__ import annotations

from datetime import UTC, datetime

from app.core.text import sanitize_single_line_text
from app.db.models import GuildMember
from app.federation.schemas import GuildSelfModerationStatus


def sanitize_timeout_reason(value: str | None) -> str | None:
    """Return a bounded, single-line reason suitable for the affected user."""

    if value is None:
        return None
    # Unicode format controls (including bidi overrides) are unsafe in text
    # relayed between operators even though UI markup escaping is in place.
    return sanitize_single_line_text(value, max_characters=512)


def member_timeout_error_detail(
    member: GuildMember,
    *,
    now: datetime | None = None,
) -> dict[str, object] | None:
    """Return the shared user-facing denial for an active guild timeout."""

    current = now or datetime.now(UTC)
    timeout_until = member.timeout_until
    active = bool(member.timeout_indefinite) or bool(
        timeout_until is not None and timeout_until > current
    )
    if not active:
        return None
    return {
        "code": "MEMBER_TIMED_OUT",
        "message": "You are currently timed out in this guild.",
        "timeout_until": timeout_until.isoformat() if timeout_until is not None else None,
        "timeout_indefinite": bool(member.timeout_indefinite),
        "reason": sanitize_timeout_reason(member.timeout_reason),
    }


def guild_self_moderation_status(
    member: GuildMember,
    *,
    now: datetime | None = None,
) -> GuildSelfModerationStatus:
    """Project private moderation state for exactly the affected member.

    This projection must never be embedded in guild snapshots, member chunks,
    or guild-topic dispatches. It is safe only on an authenticated self-status
    response (local or signed user-home-to-guild-home request).
    """

    current = (now or datetime.now(UTC)).astimezone(UTC)
    indefinite = bool(member.timeout_indefinite)
    until = member.timeout_until
    if until is not None:
        until = until.astimezone(UTC)
    active = indefinite or bool(until is not None and until > current)
    return GuildSelfModerationStatus(
        guild_id=str(member.guild_id),
        guild_domain=member.guild_domain,
        timed_out=active,
        timeout_until=until if active and not indefinite else None,
        timeout_indefinite=indefinite if active else False,
        reason=sanitize_timeout_reason(member.timeout_reason) if active else None,
    )
