from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.chat import permissions as permission_service
from app.chat.permissions import PermissionOverwrite, require_permissions, resolve_permissions
from app.core.permissions import ALL_PERMISSIONS, Permission

DOMAIN = "alpha.localhost"


def resolve(
    *,
    base: Permission,
    overwrites: list[PermissionOverwrite] | None = None,
    roles: set[tuple[int, str]] | None = None,
    owner: bool = False,
    timed_out: bool = False,
) -> int:
    return resolve_permissions(
        owner=owner,
        user_id=22,
        user_domain=DOMAIN,
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids=roles or {(10, DOMAIN)},
        base_permissions=int(base),
        overwrites=overwrites or [],
        channel_type=0,
        timed_out=timed_out,
    )


def overwrite(
    target_id: int,
    target_type: str,
    *,
    allow: Permission | None = None,
    deny: Permission | None = None,
) -> PermissionOverwrite:
    return PermissionOverwrite(
        target_id,
        DOMAIN,
        target_type,
        int(allow or 0),
        int(deny or 0),
    )


def test_owner_and_administrator_bypass_overwrites() -> None:
    deny_view = [overwrite(10, "role", deny=Permission.VIEW_CHANNEL)]
    assert resolve(base=Permission(0), overwrites=deny_view, owner=True) == ALL_PERMISSIONS
    assert resolve(base=Permission.ADMINISTRATOR, overwrites=deny_view) == ALL_PERMISSIONS


def test_overwrite_order_is_everyone_roles_then_member() -> None:
    result = resolve(
        base=Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES,
        roles={(10, DOMAIN), (11, DOMAIN)},
        overwrites=[
            overwrite(10, "role", deny=Permission.SEND_MESSAGES),
            overwrite(11, "role", allow=Permission.SEND_MESSAGES),
            overwrite(22, "member", deny=Permission.SEND_MESSAGES),
        ],
    )
    assert result & Permission.VIEW_CHANNEL
    assert not result & Permission.SEND_MESSAGES


def test_implicit_masks_remove_dependent_permissions() -> None:
    without_view = resolve(base=Permission.SEND_MESSAGES | Permission.ATTACH_FILES)
    assert without_view == 0
    without_send = resolve(base=Permission.VIEW_CHANNEL | Permission.ATTACH_FILES)
    assert without_send == Permission.VIEW_CHANNEL


def test_timeout_reduces_member_to_read_only() -> None:
    result = resolve(
        base=(
            Permission.VIEW_CHANNEL
            | Permission.READ_MESSAGE_HISTORY
            | Permission.SEND_MESSAGES
            | Permission.ADD_REACTIONS
        ),
        timed_out=True,
    )
    assert result == Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY


def test_everyone_overwrite_uses_guild_domain_for_remote_member() -> None:
    result = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=int(Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES),
        overwrites=[overwrite(10, "role", deny=Permission.SEND_MESSAGES)],
        channel_type=0,
        timed_out=False,
    )
    assert result == Permission.VIEW_CHANNEL


def test_voice_everyone_deny_then_inherit_restores_connect() -> None:
    base = int(Permission.VIEW_CHANNEL | Permission.CONNECT | Permission.SPEAK)
    denied = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=base,
        overwrites=[overwrite(10, "role", deny=Permission.CONNECT)],
        channel_type=2,
        timed_out=False,
    )
    inherited = resolve_permissions(
        owner=False,
        user_id=22,
        user_domain="remote.localhost",
        everyone_role_id=10,
        everyone_role_domain=DOMAIN,
        role_ids={(10, DOMAIN)},
        base_permissions=base,
        # The middle tri-state removes the bit from both masks. The API now
        # removes the empty row, but accepting it here protects the resolver
        # while older federation events drain.
        overwrites=[overwrite(10, "role")],
        channel_type=2,
        timed_out=False,
    )

    assert denied & Permission.VIEW_CHANNEL
    assert not denied & Permission.CONNECT
    assert not denied & Permission.SPEAK
    assert inherited & Permission.CONNECT
    assert inherited & Permission.SPEAK


@pytest.mark.asyncio
async def test_timeout_denial_explains_expiry_and_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    expiry = datetime.now(UTC) + timedelta(hours=1)
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN)
    member = SimpleNamespace(
        timeout_until=expiry,
        timeout_indefinite=False,
        timeout_reason="Repeated spam",
    )
    session = AsyncMock()
    session.get.return_value = member
    monkeypatch.setattr(permission_service, "get_permissions", AsyncMock(return_value=0))

    with pytest.raises(HTTPException) as caught:
        await require_permissions(
            session,
            AsyncMock(),
            guild,
            actor,
            Permission.SEND_MESSAGES,
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {
        "code": "MEMBER_TIMED_OUT",
        "message": "You are currently timed out in this guild.",
        "timeout_until": expiry.isoformat(),
        "timeout_indefinite": False,
        "reason": "Repeated spam",
    }


@pytest.mark.asyncio
async def test_ordinary_permission_denial_does_not_leak_timeout_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guild = SimpleNamespace(id=10, origin_domain=DOMAIN)
    actor = SimpleNamespace(id=22, origin_domain=DOMAIN)
    session = AsyncMock()
    session.get.return_value = SimpleNamespace(
        timeout_until=None,
        timeout_indefinite=False,
        timeout_reason=None,
    )
    monkeypatch.setattr(permission_service, "get_permissions", AsyncMock(return_value=0))

    with pytest.raises(HTTPException) as caught:
        await require_permissions(
            session,
            AsyncMock(),
            guild,
            actor,
            Permission.SEND_MESSAGES,
        )

    assert caught.value.detail == {
        "code": "MISSING_PERMISSIONS",
        "permissions": str(int(Permission.SEND_MESSAGES)),
        "message": "You do not have permission to perform this action.",
    }
