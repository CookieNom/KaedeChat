from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.chat.e2ee_membership import (
    GUILD_E2EE_ACCESS_MUTATION_EVENTS,
    e2ee_policy_destinations,
    pause_guild_e2ee_for_membership_change,
    remote_e2ee_authorities_for_user,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(domain="alpha.localhost")


@pytest.mark.asyncio
async def test_room_policy_fans_out_once_to_every_remote_member_home() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(
        return_value=[
            "beta.localhost",
            "gamma.localhost",
            "beta.localhost",
        ]
    )
    guild_channel = SimpleNamespace(
        id=10,
        origin_domain="alpha.localhost",
        guild_id=20,
        guild_domain="alpha.localhost",
    )

    destinations = await e2ee_policy_destinations(session, _settings(), guild_channel)

    assert destinations == {"beta.localhost", "gamma.localhost"}


@pytest.mark.asyncio
async def test_remote_device_change_reaches_all_authorities_across_three_homes() -> None:
    session = MagicMock()
    session.scalars = AsyncMock(
        side_effect=[
            ["beta.localhost", "gamma.localhost", "beta.localhost"],
            ["gamma.localhost", "delta.localhost"],
        ]
    )
    user = SimpleNamespace(id=7, origin_domain="alpha.localhost")

    authorities = await remote_e2ee_authorities_for_user(session, _settings(), user)

    assert authorities == {
        "beta.localhost",
        "gamma.localhost",
        "delta.localhost",
    }


@pytest.mark.asyncio
async def test_every_guild_access_change_pauses_all_active_encrypted_channels() -> None:
    first = SimpleNamespace(encryption_state="active")
    second = SimpleNamespace(encryption_state="active")
    session = MagicMock()
    session.scalars = AsyncMock(return_value=[first, second])
    guild = SimpleNamespace(id=20, origin_domain="alpha.localhost")

    paused = await pause_guild_e2ee_for_membership_change(session, guild)

    assert paused == [first, second]
    assert first.encryption_state == second.encryption_state == "rekeying"
    assert {
        "guild.member.add",
        "guild.member.remove",
        "guild.members.origin.remove",
        "guild.member.role.add",
        "guild.member.role.remove",
        "guild.role.update",
        "guild.role.delete",
        "guild.overwrite.upsert",
        "guild.overwrite.delete",
    } == GUILD_E2EE_ACCESS_MUTATION_EVENTS
