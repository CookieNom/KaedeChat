"""Opt-in PostgreSQL coverage for bulk moderation transaction boundaries.

Run against a disposable, fully migrated PostgreSQL database with::

    KAEDE_BULK_MODERATION_TEST_DATABASE_URL=postgresql+asyncpg://... \
      pytest -q tests/test_bulk_moderation_postgres.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.api.bulk_moderation as bulk_moderation
import app.api.moderation as moderation
from app.api.bulk_moderation import BulkBanRequest, PruneRequest
from app.core.types import EntityRef
from app.db.models import AuditLogEntry, Ban, Guild, GuildMember, Instance, User

DATABASE_URL = os.environ.get("KAEDE_BULK_MODERATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason=(
        "set KAEDE_BULK_MODERATION_TEST_DATABASE_URL to a disposable migrated PostgreSQL database"
    ),
)


class SequentialSnowflake:
    def __init__(self, start: int = 9_900_000) -> None:
        self.value = start

    async def mint(self) -> int:
        self.value += 1
        return self.value


async def cleanup_domain(session: AsyncSession, domain: str) -> None:
    await session.execute(delete(Guild).where(Guild.origin_domain == domain))
    await session.execute(delete(User).where(User.origin_domain == domain))
    await session.execute(delete(Instance).where(Instance.domain == domain))
    await session.commit()


async def seed_guild(
    session: AsyncSession,
    *,
    domain: str,
    guild_id: int,
    actor_id: int,
    user_ids: list[int],
    member_ids: set[int],
) -> tuple[Guild, User]:
    now = datetime.now(UTC)
    session.add(Instance(domain=domain, is_self=False))
    await session.flush()
    users = [
        User(
            id=user_id,
            origin_domain=domain,
            is_local=False,
            account_type="human",
            username=f"bulk_{user_id}",
            federation_introduced_by_domain=domain,
        )
        for user_id in [actor_id, *user_ids]
    ]
    session.add_all(users)
    await session.flush()
    guild = Guild(
        id=guild_id,
        origin_domain=domain,
        name="Bulk moderation transaction test",
        owner_id=actor_id,
        owner_domain=domain,
    )
    members = [
        GuildMember(
            guild_id=guild_id,
            guild_domain=domain,
            user_id=user_id,
            user_domain=domain,
            joined_at=now - timedelta(days=31),
            last_guild_activity_at=now - timedelta(days=31),
        )
        for user_id in {actor_id, *member_ids}
    ]
    session.add_all([guild, *members])
    await session.commit()
    return guild, users[0]


async def allow_permissions(*args: object, **kwargs: object) -> None:
    del args, kwargs


async def noop(*args: object, **kwargs: object) -> None:
    del args, kwargs


@pytest.mark.asyncio
async def test_bulk_ban_commits_success_failure_success_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    domain = "bulk-ban-transaction-it.example"
    guild_id, actor_id = 9_910_000, 9_910_001
    first_id, missing_id, last_id = 9_910_002, 9_910_003, 9_910_004
    published: list[int] = []

    async def capture_postcommit(
        self: moderation.MemberModerationPostCommit,
        session: AsyncSession,
        redis: object,
        snowflake: object,
        settings: object,
    ) -> None:
        del session, redis, snowflake, settings
        async with sessions() as verification:
            count = await verification.scalar(
                select(func.count())
                .select_from(Ban)
                .where(Ban.guild_id == guild_id, Ban.guild_domain == domain)
            )
            assert count == 2, "post-commit projections ran before the outer transaction committed"
        published.append(self.user_id)

    monkeypatch.setattr(moderation, "require_permissions", allow_permissions)
    monkeypatch.setattr(moderation, "queue_guild_mutation", noop)
    monkeypatch.setattr(moderation.MemberModerationPostCommit, "publish", capture_postcommit)
    settings = SimpleNamespace(domain=domain)
    redis = SimpleNamespace()
    snowflake = SequentialSnowflake()

    try:
        async with sessions() as session:
            await cleanup_domain(session, domain)
            guild, actor = await seed_guild(
                session,
                domain=domain,
                guild_id=guild_id,
                actor_id=actor_id,
                user_ids=[first_id, last_id],
                member_ids=set(),
            )
            result = await bulk_moderation._perform_bulk_ban(
                session,
                redis,  # type: ignore[arg-type]
                snowflake,  # type: ignore[arg-type]
                settings,  # type: ignore[arg-type]
                guild,
                SimpleNamespace(user=actor),  # type: ignore[arg-type]
                BulkBanRequest(
                    user_ids=[
                        EntityRef(f"{first_id}@{domain}"),
                        EntityRef(f"{missing_id}@{domain}"),
                        EntityRef(f"{last_id}@{domain}"),
                    ]
                ),
                reason="bulk transaction",
            )

        assert result["banned_users"] == [f"{first_id}@{domain}", f"{last_id}@{domain}"]
        assert result["failed_users"] == [f"{missing_id}@{domain}"]
        assert result["failed_user_details"] == [
            {
                "user_id": f"{missing_id}@{domain}",
                "code": "USER_NOT_FOUND",
                "message": "The user could not be banned.",
            }
        ]
        assert published == [first_id, last_id]
        async with sessions() as session:
            banned_ids = list(
                await session.scalars(
                    select(Ban.user_id)
                    .where(Ban.guild_id == guild_id, Ban.guild_domain == domain)
                    .order_by(Ban.user_id)
                )
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditLogEntry)
                .where(
                    AuditLogEntry.guild_id == guild_id,
                    AuditLogEntry.guild_domain == domain,
                    AuditLogEntry.action_type == 22,
                )
            )
            assert banned_ids == [first_id, last_id]
            assert audit_count == 2
    finally:
        async with sessions() as session:
            await cleanup_domain(session, domain)
        await engine.dispose()


@pytest.mark.asyncio
async def test_prune_summary_audit_failure_rolls_back_every_staged_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    domain = "prune-transaction-it.example"
    guild_id, actor_id = 9_920_000, 9_920_001
    first_id, last_id = 9_920_002, 9_920_003
    published: list[int] = []

    async def candidates(*args: object, **kwargs: object) -> list[tuple[int, str]]:
        del args, kwargs
        return [(first_id, domain), (last_id, domain)]

    async def fail_summary_audit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("injected prune summary audit failure")

    async def capture_postcommit(
        self: moderation.MemberModerationPostCommit,
        *args: object,
        **kwargs: object,
    ) -> None:
        del args, kwargs
        published.append(self.user_id)

    monkeypatch.setattr(moderation, "require_permissions", allow_permissions)
    monkeypatch.setattr(moderation, "queue_guild_access_revocation", noop)
    monkeypatch.setattr(moderation, "queue_guild_mutation", noop)
    monkeypatch.setattr(bulk_moderation, "_prune_candidates", candidates)
    monkeypatch.setattr(bulk_moderation, "add_audit_entry", fail_summary_audit)
    monkeypatch.setattr(moderation.MemberModerationPostCommit, "publish", capture_postcommit)
    settings = SimpleNamespace(domain=domain)
    redis = SimpleNamespace()
    snowflake = SequentialSnowflake(start=9_920_100)

    try:
        async with sessions() as session:
            await cleanup_domain(session, domain)
            guild, actor = await seed_guild(
                session,
                domain=domain,
                guild_id=guild_id,
                actor_id=actor_id,
                user_ids=[first_id, last_id],
                member_ids={first_id, last_id},
            )
            with pytest.raises(RuntimeError, match="injected prune summary audit failure"):
                await bulk_moderation._perform_prune(
                    session,
                    redis,  # type: ignore[arg-type]
                    snowflake,  # type: ignore[arg-type]
                    settings,  # type: ignore[arg-type]
                    guild,
                    SimpleNamespace(user=actor),  # type: ignore[arg-type]
                    PruneRequest(days=30),
                    reason="inactive cleanup",
                )

        assert published == []
        async with sessions() as session:
            remaining_ids = list(
                await session.scalars(
                    select(GuildMember.user_id)
                    .where(
                        GuildMember.guild_id == guild_id,
                        GuildMember.guild_domain == domain,
                        GuildMember.user_id.in_([first_id, last_id]),
                    )
                    .order_by(GuildMember.user_id)
                )
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditLogEntry)
                .where(AuditLogEntry.guild_id == guild_id, AuditLogEntry.guild_domain == domain)
            )
            assert remaining_ids == [first_id, last_id]
            assert audit_count == 0
    finally:
        async with sessions() as session:
            await cleanup_domain(session, domain)
        await engine.dispose()
