from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import Client
from kaede_bot.models import Channel, Role
from kaede_bot.refs import EntityRef
from kaede_bot.state import WorkerState


GUILD = EntityRef(10, "guild.example")
CHANNEL = EntityRef(20, "guild.example")
ROLE = EntityRef(30, "guild.example")
USER = EntityRef(40, "people.example")
TARGET = "https://guild.example"
AUDIT_HEADERS = {"X-Audit-Log-Reason": "routine cleanup"}


class RequestObserved(Exception):
    pass


def client(*, e2ee: bool = False) -> Client:
    return Client(
        worker_state=WorkerState(
            EntityRef(1, "apps.example"),
            2,
            Ed25519PrivateKey.generate(),
            "test",
        ),
        e2ee_device_id=("kbe_" + "a" * 43) if e2ee else None,
    )


ManagementCall = Callable[[Client], Awaitable[object]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda bot: bot.edit_guild(
                GUILD,
                target=TARGET,
                version="guild-v1",
                reason=" routine cleanup ",
                name="Renamed",
            ),
            id="guild-update",
        ),
        pytest.param(
            lambda bot: bot.edit_channel(
                GUILD,
                CHANNEL,
                target=TARGET,
                version="channel-v1",
                reason=" routine cleanup ",
                name="renamed",
            ),
            id="channel-update",
        ),
        pytest.param(
            lambda bot: bot.edit_role(
                GUILD,
                ROLE,
                target=TARGET,
                version="role-v1",
                reason=" routine cleanup ",
                name="renamed",
            ),
            id="role-update",
        ),
    ],
)
async def test_versioned_management_merges_audit_and_if_match_headers(
    invoke: ManagementCall,
) -> None:
    bot = client()
    bot.request = AsyncMock(side_effect=RequestObserved)  # type: ignore[method-assign]

    with pytest.raises(RequestObserved):
        await invoke(bot)

    headers = bot.request.await_args.kwargs["headers"]
    assert headers["X-Audit-Log-Reason"] == "routine cleanup"
    assert set(headers) == {"If-Match", "X-Audit-Log-Reason"}


@pytest.mark.asyncio
async def test_channel_reorder_uses_partial_discord_payload_and_empty_response() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]
    second = EntityRef(21, "guild.example")

    result = await bot.reorder_channels(
        GUILD,
        [(CHANNEL, 3), (second, 3, 30, True, 16)],
        target=TARGET,
    )

    assert result is None
    assert bot.request.await_args.kwargs["json"] == {
        "channels": [
            {"id": "20", "position": 3},
            {
                "id": "21",
                "position": 3,
                "parent_id": "30",
                "lock_permissions": True,
                "flags": 16,
            },
        ]
    }

    await bot.reorder_channels(GUILD, [(CHANNEL, 2, None)], target=TARGET)
    assert bot.request.await_args.kwargs["json"] == {
        "channels": [{"id": "20", "position": 2, "parent_id": None}]
    }


@pytest.mark.asyncio
async def test_channel_reorder_leaves_actual_parent_change_validation_to_server() -> (
    None
):
    bot = client()
    bot.request = AsyncMock(return_value=None)  # type: ignore[method-assign]

    await bot.reorder_channels(
        GUILD,
        [(CHANNEL, None, None, None, None), (EntityRef(21, "guild.example"), 1, 30)],
        target=TARGET,
    )

    assert bot.request.await_args.kwargs["json"] == {
        "channels": [
            {
                "id": "20",
                "position": None,
                "parent_id": None,
                "lock_permissions": None,
                "flags": None,
            },
            {"id": "21", "position": 1, "parent_id": "30"},
        ]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda bot: bot.create_channel(
                GUILD, "general", target=TARGET, reason=" routine cleanup "
            ),
            id="channel-create",
        ),
        pytest.param(
            lambda bot: bot.reorder_channels(
                GUILD,
                [(CHANNEL, 0, None, False)],
                target=TARGET,
                reason=" routine cleanup ",
            ),
            id="channel-reorder",
        ),
        pytest.param(
            lambda bot: bot.delete_channel(
                GUILD, CHANNEL, target=TARGET, reason=" routine cleanup "
            ),
            id="channel-delete",
        ),
        pytest.param(
            lambda bot: bot.create_role(
                GUILD, "Member", target=TARGET, reason=" routine cleanup "
            ),
            id="role-create",
        ),
        pytest.param(
            lambda bot: bot.reorder_roles(
                GUILD,
                [
                    (
                        Role(
                            client=bot,
                            target=TARGET,
                            ref=ROLE,
                            guild_ref=GUILD,
                            name="Member",
                            color=0,
                            permissions=0,
                            position=1,
                            version="role-v1",
                        ),
                        1,
                    )
                ],
                target=TARGET,
                reason=" routine cleanup ",
            ),
            id="role-reorder",
        ),
        pytest.param(
            lambda bot: bot.delete_role(
                GUILD, ROLE, target=TARGET, reason=" routine cleanup "
            ),
            id="role-delete",
        ),
        pytest.param(
            lambda bot: bot.add_member_role(
                GUILD, USER, ROLE, target=TARGET, reason=" routine cleanup "
            ),
            id="member-role-add",
        ),
        pytest.param(
            lambda bot: bot.remove_member_role(
                GUILD, USER, ROLE, target=TARGET, reason=" routine cleanup "
            ),
            id="member-role-remove",
        ),
        pytest.param(
            lambda bot: bot.set_member_roles(
                GUILD, USER, [ROLE], target=TARGET, reason=" routine cleanup "
            ),
            id="member-role-set",
        ),
        pytest.param(
            lambda bot: bot.update_invite_target_users(
                GUILD,
                "Abcd1234",
                [USER],
                target=TARGET,
                reason=" routine cleanup ",
            ),
            id="invite-target-users",
        ),
    ],
)
async def test_management_methods_send_normalized_audit_header(
    invoke: ManagementCall,
) -> None:
    bot = client()
    bot.request = AsyncMock(side_effect=RequestObserved)  # type: ignore[method-assign]

    with pytest.raises(RequestObserved):
        await invoke(bot)

    assert bot.request.await_args.kwargs["headers"] == AUDIT_HEADERS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(
            lambda bot: bot.start_thread(
                CHANNEL,
                "discussion",
                target=TARGET,
                installation_id=77,
                reason=" routine cleanup ",
            ),
            id="thread-create",
        ),
        pytest.param(
            lambda bot: bot.start_thread_from_message(
                CHANNEL,
                EntityRef(21, "guild.example"),
                "discussion",
                target=TARGET,
                installation_id=77,
                reason=" routine cleanup ",
            ),
            id="thread-create-from-message",
        ),
        pytest.param(
            lambda bot: bot.edit_thread(
                CHANNEL,
                target=TARGET,
                installation_id=77,
                reason=" routine cleanup ",
                name="renamed",
            ),
            id="thread-update",
        ),
        pytest.param(
            lambda bot: bot.delete_thread(
                CHANNEL,
                target=TARGET,
                installation_id=77,
                reason=" routine cleanup ",
            ),
            id="thread-delete",
        ),
    ],
)
async def test_thread_audit_header_preserves_runtime_and_e2ee_headers(
    invoke: ManagementCall,
) -> None:
    bot = client(e2ee=True)
    bot.request = AsyncMock(side_effect=RequestObserved)  # type: ignore[method-assign]

    with pytest.raises(RequestObserved):
        await invoke(bot)

    assert bot.request.await_args.kwargs["headers"] == {
        "X-Kaede-Bot-Installation": "77",
        "X-Kaede-E2EE-Device": "kbe_" + "a" * 43,
        **AUDIT_HEADERS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [False, True])
async def test_channel_delete_returns_the_typed_deleted_channel(thread: bool) -> None:
    bot = client()
    payload = {
        "id": "20",
        "origin_domain": "guild.example",
        "guild_id": "10",
        "guild_domain": "guild.example",
        "type": 11 if thread else 0,
        "name": "deleted",
        "parent_id": "19" if thread else None,
        "parent_domain": "guild.example" if thread else None,
    }
    bot.request = AsyncMock(return_value=payload)  # type: ignore[method-assign]

    deleted = (
        await bot.delete_thread(CHANNEL, target=TARGET)
        if thread
        else await bot.delete_channel(GUILD, CHANNEL, target=TARGET)
    )

    assert isinstance(deleted, Channel)
    assert deleted.ref == CHANNEL


@pytest.mark.asyncio
async def test_audit_reason_rejects_more_than_512_characters() -> None:
    bot = client()

    with pytest.raises(ValueError, match="cannot exceed 512"):
        await bot.create_channel(GUILD, "general", reason="x" * 513)
