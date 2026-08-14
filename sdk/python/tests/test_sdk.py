import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kaede_bot.client import canonical_target_origin
from kaede_bot.refs import EntityRef, User
from kaede_bot.state import WorkerState


def test_entity_ref_and_human_handle_are_distinct() -> None:
    ref = EntityRef.parse("123@chat.example")
    user = User(ref, "alice", "Alice")
    assert str(ref) == "123@chat.example"
    assert user.handle == "alice@chat.example"
    assert user.mention == "<@123@chat.example>"


@pytest.mark.parametrize(
    "value",
    ["alice@chat.example", "1@Chat.example", "1@chat.example.", "-1@chat.example"],
)
def test_entity_ref_rejects_usernames_and_noncanonical_domains(value: str) -> None:
    with pytest.raises(ValueError):
        EntityRef.parse(value)


def test_worker_state_round_trip_uses_private_permissions(tmp_path: Path) -> None:
    root = tmp_path / "state"
    state = WorkerState(
        EntityRef(1, "apps.example"), 2, Ed25519PrivateKey.generate(), "production"
    )
    state.save(root)
    assert root.stat().st_mode & 0o077 == 0
    assert (root / "worker.json").stat().st_mode & 0o077 == 0
    loaded = WorkerState.load(root)
    assert loaded.application_ref == state.application_ref
    assert loaded.worker_id == state.worker_id
    assert loaded.public_key == state.public_key


def test_worker_state_refuses_shared_directory(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir(mode=0o755)
    os.chmod(root, 0o755)
    state = WorkerState(
        EntityRef(1, "apps.example"), 2, Ed25519PrivateKey.generate(), "production"
    )
    with pytest.raises(PermissionError):
        state.save(root)


def test_bot_target_origins_are_canonical_and_safe() -> None:
    assert canonical_target_origin("https://CHAT.Example/") == "https://chat.example"
    assert canonical_target_origin("https://chat.example:443") == "https://chat.example"
    assert (
        canonical_target_origin("https://chat.example:8443")
        == "https://chat.example:8443"
    )
    for value in (
        "http://chat.example",
        "https://user@chat.example",
        "https://chat.example/api",
        "https://chat.example?target=other",
        "https://chat.example#fragment",
        "https://chat.example.",
        "https://chat.example:invalid",
    ):
        with pytest.raises(ValueError, match="canonical HTTPS origins"):
            canonical_target_origin(value)
