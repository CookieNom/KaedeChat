import pytest

from app.core.permission_contract import PERMISSION_CONTRACT, required_permissions
from app.core.permissions import ALL_PERMISSIONS, PERMISSION_METADATA, Permission


def test_permission_metadata_is_total_and_internally_consistent() -> None:
    assert {item.permission for item in PERMISSION_METADATA} == set(Permission)
    for item in PERMISSION_METADATA:
        assert item.resource_scopes
        assert item.permission.value & ~ALL_PERMISSIONS == 0
        assert all(dependency in Permission for dependency in item.dependencies)
        assert item.danger in {"normal", "elevated", "dangerous", "critical"}


def test_endpoint_permission_contract_is_unique_known_and_nonempty() -> None:
    assert len(PERMISSION_CONTRACT) >= 30
    for operation, contract in PERMISSION_CONTRACT.items():
        assert operation == contract.operation
        assert contract.scope in {"guild", "channel"}
        assert contract.permission
        assert int(contract.permission) & ~ALL_PERMISSIONS == 0
        assert required_permissions(operation) == contract.permission
    with pytest.raises(RuntimeError, match="unknown permission contract"):
        required_permissions("unregistered.operation")


def test_federated_instance_bans_use_a_dedicated_critical_permission() -> None:
    assert required_permissions("instance_ban.list") == Permission.BAN_INSTANCES
    assert required_permissions("instance_ban.put") == Permission.BAN_INSTANCES
    assert required_permissions("instance_ban.remove") == Permission.BAN_INSTANCES
    metadata = next(
        item for item in PERMISSION_METADATA if item.permission == Permission.BAN_INSTANCES
    )
    assert metadata.resource_scopes == ("guild",)
    assert metadata.danger == "critical"


def test_thread_and_forum_permission_bits_are_stable() -> None:
    assert Permission.USE_APPLICATION_COMMANDS == 1 << 32
    assert Permission.MANAGE_THREADS == 1 << 34
    assert Permission.CREATE_PUBLIC_THREADS == 1 << 35
    assert Permission.CREATE_PRIVATE_THREADS == 1 << 36
    assert Permission.SEND_MESSAGES_IN_THREADS == 1 << 38
    assert Permission.PIN_MESSAGES == 1 << 51
    assert Permission.BYPASS_SLOWMODE == 1 << 52
    # Existing persisted assignments must never move to make room for parity.
    assert Permission.STREAM == 1 << 31
    assert Permission.BAN_INSTANCES == 1 << 41


def test_thread_operations_use_their_dedicated_permissions() -> None:
    assert required_permissions("forum.post.create") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES
    )
    assert required_permissions("thread.create.public") == (
        Permission.VIEW_CHANNEL | Permission.CREATE_PUBLIC_THREADS
    )
    assert required_permissions("thread.create.private") == (
        Permission.VIEW_CHANNEL | Permission.CREATE_PRIVATE_THREADS
    )
    assert required_permissions("thread.message.create") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
    )
    assert required_permissions("thread.update.other") == (
        Permission.VIEW_CHANNEL | Permission.MANAGE_THREADS
    )
    assert required_permissions("thread.member.join") == Permission.VIEW_CHANNEL
    assert required_permissions("thread.member.add") == (
        Permission.VIEW_CHANNEL | Permission.SEND_MESSAGES_IN_THREADS
    )
    assert required_permissions("pin.update") == (
        Permission.VIEW_CHANNEL | Permission.READ_MESSAGE_HISTORY | Permission.PIN_MESSAGES
    )
    assert required_permissions("application.command.use") == (
        Permission.VIEW_CHANNEL | Permission.USE_APPLICATION_COMMANDS
    )
