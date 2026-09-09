"""Shared disposable PostgreSQL schema for tests that execute database contracts."""

import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def pytest_collection_modifyitems(items):
    # Keep CI's database selection in sync with fixtures, including their dependencies.
    for item in items:
        if "postgres_schema" in item.fixturenames or item.path.name.endswith("_postgres.py"):
            item.add_marker(pytest.mark.postgres)


@pytest_asyncio.fixture
async def postgres_schema():
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
        "KAEDE_REACTION_MIGRATION_TEST_DATABASE_URL"
    )
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL contract tests")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                schema = f"contract_{uuid4().hex}"
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
                await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
                yield connection
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
def migrate_to(postgres_schema):
    """Build a historical schema using the actual revision chain in this transaction."""
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from alembic.script import ScriptDirectory

    from app.db.base import Base

    scripts = ScriptDirectory(str(Path(__file__).parents[1] / "migrations"))

    async def migrate(revision: str):
        def upgrade(connection):
            context = MigrationContext.configure(
                connection, opts={"target_metadata": Base.metadata}
            )
            with Operations.context(context):
                for step in reversed(list(scripts.walk_revisions("base", revision))):
                    step.module.upgrade()

        await postgres_schema.run_sync(upgrade)
        return postgres_schema

    return migrate


@pytest_asyncio.fixture
async def redis_scope() -> AsyncIterator[tuple[Redis, str]]:
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL requires an isolated Redis/Dragonfly test instance")
    client = Redis.from_url(url, decode_responses=True)
    domain = f"test-{uuid4().hex}.example"
    await client.ping()
    try:
        yield client, domain
    finally:
        keys = [key async for key in client.scan_iter(match=f"*{domain}*")]
        if keys:
            await client.delete(*keys)
        await client.zrem("presence:expirations", f"{domain}:7")
        await client.aclose()


@pytest.fixture
def wait_for_postgres_lock():
    """Observe the competing query at the driver and its actual PostgreSQL blocker."""
    import asyncio

    from sqlalchemy import event

    async def wait(engine, task):
        started = asyncio.Event()
        backend_pid = None

        def query_started(connection, _cursor, statement, _parameters, _context, _many):
            nonlocal backend_pid
            if asyncio.current_task() is task and "FOR UPDATE" in statement:
                backend_pid = connection.connection.driver_connection.get_server_pid()
                started.set()

        event.listen(engine.sync_engine, "before_cursor_execute", query_started)
        try:
            async with asyncio.timeout(5):
                await started.wait()
                async with engine.connect() as observer:
                    while not await observer.scalar(
                        text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"), {"pid": backend_pid}
                    ):
                        assert not task.done(), "competing operation completed without blocking"
                        await asyncio.sleep(0.01)
                assert not task.done()
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", query_started)

    return wait


@pytest.fixture
async def media_cleanup_session(postgres_schema):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    for table in (
        "attachments",
        "media_tombstone_sources",
        "remote_media_cache",
        "media_tombstone_destinations",
        "federation_outbox",
        "federation_events",
        "guild_events",
        "guild_history_staged_messages",
        "remote_media_tombstones",
        "federation_inbox",
    ):
        await postgres_schema.execute(
            text(f"CREATE TABLE {table} AS TABLE public.{table} WITH NO DATA")
        )
    async with AsyncSession(bind=postgres_schema, expire_on_commit=False) as session:
        yield session
