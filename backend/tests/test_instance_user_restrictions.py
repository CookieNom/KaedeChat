from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.auth.instance_restrictions import (
    require_remote_user_creation_allowed,
    require_remote_user_join_allowed,
)
from app.db.models import User


def remote_user() -> User:
    return cast(User, SimpleNamespace(id=7, origin_domain="remote.test"))


@pytest.mark.asyncio
async def test_remote_suspension_blocks_guild_message_creation_but_not_join() -> None:
    restriction = SimpleNamespace(
        restriction_type="suspended",
        expires_at=SimpleNamespace(isoformat=lambda: "2026-08-26T00:00:00+00:00"),
    )
    session = AsyncMock()
    session.scalar.return_value = restriction

    with pytest.raises(HTTPException) as raised:
        await require_remote_user_creation_allowed(cast(Any, session), remote_user())
    assert raised.value.detail["code"] == "USER_SUSPENDED_FROM_INSTANCE"

    await require_remote_user_join_allowed(cast(Any, session), remote_user())


@pytest.mark.asyncio
async def test_remote_ban_blocks_message_creation_and_new_guild_membership() -> None:
    restriction = SimpleNamespace(restriction_type="banned", expires_at=None)
    session = AsyncMock()
    session.scalar.return_value = restriction

    with pytest.raises(HTTPException) as create_error:
        await require_remote_user_creation_allowed(cast(Any, session), remote_user())
    assert create_error.value.detail["code"] == "USER_BANNED_FROM_INSTANCE"

    with pytest.raises(HTTPException) as join_error:
        await require_remote_user_join_allowed(cast(Any, session), remote_user())
    assert join_error.value.detail == {"code": "USER_BANNED_FROM_INSTANCE"}
