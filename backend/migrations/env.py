from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, pool, text
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.settings import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.migration_filter import is_message_partition, references_message_partition

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())
target_metadata = Base.metadata


def include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    del compare_to
    if not reflected:
        return True
    table_name = (
        name if type_ == "table" else getattr(getattr(object_, "table", None), "name", None)
    )
    if isinstance(table_name, str) and is_message_partition(table_name):
        return False
    return not (type_ == "foreign_key_constraint" and references_message_partition(object_))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    options = context.get_x_argument(as_dictionary=True)
    online = options.get("online") == "true"
    if online:
        # Session lock survives per-revision commits; released on disconnect.
        if not connection.execute(text("SELECT pg_try_advisory_lock(1262572613, 1)")).scalar():
            raise RuntimeError("Another online migration is running")
        connection.execute(text("SET lock_timeout = '1s'"))
        connection.execute(text("SET statement_timeout = '30s'"))
        connection.commit()
    context.configure(
        connection=connection,
        transaction_per_migration=online,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    if online:
        approved = set(filter(None, options.get("approved", "").split(",")))
        current = context.get_context().get_current_heads()
        script = ScriptDirectory.from_config(config)
        pending = {r.revision for r in script.iterate_revisions("heads", current)}
        if not pending <= approved:
            raise RuntimeError("Database revision differs from the approved online migration chain")
        connection.commit()
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
