"""Migration and transaction contracts for retained evidence and client purges."""

import asyncio
import os
from datetime import UTC, datetime
from importlib import import_module
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db.base import Base
from app.safety.models import SafetyClientEvent
from app.safety.service import record_client_event

migration = import_module("migrations.versions.ac4e7f9b2d60_child_safety_cases")


@pytest_asyncio.fixture
async def safety_database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run child-safety database contracts")
    schema = "safety_" + uuid4().hex
    admin = create_async_engine(url, poolclass=NullPool)
    engine = create_async_engine(
        url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE attachments (object_key varchar(512), variants jsonb)")
            )
            await connection.execute(
                text(
                    "CREATE TABLE messages (id bigint PRIMARY KEY, deleted_at timestamptz, "
                    "content text, e2ee jsonb, edited_at timestamptz) PARTITION BY RANGE (id)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE messages_initial PARTITION OF messages "
                    "FOR VALUES FROM (0) TO (1000)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE abuse_reports (category varchar(32), "
                    "CONSTRAINT ck_abuse_reports_abuse_report_category_value "
                    "CHECK (category IN ('illegal_content','other')))"
                )
            )

            def upgrade(sync_connection):
                context = MigrationContext.configure(
                    sync_connection, opts={"target_metadata": Base.metadata}
                )
                with Operations.context(context):
                    migration.upgrade()

            await connection.run_sync(upgrade)
        yield engine
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_retained_message_trigger_covers_existing_and_future_partitions(safety_database):
    async with safety_database.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE messages_future PARTITION OF messages "
                "FOR VALUES FROM (1000) TO (2000)"
            )
        )
        for message_id in (1, 1001):
            await connection.execute(
                text(
                    "INSERT INTO messages (id, safety_state, content) "
                    "VALUES (:id, 'pending_review', 'held')"
                ),
                {"id": message_id},
            )
            for statement in (
                "DELETE FROM messages WHERE id=:id",
                "UPDATE messages SET deleted_at=now() WHERE id=:id",
                "UPDATE messages SET content='replacement' WHERE id=:id",
                "UPDATE messages SET e2ee='{}'::jsonb WHERE id=:id",
            ):
                with pytest.raises(IntegrityError, match="SAFETY_MESSAGE_RETAINED"):
                    async with connection.begin_nested():
                        await connection.execute(text(statement), {"id": message_id})
            # Metadata maintenance is allowed, and dismissing a pending case can restore it.
            await connection.execute(
                text("UPDATE messages SET edited_at=now() WHERE id=:id"), {"id": message_id}
            )
            await connection.execute(
                text("UPDATE messages SET safety_state=NULL, content='restored' WHERE id=:id"),
                {"id": message_id},
            )
            await connection.execute(
                text("UPDATE messages SET safety_state='pending_review', content='' WHERE id=:id"),
                {"id": message_id},
            )
            await connection.execute(
                text("UPDATE messages SET safety_state='removed', content='removed' WHERE id=:id"),
                {"id": message_id},
            )
            for statement in (
                "DELETE FROM messages WHERE id=:id",
                "UPDATE messages SET safety_state=NULL WHERE id=:id",
                "UPDATE messages SET safety_state='pending_review' WHERE id=:id",
                "UPDATE messages SET safety_state='hidden' WHERE id=:id",
                "UPDATE messages SET content='' WHERE id=:id",
                "UPDATE messages SET e2ee='{}'::jsonb WHERE id=:id",
            ):
                with pytest.raises(IntegrityError, match="SAFETY_MESSAGE_RETAINED"):
                    async with connection.begin_nested():
                        await connection.execute(text(statement), {"id": message_id})
        with pytest.raises(IntegrityError, match="SAFETY_MESSAGE_RETAINED"):
            async with connection.begin_nested():
                await connection.execute(
                    text(
                        "INSERT INTO messages (id,safety_state,deleted_at) "
                        "VALUES (2,'pending_review',now())"
                    )
                )
        await connection.execute(text("INSERT INTO abuse_reports (category) VALUES ('csam')"))


async def test_database_enforces_completed_submission_calendar_year(safety_database):
    async with safety_database.begin() as connection:
        insert = text(
            "INSERT INTO child_safety_cases "
            "(id,source,status,details,completed_submission_at,retain_until) "
            "VALUES (1,'user','confirmed',:details,'2024-02-29T12:00:00Z',:deadline)"
        )
        for deadline in (None, datetime(2025, 2, 28, 11, 59, 59, tzinfo=UTC)):
            with pytest.raises(IntegrityError, match="submission_minimum_retention"):
                async with connection.begin_nested():
                    await connection.execute(
                        insert, {"details": b"synthetic", "deadline": deadline}
                    )
        await connection.execute(
            insert, {"details": b"synthetic", "deadline": datetime(2025, 2, 28, 12, tzinfo=UTC)}
        )


async def test_client_feed_serializes_recipient_commits(safety_database, wait_for_postgres_lock):
    async with AsyncSession(safety_database, expire_on_commit=False) as first:
        first_events = await record_client_event(
            first,
            user_refs=[(7, "home.test")],
            message_refs=["1@home.test"],
            attachment_refs=[],
            state="pending_review",
            reason="Under review",
        )

        async def second_commit():
            async with AsyncSession(safety_database, expire_on_commit=False) as second:
                result = await record_client_event(
                    second,
                    user_refs=[(7, "home.test")],
                    message_refs=["1@home.test"],
                    attachment_refs=[],
                    state="removed",
                    reason="Removed",
                )
                await second.commit()
                return result

        task = asyncio.create_task(second_commit())
        try:
            await wait_for_postgres_lock(safety_database, task)
            async with AsyncSession(safety_database) as observer:
                assert list(await observer.scalars(select(SafetyClientEvent))) == []
            await first.commit()
            second_events = await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert int(second_events[0]["sequence"]) > int(first_events[0]["sequence"])
        async with AsyncSession(safety_database) as observer:
            rows = list(
                await observer.scalars(
                    select(SafetyClientEvent).order_by(SafetyClientEvent.sequence)
                )
            )
            assert [row.state for row in rows] == ["pending_review", "removed"]


async def test_removal_receipts_commit_and_workers_skip_locked_cases(safety_database, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.safety import removal
    from app.safety.models import ChildSafetyCase

    settings = SimpleNamespace(
        media_attachments_bucket="media",
        media_derived_bucket="derived",
        media_remote_cache_bucket="remote",
    )
    purge = AsyncMock(return_value=True)
    monkeypatch.setattr(removal, "S3Storage", lambda _: SimpleNamespace(purge_verified=purge))
    async with AsyncSession(safety_database) as session:
        for case_id in (1, 2):
            session.add(
                ChildSafetyCase(
                    id=case_id,
                    source="user",
                    status="confirmed",
                    details=b"synthetic",
                    removal_objects=[
                        {
                            "bucket": "media",
                            "key": f"ordinary/{case_id}",
                            "not_before": "2020-01-01T00:00:00+00:00",
                        }
                    ],
                )
            )
        await session.commit()
    # The receipts have no dependency on an attachment row that cleanup can delete.
    async with AsyncSession(safety_database) as locked:
        await locked.get(ChildSafetyCase, 1, with_for_update=True)
        async with AsyncSession(safety_database) as worker:
            assert await removal.retry_case_removals(worker, settings) == 1
            await worker.commit()
        purge.assert_awaited_once_with("media", "ordinary/2")
        await locked.rollback()
    async with AsyncSession(safety_database) as worker:
        complete = await worker.get(ChildSafetyCase, 2)
        assert complete.removal_completed_at is not None
        assert complete.removal_objects[0]["verified_at"]
        assert await removal.retry_case_removals(worker, settings) == 1
        await worker.commit()
    assert purge.await_count == 2
