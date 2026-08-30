from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from migrations.versions import d73c8a1f4b20_bot_installation_application_fk as migration


def test_upgrade_fails_closed_on_orphans_before_creating_the_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    execute = Mock(side_effect=lambda _sql: order.append("preflight"))
    create_foreign_key = Mock(side_effect=lambda *args, **kwargs: order.append("constraint"))
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(execute=execute, create_foreign_key=create_foreign_key),
    )

    migration.upgrade()

    execute.assert_called_once_with(migration.ORPHAN_PREFLIGHT_SQL)
    sql = migration.ORPHAN_PREFLIGHT_SQL
    assert "LEFT JOIN bot_applications" in sql
    assert "WHERE application.id IS NULL" in sql
    assert "orphan_count > 0" in sql
    assert "ERRCODE = '23503'" in sql
    create_foreign_key.assert_called_once_with(
        migration.CONSTRAINT_NAME,
        "bot_installations",
        "bot_applications",
        ["application_id", "application_domain"],
        ["id", "origin_domain"],
    )
    assert order == ["preflight", "constraint"]


def test_downgrade_removes_only_the_application_constraint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drop_constraint = Mock()
    monkeypatch.setattr(migration, "op", SimpleNamespace(drop_constraint=drop_constraint))

    migration.downgrade()

    drop_constraint.assert_called_once_with(
        migration.CONSTRAINT_NAME,
        "bot_installations",
        type_="foreignkey",
    )
