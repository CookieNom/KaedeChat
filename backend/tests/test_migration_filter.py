from dataclasses import dataclass

from app.db.migration_filter import references_message_partition


@dataclass
class FakeForeignKey:
    target_fullname: str


@dataclass
class FakeConstraint:
    elements: tuple[FakeForeignKey, ...]
    table: object | None = None


def test_reflected_partition_foreign_keys_are_ignored() -> None:
    constraint = FakeConstraint((FakeForeignKey("public.messages_2026_07.id"),))
    assert references_message_partition(constraint)


def test_parent_message_foreign_key_is_compared() -> None:
    constraint = FakeConstraint((FakeForeignKey("public.messages.id"),))
    assert not references_message_partition(constraint)
