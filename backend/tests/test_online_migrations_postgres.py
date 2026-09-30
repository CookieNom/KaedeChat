"""Exercise the production Alembic runner against an isolated PostgreSQL schema."""

import asyncio
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def test_online_runner_checks_revision_and_recovers_after_lock_timeout(tmp_path, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL contract tests")
    schema = "online_" + uuid4().hex
    engine = create_async_engine(url, poolclass=NullPool)
    scoped = create_async_engine(
        url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
    )
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        versions = tmp_path / "versions"
        versions.mkdir()
        shutil.copyfile(Path(__file__).parents[1] / "migrations/env.py", tmp_path / "env.py")
        for revision, parent, body in (
            (
                "a",
                None,
                'op.create_table("items", sa.Column("id", sa.Integer()))\n'
                '    op.create_table("busy", sa.Column("id", sa.Integer()))',
            ),
            ("b", "a", 'op.add_column("items", sa.Column("note", sa.Text()))'),
            ("c", "b", 'op.add_column("busy", sa.Column("note", sa.Text()))'),
        ):
            (versions / f"{revision}.py").write_text(
                "from alembic import op\nimport sqlalchemy as sa\n"
                f"revision = {revision!r}\ndown_revision = {parent!r}\n"
                f"def upgrade():\n    {body}\n"
            )
        config = Config()
        config.set_main_option("script_location", str(tmp_path))
        monkeypatch.setattr(
            "app.core.settings.get_settings",
            lambda: SimpleNamespace(database_url=SecretStr(url)),
        )
        monkeypatch.setattr(
            "sqlalchemy.ext.asyncio.async_engine_from_config",
            lambda *args, **kwargs: create_async_engine(
                url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}}
            ),
        )
        await asyncio.to_thread(command.upgrade, config, "a")
        config.cmd_opts = SimpleNamespace(x=["online=true", "approved=c"])
        with pytest.raises(RuntimeError, match="approved online migration chain"):
            await asyncio.to_thread(command.upgrade, config, "head")
        async with scoped.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "a"

        config.cmd_opts.x = ["online=true", "approved=b,c"]
        async with scoped.begin() as blocker:
            await blocker.execute(text("LOCK TABLE busy IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(DBAPIError, match="lock timeout"):
                await asyncio.to_thread(command.upgrade, config, "head")
        # The short first migration committed, the blocked second one rolled back.
        # Old writes still work and retry need only apply the remaining revision.
        async with scoped.begin() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "b"
            await connection.execute(text("INSERT INTO items (id) VALUES (1)"))
        await asyncio.to_thread(command.upgrade, config, "head")
        async with scoped.connect() as connection:
            assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "c"
            await connection.execute(text("SELECT note FROM busy"))
        config.cmd_opts.x = ["online=true", "approved="]
        await asyncio.to_thread(command.upgrade, config, "head")
    finally:
        await scoped.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()
