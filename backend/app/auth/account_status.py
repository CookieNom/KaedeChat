from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_


def account_is_suspended(user: Any, *, now: datetime | None = None) -> bool:
    """Return whether an account is permanently disabled or temporarily suspended."""

    current = now or datetime.now(UTC)
    suspended_until = getattr(user, "suspended_until", None)
    return user.disabled_at is not None or (
        suspended_until is not None and suspended_until > current
    )


def account_active_clause(user_model: Any, *, now: datetime | None = None) -> Any:
    """Build the SQL predicate used by authentication and gateway admission."""

    current = now or datetime.now(UTC)
    return user_model.disabled_at.is_(None) & or_(
        user_model.suspended_until.is_(None), user_model.suspended_until <= current
    )
