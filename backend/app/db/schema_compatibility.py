"""Reject missing schema before a new process serves requests or consumes jobs."""

from sqlalchemy import MetaData, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.db import models  # noqa: F401
from app.db.base import Base


async def check_schema(connection: AsyncConnection, metadata: MetaData = Base.metadata) -> None:
    rows = await connection.execute(
        text("""
            SELECT c.relname, a.attname
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
            WHERE n.nspname = current_schema()
              AND a.attnum > 0 AND NOT a.attisdropped
              AND c.relkind IN ('r', 'p')
        """)
    )
    available = {(table, column) for table, column in rows}
    missing = sorted(
        f"{table.name}.{column.name}"
        for table in metadata.tables.values()
        for column in table.columns
        if (table.name, column.name) not in available
    )
    if missing:
        raise RuntimeError(
            "Database schema is missing required columns; apply migrations before starting "
            f"this release: {', '.join(missing[:10])}"
        )


async def assert_schema_compatible(engine: AsyncEngine) -> None:
    # Extra columns from a newer additive release are intentionally accepted,
    # so rolling upgrades and application rollback can share the same database.
    async with engine.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout = '5s'"))
        await check_schema(connection)
