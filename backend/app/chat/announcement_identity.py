from __future__ import annotations

from typing import Literal

from app.core.types import MAX_SNOWFLAKE
from app.federation.network import normalize_domain

FederatedFollowRole = Literal["source", "target"]
FederatedFollowKey = tuple[int, str, FederatedFollowRole]
FederatedCrosspostKey = tuple[int, str, int, str, FederatedFollowRole]


def qualified_follow_ref(follow_id: int, authority_domain: str) -> str:
    """Return the stable target-authority-owned identity for a follow."""

    if not 1 <= follow_id <= MAX_SNOWFLAKE:
        raise ValueError("announcement follow ID is outside the snowflake range")
    return f"{follow_id}@{normalize_domain(authority_domain)}"


def federated_follow_key(
    follow_id: int,
    authority_domain: str,
    local_role: FederatedFollowRole,
) -> FederatedFollowKey:
    """Build the one canonical ORM key for a federated follow projection."""

    if local_role not in {"source", "target"}:
        raise ValueError("announcement follow local role is invalid")
    qualified = qualified_follow_ref(follow_id, authority_domain)
    _, domain = qualified.split("@", 1)
    return follow_id, domain, local_role


def federated_crosspost_key(
    source_message_id: int,
    source_message_domain: str,
    follow_id: int,
    follow_authority_domain: str,
    local_role: FederatedFollowRole,
) -> FederatedCrosspostKey:
    """Build the collision-safe ORM key for a federated crosspost ledger."""

    follow_key = federated_follow_key(follow_id, follow_authority_domain, local_role)
    if not 1 <= source_message_id <= MAX_SNOWFLAKE:
        raise ValueError("announcement source message ID is outside the snowflake range")
    return (
        source_message_id,
        normalize_domain(source_message_domain),
        *follow_key,
    )
