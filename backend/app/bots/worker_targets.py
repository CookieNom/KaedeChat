from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import or_
from sqlalchemy.sql.elements import ColumnElement

from app.db.bot_models import BotWorker


def worker_target_allowed(
    target_domains: Collection[str],
    *,
    application_domain: str,
    target_domain: str,
) -> bool:
    """Apply the public worker-target contract in one place.

    Workers always run at their application home. An empty target list is the
    documented wildcard for every application-approved runtime target; a
    non-empty list is an explicit allowlist.
    """

    return (
        target_domain == application_domain or not target_domains or target_domain in target_domains
    )


def worker_target_allowed_expression(target_domain: str) -> ColumnElement[bool]:
    """SQL form of :func:`worker_target_allowed` for live authorization."""

    return or_(
        BotWorker.application_domain == target_domain,
        BotWorker.target_domains == [],
        BotWorker.target_domains.contains([target_domain]),
    )
