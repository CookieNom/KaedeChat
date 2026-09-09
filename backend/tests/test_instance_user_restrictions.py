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
async def test_suspension_helper_allowing_join_but_rejecting_message_creation() -> None:
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
async def test_ban_helper_rejecting_creation_and_membership() -> None:
    restriction = SimpleNamespace(restriction_type="banned", expires_at=None)
    session = AsyncMock()
    session.scalar.return_value = restriction

    with pytest.raises(HTTPException) as create_error:
        await require_remote_user_creation_allowed(cast(Any, session), remote_user())
    assert create_error.value.detail["code"] == "USER_BANNED_FROM_INSTANCE"

    with pytest.raises(HTTPException) as join_error:
        await require_remote_user_join_allowed(cast(Any, session), remote_user())
    assert join_error.value.detail == {"code": "USER_BANNED_FROM_INSTANCE"}
