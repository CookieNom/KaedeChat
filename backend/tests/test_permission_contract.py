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
