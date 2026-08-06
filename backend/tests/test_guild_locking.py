from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guilds import local_guild
from app.chat.channel_access import ChannelAccess, lock_local_channel_mutation
from app.core.settings import Settings
from app.core.types import EntityReference
from app.db.models import Channel, Guild

DOMAIN = "alpha.localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize(("for_update", "expected"), [(False, False), (True, True)])
async def test_local_guild_optionally_locks_permission_generation(
    for_update: bool,
    expected: bool,
) -> None:
    guild = Guild(
        id=10,
        origin_domain=DOMAIN,
        name="Paper Lantern",
        owner_id=20,
        owner_domain=DOMAIN,
    )
    scalar = AsyncMock(return_value=guild)
    session = cast(AsyncSession, SimpleNamespace(scalar=scalar))
    settings = cast(Settings, SimpleNamespace(domain=DOMAIN))

    result = await local_guild(
        session,
        settings,
        EntityReference(guild.id),
        for_update=for_update,
    )

    statement = scalar.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert ("FOR UPDATE" in sql) is expected
    assert result is guild


@pytest.mark.asyncio
async def test_local_channel_mutation_locks_guild_then_refreshes_channel() -> None:
    guild = Guild(
        id=10,
        origin_domain=DOMAIN,
        name="Paper Lantern",
        owner_id=20,
        owner_domain=DOMAIN,
    )
    channel = Channel(
        id=11,
        origin_domain=DOMAIN,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=11,
    )
    scalar = AsyncMock(side_effect=[guild, channel])
    session = cast(AsyncSession, SimpleNamespace(scalar=scalar))
    settings = cast(Settings, SimpleNamespace(domain=DOMAIN))

    result = await lock_local_channel_mutation(
        session,
        settings,
        ChannelAccess(channel=channel, guild=guild, participants=[]),
    )

    guild_statement = scalar.await_args_list[0].args[0]
    channel_statement = scalar.await_args_list[1].args[0]
    guild_sql = str(guild_statement.compile(dialect=postgresql.dialect()))
    channel_sql = str(channel_statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in guild_sql
    assert "FOR UPDATE" not in channel_sql
    assert guild_statement.get_execution_options()["populate_existing"] is True
    assert channel_statement.get_execution_options()["populate_existing"] is True
    assert result.guild is guild
    assert result.channel is channel


@pytest.mark.asyncio
async def test_remote_guild_channel_mutation_does_not_take_local_lock() -> None:
    remote_domain = "remote.localhost"
    guild = Guild(
        id=10,
        origin_domain=remote_domain,
        name="Remote Lantern",
        owner_id=20,
        owner_domain=remote_domain,
    )
    channel = Channel(
        id=11,
        origin_domain=remote_domain,
        guild_id=guild.id,
        guild_domain=guild.origin_domain,
        type=0,
        name="general",
        created_floor_id=11,
    )
    scalar = AsyncMock()
    session = cast(AsyncSession, SimpleNamespace(scalar=scalar))
    settings = cast(Settings, SimpleNamespace(domain=DOMAIN))
    access = ChannelAccess(channel=channel, guild=guild, participants=[])

    assert await lock_local_channel_mutation(session, settings, access) is access
    scalar.assert_not_awaited()
