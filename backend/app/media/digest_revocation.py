from __future__ import annotations

from typing import Any, TypeGuard

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

TERMINAL_DIGEST_STATUSES = frozenset({"infected", "quarantined", "rejected"})


def valid_content_digest(digest: object) -> TypeGuard[str]:
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )


def _digest_lock_statement(digest: str, *, try_lock: bool = False) -> Select[tuple[Any]]:
    if not valid_content_digest(digest):
        raise ValueError("public asset digest is invalid")
    lock_key = func.hashtextextended(f"kaede-public-asset-digest:{digest}", 0)
    lock_function = (
        func.pg_try_advisory_xact_lock(lock_key)
        if try_lock
        else func.pg_advisory_xact_lock(lock_key)
    )
    return select(lock_function)


async def lock_asset_digest(session: AsyncSession, digest: str) -> None:
    """Serialize terminal evidence, binding, and cleanup for one digest."""

    await session.scalar(_digest_lock_statement(digest))


async def try_lock_asset_digest(session: AsyncSession, digest: str) -> bool:
    """Take the digest fence without waiting while cleanup holds other locks."""

    return bool(await session.scalar(_digest_lock_statement(digest, try_lock=True)))
