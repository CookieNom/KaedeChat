from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def account_is_banned(user: Any) -> bool:
    """Return whether this instance has permanently banned a local account."""

    return user.disabled_at is not None


def account_is_temporarily_suspended(user: Any, *, now: datetime | None = None) -> bool:
    """Return whether a local account is currently barred from creating state."""

    current = now or datetime.now(UTC)
    suspended_until = getattr(user, "suspended_until", None)
    return suspended_until is not None and suspended_until > current


def account_is_suspended(user: Any, *, now: datetime | None = None) -> bool:
    """Return whether an account has any active instance restriction.

    Kept as the combined status predicate for status displays and callers that
    intentionally treat both states alike. Authentication must use
    :func:`account_is_banned` instead.
    """

    return account_is_banned(user) or account_is_temporarily_suspended(user, now=now)


def account_active_clause(user_model: Any, *, now: datetime | None = None) -> Any:
    """Build the SQL predicate used by authentication and gateway admission.

    A temporary suspension is a write restriction, not a login restriction.
    """

    del now
    return user_model.disabled_at.is_(None)
