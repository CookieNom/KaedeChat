from dataclasses import dataclass

import pytest

from app.db.migration_filter import references_message_partition


@dataclass
class FakeForeignKey:
    target_fullname: str


@dataclass
class FakeConstraint:
    elements: tuple[FakeForeignKey, ...]
    table: object | None = None


@pytest.mark.parametrize(
    "target,ignored",
    [("public.messages_2026_07.id", True), ("public.messages.id", False)],
    ids=["partition", "parent"],
)
def test_reflected_partition_foreign_keys_are_ignored(target: str, ignored: bool) -> None:
    constraint = FakeConstraint((FakeForeignKey(target),))
    assert references_message_partition(constraint) is ignored


def test_migration_graph_has_one_head_and_preserves_historical_ancestry() -> None:
    from pathlib import Path

    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(str(Path(__file__).resolve().parents[1] / "migrations"))
    assert len(scripts.get_heads()) == 1
    revisions = {revision.revision: revision for revision in scripts.walk_revisions()}
    for revision, parent in {
        "2c8f4d0b6e31": "1b7e3c9a5d20",
        "c31f6a8e2d94": "b72c9e4a1f63",
        "3d9a5e1c7b42": "2c8f4d0b6e31",
        "4ea6c2d8f953": "3d9a5e1c7b42",
    }.items():
        assert revisions[revision].down_revision == parent
