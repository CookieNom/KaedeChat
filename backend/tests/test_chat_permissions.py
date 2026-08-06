from app.chat.permissions import PermissionOverwrite, resolve_permissions
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
