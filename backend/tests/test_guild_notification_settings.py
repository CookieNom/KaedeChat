from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import AuthenticatedUser
from app.api.guilds import (
    get_guild_notification_settings,
    put_guild_notification_settings,
)
from app.chat.schemas import GuildNotificationSettingsUpdate
from app.core.settings import Settings
from app.core.types import EntityReference
from app.db.models import GuildMember, GuildNotificationSetting

DOMAIN = "alpha.localhost"


def auth() -> AuthenticatedUser:
    return cast(
        AuthenticatedUser,
        SimpleNamespace(user=SimpleNamespace(id=7, origin_domain=DOMAIN)),
    )


def app_settings() -> Settings:
    return cast(Settings, SimpleNamespace(domain=DOMAIN))


def membership() -> GuildMember:
    return GuildMember(
        guild_id=10,
        guild_domain=DOMAIN,
        user_id=7,
        user_domain=DOMAIN,
        joined_at=datetime.now(UTC),
    )


def test_guild_notification_level_is_closed_to_known_values() -> None:
    assert GuildNotificationSettingsUpdate(level="all").level == "all"
    assert GuildNotificationSettingsUpdate(level="mentions").level == "mentions"
    assert GuildNotificationSettingsUpdate(level="none").level == "none"
    with pytest.raises(ValidationError):
        GuildNotificationSettingsUpdate(level="important")


@pytest.mark.asyncio
async def test_missing_preference_defaults_to_mentions_for_a_member() -> None:
    get = AsyncMock(side_effect=[membership(), None])
    session = cast(AsyncSession, SimpleNamespace(get=get))

    result = await get_guild_notification_settings(
        EntityReference(10), auth(), session, app_settings()
    )

    assert result == {
        "guild_id": "10",
        "guild_domain": DOMAIN,
        "level": "mentions",
    }


@pytest.mark.asyncio
async def test_nonmember_cannot_read_guild_notification_settings() -> None:
    session = cast(AsyncSession, SimpleNamespace(get=AsyncMock(return_value=None)))

    with pytest.raises(HTTPException) as error:
        await get_guild_notification_settings(EntityReference(10), auth(), session, app_settings())

    assert error.value.status_code == 404
    assert error.value.detail == {"code": "GUILD_NOT_FOUND"}


@pytest.mark.asyncio
async def test_preference_write_locks_membership_before_insert() -> None:
    scalar = AsyncMock(return_value=membership())
    get = AsyncMock(return_value=None)
    add = Mock()
    commit = AsyncMock()
    session = cast(
        AsyncSession,
        SimpleNamespace(scalar=scalar, get=get, add=add, commit=commit),
    )

    result = await put_guild_notification_settings(
        EntityReference(10),
        GuildNotificationSettingsUpdate(level="all"),
        auth(),
        session,
        app_settings(),
    )

    statement = scalar.await_args.args[0]
    assert "FOR UPDATE" in str(statement.compile(dialect=postgresql.dialect()))
    inserted = cast(GuildNotificationSetting, add.call_args.args[0])
    assert inserted.user_is_local is True
    assert inserted.level == "all"
    commit.assert_awaited_once()
    assert result["level"] == "all"
