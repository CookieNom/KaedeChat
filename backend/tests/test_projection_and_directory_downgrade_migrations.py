from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from migrations.versions import e84f1a2c7d30_application_directory as directory_migration
from migrations.versions import (
    f95b2c3d8e41_developer_team_snapshot_highwaters as projection_migration,
)


@pytest.mark.parametrize(
    ("migration", "guard", "first_destructive_operation"),
    (
        (
            directory_migration,
            directory_migration.DIRECTORY_DOWNGRADE_PREFLIGHT_SQL,
            "drop_index",
        ),
        (
            projection_migration,
            projection_migration.PROJECTION_DOWNGRADE_PREFLIGHT_SQL,
            "drop_index",
        ),
    ),
)
def test_lossy_downgrades_run_preflight_before_schema_changes(
    monkeypatch: pytest.MonkeyPatch,
    migration: object,
    guard: str,
    first_destructive_operation: str,
) -> None:
    order: list[str] = []

    def record(name: str):
        return Mock(side_effect=lambda *args, **kwargs: order.append(name))

    operations = SimpleNamespace(
        execute=record("execute"),
        drop_index=record("drop_index"),
        drop_constraint=record("drop_constraint"),
        drop_table=record("drop_table"),
        drop_column=record("drop_column"),
        f=lambda name: name,
    )
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()  # type: ignore[attr-defined]

    operations.execute.assert_called_once_with(guard)
    assert order[:2] == ["execute", first_destructive_operation]


def test_directory_downgrade_guard_covers_every_added_listing_field() -> None:
    guard = directory_migration.DIRECTORY_DOWNGRADE_PREFLIGHT_SQL

    for column in (
        "directory_enabled",
        "directory_approved",
        "directory_summary",
        "directory_category",
        "directory_tags",
        "directory_collections",
        "directory_media",
        "directory_external_links",
        "directory_supported_locales",
        "directory_description_localizations",
        "banner_hash",
        "terms_url",
    ):
        assert f"application.{column}" in guard
    assert "ERRCODE = '23514'" in guard


def test_projection_downgrade_guard_retains_security_highwaters_and_leases() -> None:
    guard = projection_migration.PROJECTION_DOWNGRADE_PREFLIGHT_SQL

    assert "authority_expires_at IS NOT NULL" in guard
    assert "federation_metadata_fingerprint IS NOT NULL" in guard
    assert "federation_applications_fingerprint IS NOT NULL" in guard
    assert "developer_team_member_highwaters" in guard
    assert "ERRCODE = '23514'" in guard
