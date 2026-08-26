"""Opt-in PostgreSQL integration coverage for tracker storage invariants.

Run against a disposable, fully migrated PostgreSQL database with::

    KAEDE_TRACKER_TEST_DATABASE_URL=postgresql+asyncpg://... \
      pytest -q tests/test_tracker_postgres.py
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Channel,
    Guild,
    GuildMember,
    Instance,
    TrackerBoard,
    TrackerDispatchOutbox,
    TrackerLane,
    TrackerTask,
    User,
)

DATABASE_URL = os.environ.get("KAEDE_TRACKER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    DATABASE_URL is None,
    reason="set KAEDE_TRACKER_TEST_DATABASE_URL to a disposable migrated PostgreSQL database",
)


@pytest.mark.asyncio
async def test_tracker_postgresql_constraints_and_cascades() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    domain = "tracker-storage-it.example"
    guild_id, channel_id = 8_810_001, 8_810_010
    creator_id, assignee_id = 8_810_002, 8_810_003
    lane_a_id, lane_b_id = 8_810_011, 8_810_012
    task_a_id, task_b_id = 8_810_021, 8_810_022
    now = datetime.now(UTC)

    try:
        async with sessions() as session:
            # Make reruns safe after an interrupted prior invocation.
            await session.execute(delete(Guild).where(Guild.origin_domain == domain))
            await session.execute(delete(User).where(User.origin_domain == domain))
            await session.execute(delete(Instance).where(Instance.domain == domain))
            await session.commit()

            instance = Instance(domain=domain, is_self=False)
            creator = User(
                id=creator_id,
                origin_domain=domain,
                is_local=False,
                username="tracker.creator",
                account_type="human",
                federation_introduced_by_domain=domain,
            )
            assignee = User(
                id=assignee_id,
                origin_domain=domain,
                is_local=False,
                username="tracker.assignee",
                account_type="human",
                federation_introduced_by_domain=domain,
            )
            session.add(instance)
            await session.flush()
            session.add_all([creator, assignee])
            await session.flush()

            guild = Guild(
                id=guild_id,
                origin_domain=domain,
                name="Tracker storage integration",
                owner_id=creator_id,
                owner_domain=domain,
            )
            creator_member = GuildMember(
                guild_id=guild_id,
                guild_domain=domain,
                user_id=creator_id,
                user_domain=domain,
                joined_at=now,
            )
            assignee_member = GuildMember(
                guild_id=guild_id,
                guild_domain=domain,
                user_id=assignee_id,
                user_domain=domain,
                joined_at=now,
            )
            # Guild ownership is a deferred circular FK to guild_members.
            session.add_all([guild, creator_member, assignee_member])
            await session.flush()

            channel = Channel(
                id=channel_id,
                origin_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                type=17,
                name="Production tracker",
                created_floor_id=channel_id,
            )
            board = TrackerBoard(
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                key_prefix="IT",
                next_task_number=3,
            )
            session.add(channel)
            await session.flush()
            session.add(board)
            await session.flush()

            lane_a = TrackerLane(
                id=lane_a_id,
                origin_domain=domain,
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                name="Backlog",
                kind="backlog",
                color=0,
                completed=False,
                position=0,
            )
            lane_b = TrackerLane(
                id=lane_b_id,
                origin_domain=domain,
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                name="Done",
                kind="completed",
                color=0,
                completed=True,
                position=1,
            )
            session.add_all([lane_a, lane_b])
            await session.flush()
            task_a = TrackerTask(
                id=task_a_id,
                origin_domain=domain,
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                lane_id=lane_a_id,
                lane_domain=domain,
                number=1,
                title="First",
                priority="high",
                position=0,
                creator_id=creator_id,
                creator_domain=domain,
                assignee_id=assignee_id,
                assignee_domain=domain,
                client_nonce="integration-nonce",
                client_request_hash="a" * 64,
            )
            task_b = TrackerTask(
                id=task_b_id,
                origin_domain=domain,
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                lane_id=lane_a_id,
                lane_domain=domain,
                number=2,
                title="Second",
                priority="none",
                position=1,
                creator_id=creator_id,
                creator_domain=domain,
            )
            outbox = TrackerDispatchOutbox(
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                event_type="TRACKER_TASK_CREATE",
                payload={"task_id": str(task_a_id), "version": now.isoformat()},
            )
            session.add_all([task_a, task_b, outbox])
            await session.commit()

            # Both order constraints are DEFERRABLE INITIALLY DEFERRED: direct
            # swaps temporarily collide but are valid by transaction commit.
            lane_a.position, lane_b.position = 1, 0
            task_a.position, task_b.position = 1, 0
            await session.commit()
            assert [
                item.id
                for item in await session.scalars(
                    select(TrackerLane)
                    .where(TrackerLane.channel_id == channel_id)
                    .order_by(TrackerLane.position)
                )
            ] == [lane_b_id, lane_a_id]
            assert [
                item.id
                for item in await session.scalars(
                    select(TrackerTask)
                    .where(TrackerTask.channel_id == channel_id)
                    .order_by(TrackerTask.position)
                )
            ] == [task_b_id, task_a_id]

            # The nonce is scoped to channel + exact creator identity and is
            # enforced by PostgreSQL, not only by the service preflight query.
            duplicate = TrackerTask(
                id=8_810_023,
                origin_domain=domain,
                channel_id=channel_id,
                channel_domain=domain,
                guild_id=guild_id,
                guild_domain=domain,
                lane_id=lane_b_id,
                lane_domain=domain,
                number=3,
                title="Duplicate nonce",
                priority="none",
                position=0,
                creator_id=creator_id,
                creator_domain=domain,
                client_nonce="integration-nonce",
                client_request_hash="a" * 64,
            )
            savepoint = await session.begin_nested()
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.flush()
            await savepoint.rollback()

            # A committed outbox survives a new session/transaction.
            await session.commit()
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrackerDispatchOutbox)
                    .where(
                        TrackerDispatchOutbox.channel_id == channel_id,
                        TrackerDispatchOutbox.channel_domain == domain,
                    )
                )
                == 1
            )
            loaded_task = await session.get(TrackerTask, (task_a_id, domain))
            loaded_assignee_member = await session.get(
                GuildMember, (guild_id, domain, assignee_id, domain)
            )
            assert loaded_task is not None and loaded_assignee_member is not None
            await session.delete(loaded_assignee_member)
            await session.commit()
            await session.refresh(loaded_task)
            assert loaded_task.assignee_id is None
            assert loaded_task.assignee_domain is None

            # Channel deletion cascades the normalized board, lanes, tasks,
            # and any still-pending gateway outbox rows atomically.
            loaded_channel = await session.get(Channel, (channel_id, domain))
            assert loaded_channel is not None
            await session.delete(loaded_channel)
            await session.commit()
            assert await session.get(TrackerBoard, (channel_id, domain)) is None
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrackerLane)
                    .where(
                        TrackerLane.channel_id == channel_id, TrackerLane.channel_domain == domain
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrackerTask)
                    .where(
                        TrackerTask.channel_id == channel_id, TrackerTask.channel_domain == domain
                    )
                )
                == 0
            )
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(TrackerDispatchOutbox)
                    .where(
                        TrackerDispatchOutbox.channel_id == channel_id,
                        TrackerDispatchOutbox.channel_domain == domain,
                    )
                )
                == 0
            )
    finally:
        async with sessions() as session:
            loaded_guild = await session.get(Guild, (guild_id, domain))
            if loaded_guild is not None:
                await session.delete(loaded_guild)
                await session.flush()
            await session.execute(delete(User).where(User.origin_domain == domain))
            await session.execute(delete(Instance).where(Instance.domain == domain))
            await session.commit()
        await engine.dispose()
