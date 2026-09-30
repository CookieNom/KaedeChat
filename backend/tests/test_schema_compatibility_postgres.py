import pytest
from sqlalchemy import Column, Integer, MetaData, Table, text

from app.db.schema_compatibility import check_schema


async def test_startup_requires_columns_but_accepts_additive_schema(postgres_schema):
    metadata = MetaData()
    Table("startup_contract", metadata, Column("id", Integer), Column("new_field", Integer))
    with pytest.raises(RuntimeError, match="startup_contract.id"):
        await check_schema(postgres_schema, metadata)
    await postgres_schema.execute(text("CREATE TABLE startup_contract (id integer)"))
    with pytest.raises(RuntimeError, match="startup_contract.new_field"):
        await check_schema(postgres_schema, metadata)
    await postgres_schema.execute(text("ALTER TABLE startup_contract ADD COLUMN new_field integer"))
    await postgres_schema.execute(
        text("ALTER TABLE startup_contract ADD COLUMN future_field integer")
    )
    await check_schema(postgres_schema, metadata)
