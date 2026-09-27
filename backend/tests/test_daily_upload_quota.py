from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User, UserDailyUploadUsage
from app.media.service import reserve_daily_upload


async def test_daily_quota_unlimited_and_oversized_upload() -> None:
    session = SimpleNamespace(scalar=AsyncMock())
    configured = SimpleNamespace(media_daily_upload_quota_bytes=0)
    user = User(id=1, origin_domain="local.example")
    await reserve_daily_upload(session, configured, user, 100)
    configured.media_daily_upload_quota_bytes = 99
    with pytest.raises(HTTPException) as error:
        await reserve_daily_upload(session, configured, user, 100)
    assert error.value.detail == {"code": "DAILY_UPLOAD_QUOTA_EXCEEDED"}
    session.scalar.assert_not_awaited()


async def test_daily_quota_reservations_reset_and_rollback(postgres_schema, monkeypatch) -> None:
    now = datetime(2026, 9, 27, 23, 59, tzinfo=UTC)
    monkeypatch.setattr("app.media.service.datetime", SimpleNamespace(now=lambda _: now))
    # Run the actual migration against the minimal referenced identity schema.
    await postgres_schema.execute(
        text(
            "CREATE TABLE users (id BIGINT, origin_domain VARCHAR(253), "
            "PRIMARY KEY (id, origin_domain))"
        )
    )
    await postgres_schema.execute(
        text("INSERT INTO users VALUES (1, 'local.example'), (1, 'remote.example')")
    )
    scripts = ScriptDirectory(str(Path(__file__).parents[1] / "migrations"))
    revision = scripts.get_revision("8d3f0b2c5e71")

    def migrate(connection, direction):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(revision.module, direction)()

    await postgres_schema.run_sync(migrate, "upgrade")
    configured = SimpleNamespace(media_daily_upload_quota_bytes=100)
    user = User(id=1, origin_domain="local.example")
    remote = User(id=1, origin_domain="remote.example")
    async with AsyncSession(bind=postgres_schema) as session:
        await reserve_daily_upload(session, configured, user, 60)
        await reserve_daily_upload(session, configured, user, 40)
        with pytest.raises(HTTPException) as error:
            await reserve_daily_upload(session, configured, user, 1)
        assert error.value.detail == {"code": "DAILY_UPLOAD_QUOTA_EXCEEDED"}
        # Identities with the same numeric ID on different servers are independent.
        await reserve_daily_upload(session, configured, remote, 100)
        now += timedelta(days=1)
        await reserve_daily_upload(session, configured, user, 80)
        query = select(UserDailyUploadUsage.bytes_used).where(
            UserDailyUploadUsage.user_domain == user.origin_domain
        )
        assert await session.scalar(query) == 80
        # Failed ticket transactions do not consume the allowance.
        transaction = await session.begin_nested()
        await reserve_daily_upload(session, configured, user, 20)
        await transaction.rollback()
        assert await session.scalar(query) == 80
        await reserve_daily_upload(session, configured, user, 20)
        assert await session.scalar(query) == 100
        # A delayed request from yesterday cannot reset today's consumed quota.
        now -= timedelta(days=1)
        with pytest.raises(HTTPException):
            await reserve_daily_upload(session, configured, user, 1)
        assert await session.scalar(query) == 100
        await session.commit()
    await postgres_schema.run_sync(migrate, "downgrade")
