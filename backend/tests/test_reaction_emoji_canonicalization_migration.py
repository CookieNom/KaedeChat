from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace
from typing import Any

import pytest

migration = import_module("migrations.versions.3d9a5e1c7b42_reaction_emoji_canonicalization")


class FakeResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.rows)


class FakeConnection:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        history_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows
        self.history_rows = history_rows or []
        self.updates: list[dict[str, Any]] = []
        self.history_updates: list[dict[str, Any]] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        if statement is migration.FORUM_DEFAULT_SELECT_SQL:
            return FakeResult(self.rows)
        if statement is migration.STAGED_HISTORY_SELECT_SQL:
            return FakeResult(self.history_rows)
        assert isinstance(parameters, list)
        if statement is migration.FORUM_DEFAULT_UPDATE_SQL:
            self.updates.extend(parameters)
        else:
            assert statement is migration.STAGED_HISTORY_UPDATE_SQL
            self.history_updates.extend(parameters)
        return FakeResult([])


@pytest.mark.parametrize(
    ("emoji", "expected"),
    [("🏮", "🏮"), ("❤", "❤"), ("1⃣", "1⃣"), ("👨‍👩‍👧‍👦", "👨‍👩‍👧‍👦"), ("❤️", "❤"), ("1️⃣", "1⃣")],
)
def test_frozen_forum_canonicalizer_matches_historical_vectors(emoji: str, expected: str) -> None:
    assert migration._canonical_unicode_reaction_emoji(emoji) == expected  # noqa: SLF001


def test_frozen_forum_canonicalizer_strips_variation_selectors() -> None:
    assert migration._canonical_unicode_reaction_emoji("❤️") == "❤"  # noqa: SLF001
    assert migration._canonical_unicode_reaction_emoji("1️⃣") == "1⃣"  # noqa: SLF001


def test_frozen_general_canonicalizer_qualifies_custom_domain_aliases() -> None:
    assert (
        migration._canonical_reaction_emoji(  # noqa: SLF001
            "<:lantern:7@HOME.EXAMPLE.>"
        )
        == "<:lantern:7@home.example>"
    )


@pytest.mark.parametrize(
    "emoji",
    [
        "lantern",
        "🏮🔥",
        "\ufe0f",
        "<:lantern:7@home.example>",
    ],
)
def test_frozen_forum_canonicalizer_rejects_invalid_unicode_defaults(emoji: str) -> None:
    with pytest.raises(ValueError, match="exactly one valid emoji"):
        migration._canonical_unicode_reaction_emoji(emoji)  # noqa: SLF001


def test_forum_default_repair_normalizes_or_clears_only_the_unicode_branch() -> None:
    connection = FakeConnection(
        [
            {
                "id": 1,
                "origin_domain": "home.example",
                "default_reaction_emoji": {
                    "emoji_id": None,
                    "emoji_name": "❤️",
                    "extension": True,
                },
            },
            {
                "id": 2,
                "origin_domain": "home.example",
                "default_reaction_emoji": {"emoji_id": None, "emoji_name": "❤"},
            },
            {
                "id": 3,
                "origin_domain": "home.example",
                "default_reaction_emoji": {"emoji_id": None, "emoji_name": "lantern"},
            },
            {
                "id": 4,
                "origin_domain": "home.example",
                "default_reaction_emoji": {"emoji_id": None, "emoji_name": "🏮🔥"},
            },
            {
                "id": 5,
                "origin_domain": "home.example",
                "default_reaction_emoji": {"emoji_id": "7", "emoji_name": None},
            },
            {
                "id": 6,
                "origin_domain": "home.example",
                "default_reaction_emoji": {
                    "emoji_id": None,
                    "emoji_name": "<:lantern:7@home.example>",
                },
            },
        ]
    )

    migration._canonicalize_forum_defaults(connection)  # type: ignore[arg-type]  # noqa: SLF001

    updates = {item["channel_id"]: item["payload"] for item in connection.updates}
    assert set(updates) == {1, 3, 4, 6}
    assert json.loads(updates[1]) == {
        "emoji_id": None,
        "emoji_name": "❤",
        "extension": True,
    }
    assert updates[3] is None
    assert updates[4] is None
    assert updates[6] is None


def test_staged_history_repair_canonicalizes_deduplicates_and_drops_invalid_reactions() -> None:
    later = "2026-08-29T12:00:00+00:00"
    earlier = "2026-08-29T11:00:00+00:00"
    connection = FakeConnection(
        [],
        [
            {
                "export_id": 1,
                "export_domain": "guild.example",
                "message_id": 2,
                "message_domain": "guild.example",
                "payload": {
                    "id": "2",
                    "reactions": [
                        {
                            "user_id": "10",
                            "user_domain": "users.example",
                            "emoji": "❤️",
                            "created_at": later,
                        },
                        {
                            "user_id": "10",
                            "user_domain": "users.example",
                            "emoji": "❤",
                            "created_at": earlier,
                        },
                        {
                            "user_id": "11",
                            "user_domain": "users.example",
                            "emoji": "<:lantern:7@HOME.EXAMPLE.>",
                            "created_at": later,
                        },
                        {
                            "user_id": "12",
                            "user_domain": "users.example",
                            "emoji": "lantern",
                            "created_at": later,
                        },
                        {
                            "user_id": "13",
                            "user_domain": "users.example",
                            "emoji": "🏮🔥",
                            "created_at": later,
                        },
                    ],
                },
            }
        ],
    )

    migration._canonicalize_staged_history_reactions(  # type: ignore[arg-type]  # noqa: SLF001
        connection
    )

    assert len(connection.history_updates) == 1
    payload = json.loads(connection.history_updates[0]["payload"])
    assert payload["id"] == "2"
    assert payload["reactions"] == [
        {
            "user_id": "10",
            "user_domain": "users.example",
            "emoji": "❤",
            "created_at": earlier,
        },
        {
            "user_id": "11",
            "user_domain": "users.example",
            "emoji": "<:lantern:7@home.example>",
            "created_at": later,
        },
    ]


def test_upgrade_merges_before_deleting_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    connection = FakeConnection([])
    monkeypatch.setattr(
        migration,
        "op",
        SimpleNamespace(
            execute=lambda sql: calls.append(sql),
            get_bind=lambda: connection,
        ),
    )

    migration.upgrade()

    assert (
        calls.index(migration.REACTION_MAPPING_SQL)
        < calls.index(migration.REACTION_MERGE_SQL)
        < calls.index(migration.REACTION_DELETE_LEGACY_SQL)
    )
