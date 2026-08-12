from __future__ import annotations

from pathlib import Path

MIGRATION = (
    Path(__file__).parents[1] / "migrations/versions/c31f6a8e2d94_resolve_opaque_profile_handles.py"
)


def test_profile_resolution_migration_is_on_the_current_upgrade_chain() -> None:
    source = MIGRATION.read_text()
    assert 'revision: str = "c31f6a8e2d94"' in source
    assert 'down_revision: str | None = "b72c9e4a1f63"' in source


def test_profile_resolution_trigger_allows_only_one_safe_remote_transition() -> None:
    source = MIGRATION.read_text()
    assert "NEW.id IS DISTINCT FROM OLD.id" in source
    assert "NEW.origin_domain IS DISTINCT FROM OLD.origin_domain" in source
    assert "NEW.is_local IS DISTINCT FROM OLD.is_local" in source
    assert "NOT OLD.is_local" in source
    assert "NOT OLD.profile_resolved" in source
    assert "NEW.profile_resolved" in source
    assert "left(OLD.username, 8) = 'history_'" in source
    assert "NEW.username IS DISTINCT FROM OLD.username" in source
    assert (
        "BEFORE UPDATE OF id, origin_domain, is_local, username, profile_resolved ON users"
        in source
    )
